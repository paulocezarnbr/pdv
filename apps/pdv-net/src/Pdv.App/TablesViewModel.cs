using System.Collections.ObjectModel;
using System.Globalization;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using Pdv.Data.Auth;
using Pdv.Data.Edge;
using Pdv.Data.Sales;

namespace Pdv.App;

public sealed record TableRow(
    string Id, string Table, string Area, string Status, string Waiter, string Number, string Items, string Total,
    string Elapsed, bool BillRequested);

/// <summary>Um item da comanda para escolher (pagar parte, mover).</summary>
public sealed record TableItemChoice(string Id, string Name, string Quantity, long TotalCents)
{
    public string Total => Money.Format(TotalCents);

    public string Label => $"{Name}  ·  {Quantity}  ·  {Total}";
}

/// <summary>A conferência antes do pagamento: a conta (ou a parte) e quem atendeu.</summary>
public sealed record TipRequest(string Title, long TotalCents, string Details, long SuggestedCents);

/// <summary>Escolher itens: o título, o texto do botão e os itens vivos.</summary>
public sealed record ItemPickRequest(string Title, string Confirm, IReadOnlyList<TableItemChoice> Items);

/// <summary>
/// As mesas do caixa (F9) — o <c>ui/tables_dialog.py</c>: o salão inteiro com
/// busca, e onde a mesa vira dinheiro.
/// </summary>
/// <remarks>
/// <para>
/// O painel do salão (F8) é de quem cuida do salão; esta tela é do caixa.
/// Receber, pagar parte, mover itens e juntar comandas: a regra de cada um
/// mora no <see cref="TableOrderService"/>, e aqui só se escolhe.
/// </para>
/// <para>
/// Como no Python, o cartão da mesa é registrado com a forma e o valor, sem
/// passar pelo TEF do balcão: a mesa é cobrada na maquininha avulsa. Ligar o
/// TEF aqui é decisão de produto, anotada no <c>port_csharp.md</c>.
/// </para>
/// </remarks>
public sealed partial class TablesViewModel : ObservableObject
{
    /// <summary>
    /// Dez por cento é o costume e o valor que a casa imprime na conta. Fica
    /// como botão, nunca aplicado sozinho: gorjeta cobrada sem alguém decidir
    /// por ela é a reclamação clássica.
    /// </summary>
    public const int SuggestedTipPercent = 10;

    private readonly TableOrderService _orders;
    private readonly TableService _tables;
    private readonly Identity _operator;
    private readonly TimeProvider _clock;
    private IReadOnlyList<TableOrder> _open = [];
    private Dictionary<string, string> _areas = [];
    private int _free;

    public TablesViewModel(TableOrderService orders, TableService tables, Identity @operator, TimeProvider? clock = null)
    {
        _orders = orders;
        _tables = tables;
        _operator = @operator;
        _clock = clock ?? TimeProvider.System;
        Refresh();
    }

    public Func<string, string, Task<bool>> Confirm { get; set; } = (_, _) => Task.FromResult(false);

    public Func<string, string, Task> Inform { get; set; } = (_, _) => Task.CompletedTask;

    /// <summary>A gorjeta em centavos; <c>null</c> quando o operador voltou.</summary>
    public Func<TipRequest, Task<long?>> AskTip { get; set; } = _ => Task.FromResult<long?>(null);

    /// <summary>As formas de pagamento para o valor a cobrar; <c>null</c> quando desistiu.</summary>
    public Func<long, Task<IReadOnlyList<PaymentIntent>?>> AskPayments { get; set; } =
        _ => Task.FromResult<IReadOnlyList<PaymentIntent>?>(null);

    /// <summary>Os ids marcados; <c>null</c> ou vazio quando voltou.</summary>
    public Func<ItemPickRequest, Task<IReadOnlyList<string>?>> PickItems { get; set; } =
        _ => Task.FromResult<IReadOnlyList<string>?>(null);

