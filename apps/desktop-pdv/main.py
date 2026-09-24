"""Bootstrap do PDV de Balcão.

Ordem de inicialização (importa):

1. Banco local + migrations — antes de qualquer serviço, porque o resto depende dele.
2. Verificação da cadeia de auditoria — se o ledger foi adulterado, queremos
   saber na abertura do caixa, não no fechamento.
3. Periféricos — balança em thread própria, impressora em fila própria.
4. Servidor local — o PDV vira o servidor do salão para o app do garçom e
   o KDS. Sobe ANTES da UI e, se falhar, apenas registra: o salão fica sem
   servidor, mas o balcão continua vendendo.
5. Sincronização — sobe **depois** da janela, em thread própria. Só é montada
   se o terminal estiver ativado; sem token, a fila continua enchendo em disco
   e sobe inteira no primeiro ciclo depois da ativação.
6. UI — por último, já com tudo pronto.

Tudo entre os passos 3 e 5 é opcional por construção. Nenhum deles pode
impedir a abertura do caixa: periférico, rede e nuvem falham em loja o tempo
todo, e a venda é a única coisa que não pode parar.

Uso::

    python main.py                                   # balança simulada
    PDV_EDGE=0 python main.py                        # sem servidor do salão
    PDV_SYNC=0 python main.py                        # sem sincronização
    PDV_SCALE_PROTOCOL=toledo_prix3 PDV_SCALE_PORT=COM3 \\
    PDV_PRINTER_BACKEND=win32raw python main.py      # hardware real
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from pdv.config import AppConfig, installed_data_dir
from pdv.data.database import Database
from pdv.data.repositories import OutboxRepository
from pdv.data.seed import seed_demo_data
from pdv.data.settings import SettingsStore
from pdv.domain.errors import AuditChainError
from pdv.domain.models import Cents, EntityId
from pdv.edge.worker import EdgeServer
from pdv.hardware.printer.backends import PrintService, build_printer
from pdv.hardware.scale.serial_scale import build_scale
from pdv.hardware.scale.worker import ScaleService
from pdv.provisioning.activation import (
    HttpActivationTransport,
    activate,
    load_sync_token,
)
from pdv.provisioning.secrets import SecretVault
from pdv.provisioning.staging import (
    StagedActivationError,
    promote_staged_activation,
    staged_path,
)
from pdv.remote.commands import RemoteCommandService
from pdv.services.audit import AuditService
from pdv.services.authorization import AuthorizationService
from pdv.services.cash_session import CashSessionError, CashSessionService
from pdv.services.checkout import CheckoutService
from pdv.sync.engine import SyncEngine
from pdv.sync.transport import HttpTransport
from pdv.sync.worker import SyncService
from pdv.ui.counter_window import CounterWindow
from pdv.ui.login_dialog import LoginDialog
from pdv.ui.brand import app_icon
from pdv.ui.theme import apply_theme

logger = logging.getLogger(__name__)


def verify_audit_integrity(database: Database, config: AppConfig) -> str | None:
    """Revalida a cadeia do ledger. Devolve a mensagem de erro, se houver."""
    audit = AuditService(
        tenant_id=EntityId(config.tenant_id),
        store_id=EntityId(config.store_id),
        device_id=EntityId(config.device_id),
        outbox=OutboxRepository(),
        device_secret=config.device_secret,
    )
    try:
        audit.verify_chain(database.connection)
    except AuditChainError as exc:
        return str(exc)
    return None


def build_sync(
    database: Database,
    config: AppConfig,
    checkout: CheckoutService,
    commands: RemoteCommandService | None = None,
) -> SyncService | None:
    """Monta o serviço de sincronização, se o terminal estiver ativado.

    Sem token não há o que montar: um terminal recém-instalado e ainda não
    ativado não tem para onde enviar. A fila continua enchendo em disco e sobe
    inteira no primeiro ciclo depois da ativação — é para isso que o outbox é
    durável.

    Devolve `None` em qualquer falha de montagem. O motivo é o de sempre nesta
    base: **o balcão continua vendendo**. Sincronização é o que protege a venda
    de ser adulterada depois, não o que permite fazê-la.
    """
    if os.getenv("PDV_SYNC", "1") == "0":
        return None

    try:
        token = load_sync_token(SecretVault(config.database_path.parent / "secrets"))
    except Exception:  # noqa: BLE001
        logger.exception("Não foi possível ler o token de sincronização")
        return None

    if not token:
        logger.info("Terminal ainda não ativado — a fila sobe após a ativação.")
        return None

    # Token sem ativação gravada no banco é o intervalo entre ativar pelo caixa
    # e reiniciar (`pdv.provisioning.staging`): o token já é da loja real, e o
    # banco aberto ainda é o de DEMONSTRAÇÃO. Sincronizar aqui mandaria as
    # vendas de teste para a loja.
    if not SettingsStore(database).load().activated:
        logger.info("Token presente, mas este banco não está ativado — sem sync.")
        return None

    engine = SyncEngine(
        database,
        HttpTransport(config.cloud_base_url, token),
        config,
        # É esta linha que liga o canal de comando do painel. Sem ela o
        # terminal só envia, como era até a Fase 3.5 — e continua funcionando.
        commands=commands or RemoteCommandService(database, config, checkout=checkout),
    )
    return SyncService(engine)


class StartupError(Exception):
    """O PDV não tem como abrir. A mensagem é para quem está no balcão."""


def build_config() -> AppConfig:
    """A configuração com que o caixa abre.

    Rodando do código-fonte, é o `AppConfig.from_env()` de sempre. Instalado,
    tudo sai da pasta que o `PDVSetup.exe` provisionou (ver
    `pdv.config.installed_data_dir`): o banco, o segredo do terminal no cofre
    DPAPI e a pasta de cupons. Periféricos e ativação vêm depois, do próprio
    banco (`_apply_device_settings`).
    """
    config = AppConfig.from_env()
    data_dir = installed_data_dir()
    if data_dir is None:
        return config

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StartupError(
            f"Não foi possível acessar a pasta de dados do PDV:\n{data_dir}\n\n"
            "Rode o instalador de novo como administrador."
        ) from exc

    return replace(
        config,
        database_path=data_dir / "pdv_local.db",
        device_secret=_device_secret(SecretVault(data_dir / "secrets")),
        printer=replace(config.printer, output_dir=data_dir / "cupons"),
    )


def _device_secret(vault: SecretVault) -> bytes:
    """O segredo do terminal, criado na primeira execução se ainda não existir.

    Segredo que EXISTE e não decifra não é recriado. Recriar faria a cadeia de
    auditoria inteira, assinada com o segredo antigo, passar a acusar
    adulteração — e as vendas ainda na fila subiriam com HMAC que a nuvem
    recusa. É caso de suporte, não de improviso.
    """
    if vault.exists("device_secret"):
        secret = vault.load("device_secret")
        if secret is None:
            raise StartupError(
                "O segredo deste terminal existe mas não pôde ser lido.\n\n"
                "Não reinstale nem apague a pasta de dados: chame o suporte."
            )
        return secret
    return vault.ensure_device_secret()


def _apply_device_settings(database: Database, config: AppConfig) -> AppConfig:
    """Periféricos detectados e identidade da ativação vencem o padrão do código."""
    return SettingsStore(database).apply_to(config)


def open_database(config: AppConfig) -> tuple[Database, AppConfig]:
    """Abre e migra o banco, e aplica o que o instalador gravou nele."""
    try:
        archived = promote_staged_activation(config.database_path)
    except StagedActivationError as exc:
        raise StartupError(str(exc)) from exc
    if archived is not None:
        logger.info("Ativação concluída; demonstração arquivada em %s", archived)

    database = Database(config.database_path)
    try:
        database.migrate()
        config = _apply_device_settings(database, config)

        # Demonstração só em terminal NÃO ativado. Ativado, os usuários e o
        # catálogo descem da nuvem; semear aqui criaria, dentro da loja real, os
        # logins de demonstração com PINs publicados no README — um caixa que
        # qualquer um abre.
        if not SettingsStore(database).load().activated:
            seed_demo_data(database, config)
    except sqlite3.Error as exc:
        raise StartupError(database_problem(config.database_path, exc)) from exc
    return database, config


#: Grupo "Usuários" pelo SID: o nome muda com o idioma do Windows ("Users",
#: "Usuários"). Foi um nome em inglês que quebrou o endurecimento das
#: permissões no Windows em português.
USERS_SID = "*S-1-5-32-545"


def database_problem(path: Path, exc: sqlite3.Error) -> str:
    """A mensagem para o balcão quando o banco não abre — com o remédio.

    "attempt to write a readonly database" já aconteceu em campo: o instalador
    (elevado) cria o banco, o endurecimento das permissões falha em silêncio, e
    o caixa — que roda como usuário comum — só consegue ler. Sem esta tradução
    o lojista via a frase do SQLite e mais nada.
    """
    text = str(exc).lower()
    if "readonly" in text or "read-only" in text:
        folder = path.parent
        return (
            "O Windows não está deixando o PDV gravar as vendas nesta pasta:\n"
            f"{folder}\n\n"
            "As permissões da instalação ficaram incompletas. Para corrigir, "
            "reinstale o PDV (o instalador refaz as permissões) ou peça a um "
            "administrador para rodar, no PowerShell como Administrador:\n\n"
            f'icacls "{folder}" /grant "{USERS_SID}:(OI)(CI)M" /T\n\n'
            "Nenhuma venda foi perdida: o banco está intacto, só sem permissão "
            "de escrita."
        )
    return f"Não foi possível abrir o banco de dados do PDV:\n{path}\n\n{exc}"


def _has_store_users(database: Database, config: AppConfig) -> bool:
    row = database.query_one(
        "SELECT 1 FROM users WHERE tenant_id = ? AND is_active = 1 LIMIT 1",
        (config.tenant_id,),
    )
    return row is not None


def ensure_store_users(
    database: Database,
    config: AppConfig,
    *,
    transport=None,  # noqa: ANN001 - Transport | None
    ask_retry=None,  # noqa: ANN001 - Callable[[], bool] | None
    run=None,  # noqa: ANN001 - Callable[[str, Callable], object] | None
) -> bool:
    """Terminal ativado e sem usuários: baixa o cadastro da loja ANTES do login.

    Ativado, o terminal não recebe os logins de demonstração — os usuários da
    loja descem da nuvem. Só que o sync só começava depois do login, e o login
    exige usuário: um terminal recém-ativado ficava sem ninguém capaz de
    entrar. Aqui o primeiro pull roda antes, com a tela avisando.

    Devolve `False` se quem está no balcão desistiu de tentar de novo.
    """
    if _has_store_users(database, config):
        return True

    if transport is None:
        token = load_sync_token(SecretVault(config.database_path.parent / "secrets"))
        if not token:
            raise StartupError(
                "Este terminal está ativado, mas a credencial de sincronização "
                "não foi encontrada. Ative o terminal de novo pelo painel."
            )
        transport = HttpTransport(config.cloud_base_url, token)

    engine = SyncEngine(database, transport, config)
    if run is None:
        from pdv.ui.busy import run_with_progress as run
    if ask_retry is None:
        ask_retry = _ask_retry_download

    while True:
        run("Baixando os usuários e o cadastro da loja…", engine.pull_once)
        if _has_store_users(database, config):
            return True
        if not ask_retry():
            return False


def _ask_retry_download() -> bool:
    answer = QMessageBox.question(
        None,
        "PDV Balcão",
        "Os usuários desta loja ainda não chegaram a este terminal.\n\n"
        "Confira a conexão com a internet e se há usuários cadastrados no "
        "painel da retaguarda.\n\nTentar de novo?",
        QMessageBox.StandardButton.Retry | QMessageBox.StandardButton.Close,
        QMessageBox.StandardButton.Retry,
    )
    return answer == QMessageBox.StandardButton.Retry


def activate_from_demo(config: AppConfig, *, parent=None) -> bool:  # noqa: ANN001
    """Ativação pedida pelo caixa em modo demonstração.

    Grava num banco à parte; a troca acontece na próxima abertura
    (`pdv.provisioning.staging`). Devolve se ativou.
    """
    from pdv.ui.activation_dialog import ActivationDialog

    staged = staged_path(config.database_path)
    _remove_database_files(staged)  # sobra de uma tentativa anterior
    stage = Database(staged)
    stage.migrate()
    vault = SecretVault(config.database_path.parent / "secrets")

    def run(api_url: str, code: str):  # noqa: ANN202
        return activate(
            code,
            database=stage,
            vault=vault,
            transport=HttpActivationTransport(api_url),
        )

    dialog = ActivationDialog(
        run,
        server_url=config.cloud_base_url,
        warning=(
            "As vendas, produtos e usuários de demonstração serão arquivados — "
            "não vão para a loja. O caixa reinicia com os dados da loja."
        ),
        parent=parent,
    )
    dialog.exec()
    stage.close()

    if dialog.result_value is None:
        _remove_database_files(staged)
        return False

    name = dialog.result_value.store_name or "a loja"
    QMessageBox.information(
        parent,
        "Terminal ativado",
        f"Terminal ativado para {name}.\n\nO PDV vai reiniciar para começar "
        "com os dados da loja.",
    )
    return True


def _remove_database_files(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            logger.warning("Não foi possível remover %s", candidate)


def _restart_application_after_exit() -> None:
    """Abre um PDV novo. Ele espera este soltar o banco antes de trocá-lo."""
    from PySide6.QtCore import QProcess

    arguments = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv
    QProcess.startDetached(sys.executable, arguments)


def _configure_logging(config: AppConfig) -> None:
    """Log em arquivo ao lado dos dados.

    O `PDV.exe` não tem console: sem arquivo, o motivo de qualquer falha some
    junto com a janela. Não conseguir abrir o log não impede o caixa de vender.
    """
    handlers: list[logging.Handler] = []
    for folder in (config.database_path.parent / "logs", config.database_path.parent):
        try:
            handlers.append(
                logging.FileHandler(folder / "pdv.log", encoding="utf-8", delay=False)
            )
            break
        except OSError:
            continue
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers or None,
    )


def main() -> int:
    # Antes de qualquer coisa, e antes do Qt: o autoteste roda dentro do
    # executável compilado e responde "este pacote está completo?". É a última
    # etapa do build, e o único jeito de pegar um `hiddenimports` faltando —
    # que produz um pacote que instala, abre, e falha na loja.
    if "--selftest" in sys.argv:
        from pdv.selftest import run_cli

        return run_cli()

    config = build_config()
    _configure_logging(config)
    logger.info("Banco local: %s", config.database_path)

    database, config = open_database(config)
    activated = SettingsStore(database).load().activated

    integrity_error = verify_audit_integrity(database, config)

    app = QApplication(sys.argv)
    app.setApplicationName("PDV Balcão")

    # Antes de qualquer janela: o tema fixa estilo, paleta e fonte. Se viesse
    # depois, o primeiro diálogo (o aviso de auditoria logo abaixo) apareceria
    # com o tema do Windows — claro numa máquina, escuro na outra.
    apply_theme(app)
    app.setWindowIcon(app_icon())

    if integrity_error is not None:
        # Não bloqueia a venda — bloquear o caixa por suspeita de fraude
        # puniria o cliente na fila. Avisa, e o servidor confirma no sync.
        QMessageBox.warning(
            None,
            "Auditoria",
            f"Integridade do ledger comprometida:\n\n{integrity_error}\n\n"
            "O servidor será notificado na próxima sincronização.",
        )

    # Login ANTES de qualquer periférico: sem alguém identificado não há caixa
    # a abrir, e subir balança e impressora para depois fechar seria só ruído
    # de porta serial no log.
    if activated and not ensure_store_users(database, config):
        logger.info("Sem usuários da loja; o PDV não abre sem operador.")
        return 0

    operator = LoginDialog.ask(
        AuthorizationService(database, config.tenant_id),
        store_name=config.store_name,
        demo_hint=not activated,
    )
    if operator is None:
        logger.info("Login cancelado; o PDV não abre sem operador identificado.")
        return 0

    logger.info("Caixa aberto por %s (%s)", operator.name, operator.role)

    cash_sessions = CashSessionService(database, config)
    open_session = cash_sessions.current()
    if open_session is not None and open_session.operator_id != EntityId(str(operator.id)):
        QMessageBox.critical(
            None,
            "Caixa em uso",
            "Há uma sessão de caixa aberta por outro operador. "
            "Entre com o operador responsável para encerrá-la.",
        )
        return 1
    if open_session is None:
        opening, accepted = QInputDialog.getDouble(
            None, "Abrir caixa", "Fundo de troco inicial (R$):",
            0.0, 0.0, 999_999.99, 2,
        )
        if not accepted:
            logger.info("Abertura de caixa cancelada; o PDV não foi iniciado.")
            return 0
        try:
            cash_sessions.open(
                operator_id=EntityId(str(operator.id)),
                opening_cents=Cents(
                    int((Decimal(str(opening)) * Decimal(100)).quantize(Decimal("1")))
                ),
            )
        except CashSessionError as exc:
            QMessageBox.critical(None, "Abrir caixa", str(exc))
            return 1

    checkout = CheckoutService(database, config)

    scale = ScaleService(build_scale(config.scale), config.scale)
    printer = PrintService(build_printer(config.printer))

    # O servidor do salão é opcional por variável de ambiente: uma loja que só
    # tem balcão não precisa abrir porta na rede, e superfície que não serve a
    # ninguém é só risco.
    edge: EdgeServer | None = None
    if os.getenv("PDV_EDGE", "1") != "0":
        edge = EdgeServer(database, config)
        if not edge.start():
            # Porta ocupada ou rede indisponível não pode impedir a venda: o
            # caixa é o negócio, o salão é um acréscimo.
            logger.error("Servidor do salão indisponível; o balcão segue operando")
            edge = None

    # Um serviço só para o ciclo de sync e para a tela: o aceite no caixa
    # precisa do mesmo barramento da cozinha que o ciclo usa.
    remote_commands = RemoteCommandService(
        database, config, checkout=checkout, hub=edge.hub if edge is not None else None
    )
    sync = build_sync(database, config, checkout, commands=remote_commands)

    window = CounterWindow(
        checkout,
        scale,
        printer,
        config,
        database,
        operator=operator,
        cash_sessions=cash_sessions,
        edge_port=edge.port if edge is not None else None,
        edge_scheme=edge.scheme if edge is not None else "http",
        edge_tls=edge.tls if edge is not None else None,
        remote_commands=remote_commands,
        on_activate=(
            None
            if activated
            else lambda parent: activate_from_demo(config, parent=parent)
        ),
    )
    window.show()
    scale.start()
    if sync is not None:
        sync.start()

    try:
        code = app.exec()
        if window.restart_requested:
            _restart_application_after_exit()
        return code
    finally:
        # Ordem do encerramento: primeiro o que fala com a rede, depois o
        # banco. Fechar o banco com o worker de sync ainda vivo faria a última
        # thread escrever num arquivo fechado.
        if sync is not None:
            sync.shutdown()
        if edge is not None:
            edge.stop()
        database.close()


def run() -> int:
    """Ponto de entrada: falha de inicialização vira mensagem, não traceback.

    Empacotado sem console, uma exceção aqui aparecia como a caixa crua do
    PyInstaller ("Failed to execute script 'main'"), sem dizer o que fazer.
    """
    try:
        return main()
    except Exception as exc:  # noqa: BLE001 - última linha de defesa
        logger.exception("O PDV não conseguiu iniciar")
        message = str(exc) if isinstance(exc, StartupError) else (
            f"O PDV não conseguiu iniciar.\n\n{exc}"
        )
        _show_fatal(message)
        return 1


def _show_fatal(message: str) -> None:
    try:
        app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841
        QMessageBox.critical(None, "PDV Balcão", message)
    except Exception:  # noqa: BLE001  # pragma: no cover
        if sys.stderr is not None:
            print(message, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(run())
