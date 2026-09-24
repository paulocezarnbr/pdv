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
from pdv.data.seed import (
    DEMO_OPERATOR_ID,
    DEMO_OPERATOR_LOGIN,
    DEMO_OPERATOR_PIN,
    seed_demo_data,
)
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
from pdv.services.authorization import AuthorizationService
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

    # O caixa passou a exigir login: quem opera vem da sessão autenticada, e
    # não mais de uma constante. O teste entra como Ana, de verdade.
    operator = AuthorizationService(database, config.tenant_id).authenticate(
        DEMO_OPERATOR_LOGIN, DEMO_OPERATOR_PIN
    )
    widget = CounterWindow(
        checkout, scale, printer, config, database,
        operator=operator, edge_port=8420,
    )
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
    # A coluna 1 é o garçom e a 5 é a situação — o pedido é aberto direto pelo
    # serviço neste teste, sem sessão no app, e por isso o nome vem do cadastro.
    assert panel._orders_table.item(0, 1).text() == "Ana"
    assert panel._orders_table.item(0, 5).text() == "", "ninguém pediu a conta ainda"
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

    assert panel._orders_table.item(0, 5).text() == "pedindo a conta"
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
    # Oito dígitos em dois grupos de quatro: o formato que as pessoas já leem
    # em código de confirmação, sem contar dígito.
    assert len(text) == 9 and text[4] == " "
    assert text.replace(" ", "").isdigit()


def test_a_paired_device_is_listed_and_can_be_revoked(salon, env) -> None:  # noqa: ANN001
    panel, _database, _config = salon
    code, _ = panel._auth.create_pairing_code()
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


# --------------------------------------------------------------------------- #
# Comandos do painel
# --------------------------------------------------------------------------- #


def test_the_command_badge_stays_out_of_the_way_when_idle(window) -> None:  # noqa: ANN001
    """Selo permanente dizendo "0 comandos" gastaria atenção todo dia."""
    widget, _checkout, _database, _config = window

    widget._refresh_sync_badge()

    assert widget._command_label.isVisibleTo(widget) is False


def test_the_counter_is_told_a_remote_command_is_waiting(window) -> None:  # noqa: ANN001
    """Sem o aviso, o total mudaria sozinho no meio do atendimento.

    O operador ficaria olhando para um número que não bate com o que ele
    digitou, sem nada na tela explicando por quê.
    """
    from pdv.remote.inbox import InboxRepository
    from pdv.remote.protocol import CommandKind, RemoteCommand

    widget, _checkout, database, _config = window
    InboxRepository(database).accept(
        RemoteCommand(
            command_uuid=new_id(),
            tenant_id=TENANT,
            store_id=STORE,
            device_id=DEVICE,
            kind=CommandKind.APPLY_DISCOUNT,
            payload={"order_id": "x", "percent": "10", "reason": "y"},
            issued_by_user_id="gerente",
            issued_by_name="Bruno Gerente",
            issued_at="2026-09-19T12:00:00+00:00",
            signature="nao-importa-aqui",
        )
    )

    widget._refresh_sync_badge()

    assert widget._command_label.isVisibleTo(widget) is True
    assert "1 comando" in widget._command_label.text()


# --------------------------------------------------------------------------- #
# Aceite no caixa (trava 7 do canal remoto)
# --------------------------------------------------------------------------- #


