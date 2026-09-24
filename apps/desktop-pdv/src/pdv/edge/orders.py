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
    AuditEventType,
    AuditSeverity,
    Cents,
    EntityId,
    Grams,
    Payment,
    PricingMode,
    Sale,
    SaleItem,
    iso,
    new_id,
    utc_now,
)
from pdv.edge.hub import Event, EventHub
from pdv.edge.tables import TableError, TableService
from pdv.hardware.printer.layout import ReceiptContext, build_sale_receipt
from pdv.services.audit import AuditService
from pdv.services.payments import record_payments, settle_payments

logger = logging.getLogger(__name__)


class OrderNotFoundError(PdvError):
    """Pedido inexistente ou de outro tenant."""


class OrderClosedError(PdvError):
    """Tentativa de alterar um pedido que já foi fechado ou cancelado."""


class ProductNotSellableError(PdvError):
    """Produto inexistente, inativo ou que exige balança."""


class TableOccupiedError(PdvError):
    """A mesa já tem comanda aberta.

    Carrega o pedido existente porque a resposta certa para o garçom não é um
    erro: é a comanda que já está lá. Quem toca numa mesa ocupada quer lançar
    nela, e abrir uma segunda comanda partiria a conta em duas que ninguém
    consegue juntar na hora de cobrar.
    """

    def __init__(self, message: str, order: TableOrder) -> None:
        super().__init__(message)
        self.order = order


@dataclass(frozen=True, slots=True)
class TableOrder:
    id: EntityId
    client_uuid: EntityId
    local_number: int
    table_label: str
    status: str
    total_cents: Cents
    item_count: int
    table_id: EntityId | None = None
    bill_requested_at: str | None = None

    #: Quem abriu a comanda, e o nome dele na réplica local de usuários. O
    #: nome vem resolvido na mesma consulta porque a tela do caixa mostra
    #: "Mesa 4 · João" e buscar usuário por linha faria uma consulta por mesa.
    operator_id: EntityId | None = None
    waiter_name: str = ""
    tip_cents: Cents = Cents(0)
    opened_at: str | None = None

    @property
    def bill_requested(self) -> bool:
        return bool(self.bill_requested_at)

    @property
    def charged_cents(self) -> Cents:
        """O que o cliente paga: a conta **mais** a gorjeta."""
        return Cents(int(self.total_cents) + int(self.tip_cents))

    def to_json(self) -> dict[str, object]:
        return {
            "order_id": self.id,
            "client_uuid": self.client_uuid,
            "local_number": self.local_number,
            "table_id": self.table_id,
            "table_label": self.table_label,
            "status": self.status,
            "total_cents": int(self.total_cents),
            "tip_cents": int(self.tip_cents),
            "item_count": self.item_count,
            "bill_requested_at": self.bill_requested_at,
            "operator_id": self.operator_id,
            "waiter_name": self.waiter_name,
            "opened_at": self.opened_at,
        }


