namespace Pdv.Core.Printing;

/// <summary>Um item como o cupom mostra. A quantidade vai como o texto gravado ("1", "1.5").</summary>
public sealed record ReceiptItem(
    string ProductName, long TotalCents, long UnitPriceCents, string Quantity, long NetWeightGrams = 0, long TareGrams = 0);

public sealed record ReceiptPayment(string Method, long AmountCents, long ChangeCents = 0);

/// <summary>A venda para o cupom: o que está no banco, depois de fechada.</summary>
public sealed record ReceiptSale(
    long LocalNumber, string DocumentId, IReadOnlyList<ReceiptItem> Items, long DiscountCents, DateTime PrintedLocal)
{
    public long SubtotalCents => Items.Sum(item => item.TotalCents);

    public long TotalCents => Math.Max(0, SubtotalCents - DiscountCents);
}

/// <summary>Cabeçalho e rodapé: o que não pertence à venda.</summary>
public sealed record ReceiptContext(
    string StoreName,
    string StoreDocument,
    string StoreAddress,
    string OperatorName,
    string TerminalLabel,
    string? CustomerName = null,
    long CashbackEarnedCents = 0,
    long? CreditBalanceCents = null,
    string FooterMessage = "Obrigado pela preferencia!",
    string? QrCodeData = null);

public sealed record PrinterLayout(int Columns = 48, byte Codepage = 2, int CutFeedLines = 4, bool OpenDrawerOnCash = true);

/// <summary>O cupom não fiscal em 80 mm — o <c>layout.py</c> do Python, byte a byte.</summary>
/// <remarks>
/// Função pura: venda entra, bytes saem. A gaveta abre junto do corte só
/// quando houve dinheiro. Abrir em venda no cartão é o convite ao furto.
/// </remarks>
public static class ReceiptLayout
{
    /// <summary>Os rótulos do Python — o DANFE usa os mesmos, e o cliente não pode ler dois nomes.</summary>
    public static readonly IReadOnlyDictionary<string, string> MethodLabels = new Dictionary<string, string>(StringComparer.Ordinal)
    {
        ["cash"] = "Dinheiro",
        ["debit"] = "Cartao Debito",
        ["credit"] = "Cartao Credito",
        ["pix"] = "PIX",
        ["prepaid"] = "Credito Pre-pago",
        ["credit_account"] = "Fiado",
        ["cashback"] = "Cashback",
    };

    public static byte[] BuildSaleReceipt(
        ReceiptSale sale, IReadOnlyList<ReceiptPayment> payments, ReceiptContext context, PrinterLayout layout)
    {
        var b = new EscPosBuilder(layout.Columns, layout.Codepage).Initialize();
        Header(b, context);
        Items(b, sale);
        Totals(b, sale);
        Payments(b, payments);
        Loyalty(b, context);
        Footer(b, sale, context);
        b.Feed(1);
        b.Cut(layout.CutFeedLines, partial: true);
        if (layout.OpenDrawerOnCash && payments.Any(payment => payment.Method == "cash")) b.OpenDrawer();
        return b.Build();
    }

    /// <summary>Só o pulso da gaveta (sangria e suprimento, sempre com auditoria).</summary>
    public static byte[] BuildDrawerPulse(PrinterLayout layout) =>
        new EscPosBuilder(layout.Columns).Initialize().OpenDrawer().Build();

    private static void Header(EscPosBuilder b, ReceiptContext context)
    {
        b.AlignTo(Align.Center).Bold(true).Size(2, 2);
        b.Line(EscPosBuilder.Slice(context.StoreName, b.Columns / 2));
        b.Size(1, 1).Bold(false);
        b.Line($"CNPJ: {context.StoreDocument}");
        b.Line(EscPosBuilder.Slice(context.StoreAddress, b.Columns));
        b.Feed(1);
        b.Line("*** CUPOM NAO FISCAL ***");
        b.AlignTo(Align.Left);
        b.Separator('=');
    }

