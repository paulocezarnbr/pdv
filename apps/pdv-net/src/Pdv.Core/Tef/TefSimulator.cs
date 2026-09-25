using System.Globalization;

namespace Pdv.Core.Tef;

public enum HostState
{
    Authorized,
    Confirmed,
    Undone,
}

/// <summary>
/// Provedor de TEF simulado — para homologar o fluxo do caixa sem adquirente.
/// </summary>
/// <remarks>
/// <para>
/// Ele simula também o lado da adquirente (<see cref="Host"/>): quanto está
/// autorizado, confirmado ou estornado. É o que permite testar a pergunta que
/// importa — o dinheiro do cliente terminou no lugar certo? — e não só se o
/// caixa mostrou a mensagem certa.
/// </para>
/// <para>Centavos do valor decidem o roteiro, como os cartões de teste das adquirentes:</para>
/// <list type="bullet">
///   <item><c>,51</c> — negada ("saldo insuficiente");</item>
///   <item><c>,52</c> — o cliente cancela no pinpad;</item>
///   <item><c>,53</c> — a adquirente aprova, e a resposta se perde na rede.</item>
/// </list>
/// </remarks>
public sealed class TefSimulator : ITefProvider
{
    private readonly Dictionary<string, HostState> _host = new(StringComparer.Ordinal);
    private readonly Lock _gate = new();
    private int _nsu;

    public string Name => "Simulador";

    /// <summary>Com <c>true</c>, toda chamada falha como se a rede tivesse caído.</summary>
    public bool Offline { get; set; }

    public IReadOnlyDictionary<string, HostState> Host
    {
        get
        {
            lock (_gate)
            {
                return new Dictionary<string, HostState>(_host, StringComparer.Ordinal);
            }
        }
    }

    public async Task<TefOutcome> AuthorizeAsync(TefRequest request, ITefInteraction ui, CancellationToken cancellationToken)
    {
        ThrowIfOffline();
        ui.Show("Insira, aproxime ou passe o cartão");
        await Task.Yield();

        var scenario = request.AmountCents % 100;
        if (scenario == 52)
        {
            ui.Show("Operação cancelada pelo cliente");
            return new TefOutcome.Aborted("Cancelada no pinpad");
        }

        ui.Show("Digite a senha no pinpad");
        if (scenario == 51)
        {
            ui.Show("Transação negada: saldo insuficiente");
            return new TefOutcome.Declined("Saldo insuficiente");
        }

        string nsu;
        lock (_gate)
        {
            _nsu++;
            nsu = _nsu.ToString("000000", CultureInfo.InvariantCulture);
            _host[request.TransactionId] = HostState.Authorized;
        }

        if (scenario == 53)
        {
            throw new IOException("Tempo esgotado aguardando a resposta da adquirente");
        }

        ui.Show("Transação aprovada");
        var value = (request.AmountCents / 100m).ToString("N2", CultureInfo.GetCultureInfo("pt-BR"));
        var kind = request.CardType switch
        {
            TefCardType.Credit => request.Installments > 1 ? $"CRÉDITO {request.Installments}x" : "CRÉDITO À VISTA",
            TefCardType.Debit => "DÉBITO",
            TefCardType.Pix => "PIX",
            _ => "VOUCHER",
        };
        return new TefOutcome.Approved(new TefApproval(
            request.TransactionId,
            request.AmountCents,
            request.CardType,
            nsu,
            AuthorizationCode: "A" + nsu,
            Acquirer: "SIMULADOR",
            CardBrand: "VISA",
            CustomerReceipt: $"SIMULADOR TEF\n{kind}\nVALOR: R$ {value}\nNSU: {nsu}\nVIA CLIENTE",
            MerchantReceipt: $"SIMULADOR TEF\n{kind}\nVALOR: R$ {value}\nNSU: {nsu}\nVIA ESTABELECIMENTO"));
    }

    public Task ConfirmAsync(TefReference reference, CancellationToken cancellationToken)
    {
        ThrowIfOffline();
        lock (_gate)
        {
            if (!_host.TryGetValue(reference.TransactionId, out var state))
            {
                throw new InvalidOperationException($"Adquirente não conhece a transação {reference.TransactionId}.");
            }
            if (state == HostState.Undone)
            {
                throw new InvalidOperationException($"Transação {reference.TransactionId} já foi desfeita.");
            }
            _host[reference.TransactionId] = HostState.Confirmed;
        }
        return Task.CompletedTask;
    }

    public Task UndoAsync(TefReference reference, CancellationToken cancellationToken)
    {
        ThrowIfOffline();
        lock (_gate)
        {
            // Idempotente, e inócuo para transação que nunca chegou ao host.
            if (_host.TryGetValue(reference.TransactionId, out var state) && state == HostState.Confirmed)
            {
                throw new InvalidOperationException(
                    $"Transação {reference.TransactionId} já confirmada: desfazer exige cancelamento.");
            }
            if (_host.ContainsKey(reference.TransactionId))
            {
                _host[reference.TransactionId] = HostState.Undone;
            }
        }
        return Task.CompletedTask;
    }

    private void ThrowIfOffline()
    {
        if (Offline) throw new IOException("Sem comunicação com o TEF");
    }
}
