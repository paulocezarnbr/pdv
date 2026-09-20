"""Tela do Caixa de Balcão.

Princípios de UI de PDV que o layout respeita:

* **Teclado acima do mouse.** O operador não tira a mão do teclado numa fila.
  F2 registra o pesado, F3 lança o unitário, F4 cancela item, F6 desconta,
  F7 configura cashback, F8 abre o salão, F9 as mesas, F10 finaliza,
  F11 carrega crédito pré-pago e F12 fecha o caixa.
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
from PySide6.QtGui import QColor, QKeySequence, QShortcut
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
    QStackedWidget,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pdv.config import AppConfig
from pdv.data.database import Database
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
from pdv.remote.inbox import InboxRepository
from pdv.services.authorization import AuthorizationService, Identity
from pdv.services.checkout import CheckoutService
from pdv.services.cash_session import CashSessionError, CashSessionService
from pdv.services.cashback import CashbackError, CashbackService, Customer
from pdv.services.prepaid import PrepaidError, PrepaidService
from pdv.services.credit_account import CreditAccountError, CreditAccountService
from pdv.services.discount_tiers import DiscountTierError, DiscountTierService
from pdv.ui import theme
from pdv.ui.dialogs import ManagerAuthDialog, PaymentDialog
from pdv.ui.salon_panel import SalonPanel
from pdv.ui.tables_dialog import TablesDialog

#: Rótulo e cor de cada estado da balança. As três cores semânticas do tema
#: vivem aqui e **só** aqui: é o que permite ao operador ler o estado pela cor,
#: de longe, sem processar a palavra.
_STATUS_LABELS: dict[ScaleStatus, tuple[str, str]] = {
    ScaleStatus.STABLE: ("ESTÁVEL", theme.OK),
    ScaleStatus.UNSTABLE: ("INSTÁVEL", theme.WARN),
    ScaleStatus.OVERLOAD: ("SOBRECARGA", theme.DANGER),
    ScaleStatus.NEGATIVE: ("PESO NEGATIVO", theme.DANGER),
    ScaleStatus.ZERO: ("VAZIA", theme.TEXT_FAINT),
    ScaleStatus.ERROR: ("ERRO", theme.DANGER),
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
        operator: Identity,
        cash_sessions: CashSessionService | None = None,
        edge_port: int | None = None,
        edge_scheme: str = "http",
        edge_tls=None,  # noqa: ANN001 - TlsMaterial | None
    ) -> None:
        super().__init__()
        self._operator = operator
        self._cash_sessions = cash_sessions
        self._checkout = checkout
        self._scale = scale
        self._printer = printer
        self._config = config
        self._database = database
        self._edge_port = edge_port
        self._edge_scheme = edge_scheme
        self._edge_tls = edge_tls
        self._authorization = AuthorizationService(database, config.tenant_id)
        self._cashback = CashbackService(database, config)
        self._prepaid = PrepaidService(database, config)
        self._credit_account = CreditAccountService(database, config)
        self._discount_tiers = DiscountTierService(database, config)
        self._operator_id = EntityId(str(operator.id))

        self._weighed: list[Product] = []
        self._unit: list[Product] = []
        self._filtered: list[Product] = []
        self._last_reading: ScaleReading | None = None

        self.setWindowTitle(
            f"PDV Balcão — {config.store_name} — {operator.name}"
        )
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
        layout.setContentsMargins(
            theme.SPACE_4, theme.SPACE_4, theme.SPACE_4, theme.SPACE_4
        )
        layout.setSpacing(theme.SPACE_3)
        layout.addWidget(self._build_left_panel(), stretch=4)
        layout.addWidget(self._build_right_panel(), stretch=6)
        self.setCentralWidget(root)

        self.setStatusBar(QStatusBar())
        # Quem está no caixa fica à vista o tempo todo. Numa troca de turno
        # sem isso, o operador que assume vende no nome de quem saiu — e a
        # trilha de auditoria passa a apontar para a pessoa errada.
        self._operator_label = QLabel(f"Caixa: {self._operator.first_name}")
        self._cash_label = QLabel("Sessão: aberta" if self._cash_sessions else "")
        self._sync_label = QLabel("Sincronização: —")
        self._connection_label = QLabel("Balança: conectando…")
        self._salon_label = QLabel(
            "Salão: ligado" if self._edge_port else "Salão: desligado"
        )
        # Só aparece quando há o que aplicar. Um selo permanente dizendo "0
        # comandos" gastaria atenção do operador todo dia por um evento que
        # acontece uma vez por semana.
        self._command_label = QLabel("")
        self._command_label.setStyleSheet(f"color: {theme.WARN};")
        self._command_label.setVisible(False)
        for label in (
            self._operator_label,
            self._cash_label,
            self._connection_label,
            self._salon_label,
            self._sync_label,
            self._command_label,
        ):
            label.setFont(theme.font(theme.SIZE_MICRO))
        self.statusBar().addPermanentWidget(self._operator_label)
        self.statusBar().addPermanentWidget(self._cash_label)
        self.statusBar().addPermanentWidget(self._connection_label)
        self.statusBar().addPermanentWidget(self._salon_label)
        self.statusBar().addPermanentWidget(self._sync_label)
        self.statusBar().addPermanentWidget(self._command_label)

    def _build_left_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(
            theme.SPACE_4, theme.SPACE_4, theme.SPACE_4, theme.SPACE_4
        )
        layout.setSpacing(theme.SPACE_2)

        layout.addWidget(self._section_title("PRODUTO PESÁVEL"))
        self._product_combo = QComboBox()
        self._product_combo.setFont(theme.font(theme.SIZE_BODY_LG))
        self._product_combo.setMinimumHeight(44)
        self._product_combo.currentIndexChanged.connect(self._on_product_changed)
        layout.addWidget(self._product_combo)

        price_row = QHBoxLayout()
        price_row.setSpacing(theme.SPACE_2)
        self._price_label = QLabel("R$ 0,00 / kg")
        self._price_label.setFont(
            theme.font(theme.SIZE_TITLE, theme.WEIGHT_SEMIBOLD, display=True)
        )
        price_row.addWidget(self._price_label)
        price_row.addStretch()

        self._tare_label = QLabel("Tara: 0 g")
        self._tare_label.setObjectName("hint")
        self._tare_label.setFont(theme.font(theme.SIZE_BODY))
        price_row.addWidget(self._tare_label)
        layout.addLayout(price_row)

        layout.addSpacing(theme.SPACE_4)
        layout.addWidget(self._section_title("BALANÇA"))

        # O peso é o único elemento que o cliente lê do outro lado do balcão.
        # Corte *display* com algarismos tabulares, não monoespaçada: o que se
        # precisa garantir é que o número não dance na tela enquanto a balança
        # oscila, e `tnum` já dá isso. A monoespaçada daria o mesmo e ainda
        # abriria um vão em volta da vírgula do tamanho de um dígito.
        self._weight_label = QLabel("0,000 kg")
        self._weight_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._weight_label.setFont(
            theme.font(
                theme.SIZE_DISPLAY, theme.WEIGHT_BOLD, display=True, tracking=-2.0
            )
        )
        layout.addWidget(self._weight_label)

        self._scale_status_label = QLabel("—")
        self._scale_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._scale_status_label.setFont(
            theme.font(theme.SIZE_LABEL, theme.WEIGHT_BOLD, tracking=2.0)
        )
        layout.addWidget(self._scale_status_label)

        layout.addSpacing(theme.SPACE_2)
        self._item_total_label = QLabel("Total do item: R$ 0,00")
        self._item_total_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._item_total_label.setFont(
            theme.font(theme.SIZE_TITLE, theme.WEIGHT_MEDIUM)
        )
        layout.addWidget(self._item_total_label)

        layout.addStretch()

        self._register_button = QPushButton("F2   Registrar item pesado")
        self._register_button.setObjectName("primary")
        self._register_button.setMinimumHeight(58)
        self._register_button.setFont(
            theme.font(theme.SIZE_BODY_LG, theme.WEIGHT_SEMIBOLD)
        )
        self._register_button.setEnabled(False)
        self._register_button.clicked.connect(self._register_item)
        layout.addWidget(self._register_button)

        return panel

    def _build_right_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(
            theme.SPACE_4, theme.SPACE_4, theme.SPACE_4, theme.SPACE_4
        )
        layout.setSpacing(theme.SPACE_3)

        layout.addWidget(self._build_unit_box())
        layout.addWidget(self._section_title("VENDA ATUAL"))
        layout.addWidget(self._build_items_view(), stretch=1)

        self._discount_label = QLabel("")
        self._discount_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._discount_label.setStyleSheet(f"color: {theme.WARN};")
        self._discount_label.setFont(
            theme.font(theme.SIZE_BODY_LG, theme.WEIGHT_MEDIUM)
        )
        layout.addWidget(self._discount_label)

        self._total_label = QLabel("TOTAL: R$ 0,00")
        self._total_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._total_label.setFont(
            theme.font(
                theme.SIZE_TOTAL, theme.WEIGHT_BOLD, display=True, tracking=-1.0
            )
        )
        layout.addWidget(self._total_label)

        # Uma ação principal e três secundárias. Se os quatro botões tivessem o
        # mesmo peso visual, o operador procuraria o "Receber" pelo texto em vez
        # de pela posição e pela cor — e é o botão que ele mais usa no dia.
        buttons = QHBoxLayout()
        buttons.setSpacing(theme.SPACE_2)
        self._cancel_button = _secondary_button("F4   Cancelar item", self._cancel_item)
        buttons.addWidget(self._cancel_button)

        self._discount_button = _secondary_button("F6   Desconto", self._apply_discount)
        buttons.addWidget(self._discount_button)

        self._salon_button = _secondary_button("F8   Salão", self._open_salon)
        buttons.addWidget(self._salon_button)

        self._tables_button = _secondary_button("F9   Mesas", self._open_tables)
        buttons.addWidget(self._tables_button)

        self._finish_button = QPushButton("F10   Receber")
        self._finish_button.setObjectName("primary")
        self._finish_button.setMinimumHeight(54)
        self._finish_button.setFont(
            theme.font(theme.SIZE_BODY_LG, theme.WEIGHT_SEMIBOLD)
        )
        self._finish_button.clicked.connect(self._finalize_sale)
        buttons.addWidget(self._finish_button, stretch=1)
        layout.addLayout(buttons)

        return panel

    def _build_items_view(self) -> QWidget:
        """Tabela da venda e o estado vazio que a substitui.

        Um retângulo vazio com cabeçalho de coluna não diz ao operador que está
        tudo certo — diz que algo não carregou. A tela sem itens é o estado mais
        frequente do dia (é como o caixa fica entre uma venda e a seguinte),
        então ela merece ser desenhada, não deixada em branco.
        """
        self._items_stack = QStackedWidget()

        empty = QWidget()
        empty_layout = QVBoxLayout(empty)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.setSpacing(theme.SPACE_2)

        headline = QLabel("Nenhum item na venda")
        headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        headline.setFont(theme.font(theme.SIZE_TITLE, theme.WEIGHT_MEDIUM))
        headline.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        empty_layout.addWidget(headline)

        hint = QLabel(
            "F2 registra o que está na balança   ·   F3 lança item unitário"
        )
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setObjectName("hint")
        hint.setFont(theme.font(theme.SIZE_BODY))
        empty_layout.addWidget(hint)

        self._items_table = QTableWidget(0, 5)
        self._items_table.setHorizontalHeaderLabels(
            ["Produto", "Qtd / Peso líq.", "Unitário", "Total", "Baixa estoque"]
        )
        header = self._items_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setFont(theme.font(theme.SIZE_MICRO, theme.WEIGHT_MEDIUM, tracking=0.6))
        self._items_table.verticalHeader().setVisible(False)
        self._items_table.setAlternatingRowColors(True)
        self._items_table.setShowGrid(False)
        self._items_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._items_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._items_table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )

        self._items_stack.addWidget(empty)
        self._items_stack.addWidget(self._items_table)
        return self._items_stack

    def _build_unit_box(self) -> QWidget:
        """Lançamento de item unitário — café, fatia, refrigerante.

        A confeitaria vende os dois: o bolo sai por quilo e o café sai por
        unidade. Sem este campo o catálogo tem produtos que não têm como ser
        vendidos, o que empurra o operador para o "lança como outro item" e
        destrói o relatório de mix de produtos.
        """
        box = QFrame()
        box.setObjectName("inset")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(
            theme.SPACE_3, theme.SPACE_3, theme.SPACE_3, theme.SPACE_3
        )
        layout.setSpacing(theme.SPACE_2)

        layout.addWidget(self._section_title("ITEM UNITÁRIO   ·   F3"))

        row = QHBoxLayout()
        row.setSpacing(theme.SPACE_2)
        self._unit_search = QLineEdit()
        self._unit_search.setPlaceholderText("Código ou nome do produto…")
        self._unit_search.setMinimumHeight(40)
        self._unit_search.setFont(theme.font(theme.SIZE_BODY_LG))
        self._unit_search.textChanged.connect(self._filter_unit_products)
        self._unit_search.returnPressed.connect(self._register_unit_item)
        row.addWidget(self._unit_search, stretch=5)

        self._unit_quantity = QDoubleSpinBox()
        self._unit_quantity.setPrefix("× ")
        self._unit_quantity.setDecimals(0)
        self._unit_quantity.setMinimum(1)
        self._unit_quantity.setMaximum(999)
        self._unit_quantity.setValue(1)
        self._unit_quantity.setMinimumHeight(40)
        self._unit_quantity.setFont(
            theme.font(theme.SIZE_BODY_LG, theme.WEIGHT_MEDIUM, mono=True)
        )
        row.addWidget(self._unit_quantity, stretch=1)

        add = QPushButton("Lançar")
        add.setMinimumHeight(40)
        add.setFont(theme.font(theme.SIZE_BODY, theme.WEIGHT_MEDIUM))
        add.clicked.connect(self._register_unit_item)
        row.addWidget(add, stretch=1)
        layout.addLayout(row)

        self._unit_list = QListWidget()
        self._unit_list.setMaximumHeight(104)
        self._unit_list.setFont(theme.font(theme.SIZE_BODY, mono=True))
        self._unit_list.itemDoubleClicked.connect(
            lambda _item: self._register_unit_item()
        )
        layout.addWidget(self._unit_list)

        return box

    @staticmethod
    def _section_title(text: str) -> QLabel:
        """Rótulo de seção: pequeno, caixa alta, muito espaçado.

        Caixa alta sem tracking vira um bloco cinza que o olho pula. Com o
        espaçamento aberto ela cumpre o papel de placa de corredor — some
        quando não é procurada, e é achada na hora em que é.
        """
        label = QLabel(text)
        label.setObjectName("sectionTitle")
        label.setFont(
            theme.font(theme.SIZE_MICRO, theme.WEIGHT_SEMIBOLD, tracking=1.6)
        )
        return label

    def _wire_shortcuts(self) -> None:
        QShortcut(QKeySequence("F2"), self, self._register_item)
        QShortcut(QKeySequence("F3"), self, self._focus_unit_search)
        QShortcut(QKeySequence("F4"), self, self._cancel_item)
        QShortcut(QKeySequence("F5"), self, self._manage_credit_account)
        QShortcut(QKeySequence("F6"), self, self._apply_discount)
        QShortcut(QKeySequence("Ctrl+F6"), self, self._manage_discount_tiers)
        QShortcut(QKeySequence("F7"), self, self._configure_cashback)
        QShortcut(QKeySequence("F8"), self, self._open_salon)
        QShortcut(QKeySequence("F9"), self, self._open_tables)
        QShortcut(QKeySequence("F10"), self, self._finalize_sale)
        QShortcut(QKeySequence("F11"), self, self._deposit_prepaid)
        QShortcut(QKeySequence("F12"), self, self._close_cash_session)

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
            # SKU em largura fixa e preço alinhado à direita: a lista é lida de
            # relance, e coluna que serpenteia obriga a ler palavra por palavra.
            entry = QListWidgetItem(
                f"{product.sku:<14}{product.name:<40}"
                f"{'R$ ' + format_cents(product.price_cents):>12}"
            )
            self._unit_list.addItem(entry)
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
                operator_id=self._operator_id,
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
                operator_id=self._operator_id,
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
        numeric = theme.font(theme.SIZE_BODY, mono=True)
        emphasis = theme.font(theme.SIZE_BODY, theme.WEIGHT_SEMIBOLD, mono=True)
        for column, value in enumerate(cells):
            cell = QTableWidgetItem(value)
            if column in (1, 2, 3):
                cell.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                # Coluna de dinheiro em fonte proporcional não alinha na
                # vírgula, e conferir a venda vira leitura dígito a dígito.
                cell.setFont(emphasis if column == 3 else numeric)
            elif column == 4:
                cell.setFont(numeric)
                cell.setForeground(QColor(theme.TEXT_FAINT))
            else:
                cell.setFont(theme.font(theme.SIZE_BODY, theme.WEIGHT_MEDIUM))
            self._items_table.setItem(row, column, cell)

        self._refresh_items_view()

    def _refresh_items_view(self) -> None:
        """Alterna entre o estado vazio e a tabela."""
        self._items_stack.setCurrentIndex(
            1 if self._items_table.rowCount() else 0
        )

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
                operator_id=self._operator_id,
                authorizer_id=authorizer.id,
                reason=reason.strip(),
            )
        except PdvError as exc:
            QMessageBox.critical(self, "Cancelamento negado", str(exc))
            return

        self._items_table.removeRow(row)
        self._refresh_items_view()
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
                operator_id=self._operator_id,
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
            self._database,
            self._config,
            port=self._edge_port,
            scheme=self._edge_scheme,
            tls=self._edge_tls,
            parent=self,
        ).exec()

    def _configure_cashback(self) -> None:
        authorizer = ManagerAuthDialog.ask(
            self._authorization,
            operation="Configurar a regra de cashback desta loja.",
            parent=self,
        )
        if authorizer is None:
            return
        percent, accepted = QInputDialog.getDouble(
            self, "Cashback", "Percentual sobre a venda:", 5.0, 0.0, 100.0, 2
        )
        if not accepted:
            return
        cap, accepted = QInputDialog.getDouble(
            self, "Cashback", "Teto por venda (R$; zero = sem teto):",
            10.0, 0.0, 999_999.99, 2,
        )
        if not accepted:
            return
        days, accepted = QInputDialog.getInt(
            self, "Cashback", "Validade do crédito em dias:", 30, 1, 3650
        )
        if not accepted:
            return
        try:
            self._cashback.configure(
                percent=Decimal(str(percent)),
                max_per_sale_cents=Cents(
                    int((Decimal(str(cap)) * Decimal(100)).quantize(Decimal("1")))
                ),
                validity_days=days,
                actor_user_id=authorizer.id,
            )
        except CashbackError as exc:
            QMessageBox.critical(self, "Cashback", str(exc))
            return
        self.statusBar().showMessage(
            f"Cashback de {percent:.2f}% configurado por {authorizer.name}", 8000
        )

    def _manage_discount_tiers(self) -> None:
        authorizer = ManagerAuthDialog.ask(
            self._authorization,
            operation="Configurar ou atribuir níveis automáticos de desconto.",
            parent=self,
        )
        if authorizer is None:
            return
        action, accepted = QInputDialog.getItem(
            self, "Níveis de desconto", "Operação:",
            ("Configurar nível", "Atribuir nível ao cliente"), 0, False,
        )
        if not accepted:
            return
        if action == "Configurar nível":
            labels = {
                "Bronze": "bronze", "Prata": "silver", "Ouro": "gold",
                "Diamante": "diamond", "Funcionário": "employee", "Dono": "owner",
            }
            label, accepted = QInputDialog.getItem(
                self, "Níveis de desconto", "Nível:", tuple(labels), 0, False
            )
            if not accepted:
                return
            percent, accepted = QInputDialog.getDouble(
                self, "Níveis de desconto", "Percentual:", 10.0, 0.0, 100.0, 2
            )
            if not accepted:
                return
            if labels[label] == "owner":
                requires = True
                QMessageBox.information(
                    self, "Nível Dono",
                    "O nível Dono sempre exige login e PIN de gerente em cada uso.",
                )
            else:
                requires = QMessageBox.question(
                    self, "Níveis de desconto", "Exigir gerente a cada aplicação?"
                ) == QMessageBox.StandardButton.Yes
            try:
                self._discount_tiers.configure(
                    code=labels[label], name=label, percent=Decimal(str(percent)),
                    priority=0, requires_manager=requires, actor_user_id=authorizer.id,
                )
            except DiscountTierError as exc:
                QMessageBox.critical(self, "Níveis de desconto", str(exc))
                return
            self.statusBar().showMessage(f"Nível {label} configurado", 6000)
            return
        customer = self._select_customer()
        if not isinstance(customer, Customer):
            return
        tiers = self._discount_tiers.list_active()
        if not tiers:
            QMessageBox.information(self, "Níveis de desconto", "Configure um nível primeiro.")
            return
        names = [f"{tier.name} — {tier.percent_basis_points / 100:.2f}%" for tier in tiers]
        selected, accepted = QInputDialog.getItem(
            self, "Níveis de desconto", "Nível do cliente:", names, 0, False
        )
        if not accepted:
            return
        tier = tiers[names.index(selected)]
        try:
            self._discount_tiers.assign(
                customer_id=customer.id, tier_id=tier.id, actor_user_id=authorizer.id
            )
        except DiscountTierError as exc:
            QMessageBox.critical(self, "Níveis de desconto", str(exc))
            return
        self.statusBar().showMessage(f"{customer.name}: nível {tier.name}", 6000)

    def _open_tables(self) -> None:
        """O salão inteiro, com busca, e o recebimento da conta.

        Tecla própria, separada do painel do salão (F8): são dois trabalhos
        diferentes. O painel é do gerente — parear aparelho, destravar a
        cozinha, ver quem está em turno. Esta tela é do caixa, e é onde a
        mesa vira dinheiro.
        """
        TablesDialog(
            self._database,
            self._config,
            operator=self._operator,
            on_receipt=self._printer.submit,
            parent=self,
        ).exec()

    def _finalize_sale(self) -> None:
        sale = self._checkout.current_sale
        if sale is None or not sale.items:
            return

        customer = self._select_customer()
        if customer is False:
            return

        if isinstance(customer, Customer):
            tier = self._discount_tiers.for_customer(customer.id)
            if tier is not None:
                tier_authorizer = None
                if tier.requires_manager:
                    tier_authorizer = ManagerAuthDialog.ask(
                        self._authorization,
                        operation=f"Autorizar nível {tier.name} para {customer.name}.",
                        parent=self,
                    )
                    if tier_authorizer is None:
                        return
                self._checkout.apply_customer_tier(
                    tier=tier, operator_id=self._operator_id,
                    authorizer_id=tier_authorizer.id if tier_authorizer else None,
                )
                sale = self._checkout.current_sale
                self._refresh_total()

        prepaid_balance = (
            self._prepaid.balance(customer.id)
            if isinstance(customer, Customer) else Cents(0)
        )
        credit_available = (
            self._credit_account.position(customer.id).available_cents
            if isinstance(customer, Customer) else Cents(0)
        )
        dialog = PaymentDialog(
            sale.total_cents, parent=self, prepaid_balance_cents=prepaid_balance,
            credit_available_cents=credit_available,
        )
        if dialog.exec() != PaymentDialog.DialogCode.Accepted:
            return

        total = sale.total_cents
        local_number = sale.local_number

        try:
            receipt = self._checkout.finalize_sale(
                payments=dialog.payments,
                operator_id=self._operator_id,
                operator_name=self._operator.name,
                customer_id=customer.id if isinstance(customer, Customer) else None,
                customer_name=customer.name if isinstance(customer, Customer) else None,
            )
        except PdvError as exc:
            QMessageBox.critical(self, "Não foi possível finalizar", str(exc))
            return

        # A venda já está confirmada no banco. A impressão é assíncrona: papel
        # acabado não pode desfazer uma transação concluída.
        self._printer.submit(receipt, job_name=f"Venda {local_number:06d}")

        self._items_table.setRowCount(0)
        self._refresh_items_view()
        self._refresh_total()
        self.statusBar().showMessage(
            f"Venda {local_number:06d} finalizada — R$ {format_cents(total)}", 6000
        )

    def _select_customer(self) -> Customer | None | bool:
        """Identifica o cliente; `False` significa que o operador cancelou."""
        phone, accepted = QInputDialog.getText(
            self,
            "Cliente e cashback",
            "Telefone do cliente (deixe vazio para venda sem cashback):",
        )
        if not accepted:
            return False
        if not phone.strip():
            return None
        existing = self._cashback.find_customer_by_phone(phone)
        if existing is not None:
            balance = self._cashback.balance(existing.id)
            self.statusBar().showMessage(
                f"Cliente {existing.name} — cashback R$ {format_cents(balance)}", 5000
            )
            return existing
        name, accepted = QInputDialog.getText(
            self, "Novo cliente", "Nome do cliente:"
        )
        if not accepted:
            return False
        try:
            customer_id = self._cashback.create_customer(name=name, phone=phone)
        except CashbackError as exc:
            QMessageBox.critical(self, "Cliente", str(exc))
            return False
        return Customer(customer_id, name.strip(), phone.strip())

    def _deposit_prepaid(self) -> None:
        customer = self._select_customer()
        if not isinstance(customer, Customer):
            return
        amount, accepted = QInputDialog.getDouble(
            self, "Crédito pré-pago", "Valor da carga (R$):",
            50.0, 0.01, 999_999.99, 2,
        )
        if not accepted:
            return
        authorizer = ManagerAuthDialog.ask(
            self._authorization,
            operation=f"Autorizar carga pré-paga para {customer.name}.",
            parent=self,
        )
        if authorizer is None:
            return
        cents = Cents(int((Decimal(str(amount)) * Decimal(100)).quantize(Decimal("1"))))
        try:
            balance = self._prepaid.deposit(
                customer_id=customer.id, amount_cents=cents,
                actor_user_id=self._operator_id, authorizer_user_id=authorizer.id,
            )
        except PrepaidError as exc:
            QMessageBox.critical(self, "Crédito pré-pago", str(exc))
            return
        QMessageBox.information(
            self, "Crédito pré-pago",
            f"Carga concluída. Saldo de {customer.name}: R$ {format_cents(balance)}",
        )

    def _manage_credit_account(self) -> None:
        customer = self._select_customer()
        if not isinstance(customer, Customer):
            return
        action, accepted = QInputDialog.getItem(
            self, "Fiado/Pendura", "Operação:",
            ("Configurar limite", "Receber pagamento"), 0, False,
        )
        if not accepted:
            return
        if action == "Configurar limite":
            authorizer = ManagerAuthDialog.ask(
                self._authorization,
                operation=f"Configurar limite de fiado para {customer.name}.",
                parent=self,
            )
            if authorizer is None:
                return
            limit, accepted = QInputDialog.getDouble(
                self, "Fiado/Pendura", "Limite de crédito (R$):",
                200.0, 0.0, 999_999.99, 2,
            )
            if not accepted:
                return
            days, accepted = QInputDialog.getInt(
                self, "Fiado/Pendura", "Prazo para pagamento (dias):", 30, 1, 365
            )
            if not accepted:
                return
            try:
                position = self._credit_account.configure(
                    customer_id=customer.id,
                    limit_cents=Cents(int((Decimal(str(limit))*100).quantize(Decimal("1")))),
                    due_days=days, actor_user_id=self._operator_id,
                    authorizer_user_id=authorizer.id,
                )
            except CreditAccountError as exc:
                QMessageBox.critical(self, "Fiado/Pendura", str(exc))
                return
        else:
            before = self._credit_account.position(customer.id)
            amount, accepted = QInputDialog.getDouble(
                self, "Fiado/Pendura",
                f"Dívida atual R$ {format_cents(before.outstanding_cents)}. Receber (R$):",
                0.0, 0.01, 999_999.99, 2,
            )
            if not accepted:
                return
            try:
                position = self._credit_account.pay(
                    customer_id=customer.id,
                    amount_cents=Cents(int((Decimal(str(amount))*100).quantize(Decimal("1")))),
                    actor_user_id=self._operator_id,
                )
            except CreditAccountError as exc:
                QMessageBox.critical(self, "Fiado/Pendura", str(exc))
                return
        QMessageBox.information(
            self, "Fiado/Pendura",
            f"Em aberto: R$ {format_cents(position.outstanding_cents)}\n"
            f"Disponível: R$ {format_cents(position.available_cents)}\n"
            f"Vencido: R$ {format_cents(position.overdue_cents)}",
        )

    def _close_cash_session(self) -> None:
        """F12 fecha o caixa sem revelar o esperado antes da declaração."""
        if self._cash_sessions is None or self._cash_sessions.current() is None:
            QMessageBox.information(self, "Fechar caixa", "Não há caixa aberto.")
            return
        declared, accepted = QInputDialog.getDouble(
            self, "Fechamento cego",
            "Conte o dinheiro da gaveta e informe o total (R$):",
            0.0, 0.0, 999_999.99, 2,
        )
        if not accepted:
            return
        authorizer = ManagerAuthDialog.ask(
            self._authorization,
            operation="Autorizar o fechamento cego desta sessão de caixa.",
            parent=self,
        )
        if authorizer is None:
            return
        try:
            result = self._cash_sessions.close(
                declared_cents=Cents(
                    int((Decimal(str(declared)) * Decimal(100)).quantize(Decimal("1")))
                ),
                operator_id=self._operator_id,
                authorizer_id=authorizer.id,
            )
        except CashSessionError as exc:
            QMessageBox.critical(self, "Fechar caixa", str(exc))
            return
        sign = "+" if int(result.difference_cents) > 0 else ""
        QMessageBox.information(
            self,
            "Caixa fechado",
            f"Declarado: R$ {format_cents(result.declared_cents)}\n"
            f"Esperado: R$ {format_cents(result.expected_cents)}\n"
            f"Divergência: {sign}R$ {format_cents(result.difference_cents)}",
        )
        self._cash_label.setText("Sessão: fechada")
        # Uma sessão encerrada não pode continuar recebendo vendas. Fechar a
        # janela força novo login e uma nova abertura antes da próxima venda.
        self.close()

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
        self._refresh_command_badge()

    def _refresh_command_badge(self) -> None:
        """Avisa o caixa que há ordem do painel esperando.

        Sem isto, o desconto que o gerente concedeu de longe mudaria o total na
        tela **sozinho**, no meio do atendimento, sem nada explicando por quê —
        e o operador ficaria olhando para um número que não bate com o que ele
        digitou. O aviso não pede permissão; só dá nome ao que vai acontecer.
        """
        try:
            waiting = InboxRepository(self._database).pending_count()
        except Exception:  # noqa: BLE001 - um selo não derruba o caixa
            return

        self._command_label.setVisible(waiting > 0)
        if waiting:
            self._command_label.setText(
                f"Painel: {waiting} comando(s) a aplicar"
                if waiting > 1
                else "Painel: 1 comando a aplicar"
            )

    # -- encerramento --------------------------------------------------------- #

    def closeEvent(self, event) -> None:  # noqa: N802 - override do Qt
        self._scale.shutdown()
        self._printer.shutdown()
        super().closeEvent(event)


def _secondary_button(label: str, handler) -> QPushButton:  # noqa: ANN001
    """Ação secundária: mesma altura da principal, peso visual menor.

    Igualar altura mantém a linha de botões alinhada; o que distingue a ação
    principal é a cor, não o tamanho — hierarquia por cor sobrevive ao operador
    que olha a tela de esguelha enquanto embala o pedido.
    """
    button = QPushButton(label)
    button.setMinimumHeight(54)
    button.setFont(theme.font(theme.SIZE_BODY, theme.WEIGHT_MEDIUM))
    button.clicked.connect(handler)
    return button


__all__ = ["CounterWindow", "Decimal"]
