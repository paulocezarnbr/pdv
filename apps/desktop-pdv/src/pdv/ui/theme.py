"""Sistema visual do PDV — tokens, fontes e folha de estilo.

Por que existe um tema e não estilos espalhados pelos widgets
-------------------------------------------------------------

Até aqui cada tela escolhia a própria cor com hexadecimal literal e o próprio
tamanho de fonte no construtor. Isso tem três consequências que só aparecem
em produção:

1. **O terminal não era igual em duas lojas.** Sem `setStyle` e sem paleta
   fixada, o Qt herda o tema do Windows. O mesmo PDV ficava escuro numa
   máquina e claro na outra, e as garantias de contraste — que é o que permite
   ler o peso de pé, a um metro do balcão, sob lâmpada fria — evaporavam na
   máquina que ninguém testou.
2. **Não havia estado.** Botão sem `:hover`, sem `:pressed` e sem anel de foco
   num sistema operado por teclado deixa o operador sem saber onde está o
   cursor. Numa fila isso vira item lançado na tela errada.
3. **Número não alinhava.** Coluna de dinheiro em fonte proporcional não
   alinha na vírgula, e conferir uma venda de oito itens passa a exigir leitura
   dígito a dígito.

Onde este tema se afasta das convenções de design de web
--------------------------------------------------------

Um PDV não é uma landing page, e três recomendações comuns de "premium" são
prejuízo aqui:

* **Macro-whitespace.** Dobrar o respiro custa linhas visíveis da venda. Esta
  é uma superfície operacional densa de propósito; o espaçamento serve para
  agrupar, não para impressionar.
* **Animação de entrada e rolagem.** O caixa precisa de resposta instantânea.
  Meio segundo de transição entre "peso estável" e "item registrado" é tempo
  em que o operador não sabe se pode tirar a mercadoria da balança. Só há
  movimento onde ele é resposta a um toque (`:hover`, `:pressed`).
* **Assimetria e grid quebrado.** O olho do operador precisa cair no mesmo
  lugar milhares de vezes por dia. Simetria previsível aqui é ergonomia, não
  falta de ousadia.

O que foi adotado: um único acento, família de cinza única, preto que não é
preto, escala tipográfica com pesos intermediários, algarismos tabulares e
estados completos em tudo que é clicável.

A escolha do acento
-------------------

Verde, âmbar e vermelho **já têm significado** nesta tela: são os estados da
balança (estável, instável, sobrecarga/erro). Usar qualquer um deles como cor
de marca faria um botão parecer um aviso. O acento é um azul-aço dessaturado,
que não compete com nenhum estado e nunca significa situação — só "aqui é onde
você age" e "aqui está o foco".
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

# --------------------------------------------------------------------------- #
# Cores
# --------------------------------------------------------------------------- #

#: Fundo da janela. Não é `#000000`: preto absoluto num monitor de balcão
#: espelha a luminária do teto e transforma a tela em espelho.
CANVAS = "#0e1116"
SURFACE = "#161a21"
SURFACE_RAISED = "#1d222b"
SURFACE_SUNKEN = "#11151b"
"""Campos de entrada — afundados em vez de elevados, para dizer 'digite aqui'."""

LINE = "#272d38"
LINE_STRONG = "#39434f"

TEXT = "#e7eaef"
TEXT_MUTED = "#98a1b0"
TEXT_FAINT = "#69727f"

#: Único acento do sistema. Nunca significa estado (ver docstring do módulo).
ACCENT = "#4f8fc0"
ACCENT_HOVER = "#5da0d2"
ACCENT_PRESSED = "#3f7aa8"
ON_ACCENT = "#0b0f14"

#: Cores semânticas. Estas **são** estado, e por isso não aparecem em botão.
OK = "#3fae6e"
WARN = "#d99a2b"
DANGER = "#e0524f"

#: Realce de seleção em tabela: o acento a 22% mantém o texto legível por cima,
#: o que uma seleção sólida do sistema não garante.
SELECTION = "#2b4a63"

# --------------------------------------------------------------------------- #
# Tipografia
# --------------------------------------------------------------------------- #

#: Segoe UI Variable (Windows 11) tem eixo óptico e pesos intermediários reais;
#: o Segoe UI comum só entrega Regular/Semibold/Bold. A lista é uma cadeia de
#: fallback — num Windows 10 a primeira família simplesmente não resolve.
FAMILY_UI = ["Segoe UI Variable Text", "Segoe UI", "Noto Sans", "sans-serif"]

#: Títulos grandes usam o corte *Display*, desenhado para corpo alto: hastes
#: mais finas e espaçamento mais fechado que o corte de texto.
FAMILY_DISPLAY = ["Segoe UI Variable Display", "Segoe UI", "Noto Sans", "sans-serif"]

#: Dinheiro, peso e quantidade. Monoespaçada porque o que importa é a vírgula
#: cair na mesma coluna em todas as linhas.
FAMILY_MONO = ["Cascadia Mono", "Consolas", "Noto Sans Mono", "monospace"]

#: Escala tipográfica. Os saltos são grandes de propósito: hierarquia de PDV se
#: lê em pé, de relance, não de perto.
SIZE_MICRO = 9
SIZE_LABEL = 10
SIZE_BODY = 11
SIZE_BODY_LG = 13
SIZE_TITLE = 16
SIZE_TOTAL = 30
SIZE_DISPLAY = 54

WEIGHT_REGULAR = QFont.Weight.Normal
WEIGHT_MEDIUM = QFont.Weight.Medium
WEIGHT_SEMIBOLD = QFont.Weight.DemiBold
WEIGHT_BOLD = QFont.Weight.Bold

# --------------------------------------------------------------------------- #
# Espaçamento e raio
# --------------------------------------------------------------------------- #

SPACE_1 = 4
SPACE_2 = 8
SPACE_3 = 12
SPACE_4 = 16
SPACE_5 = 24

#: Raio menor por dentro, maior por fora: bordas concêntricas num painel só
#: ficam paralelas se o filho for mais fechado que o pai.
RADIUS_CONTROL = 6
RADIUS_PANEL = 10


def font(
    size: int = SIZE_BODY,
    weight: QFont.Weight = WEIGHT_REGULAR,
    *,
    mono: bool = False,
    display: bool = False,
    tracking: float = 0.0,
) -> QFont:
    """Monta uma fonte do sistema.

    `tracking` em px absolutos: positivo para rótulos pequenos em caixa alta
    (que sem ele viram um bloco cinza ilegível), negativo para números grandes.

    Os algarismos tabulares são ligados por feature OpenType (`tnum`) mesmo na
    fonte de interface, porque tabela de venda tem número em fonte de texto
    também — o total da coluna e o total do rodapé precisam alinhar entre si.
    """
    families = FAMILY_MONO if mono else (FAMILY_DISPLAY if display else FAMILY_UI)
    result = QFont()
    result.setFamilies(families)
    result.setPointSize(size)
    result.setWeight(weight)
    if tracking:
        result.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, tracking)
    _enable_tabular_figures(result)
    return result


def _enable_tabular_figures(target: QFont) -> None:
    """Liga `tnum`. Silencioso em Qt antigo — é melhoria, não requisito."""
    try:
        target.setFeature(QFont.Tag("tnum"), 1)
    except (AttributeError, TypeError):  # pragma: no cover - Qt < 6.7
        pass


# --------------------------------------------------------------------------- #
# Folha de estilo
# --------------------------------------------------------------------------- #

STYLESHEET = f"""
QWidget {{
    background: {CANVAS};
    color: {TEXT};
}}

