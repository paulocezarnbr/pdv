namespace Fiscal.Service;

/// <summary>
/// O desfecho de uma tentativa do motor. <see cref="Final"/> falso quer dizer
/// "transmiti e não sei o que houve": o pedido fica aberto para a reconciliação
/// perguntar à SEFAZ, e nunca é concluído como adivinhação.
/// </summary>
public sealed record EngineOutcome(FiscalResult Result, bool Final, bool Discard = false)
{
    public static EngineOutcome Settled(FiscalResult result) => new(result, true);

    public static EngineOutcome Open(string code, string reason) => new(FiscalResult.Unknowable(code, reason), false);

    /// <summary>
    /// Com certeza não processado, mesmo depois de assinado: o pedido e o XML são
    /// descartados, e a retransmissão assina de novo — com o certificado corrigido.
    /// </summary>
    public static EngineOutcome NotSent(string code, string reason) => new(FiscalResult.Unknowable(code, reason), false, true);
}

/// <summary>O motor fiscal. O <see cref="FiscalWorkflow"/> cuida da idempotência; o motor, da nota.</summary>
public interface IFiscalEngine
{
    string Name { get; }

    /// <summary>
    /// Conferências sem efeito colateral, antes de reivindicar o pedido:
    /// certificado no cofre, trava de produção. Um problema aqui não consome nada.
    /// </summary>
    /// <returns><c>null</c> se pode seguir; senão, a resposta para a retaguarda.</returns>
    FiscalResult? Preflight(FiscalIntent intent);

    /// <summary>
    /// Monta, assina e transmite. Antes de transmitir, grava o XML assinado com
    /// <paramref name="prepare"/> — depois disso, uma exceção não solta o pedido.
    /// </summary>
    Task<EngineOutcome> AuthorizeAsync(FiscalIntent intent, Action<string, string, string> prepare, CancellationToken cancellation);

    /// <summary>Pergunta à SEFAZ sobre um pedido assinado e ainda sem veredito.</summary>
    Task<EngineOutcome> ReconcileAsync(PendingRequest pending, CancellationToken cancellation);
}

/// <summary>
/// As duas rotas do serviço, sem HTTP: o que o Python fazia em <c>app.py</c>,
/// mais a reconciliação com a SEFAZ que o motor real permite.
/// </summary>
public sealed class FiscalWorkflow(ResultStore store, IFiscalEngine engine, Action<string>? log = null)
{
    private readonly Action<string> _log = log ?? (_ => { });

    public async Task<FiscalResult> AuthorizeAsync(FiscalIntent intent, CancellationToken cancellation = default)
    {
        if (store.Get(intent.RequestUuid) is { } prior && prior.Status != FiscalResult.Unknown) return prior;

        if (engine.Preflight(intent) is { } refused) return refused;

        if (!store.Claim(intent.RequestUuid, intent.DocumentId))
        {
            var pending = store.Pending(intent.RequestUuid);
            if (pending?.SignedXml is not null) return await ReconcileAsync(pending, cancellation);
            if (store.Get(intent.RequestUuid) is { } settled) return settled;
            // Reivindicado e sem XML assinado: se ficou velho, o processo caiu antes
            // de transmitir e é seguro retomar; se é recente, outra chamada está em voo.
            if (pending is not { Stale: true } || !store.TakeOver(intent.RequestUuid))
            {
                return FiscalResult.Unknowable("PROCESSING", "Solicitação fiscal ainda está em processamento.");
            }
        }

        var prepared = false;
        EngineOutcome outcome;
        try
        {
            outcome = await engine.AuthorizeAsync(intent, (key, xml, context) =>
            {
                store.Prepare(intent.RequestUuid, key, xml, context);
                prepared = true;
            }, cancellation);
        }
        catch (Exception error)
        {
            // A exceção real fica no log do contêiner, fora da resposta: uma
            // biblioteca fiscal pode incluir caminho do A1 ou detalhe criptográfico.
            _log($"motor {engine.Name} falhou em {intent.RequestUuid}: {error.GetType().Name}");
            if (!prepared)
            {
                // Nada foi transmitido: soltar o pedido deixa a retaguarda retransmitir
                // com o mesmo número, em vez de prender o documento para sempre.
                store.Release(intent.RequestUuid);
                return FiscalResult.Unknowable("ENGINE_FAILURE",
                    "Falha interna antes da transmissão; nada chegou à SEFAZ e a solicitação pode ser repetida.");
            }
            return FiscalResult.Unknowable("ENGINE_FAILURE",
                "Falha interna após o início da autorização; consulte antes de reenviar.");
        }

        if (outcome.Final) store.Settle(intent.RequestUuid, outcome.Result);
        else if (outcome.Discard) store.Discard(intent.RequestUuid);
        else if (!prepared) store.Release(intent.RequestUuid);
        return outcome.Result;
    }

    public async Task<FiscalResult> StatusAsync(string requestUuid, CancellationToken cancellation = default)
    {
        var settled = store.Get(requestUuid);
        if (settled is not null && settled.Status != FiscalResult.Unknown) return settled;

        if (!store.Known(requestUuid))
        {
            // Nunca chegou aqui: o motor não foi acionado, a SEFAZ não viu nada. A
            // retaguarda pode retransmitir com o MESMO número e o MESMO request_uuid.
            return FiscalResult.Unknowable("NOT_FOUND", "Solicitação nunca recebida por este serviço.");
        }

        var pending = store.Pending(requestUuid);
        if (pending?.SignedXml is not null) return await ReconcileAsync(pending, cancellation);
        if (settled is not null) return settled;
        if (pending is { Stale: true })
        {
            // Reivindicado há tempo e nunca assinado: nada foi transmitido.
            return FiscalResult.Unknowable("NOT_FOUND", "Solicitação nunca transmitida; pode ser repetida.");
        }
        // Chegou e não concluiu: só a própria chamada em voo, ou a SEFAZ, sabe.
        return FiscalResult.Unknowable("IN_FLIGHT", "Solicitação iniciada e não concluída; consulte a SEFAZ.");
    }

    private async Task<FiscalResult> ReconcileAsync(PendingRequest pending, CancellationToken cancellation)
    {
        EngineOutcome outcome;
        try
        {
            outcome = await engine.ReconcileAsync(pending, cancellation);
        }
        catch (Exception error)
        {
            _log($"reconciliação de {pending.RequestUuid} falhou: {error.GetType().Name}");
            return FiscalResult.Unknowable("IN_FLIGHT", "Solicitação iniciada e não concluída; consulte a SEFAZ.");
        }
        if (outcome.Final) store.Settle(pending.RequestUuid, outcome.Result);
        return outcome.Result;
    }
}
