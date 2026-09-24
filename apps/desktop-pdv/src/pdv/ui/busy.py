"""Espera visível para trabalho de rede antes de o caixa abrir.

Chamada de rede na thread da tela congela a janela, e o Windows a marca como
"Não respondendo" em 5 s. Quem está no balcão fecha o programa nessa hora — no
meio de uma sincronização que ia dar certo. Aqui o trabalho roda numa thread à
parte e a tela mostra o que está acontecendo.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TypeVar

from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout, QWidget

from pdv.ui import theme

T = TypeVar("T")


def run_with_progress(
    message: str, work: Callable[[], T], *, parent: QWidget | None = None
) -> T:
    """Roda `work` fora da thread da tela e devolve o resultado (ou relança)."""
    dialog = QDialog(parent)
    dialog.setWindowTitle("PDV Balcão")
    dialog.setModal(True)
    dialog.setMinimumWidth(360)
    dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(theme.SPACE_5, theme.SPACE_5, theme.SPACE_5, theme.SPACE_5)
    label = QLabel(message)
    label.setWordWrap(True)
    label.setFont(theme.font(theme.SIZE_BODY_LG, theme.WEIGHT_MEDIUM))
    layout.addWidget(label)
    hint = QLabel("Isto leva alguns segundos. Não feche o programa.")
    hint.setObjectName("hint")
    layout.addWidget(hint)

    outcome: dict[str, object] = {}

    def target() -> None:
        try:
            outcome["value"] = work()
        except BaseException as exc:  # noqa: BLE001 - relançado na thread da tela
            outcome["error"] = exc

    worker = threading.Thread(target=target, name="pdv-busy", daemon=True)
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(80)
    timer.timeout.connect(lambda: None if worker.is_alive() else loop.quit())

    dialog.show()
    worker.start()
    timer.start()
    loop.exec()
    timer.stop()
    dialog.close()

    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    return outcome["value"]  # type: ignore[return-value]


__all__ = ["run_with_progress"]
