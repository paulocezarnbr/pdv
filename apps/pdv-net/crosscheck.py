"""Verifica, com o código do PDV em Python, um ledger gravado pelo PDV em C#.

A outra metade do contrato de auditoria: `contracts/audit-chain.json` prova
que o C# calcula o que o Python calcula; isto prova que o que o C# GRAVA no
banco o Python aceita — colunas, ordem, formato de data, tudo.

Uso (o CI faz):
    PDV_CROSSCHECK_OUT=<arquivo.db> dotnet test
    python apps/pdv-net/crosscheck.py <arquivo.db>
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "desktop-pdv" / "src"))

from pdv.data.repositories import OutboxRepository  # noqa: E402
from pdv.domain.models import EntityId  # noqa: E402
from pdv.services.audit import AuditService  # noqa: E402

# O mesmo segredo e identidade de DataTests.cs.
SECRET = bytes(range(32))


def main(path: str) -> int:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT COUNT(*) FROM audit_ledger WHERE device_id = 'device-1'").fetchone()[0]
        if rows < 2:
            print(f"FALHOU: esperava ao menos 2 elos gravados pelo C#, encontrei {rows}")
            return 1
        AuditService(
            tenant_id=EntityId("tenant-1"),
            store_id=EntityId("store-1"),
            device_id=EntityId("device-1"),
            outbox=OutboxRepository(),
            device_secret=SECRET,
        ).verify_chain(connection)
    finally:
        connection.close()
    print(f"ok: {rows} elos gravados pelo C# verificados pelo PDV em Python")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
