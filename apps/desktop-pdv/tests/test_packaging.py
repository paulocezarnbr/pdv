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
