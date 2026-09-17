"""Bootstrap do PDV de Balcão.

Ordem de inicialização (importa):

1. Banco local + migrations — antes de qualquer serviço, porque o resto depende dele.
2. Verificação da cadeia de auditoria — se o ledger foi adulterado, queremos
   saber na abertura do caixa, não no fechamento.
3. Periféricos — balança em thread própria, impressora em fila própria.
4. Servidor local — o PDV vira o servidor do salão para o app do garçom e
   o KDS. Sobe ANTES da UI e, se falhar, apenas registra: o salão fica sem
   servidor, mas o balcão continua vendendo.
5. UI — por último, já com tudo pronto.

Uso::

    python main.py                                   # balança simulada
    PDV_EDGE=0 python main.py                        # sem servidor do salão
    PDV_SCALE_PROTOCOL=toledo_prix3 PDV_SCALE_PORT=COM3 \\
    PDV_PRINTER_BACKEND=win32raw python main.py      # hardware real
"""

from __future__ import annotations

import logging
import os
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.data.repositories import OutboxRepository
from pdv.data.seed import seed_demo_data
from pdv.domain.errors import AuditChainError
from pdv.domain.models import EntityId
from pdv.edge.worker import EdgeServer
from pdv.hardware.printer.backends import PrintService, build_printer
from pdv.hardware.scale.serial_scale import build_scale
from pdv.hardware.scale.worker import ScaleService
from pdv.services.audit import AuditService
from pdv.services.checkout import CheckoutService
from pdv.ui.counter_window import CounterWindow

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


def main() -> int:
    config = AppConfig.from_env()

    database = Database(config.database_path)
    database.migrate()
    seed_demo_data(database, config)

    integrity_error = verify_audit_integrity(database, config)

    app = QApplication(sys.argv)
    app.setApplicationName("PDV Balcão")

    if integrity_error is not None:
        # Não bloqueia a venda — bloquear o caixa por suspeita de fraude
        # puniria o cliente na fila. Avisa, e o servidor confirma no sync.
        QMessageBox.warning(
            None,
            "Auditoria",
            f"Integridade do ledger comprometida:\n\n{integrity_error}\n\n"
            "O servidor será notificado na próxima sincronização.",
        )

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

    window = CounterWindow(
        checkout,
        scale,
        printer,
        config,
        database,
        edge_port=edge.port if edge is not None else None,
    )
    window.show()
    scale.start()

    try:
        return app.exec()
    finally:
        if edge is not None:
            edge.stop()
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
