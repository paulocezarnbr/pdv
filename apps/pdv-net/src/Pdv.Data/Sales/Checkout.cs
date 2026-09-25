using Pdv.Core.Tef;

namespace Pdv.Data.Sales;

public abstract record CheckoutResult
{
    private CheckoutResult() { }

    /// <summary>Venda gravada. <paramref name="AllConfirmed"/> falso: a confirmação ficou para a recuperação.</summary>
    public sealed record Closed(IReadOnlyList<Payment> Payments, IReadOnlyList<TefApproval> Approvals, bool AllConfirmed)
        : CheckoutResult
    {
        /// <summary>O cashback que esta venda gerou, para o cupom.</summary>
        public Customers.CashbackCredit? Cashback { get; init; }

        /// <summary>O saldo pré-pago depois da venda, quando ele foi usado.</summary>
        public long? PrepaidBalanceCents { get; init; }
    }

    /// <summary>Um cartão foi negado ou cancelado; os já aprovados desta venda foram desfeitos.</summary>
    public sealed record CardRefused(string Reason) : CheckoutResult;
}

/// <summary>O fechamento da venda no balcão, com dinheiro e cartão.</summary>
/// <remarks>
/// <para>A ordem é a que protege o cliente e a loja:</para>
/// <list type="number">
///   <item>valida a quitação — dividir errado não cobra ninguém;</item>
///   <item>lê os cartões, fora da transação (o diário do TEF grava sozinho);</item>
///   <item>grava pedido, pagamentos (com NSU) e auditoria numa transação só;</item>
///   <item>confirma no TEF.</item>
/// </list>
/// <para>
/// Qualquer falha antes do passo 4 desfaz os cartões já aprovados desta venda.
/// Uma queda no meio é resolvida na próxima abertura pelo
/// <see cref="TefCoordinator.RecoverPendingAsync"/>, que pergunta ao banco
/// (<see cref="SaleRepository.WasRecorded"/>) se a venda existe.
/// </para>
/// </remarks>
public sealed class Checkout(
    PdvDatabase database, TerminalIdentity terminal, AuditLedger ledger, TefCoordinator tef, TimeProvider? clock = null,
    Customers.CustomerLedgers? customers = null)
{
    private readonly SaleRepository _sales = new(terminal, clock);

    /// <param name="customerId">
    /// O cliente identificado (telefone). Com ele a venda gera cashback, e pode
    /// ser paga com pré-pago ou fiado — os dois consumidos na transação da venda.
    /// </param>
    public async Task<CheckoutResult> CloseAsync(
        string orderId, string operatorId, IReadOnlyList<PaymentIntent> intents, ITefInteraction ui,
        CancellationToken cancellationToken = default, string? customerId = null)
    {
        var order = SaleRepository.LoadOpenOrder(database.Connection, orderId);
        var change = PaymentSettlement.Change(intents, order.TotalCents);
        // Antes de qualquer cartão: no Python a falta de cliente só aparecia
        // dentro da transação, com o cartão já aprovado e por desfazer.
        if (intents.FirstOrDefault(intent => intent.NeedsCustomer) is { } needs && (customerId is null || customers is null))
        {
            throw new InsufficientPaymentException(order.TotalCents, 0, needs.Method == PaymentMethods.Prepaid
                ? "Crédito pré-pago exige cliente identificado."
                : "Fiado exige cliente identificado.");
        }

        var approvals = new List<TefApproval>();
        foreach (var intent in intents.Where(intent => intent.IsCard))
        {
            TefOutcome outcome;
            try
            {
                outcome = await tef.AuthorizeAsync(
                    orderId, intent.AmountCents, intent.CardType!.Value, ui, intent.Installments, cancellationToken);
            }
            catch
            {
                await UndoAllAsync(approvals, "outro cartão da venda falhou");
                throw;
            }

            if (outcome is TefOutcome.Approved approved)
            {
                approvals.Add(approved.Approval);
                continue;
            }

            await UndoAllAsync(approvals, "outro cartão da venda foi recusado");
            return new CheckoutResult.CardRefused(outcome switch
            {
                TefOutcome.Declined declined => $"Cartão negado: {declined.Reason}",
                TefOutcome.Aborted aborted => $"Pagamento cancelado: {aborted.Reason}",
                _ => "Cartão não aprovado",
            });
        }

        var payments = BuildPayments(intents, approvals, change);
        Customers.CashbackCredit? cashback = null;
        long? prepaidBalance = null;
        try
        {
            database.InTransaction(transaction =>
            {
                _sales.CloseOrder(transaction, order);

                // Na mesma transação que fecha o pedido: sem saldo ou sem
                // limite, nada da venda fica gravado (e os cartões são desfeitos).
                var prepaid = intents.Where(intent => intent.Method == PaymentMethods.Prepaid).Sum(intent => intent.AmountCents);
                if (prepaid > 0)
                {
                    prepaidBalance = customers!.RedeemPrepaidWithin(transaction, customerId!, orderId, prepaid, operatorId);
                }
                var credit = intents.Where(intent => intent.Method == PaymentMethods.CreditAccount).Sum(intent => intent.AmountCents);
                if (credit > 0)
                {
                    customers!.ChargeWithin(transaction, customerId!, orderId, credit, operatorId);
                }

                _sales.RecordPayments(transaction, orderId, payments);

                if (customerId is not null && customers is not null)
                {
                    cashback = customers.EarnWithin(transaction, customerId, orderId, order.TotalCents, operatorId);
                    if (cashback is not null)
                    {
                        ledger.Append(transaction, "cashback_credited", operatorId, new Dictionary<string, object?>
                        {
                            ["order_id"] = orderId,
                            ["customer_id"] = customerId,
                            ["amount_cents"] = cashback.AmountCents,
                            ["expires_at"] = cashback.ExpiresAt,
                        });
                    }
                }

                ledger.Append(transaction, "sale_closed", operatorId, new Dictionary<string, object?>
                {
                    ["order_id"] = orderId,
                    ["local_number"] = order.LocalNumber,
                    ["items"] = CountItems(transaction, orderId),
                    ["subtotal_cents"] = order.SubtotalCents,
                    ["discount_cents"] = order.DiscountCents,
                    ["total_cents"] = order.TotalCents,
                    ["payments"] = payments.Select(payment =>
                    {
                        var entry = new Dictionary<string, object?>
                        {
                            ["method"] = payment.Method,
                            ["amount_cents"] = payment.AmountCents,
                        };
                        if (payment.Nsu is not null) entry["nsu"] = payment.Nsu;
                        return entry;
                    }).ToList(),
                });
            });
        }
        catch
        {
            await UndoAllAsync(approvals, "falha ao gravar a venda");
            throw;
        }

        var allConfirmed = true;
        foreach (var approval in approvals)
        {
            allConfirmed &= await tef.ConfirmAsync(approval, cancellationToken);
        }
        return new CheckoutResult.Closed(payments, approvals, allConfirmed)
        {
            Cashback = cashback,
            PrepaidBalanceCents = prepaidBalance,
        };
    }

    private static List<Payment> BuildPayments(
        IReadOnlyList<PaymentIntent> intents, IReadOnlyList<TefApproval> approvals, long change)
    {
        var payments = new List<Payment>(intents.Count);
        var nextApproval = 0;
        var changeGiven = false;
        foreach (var intent in intents)
        {
            if (intent.IsCard)
            {
                var approval = approvals[nextApproval++];
                payments.Add(new Payment(intent.Method, intent.AmountCents, 0, approval.Nsu, approval.TransactionId));
                continue;
            }

            // O troco vai para a primeira linha em dinheiro, como no Python.
            var lineChange = !changeGiven && intent.Method == PaymentMethods.Cash ? change : 0;
            changeGiven |= intent.Method == PaymentMethods.Cash;
            payments.Add(new Payment(intent.Method, intent.AmountCents, lineChange, null, Pdv.Core.Iso.NewId()));
        }
        return payments;
    }

    private static long CountItems(Microsoft.Data.Sqlite.SqliteTransaction transaction, string orderId)
    {
        using var command = transaction.Command(
            "SELECT COUNT(*) FROM order_items WHERE order_id = $order AND canceled_at IS NULL", ("$order", orderId));
        return Convert.ToInt64(command.ExecuteScalar());
    }

    /// <summary>Desfaz os cartões aprovados desta venda. Sem cancelamento: a venda não existe.</summary>
    private async Task UndoAllAsync(IEnumerable<TefApproval> approvals, string reason)
    {
        foreach (var approval in approvals)
        {
            // CancellationToken.None: desistir do desfazimento no meio deixaria o
            // cliente cobrado por uma venda que não existe.
            await tef.UndoAsync(approval, reason, CancellationToken.None);
        }
    }
}
