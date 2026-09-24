"""O terminal de verdade contra a imagem Docker da nuvem.

Por que existe, se `e2e.py` já existe
-------------------------------------

`e2e.py` monta os payloads à mão. Prova a rota, o proxy e o HMAC — e não prova
o que o PDV **de fato** envia. Foi por esse vão que passou o defeito mais caro
do projeto: o fechamento do pedido saía do terminal como INSERT sem
`local_number`, a nuvem o recusava, o lote caía com ele, e nenhuma venda
chegava. Os testes do terminal usavam uma nuvem dublada que aceitava qualquer
coisa; os da nuvem, payloads escritos por quem escreveu a nuvem.

Aqui não há payload escrito à mão. É o código do PDV — `CheckoutService`,
`TableOrderService`, o outbox, o `SyncEngine` e o `HttpTransport`, montados
como em `main.py` — fazendo um turno curto de loja e sincronizando pelo HTTP de
verdade. O que se confere depois é o Postgres.

Como rodar: igual a `e2e.py` (Postgres em 55432, imagem em 3111), mais

    pip install httpx==0.28.1 pydantic==2.13.5 argon2-cffi==25.1.0
    python apps/cloud-api/scripts/e2e_terminal.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import uuid
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "apps" / "desktop-pdv" / "src"))

from pdv.data.database import Database  # noqa: E402
from pdv.data.repositories import ProductRepository  # noqa: E402
from pdv.data.seed import DEMO_MANAGER_ID, DEMO_OPERATOR_ID, seed_demo_data  # noqa: E402
from pdv.domain.models import Cents, EntityId, Payment, PaymentMethod, new_id  # noqa: E402
from pdv.edge.orders import TableOrderService  # noqa: E402
from pdv.edge.tables import TableService  # noqa: E402
from pdv.provisioning.activation import (  # noqa: E402
    HttpActivationTransport, activate, load_sync_token,
)
from pdv.provisioning.secrets import SecretVault  # noqa: E402
from pdv.runtime import DATABASE_NAME, load_runtime  # noqa: E402
from pdv.services.checkout import CheckoutService  # noqa: E402
from pdv.sync.engine import SyncEngine  # noqa: E402
from pdv.sync.protocol import PushBatch  # noqa: E402
from pdv.sync.transport import HttpTransport  # noqa: E402

BASE = "http://localhost:3111"
PG = ["docker", "exec", "erp-pg-test", "psql", "-U", "erp", "-d", "erp", "-tAc"]
failures = 0


def psql(sql: str) -> str:
    result = subprocess.run([*PG, sql], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit(f"psql falhou: {result.stderr}")
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def check(label: str, condition: bool, detail: str = "") -> None:
    global failures
    print(f"  {'ok ' if condition else 'FALHOU'}  {label}" + ("" if condition else f"  -> {detail}"))
    if not condition:
        failures += 1


# -- o tenant e o código, como o painel faria ---------------------------------- #
tenant = psql(f"INSERT INTO tenants (name) VALUES ('Terminal {uuid.uuid4()}') RETURNING id")
store = psql(f"INSERT INTO stores (tenant_id, name) VALUES ('{tenant}', 'Loja') RETURNING id")
device = str(uuid.uuid4())
code = uuid.uuid4().hex[:8].upper()
psql(
    "INSERT INTO device_activation_codes (code_hash, tenant_id, store_id, device_id, label) "
    f"VALUES (encode(sha256('{code}'::bytea), 'hex'), '{tenant}', '{store}', '{device}', 'Caixa')"
)

# -- a instalação: ativação e bootstrap do próprio PDV ------------------------- #
# Nada de ativar à mão: é a ativação do terminal (que precisa entregar o
# segredo do ledger e achar a rota /api sozinha) e o `load_runtime` do
# `PDV.exe` (que precisa ler o que a ativação gravou).
work = Path(tempfile.mkdtemp())
os.environ["PDV_DATA_DIR"] = str(work)
os.environ.pop("PDV_DEVICE_SECRET", None)
provisioning_db = Database(work / DATABASE_NAME)
provisioning_db.migrate()
vault = SecretVault(work / "secrets")
activate(code, database=provisioning_db, vault=vault, transport=HttpActivationTransport(BASE))
provisioning_db.close()
token = load_sync_token(vault)

runtime = load_runtime()
config, database = runtime.config, runtime.database
check("o PDV assume a identidade da ativação",
      (config.tenant_id, config.device_id) == (tenant, device), str(config.tenant_id))
seed_demo_data(database, config)
# O catálogo de demonstração é da nuvem, não do terminal: fora da fila.
database.connection.execute("DELETE FROM sync_outbox")
database.connection.commit()

operator = EntityId(DEMO_OPERATOR_ID)
manager = EntityId(DEMO_MANAGER_ID)
cafe_id = EntityId(str(database.query_one("SELECT id FROM products WHERE sku = 'CAFE-EXP'")["id"]))
cafe = ProductRepository(database.connection).get(cafe_id)

print("\n[1] turno de loja no PDV")
checkout = CheckoutService(database, config)
checkout.open_sale(operator)
checkout.register_unit_item(product=cafe, quantity=Decimal(2), operator_id=operator)
checkout.register_unit_item(product=cafe, quantity=Decimal(1), operator_id=operator)
counter_order = checkout.current_sale.id
canceled_item = checkout.current_sale.items[1].id
checkout.cancel_item(index=1, operator_id=operator, authorizer_id=manager, reason="desistiu")
counter_total = int(checkout.current_sale.total_cents)
checkout.finalize_sale(
    payments=(Payment(PaymentMethod.CASH, Cents(counter_total)),),
    operator_id=operator, operator_name="Ana",
)

tables = TableService(database, config)
orders = TableOrderService(database, config)
phone = EntityId(new_id())
table_order = orders.open_order(
    client_uuid=EntityId(new_id()), operator_id=operator,
    table_id=tables.find_by_label("Mesa 1").id, origin_device_id=phone,
)
orders.add_item(order_id=table_order.id, client_uuid=EntityId(new_id()),
                product_id=cafe_id, quantity=Decimal(3), created_by_user_id=operator)
orders.request_bill(table_order.id)
orders.clear_bill_request(table_order.id)
orders.request_bill(table_order.id)
orders.transfer(order_id=table_order.id, table_id=tables.find_by_label("Mesa 2").id,
                authorizer_id=manager, authorizer_name="Bruno")
table_total = int(orders.get_order(table_order.id).total_cents)
orders.settle(order_id=table_order.id,
              payments=(Payment(PaymentMethod.PIX, Cents(table_total + 300)),),
              operator_id=operator, operator_name="Ana", tip_cents=Cents(300))

mistake = orders.open_order(
    client_uuid=EntityId(new_id()), operator_id=operator,
    table_id=tables.find_by_label("Mesa 3").id, origin_device_id=phone,
)
orders.add_item(order_id=mistake.id, client_uuid=EntityId(new_id()),
                product_id=cafe_id, quantity=Decimal(1))
orders.cancel_order(order_id=mistake.id, authorizer_id=manager,
                    authorizer_name="Bruno", reason="mesa errada")
# O mapa do salão também sobe: a nuvem não tinha a tabela, e cada mesa criada
# ia para a quarentena do terminal para sempre.
varanda = tables.create(label="Deck 1", area="Deck", seats=2)
tables.update(varanda.id, label="Deck A")
queued = database.query_one("SELECT COUNT(*) AS n FROM sync_outbox")["n"]
print(f"  {queued} operações na fila")

print("\n[2] sincronização pelo SyncEngine e HttpTransport do PDV")
engine = SyncEngine(database, HttpTransport(BASE, token), config)
report = engine.drain()
check("nada recusado pela nuvem", report.rejected == 0, str(report))
check("a fila esvaziou", engine.pending_count() == 0, f"restam {engine.pending_count()}")
check("nada em quarentena", engine.quarantined_count() == 0)

print("\n[3] o que a nuvem guardou")


def order_row(order_id: str) -> list[str]:
    return psql(
        "SELECT concat_ws('|', status, channel, local_number, total_cents, tip_cents, "
        f"coalesce(customer_id, '')) FROM orders WHERE id = '{order_id}'"
    ).split("|")


row = order_row(counter_order)
check("a venda do balcão chegou paga", row[:2] == ["paid", "counter"], str(row))
check("com o total depois do cancelamento", row[3] == str(counter_total), str(row))
check("o item cancelado está cancelado na nuvem",
      psql(f"SELECT canceled_at IS NOT NULL FROM order_items WHERE id = '{canceled_item}'") == "t")

row = order_row(table_order.id)
check("a mesa chegou recebida", row[:2] == ["paid", "waiter"], str(row))
check("na mesa para onde foi transferida", row[5] == "Mesa 2", str(row))
check("com a gorjeta por fora do total", row[3:5] == [str(table_total), "300"], str(row))
check("o pedido de conta foi limpo e refeito",
      psql(f"SELECT bill_requested_at IS NOT NULL FROM orders WHERE id = '{table_order.id}'") == "t")

row = order_row(mistake.id)
check("a comanda cancelada chegou cancelada e zerada", row[0] == "canceled" and row[3] == "0", str(row))
check("com os itens cancelados",
      psql(f"SELECT count(*) FROM order_items WHERE order_id = '{mistake.id}' "
           "AND canceled_at IS NULL") == "0")

check("a mesa nova chegou com o nome atualizado",
      psql(f"SELECT label FROM store_tables WHERE id = '{varanda.id}'") == "Deck A")

local_audit = database.query_one("SELECT COUNT(*) AS n FROM audit_ledger")["n"]
check("a cadeia de auditoria ancorou inteira",
      psql(f"SELECT last_seq FROM device_anchors WHERE device_id = '{device}'") == str(local_audit))
check("o terminal não gerou alerta de fraude",
      psql(f"SELECT count(*) FROM fraud_alerts WHERE device_id = '{device}'") == "0")

print("\n[4] outro turno: reenviar não duplica")
checkout.open_sale(operator)
checkout.register_unit_item(product=cafe, quantity=Decimal(1), operator_id=operator)
checkout.finalize_sale(
    payments=(Payment(PaymentMethod.DEBIT, checkout.current_sale.total_cents),),
    operator_id=operator, operator_name="Ana",
)
# O lote exato que vai subir, guardado para ser reenviado depois: é o caso da
# resposta perdida, em que o terminal não sabe que a nuvem já gravou.
pending = engine._reader.claim_batch(500)  # noqa: SLF001
first = engine.drain()
replay = HttpTransport(BASE, token).push(
    PushBatch(device_id=device, tenant_id=tenant, store_id=store, items=tuple(pending))
)
check("o segundo turno entrou", first.rejected == 0 and engine.pending_count() == 0, str(first))
# Mesma `Idempotency-Key`: a rota devolve a resposta guardada do primeiro envio
# (os mesmos "applied"), sem reprocessar. O que importa é o terminal a aceitar
# como quitação e nada ser gravado de novo.
check("o reenvio é reconhecido e quitado",
      len(replay.acks) == len(pending) and all(a.status.is_settled for a in replay.acks),
      str(replay.acks)[:200])
check("o lote reenviado não criou pedido novo",
      psql(f"SELECT count(*) FROM orders WHERE tenant_id = '{tenant}'") == "4")

print()
if failures:
    sys.exit(f"{failures} verificação(ões) falharam")
print("terminal e nuvem falam a mesma língua")
