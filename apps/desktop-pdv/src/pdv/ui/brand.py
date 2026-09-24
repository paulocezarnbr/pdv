"""A marca do PDV, desenhada em código.

Um só desenho para três lugares: o ícone da janela (aqui, em tempo de
execução), o `.ico` do executável e as imagens do instalador
(`packaging/branding.py`, no build). Sem arquivo de imagem no pacote — o ícone
não tem como faltar no PyInstaller, e não envelhece quando a paleta muda.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

from pdv.ui import theme


def draw_mark(painter: QPainter, rect: QRectF) -> None:
    """A marca: um cupom sobre um quadrado arredondado azul-aço.

    O cupom é o objeto que todo cliente de balcão reconhece. A borda serrilhada
    embaixo é o que o distingue de "documento" genérico mesmo a 16 px.
    """
    size = min(rect.width(), rect.height())
    x0 = rect.x() + (rect.width() - size) / 2
    y0 = rect.y() + (rect.height() - size) / 2
    badge = QRectF(x0, y0, size, size)

    gradient = QLinearGradient(badge.topLeft(), badge.bottomRight())
    gradient.setColorAt(0.0, QColor(theme.ACCENT_HOVER))
    gradient.setColorAt(1.0, QColor(theme.ACCENT_PRESSED))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(gradient))
    painter.drawRoundedRect(badge, size * 0.22, size * 0.22)

    # Cupom
    w = size * 0.50
    h = size * 0.62
    left = x0 + (size - w) / 2
    top = y0 + size * 0.16
    teeth = 5 if size >= 32 else 3
    tooth = w / teeth
    path = QPainterPath(QPointF(left, top))
    path.lineTo(left + w, top)
    path.lineTo(left + w, top + h)
    for i in range(teeth):
        right = left + w - i * tooth
        path.lineTo(right - tooth / 2, top + h - size * 0.06)
        path.lineTo(right - tooth, top + h)
    path.closeSubpath()
    painter.setBrush(QColor(theme.TEXT))
    painter.drawPath(path)

    # Linhas do cupom: duas curtas e o total, mais grosso.
    ink = QColor(theme.ACCENT_PRESSED)
    stroke = max(1.0, size * 0.045)
    painter.setPen(QPen(ink, stroke, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    inset = w * 0.18
    for fraction, length in ((0.22, 0.64), (0.40, 0.46)):
        y = top + h * fraction
        painter.drawLine(QPointF(left + inset, y), QPointF(left + inset + (w - 2 * inset) * length, y))
    painter.setPen(QPen(ink, stroke * 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    y = top + h * 0.66
    painter.drawLine(QPointF(left + inset, y), QPointF(left + w - inset, y))


def app_icon() -> QIcon:
    """Ícone da janela e da barra de tarefas, em vários tamanhos."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        draw_mark(painter, QRectF(0, 0, size, size))
        painter.end()
        icon.addPixmap(pixmap)
    return icon


__all__ = ["app_icon", "draw_mark"]