def _kitchen_cancel(database: Database, config: AppConfig) -> tuple[str, str]:
    """Mesa 4 com um café na cozinha e o pedido de cancelamento dele na inbox.

    Devolve `(command_uuid, order_item_id)`.
    """
    from pdv.data.seed import DEMO_MANAGER_ID, DEMO_MANAGER_NAME
    from pdv.domain.models import iso
    from pdv.edge.tables import TableService
    from pdv.remote.inbox import InboxRepository
    from pdv.remote.protocol import CommandKind, RemoteCommand, sign_command

    orders = TableOrderService(database, config)
    table = TableService(database, config).find_by_label("Mesa 4")
    order = orders.open_order(
        client_uuid=EntityId(new_id()),
        operator_id=EntityId(DEMO_OPERATOR_ID),
        table_id=table.id,
        origin_device_id=EntityId(new_id()),
    )
    coffee = database.query_one("SELECT id FROM products WHERE sku = 'CAFE-EXP'")
    orders.add_item(
        order_id=order.id,
        client_uuid=EntityId(new_id()),
        product_id=EntityId(str(coffee["id"])),
        quantity=Decimal("1"),
    )
    item_id = str(orders.list_items(order.id)[0]["id"])

    uuid, issued_at = new_id(), iso(utc_now())
    payload = {"order_id": str(order.id), "order_item_id": item_id, "reason": "desistiu"}
    InboxRepository(database).accept(
        RemoteCommand(
            command_uuid=uuid,
            tenant_id=TENANT,
            store_id=STORE,
            device_id=DEVICE,
            kind=CommandKind.CANCEL_ITEM,
            payload=payload,
            issued_by_user_id=DEMO_MANAGER_ID,
            issued_by_name=DEMO_MANAGER_NAME,
            issued_at=issued_at,
            signature=sign_command(
                secret=config.device_secret,
                command_uuid=uuid,
                device_id=DEVICE,
                kind=CommandKind.CANCEL_ITEM.value,
                payload=payload,
                issued_at=issued_at,
            ),
        )
    )
    return uuid, item_id


def test_a_kitchen_cancel_asks_the_counter_by_name(window) -> None:  # noqa: ANN001
    """O pedido de aceite não se resolve sozinho: vira botão, não selo mudo."""
    widget, _checkout, database, config = window
    _kitchen_cancel(database, config)
    widget._remote.apply_pending()

    widget._refresh_sync_badge()

    assert widget._confirm_button.isVisibleTo(widget) is True
    assert "1 cancelamento" in widget._confirm_button.text()
    assert "Ctrl+F4" in widget._confirm_button.text()
    # Não é "comando a aplicar": aplicar depende de alguém aqui.
    assert widget._command_label.isVisibleTo(widget) is False


def test_the_counter_accepts_in_the_dialog(qtbot, window) -> None:  # noqa: ANN001
    from pdv.ui.remote_dialog import RemoteConfirmationDialog

    widget, _checkout, database, config = window
    _uuid, item_id = _kitchen_cancel(database, config)
    widget._remote.apply_pending()
    dialog = RemoteConfirmationDialog(widget._remote, default_login=DEMO_OPERATOR_LOGIN)
    qtbot.addWidget(dialog)
    assert dialog.waiting_count == 1
    assert "Café Expresso" in dialog._list.item(0).text()

    dialog._pin.setText("000001")
    dialog._accept_selected()
    assert dialog._error.text(), "PIN errado aparece na tela"
    assert dialog.waiting_count == 1, "e não decide nada"

    dialog._pin.setText(DEMO_OPERATOR_PIN)
    dialog._accept_selected()

    assert dialog.waiting_count == 0
    assert "Ana Caixa" in dialog.decisions[-1]
    row = database.query_one("SELECT canceled_at FROM order_items WHERE id = ?", (item_id,))
    assert row["canceled_at"] is not None


def test_declining_in_the_dialog_needs_a_reason(qtbot, window) -> None:  # noqa: ANN001
    from pdv.ui.remote_dialog import RemoteConfirmationDialog

    widget, _checkout, database, config = window
    uuid, item_id = _kitchen_cancel(database, config)
    widget._remote.apply_pending()
    dialog = RemoteConfirmationDialog(widget._remote, default_login=DEMO_OPERATOR_LOGIN)
    qtbot.addWidget(dialog)

    dialog._pin.setText(DEMO_OPERATOR_PIN)
    dialog._decline_selected()
    assert "motivo" in dialog._error.text().lower()
    assert dialog.waiting_count == 1

    dialog._pin.setText(DEMO_OPERATOR_PIN)
    dialog._reason.setText("Prato já foi servido")
    dialog._decline_selected()

    assert dialog.waiting_count == 0
    status = database.query_one(
        "SELECT status FROM remote_commands WHERE command_uuid = ?", (uuid,)
    )
    assert status["status"] == "refused"
    row = database.query_one("SELECT canceled_at FROM order_items WHERE id = ?", (item_id,))
    assert row["canceled_at"] is None


