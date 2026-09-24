"""Telas do assistente de instalação e da ativação.

O defeito que motivou a ativação nova não era de tela: o terminal instalado
tentava ativar contra o endereço de EXEMPLO do código, porque nada perguntava
o endereço da retaguarda. Por isso estes testes cobrem a normalização do
endereço tanto quanto o diálogo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pdv.provisioning.activation import (
    PLACEHOLDER_CLOUD_URL,
    ActivationError,
    ActivationRefused,
    ActivationResult,
    display_server_url,
    normalize_server_url,
)

pytest.importorskip("PySide6")

from pdv.ui.activation_dialog import ActivationDialog  # noqa: E402
from pdv.ui.setup_report import SetupResultDialog, parse_report  # noqa: E402

RESULT = ActivationResult(
    tenant_id="t", store_id="s", device_id="d", sync_token="x", store_name="Loja Centro"
)


# --------------------------------------------------------------------------- #
# Endereço da retaguarda
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("painel.loja.com.br", "https://painel.loja.com.br/api"),
        ("https://painel.loja.com.br/", "https://painel.loja.com.br/api"),
        ("HTTPS://Painel.Loja.com.br/api/", "https://painel.loja.com.br/api"),
        ("  painel.loja.com.br/erp  ", "https://painel.loja.com.br/erp/api"),
        ("http://localhost:3000", "http://localhost:3000/api"),
    ],
)
def test_the_address_is_what_the_browser_shows(typed: str, expected: str) -> None:
    """O lojista digita o que vê na barra do navegador; o `/api` é nosso."""
    assert normalize_server_url(typed) == expected


@pytest.mark.parametrize(
    "typed",
    ["", "   ", "http://painel.loja.com.br", "ftp://painel", "https://a:b@x.com", "a b"],
)
def test_bad_addresses_are_refused(typed: str) -> None:
    with pytest.raises(ActivationError):
        normalize_server_url(typed)


def test_plain_http_is_refused_outside_this_machine() -> None:
    """É por ali que passam o token do terminal e as vendas."""
    with pytest.raises(ActivationError, match="https"):
        normalize_server_url("http://192.168.0.10:3000")


def test_the_placeholder_is_never_shown_as_an_address() -> None:
    assert display_server_url(PLACEHOLDER_CLOUD_URL) == ""
    assert display_server_url("https://painel.loja.com.br/api") == "https://painel.loja.com.br"


# --------------------------------------------------------------------------- #
# Diálogo de ativação
# --------------------------------------------------------------------------- #


def test_activation_sends_the_normalized_address_and_code(qtbot) -> None:  # noqa: ANN001
    calls: list[tuple[str, str]] = []

    def fake(url: str, code: str) -> ActivationResult:
        calls.append((url, code))
        return RESULT

    dialog = ActivationDialog(fake)
    qtbot.addWidget(dialog)
    dialog._server.setText("painel.loja.com.br")
    dialog._code.setText("a1b2-c3d4")
    dialog._start()
    qtbot.waitUntil(lambda: dialog.result_value is not None, timeout=3000)

    assert calls == [("https://painel.loja.com.br/api", "A1B2C3D4")]
    assert dialog.result_value == RESULT


def test_a_bad_address_never_reaches_the_network(qtbot) -> None:  # noqa: ANN001
    calls: list[str] = []
    dialog = ActivationDialog(lambda url, code: calls.append(url) or RESULT)
    qtbot.addWidget(dialog)
    dialog._server.setText("http://painel.loja.com.br")
    dialog._code.setText("A1B2C3D4")

    dialog._start()

    assert calls == []
    assert "https" in dialog._status.text()


def test_a_refused_code_keeps_the_dialog_open_to_retry(qtbot) -> None:  # noqa: ANN001
    """Código digitado errado não pode obrigar a refazer a instalação."""
    attempts: list[str] = []

    def fake(url: str, code: str) -> ActivationResult:
        attempts.append(code)
        if len(attempts) == 1:
            raise ActivationRefused("Código expirado — gere outro no painel.")
        return RESULT

    dialog = ActivationDialog(fake, server_url="https://painel.loja.com.br/api")
    qtbot.addWidget(dialog)
    assert dialog._server.text() == "https://painel.loja.com.br"
    dialog._code.setText("AAAA1111")
    dialog._start()
    qtbot.waitUntil(lambda: not dialog.busy, timeout=3000)

    assert "expirado" in dialog._status.text()
    assert dialog.result_value is None
    assert dialog._submit.isEnabled()

    dialog._code.setText("BBBB2222")
    dialog._start()
    qtbot.waitUntil(lambda: dialog.result_value is not None, timeout=3000)
    assert attempts == ["AAAA1111", "BBBB2222"]


def test_an_unexpected_failure_becomes_a_message(qtbot) -> None:  # noqa: ANN001
    def boom(url: str, code: str) -> ActivationResult:
        raise RuntimeError("socket morreu")

    dialog = ActivationDialog(boom, server_url="https://x.com/api")
    qtbot.addWidget(dialog)
    dialog._code.setText("AAAA1111")
    dialog._start()
    qtbot.waitUntil(lambda: not dialog.busy, timeout=3000)

    assert "socket morreu" in dialog._status.text()


def test_closing_while_talking_to_the_server_is_ignored(qtbot) -> None:  # noqa: ANN001
    """Fechar no meio deixaria o código talvez consumido e o resultado perdido."""
    import threading

    release = threading.Event()

    def slow(url: str, code: str) -> ActivationResult:
        release.wait(3)
        return RESULT

    dialog = ActivationDialog(slow, server_url="https://x.com/api")
    qtbot.addWidget(dialog)
    dialog.show()
    dialog._code.setText("AAAA1111")
    dialog._start()
    dialog.reject()

    assert dialog.isVisible()
    release.set()
    qtbot.waitUntil(lambda: dialog.result_value is not None, timeout=3000)


# --------------------------------------------------------------------------- #
# Resultado da instalação
# --------------------------------------------------------------------------- #

REPORT = """[ OK ] Segredo do terminal protegido no DPAPI
[AVISO] Terminal não ativado — modo demonstração, sem sincronizar.
        → Ative pelo botão "Ativar terminal" no próprio PDV.
