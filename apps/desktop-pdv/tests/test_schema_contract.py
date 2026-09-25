"""Contrato do schema local — o banco que o PDV em C# abre.

Durante o porte (`docs/port_csharp.md`) o C# abre o MESMO `pdv_local.db`, e
as migrations continuam no Python até a fase C7. O C# não pode ter uma cópia
do schema escrita à mão: ela divergiria na primeira migration nova, e a
divergência só apareceria no caixa ("no such column").

Este arquivo grava `contracts/pdv-schema.sql`: o schema que o `migrate()`
produz de fato, na ordem do `sqlite_master`, com o `user_version`. Os testes do
C# criam os bancos deles a partir dele. Mudou uma migration, este teste
reprova até o contrato ser regerado — e o C# passa a testar contra o novo.

Para regerar: ``PDV_UPDATE_CONTRACT=1 python -m pytest tests/test_schema_contract.py``.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from pdv.data.database import SCHEMA_VERSION, Database

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "pdv-schema.sql"


def _snapshot(tmp_path: Path) -> str:
    database = Database(tmp_path / "schema.db")
    database.migrate()
    database.close()

    connection = sqlite3.connect(tmp_path / "schema.db")
    try:
        rows = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "ORDER BY rowid"
        ).fetchall()
    finally:
        connection.close()

    statements = [row[0].strip() + ";" for row in rows]
    header = (
        "-- Gerado por apps/desktop-pdv/tests/test_schema_contract.py. Não edite à mão.\n"
        f"-- Schema local do PDV na versão {SCHEMA_VERSION}, como o migrate() o deixa.\n"
    )
    return header + "\n\n".join(statements) + f"\n\nPRAGMA user_version = {SCHEMA_VERSION};\n"


def test_the_schema_contract_matches_the_migrations(tmp_path: Path) -> None:
    text = _snapshot(tmp_path)
    if os.getenv("PDV_UPDATE_CONTRACT") == "1":
        CONTRACT.write_text(text, encoding="utf-8", newline="\n")
    assert CONTRACT.exists(), "rode com PDV_UPDATE_CONTRACT=1 para gerar o contrato"
    assert CONTRACT.read_text(encoding="utf-8").replace("\r\n", "\n") == text, (
        "o schema local mudou: regere contracts/pdv-schema.sql e rode os testes do C#"
    )


def test_the_snapshot_builds_the_same_database(tmp_path: Path) -> None:
    """O snapshot, executado num banco vazio, reproduz o schema da migração."""
    connection = sqlite3.connect(tmp_path / "from-snapshot.db")
    try:
        connection.executescript(_snapshot(tmp_path))
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        connection.close()
    assert version == SCHEMA_VERSION
    assert {"orders", "payments", "audit_ledger", "sync_outbox"} <= tables
