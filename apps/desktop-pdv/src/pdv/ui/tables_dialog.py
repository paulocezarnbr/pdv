"""Visualizador de mesas do caixa — e o recebimento da conta.

Por que esta tela existe
------------------------

O `SalonPanel` (F8) mostra as mesas abertas como uma lista pequena ao lado da
fila da cozinha. Serve para conferir, não para trabalhar: quando o salão tem
trinta mesas, achar "a mesa 17, que pediu a conta" numa lista sem busca é o
tipo de coisa que faz o caixa pedir para o garçom vir dizer qual é.

E faltava a metade que importa: **receber**. `request_bill` existia desde a
Fase 3 e marcava a mesa como "pedindo a conta" — e ali a história acabava.
Nenhuma tela do balcão fechava aquele pedido. Na prática a mesa ficava ocupada
para sempre no mapa, e o jeito de liberá-la era *cancelar a comanda*: apagar a
venda para poder sentar o próximo cliente. O vetor de furto do salão virava o
procedimento normal da casa, com a justificativa pronta.

Dividir e juntar
----------------

Três gestos do dia a dia de salão, todos aqui e nenhum no celular: **pagar
parte** (o casal que acerta só o que consumiu), **mover itens** (o garçom lançou
na mesa errada) e **juntar comandas** (duas mesas encostadas viram uma conta).
A regra de cada um mora em `TableOrderService`; esta tela só escolhe.

A gorjeta entra aqui, e não no celular
--------------------------------------

Pelo mesmo motivo que o garçom pede a conta mas não a recebe: dinheiro passa
por um lugar só. A gorjeta é digitada onde ela é efetivamente recebida, junto
com a forma de pagamento, e fica registrada no nome de quem atendeu a mesa —
que é o que torna a divisão do fim da noite conferível em vez de combinada.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.domain.errors import PdvError
from pdv.domain.models import Cents, EntityId, utc_now
from pdv.edge.orders import SettledOrder, TableOrder, TableOrderService
from pdv.edge.tables import TableService
from pdv.hardware.printer.escpos import format_cents
from pdv.services.authorization import Identity
from pdv.ui import theme
from pdv.ui.dialogs import PaymentDialog

#: O mapa é relido a cada dois segundos, como o painel do salão: o garçom pede
#: a conta do celular e o caixa precisa ver a mesa mudar de cor sem apertar
#: nada. Uma consulta no SQLite local a cada dois segundos não custa nada
#: perto de manter um WebSocket por janela aberta.
REFRESH_MS = 2000

#: Sugestão de gorjeta. Dez por cento é o costume no Brasil e o valor que a
#: casa imprime na conta; fica como botão, nunca como padrão aplicado sozinho
#: — gorjeta cobrada sem alguém decidir por ela é a reclamação clássica.
SUGGESTED_TIP_PERCENT = 10


class TablesDialog(QDialog):
    """O salão inteiro, com busca, e o botão de receber a conta."""

    def __init__(
        self,
        database: Database,
        config: AppConfig,
        *,
        operator: Identity,
        on_receipt=None,  # noqa: ANN001 - callable(bytes, str) da fila de impressão
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._tables = TableService(database, config)
        self._orders = TableOrderService(database, config)
        self._operator = operator
        self._on_receipt = on_receipt
        self._rows: list[TableOrder] = []

        self.setWindowTitle("Mesas do salão")
        self.resize(1040, 700)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            theme.SPACE_4, theme.SPACE_4, theme.SPACE_4, theme.SPACE_4
        )
        layout.setSpacing(theme.SPACE_3)

        layout.addLayout(self._build_search())
        layout.addWidget(self._build_table(), stretch=1)
        layout.addWidget(self._summary_label())
        layout.addLayout(self._build_buttons())

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_MS)
        self.refresh()

    # -- construção ------------------------------------------------------------ #

    def _build_search(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(theme.SPACE_2)

        self._search = QLineEdit()
        self._search.setPlaceholderText(
            "Buscar mesa, área, garçom ou número da comanda…"
        )
        self._search.setMinimumHeight(40)
        self._search.setClearButtonEnabled(True)
        # Filtra a cada tecla, sobre a lista já em memória. Reconsultar o banco
        # a cada letra seria trinta consultas para digitar "varanda".
        self._search.textChanged.connect(self._repaint)
        row.addWidget(self._search, stretch=1)

        self._only_billing = QCheckBox("Só quem pediu a conta")
        self._only_billing.setMinimumHeight(40)
        self._only_billing.stateChanged.connect(self._repaint)
        row.addWidget(self._only_billing)

        return row

    def _build_table(self) -> QWidget:
        self._table = QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels(
            ["Mesa", "Área", "Situação", "Garçom", "Comanda", "Itens", "Total", "Tempo"]
        )
        header = self._table.horizontalHeader()
        for column in range(self._table.columnCount()):
            header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.Stretch
                if column == 0
                else QHeaderView.ResizeMode.ResizeToContents,
            )
        header.setFont(theme.font(theme.SIZE_MICRO, theme.WEIGHT_MEDIUM, tracking=0.6))
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.setFont(theme.font(theme.SIZE_BODY))
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        # Duplo clique recebe: é o gesto que o operador tenta por conta própria
        # na primeira vez que usa a tela.
        self._table.doubleClicked.connect(self._receive)
        return self._table

    def _summary_label(self) -> QWidget:
        self._summary = QLabel("")
        self._summary.setObjectName("hint")
        self._summary.setFont(theme.font(theme.SIZE_MICRO))
        return self._summary

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(theme.SPACE_2)

        receive = QPushButton("Receber conta da mesa")
        receive.setObjectName("primary")
        receive.setMinimumHeight(46)
        receive.setFont(theme.font(theme.SIZE_BODY, theme.WEIGHT_SEMIBOLD))
        receive.clicked.connect(self._receive)
        row.addWidget(receive, stretch=2)

        for label, handler in (
            ("Pagar parte", self._receive_part),
            ("Mover itens", self._move_items),
            ("Juntar comandas", self._merge),
        ):
            button = QPushButton(label)
            button.setMinimumHeight(46)
            button.clicked.connect(handler)
            row.addWidget(button, stretch=1)

        close = QPushButton("Fechar   ·   ESC")
        close.setMinimumHeight(46)
        close.clicked.connect(self.reject)
        row.addWidget(close, stretch=1)

        return row

    # -- leitura --------------------------------------------------------------- #

    def refresh(self) -> None:
        """Relê o salão do banco e repinta."""
        self._rows = self._orders.list_open_orders()
        tables = self._tables.list_tables()
        self._free = [t for t in tables if not t.occupied]
        # A área vive na mesa, não no pedido: o pedido guarda só o rótulo, de
        # propósito (renomear a mesa amanhã não pode reescrever o cupom de
        # hoje). Resolver aqui, uma vez por leitura, evita uma consulta por
        # linha só para preencher uma coluna.
        self._areas = {str(t.id): t.area for t in tables}
        self._repaint()

    def _repaint(self) -> None:
        selected = self._selected_id()
        query = self._search.text().strip().lower()
        only_billing = self._only_billing.isChecked()

        visible = [
            order
            for order in self._rows
            if (not only_billing or order.bill_requested)
            and _matches(order, self._area_of(order), query)
        ]

        self._table.setRowCount(0)
        for order in visible:
            row = self._table.rowCount()
            self._table.insertRow(row)
            cells = [
                order.table_label,
                self._area_of(order),
                "pedindo a conta" if order.bill_requested else "ocupada",
                order.waiter_name or "—",
                f"{order.local_number:05d}",
                str(order.item_count),
                f"R$ {format_cents(Cents(int(order.total_cents)))}",
                _elapsed(order.opened_at),
            ]
            for column, value in enumerate(cells):
                cell = QTableWidgetItem(value)
                if column in (4, 5, 6, 7):
                    cell.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                    cell.setFont(theme.font(theme.SIZE_BODY, mono=True))
                if order.bill_requested:
                    # A mesa que pediu a conta é a única da tela em que tem
                    # gente de pé esperando. Sem o destaque, o garçom acaba
                    # tendo de vir avisar — que é o que o app veio eliminar.
                    cell.setForeground(QColor(theme.WARN))
                self._table.setItem(row, column, cell)
            self._table.item(row, 0).setData(Qt.ItemDataRole.UserRole, order.id)

        self._restore(selected)
        self._refresh_summary(visible)

    def _area_of(self, order: TableOrder) -> str:
        return self._areas.get(str(order.table_id), "—")

    def _refresh_summary(self, visible: list[TableOrder]) -> None:
        billing = sum(1 for o in self._rows if o.bill_requested)
        total = sum(int(o.total_cents) for o in visible)
        self._summary.setText(
            f"{len(visible)} de {len(self._rows)} comandas abertas · "
            f"{billing} pedindo a conta · {len(self._free)} mesas livres · "
            f"na tela: R$ {format_cents(Cents(total))}"
        )

    # -- recebimento ----------------------------------------------------------- #

    def _receive(self) -> None:
        order = self._selected_order()
        if order is None:
            QMessageBox.information(
                self, "Receber", "Selecione uma mesa na lista."
            )
            return

        if not order.bill_requested:
            # Não bloqueia: acontece de o cliente ir embora pelo caixa sem o
            # garçom ter pedido a conta no celular. Só confirma, para que
            # receber a mesa errada exija um segundo gesto.
            confirm = QMessageBox.question(
                self,
                "Receber",
                f"A {order.table_label} ainda não pediu a conta.\n\n"
                f"Receber mesmo assim R$ "
                f"{format_cents(Cents(int(order.total_cents)))}?",
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return

        tip = TipDialog.ask(order, parent=self)
        if tip is None:
            return

        charged = Cents(int(order.total_cents) + int(tip))
        payment = PaymentDialog(charged, parent=self)
        if payment.exec() != PaymentDialog.DialogCode.Accepted:
            return

        try:
            settled = self._orders.settle(
                order_id=EntityId(str(order.id)),
                payments=payment.payments,
                operator_id=EntityId(str(self._operator.id)),
                operator_name=self._operator.name,
                tip_cents=tip,
            )
        except PdvError as exc:
            QMessageBox.critical(self, "Não foi possível receber", str(exc))
            return

        # A conta já está fechada no banco. A impressão é assíncrona, pelo
        # mesmo motivo do balcão: papel acabado não pode desfazer uma
        # transação concluída — reimprime-se.
        if self._on_receipt is not None and settled.receipt:
            self._on_receipt(
                settled.receipt, f"Mesa {settled.order.local_number:06d}"
            )

        self._announce(settled)
        self.refresh()

    # -- dividir e juntar ------------------------------------------------------ #

    def _receive_part(self) -> None:
        order = self._require_selected("Pagar parte")
        if order is None:
            return
        picked = ItemPickerDialog.ask(
            self._orders.list_items(EntityId(str(order.id))),
            title=f"{order.table_label} — o que este cliente vai pagar",
            confirm="Ir para a gorjeta",
            parent=self,
        )
        if picked is None:
            return
        ids, part = picked

        # A gorjeta e o pagamento enxergam só a parte: é ela que o cliente
        # confere, e 10% da mesa inteira cobrados de quem pediu um café é o
        # erro que ninguém percebe até a reclamação.
        tip = TipDialog.ask(
            replace(order, total_cents=Cents(part), item_count=len(ids)), parent=self
        )
        if tip is None:
            return
        payment = PaymentDialog(Cents(part + int(tip)), parent=self)
        if payment.exec() != PaymentDialog.DialogCode.Accepted:
            return

        try:
            settled = self._orders.settle_items(
                order_id=EntityId(str(order.id)),
                item_ids=ids,
                payments=payment.payments,
                operator_id=EntityId(str(self._operator.id)),
                operator_name=self._operator.name,
                tip_cents=tip,
            )
        except PdvError as exc:
            QMessageBox.critical(self, "Não foi possível receber", str(exc))
            return

        if self._on_receipt is not None and settled.receipt:
            self._on_receipt(settled.receipt, f"Mesa {settled.order.local_number:06d}")
        self._announce(settled)
        self.refresh()

    def _move_items(self) -> None:
        source = self._require_selected("Mover itens")
        if source is None:
            return
        target = self._pick_target(source, "Mover itens para qual comanda?")
        if target is None:
            return
        picked = ItemPickerDialog.ask(
            self._orders.list_items(EntityId(str(source.id))),
            title=f"{source.table_label} → {target.table_label}: quais itens",
            confirm="Mover",
            parent=self,
        )
        if picked is None:
            return
        try:
            self._orders.move_items(
                source_order_id=EntityId(str(source.id)),
                target_order_id=EntityId(str(target.id)),
                item_ids=picked[0],
                operator_id=EntityId(str(self._operator.id)),
                operator_name=self._operator.name,
            )
        except PdvError as exc:
            QMessageBox.critical(self, "Não foi possível mover", str(exc))
            return
        self.refresh()

    def _merge(self) -> None:
        source = self._require_selected("Juntar comandas")
        if source is None:
            return
        target = self._pick_target(
            source, f"Juntar a {source.table_label} em qual comanda?"
        )
        if target is None:
            return
        together = int(source.total_cents) + int(target.total_cents)
        confirm = QMessageBox.question(
            self,
            "Juntar comandas",
            f"Passar os {source.item_count} itens da {source.table_label} "
            f"(R$ {format_cents(Cents(int(source.total_cents)))}) para a "
            f"{target.table_label}?\n\nA conta da {target.table_label} fica em "
            f"R$ {format_cents(Cents(together))} e a {source.table_label} é "
            "liberada.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self._orders.merge_orders(
                source_order_id=EntityId(str(source.id)),
                target_order_id=EntityId(str(target.id)),
                operator_id=EntityId(str(self._operator.id)),
                operator_name=self._operator.name,
            )
        except PdvError as exc:
            QMessageBox.critical(self, "Não foi possível juntar", str(exc))
            return
        self.refresh()

    def _require_selected(self, title: str) -> TableOrder | None:
        order = self._selected_order()
        if order is None:
            QMessageBox.information(self, title, "Selecione uma mesa na lista.")
        return order

    def _pick_target(self, source: TableOrder, prompt: str) -> TableOrder | None:
        others = [o for o in self._rows if str(o.id) != str(source.id)]
        if not others:
            QMessageBox.information(
                self, "Sem destino", "Não há outra comanda aberta no salão."
            )
            return None
        labels = [
            f"{o.table_label} · comanda {o.local_number:05d} · "
            f"R$ {format_cents(Cents(int(o.total_cents)))}"
            for o in others
        ]
        choice, accepted = QInputDialog.getItem(self, "Destino", prompt, labels, 0, False)
        if not accepted:
            return None
        return others[labels.index(choice)]

    def _announce(self, settled: SettledOrder) -> None:
        order = settled.order
        pieces = [
            f"{order.table_label} recebida — R$ "
            f"{format_cents(Cents(int(settled.charged_cents)))}"
        ]
        if int(settled.tip_cents):
            who = order.waiter_name.split()[0] if order.waiter_name else "a equipe"
            pieces.append(
                f"gorjeta R$ {format_cents(Cents(int(settled.tip_cents)))} "
                f"para {who}"
            )
        if int(settled.change_cents):
            pieces.append(
                f"troco R$ {format_cents(Cents(int(settled.change_cents)))}"
            )
        QMessageBox.information(self, "Conta recebida", " · ".join(pieces))

    # -- utilidades ------------------------------------------------------------ #

    def _selected_order(self) -> TableOrder | None:
        key = self._selected_id()
        return next((o for o in self._rows if str(o.id) == key), None)

    def _selected_id(self) -> str | None:
        row = self._table.currentRow()
        if row < 0:
            return None
        item = self._table.item(row, 0)
        return None if item is None else str(item.data(Qt.ItemDataRole.UserRole))

    def _restore(self, key: str | None) -> None:
        """Devolve a seleção depois de repovoar.

        Sem isto, o refresh a cada dois segundos tira a linha debaixo do dedo
        do operador — e receber a mesa errada é um problema com dinheiro.
        """
        if key is None:
            return
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item is not None and str(item.data(Qt.ItemDataRole.UserRole)) == key:
                self._table.selectRow(row)
                return


class ItemPickerDialog(QDialog):
    """Escolher itens da comanda, com o total do que foi marcado à vista.

    O total muda a cada marca porque é ele que o cliente confere antes de
    pagar a parte dele — escolher às cegas e descobrir o valor só no
    pagamento é voltar atrás com a fila parada.
    """

    def __init__(
        self,
        items: list[dict[str, object]],
        *,
        title: str,
        confirm: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # Cancelado não se escolhe: já saiu da conta, e movê-lo ou cobrá-lo
        # seria desfazer um cancelamento autorizado por gerente.
        self._items = [i for i in items if not i["canceled"]]
        self.selected: list[EntityId] = []
        self.total_cents = 0

        self.setWindowTitle(title)
        self.setMinimumSize(560, 420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.SPACE_4, theme.SPACE_4, theme.SPACE_4, theme.SPACE_4)
        layout.setSpacing(theme.SPACE_3)

        self._table = QTableWidget(len(self._items), 3)
        self._table.setHorizontalHeaderLabels(["Item", "Qtd", "Valor"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        for row, item in enumerate(self._items):
            name = QTableWidgetItem(str(item["product_name"]))
            name.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            name.setCheckState(Qt.CheckState.Unchecked)
            self._table.setItem(row, 0, name)
            self._table.setItem(row, 1, QTableWidgetItem(str(item["quantity"])))
            value = QTableWidgetItem(f"R$ {format_cents(Cents(int(item['total_cents'])))}")
            value.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._table.setItem(row, 2, value)
        self._table.itemChanged.connect(self._refresh_total)
        layout.addWidget(self._table, stretch=1)

        self._total = QLabel("")
        self._total.setFont(theme.font(theme.SIZE_TITLE, theme.WEIGHT_SEMIBOLD, mono=True))
        self._total.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self._total)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setText(confirm)
        self._ok.setObjectName("primary")
        self._ok.setMinimumHeight(44)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Voltar")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh_total()

    def check(self, row: int, checked: bool = True) -> None:
        """Marca uma linha (usado pelos testes da tela)."""
        self._table.item(row, 0).setCheckState(
            Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        )

    def _refresh_total(self) -> None:
        rows = [
            row for row in range(self._table.rowCount())
            if self._table.item(row, 0) is not None
            and self._table.item(row, 0).checkState() == Qt.CheckState.Checked
        ]
        self.selected = [EntityId(str(self._items[row]["id"])) for row in rows]
        self.total_cents = sum(int(self._items[row]["total_cents"]) for row in rows)
        self._total.setText(
            f"{len(rows)} item(ns) · R$ {format_cents(Cents(self.total_cents))}"
        )
        self._ok.setEnabled(bool(rows))

    @classmethod
    def ask(
        cls,
        items: list[dict[str, object]],
        *,
        title: str,
        confirm: str,
        parent: QWidget | None = None,
    ) -> tuple[list[EntityId], int] | None:
        """Devolve os itens marcados e o total deles, ou `None` se voltou."""
        dialog = cls(items, title=title, confirm=confirm, parent=parent)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.selected:
            return None
        return dialog.selected, dialog.total_cents


class TipDialog(QDialog):
    """Quanto de gorjeta entrou com esta conta."""

    def __init__(self, order: TableOrder, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._order = order
        self.tip_cents: Cents | None = None

        self.setWindowTitle(f"{order.table_label} — conferir a conta")
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            theme.SPACE_5, theme.SPACE_4, theme.SPACE_5, theme.SPACE_4
        )
        layout.setSpacing(theme.SPACE_3)

        total = QLabel(
            f"CONTA: R$ {format_cents(Cents(int(order.total_cents)))}"
        )
        total.setFont(
            theme.font(theme.SIZE_TOTAL, theme.WEIGHT_BOLD, display=True, tracking=-1.0)
        )
        total.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(total)

        who = QLabel(
            f"Comanda {order.local_number:05d} · {order.item_count} itens"
            + (f" · atendida por {order.waiter_name}" if order.waiter_name else "")
        )
        who.setObjectName("hint")
        layout.addWidget(who)

        entry = QHBoxLayout()
        entry.setSpacing(theme.SPACE_2)
        entry.addWidget(QLabel("Gorjeta"))

        self._tip = QDoubleSpinBox()
        self._tip.setPrefix("R$ ")
        self._tip.setDecimals(2)
        self._tip.setMaximum(99_999.99)
        self._tip.setMinimumHeight(40)
        self._tip.setFont(theme.font(theme.SIZE_TITLE, theme.WEIGHT_MEDIUM, mono=True))
        entry.addWidget(self._tip, stretch=2)

        suggest = QPushButton(f"{SUGGESTED_TIP_PERCENT}%")
        suggest.setMinimumHeight(40)
        suggest.clicked.connect(self._suggest)
        entry.addWidget(suggest)

        none = QPushButton("Sem gorjeta")
        none.setMinimumHeight(40)
        none.clicked.connect(lambda: self._tip.setValue(0))
        entry.addWidget(none)
        layout.addLayout(entry)

        self._preview = QLabel("")
        self._preview.setFont(theme.font(theme.SIZE_BODY, theme.WEIGHT_MEDIUM))
        self._preview.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self._preview)
        self._tip.valueChanged.connect(self._refresh_preview)

        hint = QLabel(
            "A gorjeta fica registrada no nome de quem atendeu a mesa e não "
            "entra no faturamento da loja."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Ir para o pagamento")
        ok.setObjectName("primary")
        ok.setMinimumHeight(44)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Voltar")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh_preview()

    def _suggest(self) -> None:
        # Arredondamento para baixo, no centavo: cobrar um centavo a mais do
        # cliente por conta do arredondamento é o tipo de detalhe que vira
        # reclamação e não rende nada a ninguém.
        self._tip.setValue(
            (int(self._order.total_cents) * SUGGESTED_TIP_PERCENT // 100) / 100
        )

    def _refresh_preview(self) -> None:
        tip = self._current_tip()
        self._preview.setText(
            f"A cobrar: R$ "
            f"{format_cents(Cents(int(self._order.total_cents) + int(tip)))}"
        )

    def _current_tip(self) -> Cents:
        return Cents(int(round(self._tip.value() * 100)))

    def _accept(self) -> None:
        self.tip_cents = self._current_tip()
        self.accept()

    @classmethod
    def ask(cls, order: TableOrder, parent: QWidget | None = None) -> Cents | None:
        """Devolve a gorjeta, ou `None` se o operador voltou."""
        dialog = cls(order, parent=parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.tip_cents


# --------------------------------------------------------------------------- #
# Auxiliares
# --------------------------------------------------------------------------- #


def _matches(order: TableOrder, area: str, query: str) -> bool:
    """A busca cobre o que o operador tem em mãos quando procura uma mesa.

    Ele ouve "a mesa da varanda", lê o número da comanda no cupom, ou o garçom
    diz "é a minha". Buscar só pelo rótulo obrigaria a saber justamente o que
    ele está tentando descobrir.
    """
    if not query:
        return True
    haystack = " ".join(
        [
            order.table_label,
            area,
            order.waiter_name,
            str(order.local_number),
            f"{order.local_number:05d}",
        ]
    ).lower()
    return all(term in haystack for term in query.split())


def _elapsed(opened_at: str | None) -> str:
    """Há quanto tempo a mesa está aberta.

    É o número que diz se a comanda de R$ 12,00 é uma mesa que acabou de sentar
    ou uma que está aberta desde o almoço — e comanda esquecida aberta é como
    consumo some da conta.
    """
    if not opened_at:
        return "—"
    from datetime import datetime

    try:
        started = datetime.fromisoformat(opened_at)
    except ValueError:  # pragma: no cover - coluna corrompida
        return "—"

    seconds = int((utc_now() - started).total_seconds())
    if seconds < 3600:
        return f"{seconds // 60:02d}min"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}"


__all__ = [
    "REFRESH_MS",
    "SUGGESTED_TIP_PERCENT",
    "ItemPickerDialog",
    "TablesDialog",
    "TipDialog",
]
