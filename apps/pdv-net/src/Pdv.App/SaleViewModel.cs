using System.Collections.ObjectModel;
using System.Globalization;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using Pdv.Core.Stock;
using Pdv.Core.Tef;
using Pdv.Data;
using Pdv.Data.Auth;
using Pdv.Data.Sales;

namespace Pdv.App;

public sealed record SaleLine(string Name, string Quantity, long TotalCents)
{
    public string Total => Money.Format(TotalCents);
}

public static class Money
{
    private static readonly CultureInfo Brazil = CultureInfo.GetCultureInfo("pt-BR");

    public static string Format(long cents) => (cents / 100m).ToString("C", Brazil);

    /// <summary>"12,50", "12.50", "R$ 12,50" → 1250. Vazio ou inválido → null.</summary>
    public static long? Parse(string? text)
    {
        var cleaned = (text ?? "").Replace("R$", "", StringComparison.Ordinal).Trim();
        if (cleaned.Length == 0) return null;
        if (cleaned.Contains(',', StringComparison.Ordinal)) cleaned = cleaned.Replace(".", "", StringComparison.Ordinal).Replace(',', '.');
        return decimal.TryParse(cleaned, NumberStyles.Number, CultureInfo.InvariantCulture, out var value) && value >= 0
            ? (long)Math.Round(value * 100m, 0, MidpointRounding.AwayFromZero)
            : null;
    }
}

/// <summary>A venda no balcão: bipar, somar, receber.</summary>
/// <remarks>
/// <para>
/// É também a conversa do TEF (<see cref="ITefInteraction"/>): o que a solução
/// de TEF manda dizer ("Insira o cartão", "Digite a senha") aparece na tela na
/// ordem em que chega — o operador lê para o cliente.
/// </para>
/// <para>
/// Toda regra de dinheiro mora no <see cref="Checkout"/> e no
/// <see cref="ItemRegistration"/>, testados sobre o banco; aqui é só o que a
/// tela mostra e em que ordem.
/// </para>
/// </remarks>
public sealed partial class SaleViewModel : ObservableObject, ITefInteraction
{
    private readonly ItemRegistration _items;
    private readonly Catalog _catalog;
    private readonly Checkout _checkout;
    private readonly Identity _operator;

    private readonly Func<CancellationToken, Task<IReadOnlyList<TefRecovery>>>? _recoverPending;

    /// <param name="recoverPending">
    /// Resolve as pendências do TEF (confirma a venda gravada, desfaz a que se
    /// perdeu). Roda em <see cref="StartCommand"/>, antes da primeira venda.
    /// </param>
    public SaleViewModel(
        ItemRegistration items, Catalog catalog, Checkout checkout, Identity operatorIdentity,
        Func<CancellationToken, Task<IReadOnlyList<TefRecovery>>>? recoverPending = null)
    {
        _items = items;
        _catalog = catalog;
        _checkout = checkout;
        _operator = operatorIdentity;
        _recoverPending = recoverPending;
        Greeting = $"Olá, {operatorIdentity.FirstName}";
    }

    /// <summary>O que a abertura do caixa resolveu no TEF. O operador precisa ler: pode ter cliente esperando estorno.</summary>
    public ObservableCollection<string> Recoveries { get; } = [];

    /// <summary>
    /// Abertura do caixa: resolve as pendências do TEF de uma queda anterior
    /// antes da primeira venda. Sem isso o primeiro cartão do dia seria recusado
    /// pela trava de pendência — ou pior, o cliente de ontem seguiria cobrado.
    /// </summary>
    [RelayCommand]
    private async Task StartAsync(CancellationToken cancellationToken)
    {
        if (_recoverPending is null) return;
        Recoveries.Clear();
        foreach (var recovery in await _recoverPending(cancellationToken))
        {
            Recoveries.Add(recovery.Message);
        }
    }

    public string Greeting { get; }

    public ObservableCollection<SaleLine> Lines { get; } = [];

    public ObservableCollection<Product> Results { get; } = [];

    public ObservableCollection<string> TefMessages { get; } = [];

    public string? OrderId { get; private set; }

