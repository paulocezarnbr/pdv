"""Segurança do login — Fase 3.7.

Auditoria pedida em uso, e o que ela encontrou.

**A falha séria.** O freio por tentativas vivia num dicionário de instância.
Matar o processo e abrir de novo zerava o contador, e cada reabertura dava mais
cinco tentativas livres. Com Argon2id a ~37 ms por tentativa, as 10 000
combinações de um PIN de 4 dígitos caíam em ~6 minutos por esse caminho. O
freio parecia existir e não existia.

**As menores.** Não havia teto global (bastava espalhar as tentativas por
vários logins), o expoente do dobro crescia sem limite, e não havia política
nenhuma de PIN — `1234` era aceito, inclusive na base de demonstração, que é
onde o cliente aprende o que é normal.

O que **não** foi corrigido, porque não tem correção local: quem é
administrador da máquina lê o PIN da memória e edita este banco direto,
inclusive o relógio. A defesa contra esse perfil é a ancoragem no servidor.
"""

from __future__ import annotations

import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from pdv.config import AppConfig, PrinterConfig
from pdv.data.database import Database
from pdv.data.seed import (
    DEMO_MANAGER_LOGIN,
    DEMO_MANAGER_PIN,
    DEMO_OPERATOR_LOGIN,
    DEMO_OPERATOR_PIN,
    DEMO_OWNER_PIN,
    DEMO_WAITER_LOGIN,
    DEMO_WAITER_PIN,
    seed_demo_data,
)
from pdv.domain.errors import AuthorizationRequiredError
from pdv.domain.models import iso, utc_now
from pdv.services.authorization import (
    MAX_ATTEMPTS,
    MAX_GLOBAL_ATTEMPTS,
    PIN_MIN_LENGTH,
    AuthorizationService,
    WeakPinError,
    hash_pin,
    validate_pin,
)

TENANT = "11111111-1111-1111-1111-111111111111"


@pytest.fixture()
def store(tmp_path: Path):  # noqa: ANN201
    config = AppConfig(
        tenant_id=TENANT,
        store_id="22222222-2222-2222-2222-222222222222",
        device_id="33333333-3333-3333-3333-333333333333",
        database_path=tmp_path / "pdv.db",
        printer=PrinterConfig(backend="file", output_dir=tmp_path / "out"),
    )
    database = Database(config.database_path)
    database.migrate()
    seed_demo_data(database, config)
    return database, config


def _fail(service: AuthorizationService, login: str, times: int) -> int:
    """Erra o PIN `times` vezes. Devolve quantas foram barradas pelo freio."""
    blocked = 0
    for _ in range(times):
        try:
            service.authenticate(login, "999999")
        except AuthorizationRequiredError as exc:
            if "Aguarde" in str(exc):
                blocked += 1
    return blocked


# --------------------------------------------------------------------------- #
# A falha séria: o freio não sobrevivia ao restart
# --------------------------------------------------------------------------- #


def test_the_lockout_survives_restarting_the_app(store) -> None:  # noqa: ANN001
    """O bug. Matar o processo dava mais cinco tentativas livres.

    Num laço de "abre, tenta cinco, mata", um PIN de 4 dígitos caía em minutos.
    Era a diferença entre ter freio e parecer ter freio.
    """
    database, config = store

    first = AuthorizationService(database, config.tenant_id)
    _fail(first, DEMO_MANAGER_LOGIN, MAX_ATTEMPTS)
    assert first.lock_status(DEMO_MANAGER_LOGIN) > 0

    # Uma instância nova é o que o app monta ao reabrir.
    after_restart = AuthorizationService(database, config.tenant_id)

    assert after_restart.lock_status(DEMO_MANAGER_LOGIN) > 0
    with pytest.raises(AuthorizationRequiredError, match="Aguarde"):
        after_restart.authenticate(DEMO_MANAGER_LOGIN, DEMO_MANAGER_PIN)


