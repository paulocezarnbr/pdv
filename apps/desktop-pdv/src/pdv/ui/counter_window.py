"""Tela do Caixa de Balcão.

Princípios de UI de PDV que o layout respeita:

* **Teclado acima do mouse.** O operador não tira a mão do teclado numa fila.
  F2 registra, F4 cancela item, F10 finaliza, ESC limpa.
* **O peso é o maior elemento da tela.** É o número que o cliente confere de pé
  do outro lado do balcão.
* **Estado de conexão sempre visível.** O operador precisa saber que está
  offline — não para se preocupar, mas para não estranhar o relatório da nuvem.
* **A UI nunca calcula dinheiro.** Ela exibe o que o `CheckoutService` decidiu.
  Regra de negócio em widget é dívida técnica que vaza para o financeiro.
"""

from __future__ import annotations

from decimal import Decimal

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
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
from pdv.data.seed import DEMO_MANAGER_ID, DEMO_OPERATOR_ID, DEMO_OPERATOR_NAME
from pdv.domain.errors import PdvError
from pdv.domain.models import (
    Cents,
    EntityId,
    Payment,
    PaymentMethod,
    Product,
    ScaleReading,
    ScaleStatus,
)
from pdv.hardware.printer.backends import PrintService
from pdv.hardware.printer.escpos import format_cents, format_grams
from pdv.hardware.scale.worker import ScaleService
from pdv.services.checkout import CheckoutService

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
    ) -> None:
        super().__init__()
        self._checkout = checkout
        self._scale = scale
        self._printer = printer
        self._config = config
        self._products: list[Product] = []
        self._last_reading: ScaleReading | None = None

        self.setWindowTitle(f"PDV Balcão — {config.store_name}")
        self.resize(1180, 760)

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
        self.statusBar().addPermanentWidget(self._connection_label)
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

        self._register_button = QPushButton("F2  Registrar item")
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

        layout.addWidget(self._section_title("VENDA ATUAL"))

        self._items_table = QTableWidget(0, 5)
        self._items_table.setHorizontalHeaderLabels(
            ["Produto", "Peso líq.", "R$/kg", "Total", "Baixa estoque"]
        )
        self._items_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._items_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._items_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        layout.addWidget(self._items_table, stretch=1)

        self._total_label = QLabel("TOTAL: R$ 0,00")
        self._total_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._total_label.setFont(QFont("Segoe UI", 30, QFont.Weight.Bold))
        layout.addWidget(self._total_label)

        buttons = QHBoxLayout()
        self._cancel_button = QPushButton("F4  Cancelar item")
        self._cancel_button.setMinimumHeight(52)
        self._cancel_button.clicked.connect(self._cancel_item)
        buttons.addWidget(self._cancel_button)

        self._finish_button = QPushButton("F10  Finalizar (dinheiro)")
        self._finish_button.setMinimumHeight(52)
        self._finish_button.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        self._finish_button.clicked.connect(self._finalize_sale)
        buttons.addWidget(self._finish_button)
        layout.addLayout(buttons)

        return panel

    @staticmethod
    def _section_title(text: str) -> QLabel:
        label = QLabel(text)
        label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        label.setStyleSheet("color: #666;")
        return label

    def _wire_shortcuts(self) -> None:
        QShortcut(QKeySequence("F2"), self, self._register_item)
        QShortcut(QKeySequence("F4"), self, self._cancel_item)
        QShortcut(QKeySequence("F10"), self, self._finalize_sale)

    def _wire_scale(self) -> None:
        self._scale.reading_received.connect(self._on_reading)
        self._scale.stable_weight.connect(self._on_stable)
        self._scale.weight_changed.connect(self._on_weight_changed)
        self._scale.error_occurred.connect(self._on_scale_error)
        self._scale.connection_changed.connect(self._on_connection_changed)

    # -- dados ---------------------------------------------------------------- #

    def _load_products(self) -> None:
        self._products = [p for p in self._checkout.products() if p.is_weighed]
        self._product_combo.clear()
        for product in self._products:
            self._product_combo.addItem(product.name)
        if self._products:
            self._on_product_changed(0)

    @property
    def _selected_product(self) -> Product | None:
        index = self._product_combo.currentIndex()
        if 0 <= index < len(self._products):
            return self._products[index]
        return None

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

        self._append_item_row(result)
        self._refresh_total()
        self._register_button.setEnabled(False)

        if result.stock_warnings:
            self.statusBar().showMessage(
                "Estoque: " + " | ".join(result.stock_warnings), 10000
            )

    def _append_item_row(self, result) -> None:  # noqa: ANN001 - WeighedItemResult
        item = result.item
        row = self._items_table.rowCount()
        self._items_table.insertRow(row)

        write_off = ", ".join(
            f"{c.inventory_item_name.split()[0]} {c.consumed_mg / 1000:.1f}g"
            for c in result.consumptions
        )
        cells = [
            item.product_name,
            format_grams(item.net_weight_grams),
            format_cents(item.unit_price_cents),
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
        """Cancelamento exige senha de gerente — vetor de furto nº 1 em PDV."""
        row = self._items_table.currentRow()
        if row < 0:
            return

        reason, confirmed = QInputDialog.getText(
            self, "Cancelamento de item",
            "Motivo do cancelamento (registrado na auditoria):",
        )
        if not confirmed or not reason.strip():
            return

        # Em produção: diálogo de credencial validando Argon2id contra
        # `users.password_hash` replicado — funciona offline.
        password, confirmed = QInputDialog.getText(
            self, "Autorização de gerente", "Senha do gerente:",
        )
        if not confirmed or not password:
            return

        try:
            self._checkout.cancel_item(
                index=row,
                operator_id=EntityId(DEMO_OPERATOR_ID),
                authorizer_id=EntityId(DEMO_MANAGER_ID),
                reason=reason.strip(),
            )
        except PdvError as exc:
            QMessageBox.critical(self, "Cancelamento negado", str(exc))
            return

        self._items_table.removeRow(row)
        self._refresh_total()

    def _finalize_sale(self) -> None:
        sale = self._checkout.current_sale
        if sale is None or not sale.items:
            return

        total = sale.total_cents
        payment = Payment(
            method=PaymentMethod.CASH,
            amount_cents=Cents(int(total)),
            change_cents=Cents(0),
        )

        try:
            receipt = self._checkout.finalize_sale(
                payments=(payment,),
                operator_id=EntityId(DEMO_OPERATOR_ID),
                operator_name=DEMO_OPERATOR_NAME,
            )
        except PdvError as exc:
            QMessageBox.critical(self, "Não foi possível finalizar", str(exc))
            return

        # A venda já está confirmada no banco. A impressão é assíncrona: papel
        # acabado não pode desfazer uma transação concluída.
        self._printer.submit(receipt, job_name=f"Venda {sale.local_number:06d}")

        self._items_table.setRowCount(0)
        self._refresh_total()
        self.statusBar().showMessage(
            f"Venda {sale.local_number:06d} finalizada — R$ {format_cents(total)}", 6000
        )

    def _refresh_total(self) -> None:
        sale = self._checkout.current_sale
        total = sale.total_cents if sale else Cents(0)
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
