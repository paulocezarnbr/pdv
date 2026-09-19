"""O código de pareamento — tamanho, prazo, revogação e freio.

O código é a única coisa entre um celular qualquer da rede da loja e a
capacidade de lançar pedido. Ele fica exposto na tela do caixa, onde qualquer
um que passe pelo balcão consegue lê-lo, e é por isso que cada peça aqui tem
uma razão específica:

* **oito dígitos**, porque seis eram um milhão de combinações contra um
  atacante que dispara requisições sem esperar ninguém;
* **prazo visível**, porque olhar para um código sem saber se ele ainda vale
  é o que fazia o garçom descobrir o vencimento errando no celular;
* **revogação**, porque a alternativa era esperar cinco minutos olhando para o
  próprio código exposto;
* **freio**, porque tentativa que não custa nada é tentativa infinita.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from pdv.config import AppConfig, PrinterConfig, StockConfig
from pdv.data.database import Database
from pdv.data.seed import seed_demo_data
from pdv.domain.models import iso, utc_now
from pdv.edge.auth import (
    MAX_PAIRING_ATTEMPTS,
    PAIRING_CODE_DIGITS,
    PAIRING_TTL,
    EdgeAuth,
    PairingError,
)

TENANT = "11111111-1111-1111-1111-111111111111"
STORE = "22222222-2222-2222-2222-222222222222"


@pytest.fixture()
def auth(tmp_path: Path) -> EdgeAuth:
    config = AppConfig(
        tenant_id=TENANT,
        store_id=STORE,
        device_id="33333333-3333-3333-3333-333333333333",
        database_path=tmp_path / "pdv.db",
        printer=PrinterConfig(backend="file", output_dir=tmp_path / "out"),
        stock=StockConfig(),
    )
    database = Database(config.database_path)
    database.migrate()
    seed_demo_data(database, config)
    return EdgeAuth(database, TENANT, STORE)


# --------------------------------------------------------------------------- #
# Tamanho e prazo
# --------------------------------------------------------------------------- #


def test_the_code_has_eight_digits(auth: EdgeAuth) -> None:
    """Seis dígitos contra um atacante que não espera ninguém eram poucos.

    A conta que importa não é "quanto tempo para adivinhar tudo", é "quantas
    tentativas cabem na janela de cinco minutos". Oito multiplicam o espaço por
    cem e continuam sendo dois grupos de quatro na tela.
    """
    code, _ = auth.create_pairing_code()

    assert len(code) == PAIRING_CODE_DIGITS == 8
    assert code.isdigit()


def test_the_code_says_how_long_it_has_left(auth: EdgeAuth) -> None:
    _code, pairing = auth.create_pairing_code()

    assert pairing.is_alive
    assert 0 < pairing.remaining_seconds <= PAIRING_TTL.total_seconds()


def test_the_counter_can_read_the_remaining_time_later(auth: EdgeAuth) -> None:
    """É o que alimenta a contagem regressiva no painel do caixa."""
    auth.create_pairing_code()

    active = auth.active_pairing_code()

    assert active is not None
    assert active.remaining_seconds > 0


def test_the_code_itself_is_never_readable_again(auth: EdgeAuth) -> None:
    """Se desse para relê-lo do banco, guardar só o hash não serviria de nada."""
    code, _ = auth.create_pairing_code()

    dump = " ".join(
        str(value)
        for row in auth._db.query_all("SELECT * FROM edge_pairing_codes")
        for value in tuple(row)
    )
    assert code not in dump


def test_there_is_no_active_code_before_generating_one(auth: EdgeAuth) -> None:
    assert auth.active_pairing_code() is None


# --------------------------------------------------------------------------- #
# Revogação
# --------------------------------------------------------------------------- #


def test_revoking_kills_the_code_before_the_deadline(auth: EdgeAuth) -> None:
    """Alguém estranho passou pelo balcão com o código na tela."""
    code, _ = auth.create_pairing_code()

    assert auth.revoke_pairing_codes() == 1

    assert auth.active_pairing_code() is None
    with pytest.raises(PairingError):
        auth.pair(code, device_name="Celular do intruso")


def test_generating_a_new_code_kills_the_old_one(auth: EdgeAuth) -> None:
    """Antes não matava, e o efeito era contraintuitivo na direção errada.

    Quem via o código na tela e clicava "gerar outro" — porque achou que alguém
    tinha lido — deixava os dois válidos, inclusive o que acabara de ser lido.
    """
    first, _ = auth.create_pairing_code()
    second, _ = auth.create_pairing_code()

    with pytest.raises(PairingError):
        auth.pair(first, device_name="Celular com o código velho")

    assert auth.pair(second, device_name="Celular do garçom")


def test_revoking_does_not_pretend_a_device_paired(auth: EdgeAuth) -> None:
    """`used_at` é a coluna que alguém vai olhar para saber quem entrou.

    Marcar o código revogado como "usado" mentiria justamente ali.
    """
    auth.create_pairing_code()

    auth.revoke_pairing_codes()

    row = auth._db.query_one("SELECT used_at, used_by FROM edge_pairing_codes")
    assert row["used_at"] is None
    assert row["used_by"] is None


def test_revoking_with_nothing_alive_is_harmless(auth: EdgeAuth) -> None:
    assert auth.revoke_pairing_codes() == 0


def test_an_expired_code_does_not_show_as_active(auth: EdgeAuth) -> None:
    auth.create_pairing_code()
    past = iso(utc_now() - timedelta(seconds=1))
    with auth._db.transaction() as connection:
        connection.execute("UPDATE edge_pairing_codes SET expires_at = ?", (past,))

    assert auth.active_pairing_code() is None


# --------------------------------------------------------------------------- #
# Freio
# --------------------------------------------------------------------------- #


def test_guessing_gets_throttled(auth: EdgeAuth) -> None:
    """Tentativa que não custa nada é tentativa infinita."""
    for _ in range(MAX_PAIRING_ATTEMPTS):
        with pytest.raises(PairingError):
            auth.pair("00000000", device_name="Celular do atacante")

    assert auth.pairing_lock_seconds() > 0

    # E agora nem o código certo passa, enquanto o freio estiver ativo: a
    # alternativa seria um oráculo dizendo ao atacante que ele acertou.
    code, _ = auth.create_pairing_code()
    with pytest.raises(PairingError) as excinfo:
        auth.pair(code, device_name="Celular do garçom")
    assert "Aguarde" in str(excinfo.value)


def test_the_failure_counter_survives_the_attempt_that_raised(auth: EdgeAuth) -> None:
    """A falha é registrada FORA da transação que o `raise` desfaz.

    Contá-la dentro faria o rollback apagar o próprio contador — e o freio
    nunca chegaria ao teto, por mais que alguém tentasse.
    """
    for _ in range(3):
        with pytest.raises(PairingError):
            auth.pair("00000000", device_name="x")

    row = auth._db.query_one(
        "SELECT failures FROM auth_throttle WHERE scope = 'edge:pair'"
    )
    assert row is not None and int(row["failures"]) == 3


def test_a_successful_pairing_clears_the_counter(auth: EdgeAuth) -> None:
    """Aqui limpar no acerto é seguro, ao contrário do freio global de login.

    Acertar exige um código que o caixa **acabou de gerar**, de uso único: não
    existe a saída barata de errar várias vezes e acertar o próprio para
    recomeçar do zero.
    """
    for _ in range(3):
        with pytest.raises(PairingError):
            auth.pair("00000000", device_name="x")

    code, _ = auth.create_pairing_code()
    auth.pair(code, device_name="Celular do garçom")

    assert (
        auth._db.query_one(
            "SELECT failures FROM auth_throttle WHERE scope = 'edge:pair'"
        )
        is None
    )


def test_the_throttle_survives_a_restart(tmp_path: Path) -> None:
    """Matar o processo não pode devolver as tentativas.

    É a mesma correção que o freio de login recebeu: contador em memória vira
    "abre, tenta, mata, repete".
    """
    config = AppConfig(
        tenant_id=TENANT,
        store_id=STORE,
        device_id="33333333-3333-3333-3333-333333333333",
        database_path=tmp_path / "pdv.db",
        printer=PrinterConfig(backend="file", output_dir=tmp_path / "out"),
        stock=StockConfig(),
    )
    database = Database(config.database_path)
    database.migrate()
    seed_demo_data(database, config)

    first = EdgeAuth(database, TENANT, STORE)
    for _ in range(MAX_PAIRING_ATTEMPTS):
        with pytest.raises(PairingError):
            first.pair("00000000", device_name="x")
    database.close()

    restarted = Database(config.database_path)
    restarted.migrate()

    assert EdgeAuth(restarted, TENANT, STORE).pairing_lock_seconds() > 0


def test_an_honest_mistake_does_not_lock_the_store(auth: EdgeAuth) -> None:
    """Um garçom erra duas ou três vezes; dez é folga larga de propósito."""
    for _ in range(3):
        with pytest.raises(PairingError):
            auth.pair("00000000", device_name="x")

    assert auth.pairing_lock_seconds() == 0
    code, _ = auth.create_pairing_code()
    assert auth.pair(code, device_name="Celular do garçom")
