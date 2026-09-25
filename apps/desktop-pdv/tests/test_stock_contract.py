"""Contrato da baixa de estoque por ficha técnica — o que o C# tem de calcular igual.

O consumo de insumo sobe para a nuvem (`order_item_ingredients`,
`stock_movements`) e alimenta o CMV e a previsão de demanda. Um miligrama de
diferença por venda entre os dois PDVs é um estoque que diverge da nuvem em
semanas, sem erro nenhum em lugar nenhum.

Os pontos que divergem em silêncio entre linguagens, e por isso estão aqui:

* o arredondamento do consumo é `ROUND_HALF_UP`, uma vez, no fim;
* o total do item unitário usa o `quantize` padrão do `Decimal`, que é
  `ROUND_HALF_EVEN` — 1,5 × R$ 3,33 = 499,5 centavos vira 500, e 1,5 × R$ 3,35
  = 502,5 vira 502;
* o rendimento divide (o produto encolhe no forno), não multiplica.

Para regerar: ``PDV_UPDATE_CONTRACT=1 python -m pytest tests/test_stock_contract.py``.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

from pdv.domain.models import Cents, EntityId, Grams, Milligrams, Recipe, RecipeLine
from pdv.services.stock import explode_recipe

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "recipe-explosion.json"


def _line(item: str, name: str, mg: int, waste: str, cost: int) -> dict:
    return {"inventory_item_id": item, "inventory_item_name": name, "qty_per_base_mg": mg,
            "waste_percent": waste, "unit_cost_cents_per_kg": cost}


RECIPES = [
    {
        "name": "torta_847g",
        "base_qty_g": 1000, "yield_factor": "1",
        "lines": [_line("farinha", "Farinha", 250_000, "0", 520),
                  _line("acucar", "Açúcar", 180_000, "0", 430),
                  _line("chocolate", "Chocolate", 320_000, "2.5", 4890)],
        "sold": [847, 1, 1000, 3333],
    },
    {
        "name": "bolo_que_encolhe",
        "base_qty_g": 1200, "yield_factor": "0.85",
        "lines": [_line("ovos", "Ovos", 360_000, "3.5", 1150),
                  _line("manteiga", "Manteiga", 125_000, "1", 5290)],
        "sold": [100, 250, 999, 1200],
    },
    {
        "name": "meio_exato",
        # 1 mg por grama-base e 1 g vendido de 2 g-base: 0,5 mg -> ROUND_HALF_UP = 1
        "base_qty_g": 2, "yield_factor": "1",
        "lines": [_line("sal", "Sal", 1, "0", 100), _line("fermento", "Fermento", 3, "0", 999)],
        "sold": [1, 3],
    },
]

UNIT_TOTALS = [(333, "1.5"), (335, "1.5"), (1000, "3"), (799, "0.5"), (125, "2.25"), (1, "0.5"), (3, "0.5")]


def _recipe(spec: dict) -> Recipe:
    return Recipe(
        id=EntityId("r-" + spec["name"]),
        product_id=EntityId("p-" + spec["name"]),
        base_qty_g=Grams(spec["base_qty_g"]),
        yield_factor=Decimal(spec["yield_factor"]),
        lines=tuple(
            RecipeLine(
                inventory_item_id=EntityId(line["inventory_item_id"]),
                inventory_item_name=line["inventory_item_name"],
                qty_per_base_mg=Milligrams(line["qty_per_base_mg"]),
                waste_percent=Decimal(line["waste_percent"]),
                unit_cost_cents_per_kg=Cents(line["unit_cost_cents_per_kg"]),
            )
            for line in spec["lines"]
        ),
    )


def _build() -> dict:
    cases = []
    for spec in RECIPES:
        recipe = _recipe(spec)
        for sold in spec["sold"]:
            cases.append({
                "recipe": {k: v for k, v in spec.items() if k != "sold"},
                "sold_grams": sold,
                "consumptions": [
                    {"inventory_item_id": c.inventory_item_id, "consumed_mg": int(c.consumed_mg),
                     "unit_cost_cents": int(c.unit_cost_cents)}
                    for c in explode_recipe(recipe, Grams(sold))
                ],
            })
    unit_totals = [
        {"price_cents": price, "quantity": quantity,
         # a mesma expressão do CheckoutService.register_unit_item
         "total_cents": int((Decimal(price) * Decimal(quantity)).quantize(Decimal("1")))}
        for price, quantity in UNIT_TOTALS
    ]
    return {
        "comment": "Gerado por apps/desktop-pdv/tests/test_stock_contract.py. Não edite à mão.",
        "cases": cases,
        "unit_totals": unit_totals,
    }


def test_the_stock_contract_matches_the_implementation() -> None:
    built = _build()
    if os.getenv("PDV_UPDATE_CONTRACT") == "1":
        CONTRACT.write_text(json.dumps(built, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert CONTRACT.exists(), "rode com PDV_UPDATE_CONTRACT=1 para gerar o contrato"
    assert json.loads(CONTRACT.read_text(encoding="utf-8")) == built, (
        "a baixa de estoque mudou: regere o contrato e alinhe o C# (apps/pdv-net)"
    )


def test_the_contract_really_exercises_both_roundings() -> None:
    totals = {(t["price_cents"], t["quantity"]): t["total_cents"] for t in _build()["unit_totals"]}
    assert totals[(333, "1.5")] == 500 and totals[(335, "1.5")] == 502  # meio para o par
    half = next(c for c in _build()["cases"] if c["recipe"]["name"] == "meio_exato" and c["sold_grams"] == 1)
    assert half["consumptions"][0]["consumed_mg"] == 1  # 0,5 mg sobe