QFrame#panel {{
    background: {SURFACE};
    border: 1px solid {LINE};
    border-radius: {RADIUS_PANEL}px;
}}

/* Caixa interna dentro de um painel: sem borda, só um degrau de superfície.
   Borda dentro de borda vira moldura de moldura e suja a leitura. */
QFrame#inset {{
    background: {SURFACE_RAISED};
    border: none;
    border-radius: {RADIUS_CONTROL}px;
}}

QLabel {{
    background: transparent;
}}

QLabel#sectionTitle {{
    color: {TEXT_FAINT};
}}

QLabel#hint {{
    color: {TEXT_MUTED};
}}

QLabel#error {{
    color: {DANGER};
}}

/* -- botões ------------------------------------------------------------- */

QPushButton {{
    background: {SURFACE_RAISED};
    color: {TEXT};
    border: 1px solid {LINE_STRONG};
    border-radius: {RADIUS_CONTROL}px;
    padding: 8px 14px;
}}

QPushButton:hover {{
    background: #242a34;
    border-color: {ACCENT};
}}

/* Sem `transform` no QSS: o afundamento é feito com a borda de cima mais
   escura, que dá a mesma leitura de tecla pressionada. */
QPushButton:pressed {{
    background: #11151b;
    border-color: {ACCENT_PRESSED};
    border-top-color: {CANVAS};
}}

