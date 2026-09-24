"""A fiação do `main.py`.

Esta camada não tem lógica de negócio nenhuma, e é justamente por isso que
precisa de teste. Um serviço perfeito que ninguém instancia não existe: até
esta fase o `SyncEngine` estava inteiro, testado e **nunca era montado pelo
app** — a fila enchia em disco e nada subia. Nenhum teste de serviço pegaria
isso, porque todos constroem o motor com as próprias mãos.

É o mesmo tipo de defeito que o `from __future__ import annotations` causou no
servidor do salão na Fase 3 (ver `test_edge.py`): tudo verde por baixo, e a
ponta desligada.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pdv.config import AppConfig, PrinterConfig, StockConfig
from pdv.data.database import Database
from pdv.data.seed import seed_demo_data
from pdv.provisioning.activation import SYNC_TOKEN_NAME
from pdv.provisioning.secrets import SecretVault
from pdv.services.checkout import CheckoutService

pytest.importorskip("PySide6")

import main  # noqa: E402

TENANT = "11111111-1111-1111-1111-111111111111"


@pytest.fixture()
def terminal(tmp_path: Path):  # noqa: ANN201
    config = AppConfig(
        tenant_id=TENANT,
        store_id="22222222-2222-2222-2222-222222222222",
        device_id="33333333-3333-3333-3333-333333333333",
        database_path=tmp_path / "pdv.db",
        printer=PrinterConfig(backend="file", output_dir=tmp_path / "out"),
        stock=StockConfig(),
    )
    database = Database(config.database_path)
    database.migrate()
    seed_demo_data(database, config)
    return database, config, CheckoutService(database, config)


def _activate(config: AppConfig) -> None:
    """Grava um token, como a ativação do terminal faria."""
    SecretVault(config.database_path.parent / "secrets").store(
        SYNC_TOKEN_NAME, b"token-de-teste"
    )


def test_an_activated_terminal_gets_a_sync_service(terminal, monkeypatch) -> None:  # noqa: ANN001
    """O teste que faltava: o app realmente monta o motor."""
    database, config, checkout = terminal
    monkeypatch.delenv("PDV_SYNC", raising=False)
    _activate(config)

    service = main.build_sync(database, config, checkout)

    assert service is not None


def test_the_sync_service_speaks_commands(terminal, monkeypatch) -> None:  # noqa: ANN001
    """E o canal de comando do painel fica ligado.

    O `RemoteCommandService` é passado ao motor aqui e em nenhum outro lugar do
    app. Esquecê-lo deixaria o terminal aplicando zero comandos, sem erro
    nenhum no log — o painel mostraria `pendente` para sempre.
    """
    database, config, checkout = terminal
    monkeypatch.delenv("PDV_SYNC", raising=False)
    _activate(config)

    service = main.build_sync(database, config, checkout)

    assert service._worker._engine.speaks_commands is True


def test_a_terminal_that_was_never_activated_does_not_sync(terminal, monkeypatch) -> None:  # noqa: ANN001
    """Sem token não há para onde enviar.

    E isso **não** é erro: a fila é durável e sobe inteira no primeiro ciclo
    depois da ativação.
    """
    database, config, checkout = terminal
    monkeypatch.delenv("PDV_SYNC", raising=False)

    assert main.build_sync(database, config, checkout) is None


def test_sync_can_be_turned_off_by_environment(terminal, monkeypatch) -> None:  # noqa: ANN001
    """Para desenvolvimento e para a loja que ainda não tem nuvem."""
    database, config, checkout = terminal
    _activate(config)
    monkeypatch.setenv("PDV_SYNC", "0")

    assert main.build_sync(database, config, checkout) is None


def test_a_broken_vault_does_not_stop_the_counter(terminal, monkeypatch) -> None:  # noqa: ANN001
    """O balcão continua vendendo.

    Sincronização protege a venda de ser adulterada depois; não é o que
    permite fazê-la. Uma falha ao ler o cofre não pode impedir a abertura do
    caixa.
    """
    database, config, checkout = terminal
    monkeypatch.delenv("PDV_SYNC", raising=False)
    monkeypatch.setattr(
        main, "load_sync_token", lambda vault: (_ for _ in ()).throw(OSError("cofre"))
    )

    assert main.build_sync(database, config, checkout) is None


# --------------------------------------------------------------------------- #
# Terminal instalado: o caixa abre o que o instalador provisionou
# --------------------------------------------------------------------------- #
#
# O defeito que motivou esta seção: instalado, o `PDV.exe` procurava o banco em
# `./pdv_local.db` — relativo ao diretório de trabalho, que no atalho é
# `Program Files`, somente leitura para o caixa. O PyInstaller mostrava
# "unable to open database file", enquanto o `PDVSetup.exe` tinha deixado
# banco, segredo e periféricos prontos em ProgramData.


@pytest.fixture()
def installed(tmp_path: Path, monkeypatch):  # noqa: ANN001, ANN201
    """O processo como o PDV.exe empacotado: `sys.frozen`, sem variáveis de dev."""
    monkeypatch.setattr(main.sys, "frozen", True, raising=False)
    monkeypatch.setenv("ProgramData", str(tmp_path))
    for name in ("PDV_DB_PATH", "PDV_DATA_DIR", "PDV_TENANT_ID"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "ERPFood" / "PDV"


def test_the_packaged_app_opens_the_database_the_installer_prepared(installed) -> None:  # noqa: ANN001
    config = main.build_config()

    assert config.database_path == installed / "pdv_local.db"
    assert config.printer.output_dir == installed / "cupons"


def test_it_is_the_same_folder_the_setup_wizard_provisions(installed) -> None:  # noqa: ANN001
    """Duas definições desta pasta já divergiram uma vez."""
    import setup_wizard
    from pdv.config import default_data_dir, installed_data_dir

    assert setup_wizard.default_data_dir is default_data_dir
    assert installed_data_dir() == default_data_dir() == installed


def test_the_device_secret_comes_from_the_vault_not_the_dev_key(installed) -> None:  # noqa: ANN001
    from pdv.config import AppConfig as Config

    vault = SecretVault(installed / "secrets")
    installed.mkdir(parents=True)
    provisioned = vault.ensure_device_secret()

    config = main.build_config()

    assert config.device_secret == provisioned
    assert config.device_secret != Config(tenant_id="t", store_id="s", device_id="d").device_secret


def test_an_unreadable_secret_is_never_replaced(installed) -> None:  # noqa: ANN001
    """Recriar o segredo faria toda a auditoria já gravada acusar adulteração."""
    (installed / "secrets").mkdir(parents=True)
    broken = installed / "secrets" / "device_secret.bin"
    broken.write_bytes(b"lixo-que-nao-decifra")
    before = {p.name: p.read_bytes() for p in (installed / "secrets").iterdir()}

    with pytest.raises(main.StartupError, match="suporte"):
        main.build_config()

    after = {p.name: p.read_bytes() for p in (installed / "secrets").iterdir()}
    assert after == before


def test_an_explicit_db_path_still_wins(installed, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("PDV_DB_PATH", str(tmp_path / "suporte.db"))

    assert main.build_config().database_path == tmp_path / "suporte.db"


def test_running_from_source_keeps_the_local_database(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delattr(main.sys, "frozen", raising=False)
    for name in ("PDV_DB_PATH", "PDV_DATA_DIR"):
        monkeypatch.delenv(name, raising=False)

    assert main.build_config().database_path == Path("./pdv_local.db")


def test_detected_peripherals_and_activation_are_applied(installed) -> None:  # noqa: ANN001
    from pdv.data.settings import SettingsStore

    installed.mkdir(parents=True)
    config = main.build_config()
    database = Database(config.database_path)
    database.migrate()
    SettingsStore(database).set_many(
        {
            "scale.protocol": "toledo_prix3",
            "scale.port": "COM4",
            "printer.backend": "win32raw",
            "printer.name": "EPSON TM-T20X Receipt",
            "device.activated": "1",
            "device.tenant_id": "aaaaaaaa-0000-0000-0000-000000000001",
            "device.store_id": "aaaaaaaa-0000-0000-0000-000000000002",
            "device.id": "aaaaaaaa-0000-0000-0000-000000000003",
        }
    )
    database.close()

    opened, applied = main.open_database(config)

    assert applied.scale.port == "COM4"
    assert applied.printer.backend == "win32raw"
    assert applied.printer.output_dir == installed / "cupons"
    assert applied.tenant_id == "aaaaaaaa-0000-0000-0000-000000000001"
    opened.close()


def test_an_activated_terminal_does_not_get_the_demo_logins(installed) -> None:  # noqa: ANN001
    """Os PINs de demonstração estão no README. Numa loja ativada, seriam um
    caixa que qualquer um abre."""
    from pdv.data.settings import SettingsStore

    installed.mkdir(parents=True)
    config = main.build_config()
    database = Database(config.database_path)
    database.migrate()
    SettingsStore(database).set_many(
        {"device.activated": "1", "device.tenant_id": "aaaaaaaa-0000-0000-0000-000000000001"}
    )
    database.close()

    opened, _ = main.open_database(config)

    logins = {str(r["login"]) for r in opened.query_all("SELECT login FROM users")}
    assert logins == set()
    opened.close()


def test_a_terminal_not_yet_activated_opens_in_demo_mode(installed) -> None:  # noqa: ANN001
    installed.mkdir(parents=True)
    opened, _ = main.open_database(main.build_config())

    logins = {str(r["login"]) for r in opened.query_all("SELECT login FROM users")}
    assert {"ana", "bruno", "olivia"} <= logins
    opened.close()


def test_a_startup_failure_becomes_a_message_not_a_traceback(monkeypatch) -> None:  # noqa: ANN001
    shown: list[str] = []
    monkeypatch.setattr(main, "_show_fatal", shown.append)

    def broken() -> int:
        raise main.StartupError("Não foi possível abrir o banco de dados do PDV")

    monkeypatch.setattr(main, "main", broken)

    assert main.run() == 1
    assert shown == ["Não foi possível abrir o banco de dados do PDV"]
