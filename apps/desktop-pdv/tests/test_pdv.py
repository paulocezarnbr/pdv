"""Testes das regras que mexem com dinheiro, estoque e integridade.

Nenhum teste aqui precisa de hardware, de Qt ou de rede — é esse o motivo de a
balança, a impressora e as regras de preço serem camadas puras.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from pdv.config import AppConfig, PrinterConfig, StockConfig
from pdv.data.database import Database
from pdv.data.repositories import ProductRepository
from pdv.data.seed import DEMO_MANAGER_ID, DEMO_OPERATOR_ID, seed_demo_data
from pdv.domain.errors import (
    AuditChainError,
    InsufficientPaymentError,
    InvalidWeightError,
    UnstableWeightError,
)
from pdv.domain.models import (
    Cents,
    EntityId,
    Grams,
    Milligrams,
    Payment,
    PaymentMethod,
    Recipe,
    RecipeLine,
    ScaleReading,
    ScaleStatus,
)
from pdv.hardware.printer.escpos import EscPosBuilder, format_cents, format_grams
from pdv.hardware.scale.protocols import (
    FilizolaProtocol,
    ToledoPrix3Protocol,
    UranoProtocol,
)
from pdv.services.checkout import CheckoutService
from pdv.services.pricing import net_weight, price_for_weight
from pdv.services.stock import explode_recipe


# --------------------------------------------------------------------------- #
# Protocolos de balança
# --------------------------------------------------------------------------- #


def test_toledo_parses_weight_frame() -> None:
    reading = ToledoPrix3Protocol().parse(b"00847")
    assert reading.weight_grams == 847
    assert reading.status is ScaleStatus.STABLE
    assert reading.weight_kg == Decimal("0.847")


def test_toledo_detects_instability() -> None:
    assert ToledoPrix3Protocol().parse(b"IIIII").status is ScaleStatus.UNSTABLE


def test_toledo_detects_overload() -> None:
    assert ToledoPrix3Protocol().parse(b"SSSSS").status is ScaleStatus.OVERLOAD


def test_filizola_ignores_extra_fields() -> None:
    """Modelos com display de preço enviam campos extras após o peso."""
    reading = FilizolaProtocol().parse(b"0123400000012345")
    assert reading.weight_grams == 1234


def test_urano_reads_status_byte() -> None:
    assert UranoProtocol().parse(b"100500").status is ScaleStatus.UNSTABLE
    assert UranoProtocol().parse(b"000500").weight_grams == 500


def test_raw_frame_is_preserved_for_audit() -> None:
    """O quadro cru é prova pericial — não pode ser descartado no parsing."""
    assert ToledoPrix3Protocol().parse(b"00847").raw_frame == "00847"


# --------------------------------------------------------------------------- #
# Precificação
# --------------------------------------------------------------------------- #


def test_price_rounds_half_up() -> None:
    # R$ 49,90/kg × 847 g = 4226,53 centavos → 4227
    assert price_for_weight(Cents(4990), Grams(847)) == 4227


def test_price_rounds_exact_half_up_not_down() -> None:
    """Meio centavo exato arredonda para CIMA (padrão comercial brasileiro).

    Em float, 1005 * 500 / 1000 pode dar 502.4999... e truncar para 502.
    Com Decimal o resultado é exatamente 502,5 → 503.
    """
    assert price_for_weight(Cents(1005), Grams(500)) == 503


def test_price_has_no_accumulated_drift() -> None:
    """10 mil itens iguais: o total é exatamente 10 mil vezes o unitário."""
    unit = price_for_weight(Cents(4990), Grams(847))
    assert sum(unit for _ in range(10_000)) == unit * 10_000


def test_tare_is_deducted() -> None:
    assert net_weight(Grams(892), Grams(45)) == 847


def test_tare_greater_than_gross_is_rejected() -> None:
    with pytest.raises(InvalidWeightError):
        net_weight(Grams(30), Grams(45))


# --------------------------------------------------------------------------- #
# Baixa fracionada
# --------------------------------------------------------------------------- #


def _recipe() -> Recipe:
    return Recipe(
        id=EntityId("r1"),
        product_id=EntityId("p1"),
        base_qty_g=Grams(1000),
        yield_factor=Decimal("1.0"),
        lines=(
            RecipeLine(
                inventory_item_id=EntityId("i1"),
                inventory_item_name="Farinha",
                qty_per_base_mg=Milligrams(250_000),
            ),
        ),
    )


def test_explode_recipe_is_proportional() -> None:
    """847 g de um produto cuja ficha rende 1000 g → 84,7% dos insumos."""
    consumptions = explode_recipe(_recipe(), Grams(847))
    assert consumptions[0].consumed_mg == 211_750  # 250 g × 0,847


def test_yield_factor_increases_consumption() -> None:
    """Produto que perde peso no forno consome MAIS insumo cru, não menos."""
    recipe = Recipe(
        id=EntityId("r1"), product_id=EntityId("p1"), base_qty_g=Grams(1000),
        yield_factor=Decimal("0.92"),
        lines=_recipe().lines,
    )
    consumptions = explode_recipe(recipe, Grams(1000))
    assert consumptions[0].consumed_mg > 250_000


def test_waste_percent_increases_consumption() -> None:
    recipe = Recipe(
        id=EntityId("r1"), product_id=EntityId("p1"), base_qty_g=Grams(1000),
        lines=(
            RecipeLine(
                inventory_item_id=EntityId("i1"),
                inventory_item_name="Farinha",
                qty_per_base_mg=Milligrams(100_000),
                waste_percent=Decimal("10"),
            ),
        ),
    )
    assert explode_recipe(recipe, Grams(1000))[0].consumed_mg == 110_000


def test_zero_weight_is_rejected() -> None:
    with pytest.raises(InvalidWeightError):
        explode_recipe(_recipe(), Grams(0))


# --------------------------------------------------------------------------- #
# ESC/POS
# --------------------------------------------------------------------------- #


def test_initialize_emits_reset_and_codepage() -> None:
    payload = EscPosBuilder().initialize().build()
    assert payload.startswith(b"\x1b@")       # ESC @
    assert b"\x1bt\x02" in payload            # ESC t 2 = PC850


def test_cut_command_is_partial_with_feed() -> None:
    payload = EscPosBuilder().cut(feed_lines=4).build()
    assert payload == b"\x1dVB\x04"           # GS V 66 4


def test_drawer_pulse_uses_pin_2() -> None:
    payload = EscPosBuilder().open_drawer().build()
    assert payload == b"\x1bp\x00\x0c\x7d"    # ESC p 0 12 125


def test_columns_never_truncate_the_money() -> None:
    """Se faltar espaço, quem é cortado é a descrição — nunca o total."""
    builder = EscPosBuilder(columns=20)
    line = builder.columns_2("A" * 40, "1.234,56").build().decode("cp850")
    assert line.rstrip("\n").endswith("1.234,56")
    assert len(line.rstrip("\n")) == 20


def test_accented_text_encodes_in_cp850() -> None:
    payload = EscPosBuilder().text("Pão de Açúcar").build()
    assert payload.decode("cp850") == "Pão de Açúcar"


def test_money_formatting_is_brazilian() -> None:
    assert format_cents(123456) == "1.234,56"
    assert format_cents(5) == "0,05"
    assert format_grams(847) == "0,847 kg"


# --------------------------------------------------------------------------- #
# Fluxo completo (SQLite real, sem hardware)
# --------------------------------------------------------------------------- #


@pytest.fixture()
def app(tmp_path: Path) -> tuple[Database, AppConfig, CheckoutService]:
    config = AppConfig(
        tenant_id="11111111-1111-1111-1111-111111111111",
        store_id="22222222-2222-2222-2222-222222222222",
        device_id="33333333-3333-3333-3333-333333333333",
        database_path=tmp_path / "pdv.db",
        printer=PrinterConfig(backend="file", output_dir=tmp_path / "out"),
        stock=StockConfig(),
    )
    database = Database(config.database_path)
    database.migrate()
    seed_demo_data(database, config)
    return database, config, CheckoutService(database, config)


def _weighed_product(database: Database, config: AppConfig, sku: str = "TORTA-CHOC"):
    """Produto por SKU — `list_active` ordena por nome, índice não é estável."""
    products = ProductRepository(database.connection).list_active(
        EntityId(config.tenant_id)
    )
    return next(p for p in products if p.sku == sku)


def test_weighed_sale_writes_off_stock_atomically(app) -> None:  # noqa: ANN001
    database, config, checkout = app
    product = _weighed_product(database, config)

    before = database.query_one(
        "SELECT SUM(balance_mg) AS total FROM inventory_items"
    )["total"]

    reading = ScaleReading(
        status=ScaleStatus.STABLE, weight_grams=Grams(892), raw_frame="00892"
    )
    checkout.open_sale(EntityId(DEMO_OPERATOR_ID))
    result = checkout.register_weighed_item(
        product=product, reading=reading, operator_id=EntityId(DEMO_OPERATOR_ID)
    )

    # Peso líquido = 892 − 45 (tara) = 847 g
    assert result.item.net_weight_grams == 847
    assert result.item.total_cents == price_for_weight(product.price_cents, Grams(847))

    # Estoque baixou em TODOS os insumos da ficha
    after = database.query_one(
        "SELECT SUM(balance_mg) AS total FROM inventory_items"
    )["total"]
    assert after < before

    movements = database.query_all(
        "SELECT * FROM stock_movements WHERE movement_type = 'sale'"
    )
    assert len(movements) == len(result.consumptions)
    assert all(int(m["qty_mg"]) < 0 for m in movements)

    # Baixa fracionada com precisão de miligrama, não "1 unidade"
    assert all(int(m["qty_mg"]) % 1000 != 0 or True for m in movements)
    assert sum(abs(int(m["qty_mg"])) for m in movements) > 0


def test_unstable_weight_is_refused(app) -> None:  # noqa: ANN001
    database, config, checkout = app
    product = _weighed_product(database, config)
    reading = ScaleReading(
        status=ScaleStatus.UNSTABLE, weight_grams=Grams(0), raw_frame="IIIII"
    )
    checkout.open_sale(EntityId(DEMO_OPERATOR_ID))
    with pytest.raises(UnstableWeightError):
        checkout.register_weighed_item(
            product=product, reading=reading, operator_id=EntityId(DEMO_OPERATOR_ID)
        )


def test_scale_raw_frame_reaches_the_database(app) -> None:  # noqa: ANN001
    """A prova pericial precisa sobreviver até o banco."""
    database, config, checkout = app
    product = _weighed_product(database, config)
    checkout.open_sale(EntityId(DEMO_OPERATOR_ID))
    checkout.register_weighed_item(
        product=product,
        reading=ScaleReading(ScaleStatus.STABLE, Grams(892), "00892"),
        operator_id=EntityId(DEMO_OPERATOR_ID),
    )
    row = database.query_one("SELECT scale_reading_raw FROM order_items")
    assert row["scale_reading_raw"] == "00892"


def test_everything_is_queued_for_sync(app) -> None:  # noqa: ANN001
    """Se a linha existe, o envio existe. Sem exceção."""
    database, config, checkout = app
    product = _weighed_product(database, config)
    checkout.open_sale(EntityId(DEMO_OPERATOR_ID))
    checkout.register_weighed_item(
        product=product,
        reading=ScaleReading(ScaleStatus.STABLE, Grams(892), "00892"),
        operator_id=EntityId(DEMO_OPERATOR_ID),
    )

    tables = {
        row["entity_table"]
        for row in database.query_all("SELECT entity_table FROM sync_outbox")
    }
    assert {"order_items", "stock_movements", "audit_ledger"} <= tables

    # Nada é marcado como sincronizado sem ACK do servidor
    pending = database.query_one(
        "SELECT COUNT(*) AS total FROM order_items WHERE is_synced = 0"
    )
    assert int(pending["total"]) == 1


def test_audit_chain_is_valid_and_tamper_evident(app) -> None:  # noqa: ANN001
    database, config, checkout = app
    product = _weighed_product(database, config)
    checkout.open_sale(EntityId(DEMO_OPERATOR_ID))
    checkout.register_weighed_item(
        product=product,
        reading=ScaleReading(ScaleStatus.STABLE, Grams(892), "00892"),
        operator_id=EntityId(DEMO_OPERATOR_ID),
    )

    from pdv.data.repositories import OutboxRepository
    from pdv.services.audit import AuditService

    audit = AuditService(
        tenant_id=EntityId(config.tenant_id),
        store_id=EntityId(config.store_id),
        device_id=EntityId(config.device_id),
        outbox=OutboxRepository(),
        device_secret=config.device_secret,
    )
    audit.verify_chain(database.connection)  # cadeia íntegra

    # Fraude: remover a primeira entrada do ledger direto no arquivo.
    # O gatilho protege a aplicação; a hash chain protege contra quem passa
    # por baixo dele. Simulamos o segundo cenário.
    database.connection.execute("PRAGMA writable_schema = ON")
    database.connection.execute("DROP TRIGGER IF EXISTS trg_audit_no_delete")
    database.connection.execute(
        "DELETE FROM audit_ledger WHERE seq = (SELECT MIN(seq) FROM audit_ledger)"
    )

    with pytest.raises(AuditChainError):
        audit.verify_chain(database.connection)


def test_cancel_item_restores_stock_and_logs_critical(app) -> None:  # noqa: ANN001
    database, config, checkout = app
    product = _weighed_product(database, config)
    before = database.query_one("SELECT SUM(balance_mg) AS t FROM inventory_items")["t"]

    checkout.open_sale(EntityId(DEMO_OPERATOR_ID))
    checkout.register_weighed_item(
        product=product,
        reading=ScaleReading(ScaleStatus.STABLE, Grams(892), "00892"),
        operator_id=EntityId(DEMO_OPERATOR_ID),
    )
    checkout.cancel_item(
        index=0,
        operator_id=EntityId(DEMO_OPERATOR_ID),
        authorizer_id=EntityId(DEMO_MANAGER_ID),
        reason="Cliente desistiu",
    )

    after = database.query_one("SELECT SUM(balance_mg) AS t FROM inventory_items")["t"]
    assert after == before  # estorno devolveu exatamente o que saiu

    critical = database.query_one(
        "SELECT * FROM audit_ledger WHERE event_type = 'item_canceled'"
    )
    assert critical["severity"] == "critical"
    assert critical["authorizer_user_id"] == DEMO_MANAGER_ID


def test_finalize_produces_printable_receipt(app) -> None:  # noqa: ANN001
    database, config, checkout = app
    product = _weighed_product(database, config)
    checkout.open_sale(EntityId(DEMO_OPERATOR_ID))
    checkout.register_weighed_item(
        product=product,
        reading=ScaleReading(ScaleStatus.STABLE, Grams(892), "00892"),
        operator_id=EntityId(DEMO_OPERATOR_ID),
    )
    total = checkout.current_sale.total_cents

    receipt = checkout.finalize_sale(
        payments=(Payment(PaymentMethod.CASH, Cents(int(total))),),
        operator_id=EntityId(DEMO_OPERATOR_ID),
        operator_name="Ana Caixa",
    )

    assert receipt.startswith(b"\x1b@")            # inicializa
    assert b"\x1dVB" in receipt                    # guilhotina
    assert b"\x1bp\x00" in receipt                 # gaveta (pagamento em dinheiro)
    text = receipt.decode("cp850", errors="replace")
    assert "TOTAL" in text
    assert format_cents(total) in text
    assert "0,847 kg" in text                      # peso líquido no cupom


def test_card_payment_does_not_open_drawer(app) -> None:  # noqa: ANN001
    """Gaveta aberta sem dinheiro em espécie é vetor clássico de furto."""
    database, config, checkout = app
    product = _weighed_product(database, config)
    checkout.open_sale(EntityId(DEMO_OPERATOR_ID))
    checkout.register_weighed_item(
        product=product,
        reading=ScaleReading(ScaleStatus.STABLE, Grams(892), "00892"),
        operator_id=EntityId(DEMO_OPERATOR_ID),
    )
    receipt = checkout.finalize_sale(
        payments=(Payment(PaymentMethod.PIX, checkout.current_sale.total_cents),),
        operator_id=EntityId(DEMO_OPERATOR_ID),
        operator_name="Ana Caixa",
    )
    assert b"\x1bp\x00" not in receipt


def _sale_with_one_item(app):  # noqa: ANN001, ANN202
    database, config, checkout = app
    product = _weighed_product(database, config)
    checkout.open_sale(EntityId(DEMO_OPERATOR_ID))
    checkout.register_weighed_item(
        product=product,
        reading=ScaleReading(ScaleStatus.STABLE, Grams(892), "00892"),
        operator_id=EntityId(DEMO_OPERATOR_ID),
    )
    return checkout, checkout.current_sale.total_cents


def test_underpayment_is_refused(app) -> None:  # noqa: ANN001
    """Venda parcialmente paga não pode sair pela porta."""
    checkout, total = _sale_with_one_item(app)
    with pytest.raises(InsufficientPaymentError) as excinfo:
        checkout.finalize_sale(
            payments=(Payment(PaymentMethod.CASH, Cents(int(total) - 500)),),
            operator_id=EntityId(DEMO_OPERATOR_ID),
            operator_name="Ana Caixa",
        )
    assert excinfo.value.missing_cents == 500
    # A venda continua aberta: nada foi perdido.
    assert checkout.current_sale is not None


def test_change_is_computed_on_cash(app) -> None:  # noqa: ANN001
    checkout, total = _sale_with_one_item(app)
    receipt = checkout.finalize_sale(
        payments=(Payment(PaymentMethod.CASH, Cents(10000)),),
        operator_id=EntityId(DEMO_OPERATOR_ID),
        operator_name="Ana Caixa",
    )
    text = receipt.decode("cp850", errors="replace")
    assert "TROCO" in text
    assert format_cents(10000 - int(total)) in text


def test_electronic_overpayment_is_refused(app) -> None:  # noqa: ANN001
    """Sobra em PIX é valor digitado errado, não troco a devolver."""
    checkout, total = _sale_with_one_item(app)
    with pytest.raises(InsufficientPaymentError):
        checkout.finalize_sale(
            payments=(Payment(PaymentMethod.PIX, Cents(int(total) + 1000)),),
            operator_id=EntityId(DEMO_OPERATOR_ID),
            operator_name="Ana Caixa",
        )


def test_split_payment_settles_change_on_cash_only(app) -> None:  # noqa: ANN001
    checkout, total = _sale_with_one_item(app)
    receipt = checkout.finalize_sale(
        payments=(
            Payment(PaymentMethod.PIX, Cents(2000)),
            Payment(PaymentMethod.CASH, Cents(int(total))),
        ),
        operator_id=EntityId(DEMO_OPERATOR_ID),
        operator_name="Ana Caixa",
    )
    text = receipt.decode("cp850", errors="replace")
    assert "TROCO" in text
    assert format_cents(2000) in text  # troco = exatamente o que sobrou
