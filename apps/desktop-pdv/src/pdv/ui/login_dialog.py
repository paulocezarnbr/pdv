"""Login de abertura do caixa.

Por que o PDV passou a exigir login
-----------------------------------

Até aqui o terminal abria já operando, com o operador fixado no código
(`DEMO_OPERATOR_ID`). Toda venda, todo cancelamento e todo evento de auditoria
saíam no nome da mesma pessoa — inclusive os feitos por outra. Num sistema cujo
módulo central é anti-furto, isso esvazia a trilha: "quem fez" era sempre a
mesma resposta, e portanto nenhuma.

Com login, o `operator_id` gravado é de quem está no balcão. A troca de turno
deixa de ser invisível.

O que este diálogo **não** faz
------------------------------

Não oferece lista de logins. Um combo com os nomes da loja entrega a um
estranho quais contas existem antes de ele tentar o primeiro PIN — e o ganho
seria digitar quatro letras. O diálogo de **autorização** (`dialogs.py`) lista,
e isso é diferente: lá quem está na frente da tela já é um operador
identificado, e a lista é curta por definição.

Não tem "lembrar de mim" nem botão de cancelar que abra o caixa mesmo assim.
Fechar o diálogo fecha o PDV: um caixa aberto sem ninguém identificado é pior
que um caixa fechado.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from pdv.domain.errors import AuthorizationRequiredError
from pdv.services.authorization import AuthorizationService, Identity
from pdv.ui import theme


class LoginDialog(QDialog):
    """Pede login e PIN antes de abrir o caixa."""

    def __init__(
        self,
        authorization: AuthorizationService,
        *,
        store_name: str,
        demo_hint: bool = False,
        parent=None,  # noqa: ANN001
    ) -> None:
        super().__init__(parent)
        self._auth = authorization
        self.identity: Identity | None = None

        self.setWindowTitle("Abrir o caixa")
        self.setModal(True)
        self.setMinimumWidth(380)
        # Sem o botão de fechar no título: sair daqui é sair do PDV, e isso
        # passa pelo "Sair", que diz o que vai acontecer.
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            theme.SPACE_5, theme.SPACE_5, theme.SPACE_5, theme.SPACE_5
        )
        layout.setSpacing(theme.SPACE_3)

        title = QLabel(store_name)
        title.setFont(theme.font(theme.SIZE_TITLE, theme.WEIGHT_SEMIBOLD, display=True))
        layout.addWidget(title)

        subtitle = QLabel("Identifique-se para abrir o caixa.")
        subtitle.setObjectName("hint")
        layout.addWidget(subtitle)
        layout.addSpacing(theme.SPACE_2)

        form = QFormLayout()
        form.setSpacing(theme.SPACE_2)
        self._login = QLineEdit()
        self._login.setMinimumHeight(40)
        self._login.setMaxLength(64)
        self._pin = QLineEdit()
        self._pin.setMinimumHeight(40)
        self._pin.setMaxLength(12)
        self._pin.setEchoMode(QLineEdit.EchoMode.Password)
        # O PIN é só de dígitos; o teclado numérico do balcão é o que se usa.
        self._pin.setInputMethodHints(Qt.InputMethodHint.ImhDigitsOnly)
        form.addRow("Login", self._login)
        form.addRow("PIN", self._pin)
        layout.addLayout(form)

        if demo_hint:
            # Só em terminal NÃO ativado. Ativado, estes logins não existem
            # neste computador (ver `main.open_database`), e mostrá-los seria
            # mandar o operador tentar uma senha que não vai funcionar.
            demo = QLabel(
                "Modo demonstração — entre com <b>ana</b> / <b>705284</b> "
                "(caixa) ou <b>olivia</b> / <b>84627519</b> (proprietária)."
            )
            demo.setObjectName("hint")
            demo.setWordWrap(True)
            demo.setTextFormat(Qt.TextFormat.RichText)
            layout.addWidget(demo)

        self._error = QLabel("")
        self._error.setWordWrap(True)
        self._error.setStyleSheet(f"color: {theme.DANGER};")
        self._error.setMinimumHeight(34)
        layout.addWidget(self._error)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Close
        )
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Entrar")
        ok.setObjectName("primary")
        ok.setMinimumHeight(44)
        close = self._buttons.button(QDialogButtonBox.StandardButton.Close)
        close.setText("Sair")
        close.setMinimumHeight(44)
        self._buttons.accepted.connect(self._try_login)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self._login.returnPressed.connect(self._pin.setFocus)
        self._pin.returnPressed.connect(self._try_login)

        #: Conta regressiva do bloqueio. Um botão desabilitado sem explicação
        #: faz o operador achar que o sistema travou e reiniciar a máquina —
        #: que é justamente o que o freio persistente existe para não premiar.
        self._countdown = QTimer(self)
        self._countdown.setInterval(1000)
        self._countdown.timeout.connect(self._tick)

    # -- ciclo ---------------------------------------------------------------- #

    def _try_login(self) -> None:
        login = self._login.text().strip()
        pin = self._pin.text()
        if not login or not pin:
            self._error.setText("Informe o login e o PIN.")
            return

        try:
            self.identity = self._auth.authenticate(login, pin)
        except AuthorizationRequiredError as exc:
            self.identity = None
            self._error.setText(str(exc))
            # O PIN errado nunca fica no campo: o próximo Enter tentaria o
            # mesmo valor e gastaria mais uma tentativa do freio.
            self._pin.clear()
            self._pin.setFocus()
            if self._auth.lock_status(login) > 0:
                self._start_countdown(login)
            return

        self.accept()

    def _start_countdown(self, login: str) -> None:
        self._locked_login = login
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self._countdown.start()
        self._tick()

    def _tick(self) -> None:
        remaining = self._auth.lock_status(self._locked_login)
        if remaining <= 0:
            self._countdown.stop()
            self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
            self._error.setText("Pode tentar de novo.")
            return
        self._error.setText(
            f"Muitas tentativas. Aguarde {remaining}s.\n"
            "Se não foi você, avise o gerente."
        )

    # -- API ------------------------------------------------------------------ #

    @classmethod
    def ask(
        cls,
        authorization: AuthorizationService,
        *,
        store_name: str,
        demo_hint: bool = False,
        parent=None,  # noqa: ANN001
    ) -> Identity | None:
        """Mostra o diálogo. Devolve quem entrou, ou `None` se desistiu."""
        dialog = cls(
            authorization, store_name=store_name, demo_hint=demo_hint, parent=parent
        )
        dialog.exec()
        return dialog.identity


__all__ = ["LoginDialog"]
