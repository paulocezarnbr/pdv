"""Tela do Caixa de Balcão.

Princípios de UI de PDV que o layout respeita:

* **Teclado acima do mouse.** O operador não tira a mão do teclado numa fila.
  F2 registra o pesado, F3 lança o unitário, F4 cancela item, F6 desconta,
  F8 abre o salão, F10 finaliza.
* **O peso é o maior elemento da tela.** É o número que o cliente confere de pé
  do outro lado do balcão.
* **Estado de conexão sempre visível.** O operador precisa saber que está
  offline — não para se preocupar, mas para não estranhar o relatório da nuvem.
* **A UI nunca calcula dinheiro.** Ela exibe o que o `CheckoutService` decidiu.
  Regra de negócio em widget é dívida técnica que vaza para o financeiro.
* **A UI nunca decide quem pode autorizar.** Cancelamento e desconto passam
  pelo `AuthorizationService`, que valida Argon2id contra a réplica local e
  funciona sem internet. Diálogo que pede senha e aceita qualquer coisa é pior
  do que não pedir: fabrica no relatório a aparência de uma autorização.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.data.seed import DEMO_OPERATOR_ID, DEMO_OPERATOR_NAME
from pdv.domain.errors import PdvError
from pdv.domain.models import (
    Cents,
    EntityId,
    Product,
    SaleItem,
    ScaleReading,
    ScaleStatus,
)
from pdv.hardware.printer.backends import PrintService
from pdv.hardware.printer.escpos import format_cents, format_grams
from pdv.hardware.scale.worker import ScaleService
from pdv.services.authorization import AuthorizationService
from pdv.services.checkout import CheckoutService
from pdv.ui.dialogs import ManagerAuthDialog, PaymentDialog
from pdv.ui.salon_panel import SalonPanel

_STATUS_LABELS: dict[ScaleStatus, tuple[str, str]] = {
    ScaleStatus.STABLE: ("ESTAVEL", "#1b7f3b"),
    ScaleStatus.UNSTABLE: ("INSTAVEL", "#b8860b"),
    ScaleStatus.OVERLOAD: ("SOBRECARGA", "#b00020"),
    ScaleStatus.NEGATIVE: ("PESO NEGATIVO", "#b00020"),
    ScaleStatus.ZERO: ("VAZIA", "#555555"),
    ScaleStatus.ERROR: ("ERRO", "#b00020"),
}


class CounterWindow(QMainWindow):
    """Janela principal do PDV de balcão."""

    def __init__(
        self,
        checkout: CheckoutService,
        scale: ScaleService,
        printer: PrintService,
        config: AppConfig,
        database: Database,
        *,
        edge_port: int | None = None,
    ) -> None:
        super().__init__()
        self._checkout = checkout
        self._scale = scale
        self._printer = printer
        self._config = config
        self._database = database
        self._edge_port = edge_port
        self._authorization = AuthorizationService(database, config.tenant_id)

        self._weighed: list[Product] = []
        self._unit: list[Product] = []
        self._filtered: list[Product] = []
        self._last_reading: ScaleReading | None = None

        self.setWindowTitle(f"PDV Balcão — {config.store_name}")
        self.resize(1280, 820)

        self._build_ui()
        self._wire_scale()
        self._wire_shortcuts()
        self._load_products()

        # Contador de pendências de sync: informação operacional, não alarme.
        self._sync_timer = QTimer(self)
        self._sync_timer.timeout.connect(self._refresh_sync_badge)
        self._sync_timer.start(5000)

    # -- construção da UI ----------------------------------------------------- #

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)
        layout.addWidget(self._build_left_panel(), stretch=4)
        layout.addWidget(self._build_right_panel(), stretch=6)
        self.setCentralWidget(root)

        self.setStatusBar(QStatusBar())
        self._sync_label = QLabel("Sincronização: —")
        self._connection_label = QLabel("Balança: conectando…")
        self._salon_label = QLabel(
            "Salão: ligado" if self._edge_port else "Salão: desligado"
        )
        self.statusBar().addPermanentWidget(self._connection_label)
        self.statusBar().addPermanentWidget(self._salon_label)
        self.statusBar().addPermanentWidget(self._sync_label)

    def _build_left_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(panel)
        layout.setSpacing(12)

        layout.addWidget(self._section_title("PRODUTO PESÁVEL"))
        self._product_combo = QComboBox()
        self._product_combo.setFont(QFont("Segoe UI", 12))
        self._product_combo.setMinimumHeight(44)
        self._product_combo.currentIndexChanged.connect(self._on_product_changed)
        layout.addWidget(self._product_combo)

        self._price_label = QLabel("R$ 0,00 / kg")
        self._price_label.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        layout.addWidget(self._price_label)

        self._tare_label = QLabel("Tara: 0 g")
        layout.addWidget(self._tare_label)

        layout.addSpacing(12)
        layout.addWidget(self._section_title("BALANÇA"))

        self._weight_label = QLabel("0,000 kg")
        self._weight_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._weight_label.setFont(QFont("Consolas", 52, QFont.Weight.Bold))
        layout.addWidget(self._weight_label)

        self._scale_status_label = QLabel("—")
        self._scale_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._scale_status_label.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        layout.addWidget(self._scale_status_label)

        self._item_total_label = QLabel("Total do item: R$ 0,00")
        self._item_total_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._item_total_label.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        layout.addWidget(self._item_total_label)

        layout.addStretch()

        self._register_button = QPushButton("F2  Registrar item pesado")
        self._register_button.setMinimumHeight(56)
        self._register_button.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        self._register_button.setEnabled(False)
        self._register_button.clicked.connect(self._register_item)
        layout.addWidget(self._register_button)

        return panel

    def _build_right_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(panel)
        layout.setSpacing(12)

        layout.addWidget(self._build_unit_box())
        layout.addWidget(self._section_title("VENDA ATUAL"))

        self._items_table = QTableWidget(0, 5)
        self._items_table.setHorizontalHeaderLabels(
            ["Produto", "Qtd / Peso líq.", "Unitário", "Total", "Baixa estoque"]
        )
        self._items_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._items_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._items_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        layout.addWidget(self._items_table, stretch=1)

        self._discount_label = QLabel("")
        self._discount_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._discount_label.setStyleSheet("color: #b8860b;")
        self._discount_label.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        layout.addWidget(self._discount_label)

        self._total_label = QLabel("TOTAL: R$ 0,00")
        self._total_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._total_label.setFont(QFont("Segoe UI", 30, QFont.Weight.Bold))
        layout.addWidget(self._total_label)

        buttons = QHBoxLayout()
        self._cancel_button = QPushButton("F4  Cancelar item")
        self._cancel_button.setMinimumHeight(52)
        self._cancel_button.clicked.connect(self._cancel_item)
        buttons.addWidget(self._cancel_button)

        self._discount_button = QPushButton("F6  Desconto")
        self._discount_button.setMinimumHeight(52)
        self._discount_button.clicked.connect(self._apply_discount)
        buttons.addWidget(self._discount_button)

        self._salon_button = QPushButton("F8  Salão")
        self._salon_button.setMinimumHeight(52)
        self._salon_button.clicked.connect(self._open_salon)
        buttons.addWidget(self._salon_button)

        self._finish_button = QPushButton("F10  Receber")
        self._finish_button.setMinimumHeight(52)
        self._finish_button.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        self._finish_button.clicked.connect(self._finalize_sale)
        buttons.addWidget(self._finish_button)
        layout.addLayout(buttons)

        return panel

    def _build_unit_box(self) -> QWidget:
        """Lançamento de item unitário — café, fatia, refrigerante.

        A confeitaria vende os dois: o bolo sai por quilo e o café sai por
        unidade. Sem este campo o catálogo tem produtos que não têm como ser
        vendidos, o que empurra o operador para o "lança como outro item" e
        destrói o relatório de mix de produtos.
        """
        box = QFrame()
        box.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(box)
        layout.setSpacing(6)

        layout.addWidget(self._section_title("ITEM UNITÁRIO (F3)"))

        row = QHBoxLayout()
        self._unit_search = QLineEdit()
        self._unit_search.setPlaceholderText("Código ou nome do produto…")
        self._unit_search.setMinimumHeight(40)
        self._unit_search.setFont(QFont("Segoe UI", 12))
        self._unit_search.textChanged.connect(self._filter_unit_products)
        self._unit_search.returnPressed.connect(self._register_unit_item)
        row.addWidget(self._unit_search, stretch=5)

        self._unit_quantity = QDoubleSpinBox()
        self._unit_quantity.setPrefix("x ")
        self._unit_quantity.setDecimals(0)
        self._unit_quantity.setMinimum(1)
        self._unit_quantity.setMaximum(999)
        self._unit_quantity.setValue(1)
        self._unit_quantity.setMinimumHeight(40)
        self._unit_quantity.setFont(QFont("Consolas", 13))
        row.addWidget(self._unit_quantity, stretch=1)

        add = QPushButton("Lançar")
        add.setMinimumHeight(40)
        add.clicked.connect(self._register_unit_item)
        row.addWidget(add, stretch=1)
        layout.addLayout(row)

        self._unit_list = QListWidget()
        self._unit_list.setMaximumHeight(96)
        self._unit_list.itemDoubleClicked.connect(
            lambda _item: self._register_unit_item()
        )
        layout.addWidget(self._unit_list)

        return box

    @staticmethod
    def _section_title(text: str) -> QLabel:
        label = QLabel(text)
        label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        label.setStyleSheet("color: #666;")
        return label

    def _wire_shortcuts(self) -> None:
        QShortcut(QKeySequence("F2"), self, self._register_item)
        QShortcut(QKeySequence("F3"), self, self._focus_unit_search)
        QShortcut(QKeySequence("F4"), self, self._cancel_item)
        QShortcut(QKeySequence("F6"), self, self._apply_discount)
        QShortcut(QKeySequence("F8"), self, self._open_salon)
        QShortcut(QKeySequence("F10"), self, self._finalize_sale)

    def _wire_scale(self) -> None:
        self._scale.reading_received.connect(self._on_reading)
        self._scale.stable_weight.connect(self._on_stable)
        self._scale.weight_changed.connect(self._on_weight_changed)
        self._scale.error_occurred.connect(self._on_scale_error)
        self._scale.connection_changed.connect(self._on_connection_changed)

    # -- dados ---------------------------------------------------------------- #

    def _load_products(self) -> None:
        products = self._checkout.products()
        self._weighed = [p for p in products if p.is_weighed]
        self._unit = [p for p in products if not p.is_weighed]

        self._product_combo.clear()
        for product in self._weighed:
            self._product_combo.addItem(product.name)
        if self._weighed:
            self._on_product_changed(0)

        self._filter_unit_products("")

    @property
    def _selected_product(self) -> Product | None:
        index = self._product_combo.currentIndex()
        if 0 <= index < len(self._weighed):
            return self._weighed[index]
        return None

    @property
    def _selected_unit_product(self) -> Product | None:
        row = self._unit_list.currentRow()
        if 0 <= row < len(self._filtered):
            return self._filtered[row]
        # Uma única correspondência dispensa seleção: quem digitou o código
        # inteiro já escolheu, e obrigar a descer com a seta é atrito na fila.
        if len(self._filtered) == 1:
            return self._filtered[0]
        return None

    @Slot(str)
    def _filter_unit_products(self, text: str) -> None:
        needle = text.strip().lower()
        self._filtered = [
            product
            for product in self._unit
            if not needle
            or needle in product.name.lower()
            or needle in product.sku.lower()
        ]

        self._unit_list.clear()
        for product in self._filtered:
            self._unit_list.addItem(
                QListWidgetItem(
                    f"{product.sku}   {product.name}   "
                    f"R$ {format_cents(product.price_cents)}"
                )
            )
        if len(self._filtered) == 1:
            self._unit_list.setCurrentRow(0)

    # -- reações da balança --------------------------------------------------- #

    @Slot(int)
    def _on_product_changed(self, _index: int) -> None:
        product = self._selected_product
        if product is None:
            return
        self._price_label.setText(f"R$ {format_cents(product.price_cents)} / kg")
        self._tare_label.setText(f"Tara: {product.tare_grams} g")
        self._recalculate_preview()

    @Slot(ScaleReading)
    def _on_reading(self, reading: ScaleReading) -> None:
        self._last_reading = reading
        self._weight_label.setText(format_grams(reading.weight_grams))

        label, color = _STATUS_LABELS.get(reading.status, ("—", "#555"))
        self._scale_status_label.setText(label)
        self._scale_status_label.setStyleSheet(f"color: {color};")
        self._recalculate_preview()

    @Slot(ScaleReading)
    def _on_stable(self, _reading: ScaleReading) -> None:
        """Só aqui o registro é liberado — peso instável cobra o valor errado."""
        self._register_button.setEnabled(True)
        self.statusBar().showMessage("Peso estável — pronto para registrar", 3000)

    @Slot()
    def _on_weight_changed(self) -> None:
        self._register_button.setEnabled(False)

    @Slot(str)
    def _on_scale_error(self, message: str) -> None:
        self._register_button.setEnabled(False)
        self._scale_status_label.setText("ERRO")
        self._scale_status_label.setStyleSheet("color: #b00020;")
        self.statusBar().showMessage(f"Balança: {message}", 8000)

    @Slot(bool)
    def _on_connection_changed(self, connected: bool) -> None:
        self._connection_label.setText(
            "Balança: conectada" if connected else "Balança: DESCONECTADA"
        )

    def _recalculate_preview(self) -> None:
        """Prévia do valor — cálculo idêntico ao que será persistido."""
        product = self._selected_product
        reading = self._last_reading
        if product is None or reading is None:
            return

        from pdv.services.pricing import net_weight, price_for_weight

        try:
            net = net_weight(reading.weight_grams, product.tare_grams)
            total = price_for_weight(product.price_cents, net)
        except PdvError:
            self._item_total_label.setText("Total do item: —")
            return

        self._item_total_label.setText(
            f"Líquido {format_grams(net)}  →  R$ {format_cents(total)}"
        )

    # -- ações ---------------------------------------------------------------- #

    def _register_item(self) -> None:
        product = self._selected_product
        reading = self._scale.last_stable_reading
        if product is None:
            return
        if reading is None:
            QMessageBox.warning(
                self, "Peso instável",
                "Aguarde a balança estabilizar antes de registrar o item.",
            )
            return

        try:
            result = self._checkout.register_weighed_item(
                product=product,
                reading=reading,
                operator_id=EntityId(DEMO_OPERATOR_ID),
            )
        except PdvError as exc:
            QMessageBox.critical(self, "Não foi possível registrar", str(exc))
            return

        self._append_item_row(result.item, result.consumptions)
        self._refresh_total()
        self._register_button.setEnabled(False)

        if result.stock_warnings:
            self.statusBar().showMessage(
                "Estoque: " + " | ".join(result.stock_warnings), 10000
            )

    @Slot()
    def _focus_unit_search(self) -> None:
        self._unit_search.setFocus()
        self._unit_search.selectAll()

    def _register_unit_item(self) -> None:
        product = self._selected_unit_product
        if product is None:
            self.statusBar().showMessage(
                "Selecione o produto unitário antes de lançar", 4000
            )
            return

        try:
            quantity = Decimal(str(int(self._unit_quantity.value())))
        except (InvalidOperation, ValueError):  # pragma: no cover - spin box limita
            return

        try:
            item = self._checkout.register_unit_item(
                product=product,
                quantity=quantity,
                operator_id=EntityId(DEMO_OPERATOR_ID),
            )
        except PdvError as exc:
            QMessageBox.critical(self, "Não foi possível lançar", str(exc))
            return

        self._append_item_row(item, item.consumptions)
        self._refresh_total()

        # Deixar o campo pronto para o próximo item: numa fila, o operador
        # lança três cafés seguidos e não deve precisar limpar nada.
        self._unit_search.clear()
        self._unit_quantity.setValue(1)
        self._unit_search.setFocus()

    def _append_item_row(self, item: SaleItem, consumptions) -> None:  # noqa: ANN001
        row = self._items_table.rowCount()
        self._items_table.insertRow(row)

        write_off = ", ".join(
            f"{c.inventory_item_name.split()[0]} {c.consumed_mg / 1000:.1f}g"
            for c in consumptions
        )
        if item.net_weight_grams:
            measure = format_grams(item.net_weight_grams)
            unit_price = f"{format_cents(item.unit_price_cents)}/kg"
        else:
            measure = f"x {item.quantity}"
            unit_price = format_cents(item.unit_price_cents)

        cells = [
            item.product_name,
            measure,
            unit_price,
            format_cents(item.total_cents),
            write_off,
        ]
        for column, value in enumerate(cells):
            cell = QTableWidgetItem(value)
            if column in (1, 2, 3):
                cell.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
            self._items_table.setItem(row, column, cell)

    def _cancel_item(self) -> None:
        """Cancelamento exige credencial de gerente — vetor de furto nº 1."""
        row = self._items_table.currentRow()
        if row < 0:
            return

        product_name = self._items_table.item(row, 0).text()
        value = self._items_table.item(row, 3).text()

        reason, confirmed = QInputDialog.getText(
            self, "Cancelamento de item",
            "Motivo do cancelamento (registrado na auditoria):",
        )
        if not confirmed or not reason.strip():
            return

        authorizer = ManagerAuthDialog.ask(
            self._authorization,
            operation=f"Cancelar {product_name} — R$ {value}",
            parent=self,
        )
        if authorizer is None:
            return

        try:
            self._checkout.cancel_item(
                index=row,
                operator_id=EntityId(DEMO_OPERATOR_ID),
                authorizer_id=authorizer.id,
                reason=reason.strip(),
            )
        except PdvError as exc:
            QMessageBox.critical(self, "Cancelamento negado", str(exc))
            return

        self._items_table.removeRow(row)
        self._refresh_total()
        self.statusBar().showMessage(
            f"Item cancelado — autorizado por {authorizer.name}", 8000
        )

    def _apply_discount(self) -> None:
        """Desconto percentual, limitado pelo perfil de quem autoriza."""
        sale = self._checkout.current_sale
        if sale is None or not sale.items:
            return

        percent, confirmed = QInputDialog.getDouble(
            self, "Desconto", "Percentual sobre o subtotal:", 0.0, 0.0, 100.0, 2
        )
        if not confirmed or percent <= 0:
            return

        requested = Decimal(str(percent))
        authorizer = ManagerAuthDialog.ask(
            self._authorization,
            operation=(
                f"Desconto de {percent:.2f}% sobre "
                f"R$ {format_cents(sale.subtotal_cents)}"
            ),
            percent=requested,
            parent=self,
        )
        if authorizer is None:
            return

        reason, confirmed = QInputDialog.getText(
            self, "Desconto", "Motivo (registrado na auditoria):"
        )
        if not confirmed or not reason.strip():
            return

        try:
            self._checkout.apply_discount(
                percent=requested,
                operator_id=EntityId(DEMO_OPERATOR_ID),
                authorizer_id=authorizer.id,
                reason=reason.strip(),
            )
        except PdvError as exc:
            QMessageBox.critical(self, "Desconto negado", str(exc))
            return

        self._refresh_total()
        self.statusBar().showMessage(
            f"Desconto autorizado por {authorizer.name}", 8000
        )

    def _open_salon(self) -> None:
        SalonPanel(
            self._database, self._config, port=self._edge_port, parent=self
        ).exec()

    def _finalize_sale(self) -> None:
        sale = self._checkout.current_sale
        if sale is None or not sale.items:
            return

        dialog = PaymentDialog(sale.total_cents, parent=self)
        if dialog.exec() != PaymentDialog.DialogCode.Accepted:
            return

        total = sale.total_cents
        local_number = sale.local_number

        try:
            receipt = self._checkout.finalize_sale(
                payments=dialog.payments,
                operator_id=EntityId(DEMO_OPERATOR_ID),
                operator_name=DEMO_OPERATOR_NAME,
            )
        except PdvError as exc:
            QMessageBox.critical(self, "Não foi possível finalizar", str(exc))
            return

        # A venda já está confirmada no banco. A impressão é assíncrona: papel
        # acabado não pode desfazer uma transação concluída.
        self._printer.submit(receipt, job_name=f"Venda {local_number:06d}")

        self._items_table.setRowCount(0)
        self._refresh_total()
        self.statusBar().showMessage(
            f"Venda {local_number:06d} finalizada — R$ {format_cents(total)}", 6000
        )

    def _refresh_total(self) -> None:
        sale = self._checkout.current_sale
        total = sale.total_cents if sale else Cents(0)
        discount = sale.discount_cents if sale else Cents(0)

        if int(discount) > 0 and sale is not None:
            self._discount_label.setText(
                f"Subtotal R$ {format_cents(sale.subtotal_cents)}   "
                f"Desconto −R$ {format_cents(discount)}"
            )
        else:
            self._discount_label.setText("")

        self._total_label.setText(f"TOTAL: R$ {format_cents(total)}")

    @Slot()
    def _refresh_sync_badge(self) -> None:
        pending = self._checkout.pending_sync_count()
        self._sync_label.setText(
            "Sincronização: em dia" if pending == 0 else f"Sincronização: {pending} pendente(s)"
        )

    # -- encerramento --------------------------------------------------------- #

    def closeEvent(self, event) -> None:  # noqa: N802 - override do Qt
        self._scale.shutdown()
        self._printer.shutdown()
        super().closeEvent(event)


__all__ = ["CounterWindow", "Decimal"]
