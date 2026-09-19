"""Testes do servidor local — app do garçom e KDS (Fase 3).

O cenário que estes testes existem para cobrir é o Wi-Fi da loja, que cai atrás
da geladeira e volta. O celular reenvia sem saber se o pedido entrou.

**O modo de falha mais caro do sistema não é perder um pedido — é faturar o
mesmo duas vezes.** O app tem duas rotas até a nuvem (a LAN, por este terminal,
e a internet, direto) e escolhe uma sem poder confirmar que a outra não
entregou. O que faz as duas convergirem é o `client_uuid` gerado no celular ser
preservado ponta a ponta. É esse invariante que a maior parte deste arquivo
protege.
"""

from __future__ import annotations

import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest

from pdv.config import AppConfig, PrinterConfig
from pdv.data.database import Database
from pdv.data.repositories import ProductRepository
from pdv.data.seed import DEMO_OPERATOR_ID, seed_demo_data
from pdv.domain.models import EntityId, new_id
from pdv.edge.auth import DeviceAuthError, EdgeAuth, PairingError
from pdv.edge.hub import Event, EventHub
from pdv.edge.kds import InvalidTransitionError, KdsService, TicketNotFoundError
from pdv.edge.orders import (
    OrderClosedError,
    OrderNotFoundError,
    ProductNotSellableError,
    TableOrderService,
)

TENANT = "11111111-1111-1111-1111-111111111111"
STORE = "22222222-2222-2222-2222-222222222222"
DEVICE = "33333333-3333-3333-3333-333333333333"


@pytest.fixture()
def env(tmp_path: Path):  # noqa: ANN201
    config = AppConfig(
        tenant_id=TENANT,
        store_id=STORE,
        device_id=DEVICE,
        database_path=tmp_path / "pdv.db",
        printer=PrinterConfig(backend="file", output_dir=tmp_path / "out"),
    )
    database = Database(config.database_path)
    database.migrate()
    seed_demo_data(database, config)

    hub = EventHub()
    return (
        database,
        config,
        hub,
        TableOrderService(database, config, hub),
        KdsService(database, config, hub),
        EdgeAuth(database, TENANT, STORE),
    )


def _unit_products(database: Database):  # noqa: ANN202
    products = ProductRepository(database.connection).list_active(EntityId(TENANT))
    return [p for p in products if not p.is_weighed]


def _weighed_product(database: Database):  # noqa: ANN202
    products = ProductRepository(database.connection).list_active(EntityId(TENANT))
    return next(p for p in products if p.is_weighed)


def _open(orders: TableOrderService, uuid: str | None = None, table: str = "Mesa 7"):  # noqa: ANN202
    return orders.open_order(
        client_uuid=EntityId(uuid or new_id()),
        operator_id=EntityId(DEMO_OPERATOR_ID),
        table_label=table,
        origin_device_id=EntityId("cel-ana"),
    )


# --------------------------------------------------------------------------- #
# Idempotência — o invariante que impede faturamento duplicado
# --------------------------------------------------------------------------- #


def test_resending_an_order_does_not_create_a_second_one(env) -> None:  # noqa: ANN001
    """O Wi-Fi caiu depois de gravar e antes da resposta chegar ao celular.

    O app não tem como distinguir isso de "não chegou", então reenvia. Se o
    reenvio abrisse outro pedido, a mesma mesa seria cobrada duas vezes.
    """
    database, _config, _hub, orders, _kds, _auth = env
    uuid = new_id()

    first = _open(orders, uuid)
    second = _open(orders, uuid)

    assert first.id == second.id
    assert first.local_number == second.local_number
    total = database.query_one("SELECT COUNT(*) AS n FROM orders")["n"]
    assert total == 1


