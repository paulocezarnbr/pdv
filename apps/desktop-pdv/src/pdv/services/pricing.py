"""Precificação de item pesado.

Funções puras, sem I/O. Toda a aritmética passa por `Decimal` com
`ROUND_HALF_UP` — o arredondamento comercial usado no varejo brasileiro.

Por que não `float`: ``0.1 + 0.2 != 0.3`` em ponto flutuante binário. Numa loja
com 400 vendas/dia, centavos perdidos em arredondamento viram divergência de
caixa no fim do mês, e o operador leva a culpa por um bug do programador.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from pdv.domain.errors import InvalidWeightError
from pdv.domain.models import GRAMS_PER_KILO, Cents, Grams

_ONE_CENT = Decimal("1")


def net_weight(gross_grams: Grams, tare_grams: Grams) -> Grams:
    """Peso líquido = bruto − tara, nunca negativo.

    A tara é a embalagem (pote, bandeja, saco). Cobrar o peso da embalagem é
    infração ao Inmetro, além de roubar o cliente.

    Raises:
        InvalidWeightError: tara maior que o peso bruto — indica cadastro errado
            ou item colocado na balança sem a embalagem correspondente.
    """
    if gross_grams < 0:
        raise InvalidWeightError("Peso bruto negativo: tare a balança")
    if tare_grams < 0:
        raise InvalidWeightError("Tara negativa no cadastro do produto")
    if tare_grams > gross_grams:
        raise InvalidWeightError(
            f"Tara ({tare_grams} g) maior que o peso bruto ({gross_grams} g)"
        )
    return Grams(gross_grams - tare_grams)


def price_for_weight(price_cents_per_kg: Cents, net_grams: Grams) -> Cents:
    """Preço do item pesado, em centavos.

    ``total = preço_por_kg × gramas ÷ 1000``, arredondado **uma única vez** no
    fim. Arredondar em etapas intermediárias é a origem clássica da divergência
    de um centavo que o cliente sempre nota.

    Exemplo: R$ 49,90/kg × 847 g → 4990 × 847 ÷ 1000 = 4226,53 → **4227** centavos.
    """
    if net_grams < 0:
        raise InvalidWeightError("Peso líquido negativo")
    if price_cents_per_kg < 0:
        raise InvalidWeightError("Preço por quilo negativo no cadastro")

    exact = (
        Decimal(int(price_cents_per_kg)) * Decimal(int(net_grams))
    ) / Decimal(GRAMS_PER_KILO)
    return Cents(int(exact.quantize(_ONE_CENT, rounding=ROUND_HALF_UP)))


def price_for_units(unit_price_cents: Cents, quantity: Decimal) -> Cents:
    """Preço de item vendido por unidade (inclui fração, ex.: 1,5 porção)."""
    if quantity < 0:
        raise InvalidWeightError("Quantidade negativa")
    exact = Decimal(int(unit_price_cents)) * quantity
    return Cents(int(exact.quantize(_ONE_CENT, rounding=ROUND_HALF_UP)))


def apply_discount_percent(amount_cents: Cents, percent: Decimal) -> Cents:
    """Desconto percentual. O valor descontado é o arredondado, não o bruto."""
    if percent < 0 or percent > 100:
        raise InvalidWeightError(f"Percentual de desconto inválido: {percent}")
    exact = Decimal(int(amount_cents)) * percent / Decimal(100)
    return Cents(int(exact.quantize(_ONE_CENT, rounding=ROUND_HALF_UP)))


def weight_to_kg(grams: Grams) -> Decimal:
    """Gramas → quilos com as 3 casas que a balança entrega. Só para exibição."""
    return (Decimal(int(grams)) / Decimal(GRAMS_PER_KILO)).quantize(Decimal("0.001"))
