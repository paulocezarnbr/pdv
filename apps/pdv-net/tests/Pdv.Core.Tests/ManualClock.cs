namespace Pdv.Core.Tests;

/// <summary>
/// Relógio de teste com os dois tempos independentes: o de parede (que o
/// operador consegue mudar no Windows) e o monotônico (que não volta).
/// </summary>
public sealed class ManualClock(DateTimeOffset start) : TimeProvider
{
    private DateTimeOffset _now = start;
    private long _ticks = 1_000_000_000;

    public override DateTimeOffset GetUtcNow() => _now;

    public override long GetTimestamp() => _ticks;

    public override long TimestampFrequency => TimeSpan.TicksPerSecond;

    /// <summary>O tempo passa: os dois relógios andam.</summary>
    public void Advance(TimeSpan elapsed)
    {
        _now += elapsed;
        _ticks += elapsed.Ticks;
    }

    /// <summary>Alguém mexe no relógio do Windows: só o de parede muda.</summary>
    public void SetWallClock(DateTimeOffset moment) => _now = moment;
}
