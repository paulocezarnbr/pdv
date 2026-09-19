"""Painel do Salão — o que o caixa precisa ver do app do garçom e do KDS.

A Fase 3 construiu o servidor, mas quem opera a loja não abre terminal para
parear um celular nem para saber se a cozinha está atrasada. Este painel é a
superfície disso no balcão, e existe por três motivos concretos:

* **Parear um aparelho** precisa de um código que apareça na tela do caixa. Sem
  isso o garçom não entra na rede da loja e o app é inútil na prática.
* **Revogar** precisa ser imediato e local. Celular perdido no fim do turno não
  pode esperar a próxima sincronização para deixar de lançar pedido.
* **A fila da cozinha** fica numa TV que ninguém consegue tocar com as mãos
  sujas. O caixa é quem destrava um ticket travado.

O painel só lê e comanda serviços que já existem e já são testados — não há
regra de negócio aqui.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
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
from pdv.domain.models import Cents, EntityId
from pdv.edge.auth import EdgeAuth
from pdv.edge.discovery import local_ip_address
from pdv.edge.kds import KdsService
from pdv.edge.orders import TableOrderService
from pdv.hardware.printer.escpos import format_cents
from pdv.ui import theme

#: O painel não recebe eventos do hub: ele é do caixa, não da cozinha, e um
#: WebSocket a mais por janela aberta pagaria um custo que uma consulta a cada
#: dois segundos no SQLite local não cobra.
REFRESH_MS = 2000

#: Os estados do ticket viajam em inglês no protocolo (o app do garçom e o KDS
#: leem o mesmo JSON) e são traduzidos só na hora de aparecer. Traduzir no
#: serviço quebraria o contrato do cliente mobile.
_STATUS_LABELS: dict[str, str] = {
    "queued": "na fila",
    "preparing": "preparando",
    "ready": "pronto",
    "delivered": "entregue",
}


class SalonPanel(QDialog):
    """Pareamento, mesas abertas e fila da cozinha."""

    def __init__(
        self,
        database: Database,
        config: AppConfig,
        *,
        port: int | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._auth = EdgeAuth(database, config.tenant_id, config.store_id)
        self._orders = TableOrderService(database, config)
        self._kds = KdsService(database, config)
        self._port = port

        self.setWindowTitle("Salão — garçom e cozinha")
        self.resize(980, 680)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            theme.SPACE_4, theme.SPACE_4, theme.SPACE_4, theme.SPACE_4
        )
        layout.setSpacing(theme.SPACE_3)
        layout.addWidget(self._build_pairing_box())

        columns = QHBoxLayout()
        columns.setSpacing(theme.SPACE_3)
        columns.addWidget(self._build_orders_box(), stretch=4)
        columns.addWidget(self._build_kds_box(), stretch=6)
        layout.addLayout(columns)

        close = QPushButton("Fechar   ·   ESC")
        close.setMinimumHeight(40)
        close.setFont(theme.font(theme.SIZE_BODY, theme.WEIGHT_MEDIUM))
        close.clicked.connect(self.reject)
        layout.addWidget(close)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_MS)
        self.refresh()

    # -- construção ------------------------------------------------------------ #

    def _build_pairing_box(self) -> QWidget:
        box = QFrame()
        box.setObjectName("panel")
        layout = QHBoxLayout(box)
        layout.setContentsMargins(
            theme.SPACE_4, theme.SPACE_3, theme.SPACE_4, theme.SPACE_3
        )
        layout.setSpacing(theme.SPACE_4)

        left = QVBoxLayout()
        left.setSpacing(theme.SPACE_1)
        left.addWidget(_title("SERVIDOR DO SALÃO"))
        self._address_label = QLabel("—")
        self._address_label.setFont(theme.font(theme.SIZE_BODY, mono=True))
        left.addWidget(self._address_label)
        layout.addLayout(left, stretch=3)

        middle = QVBoxLayout()
        middle.addWidget(_title("CÓDIGO DE PAREAMENTO"))
        self._code_label = QLabel("— — — — — —")
        self._code_label.setFont(
            theme.font(
                theme.SIZE_TOTAL, theme.WEIGHT_BOLD, mono=True, tracking=6.0
            )
        )
        self._code_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        middle.addWidget(self._code_label)
        self._code_hint = QLabel("Gere um código e digite-o no aplicativo do garçom.")
        self._code_hint.setObjectName("hint")
        self._code_hint.setWordWrap(True)
        middle.addWidget(self._code_hint)
        layout.addLayout(middle, stretch=4)

        right = QVBoxLayout()
        generate = QPushButton("Gerar código")
        generate.setObjectName("primary")
        generate.setMinimumHeight(44)
        generate.setFont(theme.font(theme.SIZE_BODY, theme.WEIGHT_SEMIBOLD))
        generate.clicked.connect(self._generate_code)
        right.addWidget(generate)

        revoke = QPushButton("Revogar aparelho")
        revoke.setMinimumHeight(40)
        revoke.clicked.connect(self._revoke_device)
        right.addWidget(revoke)
        layout.addLayout(right, stretch=2)

        return box

    def _build_orders_box(self) -> QWidget:
        box = QFrame()
        box.setObjectName("panel")
        layout = QVBoxLayout(box)

        layout.setContentsMargins(
            theme.SPACE_3, theme.SPACE_3, theme.SPACE_3, theme.SPACE_3
        )
        layout.setSpacing(theme.SPACE_2)

        layout.addWidget(_title("MESAS ABERTAS"))
        self._orders_table = QTableWidget(0, 5)
        self._orders_table.setHorizontalHeaderLabels(
            ["Mesa", "Nº", "Itens", "Total", "Situação"]
        )
        _configure(self._orders_table, stretch_column=0)
        layout.addWidget(self._orders_table, stretch=1)

        layout.addWidget(_title("APARELHOS PAREADOS"))
        self._devices_table = QTableWidget(0, 3)
        self._devices_table.setHorizontalHeaderLabels(["Aparelho", "Tipo", "Situação"])
        _configure(self._devices_table, stretch_column=0)
        self._devices_table.setMaximumHeight(160)
        layout.addWidget(self._devices_table)

        return box

    def _build_kds_box(self) -> QWidget:
        box = QFrame()
        box.setObjectName("panel")
        layout = QVBoxLayout(box)

        layout.setContentsMargins(
            theme.SPACE_3, theme.SPACE_3, theme.SPACE_3, theme.SPACE_3
        )
        layout.setSpacing(theme.SPACE_2)

        layout.addWidget(_title("FILA DA COZINHA"))
        self._kds_table = QTableWidget(0, 5)
        self._kds_table.setHorizontalHeaderLabels(
            ["Mesa", "Item", "Qtd", "Situação", "Espera"]
        )
        _configure(self._kds_table, stretch_column=1)
        layout.addWidget(self._kds_table, stretch=1)

        buttons = QHBoxLayout()
        buttons.setSpacing(theme.SPACE_2)
        advance = QPushButton("Avançar   ·   bump")
        advance.setObjectName("primary")
        advance.setMinimumHeight(44)
        advance.setFont(theme.font(theme.SIZE_BODY, theme.WEIGHT_SEMIBOLD))
        advance.clicked.connect(self._bump)
        buttons.addWidget(advance)

        back = QPushButton("Voltar   ·   recall")
        back.setMinimumHeight(44)
        back.setFont(theme.font(theme.SIZE_BODY, theme.WEIGHT_MEDIUM))
        back.clicked.connect(self._recall)
        buttons.addWidget(back)
        layout.addLayout(buttons)

        return box

    # -- ações ----------------------------------------------------------------- #

    def _generate_code(self) -> None:
        try:
            code = self._auth.create_pairing_code()
        except PdvError as exc:
            QMessageBox.critical(self, "Pareamento", str(exc))
            return
        # Agrupado de três em três: o garçom lê da tela e digita no celular a
        # dois metros de distância, geralmente de pé.
        self._code_label.setText(f"{code[:3]} {code[3:]}")
        self._code_hint.setText(
            "Válido por 5 minutos e por um único aparelho. "
            "Gerar outro código não invalida este."
        )

    def _revoke_device(self) -> None:
        row = self._devices_table.currentRow()
        if row < 0:
            QMessageBox.information(
                self, "Revogar", "Selecione um aparelho na lista abaixo."
            )
            return

        item = self._devices_table.item(row, 0)
        device_id = str(item.data(Qt.ItemDataRole.UserRole))
        name = item.text()

        confirm = QMessageBox.question(
            self,
            "Revogar aparelho",
            f"Revogar {name}?\n\nO aparelho para de lançar pedidos imediatamente. "
            "Os pedidos já lançados continuam valendo.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self._auth.revoke(EntityId(device_id))
        self.refresh()

    def _bump(self) -> None:
        self._transition(self._kds.bump)

    def _recall(self) -> None:
        self._transition(self._kds.recall)

    def _transition(self, action) -> None:  # noqa: ANN001 - método do KdsService
        row = self._kds_table.currentRow()
        if row < 0:
            return
        ticket_id = str(self._kds_table.item(row, 0).data(Qt.ItemDataRole.UserRole))
        try:
            action(EntityId(ticket_id))
        except PdvError as exc:
            QMessageBox.warning(self, "Cozinha", str(exc))
            return
        self.refresh()

    # -- leitura --------------------------------------------------------------- #

    def refresh(self) -> None:
        self._refresh_address()
        self._refresh_orders()
        self._refresh_devices()
        self._refresh_kds()

    def _refresh_address(self) -> None:
        if self._port is None:
            self._address_label.setText(
                "Servidor do salão DESLIGADO — o balcão segue vendendo."
            )
            self._address_label.setStyleSheet(f"color: {theme.DANGER};")
            return
        # O endereço deixou de ser só diagnóstico: é o app do garçom. Quem está
        # no caixa precisa saber o que ditar para o celular, sem procurar.
        self._address_label.setText(
            f"App do garçom: http://{local_ip_address()}:{self._port}\n"
            "Abra no navegador do celular e pareie com o código acima."
        )
        self._address_label.setStyleSheet(f"color: {theme.OK};")

    def _refresh_orders(self) -> None:
        orders = self._orders.list_open_orders()
        selected = _selected_key(self._orders_table)
        self._orders_table.setRowCount(0)
        for order in orders:
            row = self._orders_table.rowCount()
            self._orders_table.insertRow(row)
            cells = [
                order.table_label,
                f"{order.local_number:05d}",
                str(order.item_count),
                f"R$ {format_cents(Cents(int(order.total_cents)))}",
                "pedindo a conta" if order.bill_requested else "",
            ]
            for column, value in enumerate(cells):
                cell = QTableWidgetItem(value)
                # A coluna de situação é texto, não número: alinhá-la à direita
                # e em monoespaçada junto com o dinheiro faria a fila de
                # "pedindo a conta" parecer mais uma coluna de valores.
                if column and column < 4:
                    cell.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                    cell.setFont(theme.font(theme.SIZE_BODY, mono=True))
                self._orders_table.setItem(row, column, cell)

            if order.bill_requested:
                # O caixa precisa enxergar de longe qual mesa está esperando
                # para pagar — é a única linha da tela em que alguém está de pé
                # aguardando. Sem o destaque, o garçom acaba tendo de vir
                # avisar, que é justamente o que o app veio eliminar.
                for column in range(self._orders_table.columnCount()):
                    self._orders_table.item(row, column).setForeground(
                        QColor(theme.WARN)
                    )
            self._orders_table.item(row, 0).setData(
                Qt.ItemDataRole.UserRole, order.id
            )
        _restore_key(self._orders_table, selected)

    def _refresh_devices(self) -> None:
        selected = _selected_key(self._devices_table)
        self._devices_table.setRowCount(0)
        for device in self._auth.list_devices():
            row = self._devices_table.rowCount()
            self._devices_table.insertRow(row)
            revoked = device.get("revoked_at") is not None
            cells = [
                str(device.get("name") or "—"),
                str(device.get("kind") or "—"),
                "revogado" if revoked else _last_seen(device.get("last_seen_at")),
            ]
            for column, value in enumerate(cells):
                cell = QTableWidgetItem(value)
                if revoked:
                    cell.setForeground(QColor(theme.DANGER))
                self._devices_table.setItem(row, column, cell)
            self._devices_table.item(row, 0).setData(
                Qt.ItemDataRole.UserRole, str(device.get("id"))
            )
        _restore_key(self._devices_table, selected)

    def _refresh_kds(self) -> None:
        selected = _selected_key(self._kds_table)
        self._kds_table.setRowCount(0)
        for ticket in self._kds.list_active():
            row = self._kds_table.rowCount()
            self._kds_table.insertRow(row)
            cells = [
                ticket.table_label,
                ticket.product_name,
                ticket.quantity,
                _STATUS_LABELS.get(ticket.status, ticket.status),
                _format_wait(ticket.waiting_seconds),
            ]
            for column, value in enumerate(cells):
                cell = QTableWidgetItem(value)
                if column in (2, 4):
                    cell.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                    cell.setFont(theme.font(theme.SIZE_BODY, mono=True))
                if ticket.is_late:
                    # Atraso é a única informação da fila que precisa gritar: o
                    # prato parado há 15 minutos já está custando a mesa.
                    cell.setForeground(QColor(theme.DANGER))
                self._kds_table.setItem(row, column, cell)
            self._kds_table.item(row, 0).setData(Qt.ItemDataRole.UserRole, ticket.id)
        _restore_key(self._kds_table, selected)


# --------------------------------------------------------------------------- #
# Auxiliares de tabela
# --------------------------------------------------------------------------- #


def _format_wait(seconds: int) -> str:
    """Tempo de espera do ticket.

    `MM:SS` só faz sentido até uma hora. Passando disso o minuto acumulado
    vira um número que ninguém lê — um ticket esquecido no fim do expediente
    aparecia como "377:23", que não comunica "seis horas parado", comunica
    "tem coisa errada nesta tela".
    """
    if seconds < 3600:
        minutes, remainder = divmod(seconds, 60)
        return f"{minutes:02d}:{remainder:02d}"
    hours, remainder = divmod(seconds, 3600)
    return f"{hours}h{remainder // 60:02d}"


def _last_seen(value: object) -> str:
    """Só a hora do último contato.

    O campo guarda ISO-8601 em UTC, que não cabe na coluna e não diz nada ao
    operador. O que ele precisa saber é se o aparelho falou com o terminal
    agora há pouco — a data só interessaria se fosse de outro dia, e nesse caso
    o aparelho já não está em turno.
    """
    if not value:
        return "nunca conectou"
    text = str(value)
    clock = text[11:19] if len(text) >= 19 else text
    return f"visto {clock} UTC"


def _title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("sectionTitle")
    label.setFont(theme.font(theme.SIZE_MICRO, theme.WEIGHT_SEMIBOLD, tracking=1.6))
    return label


def _configure(table: QTableWidget, *, stretch_column: int) -> None:
    header = table.horizontalHeader()
    # As demais colunas encolhem até o conteúdo para que a coluna que importa
    # (mesa, item) fique com o resto. Sem isto a largura default de cada coluna
    # numérica espremia "Mesa 4" até virar "Me…" — e a mesa é justamente o que
    # o operador procura na tela.
    for column in range(table.columnCount()):
        header.setSectionResizeMode(
            column,
            QHeaderView.ResizeMode.Stretch
            if column == stretch_column
            else QHeaderView.ResizeMode.ResizeToContents,
        )
    header.setFont(theme.font(theme.SIZE_MICRO, theme.WEIGHT_MEDIUM, tracking=0.6))
    table.verticalHeader().setVisible(False)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.setFont(theme.font(theme.SIZE_BODY))
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)


def _selected_key(table: QTableWidget) -> str | None:
    """Guarda o id da linha selecionada antes de repovoar a tabela.

    Sem isto, o refresh a cada dois segundos tiraria a seleção debaixo do dedo
    do operador — e ele acabaria dando bump no ticket errado.
    """
    row = table.currentRow()
    if row < 0:
        return None
    item = table.item(row, 0)
    return None if item is None else str(item.data(Qt.ItemDataRole.UserRole))


def _restore_key(table: QTableWidget, key: str | None) -> None:
    if key is None:
        return
    for row in range(table.rowCount()):
        item = table.item(row, 0)
        if item is not None and str(item.data(Qt.ItemDataRole.UserRole)) == key:
            table.selectRow(row)
            return


__all__ = ["SalonPanel"]
