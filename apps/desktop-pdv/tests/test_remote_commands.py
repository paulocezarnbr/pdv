"""Comandos remotos — Fase 3.5.b.

O critério de aceite do `docs/plan.md`, verbatim:

    "com o terminal **offline**, o gerente concede um desconto pelo painel; o
    comando fica `pendente`. Ao reconectar, é aplicado **uma vez só**
    (reenviá-lo não duplica), aparece no cupom e gera entrada de auditoria
    nomeando o gerente remoto **e** o terminal. Um desconto acima do teto do
    perfil é recusado pelo terminal, mesmo vindo do painel."

Cada trava do `commands.py` tem um teste aqui, e todos são escritos do ponto de
vista de quem tentaria abusar do canal. Um teste que só confirma que o caminho
feliz funciona não vale nada num módulo cuja razão de existir é recusar.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from pdv.config import AppConfig, PrinterConfig, StockConfig
from pdv.data.database import Database
from pdv.data.repositories import ProductRepository
from pdv.data.seed import (
    DEMO_MANAGER_ID,
    DEMO_MANAGER_NAME,
    DEMO_OPERATOR_ID,
    DEMO_OPERATOR_NAME,
    DEMO_OWNER_ID,
    DEMO_OWNER_NAME,
    seed_demo_data,
)
from pdv.data.settings import SettingsStore
from pdv.domain.models import (
    Cents,
    EntityId,
    Grams,
    Payment,
    PaymentMethod,
    ScaleReading,
    ScaleStatus,
    iso,
    new_id,
    utc_now,
)
from pdv.remote.commands import (
    CHANNEL,
    REMOTE_ENABLED_KEY,
    RemoteCommandService,
)
from pdv.remote.inbox import InboxRepository
from pdv.remote.protocol import CommandKind, CommandStatus, RemoteCommand, sign_command
from pdv.services.checkout import CheckoutService

TENANT = "11111111-1111-1111-1111-111111111111"
STORE = "22222222-2222-2222-2222-222222222222"
DEVICE = "33333333-3333-3333-3333-333333333333"
SECRET = b"segredo-do-terminal-provisionado-na-ativacao"


# --------------------------------------------------------------------------- #
# Cenário
# --------------------------------------------------------------------------- #


@pytest.fixture()
def terminal(tmp_path: Path):  # noqa: ANN201
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
    return database, config


@pytest.fixture()
def open_sale(terminal):  # noqa: ANN001, ANN201
    """Uma venda aberta de R$ 20,00, do jeito que o caixa a deixaria."""
    database, config = terminal
    checkout = CheckoutService(database, config)
    sale = checkout.open_sale(EntityId(DEMO_OPERATOR_ID))

    product = next(
        p
        for p in ProductRepository(database.connection).list_active(EntityId(TENANT))
        if p.sku == "CAFE-EXP"
    )
    for _ in range(4):
        checkout.register_unit_item(
            product=product,
            quantity=Decimal("1"),
            operator_id=EntityId(DEMO_OPERATOR_ID),
        )
    return database, config, checkout, sale


@pytest.fixture()
def weighed_sale(terminal):  # noqa: ANN001, ANN201
    """Uma venda com item **pesado**, que é o que tem ficha técnica.

    O estorno do item unitário com ficha (a fatia) lançado na mesa está em
    `test_table_stock.py`; aqui é o do balcão.
    """
    database, config = terminal
    checkout = CheckoutService(database, config)
    sale = checkout.open_sale(EntityId(DEMO_OPERATOR_ID))

    product = next(
        p
        for p in ProductRepository(database.connection).list_active(EntityId(TENANT))
        if p.sku == "TORTA-CHOC"
    )
    checkout.register_weighed_item(
        product=product,
        reading=ScaleReading(
            status=ScaleStatus.STABLE, weight_grams=Grams(892), raw_frame="00892"
        ),
        operator_id=EntityId(DEMO_OPERATOR_ID),
    )
    return database, config, checkout, sale


def _command(
    kind: CommandKind = CommandKind.APPLY_DISCOUNT,
    *,
    payload: dict | None = None,
    device_id: str = DEVICE,
    tenant_id: str = TENANT,
    issued_by: str = DEMO_MANAGER_ID,
    issued_name: str = DEMO_MANAGER_NAME,
    issued_at: str | None = None,
    secret: bytes = SECRET,
    command_uuid: str | None = None,
) -> RemoteCommand:
    """Emite um comando como a nuvem emitiria — assinando com o mesmo código."""
    payload = payload if payload is not None else {}
    command_uuid = command_uuid or new_id()
    issued_at = issued_at or iso(utc_now())
    return RemoteCommand(
        command_uuid=command_uuid,
        tenant_id=tenant_id,
        store_id=STORE,
        device_id=device_id,
        kind=kind,
        payload=payload,
        issued_by_user_id=issued_by,
        issued_by_name=issued_name,
        issued_at=issued_at,
        signature=sign_command(
            secret=secret,
            command_uuid=command_uuid,
            device_id=device_id,
            kind=kind.value,
            payload=payload,
            issued_at=issued_at,
        ),
    )


def _discount(order_id: str, percent: str = "10", **kwargs) -> RemoteCommand:  # noqa: ANN003
    return _command(
        payload={
            "order_id": order_id,
            "percent": percent,
            "reason": "Cliente aguardou 40 minutos",
        },
        **kwargs,
    )


def _audit_rows(database: Database) -> list:
    return database.query_all("SELECT * FROM audit_ledger ORDER BY seq")


def _consumptions(database: Database, order_item_id: str) -> dict[str, int]:
    """O que o item baixou do estoque, por insumo."""
    return {
        str(row["inventory_item_id"]): int(row["consumed_mg"])
        for row in database.query_all(
            "SELECT inventory_item_id, consumed_mg FROM order_item_ingredients "
            " WHERE order_item_id = ?",
            (order_item_id,),
        )
    }


# --------------------------------------------------------------------------- #
# O critério de aceite
# --------------------------------------------------------------------------- #


def test_the_command_waits_while_the_terminal_is_offline(open_sale) -> None:  # noqa: ANN001
    """Offline, o comando chega e fica. Não some, nem aplica sozinho."""
    database, _, _, sale = open_sale
    inbox = InboxRepository(database)

    assert inbox.accept(_discount(str(sale.id))) is True

    assert inbox.pending_count() == 1


def test_on_reconnect_the_discount_lands_once(open_sale) -> None:  # noqa: ANN001
    database, config, checkout, sale = open_sale
    service = RemoteCommandService(database, config, checkout=checkout)
    command = _discount(str(sale.id), percent="25")
    inbox = InboxRepository(database)
    inbox.accept(command)

    report = service.apply_pending()

    assert (report.applied, report.refused) == (1, 0)
    order = database.query_one("SELECT * FROM orders WHERE id = ?", (sale.id,))
    assert order["subtotal_cents"] == 2800
    assert order["discount_cents"] == 700
    assert order["total_cents"] == 2100
    assert inbox.status_of(command.command_uuid) is CommandStatus.APPLIED


def test_redelivering_the_same_command_does_not_discount_twice(open_sale) -> None:  # noqa: ANN001
    """A rede instável é o caso normal, não a exceção.

    Aplicar duas vezes é perda que ninguém reclama: o cliente não avisa que
    pagou menos do que devia.
    """
    database, config, checkout, sale = open_sale
    service = RemoteCommandService(database, config, checkout=checkout)
    inbox = InboxRepository(database)
    command = _discount(str(sale.id), percent="25")

    inbox.accept(command)
    service.apply_pending()
    # A nuvem não recebeu a confirmação e reenvia o MESMO comando.
    assert inbox.accept(command) is False
    second = service.apply_pending()

    assert second.total == 0, "o comando reentregue nem chega a ser reavaliado"
    order = database.query_one("SELECT * FROM orders WHERE id = ?", (sale.id,))
    assert order["discount_cents"] == 700


def test_the_remote_discount_reaches_the_receipt(open_sale) -> None:  # noqa: ANN001
    """O caixa está com esta venda na tela: o cupom precisa sair com o desconto.

    Este é o teste que pega o bug de dessincronia — aplicar só no banco deixa
    `finalize_sale` gravar o total antigo por cima, e o desconto do gerente
    some no instante exato em que o cliente vai pagar.
    """
    database, config, checkout, sale = open_sale
    service = RemoteCommandService(database, config, checkout=checkout)
    InboxRepository(database).accept(_discount(str(sale.id), percent="25"))

    service.apply_pending()
    assert int(checkout.current_sale.total_cents) == 2100

    payload = checkout.finalize_sale(
        payments=(Payment(method=PaymentMethod.CASH, amount_cents=Cents(2100)),),
        operator_id=EntityId(DEMO_OPERATOR_ID),
        operator_name=DEMO_OPERATOR_NAME,
    )

    receipt = payload.decode("cp850", errors="replace")
    assert "Desconto" in receipt
    assert "-7,00" in receipt
    assert "21,00" in receipt


def test_the_audit_names_the_remote_manager_and_the_terminal(open_sale) -> None:  # noqa: ANN001
    """Dupla identidade.

    Sem o canal e o terminal no evento, um desconto remoto fica
    indistinguível de um concedido no balcão — e o painel vira a rota limpa
    para o furto que o ledger existe para achar.
    """
    database, config, checkout, sale = open_sale
    service = RemoteCommandService(database, config, checkout=checkout)
    command = _discount(str(sale.id), percent="25")
    InboxRepository(database).accept(command)

    service.apply_pending()

    entry = next(
        row for row in _audit_rows(database) if row["event_type"] == "discount_applied"
    )
    assert entry["actor_user_id"] == DEMO_MANAGER_ID
    assert entry["authorizer_user_id"] == DEMO_MANAGER_ID
    payload = entry["payload_json"]
    assert f'"channel":"{CHANNEL}"' in payload.replace(" ", "")
    assert DEVICE in payload
    assert DEMO_MANAGER_NAME in payload
    assert command.command_uuid in payload


def test_above_the_ceiling_the_terminal_refuses(open_sale) -> None:  # noqa: ANN001
    """Estar longe não amplia poder.

    O gerente da base de demonstração tem teto de 30%. Pelo balcão o diálogo
    barraria 50%; pelo painel, quem barra é o terminal.
    """
    database, config, checkout, sale = open_sale
    service = RemoteCommandService(database, config, checkout=checkout)
    command = _discount(str(sale.id), percent="50")
    inbox = InboxRepository(database)
    inbox.accept(command)

    report = service.apply_pending()

    assert (report.applied, report.refused) == (0, 1)
    order = database.query_one("SELECT * FROM orders WHERE id = ?", (sale.id,))
    assert order["discount_cents"] == 0
    assert inbox.status_of(command.command_uuid) is CommandStatus.REFUSED
    refusal = next(
        row
        for row in _audit_rows(database)
        if row["event_type"] == "remote_command_refused"
    )
    assert "30" in refusal["payload_json"] and "50" in refusal["payload_json"]


def test_the_ceiling_comes_from_the_local_replica(open_sale) -> None:  # noqa: ANN001
    """O teto não pode vir do comando.

    Se viesse, bastaria comprometer o painel para conceder qualquer
    percentual: a trava seria conferida contra o número que o atacante
    escolheu.
    """
    database, config, checkout, sale = open_sale
    service = RemoteCommandService(database, config, checkout=checkout)
    command = _command(
        payload={
            "order_id": str(sale.id),
            "percent": "90",
            "reason": "Cortesia",
            "max_discount_percent": "100",  # o painel "garante" que pode
        }
    )
    InboxRepository(database).accept(command)

    assert service.apply_pending().refused == 1
    assert (
        database.query_one("SELECT * FROM orders WHERE id = ?", (sale.id,))[
            "discount_cents"
        ]
        == 0
    )


# --------------------------------------------------------------------------- #
# As outras travas
# --------------------------------------------------------------------------- #


def test_a_forged_signature_is_refused(open_sale) -> None:  # noqa: ANN001
    """Autenticar a conexão não é o mesmo que autenticar o comando."""
    database, config, checkout, sale = open_sale
    service = RemoteCommandService(database, config, checkout=checkout)
    command = _discount(str(sale.id), secret=b"chave-de-outro-terminal")
    InboxRepository(database).accept(command)

    assert service.apply_pending().refused == 1
    refusal = next(
        row
        for row in _audit_rows(database)
        if row["event_type"] == "remote_command_refused"
    )
    assert refusal["severity"] == "critical", "assinatura falsa nunca é engano"


def test_changing_the_payload_after_signing_is_refused(open_sale) -> None:  # noqa: ANN001
    """Interceptar o canal e trocar 10% por 90% não pode funcionar."""
    database, config, checkout, sale = open_sale
    service = RemoteCommandService(database, config, checkout=checkout)
    honest = _discount(str(sale.id), percent="10")
    tampered = RemoteCommand(
        **{
            **{f.name: getattr(honest, f.name) for f in honest.__dataclass_fields__.values()},
            "payload": {**honest.payload, "percent": "90"},
        }
    )
    InboxRepository(database).accept(tampered)

    assert service.apply_pending().refused == 1


def test_a_command_for_another_terminal_is_refused(open_sale) -> None:  # noqa: ANN001
    """Replay de loja para loja: o mesmo desconto, no terminal errado."""
    database, config, checkout, sale = open_sale
    service = RemoteCommandService(database, config, checkout=checkout)
    other = "99999999-9999-9999-9999-999999999999"
    InboxRepository(database).accept(_discount(str(sale.id), device_id=other))

    assert service.apply_pending().refused == 1


def test_a_stale_command_is_refused(open_sale) -> None:  # noqa: ANN001
    """Comando capturado do canal não vale para sempre."""
    database, config, checkout, sale = open_sale
    service = RemoteCommandService(database, config, checkout=checkout)
    yesterday = iso(utc_now() - timedelta(hours=30))
    InboxRepository(database).accept(_discount(str(sale.id), issued_at=yesterday))

    assert service.apply_pending().refused == 1


def test_a_command_from_yesterday_morning_still_applies(open_sale) -> None:  # noqa: ANN001
    """A janela é generosa de propósito.

    Um PDV com a internet caída desde a manhã ainda deve receber o desconto
    que o gerente concedeu ao meio-dia — senão a trava anti-replay vira um
    jeito de o sistema falhar sozinho.
    """
    database, config, checkout, sale = open_sale
    service = RemoteCommandService(database, config, checkout=checkout)
    hours_ago = iso(utc_now() - timedelta(hours=8))
    InboxRepository(database).accept(_discount(str(sale.id), issued_at=hours_ago))

    assert service.apply_pending().applied == 1


def test_the_panel_never_rewrites_a_closed_sale(open_sale) -> None:  # noqa: ANN001
    """Venda fechada se corrige por estorno, não por comando remoto.

    Deixar o painel mexer num pedido pago faria o total divergir do dinheiro
    que já entrou na gaveta — e, com documento fiscal transmitido, do que foi
    declarado.
    """
    database, config, checkout, sale = open_sale
    checkout.finalize_sale(
        payments=(Payment(method=PaymentMethod.CASH, amount_cents=Cents(2800)),),
        operator_id=EntityId(DEMO_OPERATOR_ID),
        operator_name=DEMO_OPERATOR_NAME,
    )
    service = RemoteCommandService(database, config, checkout=checkout)
    InboxRepository(database).accept(_discount(str(sale.id)))

    assert service.apply_pending().refused == 1
    order = database.query_one("SELECT * FROM orders WHERE id = ?", (sale.id,))
    assert order["discount_cents"] == 0
    assert order["total_cents"] == 2800


def test_a_cashier_cannot_authorize_from_the_panel(open_sale) -> None:  # noqa: ANN001
    """O perfil vale nos dois canais. Ana tem PIN e não pode autorizar."""
    database, config, checkout, sale = open_sale
    service = RemoteCommandService(database, config, checkout=checkout)
    InboxRepository(database).accept(
        _discount(str(sale.id), issued_by=DEMO_OPERATOR_ID, issued_name=DEMO_OPERATOR_NAME)
    )

    assert service.apply_pending().refused == 1


def test_a_discount_without_a_reason_is_refused(open_sale) -> None:  # noqa: ANN001
    """Motivo em branco é um desconto que ninguém consegue explicar depois."""
    database, config, checkout, sale = open_sale
    service = RemoteCommandService(database, config, checkout=checkout)
    InboxRepository(database).accept(
        _command(payload={"order_id": str(sale.id), "percent": "10", "reason": "  "})
    )

    assert service.apply_pending().refused == 1


def test_the_kill_switch_stops_the_channel(open_sale) -> None:  # noqa: ANN001
    """O dono desliga pelo terminal, sem depender de a nuvem cooperar.

    Que é exatamente o que não se pode supor quando o painel é justamente o
    que foi comprometido.
    """
    database, config, checkout, sale = open_sale
    SettingsStore(database).set(REMOTE_ENABLED_KEY, "0")
    service = RemoteCommandService(database, config, checkout=checkout)
    InboxRepository(database).accept(_discount(str(sale.id)))

    assert service.enabled is False
    assert service.apply_pending().refused == 1
    assert (
        database.query_one("SELECT * FROM orders WHERE id = ?", (sale.id,))[
            "discount_cents"
        ]
        == 0
    )


def test_the_channel_is_on_by_default(terminal) -> None:  # noqa: ANN001
    """Loja que nunca abriu a tela de configuração ainda recebe comando."""
    database, config = terminal

    assert RemoteCommandService(database, config).enabled is True


# --------------------------------------------------------------------------- #
# Cancelamento de item
# --------------------------------------------------------------------------- #


def test_a_remote_cancel_reverses_the_stock(weighed_sale) -> None:  # noqa: ANN001
    """Cancelar sem estornar insumo é como o estoque começa a mentir."""
    database, config, checkout, sale = weighed_sale
    item = checkout.current_sale.items[0]
    consumed = _consumptions(database, str(item.id))
    assert consumed, "a torta da base de demonstração tem ficha técnica"
    before = {key: _balance(database, key) for key in consumed}

    service = RemoteCommandService(database, config, checkout=checkout)
    InboxRepository(database).accept(
        _command(
            kind=CommandKind.CANCEL_ITEM,
            payload={
                "order_id": str(sale.id),
                "order_item_id": str(item.id),
                "reason": "Pedido trocado pelo cliente",
            },
        )
    )

    assert service.apply_pending().applied == 1
    for key, quantity in consumed.items():
        assert _balance(database, key) == before[key] + quantity

    assert checkout.current_sale.items == []
    order = database.query_one("SELECT * FROM orders WHERE id = ?", (sale.id,))
    assert order["subtotal_cents"] == 0


def test_a_remote_cancel_is_a_critical_audit_event(open_sale) -> None:  # noqa: ANN001
    database, config, checkout, sale = open_sale
    item = checkout.current_sale.items[0]
    service = RemoteCommandService(database, config, checkout=checkout)
    InboxRepository(database).accept(
        _command(
            kind=CommandKind.CANCEL_ITEM,
            payload={
                "order_id": str(sale.id),
                "order_item_id": str(item.id),
                "reason": "Pedido trocado pelo cliente",
            },
        )
    )

    service.apply_pending()

    entry = next(
        row for row in _audit_rows(database) if row["event_type"] == "item_canceled"
    )
    assert entry["severity"] == "critical"
    assert CHANNEL in entry["payload_json"]
    assert DEVICE in entry["payload_json"]


def test_owner_cannot_issue_remote_item_cancellation(open_sale) -> None:  # noqa: ANN001
    database, config, checkout, sale = open_sale
    item = checkout.current_sale.items[0]
    InboxRepository(database).accept(
        _command(
            kind=CommandKind.CANCEL_ITEM,
            payload={
                "order_id": str(sale.id),
                "order_item_id": str(item.id),
                "reason": "Tentativa pelo perfil errado",
            },
            issued_by=DEMO_OWNER_ID,
            issued_name=DEMO_OWNER_NAME,
        )
    )
    report = RemoteCommandService(database, config, checkout=checkout).apply_pending()
    assert report.refused == 1
    assert database.query_one(
        "SELECT canceled_at FROM order_items WHERE id=?", (item.id,)
    )["canceled_at"] is None


def test_cancelling_an_item_never_makes_the_total_negative(open_sale) -> None:  # noqa: ANN001
    """O desconto foi autorizado sobre o subtotal antigo.

    Mantê-lo integral depois de o pedido encolher transformaria o
    cancelamento em crédito para o cliente.
    """
    database, config, checkout, sale = open_sale
    service = RemoteCommandService(database, config, checkout=checkout)
    inbox = InboxRepository(database)
    inbox.accept(_discount(str(sale.id), percent="30"))
    service.apply_pending()

    for item in list(checkout.current_sale.items):
        inbox.accept(
            _command(
                kind=CommandKind.CANCEL_ITEM,
                payload={
                    "order_id": str(sale.id),
                    "order_item_id": str(item.id),
                    "reason": "Mesa desistiu",
                },
            )
        )
        service.apply_pending()

    order = database.query_one("SELECT * FROM orders WHERE id = ?", (sale.id,))
    assert order["subtotal_cents"] == 0
    assert order["discount_cents"] == 0
    assert order["total_cents"] == 0


def test_an_already_canceled_item_is_not_canceled_again(weighed_sale) -> None:  # noqa: ANN001
    """Dois cliques no painel não podem estornar o insumo duas vezes.

    A idempotência por `command_uuid` não cobre este caso: são dois comandos
    diferentes pedindo a mesma coisa. Quem barra o segundo é o `canceled_at`
    que o primeiro gravou.
    """
    database, config, checkout, sale = weighed_sale
    item = checkout.current_sale.items[0]
    service = RemoteCommandService(database, config, checkout=checkout)
    inbox = InboxRepository(database)

    def cancel() -> None:
        inbox.accept(
            _command(
                kind=CommandKind.CANCEL_ITEM,
                payload={
                    "order_id": str(sale.id),
                    "order_item_id": str(item.id),
                    "reason": "Pedido trocado",
                },
            )
        )

    cancel()
    service.apply_pending()
    after_first = {key: _balance(database, key) for key in _consumptions(database, str(item.id))}

    cancel()  # comando NOVO, mesmo item
    report = service.apply_pending()

    assert report.refused == 1
    assert {key: _balance(database, key) for key in after_first} == after_first


# --------------------------------------------------------------------------- #
# Relato de volta
# --------------------------------------------------------------------------- #


def test_the_results_wait_to_be_reported(open_sale) -> None:  # noqa: ANN001
    """Falhar ao avisar a nuvem não pode desfazer o que já foi aplicado."""
    database, config, checkout, sale = open_sale
    service = RemoteCommandService(database, config, checkout=checkout)
    inbox = InboxRepository(database)
    applied = _discount(str(sale.id), percent="10")
    over = _discount(str(sale.id), percent="80")
    inbox.accept(applied)
    inbox.accept(over)
    service.apply_pending()

    results = {r.command_uuid: r for r in inbox.unreported()}
    assert results[applied.command_uuid].status is CommandStatus.APPLIED
    assert results[over.command_uuid].status is CommandStatus.REFUSED
    assert results[over.command_uuid].message, "a recusa volta com o motivo"

    inbox.mark_reported(list(results))

    assert inbox.unreported() == []
    # E o desconto continua onde estava.
    assert (
        database.query_one("SELECT * FROM orders WHERE id = ?", (sale.id,))[
            "discount_cents"
        ]
        == 280
    )


def _balance(database: Database, inventory_item_id: str) -> int:
    row = database.query_one(
        "SELECT COALESCE(SUM(qty_mg), 0) AS saldo FROM stock_movements "
        " WHERE inventory_item_id = ?",
        (inventory_item_id,),
    )
    return int(row["saldo"])
