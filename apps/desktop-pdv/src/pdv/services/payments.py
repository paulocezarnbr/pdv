"""Quitação e registro do pagamento.

Este módulo nasceu de duas correções, e vale registrar as duas.

**A quitação era um método privado do caixa.** `CheckoutService._settle_payments`
validava troco e pagamento insuficiente, e era a única implementação. Quando o
recebimento de mesa apareceu — o caixa fecha a conta que o garçom pediu — a
alternativa era reimplementar as mesmas duas regras num segundo lugar. Duas
cópias da regra de troco divergem na primeira alteração, e o lado que diverge é
o que devolve dinheiro vivo.

**O pagamento não era gravado.** A venda fechava, o ledger registrava a forma de
pagamento dentro do evento, e a tabela `payments` — que existe no schema desde
o começo — ficava vazia. Na prática o sistema sabia *quanto* entrou e não sabia
*como*: fechamento de caixa por forma de pagamento, conciliação de maquininha e
conferência de sangria não tinham de onde sair. O ledger não substitui isso: ele
é prova de que o evento aconteceu, não a tabela de onde se soma dinheiro.
"""

from __future__ import annotations

import logging
import sqlite3

from pdv.data.repositories import OutboxRepository
from pdv.domain.errors import InsufficientPaymentError
from pdv.domain.models import Cents, EntityId, Payment, iso, new_id, utc_now

logger = logging.getLogger(__name__)


def settle_payments(
    payments: tuple[Payment, ...], total_cents: Cents
) -> tuple[Payment, ...]:
    """Valida a quitação e calcula o troco.

    Duas regras que o caixa não pode violar:

    * **Pagamento insuficiente não fecha venda.** Sem esta checagem, um pedido
      sai pela porta parcialmente pago e a diferença só aparece na conciliação
      — quando já não há a quem cobrar.
    * **Troco só existe em espécie.** Sobra em cartão ou PIX significa valor
      digitado errado na maquininha, não troco a devolver. Devolver dinheiro
      vivo contra um pagamento eletrônico é o golpe do troco.

    Raises:
        InsufficientPaymentError: falta dinheiro, ou sobra em meio eletrônico.
    """
    if not payments:
        raise InsufficientPaymentError(int(total_cents), 0)

    paid = sum(int(p.amount_cents) for p in payments)
    if paid < int(total_cents):
        raise InsufficientPaymentError(int(total_cents), paid)

    change = paid - int(total_cents)
    if change == 0:
        return tuple(Payment(p.method, p.amount_cents, Cents(0)) for p in payments)

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


def record_payments(
    connection: sqlite3.Connection,
    outbox: OutboxRepository,
    *,
    order_id: EntityId,
    tenant_id: EntityId,
    payments: tuple[Payment, ...],
) -> None:
    """Grava as linhas de pagamento e as enfileira para a nuvem.

    Recebe a conexão do chamador de propósito: o pagamento precisa entrar na
    **mesma** transação que fecha o pedido. Gravado depois, uma queda de energia
    no intervalo deixaria um pedido pago sem forma de pagamento — e a diferença
    apareceria só no fechamento do caixa, sem ninguém a quem perguntar.
    """
    now = iso(utc_now())
    for payment in payments:
        payment_id = new_id()
        client_uuid = new_id()
        connection.execute(
            """
            INSERT INTO payments
                (id, order_id, tenant_id, method, amount_cents, change_cents,
                 created_at, client_uuid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payment_id,
                order_id,
                tenant_id,
                payment.method.value,
                int(payment.amount_cents),
                int(payment.change_cents),
                now,
                client_uuid,
            ),
        )
        outbox.enqueue(
            connection,
            entity_table="payments",
            entity_id=EntityId(payment_id),
            client_uuid=EntityId(client_uuid),
            operation="insert",
            payload={
                "id": payment_id,
                "order_id": order_id,
                "method": payment.method.value,
                "amount_cents": int(payment.amount_cents),
                "change_cents": int(payment.change_cents),
                "created_at": now,
            },
        )


__all__ = ["record_payments", "settle_payments"]