def test_the_phones_uuid_is_what_gets_stored(env) -> None:  # noqa: ANN001
    """O terminal **não** inventa `client_uuid` próprio.

    Se inventasse, a mesma comanda chegaria à nuvem com dois identificadores —
    um vindo do PDV, outro vindo do celular pela rota da internet — e a
    deduplicação por `(tenant_id, client_uuid)` não veria relação entre eles.
    """
    database, _config, _hub, orders, _kds, _auth = env
    uuid = new_id()

    order = _open(orders, uuid)

    assert order.client_uuid == uuid
    row = database.query_one("SELECT client_uuid FROM orders WHERE id = ?", (order.id,))
    assert row["client_uuid"] == uuid

    # E é esse mesmo uuid que sobe na fila de sincronização.
    outbox = database.query_one(
        "SELECT client_uuid FROM sync_outbox WHERE entity_table = 'orders'"
    )
    assert outbox["client_uuid"] == uuid


def test_resending_an_item_does_not_charge_twice(env) -> None:  # noqa: ANN001
    database, _config, _hub, orders, _kds, _auth = env
    product = _unit_products(database)[0]
    order = _open(orders)
    item_uuid = new_id()

    after_first = orders.add_item(
        order_id=order.id,
        client_uuid=EntityId(item_uuid),
        product_id=product.id,
        quantity=Decimal("2"),
    )
    after_resend = orders.add_item(
        order_id=order.id,
        client_uuid=EntityId(item_uuid),
        product_id=product.id,
        quantity=Decimal("2"),
    )

    assert after_first.total_cents == after_resend.total_cents
    assert after_resend.item_count == 1
    tickets = database.query_one("SELECT COUNT(*) AS n FROM kds_tickets")["n"]
    assert tickets == 1, "reenvio não pode mandar o prato duas vezes para a cozinha"


def test_concurrent_resends_settle_on_one_order(env) -> None:  # noqa: ANN001
    """Dois reenvios simultâneos do mesmo celular (o usuário tocou duas vezes)."""
    database, _config, _hub, orders, _kds, _auth = env
    uuid = new_id()
    results: list[object] = []
    barrier = threading.Barrier(2)

    def attempt() -> None:
        barrier.wait()
        try:
            results.append(_open(orders, uuid))
        except Exception as exc:  # noqa: BLE001
            results.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert database.query_one("SELECT COUNT(*) AS n FROM orders")["n"] == 1
    assert not any(isinstance(r, Exception) for r in results), results


# --------------------------------------------------------------------------- #
# Regras de venda no salão
# --------------------------------------------------------------------------- #


def test_weighed_products_are_refused_on_the_phone(env) -> None:  # noqa: ANN001
    """Peso digitado por quem cobra é exatamente o furto que o M09 combate.

    Quem pesa é a balança do balcão, que guarda o quadro cru como prova pericial.
    """
    database, _config, _hub, orders, _kds, _auth = env
    order = _open(orders)

    with pytest.raises(ProductNotSellableError, match="balança"):
        orders.add_item(
            order_id=order.id,
            client_uuid=EntityId(new_id()),
            product_id=_weighed_product(database).id,
            quantity=Decimal("1"),
        )


def test_price_comes_from_the_catalogue_not_from_the_phone(env) -> None:  # noqa: ANN001
    """O app envia produto e quantidade — nunca o preço."""
    database, _config, _hub, orders, _kds, _auth = env
    product = _unit_products(database)[0]
    order = _open(orders)

    updated = orders.add_item(
        order_id=order.id,
        client_uuid=EntityId(new_id()),
        product_id=product.id,
        quantity=Decimal("3"),
    )

    assert int(updated.total_cents) == int(product.price_cents) * 3


def test_quantity_must_be_positive(env) -> None:  # noqa: ANN001
    database, _config, _hub, orders, _kds, _auth = env
    order = _open(orders)

    with pytest.raises(ProductNotSellableError):
        orders.add_item(
            order_id=order.id,
            client_uuid=EntityId(new_id()),
            product_id=_unit_products(database)[0].id,
            quantity=Decimal("0"),
        )


