"""Contrato do cupom ESC/POS — os bytes que o C# tem de mandar à impressora iguais.

O cupom é conferido byte a byte: um comando a mais e a guilhotina corta o
texto, uma code page errada e o "ç" sai como lixo, e a gaveta que abre em
venda no cartão é o convite ao furto. Nada disso aparece em teste que olha
só o texto.

O que diverge em silêncio entre linguagens, e por isso está aqui:

* **PC850 com `errors="replace"`.** Caractere fora da página vira `?`. O .NET,
  por padrão, faz "best fit" (`€` viraria `E`), e um código fora da BMP pode
  virar dois `?`.
* **Truncamento.** Quem perde espaço é o texto da esquerda, nunca o valor.
* **A quantidade.** É o `format(Decimal, "g")`, e o peso sai em `0,847 kg`.
* **A gaveta** só abre com dinheiro.

A hora vai no cupom no fuso da máquina (`astimezone()`), então o contrato leva
a hora local que o Python imprimiu, e o C# a recebe já convertida.

Para regerar: ``PDV_UPDATE_CONTRACT=1 python -m pytest tests/test_receipt_contract.py``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from pdv.config import PrinterConfig
from pdv.domain.models import (
    Cents, EntityId, Grams, Payment, PaymentMethod, PricingMode, Sale, SaleItem,
)
from pdv.hardware.printer.escpos import EscPosBuilder, format_cents, format_grams
from pdv.hardware.printer.layout import ReceiptContext, build_drawer_pulse, build_sale_receipt

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "receipts.json"
CREATED = datetime(2026, 9, 25, 15, 4, 5, tzinfo=timezone.utc)


def _item(name: str, total: int, price: int, *, quantity: str = "1", net: int = 0, tare: int = 0) -> dict:
    return {"product_name": name, "total_cents": total, "unit_price_cents": price,
            "quantity": quantity, "net_weight_grams": net, "tare_grams": tare}


SALES: list[dict] = [
    {
        "name": "pesado_com_tara_e_dinheiro",
        "local_number": 42,
        "discount_cents": 0,
        "items": [_item("Torta de chocolate", 4227, 4990, net=847, tare=35)],
        "payments": [{"method": "cash", "amount_cents": 5000, "change_cents": 773}],
        "context": {},
    },
    {
        "name": "unitarios_desconto_cartao_sem_gaveta",
        "local_number": 7,
        "discount_cents": 473,
        "items": [
            _item("Café expresso", 1500, 750, quantity="2"),
            _item("Pão de queijo — porção", 500, 333, quantity="1.5"),
            _item("Refrigerante lata 350ml sabor guaraná com nome enorme de verdade", 333, 333),
        ],
        "payments": [{"method": "debit", "amount_cents": 1860, "change_cents": 0}],
        "context": {},
    },
    {
        "name": "cliente_cashback_prepago_qrcode",
        "local_number": 123456,
        "discount_cents": 0,
        "items": [_item("Fatia", 1450, 1450), _item("Bolo €uro 🎂", 2500, 10000, quantity="0.25")],
        "payments": [
            {"method": "prepaid", "amount_cents": 2000, "change_cents": 0},
            {"method": "cash", "amount_cents": 2000, "change_cents": 50},
        ],
        "context": {
            "customer_name": "Lia Cliente de Nome Muito Comprido Mesmo",
            "cashback_earned_cents": 195,
            "credit_balance_cents": 550,
            "qrcode_data": "https://www.fazenda.rj.gov.br/nfce/consulta?p=3326|3|1",
        },
    },
    {
        "name": "fiado_e_pix_sem_cliente",
        "local_number": 1,
        "discount_cents": 0,
        "items": [_item("Açaí 500g", 123456, 123456)],
        "payments": [
            {"method": "credit_account", "amount_cents": 100000, "change_cents": 0},
            {"method": "pix", "amount_cents": 23456, "change_cents": 0},
        ],
        "context": {"cashback_earned_cents": 0},
    },
]

CONTEXT = {
    "store_name": "Confeitaria Dolce Affetto & Cia",
    "store_document": "12.345.678/0001-90",
    "store_address": "Rua das Laranjeiras, 1234 - Laranjeiras - Rio de Janeiro/RJ - CEP 22240-003",
    "operator_name": "Ana Caixa",
    "terminal_label": "PDV 33333333",
}


def _sale(spec: dict) -> Sale:
    items = tuple(
        SaleItem(
            id=EntityId(f"i{n}"), client_uuid=EntityId(f"u{n}"), product_id=EntityId(f"p{n}"),
            product_name=item["product_name"],
            pricing_mode=PricingMode.WEIGHT if item["net_weight_grams"] else PricingMode.UNIT,
            unit_price_cents=Cents(item["unit_price_cents"]), total_cents=Cents(item["total_cents"]),
            quantity=Decimal(item["quantity"]), net_weight_grams=Grams(item["net_weight_grams"]),
            tare_grams=Grams(item["tare_grams"]), created_at=CREATED,
        )
        for n, item in enumerate(spec["items"])
    )
    return Sale(
        id=EntityId("sale"), client_uuid=EntityId("0199-doc-uuid"), tenant_id=EntityId("t"),
        store_id=EntityId("s"), device_id=EntityId("d"), operator_id=EntityId("o"),
        local_number=spec["local_number"], items=items,
        discount_cents=Cents(spec["discount_cents"]), created_at=CREATED,
    )


def _builder_cases() -> list[dict]:
    cases = []

    def case(name: str, builder: EscPosBuilder) -> None:
        cases.append({"name": name, "hex": builder.build().hex()})

    case("initialize", EscPosBuilder().initialize())
    case("styles", EscPosBuilder().align(1).bold().underline().size(2, 3).size(0, 9).reset_style())
    case("text_pc850", EscPosBuilder().line("Ação ç ã é ü € — ☕ 🎂 fim"))
    case("columns_2_fit", EscPosBuilder(columns=20).columns_2("Subtotal", "12,50"))
    case("columns_2_truncates_left", EscPosBuilder(columns=20).columns_2("Um texto que não cabe", "1.234,56"))
    case("columns_2_value_bigger_than_line", EscPosBuilder(columns=5).columns_2("x", "123.456,78"))
    case("columns_3", EscPosBuilder(columns=24).columns_3("Descrição longa demais", "QTD", "TOTAL"))
    case("columns_3_falls_back", EscPosBuilder(columns=8).columns_3("abc", "QTD", "TOTAL"))
    case("feed_cut_drawer", EscPosBuilder().feed(3).feed(999).cut(4).cut(0, partial=False).open_drawer().open_drawer(pin=5, on_ms=1, off_ms=9999))
    case("qrcode", EscPosBuilder().qrcode("chave|3|1", module_size=4))
    case("separator_centered", EscPosBuilder(columns=10).separator("=").centered("meio"))
    return cases


def _receipts() -> list[dict]:
    config = PrinterConfig()
    receipts = []
    for spec in SALES:
        payments = tuple(
            Payment(PaymentMethod(p["method"]), Cents(p["amount_cents"]), Cents(p["change_cents"]))
            for p in spec["payments"]
        )
        context = ReceiptContext(**CONTEXT, **spec["context"])
        receipts.append({
            **spec,
            "context": {**CONTEXT, **spec["context"]},
            "printed_local": CREATED.astimezone().strftime("%Y-%m-%dT%H:%M:%S"),
            "hex": build_sale_receipt(_sale(spec), payments, context, config).hex(),
        })
    return receipts


def _build() -> dict:
    return {
        "comment": "Gerado por apps/desktop-pdv/tests/test_receipt_contract.py. Não edite à mão.",
        "builder": _builder_cases(),
        "receipts": _receipts(),
        "drawer_pulse_hex": build_drawer_pulse(PrinterConfig()).hex(),
        "format_cents": {str(v): format_cents(v) for v in (0, 5, 99, 100, 123456, 100000000, -150)},
        "format_grams": {str(v): format_grams(v) for v in (0, 7, 847, 1250, 30000, -35)},
    }


def _normalized(built: dict) -> dict:
    # A hora local depende do fuso de quem gera: o contrato guarda a do gerador,
    # e a comparação usa a mesma para que o teste passe em qualquer fuso.
    return json.loads(json.dumps(built, ensure_ascii=False))


def test_the_receipt_contract_matches_the_implementation() -> None:
    built = _build()
    if os.getenv("PDV_UPDATE_CONTRACT") == "1":
        CONTRACT.write_text(json.dumps(built, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert CONTRACT.exists(), "rode com PDV_UPDATE_CONTRACT=1 para gerar o contrato"
    stored = json.loads(CONTRACT.read_text(encoding="utf-8"))
    mine = _normalized(built)
    # O cupom carrega a hora local; fora do fuso de quem gerou, compara o resto.
    if stored["receipts"][0]["printed_local"] != mine["receipts"][0]["printed_local"]:
        for receipt in stored["receipts"] + mine["receipts"]:
            receipt.pop("hex"), receipt.pop("printed_local")
    assert stored == mine, "o cupom mudou: regere o contrato e alinhe o C# (apps/pdv-net)"


def test_the_contract_really_exercises_the_traps() -> None:
    built = _build()
    text = bytes.fromhex(next(c for c in built["builder"] if c["name"] == "text_pc850")["hex"])
    assert b"\x87" in text and b"?" in text  # ç em PC850; € e emoji viram ?
    card = next(r for r in built["receipts"] if r["name"] == "unitarios_desconto_cartao_sem_gaveta")
    cash = next(r for r in built["receipts"] if r["name"] == "pesado_com_tara_e_dinheiro")
    assert "1b7000" not in card["hex"] and "1b7000" in cash["hex"]  # gaveta só com dinheiro
