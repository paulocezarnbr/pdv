"""Baixa fracionada de estoque por ficha técnica.

O problema que este módulo resolve:

O cliente leva 847 g de torta. A torta tem ficha técnica calculada para 1000 g
(farinha 250 g, açúcar 180 g, chocolate 320 g...). O sistema precisa baixar
**exatamente** a fração correspondente de cada insumo — 211,75 g de farinha,
152,46 g de açúcar — e não "1 torta".

Sem isso, o estoque de insumos vira ficção em uma semana e a previsão de demanda
(M13) recebe lixo como entrada.

Toda a matemática ocorre em **inteiros de miligrama**. O `Decimal` aparece só
nos fatores (perda e rendimento) e o arredondamento acontece **uma vez**, no
final de cada insumo.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from pdv.config import StockConfig
from pdv.data.repositories import StockRepository
from pdv.domain.errors import InsufficientStockError, InvalidWeightError
from pdv.domain.models import (
    Cents,
    EntityId,
    Grams,
    IngredientConsumption,
    Milligrams,
    Recipe,
)

_ONE = Decimal("1")
_HUNDRED = Decimal("100")


def explode_recipe(recipe: Recipe, sold_grams: Grams) -> tuple[IngredientConsumption, ...]:
    """Converte o peso vendido no consumo de cada insumo da ficha técnica.

    Fórmula por linha::

        consumo_mg = qty_per_base_mg
                   × (sold_grams / base_qty_g)     ← proporção do lote
                   × (1 + waste_percent / 100)     ← perda de manipulação
                   ÷ yield_factor                  ← perda de cocção

    ``yield_factor`` menor que 1 significa que o produto encolhe no forno: para
    entregar 847 g assados é preciso consumir **mais** insumo cru. Dividir (e
    não multiplicar) é o que representa isso corretamente.

    Raises:
        InvalidWeightError: peso não positivo ou fator de rendimento inválido.
    """
    if sold_grams <= 0:
        raise InvalidWeightError("Peso vendido deve ser maior que zero")
    if recipe.base_qty_g <= 0:
        raise InvalidWeightError(
            f"Ficha técnica {recipe.id} com rendimento-base inválido"
        )
    if recipe.yield_factor <= 0:
        raise InvalidWeightError(
            f"Ficha técnica {recipe.id} com fator de rendimento inválido"
        )

    proportion = Decimal(int(sold_grams)) / Decimal(int(recipe.base_qty_g))

    consumptions: list[IngredientConsumption] = []
    for line in recipe.lines:
        waste_multiplier = _ONE + (line.waste_percent / _HUNDRED)
        exact_mg = (
            Decimal(int(line.qty_per_base_mg))
            * proportion
            * waste_multiplier
            / recipe.yield_factor
        )
        # Arredondamento único, no fim. Nada de arredondar a proporção antes.
        consumed = int(exact_mg.quantize(_ONE, rounding=ROUND_HALF_UP))

        consumptions.append(
            IngredientConsumption(
                inventory_item_id=line.inventory_item_id,
                inventory_item_name=line.inventory_item_name,
                consumed_mg=Milligrams(consumed),
                unit_cost_cents=_cost_for(line.unit_cost_cents_per_kg, consumed),
            )
        )

    return tuple(consumptions)


def _cost_for(cost_cents_per_kg: Cents, consumed_mg: int) -> Cents:
    """Custo do insumo consumido — insumo para o CMV do módulo BI."""
    exact = Decimal(int(cost_cents_per_kg)) * Decimal(consumed_mg) / Decimal(1_000_000)
    return Cents(int(exact.quantize(_ONE, rounding=ROUND_HALF_UP)))


def total_cost_cents(consumptions: tuple[IngredientConsumption, ...]) -> Cents:
    """Custo total da mercadoria vendida para este item."""
    return Cents(sum(int(c.unit_cost_cents) for c in consumptions))


class StockService:
    """Aplica a baixa no SQLite local, dentro da transação do chamador."""

    def __init__(self, repository: StockRepository, config: StockConfig) -> None:
        self._repository = repository
        self._config = config

    def check_availability(
        self, consumptions: tuple[IngredientConsumption, ...]
    ) -> list[str]:
        """Valida o saldo projetado e devolve os avisos.

        Política padrão do food service: **avisa, mas não trava a fila**. Um
        insumo com saldo negativo quase sempre é erro de cadastro ou entrada de
        nota atrasada — travar a venda por causa disso custa mais caro que o
        próprio insumo. Tenants que preferem o contrário ligam
        `block_sale_on_negative_stock`.

        Raises:
            InsufficientStockError: se a política do tenant for bloquear.
        """
        warnings: list[str] = []
        for consumption in consumptions:
            available = int(self._repository.balance_mg(consumption.inventory_item_id))
            required = int(consumption.consumed_mg)
            if available < required:
                if self._config.block_sale_on_negative_stock:
                    raise InsufficientStockError(
                        consumption.inventory_item_name, available, required
                    )
                warnings.append(
                    f"{consumption.inventory_item_name}: saldo "
                    f"{available / 1000:.3f} g para consumo de {required / 1000:.3f} g"
                )
        return warnings

    def write_off(
        self,
        consumptions: tuple[IngredientConsumption, ...],
        *,
        tenant_id: EntityId,
        store_id: EntityId,
        device_id: EntityId,
        order_item_id: EntityId,
    ) -> None:
        """Grava um movimento de saída por insumo (quantidade **negativa**)."""
        for consumption in consumptions:
            self._repository.register_movement(
                tenant_id=tenant_id,
                store_id=store_id,
                device_id=device_id,
                inventory_item_id=consumption.inventory_item_id,
                qty_mg=Milligrams(-int(consumption.consumed_mg)),
                movement_type="sale",
                reference_type="order_item",
                reference_id=order_item_id,
                unit_cost_cents=consumption.unit_cost_cents,
            )