[INFO] 3 porta(s) serial(is) verificada(s)

Teste de instalação:
[ OK ] Banco de dados: gravação e WAL
[FALHA] Impressora: sem papel
         → Troque a bobina e rode "Reconfigurar periféricos".
Linha que esta tela ainda não conhece"""


def test_the_report_becomes_lines_with_state_and_remedy() -> None:
    lines = parse_report(REPORT)

    assert [line.status for line in lines] == [
        "ok", "warning", "info", "ok", "failed", "info",
    ]
    assert lines[1].remedy.startswith("Ative pelo botão")
    assert "Troque a bobina" in lines[4].remedy
    assert lines[-1].text == "Linha que esta tela ainda não conhece"
    assert all("Teste de instalação" not in line.text for line in lines)


@pytest.mark.parametrize(
    ("code", "headline"),
    [(0, "Pronto para vender"), (2, "Instalado, com pendências"), (3, "Ainda não é possível vender")],
)
def test_the_headline_follows_the_exit_code(qtbot, code: int, headline: str) -> None:  # noqa: ANN001
    dialog = SetupResultDialog(code, REPORT, log_path=Path("C:/x/setup.log"))
    qtbot.addWidget(dialog)

    assert dialog.headline == headline
    assert len(dialog.lines) == 6


def test_demo_logins_are_shown_only_when_asked(qtbot) -> None:  # noqa: ANN001
    from PySide6.QtWidgets import QLabel

    shown = SetupResultDialog(2, REPORT, demo_logins=True)
    hidden = SetupResultDialog(0, REPORT, demo_logins=False)
    qtbot.addWidget(shown)
    qtbot.addWidget(hidden)

    def texts(dialog) -> str:  # noqa: ANN001
        return " ".join(label.text() for label in dialog.findChildren(QLabel))

    assert "84627519" in texts(shown)
    assert "84627519" not in texts(hidden)


def test_the_setup_refuses_to_activate_against_the_placeholder(tmp_path: Path) -> None:
    """O defeito original: ativar contra `api.erpfood.local`, que não existe."""
    import setup_wizard
    from pdv.config import AppConfig
    from pdv.data.database import Database
    from pdv.provisioning.secrets import SecretVault

    database = Database(tmp_path / "pdv.db")
    database.migrate()
    base = AppConfig(tenant_id="t", store_id="s", device_id="d")
    assert base.cloud_base_url == PLACEHOLDER_CLOUD_URL

    line = setup_wizard._activate_step(
        database, SecretVault(tmp_path / "secrets"), base, "A1B2C3D4", None
    )

    assert line.startswith("[AVISO]")
    assert "Endereço da retaguarda" in line
    database.close()
