namespace Pdv.Data.Sync;

/// <summary>
/// O laço de sincronização em segundo plano — nunca na thread da tela: um
/// servidor lento não pode virar fila parada no balcão.
/// </summary>
/// <remarks>
/// A cadência curta é decisão de <b>segurança</b>: o tempo entre o commit
/// local e o veredito da nuvem é a janela em que a venda só existe neste PC e
/// pode ser adulterada por quem tem acesso a ele. Com fila, acelera; em dia,
/// desacelera para não bater na nuvem à toa.
/// </remarks>
public sealed class SyncWorker(SyncEngine engine, Action<string>? log = null)
{
    public static readonly TimeSpan IdleInterval = TimeSpan.FromSeconds(15);
    public static readonly TimeSpan BusyInterval = TimeSpan.FromSeconds(3);
    public static readonly TimeSpan ErrorInterval = TimeSpan.FromSeconds(30);

    /// <summary>Ciclos de envio por pull de cadastro: preço novo espera, venda não.</summary>
    public const int PullEveryNCycles = 20;

    private readonly Action<string> _log = log ?? (_ => { });
    private int _cycle;
    private bool? _online;

    public event EventHandler<SyncReport>? CycleFinished;

    /// <summary>A enviar e em quarentena, depois de cada ciclo.</summary>
    public event EventHandler<(long Pending, long Quarantined)>? QueueChanged;
    public event EventHandler<bool>? ConnectionChanged;

    /// <summary>Roda até o cancelamento. Nenhuma exceção de um ciclo mata o laço.</summary>
    public async Task RunAsync(CancellationToken cancellation)
    {
        while (!cancellation.IsCancellationRequested)
        {
            var interval = await TickAsync(cancellation);
            try
            {
                await Task.Delay(interval, cancellation);
            }
            catch (OperationCanceledException)
            {
                return;
            }
        }
    }

    /// <summary>Um ciclo. Devolve quanto esperar até o próximo.</summary>
    public async Task<TimeSpan> TickAsync(CancellationToken cancellation = default)
    {
        _cycle++;
        SyncReport report;
        try
        {
            report = await engine.DrainAsync(maxCycles: 10, cancellation);
        }
        catch (OperationCanceledException) when (cancellation.IsCancellationRequested)
        {
            return TimeSpan.Zero;
        }
        catch (Exception error)
        {
            // Se o laço morrer, o PDV para de sincronizar calado e ninguém vê
            // até o fechamento do mês. Registra e segue.
            _log($"Erro inesperado no ciclo de sincronização: {error}");
            SetOnline(false);
            CycleFinished?.Invoke(this, new SyncReport(Error: error.Message));
            return ErrorInterval;
        }

        SetOnline(report.Error is null);
        CycleFinished?.Invoke(this, report);

        // Depois do envio, dê certo ou não: fila presa é o que o painel precisa ver.
        try
        {
            await engine.HeartbeatAsync(cancellation);
        }
        catch (Exception error) when (error is not OperationCanceledException)
        {
            _log($"Erro inesperado no relato de saúde: {error.Message}");
        }

        var pending = engine.PendingCount();
        var quarantined = engine.QuarantinedCount();
        QueueChanged?.Invoke(this, (pending - quarantined, quarantined));
        if (report.Error is not null) return ErrorInterval;

        if (_cycle % PullEveryNCycles == 0)
        {
            try
            {
                await engine.PullOnceAsync(cancellation);
            }
            catch (Exception error) when (error is not OperationCanceledException)
            {
                _log($"Falha no pull de cadastros: {error.Message}");
            }
        }
        return pending > 0 ? BusyInterval : IdleInterval;
    }

    private void SetOnline(bool online)
    {
        if (_online == online) return;
        _online = online;
        ConnectionChanged?.Invoke(this, online);
    }
}
