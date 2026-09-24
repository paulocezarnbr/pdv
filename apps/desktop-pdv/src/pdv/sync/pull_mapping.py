"""Como uma linha de cadastro da nuvem vira uma linha no banco do caixa.

Até aqui o pull fazia `INSERT` com **todas** as chaves que a nuvem mandasse,
com os nomes das colunas vindos da resposta interpolados no SQL. As duas pontas
nunca tiveram o mesmo schema, e o pull nunca aplicou uma linha sequer:

* `users` chega com `server_seq`, que o caixa não tem → "no column named";
* `products` chega sem `store_id`, obrigatório no caixa (o catálogo da nuvem é
  da rede inteira; o do caixa é da loja);
* receitas e insumos têm modelos diferentes (`yield_grams` × `base_qty_g` +
  `yield_factor`, `unit_cost_cents` × `avg_cost_cents_per_kg`).

Aqui cada tabela diz o que aceita. Coluna nova na nuvem é ignorada em vez de
derrubar o caixa, nome de coluna nunca vem da rede, e o que o caixa precisa e
a nuvem não manda é preenchido pela configuração do terminal.

O que **não** é aplicado, e por quê
-----------------------------------

Receitas, linhas de receita e insumos ficam de fora até os modelos convergirem.
Converter `yield_grams` em `base_qty_g`/`yield_factor`, ou custo por unidade em
custo por quilo, é decidir a regra de baixa de estoque por adivinhação — e a
baixa errada aparece no CMV do mês, não no dia. E o saldo de insumo é do caixa
(ele é que registra os movimentos): aceitar o saldo da nuvem por LWW apagaria
movimentos locais ainda não sincronizados.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pdv.config import AppConfig


def _int(value: Any) -> int:
    # O driver da nuvem serializa BIGINT como texto ("700") para não perder
    # precisão no JSON; aqui volta a ser inteiro.
    return int(str(value))


def _flag(value: Any) -> int:
    if isinstance(value, str):
        return 1 if value.strip().lower() in ("1", "true", "t", "yes") else 0
    return 1 if value else 0


def _text(value: Any) -> str:
    return str(value)


@dataclass(frozen=True, slots=True)
class PullMapping:
    """O contrato de uma tabela de cadastro."""

    #: Colunas copiadas da nuvem quando presentes, com a conversão de cada uma.
    columns: dict[str, Callable[[Any], Any]]
    #: Sem estas, a linha é descartada — melhor que gravar cadastro pela metade.
    required: tuple[str, ...]
    #: O que o caixa exige e a nuvem não manda.
    fill: Callable[[AppConfig], dict[str, Any]] = field(default=lambda _c: {})


PULL_MAPPINGS: dict[str, PullMapping] = {
    "users": PullMapping(
        columns={
            "id": _text,
            "tenant_id": _text,
            "name": _text,
            "login": _text,
            "role": _text,
            "pin_hash": _text,
            "max_discount_percent": _text,
            "can_authorize": _flag,
            "is_active": _flag,
            "updated_at": _text,
        },
        required=("id", "tenant_id", "name", "login", "role", "updated_at"),
    ),
    "products": PullMapping(
        columns={
            "id": _text,
            "tenant_id": _text,
            "sku": _text,
            "barcode": _text,
            "name": _text,
            "category": _text,
            "pricing_mode": _text,
            "price_cents": _int,
            "tare_grams": _int,
            "is_active": _flag,
            "updated_at": _text,
            "server_seq": _int,
        },
        required=("id", "tenant_id", "sku", "name", "pricing_mode", "price_cents", "updated_at"),
        # O catálogo da nuvem vale para a rede; no caixa, o produto é desta loja.
        fill=lambda config: {"store_id": config.store_id},
    ),
}

#: Tabelas que a nuvem oferece e o caixa ainda não sabe aplicar com segurança.
NOT_APPLIED: dict[str, str] = {
    "recipes": "modelo de rendimento diferente (yield_grams × base_qty_g/yield_factor)",
    "recipe_lines": "quantidade por base diferente (quantity_mg × qty_per_base_mg)",
    "inventory_items": "o saldo é do caixa; custo em unidades diferentes",
}


def map_row(
    table: str, row: dict[str, Any], config: AppConfig
) -> dict[str, Any] | None:
    """A linha pronta para o banco do caixa, ou `None` se ela não serve.

    Linha de OUTRO tenant é descartada aqui também. A nuvem já filtra; conferir
    de novo custa uma comparação, e é o que impede um cadastro de outra rede —
    por defeito ou por ataque — de virar login válido neste caixa.
    """
    mapping = PULL_MAPPINGS[table]
    if str(row.get("tenant_id") or "") != config.tenant_id:
        return None
    if any(row.get(name) in (None, "") for name in mapping.required):
        return None

    values: dict[str, Any] = {}
    for name, convert in mapping.columns.items():
        if name not in row:
            continue
        raw = row[name]
        if raw is None:
            # Nulo vindo da nuvem é "sem valor", não "apague": fica o padrão do
            # caixa na inserção e o valor atual na atualização. Gravar NULL
            # violaria `NOT NULL DEFAULT` do lado de cá e derrubaria a tabela.
            continue
        try:
            values[name] = convert(raw)
        except (TypeError, ValueError):
            return None
    values.update(mapping.fill(config))
    return values


__all__ = ["NOT_APPLIED", "PULL_MAPPINGS", "PullMapping", "map_row"]