@dataclass(frozen=True, slots=True)
class SettledOrder:
    """O resultado do recebimento, com o que o cupom precisa imprimir."""

    order: TableOrder
    payments: tuple[Payment, ...]
    tip_cents: Cents
    charged_cents: Cents
    #: O cupom pronto para a fila de impressão. A venda já está confirmada
    #: quando o papel começa a sair: papel acabado não desfaz transação.
    receipt: bytes = b""

    @property
    def change_cents(self) -> Cents:
        return Cents(sum(int(p.change_cents) for p in self.payments))


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
        origin_device_id: EntityId,
        table_id: EntityId | None = None,
        table_label: str = "",
    ) -> TableOrder:
        """Abre um pedido, ou devolve o existente se o uuid já foi visto.

        Reenviar após um timeout é o caso **normal** no salão, não a exceção: o
        Wi-Fi da loja cai atrás da geladeira e o celular não sabe se o pedido
        entrou. Por isso repetir é seguro por construção.

        Args:
            table_id: a mesa do cadastro. `table_label` sozinho continua aceito
                para não quebrar aparelho antigo ainda não atualizado — mas o
                rótulo é resolvido contra o cadastro, e um nome desconhecido é
                recusado em vez de criar mesa fantasma.

        Raises:
            TableOccupiedError: a mesa já tem comanda aberta. A exceção carrega
                essa comanda, que é o que o app deve abrir.
        """
        existing = self._find_by_client_uuid(client_uuid)
        if existing is not None:
            logger.info("Pedido reenviado, devolvendo o existente: %s", client_uuid)
            return existing

        table = self._resolve_table(table_id, table_label)
        # A trava que faltava. Sem ela, dois garçons tocando na mesma mesa ao
        # mesmo tempo abriam duas comandas para um cliente só.
        occupying = self._open_order_of(table.id)
        if occupying is not None:
            raise TableOccupiedError(
                f"A {table.label} já tem a comanda {occupying.local_number} aberta.",
                occupying,
            )

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
                # `customer_id` guarda a **cópia** do rótulo, e não é redundância
                # com `table_id`: renomear a mesa amanhã não pode reescrever o
                # que saiu impresso no cupom de hoje.
                connection.execute(
                    "UPDATE orders SET table_id = ?, customer_id = ?, "
                    "updated_at = ? WHERE id = ?",
                    (table.id, table.label, now, order_id),
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
                        "status": "open",
                        "table_id": table.id,
                        "table_label": table.label,
                        "local_number": local_number,
                        "operator_id": operator_id,
                        "opened_at": now,
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
            table_label=table.label,
            status="open",
            total_cents=Cents(0),
            item_count=0,
            table_id=table.id,
        )
        self._hub.publish(
            Event(
                "order.opened",
                {
                    "order_id": order.id,
                    "local_number": order.local_number,
                    "table_id": table.id,
                    "table_label": order.table_label,
                },
            )
        )
        return order

    # -- fechamento ----------------------------------------------------------- #

    def request_bill(self, order_id: EntityId) -> TableOrder:
        """O garçom pede a conta. **Não** recebe.

        Aqui está a divisão que mantém o dinheiro num lugar só: o celular
        sinaliza que a mesa quer fechar, e o caixa recebe. Deixar o app fechar a
        conta criaria um segundo ponto de recebimento — sem gaveta, sem
        impressora e sem conferência de troco — que é como o furto de sala entra
        pela porta da frente.

        Repetir é inofensivo: marcar duas vezes não muda o instante gravado.
        """
        order = self._require_open(order_id)
        if order.bill_requested:
            return order

        now = iso(utc_now())
        with self._db.transaction() as connection:
            connection.execute(
                "UPDATE orders SET bill_requested_at = ?, updated_at = ?, "
                "is_synced = 0 WHERE id = ? AND status = 'open'",
                (now, now, order_id),
            )
            self._outbox.enqueue(
                connection,
                entity_table="orders",
                entity_id=order_id,
                client_uuid=EntityId(new_id()),
                operation="update",
                payload={"id": order_id, "bill_requested_at": now},
            )

        self._hub.publish(
            Event(
                "order.bill_requested",
                {
                    "order_id": order_id,
                    "local_number": order.local_number,
                    "table_label": order.table_label,
                    "total_cents": int(order.total_cents),
                },
            )
        )
        logger.info("Conta pedida: mesa %s", order.table_label)
        return self.get_order(order_id)

    def settle(
        self,
        *,
        order_id: EntityId,
        payments: tuple[Payment, ...],
        operator_id: EntityId,
        operator_name: str,
        tip_cents: Cents = Cents(0),
    ) -> SettledOrder:
        """O caixa **recebe** a conta da mesa e fecha a comanda.

        Era a metade que faltava. `request_bill` existia desde a Fase 3 e
        marcava a mesa como "pedindo a conta" — e ali a história acabava: nada
        no balcão fechava aquele pedido. Na prática a mesa ficava ocupada para
        sempre no mapa, e o jeito de liberá-la era cancelar a comanda, isto é,
        apagar a venda para poder sentar o próximo cliente. O vetor de furto do
        salão virava o procedimento normal da casa.

        O que este método faz é o fechamento de verdade: valida a quitação com
        as mesmas regras do balcão, grava as formas de pagamento, fecha o
        pedido e libera a mesa — que fica livre por consequência, porque a
        ocupação é derivada de existir pedido aberto apontando para ela.

        Sobre a gorjeta: entra por fora do total. Ela não é faturamento da loja
        e não pode inflar a base de imposto; é registrada na comanda para
        fechar o resultado de quem atendeu (ver `services/staff_report.py`).

        Raises:
            OrderClosedError: pedido já fechado ou cancelado. Repetir não
                cobra duas vezes.
            InsufficientPaymentError: falta dinheiro, ou sobra em meio
                eletrônico (ver `services/payments.py`).
        """
        order = self._require_open(order_id)
        if order.item_count == 0:
            raise OrderClosedError(
                f"A comanda {order.local_number} não tem itens. "
                "Mesa sem consumo se libera cancelando, não recebendo."
            )

        tip = Cents(max(0, int(tip_cents)))
        charged = Cents(int(order.total_cents) + int(tip))
        settled = settle_payments(payments, charged)

        now = iso(utc_now())
        with self._db.transaction() as connection:
            connection.execute(
                "UPDATE orders SET status = 'paid', closed_at = ?, updated_at = ?, "
                "       tip_cents = ?, is_synced = 0 "
                " WHERE id = ? AND status = 'open'",
                (now, now, int(tip), order_id),
            )
            record_payments(
                connection,
                self._outbox,
                order_id=order_id,
                tenant_id=EntityId(self._config.tenant_id),
                payments=settled,
            )
            self._outbox.enqueue(
                connection,
                entity_table="orders",
                entity_id=order_id,
                client_uuid=EntityId(new_id()),
                operation="update",
                payload={
                    "id": order_id,
                    "status": "paid",
                    "closed_at": now,
                    "total_cents": int(order.total_cents),
                    "tip_cents": int(tip),
                    "table_id": order.table_id,
                    "served_by_user_id": order.operator_id,
                },
            )
            # O recebimento da mesa entra no ledger como venda fechada, igual
            # ao do balcão: são a mesma coisa vista de dois lugares, e separá-las
            # faria o faturamento do dia depender de somar dois relatórios.
            self._audit().append(
                connection,
                event_type=AuditEventType.SALE_CLOSED,
                actor_user_id=operator_id,
                severity=AuditSeverity.INFO,
                payload={
                    "order_id": order_id,
                    "local_number": order.local_number,
                    "channel": "waiter",
                    "table_label": order.table_label,
                    "served_by_user_id": order.operator_id,
                    "served_by_name": order.waiter_name,
                    "received_by_name": operator_name,
                    "items": order.item_count,
                    "total_cents": int(order.total_cents),
                    "tip_cents": int(tip),
                    "payments": [
                        {"method": p.method.value, "amount_cents": int(p.amount_cents)}
                        for p in settled
                    ],
                },
            )

        self._hub.publish(
            Event(
                "order.settled",
                {
                    "order_id": order_id,
                    "local_number": order.local_number,
                    "table_id": order.table_id,
                    "table_label": order.table_label,
                    "total_cents": int(order.total_cents),
                    "tip_cents": int(tip),
                },
            )
        )
        logger.info(
            "Mesa recebida: %s (comanda %s) por %s",
            order.table_label, order.local_number, operator_name,
        )
        closed = self.get_order(order_id)
        return SettledOrder(
            order=closed,
            payments=settled,
            tip_cents=tip,
            charged_cents=charged,
            receipt=self._receipt(closed, settled, operator_name),
        )

    def _receipt(
        self, order: TableOrder, payments: tuple[Payment, ...], operator_name: str
    ) -> bytes:
        """Monta o cupom da mesa com o mesmo layout do balcão.

        Mesmo caminho de `CheckoutService.finalize_sale`, e de propósito: um
        segundo layout de cupom só para mesa divergiria do primeiro na próxima
        alteração legal, e o cliente receberia documentos diferentes conforme
        tivesse sentado ou comprado no balcão.
        """
        sale = Sale(
            id=order.id,
            client_uuid=order.client_uuid,
            tenant_id=EntityId(self._config.tenant_id),
            store_id=EntityId(self._config.store_id),
            device_id=EntityId(self._config.device_id),
            operator_id=order.operator_id or EntityId(self._config.device_id),
            local_number=order.local_number,
            items=self._sale_items(order.id),
            discount_cents=Cents(0),
        )
        return build_sale_receipt(
            sale,
            payments,
            ReceiptContext(
                store_name=self._config.store_name,
                store_document=self._config.store_document,
                store_address=self._config.store_address,
                operator_name=operator_name,
                terminal_label=f"PDV {self._config.device_id[:8]}",
                # O rótulo da mesa vai no lugar do cliente: é o que a pessoa
                # confere para saber que a conta é a dela.
                customer_name=order.table_label or None,
            ),
            self._config.printer,
        )

    def _sale_items(self, order_id: EntityId) -> tuple[SaleItem, ...]:
        """Os itens vivos da comanda, no formato que o cupom consome.

        Os cancelados ficam de fora: no cupom eles confundiriam o cliente, que
        não tem como distinguir "cancelado" de "cobrado". Na tela do garçom
        eles aparecem, porque lá o problema é o oposto — é ele quem precisa
        mostrar ao cliente que o item saiu da conta.
        """
        rows = self._db.query_all(
            "SELECT id, client_uuid, product_id, product_name, pricing_mode, "
            "       quantity, unit_price_cents, total_cents "
            "  FROM order_items "
            " WHERE order_id = ? AND canceled_at IS NULL "
            " ORDER BY created_at",
            (order_id,),
        )
        return tuple(
            SaleItem(
                id=EntityId(str(row["id"])),
                client_uuid=EntityId(str(row["client_uuid"])),
                product_id=EntityId(str(row["product_id"])),
                product_name=str(row["product_name"]),
                pricing_mode=PricingMode(str(row["pricing_mode"])),
                quantity=Decimal(str(row["quantity"])),
                gross_weight_grams=Grams(0),
                tare_grams=Grams(0),
                net_weight_grams=Grams(0),
                unit_price_cents=Cents(int(row["unit_price_cents"])),
                total_cents=Cents(int(row["total_cents"])),
                scale_reading_raw=None,
                consumptions=(),
            )
            for row in rows
        )

    def clear_bill_request(self, order_id: EntityId) -> TableOrder:
        """Desfaz o pedido de conta — a mesa resolveu pedir sobremesa."""
        now = iso(utc_now())
        with self._db.transaction() as connection:
            connection.execute(
                "UPDATE orders SET bill_requested_at = NULL, updated_at = ?, "
                "is_synced = 0 WHERE id = ? AND status = 'open'",
                (now, order_id),
            )
        self._hub.publish(Event("order.bill_cleared", {"order_id": order_id}))
        return self.get_order(order_id)

    def cancel_order(
        self,
        *,
        order_id: EntityId,
        authorizer_id: EntityId,
        authorizer_name: str,
        reason: str,
    ) -> TableOrder:
        """Cancela a comanda inteira — **exige gerente** (ver `manager.py`).

        Uma mesa aberta por engano precisa sumir do salão, senão o mapa mente e
        a mesa fica bloqueada a noite toda. Mas cancelar comanda com item
        lançado é o vetor de furto clássico: a comida sai, a comanda some.
        Por isso passa por credencial e vira evento `critical` no ledger.
        """
        order = self._require_open(order_id)
        reason = " ".join(str(reason).split())[:200]
        if not reason:
            raise ProductNotSellableError("Cancelar comanda exige motivo.")

        now = iso(utc_now())
        with self._db.transaction() as connection:
            sales = SaleRepository(connection, self._outbox)
            for row in connection.execute(
                "SELECT id FROM order_items WHERE order_id = ? AND canceled_at IS NULL",
                (order_id,),
            ).fetchall():
                sales.cancel_item(
                    EntityId(str(row["id"])),
                    canceled_at=now,
                    canceled_by_user_id=authorizer_id,
                    reason=f"[comanda cancelada] {reason}",
                )
            # Ticket na fila da cozinha de comanda cancelada some da tela: manter
            # é mandar preparar comida que ninguém vai receber.
            connection.execute(
                "UPDATE kds_tickets SET status = 'canceled', updated_at = ? "
                " WHERE order_id = ? AND status <> 'canceled'",
                (now, order_id),
            )
            connection.execute(
                "UPDATE orders SET status = 'canceled', closed_at = ?, "
                "authorized_by_user_id = ?, subtotal_cents = 0, discount_cents = 0, "
                "total_cents = 0, updated_at = ?, is_synced = 0 WHERE id = ?",
                (now, authorizer_id, now, order_id),
            )
            self._outbox.enqueue(
                connection,
                entity_table="orders",
                entity_id=order_id,
                client_uuid=EntityId(new_id()),
                operation="update",
                payload={
                    "id": order_id,
                    "status": "canceled",
                    "authorized_by_user_id": authorizer_id,
                    "reason": reason,
                },
            )
            self._audit().append(
                connection,
                event_type=AuditEventType.ITEM_CANCELED,
                actor_user_id=authorizer_id,
                authorizer_user_id=authorizer_id,
                severity=AuditSeverity.CRITICAL,
                payload={
                    "order_id": order_id,
                    "local_number": order.local_number,
                    "table_label": order.table_label,
                    "item_count": order.item_count,
                    "total_cents": int(order.total_cents),
                    "reason": reason,
                    "authorizer_name": authorizer_name,
                    "channel": "waiter",
                },
            )

        self._hub.publish(
            Event(
                "order.canceled",
                {
                    "order_id": order_id,
                    "local_number": order.local_number,
                    "table_label": order.table_label,
                },
            )
        )
        logger.warning(
            "Comanda %s cancelada por %s: %s",
            order.local_number, authorizer_name, reason,
        )
        return self.get_order(order_id)

    def transfer(
        self,
        *,
        order_id: EntityId,
        table_id: EntityId,
        authorizer_id: EntityId,
        authorizer_name: str,
    ) -> TableOrder:
        """Muda a comanda de mesa.

        A mesa de destino precisa estar livre. Empurrar uma comanda para cima de
        outra juntaria duas contas sem ninguém decidir isso — e a junção de
        contas é operação de caixa, não de celular.
        """
        order = self._require_open(order_id)
        table = self._resolve_table(table_id, "")
        if table.id == order.table_id:
            return order

        occupying = self._open_order_of(table.id)
        if occupying is not None:
            raise TableOccupiedError(
                f"A {table.label} já tem a comanda {occupying.local_number} aberta.",
                occupying,
            )

        now = iso(utc_now())
        with self._db.transaction() as connection:
            connection.execute(
                "UPDATE orders SET table_id = ?, customer_id = ?, updated_at = ?, "
                "is_synced = 0 WHERE id = ?",
                (table.id, table.label, now, order_id),
            )
            self._outbox.enqueue(
                connection,
                entity_table="orders",
                entity_id=order_id,
                client_uuid=EntityId(new_id()),
                operation="update",
                payload={
                    "id": order_id,
                    "table_id": table.id,
                    "table_label": table.label,
                },
            )
            self._audit().append(
                connection,
                event_type=AuditEventType.PRICE_OVERRIDE,
                actor_user_id=authorizer_id,
                authorizer_user_id=authorizer_id,
                severity=AuditSeverity.WARNING,
                payload={
                    "operation": "table_transfer",
                    "order_id": order_id,
                    "from_table": order.table_label,
                    "to_table": table.label,
                    "authorizer_name": authorizer_name,
                    "channel": "waiter",
                },
            )

        self._hub.publish(
            Event(
                "order.transferred",
                {
                    "order_id": order_id,
                    "from_table": order.table_label,
                    "to_table": table.label,
                },
            )
        )
        return self.get_order(order_id)

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
        created_by_user_id: EntityId | None = None,
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
            # Quem lançou, gravado no item e não só na comanda: mesa grande é
            # atendida por mais de uma pessoa, e atribuir tudo a quem abriu
            # apagaria o segundo garçom do relatório e da trilha.
            SaleRepository(connection, self._outbox).add_item(
                item,
                order_id=order_id,
                tenant_id=EntityId(self._config.tenant_id),
                created_by_user_id=created_by_user_id,
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

    #: Colunas do pedido de mesa. Uma constante porque `get_order`,
    #: `list_open_orders` e a busca por mesa precisam montar exatamente o mesmo
    #: `TableOrder`, e três listas de colunas divergem na primeira alteração.
    _SELECT = (
        "SELECT o.id, o.client_uuid, o.local_number, o.customer_id, o.status, "
        "       o.total_cents, o.tip_cents, o.table_id, o.bill_requested_at, "
        "       o.operator_id, o.opened_at, u.name AS waiter_name, "
        "       (SELECT COUNT(*) FROM order_items i "
        "         WHERE i.order_id = o.id AND i.canceled_at IS NULL) AS items "
        "  FROM orders o "
        # LEFT JOIN, não INNER: comanda aberta por um usuário que depois saiu do
        # cadastro não pode sumir da tela do caixa — sumir é pior que aparecer
        # sem nome, porque a mesa continua ocupada na vida real.
        "  LEFT JOIN users u ON u.id = o.operator_id "
    )

    def get_order(self, order_id: EntityId) -> TableOrder:
        row = self._db.query_one(
            self._SELECT + " WHERE o.id = ? AND o.tenant_id = ?",
            (order_id, self._config.tenant_id),
        )
        if row is None:
            raise OrderNotFoundError(f"Pedido {order_id} não encontrado.")
        return _to_order(row)

    def list_open_orders(self) -> list[TableOrder]:
        rows = self._db.query_all(
            self._SELECT
            + " WHERE o.tenant_id = ? AND o.status = 'open' AND o.channel = 'waiter' "
            " ORDER BY o.opened_at",
            (self._config.tenant_id,),
        )
        return [_to_order(row) for row in rows]

    def list_items(self, order_id: EntityId) -> list[dict[str, object]]:
        """Os itens da comanda, para a tela do garçom.

        Os cancelados vêm junto, marcados. Sumir com eles faria a conta parecer
        ter encolhido sozinha, e é exatamente o item cancelado que o garçom
        precisa conseguir mostrar ao cliente que reclama.
        """
        rows = self._db.query_all(
            "SELECT i.id, i.product_name, i.quantity, i.unit_price_cents, "
            "       i.total_cents, i.created_at, i.canceled_at, i.cancel_reason, "
            "       (SELECT t.status FROM kds_tickets t "
            "         WHERE t.order_item_id = i.id ORDER BY t.created_at DESC "
            "         LIMIT 1) AS kds_status "
            "  FROM order_items i WHERE i.order_id = ? ORDER BY i.created_at",
            (order_id,),
        )
        return [
            {
                "id": str(row["id"]),
                "product_name": str(row["product_name"]),
                "quantity": str(row["quantity"]),
                "unit_price_cents": int(row["unit_price_cents"]),
                "total_cents": int(row["total_cents"]),
                "created_at": str(row["created_at"]),
                "canceled": row["canceled_at"] is not None,
                "cancel_reason": row["cancel_reason"],
                "kds_status": row["kds_status"],
            }
            for row in rows
        ]

    # -- internos ------------------------------------------------------------- #

    def _find_by_client_uuid(self, client_uuid: EntityId) -> TableOrder | None:
        row = self._db.query_one(
            "SELECT id FROM orders WHERE client_uuid = ? AND tenant_id = ?",
            (client_uuid, self._config.tenant_id),
        )
        return self.get_order(EntityId(str(row["id"]))) if row else None

    def _open_order_of(self, table_id: EntityId) -> TableOrder | None:
        row = self._db.query_one(
            self._SELECT
            + " WHERE o.tenant_id = ? AND o.table_id = ? AND o.status = 'open' "
            " ORDER BY o.opened_at LIMIT 1",
            (self._config.tenant_id, table_id),
        )
        return _to_order(row) if row else None

    def _resolve_table(self, table_id: EntityId | None, label: str):  # noqa: ANN202
        """Aceita id ou rótulo, e sempre devolve mesa do **cadastro**.

        Aceitar rótulo livre foi o que criou "mesa 5", "Mesa 5" e "M5" como três
        mesas distintas. Aqui um nome desconhecido é recusado — o app mostra o
        mapa e o garçom escolhe.
        """
        tables = TableService(self._db, self._config)
        if table_id:
            table = tables.get(EntityId(str(table_id)))
            if not table.is_active:
                raise TableError(f"A {table.label} está fora do mapa do salão.")
            return table

        found = tables.find_by_label(label) if label.strip() else None
        if found is None:
            raise TableError(
                f"Mesa {label.strip()!r} não existe no cadastro. "
                "Cadastre-a nas opções de gerente antes de usá-la."
                if label.strip()
                else "Escolha uma mesa do salão."
            )
        return found

    def _audit(self) -> AuditService:
        return AuditService(
            tenant_id=EntityId(self._config.tenant_id),
            store_id=EntityId(self._config.store_id),
            device_id=EntityId(self._config.device_id),
            outbox=self._outbox,
            device_secret=self._config.device_secret,
        )

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
        table_id=EntityId(str(row["table_id"])) if row["table_id"] else None,
        bill_requested_at=(
            str(row["bill_requested_at"]) if row["bill_requested_at"] else None
        ),
        operator_id=EntityId(str(row["operator_id"])) if row["operator_id"] else None,
        waiter_name=str(row["waiter_name"] or ""),
        tip_cents=Cents(int(row["tip_cents"] or 0)),
        opened_at=str(row["opened_at"]) if row["opened_at"] else None,
    )


__all__ = [
    "OrderClosedError",
    "OrderNotFoundError",
    "ProductNotSellableError",
    "SettledOrder",
    "TableOccupiedError",
    "TableOrder",
    "TableOrderService",
]
