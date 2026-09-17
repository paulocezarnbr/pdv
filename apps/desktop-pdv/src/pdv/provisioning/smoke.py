"""Teste de fumaça pós-instalação.

Um instalador que termina com "concluído com sucesso" sem ter exercitado nada
está mentindo por omissão. O erro aparece depois, no sábado cheio, com o
técnico a 40 km de distância.

Este módulo exercita, na ordem, o caminho completo de uma venda:

    banco → balança → impressora → sincronização

e diz **exatamente** o que falhou, com o que fazer a respeito. Cada verificação
é independente: uma balança desconectada não impede de descobrir que a
impressora também está sem papel. Descobrir os dois problemas numa visita é
melhor que descobrir um de cada vez.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.data.repositories import OutboxRepository
from pdv.domain.errors import PdvError
from pdv.domain.models import EntityId
from pdv.hardware.printer.backends import build_printer
from pdv.hardware.printer.escpos import Align, EscPosBuilder
from pdv.hardware.scale.serial_scale import build_scale
from pdv.services.audit import AuditService


class CheckStatus(Enum):
    OK = "ok"
    WARNING = "warning"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str
    remedy: str | None = None

    @property
    def blocking(self) -> bool:
        return self.status is CheckStatus.FAILED


@dataclass(frozen=True, slots=True)
class SmokeReport:
    results: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        return not any(r.blocking for r in self.results)

    @property
    def can_sell(self) -> bool:
        """Banco e impressora bastam para vender: balança só afeta item pesado."""
        critical = {"Banco de dados", "Impressora"}
        return not any(r.blocking for r in self.results if r.name in critical)

    def to_text(self) -> str:
        symbols = {
            CheckStatus.OK: "[ OK ]",
            CheckStatus.WARNING: "[AVISO]",
            CheckStatus.FAILED: "[FALHA]",
        }
        lines = []
        for r in self.results:
            lines.append(f"{symbols[r.status]} {r.name}: {r.detail}")
            if r.remedy and r.status is not CheckStatus.OK:
                lines.append(f"         → {r.remedy}")
        return "\n".join(lines)


def run_smoke_test(
    config: AppConfig,
    database: Database,
    *,
    print_test_receipt: bool = True,
) -> SmokeReport:
    """Executa todas as verificações e devolve o relatório."""
    return SmokeReport(
        results=(
            _check_database(config, database),
            _check_audit_chain(config, database),
            _check_scale(config),
            _check_printer(config, print_test_receipt),
            _check_catalog(database),
        )
    )


# --------------------------------------------------------------------------- #
# Verificações
# --------------------------------------------------------------------------- #


def _check_database(config: AppConfig, database: Database) -> CheckResult:
    try:
        row = database.query_one("PRAGMA journal_mode")
        mode = str(row[0]).lower() if row else "?"

        # Escrita real: permissão de leitura não garante permissão de escrita,
        # e é escrevendo que o PDV vende.
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO device_settings (key, value, updated_at) "
                "VALUES ('smoke.test', '1', datetime('now')) "
                "ON CONFLICT (key) DO UPDATE SET value = '1'"
            )
    except Exception as exc:
        return CheckResult(
            "Banco de dados",
            CheckStatus.FAILED,
            f"não foi possível gravar: {exc}",
            f"Verifique a permissão de escrita em {config.database_path.parent}",
        )

    if mode != "wal":
        return CheckResult(
            "Banco de dados",
            CheckStatus.WARNING,
            f"journal_mode={mode} (esperado WAL)",
            "Sem WAL, a sincronização trava a tela do caixa durante o envio",
        )

    return CheckResult("Banco de dados", CheckStatus.OK, "gravação OK, modo WAL ativo")


def _check_audit_chain(config: AppConfig, database: Database) -> CheckResult:
    audit = AuditService(
        tenant_id=EntityId(config.tenant_id),
        store_id=EntityId(config.store_id),
        device_id=EntityId(config.device_id),
        outbox=OutboxRepository(),
        device_secret=config.device_secret,
    )
    try:
        audit.verify_chain(database.connection)
    except PdvError as exc:
        return CheckResult(
            "Auditoria",
            CheckStatus.FAILED,
            str(exc),
            "Cadeia inconsistente. Não opere antes de acionar o suporte",
        )
    return CheckResult("Auditoria", CheckStatus.OK, "cadeia íntegra")


def _check_scale(config: AppConfig) -> CheckResult:
    if config.scale.protocol == "simulated":
        return CheckResult(
            "Balança",
            CheckStatus.WARNING,
            "nenhuma balança detectada — modo simulado",
            "Produto por peso não poderá ser vendido. Confira o cabo USB/serial "
            "e execute a detecção novamente",
        )

    driver = build_scale(config.scale)
    try:
        driver.open()
        reading = driver.read()
    except PdvError as exc:
        return CheckResult(
            "Balança",
            CheckStatus.FAILED,
            f"{config.scale.protocol} em {config.scale.port}: {exc}",
            "Confira se a balança está ligada e se a porta não está em uso por "
            "outro programa",
        )
    finally:
        driver.close()

    return CheckResult(
        "Balança",
        CheckStatus.OK,
        f"{config.scale.protocol} em {config.scale.port} respondeu "
        f"({reading.status.value}, quadro {reading.raw_frame!r})",
    )


def _check_printer(config: AppConfig, print_receipt: bool) -> CheckResult:
    backend = build_printer(config.printer)

    if not backend.is_available():
        return CheckResult(
            "Impressora",
            CheckStatus.FAILED,
            f"{config.printer.windows_printer_name!r} não encontrada",
            "Instale o driver da Epson TM-T20X e confira o nome exato em "
            "Dispositivos e Impressoras",
        )

    if not print_receipt:
        return CheckResult("Impressora", CheckStatus.OK, "encontrada (sem teste de impressão)")

    try:
        backend.send(_test_receipt(config), job_name="PDV - Teste de instalacao")
    except PdvError as exc:
        return CheckResult(
            "Impressora",
            CheckStatus.FAILED,
            f"falha ao imprimir: {exc}",
            "Verifique papel, tampa fechada e cabo USB",
        )
    except OSError as exc:
        # O backend de arquivo escreve direto com `pathlib` e deixa o `OSError`
        # subir cru. Sem este ramo, uma pasta sem permissão de escrita derruba o
        # assistente inteiro em vez de virar uma linha de relatório — e o
        # técnico fica sem saber o que mais estava certo ou errado na máquina.
        return CheckResult(
            "Impressora",
            CheckStatus.FAILED,
            f"falha ao gravar o cupom: {exc}",
            "Confira a permissão de escrita na pasta de cupons",
        )

    return CheckResult(
        "Impressora",
        CheckStatus.OK,
        "cupom de teste enviado — confira se saiu cortado e se a gaveta abriu",
    )


def _check_catalog(database: Database) -> CheckResult:
    row = database.query_one(
        "SELECT COUNT(*) AS total FROM products WHERE is_active = 1"
    )
    total = int(row["total"]) if row else 0

    if total == 0:
        return CheckResult(
            "Catálogo",
            CheckStatus.WARNING,
            "nenhum produto cadastrado",
            "Os produtos descem na primeira sincronização com a nuvem",
        )
    return CheckResult("Catálogo", CheckStatus.OK, f"{total} produto(s) disponível(is)")


def _test_receipt(config: AppConfig) -> bytes:
    """Cupom de teste que exercita o que o cupom real usa.

    Inclui acentuação (valida a code page), corte e pulso de gaveta — os três
    pontos que costumam falhar só no hardware do cliente.
    """
    b = EscPosBuilder(columns=config.printer.columns, codepage=config.printer.codepage)
    b.initialize()
    b.align(Align.CENTER).bold(True).size(2, 2)
    b.line("TESTE")
    b.size(1, 1).bold(False)
    b.line("Instalacao do PDV")
    b.align(Align.LEFT).separator("=")
    b.line("Acentuacao: ção, ãé, ÁÊÍÕÜ")
    b.columns_2("Alinhamento em colunas", "1.234,56")
    b.separator()
    b.line(f"Loja....: {config.store_name}")
    b.line(f"Terminal: {config.device_id[:8]}")
    b.line(f"Balanca.: {config.scale.protocol} @ {config.scale.port}")
    b.feed(1)
    b.align(Align.CENTER).line("Se este cupom saiu cortado")
    b.line("e a gaveta abriu, esta tudo certo.")
    b.align(Align.LEFT)
    b.feed(1).cut(feed_lines=config.printer.cut_feed_lines)
    b.open_drawer()
    return b.build()
