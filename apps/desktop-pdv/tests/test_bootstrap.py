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
