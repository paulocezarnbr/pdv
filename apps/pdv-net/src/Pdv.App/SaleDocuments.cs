using Pdv.Core.Printing;
using Pdv.Data.Fiscal;

namespace Pdv.App;

/// <summary>
/// O papel de uma venda fechada: DANFE quando a NFC-e sai autorizada, cupom
/// em todo o resto.
/// </summary>
/// <remarks>
/// <para>
/// A venda já está gravada quando isto roda — nada aqui a desfaz. Qualquer
/// falha no caminho da nota vira cupom e aviso: o cliente sai com papel, e a
/// nota continua sendo pedida em segundo plano (<see cref="FiscalIssuance"/>).
/// </para>
/// <para>
/// Sem <see cref="FiscalIssuance"/> (chave <c>fiscal.enabled</c> desligada, o
/// padrão até a homologação), é só o cupom, como antes.
/// </para>
/// </remarks>
public sealed class SaleDocuments(
    Func<ClosedSale, byte[]> composeReceipt,
    Action<byte[], string> submit,
    PrinterLayout layout,
    FiscalIssuance? fiscal = null,
    Func<CancellationToken, Task>? pushOutbox = null,
    Action<string>? log = null)
{
    private readonly Action<string> _log = log ?? (_ => { });

    public bool FiscalEnabled => fiscal is not null;

    /// <summary>Imprime e devolve o aviso para o operador, ou <c>null</c> se não há o que dizer.</summary>
    public async Task<string?> PrintAsync(ClosedSale sale, CancellationToken cancellation = default)
    {
        if (fiscal is null)
        {
            submit(composeReceipt(sale), "PDV Cupom");
            return null;
        }

        FiscalOutcome outcome;
        try
        {
            using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellation);
            deadline.CancelAfter(FiscalIssuance.CounterDeadline * 2);
            outcome = await fiscal.RequestAsync(sale.OrderId, pushOutbox, deadline.Token);
        }
        catch (Exception error) when (error is not OperationCanceledException || !cancellation.IsCancellationRequested)
        {
            // Defeito, não rede (a rede já vira "pendente" lá dentro). O motivo
            // vai para o log; o operador lê o efeito para ele.
            _log($"fiscal: pedido da nota da venda {sale.OrderId} falhou: {error}");
            submit(composeReceipt(sale), "PDV Cupom");
            return "A NFC-e não pôde ser pedida agora. O cupom foi impresso; a nota será pedida de novo sozinha.";
        }

        if (outcome is { Kind: FiscalOutcomeKind.Authorized, Danfe: { } danfe })
        {
            try
            {
                submit(NfceDanfeLayout.Build(danfe, layout), "PDV DANFE NFC-e");
                return outcome.Notice;
            }
            catch (DanfeException error)
            {
                _log($"fiscal: DANFE da venda {sale.OrderId} recusado na montagem: {error.Message}");
                submit(composeReceipt(sale), "PDV Cupom");
                return $"A NFC-e foi autorizada, mas o DANFE não pôde ser montado: {error.Message}";
            }
        }

        submit(composeReceipt(sale), "PDV Cupom");
        return outcome.Kind == FiscalOutcomeKind.NotRequired ? null : outcome.Notice;
    }

    /// <summary>
    /// A segunda via da última venda: o DANFE se a nota já saiu autorizada —
    /// inclusive a que só foi autorizada em segundo plano —, senão o último papel.
    /// </summary>
    public byte[]? Reprint(string? lastOrderId, byte[]? lastPrinted)
    {
        if (fiscal is not null && lastOrderId is not null)
        {
            try
            {
                if (fiscal.Outcome(lastOrderId) is { Kind: FiscalOutcomeKind.Authorized, Danfe: { } danfe })
                {
                    return NfceDanfeLayout.Build(danfe, layout);
                }
            }
            catch (Exception error)
            {
                _log($"fiscal: reimpressão do DANFE da venda {lastOrderId}: {error.Message}");
            }
        }
        return lastPrinted;
    }
}
