"""Ativação do terminal, numa tela só — no instalador e no caixa.

Antes a ativação era um `QInputDialog` pedindo só o código, e o endereço da
retaguarda vinha de `PDV_CLOUD_URL` — que nenhuma instalação define. O terminal
tentava ativar contra o endereço de exemplo do código e falhava sempre, com uma
mensagem de rede que não dizia o que fazer. Aqui o lojista informa as duas
coisas que o painel mostra: o endereço que ele abre no navegador e o código.

O diálogo coleta e mostra; quem ativa é a função recebida (`activate_fn`), que
é o `pdv.provisioning.activation.activate` de verdade. A chamada de rede roda
numa thread à parte: são até 20 s de espera, e o Windows marca como "Não
respondendo" uma janela que para de processar eventos por 5 s — o reflexo do
lojista diante disso é fechar e tentar de novo, queimando um código válido.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pdv.domain.errors import PdvError
from pdv.provisioning.activation import (
    ActivationResult,
    display_server_url,
    normalize_code,
    normalize_server_url,
)
from pdv.ui import theme

#: `activate_fn(endereço_da_api, código) -> ActivationResult`.
ActivateFn = Callable[[str, str], ActivationResult]


class ActivationDialog(QDialog):
    """Pede endereço e código, ativa, e só fecha com sucesso ou "depois"."""

    def __init__(
        self,
        activate_fn: ActivateFn,
        *,
        server_url: str = "",
        warning: str = "",
        parent: QWidget | None = None,
    ) -> None:
        """
        Args:
            server_url: endereço já conhecido (gravado ou da linha de comando).
            warning: aviso mostrado antes de ativar — o caixa usa para dizer
                que os dados de demonstração vão ser arquivados.
        """
        super().__init__(parent)
        self._activate_fn = activate_fn
        self.result_value: ActivationResult | None = None
        self._worker: threading.Thread | None = None
        self._outcome: tuple[ActivationResult | None, str] | None = None

        self.setWindowTitle("Ativar este terminal")
        self.setModal(True)
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.SPACE_5, theme.SPACE_5, theme.SPACE_5, theme.SPACE_4)
        layout.setSpacing(theme.SPACE_3)

        title = QLabel("Ativar este terminal")
        title.setFont(theme.font(theme.SIZE_TITLE, theme.WEIGHT_SEMIBOLD, display=True))
        layout.addWidget(title)

        steps = QLabel(
            "1. No painel da retaguarda, abra <b>Terminais</b> e gere um "
            "<b>código de ativação</b>.<br>"
            "2. Digite abaixo o endereço do painel e o código. O código vale "
            "15 minutos e serve uma única vez."
        )
        steps.setObjectName("hint")
        steps.setWordWrap(True)
        steps.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(steps)

        if warning:
            notice = QLabel(warning)
            notice.setWordWrap(True)
            notice.setObjectName("notice")
            notice.setStyleSheet(
                f"QLabel#notice {{ color: {theme.WARN}; border: 1px solid {theme.WARN};"
                f" border-radius: {theme.RADIUS_CONTROL}px; padding: 8px 10px; }}"
            )
            layout.addWidget(notice)

        form = QFormLayout()
        form.setSpacing(theme.SPACE_2)
        self._server = QLineEdit(display_server_url(server_url))
        self._server.setPlaceholderText("painel.minhaloja.com.br")
        self._server.setMinimumHeight(40)
        self._code = QLineEdit()
        self._code.setPlaceholderText("A1B2-C3D4")
        self._code.setMinimumHeight(40)
        self._code.setMaxLength(40)
        self._code.setFont(
            theme.font(theme.SIZE_TITLE, theme.WEIGHT_MEDIUM, mono=True, tracking=3.0)
        )
        # Digitado em minúscula aparece em maiúscula: é como o painel mostra, e
        # o lojista confere caractere por caractere com o que está na tela.
        self._code.textEdited.connect(self._uppercase)
        form.addRow("Endereço do painel", self._server)
        form.addRow("Código de ativação", self._code)
        layout.addLayout(form)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setMinimumHeight(38)
        layout.addWidget(self._status)

        buttons = QHBoxLayout()
        self._later = QPushButton("Ativar depois")
        self._later.setMinimumHeight(42)
        self._later.clicked.connect(self.reject)
        self._submit = QPushButton("Ativar")
        self._submit.setObjectName("primary")
        self._submit.setMinimumHeight(42)
        self._submit.setDefault(True)
        self._submit.clicked.connect(self._start)
        buttons.addWidget(self._later)
        buttons.addStretch(1)
        buttons.addWidget(self._submit)
        layout.addLayout(buttons)

        hint = QLabel("Sem ativar, o PDV funciona em modo demonstração e não sincroniza.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._poll = QTimer(self)
        self._poll.setInterval(100)
        self._poll.timeout.connect(self._check_worker)

        (self._code if self._server.text() else self._server).setFocus()

    # -- ações ---------------------------------------------------------------- #

    def _uppercase(self, text: str) -> None:
        position = self._code.cursorPosition()
        self._code.setText(text.upper())
        self._code.setCursorPosition(position)

    def _start(self) -> None:
        try:
            server = normalize_server_url(self._server.text())
            code = normalize_code(self._code.text())
        except PdvError as exc:
            self._show_error(str(exc))
            return

        self._set_busy(True)
        self._status.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        self._status.setText("Falando com a retaguarda…")
        self._outcome = None

        def work() -> None:
            try:
                self._outcome = (self._activate_fn(server, code), "")
            except PdvError as exc:
                self._outcome = (None, str(exc))
            except Exception as exc:  # noqa: BLE001 - vira mensagem, não crash
                self._outcome = (None, f"Falha inesperada na ativação: {exc}")

        self._worker = threading.Thread(target=work, name="pdv-activation", daemon=True)
        self._worker.start()
        self._poll.start()

    def _check_worker(self) -> None:
        if self._outcome is None:
            return
        self._poll.stop()
        result, error = self._outcome
        self._set_busy(False)
        if result is None:
            self._show_error(error)
            return
        self.result_value = result
        self.accept()

    def reject(self) -> None:  # noqa: D102 - override do Qt
        # Fechar no meio da chamada deixaria o código talvez consumido e o
        # resultado perdido. Espera-se a resposta.
        if self._poll.isActive():
            return
        super().reject()

    # -- apoio ---------------------------------------------------------------- #

    def _set_busy(self, busy: bool) -> None:
        for widget in (self._server, self._code, self._submit, self._later):
            widget.setEnabled(not busy)

    def _show_error(self, message: str) -> None:
        self._status.setStyleSheet(f"color: {theme.DANGER};")
        self._status.setText(message)
        self._code.setFocus()

    @property
    def busy(self) -> bool:
        return self._poll.isActive()


__all__ = ["ActivateFn", "ActivationDialog"]
