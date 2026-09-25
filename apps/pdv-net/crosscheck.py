"""Verifica, com o código do PDV em Python, um ledger gravado pelo PDV em C#.

A outra metade dos contratos: `contracts/audit-chain.json` e
`contracts/pin-hashes.json` provam que o C# calcula e verifica o que o Python
calcula; isto prova que o que o C# GRAVA no banco o Python aceita — a cadeia de
auditoria (colunas, ordem, formato de data) e o hash de PIN (Argon2id).

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
from pdv.services.authorization import _verify  # noqa: E402

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

        # PIN: hash Argon2id gerado pelo C#, verificado pelo argon2-cffi.
        row = connection.execute("SELECT pin_hash FROM users WHERE id = 'u-crosscheck'").fetchone()
        if row is None or not _verify(row["pin_hash"], "480362") or _verify(row["pin_hash"], "480363"):
            print("FALHOU: o hash de PIN gravado pelo C# não verifica no Python")
            return 1
    finally:
        connection.close()
    print(f"ok: {rows} elos e o hash de PIN gravados pelo C# verificados pelo PDV em Python")
    return 0


#: O mesmo valor de SecretVaultTests.Known (C#).
KNOWN_SECRET = bytes(31 - i for i in range(32))


def write_vault(folder: str) -> int:
    """Grava um cofre pelo PDV em Python, para o C# ler (DPAPI desta máquina)."""
    from pdv.provisioning.secrets import SecretVault

    SecretVault(Path(folder)).store("device_secret", KNOWN_SECRET)
    print(f"ok: cofre gravado pelo Python em {folder}")
    return 0


def read_vault(folder: str) -> int:
    """Lê, pelo PDV em Python, o cofre que o C# gravou."""
    from pdv.provisioning.secrets import SecretVault

    value = SecretVault(Path(folder)).load("device_secret")
    if value != KNOWN_SECRET:
        print("FALHOU: o cofre gravado pelo C# não abre no Python (DPAPI, entropia ou escopo)")
        return 1
    print("ok: cofre gravado pelo C# lido pelo PDV em Python")
    return 0


if __name__ == "__main__":
    if sys.argv[1] == "write-vault":
        sys.exit(write_vault(sys.argv[2]))
    status = main(sys.argv[1])
    if status == 0 and len(sys.argv) > 2:
        status = read_vault(sys.argv[2])
    sys.exit(status)
