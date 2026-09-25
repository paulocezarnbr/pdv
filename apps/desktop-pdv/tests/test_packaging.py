"""Empacotamento — o que dá para conferir sem Windows.

O instalador bloqueia **downgrade** comparando a versão do `.iss` com a do
PDV já instalado. Isso só protege se a versão subir junto com o schema: um
binário antigo que encontra uma migration que não conhece abre sobre um banco
que ele não sabe ler. E a versão vive em três arquivos — divergir entre eles
produz um `PDV.exe` que diz uma versão em Propriedades enquanto o instalador
compara outra.
"""

from __future__ import annotations

import re
from pathlib import Path

PACKAGING = Path(__file__).resolve().parents[1] / "packaging"


def _iss_version() -> str:
    text = (PACKAGING / "installer.iss").read_text(encoding="utf-8")
    match = re.search(r'#define AppVersion\s+"(\d+\.\d+\.\d+)"', text)
    assert match, "installer.iss sem AppVersion"
    return match.group(1)


def test_the_three_version_sources_agree() -> None:
    version = _iss_version()
    four = f"{version}.0"
    tuple_form = "(" + ", ".join([*version.split("."), "0"]) + ")"

    info = (PACKAGING / "version_info.txt").read_text(encoding="utf-8")
    assert f"filevers={tuple_form}" in info
    assert f"prodvers={tuple_form}" in info
    assert f"'FileVersion', '{four}'" in info
    assert f"'ProductVersion', '{four}'" in info

    build = (PACKAGING / "build.ps1").read_text(encoding="utf-8")
    assert set(re.findall(r"--file-version=([\d.]+)", build)) == {four}
    assert set(re.findall(r"--product-version=([\d.]+)", build)) == {four}


def test_the_installer_version_moved_past_the_schema_that_needs_it() -> None:
    """Schema 14 (aceite presencial) entrou na 1.1.0.

    Sem subir a versão, um instalador 1.0.0 antigo passaria por cima de um PDV
    já migrado sem o aviso de downgrade.
    """
    from pdv.data.database import SCHEMA_VERSION

    major, minor, _ = (int(part) for part in _iss_version().split("."))
    assert SCHEMA_VERSION >= 14
    assert (major, minor) >= (1, 1)


def test_the_selftest_waits_for_the_gui_binary() -> None:
    """`& PDV.exe` não espera app gráfico no PowerShell.

    O autoteste passava sem ter rodado: o código lido era o do passo anterior.
    """
    build = (PACKAGING / "build.ps1").read_text(encoding="utf-8")
    assert "& $exePath --selftest" not in build
    assert "Start-Process -FilePath $exePath -ArgumentList '--selftest'" in build
    assert "-Wait -PassThru" in build


def test_the_installer_never_allows_a_non_admin_install() -> None:
    """Sem admin não há ACL nem Program Files: o endurecimento não roda.

    E a diretiva só aceita `commandline`/`dialog`. Um `none` ali — que parece
    o jeito de dizer "nenhuma troca" — faz o Inno Setup abortar a compilação.
    """
    text = (PACKAGING / "installer.iss").read_text(encoding="utf-8")
    directives = {
        line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
        for line in text.splitlines()
        if "=" in line and not line.lstrip().startswith((";", "#", "{"))
        and line.split("=", 1)[0].strip().isidentifier()
    }
    assert directives.get("PrivilegesRequired") == "admin"
    assert "PrivilegesRequiredOverridesAllowed" not in directives


def test_accents_in_the_installer_need_the_utf8_bom() -> None:
    """Sem BOM o Inno Setup lê o .iss como ANSI, e "Balcão" vira lixo na tela."""
    raw = (PACKAGING / "installer.iss").read_bytes()
    if any(byte > 127 for byte in raw):
        assert raw.startswith(b"\xef\xbb\xbf")


