"""O roteiro do salão que o PDV em Python e o em C# interpretam igual.

O porte do servidor do salão (C6e) troca ~4.000 linhas de uma vez, e o app
do garçom é reaproveitado sem mudar uma linha: a resposta de cada operação
precisa ser a MESMA. Em vez de um contrato por função, um roteiro: passos em
JSON (criar mesa, abrir comanda, lançar item, transferir, receber...), cada um
com o resultado que o Python obteve. O C# roda os mesmos passos e exige os
mesmos resultados (`SalonContractTests`).

Ids e horários mudam a cada execução, então saem normalizados: o primeiro UUID
visto vira `<id1>`, o segundo `<id2>`, e assim por diante, na ordem em que
aparecem; todo horário ISO vira `<ts>`. Os dois lados normalizam do mesmo jeito.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from decimal import Decimal

from pdv.config import AppConfig, PrinterConfig, ScaleConfig
from pdv.data.database import Database
from pdv.domain.errors import PdvError
from pdv.domain.models import Cents, EntityId, Payment, PaymentMethod
from pdv.edge.auth import EdgeAuth
from pdv.edge.hub import EventHub
from pdv.edge.kds import KdsService
from pdv.edge.manager import ManagerSessions
from pdv.edge.orders import SettledOrder, TableOrderService
from pdv.edge.staff import StaffSessions
from pdv.edge.tables import TableService
from pdv.services.authorization import AuthorizationService
from pdv.services.staff_report import StaffReport

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
CONTRACT = CONTRACTS / "salon.json"

TENANT = "5a1a0000-0000-4000-8000-000000000001"
STORE = "5a1a0000-0000-4000-8000-000000000002"
DEVICE = "5a1a0000-0000-4000-8000-000000000003"
#: A chave do ledger. Fixa só para o roteiro: o hash sai normalizado de
#: qualquer jeito, porque cobre ids e horários que mudam a cada execução.
DEVICE_SECRET = "chave-do-roteiro-do-salao"
WAITER = "5a1a0000-0000-4000-8000-0000000000a1"
MANAGER = "5a1a0000-0000-4000-8000-0000000000a2"
CAFE = "5a1a0000-0000-4000-8000-0000000000b1"
TORTA = "5a1a0000-0000-4000-8000-0000000000b2"
FATIA = "5a1a0000-0000-4000-8000-0000000000b3"
MISSING = "5a1a0000-0000-4000-8000-00000000dead"


def _uuid(n: int) -> str:
    """O `client_uuid` que o celular mandaria: fixo, para o reenvio ser o mesmo."""
    return f"5a1a0000-0000-4000-8000-{n:012x}"

#: O que o salão precisa existir antes do primeiro passo. Vai no contrato: o
#: C# insere estas mesmas linhas, sem depender do `seed.py` (que é C7a).
SEED: dict[str, list[dict[str, Any]]] = {
    "users": [
        {"id": "5a1a0000-0000-4000-8000-0000000000a1", "tenant_id": TENANT, "name": "João Garçom",
         "login": "joao", "role": "waiter", "updated_at": "2026-09-26T00:00:00.000+00:00",
         # PIN 4826. Hash fixo: o C# confere o mesmo Argon2id que o Python gerou.
         "pin_hash": "$argon2id$v=19$m=65536,t=3,p=4$L/HZrr606fohJr3zkx0hxg$4468AcXI/qjZWrcpbM9bc51BwKWjVzJBm7UrMATF2kY"},
        {"id": "5a1a0000-0000-4000-8000-0000000000a2", "tenant_id": TENANT, "name": "Bruno Gerente",
         "login": "bruno", "role": "manager", "can_authorize": 1, "max_discount_percent": "30",
         "updated_at": "2026-09-26T00:00:00.000+00:00",
         # PIN 7391.
         "pin_hash": "$argon2id$v=19$m=65536,t=3,p=4$So7KHgj4E7qSNqB0dLi9Cg$gaNMRGt69ipftremGgDs+QrziObdh6tsqghlwY7zBh0"},
        {"id": "5a1a0000-0000-4000-8000-0000000000a3", "tenant_id": TENANT, "name": "Olga Dona",
         "login": "olga", "role": "owner", "can_authorize": 1, "max_discount_percent": "100",
         "updated_at": "2026-09-26T00:00:00.000+00:00",
         # PIN 5160.
         "pin_hash": "$argon2id$v=19$m=65536,t=3,p=4$7CPgBUYfN32CU12ofQnkYA$e76mY7TbKQzMYGGVIVPrfvwwiEn9YKIypLB4ogVSHHg"},
    ],
    "inventory_items": [
        {"id": "5a1a0000-0000-4000-8000-0000000000e1", "tenant_id": TENANT, "store_id": STORE,
         "name": "Farinha", "unit": "mg", "balance_mg": 10_000_000, "min_stock_mg": 0,
         "avg_cost_cents_per_kg": 650, "updated_at": "2026-09-26T00:00:00.000+00:00"},
        {"id": "5a1a0000-0000-4000-8000-0000000000e2", "tenant_id": TENANT, "store_id": STORE,
         "name": "Chocolate", "unit": "mg", "balance_mg": 5_000_000, "min_stock_mg": 0,
         "avg_cost_cents_per_kg": 4_200, "updated_at": "2026-09-26T00:00:00.000+00:00"},
    ],
    "products": [
        {"id": "5a1a0000-0000-4000-8000-0000000000b1", "tenant_id": TENANT, "store_id": STORE,
         "sku": "CAFE", "name": "Café coado", "pricing_mode": "unit", "price_cents": 700,
         "updated_at": "2026-09-26T00:00:00.000+00:00"},
        {"id": "5a1a0000-0000-4000-8000-0000000000b2", "tenant_id": TENANT, "store_id": STORE,
         "sku": "TORTA", "name": "Torta de limão", "pricing_mode": "weight", "price_cents": 5900,
         "updated_at": "2026-09-26T00:00:00.000+00:00"},
        # Unitário COM ficha: é o que prova a baixa de insumo pelo salão.
        {"id": "5a1a0000-0000-4000-8000-0000000000b3", "tenant_id": TENANT, "store_id": STORE,
         "sku": "FATIA", "name": "Fatia de torta", "pricing_mode": "unit", "price_cents": 1450,
         "recipe_id": "5a1a0000-0000-4000-8000-0000000000f1",
         "updated_at": "2026-09-26T00:00:00.000+00:00"},
    ],
    "recipes": [
        {"id": "5a1a0000-0000-4000-8000-0000000000f1", "tenant_id": TENANT,
         "product_id": "5a1a0000-0000-4000-8000-0000000000b3", "base_qty_g": 150, "yield_factor": "0.92",
         "updated_at": "2026-09-26T00:00:00.000+00:00"},
    ],
    "recipe_lines": [
        {"id": "5a1a0000-0000-4000-8000-0000000000f2", "recipe_id": "5a1a0000-0000-4000-8000-0000000000f1",
         "inventory_item_id": "5a1a0000-0000-4000-8000-0000000000e1", "qty_per_base_mg": 37_500,
         "waste_percent": "2", "updated_at": "2026-09-26T00:00:00.000+00:00"},
        {"id": "5a1a0000-0000-4000-8000-0000000000f3", "recipe_id": "5a1a0000-0000-4000-8000-0000000000f1",
         "inventory_item_id": "5a1a0000-0000-4000-8000-0000000000e2", "qty_per_base_mg": 48_000,
         "waste_percent": "3", "updated_at": "2026-09-26T00:00:00.000+00:00"},
    ],
}

#: Os passos. `save` guarda ids do resultado com um nome (`{"o1": "order_id"}`,
#: `{"i1": "0.id"}` para o primeiro da lista); `$nome` num argumento, inclusive
#: dentro de lista, é trocado pelo id guardado.
SCRIPT: list[dict[str, Any]] = [
    {"op": "tables.list"},
    {"op": "tables.seed", "args": {"count": 3}},
    {"op": "tables.seed", "args": {"count": 3}},
    {"op": "tables.create", "args": {"label": "  Varanda   1 ", "area": "Varanda", "seats": 2},
     "save": {"varanda": "id"}},
    {"op": "tables.create", "args": {"label": "mesa 1"}},
    {"op": "tables.create", "args": {"label": "   "}},
    {"op": "tables.create", "args": {"label": "Balcão", "area": "", "seats": 500}},
    {"op": "tables.create", "args": {"label": "Balcão", "area": "Bar", "seats": 500}},
    {"op": "tables.create", "args": {"label": "Mesa com um nome comprido demais para caber", "seats": 0}},
    {"op": "tables.update", "args": {"table_id": "$varanda", "label": "Varanda 2", "seats": 6}},
    {"op": "tables.update", "args": {"table_id": "$varanda"}},
    {"op": "tables.update", "args": {"table_id": "$varanda", "label": "Mesa 2"}},
    {"op": "tables.update", "args": {"table_id": "5a1a0000-0000-4000-8000-00000000dead", "label": "X"}},
    {"op": "tables.set_active", "args": {"table_id": "$varanda", "active": False}},
    {"op": "tables.set_active", "args": {"table_id": "$varanda", "active": False}},
    {"op": "tables.update", "args": {"table_id": "$varanda", "label": "Varanda 3"}},
    {"op": "tables.create", "args": {"label": "Varanda 2"}},
    {"op": "tables.set_active", "args": {"table_id": "$varanda", "active": True}},
    {"op": "tables.list", "args": {"include_inactive": True}},
    {"op": "tables.find", "args": {"label": "MESA 3"}},
    {"op": "tables.find", "args": {"label": "Mesa 99"}},
    # -- comandas ----------------------------------------------------------- #
    {"op": "tables.find", "args": {"label": "Mesa 1"}, "save": {"m1": "id"}},
    {"op": "tables.find", "args": {"label": "Mesa 2"}, "save": {"m2": "id"}},
    {"op": "tables.find", "args": {"label": "Mesa 3"}, "save": {"m3": "id"}},
    {"op": "orders.open", "args": {"client_uuid": _uuid(0xc001), "operator_id": WAITER, "table_id": "$m1"},
     "save": {"o1": "order_id"}},
    {"op": "orders.open", "args": {"client_uuid": _uuid(0xc001), "operator_id": WAITER, "table_id": "$m1"}},
    {"op": "orders.open", "args": {"client_uuid": _uuid(0xc002), "operator_id": WAITER, "table_id": "$m1"}},
    {"op": "orders.open", "args": {"client_uuid": _uuid(0xc003), "operator_id": WAITER, "table_label": " mesa 2 "},
     "save": {"o2": "order_id"}},
    {"op": "orders.open", "args": {"client_uuid": _uuid(0xc004), "operator_id": WAITER, "table_label": "Mesa 99"}},
    {"op": "orders.open", "args": {"client_uuid": _uuid(0xc005), "operator_id": WAITER, "table_label": "  "}},
    {"op": "orders.open", "args": {"client_uuid": _uuid(0xc006), "operator_id": WAITER, "table_id": "$varanda"}},
    {"op": "orders.open", "args": {"client_uuid": _uuid(0xc006), "operator_id": WAITER, "table_id": MISSING}},
    # -- itens e cozinha ------------------------------------------------------ #
    {"op": "orders.add_item", "args": {"order_id": "$o1", "client_uuid": _uuid(0xd001), "product_id": CAFE,
                                       "quantity": "2", "notes": "  sem açúcar ", "created_by_user_id": WAITER}},
    {"op": "orders.add_item", "args": {"order_id": "$o1", "client_uuid": _uuid(0xd001), "product_id": CAFE,
                                       "quantity": "2"}},
    {"op": "orders.add_item", "args": {"order_id": "$o1", "client_uuid": _uuid(0xd002), "product_id": TORTA,
                                       "quantity": "1"}},
    {"op": "orders.add_item", "args": {"order_id": "$o1", "client_uuid": _uuid(0xd003), "product_id": MISSING,
                                       "quantity": "1"}},
    {"op": "orders.add_item", "args": {"order_id": "$o1", "client_uuid": _uuid(0xd004), "product_id": CAFE,
                                       "quantity": "0"}},
    {"op": "orders.add_item", "args": {"order_id": "$o1", "client_uuid": _uuid(0xd005), "product_id": CAFE,
                                       "quantity": "1.50"}},
    {"op": "orders.add_item", "args": {"order_id": "$o1", "client_uuid": _uuid(0xd006), "product_id": CAFE,
                                       "quantity": "0.25", "station": "bar"}},
    # 700 × 0,015 = 10,5: o Python arredonda meio-para-par (10), não para cima (11).
    {"op": "orders.add_item", "args": {"order_id": "$o1", "client_uuid": _uuid(0xd007), "product_id": CAFE,
                                       "quantity": "0.015", "created_by_user_id": MANAGER}},
    {"op": "orders.add_item", "args": {"order_id": MISSING, "client_uuid": _uuid(0xd008), "product_id": CAFE,
                                       "quantity": "1"}},
    {"op": "orders.items", "args": {"order_id": "$o1"},
     "save": {"i1": "0.id", "i2": "1.id", "i3": "2.id", "i4": "3.id"}},
    {"op": "kds.list"},
    {"op": "kds.list", "args": {"station": "bar"}, "save": {"t_bar": "0.ticket_id"}},
    {"op": "kds.list", "args": {"station": "cozinha"}, "save": {"t1": "0.ticket_id", "t2": "1.ticket_id"}},
    {"op": "kds.bump", "args": {"ticket_id": "$t1"}},
    {"op": "kds.bump", "args": {"ticket_id": "$t1"}},
    {"op": "kds.recall", "args": {"ticket_id": "$t1"}},
    {"op": "kds.advance", "args": {"ticket_id": "$t1", "to_status": "delivered"}},
    {"op": "kds.bump", "args": {"ticket_id": "$t1"}},
    {"op": "kds.bump", "args": {"ticket_id": "$t1"}},
    {"op": "kds.bump", "args": {"ticket_id": "$t1"}},
    {"op": "kds.recall", "args": {"ticket_id": "$t2"}},
    {"op": "kds.advance", "args": {"ticket_id": "$t_bar", "to_status": "canceled"}},
    {"op": "kds.recall", "args": {"ticket_id": "$t_bar"}},
    {"op": "kds.get", "args": {"ticket_id": "$t1"}},
    {"op": "kds.get", "args": {"ticket_id": MISSING}},
    # -- conta ------------------------------------------------------------------ #
    {"op": "orders.request_bill", "args": {"order_id": "$o1"}},
    {"op": "orders.request_bill", "args": {"order_id": "$o1"}},
    {"op": "tables.find", "args": {"label": "Mesa 1"}},
    {"op": "orders.clear_bill", "args": {"order_id": "$o1"}},
    {"op": "orders.clear_bill", "args": {"order_id": "$o1"}},
    # -- dividir, juntar e transferir -------------------------------------------- #
    {"op": "orders.add_item", "args": {"order_id": "$o2", "client_uuid": _uuid(0xd010), "product_id": CAFE,
                                       "quantity": "3"}},
    {"op": "orders.move_items", "args": {"source_order_id": "$o1", "target_order_id": "$o2",
                                         "item_ids": ["$i2", "$i2"]}},
    {"op": "orders.move_items", "args": {"source_order_id": "$o1", "target_order_id": "$o2", "item_ids": ["$i2"]}},
    {"op": "orders.move_items", "args": {"source_order_id": "$o1", "target_order_id": "$o1", "item_ids": ["$i1"]}},
    {"op": "orders.move_items", "args": {"source_order_id": "$o1", "target_order_id": "$o2", "item_ids": []}},
    {"op": "orders.transfer", "args": {"order_id": "$o2", "table_id": "$m3"}},
    {"op": "orders.transfer", "args": {"order_id": "$o2", "table_id": "$m1"}},
    {"op": "orders.transfer", "args": {"order_id": "$o2", "table_id": "$m3"}},
    {"op": "orders.transfer", "args": {"order_id": "$o2", "table_id": "$varanda"}},
    {"op": "orders.settle_items", "args": {"order_id": "$o1", "item_ids": ["$i1"], "tip_cents": 100,
                                           "payments": [{"method": "cash", "amount_cents": 2000}]}},
    {"op": "orders.settle_items", "args": {"order_id": "$o1", "item_ids": ["$i1"],
                                           "payments": [{"method": "cash", "amount_cents": 2000}]}},
    {"op": "orders.list_open"},
    # Gorjeta negativa vira zero; o troco (15) sai no PRIMEIRO dinheiro.
    {"op": "orders.settle", "args": {"order_id": "$o1", "tip_cents": -50,
                                     "payments": [{"method": "pix", "amount_cents": 100},
                                                  {"method": "cash", "amount_cents": 50},
                                                  {"method": "cash", "amount_cents": 50}]}},
    {"op": "orders.settle", "args": {"order_id": "$o1",
                                     "payments": [{"method": "cash", "amount_cents": 5000}]}},
    {"op": "orders.add_item", "args": {"order_id": "$o1", "client_uuid": _uuid(0xd011), "product_id": CAFE,
                                       "quantity": "1"}},
    {"op": "orders.open", "args": {"client_uuid": _uuid(0xc007), "operator_id": MANAGER, "table_id": "$m2"},
     "save": {"o3": "order_id"}},
    {"op": "orders.settle", "args": {"order_id": "$o3", "payments": [{"method": "cash", "amount_cents": 100}]}},
    {"op": "orders.cancel", "args": {"order_id": "$o3", "reason": "   "}},
    {"op": "orders.cancel", "args": {"order_id": "$o3", "reason": "  aberta   por\tengano "}},
    {"op": "orders.open", "args": {"client_uuid": _uuid(0xc008), "operator_id": WAITER, "table_id": "$m1"},
     "save": {"o4": "order_id"}},
    {"op": "orders.add_item", "args": {"order_id": "$o4", "client_uuid": _uuid(0xd012), "product_id": CAFE,
                                       "quantity": "1", "notes": "com leite"}},
    # A fatia baixa insumo no lançamento; porção de zero grama é recusada.
    {"op": "stock.balances"},
    {"op": "orders.add_item", "args": {"order_id": "$o4", "client_uuid": _uuid(0xd014), "product_id": FATIA,
                                       "quantity": "2"}},
    {"op": "orders.add_item", "args": {"order_id": "$o4", "client_uuid": _uuid(0xd014), "product_id": FATIA,
                                       "quantity": "2"}},
    {"op": "orders.add_item", "args": {"order_id": "$o4", "client_uuid": _uuid(0xd015), "product_id": FATIA,
                                       "quantity": "0.001"}},
    {"op": "stock.balances"},
    {"op": "orders.merge", "args": {"source_order_id": "$o4", "target_order_id": "$o2"}},
    {"op": "orders.merge", "args": {"source_order_id": "$o4", "target_order_id": "$o2"}},
    {"op": "stock.balances"},
    {"op": "orders.list_open"},
    {"op": "orders.get", "args": {"order_id": MISSING}},
    {"op": "orders.cancel", "args": {"order_id": "$o2", "reason": "cliente foi embora"}},
    # Cancelar a comanda que recebeu a fatia devolve o insumo.
    {"op": "stock.balances"},
    {"op": "orders.items", "args": {"order_id": "$o2"}},
    # Escolher TODOS os itens é o recebimento inteiro, na própria comanda.
    {"op": "orders.open", "args": {"client_uuid": _uuid(0xc009), "operator_id": WAITER, "table_id": "$m2"},
     "save": {"o5": "order_id"}},
    {"op": "orders.add_item", "args": {"order_id": "$o5", "client_uuid": _uuid(0xd013), "product_id": CAFE,
                                       "quantity": "1"}},
    {"op": "orders.items", "args": {"order_id": "$o5"}, "save": {"i5": "0.id"}},
    {"op": "orders.settle_items", "args": {"order_id": "$o5", "item_ids": ["$i5"],
                                           "payments": [{"method": "debit", "amount_cents": 700}]}},
    {"op": "kds.list"},
    {"op": "tables.list"},
    # -- pareamento ------------------------------------------------------------ #
    {"op": "auth.active_code"},
    {"op": "auth.pair", "args": {"code": "12345678", "device_name": "Celular"}},
    {"op": "auth.create_code", "save": {"code1": "code"}},
    {"op": "auth.active_code"},
    {"op": "auth.pair", "args": {"code": "$code1", "device_name": "Celular", "kind": "tablet"}},
    # O código aceita espaço em volta, e o nome sai aparado.
    {"op": "auth.pair", "args": {"code": " $code1 ", "device_name": "  Celular do João  "},
     "save": {"tok1": "token", "dev1": "device.id"}},
    {"op": "auth.pair", "args": {"code": "$code1", "device_name": "Outro"}},
    {"op": "auth.create_code", "save": {"code2": "code"}},
    {"op": "auth.pair", "args": {"code": "$code2", "device_name": "   ", "kind": "kds"},
     "save": {"tok2": "token", "dev2": "device.id"}},
    {"op": "auth.authenticate", "args": {"token": "$tok1"}},
    {"op": "auth.authenticate", "args": {"token": ""}},
    {"op": "auth.authenticate", "args": {"token": "nao-e-um-token"}},
    {"op": "auth.create_code", "save": {"code3": "code"}},
    {"op": "auth.create_code", "save": {"code4": "code"}},
    {"op": "auth.pair", "args": {"code": "$code3", "device_name": "Velho"}},
    {"op": "auth.revoke_codes"},
    {"op": "auth.revoke_codes"},
    {"op": "auth.active_code"},
    {"op": "auth.pair", "args": {"code": "$code4", "device_name": "Revogado"}},
    {"op": "auth.lock_seconds"},
    # -- sessão do garçom ------------------------------------------------------ #
    {"op": "staff.login", "args": {"login": "joao", "pin": "0000", "device_id": "$dev1"}},
    {"op": "staff.login", "args": {"login": "  JOAO ", "pin": "4826", "device_id": "$dev1"}, "save": {"st1": "token"}},
    {"op": "staff.require", "args": {"token": "$st1", "device_id": "$dev1"}},
    {"op": "staff.require", "args": {"token": "$st1", "device_id": "$dev2"}},
    {"op": "staff.require", "args": {"token": "", "device_id": "$dev1"}},
    {"op": "staff.require", "args": {"token": "outro", "device_id": "$dev1"}},
    {"op": "staff.login", "args": {"login": "joao", "pin": "4826", "device_id": "$dev1"}, "save": {"st2": "token"}},
    {"op": "staff.require", "args": {"token": "$st1", "device_id": "$dev1"}},
    {"op": "staff.login", "args": {"login": "bruno", "pin": "7391", "device_id": "$dev2"}, "save": {"st3": "token"}},
    {"op": "staff.active"},
    {"op": "staff.revoke_user", "args": {"user_id": WAITER}},
    {"op": "staff.require", "args": {"token": "$st2", "device_id": "$dev1"}},
    {"op": "staff.logout", "args": {"token": "$st3"}},
    {"op": "staff.logout", "args": {"token": "$st3"}},
    {"op": "staff.logout", "args": {"token": ""}},
    {"op": "staff.active"},
    # -- gerente --------------------------------------------------------------- #
    {"op": "manager.authorize", "args": {"login": "joao", "pin": "4826", "device_id": "$dev1"}},
    {"op": "manager.authorize", "args": {"login": "olga", "pin": "5160", "device_id": "$dev1"}},
    {"op": "manager.authorize", "args": {"login": "bruno", "pin": "1111", "device_id": "$dev1"}},
    {"op": "manager.authorize", "args": {"login": "bruno", "pin": "7391", "device_id": "$dev1"}, "save": {"mg1": "token"}},
    {"op": "manager.require", "args": {"token": "$mg1", "device_id": "$dev1"}},
    {"op": "manager.require", "args": {"token": "$mg1", "device_id": "$dev2"}},
    {"op": "manager.require", "args": {"token": "", "device_id": "$dev1"}},
    {"op": "manager.require", "args": {"token": "inventado", "device_id": "$dev1"}},
    {"op": "manager.active"},
    {"op": "manager.revoke", "args": {"token": "$mg1"}},
    {"op": "manager.revoke", "args": {"token": "$mg1"}},
    {"op": "manager.require", "args": {"token": "$mg1", "device_id": "$dev1"}},
    {"op": "manager.active"},
    # -- resultado do turno ---------------------------------------------------- #
    {"op": "report.by_waiter"},
    {"op": "report.for_user", "args": {"user_id": WAITER}},
    {"op": "report.for_user", "args": {"user_id": "5a1a0000-0000-4000-8000-0000000000a3"}},
    {"op": "report.for_user", "args": {"user_id": MISSING}},
    {"op": "report.totals"},
    # -- aparelho revogado e o freio do pareamento ------------------------------ #
    {"op": "auth.revoke", "args": {"device_id": "$dev2"}},
    {"op": "auth.revoke", "args": {"device_id": "$dev2"}},
    {"op": "auth.authenticate", "args": {"token": "$tok2"}},
    {"op": "auth.devices"},
    *[{"op": "auth.pair", "args": {"code": f"0000000{n}", "device_name": "Ataque"}} for n in range(9)],
    {"op": "auth.lock_seconds"},
    {"op": "auth.create_code", "save": {"code5": "code"}},
    {"op": "auth.pair", "args": {"code": "$code5", "device_name": "Legítimo"}},
]

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z|[+-]\d{2}:\d{2})?")


#: Chaves cujo valor depende do relógio ou da chave do ledger, e não da regra:
#: o hash cobre ids e horários, e a espera do ticket, os segundos que o teste
#: levou. Saem trocadas por um marcador, dos dois lados.
_VOLATILE = {
    "hash": "<hash>", "prev_hash": "<hash>", "waiting_seconds": "<s>",
    # Credenciais: aleatórias por construção. O que se confere é que vieram.
    "token": "<token>", "code": "<code>",
    "remaining_seconds": "<s>", "expires_in_seconds": "<s>",
}


class Normalizer:
    """UUID → `<idN>` na ordem de aparição; horário ISO → `<ts>`."""

    def __init__(self) -> None:
        self._ids: dict[str, str] = {}

    def text(self, value: str) -> str:
        value = _TS.sub("<ts>", value)
        return _UUID.sub(lambda m: self._ids.setdefault(m.group(0), f"<id{len(self._ids) + 1}>"), value)

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(v) for v in value]
        if isinstance(value, dict):
            return {
                k: _VOLATILE[k] if k in _VOLATILE and v is not None else self.value(v)
                for k, v in value.items()
            }
        return value


def open_database(tmp_path: Path) -> tuple[Database, AppConfig]:
    config = AppConfig(
        tenant_id=TENANT, store_id=STORE, device_id=DEVICE,
        database_path=tmp_path / "pdv.db",
        scale=ScaleConfig(protocol="simulated"),
        printer=PrinterConfig(backend="file", output_dir=tmp_path / "cupons"),
        device_secret=DEVICE_SECRET.encode(),
    )
    database = Database(config.database_path)
    database.migrate()
    for table, rows in SEED.items():
        for row in rows:
            columns = ", ".join(row)
            marks = ", ".join("?" for _ in row)
            database.connection.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(row.values())
            )
    database.connection.commit()
    return database, config


def _dig(value: Any, path: str) -> str:
    """`"0.id"` → `value[0]["id"]`: de onde, no resultado, sai o id a guardar."""
    for part in path.split("."):
        value = value[int(part)] if part.isdigit() else value[part]
    return str(value)


def _payments(raw: list[dict[str, Any]]) -> tuple[Payment, ...]:
    return tuple(Payment(PaymentMethod(p["method"]), Cents(int(p["amount_cents"]))) for p in raw)


def _settled(settled: SettledOrder) -> dict[str, Any]:
    """O recebimento como o servidor do salão o devolve à tela do caixa."""
    return {
        "order": settled.order.to_json(),
        "payments": [
            {"method": p.method.value, "amount_cents": int(p.amount_cents), "change_cents": int(p.change_cents)}
            for p in settled.payments
        ],
        "tip_cents": int(settled.tip_cents),
        "charged_cents": int(settled.charged_cents),
        "change_cents": int(settled.change_cents),
    }


def run(tmp_path: Path) -> dict[str, Any]:
    database, config = open_database(tmp_path)
    hub = EventHub()
    events = hub.subscribe()
    tables = TableService(database, config)
    orders = TableOrderService(database, config, hub)
    kds = KdsService(database, config, hub)
    auth = EdgeAuth(database, TENANT, STORE)
    staff = StaffSessions(database, TENANT)
    managers = ManagerSessions(AuthorizationService(database, TENANT))
    report = StaffReport(database, config)
    saved: dict[str, str] = {}

    def resolve(value: Any) -> Any:
        if isinstance(value, str) and value.strip().startswith("$"):
            return value.replace(value.strip(), saved[value.strip()[1:]])
        if isinstance(value, list):
            return [resolve(v) for v in value]
        if isinstance(value, dict):
            return {k: resolve(v) for k, v in value.items()}
        return value

    def open_order(client_uuid: str, operator_id: str, table_id: str | None = None, table_label: str = "") -> Any:
        return orders.open_order(
            client_uuid=EntityId(client_uuid), operator_id=EntityId(operator_id),
            origin_device_id=EntityId(DEVICE), table_id=table_id, table_label=table_label,
        ).to_json()

    def add_item(order_id: str, client_uuid: str, product_id: str, quantity: str, **extra: Any) -> Any:
        return orders.add_item(
            order_id=EntityId(order_id), client_uuid=EntityId(client_uuid),
            product_id=EntityId(product_id), quantity=Decimal(quantity), **extra,
        ).to_json()

    def move_items(source_order_id: str, target_order_id: str, item_ids: list[str]) -> Any:
        source, target = orders.move_items(
            source_order_id=EntityId(source_order_id), target_order_id=EntityId(target_order_id),
            item_ids=[EntityId(i) for i in item_ids], operator_id=EntityId(MANAGER), operator_name="Bruno Gerente",
        )
        return [source.to_json(), target.to_json()]

    def create_code() -> Any:
        code, pairing = auth.create_pairing_code()
        return {"code": code, "remaining_seconds": pairing.remaining_seconds}

    def device(token: str) -> Any:
        found = auth.authenticate(token)
        return {"id": found.id, "name": found.name, "kind": found.kind, "operator_id": found.operator_id}

    def pair(code: str, device_name: str, kind: str = "waiter") -> Any:
        token = auth.pair(code, device_name=device_name, kind=kind)
        return {"token": token, "device": device(token)}

    def manager_require(token: str, device_id: str) -> Any:
        who = managers.require(token, EntityId(device_id))
        return {"user_id": who.id, "name": who.name, "role": who.role}

    handlers: dict[str, Callable[..., Any]] = {
        "auth.create_code": create_code,
        "auth.active_code": lambda: (
            lambda p: {"remaining_seconds": p.remaining_seconds} if p else None
        )(auth.active_pairing_code()),
        "auth.revoke_codes": lambda: auth.revoke_pairing_codes(),
        "auth.pair": pair,
        "auth.authenticate": device,
        "auth.lock_seconds": lambda: auth.pairing_lock_seconds(),
        "auth.revoke": lambda device_id: auth.revoke(EntityId(device_id)),
        "auth.devices": lambda: auth.list_devices(),
        "staff.login": lambda login, pin, device_id: staff.login(
            login=login, pin=pin, device_id=EntityId(device_id)
        ).to_json(with_token=True),
        "staff.require": lambda token, device_id: staff.require(token, EntityId(device_id)).to_json(),
        "staff.logout": lambda token: staff.logout(token),
        "staff.revoke_user": lambda user_id: staff.revoke_user(EntityId(user_id)),
        "staff.active": lambda: staff.list_active(),
        "manager.authorize": lambda login, pin, device_id: managers.authorize(
            login=login, pin=pin, device_id=EntityId(device_id)
        ).to_json(),
        "manager.require": manager_require,
        "manager.revoke": lambda token: managers.revoke(token),
        "manager.active": lambda: managers.active_count(),
        "report.by_waiter": lambda: [r.to_json() for r in report.by_waiter()],
        "report.for_user": lambda user_id: report.for_user(EntityId(user_id)),
        "report.totals": lambda: report.totals(),
        "stock.balances": lambda: [
            {"name": str(r["name"]), "balance_mg": int(r["balance_mg"])}
            for r in database.query_all("SELECT name, balance_mg FROM inventory_items ORDER BY name")
        ],
        "tables.list": lambda include_inactive=False: [
            t.to_json() for t in tables.list_tables(include_inactive=include_inactive)
        ],
        "tables.seed": lambda count: tables.seed_default_tables(count),
        "tables.create": lambda **a: tables.create(**a).to_json(),
        "tables.update": lambda table_id, **a: tables.update(table_id, **a).to_json(),
        "tables.set_active": lambda table_id, active: tables.set_active(table_id, active).to_json(),
        "tables.find": lambda label: (lambda t: t.to_json() if t else None)(tables.find_by_label(label)),
        "orders.open": open_order,
        "orders.add_item": add_item,
        "orders.items": lambda order_id: orders.list_items(EntityId(order_id)),
        "orders.get": lambda order_id: orders.get_order(EntityId(order_id)).to_json(),
        "orders.list_open": lambda: [o.to_json() for o in orders.list_open_orders()],
        "orders.request_bill": lambda order_id: orders.request_bill(EntityId(order_id)).to_json(),
        "orders.clear_bill": lambda order_id: orders.clear_bill_request(EntityId(order_id)).to_json(),
        "orders.transfer": lambda order_id, table_id: orders.transfer(
            order_id=EntityId(order_id), table_id=EntityId(table_id),
            authorizer_id=EntityId(MANAGER), authorizer_name="Bruno Gerente",
        ).to_json(),
        "orders.move_items": move_items,
        "orders.merge": lambda source_order_id, target_order_id: orders.merge_orders(
            source_order_id=EntityId(source_order_id), target_order_id=EntityId(target_order_id),
            operator_id=EntityId(MANAGER), operator_name="Bruno Gerente",
        ).to_json(),
        "orders.settle": lambda order_id, payments, tip_cents=0: _settled(orders.settle(
            order_id=EntityId(order_id), payments=_payments(payments), operator_id=EntityId(MANAGER),
            operator_name="Bruno Gerente", tip_cents=Cents(tip_cents),
        )),
        "orders.settle_items": lambda order_id, item_ids, payments, tip_cents=0: _settled(orders.settle_items(
            order_id=EntityId(order_id), item_ids=[EntityId(i) for i in item_ids], payments=_payments(payments),
            operator_id=EntityId(MANAGER), operator_name="Bruno Gerente", tip_cents=Cents(tip_cents),
        )),
        "orders.cancel": lambda order_id, reason: orders.cancel_order(
            order_id=EntityId(order_id), authorizer_id=EntityId(MANAGER),
            authorizer_name="Bruno Gerente", reason=reason,
        ).to_json(),
        "kds.list": lambda station=None: [t.to_json() for t in kds.list_active(station)],
        "kds.get": lambda ticket_id: kds.get(EntityId(ticket_id)).to_json(),
        "kds.bump": lambda ticket_id: kds.bump(EntityId(ticket_id)).to_json(),
        "kds.recall": lambda ticket_id: kds.recall(EntityId(ticket_id)).to_json(),
        "kds.advance": lambda ticket_id, to_status: kds.advance(EntityId(ticket_id), to_status).to_json(),
    }

    normalizer = Normalizer()
    results = []
    for step in SCRIPT:
        try:
            value = handlers[step["op"]](**resolve(step.get("args", {})))
            save = step.get("save")
            for name, path in ({save: "id"} if isinstance(save, str) else save or {}).items():
                saved[name] = _dig(value, path)
            outcome: dict[str, Any] = {"result": value}
        except PdvError as exc:
            outcome = {"error": str(exc)}
        # O que o passo publicou no barramento, sem o `at`: é o que a tela do
        # KDS e o app do garçom recebem pelo WebSocket.
        published = []
        while (event := events.get(timeout=0)) is not None:
            published.append({"kind": event.kind, **event.payload})
        if published:
            outcome["events"] = published
        results.append(normalizer.value(outcome))

    outbox = [
        normalizer.value({
            "entity_table": row["entity_table"],
            "operation": row["operation"],
            "payload": json.loads(row["payload_json"]),
        })
        for row in database.query_all(
            "SELECT entity_table, operation, payload_json FROM sync_outbox ORDER BY seq"
        )
    ]
    database.close()
    return {"results": results, "outbox": outbox}


def document(run_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "descricao": (
            "Roteiro do salão interpretado pelo PDV em Python e pelo em C#. Gerado por "
            "apps/desktop-pdv/tests/salon_script.py; não edite à mão: rode "
            "PDV_UPDATE_CONTRACT=1 python -m pytest tests/test_salon_contract.py."
        ),
        "tenant_id": TENANT,
        "store_id": STORE,
        "device_id": DEVICE,
        "device_secret": DEVICE_SECRET,
        "seed": SEED,
        "script": SCRIPT,
        **run_result,
    }
