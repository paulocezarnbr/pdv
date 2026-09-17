# -*- mode: python ; coding: utf-8 -*-
"""Spec do PyInstaller para o PDV de Balcão.

Modo **onedir** e não onefile, de propósito:

* Onefile extrai tudo para `%TEMP%` a cada execução — um diretório onde o
  operador tem permissão total de escrita. Isso **anula** o endurecimento de
  ACL do instalador: bastaria trocar um .pyd no temp entre a extração e a
  carga. Onedir mantém os binários em Program Files, protegidos pela ACL.
* Onefile também abre o caixa com 3–8 s de atraso a cada início por causa da
  extração. Num PDV isso é inaceitável.

Build:
    pyinstaller packaging/pdv.spec --noconfirm --clean
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

APP_NAME = "PDV"
ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH é injetado pelo PyInstaller
SRC = ROOT / "src"

# O schema precisa viajar junto: `database.py` o lê por caminho relativo ao
# módulo (`Path(__file__).with_name("schema.sql")`). Sem esta linha, o app
# instalado sobe e falha na primeira migration — e só em produção.
datas = [
    (str(SRC / "pdv" / "data" / "schema.sql"), "pdv/data"),
]

hiddenimports = [
    # Backends de impressão resolvidos por import tardio dentro de funções:
    # o analisador estático do PyInstaller não os enxerga.
    "win32print",
    "win32api",
    # Driver serial e seus backends por plataforma.
    "serial",
    "serial.tools.list_ports",
    *collect_submodules("serial"),
]

# Corta o que não é usado no terminal de caixa. Cada exclusão reduz o tamanho
# do pacote e, mais importante, a superfície de ataque instalada na loja.
excludes = [
    "tkinter",
    "unittest",
    "pytest",
    "pydoc",
    "doctest",
    "test",
    "distutils",
    "setuptools",
    "pip",
    "numpy",
    "matplotlib",
    "PIL",
    # Módulos do Qt que o PDV não usa — WebEngine sozinho pesa ~130 MB.
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtQuick",
    "PySide6.QtQml",
    "PySide6.Qt3DCore",
    "PySide6.QtMultimedia",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtBluetooth",
    "PySide6.QtPositioning",
    "PySide6.QtDesigner",
    "PySide6.QtTest",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "main.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    # optimize=2 aplica -OO: remove asserts E **docstrings** dos módulos
    # empacotados. Neste projeto as docstrings carregam a explicação das regras
    # de negócio e do modelo de ameaça; não há motivo para embarcá-las no
    # binário entregue ao cliente.
    optimize=2,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX dispara heurística de antivírus; num PDV isso é suporte na madrugada
    console=False,  # app gráfico: sem janela de console
    disable_windowed_traceback=True,  # traceback vai para o log, não para a tela do cliente
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "packaging" / "pdv.ico")
    if (ROOT / "packaging" / "pdv.ico").exists()
    else None,
    version=str(ROOT / "packaging" / "version_info.txt")
    if (ROOT / "packaging" / "version_info.txt").exists()
    else None,
    # O app roda como usuário comum. Exigir admin para VENDER seria péssimo:
    # cada abertura de caixa viraria um prompt de UAC para o operador clicar
    # no automático — e usuário treinado a aprovar UAC é uma brecha, não uma
    # proteção. Admin é exigido apenas na instalação e na atualização.
    uac_admin=False,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