    private static void Items(EscPosBuilder b, ReceiptSale sale)
    {
        b.Bold(true);
        b.Columns3("ITEM", "QTD", "  TOTAL");
        b.Bold(false);
        b.Separator();
        for (var index = 0; index < sale.Items.Count; index++) Item(b, index + 1, sale.Items[index]);
        b.Separator();
    }

    /// <summary>O pesado imprime peso líquido × preço/kg: o cliente confere a conta na hora.</summary>
    private static void Item(EscPosBuilder b, int index, ReceiptItem item)
    {
        b.Line(EscPosBuilder.Slice($"{index:00} {item.ProductName}", b.Columns));
        string detail;
        if (item.NetWeightGrams != 0)
        {
            detail = $"   {EscPosBuilder.FormatGrams(item.NetWeightGrams)} x {EscPosBuilder.FormatCents(item.UnitPriceCents)}/kg";
            if (item.TareGrams != 0) detail += $" (tara {item.TareGrams}g)";
        }
        else
        {
            detail = $"   {item.Quantity} x {EscPosBuilder.FormatCents(item.UnitPriceCents)}";
        }
        b.Columns2(detail, EscPosBuilder.FormatCents(item.TotalCents));
    }

    private static void Totals(EscPosBuilder b, ReceiptSale sale)
    {
        b.Columns2("Subtotal", EscPosBuilder.FormatCents(sale.SubtotalCents));
        if (sale.DiscountCents != 0) b.Columns2("Desconto", "-" + EscPosBuilder.FormatCents(sale.DiscountCents));
        b.Bold(true).Size(1, 2);
        b.Columns2("TOTAL", EscPosBuilder.FormatCents(sale.TotalCents));
        b.Size(1, 1).Bold(false);
        b.Separator('=');
    }

    private static void Payments(EscPosBuilder b, IReadOnlyList<ReceiptPayment> payments)
    {
        if (payments.Count == 0) return;
        foreach (var payment in payments)
        {
            b.Columns2(MethodLabels.GetValueOrDefault(payment.Method, payment.Method), EscPosBuilder.FormatCents(payment.AmountCents));
        }
        var change = payments.Sum(payment => payment.ChangeCents);
        if (change != 0)
        {
            b.Bold(true);
            b.Columns2("TROCO", EscPosBuilder.FormatCents(change));
            b.Bold(false);
        }
        b.Separator();
    }

    private static void Loyalty(EscPosBuilder b, ReceiptContext context)
    {
        if (string.IsNullOrEmpty(context.CustomerName) && context.CashbackEarnedCents == 0 && context.CreditBalanceCents is null or 0)
        {
            return;
        }
        if (!string.IsNullOrEmpty(context.CustomerName)) b.Columns2("Cliente", EscPosBuilder.Slice(context.CustomerName, b.Columns / 2));
        if (context.CashbackEarnedCents != 0) b.Columns2("Cashback creditado", EscPosBuilder.FormatCents(context.CashbackEarnedCents));
        if (context.CreditBalanceCents is { } balance) b.Columns2("Saldo pre-pago", EscPosBuilder.FormatCents(balance));
        b.Separator();
    }

    private static void Footer(EscPosBuilder b, ReceiptSale sale, ReceiptContext context)
    {
        b.Line($"Venda: {sale.LocalNumber:000000}   {context.TerminalLabel}");
        b.Line($"Operador: {context.OperatorName}");
        b.Line($"Data: {sale.PrintedLocal.ToString("dd/MM/yyyy HH:mm:ss", System.Globalization.CultureInfo.InvariantCulture)}");
        // Identificador do documento offline: é por ele que o suporte acha a venda no servidor.
        b.Line($"Doc: {sale.DocumentId}");
        if (!string.IsNullOrEmpty(context.QrCodeData))
        {
            b.Feed(1).AlignTo(Align.Center);
            b.QrCode(context.QrCodeData);
            b.AlignTo(Align.Left);
        }
        b.Feed(1).AlignTo(Align.Center);
        b.Line(context.FooterMessage);
        b.AlignTo(Align.Left);
    }
}
