namespace Pdv.Core.Scale;

/// <summary>Transporte físico da balança: abrir a porta e obter uma leitura. Nada de regra.</summary>
public interface IScaleDriver : IDisposable
{
    bool IsOpen { get; }

    /// <exception cref="ScaleException">Porta indisponível.</exception>
    void Open();

    void Close();

    /// <summary>Uma leitura. Bloqueia até o tempo limite da porta.</summary>
    /// <exception cref="ScaleException">Falha de porta, tempo esgotado ou quadro inválido.</exception>
    ScaleReading Read();
}

/// <summary>Quando o peso pode ser cobrado — a parte pura do <c>ScaleWorker</c> do Python.</summary>
/// <remarks>
/// A mercadoria balança no prato antes de assentar. Cobrar a primeira leitura
/// é cobrar o peso errado: o peso só vale depois de N leituras estáveis
/// <b>idênticas</b> seguidas, e deixa de valer assim que muda.
/// </remarks>
public sealed class StabilityTracker(int stableReadings = 3)
{
    private long? _lastGrams;
    private int _repeat;
    private bool _stableEmitted;

    /// <summary>A última leitura estável ainda válida; <c>null</c> se o peso mudou desde então.</summary>
    public ScaleReading? LastStable { get; private set; }

    /// <summary>Avalia uma leitura. Devolve se o peso estável de antes deixou de valer, e a nova leitura estável, se houver.</summary>
    public (bool Changed, ScaleReading? Stable) Evaluate(ScaleReading reading)
    {
        if (reading.Status != ScaleStatus.Stable) return (Reset(), null);

        var changed = false;
        if (reading.WeightGrams == _lastGrams)
        {
            _repeat++;
        }
        else
        {
            if (_lastGrams is not null && _stableEmitted)
            {
                changed = true;
                LastStable = null;
            }
            _lastGrams = reading.WeightGrams;
            _repeat = 1;
            _stableEmitted = false;
        }

        if (_repeat >= stableReadings && !_stableEmitted)
        {
            _stableEmitted = true;
            LastStable = reading;
            return (changed, reading);
        }
        return (changed, null);
    }

    /// <summary>Leitura com falha ou fora de estável: o que era estável deixa de valer.</summary>
    public bool Reset()
    {
        var changed = _stableEmitted;
        _lastGrams = null;
        _repeat = 0;
        _stableEmitted = false;
        LastStable = null;
        return changed;
    }
}

