"""Mesas do salão, fechamento de conta e opções de gerente — Fase 3.6.

Três bugs motivaram este arquivo, e cada um tem um teste com o nome do que
acontecia na loja:

1. **A mesa abria duas vezes.** Dois garçons tocando na "Mesa 5" criavam duas
   comandas para o mesmo cliente, e ninguém percebia até a hora de cobrar.
2. **Não dava para fechar.** O app só sabia abrir e lançar; a mesa ficava
   ocupada até alguém mexer no caixa.
3. **Mesa era texto livre.** "mesa 5", "Mesa 5" e "M5" eram três mesas, e não
   havia o que configurar.

O quarto bloco cobre a fronteira nova: operação de gerente pelo celular. O
aparelho pareado não é a pessoa, e cancelar comanda com item lançado é o vetor
de furto do salão.
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
    DEMO_OWNER_LOGIN,
    DEMO_OWNER_PIN,
    seed_demo_data,
)
from pdv.domain.errors import AuthorizationRequiredError
from pdv.domain.models import EntityId, new_id
from pdv.edge.manager import ManagerSessions
from pdv.edge.orders import TableOccupiedError, TableOrderService
from pdv.edge.tables import TableError, TableService
from pdv.services.authorization import AuthorizationService

TENANT = "11111111-1111-1111-1111-111111111111"
STORE = "22222222-2222-2222-2222-222222222222"
DEVICE = "33333333-3333-3333-3333-333333333333"
PHONE = EntityId("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest.fixture()
def salon(tmp_path: Path):  # noqa: ANN201
    config = AppConfig(
        tenant_id=TENANT,
        store_id=STORE,
        device_id=DEVICE,
        database_path=tmp_path / "pdv.db",
        printer=PrinterConfig(backend="file", output_dir=tmp_path / "out"),
        stock=StockConfig(),
    )
    database = Database(config.database_path)
    database.migrate()
    seed_demo_data(database, config)
    return database, config, TableService(database, config), TableOrderService(
        database, config
    )


def _open(orders: TableOrderService, tables: TableService, label: str):  # noqa: ANN202
    table = tables.find_by_label(label)
    assert table is not None, f"{label} deveria estar no cadastro de demonstração"
    return orders.open_order(
        client_uuid=EntityId(new_id()),
        operator_id=EntityId(DEMO_OPERATOR_ID),
        table_id=table.id,
        origin_device_id=PHONE,
    )


def _coffee(database: Database) -> EntityId:
    row = database.query_one("SELECT id FROM products WHERE sku = 'CAFE-EXP'")
    return EntityId(str(row["id"]))


# --------------------------------------------------------------------------- #
# 1. Abrir mesa
# --------------------------------------------------------------------------- #


def test_the_demo_store_already_has_a_floor_plan(salon) -> None:  # noqa: ANN001
    """O primeiro dia não pode começar com tela vazia e botão de cadastro."""
    _, _, tables, _ = salon

    plan = tables.list_tables()

    assert len(plan) == 10
    assert {t.area for t in plan} == {"Salão", "Varanda"}
    assert all(t.status == "free" for t in plan)


def test_a_table_cannot_be_opened_twice(salon) -> None:  # noqa: ANN001
    """O bug. Dois garçons, a mesma mesa, o mesmo instante.

    A conta partida em duas comandas só aparece na hora de cobrar — e aí não
    há como saber qual das duas o cliente reconhece.
    """
    _, _, tables, orders = salon
    first = _open(orders, tables, "Mesa 3")

    with pytest.raises(TableOccupiedError) as caught:
        _open(orders, tables, "Mesa 3")

    # A exceção carrega a comanda existente: o app abre essa, em vez de mostrar
    # erro para quem só queria lançar na mesa.
    assert caught.value.order.id == first.id
    assert str(first.local_number) in str(caught.value)


def test_resending_the_same_order_is_still_harmless(salon) -> None:  # noqa: ANN001
    """A trava nova não pode quebrar a idempotência antiga.

    O Wi-Fi cai atrás da geladeira e o celular reenvia sem saber se entrou.
    Isso precisa continuar devolvendo a mesma comanda, não um conflito.
    """
    _, _, tables, orders = salon
    table = tables.find_by_label("Mesa 2")
    client_uuid = EntityId(new_id())

    def send():  # noqa: ANN202
        return orders.open_order(
            client_uuid=client_uuid,
            operator_id=EntityId(DEMO_OPERATOR_ID),
            table_id=table.id,
            origin_device_id=PHONE,
        )

    assert send().id == send().id


def test_a_table_outside_the_floor_plan_is_refused(salon) -> None:  # noqa: ANN001
    """Rótulo livre foi o que criou "mesa 5", "Mesa 5" e "M5"."""
    _, _, _, orders = salon

    with pytest.raises(TableError, match="não existe no cadastro"):
        orders.open_order(
            client_uuid=EntityId(new_id()),
            operator_id=EntityId(DEMO_OPERATOR_ID),
            table_label="Mesa dos fundos",
            origin_device_id=PHONE,
        )


def test_the_label_is_matched_without_case(salon) -> None:  # noqa: ANN001
    """O garçom digita como quiser; a mesa é uma só."""
    _, _, _, orders = salon

    order = orders.open_order(
        client_uuid=EntityId(new_id()),
        operator_id=EntityId(DEMO_OPERATOR_ID),
        table_label="mesa 4",
        origin_device_id=PHONE,
    )

    assert order.table_label == "Mesa 4"


def test_the_order_keeps_its_own_copy_of_the_label(salon) -> None:  # noqa: ANN001
    """Renomear a mesa amanhã não reescreve o cupom de hoje."""
    _, _, tables, orders = salon
    order = _open(orders, tables, "Mesa 6")
    table = tables.find_by_label("Mesa 6")

    tables.update(table.id, label="Varanda 3")

    assert orders.get_order(order.id).table_label == "Mesa 6"
    assert tables.get(table.id).label == "Varanda 3"


# --------------------------------------------------------------------------- #
# 2. Fechar mesa
# --------------------------------------------------------------------------- #


def test_the_map_shows_which_tables_are_busy(salon) -> None:  # noqa: ANN001
    _, _, tables, orders = salon
    order = _open(orders, tables, "Mesa 1")

    busy = next(t for t in tables.list_tables() if t.label == "Mesa 1")

    assert busy.status == "busy"
    assert busy.order_id == order.id
    assert busy.local_number == order.local_number


def test_asking_for_the_bill_does_not_take_the_money(salon) -> None:  # noqa: ANN001
    """A divisão que mantém o dinheiro num lugar só.

    O celular sinaliza; quem recebe é o caixa. Um segundo ponto de
    recebimento — sem gaveta, sem impressora e sem conferência de troco — é
    como o furto de salão entra pela porta da frente.
    """
    database, _, tables, orders = salon
    order = _open(orders, tables, "Mesa 5")

    asked = orders.request_bill(order.id)

    assert asked.bill_requested is True
    assert asked.status == "open", "pedir a conta não fecha a venda"
    assert next(t for t in tables.list_tables() if t.label == "Mesa 5").status == "billing"
    assert database.query_one(
        "SELECT closed_at FROM orders WHERE id = ?", (order.id,)
    )["closed_at"] is None


def test_asking_twice_does_not_move_the_timestamp(salon) -> None:  # noqa: ANN001
    """O garçom toca duas vezes porque não viu a tela mudar."""
    _, _, tables, orders = salon
    order = _open(orders, tables, "Mesa 5")

    first = orders.request_bill(order.id).bill_requested_at
    assert orders.request_bill(order.id).bill_requested_at == first


def test_the_table_can_change_its_mind_and_order_dessert(salon) -> None:  # noqa: ANN001
    _, _, tables, orders = salon
    order = _open(orders, tables, "Mesa 5")
    orders.request_bill(order.id)

    assert orders.clear_bill_request(order.id).bill_requested is False


def test_canceling_the_order_frees_the_table(salon) -> None:  # noqa: ANN001
    """Mesa aberta por engano precisa sumir do mapa.

    Senão o salão mente e a mesa fica bloqueada a noite toda.
    """
    database, _, tables, orders = salon
    order = _open(orders, tables, "Mesa 8")
    orders.add_item(
        order_id=order.id,
        client_uuid=EntityId(new_id()),
        product_id=_coffee(database),
        quantity=__import__("decimal").Decimal("2"),
    )

    canceled = orders.cancel_order(
        order_id=order.id,
        authorizer_id=EntityId("55555555-5555-5555-5555-555555555555"),
        authorizer_name="Bruno Gerente",
        reason="Cliente desistiu antes de servir",
    )

    assert canceled.status == "canceled"
    assert canceled.total_cents == 0
    assert next(t for t in tables.list_tables() if t.label == "Mesa 8").status == "free"
    # O ticket sai da fila: mandar preparar comida que ninguém vai receber é
    # prejuízo que a cozinha só descobre quando o prato fica pronto.
    assert database.query_all(
        "SELECT id FROM kds_tickets WHERE order_id = ? AND status <> 'canceled'",
        (order.id,),
    ) == []


def test_canceling_an_order_is_a_critical_audit_event(salon) -> None:  # noqa: ANN001
    """A comida sai, a comanda some. É o vetor de furto do salão."""
    database, _, tables, orders = salon
    order = _open(orders, tables, "Mesa 8")

    orders.cancel_order(
        order_id=order.id,
        authorizer_id=EntityId("55555555-5555-5555-5555-555555555555"),
        authorizer_name="Bruno Gerente",
        reason="Mesa aberta por engano",
    )

    entry = database.query_one(
        "SELECT * FROM audit_ledger WHERE event_type = 'item_canceled' ORDER BY seq DESC"
    )
    assert entry["severity"] == "critical"
    assert "Bruno Gerente" in entry["payload_json"]
    assert "Mesa 8" in entry["payload_json"]


def test_a_canceled_order_cannot_receive_items(salon) -> None:  # noqa: ANN001
    database, _, tables, orders = salon
    order = _open(orders, tables, "Mesa 8")
    orders.cancel_order(
        order_id=order.id,
        authorizer_id=EntityId("55555555-5555-5555-5555-555555555555"),
        authorizer_name="Bruno Gerente",
        reason="Mesa aberta por engano",
    )

    from pdv.edge.orders import OrderClosedError

    with pytest.raises(OrderClosedError):
        orders.add_item(
            order_id=order.id,
            client_uuid=EntityId(new_id()),
            product_id=_coffee(database),
            quantity=__import__("decimal").Decimal("1"),
        )


def test_a_freed_table_can_be_opened_again(salon) -> None:  # noqa: ANN001
    """A trava é da comanda aberta, não da mesa para sempre."""
    _, _, tables, orders = salon
    first = _open(orders, tables, "Mesa 8")
    orders.cancel_order(
        order_id=first.id,
        authorizer_id=EntityId("55555555-5555-5555-5555-555555555555"),
        authorizer_name="Bruno Gerente",
        reason="Mesa aberta por engano",
    )

    second = _open(orders, tables, "Mesa 8")

    assert second.id != first.id


def test_transferring_moves_the_order_to_a_free_table(salon) -> None:  # noqa: ANN001
    _, _, tables, orders = salon
    order = _open(orders, tables, "Mesa 1")
    destination = tables.find_by_label("Varanda 1")

    moved = orders.transfer(
        order_id=order.id,
        table_id=destination.id,
        authorizer_id=EntityId("55555555-5555-5555-5555-555555555555"),
        authorizer_name="Bruno Gerente",
    )

    assert moved.table_label == "Varanda 1"
    assert next(t for t in tables.list_tables() if t.label == "Mesa 1").status == "free"
    assert next(t for t in tables.list_tables() if t.label == "Varanda 1").status == "busy"


def test_transferring_onto_an_occupied_table_is_refused(salon) -> None:  # noqa: ANN001
    """Juntar duas contas é operação de caixa, não de celular."""
    _, _, tables, orders = salon
    order = _open(orders, tables, "Mesa 1")
    _open(orders, tables, "Mesa 2")

    with pytest.raises(TableOccupiedError):
        orders.transfer(
            order_id=order.id,
            table_id=tables.find_by_label("Mesa 2").id,
            authorizer_id=EntityId("55555555-5555-5555-5555-555555555555"),
            authorizer_name="Bruno Gerente",
        )


# --------------------------------------------------------------------------- #
# 3. Configurar mesas
# --------------------------------------------------------------------------- #


def test_two_active_tables_cannot_share_a_name(salon) -> None:  # noqa: ANN001
    """Nome repetido é o erro de digitação que vira conta trocada."""
    _, _, tables, _ = salon

    with pytest.raises(TableError, match="Já existe"):
        tables.create(label="mesa 1")


def test_a_busy_table_cannot_leave_the_floor_plan(salon) -> None:  # noqa: ANN001
    """Sumir com a mesa deixaria a comanda aberta sem porta de entrada.

    O pedido continuaria existindo, invisível, até alguém achar na
    conciliação do dia seguinte.
    """
    _, _, tables, orders = salon
    order = _open(orders, tables, "Mesa 1")
    table = tables.find_by_label("Mesa 1")

    with pytest.raises(TableError) as caught:
        tables.set_active(table.id, False)

    # A mensagem nomeia a comanda: o gerente precisa saber qual conta está
    # segurando a mesa, não só que "não dá".
    assert str(order.local_number) in str(caught.value)
    assert tables.get(table.id).is_active is True


def test_a_table_removed_from_the_map_keeps_its_history(salon) -> None:  # noqa: ANN001
    """Desativar, nunca apagar.

    Apagar a linha arrebentaria as comandas antigas que apontam para ela — e o
    relatório de faturamento por mesa perderia todo o passado.
    """
    database, _, tables, orders = salon
    order = _open(orders, tables, "Varanda 2")
    table = tables.find_by_label("Varanda 2")
    orders.cancel_order(
        order_id=order.id,
        authorizer_id=EntityId("55555555-5555-5555-5555-555555555555"),
        authorizer_name="Bruno Gerente",
        reason="Fim do expediente",
    )

    tables.set_active(table.id, False)

    assert tables.find_by_label("Varanda 2") is None, "sai do mapa"
    assert database.query_one(
        "SELECT table_id FROM orders WHERE id = ?", (order.id,)
    )["table_id"] == table.id, "a comanda antiga ainda aponta para ela"


def test_a_deactivated_name_can_be_reused(salon) -> None:  # noqa: ANN001
    """O índice único vale só entre as ativas."""
    _, _, tables, _ = salon
    original = tables.find_by_label("Varanda 1")
    tables.set_active(original.id, False)

    replacement = tables.create(label="Varanda 1", area="Varanda", seats=8)

    assert replacement.id != original.id


def test_seeding_skips_what_already_exists(salon) -> None:  # noqa: ANN001
    _, _, tables, _ = salon

    created = tables.seed_default_tables(12)

    assert created == 4, "Mesa 1 a 8 já existem; sobram 9 a 12"
    assert len(tables.list_tables()) == 14


def test_the_label_is_normalized_on_the_way_in(salon) -> None:  # noqa: ANN001
    """Espaço duplo e sobra nas pontas são o mesmo nome digitado com pressa."""
    _, _, tables, _ = salon

    table = tables.create(label="  Balcão   Alto  ")

    assert table.label == "Balcão Alto"


def test_a_blank_label_is_refused(salon) -> None:  # noqa: ANN001
    _, _, tables, _ = salon

    with pytest.raises(TableError):
        tables.create(label="   ")


# --------------------------------------------------------------------------- #
# Migração de uma loja já instalada
# --------------------------------------------------------------------------- #


def test_the_upgrade_rescues_tables_that_were_only_text(tmp_path: Path) -> None:
    """A loja não pode abrir na segunda-feira com o salão vazio.

    Uma base em produção tem meses de comandas apontando para mesas que só
    existiam como texto em `customer_id`. Se a migração criasse a tabela nova e
    parasse por aí, o mapa apareceria vazio e o garçom não teria por onde
    chegar às comandas abertas.
    """
    import sqlite3

    from pdv.data.database import _MIGRATION_2_EDGE, _MIGRATION_3_INBOX, _SCHEMA_FILE

    # Uma base na versão 3, como estava antes desta fase: sem `store_tables` e
    # sem `orders.table_id`.
    path = tmp_path / "loja.db"
    legacy = sqlite3.connect(path)
    schema = _SCHEMA_FILE.read_text(encoding="utf-8")
    schema = schema.replace(
        "    table_id               TEXT,          -- mesa do salão (ver store_tables)\n", ""
    ).replace(
        "    bill_requested_at      TEXT,          "
        "-- o garçom pediu a conta; quem recebe é o caixa\n",
        "",
    )
    schema = schema.split("-- Fase 3.6")[0]
    legacy.executescript(schema)
    legacy.executescript(_MIGRATION_2_EDGE)
    legacy.executescript(_MIGRATION_3_INBOX)
    for number, label in enumerate(["Mesa 5", "mesa 5", "Varanda"], start=1):
        legacy.execute(
            "INSERT INTO orders (id, tenant_id, store_id, device_id, local_number, "
            " channel, status, customer_id, operator_id, opened_at, created_at, "
            " updated_at, origin_device_id, client_uuid) "
            "VALUES (?, ?, ?, ?, ?, 'waiter', 'open', ?, 'op', 'x', 'x', 'x', 'd', ?)",
            (f"o{number}", TENANT, STORE, DEVICE, number, label, f"cu{number}"),
        )
    legacy.execute("PRAGMA user_version = 3")
    legacy.commit()
    legacy.close()

    Database(path).migrate()

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    labels = sorted(
        str(r["label"]) for r in connection.execute("SELECT label FROM store_tables")
    )
    # "Mesa 5" e "mesa 5" eram a mesma mesa digitada de dois jeitos — e viram uma.
    assert labels == ["Mesa 5", "Varanda"]
    assert all(
        r["table_id"] is not None
        for r in connection.execute("SELECT table_id FROM orders")
    ), "toda comanda antiga encontra a mesa dela"
    from pdv.data.database import SCHEMA_VERSION

    assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# 4. Opções de gerente
# --------------------------------------------------------------------------- #


@pytest.fixture()
def managers(salon) -> ManagerSessions:  # noqa: ANN001
    database, config, _, _ = salon
    return ManagerSessions(AuthorizationService(database, config.tenant_id))


def test_a_manager_pin_opens_a_short_grant(managers: ManagerSessions) -> None:
    grant = managers.authorize(
        login=DEMO_MANAGER_LOGIN, pin=DEMO_MANAGER_PIN, device_id=PHONE
    )

    assert managers.require(grant.token, PHONE).name == "Bruno Gerente"


def test_the_cashier_pin_does_not_open_one(managers: ManagerSessions) -> None:
    """Ana tem PIN válido e, de propósito, não pode autorizar.

    Liberar o próprio cancelamento é o furto inteiro em um passo.
    """
    with pytest.raises(AuthorizationRequiredError):
        managers.authorize(login="ana", pin=DEMO_OPERATOR_PIN, device_id=PHONE)


def test_owner_credential_does_not_become_a_manager_grant(
    managers: ManagerSessions,
) -> None:
    with pytest.raises(AuthorizationRequiredError, match="gerente"):
        managers.authorize(
            login=DEMO_OWNER_LOGIN, pin=DEMO_OWNER_PIN, device_id=PHONE
        )


def test_a_wrong_pin_opens_nothing(managers: ManagerSessions) -> None:
    with pytest.raises(AuthorizationRequiredError):
        managers.authorize(login=DEMO_MANAGER_LOGIN, pin="0000", device_id=PHONE)

    assert managers.active_count() == 0


def test_a_grant_does_not_travel_between_phones(managers: ManagerSessions) -> None:
    """Sem isto, um token vazado valeria em qualquer aparelho da loja."""
    grant = managers.authorize(
        login=DEMO_MANAGER_LOGIN, pin=DEMO_MANAGER_PIN, device_id=PHONE
    )

    with pytest.raises(AuthorizationRequiredError, match="este aparelho"):
        managers.require(grant.token, EntityId("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"))

    assert managers.require(grant.token, PHONE), "o aparelho certo continua valendo"


def test_an_expired_grant_stops_working(managers: ManagerSessions) -> None:
    """O celular fica no balcão, desbloqueado, a noite inteira.

    Uma sessão que durasse o turno seria promover o aparelho a gerente.
    """
    from datetime import timedelta

    from pdv.domain.models import utc_now

    grant = managers.authorize(
        login=DEMO_MANAGER_LOGIN, pin=DEMO_MANAGER_PIN, device_id=PHONE
    )
    managers._grants[grant.token] = type(grant)(
        token=grant.token,
        authorizer=grant.authorizer,
        device_id=grant.device_id,
        expires_at=utc_now() - timedelta(seconds=1),
    )

    with pytest.raises(AuthorizationRequiredError, match="expirada"):
        managers.require(grant.token, PHONE)


def test_no_token_means_no_permission(managers: ManagerSessions) -> None:
    with pytest.raises(AuthorizationRequiredError):
        managers.require(None, PHONE)
    with pytest.raises(AuthorizationRequiredError):
        managers.require("", PHONE)


def test_the_manager_can_hand_the_grant_back(managers: ManagerSessions) -> None:
    grant = managers.authorize(
        login=DEMO_MANAGER_LOGIN, pin=DEMO_MANAGER_PIN, device_id=PHONE
    )

    assert managers.revoke(grant.token) is True
    with pytest.raises(AuthorizationRequiredError):
        managers.require(grant.token, PHONE)
