# -*- mode: python ; coding: utf-8 -*-
"""Spec do PyInstaller para o PDV de Balcão.

Modo **onedir** e não onefile, de propósito:

* Onefile extrai tudo para `%TEMP%` a cada execução — um diretório onde o
  operador tem permissão total de escrita. Isso **anula** o endurecimento de
  ACL do instalador: bastaria trocar um .pyd no temp entre a extração e a
  carga. Onedir mantém os binários em Program Files, protegidos pela ACL.
* Onefile também abre o caixa com 3–8 s de atraso a cada início por causa da
  extração. Num PDV isso é inaceitável.

Dois executáveis, um único diretório:

* `PDV.exe` — o caixa, executado pelo operador.
* `PDVSetup.exe` — o assistente de primeira execução, chamado pelo instalador
  logo depois do endurecimento de ACL. Compartilham o mesmo `COLLECT`, então as
  DLLs do Qt e do Python entram no pacote **uma vez só**: dois onedir separados
  dobrariam os ~120 MB sem ganho nenhum.

Build:
    pyinstaller packaging/pdv.spec --noconfirm --clean
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

APP_NAME = "PDV"
SETUP_NAME = "PDVSetup"
ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH é injetado pelo PyInstaller
SRC = ROOT / "src"

# O schema precisa viajar junto: `database.py` o lê por caminho relativo ao
# módulo (`Path(__file__).with_name("schema.sql")`). Sem esta linha, o app
# instalado sobe e falha na primeira migration — e só em produção.
datas = [
    (str(SRC / "pdv" / "data" / "schema.sql"), "pdv/data"),
    # O app do garçom é um arquivo em disco, lido por caminho relativo ao
    # módulo (`webapp/__init__.py`), exatamente como o schema. Sem estas duas
    # linhas o pacote instala, o caixa abre, o celular pareia — e a primeira
    # tela do garçom é um 500. O defeito só aparece na loja, porque rodando do
    # código-fonte o arquivo está sempre lá.
    (str(SRC / "pdv" / "edge" / "webapp" / "index.html"), "pdv/edge/webapp"),
    # O SweetAlert2 vem junto pelo mesmo motivo que não vem de CDN: a loja
    # opera sem internet (ver a rota `/vendor` em `edge/server.py`).
    (str(SRC / "pdv" / "edge" / "webapp" / "vendor"), "pdv/edge/webapp/vendor"),
]
# Schemas e tabelas de endpoints do PyNFe são abertos por caminho em runtime.
datas += collect_data_files("pynfe")
# Mantém METADATA, autores e licença LGPL junto do executável distribuído.
# Código fechado pode usar a biblioteca, mas não pode apagar os direitos e
# avisos do componente open source que está sendo redistribuído.
datas += copy_metadata("PyNFe")

hiddenimports = [
    # Backends de impressão resolvidos por import tardio dentro de funções:
    # o analisador estático do PyInstaller não os enxerga.
    "win32print",
    "win32api",
    # DPAPI — usado pelo cofre de segredos via import tardio.
    "win32crypt",
    # Driver serial e seus backends por plataforma.
    "serial",
    "serial.tools.list_ports",
    *collect_submodules("serial"),
    # Servidor local do salao: o uvicorn resolve loop, protocolo e ciclo de vida
    # por string em tempo de execucao, entao o analisador estatico nao enxerga
    # nenhum deles. Sem estas linhas o pacote instala e o servidor morre ao subir.
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "uvicorn.logging",
    "websockets",
    "websockets.legacy",
    *collect_submodules("zeroconf"),
    # Argon2id da autorizacao de gerente. O `argon2-cffi` carrega a extensao
    # compilada por nome (`_argon2_cffi_bindings._ffi`) dentro de um import
    # tardio, entao o analisador estatico nao a enxerga. Sem estas linhas o
    # pacote instala, o caixa abre, e o gerente descobre que nao consegue
    # autorizar um cancelamento — na frente do cliente.
    "argon2",
    *collect_submodules("argon2"),
    "_argon2_cffi_bindings",
    "_argon2_cffi_bindings._ffi",
    # Certificado TLS do servidor do salão (`edge/tls.py`). A `cryptography`
    # é importada **dentro** das funções, de propósito — para que a ausência
    # dela derrube o salão para HTTP em vez de impedir o caixa de abrir. O
    # preço é que o analisador estático não a enxerga, e sem estas linhas o
    # pacote instalado nunca subiria em HTTPS: cairia no `except ImportError`
    # e ninguém notaria, porque a queda é silenciosa por construção.
    "cryptography",
    "cryptography.x509",
    "cryptography.hazmat.primitives.serialization",
    "cryptography.hazmat.primitives.asymmetric.ec",
    "cryptography.hazmat.bindings._rust",
    # O provedor fiscal é carregado apenas quando o tenant habilita NFC-e.
    # Incluí-lo explicitamente evita que a build do caixa funcione até o dia
    # da primeira emissão fiscal e então falhe por import tardio.
    "pynfe",
    "pynfe.entidades",
    "pynfe.processamento",
    "pynfe.processamento.comunicacao",
    "pynfe.processamento.serializacao",
    "pynfe.utils",
    "pynfe.utils.webservices",
    "signxml",
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

# Segunda análise: o assistente de instalação. Ponto de entrada diferente,
# mesma árvore de dependências — por isso as duas entram no mesmo COLLECT.
setup_analysis = Analysis(  # noqa: F821
    [str(ROOT / "setup_wizard.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=2,
)

pyz = PYZ(a.pure)  # noqa: F821
setup_pyz = PYZ(setup_analysis.pure)  # noqa: F821

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

setup_exe = EXE(  # noqa: F821
    setup_pyz,
    setup_analysis.scripts,
    [],
    exclude_binaries=True,
    name=SETUP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Sem console: durante a instalação uma janela preta piscando assusta o
    # lojista e não informa nada — o relatório sai numa caixa de diálogo e,
    # sempre, em ProgramData\ERPFood\PDV\logs\setup.log.
    console=False,
    disable_windowed_traceback=True,
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
    # Escreve só em ProgramData, onde o instalador já concedeu Modify ao grupo
    # Users. Pedir elevação aqui obrigaria o lojista a aprovar um segundo UAC
    # para redetectar a balança — e ele aprovaria sem ler.
    uac_admin=False,
)

coll = COLLECT(  # noqa: F821
    exe,
    setup_exe,
    a.binaries,
    a.datas,
    setup_analysis.binaries,
    setup_analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
