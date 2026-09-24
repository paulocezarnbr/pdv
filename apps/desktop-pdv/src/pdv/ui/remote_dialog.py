"""Aceite, no caixa, de comando do painel que não pode valer sem alguém na loja.

O diálogo coleta e mostra; quem decide é `RemoteCommandService.confirm` /
`decline`, que confere a credencial por conta própria. Se o diálogo validasse o
PIN e só depois chamasse o serviço, a trava passaria a morar em duas camadas —
e a próxima tela que chamasse o serviço sem este diálogo pularia a metade que
ficou aqui.

Aceitar e recusar pedem a **mesma** credencial, de propósito. Recusar sem login
deixaria qualquer pessoa no balcão derrubar a ordem do gerente sem deixar nome,
e o painel receberia "recusado" sem ter a quem perguntar por quê.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pdv.domain.errors import PdvError
from pdv.domain.models import utc_now
from pdv.remote.commands import CommandRefused, RemoteCommandService
from pdv.remote.inbox import AwaitingCommand
from pdv.ui import theme


class RemoteConfirmationDialog(QDialog):
    """Lista o que espera aceite e decide um de cada vez."""

    def __init__(
        self,
        service: RemoteCommandService,
        *,
        default_login: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        #: O que foi decidido nesta abertura, na ordem — para a barra de status.
        self.decisions: list[str] = []

        self.setWindowTitle("Pedido do painel — aceite no caixa")
        self.setMinimumWidth(620)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            theme.SPACE_5, theme.SPACE_4, theme.SPACE_5, theme.SPACE_4
        )
        layout.setSpacing(theme.SPACE_3)

        headline = QLabel(
            "O painel pede para cancelar um item que já foi para a cozinha. "
            "Confira a mesa antes de aceitar."
        )
        headline.setFont(theme.font(theme.SIZE_BODY_LG, theme.WEIGHT_SEMIBOLD))
        headline.setWordWrap(True)
        layout.addWidget(headline)

        self._list = QListWidget()
        self._list.setWordWrap(True)
        self._list.setMinimumHeight(140)
        layout.addWidget(self._list)

        form = QFormLayout()
        self._login = QComboBox()
        self._login.setEditable(True)
        self._login.addItems(service.confirmer_logins())
        if default_login:
            self._login.setCurrentText(default_login)
        self._login.setMinimumHeight(34)
        form.addRow("Login:", self._login)

        self._pin = QLineEdit()
        self._pin.setEchoMode(QLineEdit.EchoMode.Password)
        self._pin.setMaxLength(12)
        self._pin.setMinimumHeight(34)
        self._pin.setFont(
            theme.font(theme.SIZE_TITLE, theme.WEIGHT_MEDIUM, mono=True, tracking=4.0)
        )
        form.addRow("PIN:", self._pin)

        self._reason = QLineEdit()
        self._reason.setMaxLength(200)
        self._reason.setMinimumHeight(34)
        self._reason.setPlaceholderText("Obrigatório para recusar — volta para o painel")
        form.addRow("Motivo:", self._reason)
        layout.addLayout(form)

        self._error = QLabel("")
        self._error.setObjectName("error")
        self._error.setFont(theme.font(theme.SIZE_BODY))
        self._error.setWordWrap(True)
        layout.addWidget(self._error)

        buttons = QHBoxLayout()
        self._accept_button = QPushButton("Aceitar cancelamento")
        self._accept_button.setObjectName("primary")
        self._accept_button.setMinimumHeight(40)
        self._accept_button.clicked.connect(self._accept_selected)
        self._decline_button = QPushButton("Recusar")
        self._decline_button.setMinimumHeight(40)
        self._decline_button.clicked.connect(self._decline_selected)
        # Fechar não decide nada: o pedido continua esperando e o selo
        # continua na barra. É a saída de quem precisa ir olhar a mesa antes.
        close = QPushButton("Fechar")
        close.setMinimumHeight(40)
        close.clicked.connect(self.reject)
        buttons.addWidget(self._accept_button)
        buttons.addWidget(self._decline_button)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self._reload()
        self._pin.setFocus()

    # -- ações ---------------------------------------------------------------- #

    def _accept_selected(self) -> None:
        uuid = self._selected_uuid()
        if uuid is None:
            return
        login, pin = self._credentials()
        if not login:
            return
        try:
            message = self._service.confirm(uuid, login=login, pin=pin)
        except CommandRefused as exc:
            # Uma trava recusou na hora do aceite (pedido fechou, janela
            # venceu). É decisão, não erro de digitação: sai da lista.
            self.decisions.append(f"Recusado: {exc}")
            self._after_decision(f"O comando não vale mais: {exc}")
            return
        except PdvError as exc:
            self._fail(str(exc))
            return
        self.decisions.append(message[:1].upper() + message[1:])
        self._after_decision("")

    def _decline_selected(self) -> None:
        uuid = self._selected_uuid()
        if uuid is None:
            return
        login, pin = self._credentials()
        if not login:
            return
        try:
            self._service.decline(
                uuid, login=login, pin=pin, reason=self._reason.text()
            )
        except PdvError as exc:
            self._fail(str(exc))
            return
        self.decisions.append("Pedido do painel recusado no caixa")
        self._after_decision("")

    # -- apoio ---------------------------------------------------------------- #

    def _credentials(self) -> tuple[str, str]:
        login = self._login.currentText().strip()
        pin = self._pin.text()
        if not login or not pin:
            self._error.setText("Informe login e PIN de quem está no caixa.")
            return "", ""
        return login, pin

    def _selected_uuid(self) -> str | None:
        item = self._list.currentItem()
        if item is None:
            self._error.setText("Escolha o pedido na lista.")
            return None
        return str(item.data(Qt.ItemDataRole.UserRole))

    def _fail(self, message: str) -> None:
        self._error.setText(message)
        self._pin.clear()
        self._pin.setFocus()

    def _after_decision(self, message: str) -> None:
        self._error.setText(message)
        self._pin.clear()
        self._reason.clear()
        self._reload()
        if self._list.count() == 0 and not message:
            self.accept()

    def _reload(self) -> None:
        self._list.clear()
        for entry in self._service.awaiting():
            item = QListWidgetItem(_describe(entry))
            item.setData(Qt.ItemDataRole.UserRole, entry.command.command_uuid)
            self._list.addItem(item)
        if self._list.count():
            self._list.setCurrentRow(0)
        has_items = self._list.count() > 0
        self._accept_button.setEnabled(has_items)
        self._decline_button.setEnabled(has_items)

    @property
    def waiting_count(self) -> int:
        return self._list.count()


def _describe(entry: AwaitingCommand) -> str:
    return f"{entry.note}\nAguardando há {_age(entry.requested_at)}"


def _age(requested_at: str) -> str:
    try:
        since = datetime.fromisoformat(requested_at)
    except ValueError:
        return "—"
    minutes = max(0, int((utc_now() - since).total_seconds() // 60))
    if minutes < 1:
        return "menos de 1 min"
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


__all__ = ["RemoteConfirmationDialog"]