    /// <summary>O índice da comanda de destino na lista mostrada; <c>null</c> quando desistiu.</summary>
    public Func<string, IReadOnlyList<string>, Task<int?>> PickTarget { get; set; } =
        (_, _) => Task.FromResult<int?>(null);

    /// <summary>A conta fechada, para a casca imprimir. A venda já está gravada.</summary>
    public event EventHandler<SettledOrder>? Settled;

    public ObservableCollection<TableRow> Rows { get; } = [];

    [ObservableProperty]
    public partial TableRow? Selected { get; set; }

    [ObservableProperty]
    public partial string Query { get; set; } = "";

    [ObservableProperty]
    public partial bool OnlyBilling { get; set; }

    [ObservableProperty]
    public partial string Summary { get; private set; } = "";

    /// <summary>A recusa da última ação; vazio quando deu certo.</summary>
    [ObservableProperty]
    public partial string Error { get; private set; } = "";

    partial void OnQueryChanged(string value) => Repaint();

    partial void OnOnlyBillingChanged(bool value) => Repaint();

    /// <summary>Relê o salão do banco. A casca chama a cada dois segundos.</summary>
    public void Refresh()
    {
        _open = _orders.ListOpenOrders();
        var tables = _tables.List();
        _free = tables.Count(table => !table.Occupied);
        // A área vive na mesa; o pedido guarda só a cópia do rótulo, de propósito.
        _areas = tables.ToDictionary(table => table.Id, table => table.Area);
        Repaint();
    }

    /// <summary>Filtra o que já está em memória: reconsultar a cada tecla seria uma consulta por letra.</summary>
    private void Repaint()
    {
        // Sem devolver a seleção, o refresh tiraria a linha de baixo do dedo, e
        // receber a mesa errada é problema com dinheiro.
        var selected = Selected?.Id;
        var visible = _open.Where(order => (!OnlyBilling || order.BillRequested) && Matches(order, AreaOf(order), Query)).ToList();
        var rows = visible.Select(order => new TableRow(
            order.Id, order.TableLabel, AreaOf(order), order.BillRequested ? "pedindo a conta" : "ocupada",
            order.WaiterName.Length == 0 ? "—" : order.WaiterName,
            order.LocalNumber.ToString("00000", CultureInfo.InvariantCulture),
            order.ItemCount.ToString(CultureInfo.InvariantCulture), Money.Format(order.TotalCents),
            Elapsed(order.OpenedAt, _clock.GetUtcNow()), order.BillRequested)).ToList();

        for (var i = 0; i < rows.Count; i++)
        {
            if (i < Rows.Count)
            {
                if (Rows[i] != rows[i]) Rows[i] = rows[i];
            }
            else
            {
                Rows.Add(rows[i]);
            }
        }
        while (Rows.Count > rows.Count) Rows.RemoveAt(Rows.Count - 1);
        Selected = Rows.FirstOrDefault(row => row.Id == selected);

        Summary = $"{visible.Count} de {_open.Count} comandas abertas · {_open.Count(o => o.BillRequested)} pedindo a conta · " +
            $"{_free} mesas livres · na tela: {Money.Format(visible.Sum(o => o.TotalCents))}";
    }

    private string AreaOf(TableOrder order) =>
        order.TableId is { } id && _areas.TryGetValue(id, out var area) ? area : "—";

    private TableOrder? SelectedOrder() => _open.FirstOrDefault(order => order.Id == Selected?.Id);

    private async Task<TableOrder?> RequireSelectedAsync(string title)
    {
        if (SelectedOrder() is { } order) return order;
        await Inform(title, "Selecione uma mesa na lista.");
        return null;
    }