def test_even_the_right_pin_waits_out_the_lockout(store) -> None:  # noqa: ANN001
    """O freio não abre para quem acerta.

    Se acertar durante o bloqueio liberasse, o bloqueio seria só um atraso
    entre tentativas — e o atacante continuaria varrendo o espaço de PINs.
    """
    database, config = store
    service = AuthorizationService(database, config.tenant_id)
    _fail(service, DEMO_MANAGER_LOGIN, MAX_ATTEMPTS)

    with pytest.raises(AuthorizationRequiredError, match="Aguarde"):
        service.authorize(DEMO_MANAGER_LOGIN, DEMO_MANAGER_PIN)


def test_the_lockout_is_recorded_where_it_can_be_audited(store) -> None:  # noqa: ANN001
    database, config = store
    service = AuthorizationService(database, config.tenant_id)

    _fail(service, DEMO_MANAGER_LOGIN, MAX_ATTEMPTS)

    row = database.query_one(
        "SELECT failures, locked_until FROM auth_throttle WHERE scope = ?",
        (f"login:{DEMO_MANAGER_LOGIN}",),
    )
    assert int(row["failures"]) == MAX_ATTEMPTS
    assert row["locked_until"] is not None


def test_a_clock_moved_backwards_does_not_shorten_the_lockout(store) -> None:  # noqa: ANN001
    """O piso monotônico.

    O banco guarda relógio de parede, porque precisa sobreviver ao processo.
    Dentro da sessão, o serviço também guarda um piso que o relógio não move —
    então adiantar o `locked_until` no banco não libera quem já está preso.
    """
    database, config = store
    service = AuthorizationService(database, config.tenant_id)
    _fail(service, DEMO_MANAGER_LOGIN, MAX_ATTEMPTS)

    # Simula o relógio andando para trás: o bloqueio no banco "já venceu".
    with database.transaction() as connection:
        connection.execute(
            "UPDATE auth_throttle SET locked_until = ? WHERE scope = ?",
            (iso(utc_now() - timedelta(hours=1)), f"login:{DEMO_MANAGER_LOGIN}"),
        )

    assert service.lock_status(DEMO_MANAGER_LOGIN) > 0, "o piso da sessão segura"


# --------------------------------------------------------------------------- #
# O teto global
# --------------------------------------------------------------------------- #


def test_spreading_attempts_across_logins_does_not_buy_free_tries(store) -> None:  # noqa: ANN001
    """Sem teto global, cada login rendia sua cota.

    Com quatro usuários cadastrados, eram vinte tentativas livres antes de
    qualquer freio — e o atacante nem precisava saber quais logins existem,
    porque inventar também conta.
    """
    database, config = store
    service = AuthorizationService(database, config.tenant_id)

    for index in range(MAX_GLOBAL_ATTEMPTS):
        try:
            service.authenticate(f"inexistente{index}", "999999")
        except AuthorizationRequiredError:
            pass

    # Um login que nunca errou também é barrado: o freio é do terminal.
    with pytest.raises(AuthorizationRequiredError, match="neste terminal"):
        service.authenticate(DEMO_OPERATOR_LOGIN, DEMO_OPERATOR_PIN)


def test_a_successful_login_does_not_clear_the_global_brake(store) -> None:  # noqa: ANN001
    """Senão o atacante teria uma saída barata.

    Errar dezenove vezes, acertar o próprio login e recomeçar do zero.
    """
    database, config = store
    service = AuthorizationService(database, config.tenant_id)
    for index in range(MAX_GLOBAL_ATTEMPTS - 1):
        try:
            service.authenticate(f"inexistente{index}", "999999")
        except AuthorizationRequiredError:
            pass

    service.authenticate(DEMO_OPERATOR_LOGIN, DEMO_OPERATOR_PIN)

    row = database.query_one("SELECT failures FROM auth_throttle WHERE scope = '*'")
    assert int(row["failures"]) == MAX_GLOBAL_ATTEMPTS - 1


def test_a_successful_login_does_clear_its_own_counter(store) -> None:  # noqa: ANN001
    """Quem erra o PIN duas vezes e acerta não fica a um erro do bloqueio."""
    database, config = store
    service = AuthorizationService(database, config.tenant_id)
    _fail(service, DEMO_OPERATOR_LOGIN, 2)

    service.authenticate(DEMO_OPERATOR_LOGIN, DEMO_OPERATOR_PIN)

    assert database.query_one(
        "SELECT scope FROM auth_throttle WHERE scope = ?",
        (f"login:{DEMO_OPERATOR_LOGIN}",),
    ) is None


