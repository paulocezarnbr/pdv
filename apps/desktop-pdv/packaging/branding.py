"""Gera o ícone do PDV e as imagens do instalador a partir do tema.

Roda no build (`build.ps1`, antes do PyInstaller), e os arquivos gerados não são
versionados: são derivados de `pdv/ui/theme.py`, e uma cópia binária no
repositório envelheceria no dia em que a paleta mudasse. Sem ícone, o
`PDV.exe`, o atalho e o instalador saíam com o ícone genérico do Windows — o
mesmo de qualquer executável baixado da internet, na tela em que a SmartScreen
já diz "editor desconhecido".

Desenho só com formas, sem texto: fonte depende da máquina de build, e um
ícone que muda de letra conforme o runner não é marca.

    python packaging/branding.py [pasta_de_saída]
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QBrush,
    QColor,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)

from pdv.ui import theme  # noqa: E402
from pdv.ui.brand import draw_mark  # noqa: E402

#: Tamanhos embutidos no .ico. O Windows escolhe o mais próximo para cada lugar
#: (barra de tarefas, atalho, Alt+Tab, Painel de Controle).
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

#: Imagens do assistente do Inno Setup (estilo moderno), a 100%, 150% e 200%
#: de escala. O Inno escolhe a que casa com o DPI da tela.
WIZARD_LARGE = ((164, 314), (246, 471), (328, 628))
WIZARD_SMALL = ((55, 55), (83, 83), (110, 110))


def render_icon_png(size: int) -> bytes:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    # Margem mínima nos tamanhos pequenos: a 16 px cada pixel de borda conta.
    margin = 0 if size <= 24 else size * 0.04
    draw_mark(painter, QRectF(margin, margin, size - 2 * margin, size - 2 * margin))
    painter.end()
    return _png_bytes(image)


def build_ico(sizes: tuple[int, ...] = ICO_SIZES) -> bytes:
    """Um .ico com uma imagem PNG por tamanho (formato aceito desde o Vista)."""
    images = [render_icon_png(size) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for size, data in zip(sizes, images):
        dimension = 0 if size >= 256 else size  # 0 significa 256 no formato ICO
        entries += struct.pack(
            "<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(data), offset
        )
        offset += len(data)
    return header + entries + b"".join(images)


def render_wizard_large(width: int, height: int) -> QImage:
    """Faixa lateral das páginas de boas-vindas e de conclusão."""
    image = QImage(width, height, QImage.Format.Format_RGB888)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    gradient = QLinearGradient(0, 0, 0, height)
    gradient.setColorAt(0.0, QColor(theme.SURFACE_RAISED))
    gradient.setColorAt(1.0, QColor(theme.CANVAS))
    painter.fillRect(0, 0, width, height, QBrush(gradient))

    # Anéis discretos atrás da marca: dão profundidade sem competir com ela.
    center = QPointF(width / 2, height * 0.36)
    for index, radius in enumerate((0.62, 0.48, 0.34)):
        color = QColor(theme.ACCENT)
        color.setAlpha(28 + index * 14)
        painter.setPen(QPen(color, max(1.0, width * 0.008)))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        r = width * radius
        painter.drawEllipse(center, r, r)

    mark = width * 0.46
    draw_mark(painter, QRectF(center.x() - mark / 2, center.y() - mark / 2, mark, mark))

    # Três barras no rodapé, na cor dos estados da balança: a mesma linguagem
    # visual que o operador vai encontrar no caixa.
    bar_h = max(2.0, height * 0.008)
    y = height * 0.9
    span = width * 0.56
    x = (width - span) / 2
    for color in (theme.OK, theme.WARN, theme.ACCENT):
        painter.fillRect(QRectF(x, y, span / 3 - width * 0.02, bar_h), QColor(color))
        x += span / 3
    painter.end()
    return image


def render_wizard_small(width: int, height: int) -> QImage:
    """Ícone no canto do cabeçalho das páginas internas (fundo claro do Inno)."""
    image = QImage(width, height, QImage.Format.Format_RGB888)
    image.fill(QColor("#ffffff"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    margin = width * 0.06
    draw_mark(painter, QRectF(margin, margin, width - 2 * margin, height - 2 * margin))
    painter.end()
    return image


def _png_bytes(image: QImage) -> bytes:
    buffer = QByteArray()
    device = QBuffer(buffer)
    device.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(device, "PNG")
    device.close()
    return bytes(buffer.data())


def generate(output: Path) -> list[Path]:
    """Gera todos os arquivos e devolve os caminhos."""
    _app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])  # noqa: F841
    output.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    ico = output / "pdv.ico"
    ico.write_bytes(build_ico())
    written.append(ico)

    for scale, (width, height) in zip((100, 150, 200), WIZARD_LARGE):
        path = output / f"wizard-large-{scale}.bmp"
        if not render_wizard_large(width, height).save(str(path), "BMP"):
            raise RuntimeError(f"Não foi possível gravar {path}")
        written.append(path)
    for scale, (width, height) in zip((100, 150, 200), WIZARD_SMALL):
        path = output / f"wizard-small-{scale}.bmp"
        if not render_wizard_small(width, height).save(str(path), "BMP"):
            raise RuntimeError(f"Não foi possível gravar {path}")
        written.append(path)
    return written


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "packaging" / "assets"
    for item in generate(target):
        print(f"  {item.name:<24} {item.stat().st_size:>8} bytes")
