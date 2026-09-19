"""A camada HTTP do salão — mapa de mesas, conta, gerente e o app web.

Existe separado de `test_salon.py` pelo motivo registrado em `test_edge.py`:
um bug real (o `from __future__ import annotations` no `server.py`) fez **toda**
rota responder 422 com os serviços 100% verdes por baixo. Serviço testado não
prova rota montada.

E prova também o que o `curl` do usuário provaria: que abrir o endereço do PDV
no navegador do celular devolve o app, e não 404.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pdv.config import AppConfig, PrinterConfig, StockConfig
from pdv.data.database import Database
from pdv.data.seed import (
    DEMO_MANAGER_LOGIN,
    DEMO_MANAGER_PIN,
    DEMO_OPERATOR_ID,
    DEMO_OPERATOR_PIN,
    seed_demo_data,
)
from pdv.domain.models import new_id
from pdv.edge.auth import EdgeAuth

TENANT = "11111111-1111-1111-1111-111111111111"
STORE = "22222222-2222-2222-2222-222222222222"
DEVICE = "33333333-3333-3333-3333-333333333333"


@pytest.fixture()
def client(tmp_path: Path):  # noqa: ANN201
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    config = AppConfig(
        tenant_id=TENANT,
        store_id=STORE,
        device_id=DEVICE,
        store_name="Confeitaria Aurora",
        database_path=tmp_path / "pdv.db",
        printer=PrinterConfig(backend="file", output_dir=tmp_path / "out"),
        stock=StockConfig(),
    )
    database = Database(config.database_path)
    database.migrate()
    seed_demo_data(database, config)

    from pdv.edge.server import create_app

    with fastapi_testclient.TestClient(create_app(database, config)) as test_client:
        test_client.pdv_database = database  # type: ignore[attr-defined]
        test_client.pdv_config = config  # type: ignore[attr-defined]
        yield test_client


@pytest.fixture()
def phone(client):  # noqa: ANN001, ANN201
    """Um celular pareado, como o do garçom."""
    auth = EdgeAuth(client.pdv_database, TENANT, STORE)
    response = client.post(
        "/pair",
        json={"code": auth.create_pairing_code(), "device_name": "Celular do João"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


@pytest.fixture()
def manager(client, phone):  # noqa: ANN001, ANN201
    response = client.post(
        "/manager/session",
        json={"login": DEMO_MANAGER_LOGIN, "pin": DEMO_MANAGER_PIN},
        headers=phone,
    )
    assert response.status_code == 200, response.text
    return {**phone, "X-Manager-Token": response.json()["token"]}


def _table(client, phone, label: str) -> dict:  # noqa: ANN001
    tables = client.get("/tables", headers=phone).json()["tables"]
    return next(t for t in tables if t["label"] == label)


def _open(client, phone, label: str) -> dict:  # noqa: ANN001
    return client.post(
        "/orders",
        json={
            "client_uuid": new_id(),
            "operator_id": DEMO_OPERATOR_ID,
            "table_id": _table(client, phone, label)["id"],
        },
        headers=phone,
    )


# --------------------------------------------------------------------------- #
# O app web
# --------------------------------------------------------------------------- #


def test_the_root_serves_the_waiter_app(client) -> None:  # noqa: ANN001
    """O sintoma relatado: abrir o endereço do PDV no celular dava 404."""
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>" in response.text


def test_the_app_is_small_enough_for_the_store_wifi(client) -> None:  # noqa: ANN001
    """Carrega no celular mais velho da equipe, pelo Wi-Fi da loja."""
    assert len(client.get("/").content) < 128 * 1024


def test_the_app_can_be_pinned_to_the_home_screen(client) -> None:  # noqa: ANN001
    """Atalho em tela cheia é a diferença entre "um site" e "o app do salão"."""
    manifest = client.get("/manifest.webmanifest").json()

    assert manifest["display"] == "standalone"
    assert "Confeitaria Aurora" in manifest["name"]


def test_the_app_itself_needs_no_token(client) -> None:  # noqa: ANN001
    """A página é pública; **os dados** não.

    Exigir token para servir o HTML impediria a própria tela de pareamento de
    carregar — o aparelho ainda não tem token nenhum.
    """
    assert client.get("/").status_code == 200
    assert client.get("/tables").status_code == 401


# --------------------------------------------------------------------------- #
# Mapa de mesas
# --------------------------------------------------------------------------- #


def test_the_map_comes_with_occupancy_in_one_call(client, phone) -> None:  # noqa: ANN001
    """Uma chamada por mesa faria o celular disparar trinta a cada refresh."""
    _open(client, phone, "Mesa 2")

    tables = client.get("/tables", headers=phone).json()["tables"]

    assert len(tables) == 10
    assert next(t for t in tables if t["label"] == "Mesa 2")["status"] == "busy"
    assert next(t for t in tables if t["label"] == "Mesa 3")["status"] == "free"


def test_opening_an_occupied_table_returns_the_existing_order(client, phone) -> None:  # noqa: ANN001
    """O bug relatado, pela rota.

    O 409 traz a comanda no corpo justamente para o app abrir **essa** — tocar
    numa mesa ocupada é querer lançar nela, não receber um erro.
    """
    first = _open(client, phone, "Mesa 4").json()

    conflict = _open(client, phone, "Mesa 4")

    assert conflict.status_code == 409
    assert conflict.json()["detail"]["order"]["order_id"] == first["order_id"]


def test_an_unknown_table_is_a_404_not_a_new_table(client, phone) -> None:  # noqa: ANN001
    response = client.post(
        "/orders",
        json={
            "client_uuid": new_id(),
            "operator_id": DEMO_OPERATOR_ID,
            "table_label": "Mesa do fundo",
        },
        headers=phone,
    )

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Fechar a conta
# --------------------------------------------------------------------------- #


def test_the_waiter_asks_for_the_bill_and_the_map_shows_it(client, phone) -> None:  # noqa: ANN001
    order = _open(client, phone, "Mesa 5").json()

    response = client.post(f"/orders/{order['order_id']}/bill", headers=phone)

    assert response.status_code == 200
    assert response.json()["status"] == "open", "pedir a conta não fecha a venda"
    assert _table(client, phone, "Mesa 5")["status"] == "billing"


def test_the_bill_request_can_be_undone(client, phone) -> None:  # noqa: ANN001
    order = _open(client, phone, "Mesa 5").json()
    client.post(f"/orders/{order['order_id']}/bill", headers=phone)

    client.delete(f"/orders/{order['order_id']}/bill", headers=phone)

    assert _table(client, phone, "Mesa 5")["status"] == "busy"


def test_the_order_detail_lists_the_items(client, phone) -> None:  # noqa: ANN001
    """A tela da comanda precisa dos itens, não só do total."""
    order = _open(client, phone, "Mesa 6").json()
    product = next(
        p
        for p in client.get("/menu", headers=phone).json()["products"]
        if p["sellable_by_waiter"]
    )
    client.post(
        f"/orders/{order['order_id']}/items",
        json={"client_uuid": new_id(), "product_id": product["id"], "quantity": "2"},
        headers=phone,
    )

    detail = client.get(f"/orders/{order['order_id']}", headers=phone).json()

    assert len(detail["items"]) == 1
    assert detail["items"][0]["product_name"] == product["name"]
    assert detail["items"][0]["kds_status"] == "queued"
    assert detail["total_cents"] == product["price_cents"] * 2


# --------------------------------------------------------------------------- #
# Opções de gerente
# --------------------------------------------------------------------------- #


def test_the_phone_alone_cannot_configure_the_floor_plan(client, phone) -> None:  # noqa: ANN001
    """O aparelho pareado não é a pessoa.

    O celular fica em cima do balcão a noite inteira; se o pareamento bastasse,
    qualquer um que passasse por ali cadastraria e cancelaria.
    """
    for method, path, body in (
        ("post", "/tables", {"label": "Mesa 99"}),
        ("post", "/tables/seed", {"count": 4}),
    ):
        response = getattr(client, method)(path, json=body, headers=phone)
        assert response.status_code == 403, f"{path} respondeu {response.status_code}"


def test_the_phone_alone_cannot_cancel_an_order(client, phone) -> None:  # noqa: ANN001
    """A comida sai, a comanda some. É o vetor de furto do salão."""
    order = _open(client, phone, "Mesa 7").json()

    response = client.post(
        f"/orders/{order['order_id']}/cancel",
        json={"reason": "sumiu"},
        headers=phone,
    )

    assert response.status_code == 403


def test_the_cashier_pin_does_not_unlock_the_manager_options(client, phone) -> None:  # noqa: ANN001
    """Ana tem PIN válido e, de propósito, não autoriza."""
    response = client.post(
        "/manager/session",
        json={"login": "ana", "pin": DEMO_OPERATOR_PIN},
        headers=phone,
    )

    assert response.status_code == 403


def test_a_manager_creates_a_table(client, manager) -> None:  # noqa: ANN001
    response = client.post(
        "/tables", json={"label": "Balcão 1", "area": "Balcão", "seats": 2},
        headers=manager,
    )

    assert response.status_code == 200
    assert response.json()["label"] == "Balcão 1"
    assert _table(client, manager, "Balcão 1")["area"] == "Balcão"


def test_a_repeated_table_name_is_a_conflict(client, manager) -> None:  # noqa: ANN001
    response = client.post("/tables", json={"label": "mesa 1"}, headers=manager)

    assert response.status_code == 409


def test_a_manager_renames_and_retires_a_table(client, manager) -> None:  # noqa: ANN001
    table = _table(client, manager, "Varanda 1")

    renamed = client.patch(
        f"/tables/{table['id']}", json={"label": "Varanda A", "seats": 6},
        headers=manager,
    )
    assert renamed.status_code == 200
    assert renamed.json()["seats"] == 6

    retired = client.patch(
        f"/tables/{table['id']}",
        json={"label": "Varanda A", "is_active": False},
        headers=manager,
    )
    assert retired.status_code == 200
    labels = [t["label"] for t in client.get("/tables", headers=manager).json()["tables"]]
    assert "Varanda A" not in labels


def test_a_busy_table_cannot_be_retired_over_http(client, manager) -> None:  # noqa: ANN001
    _open(client, manager, "Mesa 1")
    table = _table(client, manager, "Mesa 1")

    response = client.patch(
        f"/tables/{table['id']}", json={"label": "Mesa 1", "is_active": False},
        headers=manager,
    )

    assert response.status_code == 409


def test_a_manager_seeds_the_floor_plan(client, manager) -> None:  # noqa: ANN001
    response = client.post("/tables/seed", json={"count": 12}, headers=manager)

    assert response.status_code == 200
    assert response.json()["created"] == 4, "Mesa 1 a 8 já existem"


def test_a_manager_cancels_an_order_and_frees_the_table(client, manager) -> None:  # noqa: ANN001
    order = _open(client, manager, "Mesa 8").json()

    response = client.post(
        f"/orders/{order['order_id']}/cancel",
        json={"reason": "Cliente desistiu antes de servir"},
        headers=manager,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "canceled"
    assert _table(client, manager, "Mesa 8")["status"] == "free"


def test_cancelling_demands_a_reason(client, manager) -> None:  # noqa: ANN001
    """Motivo em branco é um cancelamento que ninguém explica depois."""
    order = _open(client, manager, "Mesa 8").json()

    response = client.post(
        f"/orders/{order['order_id']}/cancel", json={"reason": " "}, headers=manager
    )

    assert response.status_code == 422


def test_a_manager_transfers_a_table(client, manager) -> None:  # noqa: ANN001
    order = _open(client, manager, "Mesa 1").json()
    destination = _table(client, manager, "Varanda 2")

    response = client.post(
        f"/orders/{order['order_id']}/transfer",
        json={"table_id": destination["id"]},
        headers=manager,
    )

    assert response.status_code == 200
    assert response.json()["table_label"] == "Varanda 2"
    assert _table(client, manager, "Mesa 1")["status"] == "free"


def test_transferring_onto_a_busy_table_is_a_conflict(client, manager) -> None:  # noqa: ANN001
    order = _open(client, manager, "Mesa 1").json()
    _open(client, manager, "Mesa 2")

    response = client.post(
        f"/orders/{order['order_id']}/transfer",
        json={"table_id": _table(client, manager, "Mesa 2")["id"]},
        headers=manager,
    )

    assert response.status_code == 409


def test_a_grant_does_not_travel_to_another_phone(client, manager) -> None:  # noqa: ANN001
    """Token de gerente vazado não vale no aparelho ao lado."""
    auth = EdgeAuth(client.pdv_database, TENANT, STORE)
    other = client.post(
        "/pair",
        json={"code": auth.create_pairing_code(), "device_name": "Celular da Ana"},
    ).json()

    response = client.post(
        "/tables",
        json={"label": "Mesa 50"},
        headers={
            "Authorization": f"Bearer {other['token']}",
            "X-Manager-Token": manager["X-Manager-Token"],
        },
    )

    assert response.status_code == 403


def test_the_manager_can_hand_the_grant_back(client, manager) -> None:  # noqa: ANN001
    assert client.get("/manager/session", headers=manager).status_code == 200

    client.delete("/manager/session", headers=manager)

    assert client.get("/manager/session", headers=manager).status_code == 403
