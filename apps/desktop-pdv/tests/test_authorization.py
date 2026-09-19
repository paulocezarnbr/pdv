"""Testes da autorização de gerente e das operações que dependem dela.

O que estes testes protegem é um controle **antifurto**, não uma tela de login.
A diferença importa: um login frouxo deixa alguém entrar; uma autorização
frouxa fabrica no relatório a prova de que um gerente liberou um cancelamento
que nunca existiu. A auditoria fica formalmente íntegra e materialmente falsa —
e é justamente o relatório que o dono usa para decidir em quem confiar.

Por isso a maior parte do arquivo cobre os *caminhos de recusa*.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from pdv.config import AppConfig, PrinterConfig
from pdv.data.database import Database
from pdv.data.repositories import ProductRepository
from pdv.data.seed import (
    DEMO_MANAGER_ID,
    DEMO_MANAGER_LOGIN,
    DEMO_MANAGER_PIN,
    DEMO_OPERATOR_ID,
    DEMO_OPERATOR_PIN,
    seed_demo_data,
)
from pdv.domain.errors import AuthorizationRequiredError, InvalidWeightError, PdvError
from pdv.domain.models import EntityId, PricingMode
from pdv.services.authorization import (
    MAX_ATTEMPTS,
    AuthorizationService,
    hash_pin,
)
from pdv.services.checkout import CheckoutService

TENANT = "11111111-1111-1111-1111-111111111111"
STORE = "22222222-2222-2222-2222-222222222222"
DEVICE = "33333333-3333-3333-3333-333333333333"


@pytest.fixture()
def env(tmp_path: Path):  # noqa: ANN201
    config = AppConfig(
        tenant_id=TENANT,
        store_id=STORE,
        device_id=DEVICE,
        database_path=tmp_path / "pdv.db",
        printer=PrinterConfig(backend="file", output_dir=tmp_path / "out"),
    )
    database = Database(config.database_path)
    database.migrate()
    seed_demo_data(database, config)
    return database, config, AuthorizationService(database, TENANT)


def _unit_product(database: Database):  # noqa: ANN202
    products = ProductRepository(database.connection).list_active(EntityId(TENANT))
    return next(p for p in products if p.pricing_mode is PricingMode.UNIT)


def _weighed_product(database: Database):  # noqa: ANN202
    products = ProductRepository(database.connection).list_active(EntityId(TENANT))
    return next(p for p in products if p.is_weighed)


# --------------------------------------------------------------------------- #
# Credencial
# --------------------------------------------------------------------------- #


def test_the_manager_pin_authorizes(env) -> None:  # noqa: ANN001
    _database, _config, auth = env

    authorizer = auth.authorize(DEMO_MANAGER_LOGIN, DEMO_MANAGER_PIN)

    assert authorizer.id == DEMO_MANAGER_ID
    assert authorizer.max_discount_percent == Decimal("30")


def test_a_wrong_pin_is_refused(env) -> None:  # noqa: ANN001
    _database, _config, auth = env

    with pytest.raises(AuthorizationRequiredError):
        auth.authorize(DEMO_MANAGER_LOGIN, "0000")


def test_the_cashier_pin_does_not_authorize(env) -> None:  # noqa: ANN001
    """Credencial correta, poder ausente.

    Este é o caso que um `if senha == algo` nunca pega: a Ana existe, o PIN
    dela está certo, e mesmo assim ela não pode liberar o próprio
    cancelamento. Autorizar a si mesmo é o furto inteiro em um passo.
    """
    _database, _config, auth = env

    with pytest.raises(AuthorizationRequiredError, match="permissão"):
        auth.authorize("ana", DEMO_OPERATOR_PIN)


def test_an_unknown_login_is_refused_without_naming_it(env) -> None:  # noqa: ANN001
    """A mensagem é a mesma de PIN errado — não confirma quais logins existem."""
    _database, _config, auth = env

    with pytest.raises(AuthorizationRequiredError) as unknown:
        auth.authorize("fantasma", "1234")
    with pytest.raises(AuthorizationRequiredError) as wrong_pin:
        auth.authorize(DEMO_MANAGER_LOGIN, "9999")

    assert str(unknown.value) == str(wrong_pin.value)


def test_the_login_is_case_insensitive(env) -> None:  # noqa: ANN001
    _database, _config, auth = env

    assert auth.authorize("BRUNO", DEMO_MANAGER_PIN).id == DEMO_MANAGER_ID


def test_the_pin_is_not_stored_in_clear_text(env) -> None:  # noqa: ANN001
    """O banco local é legível pelo operador — PIN em texto seria o mesmo que
    nenhum PIN."""
    database, _config, _auth = env

    row = database.query_one(
        "SELECT pin_hash FROM users WHERE id = ?", (DEMO_MANAGER_ID,)
    )

    assert DEMO_MANAGER_PIN not in str(row["pin_hash"])
    assert str(row["pin_hash"]).startswith("$argon2id$")


def test_a_user_without_pin_cannot_authorize(env) -> None:  # noqa: ANN001
    """Usuário sincronizado da retaguarda ainda sem PIN local.

    Sem este ramo, `pin_hash` nulo cairia na verificação do Argon2 e o
    comportamento dependeria da biblioteca — um "talvez" no meio do controle.
    """
    database, _config, auth = env
    with database.transaction() as tx:
        tx.execute(
            "INSERT INTO users (id, tenant_id, name, login, role, can_authorize, "
            "max_discount_percent, pin_hash, updated_at) "
            "VALUES ('u-sem-pin', ?, 'Carla Supervisora', 'carla', 'manager', 1, "
            "'20', NULL, datetime('now'))",
            (TENANT,),
        )

    with pytest.raises(AuthorizationRequiredError, match="PIN"):
        auth.authorize("carla", "1234")


def test_a_revoked_user_stops_authorizing(env) -> None:  # noqa: ANN001
    database, _config, auth = env
    with database.transaction() as tx:
        tx.execute("UPDATE users SET is_active = 0 WHERE id = ?", (DEMO_MANAGER_ID,))

    with pytest.raises(AuthorizationRequiredError):
        auth.authorize(DEMO_MANAGER_LOGIN, DEMO_MANAGER_PIN)


def test_only_authorizers_are_offered_in_the_dialog(env) -> None:  # noqa: ANN001
    _database, _config, auth = env

    assert auth.list_authorizers() == [DEMO_MANAGER_LOGIN]


# --------------------------------------------------------------------------- #
# Freio de força bruta
# --------------------------------------------------------------------------- #


def test_repeated_failures_lock_the_login_out(env) -> None:  # noqa: ANN001
    """10 000 combinações de um PIN de 4 dígitos passam rápido sem freio."""
    _database, _config, auth = env

    for _ in range(MAX_ATTEMPTS):
        with pytest.raises(AuthorizationRequiredError):
            auth.authorize(DEMO_MANAGER_LOGIN, "0000")

    with pytest.raises(AuthorizationRequiredError, match="Aguarde"):
        auth.authorize(DEMO_MANAGER_LOGIN, DEMO_MANAGER_PIN)


def test_the_lockout_is_per_login(env) -> None:  # noqa: ANN001
    """Travar o gerente porque alguém errou o login da Ana pararia a loja."""
    _database, _config, auth = env

    for _ in range(MAX_ATTEMPTS):
        with pytest.raises(AuthorizationRequiredError):
            auth.authorize("ana", "0000")

    assert auth.authorize(DEMO_MANAGER_LOGIN, DEMO_MANAGER_PIN).id == DEMO_MANAGER_ID


def test_a_success_clears_the_failure_count(env) -> None:  # noqa: ANN001
    _database, _config, auth = env

    for _ in range(MAX_ATTEMPTS - 1):
        with pytest.raises(AuthorizationRequiredError):
            auth.authorize(DEMO_MANAGER_LOGIN, "0000")
    auth.authorize(DEMO_MANAGER_LOGIN, DEMO_MANAGER_PIN)

    # Se o contador não zerasse, o próximo erro isolado bloquearia o gerente.
    with pytest.raises(AuthorizationRequiredError):
        auth.authorize(DEMO_MANAGER_LOGIN, "0000")
    assert auth.authorize(DEMO_MANAGER_LOGIN, DEMO_MANAGER_PIN).id == DEMO_MANAGER_ID


def test_two_hashes_of_the_same_pin_differ(env) -> None:  # noqa: ANN001
    """Sal por hash: PINs iguais entre usuários não se denunciam no banco."""
    assert hash_pin("483916") != hash_pin("483916")


# --------------------------------------------------------------------------- #
# Desconto — o teto é de quem autoriza
# --------------------------------------------------------------------------- #


def test_a_discount_within_the_ceiling_is_authorized(env) -> None:  # noqa: ANN001
    _database, _config, auth = env

    authorizer = auth.authorize_discount(
        DEMO_MANAGER_LOGIN, DEMO_MANAGER_PIN, Decimal("10")
    )

    assert authorizer.id == DEMO_MANAGER_ID


def test_the_right_pin_does_not_lift_the_ceiling(env) -> None:  # noqa: ANN001
    """Limite que a senha certa contorna é limite decorativo."""
    _database, _config, auth = env

    with pytest.raises(AuthorizationRequiredError, match="30"):
        auth.authorize_discount(DEMO_MANAGER_LOGIN, DEMO_MANAGER_PIN, Decimal("50"))


# --------------------------------------------------------------------------- #
# Item unitário
# --------------------------------------------------------------------------- #


def test_a_unit_item_is_registered_with_the_catalogue_price(env) -> None:  # noqa: ANN001
    database, config, _auth = env
    checkout = CheckoutService(database, config)
    product = _unit_product(database)

    item = checkout.register_unit_item(
        product=product,
        quantity=Decimal("3"),
        operator_id=EntityId(DEMO_OPERATOR_ID),
    )

    assert int(item.total_cents) == int(product.price_cents) * 3
    assert int(checkout.current_sale.total_cents) == int(item.total_cents)


def test_a_unit_item_without_a_recipe_does_not_break(env) -> None:  # noqa: ANN001
    """Café não tem ficha técnica, e isso é normal.

    O caminho do pesado exige receita — sem ela não há o que baixar. O unitário
    não pode herdar essa exigência, ou metade do catálogo de uma cafeteria
    ficaria invendável.
    """
    database, config, _auth = env
    checkout = CheckoutService(database, config)

    item = checkout.register_unit_item(
        product=_unit_product(database),
        quantity=Decimal("1"),
        operator_id=EntityId(DEMO_OPERATOR_ID),
    )

    assert item.consumptions == ()
    movements = database.query_one("SELECT COUNT(*) AS n FROM stock_movements")["n"]
    assert movements == 0


def test_a_weighed_product_cannot_be_typed_in_as_a_unit(env) -> None:  # noqa: ANN001
    """Quantidade digitada no lugar do peso é o furto que o M09 combate."""
    database, config, _auth = env
    checkout = CheckoutService(database, config)

    with pytest.raises(InvalidWeightError, match="balança"):
        checkout.register_unit_item(
            product=_weighed_product(database),
            quantity=Decimal("1"),
            operator_id=EntityId(DEMO_OPERATOR_ID),
        )


def test_a_non_positive_quantity_is_refused(env) -> None:  # noqa: ANN001
    """Quantidade negativa seria um estorno disfarçado de venda — e passaria
    longe do cancelamento, que exige gerente."""
    database, config, _auth = env
    checkout = CheckoutService(database, config)

    with pytest.raises(InvalidWeightError):
        checkout.register_unit_item(
            product=_unit_product(database),
            quantity=Decimal("-1"),
            operator_id=EntityId(DEMO_OPERATOR_ID),
        )


def test_a_unit_item_is_audited(env) -> None:  # noqa: ANN001
    database, config, _auth = env
    checkout = CheckoutService(database, config)

    checkout.register_unit_item(
        product=_unit_product(database),
        quantity=Decimal("2"),
        operator_id=EntityId(DEMO_OPERATOR_ID),
    )

    events = database.query_all(
        "SELECT event_type FROM audit_ledger ORDER BY seq"
    )
    assert [e["event_type"] for e in events] == ["item_registered"]


def test_mixed_items_add_up(env) -> None:  # noqa: ANN001
    """Bolo por quilo e café por unidade na mesma venda — o caso real."""
    database, config, _auth = env
    checkout = CheckoutService(database, config)
    product = _unit_product(database)

    first = checkout.register_unit_item(
        product=product, quantity=Decimal("1"),
        operator_id=EntityId(DEMO_OPERATOR_ID),
    )
    second = checkout.register_unit_item(
        product=product, quantity=Decimal("2"),
        operator_id=EntityId(DEMO_OPERATOR_ID),
    )

    total = int(checkout.current_sale.total_cents)
    assert total == int(first.total_cents) + int(second.total_cents)
    row = database.query_one(
        "SELECT total_cents FROM orders WHERE id = ?", (checkout.current_sale.id,)
    )
    assert int(row["total_cents"]) == total, "o total persistido tem de acompanhar"


# --------------------------------------------------------------------------- #
# Desconto aplicado na venda
# --------------------------------------------------------------------------- #


def test_the_discount_is_stored_in_cents_with_the_authorizer(env) -> None:  # noqa: ANN001
    database, config, _auth = env
    checkout = CheckoutService(database, config)
    product = _unit_product(database)
    checkout.register_unit_item(
        product=product, quantity=Decimal("2"),
        operator_id=EntityId(DEMO_OPERATOR_ID),
    )
    subtotal = int(checkout.current_sale.subtotal_cents)

    discount = checkout.apply_discount(
        percent=Decimal("10"),
        operator_id=EntityId(DEMO_OPERATOR_ID),
        authorizer_id=EntityId(DEMO_MANAGER_ID),
        reason="cliente fidelidade",
    )

    assert int(discount) == round(subtotal * 0.10)
    assert int(checkout.current_sale.total_cents) == subtotal - int(discount)

    event = database.query_one(
        "SELECT authorizer_user_id, severity FROM audit_ledger "
        " WHERE event_type = 'discount_applied'"
    )
    assert event["authorizer_user_id"] == DEMO_MANAGER_ID
    assert event["severity"] == "warning"


def test_a_discount_without_items_is_refused(env) -> None:  # noqa: ANN001
    database, config, _auth = env
    checkout = CheckoutService(database, config)

    with pytest.raises(PdvError):
        checkout.apply_discount(
            percent=Decimal("10"),
            operator_id=EntityId(DEMO_OPERATOR_ID),
            authorizer_id=EntityId(DEMO_MANAGER_ID),
            reason="—",
        )
