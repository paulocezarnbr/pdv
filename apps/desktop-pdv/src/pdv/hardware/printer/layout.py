"""Layout do cupom não fiscal em 80 mm (48 colunas).

Função pura: recebe a venda, devolve `bytes`. Nenhum acesso a hardware — o que
permite `assert payload == esperado` em teste unitário e revisar o cupom com
`payload.decode("cp850")` durante o desenvolvimento.
"""

from __future__ import annotations

from dataclasses import dataclass

from pdv.config import PrinterConfig
from pdv.domain.models import (
    Payment,
    PaymentMethod,
    Sale,
    SaleItem,
)
from pdv.hardware.printer.escpos import (
    Align,
    EscPosBuilder,
    format_cents,
    format_grams,
)

_METHOD_LABELS: dict[PaymentMethod, str] = {
    PaymentMethod.CASH: "Dinheiro",
    PaymentMethod.DEBIT: "Cartao Debito",
    PaymentMethod.CREDIT: "Cartao Credito",
    PaymentMethod.PIX: "PIX",
    PaymentMethod.PREPAID: "Credito Pre-pago",
    PaymentMethod.CREDIT_ACCOUNT: "Fiado",
    PaymentMethod.CASHBACK: "Cashback",
}


@dataclass(frozen=True, slots=True)
class ReceiptContext:
    """Dados de cabeçalho/rodapé que não pertencem à venda em si."""

    store_name: str
    store_document: str
    store_address: str
    operator_name: str
    terminal_label: str
    customer_name: str | None = None
    cashback_earned_cents: int = 0
    credit_balance_cents: int | None = None
    footer_message: str = "Obrigado pela preferencia!"
    qrcode_data: str | None = None


def build_sale_receipt(
    sale: Sale,
    payments: tuple[Payment, ...],
    context: ReceiptContext,
    config: PrinterConfig,
) -> bytes:
    """Monta o cupom completo, com corte e (se couber) abertura de gaveta."""
    b = EscPosBuilder(columns=config.columns, codepage=config.codepage)
    b.initialize()

    _header(b, context)
    _items(b, sale)
    _totals(b, sale)
    _payments(b, payments)
    _loyalty(b, context)
    _footer(b, sale, context)

    b.feed(1)
    b.cut(feed_lines=config.cut_feed_lines, partial=True)

    # A gaveta abre junto do corte apenas quando houve dinheiro em espécie.
    # Qualquer outra abertura é evento auditado (ver services/audit.py).
    if config.open_drawer_on_cash and any(p.method.opens_drawer for p in payments):
        b.open_drawer()

    return b.build()


def build_drawer_pulse(config: PrinterConfig) -> bytes:
    """Payload mínimo para abrir a gaveta sem imprimir nada.

    Usado em sangria/suprimento — e sempre acompanhado de registro no ledger de
    auditoria com o usuário que autorizou.
    """
    return EscPosBuilder(columns=config.columns).initialize().open_drawer().build()


# --------------------------------------------------------------------------- #
# Blocos do cupom
# --------------------------------------------------------------------------- #


def _header(b: EscPosBuilder, ctx: ReceiptContext) -> None:
    b.align(Align.CENTER).bold(True).size(2, 2)
    b.line(ctx.store_name[: b.columns // 2])
    b.size(1, 1).bold(False)
    b.line(f"CNPJ: {ctx.store_document}")
    b.line(ctx.store_address[: b.columns])
    b.feed(1)
    b.line("*** CUPOM NAO FISCAL ***")
    b.align(Align.LEFT)
    b.separator("=")


def _items(b: EscPosBuilder, sale: Sale) -> None:
    b.bold(True)
    b.columns_3("ITEM", "QTD", "  TOTAL")
    b.bold(False)
    b.separator()

    for index, item in enumerate(sale.items, start=1):
        _item_block(b, index, item)

    b.separator()


def _item_block(b: EscPosBuilder, index: int, item: SaleItem) -> None:
    """Item de venda.

    Produto pesado imprime uma segunda linha com peso líquido × preço/kg. Isso
    não é enfeite: é o que permite o cliente conferir a conta na hora e é a
    primeira defesa contra reclamação de peso no balcão.
    """
    title = f"{index:02d} {item.product_name}"
    b.line(title[: b.columns])

    if item.net_weight_grams:
        detail = (
            f"   {format_grams(item.net_weight_grams)} x "
            f"{format_cents(item.unit_price_cents)}/kg"
        )
        if item.tare_grams:
            detail += f" (tara {item.tare_grams}g)"
    else:
        detail = f"   {item.quantity:g} x {format_cents(item.unit_price_cents)}"

    b.columns_2(detail, format_cents(item.total_cents))


def _totals(b: EscPosBuilder, sale: Sale) -> None:
    b.columns_2("Subtotal", format_cents(sale.subtotal_cents))
    if sale.discount_cents:
        b.columns_2("Desconto", f"-{format_cents(sale.discount_cents)}")

    b.bold(True).size(1, 2)
    b.columns_2("TOTAL", format_cents(sale.total_cents))
    b.size(1, 1).bold(False)
    b.separator("=")


def _payments(b: EscPosBuilder, payments: tuple[Payment, ...]) -> None:
    if not payments:
        return
    for payment in payments:
        label = _METHOD_LABELS.get(payment.method, payment.method.value)
        b.columns_2(label, format_cents(payment.amount_cents))
    change = sum(p.change_cents for p in payments)
    if change:
        b.bold(True)
        b.columns_2("TROCO", format_cents(change))
        b.bold(False)
    b.separator()


def _loyalty(b: EscPosBuilder, ctx: ReceiptContext) -> None:
    if not (ctx.customer_name or ctx.cashback_earned_cents or ctx.credit_balance_cents):
        return
    if ctx.customer_name:
        b.columns_2("Cliente", ctx.customer_name[: b.columns // 2])
    if ctx.cashback_earned_cents:
        b.columns_2("Cashback creditado", format_cents(ctx.cashback_earned_cents))
    if ctx.credit_balance_cents is not None:
        b.columns_2("Saldo pre-pago", format_cents(ctx.credit_balance_cents))
    b.separator()


def _footer(b: EscPosBuilder, sale: Sale, ctx: ReceiptContext) -> None:
    b.line(f"Venda: {sale.local_number:06d}   {ctx.terminal_label}")
    b.line(f"Operador: {ctx.operator_name}")
    b.line(f"Data: {sale.created_at.astimezone().strftime('%d/%m/%Y %H:%M:%S')}")
    # Identificador do documento offline: é por ele que o suporte rastreia a
    # venda no servidor depois da sincronização.
    b.line(f"Doc: {sale.client_uuid}")

    if ctx.qrcode_data:
        b.feed(1).align(Align.CENTER)
        b.qrcode(ctx.qrcode_data)
        b.align(Align.LEFT)

    b.feed(1).align(Align.CENTER)
    b.line(ctx.footer_message)
    b.align(Align.LEFT)
