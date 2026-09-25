namespace Fiscal.Service;

/// <summary>
/// O motor travado do serviço em Python: recusa tudo, sem inventar XML.
/// </summary>
/// <remarks>
/// Continua existindo para quem precise desligar a emissão sem desligar o
/// serviço (<c>FISCAL_ENGINE=gate</c>): a retaguarda recebe um motivo claro em
/// vez de um serviço fora do ar.
/// </remarks>
public sealed class HomologationGateEngine : IFiscalEngine
{
    public string Name => "homologation-gated";

    public FiscalResult? Preflight(FiscalIntent intent) => null;

    public Task<EngineOutcome> AuthorizeAsync(
        FiscalIntent intent, Action<string, string, string> prepare, CancellationToken cancellation) =>
        Task.FromResult(EngineOutcome.Settled(new FiscalResult(
            FiscalResult.Rejected,
            "FISCAL_ENGINE_NOT_HOMOLOGATED",
            "Emissão desligada neste serviço fiscal (FISCAL_ENGINE=gate). " +
            "A reserva foi preservada; não emita outro número.")));

    public Task<EngineOutcome> ReconcileAsync(PendingRequest pending, CancellationToken cancellation) =>
        Task.FromResult(EngineOutcome.Open("IN_FLIGHT", "Motor travado não consulta a SEFAZ."));
}
