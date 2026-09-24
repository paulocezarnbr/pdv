"""Resultado da instalação, legível por quem está no balcão.

Antes era um `QMessageBox` com uma frase e o relatório inteiro escondido em
"Mostrar detalhes" — em texto corrido, com `[AVISO]` no meio de `[INFO]`. O
lojista clicava OK sem abrir, e a pendência que impedia a venda por peso só
aparecia no primeiro cliente com bolo na balança.

Aqui cada verificação é uma linha com a cor do estado e, quando há algo a fazer,
o que fazer logo embaixo. O texto vem do mesmo relatório que vai para o
`setup.log`: a janela lê, não recalcula — duas versões do resultado divergiriam.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from pdv.ui import theme

#: Estado de cada linha, na ordem em que a cor chama atenção.
_TAGS: dict[str, str] = {
    "[FALHA]": "failed",
    "[AVISO]": "warning",
    "[ OK ]": "ok",
    "[INFO]": "info",
}

_COLORS = {
    "failed": theme.DANGER,
    "warning": theme.WARN,
    "ok": theme.OK,
    "info": theme.TEXT_FAINT,
}

_HEADLINES = {
    0: ("Pronto para vender", "Tudo o que foi verificado está funcionando.", theme.OK),
    2: (
        "Instalado, com pendências",
        "O PDV já abre e vende. Resolva os itens em amarelo quando puder.",
        theme.WARN,
    ),
    3: (
        "Ainda não é possível vender",
        "Resolva os itens em vermelho antes de abrir o caixa.",
        theme.DANGER,
    ),
}


@dataclass(frozen=True, slots=True)
class ReportLine:
    status: str
    text: str
    remedy: str = ""


def parse_report(report: str) -> list[ReportLine]:
    """Transforma o relatório em linhas com estado e remédio.

    Linha sem marcador conhecido vira `info` em vez de sumir: um texto novo no
    relatório que esta tela não conhece ainda precisa aparecer.
    """
    lines: list[ReportLine] = []
    for raw in report.splitlines():
        text = raw.strip()
        if not text or text.endswith(":") and not text.startswith("["):
            continue
        if text.startswith("→") and lines:
            last = lines[-1]
            remedy = text.lstrip("→ ").strip()
            lines[-1] = ReportLine(
                last.status, last.text, f"{last.remedy} {remedy}".strip()
            )
            continue
        for tag, status in _TAGS.items():
            if text.startswith(tag):
                lines.append(ReportLine(status, text[len(tag):].strip()))
                break
        else:
            lines.append(ReportLine("info", text))
    return lines


class SetupResultDialog(QDialog):
    """A última tela da instalação."""

    def __init__(
        self,
        exit_code: int,
        report: str,
        *,
        log_path: Path | None = None,
        demo_logins: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._report = report
        self.setWindowTitle("Instalação do PDV Balcão")
        self.setMinimumSize(620, 460)

        headline, subtitle, color = _HEADLINES.get(exit_code, _HEADLINES[3])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.SPACE_5, theme.SPACE_5, theme.SPACE_5, theme.SPACE_4)
        layout.setSpacing(theme.SPACE_3)

        title = QLabel(headline)
        title.setFont(theme.font(theme.SIZE_TITLE, theme.WEIGHT_SEMIBOLD, display=True))
        title.setStyleSheet(f"color: {color};")
        layout.addWidget(title)
        self.headline = headline

        sub = QLabel(subtitle)
        sub.setObjectName("hint")
        sub.setWordWrap(True)
        layout.addWidget(sub)

        rows = QWidget()
        rows_layout = QVBoxLayout(rows)
        rows_layout.setContentsMargins(0, 0, 0, 0)
        rows_layout.setSpacing(theme.SPACE_2)
        self.lines = parse_report(report)
        for line in self.lines:
            rows_layout.addWidget(_row(line))
        rows_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(rows)
        layout.addWidget(scroll, stretch=1)

        if demo_logins:
            demo = QLabel(
                "<b>Modo demonstração</b> — o terminal ainda não foi ativado. "
                "Para experimentar, entre com <b>olivia</b> / <b>84627519</b> "
                "(proprietária) ou <b>ana</b> / <b>705284</b> (caixa). "
                "Ao ativar, estes logins deixam de existir neste terminal."
            )
            demo.setWordWrap(True)
            demo.setTextFormat(Qt.TextFormat.RichText)
            demo.setStyleSheet(
                f"border: 1px solid {theme.LINE_STRONG}; border-radius: "
                f"{theme.RADIUS_CONTROL}px; padding: 10px 12px;"
            )
            layout.addWidget(demo)

        if log_path is not None:
            where = QLabel(f"Relatório completo: {log_path}")
            where.setObjectName("hint")
            where.setWordWrap(True)
            where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(where)

        footer = QHBoxLayout()
        footer.addStretch(1)
        copy = QPushButton("Copiar relatório")
        copy.setMinimumHeight(40)
        copy.clicked.connect(self._copy)
        done = QPushButton("Concluir")
        done.setObjectName("primary")
        done.setMinimumHeight(40)
        done.setDefault(True)
        done.clicked.connect(self.accept)
        footer.addWidget(copy)
        footer.addWidget(done)
        layout.addLayout(footer)

    def _copy(self) -> None:
        # Para colar no chamado de suporte: é o texto do log, sem as cores.
        QGuiApplication.clipboard().setText(self._report)


def _row(line: ReportLine) -> QWidget:
    frame = QFrame()
    frame.setObjectName("inset")
    box = QHBoxLayout(frame)
    box.setContentsMargins(theme.SPACE_3, theme.SPACE_2, theme.SPACE_3, theme.SPACE_2)
    box.setSpacing(theme.SPACE_3)

    dot = QLabel("●")
    dot.setStyleSheet(f"color: {_COLORS[line.status]};")
    dot.setAlignment(Qt.AlignmentFlag.AlignTop)
    box.addWidget(dot)

    texts = QVBoxLayout()
    texts.setSpacing(2)
    main = QLabel(line.text)
    main.setWordWrap(True)
    if line.status == "info":
        main.setObjectName("hint")
    texts.addWidget(main)
    if line.remedy:
        remedy = QLabel(f"O que fazer: {line.remedy}")
        remedy.setWordWrap(True)
        remedy.setStyleSheet(f"color: {_COLORS[line.status]};")
        texts.addWidget(remedy)
    box.addLayout(texts, stretch=1)
    return frame


__all__ = ["ReportLine", "SetupResultDialog", "parse_report"]
