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
import sys
from decimal import Decimal

from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.data.repositories import OutboxRepository
from pdv.data.seed import seed_demo_data
from pdv.domain.errors import AuditChainError
from pdv.domain.models import Cents, EntityId
from pdv.edge.worker import EdgeServer
from pdv.hardware.printer.backends import PrintService, build_printer
from pdv.hardware.scale.serial_scale import build_scale
from pdv.hardware.scale.worker import ScaleService
from pdv.provisioning.activation import load_sync_token
from pdv.provisioning.secrets import SecretVault
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
    database: Database, config: AppConfig, checkout: CheckoutService
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

    engine = SyncEngine(
        database,
        HttpTransport(config.cloud_base_url, token),
        config,
        # É esta linha que liga o canal de comando do painel. Sem ela o
        # terminal só envia, como era até a Fase 3.5 — e continua funcionando.
        commands=RemoteCommandService(database, config, checkout=checkout),
    )
    return SyncService(engine)


def main() -> int:
    # Antes de qualquer coisa, e antes do Qt: o autoteste roda dentro do
    # executável compilado e responde "este pacote está completo?". É a última
    # etapa do build, e o único jeito de pegar um `hiddenimports` faltando —
    # que produz um pacote que instala, abre, e falha na loja.
    if "--selftest" in sys.argv:
        from pdv.selftest import run_cli

        return run_cli()

    config = AppConfig.from_env()

    database = Database(config.database_path)
    database.migrate()
    seed_demo_data(database, config)

    integrity_error = verify_audit_integrity(database, config)

    app = QApplication(sys.argv)
    app.setApplicationName("PDV Balcão")

    # Antes de qualquer janela: o tema fixa estilo, paleta e fonte. Se viesse
    # depois, o primeiro diálogo (o aviso de auditoria logo abaixo) apareceria
    # com o tema do Windows — claro numa máquina, escuro na outra.
    apply_theme(app)

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
    operator = LoginDialog.ask(
        AuthorizationService(database, config.tenant_id),
        store_name=config.store_name,
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

    sync = build_sync(database, config, checkout)

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
    )
    window.show()
    scale.start()
    if sync is not None:
        sync.start()

    try:
        return app.exec()
    finally:
        # Ordem do encerramento: primeiro o que fala com a rede, depois o
        # banco. Fechar o banco com o worker de sync ainda vivo faria a última
        # thread escrever num arquivo fechado.
        if sync is not None:
            sync.shutdown()
        if edge is not None:
            edge.stop()
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