def test_old_failures_stop_counting(store) -> None:  # noqa: ANN001
    """Cinco erros espalhados por seis meses não são um ataque.

    Sem a janela, o freio acabaria bloqueando quem nunca foi atacado — e a
    proteção viraria o problema.
    """
    database, config = store
    service = AuthorizationService(database, config.tenant_id)
    _fail(service, DEMO_OPERATOR_LOGIN, MAX_ATTEMPTS - 1)

    with database.transaction() as connection:
        connection.execute(
            "UPDATE auth_throttle SET first_failure_at = ? WHERE scope = ?",
            (iso(utc_now() - timedelta(days=30)), f"login:{DEMO_OPERATOR_LOGIN}"),
        )

    _fail(service, DEMO_OPERATOR_LOGIN, 1)

    row = database.query_one(
        "SELECT failures FROM auth_throttle WHERE scope = ?",
        (f"login:{DEMO_OPERATOR_LOGIN}",),
    )
    assert int(row["failures"]) == 1, "a contagem recomeçou"


def test_a_stubborn_attacker_does_not_freeze_the_terminal(store) -> None:  # noqa: ANN001
    """O expoente do dobro tem teto.

    Sem ele, insistir levava o cálculo a `2**10000` antes do `min()` — e a
    defesa viraria o travamento que ela deveria impedir.
    """
    database, config = store
    service = AuthorizationService(database, config.tenant_id)

    started = time.monotonic()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO auth_throttle "
            "  (scope, failures, locked_until, first_failure_at, last_failure_at) "
            "VALUES (?, 10000, NULL, ?, ?)",
            (f"login:{DEMO_OPERATOR_LOGIN}", iso(utc_now()), iso(utc_now())),
        )
    _fail(service, DEMO_OPERATOR_LOGIN, 1)

    assert time.monotonic() - started < 5.0
    assert service.lock_status(DEMO_OPERATOR_LOGIN) <= 301


# --------------------------------------------------------------------------- #
# Enumeração de logins
# --------------------------------------------------------------------------- #


def test_an_unknown_login_costs_the_same_as_a_wrong_pin(store) -> None:  # noqa: ANN001
    """Resposta imediata para login inexistente entrega quais logins existem.

    O atacante passaria a gastar tentativas só nos que valem — e o espaço de
    busca encolheria de "todos os nomes" para "quatro".
    """
    database, config = store
    service = AuthorizationService(database, config.tenant_id)

    def measure(login: str) -> float:
        started = time.perf_counter()
        try:
            service.authenticate(login, "999999")
        except AuthorizationRequiredError:
            pass
        return time.perf_counter() - started

    unknown = measure("nao-existe-mesmo")
    known = measure(DEMO_OPERATOR_LOGIN)

    # Argon2 custa ~37 ms; a diferença entre os dois caminhos tem de ficar
    # muito abaixo disso para não ser mensurável pela rede.
    assert abs(unknown - known) < known, f"{unknown=:.3f} {known=:.3f}"


def test_the_message_does_not_say_which_half_was_wrong(store) -> None:  # noqa: ANN001
    database, config = store
    service = AuthorizationService(database, config.tenant_id)

    with pytest.raises(AuthorizationRequiredError) as unknown:
        service.authenticate("nao-existe", "999999")
    with pytest.raises(AuthorizationRequiredError) as wrong_pin:
        service.authenticate(DEMO_OPERATOR_LOGIN, "999999")

    assert str(unknown.value) == str(wrong_pin.value)


# --------------------------------------------------------------------------- #
# Política de PIN
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "pin",
    ["1234", "1111", "12345", "", "abcdef", "12 34 56", "1234567890123"],
)
def test_a_pin_that_does_not_meet_the_policy_is_refused(pin: str) -> None:
    with pytest.raises(WeakPinError):
        validate_pin(pin)


@pytest.mark.parametrize(
    "pin", ["123456", "654321", "000000", "111111", "121212", "987654", "098765"]
)
def test_sequences_and_repetitions_are_refused(pin: str) -> None:
    """São o que as pessoas escolhem quando ninguém as impede.

    E é exatamente o que um atacante tenta primeiro, então a política que não
    os recusa não está protegendo do caso real.
    """
    with pytest.raises(WeakPinError):
        validate_pin(pin)