def test_unknown_order_is_reported_not_created(env) -> None:  # noqa: ANN001
    database, _config, _hub, orders, _kds, _auth = env

    with pytest.raises(OrderNotFoundError):
        orders.add_item(
            order_id=EntityId("pedido-que-nao-existe"),
            client_uuid=EntityId(new_id()),
            product_id=_unit_products(database)[0].id,
            quantity=Decimal("1"),
        )


def test_closed_order_cannot_receive_items(env) -> None:  # noqa: ANN001
    """Venda fechada altera-se por estorno, não por edição."""
    database, _config, _hub, orders, _kds, _auth = env
    order = _open(orders)

    with database.transaction() as connection:
        connection.execute(
            "UPDATE orders SET status = 'paid' WHERE id = ?", (order.id,)
        )

    with pytest.raises(OrderClosedError):
        orders.add_item(
            order_id=order.id,
            client_uuid=EntityId(new_id()),
            product_id=_unit_products(database)[0].id,
            quantity=Decimal("1"),
        )


def test_each_table_is_its_own_order(env) -> None:  # noqa: ANN001
    """O salão não tem "venda atual": oito mesas abertas ao mesmo tempo."""
    database, _config, _hub, orders, _kds, _auth = env
    product = _unit_products(database)[0]

    mesa3 = _open(orders, table="Mesa 3")
    mesa7 = _open(orders, table="Mesa 7")
    orders.add_item(
        order_id=mesa3.id,
        client_uuid=EntityId(new_id()),
        product_id=product.id,
        quantity=Decimal("1"),
    )

    assert orders.get_order(mesa7.id).total_cents == 0
    assert orders.get_order(mesa3.id).total_cents == product.price_cents
    assert {o.table_label for o in orders.list_open_orders()} == {"Mesa 3", "Mesa 7"}


# --------------------------------------------------------------------------- #
# KDS
# --------------------------------------------------------------------------- #


def _queued_ticket(env, notes: str = ""):  # noqa: ANN001, ANN202
    database, _config, _hub, orders, kds, _auth = env
    order = _open(orders)
    orders.add_item(
        order_id=order.id,
        client_uuid=EntityId(new_id()),
        product_id=_unit_products(database)[0].id,
        quantity=Decimal("1"),
        notes=notes,
    )
    return kds.list_active()[0]


def test_item_reaches_the_kitchen_queue(env) -> None:  # noqa: ANN001
    ticket = _queued_ticket(env, notes="sem açúcar")

    assert ticket.status == "queued"
    assert ticket.notes == "sem açúcar"
    assert ticket.table_label == "Mesa 7"


def test_bump_walks_the_kitchen_lifecycle(env) -> None:  # noqa: ANN001
    _database, _config, _hub, _orders, kds, _auth = env
    ticket = _queued_ticket(env)

    assert kds.bump(ticket.id).status == "preparing"
    assert kds.bump(ticket.id).status == "ready"
    assert kds.bump(ticket.id).status == "delivered"


def test_recall_undoes_the_last_step(env) -> None:  # noqa: ANN001
    """A cozinha bateu pronto no prato errado.

    Desfazer não pode exigir cancelar o item — cancelamento mexe na venda e é
    decisão de gerente, não de quem está na chapa.
    """
    _database, _config, _hub, _orders, kds, _auth = env
    ticket = _queued_ticket(env)
    kds.bump(ticket.id)
    kds.bump(ticket.id)

    assert kds.recall(ticket.id).status == "preparing"


def test_recall_clears_the_abandoned_timestamp(env) -> None:  # noqa: ANN001
    """`ready_at` de um ticket que voltou faria o tempo de preparo mentir."""
    database, _config, _hub, _orders, kds, _auth = env
    ticket = _queued_ticket(env)
    kds.bump(ticket.id)
    kds.bump(ticket.id)
    assert database.query_one(
        "SELECT ready_at FROM kds_tickets WHERE id = ?", (ticket.id,)
    )["ready_at"]

    kds.recall(ticket.id)

    assert not database.query_one(
        "SELECT ready_at FROM kds_tickets WHERE id = ?", (ticket.id,)
    )["ready_at"]