def test_only_preprocessor_directives_start_a_line_with_hash() -> None:
    """O ISPP lê toda linha que começa com `#` como diretiva.

    Na 1.1.4 um `#13#10` (quebra de linha do Pascal) caiu no começo da linha,
    dentro de um MsgBox, e o build morreu com "Unknown preprocessor directive"
    — depois de dez minutos de testes e PyInstaller.
    """
    text = (PACKAGING / "installer.iss").read_text(encoding="utf-8-sig")
    directives = ("define", "include", "if", "ifdef", "ifndef", "elif", "else",
                  "endif", "error", "pragma", "expr", "emit", "sub", "endsub")
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#") and not stripped.startswith("#{"):
            word = re.match(r"#\s*(\w+)", stripped)
            assert word and word.group(1) in directives, (
                f"installer.iss:{number} começa com '#' e não é diretiva: {stripped[:40]}"
            )


def test_names_that_live_on_the_customer_machine_do_not_change() -> None:
    """Mudar estes nomes deixaria atalho duplicado e regra de firewall órfã
    nas lojas que atualizarem por cima."""
    text = (PACKAGING / "installer.iss").read_text(encoding="utf-8-sig")
    assert '#define AppName        "PDV Balcao"' in text
    assert text.count('name=""PDV Balcao - Servidor Local""') == 2


def test_every_image_the_installer_references_is_generated(tmp_path) -> None:  # noqa: ANN001
    """O .iss aponta para packaging/assets; o build gera essa pasta do zero."""
    import importlib.util

    pytest = __import__("pytest")
    pytest.importorskip("PySide6")
    spec = importlib.util.spec_from_file_location("branding", PACKAGING / "branding.py")
    branding = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(branding)

    generated = {path.name for path in branding.generate(tmp_path)}

    text = (PACKAGING / "installer.iss").read_text(encoding="utf-8-sig")
    referenced = set(re.findall(r"assets\\([\w.-]+)", text))
    assert referenced, "o .iss deixou de referenciar as imagens"
    assert referenced <= generated

    spec_text = (PACKAGING / "pdv.spec").read_text(encoding="utf-8")
    assert '"assets" / "pdv.ico"' in spec_text


def test_the_icon_is_a_real_multi_size_ico(tmp_path) -> None:  # noqa: ANN001
    import importlib.util
    import struct

    __import__("pytest").importorskip("PySide6")
    spec = importlib.util.spec_from_file_location("branding", PACKAGING / "branding.py")
    branding = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(branding)

    data = branding.build_ico()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1)
    sizes = []
    for index in range(count):
        width, _h, _c, _r, _p, _bpp, length, offset = struct.unpack(
            "<BBBBHHII", data[6 + 16 * index: 22 + 16 * index]
        )
        sizes.append(width or 256)
        assert data[offset: offset + 8] == b"\x89PNG\r\n\x1a\n"
        assert offset + length <= len(data)
    assert {16, 32, 48, 256} <= set(sizes)


# --------------------------------------------------------------------------- #
# Windows em português
# --------------------------------------------------------------------------- #


def test_the_hardening_never_names_an_account_by_its_english_name() -> None:
    """No Windows em português o grupo é "Administradores".

    O `icacls /setowner Administrators` falhava com 1332 ("não foi feito
    mapeamento entre os nomes de conta"), o script parava no primeiro passo e a
    pasta de dados ficava sem escrita para o caixa: o PDV abria com "attempt to
    write a readonly database". Toda conta vai pelo SID.
    """
    script = (PACKAGING / "harden.ps1").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )

    for name in ("Administrators", "'Users'", '"Users"', "Everyone", "SYSTEM'"):
        assert name not in code, f"nome de conta dependente de idioma: {name}"
    assert "/setowner', $SID_ADMINISTRATORS" in code
    assert '/subcategory:"File System"' not in code, "subcategoria pelo GUID"


def test_the_installer_checks_that_the_hardening_finished() -> None:
    """O [Run] ignora código de saída: sem a conferência a falha é muda."""
    installer = (PACKAGING / "installer.iss").read_text(encoding="utf-8-sig")
    script = (PACKAGING / "harden.ps1").read_text(encoding="utf-8")

    assert "AfterInstall: CheckHardening" in installer
    assert "harden.log" in installer
    marker = re.search(r"Pos\('([^']+)'", installer)
    assert marker is not None
    assert f"Write-Host '{marker.group(1)}.'" in script, (
        "a frase que o instalador procura precisa ser a que o script imprime"
    )


