using System.Collections.ObjectModel;
using System.Globalization;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using Pdv.Core.Printing;
using Pdv.Data.Edge;

namespace Pdv.App;

/// <summary>Onde o app do garçom está: o que o painel dita para o celular.</summary>
/// <param name="ShortFingerprint">A digital do certificado; <c>null</c> quando o salão está em HTTP.</param>
public sealed record SalonEndpoint(string Scheme, int Port, string Address, string? ShortFingerprint)
{
    public string Url => $"{Scheme}://{Address}:{Port}";
}

public enum SalonAddressState
{
    /// <summary>O servidor não subiu (desligado, porta ocupada): o balcão segue vendendo.</summary>
    Off,

    /// <summary>No ar em HTTP: PIN e token trafegam em claro.</summary>
    Plain,

    Secure,
}

public sealed record SalonOrderRow(
    string Id, string Table, string Waiter, string Number, string Items, string Total, string Status, bool BillRequested);

public sealed record SalonPersonRow(string UserId, string Name, string Role, string Seen);

public sealed record SalonDeviceRow(string Id, string Name, string Kind, string Status, bool Revoked);

public sealed record SalonTicketRow(string Id, string Table, string Item, string Quantity, string Status, string Wait, bool Late);

/// <summary>
/// O painel do salão (F8) — o <c>ui/salon_panel.py</c>: parear e revogar
/// aparelho, as mesas abertas, quem está em turno e a fila da cozinha.
/// </summary>
/// <remarks>
/// <para>
/// Só lê e comanda serviços que já existem e já são testados; não há regra de
/// negócio aqui. Quem opera a loja não abre terminal para parear um celular,
/// e o caixa é quem destrava o ticket preso na TV da cozinha, que ninguém toca
/// com a mão suja.
/// </para>
/// <para>
/// Sem assinatura no barramento: o painel é do caixa, e uma consulta ao SQLite
/// local a cada dois segundos (<see cref="Refresh"/>, chamado pela casca) não
/// cobra o que uma assinatura por janela aberta cobraria. O código tem relógio
/// próprio, de um segundo (<see cref="Tick"/>): no ritmo do refresh a contagem
/// pularia de 4:58 para 4:56, e o operador desconfia do número que está lendo.
/// </para>
/// </remarks>
public sealed partial class SalonPanelViewModel : ObservableObject
{
    /// <summary>Com a largura de oito dígitos: o painel não encolhe quando o código vence.</summary>
    public const string CodePlaceholder = "———— ————";

    private const string IdleHint = "Gere um código e digite-o no aplicativo do garçom.";
    private const string RevokedHint = "Código revogado. Ninguém mais pareia com ele — gere outro.";

    /// <summary>
    /// Os estados viajam em inglês no protocolo (o app do garçom e o KDS leem
    /// o mesmo JSON) e só se traduzem na hora de aparecer.
    /// </summary>
    private static readonly Dictionary<string, string> TicketLabels = new()
    {
        ["queued"] = "na fila",
        ["preparing"] = "preparando",
        ["ready"] = "pronto",
        ["delivered"] = "entregue",
    };

    /// <summary>Regra a mais que o Python, que mostrava "waiter" e "kds" crus.</summary>
    private static readonly Dictionary<string, string> KindLabels = new()
    {
        ["waiter"] = "garçom",
        ["kds"] = "cozinha",
        ["owner"] = "dono",
        ["manager"] = "gerente",
        ["cashier"] = "caixa",
    };

    private readonly EdgeAuth _auth;
    private readonly TableOrderService _orders;
    private readonly KdsService _kds;
    private readonly StaffSessions _staff;
    private readonly TimeProvider _clock;

    /// <summary>
    /// O código em texto só existe aqui, entre gerá-lo e ele vencer. O banco
    /// guarda só o hash: relê-lo é impossível por construção.
    /// </summary>
    private string _codeText = "";

    public SalonPanelViewModel(
        EdgeAuth auth, TableOrderService orders, KdsService kds, StaffSessions staff, SalonEndpoint? endpoint,
        TimeProvider? clock = null)
    {
        _auth = auth;
        _orders = orders;
        _kds = kds;
        _staff = staff;
        _clock = clock ?? TimeProvider.System;
        (AddressState, Address) = Describe(endpoint);
        Refresh();
        Tick();
    }

    /// <summary>Confirmação de algo que não se desfaz (título, pergunta). A casca abre o diálogo.</summary>
    public Func<string, string, Task<bool>> Confirm { get; set; } = (_, _) => Task.FromResult(false);

    public SalonAddressState AddressState { get; }

    public string Address { get; }

    public ObservableCollection<SalonOrderRow> Orders { get; } = [];

    public ObservableCollection<SalonPersonRow> Staff { get; } = [];

    public ObservableCollection<SalonDeviceRow> Devices { get; } = [];

    public ObservableCollection<SalonTicketRow> Tickets { get; } = [];

