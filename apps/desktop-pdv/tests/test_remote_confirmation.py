"""Aceite presencial de comando remoto de risco — Fase 3.5.b, trava 7.

O item do `docs/plan.md`, verbatim:

    "Confirmação no terminal para operações de risco: cancelar item já
    impresso ou abrir gaveta exige aceite do operador presente."

"Impresso", num salão, é o item que já foi para a cozinha: o ticket saiu, o
prato está sendo feito ou já foi para a mesa. É esse o cancelamento remoto que
fecha o furto de salão sem ninguém na loja olhando — as seis travas anteriores
não veem nada de errado nele, porque o gerente existe, está dentro do teto e
assinou. Abrir gaveta nem entra: o terminal não aceita esse comando de fora
(`test_there_is_no_remote_drawer_command`).

Os testes são escritos do ponto de vista de quem tentaria contornar o aceite:
o garçom aceitando o cancelamento do próprio prato, o PIN errado virando
recusa, o comando comum passando pela porta do aceite, o aceite ressuscitando
um pedido já cancelado ou um comando vencido.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from pdv.config import AppConfig, PrinterConfig, StockConfig
from pdv.data.database import SCHEMA_VERSION, Database
from pdv.data.repositories import ProductRepository
from pdv.data.seed import (
    DEMO_MANAGER_ID,
    DEMO_MANAGER_LOGIN,
    DEMO_MANAGER_NAME,
    DEMO_MANAGER_PIN,
    DEMO_OPERATOR_ID,
    DEMO_OPERATOR_LOGIN,
    DEMO_OPERATOR_NAME,
    DEMO_OPERATOR_PIN,
    DEMO_WAITER_LOGIN,
    DEMO_WAITER_PIN,
    seed_demo_data,
)
from pdv.domain.errors import AuthorizationRequiredError
from pdv.domain.models import EntityId, iso, new_id, utc_now
from pdv.edge.hub import EventHub
from pdv.edge.orders import TableOrderService
from pdv.edge.tables import TableService
from pdv.remote import commands as commands_module
from pdv.remote.commands import (
    CHANNEL,
    CommandRefused,
    ConfirmationError,
    RemoteCommandService,
)
from pdv.remote.inbox import InboxRepository
from pdv.remote.protocol import CommandKind, CommandStatus, RemoteCommand, sign_command
from pdv.services.checkout import CheckoutService
from pdv.sync.engine import SyncEngine
from pdv.sync.protocol import (
    CommandDelivery,
    CommandFetch,
    CommandReport,
    PullResponse,
    PushResponse,
)

TENANT = "11111111-1111-1111-1111-111111111111"
STORE = "22222222-2222-2222-2222-222222222222"
DEVICE = "33333333-3333-3333-3333-333333333333"
PHONE = EntityId("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
SECRET = b"segredo-do-terminal-provisionado-na-ativacao"
REASON = "Cliente desistiu do prato"


# --------------------------------------------------------------------------- #
# Cenário
# --------------------------------------------------------------------------- #


@pytest.fixture()
def salon(tmp_path: Path):  # noqa: ANN201
    """Mesa 3 aberta pelo celular, com dois cafés já na fila da cozinha."""
    config = AppConfig(
        tenant_id=TENANT,
        store_id=STORE,
        device_id=DEVICE,
        database_path=tmp_path / "pdv.db",
        printer=PrinterConfig(backend="file", output_dir=tmp_path / "out"),
        stock=StockConfig(),
        device_secret=SECRET,
    )
    database = Database(config.database_path)
    database.migrate()
    seed_demo_data(database, config)

    hub = EventHub()
    orders = TableOrderService(database, config, hub)
    table = TableService(database, config).find_by_label("Mesa 3")
    assert table is not None
    order = orders.open_order(
        client_uuid=EntityId(new_id()),
        operator_id=EntityId(DEMO_OPERATOR_ID),
        table_id=table.id,
        origin_device_id=PHONE,
    )
    coffee = database.query_one("SELECT id FROM products WHERE sku = 'CAFE-EXP'")
    for _ in range(2):
        orders.add_item(
            order_id=order.id,
            client_uuid=EntityId(new_id()),
            product_id=EntityId(str(coffee["id"])),
            quantity=Decimal("1"),
        )
    items = orders.list_items(order.id)
    service = RemoteCommandService(database, config, hub=hub)
    return database, config, orders, service, hub, str(order.id), str(items[0]["id"])


def _cancel(order_id: str, item_id: str, **kwargs) -> RemoteCommand:  # noqa: ANN003
    return _command(
        CommandKind.CANCEL_ITEM,
        payload={"order_id": order_id, "order_item_id": item_id, "reason": REASON},
        **kwargs,
    )


def _command(
    kind: CommandKind,
    *,
    payload: dict,
    issued_at: str | None = None,
) -> RemoteCommand:
    command_uuid = new_id()
    issued_at = issued_at or iso(utc_now())
    return RemoteCommand(
        command_uuid=command_uuid,
        tenant_id=TENANT,
        store_id=STORE,
        device_id=DEVICE,
        kind=kind,
        payload=payload,
        issued_by_user_id=DEMO_MANAGER_ID,
        issued_by_name=DEMO_MANAGER_NAME,
        issued_at=issued_at,
        signature=sign_command(
            secret=SECRET,
            command_uuid=command_uuid,
            device_id=DEVICE,
            kind=kind.value,
            payload=payload,
            issued_at=issued_at,
        ),
    )


def _held(salon) -> RemoteCommand:  # noqa: ANN001
    """Entrega o cancelamento e roda o ciclo: o comando fica esperando o caixa."""
    database, _, _, service, _, order_id, item_id = salon
    command = _cancel(order_id, item_id)
    InboxRepository(database).accept(command)
    report = service.apply_pending()
    assert report.awaiting == 1
    return command


def _item(database: Database, item_id: str):  # noqa: ANN202
    return database.query_one("SELECT * FROM order_items WHERE id = ?", (item_id,))


def _ticket_status(database: Database, item_id: str) -> str:
    row = database.query_one(
        "SELECT status FROM kds_tickets WHERE order_item_id = ?", (item_id,)
    )
    return str(row["status"])


def _last_audit(database: Database):  # noqa: ANN202
    import json

    row = database.query_one("SELECT * FROM audit_ledger ORDER BY seq DESC LIMIT 1")
    return row, json.loads(row["payload_json"])


# --------------------------------------------------------------------------- #
# A trava: o item que já foi para a cozinha espera alguém na loja
# --------------------------------------------------------------------------- #


def test_an_item_in_the_kitchen_waits_for_someone_at_the_counter(salon) -> None:  # noqa: ANN001
    database, _, _, service, _, order_id, item_id = salon
    inbox = InboxRepository(database)
    command = _cancel(order_id, item_id)
    inbox.accept(command)

    report = service.apply_pending()

    assert (report.applied, report.refused, report.awaiting) == (0, 0, 1)
    assert _item(database, item_id)["canceled_at"] is None
    assert _ticket_status(database, item_id) == "queued", "a cozinha segue como estava"
    # Continua `pending`: não foi decidido, e a nuvem continua entregando.
    assert inbox.status_of(command.command_uuid) is CommandStatus.PENDING
    assert inbox.unreported() == [], "não há resultado para relatar"


def test_the_counter_reads_what_it_is_deciding(salon) -> None:  # noqa: ANN001
    """Quem decide precisa saber o quê, de quem, onde e por quê."""
    database, _, _, service, _, _, _ = salon
    _held(salon)

    [waiting] = service.awaiting()

    assert DEMO_MANAGER_NAME in waiting.note
    assert "Café Expresso" in waiting.note
    assert "R$ 7,00" in waiting.note
    assert "Mesa 3" in waiting.note
    assert "fila da cozinha" in waiting.note
    assert REASON in waiting.note


def test_the_wait_does_not_restart_every_cycle(salon) -> None:  # noqa: ANN001
    """A data da espera diz há quanto tempo o pedido está parado no caixa."""
    database, _, _, service, _, _, _ = salon
    _held(salon)
    [before] = service.awaiting()

    again = service.apply_pending()

    [after] = service.awaiting()
    assert again.awaiting == 1
    assert after.requested_at == before.requested_at


def test_the_note_follows_the_kitchen(salon) -> None:  # noqa: ANN001
    """O prato ficou pronto enquanto esperava: quem decide lê o estado de agora."""
    database, _, _, service, _, _, item_id = salon
    _held(salon)
    database.connection.execute(
        "UPDATE kds_tickets SET status = 'ready' WHERE order_item_id = ?", (item_id,)
    )

    service.apply_pending()

    [waiting] = service.awaiting()
    assert "pronto na cozinha" in waiting.note


def test_an_item_that_never_reached_the_kitchen_is_not_held(tmp_path: Path) -> None:
    """Venda de balcão não tem cozinha no meio: o cancelamento aplica direto.

    A trava é para o que já saiu. Segurar todo cancelamento remoto transformaria
    o aceite em carimbo — e carimbo que se dá sem ler é o que deixa de proteger.
    """
    config = AppConfig(
        tenant_id=TENANT,
        store_id=STORE,
        device_id=DEVICE,
        database_path=tmp_path / "pdv.db",
        printer=PrinterConfig(backend="file", output_dir=tmp_path / "out"),
        stock=StockConfig(),
        device_secret=SECRET,
    )
    database = Database(config.database_path)
    database.migrate()
    seed_demo_data(database, config)
    checkout = CheckoutService(database, config)
    sale = checkout.open_sale(EntityId(DEMO_OPERATOR_ID))
    product = next(
        p
        for p in ProductRepository(database.connection).list_active(EntityId(TENANT))
        if p.sku == "CAFE-EXP"
    )
    checkout.register_unit_item(
        product=product, quantity=Decimal("1"), operator_id=EntityId(DEMO_OPERATOR_ID)
    )
    item_id = str(sale.items[0].id)
    InboxRepository(database).accept(_cancel(str(sale.id), item_id))

    report = RemoteCommandService(database, config, checkout=checkout).apply_pending()

    assert (report.applied, report.awaiting) == (1, 0)
    assert _item(database, item_id)["canceled_at"] is not None


def test_there_is_no_remote_drawer_command() -> None:
    """A outra metade do item do plano: gaveta não abre de longe, nem com aceite.

    Mais forte que exigir confirmação — o terminal nem reconhece o comando. Se
    alguém acrescentar `open_drawer` ao enum, este teste quebra e obriga a
    pergunta a ser feita de novo, em voz alta.
    """
    assert {kind.value for kind in CommandKind} == {"apply_discount", "cancel_item"}


# --------------------------------------------------------------------------- #
# O aceite
# --------------------------------------------------------------------------- #


def test_the_cashier_accepts_and_the_item_is_canceled_once(salon) -> None:  # noqa: ANN001
    database, _, orders, service, _, order_id, item_id = salon
    command = _held(salon)
    before = orders.get_order(EntityId(order_id)).total_cents

    message = service.confirm(
        command.command_uuid, login=DEMO_OPERATOR_LOGIN, pin=DEMO_OPERATOR_PIN
    )

    assert DEMO_OPERATOR_NAME in message
    assert _item(database, item_id)["canceled_at"] is not None
    assert orders.get_order(EntityId(order_id)).total_cents == before - 700
    assert InboxRepository(database).status_of(command.command_uuid) is CommandStatus.APPLIED
    assert service.awaiting() == []

    with pytest.raises(ConfirmationError):
        service.confirm(
            command.command_uuid, login=DEMO_OPERATOR_LOGIN, pin=DEMO_OPERATOR_PIN
        )
    assert orders.get_order(EntityId(order_id)).total_cents == before - 700


def test_the_audit_names_all_three(salon) -> None:  # noqa: ANN001
    """Quem mandou, qual terminal, e quem estava na loja e concordou."""
    database, _, _, service, _, _, item_id = salon
    command = _held(salon)

    service.confirm(command.command_uuid, login=DEMO_OPERATOR_LOGIN, pin=DEMO_OPERATOR_PIN)

    row, payload = _last_audit(database)
    assert row["event_type"] == "item_canceled"
    assert row["severity"] == "critical"
    assert row["actor_user_id"] == DEMO_MANAGER_ID
    assert payload["channel"] == CHANNEL
    assert payload["target_device_id"] == DEVICE
    assert payload["confirmed_by_user_id"] == DEMO_OPERATOR_ID
    assert payload["confirmed_by_name"] == DEMO_OPERATOR_NAME
    assert payload["kitchen_status"] == "queued"
    assert payload["order_item_id"] == item_id


def test_the_kitchen_stops_making_the_dish(salon) -> None:  # noqa: ANN001
    """Defeito que existia antes da trava: o cancelamento remoto não tirava o
    ticket da fila, e a cozinha fazia de graça o prato que saiu da conta."""
    database, _, _, service, hub, _, item_id = salon
    command = _held(salon)

    with hub.subscribe({"ticket.changed"}) as kitchen:
        service.confirm(
            command.command_uuid, login=DEMO_OPERATOR_LOGIN, pin=DEMO_OPERATOR_PIN
        )
        event = kitchen.get(timeout=1)

    assert _ticket_status(database, item_id) == "canceled"
    assert event is not None
    assert event.payload["status"] == "canceled"


def test_the_other_item_stays_in_the_kitchen(salon) -> None:  # noqa: ANN001
    database, _, orders, service, _, order_id, item_id = salon
    command = _held(salon)
    other = next(
        i for i in orders.list_items(EntityId(order_id)) if str(i["id"]) != item_id
    )

    service.confirm(command.command_uuid, login=DEMO_OPERATOR_LOGIN, pin=DEMO_OPERATOR_PIN)

    assert _ticket_status(database, str(other["id"])) == "queued"


def test_a_manager_at_the_counter_may_accept(salon) -> None:  # noqa: ANN001
    _, _, _, service, _, _, _ = salon
    command = _held(salon)

    service.confirm(command.command_uuid, login=DEMO_MANAGER_LOGIN, pin=DEMO_MANAGER_PIN)

    assert service.awaiting() == []


# --------------------------------------------------------------------------- #
# Quem tentaria contornar
# --------------------------------------------------------------------------- #


def test_a_waiter_cannot_accept_the_cancel(salon) -> None:  # noqa: ANN001
    """No furto de salão é o garçom quem leva o prato. O aceite dele fecharia o
    circuito sem mais ninguém olhando."""
    database, _, _, service, _, _, item_id = salon
    command = _held(salon)

    with pytest.raises(ConfirmationError, match="caixa"):
        service.confirm(command.command_uuid, login=DEMO_WAITER_LOGIN, pin=DEMO_WAITER_PIN)

    assert _item(database, item_id)["canceled_at"] is None
    assert len(service.awaiting()) == 1


def test_a_wrong_pin_decides_nothing(salon) -> None:  # noqa: ANN001
    """PIN digitado errado não pode virar recusa: desfaria o que o gerente mandou."""
    database, _, _, service, _, _, item_id = salon
    command = _held(salon)

    with pytest.raises(AuthorizationRequiredError):
        service.confirm(command.command_uuid, login=DEMO_OPERATOR_LOGIN, pin="999999")
    with pytest.raises(AuthorizationRequiredError):
        service.decline(
            command.command_uuid, login=DEMO_OPERATOR_LOGIN, pin="999999", reason="x"
        )

    assert _item(database, item_id)["canceled_at"] is None
    assert InboxRepository(database).status_of(command.command_uuid) is CommandStatus.PENDING
    assert len(service.awaiting()) == 1


def test_an_ordinary_command_cannot_skip_the_cycle_through_the_door(salon) -> None:  # noqa: ANN001
    """Só o que o terminal pôs para esperar entra pelo aceite.

    Aceitar qualquer pendente por aqui aplicaria na tela algo que o ciclo
    normal ainda nem conferiu.
    """
    database, _, _, service, _, order_id, _ = salon
    discount = _command(
        CommandKind.APPLY_DISCOUNT,
        payload={"order_id": order_id, "percent": "10", "reason": "demora"},
    )
    InboxRepository(database).accept(discount)

    with pytest.raises(ConfirmationError, match="não está esperando"):
        service.confirm(
            discount.command_uuid, login=DEMO_OPERATOR_LOGIN, pin=DEMO_OPERATOR_PIN
        )

    order = database.query_one("SELECT discount_cents FROM orders WHERE id = ?", (order_id,))
    assert order["discount_cents"] == 0


def test_accepting_does_not_revive_a_canceled_order(salon) -> None:  # noqa: ANN001
    """Entre o pedido e o aceite a comanda foi cancelada: as travas rodam de novo."""
    database, _, orders, service, _, order_id, _ = salon
    command = _held(salon)
    orders.cancel_order(
        order_id=EntityId(order_id),
        authorizer_id=EntityId(DEMO_MANAGER_ID),
        authorizer_name=DEMO_MANAGER_NAME,
        reason="Mesa aberta por engano",
    )

    with pytest.raises(CommandRefused):
        service.confirm(
            command.command_uuid, login=DEMO_OPERATOR_LOGIN, pin=DEMO_OPERATOR_PIN
        )

    assert InboxRepository(database).status_of(command.command_uuid) is CommandStatus.REFUSED
    assert service.awaiting() == []


def test_accepting_does_not_revive_a_manager_who_left(salon) -> None:  # noqa: ANN001
    database, _, _, service, _, _, item_id = salon
    command = _held(salon)
    database.connection.execute(
        "UPDATE users SET is_active = 0 WHERE id = ?", (DEMO_MANAGER_ID,)
    )

    with pytest.raises(CommandRefused):
        service.confirm(
            command.command_uuid, login=DEMO_OPERATOR_LOGIN, pin=DEMO_OPERATOR_PIN
        )

    assert _item(database, item_id)["canceled_at"] is None


def test_the_wait_expires_with_the_command_window(salon, monkeypatch) -> None:  # noqa: ANN001
    """Parado no caixa não é eterno: vence na mesma janela de todo comando.

    Sem isto, um cancelamento esquecido na fila na terça seria aceito por
    engano no sábado, sobre uma mesa que ninguém lembra mais.
    """
    database, _, _, service, _, _, item_id = salon
    command = _held(salon)
    monkeypatch.setattr(commands_module, "is_fresh", lambda _issued_at: False)

    report = service.apply_pending()

    assert report.refused == 1
    assert InboxRepository(database).status_of(command.command_uuid) is CommandStatus.REFUSED
    assert _item(database, item_id)["canceled_at"] is None
    with pytest.raises(ConfirmationError):
        service.confirm(
            command.command_uuid, login=DEMO_OPERATOR_LOGIN, pin=DEMO_OPERATOR_PIN
        )


def test_the_kill_switch_also_refuses_what_was_waiting(salon) -> None:  # noqa: ANN001
    from pdv.data.settings import SettingsStore
    from pdv.remote.commands import REMOTE_ENABLED_KEY

    database, _, _, service, _, _, item_id = salon
    command = _held(salon)
    SettingsStore(database).set(REMOTE_ENABLED_KEY, "0")

    with pytest.raises(CommandRefused, match="desligado"):
        service.confirm(
            command.command_uuid, login=DEMO_OPERATOR_LOGIN, pin=DEMO_OPERATOR_PIN
        )

    assert _item(database, item_id)["canceled_at"] is None


# --------------------------------------------------------------------------- #
# A recusa no caixa
# --------------------------------------------------------------------------- #


def test_declining_is_final_and_goes_back_with_a_name(salon) -> None:  # noqa: ANN001
    """"Recusado" sem motivo faz o gerente emitir de novo igual."""
    database, _, _, service, _, _, item_id = salon
    command = _held(salon)

    service.decline(
        command.command_uuid,
        login=DEMO_OPERATOR_LOGIN,
        pin=DEMO_OPERATOR_PIN,
        reason="O prato já foi servido e comido",
    )

    inbox = InboxRepository(database)
    assert inbox.status_of(command.command_uuid) is CommandStatus.REFUSED
    [result] = inbox.unreported()
    assert DEMO_OPERATOR_NAME in result.message
    assert "já foi servido" in result.message
    assert _item(database, item_id)["canceled_at"] is None
    assert _ticket_status(database, item_id) == "queued"

    row, payload = _last_audit(database)
    assert row["event_type"] == "remote_command_refused"
    assert payload["declined_by_user_id"] == DEMO_OPERATOR_ID
    assert payload["channel"] == CHANNEL

    with pytest.raises(ConfirmationError):
        service.confirm(
            command.command_uuid, login=DEMO_OPERATOR_LOGIN, pin=DEMO_OPERATOR_PIN
        )


def test_declining_needs_a_reason(salon) -> None:  # noqa: ANN001
    database, _, _, service, _, _, _ = salon
    command = _held(salon)

    with pytest.raises(ConfirmationError, match="motivo"):
        service.decline(
            command.command_uuid, login=DEMO_OPERATOR_LOGIN, pin=DEMO_OPERATOR_PIN,
            reason="   ",
        )

    assert len(service.awaiting()) == 1


def test_a_waiter_cannot_decline_either(salon) -> None:  # noqa: ANN001
    _, _, _, service, _, _, _ = salon
    command = _held(salon)

    with pytest.raises(ConfirmationError):
        service.decline(
            command.command_uuid, login=DEMO_WAITER_LOGIN, pin=DEMO_WAITER_PIN,
            reason="não quero",
        )

    assert len(service.awaiting()) == 1


# --------------------------------------------------------------------------- #
# O painel fica sabendo
# --------------------------------------------------------------------------- #


class _Cloud:
    """Nuvem falsa que fala de comando. `names_awaiting` separa nova de antiga."""

    def __init__(self, *, names_awaiting: bool) -> None:
        self.names_awaiting = names_awaiting
        self.queue: list[RemoteCommand] = []
        self.reports: list[CommandReport] = []

    def push(self, batch):  # noqa: ANN001, ANN201
        return PushResponse(acks=())

    def pull(self, request):  # noqa: ANN001, ANN201
        return PullResponse(entity_table=request.entity_table, rows=(), last_server_seq=0)

    def fetch_commands(self, request: CommandFetch) -> CommandDelivery:
        return CommandDelivery(commands=tuple(self.queue))

    def report_commands(self, report: CommandReport) -> tuple[str, ...]:
        self.reports.append(report)
        settled = {r.command_uuid for r in report.results}
        self.queue = [c for c in self.queue if c.command_uuid not in settled]
        named = set(settled)
        if self.names_awaiting:
            named |= {n.command_uuid for n in report.awaiting}
        return tuple(named)


def _engine(salon, cloud: _Cloud) -> SyncEngine:  # noqa: ANN001
    database, config, _, service, _, _, _ = salon
    return SyncEngine(database, cloud, config, commands=service)


def test_the_panel_learns_the_command_is_waiting_for_the_counter(salon) -> None:  # noqa: ANN001
    """Sem isto o painel diria "entregue", que o gerente lê como "já vai"."""
    _, _, _, _, _, order_id, item_id = salon
    cloud = _Cloud(names_awaiting=True)
    command = _cancel(order_id, item_id)
    cloud.queue.append(command)
    engine = _engine(salon, cloud)

    first = engine.command_cycle()

    assert first.awaiting == 1
    [report] = cloud.reports
    assert report.results == ()
    [notice] = report.awaiting
    assert notice.command_uuid == command.command_uuid
    assert "fila da cozinha" in notice.message

    engine.command_cycle()
    assert len(cloud.reports) == 1, "a espera já conhecida não sobe de novo"


def test_an_older_cloud_just_hears_it_again(salon) -> None:  # noqa: ANN001
    """Nuvem que não conhece a espera não a nomeia — e o terminal reavisa.

    Custa alguns bytes por ciclo. Marcar sem confirmação deixaria o painel
    novo, quando chegar, sem saber que o comando está parado.
    """
    _, _, _, _, _, order_id, item_id = salon
    cloud = _Cloud(names_awaiting=False)
    cloud.queue.append(_cancel(order_id, item_id))
    engine = _engine(salon, cloud)

    engine.command_cycle()
    engine.command_cycle()

    assert [len(r.awaiting) for r in cloud.reports] == [1, 1]


def test_once_decided_only_the_result_goes_up(salon) -> None:  # noqa: ANN001
    _, _, _, service, _, order_id, item_id = salon
    cloud = _Cloud(names_awaiting=True)
    command = _cancel(order_id, item_id)
    cloud.queue.append(command)
    engine = _engine(salon, cloud)
    engine.command_cycle()

    service.confirm(command.command_uuid, login=DEMO_OPERATOR_LOGIN, pin=DEMO_OPERATOR_PIN)
    engine.command_cycle()

    last = cloud.reports[-1]
    assert last.awaiting == ()
    assert [(r.command_uuid, r.status) for r in last.results] == [
        (command.command_uuid, CommandStatus.APPLIED)
    ]
    assert cloud.queue == []


def test_the_awaiting_notice_travels_in_its_own_field() -> None:
    """Formato do fio: a espera não é um terceiro status dentro de `results`."""
    from pdv.remote.inbox import AwaitingNotice
    from pdv.sync.transport import HttpTransport

    sent: dict = {}

    class _Response:
        status_code = 200
        text = ""

        @staticmethod
        def json() -> dict:
            return {"accepted": ["c-1"]}

    class _Client:
        def post(self, path: str, json: dict) -> _Response:  # noqa: A002
            sent["path"], sent["body"] = path, json
            return _Response()

    transport = HttpTransport("https://nuvem.invalid/api", "token")
    transport._get_client = lambda: _Client()  # type: ignore[method-assign]

    accepted = transport.report_commands(
        CommandReport(
            tenant_id=TENANT,
            store_id=STORE,
            device_id=DEVICE,
            awaiting=(AwaitingNotice("c-1", "espera o caixa", "2026-09-24T10:00:00+00:00"),),
        )
    )

    assert accepted == ("c-1",)
    assert sent["body"]["results"] == []
    assert sent["body"]["awaiting"] == [
        {
            "command_uuid": "c-1",
            "message": "espera o caixa",
            "requested_at": "2026-09-24T10:00:00+00:00",
        }
    ]


# --------------------------------------------------------------------------- #
# Loja instalada
# --------------------------------------------------------------------------- #


def test_an_installed_store_gets_the_columns_without_losing_its_inbox(
    salon,  # noqa: ANN001
) -> None:
    database, config, _, _, _, order_id, item_id = salon
    command = _cancel(order_id, item_id)
    InboxRepository(database).accept(command)
    connection = database.connection
    # Volta a base para a versão 13, como está numa loja hoje.
    connection.executescript(
        """
        CREATE TABLE rc_old AS SELECT command_uuid, tenant_id, store_id, device_id,
            kind, payload_json, issued_by_user_id, issued_by_name, issued_at,
            signature, status, received_at, settled_at, result_message,
            reported_at FROM remote_commands;
        DROP TABLE remote_commands;
        ALTER TABLE rc_old RENAME TO remote_commands;
        PRAGMA user_version = 13;
        """
    )
    database.close()

    upgraded = Database(config.database_path)
    upgraded.migrate()

    columns = {
        str(row["name"])
        for row in upgraded.query_all("PRAGMA table_info(remote_commands)")
    }
    assert {
        "confirmation_requested_at",
        "confirmation_note",
        "confirmation_reported_at",
    } <= columns
    assert upgraded.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION
    assert InboxRepository(upgraded).status_of(command.command_uuid) is CommandStatus.PENDING