QPushButton:focus {{
    border: 2px solid {ACCENT};
    padding: 7px 13px;
}}

QPushButton:disabled {{
    background: {SURFACE};
    color: {TEXT_FAINT};
    border-color: {LINE};
}}

QPushButton#primary {{
    background: {ACCENT};
    color: {ON_ACCENT};
    border: 1px solid {ACCENT};
}}

QPushButton#primary:hover {{
    background: {ACCENT_HOVER};
    border-color: {ACCENT_HOVER};
}}

QPushButton#primary:pressed {{
    background: {ACCENT_PRESSED};
    border-color: {ACCENT_PRESSED};
}}

QPushButton#primary:focus {{
    border: 2px solid {TEXT};
    padding: 7px 13px;
}}

/* Desabilitado tem de continuar parecendo o botão principal, só apagado: se
   virar um botão neutro qualquer, o operador procura o F10 e não acha. */
QPushButton#primary:disabled {{
    background: #24313c;
    color: {TEXT_FAINT};
    border-color: #24313c;
}}

/* -- entradas ----------------------------------------------------------- */

QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox, QAbstractSpinBox {{
    background: {SURFACE_SUNKEN};
    color: {TEXT};
    border: 1px solid {LINE_STRONG};
    border-radius: {RADIUS_CONTROL}px;
    padding: 6px 10px;
    selection-background-color: {SELECTION};
    selection-color: {TEXT};
}}

QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QAbstractSpinBox:focus {{
    border: 2px solid {ACCENT};
    padding: 5px 9px;
}}

QLineEdit:disabled, QComboBox:disabled, QAbstractSpinBox:disabled {{
    color: {TEXT_FAINT};
    border-color: {LINE};
}}

QLineEdit::placeholder {{
    color: {TEXT_FAINT};
}}

QComboBox::drop-down {{
    border: none;
    width: 28px;
}}

QComboBox QAbstractItemView {{
    background: {SURFACE_RAISED};
    border: 1px solid {LINE_STRONG};
    border-radius: {RADIUS_CONTROL}px;
    selection-background-color: {SELECTION};
    outline: none;
    padding: 4px;
}}

QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
    background: {SURFACE_RAISED};
    border: none;
    width: 18px;
}}

QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{
    background: {LINE_STRONG};
}}

/* -- tabelas e listas --------------------------------------------------- */

QTableWidget, QListWidget {{
    background: {SURFACE_SUNKEN};
    alternate-background-color: #141920;
    border: 1px solid {LINE};
    border-radius: {RADIUS_CONTROL}px;
    gridline-color: {LINE};
    outline: none;
}}

QTableWidget::item, QListWidget::item {{
    padding: 7px 10px;
    border: none;
}}