def test_the_dialog_offers_only_counter_logins(qtbot, window) -> None:  # noqa: ANN001
    from pdv.data.seed import DEMO_WAITER_LOGIN
    from pdv.ui.remote_dialog import RemoteConfirmationDialog

    widget, _checkout, _database, _config = window
    dialog = RemoteConfirmationDialog(widget._remote, default_login=DEMO_OPERATOR_LOGIN)
    qtbot.addWidget(dialog)

    logins = [dialog._login.itemText(i) for i in range(dialog._login.count())]
    assert DEMO_OPERATOR_LOGIN in logins
    assert DEMO_WAITER_LOGIN not in logins
    assert dialog._accept_button.isEnabled() is False, "nada a decidir"


# --------------------------------------------------------------------------- #
# Atalhos e modo demonstração
# --------------------------------------------------------------------------- #


def test_every_shortcut_is_wired_from_the_single_table(window) -> None:  # noqa: ANN001
    """Teclado, painel e F1 saem da mesma tabela; uma tecla órfã quebra aqui."""
    from pdv.ui.counter_window import SHORTCUTS

    widget, *_ = window
    keys = [key for key, *_rest in SHORTCUTS]

    assert len(keys) == len(set(keys)), "tecla repetida na tabela"
    assert set(widget._shortcuts) == set(keys)
    for key, _label, method, _panel in SHORTCUTS:
        assert callable(getattr(widget, method)), f"{key} aponta para {method}, que não existe"


def test_keys_without_a_button_are_visible_in_the_side_panel(window) -> None:  # noqa: ANN001
    """F5, F7, F11, F12 e os Ctrl ficavam invisíveis: só quem decorou sabia."""
    widget, *_ = window

    assert {"F5", "F7", "F11", "F12", "Ctrl+F4", "Ctrl+F6"} <= set(widget._shortcut_buttons)
    # Os que já têm botão na tela não se repetem no painel.
    assert not {"F2", "F4", "F6", "F10"} & set(widget._shortcut_buttons)


def test_f1_lists_every_shortcut(window, monkeypatch) -> None:  # noqa: ANN001
    from PySide6.QtWidgets import QDialog, QLabel

    from pdv.ui.counter_window import SHORTCUTS

    widget, *_ = window
    monkeypatch.setattr(QDialog, "exec", lambda self: 0)

    widget._show_shortcuts()

    texts = {label.text() for label in widget._shortcuts_dialog.findChildren(QLabel)}
    for key, label, *_rest in SHORTCUTS:
        assert key in texts and label in texts


def test_the_demo_banner_shows_only_in_demo_mode(window, qtbot, env) -> None:  # noqa: ANN001
    widget, checkout, database, config = window
    assert widget._demo_banner.isVisibleTo(widget) is False

    demo = CounterWindow(
        checkout,
        ScaleService(build_scale(config.scale), config.scale),
        PrintService(build_printer(config.printer)),
        config,
        database,
        operator=widget._operator,
        on_activate=lambda parent: False,
    )
    qtbot.addWidget(demo)
    demo._refresh_sync_badge()

    assert demo._demo_banner.isVisibleTo(demo) is True
    assert "demonstração" in demo._sync_label.text()


def test_activating_from_the_banner_asks_for_a_restart(window, qtbot) -> None:  # noqa: ANN001
    widget, checkout, database, config = window
    calls: list[object] = []
    demo = CounterWindow(
        checkout,
        ScaleService(build_scale(config.scale), config.scale),
        PrintService(build_printer(config.printer)),
        config,
        database,
        operator=widget._operator,
        on_activate=lambda parent: calls.append(parent) or True,
    )
    qtbot.addWidget(demo)
    demo.show()

    demo._activate_button.click()

    assert calls == [demo]
    assert demo.restart_requested is True
    assert demo.isVisible() is False


def test_giving_up_on_activation_keeps_the_counter_open(window, qtbot) -> None:  # noqa: ANN001
    widget, checkout, database, config = window
    demo = CounterWindow(
        checkout,
        ScaleService(build_scale(config.scale), config.scale),
        PrintService(build_printer(config.printer)),
        config,
        database,
        operator=widget._operator,
        on_activate=lambda parent: False,
    )
    qtbot.addWidget(demo)
    demo.show()

    demo._activate_button.click()

    assert demo.restart_requested is False
    assert demo.isVisible() is True