    [ObservableProperty]
    public partial string Query { get; set; } = "";

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(Total))]
    [NotifyCanExecuteChangedFor(nameof(PayCashCommand), nameof(PayCardCommand))]
    public partial long TotalCents { get; set; }

    public string Total => Money.Format(TotalCents);

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(Change))]
    public partial string CashReceived { get; set; } = "";

    /// <summary>O troco, conforme o operador digita o que recebeu.</summary>
    public string? Change =>
        Money.Parse(CashReceived) is { } received && received >= TotalCents && TotalCents > 0
            ? "Troco: " + Money.Format(received - TotalCents)
            : null;

    [ObservableProperty]
    public partial string? Notice { get; set; }

    [ObservableProperty]
    public partial string? Error { get; set; }

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(PayCashCommand), nameof(PayCardCommand))]
    public partial bool IsPaying { get; set; }

    // -- itens ---------------------------------------------------------------

    /// <summary>Enter no campo: código de barras exato entra direto; senão, busca por nome.</summary>
    [RelayCommand]
    private void Scan()
    {
        Error = null;
        var text = Query.Trim();
        if (text.Length == 0) return;

        if (_catalog.FindByBarcode(text) is { } product)
        {
            Add(product);
            Query = "";
            Results.Clear();
            return;
        }

        Results.Clear();
        foreach (var found in _catalog.Search(text)) Results.Add(found);
        if (Results.Count == 0) Error = $"Nenhum produto encontrado para \"{text}\".";
    }

    [RelayCommand]
    private void Add(Product product)
    {
        Error = null;
        Notice = null;
        try
        {
            var result = _items.RegisterUnitItem(OrderId, product, 1m, _operator.Id);
            OrderId = result.Order.Id;
            Lines.Add(new SaleLine(result.Item.ProductName, Quantity(result.Item.Quantity), result.Item.TotalCents));
            TotalCents = result.Order.TotalCents;
            if (result.StockWarnings.Count > 0) Notice = "Estoque baixo: " + string.Join("; ", result.StockWarnings);
        }
        catch (InvalidQuantityException error)
        {
            Error = error.Message;
        }
        catch (InsufficientStockException error)
        {
            Error = error.Message;
        }
    }

    private static string Quantity(decimal quantity) => quantity.ToString("0.###", CultureInfo.GetCultureInfo("pt-BR"));

    // -- pagamento -----------------------------------------------------------

    private bool CanPay() => !IsPaying && TotalCents > 0 && OrderId is not null;

    [RelayCommand(CanExecute = nameof(CanPay))]
    private Task PayCashAsync()
    {
        var received = Money.Parse(CashReceived) ?? TotalCents;
        return PayAsync([PaymentIntent.Cash(received)]);
    }

    [RelayCommand(CanExecute = nameof(CanPay))]
    private Task PayCardAsync(TefCardType card) => PayAsync([PaymentIntent.Card(card, TotalCents)]);

    private async Task PayAsync(IReadOnlyList<PaymentIntent> intents)
    {
        IsPaying = true;
        Error = null;
        Notice = null;
        TefMessages.Clear();
        try
        {
            var result = await _checkout.CloseAsync(OrderId!, _operator.Id, intents, this);
            switch (result)
            {
                case CheckoutResult.Closed closed:
                    var change = closed.Payments.Sum(payment => payment.ChangeCents);
                    Notice = change > 0
                        ? $"Venda finalizada. Troco: {Money.Format(change)}"
                        : "Venda finalizada.";
                    if (!closed.AllConfirmed)
                    {
                        Notice += " O cartão será confirmado quando o TEF voltar a responder.";
                    }
                    StartNewSale();
                    break;
                case CheckoutResult.CardRefused refused:
                    Error = refused.Reason;
                    break;
            }
        }
        catch (InsufficientPaymentException error)
        {
            Error = error.Message;
        }
        catch (TefCommunicationException error)
        {
            Error = error.Message;
        }
        catch (TefPendingException error)
        {
            Error = error.Message;
        }
        finally
        {
            IsPaying = false;
        }
    }

    private void StartNewSale()
    {
        OrderId = null;
        Lines.Clear();
        TotalCents = 0;
        CashReceived = "";
        Query = "";
        Results.Clear();
    }

    // -- a conversa do TEF ---------------------------------------------------

    /// <summary>Para perguntas do TEF (parcelas, crédito ou débito). A casca liga a um diálogo.</summary>
    public Func<string, IReadOnlyList<string>, Task<int?>> AskChoice { get; set; } = (_, _) => Task.FromResult<int?>(0);

    public Func<string, Task<string?>> AskText { get; set; } = _ => Task.FromResult<string?>(null);

    void ITefInteraction.Show(string message) => TefMessages.Add(message);

    Task<int?> ITefInteraction.ChooseAsync(string title, IReadOnlyList<string> options, CancellationToken cancellationToken) =>
        AskChoice(title, options);

    Task<string?> ITefInteraction.AskAsync(string prompt, CancellationToken cancellationToken) => AskText(prompt);
}