def test_invalid_transitions_are_refused(env) -> None:  # noqa: ANN001
    """Uma tela com versão antiga não inventa estado no banco da loja."""
    _database, _config, _hub, _orders, kds, _auth = env
    ticket = _queued_ticket(env)

    with pytest.raises(InvalidTransitionError):
        kds.advance(ticket.id, "delivered")
    with pytest.raises(InvalidTransitionError):
        kds.advance(ticket.id, "inventado")


def test_delivered_tickets_leave_the_active_queue(env) -> None:  # noqa: ANN001
    """A tela mostra o que falta fazer; histórico é relatório."""
    _database, _config, _hub, _orders, kds, _auth = env
    ticket = _queued_ticket(env)
    for _ in range(3):
        kds.bump(ticket.id)

    assert kds.list_active() == []


def test_unknown_ticket_is_reported(env) -> None:  # noqa: ANN001
    _database, _config, _hub, _orders, kds, _auth = env

    with pytest.raises(TicketNotFoundError):
        kds.bump(EntityId("ticket-inexistente"))


def test_late_flag_uses_time_since_the_customer_ordered(env) -> None:  # noqa: ANN001
    """O que importa ao cliente é há quanto tempo ele pediu."""
    database, _config, _hub, _orders, kds, _auth = env
    ticket = _queued_ticket(env)

    assert ticket.is_late is False
    with database.transaction() as connection:
        connection.execute(
            "UPDATE kds_tickets SET queued_at = '2020-01-01T00:00:00Z' WHERE id = ?",
            (ticket.id,),
        )

    assert kds.list_active()[0].is_late is True


# --------------------------------------------------------------------------- #
# Barramento de eventos
# --------------------------------------------------------------------------- #


def test_order_reaches_the_kitchen_screen_in_under_a_second(env) -> None:  # noqa: ANN001
    """O critério de aceite da Fase 3, medido.

    Wi-Fi isolado, sem rota para a internet: o pedido lançado no celular tem de
    aparecer no KDS em menos de 1 s. Aqui não há rede envolvida — só o caminho
    gravar → publicar → entregar —, então isto mede o piso do sistema.
    """
    database, _config, hub, orders, _kds, _auth = env
    subscription = hub.subscribe({"ticket.queued"})
    order = _open(orders)

    started = time.perf_counter()
    orders.add_item(
        order_id=order.id,
        client_uuid=EntityId(new_id()),
        product_id=_unit_products(database)[0].id,
        quantity=Decimal("1"),
    )
    event = subscription.get(timeout=1.0)
    elapsed = time.perf_counter() - started

    assert event is not None, "o KDS não recebeu o pedido em 1 s"
    assert elapsed < 1.0
    assert event.payload["table_label"] == "Mesa 7"


def test_a_frozen_screen_never_blocks_the_sale(env) -> None:  # noqa: ANN001
    """Tablet travado na cozinha não pode segurar quem está gravando a venda.

    A fila enche, o evento mais antigo é descartado e o publicador segue. O
    contrário — publicador esperando o assinante — pararia o caixa por causa de
    um tablet com Wi-Fi ruim.
    """
    _database, _config, hub, _orders, _kds, _auth = env
    subscription = hub.subscribe()

    started = time.perf_counter()
    for i in range(1000):
        hub.publish(Event("ticket.queued", {"n": i}))
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0
    assert subscription.dropped > 0, "a fila deveria ter descartado o excesso"


def test_the_stale_events_are_dropped_not_the_fresh_ones(env) -> None:  # noqa: ANN001
    """Uma tela atrasada precisa do estado atual, não do histórico perdido."""
    _database, _config, hub, _orders, _kds, _auth = env
    subscription = hub.subscribe()

    for i in range(500):
        hub.publish(Event("ticket.queued", {"n": i}))

    received = []
    while (event := subscription.get(timeout=0.01)) is not None:
        received.append(event.payload["n"])

    assert received[-1] == 499, "o evento mais novo tem de sobreviver"
    assert received[0] > 0, "os mais antigos é que saem"


