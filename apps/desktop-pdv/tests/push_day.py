"""Um dia de caixa de verdade, do jeito que ele sai pela fila de sincronização.

O push nunca entregou uma venda: a nuvem esperava `quantity_mg` e o caixa
mandava `qty_mg`, e a venda de balcão chegava sem `local_number`, que lá é
obrigatório. O lote inteiro abortava com 500. Nenhum teste pegou porque os dois
lados eram testados cada um contra o seu próprio dublê: o daqui aceitava
qualquer coisa, e o de lá recebia payloads escritos à mão no formato de lá.

Este módulo roda os fluxos reais do caixa — balcão com receita e cancelamento,
mesa do garçom do início ao fim, caixa, cashback, pré-pago, fiado, níveis de
desconto, cadastro de mesa — e devolve a fila exatamente como o transporte a
enviaria. O resultado vira `contracts/push-day.json` na raiz do monorepo, que a
nuvem aplica no `SyncMerger` real contra Postgres. As duas pontas passam a ser
testadas contra o MESMO arquivo.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from pdv.config import AppConfig, PrinterConfig, ScaleConfig
from pdv.data.database import Database
from pdv.data.repositories import ProductRepository
from pdv.data.seed import (
    DEMO_MANAGER_ID,
    DEMO_OPERATOR_ID,
    DEMO_WAITER_ID,
    seed_demo_data,
)
from pdv.domain.models import (
    Cents,
    EntityId,
    Grams,
    Payment,
    PaymentMethod,
    ScaleReading,
    ScaleStatus,
    new_id,
)
from pdv.edge.orders import TableOrderService
from pdv.edge.tables import TableService
from pdv.services.cash_session import CashSessionService
from pdv.services.cashback import CashbackService
from pdv.services.checkout import CheckoutService
from pdv.services.credit_account import CreditAccountService
from pdv.services.discount_tiers import DiscountTierService
from pdv.services.prepaid import PrepaidService

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
CURRENT = CONTRACTS / "push-day.json"

TENANT = "c0c0c0c0-0000-4000-8000-000000000001"
STORE = "c0c0c0c0-0000-4000-8000-000000000002"
DEVICE = "c0c0c0c0-0000-4000-8000-000000000003"
#: Segredo só deste arquivo. A nuvem precisa dele para revalidar a cadeia de
#: auditoria — é o mesmo papel do segredo que o terminal recebe na ativação.
SECRET = b"segredo-do-contrato-dia-de-caixa"
PHONE = EntityId("c0c0c0c0-0000-4000-8000-0000000000aa")


def run_day(tmp_path: Path) -> list[dict[str, object]]:
    """Roda o dia e devolve a fila, na ordem de envio."""
    config = AppConfig(
        tenant_id=TENANT,
        store_id=STORE,
        device_id=DEVICE,
        device_secret=SECRET,
        database_path=tmp_path / "pdv.db",
        scale=ScaleConfig(protocol="simulated"),
        printer=PrinterConfig(backend="file", output_dir=tmp_path / "cupons"),
    )
    database = Database(config.database_path)
    database.migrate()
    seed_demo_data(database, config)
    # O que o cadastro de demonstração enfileira não é o dia de ninguém.
    database.connection.execute("DELETE FROM sync_outbox")

    operator = EntityId(DEMO_OPERATOR_ID)
    manager = EntityId(DEMO_MANAGER_ID)
    waiter = EntityId(DEMO_WAITER_ID)
    products = {
        p.sku: p
        for p in ProductRepository(database.connection).list_active(EntityId(TENANT))
    }

    drawer = CashSessionService(database, config)
    drawer.open(operator_id=operator, opening_cents=Cents(10_000))

    checkout = CheckoutService(database, config)

    # 1. Balcão: torta pesada (receita -> baixa de insumo) paga em dinheiro.
    checkout.open_sale(operator)
    checkout.register_weighed_item(
        product=products["TORTA-CHOC"],
        reading=ScaleReading(ScaleStatus.STABLE, Grams(892), "00892"),
        operator_id=operator,
    )
    total = checkout.current_sale.total_cents
    checkout.finalize_sale(
        payments=(Payment(PaymentMethod.CASH, Cents(int(total) + 150)),),
        operator_id=operator,
        operator_name="Ana Caixa",
    )

    # 2. Balcão com item cancelado pelo gerente (estorno de insumo), em Pix.
    checkout.open_sale(operator)
    checkout.register_unit_item(
        product=products["CAFE-EXP"], quantity=Decimal("2"), operator_id=operator
    )
    checkout.register_weighed_item(
        product=products["BOLO-CENOURA"],
        reading=ScaleReading(ScaleStatus.STABLE, Grams(410), "00410"),
        operator_id=operator,
    )
    checkout.cancel_item(
        index=1, operator_id=operator, authorizer_id=manager, reason="Cliente desistiu"
    )
    total = checkout.current_sale.total_cents
    checkout.finalize_sale(
        payments=(Payment(PaymentMethod.PIX, Cents(int(total))),),
        operator_id=operator,
        operator_name="Ana Caixa",
    )

    # 3. Cliente identificado: cashback, pré-pago, fiado e nível de desconto.
    cashback = CashbackService(database, config)
    cashback.configure(
        percent=Decimal("5"), max_per_sale_cents=Cents(2_000), validity_days=90,
        actor_user_id=manager,
    )
    customer = cashback.create_customer(name="Lia Cliente", phone="11988887777")
    PrepaidService(database, config).deposit(
        customer_id=customer, amount_cents=Cents(5_000),
        actor_user_id=operator, authorizer_user_id=manager,
    )
    credit = CreditAccountService(database, config)
    credit.configure(
        customer_id=customer, limit_cents=Cents(20_000), due_days=30,
        actor_user_id=manager, authorizer_user_id=manager,
    )
    credit.configure(
        customer_id=customer, limit_cents=Cents(30_000), due_days=30,
        actor_user_id=manager, authorizer_user_id=manager,
    )
    tiers = DiscountTierService(database, config)
    tiers.configure(
        code="employee", name="Funcionário", percent=Decimal("10"), priority=10,
        requires_manager=True, actor_user_id=manager,
    )
    tiers.configure(
        code="employee", name="Funcionário", percent=Decimal("15"), priority=10,
        requires_manager=True, actor_user_id=manager,
    )
    gold = tiers.configure(
        code="gold", name="Ouro", percent=Decimal("5"), priority=5,
        requires_manager=False, actor_user_id=manager,
    )
    bronze = tiers.configure(
        code="bronze", name="Bronze", percent=Decimal("2"), priority=1,
        requires_manager=False, actor_user_id=manager,
    )
    tiers.assign(customer_id=customer, tier_id=bronze.id, actor_user_id=manager)
    tiers.assign(customer_id=customer, tier_id=gold.id, actor_user_id=manager)

    checkout.open_sale(operator)
    checkout.register_unit_item(
        product=products["CAFE-EXP"], quantity=Decimal("3"), operator_id=operator
    )
    total = int(checkout.current_sale.total_cents)
    checkout.finalize_sale(
        payments=(
            Payment(PaymentMethod.PREPAID, Cents(total // 2)),
            Payment(PaymentMethod.CREDIT_ACCOUNT, Cents(total - total // 2)),
        ),
        operator_id=operator,
        operator_name="Ana Caixa",
        customer_id=customer,
        customer_name="Lia Cliente",
    )
    cashback.redeem(
        customer_id=customer, order_id=EntityId(new_id()),
        amount_cents=Cents(1), actor_user_id=operator,
    )
    credit.pay(customer_id=customer, amount_cents=Cents(500), actor_user_id=operator)

    # 4. Cadastro do salão: mesa nova, renomeada, e uma aposentada.
    tables = TableService(database, config)
    terrace = tables.create(label="Varanda 9", area="Varanda", seats=2)
    tables.update(terrace.id, label="Varanda 10", seats=4)
    retired = tables.create(label="Mesa 99", area="Salão", seats=4)
    tables.set_active(retired.id, False)

    # 5. Mesa do garçom do início ao fim: abre, lança, pede a conta, muda de
    #    mesa e o caixa recebe. E uma segunda comanda aberta por engano.
    orders = TableOrderService(database, config)
    free = [t for t in tables.list_tables() if t.status == "free"]
    order = orders.open_order(
        client_uuid=EntityId(new_id()), operator_id=waiter,
        table_id=free[0].id, origin_device_id=PHONE,
    )
    orders.add_item(
        order_id=order.id, client_uuid=EntityId(new_id()),
        product_id=products["CAFE-EXP"].id, quantity=Decimal("2"),
        created_by_user_id=waiter,
    )
    orders.request_bill(order.id)
    orders.transfer(
        order_id=order.id, table_id=free[1].id,
        authorizer_id=manager, authorizer_name="Bruno Gerente",
    )
    settled = orders.get_order(order.id)
    orders.settle(
        order_id=order.id,
        payments=(Payment(PaymentMethod.DEBIT, Cents(int(settled.total_cents) + 200)),),
        operator_id=operator, operator_name="Ana Caixa", tip_cents=Cents(200),
    )

    mistake = orders.open_order(
        client_uuid=EntityId(new_id()), operator_id=waiter,
        table_id=free[2].id, origin_device_id=PHONE,
    )
    orders.add_item(
        order_id=mistake.id, client_uuid=EntityId(new_id()),
        product_id=products["CAFE-EXP"].id, quantity=Decimal("1"),
        created_by_user_id=waiter,
    )
    orders.cancel_order(
        order_id=mistake.id, authorizer_id=manager, authorizer_name="Bruno Gerente",
        reason="Mesa aberta por engano",
    )

    # 6. Dividir e juntar conta no caixa: um item muda de mesa, um cliente paga
    #    só o que consumiu, e as duas comandas viram uma.
    def table_with(table_id: EntityId, items: int):  # noqa: ANN202
        opened = orders.open_order(
            client_uuid=EntityId(new_id()), operator_id=waiter,
            table_id=table_id, origin_device_id=PHONE,
        )
        for _ in range(items):
            orders.add_item(
                order_id=opened.id, client_uuid=EntityId(new_id()),
                product_id=products["CAFE-EXP"].id, quantity=Decimal("1"),
                created_by_user_id=waiter,
            )
        return opened

    def live(order_id: EntityId) -> list[EntityId]:
        return [
            EntityId(str(r["id"])) for r in database.query_all(
                "SELECT id FROM order_items WHERE order_id = ? AND canceled_at IS NULL "
                "ORDER BY created_at", (order_id,),
            )
        ]

    big, small = table_with(free[3].id, 3), table_with(free[4].id, 1)
    orders.move_items(
        source_order_id=big.id, target_order_id=small.id,
        item_ids=live(big.id)[:1], operator_id=operator, operator_name="Ana Caixa",
    )
    orders.settle_items(
        order_id=big.id, item_ids=live(big.id)[:1],
        payments=(Payment(PaymentMethod.PIX, products["CAFE-EXP"].price_cents),),
        operator_id=operator, operator_name="Ana Caixa",
    )
    orders.merge_orders(
        source_order_id=big.id, target_order_id=small.id,
        operator_id=operator, operator_name="Ana Caixa",
    )
    joined = orders.get_order(small.id)
    orders.settle(
        order_id=small.id, payments=(Payment(PaymentMethod.CASH, joined.total_cents),),
        operator_id=operator, operator_name="Ana Caixa",
    )

    drawer.close(declared_cents=Cents(14_000), operator_id=operator, authorizer_id=manager)

    rows = database.query_all(
        "SELECT entity_table, entity_id, client_uuid, operation, payload_json "
        "FROM sync_outbox ORDER BY seq"
    )
    database.close()
    return [
        {
            "entity_table": row["entity_table"],
            "entity_id": row["entity_id"],
            "client_uuid": row["client_uuid"],
            "operation": row["operation"],
            "payload": json.loads(row["payload_json"]),
        }
        for row in rows
    ]


def document(items: list[dict[str, object]], *, caixa: str) -> dict[str, object]:
    return {
        "descricao": (
            "Fila de sincronização de um dia de caixa real, gerada por "
            "apps/desktop-pdv/tests/push_day.py. Não edite à mão: rode "
            "PDV_UPDATE_CONTRACT=1 python -m pytest tests/test_push_contract.py."
        ),
        "caixa": caixa,
        "tenant_id": TENANT,
        "store_id": STORE,
        "device_id": DEVICE,
        "device_secret_hex": SECRET.hex(),
        "items": items,
    }


def shapes(items: list[dict[str, object]]) -> set[tuple[str, str, tuple[str, ...]]]:
    """O formato de cada item: tabela, operação e o conjunto de chaves.

    É o que o teste compara, e não os valores: ids e horários mudam a cada
    execução, e comparar o arquivo inteiro faria o teste falhar por nada.
    """
    return {
        (str(i["entity_table"]), str(i["operation"]), tuple(sorted(i["payload"])))  # type: ignore[arg-type]
        for i in items
    }
