"""A identidade da pessoa no salão — sessão, gorjeta e resultado por funcionário.

O que estes testes protegem
---------------------------

O pedido era atribuído ao **celular**. Disso decorriam duas coisas, e nenhuma
delas aparecia como erro em lugar nenhum:

* resultado e gorjeta por funcionário eram impossíveis de apurar — três garçons
  revezando o mesmo tablet viravam uma coluna só;
* a trilha de auditoria respondia "quem cancelou?" com o nome de um objeto.

O arquivo cobre as duas credenciais (aparelho e pessoa), a fronteira entre elas,
o recebimento da conta no caixa e a apuração do turno.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from pdv.config import AppConfig, PrinterConfig, StockConfig
from pdv.data.database import Database
from pdv.data.seed import (
    DEMO_MANAGER_LOGIN,
    DEMO_MANAGER_PIN,
    DEMO_WAITER2_LOGIN,
    DEMO_WAITER2_PIN,
    DEMO_WAITER_ID,
    DEMO_WAITER_LOGIN,
    DEMO_WAITER_NAME,
    DEMO_WAITER_PIN,
    seed_demo_data,
)
from pdv.domain.errors import AuthorizationRequiredError, InsufficientPaymentError
from pdv.domain.models import Cents, EntityId, Payment, PaymentMethod, new_id
from pdv.edge.auth import EdgeAuth
from pdv.edge.orders import OrderClosedError, TableOrderService
from pdv.edge.staff import SESSION_TTL, StaffAuthError, StaffSessions
from pdv.services.staff_report import StaffReport, business_day_window

TENANT = "11111111-1111-1111-1111-111111111111"
STORE = "22222222-2222-2222-2222-222222222222"
DEVICE = "33333333-3333-3333-3333-333333333333"


@pytest.fixture()
def env(tmp_path: Path):  # noqa: ANN201
    config = AppConfig(
        tenant_id=TENANT,
        store_id=STORE,
        device_id=DEVICE,
        store_name="Confeitaria Aurora",
        database_path=tmp_path / "pdv.db",
        printer=PrinterConfig(backend="file", output_dir=tmp_path / "out"),
        stock=StockConfig(),
    )
    database = Database(config.database_path)
    database.migrate()
    seed_demo_data(database, config)
    return database, config


@pytest.fixture()
def phone(env):  # noqa: ANN001, ANN201
    """Um aparelho pareado, sem ninguém dentro."""
    database, config = env
    auth = EdgeAuth(database, config.tenant_id, config.store_id)
    code, _ = auth.create_pairing_code()
    token = auth.pair(code, device_name="Celular do salão")
    return auth.authenticate(token)


@pytest.fixture()
def sessions(env):  # noqa: ANN001, ANN201
    database, config = env
    return StaffSessions(database, config.tenant_id)


def _unit_product(database: Database):  # noqa: ANN202
    from pdv.data.repositories import ProductRepository

    return next(
        p
        for p in ProductRepository(database.connection).list_active(EntityId(TENANT))
        if not p.is_weighed
    )


def _open_with_item(env, operator_id: str, label: str, quantity: str = "1"):  # noqa: ANN001, ANN202
    database, config = env
    orders = TableOrderService(database, config)
    order = orders.open_order(
        client_uuid=EntityId(new_id()),
        operator_id=EntityId(operator_id),
        table_label=label,
        origin_device_id=EntityId("cel-1"),
    )
    orders.add_item(
        order_id=order.id,
        client_uuid=EntityId(new_id()),
        product_id=_unit_product(database).id,
        quantity=Decimal(quantity),
        created_by_user_id=EntityId(operator_id),
    )
    return orders, orders.get_order(order.id)


# --------------------------------------------------------------------------- #
# A sessão
# --------------------------------------------------------------------------- #


def test_the_waiter_logs_in_with_the_same_credential_as_the_counter(
    sessions, phone
) -> None:  # noqa: ANN001
    """Mesma validação do balcão: Argon2id contra a réplica local, offline."""
    session = sessions.login(
        login=DEMO_WAITER_LOGIN, pin=DEMO_WAITER_PIN, device_id=EntityId(phone.id)
    )

    assert session.user_id == DEMO_WAITER_ID
    assert session.name == DEMO_WAITER_NAME
    assert session.first_name == "João"
    assert session.role == "waiter"


def test_a_wrong_pin_does_not_open_a_shift(sessions, phone) -> None:  # noqa: ANN001
    with pytest.raises(AuthorizationRequiredError):
        sessions.login(
            login=DEMO_WAITER_LOGIN, pin="999999", device_id=EntityId(phone.id)
        )


def test_the_session_survives_a_restart_of_the_pdv(env, sessions, phone) -> None:  # noqa: ANN001
    """É a diferença deliberada para a concessão de gerente.

    A do gerente é **poder** e morre com o processo, de propósito. Esta é
    **identidade**: se evaporasse na queda de energia de um sábado, a loja
    inteira redigitaria PIN no meio do serviço — e o caminho de menor
    resistência viraria deixar um login só aberto para todos.
    """
    database, config = env
    session = sessions.login(
        login=DEMO_WAITER_LOGIN, pin=DEMO_WAITER_PIN, device_id=EntityId(phone.id)
    )

    # Um processo novo, com a mesma base em disco.
    database.close()
    restarted = Database(config.database_path)
    restarted.migrate()

    alive = StaffSessions(restarted, TENANT).require(
        session.token, EntityId(phone.id)
    )
    assert alive.user_id == DEMO_WAITER_ID


def test_a_session_does_not_travel_to_another_device(env, sessions, phone) -> None:  # noqa: ANN001
    """Sem isto, um token lido da tela de um celular valeria em qualquer outro."""
    database, config = env
    session = sessions.login(
        login=DEMO_WAITER_LOGIN, pin=DEMO_WAITER_PIN, device_id=EntityId(phone.id)
    )

    auth = EdgeAuth(database, config.tenant_id, config.store_id)
    code, _ = auth.create_pairing_code()
    other = auth.authenticate(auth.pair(code, device_name="Outro celular"))

    with pytest.raises(StaffAuthError):
        sessions.require(session.token, EntityId(other.id))

    # E quem a abriu legitimamente continua com ela.
    assert sessions.require(session.token, EntityId(phone.id)).user_id == DEMO_WAITER_ID


def test_logging_in_ends_the_previous_shift_on_that_device(sessions, phone) -> None:  # noqa: ANN001
    """Troca de turno no mesmo tablet.

    Sem isto, bastaria o app guardar o token antigo para os pedidos da noite
    continuarem saindo no nome de quem já foi para casa.
    """
    first = sessions.login(
        login=DEMO_WAITER_LOGIN, pin=DEMO_WAITER_PIN, device_id=EntityId(phone.id)
    )
    second = sessions.login(
        login=DEMO_WAITER2_LOGIN, pin=DEMO_WAITER2_PIN, device_id=EntityId(phone.id)
    )

    with pytest.raises(StaffAuthError):
        sessions.require(first.token, EntityId(phone.id))
    assert sessions.require(second.token, EntityId(phone.id)).login == DEMO_WAITER2_LOGIN


def test_the_counter_can_end_a_shift_remotely(sessions, phone) -> None:  # noqa: ANN001
    """Alguém foi embora sem sair do app, e o celular ficou no balcão."""
    session = sessions.login(
        login=DEMO_WAITER_LOGIN, pin=DEMO_WAITER_PIN, device_id=EntityId(phone.id)
    )

    assert sessions.revoke_user(EntityId(DEMO_WAITER_ID)) == 1

    with pytest.raises(StaffAuthError):
        sessions.require(session.token, EntityId(phone.id))


def test_an_expired_session_is_refused(env, sessions, phone) -> None:  # noqa: ANN001
    """O turno acaba. O aparelho esquecido na gaveta não amanhece logado."""
    database, _config = env
    session = sessions.login(
        login=DEMO_WAITER_LOGIN, pin=DEMO_WAITER_PIN, device_id=EntityId(phone.id)
    )

    stale = datetime.now(timezone.utc) - timedelta(seconds=1)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE edge_staff_sessions SET expires_at = ?", (stale.isoformat(),)
        )

    with pytest.raises(StaffAuthError):
        sessions.require(session.token, EntityId(phone.id))


def test_the_raw_session_token_is_never_stored(env, sessions, phone) -> None:  # noqa: ANN001
    """Um dump do banco não entrega sessão de ninguém."""
    database, _config = env
    session = sessions.login(
        login=DEMO_WAITER_LOGIN, pin=DEMO_WAITER_PIN, device_id=EntityId(phone.id)
    )

    dump = " ".join(
        str(value)
        for row in database.query_all("SELECT * FROM edge_staff_sessions")
        for value in tuple(row)
    )
    assert session.token not in dump


def test_the_session_lasts_a_shift(sessions, phone) -> None:  # noqa: ANN001
    session = sessions.login(
        login=DEMO_WAITER_LOGIN, pin=DEMO_WAITER_PIN, device_id=EntityId(phone.id)
    )

    remaining = session.expires_at - datetime.now(timezone.utc)
    assert timedelta(hours=13) < remaining <= SESSION_TTL


def test_the_token_only_leaves_on_the_login_response(sessions, phone) -> None:  # noqa: ANN001
    """Repeti-lo em toda consulta o espalharia pelo log de qualquer proxy."""
    session = sessions.login(
        login=DEMO_WAITER_LOGIN, pin=DEMO_WAITER_PIN, device_id=EntityId(phone.id)
    )

    assert "token" in session.to_json(with_token=True)
    assert "token" not in session.to_json()


# --------------------------------------------------------------------------- #
# Receber a conta
# --------------------------------------------------------------------------- #


def test_the_counter_settles_the_table_and_frees_it(env) -> None:  # noqa: ANN001
    """A metade que faltava desde a Fase 3.

    `request_bill` marcava a mesa e a história acabava ali: nada no balcão
    fechava aquele pedido. A mesa ficava ocupada para sempre, e o jeito de
    liberá-la era cancelar a comanda — apagar a venda para sentar o próximo
    cliente.
    """
    database, config = env
    orders, order = _open_with_item(env, DEMO_WAITER_ID, "Mesa 3")
    orders.request_bill(order.id)

    settled = orders.settle(
        order_id=order.id,
        payments=(Payment(PaymentMethod.CASH, Cents(int(order.total_cents))),),
        operator_id=EntityId("caixa"),
        operator_name="Ana Caixa",
    )

    assert settled.order.status == "paid"
    from pdv.edge.tables import TableService

    mesa = next(t for t in TableService(database, config).list_tables() if t.label == "Mesa 3")
    assert mesa.status == "free", "a mesa se libera porque o pedido fechou"


def test_settling_records_how_the_money_came_in(env) -> None:  # noqa: ANN001
    """A tabela `payments` existia no schema e nunca recebia linha.

    Sem ela o sistema sabia *quanto* entrou e não sabia *como*: fechamento por
    forma de pagamento e conciliação de maquininha não tinham de onde sair.
    """
    database, _config = env
    orders, order = _open_with_item(env, DEMO_WAITER_ID, "Mesa 3")
    total = int(order.total_cents)

    orders.settle(
        order_id=order.id,
        payments=(
            Payment(PaymentMethod.PIX, Cents(total - 100)),
            Payment(PaymentMethod.CASH, Cents(500)),
        ),
        operator_id=EntityId("caixa"),
        operator_name="Ana Caixa",
    )

    rows = database.query_all(
        "SELECT method, amount_cents, change_cents FROM payments WHERE order_id = ?",
        (order.id,),
    )
    assert {str(r["method"]) for r in rows} == {"pix", "cash"}
    # O troco sai só no dinheiro: devolver vivo contra eletrônico é o golpe do troco.
    troco = {str(r["method"]): int(r["change_cents"]) for r in rows}
    assert troco["pix"] == 0
    assert troco["cash"] == 400


def test_the_tip_stays_out_of_the_revenue(env) -> None:  # noqa: ANN001
    """Somá-la ao total cobraria imposto sobre dinheiro que é da equipe."""
    orders, order = _open_with_item(env, DEMO_WAITER_ID, "Mesa 3")
    total = int(order.total_cents)

    settled = orders.settle(
        order_id=order.id,
        payments=(Payment(PaymentMethod.CASH, Cents(total + 300)),),
        operator_id=EntityId("caixa"),
        operator_name="Ana Caixa",
        tip_cents=Cents(300),
    )

    assert int(settled.order.total_cents) == total, "a conta não cresce com a gorjeta"
    assert int(settled.order.tip_cents) == 300
    assert int(settled.charged_cents) == total + 300


def test_the_payment_has_to_cover_the_tip_too(env) -> None:  # noqa: ANN001
    """Senão a gorjeta sairia do caixa da loja."""
    orders, order = _open_with_item(env, DEMO_WAITER_ID, "Mesa 3")

    with pytest.raises(InsufficientPaymentError):
        orders.settle(
            order_id=order.id,
            payments=(Payment(PaymentMethod.CASH, Cents(int(order.total_cents))),),
            operator_id=EntityId("caixa"),
            operator_name="Ana Caixa",
            tip_cents=Cents(500),
        )


def test_a_settled_table_cannot_be_charged_twice(env) -> None:  # noqa: ANN001
    orders, order = _open_with_item(env, DEMO_WAITER_ID, "Mesa 3")
    payment = (Payment(PaymentMethod.CASH, Cents(int(order.total_cents))),)
    orders.settle(
        order_id=order.id,
        payments=payment,
        operator_id=EntityId("caixa"),
        operator_name="Ana Caixa",
    )

    with pytest.raises(OrderClosedError):
        orders.settle(
            order_id=order.id,
            payments=payment,
            operator_id=EntityId("caixa"),
            operator_name="Ana Caixa",
        )


def test_an_empty_table_is_not_settled(env) -> None:  # noqa: ANN001
    """Mesa sem consumo se libera cancelando, não recebendo R$ 0,00."""
    database, config = env
    orders = TableOrderService(database, config)
    order = orders.open_order(
        client_uuid=EntityId(new_id()),
        operator_id=EntityId(DEMO_WAITER_ID),
        table_label="Mesa 8",
        origin_device_id=EntityId("cel-1"),
    )

    with pytest.raises(OrderClosedError):
        orders.settle(
            order_id=order.id,
            payments=(Payment(PaymentMethod.CASH, Cents(0)),),
            operator_id=EntityId("caixa"),
            operator_name="Ana Caixa",
        )


def test_settling_prints_a_receipt_naming_the_table(env) -> None:  # noqa: ANN001
    orders, order = _open_with_item(env, DEMO_WAITER_ID, "Mesa 3")

    settled = orders.settle(
        order_id=order.id,
        payments=(Payment(PaymentMethod.CASH, Cents(int(order.total_cents))),),
        operator_id=EntityId("caixa"),
        operator_name="Ana Caixa",
    )

    assert settled.receipt, "a mesa também sai com cupom"
    assert b"Mesa 3" in settled.receipt


def test_settling_names_who_served_in_the_ledger(env) -> None:  # noqa: ANN001
    """Num sistema anti-furto, "quem atendeu" não pode ser o id de um celular."""
    database, _config = env
    orders, order = _open_with_item(env, DEMO_WAITER_ID, "Mesa 3")

    orders.settle(
        order_id=order.id,
        payments=(Payment(PaymentMethod.CASH, Cents(int(order.total_cents))),),
        operator_id=EntityId("caixa"),
        operator_name="Ana Caixa",
    )

    row = database.query_one(
        "SELECT payload_json FROM audit_ledger WHERE event_type = 'sale_closed' "
        " ORDER BY seq DESC LIMIT 1"
    )
    assert DEMO_WAITER_NAME in str(row["payload_json"])
    assert "Ana Caixa" in str(row["payload_json"])


# --------------------------------------------------------------------------- #
# Resultado por funcionário
# --------------------------------------------------------------------------- #


def test_the_report_separates_two_waiters(env) -> None:  # noqa: ANN001
    """O relatório que não existia enquanto o pedido era do celular."""
    database, config = env
    from pdv.data.seed import DEMO_WAITER2_ID

    orders, joao = _open_with_item(env, DEMO_WAITER_ID, "Mesa 1", quantity="2")
    orders.settle(
        order_id=joao.id,
        payments=(Payment(PaymentMethod.CASH, Cents(int(joao.total_cents) + 200)),),
        operator_id=EntityId("caixa"),
        operator_name="Ana Caixa",
        tip_cents=Cents(200),
    )

    _orders2, maria = _open_with_item(env, DEMO_WAITER2_ID, "Mesa 2")
    orders.settle(
        order_id=maria.id,
        payments=(Payment(PaymentMethod.PIX, Cents(int(maria.total_cents))),),
        operator_id=EntityId("caixa"),
        operator_name="Ana Caixa",
    )

    results = {r.name: r for r in StaffReport(database, config).by_waiter()}

    assert results[DEMO_WAITER_NAME].orders == 1
    assert results[DEMO_WAITER_NAME].items == 1
    assert int(results[DEMO_WAITER_NAME].tip_cents) == 200
    assert int(results["Maria Garçonete"].tip_cents) == 0
    assert int(results[DEMO_WAITER_NAME].total_cents) == int(joao.total_cents)


def test_an_open_table_counts_apart_from_a_settled_one(env) -> None:  # noqa: ANN001
    """Comanda aberta não é faturamento — mas o caixa precisa vê-la."""
    database, config = env
    _orders, _order = _open_with_item(env, DEMO_WAITER_ID, "Mesa 1")

    result = StaffReport(database, config).by_waiter()[0]

    assert result.open_orders == 1
    assert result.orders == 0
    assert int(result.total_cents) == 0


def test_a_waiter_who_has_not_served_yet_is_not_an_error(env) -> None:  # noqa: ANN001
    """Começo de turno. Devolver 404 faria o app mostrar falha na abertura."""
    database, config = env

    summary = StaffReport(database, config).for_user(EntityId(DEMO_WAITER_ID))

    assert summary["name"] == DEMO_WAITER_NAME
    assert summary["orders"] == 0
    assert summary["tip_cents"] == 0


def test_the_report_only_lists_who_worked(env) -> None:  # noqa: ANN001
    """Listar a folha inteira com zeros viraria um ranking de quem não trabalhou."""
    database, config = env
    _orders, _order = _open_with_item(env, DEMO_WAITER_ID, "Mesa 1")

    results = StaffReport(database, config).by_waiter()

    assert [r.name for r in results] == [DEMO_WAITER_NAME]


def test_the_business_day_does_not_turn_at_midnight(env) -> None:  # noqa: ANN001
    """A mesa que senta às 23h40 e paga às 00h20 é do turno da noite."""
    late = datetime(2026, 5, 10, 23, 40).astimezone()
    early = datetime(2026, 5, 11, 0, 20).astimezone()

    assert business_day_window(late) == business_day_window(early)

    # E a manhã seguinte, depois das 5h, já é outro dia.
    morning = datetime(2026, 5, 11, 9, 0).astimezone()
    assert business_day_window(morning) != business_day_window(late)


# --------------------------------------------------------------------------- #
# A camada HTTP
# --------------------------------------------------------------------------- #


@pytest.fixture()
def http(env):  # noqa: ANN001, ANN201
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    database, config = env
    from pdv.edge.server import create_app

    with fastapi_testclient.TestClient(create_app(database, config)) as client:
        auth = EdgeAuth(database, config.tenant_id, config.store_id)
        code, _ = auth.create_pairing_code()
        token = auth.pair(code, device_name="Celular do salão")
        yield client, {"Authorization": f"Bearer {token}"}


def test_the_shift_opens_and_closes_over_http(http) -> None:  # noqa: ANN001
    client, device = http

    opened = client.post(
        "/staff/session",
        json={"login": DEMO_WAITER_LOGIN, "pin": DEMO_WAITER_PIN},
        headers=device,
    )
    assert opened.status_code == 200, opened.text
    session = {**device, "X-Staff-Token": opened.json()["token"]}

    assert client.get("/staff/session", headers=session).json()["name"] == DEMO_WAITER_NAME

    assert client.delete("/staff/session", headers=session).json()["ended"] is True
    assert client.get("/staff/session", headers=session).status_code == 403


def test_a_stranger_pin_does_not_open_a_shift_over_http(http) -> None:  # noqa: ANN001
    client, device = http

    response = client.post(
        "/staff/session",
        json={"login": DEMO_WAITER_LOGIN, "pin": "111222"},
        headers=device,
    )

    assert response.status_code == 403


def test_a_pin_without_a_paired_device_is_useless(http) -> None:  # noqa: ANN001
    """As duas camadas se sustentam justamente por serem exigidas juntas."""
    client, _device = http

    response = client.post(
        "/staff/session", json={"login": DEMO_WAITER_LOGIN, "pin": DEMO_WAITER_PIN}
    )

    assert response.status_code == 401


def test_the_order_carries_the_person_not_the_phone(http) -> None:  # noqa: ANN001
    """O ponto de toda esta fase, verificado pela rota."""
    client, device = http
    opened = client.post(
        "/staff/session",
        json={"login": DEMO_WAITER_LOGIN, "pin": DEMO_WAITER_PIN},
        headers=device,
    ).json()
    session = {**device, "X-Staff-Token": opened["token"]}

    table = client.get("/tables", headers=session).json()["tables"][0]
    order = client.post(
        "/orders",
        json={"client_uuid": new_id(), "table_id": table["id"]},
        headers=session,
    ).json()

    detail = client.get(f"/orders/{order['order_id']}", headers=session).json()
    assert detail["operator_id"] == DEMO_WAITER_ID
    assert detail["waiter_name"] == DEMO_WAITER_NAME


def test_the_item_records_who_launched_it(env, http) -> None:  # noqa: ANN001
    """Mesa grande é atendida por mais de uma pessoa."""
    database, _config = env
    client, device = http
    first = client.post(
        "/staff/session",
        json={"login": DEMO_WAITER_LOGIN, "pin": DEMO_WAITER_PIN},
        headers=device,
    ).json()
    joao = {**device, "X-Staff-Token": first["token"]}

    table = client.get("/tables", headers=joao).json()["tables"][0]
    order = client.post(
        "/orders",
        json={"client_uuid": new_id(), "table_id": table["id"]},
        headers=joao,
    ).json()

    # Troca de turno no meio da mesa: Maria entra no mesmo aparelho.
    second = client.post(
        "/staff/session",
        json={"login": DEMO_WAITER2_LOGIN, "pin": DEMO_WAITER2_PIN},
        headers=device,
    ).json()
    maria = {**device, "X-Staff-Token": second["token"]}

    product = next(
        p
        for p in client.get("/menu", headers=maria).json()["products"]
        if p["sellable_by_waiter"]
    )
    client.post(
        f"/orders/{order['order_id']}/items",
        json={"client_uuid": new_id(), "product_id": product["id"], "quantity": "1"},
        headers=maria,
    )

    from pdv.data.seed import DEMO_WAITER2_ID

    detail = client.get(f"/orders/{order['order_id']}", headers=maria).json()
    assert detail["operator_id"] == DEMO_WAITER_ID, "a comanda é de quem a abriu"

    # E o item é de quem o lançou. Sem esta coluna, o segundo garçom sumiria
    # da trilha e todo o consumo da mesa apareceria no nome do primeiro.
    row = database.query_one(
        "SELECT created_by_user_id FROM order_items WHERE order_id = ?",
        (order["order_id"],),
    )
    assert str(row["created_by_user_id"]) == DEMO_WAITER2_ID


def test_the_waiter_sees_only_their_own_numbers(http) -> None:  # noqa: ANN001
    """Ver a gorjeta do colega não é informação de trabalho."""
    client, device = http
    opened = client.post(
        "/staff/session",
        json={"login": DEMO_WAITER_LOGIN, "pin": DEMO_WAITER_PIN},
        headers=device,
    ).json()
    session = {**device, "X-Staff-Token": opened["token"]}

    summary = client.get("/staff/summary", headers=session).json()

    assert summary["user_id"] == DEMO_WAITER_ID
    assert set(summary) >= {"orders", "items", "total_cents", "tip_cents"}


def test_a_manager_grant_still_needs_a_shift(http) -> None:  # noqa: ANN001
    """As três credenciais empilham; nenhuma substitui a outra."""
    client, device = http
    opened = client.post(
        "/staff/session",
        json={"login": DEMO_WAITER_LOGIN, "pin": DEMO_WAITER_PIN},
        headers=device,
    ).json()
    session = {**device, "X-Staff-Token": opened["token"]}

    grant = client.post(
        "/manager/session",
        json={"login": DEMO_MANAGER_LOGIN, "pin": DEMO_MANAGER_PIN},
        headers=session,
    )
    assert grant.status_code == 200, grant.text

    # Gerente autorizado, mas sem turno aberto no aparelho: a rota de escrita
    # continua recusando, porque ela precisa saber quem está agindo.
    sem_turno = {**device, "X-Manager-Token": grant.json()["token"]}
    table = client.get("/tables", headers=device).json()["tables"][0]
    response = client.post(
        "/orders",
        json={"client_uuid": new_id(), "table_id": table["id"]},
        headers=sem_turno,
    )
    assert response.status_code == 403
    assert response.headers.get("X-Auth-Scope") == "staff"