def test_subscribers_only_get_their_topics(env) -> None:  # noqa: ANN001
    _database, _config, hub, _orders, _kds, _auth = env
    kitchen = hub.subscribe({"ticket.queued"})
    everything = hub.subscribe()

    hub.publish(Event("order.opened", {"x": 1}))

    assert kitchen.get(timeout=0.01) is None
    assert everything.get(timeout=0.01) is not None


def test_closing_a_subscription_removes_it(env) -> None:  # noqa: ANN001
    """Tela que desconectou não pode continuar acumulando fila para sempre."""
    _database, _config, hub, _orders, _kds, _auth = env

    with hub.subscribe():
        assert hub.subscriber_count == 1
    assert hub.subscriber_count == 0


# --------------------------------------------------------------------------- #
# Pareamento e autenticação
# --------------------------------------------------------------------------- #


def test_pairing_requires_a_code_from_the_till(env) -> None:  # noqa: ANN001
    """Estar na LAN não autoriza nada: a âncora é o acesso físico ao caixa."""
    _database, _config, _hub, _orders, _kds, auth = env

    with pytest.raises(PairingError):
        auth.pair("000000", device_name="Celular invasor")


def test_paired_device_authenticates_with_its_token(env) -> None:  # noqa: ANN001
    _database, _config, _hub, _orders, _kds, auth = env
    code, _ = auth.create_pairing_code()

    token = auth.pair(code, device_name="Celular da Ana")
    device = auth.authenticate(token)

    assert device.name == "Celular da Ana"
    assert device.kind == "waiter"


def test_a_pairing_code_works_exactly_once(env) -> None:  # noqa: ANN001
    """O código fica visível na tela do caixa; quem passa pelo balcão lê."""
    _database, _config, _hub, _orders, _kds, auth = env
    code, _ = auth.create_pairing_code()
    auth.pair(code, device_name="Celular da Ana")

    with pytest.raises(PairingError):
        auth.pair(code, device_name="Celular do ladrão")


def test_expired_code_is_refused(env) -> None:  # noqa: ANN001
    database, _config, _hub, _orders, _kds, auth = env
    code, _ = auth.create_pairing_code()
    with database.transaction() as connection:
        connection.execute(
            "UPDATE edge_pairing_codes SET expires_at = '2020-01-01T00:00:00Z'"
        )

    with pytest.raises(PairingError):
        auth.pair(code, device_name="Celular atrasado")


def test_the_raw_token_is_never_stored(env) -> None:  # noqa: ANN001
    """Dump do banco — que o operador consegue abrir — não entrega credencial."""
    database, _config, _hub, _orders, _kds, auth = env
    token = auth.pair(auth.create_pairing_code()[0], device_name="Celular da Ana")

    stored = database.query_all("SELECT token_hash FROM edge_devices")
    assert all(row["token_hash"] != token for row in stored)
    assert all(token not in str(row["token_hash"]) for row in stored)


def test_revocation_takes_effect_immediately(env) -> None:  # noqa: ANN001
    """Celular perdido se revoga do caixa, e vale já — não em alguns minutos."""
    _database, _config, _hub, _orders, _kds, auth = env
    token = auth.pair(auth.create_pairing_code()[0], device_name="Celular perdido")
    device = auth.authenticate(token)

    assert auth.revoke(device.id) is True

    with pytest.raises(DeviceAuthError, match="revogado"):
        auth.authenticate(token)


def test_unknown_and_missing_tokens_are_refused(env) -> None:  # noqa: ANN001
    _database, _config, _hub, _orders, _kds, auth = env

    with pytest.raises(DeviceAuthError):
        auth.authenticate(None)
    with pytest.raises(DeviceAuthError):
        auth.authenticate("token-inventado")


def test_pairing_records_last_seen(env) -> None:  # noqa: ANN001
    """É assim que o caixa mostra quais celulares estão online."""
    _database, _config, _hub, _orders, _kds, auth = env
    token = auth.pair(auth.create_pairing_code()[0], device_name="Celular da Ana")
    auth.authenticate(token)

    assert auth.list_devices()[0]["last_seen_at"] is not None