@pytest.mark.parametrize("pin", ["483916", "705284", "629471", "318264", "90210457"])
def test_a_reasonable_pin_is_accepted(pin: str) -> None:
    assert validate_pin(pin) == pin


def test_the_demo_pins_obey_the_same_policy() -> None:
    """A base de demonstração é onde o cliente aprende o que é normal.

    Uma demonstração com `1234` ensina `1234`, e a política que recusa o PIN
    fraco do cliente não pode abrir exceção para a própria demonstração.
    """
    for pin in (
        DEMO_OPERATOR_PIN, DEMO_MANAGER_PIN, DEMO_OWNER_PIN, DEMO_WAITER_PIN,
    ):
        assert validate_pin(pin) == pin


def test_the_pin_is_never_stored_in_the_clear(store) -> None:  # noqa: ANN001
    """O `pdv_local.db` pode ser aberto pelo operador."""
    database, _config = store

    rows = database.query_all("SELECT login, pin_hash FROM users")

    assert rows
    for row in rows:
        stored = str(row["pin_hash"])
        assert stored.startswith("$argon2id$"), stored[:20]
        assert DEMO_MANAGER_PIN not in stored
        assert DEMO_OPERATOR_PIN not in stored
        assert DEMO_OWNER_PIN not in stored


def test_two_users_with_the_same_pin_get_different_hashes(store) -> None:  # noqa: ANN001
    """Sal por hash. Sem ele, hashes iguais entregariam PINs iguais."""
    assert hash_pin("483916") != hash_pin("483916")


def test_importing_a_legacy_pin_can_skip_the_policy(store) -> None:  # noqa: ANN001
    """Recusar um cadastro antigo da retaguarda travaria a loja inteira.

    A exceção é só para importação; nunca para um PIN digitado agora.
    """
    assert hash_pin("1234", enforce_policy=False).startswith("$argon2id$")
    with pytest.raises(WeakPinError):
        hash_pin("1234")


# --------------------------------------------------------------------------- #
# Quem é você ≠ o que você pode liberar
# --------------------------------------------------------------------------- #


def test_a_cashier_can_log_in_but_cannot_authorize(store) -> None:  # noqa: ANN001
    """A separação que o módulo existe para manter.

    Ana precisa entrar para trabalhar. Ana liberar o próprio cancelamento é o
    furto inteiro em um passo.
    """
    database, config = store
    service = AuthorizationService(database, config.tenant_id)

    identity = service.authenticate(DEMO_OPERATOR_LOGIN, DEMO_OPERATOR_PIN)
    assert identity.role == "cashier"
    assert identity.can_authorize is False

    with pytest.raises(AuthorizationRequiredError, match="permissão"):
        service.authorize(DEMO_OPERATOR_LOGIN, DEMO_OPERATOR_PIN)


def test_a_waiter_can_log_in_with_their_own_credential(store) -> None:  # noqa: ANN001
    """O aparelho é pareado uma vez; quem troca a cada turno é a pessoa."""
    database, config = store
    service = AuthorizationService(database, config.tenant_id)

    identity = service.authenticate(DEMO_WAITER_LOGIN, DEMO_WAITER_PIN)

    assert identity.role == "waiter"
    assert identity.first_name == "João"
    assert identity.can_authorize is False


def test_the_manager_ceiling_still_holds(store) -> None:  # noqa: ANN001
    database, config = store
    service = AuthorizationService(database, config.tenant_id)

    with pytest.raises(AuthorizationRequiredError, match="30"):
        service.authorize_discount(
            DEMO_MANAGER_LOGIN, DEMO_MANAGER_PIN, Decimal("50")
        )


def test_trying_to_authorize_without_permission_counts_as_a_failure(store) -> None:  # noqa: ANN001
    """Insistir em liberar o que não se pode é sinal, não engano."""
    database, config = store
    service = AuthorizationService(database, config.tenant_id)

    for _ in range(2):
        with pytest.raises(AuthorizationRequiredError):
            service.authorize(DEMO_OPERATOR_LOGIN, DEMO_OPERATOR_PIN)

    row = database.query_one(
        "SELECT failures FROM auth_throttle WHERE scope = ?",
        (f"login:{DEMO_OPERATOR_LOGIN}",),
    )
    assert int(row["failures"]) == 2


