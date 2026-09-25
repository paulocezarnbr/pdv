using System.IO.Ports;
using Pdv.Core.Scale;

namespace Pdv.Data.Hardware;

/// <summary>Como a balança está ligada — as chaves <c>scale.*</c> de <c>device_settings</c>.</summary>
/// <remarks>
/// Os padrões cobrem o caso mais comum no Brasil (9600 8N1). Firmware antigo
/// às vezes pede 8N2 ou paridade par: confira o manual antes de culpar o
/// programa. <c>simulated</c> é a balança de demonstração.
/// </remarks>
public sealed record ScaleSettings(
    string Protocol = "simulated",
    string Port = "COM3",
    int BaudRate = 9600,
    int DataBits = 8,
    Parity Parity = Parity.None,
    StopBits StopBits = StopBits.One,
    int TimeoutMilliseconds = 400,
    int PollIntervalMilliseconds = 200,
    int StableReadings = 3,
    long MaxWeightGrams = 30_000)
{
    public bool IsSimulated => Protocol == "simulated";

    /// <summary>
    /// O que a detecção de hardware gravou, como o <c>apply_to</c> do Python:
    /// protocolo <b>e</b> porta, ou nada (fica a simulada).
    /// </summary>
    public static ScaleSettings Load(PdvDatabase database)
    {
        string? Value(string key) => database.Scalar(
            "SELECT value FROM device_settings WHERE key = $key", ("$key", key)) as string is { Length: > 0 } value
            ? value
            : null;

        var defaults = new ScaleSettings();
        if (Value("scale.protocol") is not { } protocol || Value("scale.port") is not { } port) return defaults;
        var baud = int.TryParse(Value("scale.baudrate"), out var parsed) && parsed > 0 ? parsed : defaults.BaudRate;
        return defaults with { Protocol = protocol, Port = port, BaudRate = baud };
    }

    public IScaleDriver BuildDriver(TimeProvider? clock = null) =>
        IsSimulated ? new SimulatedScale(clock: clock) : new SerialScale(this, ScaleProtocols.Build(Protocol), clock);
}

/// <summary>A balança física numa porta COM ou USB-serial — o <c>SerialScale</c> do Python.</summary>
/// <remarks>
/// <list type="number">
/// <item><b>Streaming pega o último quadro, não o primeiro</b>: o primeiro pode
/// ser o peso do cliente anterior.</item>
/// <item><b>O buffer de entrada é limpo antes de cada requisição</b>: sem isso
/// a resposta lida é a da pergunta anterior, e o erro só aparece com a fila
/// andando rápido.</item>
/// <item><b>Acima da capacidade é sobrecarga</b>, além do byte de status:
/// firmware antigo às vezes só satura o valor.</item>
/// </list>
/// </remarks>
public sealed class SerialScale(ScaleSettings settings, ScaleProtocol protocol, TimeProvider? clock = null) : IScaleDriver
{
    private readonly TimeProvider _clock = clock ?? TimeProvider.System;
    private SerialPort? _port;
    private byte[] _buffer = [];

    public ScaleProtocol Protocol => protocol;

    public bool IsOpen => _port?.IsOpen == true;

    public void Open()
    {
        try
        {
            _port = new SerialPort(settings.Port, settings.BaudRate, settings.Parity, settings.DataBits, settings.StopBits)
            {
                ReadTimeout = settings.TimeoutMilliseconds,
                WriteTimeout = settings.TimeoutMilliseconds,
            };
            _port.Open();
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or ArgumentException
                                          or InvalidOperationException)
        {
            _port?.Dispose();
            _port = null;
            throw new ScaleNotConnectedException($"Não foi possível abrir {settings.Port}: {error.Message}");
        }
        _buffer = [];
    }

    public void Close()
    {
        var port = _port;
        _port = null;
        port?.Dispose();
    }

    public ScaleReading Read()
    {
        if (_port is not { IsOpen: true } port) throw new ScaleNotConnectedException("Porta serial da balança fechada");

        byte[]? frame;
        try
        {
            frame = protocol.IsStreaming ? ReadStreaming(port) : ReadOnRequest(port);
        }
        catch (ScaleException)
        {
            throw;
        }
        catch (TimeoutException)
        {
            frame = null;
        }
        catch (Exception error) when (error is IOException or InvalidOperationException or UnauthorizedAccessException)
        {
            // Cabo removido, driver do Windows, porta tomada por outro programa.
            throw new ScaleException($"Falha de leitura na balança: {error.Message}");
        }

        if (frame is null)
        {
            throw new ScaleTimeoutException(
                $"Balança {protocol.Name} não respondeu em {settings.TimeoutMilliseconds / 1000.0:0.0}s");
        }

        var reading = protocol.Parse(frame, _clock.GetUtcNow());
        return reading.WeightGrams > settings.MaxWeightGrams
            ? reading with { Status = ScaleStatus.Overload, WeightGrams = 0 }
            : reading;
    }

    private byte[]? ReadOnRequest(SerialPort port)
    {
        // Descarta a resposta atrasada da pergunta anterior antes de perguntar de novo.
        port.DiscardInBuffer();
        port.Write(protocol.RequestFrame!, 0, protocol.RequestFrame!.Length);

        var raw = new List<byte>();
        while (true)
        {
            int value;
            try
            {
                value = port.ReadByte();
            }
            catch (TimeoutException)
            {
                break;
            }
            if (value < 0) break;
            raw.Add((byte)value);
            if (value == protocol.EndByte) break;
        }
        if (raw.Count == 0) return null;

        var (frame, _) = ScaleProtocols.ExtractLastFrame(raw.ToArray(), protocol.StartByte, protocol.EndByte);
        return frame ?? throw new ScaleFrameException($"Quadro sem delimitadores: {ScaleProtocol.Raw(raw.ToArray())}");
    }

    private byte[]? ReadStreaming(SerialPort port)
    {
        var pending = port.BytesToRead;
        byte[] chunk;
        if (pending > 0)
        {
            chunk = new byte[pending];
            chunk = chunk[..port.Read(chunk, 0, pending)];
        }
        else
        {
            // Bloqueia até o tempo limite, como o read(1) do pyserial.
            chunk = [(byte)port.ReadByte()];
        }

        var (frame, remaining) = ScaleProtocols.ExtractLastFrame([.. _buffer, .. chunk], protocol.StartByte, protocol.EndByte);
        _buffer = remaining;
        return frame;
    }

    public void Dispose() => Close();
}
