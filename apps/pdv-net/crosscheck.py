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


#: Os mesmos de SalonCertificateTests (C#).
TLS_STORE = "Loja"
TLS_HOSTS = ("localhost", "127.0.0.1")


def write_tls(folder: str) -> int:
    """Gera o certificado do salão pelo PDV em Python, para o C# reaproveitar."""
    from pdv.edge.tls import ensure_certificate

    material = ensure_certificate(Path(folder), store_name=TLS_STORE, hosts=TLS_HOSTS)
    if material is None:
        print("FALHOU: o PDV em Python não gerou o certificado do salão")
        return 1
    (Path(folder) / "fingerprint.txt").write_text(material.fingerprint, encoding="ascii")
    print(f"ok: certificado do salão gerado pelo Python em {folder}")
    return 0


def read_tls(folder: Path) -> int:
    """O certificado que o C# gerou é REAPROVEITADO pelo Python, não trocado.

    Trocar mudaria a digital que o garçom conferiu no pareamento: na transição
    entre os dois PDVs, cada troca faria a loja inteira conferir de novo.
    """
    from pdv.edge.tls import ensure_certificate

    expected = (folder / "fingerprint.txt").read_text(encoding="ascii").strip()
    material = ensure_certificate(folder, store_name=TLS_STORE, hosts=TLS_HOSTS)
    if material is None or material.fingerprint != expected:
        print("FALHOU: o PDV em Python não reaproveitou o certificado do salão gerado pelo C#")
        return 1
    print("ok: certificado do salão gerado pelo C# reaproveitado pelo PDV em Python")
    return 0


def read_activation(folder: Path) -> int:
    """Abre, pelo PDV em Python, o terminal que o C# ativou (ActivationTests)."""
    from pdv.data.database import Database
    from pdv.data.settings import SettingsStore
    from pdv.provisioning.activation import SYNC_TOKEN_NAME, load_sync_token
    from pdv.provisioning.secrets import SecretVault
    from pdv.provisioning.staging import promote_staged_activation

    path = folder / "pdv_local.db"
    if not path.exists():
        print(f"FALHOU: o C# não gravou o terminal ativado em {folder}")
        return 1
    if promote_staged_activation(path) is not None:
        print("FALHOU: o C# deixou uma ativação pendente em vez de promovê-la")
        return 1
    database = Database(path)
    try:
        database.migrate()  # na versão certa, não faz nada; noutra, falharia aqui
        settings = SettingsStore(database).load()
        row = database.connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    finally:
        database.close()
    vault = SecretVault(folder / "secrets")
    expected = {
        "activated": True,
        "tenant": "aaaaaaaa-0000-0000-0000-000000000001",
        "store": "bbbbbbbb-0000-0000-0000-000000000002",
        "device": "cccccccc-0000-0000-0000-000000000003",
        "token": "token-de-sincronizacao-de-teste",
        "products": 0,
    }
    actual = {
        "activated": settings.activated,
        "tenant": settings.tenant_id,
        "store": settings.store_id,
        "device": settings.device_id,
        "token": load_sync_token(vault),
        "products": row,
    }
    if actual != expected:
        wrong = sorted(key for key in expected if expected[key] != actual[key])
        print(f"FALHOU: terminal ativado pelo C# lido errado pelo Python em: {', '.join(wrong)}")
        return 1
    if not vault.exists(SYNC_TOKEN_NAME):
        print("FALHOU: o token não está no cofre")
        return 1
    print("ok: terminal ativado pelo C# (banco promovido, identidade e token) aberto pelo PDV em Python")
    return 0


if __name__ == "__main__":
    if sys.argv[1] == "write-vault":
        sys.exit(write_vault(sys.argv[2]))
    if sys.argv[1] == "write-tls":
        sys.exit(write_tls(sys.argv[2]))
    status = main(sys.argv[1])
    if status == 0 and len(sys.argv) > 2:
        status = read_vault(sys.argv[2])
    if status == 0:
        status = read_activation(Path(sys.argv[1]).parent / "activation")
    if status == 0:
        status = read_tls(Path(sys.argv[1]).parent / "tls")
    sys.exit(status)
