using System.Collections.ObjectModel;
using System.Globalization;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using Pdv.Core.Scale;
using Pdv.Core.Stock;
using Pdv.Core.Tef;
using Pdv.Data;
using Pdv.Data.Auth;
using Pdv.Data.Remote;
using Pdv.Data.Sales;

namespace Pdv.App;

public sealed record SaleLine(string ItemId, string Name, string Quantity, long TotalCents)
{
    public string Total => Money.Format(TotalCents);
}

/// <summary>O que o diálogo de autorização precisa: o que está sendo liberado, quem pode, e a conferência do PIN.</summary>
/// <param name="Authorize">
/// Confere login e PIN (com o freio). Lança <see cref="AuthenticationException"/>
/// com a mensagem para mostrar; o diálogo continua aberto para outra tentativa.
/// </param>
public sealed record AuthorizationRequest(string Operation, IReadOnlyList<string> Logins, Func<string, string, Identity> Authorize);

/// <summary>Um pedido do painel parado no caixa: o que ele pede, quem pode decidir, e as duas decisões.</summary>
/// <param name="Accept">Login e PIN → a mensagem do que foi feito.</param>
/// <param name="Decline">Login, PIN e motivo. O motivo volta ao painel.</param>
/// <remarks>
/// Erro de credencial ou papel (<see cref="AuthenticationException"/>,
/// <see cref="ConfirmationException"/>) não decide nada: o diálogo mostra e
/// continua aberto. Uma trava que recusa (<see cref="CommandRefusedException"/>)
/// decide, e o diálogo fecha.
/// </remarks>
public sealed record RemoteDecisionRequest(
    string Note, IReadOnlyList<string> Logins, Func<string, string, string> Accept, Action<string, string, string> Decline);

public static class Money
{
    private static readonly CultureInfo Brazil = CultureInfo.GetCultureInfo("pt-BR");

    public static string Format(long cents) => (cents / 100m).ToString("C", Brazil);

    /// <summary>"10", "12,5", "12.5", "7,5%" → percentual. Vazio ou inválido → null.</summary>
    public static decimal? ParsePercent(string? text)
    {
        var cleaned = (text ?? "").Replace("%", "", StringComparison.Ordinal).Trim().Replace(',', '.');
        return decimal.TryParse(cleaned, NumberStyles.Number, CultureInfo.InvariantCulture, out var value) ? value : null;
    }

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
    private readonly SaleAdjustments? _adjustments;
    private readonly StaffAuthentication? _authorization;
    private readonly Func<ScaleReading?>? _stableWeight;
    private readonly RemoteCommandService? _remote;
    private readonly CashSessionService? _cash;