/// <summary>A leitura contínua da balança, fora da thread da tela — o <c>ScaleWorker</c>/<c>ScaleService</c> do Python.</summary>
/// <remarks>
/// <para>
/// Invariante 9 do <c>plan.md</c>: porta serial <b>nunca</b> é lida na thread da
/// tela. Uma COM travada congelaria o caixa com a fila esperando.
/// </para>
/// <para>
/// Os eventos saem da thread do leitor; quem mostra na tela despacha para a
/// dela. Um tempo esgotado isolado é ruído normal em serial: o erro só é
/// avisado na 3ª e na 30ª falha seguida, como no Python.
/// </para>
/// </remarks>
public sealed class ScaleMonitor(
    IScaleDriver driver, TimeSpan pollInterval, int stableReadings = 3, TimeProvider? clock = null) : IAsyncDisposable
{
    private readonly TimeProvider _clock = clock ?? TimeProvider.System;
    private readonly StabilityTracker _tracker = new(stableReadings);
    private readonly object _gate = new();
    private CancellationTokenSource? _stop;
    private Task? _loop;
    private int _errorStreak;

    /// <summary>Toda leitura, estável ou não — o mostrador ao vivo.</summary>
    public event Action<ScaleReading>? ReadingReceived;

    /// <summary>Uma vez por estabilização: habilita o registro do item.</summary>
    public event Action<ScaleReading>? StableWeight;

    /// <summary>O peso estável deixou de valer (mercadoria retirada ou trocada).</summary>
    public event Action? WeightChanged;

    public event Action<string>? ErrorOccurred;

    public event Action<bool>? ConnectionChanged;

    /// <summary>A última leitura estável ainda válida — o que o botão "Registrar" cobra.</summary>
    public ScaleReading? LastStable
    {
        get { lock (_gate) return _tracker.LastStable; }
    }

    public void Start()
    {
        if (_loop is not null) return;
        _stop = new CancellationTokenSource();
        _loop = Task.Run(() => RunAsync(_stop.Token));
    }

    public async ValueTask StopAsync()
    {
        if (_stop is null || _loop is null) return;
        await _stop.CancelAsync();
        try
        {
            await _loop;
        }
        catch (OperationCanceledException)
        {
        }
        _stop.Dispose();
        _stop = null;
        _loop = null;
    }

    public async ValueTask DisposeAsync()
    {
        await StopAsync();
        driver.Dispose();
    }

    private async Task RunAsync(CancellationToken cancellation)
    {
        try
        {
            driver.Open();
            ConnectionChanged?.Invoke(true);
        }
        catch (ScaleException error)
        {
            ErrorOccurred?.Invoke(error.Message);
            ConnectionChanged?.Invoke(false);
            return;
        }

        try
        {
            while (!cancellation.IsCancellationRequested)
            {
                Tick();
                await Task.Delay(pollInterval, _clock, cancellation);
            }
        }
        catch (OperationCanceledException)
        {
        }
        finally
        {
            driver.Close();
            ConnectionChanged?.Invoke(false);
        }
    }

    /// <summary>Uma volta do laço. Público para o teste conduzir sem relógio.</summary>
    public void Tick()
    {
        ScaleReading reading;
        try
        {
            reading = driver.Read();
        }
        catch (ScaleException error)
        {
            _errorStreak++;
            if (_errorStreak is 3 or 30) ErrorOccurred?.Invoke(error.Message);
            bool lost;
            lock (_gate) lost = _tracker.Reset();
            if (lost) WeightChanged?.Invoke();
            return;
        }

        _errorStreak = 0;
        ReadingReceived?.Invoke(reading);
        (bool Changed, ScaleReading? Stable) verdict;
        lock (_gate) verdict = _tracker.Evaluate(reading);
        if (verdict.Changed) WeightChanged?.Invoke();
        if (verdict.Stable is { } stable) StableWeight?.Invoke(stable);
    }
}

/// <summary>Balança simulada, para demonstração e teste de tela sem equipamento.</summary>
/// <remarks>
/// O peso oscila por algumas leituras e depois assenta, como na balança de
/// verdade. Gera quadros no formato da Toledo, então a leitura passa pelo
/// protocolo de verdade em vez de contorná-lo.
/// </remarks>
public sealed class SimulatedScale(long targetGrams = 847, int settleAfter = 3, TimeProvider? clock = null) : IScaleDriver
{
    private readonly ToledoPrix3Protocol _protocol = new();
    private readonly TimeProvider _clock = clock ?? TimeProvider.System;
    private long _target = targetGrams;
    private int _count;

    public bool IsOpen { get; private set; }

    public void Open()
    {
        IsOpen = true;
        _count = 0;
    }

    public void Close() => IsOpen = false;

    /// <summary>Outra mercadoria no prato.</summary>
    public void SetTarget(long grams)
    {
        Interlocked.Exchange(ref _target, Math.Max(0, grams));
        Interlocked.Exchange(ref _count, 0);
    }

    public ScaleReading Read()
    {
        if (!IsOpen) throw new ScaleNotConnectedException("Balança simulada fechada");

        var count = Interlocked.Increment(ref _count);
        var target = Interlocked.Read(ref _target);
        var grams = count <= settleAfter ? Math.Max(0, target + Random.Shared.Next(-25, 26)) : target;
        var frame = System.Text.Encoding.ASCII.GetBytes(Math.Min(grams, 99_999).ToString("00000"));
        return _protocol.Parse(frame, _clock.GetUtcNow());
    }

    public void Dispose() => Close();
}
