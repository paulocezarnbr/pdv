"""O `PDV.exe` lê o que o `PDVSetup.exe` gravou.

O caixa montava a configuração só do ambiente: abria `./pdv_local.db`, com os
IDs de demonstração e a chave de desenvolvimento, e ignorava a ativação, o
cofre e os periféricos detectados. Cada teste aqui é uma dessas perdas.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pdv.data.database import Database
from pdv.data.settings import SettingsStore
from pdv.provisioning.secrets import SecretVault
from pdv.runtime import DATABASE_NAME, load_runtime, should_seed_demo


@pytest.fixture()
def provisioned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Uma pasta de dados como o instalador deixa depois de ativar."""
    for name in ("PDV_DB_PATH", "PDV_DEVICE_SECRET", "PDV_DEMO", "PDV_CLOUD_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PDV_DATA_DIR", str(tmp_path))

    database = Database(tmp_path / DATABASE_NAME)
    database.migrate()
    SettingsStore(database).set_many({
        "device.tenant_id": "tenant-da-loja",
        "device.store_id": "loja-centro",
        "device.id": "caixa-1",
        "device.activated": "1",
        "cloud.base_url": "https://api.loja.com.br",
        "printer.backend": "file",
    })
    database.close()
    SecretVault(tmp_path / "secrets").ensure_device_secret()
    return tmp_path


def test_the_counter_opens_the_provisioned_database(provisioned: Path) -> None:
    runtime = load_runtime()

    assert runtime.installed
    assert runtime.config.database_path == provisioned / DATABASE_NAME


def test_the_activation_identity_reaches_the_counter(provisioned: Path) -> None:
    """Com os IDs de demonstração, toda venda saía para o tenant errado."""
    config = load_runtime().config

    assert (config.tenant_id, config.store_id, config.device_id) == (
        "tenant-da-loja", "loja-centro", "caixa-1",
    )
    assert config.cloud_base_url == "https://api.loja.com.br"


def test_the_ledger_is_signed_with_the_vault_secret(provisioned: Path) -> None:
    """A chave que a ativação entregou à nuvem é a que assina o ledger.

    Com a chave de desenvolvimento, a nuvem acusaria HMAC inválido em todo elo.
    """
    config = load_runtime().config

    assert config.device_secret == SecretVault(provisioned / "secrets").ensure_device_secret()
    assert b"DESENVOLVIMENTO" not in config.device_secret


def test_the_secret_is_the_same_on_every_start(provisioned: Path) -> None:
    first = load_runtime()
    first.database.close()

    assert load_runtime().config.device_secret == first.config.device_secret


def test_receipts_are_written_where_the_store_user_can_write(provisioned: Path) -> None:
    assert load_runtime().config.printer.output_dir == provisioned / "cupons"


def test_the_store_never_gets_the_demo_catalog(
    provisioned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = load_runtime()
    assert not should_seed_demo(runtime)

    monkeypatch.setenv("PDV_DEMO", "1")
    assert should_seed_demo(runtime), "demonstração continua possível, se pedida"


def test_running_from_source_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PDV_DATA_DIR", raising=False)
    monkeypatch.setenv("PDV_DB_PATH", str(tmp_path / "dev.db"))

    runtime = load_runtime()

    assert not runtime.installed
    assert runtime.config.database_path == tmp_path / "dev.db"
    assert should_seed_demo(runtime)
