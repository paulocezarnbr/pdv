"""Primeira abertura: modo demonstração, ativação pelo caixa e cadastro da loja.

Três defeitos de uso real, nenhum deles visível num teste de serviço:

* o terminal ativado não recebe os logins de demonstração — e os usuários da
  loja só desciam da nuvem DEPOIS do login. Ninguém conseguia entrar;
* ativar em modo demonstração mandaria as vendas de teste para a loja real;
* nada na tela dizia que o caixa estava em demonstração.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from pdv.config import AppConfig, PrinterConfig, ScaleConfig
from pdv.data.database import Database
from pdv.data.seed import seed_demo_data
from pdv.data.settings import SettingsStore
from pdv.provisioning import staging
from pdv.provisioning.staging import (
    StagedActivationError,
    promote_staged_activation,
    staged_path,
)
from pdv.sync.protocol import PullResponse, PushResponse

pytest.importorskip("PySide6")

import main  # noqa: E402

REAL_TENANT = "aaaaaaaa-0000-0000-0000-000000000001"


def _config(path: Path, tenant: str = REAL_TENANT) -> AppConfig:
    return AppConfig(
        tenant_id=tenant,
        store_id="aaaaaaaa-0000-0000-0000-000000000002",
        device_id="aaaaaaaa-0000-0000-0000-000000000003",
        database_path=path,
        scale=ScaleConfig(protocol="simulated"),
        printer=PrinterConfig(backend="file", output_dir=path.parent / "cupons"),
    )


# --------------------------------------------------------------------------- #
# Troca do banco de demonstração pelo da loja
# --------------------------------------------------------------------------- #


def test_without_a_pending_activation_nothing_moves(tmp_path: Path) -> None:
    database = tmp_path / "pdv_local.db"
    database.write_bytes(b"demo")

    assert promote_staged_activation(database) is None
    assert database.read_bytes() == b"demo"


def test_the_demo_is_archived_not_deleted(tmp_path: Path) -> None:
    database = tmp_path / "pdv_local.db"
    database.write_bytes(b"demo")
    Path(f"{database}-wal").write_bytes(b"demo-wal")
    staged_path(database).write_bytes(b"loja")

    archived = promote_staged_activation(database, now=datetime(2026, 9, 24, 10, 30))

    assert archived == tmp_path / "pdv_demo-20260924-103000.db"
    assert archived.read_bytes() == b"demo"
    assert Path(f"{archived}-wal").read_bytes() == b"demo-wal", "o WAL vai junto"
    assert database.read_bytes() == b"loja"
    assert not staged_path(database).exists()
    assert not Path(f"{database}-wal").exists()


def test_a_locked_database_waits_and_then_says_what_to_do(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    database = tmp_path / "pdv_local.db"
    database.write_bytes(b"demo")
    staged_path(database).write_bytes(b"loja")

    def locked(*_args) -> None:  # noqa: ANN002
        raise PermissionError("em uso")

    monkeypatch.setattr(staging.os, "replace", locked)
    monkeypatch.setattr(staging.time, "sleep", lambda _s: None)

    with pytest.raises(StagedActivationError, match="Feche"):
        promote_staged_activation(database, wait_seconds=0)

    assert database.read_bytes() == b"demo", "nada foi movido pela metade"


def test_opening_the_pdv_completes_a_pending_activation(tmp_path: Path) -> None:
    demo_path = tmp_path / "pdv_local.db"
    demo = Database(demo_path)
    demo.migrate()
    seed_demo_data(demo, _config(demo_path, "11111111-1111-1111-1111-111111111111"))
    demo.close()

    staged = Database(staged_path(demo_path))
    staged.migrate()
    SettingsStore(staged).set_many(
        {"device.activated": "1", "device.tenant_id": REAL_TENANT}
    )
    staged.close()

    opened, config = main.open_database(_config(demo_path, "11111111-1111-1111-1111-111111111111"))

    assert config.tenant_id == REAL_TENANT
    assert opened.query_one("SELECT COUNT(*) AS n FROM products")["n"] == 0, (
        "nenhuma venda nem produto de demonstração no banco da loja"
    )
    assert list(tmp_path.glob("pdv_demo-*.db")), "a demonstração ficou arquivada"
    opened.close()


def test_a_token_without_activation_never_syncs_the_demo(tmp_path: Path) -> None:
    """Entre ativar pelo caixa e reiniciar, o token já é da loja e o banco aberto
    ainda é o de demonstração."""
    from pdv.provisioning.activation import SYNC_TOKEN_NAME
    from pdv.provisioning.secrets import SecretVault
    from pdv.services.checkout import CheckoutService

    config = _config(tmp_path / "pdv_local.db")
    database = Database(config.database_path)
    database.migrate()
    SecretVault(tmp_path / "secrets").store(SYNC_TOKEN_NAME, b"token-da-loja")

    assert main.build_sync(database, config, CheckoutService(database, config)) is None


# --------------------------------------------------------------------------- #
# Cadastro da loja antes do login
# --------------------------------------------------------------------------- #


class _Cloud:
    """Nuvem que entrega os usuários na N-ésima tentativa de pull."""

    def __init__(self, deliver_on: int = 1) -> None:
        self.deliver_on = deliver_on
        self.pulls: dict[str, int] = {}

    def push(self, batch):  # noqa: ANN001, ANN201
        return PushResponse(acks=())

    def pull(self, request):  # noqa: ANN001, ANN201
        count = self.pulls.get(request.entity_table, 0) + 1
        self.pulls[request.entity_table] = count
        rows: tuple = ()
        if request.entity_table == "users" and count >= self.deliver_on:
            rows = (
                {
                    "id": "bbbbbbbb-0000-0000-0000-000000000001",
                    "tenant_id": REAL_TENANT,
                    "name": "Carla Caixa",
                    "login": "carla",
                    "role": "cashier",
                    "can_authorize": 0,
                    "max_discount_percent": "0",
                    "pin_hash": "argon2-fake",
                    "is_active": 1,
                    "updated_at": "2026-09-24T10:00:00+00:00", "server_seq": "7",
                },
            )
        return PullResponse(entity_table=request.entity_table, rows=rows, last_server_seq=count)


def _activated(tmp_path: Path) -> tuple[Database, AppConfig]:
    config = _config(tmp_path / "pdv_local.db")
    database = Database(config.database_path)
    database.migrate()
    SettingsStore(database).set_many({"device.activated": "1", "device.tenant_id": REAL_TENANT})
    return database, config


def _direct(_message, work):  # noqa: ANN001, ANN202
    return work()


def test_the_store_users_arrive_before_the_login(tmp_path: Path) -> None:
    database, config = _activated(tmp_path)

    ok = main.ensure_store_users(
        database, config, transport=_Cloud(), run=_direct, ask_retry=lambda: False
    )

    assert ok is True
    logins = {r["login"] for r in database.query_all("SELECT login FROM users")}
    assert logins == {"carla"}


def test_offline_first_start_offers_to_try_again(tmp_path: Path) -> None:
    database, config = _activated(tmp_path)
    asked: list[int] = []

    ok = main.ensure_store_users(
        database, config, transport=_Cloud(deliver_on=2), run=_direct,
        ask_retry=lambda: asked.append(1) or True,
    )

    assert ok is True
    assert asked == [1], "uma tentativa falhou, a segunda trouxe os usuários"


def test_giving_up_closes_the_pdv_instead_of_opening_without_anyone(tmp_path: Path) -> None:
    database, config = _activated(tmp_path)

    ok = main.ensure_store_users(
        database, config, transport=_Cloud(deliver_on=99), run=_direct,
        ask_retry=lambda: False,
    )

    assert ok is False


def test_an_activated_terminal_without_a_token_says_so(tmp_path: Path) -> None:
    database, config = _activated(tmp_path)

    with pytest.raises(main.StartupError, match="credencial"):
        main.ensure_store_users(database, config, run=_direct, ask_retry=lambda: False)


def test_a_terminal_with_users_does_not_touch_the_network(tmp_path: Path) -> None:
    database, config = _activated(tmp_path)
    database.connection.execute(
        "INSERT INTO users (id, tenant_id, name, login, role, can_authorize, "
        "max_discount_percent, pin_hash, updated_at) VALUES "
        "('u1', ?, 'Carla', 'carla', 'cashier', 0, '0', 'x', '2026-01-01')",
        (REAL_TENANT,),
    )
    cloud = _Cloud()

    assert main.ensure_store_users(database, config, transport=cloud, run=_direct) is True
    assert cloud.pulls == {}


# --------------------------------------------------------------------------- #
# Telas
# --------------------------------------------------------------------------- #


def test_the_demo_hint_appears_only_in_demo_mode(qtbot, tmp_path: Path) -> None:  # noqa: ANN001
    from PySide6.QtWidgets import QLabel

    from pdv.services.authorization import AuthorizationService
    from pdv.ui.login_dialog import LoginDialog

    database, config = _activated(tmp_path)
    auth = AuthorizationService(database, config.tenant_id)
    demo = LoginDialog(auth, store_name="Loja", demo_hint=True)
    real = LoginDialog(auth, store_name="Loja")
    qtbot.addWidget(demo)
    qtbot.addWidget(real)

    def texts(dialog) -> str:  # noqa: ANN001
        return " ".join(label.text() for label in dialog.findChildren(QLabel))

    assert "705284" in texts(demo)
    assert "705284" not in texts(real)


def test_the_activated_store_name_replaces_the_demo_name(tmp_path: Path) -> None:
    """Login e título diziam "Confeitaria Demo" num terminal já da loja."""
    from pdv.provisioning.activation import ActivationResult, activate
    from pdv.provisioning.secrets import SecretVault

    class _Transport:
        def activate(self, code, fingerprint):  # noqa: ANN001, ANN201
            return ActivationResult(
                tenant_id=REAL_TENANT, store_id="s", device_id="d",
                sync_token="tok", store_name="Padaria Estrela",
            )

    database = Database(tmp_path / "pdv_local.db")
    database.migrate()
    activate("A1B2C3D4", database=database, vault=SecretVault(tmp_path / "secrets"),
             transport=_Transport())

    applied = SettingsStore(database).apply_to(_config(tmp_path / "pdv_local.db"))

    assert applied.store_name == "Padaria Estrela"
