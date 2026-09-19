"""Testes da janela do caixa e do painel do salão.

Estes testes cobrem uma camada que os testes de serviço **não alcançam**: a
fiação. Na Fase 3 um `from __future__ import annotations` no `server.py` fez
todas as rotas responderem 422 com os serviços 100% verdes por baixo — o erro
não estava na regra, estava no ponto onde a regra encosta no framework.

A janela tem a mesma exposição: um nome de coluna trocado, um `None` onde a
tabela espera texto, um sinal ligado a um método que mudou de assinatura. Nada
disso aparece em teste de `CheckoutService`; aparece na frente do cliente.

Por isso o foco aqui é construir de verdade e mandar repintar de verdade.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from pdv.config import AppConfig, PrinterConfig, ScaleConfig
from pdv.data.database import Database
from pdv.data.repositories import ProductRepository
from pdv.data.seed import DEMO_OPERATOR_ID, seed_demo_data
from pdv.domain.models import (
    EntityId,
    Grams,
    PricingMode,
    ScaleReading,
    ScaleStatus,
    new_id,
    utc_now,
)
from pdv.edge.orders import TableOrderService
from pdv.services.checkout import CheckoutService

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402

from pdv.hardware.printer.backends import PrintService, build_printer  # noqa: E402
from pdv.hardware.scale.serial_scale import build_scale  # noqa: E402
from pdv.hardware.scale.worker import ScaleService  # noqa: E402
from pdv.ui.counter_window import CounterWindow  # noqa: E402
from pdv.ui.salon_panel import SalonPanel  # noqa: E402

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
        scale=ScaleConfig(protocol="simulated"),
        printer=PrinterConfig(backend="file", output_dir=tmp_path / "cupons"),
    )
    database = Database(config.database_path)
    database.migrate()
    seed_demo_data(database, config)
    return database, config


def _unit_product(database: Database):  # noqa: ANN202
    products = ProductRepository(database.connection).list_active(EntityId(TENANT))
    return next(p for p in products if p.pricing_mode is PricingMode.UNIT)


@pytest.fixture()
def window(qtbot, env):  # noqa: ANN001, ANN201
    database, config = env
    checkout = CheckoutService(database, config)
    scale = ScaleService(build_scale(config.scale), config.scale)
    printer = PrintService(build_printer(config.printer))

    widget = CounterWindow(checkout, scale, printer, config, database, edge_port=8420)
    qtbot.addWidget(widget)
    yield widget, checkout, database, config

    # A balança não foi iniciada, mas a fila de impressão sobe uma thread no
    # construtor: sem o desligamento o pytest terminaria com ela viva.
    printer.shutdown()


# --------------------------------------------------------------------------- #
# Janela do caixa
# --------------------------------------------------------------------------- #


def test_unit_products_reach_the_counter(window) -> None:  # noqa: ANN001
    """O bug que este teste tranca: o catálogo tinha três produtos unitários e
    a tela filtrava só pesáveis — eram invendáveis no caixa."""
    widget, _checkout, _database, _config = window

    names = [widget._unit_list.item(i).text() for i in range(widget._unit_list.count())]

    assert len(names) == 3
    assert any("Café Expresso" in name for name in names)
    assert widget._product_combo.count() == 2, "os pesáveis continuam no combo"


def test_the_search_filters_by_code_and_by_name(window) -> None:  # noqa: ANN001
    widget, _checkout, _database, _config = window

    widget._unit_search.setText("SUCO")
    assert widget._unit_list.count() == 1

    widget._unit_search.setText("torta")
    assert widget._unit_list.count() == 1
    assert widget._selected_unit_product is not None

    widget._unit_search.setText("xxx")
    assert widget._unit_list.count() == 0
    assert widget._selected_unit_product is None


def test_registering_a_unit_item_updates_the_table_and_the_total(window) -> None:  # noqa: ANN001
    widget, checkout, _database, _config = window
    widget._unit_search.setText("CAFE-EXP")
    widget._unit_quantity.setValue(2)

    widget._register_unit_item()

    assert widget._items_table.rowCount() == 1
    assert widget._items_table.item(0, 1).text() == "x 2"
    assert "14,00" in widget._total_label.text()
    assert int(checkout.current_sale.total_cents) == 1400
    assert widget._unit_search.text() == "", "o campo tem de ficar pronto para o próximo"


def test_a_weighed_item_still_shows_its_net_weight(window) -> None:  # noqa: ANN001
    """As duas naturezas dividem a mesma tabela; a coluna muda de significado."""
    widget, checkout, database, _config = window
    product = next(
        p
        for p in ProductRepository(database.connection).list_active(EntityId(TENANT))
        if p.is_weighed
    )
    reading = ScaleReading(
        weight_grams=Grams(1045),
        status=ScaleStatus.STABLE,
        raw_frame="+001.045kg",
        read_at=utc_now(),
    )
    item = checkout.register_weighed_item(
        product=product, reading=reading, operator_id=EntityId(DEMO_OPERATOR_ID)
    )
    widget._append_item_row(item.item, item.consumptions)

    assert "kg" in widget._items_table.item(0, 1).text()
    assert "/kg" in widget._items_table.item(0, 2).text()


def test_the_discount_line_appears_only_when_there_is_one(window) -> None:  # noqa: ANN001
    widget, checkout, _database, _config = window
    widget._unit_search.setText("CAFE-EXP")
    widget._register_unit_item()

    assert widget._discount_label.text() == ""

    checkout.apply_discount(
        percent=Decimal("10"),
        operator_id=EntityId(DEMO_OPERATOR_ID),
        authorizer_id=EntityId(DEMO_OPERATOR_ID),
        reason="teste",
    )
    widget._refresh_total()

    assert "Desconto" in widget._discount_label.text()
    assert "6,30" in widget._total_label.text()


def test_finalizing_without_items_does_nothing(window) -> None:  # noqa: ANN001
    """F10 com a tela vazia não pode abrir diálogo de recebimento de R$ 0,00."""
    widget, checkout, _database, _config = window

    widget._finalize_sale()

    assert checkout.current_sale is None


# --------------------------------------------------------------------------- #
# Painel do salão
# --------------------------------------------------------------------------- #


@pytest.fixture()
def salon(qtbot, env):  # noqa: ANN001, ANN201
    database, config = env
    panel = SalonPanel(database, config, port=8420)
    qtbot.addWidget(panel)
    # O timer de 2 s repintaria durante o teste e roubaria a seleção.
    panel._timer.stop()
    return panel, database, config


def test_the_panel_shows_open_tables_and_the_kitchen_queue(salon, env) -> None:  # noqa: ANN001
    panel, database, config = salon
    orders = TableOrderService(database, config)
    order = orders.open_order(
        client_uuid=EntityId(new_id()),
        operator_id=EntityId(DEMO_OPERATOR_ID),
        table_label="Mesa 7",
        origin_device_id=EntityId("cel-ana"),
    )
    orders.add_item(
        order_id=order.id,
        client_uuid=EntityId(new_id()),
        product_id=_unit_product(database).id,
        quantity=Decimal("2"),
    )

    panel.refresh()

    assert panel._orders_table.rowCount() == 1
    assert panel._orders_table.item(0, 0).text() == "Mesa 7"
    assert panel._orders_table.item(0, 4).text() == "", "ninguém pediu a conta ainda"
    assert panel._kds_table.rowCount() == 1
    assert panel._kds_table.item(0, 3).text() == "na fila"


def test_the_counter_sees_which_table_asked_for_the_bill(salon, env) -> None:  # noqa: ANN001
    """É a única linha da tela em que alguém está de pé, esperando para pagar.

    Sem o destaque, o garçom acaba tendo de vir avisar o caixa — que é
    exatamente o passo que o app veio eliminar.
    """
    panel, database, config = salon
    orders = TableOrderService(database, config)
    order = orders.open_order(
        client_uuid=EntityId(new_id()),
        operator_id=EntityId(DEMO_OPERATOR_ID),
        table_label="Mesa 7",
        origin_device_id=EntityId("cel-ana"),
    )
    orders.request_bill(order.id)

    panel.refresh()

    from pdv.ui import theme

    assert panel._orders_table.item(0, 4).text() == "pedindo a conta"
    assert panel._orders_table.item(0, 0).foreground().color().name() == theme.WARN


def test_the_panel_tells_where_the_waiter_app_lives(salon, env) -> None:  # noqa: ANN001
    """O endereço deixou de ser diagnóstico: é o app que o celular abre."""
    panel, _database, _config = salon

    panel.refresh()

    assert "App do garçom" in panel._address_label.text()
    assert ":8420" in panel._address_label.text()


def test_the_counter_can_unstick_a_ticket(salon, env) -> None:  # noqa: ANN001
    """A TV da cozinha fica fora de alcance e ninguém toca nela de mão suja."""
    panel, database, config = salon
    orders = TableOrderService(database, config)
    order = orders.open_order(
        client_uuid=EntityId(new_id()),
        operator_id=EntityId(DEMO_OPERATOR_ID),
        table_label="Mesa 3",
        origin_device_id=EntityId("cel-ana"),
    )
    orders.add_item(
        order_id=order.id,
        client_uuid=EntityId(new_id()),
        product_id=_unit_product(database).id,
        quantity=Decimal("1"),
    )
    panel.refresh()
    panel._kds_table.selectRow(0)

    panel._bump()
    assert panel._kds_table.item(0, 3).text() == "preparando"

    panel._recall()
    assert panel._kds_table.item(0, 3).text() == "na fila"


def test_the_selection_survives_a_refresh(salon, env) -> None:  # noqa: ANN001
    """Sem isto, o repinte a cada dois segundos tiraria a seleção debaixo do
    dedo do operador — e ele daria bump no ticket errado."""
    panel, database, config = salon
    orders = TableOrderService(database, config)
    for label in ("Mesa 1", "Mesa 2"):
        order = orders.open_order(
            client_uuid=EntityId(new_id()),
            operator_id=EntityId(DEMO_OPERATOR_ID),
            table_label=label,
            origin_device_id=EntityId("cel-ana"),
        )
        orders.add_item(
            order_id=order.id,
            client_uuid=EntityId(new_id()),
            product_id=_unit_product(database).id,
            quantity=Decimal("1"),
        )
    panel.refresh()
    panel._kds_table.selectRow(1)
    chosen = panel._kds_table.item(1, 0).text()

    panel.refresh()

    assert panel._kds_table.item(panel._kds_table.currentRow(), 0).text() == chosen


def test_a_pairing_code_is_shown_grouped(salon) -> None:  # noqa: ANN001
    """O garçom lê da tela do caixa e digita no celular, de pé, a dois metros."""
    panel, _database, _config = salon

    panel._generate_code()

    text = panel._code_label.text()
    assert len(text) == 7 and text[3] == " "
    assert text.replace(" ", "").isdigit()


def test_a_paired_device_is_listed_and_can_be_revoked(salon, env) -> None:  # noqa: ANN001
    panel, _database, _config = salon
    code = panel._auth.create_pairing_code()
    token = panel._auth.pair(code, device_name="Celular da Ana")

    # Pareado e ainda calado: o pareamento não registra contato, e a coluna tem
    # de distinguir "nunca falou" de "falou agora" — é assim que o caixa
    # descobre que o garçom digitou o código e parou na tela de login.
    panel.refresh()
    assert panel._devices_table.rowCount() == 1
    assert panel._devices_table.item(0, 2).text() == "nunca conectou"

    panel._auth.authenticate(token)
    panel.refresh()
    assert panel._devices_table.item(0, 2).text().startswith("visto")

    device_id = panel._devices_table.item(0, 0).data(Qt.ItemDataRole.UserRole)
    panel._auth.revoke(EntityId(str(device_id)))
    panel.refresh()

    assert panel._devices_table.item(0, 2).text() == "revogado"


def test_the_panel_says_so_when_the_salon_server_is_off(qtbot, env) -> None:  # noqa: ANN001
    """Servidor desligado não é falha do caixa — e o operador precisa saber
    por que o celular do garçom não acha a loja."""
    database, config = env
    panel = SalonPanel(database, config, port=None)
    qtbot.addWidget(panel)
    panel._timer.stop()

    assert "DESLIGADO" in panel._address_label.text()


# --------------------------------------------------------------------------- #
# Tema e formatação
# --------------------------------------------------------------------------- #


def test_the_waiting_time_stays_readable_past_an_hour() -> None:
    """"377:23" não comunica "seis horas parado" — comunica tela quebrada."""
    from pdv.ui.salon_panel import _format_wait

    assert _format_wait(0) == "00:00"
    assert _format_wait(83) == "01:23"
    assert _format_wait(3599) == "59:59"
    assert _format_wait(3600) == "1h00"
    assert _format_wait(22643) == "6h17"


def test_the_theme_is_pinned_not_inherited_from_windows(qtbot) -> None:  # noqa: ANN001
    """Dois terminais da mesma loja têm de mostrar a mesma tela.

    Sem paleta e folha de estilo explícitas o Qt segue o tema do Windows, e o
    contraste calculado para o balcão — ler o peso de pé, a um metro, sob
    lâmpada fria — valeria só na máquina que foi testada.
    """
    from PySide6.QtWidgets import QApplication

    from pdv.ui import theme

    app = QApplication.instance()
    theme.apply_theme(app)

    assert app.palette().window().color().name() == theme.CANVAS
    assert app.palette().windowText().color().name() == theme.TEXT
    assert "QPushButton#primary" in app.styleSheet()
    assert app.font().families()[0] == theme.FAMILY_UI[0]


def test_the_style_is_fusion(qtbot) -> None:  # noqa: ANN001
    """O estilo nativo do Windows ignora boa parte do QSS e segue o tema do SO.

    Ler o estilo depois de `apply_theme` devolve vazio: a folha de estilo
    embrulha o estilo base num `QStyleSheetStyle`, e o PySide não expõe
    `baseStyle()`. Limpar a folha desembrulha — é o único jeito de conferir o
    que está por baixo.
    """
    from PySide6.QtWidgets import QApplication

    from pdv.ui import theme

    app = QApplication.instance()
    theme.apply_theme(app)
    installed = app.styleSheet()
    try:
        app.setStyleSheet("")
        assert app.style().name() == "fusion"
    finally:
        app.setStyleSheet(installed)
