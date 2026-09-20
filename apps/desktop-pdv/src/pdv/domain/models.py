"""Modelos de domínio do PDV.

Regras invioláveis (ver `docs/plan.md`, seção 4):

* Dinheiro é ``int`` em **centavos**. ``float`` é proibido.
* Peso é ``int`` em **gramas**.
* Insumo é ``int`` em **miligramas** (ou mililitros).
* Todo dataclass aqui é imutável (``frozen=True``): estado muda criando um novo
  objeto, nunca mutando o anterior. Isso elimina uma classe inteira de bug em
  código com threads (balança e impressora rodam fora da UI thread).

Este módulo não importa Qt, sqlite3, pyserial nem qualquer I/O — é puro, e por
isso testável sem hardware.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Final, NewType

# --------------------------------------------------------------------------- #
# Tipos primitivos nomeados — evitam trocar gramas por miligramas sem o mypy ver
# --------------------------------------------------------------------------- #

Cents = NewType("Cents", int)
Grams = NewType("Grams", int)
Milligrams = NewType("Milligrams", int)
EntityId = NewType("EntityId", str)

GENESIS_HASH: Final[str] = "0" * 64
GRAMS_PER_KILO: Final[int] = 1000
MILLIGRAMS_PER_GRAM: Final[int] = 1000


def new_id() -> EntityId:
    """UUIDv7 gerado no cliente (invariante 5 do plan.md).

    UUIDv7 é ordenado pelo tempo: o índice do PostgreSQL não fragmenta quando
    milhares de vendas offline sobem de uma vez. Cai para UUIDv4 em runtimes
    anteriores ao Python 3.14.
    """
    generator = getattr(uuid, "uuid7", None)
    if generator is not None:
        return EntityId(str(generator()))
    return EntityId(str(uuid.uuid4()))


def utc_now() -> datetime:
    """Timestamp do cliente. NÃO é a verdade — ver `server_received_at` no DER."""
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    """Serialização canônica para o SQLite (ISO-8601 em UTC)."""
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------- #
# Balança
# --------------------------------------------------------------------------- #


class ScaleStatus(Enum):
    """Estado reportado pela balança.

    Só ``STABLE`` autoriza o registro de uma venda — vender sobre peso instável
    é rombo de estoque garantido.
    """

    STABLE = "stable"
    UNSTABLE = "unstable"
    OVERLOAD = "overload"
    NEGATIVE = "negative"
    ZERO = "zero"
    ERROR = "error"

    @property
    def sellable(self) -> bool:
        return self is ScaleStatus.STABLE


@dataclass(frozen=True, slots=True)
class ScaleReading:
    """Uma leitura da balança.

    ``raw_frame`` guarda o quadro **cru** recebido na serial. Ele é persistido
    junto do item de venda (`order_items.scale_reading_raw`) e é a prova
    pericial de que o peso cobrado foi o peso lido — peça central do módulo
    anti-furto quando um operador alega "a balança que errou".
    """

    status: ScaleStatus
    weight_grams: Grams
    raw_frame: str
    read_at: datetime = field(default_factory=utc_now)

    @property
    def weight_kg(self) -> Decimal:
        return (Decimal(self.weight_grams) / Decimal(GRAMS_PER_KILO)).quantize(
            Decimal("0.001")
        )

    def with_tare(self, tare_grams: Grams) -> ScaleReading:
        """Retorna nova leitura com a tara descontada, nunca negativa."""
        net = max(0, self.weight_grams - tare_grams)
        return ScaleReading(
            status=self.status,
            weight_grams=Grams(net),
            raw_frame=self.raw_frame,
            read_at=self.read_at,
        )


# --------------------------------------------------------------------------- #
# Catálogo e ficha técnica
# --------------------------------------------------------------------------- #


class PricingMode(Enum):
    UNIT = "unit"
    WEIGHT = "weight"


@dataclass(frozen=True, slots=True)
class Product:
    id: EntityId
    tenant_id: EntityId
    sku: str
    name: str
    pricing_mode: PricingMode
    price_cents: Cents
    """Se ``UNIT``: preço do item. Se ``WEIGHT``: **preço por quilo**."""
    tare_grams: Grams = Grams(0)
    recipe_id: EntityId | None = None

    @property
    def is_weighed(self) -> bool:
        return self.pricing_mode is PricingMode.WEIGHT


@dataclass(frozen=True, slots=True)
class RecipeLine:
    """Uma linha da ficha técnica.

    ``qty_per_base_mg`` é a quantidade do insumo consumida para produzir
    ``Recipe.base_qty_g`` do produto. Manter em mg inteiro é o que permite a
    baixa fracionada exata sem acúmulo de erro.
    """

    inventory_item_id: EntityId
    inventory_item_name: str
    qty_per_base_mg: Milligrams
    waste_percent: Decimal = Decimal("0")
    unit_cost_cents_per_kg: Cents = Cents(0)


@dataclass(frozen=True, slots=True)
class Recipe:
    id: EntityId
    product_id: EntityId
    base_qty_g: Grams
    yield_factor: Decimal = Decimal("1.0")
    """Fator de rendimento (perda de cocção). 0.85 = o produto perde 15%."""
    lines: tuple[RecipeLine, ...] = ()


@dataclass(frozen=True, slots=True)
class IngredientConsumption:
    """Resultado da explosão da ficha técnica para um peso vendido."""

    inventory_item_id: EntityId
    inventory_item_name: str
    consumed_mg: Milligrams
    unit_cost_cents: Cents = Cents(0)


# --------------------------------------------------------------------------- #
# Venda
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SaleItem:
    """Item registrado na venda. Imutável: cancelar cria evento, não apaga."""

    id: EntityId
    client_uuid: EntityId
    product_id: EntityId
    product_name: str
    pricing_mode: PricingMode
    unit_price_cents: Cents
    total_cents: Cents
    quantity: Decimal = Decimal("1")
    gross_weight_grams: Grams = Grams(0)
    tare_grams: Grams = Grams(0)
    net_weight_grams: Grams = Grams(0)
    scale_reading_raw: str | None = None
    consumptions: tuple[IngredientConsumption, ...] = ()
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class Sale:
    id: EntityId
    client_uuid: EntityId
    tenant_id: EntityId
    store_id: EntityId
    device_id: EntityId
    operator_id: EntityId
    local_number: int
    items: tuple[SaleItem, ...] = ()
    discount_cents: Cents = Cents(0)
    created_at: datetime = field(default_factory=utc_now)

    @property
    def subtotal_cents(self) -> Cents:
        return Cents(sum(item.total_cents for item in self.items))

    @property
    def total_cents(self) -> Cents:
        return Cents(max(0, self.subtotal_cents - self.discount_cents))


class PaymentMethod(Enum):
    CASH = "cash"
    DEBIT = "debit"
    CREDIT = "credit"
    PIX = "pix"
    PREPAID = "prepaid"
    CREDIT_ACCOUNT = "credit_account"
    CASHBACK = "cashback"

    @property
    def opens_drawer(self) -> bool:
        """Só dinheiro justifica abrir a gaveta — o resto vira evento auditado."""
        return self is PaymentMethod.CASH


@dataclass(frozen=True, slots=True)
class Payment:
    method: PaymentMethod
    amount_cents: Cents
    change_cents: Cents = Cents(0)


# --------------------------------------------------------------------------- #
# Auditoria
# --------------------------------------------------------------------------- #


class AuditEventType(Enum):
    WEIGHT_CAPTURED = "weight_captured"
    ITEM_REGISTERED = "item_registered"
    ITEM_CANCELED = "item_canceled"
    DISCOUNT_APPLIED = "discount_applied"
    PRICE_OVERRIDE = "price_override"
    DRAWER_OPENED = "drawer_opened"
    WITHDRAWAL = "withdrawal"
    SALE_CLOSED = "sale_closed"
    SESSION_CLOSED = "session_closed"
    CASHBACK_CREDITED = "cashback_credited"
    CASHBACK_REDEEMED = "cashback_redeemed"
    SCALE_ANOMALY = "scale_anomaly"
    # Fase 3.5: um comando do painel que o terminal se recusou a obedecer.
    # Tipo próprio, e não um `price_override` reaproveitado: recusa remota é
    # sinal de segurança, e misturá-la com evento de operação a esconderia
    # justamente no relatório onde ela precisa aparecer.
    REMOTE_COMMAND_REFUSED = "remote_command_refused"


class AuditSeverity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """Elo da cadeia imutável. Ver `docs/der.md`, seção 2.6."""

    id: EntityId
    seq: int
    event_type: AuditEventType
    severity: AuditSeverity
    actor_user_id: EntityId
    authorizer_user_id: EntityId | None
    payload_json: str
    prev_hash: str
    hash: str
    created_at: datetime