def test_the_hardening_proves_the_counter_can_write_its_data() -> None:
    script = (PACKAGING / "harden.ps1").read_text(encoding="utf-8")

    assert "Test-Hardening -InstallPath $InstallDir -DataPath $DataDir" in script
    assert "nao consegue gravar em $DataPath" in script


def test_a_single_unreadable_file_does_not_stop_the_hardening() -> None:
    """1.1.4: o icacls escreveu "Acesso negado" (um pdv.log com ACL própria).

    No Windows PowerShell 5.1, stderr redirecionado com 'Stop' vira exceção: o
    script morreu depois de liberar a pasta de dados e antes de tudo o mais, e
    o instalador avisou "permissões não aplicadas". O icacls roda com
    'Continue', cada etapa segue mesmo que outra falhe, e a pasta de dados vem
    primeiro — é a única cuja falha impede o caixa de abrir.
    """
    script = (PACKAGING / "harden.ps1").read_text(encoding="utf-8")
    invoke = script[script.index("function Invoke-Icacls"):script.index("function Test-UsersCanModify")]
    assert "$ErrorActionPreference = 'Continue'" in invoke
    assert "[switch] $BestEffort" in invoke

    main = script[script.index("Assert-Elevated\n", script.index("# ----")):]
    steps = [main.index(f"Invoke-Step '{name}'") for name in ("pasta de dados", "logs", "programa")]
    assert steps == sorted(steps), "a pasta de dados precisa vir primeiro"
    assert "Protect-DataDirectory   -Path" not in main, "etapa solta, fora do Invoke-Step"


def test_the_hardening_checks_the_database_files_not_only_the_folder() -> None:
    script = (PACKAGING / "harden.ps1").read_text(encoding="utf-8")
    for name in ("'pdv_local.db'", "'pdv_local.db-wal'", "'pdv_local.db-shm'"):
        assert name in script
    assert "somente leitura - o caixa nao abriria" in script


def test_the_data_folder_is_writable_even_if_powershell_never_runs() -> None:
    """`users-modify` é concedido pelo próprio Inno, pelo SID."""
    installer = (PACKAGING / "installer.iss").read_text(encoding="utf-8-sig")
    assert 'Name: "{#DataDir}"; Permissions: users-modify' in installer


def test_the_installer_finds_the_existing_installation_in_the_registry() -> None:
    """O "{{" do AppId é escape só do [Setup].

    Até a 1.1.4 o [Code] procurava `Uninstall\\{{8F3A…}_is1` — chave que não
    existe —, e toda instalação por cima era tratada como nova: sem detectar a
    versão, sem bloquear downgrade, sem oferecer reparo.
    """
    installer = (PACKAGING / "installer.iss").read_text(encoding="utf-8-sig")
    guid = re.search(r'#define AppGuid\s+"([0-9A-F-]{36})"', installer)
    app_id = re.search(r'#define AppId\s+"(.+)"', installer)
    assert guid and app_id
    assert app_id.group(1) == "{{" + guid.group(1) + "}"

    code = installer[installer.index("\n[Code]\n"):]
    assert "{#AppId}" not in code, "no [Code] a chave vem do AppGuid"
    assert r"Uninstall\{' + '{#AppGuid}' + '}_is1'" in code


def test_the_installer_offers_update_and_repair_with_the_versions_it_found() -> None:
    installer = (PACKAGING / "installer.iss").read_text(encoding="utf-8-sig")
    code = installer[installer.index("\n[Code]\n"):]
    assert "GetVersionNumbersString" in code, "a versão do PDV.exe é lida do arquivo"
    assert "UninstallValue('DisplayVersion')" in code
    assert "CreateInputOptionPage" in code
    assert "Check: not QuickRepair" in installer, "reparo rápido não recopia o programa"
    # O harden.ps1 é copiado sempre: é a versão nova dele que conserta.
    harden = next(line for line in installer.splitlines() if line.startswith('Source: "harden.ps1"'))
    assert "Check:" not in harden