    /// <param name="recoverPending">
    /// Resolve as pendências do TEF (confirma a venda gravada, desfaz a que se
    /// perdeu). Roda em <see cref="StartCommand"/>, antes da primeira venda.
    /// </param>
    /// <param name="adjustments">Cancelamento e desconto. Sem ele, F4 e F6 ficam desligados.</param>
    /// <param name="authorization">Quem confere o PIN do gerente no diálogo.</param>
    /// <param name="stableWeight">A última leitura estável da balança, ou <c>null</c> se o peso mudou.</param>
    public SaleViewModel(
        ItemRegistration items, Catalog catalog, Checkout checkout, Identity operatorIdentity,
        Func<CancellationToken, Task<IReadOnlyList<TefRecovery>>>? recoverPending = null,
        SaleAdjustments? adjustments = null, StaffAuthentication? authorization = null,
        Func<ScaleReading?>? stableWeight = null, RemoteCommandService? remote = null,
        CashSessionService? cashSessions = null)
    {
        _items = items;
        _catalog = catalog;
        _checkout = checkout;
        _operator = operatorIdentity;
        _recoverPending = recoverPending;
        _adjustments = adjustments;
        _authorization = authorization;
        _stableWeight = stableWeight;
        _remote = remote;
        _cash = cashSessions;
        if (remote is not null) remote.OrderChanged += ReloadIfOpen;
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
    [NotifyCanExecuteChangedFor(nameof(PayCashCommand), nameof(PayCardCommand), nameof(DiscountCommand))]
    public partial long TotalCents { get; set; }

    public string Total => Money.Format(TotalCents);

    public long SubtotalCents { get; private set; }

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(Discount))]
    public partial long DiscountCents { get; set; }

    /// <summary>"Desconto: −R$ 4,23", só quando há desconto.</summary>
    public string? Discount => DiscountCents > 0
        ? $"Subtotal {Money.Format(SubtotalCents)} · desconto −{Money.Format(DiscountCents)}"
        : null;

    /// <summary>O item marcado na lista, alvo do F4.</summary>
    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(CancelItemCommand))]
    public partial SaleLine? SelectedLine { get; set; }

    // -- balança -------------------------------------------------------------

    /// <summary>O mostrador da balança: peso ao vivo, ou o que ela está dizendo.</summary>
    [ObservableProperty]
    public partial string ScaleText { get; set; } = "Balança: aguardando";

    /// <summary>Peso estável e ainda no prato: o que o próximo item pesado vai cobrar.</summary>
    [ObservableProperty]
    public partial bool ScaleStable { get; set; }

    /// <summary>Toda leitura da balança — a casca chama da thread da tela.</summary>
    public void ShowReading(ScaleReading reading)
    {
        ScaleText = reading.Status switch
        {
            ScaleStatus.Stable or ScaleStatus.Zero => "Balança: " + WeightPricing.Kilos(reading.WeightGrams),
            ScaleStatus.Unstable => "Balança: estabilizando…",
            ScaleStatus.Overload => "Balança: sobrecarga",
            ScaleStatus.Negative => "Balança: peso negativo — tare a balança",
            _ => "Balança: erro de leitura",
        };
        ScaleStable = _stableWeight?.Invoke() is { WeightGrams: > 0 };
    }

    public void ShowScaleError(string message)
    {
        ScaleText = "Balança: " + message;
        ScaleStable = false;
    }

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
    [NotifyCanExecuteChangedFor(nameof(PayCashCommand), nameof(PayCardCommand), nameof(CancelItemCommand), nameof(DiscountCommand),
        nameof(CloseCashCommand), nameof(ReviewRemoteCommand))]
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

    /// <summary>Lança o produto: por unidade direto; por peso, com a leitura estável da balança.</summary>
    [RelayCommand]
    private void Add(Product product)
    {
        Error = null;
        Notice = null;
        try
        {
            ItemResult result;
            if (product.IsWeighed)
            {
                if (_stableWeight is null)
                {
                    Error = "Este caixa não tem balança configurada.";
                    return;
                }
                if (_stableWeight() is not { } reading)
                {
                    Error = "Aguarde a balança estabilizar antes de registrar o item.";
                    return;
                }
                result = _items.RegisterWeighedItem(OrderId, product, reading, _operator.Id);
            }
            else
            {
                result = _items.RegisterUnitItem(OrderId, product, 1m, _operator.Id);
            }

            OrderId = result.Order.Id;
            var item = result.Item;
            Lines.Add(new SaleLine(
                item.Id, item.ProductName,
                item.IsWeighed ? WeightPricing.Kilos(item.NetWeightGrams) : Quantity(item.Quantity),
                item.TotalCents));
            ApplyTotals(result.Order);
            if (result.StockWarnings.Count > 0) Notice = "Estoque baixo: " + string.Join("; ", result.StockWarnings);
        }
        catch (Exception error) when (error is InvalidQuantityException or InsufficientStockException
                                          or UnstableWeightException or RecipeNotFoundException)
        {
            Error = error.Message;
        }
    }

    // -- fechamento do caixa -------------------------------------------------

    /// <summary>
    /// O caixa foi fechado. A casca mostra o resultado e volta ao login: uma
    /// sessão encerrada não recebe mais venda.
    /// </summary>
    public event EventHandler<CashReconciliation>? CashClosed;

    private bool CanCloseCash() => !IsPaying && _cash is not null && _authorization is not null;

    /// <summary>
    /// F12: fechamento cego. Primeiro a contagem, depois o PIN de quem libera,
    /// e só então o esperado aparece — ninguém conta "até chegar lá".
    /// </summary>
    [RelayCommand(CanExecute = nameof(CanCloseCash))]
    private async Task CloseCashAsync()
    {
        Error = null;
        Notice = null;
        if (Lines.Count > 0)
        {
            // Regra a mais que o Python: a venda aberta ficaria órfã na sessão seguinte.
            Error = "Finalize ou cancele a venda aberta antes de fechar o caixa.";
            return;
        }
        if (_cash!.Current() is null)
        {
            Error = "Não há caixa aberto.";
            return;
        }

        var typed = await AskText("Fechamento cego: conte o dinheiro da gaveta e informe o total (R$)");
        if (typed is null) return;
        var declared = string.IsNullOrWhiteSpace(typed) ? 0 : Money.Parse(typed);
        if (declared is null)
        {
            Error = $"\"{typed.Trim()}\" não é um valor.";
            return;
        }

        var authorizer = await AskAuthorizer(new AuthorizationRequest(
            "Autorizar o fechamento cego desta sessão de caixa.",
            _authorization!.ListAuthorizers(),
            _authorization.Authorize));
        if (authorizer is null) return;

        try
        {
            var result = _cash.Close(declared.Value, _operator.Id, authorizer.Id);
            CashClosed?.Invoke(this, result);
        }
        catch (CashSessionException error)
        {
            Error = error.Message;
        }
    }

    /// <summary>O que a tela mostra no fechamento: declarado, esperado e a divergência com sinal.</summary>
    public static string Describe(CashReconciliation result)
    {
        var sign = result.DifferenceCents > 0 ? "+" : result.DifferenceCents < 0 ? "−" : "";
        return $"Declarado: {Money.Format(result.DeclaredCents)}\n" +
               $"Esperado: {Money.Format(result.ExpectedCents)}\n" +
               $"Divergência: {sign}{Money.Format(Math.Abs(result.DifferenceCents))}";
    }

    // -- pedidos do painel ---------------------------------------------------

    /// <summary>Pedidos do painel esperando alguém no caixa decidir.</summary>
    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(RemoteNotice))]
    [NotifyCanExecuteChangedFor(nameof(ReviewRemoteCommand))]
    public partial long RemoteAwaiting { get; set; }

    public string? RemoteNotice => RemoteAwaiting switch
    {
        0 => null,
        1 => "1 pedido do painel espera o seu aceite.",
        _ => $"{RemoteAwaiting} pedidos do painel esperam o seu aceite.",
    };

    /// <summary>O diálogo de aceite. Devolve a mensagem do que foi decidido, ou <c>null</c> se fechou sem decidir.</summary>
    public Func<RemoteDecisionRequest, Task<string?>> AskRemoteDecision { get; set; } = _ => Task.FromResult<string?>(null);

    /// <summary>Relê a fila de pedidos do painel. A casca chama depois de cada ciclo de sincronização.</summary>
    public void RefreshRemote() => RemoteAwaiting = _remote?.Inbox.AwaitingCount() ?? 0;

    /// <summary>
    /// Um comando do painel mudou este pedido (por este caixa ou pelo ciclo de
    /// sincronização): relê itens e totais do banco. Sem isso a tela mostraria o
    /// total antigo ao cliente — o fechamento já cobra o do banco.
    /// </summary>
    public void ReloadIfOpen(string orderId)
    {
        if (orderId != OrderId || _adjustments is null) return;
        var order = SaleRepository.LoadOpenOrder(_adjustments.Connection, orderId);
        var live = _adjustments.LiveItems(orderId);
        var selected = SelectedLine?.ItemId;
        Lines.Clear();
        foreach (var item in live)
        {
            Lines.Add(new SaleLine(
                item.Id, item.ProductName,
                item.IsWeighed ? WeightPricing.Kilos(item.NetWeightGrams) : Quantity(item.Quantity), item.TotalCents));
        }
        SelectedLine = Lines.FirstOrDefault(line => line.ItemId == selected);
        ApplyTotals(order);
        Notice = "O painel alterou esta venda.";
    }

    private bool CanReviewRemote() => RemoteAwaiting > 0 && _remote is not null && !IsPaying;

    /// <summary>Cada pedido parado, um de cada vez, com o PIN de quem está no caixa.</summary>
    [RelayCommand(CanExecute = nameof(CanReviewRemote))]
    private async Task ReviewRemoteAsync()
    {
        Error = null;
        foreach (var waiting in _remote!.Awaiting())
        {
            var uuid = waiting.Command.CommandUuid;
            string? outcome;
            try
            {
                outcome = await AskRemoteDecision(new RemoteDecisionRequest(
                    waiting.Note,
                    _remote.ConfirmerLogins(),
                    (login, pin) => _remote.Confirm(uuid, login, pin),
                    (login, pin, reason) => _remote.Decline(uuid, login, pin, reason)));
            }
            catch (CommandRefusedException refused)
            {
                // Uma trava recusou na hora do aceite (a janela venceu, o pedido fechou).
                Error = "O painel pediu, mas não foi possível: " + refused.Message;
                continue;
            }
            if (outcome is null) break;
            Notice = outcome;
        }
        RefreshRemote();
    }

    private void ApplyTotals(OpenOrder order)
    {
        SubtotalCents = order.SubtotalCents;
        DiscountCents = order.DiscountCents;
        OnPropertyChanged(nameof(Discount));
        TotalCents = order.TotalCents;
    }

    private static string Quantity(decimal quantity) => quantity.ToString("0.###", CultureInfo.GetCultureInfo("pt-BR"));

    // -- cancelamento e desconto --------------------------------------------

    /// <summary>O diálogo de login e PIN de quem libera. A casca liga a um ContentDialog.</summary>
    public Func<AuthorizationRequest, Task<Identity?>> AskAuthorizer { get; set; } = _ => Task.FromResult<Identity?>(null);

    private bool CanCancelItem() => !IsPaying && SelectedLine is not null && _adjustments is not null && _authorization is not null;

    /// <summary>F4: cancela o item marcado. Motivo, depois PIN de gerente — o vetor de furto nº 1 não sai barato.</summary>
    [RelayCommand(CanExecute = nameof(CanCancelItem))]
    private async Task CancelItemAsync()
    {
        if (SelectedLine is not { } line || OrderId is null) return;
        Error = null;
        Notice = null;

        var reason = (await AskText("Motivo do cancelamento (registrado na auditoria):"))?.Trim();
        if (string.IsNullOrEmpty(reason)) return;

        var authorizer = await AskAuthorizer(new AuthorizationRequest(
            $"Cancelar {line.Name} — {line.Total}",
            _authorization!.ListAuthorizers(Roles.ItemCancel),
            (login, pin) => _authorization.AuthorizeRole(login, pin, Roles.ItemCancel)));
        if (authorizer is null) return;

        try
        {
            var order = _adjustments!.CancelItem(OrderId, line.ItemId, _operator.Id, authorizer, reason);
            Lines.Remove(line);
            SelectedLine = null;
            ApplyTotals(order);
            Notice = $"Item cancelado — autorizado por {authorizer.Name}";
        }
        catch (Exception error) when (error is AuthorizationRequiredException or InvalidQuantityException
                                          or OrderNotOpenException)
        {
            Error = error.Message;
        }
    }

    private bool CanDiscount() => !IsPaying && TotalCents > 0 && OrderId is not null && _adjustments is not null && _authorization is not null;

    /// <summary>F6: desconto percentual sobre o subtotal, dentro do teto de quem autoriza.</summary>
    [RelayCommand(CanExecute = nameof(CanDiscount))]
    private async Task DiscountAsync()
    {
        if (OrderId is null) return;
        Error = null;
        Notice = null;

        var typed = await AskText($"Desconto: percentual sobre o subtotal de {Money.Format(SubtotalCents)}");
        if (string.IsNullOrWhiteSpace(typed)) return;
        if (Money.ParsePercent(typed) is not { } percent || percent <= 0 || percent > 100)
        {
            Error = "Informe um percentual entre 0 e 100.";
            return;
        }

        var shown = percent.ToString("0.##", CultureInfo.GetCultureInfo("pt-BR"));
        var authorizer = await AskAuthorizer(new AuthorizationRequest(
            $"Desconto de {shown}% sobre {Money.Format(SubtotalCents)}",
            _authorization!.ListAuthorizers(),
            (login, pin) => _authorization.AuthorizeDiscount(login, pin, percent)));
        if (authorizer is null) return;

        var reason = (await AskText("Motivo do desconto (registrado na auditoria):"))?.Trim();
        if (string.IsNullOrEmpty(reason)) return;

        try
        {
            var (discount, order) = _adjustments!.ApplyDiscount(OrderId, percent, _operator.Id, authorizer, reason);
            ApplyTotals(order);
            Notice = $"Desconto de {Money.Format(discount)} autorizado por {authorizer.Name}";
        }
        catch (Exception error) when (error is AuthorizationRequiredException or InvalidQuantityException
                                          or OrderNotOpenException)
        {
            Error = error.Message;
        }
    }

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
        SelectedLine = null;
        SubtotalCents = 0;
        DiscountCents = 0;
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