    [ObservableProperty]
    public partial SalonOrderRow? SelectedOrder { get; set; }

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(EndShiftCommand))]
    public partial SalonPersonRow? SelectedPerson { get; set; }

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(RevokeDeviceCommand))]
    public partial SalonDeviceRow? SelectedDevice { get; set; }

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(BumpCommand), nameof(RecallCommand))]
    public partial SalonTicketRow? SelectedTicket { get; set; }

    [ObservableProperty]
    public partial string Code { get; private set; } = CodePlaceholder;

    [ObservableProperty]
    public partial string CodeHint { get; private set; } = IdleHint;

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(RevokeCodeCommand))]
    public partial bool CodeAlive { get; private set; }

    /// <summary>O que deu errado na última ação, para a tela mostrar; vazio quando deu certo.</summary>
    [ObservableProperty]
    public partial string Error { get; private set; } = "";

    [RelayCommand]
    private void GenerateCode()
    {
        Error = "";
        var (code, _) = _auth.CreatePairingCode();
        // De quatro em quatro: o garçom lê a dois metros, de pé, e digita no
        // celular. Oito dígitos em dois grupos é o que as pessoas leem sem contar.
        _codeText = $"{code[..4]} {code[4..]}";
        Tick();
    }

    /// <summary>
    /// Mata o código antes do prazo — "alguém leu o código da minha tela". Fica
    /// ao lado de revogar aparelho ("o celular sumiu"): o operador decide entre
    /// os dois no mesmo instante.
    /// </summary>
    [RelayCommand(CanExecute = nameof(CodeAlive))]
    private void RevokeCode()
    {
        if (_auth.RevokePairingCodes() > 0) CodeHint = RevokedHint;
        _codeText = "";
        Tick();
    }

    /// <summary>A contagem do código, a cada segundo.</summary>
    /// <remarks>
    /// Sem ela o operador olhava um código sem saber se ainda valia, e só
    /// descobria quando o garçom errava no celular.
    /// </remarks>
    public void Tick()
    {
        var active = _auth.ActivePairingCode();
        CodeAlive = active is not null;
        if (active is null || _codeText.Length == 0)
        {
            // Vencido, revogado ou gerado noutra tela: o texto não está mais em
            // mãos, e dígitos que já não pareiam seriam pior que nada.
            Code = CodePlaceholder;
            if (active is not null)
            {
                CodeHint = $"Há um código vivo, gerado em outra tela ({Clock(active.RemainingSeconds)} restantes). " +
                    "Gerar outro invalida aquele.";
            }
            else if (CodeHint != RevokedHint)
            {
                CodeHint = IdleHint;
            }
            return;
        }
        Code = _codeText;
        CodeHint = $"Vence em {Clock(active.RemainingSeconds)} · vale para um único aparelho. Gerar outro código invalida este.";
    }

    [RelayCommand(CanExecute = nameof(HasDevice))]
    private async Task RevokeDeviceAsync()
    {
        if (SelectedDevice is not { } device) return;
        if (!await Confirm("Revogar aparelho",
                $"Revogar {device.Name}?\n\nO aparelho para de lançar pedidos imediatamente. " +
                "Os pedidos já lançados continuam valendo.")) return;
        _auth.Revoke(device.Id);
        Refresh();
    }

    private bool HasDevice() => SelectedDevice is { Revoked: false };

    /// <summary>Derruba quem foi embora sem sair do app, em todos os aparelhos.</summary>
    [RelayCommand(CanExecute = nameof(HasPerson))]
    private async Task EndShiftAsync()
    {
        if (SelectedPerson is not { } person) return;
        if (!await Confirm("Encerrar turno",
                $"Encerrar a sessão de {person.Name} em todos os aparelhos?\n\n" +
                "As comandas já lançadas continuam no nome dela.")) return;
        _staff.RevokeUser(person.UserId);
        Refresh();
    }

    private bool HasPerson() => SelectedPerson is not null;

    [RelayCommand(CanExecute = nameof(HasTicket))]
    private void Bump() => Transition(_kds.Bump);

    [RelayCommand(CanExecute = nameof(HasTicket))]
    private void Recall() => Transition(_kds.Recall);

    private bool HasTicket() => SelectedTicket is not null;

    private void Transition(Func<string, KdsTicket> action)
    {
        if (SelectedTicket is not { } ticket) return;
        try
        {
            action(ticket.Id);
            Error = "";
        }
        catch (Exception error) when (error is InvalidTransitionException or TicketNotFoundException)
        {
            Error = error.Message;
        }
        Refresh();
    }

    /// <summary>Relê mesas, pessoas, aparelhos e cozinha, sem tirar a seleção de baixo do dedo.</summary>
    /// <remarks>
    /// Sem guardar a seleção, o refresh a cada dois segundos a tiraria, e o
    /// operador daria bump no ticket errado.
    /// </remarks>
    public void Refresh()
    {
        var order = SelectedOrder?.Id;
        var person = SelectedPerson?.UserId;
        var device = SelectedDevice?.Id;
        var ticket = SelectedTicket?.Id;

        Replace(Orders, _orders.ListOpenOrders().Select(OrderRow));
        Replace(Staff, _staff.ListActive().Select(s => new SalonPersonRow(
            s.UserId, s.UserName.Length == 0 ? "—" : s.UserName, Label(KindLabels, s.Role), Seen(s.LastSeenAt))));
        Replace(Devices, _auth.ListDevices().Select(d => new SalonDeviceRow(
            d.Id, d.Name.Length == 0 ? "—" : d.Name, Label(KindLabels, d.Kind),
            d.RevokedAt is null ? Seen(d.LastSeenAt) : "revogado", d.RevokedAt is not null)));
        Replace(Tickets, _kds.ListActive().Select(t => new SalonTicketRow(
            t.Id, t.TableLabel, t.ProductName, t.Quantity, Label(TicketLabels, t.Status), Wait(t.WaitingSeconds), t.IsLate)));

        SelectedOrder = Orders.FirstOrDefault(r => r.Id == order);
        SelectedPerson = Staff.FirstOrDefault(r => r.UserId == person);
        SelectedDevice = Devices.FirstOrDefault(r => r.Id == device);
        SelectedTicket = Tickets.FirstOrDefault(r => r.Id == ticket);
    }

    private static SalonOrderRow OrderRow(TableOrder order) => new(
        order.Id,
        order.TableLabel,
        string.IsNullOrWhiteSpace(order.WaiterName) ? "—" : order.WaiterName.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries)[0],
        order.LocalNumber.ToString("00000", CultureInfo.InvariantCulture),
        order.ItemCount.ToString(CultureInfo.InvariantCulture),
        $"R$ {EscPosBuilder.FormatCents(order.TotalCents)}",
        // A única linha em que alguém está de pé esperando: sem o destaque, o
        // garçom tem de vir avisar, que é o que o app veio eliminar.
        order.BillRequested ? "pedindo a conta" : "",
        order.BillRequested);

    /// <summary>
    /// Troca só o que mudou. Recriar a lista inteira a cada dois segundos
    /// pisca a tela e rola a lista de volta para o topo.
    /// </summary>
    private static void Replace<T>(ObservableCollection<T> target, IEnumerable<T> rows)
    {
        var fresh = rows.ToList();
        for (var i = 0; i < fresh.Count; i++)
        {
            if (i < target.Count)
            {
                if (!EqualityComparer<T>.Default.Equals(target[i], fresh[i])) target[i] = fresh[i];
            }
            else
            {
                target.Add(fresh[i]);
            }
        }
        while (target.Count > fresh.Count) target.RemoveAt(target.Count - 1);
    }

    private static (SalonAddressState, string) Describe(SalonEndpoint? endpoint)
    {
        if (endpoint is null) return (SalonAddressState.Off, "Servidor do salão DESLIGADO — o balcão segue vendendo.");
        // O endereço é o app do garçom: quem está no caixa precisa saber o que
        // ditar para o celular, sem procurar.
        var lines = new List<string>
        {
            $"App do garçom: {endpoint.Url}",
            "Abra no navegador do celular e pareie com o código ao lado.",
        };
        if (endpoint.ShortFingerprint is { } fingerprint)
        {
            // A digital é o que torna o autoassinado conferível: o celular avisa,
            // e quem está no balcão compara estes quatro blocos antes de aceitar.
            lines.Add($"O celular vai avisar que o certificado é da própria loja. Confira a digital: {fingerprint}");
            return (SalonAddressState.Secure, string.Join('\n', lines));
        }
        lines.Add("SEM CRIPTOGRAFIA: o PIN e o token trafegam em claro na rede.");
        return (SalonAddressState.Plain, string.Join('\n', lines));
    }

    private static string Label(Dictionary<string, string> labels, string value) =>
        value.Length == 0 ? "—" : labels.GetValueOrDefault(value, value);

    /// <summary>Contagem regressiva como relógio de parede.</summary>
    public static string Clock(int seconds)
    {
        seconds = Math.Max(0, seconds);
        return $"{seconds / 60}:{seconds % 60:00}";
    }

    /// <summary>
    /// A espera do ticket. <c>MM:SS</c> só até uma hora: o ticket esquecido no
    /// fim do expediente aparecia como "377:23", que não diz "seis horas
    /// parado", diz "tem coisa errada nesta tela".
    /// </summary>
    public static string Wait(long seconds)
    {
        seconds = Math.Max(0, seconds);
        return seconds < 3600
            ? $"{seconds / 60:00}:{seconds % 60:00}"
            : $"{seconds / 3600}h{seconds % 3600 / 60:00}";
    }

    /// <summary>
    /// Só a hora do último contato: o operador quer saber se o aparelho falou
    /// agora há pouco. Na hora da loja — regra a mais que o Python, que mostrava
    /// a hora UTC, e às 20h de Brasília dizia 23h.
    /// </summary>
    private string Seen(string? value)
    {
        if (string.IsNullOrEmpty(value)) return "nunca conectou";
        if (!DateTimeOffset.TryParse(value, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var moment))
        {
            return value;
        }
        var local = TimeZoneInfo.ConvertTime(moment, _clock.LocalTimeZone);
        return $"visto {local:HH:mm:ss}";
    }
}