# --------------------------------------------------------------------------- #
# Camada HTTP
# --------------------------------------------------------------------------- #
#
# Estes testes existem porque um bug real passou por toda a suíte acima: com
# `from __future__ import annotations`, o FastAPI não conseguia resolver os
# modelos declarados dentro de `create_app` e rebaixava cada parâmetro a query
# string. O servidor subia, as rotas existiam e **toda** requisição respondia
# 422 reclamando de um parâmetro que ninguém declarou. Testar só os serviços
# nunca teria mostrado isso.


@pytest.fixture()
def client(env):  # noqa: ANN001, ANN201
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    database, config, hub, _orders, _kds, _auth = env

    from pdv.edge.server import create_app

    with fastapi_testclient.TestClient(create_app(database, config, hub)) as test_client:
        yield test_client


@pytest.fixture()
def device_headers(env, client):  # noqa: ANN001, ANN201
    """Só o aparelho: serve para ler o mapa, não para lançar."""
    _database, _config, _hub, _orders, _kds, auth = env
    code, _ = auth.create_pairing_code()
    response = client.post(
        "/pair", json={"code": code, "device_name": "Celular da Ana"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


@pytest.fixture()
def headers(client, device_headers):  # noqa: ANN001, ANN201
    """Aparelho **e** pessoa — o que as rotas de escrita exigem."""
    from pdv.data.seed import DEMO_WAITER_LOGIN, DEMO_WAITER_PIN

    response = client.post(
        "/staff/session",
        json={"login": DEMO_WAITER_LOGIN, "pin": DEMO_WAITER_PIN},
        headers=device_headers,
    )
    assert response.status_code == 200, response.text
    return {**device_headers, "X-Staff-Token": response.json()["token"]}


def test_health_is_open_and_says_which_store(client) -> None:  # noqa: ANN001
    """É por ela que o app confirma que achou o PDV certo na LAN."""
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["store_id"] == STORE


def test_business_routes_demand_a_token(client) -> None:  # noqa: ANN001
    """Estar na LAN não autoriza: a rede da loja é a do Wi-Fi do cliente."""
    for method, path in (
        ("get", "/menu"),
        ("get", "/orders"),
        ("get", "/kds/tickets"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code == 401, f"{path} respondeu {response.status_code}"


def test_a_wrong_pairing_code_is_forbidden(client) -> None:  # noqa: ANN001
    response = client.post("/pair", json={"code": "000000", "device_name": "x"})

    assert response.status_code == 403


def test_menu_marks_what_the_waiter_cannot_sell(client, headers) -> None:  # noqa: ANN001
    """O app esconde o item por peso em vez de deixar o garçom errar na mesa."""
    products = client.get("/menu", headers=headers).json()["products"]

    assert products
    assert any(p["sellable_by_waiter"] for p in products)
    assert any(not p["sellable_by_waiter"] for p in products)


def test_order_lifecycle_over_http(env, client, headers) -> None:  # noqa: ANN001
    database, _config, _hub, _orders, _kds, _auth = env
    product = _unit_products(database)[0]
    order_uuid = new_id()

    created = client.post(
        "/orders",
        headers=headers,
        json={
            "client_uuid": order_uuid,
            "table_label": "Mesa 7",
            "operator_id": DEMO_OPERATOR_ID,
        },
    )
    assert created.status_code == 200, created.text
    order_id = created.json()["order_id"]

    added = client.post(
        f"/orders/{order_id}/items",
        headers=headers,
        json={
            "client_uuid": new_id(),
            "product_id": product.id,
            "quantity": "2",
            "notes": "sem açúcar",
        },
    )
    assert added.status_code == 200, added.text
    assert added.json()["total_cents"] == int(product.price_cents) * 2

    # O reenvio do pedido devolve o mesmo, não abre outro.
    resent = client.post(
        "/orders",
        headers=headers,
        json={
            "client_uuid": order_uuid,
            "table_label": "Mesa 7",
            "operator_id": DEMO_OPERATOR_ID,
        },
    )
    assert resent.json()["order_id"] == order_id
    assert database.query_one("SELECT COUNT(*) AS n FROM orders")["n"] == 1


def test_weighed_item_over_http_is_a_conflict(env, client, headers) -> None:  # noqa: ANN001
    database, _config, _hub, _orders, _kds, _auth = env
    order = client.post(
        "/orders",
        headers=headers,
        json={
            "client_uuid": new_id(),
            "table_label": "Mesa 3",
            "operator_id": DEMO_OPERATOR_ID,
        },
    ).json()

    response = client.post(
        f"/orders/{order['order_id']}/items",
        headers=headers,
        json={
            "client_uuid": new_id(),
            "product_id": _weighed_product(database).id,
            "quantity": "1",
        },
    )

    assert response.status_code == 409
    assert "balança" in response.json()["detail"]


def test_unknown_order_over_http_is_404(client, headers) -> None:  # noqa: ANN001
    response = client.post(
        "/orders/nao-existe/items",
        headers=headers,
        json={"client_uuid": new_id(), "product_id": "x", "quantity": "1"},
    )

    assert response.status_code == 404


def test_malformed_quantity_is_rejected(env, client, headers) -> None:  # noqa: ANN001
    database, _config, _hub, _orders, _kds, _auth = env
    order = client.post(
        "/orders",
        headers=headers,
        json={
            "client_uuid": new_id(),
            "table_label": "Mesa 3",
            "operator_id": DEMO_OPERATOR_ID,
        },
    ).json()

    response = client.post(
        f"/orders/{order['order_id']}/items",
        headers=headers,
        json={
            "client_uuid": new_id(),
            "product_id": _unit_products(database)[0].id,
            "quantity": "dois",
        },
    )

    assert response.status_code == 422


def test_revoking_a_device_closes_the_door_at_once(env, client, headers) -> None:  # noqa: ANN001
    _database, _config, _hub, _orders, _kds, auth = env
    assert client.get("/menu", headers=headers).status_code == 200

    auth.revoke(EntityId(str(auth.list_devices()[0]["id"])))

    assert client.get("/menu", headers=headers).status_code == 401


def test_kds_stream_refuses_an_unpaired_device(client) -> None:  # noqa: ANN001
    """O WebSocket autentica no handshake, antes de aceitar a conexão."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/kds/stream?token=inventado") as ws:
            ws.receive_json()


def test_kds_stream_opens_with_the_full_queue(env, client, headers) -> None:  # noqa: ANN001
    """Tela que reconecta precisa do estado inteiro, não só do que mudar depois."""
    database, _config, _hub, orders, _kds, _auth = env
    order = _open(orders)
    orders.add_item(
        order_id=order.id,
        client_uuid=EntityId(new_id()),
        product_id=_unit_products(database)[0].id,
        quantity=Decimal("1"),
    )
    token = headers["Authorization"].removeprefix("Bearer ")

    with client.websocket_connect(f"/kds/stream?token={token}") as ws:
        snapshot = ws.receive_json()

    assert snapshot["kind"] == "snapshot"
    assert len(snapshot["tickets"]) == 1


def test_kds_bump_over_http_and_invalid_action(env, client, headers) -> None:  # noqa: ANN001
    database, _config, _hub, orders, _kds, _auth = env
    order = _open(orders)
    orders.add_item(
        order_id=order.id,
        client_uuid=EntityId(new_id()),
        product_id=_unit_products(database)[0].id,
        quantity=Decimal("1"),
    )
    ticket_id = client.get("/kds/tickets", headers=headers).json()["tickets"][0][
        "ticket_id"
    ]

    assert (
        client.post(f"/kds/tickets/{ticket_id}/bump", headers=headers).json()["status"]
        == "preparing"
    )
    assert (
        client.post(f"/kds/tickets/{ticket_id}/explodir", headers=headers).status_code
        == 404
    )
