"""Pedidos lançados pelo app do garçom.

Diferença essencial para o `CheckoutService`
--------------------------------------------

O caixa atende **uma venda por vez** — por isso `CheckoutService` guarda
`_current`. O salão tem oito mesas abertas ao mesmo tempo, e dois garçons
lançando em paralelo. Aqui não existe "venda atual": toda operação identifica o
pedido explicitamente.

O invariante que impede faturamento duplicado
---------------------------------------------

O celular gera o `client_uuid` do pedido e o de cada item, e o PDV grava
**exatamente** esses valores. Isso importa porque o app tem dois caminhos até a
nuvem — a LAN (via este terminal) e a internet (direto) — e ele escolhe um
deles sem ter como saber, sob falha de rede, se o outro já entregou.

Se o terminal gerasse `client_uuid` próprio ao receber o pedido, o mesmo pedido
chegaria à nuvem com dois identificadores diferentes: um vindo do PDV e outro
vindo do celular. A deduplicação por `(tenant_id, client_uuid)` não veria
relação nenhuma entre eles e o restaurante seria **cobrado duas vezes pela mesma
comanda**. Preservar o uuid da origem é o que faz os dois caminhos convergirem.

Pelo mesmo motivo, reenviar um pedido que já chegou é inofensivo: a segunda
gravação encontra o uuid e devolve o pedido existente em vez de criar outro.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.data.repositories import OutboxRepository, ProductRepository, SaleRepository
from pdv.domain.errors import PdvError
from pdv.domain.models import (
    Cents,
    EntityId,
    Grams,
    PricingMode,
    SaleItem,
    iso,
    new_id,
    utc_now,
)
from pdv.edge.hub import Event, EventHub

logger = logging.getLogger(__name__)


class OrderNotFoundError(PdvError):
    """Pedido inexistente ou de outro tenant."""


class OrderClosedError(PdvError):
    """Tentativa de alterar um pedido que já foi fechado ou cancelado."""


class ProductNotSellableError(PdvError):
    """Produto inexistente, inativo ou que exige balança."""


@dataclass(frozen=True, slots=True)
class TableOrder:
    id: EntityId
    client_uuid: EntityId
    local_number: int
    table_label: str
    status: str
    total_cents: Cents
    item_count: int


class TableOrderService:
    """Pedidos de mesa, gravados no mesmo banco que o caixa usa."""

    def __init__(
        self,
        database: Database,
        config: AppConfig,
        hub: EventHub | None = None,
    ) -> None:
        self._db = database
        self._config = config
        self._outbox = OutboxRepository()
        self._hub = hub or EventHub()

    # -- abertura ------------------------------------------------------------- #

    def open_order(
        self,
        *,
        client_uuid: EntityId,
        operator_id: EntityId,
        table_label: str,
        origin_device_id: EntityId,
    ) -> TableOrder:
        """Abre um pedido, ou devolve o existente se o uuid já foi visto.

        Reenviar após um timeout é o caso **normal** no salão, não a exceção: o
        Wi-Fi da loja cai atrás da geladeira e o celular não sabe se o pedido
        entrou. Por isso repetir é seguro por construção.
        """
        existing = self._find_by_client_uuid(client_uuid)
        if existing is not None:
            logger.info("Pedido reenviado, devolvendo o existente: %s", client_uuid)
            return existing

        order_id = new_id()
        now = iso(utc_now())

        try:
            with self._db.transaction() as connection:
                local_number = self._db.next_counter(connection, "order_local_number")

                SaleRepository(connection, self._outbox).create_order(
                    order_id=order_id,
                    client_uuid=client_uuid,
                    tenant_id=EntityId(self._config.tenant_id),
                    store_id=EntityId(self._config.store_id),
                    device_id=EntityId(self._config.device_id),
                    operator_id=operator_id,
                    local_number=local_number,
                    channel="waiter",
                    origin_device_id=origin_device_id,
                )
                # A mesa mora no pedido, não numa tabela à parte: o salão desta
                # fase é simples e uma tabela `tables` só ganharia sentido com
                # mapa de salão, junção e transferência de mesa (Fase 5).
                connection.execute(
                    "UPDATE orders SET customer_id = ?, updated_at = ? WHERE id = ?",
                    (table_label.strip()[:32], now, order_id),
                )

                self._outbox.enqueue(
                    connection,
                    entity_table="orders",
                    entity_id=order_id,
                    client_uuid=client_uuid,
                    operation="insert",
                    payload={
                        "id": order_id,
                        "channel": "waiter",
                        "table_label": table_label,
                        "local_number": local_number,
                        "origin_device_id": origin_device_id,
                    },
                )
        except sqlite3.IntegrityError:
            # Corrida entre dois reenvios simultâneos do mesmo celular: o outro
            # ganhou. O resultado correto é o pedido dele, não um erro.
            existing = self._find_by_client_uuid(client_uuid)
            if existing is None:  # pragma: no cover
                raise
            return existing

        order = TableOrder(
            id=EntityId(order_id),
            client_uuid=client_uuid,
            local_number=local_number,
            table_label=table_label,
            status="open",
            total_cents=Cents(0),
            item_count=0,
        )
        self._hub.publish(
            Event(
                "order.opened",
                {
                    "order_id": order.id,
                    "local_number": order.local_number,
                    "table_label": order.table_label,
                },
            )
        )
        return order

    # -- itens ---------------------------------------------------------------- #

    def add_item(
        self,
        *,
        order_id: EntityId,
        client_uuid: EntityId,
        product_id: EntityId,
        quantity: Decimal,
        notes: str = "",
        station: str = "cozinha",
    ) -> TableOrder:
        """Acrescenta um item unitário e enfileira o ticket na cozinha.

        Item **por peso não entra por aqui**: quem pesa é a balança do balcão, e
        aceitar um peso digitado no celular abriria exatamente o buraco que o
        módulo anti-furto existe para fechar — peso informado por quem cobra, sem
        o quadro cru da balança como prova.
        """
        if quantity <= 0:
            raise ProductNotSellableError("Quantidade precisa ser maior que zero.")

        existing_item = self._db.query_one(
            "SELECT order_id FROM order_items WHERE client_uuid = ?", (client_uuid,)
        )
        if existing_item is not None:
            return self.get_order(EntityId(str(existing_item["order_id"])))

        order = self._require_open(order_id)
        product = ProductRepository(self._db.connection).get(product_id)

        if product is None:
            raise ProductNotSellableError(f"Produto {product_id} não encontrado.")
        if product.is_weighed:
            raise ProductNotSellableError(
                f"{product.name} é vendido por peso e precisa da balança do balcão."
            )

        # Preço unitário × quantidade, com arredondamento único no fim. Duas
        # multiplicações arredondadas separadamente acumulam centavos de
        # diferença ao longo do dia.
        total = Cents(
            int(
                (Decimal(int(product.price_cents)) * quantity).quantize(Decimal("1"))
            )
        )

        item = SaleItem(
            id=EntityId(new_id()),
            client_uuid=client_uuid,
            product_id=product.id,
            product_name=product.name,
            pricing_mode=PricingMode.UNIT,
            quantity=quantity,
            gross_weight_grams=Grams(0),
            tare_grams=Grams(0),
            net_weight_grams=Grams(0),
            unit_price_cents=product.price_cents,
            total_cents=total,
            scale_reading_raw=None,
            consumptions=(),
        )

        ticket_id = new_id()
        now = iso(utc_now())

        with self._db.transaction() as connection:
            SaleRepository(connection, self._outbox).add_item(
                item, order_id=order_id, tenant_id=EntityId(self._config.tenant_id)
            )
            connection.execute(
                "UPDATE orders SET subtotal_cents = subtotal_cents + ?, "
                "total_cents = total_cents + ?, updated_at = ? WHERE id = ?",
                (int(total), int(total), now, order_id),
            )
            connection.execute(
                """
                INSERT INTO kds_tickets
                    (id, tenant_id, store_id, order_id, order_item_id, station,
                     product_name, quantity, notes, status, queued_at,
                     created_at, updated_at, origin_device_id, client_uuid)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
                """,
                (
                    ticket_id, self._config.tenant_id, self._config.store_id,
                    order_id, item.id, station, product.name, str(quantity),
                    notes.strip()[:200] or None, now, now, now,
                    self._config.device_id, new_id(),
                ),
            )
            self._outbox.enqueue(
                connection,
                entity_table="order_items",
                entity_id=item.id,
                client_uuid=client_uuid,
                operation="insert",
                payload={
                    "order_id": order_id,
                    "product_id": product.id,
                    "quantity": str(quantity),
                    "total_cents": int(total),
                },
            )

        self._hub.publish(
            Event(
                "ticket.queued",
                {
                    "ticket_id": ticket_id,
                    "order_id": order_id,
                    "local_number": order.local_number,
                    "table_label": order.table_label,
                    "station": station,
                    "product_name": product.name,
                    "quantity": str(quantity),
                    "notes": notes,
                },
            )
        )
        return self.get_order(order_id)

    # -- consultas ------------------------------------------------------------ #

    def get_order(self, order_id: EntityId) -> TableOrder:
        row = self._db.query_one(
            "SELECT o.id, o.client_uuid, o.local_number, o.customer_id, o.status, "
            "       o.total_cents, "
            "       (SELECT COUNT(*) FROM order_items i "
            "         WHERE i.order_id = o.id AND i.canceled_at IS NULL) AS items "
            "  FROM orders o WHERE o.id = ? AND o.tenant_id = ?",
            (order_id, self._config.tenant_id),
        )
        if row is None:
            raise OrderNotFoundError(f"Pedido {order_id} não encontrado.")
        return _to_order(row)

    def list_open_orders(self) -> list[TableOrder]:
        rows = self._db.query_all(
            "SELECT o.id, o.client_uuid, o.local_number, o.customer_id, o.status, "
            "       o.total_cents, "
            "       (SELECT COUNT(*) FROM order_items i "
            "         WHERE i.order_id = o.id AND i.canceled_at IS NULL) AS items "
            "  FROM orders o "
            " WHERE o.tenant_id = ? AND o.status = 'open' AND o.channel = 'waiter' "
            " ORDER BY o.opened_at",
            (self._config.tenant_id,),
        )
        return [_to_order(row) for row in rows]

    # -- internos ------------------------------------------------------------- #

    def _find_by_client_uuid(self, client_uuid: EntityId) -> TableOrder | None:
        row = self._db.query_one(
            "SELECT id FROM orders WHERE client_uuid = ? AND tenant_id = ?",
            (client_uuid, self._config.tenant_id),
        )
        return self.get_order(EntityId(str(row["id"]))) if row else None

    def _require_open(self, order_id: EntityId) -> TableOrder:
        order = self.get_order(order_id)
        if order.status != "open":
            raise OrderClosedError(
                f"O pedido {order.local_number} está {order.status}. "
                "Um pedido fechado altera-se por estorno, não por edição."
            )
        return order


def _to_order(row: sqlite3.Row) -> TableOrder:
    return TableOrder(
        id=EntityId(str(row["id"])),
        client_uuid=EntityId(str(row["client_uuid"])),
        local_number=int(row["local_number"]),
        table_label=str(row["customer_id"] or ""),
        status=str(row["status"]),
        total_cents=Cents(int(row["total_cents"])),
        item_count=int(row["items"]),
    )
