"""Orquestração da venda no balcão.

Este é o serviço que amarra tudo. O método `register_weighed_item` executa, em
**uma única transação SQLite**:

1. valida a estabilidade do peso,
2. desconta a tara e calcula o preço,
3. explode a ficha técnica em consumo de insumos,
4. grava o item de venda,
5. baixa o estoque (um movimento por insumo),
6. grava a foto dos ingredientes consumidos,
7. encadeia dois eventos no ledger de auditoria,
8. enfileira tudo no outbox de sincronização.

Ou tudo isso acontece, ou nada acontece. Não existe o estado intermediário onde
o estoque baixou e a venda sumiu — que é exatamente o bug que transforma ERP de
restaurante em planilha paralela.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.data.repositories import (
    OutboxRepository,
    ProductRepository,
    RecipeRepository,
    SaleRepository,
    StockRepository,
)
from pdv.domain.errors import (
    InsufficientPaymentError,
    InvalidWeightError,
    UnstableWeightError,
)
from pdv.domain.models import (
    AuditEventType,
    AuditSeverity,
    Cents,
    EntityId,
    Grams,
    IngredientConsumption,
    Payment,
    PricingMode,
    Product,
    Sale,
    SaleItem,
    ScaleReading,
    new_id,
)
from pdv.hardware.printer.layout import ReceiptContext, build_sale_receipt
from pdv.services.audit import AuditService
from pdv.services.pricing import net_weight, price_for_weight
from pdv.services.stock import StockService, explode_recipe, total_cost_cents


@dataclass(frozen=True, slots=True)
class WeighedItemResult:
    """Resultado do registro de um item pesado."""

    item: SaleItem
    consumptions: tuple[IngredientConsumption, ...]
    cost_cents: Cents
    stock_warnings: tuple[str, ...] = ()
    audit_seq: int = 0

    @property
    def margin_cents(self) -> Cents:
        """Margem bruta do item — alimenta o BI e a sugestão de preço."""
        return Cents(int(self.item.total_cents) - int(self.cost_cents))


@dataclass
class OpenSale:
    """Venda em aberto no terminal. Mutável por natureza — é um rascunho."""

    id: EntityId
    client_uuid: EntityId
    local_number: int
    items: list[SaleItem] = field(default_factory=list)
    discount_cents: Cents = Cents(0)

    @property
    def subtotal_cents(self) -> Cents:
        return Cents(sum(int(item.total_cents) for item in self.items))

    @property
    def total_cents(self) -> Cents:
        return Cents(max(0, int(self.subtotal_cents) - int(self.discount_cents)))


class CheckoutService:
    """Fachada usada pela UI do caixa."""

    def __init__(self, database: Database, config: AppConfig) -> None:
        self._db = database
        self._config = config
        self._outbox = OutboxRepository()
        self._stock_service: StockService | None = None
        self._current: OpenSale | None = None

    # -- ciclo da venda ------------------------------------------------------- #

    @property
    def current_sale(self) -> OpenSale | None:
        return self._current

    def open_sale(self, operator_id: EntityId) -> OpenSale:
        """Abre a venda já persistida — não é rascunho em memória.

        Persistir na abertura é o que garante que uma queda de energia entre o
        primeiro e o segundo item não perca o primeiro.
        """
        if self._current is not None:
            return self._current

        with self._db.transaction() as connection:
            local_number = self._db.next_counter(connection, "order_local_number")
            order_id = new_id()
            client_uuid = new_id()

            SaleRepository(connection, self._outbox).create_order(
                order_id=order_id,
                client_uuid=client_uuid,
                tenant_id=EntityId(self._config.tenant_id),
                store_id=EntityId(self._config.store_id),
                device_id=EntityId(self._config.device_id),
                operator_id=operator_id,
                local_number=local_number,
            )

        self._current = OpenSale(
            id=order_id, client_uuid=client_uuid, local_number=local_number
        )
        return self._current

    def register_weighed_item(
        self,
        *,
        product: Product,
        reading: ScaleReading,
        operator_id: EntityId,
        tare_override_grams: Grams | None = None,
    ) -> WeighedItemResult:
        """Registra um item pesado. **Transação única** (ver docstring do módulo).

        Raises:
            UnstableWeightError: peso não estabilizado — a mercadoria ainda está
                acomodando no prato e o valor cobrado seria o errado.
            InvalidWeightError: peso zerado ou tara maior que o bruto.
            RecipeNotFoundError: produto pesável sem ficha técnica.
        """
        if not reading.status.sellable:
            raise UnstableWeightError(
                f"Peso não estabilizado (status: {reading.status.value}). "
                "Aguarde a balança parar antes de registrar."
            )
        if product.pricing_mode is not PricingMode.WEIGHT:
            raise InvalidWeightError(
                f"Produto {product.name!r} não é vendido por peso"
            )

        sale = self._current or self.open_sale(operator_id)

        gross = reading.weight_grams
        tare = tare_override_grams if tare_override_grams is not None else product.tare_grams
        net = net_weight(gross, tare)
        if net <= 0:
            raise InvalidWeightError("Peso líquido zerado após descontar a tara")

        total = price_for_weight(product.price_cents, net)

        with self._db.transaction() as connection:
            recipe = RecipeRepository(connection).get_for_product(product)
            consumptions = explode_recipe(recipe, net)

            stock_repository = StockRepository(connection, self._outbox)
            stock_service = StockService(stock_repository, self._config.stock)
            warnings = stock_service.check_availability(consumptions)

            item = SaleItem(
                id=new_id(),
                client_uuid=new_id(),
                product_id=product.id,
                product_name=product.name,
                pricing_mode=PricingMode.WEIGHT,
                unit_price_cents=product.price_cents,
                total_cents=total,
                quantity=Decimal("1"),
                gross_weight_grams=gross,
                tare_grams=tare,
                net_weight_grams=net,
                # Prova pericial: o quadro CRU da balança vai para o banco e
                # sobe para a nuvem junto do item.
                scale_reading_raw=reading.raw_frame,
                consumptions=consumptions,
            )

            sale_repository = SaleRepository(connection, self._outbox)
            sale_repository.add_item(
                item, order_id=sale.id, tenant_id=EntityId(self._config.tenant_id)
            )

            stock_service.write_off(
                consumptions,
                tenant_id=EntityId(self._config.tenant_id),
                store_id=EntityId(self._config.store_id),
                device_id=EntityId(self._config.device_id),
                order_item_id=item.id,
            )

            audit = self._audit()

            # Dois eventos distintos de propósito: o peso capturado é a prova do
            # que a balança disse; o item registrado é a prova do que foi
            # cobrado. Divergência entre os dois denuncia manipulação.
            weight_entry = audit.append(
                connection,
                event_type=AuditEventType.WEIGHT_CAPTURED,
                actor_user_id=operator_id,
                severity=AuditSeverity.INFO,
                payload={
                    "order_id": sale.id,
                    "product_id": product.id,
                    "product_name": product.name,
                    "gross_grams": int(gross),
                    "tare_grams": int(tare),
                    "net_grams": int(net),
                    "scale_status": reading.status.value,
                    "scale_raw_frame": reading.raw_frame,
                    "read_at": reading.read_at.isoformat(),
                },
            )

            audit.append(
                connection,
                event_type=AuditEventType.ITEM_REGISTERED,
                actor_user_id=operator_id,
                severity=AuditSeverity.INFO,
                payload={
                    "order_id": sale.id,
                    "order_item_id": item.id,
                    "product_id": product.id,
                    "net_grams": int(net),
                    "price_cents_per_kg": int(product.price_cents),
                    "total_cents": int(total),
                    "consumptions": [
                        {
                            "inventory_item_id": c.inventory_item_id,
                            "consumed_mg": int(c.consumed_mg),
                        }
                        for c in consumptions
                    ],
                },
            )

            sale.items.append(item)
            sale_repository.update_totals(
                sale.id, sale.subtotal_cents, sale.discount_cents, sale.total_cents
            )

        return WeighedItemResult(
            item=item,
            consumptions=consumptions,
            cost_cents=total_cost_cents(consumptions),
            stock_warnings=tuple(warnings),
            audit_seq=weight_entry.seq,
        )

    # -- fechamento ----------------------------------------------------------- #

    def finalize_sale(
        self,
        *,
        payments: tuple[Payment, ...],
        operator_id: EntityId,
        operator_name: str,
        customer_name: str | None = None,
    ) -> bytes:
        """Fecha a venda e devolve o payload ESC/POS pronto para impressão.

        A impressão **não** acontece aqui: o payload é entregue à fila
        (`PrintService`). A venda já está confirmada quando o papel começa a
        sair — se a impressora falhar, reimprime-se; a transação não se perde.
        """
        sale = self._current
        if sale is None or not sale.items:
            raise InvalidWeightError("Não há venda aberta com itens para finalizar")

        payments = self._settle_payments(payments, sale.total_cents)

        with self._db.transaction() as connection:
            SaleRepository(connection, self._outbox).close_order(
                order_id=sale.id,
                client_uuid=sale.client_uuid,
                tenant_id=EntityId(self._config.tenant_id),
                store_id=EntityId(self._config.store_id),
                device_id=EntityId(self._config.device_id),
                subtotal=sale.subtotal_cents,
                discount=sale.discount_cents,
                total=sale.total_cents,
            )

            self._audit().append(
                connection,
                event_type=AuditEventType.SALE_CLOSED,
                actor_user_id=operator_id,
                severity=AuditSeverity.INFO,
                payload={
                    "order_id": sale.id,
                    "local_number": sale.local_number,
                    "items": len(sale.items),
                    "subtotal_cents": int(sale.subtotal_cents),
                    "discount_cents": int(sale.discount_cents),
                    "total_cents": int(sale.total_cents),
                    "payments": [
                        {"method": p.method.value, "amount_cents": int(p.amount_cents)}
                        for p in payments
                    ],
                },
            )

        domain_sale = Sale(
            id=sale.id,
            client_uuid=sale.client_uuid,
            tenant_id=EntityId(self._config.tenant_id),
            store_id=EntityId(self._config.store_id),
            device_id=EntityId(self._config.device_id),
            operator_id=operator_id,
            local_number=sale.local_number,
            items=tuple(sale.items),
            discount_cents=sale.discount_cents,
        )

        receipt = build_sale_receipt(
            domain_sale,
            payments,
            ReceiptContext(
                store_name=self._config.store_name,
                store_document=self._config.store_document,
                store_address=self._config.store_address,
                operator_name=operator_name,
                terminal_label=f"PDV {self._config.device_id[:8]}",
                customer_name=customer_name,
            ),
            self._config.printer,
        )

        self._current = None
        return receipt

    def cancel_item(
        self,
        *,
        index: int,
        operator_id: EntityId,
        authorizer_id: EntityId,
        reason: str,
    ) -> None:
        """Cancela item já registrado — **exige autorização de gerente**.

        O item não é apagado: recebe `canceled_at`, o estoque é **estornado**
        com movimento positivo e a operação vira evento `critical` no ledger.
        Cancelamento é o vetor de furto mais comum em PDV; por isso a trilha
        registra quem pediu e quem liberou.
        """
        sale = self._current
        if sale is None or index >= len(sale.items):
            raise InvalidWeightError("Item inexistente na venda atual")

        item = sale.items[index]

        with self._db.transaction() as connection:
            connection.execute(
                "UPDATE order_items SET canceled_at = datetime('now'), "
                "canceled_by_user_id = ?, cancel_reason = ? WHERE id = ?",
                (authorizer_id, reason, item.id),
            )

            stock_repository = StockRepository(connection, self._outbox)
            for consumption in item.consumptions:
                stock_repository.register_movement(
                    tenant_id=EntityId(self._config.tenant_id),
                    store_id=EntityId(self._config.store_id),
                    device_id=EntityId(self._config.device_id),
                    inventory_item_id=consumption.inventory_item_id,
                    qty_mg=consumption.consumed_mg,  # positivo = estorno
                    movement_type="adjustment",
                    reference_type="order_item_cancel",
                    reference_id=item.id,
                )

            self._audit().append(
                connection,
                event_type=AuditEventType.ITEM_CANCELED,
                actor_user_id=operator_id,
                authorizer_user_id=authorizer_id,
                severity=AuditSeverity.CRITICAL,
                payload={
                    "order_id": sale.id,
                    "order_item_id": item.id,
                    "product_name": item.product_name,
                    "net_grams": int(item.net_weight_grams),
                    "total_cents": int(item.total_cents),
                    "reason": reason,
                },
            )

            sale.items.pop(index)
            SaleRepository(connection, self._outbox).update_totals(
                sale.id, sale.subtotal_cents, sale.discount_cents, sale.total_cents
            )

    # -- internos ------------------------------------------------------------- #

    @staticmethod
    def _settle_payments(
        payments: tuple[Payment, ...], total_cents: Cents
    ) -> tuple[Payment, ...]:
        """Valida a quitação e calcula o troco.

        Duas regras que o caixa não pode violar:

        * **Pagamento insuficiente não fecha venda.** Sem esta checagem, um
          pedido sai pela porta parcialmente pago e a diferença só aparece na
          conciliação — quando já não há a quem cobrar.
        * **Troco só existe em espécie.** Sobra em cartão ou PIX significa
          valor digitado errado na maquininha, não troco a devolver. Devolver
          dinheiro vivo contra um pagamento eletrônico é o golpe do troco.
        """
        if not payments:
            raise InsufficientPaymentError(int(total_cents), 0)

        paid = sum(int(p.amount_cents) for p in payments)
        if paid < int(total_cents):
            raise InsufficientPaymentError(int(total_cents), paid)

        change = paid - int(total_cents)
        if change == 0:
            return tuple(
                Payment(p.method, p.amount_cents, Cents(0)) for p in payments
            )

        cash_index = next(
            (i for i, p in enumerate(payments) if p.method.opens_drawer), None
        )
        if cash_index is None:
            raise InsufficientPaymentError(
                int(total_cents),
                paid,
                detail=(
                    f"Pagamento eletrônico excede o total em "
                    f"R$ {change / 100:.2f}. Corrija o valor: não há troco "
                    "para cartão ou PIX."
                ),
            )

        return tuple(
            Payment(p.method, p.amount_cents, Cents(change if i == cash_index else 0))
            for i, p in enumerate(payments)
        )

    def _audit(self) -> AuditService:
        return AuditService(
            tenant_id=EntityId(self._config.tenant_id),
            store_id=EntityId(self._config.store_id),
            device_id=EntityId(self._config.device_id),
            outbox=self._outbox,
        )

    def pending_sync_count(self) -> int:
        return self._outbox.pending_count(self._db.connection)

    def products(self) -> list[Product]:
        return ProductRepository(self._db.connection).list_active(
            EntityId(self._config.tenant_id)
        )
