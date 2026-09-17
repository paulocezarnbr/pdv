"""Assistente de primeira execução — chamado pelo instalador.

Fecha a lacuna entre "arquivos copiados" e "PDV vendendo":

    1. prepara o banco e as pastas de dados
    2. gera o segredo do terminal no DPAPI
    3. varre as portas e identifica balança e impressora
    4. roda o teste de fumaça e relata o que estiver errado

Uso::

    PDVSetup.exe                 # interativo, com janela
    PDVSetup.exe --silent        # sem interface (instalação automatizada)
    PDVSetup.exe --detect-only   # só redetecta periféricos
    PDVSetup.exe --demo          # + catálogo de demonstração (nunca em loja real)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.data.seed import seed_demo_data
from pdv.data.settings import SettingsStore
from pdv.provisioning.activation import (
    ActivationError,
    HttpActivationTransport,
    activate,
    is_activated,
)
from pdv.provisioning.detection import detect_all, settings_from_detection
from pdv.provisioning.secrets import SecretVault
from pdv.provisioning.smoke import CheckStatus, run_smoke_test

logger = logging.getLogger("pdv.setup")

DEFAULT_DATA_DIR = Path(
    os.getenv("PDV_DATA_DIR", r"C:\ProgramData\ERPFood\PDV")
)

#: Códigos de saída lidos pelo instalador para decidir o que mostrar ao lojista.
EXIT_OK = 0
EXIT_WARNINGS = 2
EXIT_FAILED = 3


def _console_stream():  # noqa: ANN202
    """Saída de console utilizável, ou `None` se não houver nenhuma.

    Dois modos de falha reais, ambos custaram um provisionamento inteiro:

    * O console brasileiro do Windows é **cp1252**. O relatório usa `→` nas
      linhas de remédio, e `print` de um caractere fora da cp1252 levanta
      `UnicodeEncodeError` — o provisionamento terminava certo e morria na hora
      de contar o resultado.
    * Compilado com `console=False`, o PyInstaller deixa `sys.stdout` em `None`.
      Aí `print` nem chega a codificar: quebra em `AttributeError`.

    O relatório completo vai para `setup.log` em UTF-8 de qualquer forma, então
    perder o console é aceitável. Perder o relatório não seria.
    """
    stream = sys.stdout
    if stream is None:
        return None
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        # Stream sem reconfigure (redirecionado por outro processo) continua
        # servindo: quem escreve nele trata o erro por conta.
        pass
    return stream


def _report(text: str) -> None:
    """Escreve o relatório no console, se houver um que aceite."""
    stream = _console_stream()
    if stream is None:
        return
    try:
        stream.write(text + "\n")
        stream.flush()
    except (UnicodeEncodeError, OSError, ValueError):
        pass


def _configure_logging(data_dir: Path) -> None:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    handlers: list[logging.Handler] = [
        logging.FileHandler(log_dir / "setup.log", encoding="utf-8")
    ]
    stream = _console_stream()
    if stream is not None:
        handlers.append(logging.StreamHandler(stream))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def _activate_step(
    database: Database,
    vault: SecretVault,
    base_config: AppConfig,
    activation_code: str | None,
) -> str:
    """Ativa o terminal se houver código. Nunca aborta o provisionamento.

    Um terminal sem ativação ainda **vende**: registra a venda, baixa estoque e
    imprime o cupom, acumulando tudo na fila de saída. O que ele não faz é
    sincronizar. Travar a instalação porque a internet da loja ainda não foi
    ligada transformaria um contratempo em visita técnica perdida.
    """
    if is_activated(database):
        settings = SettingsStore(database).load()
        return f"[ OK ] Terminal já ativado (loja {settings.store_id})"

    if not activation_code:
        return (
            "[AVISO] Terminal não ativado — trabalhará offline.\n"
            "        → As vendas ficam na fila até a ativação. Rode "
            "PDVSetup.exe --activation-code CODIGO"
        )

    try:
        result = activate(
            activation_code,
            database=database,
            vault=vault,
            transport=HttpActivationTransport(base_config.cloud_base_url),
        )
    except ActivationError as exc:
        logger.warning("Ativação não concluída: %s", exc)
        return f"[AVISO] Ativação não concluída: {exc}\n        → O PDV vende offline até isso ser resolvido"

    return (
        f"[ OK ] Terminal ativado para {result.store_name or result.store_id} "
        f"(device {result.device_id[:8]})"
    )


def provision(
    data_dir: Path,
    *,
    detect_only: bool = False,
    seed_demo: bool = False,
    activation_code: str | None = None,
) -> tuple[int, str]:
    """Executa o provisionamento. Devolve `(código_de_saída, relatório)`."""
    data_dir.mkdir(parents=True, exist_ok=True)

    base_config = AppConfig.from_env()
    database_path = data_dir / "pdv_local.db"

    database = Database(database_path)
    database.migrate()

    store = SettingsStore(database)
    lines: list[str] = []

    # --- 1. Segredo do terminal ------------------------------------------- #
    vault = SecretVault(data_dir / "secrets")
    device_secret = vault.ensure_device_secret()
    lines.append("[ OK ] Segredo do terminal protegido no DPAPI")

    # --- 1.5. Ativação do terminal ----------------------------------------- #
    # Antes da detecção de propósito: a ativação define `device_id` e
    # `tenant_id`, e é sob essa identidade que o ledger de auditoria começa a
    # ser encadeado. Ativar depois de já haver eventos gravados obrigaria a
    # reancorar a cadeia no servidor.
    lines.append(_activate_step(database, vault, base_config, activation_code))

    # --- 2. Detecção de periféricos ---------------------------------------- #
    logger.info("Varrendo portas seriais e impressoras...")
    detection = detect_all()

    detected_values = settings_from_detection(detection)

    # Numa reinstalação/atualização a configuração existente manda. Sobrescrever
    # aqui rebaixaria para `simulated` um terminal cuja balança apenas estava
    # desligada no momento do update — e a loja pararia de vender por peso sem
    # que nada tivesse de fato quebrado.
    if detect_only:
        store.set_many(detected_values)
    else:
        kept = set(detected_values) - set(store.set_many_if_absent(detected_values))
        if kept:
            lines.append(
                "[INFO] Configuração existente preservada "
                f"({', '.join(sorted(kept))})"
            )

    lines.append(f"[INFO] {detection.ports_scanned} porta(s) serial(is) verificada(s)")

    # O relatório fala do que ficou **configurado**, não do que a varredura viu.
    # Numa atualização os dois divergem de propósito, e anunciar "nenhuma
    # balança detectada" para um terminal que segue configurado e vendendo
    # mandaria o técnico caçar um problema que não existe.
    effective = store.load()

    scale = detection.best_scale
    if scale is not None:
        lines.append(
            f"[ OK ] Balança: {scale.protocol} em {scale.port} @ {scale.baudrate} "
            f"(confiança {scale.confidence})"
        )
    elif effective.scale_protocol and effective.scale_protocol != "simulated":
        lines.append(
            f"[INFO] Balança: mantida a configuração atual "
            f"({effective.scale_protocol} em {effective.scale_port}) — "
            f"não respondeu durante a varredura."
        )
    else:
        lines.append(
            "[AVISO] Nenhuma balança detectada — configurado modo simulado.\n"
            "        → Produto por peso não será vendido até a balança ser ligada."
        )

    printer = detection.best_printer
    if printer is not None:
        lines.append(f"[ OK ] Impressora: {printer.name}")
    elif effective.printer_backend and effective.printer_backend != "file":
        lines.append(
            f"[INFO] Impressora: mantida a configuração atual "
            f"({effective.printer_name})"
        )
    else:
        lines.append(
            "[AVISO] Nenhuma impressora encontrada — cupons irão para arquivo.\n"
            "        → Instale o driver da Epson TM-T20X."
        )

    if detect_only:
        return EXIT_OK, "\n".join(lines)

    # --- 3. Catálogo inicial ----------------------------------------------- #
    # Catálogo de demonstração é **opt-in**. Numa loja real os produtos descem
    # na primeira sincronização com a retaguarda; semear "Bolo de Chocolate" no
    # PDV do cliente criaria itens fantasma que ele teria de apagar um a um — e
    # que, pior, apareceriam na busca do operador durante uma venda de verdade.
    config = store.apply_to(
        AppConfig(
            tenant_id=base_config.tenant_id,
            store_id=base_config.store_id,
            device_id=base_config.device_id,
            device_secret=device_secret,
            database_path=database_path,
            cloud_base_url=base_config.cloud_base_url,
            # Cupons de contingência vão para a pasta de dados, **não** para o
            # `./out` relativo ao diretório de trabalho: instalado, o cwd é
            # Program Files, onde o endurecimento de ACL deixa o grupo Usuários
            # só com leitura. Pelo atalho "Reconfigurar periféricos" (que roda
            # sem elevação) a gravação falharia, e o teste acusaria uma
            # impressora perfeita como defeituosa.
            printer=replace(base_config.printer, output_dir=data_dir / "cupons"),
        )
    )
    if seed_demo:
        seed_demo_data(database, config)
        lines.append("[INFO] Catálogo de demonstração carregado (--demo)")

    # --- 4. Teste de fumaça ------------------------------------------------- #
    lines.append("")
    lines.append("Teste de instalação:")
    report = run_smoke_test(config, database)
    lines.append(report.to_text())

    database.close()

    if not report.can_sell:
        return EXIT_FAILED, "\n".join(lines)
    if any(r.status is not CheckStatus.OK for r in report.results):
        return EXIT_WARNINGS, "\n".join(lines)
    return EXIT_OK, "\n".join(lines)


#: QApplication é criada uma vez e mantida viva pelo processo inteiro. Soltar a
#: referência entre o diálogo de ativação e a janela de relatório deixaria o Qt
#: destruir a aplicação e recriá-la — caminho conhecido para travar no Windows.
_QT_APP: object | None = None


def _qt_app():  # noqa: ANN202
    """A `QApplication` do processo, ou `None` se o Qt não estiver disponível."""
    global _QT_APP

    if _QT_APP is not None:
        return _QT_APP
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:  # pragma: no cover
        return None

    _QT_APP = QApplication.instance() or QApplication(sys.argv)
    return _QT_APP


def _ask_activation_code() -> str | None:
    """Pede o código de ativação numa caixa de diálogo.

    Cancelar é uma resposta legítima, não um erro: o PDV instalado e não ativado
    vende offline e acumula na fila. Devolve `None` sem Qt disponível — a
    instalação segue e o relatório avisa como ativar depois.
    """
    if _qt_app() is None:  # pragma: no cover
        return None

    from PySide6.QtWidgets import QInputDialog

    code, accepted = QInputDialog.getText(
        None,
        "Ativação do terminal",
        "Código de ativação (gerado no painel administrativo):\n\n"
        "Deixe em branco para ativar depois — o PDV já vende offline.",
    )
    return code.strip() if accepted and code.strip() else None


def _show_window(exit_code: int, report: str) -> None:
    """Mostra o resultado numa janela. Cai para o console se o Qt não subir."""
    if _qt_app() is None:  # pragma: no cover
        _report(report)
        return

    from PySide6.QtWidgets import QMessageBox

    box = QMessageBox()
    box.setWindowTitle("Instalação do PDV Balcão")
    if exit_code == EXIT_OK:
        box.setIcon(QMessageBox.Icon.Information)
        box.setText("PDV instalado e pronto para vender.")
    elif exit_code == EXIT_WARNINGS:
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("PDV instalado, mas com pendências.")
    else:
        box.setIcon(QMessageBox.Icon.Critical)
        box.setText("A instalação terminou com falhas que impedem a venda.")

    box.setDetailedText(report)
    box.exec()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assistente de instalação do PDV")
    parser.add_argument("--silent", action="store_true", help="sem interface gráfica")
    parser.add_argument(
        "--detect-only", action="store_true", help="apenas redetectar periféricos"
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="carregar o catálogo de demonstração (nunca em loja real)",
    )
    parser.add_argument(
        "--activation-code",
        help="código de ativação gerado no painel administrativo",
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args(argv)

    _configure_logging(args.data_dir)

    try:
        # Sem código na linha de comando e com interface disponível, perguntamos.
        # O instalador não tem como saber o código: ele é gerado no painel no
        # momento da implantação e ditado para quem está na loja.
        code = args.activation_code
        if not code and not args.silent and not args.detect_only:
            code = _ask_activation_code()

        exit_code, report = provision(
            args.data_dir,
            detect_only=args.detect_only,
            seed_demo=args.demo,
            activation_code=code,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Falha no provisionamento")
        report = f"[FALHA] Erro inesperado: {exc}"
        exit_code = EXIT_FAILED

    # O logger já espelha no console quando existe um; imprimir de novo aqui só
    # duplicaria o relatório na tela do técnico.
    logger.info("Resultado do provisionamento:\n%s", report)

    if not args.silent:
        _show_window(exit_code, report)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