    [RelayCommand]
    private async Task ReceiveAsync()
    {
        if (await RequireSelectedAsync("Receber") is not { } order) return;
        if (!order.BillRequested && !await Confirm("Receber",
                $"A {order.TableLabel} ainda não pediu a conta.\n\nReceber mesmo assim {Money.Format(order.TotalCents)}?"))
        {
            // Não bloqueia: o cliente sai pelo caixa sem o garçom ter pedido a
            // conta. Só exige um segundo gesto para receber a mesa errada.
            return;
        }
        if (await AskTip(Tip(order, order.TotalCents, order.ItemCount)) is not { } tip) return;
        if (await AskPayments(order.TotalCents + tip) is not { Count: > 0 } payments) return;

        await SettleAsync(() => _orders.Settle(order.Id, payments, _operator.Id, _operator.Name, tip));
    }

    [RelayCommand]
    private async Task ReceivePartAsync()
    {
        if (await RequireSelectedAsync("Pagar parte") is not { } order) return;
        var items = LiveItems(order.Id);
        if (await PickItems(new ItemPickRequest($"{order.TableLabel} — o que este cliente vai pagar", "Ir para a gorjeta", items))
            is not { Count: > 0 } ids) return;
        // A gorjeta e o pagamento enxergam só a parte: 10% da mesa inteira
        // cobrados de quem pediu um café é o erro que ninguém percebe até a reclamação.
        var part = items.Where(item => ids.Contains(item.Id)).Sum(item => item.TotalCents);
        if (await AskTip(Tip(order, part, ids.Count)) is not { } tip) return;
        if (await AskPayments(part + tip) is not { Count: > 0 } payments) return;

        await SettleAsync(() => _orders.SettleItems(order.Id, ids, payments, _operator.Id, _operator.Name, tip));
    }

    [RelayCommand]
    private async Task MoveItemsAsync()
    {
        if (await RequireSelectedAsync("Mover itens") is not { } source) return;
        if (await PickTargetAsync(source, "Mover itens para qual comanda?") is not { } target) return;
        if (await PickItems(new ItemPickRequest($"{source.TableLabel} → {target.TableLabel}: quais itens", "Mover",
                LiveItems(source.Id))) is not { Count: > 0 } ids) return;
        Run(() => _orders.MoveItems(source.Id, target.Id, ids, _operator.Id, _operator.Name));
    }

    [RelayCommand]
    private async Task MergeAsync()
    {
        if (await RequireSelectedAsync("Juntar comandas") is not { } source) return;
        if (await PickTargetAsync(source, $"Juntar a {source.TableLabel} em qual comanda?") is not { } target) return;
        if (!await Confirm("Juntar comandas",
                $"Passar os {source.ItemCount} itens da {source.TableLabel} ({Money.Format(source.TotalCents)}) para a " +
                $"{target.TableLabel}?\n\nA conta da {target.TableLabel} fica em " +
                $"{Money.Format(source.TotalCents + target.TotalCents)} e a {source.TableLabel} é liberada.")) return;
        Run(() => _orders.MergeOrders(source.Id, target.Id, _operator.Id, _operator.Name));
    }

    private async Task<TableOrder?> PickTargetAsync(TableOrder source, string prompt)
    {
        var others = _open.Where(order => order.Id != source.Id).ToList();
        if (others.Count == 0)
        {
            await Inform("Sem destino", "Não há outra comanda aberta no salão.");
            return null;
        }
        var labels = others.Select(order =>
            $"{order.TableLabel} · comanda {order.LocalNumber:00000} · {Money.Format(order.TotalCents)}").ToList();
        return await PickTarget(prompt, labels) is { } index && index >= 0 && index < others.Count ? others[index] : null;
    }

    /// <summary>Cancelado não se escolhe: movê-lo ou cobrá-lo desfaria um cancelamento autorizado.</summary>
    private List<TableItemChoice> LiveItems(string orderId) =>
        [.. _orders.ListItems(orderId).Select(node => node!.AsObject())
            .Where(item => !item["canceled"]!.GetValue<bool>())
            .Select(item => new TableItemChoice(item["id"]!.GetValue<string>(), item["product_name"]!.GetValue<string>(),
                item["quantity"]!.GetValue<string>(), item["total_cents"]!.GetValue<long>()))];

