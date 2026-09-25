using Pdv.Core.Tef;

namespace Pdv.Data.Sales;

/// <summary>Os valores de <c>payments.method</c> — os mesmos do Python e da nuvem.</summary>
public static class PaymentMethods
{
    public const string Cash = "cash";
    public const string Debit = "debit";
    public const string Credit = "credit";
    public const string Pix = "pix";

    /// <summary>Crédito pré-pago do cliente (carga feita antes).</summary>
    public const string Prepaid = "prepaid";

    /// <summary>Fiado: a venda vai para a conta do cliente, com vencimento.</summary>
    public const string CreditAccount = "credit_account";

    public static string ForCard(TefCardType card) => card switch
    {
        TefCardType.Debit => Debit,
        TefCardType.Credit => Credit,
        TefCardType.Pix => Pix,
        // A nuvem ainda não tem "voucher" em payments.method: gravar outro
        // nome faria o lote inteiro ser recusado na sincronização.
        _ => throw new ArgumentException($"Forma de pagamento {card} ainda não é aceita pela retaguarda."),
    };
}

/// <summary>O que o operador escolheu: dinheiro ou cartão (pelo TEF).</summary>
public sealed record PaymentIntent(string Method, long AmountCents, TefCardType? CardType = null, int Installments = 1)
{
    public static PaymentIntent Cash(long amountCents) => new(PaymentMethods.Cash, amountCents);

    public static PaymentIntent Card(TefCardType card, long amountCents, int installments = 1) =>
        new(PaymentMethods.ForCard(card), amountCents, card, installments);

    public static PaymentIntent Prepaid(long amountCents) => new(PaymentMethods.Prepaid, amountCents);

    public static PaymentIntent CreditAccount(long amountCents) => new(PaymentMethods.CreditAccount, amountCents);

    public bool IsCard => CardType is not null;

    /// <summary>Pré-pago e fiado só existem com cliente identificado.</summary>
    public bool NeedsCustomer => Method is PaymentMethods.Prepaid or PaymentMethods.CreditAccount;
}

/// <summary>Uma linha de <c>payments</c>.</summary>
public sealed record Payment(string Method, long AmountCents, long ChangeCents, string? Nsu, string ClientUuid);

public sealed class InsufficientPaymentException(long totalCents, long paidCents, string message)
    : Exception(message)
{
    public long TotalCents { get; } = totalCents;

    public long PaidCents { get; } = paidCents;
}

/// <summary>A quitação: o <c>settle_payments</c> do Python, com uma regra a mais.</summary>
public static class PaymentSettlement
{
    /// <summary>Valida a quitação e devolve o troco, que só existe em espécie.</summary>
    /// <remarks>
    /// <para>As regras do Python:</para>
    /// <list type="bullet">
    ///   <item>pagamento insuficiente não fecha venda;</item>
    ///   <item>troco só em dinheiro: sobra em cartão é valor digitado errado.</item>
    /// </list>
    /// <para>
    /// A regra a mais: o troco não passa do que entrou em dinheiro. No Python,
    /// R$ 15 no cartão + R$ 5 em dinheiro numa venda de R$ 10 devolvia R$ 10 em
    /// espécie — o golpe do troco pela porta do pagamento misto.
    /// </para>
    /// <para>Roda ANTES de qualquer cartão ser lido: dividir errado não cobra ninguém.</para>
    /// </remarks>
    /// <returns>O troco, em centavos.</returns>
    public static long Change(IReadOnlyList<PaymentIntent> intents, long totalCents)
    {
        if (intents.Count == 0)
        {
            throw new InsufficientPaymentException(totalCents, 0, "Nenhuma forma de pagamento informada.");
        }
        if (intents.Any(intent => intent.AmountCents <= 0))
        {
            throw new InsufficientPaymentException(totalCents, 0, "Toda forma de pagamento precisa de valor positivo.");
        }

        var paid = intents.Sum(intent => intent.AmountCents);
        if (paid < totalCents)
        {
            throw new InsufficientPaymentException(totalCents, paid,
                $"Faltam {Money(totalCents - paid)} para fechar a venda.");
        }

        var electronic = intents.Where(intent => intent.Method != PaymentMethods.Cash).Sum(intent => intent.AmountCents);
        if (electronic > totalCents)
        {
            throw new InsufficientPaymentException(totalCents, paid,
                $"Pagamento eletrônico excede o total em {Money(electronic - totalCents)}. " +
                "Corrija o valor: não há troco para cartão ou PIX.");
        }
        return paid - totalCents;
    }

    private static string Money(long cents) =>
        (cents / 100m).ToString("C", System.Globalization.CultureInfo.GetCultureInfo("pt-BR"));
}