QTableWidget::item:selected, QListWidget::item:selected {{
    background: {SELECTION};
    color: {TEXT};
}}

QListWidget::item:hover {{
    background: {SURFACE_RAISED};
}}

QHeaderView::section {{
    background: {SURFACE};
    color: {TEXT_FAINT};
    border: none;
    border-bottom: 1px solid {LINE_STRONG};
    padding: 8px 10px;
}}

QTableCornerButton::section {{
    background: {SURFACE};
    border: none;
}}

/* A numeração de linha à esquerda não informa nada numa venda de balcão e
   ainda rouba largura da coluna do produto. */
QTableWidget QHeaderView::section:vertical {{
    background: {SURFACE_SUNKEN};
    color: {TEXT_FAINT};
    border-right: 1px solid {LINE};
}}

/* -- rolagem ------------------------------------------------------------ */

QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}

QScrollBar::handle:vertical {{
    background: {LINE_STRONG};
    border-radius: 5px;
    min-height: 32px;
}}

QScrollBar::handle:vertical:hover {{
    background: {TEXT_FAINT};
}}

QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
    width: 0;
}}

QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
}}

QScrollBar::handle:horizontal {{
    background: {LINE_STRONG};
    border-radius: 5px;
    min-width: 32px;
}}

/* -- diálogos e barra de status ----------------------------------------- */

QDialog {{
    background: {CANVAS};
}}

QStatusBar {{
    background: {SURFACE};
    border-top: 1px solid {LINE};
    color: {TEXT_MUTED};
}}

QStatusBar::item {{
    border: none;
}}

QStatusBar QLabel {{
    color: {TEXT_MUTED};
    padding: 0 10px;
    border-left: 1px solid {LINE};
}}

QToolTip {{
    background: {SURFACE_RAISED};
    color: {TEXT};
    border: 1px solid {LINE_STRONG};
    padding: 6px 8px;
}}

QMessageBox {{
    background: {SURFACE};
}}
"""


def apply_theme(app: QApplication) -> None:
    """Fixa o visual do terminal.

    `setStyle("Fusion")` não é preferência estética: o estilo nativo do Windows
    desenha os controles pelo tema do sistema e ignora boa parte da folha de
    estilo. Com ele, dois terminais da mesma loja — um com Windows em modo
    claro — mostrariam telas diferentes, e o contraste calculado aqui valeria
    só numa delas.
    """
    app.setStyle("Fusion")
    app.setPalette(_palette())
    app.setFont(font(SIZE_BODY))
    app.setStyleSheet(STYLESHEET)


def _palette() -> QPalette:
    """Paleta explícita para o que a folha de estilo não alcança.

    Menus, tooltips e diálogos nativos (`QMessageBox`, `QInputDialog`) pegam
    cor da paleta, não do QSS. Sem isto eles apareceriam claros no meio de uma
    tela escura — o "recorte de outro tema" que denuncia interface montada às
    pressas.
    """
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(CANVAS))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(SURFACE_SUNKEN))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(SURFACE))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(SURFACE_RAISED))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(SURFACE_RAISED))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(SELECTION))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Link, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(TEXT_FAINT))

    disabled = QPalette.ColorGroup.Disabled
    palette.setColor(disabled, QPalette.ColorRole.Text, QColor(TEXT_FAINT))
    palette.setColor(disabled, QPalette.ColorRole.ButtonText, QColor(TEXT_FAINT))
    palette.setColor(disabled, QPalette.ColorRole.WindowText, QColor(TEXT_FAINT))
    return palette


__all__ = [
    "ACCENT",
    "CANVAS",
    "DANGER",
    "LINE",
    "OK",
    "SURFACE",
    "SURFACE_RAISED",
    "TEXT",
    "TEXT_FAINT",
    "TEXT_MUTED",
    "WARN",
    "apply_theme",
    "font",
]