    private static TipRequest Tip(TableOrder order, long totalCents, int items) => new(
        $"{order.TableLabel} — conferir a conta",
        totalCents,
        $"Comanda {order.LocalNumber:00000} · {items} itens" +
        (order.WaiterName.Length == 0 ? "" : $" · atendida por {order.WaiterName}"),
        SuggestedTip(totalCents));

    /// <summary>Para baixo, no centavo: um centavo a mais de arredondamento vira reclamação e não rende nada.</summary>
    public static long SuggestedTip(long totalCents) => totalCents * SuggestedTipPercent / 100;

    private async Task SettleAsync(Func<SettledOrder> settle)
    {
        SettledOrder settled;
        try
        {
            settled = settle();
            Error = "";
        }
        catch (Exception error) when (IsRefusal(error))
        {
            Error = error.Message;
            Refresh();
            return;
        }
        // A conta já está fechada no banco; a impressão vem depois e não a desfaz.
        Settled?.Invoke(this, settled);
        Refresh();
        await Inform("Conta recebida", Announce(settled));
    }

    private void Run(Action action)
    {
        try
        {
            action();
            Error = "";
        }
        catch (Exception error) when (IsRefusal(error))
        {
            Error = error.Message;
        }
        Refresh();
    }

    /// <summary>
    /// O que o serviço recusa com motivo para o operador (o <c>PdvError</c> do
    /// Python) — inclusive a comanda que outro caixa fechou enquanto esta tela
    /// estava aberta. O resto é defeito e sobe.
    /// </summary>
    private static bool IsRefusal(Exception error) =>
        error is OrderClosedException or OrderNotFoundException or InsufficientPaymentException;

    public static string Announce(SettledOrder settled)
    {
        var order = settled.Order;
        var pieces = new List<string> { $"{order.TableLabel} recebida — {Money.Format(settled.ChargedCents)}" };
        if (settled.TipCents != 0)
        {
            var who = order.WaiterName.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries) is [var first, ..]
                ? first
                : "a equipe";
            pieces.Add($"gorjeta {Money.Format(settled.TipCents)} para {who}");
        }
        if (settled.ChangeCents != 0) pieces.Add($"troco {Money.Format(settled.ChangeCents)}");
        return string.Join(" · ", pieces);
    }

    /// <summary>
    /// A busca cobre o que o operador tem em mãos: "a mesa da varanda", o
    /// número da comanda no cupom, ou o garçom dizendo "é a minha".
    /// </summary>
    public static bool Matches(TableOrder order, string area, string query)
    {
        var terms = query.Trim().ToLowerInvariant().Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        if (terms.Length == 0) return true;
        var haystack = string.Join(' ', order.TableLabel, area, order.WaiterName,
            order.LocalNumber.ToString(CultureInfo.InvariantCulture),
            order.LocalNumber.ToString("00000", CultureInfo.InvariantCulture)).ToLowerInvariant();
        return terms.All(haystack.Contains);
    }

    /// <summary>
    /// Há quanto tempo a mesa está aberta: diz se a comanda de R$ 12,00 acabou
    /// de sentar ou está aberta desde o almoço — e comanda esquecida é como
    /// consumo some da conta.
    /// </summary>
    public static string Elapsed(string? openedAt, DateTimeOffset now)
    {
        if (string.IsNullOrEmpty(openedAt) ||
            !DateTimeOffset.TryParse(openedAt, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var started))
        {
            return "—";
        }
        // Relógio do Windows atrasado não mostra tempo negativo (o Python mostraria).
        var seconds = Math.Max(0, (long)(now - started).TotalSeconds);
        return seconds < 3600 ? $"{seconds / 60:00}min" : $"{seconds / 3600}h{seconds % 3600 / 60:00}";
    }
}