def test_an_inactive_user_cannot_log_in(store) -> None:  # noqa: ANN001
    """Desligar alguém precisa ter efeito imediato no terminal."""
    database, config = store
    with database.transaction() as connection:
        connection.execute(
            "UPDATE users SET is_active = 0 WHERE login = ?", (DEMO_OPERATOR_LOGIN,)
        )

    service = AuthorizationService(database, config.tenant_id)

    with pytest.raises(AuthorizationRequiredError):
        service.authenticate(DEMO_OPERATOR_LOGIN, DEMO_OPERATOR_PIN)


# --------------------------------------------------------------------------- #
# O login de abertura do caixa
# --------------------------------------------------------------------------- #

pytest.importorskip("PySide6")


@pytest.fixture()
def auth(store):  # noqa: ANN001, ANN201
    database, config = store
    return AuthorizationService(database, config.tenant_id)


def _dialog(qtbot, auth):  # noqa: ANN001, ANN202
    from pdv.ui.login_dialog import LoginDialog

    dialog = LoginDialog(auth, store_name="Confeitaria Demo")
    qtbot.addWidget(dialog)
    return dialog


def test_the_counter_does_not_open_without_a_credential(qtbot, auth) -> None:  # noqa: ANN001
    """Até aqui o PDV abria já operando, com o operador fixado no código.

    Toda venda e todo cancelamento saíam no nome da mesma pessoa, inclusive os
    feitos por outra. Num sistema cujo módulo central é anti-furto, isso
    esvazia a trilha: "quem fez" era sempre a mesma resposta.
    """
    dialog = _dialog(qtbot, auth)
    dialog._login.setText(DEMO_OPERATOR_LOGIN)
    dialog._pin.setText("999999")

    dialog._try_login()

    assert dialog.identity is None
    assert dialog.result() != int(dialog.DialogCode.Accepted)
    assert dialog._pin.text() == "", "o PIN errado não pode ficar no campo"


def test_a_valid_credential_opens_the_counter(qtbot, auth) -> None:  # noqa: ANN001
    dialog = _dialog(qtbot, auth)
    dialog._login.setText(DEMO_OPERATOR_LOGIN)
    dialog._pin.setText(DEMO_OPERATOR_PIN)

    dialog._try_login()

    assert dialog.identity is not None
    assert dialog.identity.name == "Ana Caixa"


def test_a_cashier_opens_the_counter_even_without_authorizing_power(qtbot, auth) -> None:  # noqa: ANN001
    """Entrar e liberar são perguntas diferentes.

    Se o login exigisse `can_authorize`, só o gerente conseguiria abrir o
    caixa — e a loja passaria o turno inteiro operando no nome dele.
    """
    dialog = _dialog(qtbot, auth)
    dialog._login.setText(DEMO_OPERATOR_LOGIN)
    dialog._pin.setText(DEMO_OPERATOR_PIN)

    dialog._try_login()

    assert dialog.identity.can_authorize is False


def test_the_dialog_counts_down_the_lockout(qtbot, auth) -> None:  # noqa: ANN001
    """Botão desabilitado sem explicação faz o operador reiniciar a máquina.

    Que é justamente o que o freio persistente existe para não premiar.
    """
    from PySide6.QtWidgets import QDialogButtonBox

    dialog = _dialog(qtbot, auth)
    dialog._login.setText(DEMO_MANAGER_LOGIN)
    for _ in range(MAX_ATTEMPTS):
        dialog._pin.setText("999999")
        dialog._try_login()

    ok = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok.isEnabled() is False
    assert "Aguarde" in dialog._error.text()
    assert "gerente" in dialog._error.text()
    dialog._countdown.stop()


def test_the_login_field_alone_is_not_enough(qtbot, auth) -> None:  # noqa: ANN001
    dialog = _dialog(qtbot, auth)
    dialog._login.setText(DEMO_OPERATOR_LOGIN)

    dialog._try_login()

    assert dialog.identity is None
    assert "PIN" in dialog._error.text()
