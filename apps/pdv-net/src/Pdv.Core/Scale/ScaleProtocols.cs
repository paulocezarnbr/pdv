using System.Text;

namespace Pdv.Core.Scale;

/// <summary>Estado reportado pela balança. Só <see cref="Stable"/> autoriza o registro de uma venda.</summary>
public enum ScaleStatus
{
    Stable,
    Unstable,
    Overload,
    Negative,
    Zero,
    Error,
}

/// <summary>Uma leitura da balança.</summary>
/// <remarks>
/// <see cref="RawFrame"/> é o quadro <b>cru</b> recebido na serial. Vai para
/// <c>order_items.scale_reading_raw</c> e para o evento <c>weight_captured</c>:
/// é a prova de que o peso cobrado foi o peso lido, quando alguém alega que
/// "a balança errou".
/// </remarks>
public sealed record ScaleReading(ScaleStatus Status, long WeightGrams, string RawFrame, DateTimeOffset ReadAt)
{
    public bool Sellable => Status == ScaleStatus.Stable;

    /// <summary>O valor do enum do Python (<c>"stable"</c>, <c>"unstable"</c>…), que vai na auditoria.</summary>
    public string StatusName => Status.ToString().ToLowerInvariant();
}

public class ScaleException(string message) : Exception(message);

/// <summary>Porta serial indisponível: cabo solto, COM errada, driver ausente.</summary>
public sealed class ScaleNotConnectedException(string message) : ScaleException(message);

/// <summary>A balança não respondeu dentro da janela esperada.</summary>
public sealed class ScaleTimeoutException(string message) : ScaleException(message);

/// <summary>Quadro recebido não bate com o protocolo configurado.</summary>
public sealed class ScaleFrameException(string message) : ScaleException(message);

/// <summary>Traduz bytes crus em leitura de peso, para um modelo de balança — o <c>ScaleProtocol</c> do Python.</summary>
/// <remarks>
/// <para>
/// O formato do quadro varia entre fabricantes <b>e entre firmwares do mesmo
/// fabricante</b>. Cada modelo é uma classe isolada: ajustar a Toledo de uma
/// loja não pode arriscar a Filizola da loja vizinha.
/// </para>
/// <para>
/// É pura: recebe bytes, devolve leitura. Não abre porta, não dorme, não loga.
/// Conferida quadro a quadro contra <c>contracts/scale-weighing.json</c>, gerado
/// pelo Python.
/// </para>
/// </remarks>
public abstract class ScaleProtocol
{
    public const byte Stx = 0x02;
    public const byte Etx = 0x03;
    public const byte Enq = 0x05;

    public abstract string Name { get; }

    /// <summary>Quadro enviado para pedir uma pesagem. <c>null</c>: a balança transmite sozinha.</summary>
    public virtual byte[]? RequestFrame => [Enq];

    public byte StartByte => Stx;

    public byte EndByte => Etx;

    /// <summary>Dígitos de peso no quadro e casas decimais implícitas (em kg).</summary>
    protected virtual int WeightDigits => 5;

    protected virtual int WeightDecimals => 3;

    public bool IsStreaming => RequestFrame is null;

    /// <summary>Converte o miolo do quadro (sem STX/ETX) em leitura.</summary>
    /// <exception cref="ScaleFrameException">Quadro incompatível com o protocolo.</exception>
    public abstract ScaleReading Parse(byte[] frame, DateTimeOffset readAt);

    // -- o que o Python faz sem dizer ---------------------------------------

    /// <summary>
    /// <c>frame.decode("ascii", errors="replace").strip()</c>: byte fora do
    /// ASCII vira U+FFFD, e o <c>strip()</c> do Python também tira os
    /// separadores 0x1C–0x1F, que o <c>Trim()</c> do .NET deixa.
    /// </summary>
    protected static string Body(byte[] frame)
    {
        var text = new StringBuilder(frame.Length);
        foreach (var value in frame) text.Append(value < 0x80 ? (char)value : '�');
        return text.ToString().Trim(PythonWhitespace);
    }

    private static readonly char[] PythonWhitespace =
        [' ', '\t', '\n', '\v', '\f', '\r', '\x1c', '\x1d', '\x1e', '\x1f'];

    /// <summary>
    /// <c>raw.decode("ascii", errors="backslashreplace")</c>: o quadro cru
    /// gravado como prova. Byte fora do ASCII vira <c>\xNN</c> — outra grafia e a
    /// prova deixa de bater com a que o Python gravou para o mesmo equipamento.
    /// </summary>
    public static string Raw(byte[] frame)
    {
        var text = new StringBuilder(frame.Length);
        foreach (var value in frame)
        {
            if (value < 0x80) text.Append((char)value);
            else text.Append("\\x").Append(value.ToString("x2"));
        }
        return text.ToString();
    }

    protected static string Digits(string body) => new(body.Where(IsDigit).ToArray());

    protected static bool IsDigit(char value) => value is >= '0' and <= '9';

    /// <summary>"01234" com 3 casas = 1,234 kg = 1234 g; o fator cobre balança de 2 ou 4 casas.</summary>
    protected long DigitsToGrams(string digits)
    {
        if (digits.Length == 0 || !digits.All(IsDigit))
        {
            throw new ScaleFrameException($"{Name}: dígitos inválidos '{digits}'");
        }
        var factor = (long)Math.Pow(10, 3 - WeightDecimals);
        return long.Parse(digits, System.Globalization.CultureInfo.InvariantCulture) * factor;
    }

    protected static ScaleReading Reading(ScaleStatus status, long grams, byte[] frame, DateTimeOffset readAt) =>
        new(status, grams, Raw(frame), readAt);

    protected static ScaleStatus ZeroOrStable(long grams) => grams == 0 ? ScaleStatus.Zero : ScaleStatus.Stable;
}

/// <summary>Toledo Prix 3 / Prix 4, modo requisição: ENQ, e a resposta STX + peso + ETX.</summary>
/// <remarks>
/// Condições anômalas chegam como letras no lugar dos dígitos: <c>I</c> instável,
/// <c>S</c> sobrecarga, <c>N</c> negativo. Firmware de 6 dígitos só prefixa um
/// zero: valem os 5 <b>finais</b>.
/// </remarks>
public sealed class ToledoPrix3Protocol : ScaleProtocol
{
    public override string Name => "toledo_prix3";

    public override ScaleReading Parse(byte[] frame, DateTimeOffset readAt)
    {
        var body = Body(frame);
        if (body.Length == 0) throw new ScaleFrameException("toledo: quadro vazio");

        var upper = body.ToUpperInvariant();
        foreach (var (marker, status) in Anomalies)
        {
            if (upper.Contains(marker)) return Reading(status, 0, frame, readAt);
        }

        var digits = Digits(body);
        if (digits.Length > WeightDigits) digits = digits[^WeightDigits..];
        if (digits.Length != WeightDigits)
        {
            throw new ScaleFrameException($"toledo: esperados {WeightDigits} dígitos, recebido '{body}'");
        }

        var grams = DigitsToGrams(digits);
        return Reading(ZeroOrStable(grams), grams, frame, readAt);
    }

    private static readonly (char Marker, ScaleStatus Status)[] Anomalies =
        [('I', ScaleStatus.Unstable), ('S', ScaleStatus.Overload), ('N', ScaleStatus.Negative)];
}

/// <summary>Filizola, transmissão contínua: a balança fala sem ser perguntada.</summary>
/// <remarks>
/// Modelos com display de preço mandam campos extras (tara, preço/kg, total)
/// depois do peso: valem os 5 <b>iniciais</b>, o que serve às duas famílias.
/// </remarks>
public sealed class FilizolaProtocol : ScaleProtocol
{
    public override string Name => "filizola";

    public override byte[]? RequestFrame => null;

    public override ScaleReading Parse(byte[] frame, DateTimeOffset readAt)
    {
        var body = Body(frame);
        if (body.Length == 0) throw new ScaleFrameException("filizola: quadro vazio");

        var upper = body.ToUpperInvariant();
        if (upper.Contains('I')) return Reading(ScaleStatus.Unstable, 0, frame, readAt);
        if (upper.Contains('S')) return Reading(ScaleStatus.Overload, 0, frame, readAt);
        if (body.StartsWith('-')) return Reading(ScaleStatus.Negative, 0, frame, readAt);

        var digits = Digits(body);
        if (digits.Length < WeightDigits) throw new ScaleFrameException($"filizola: quadro curto '{body}'");

        var grams = DigitsToGrams(digits[..WeightDigits]);
        return Reading(ZeroOrStable(grams), grams, frame, readAt);
    }
}

/// <summary>Urano POP-Z / UDC, modo requisição. Parte da linha põe um byte de status antes dos dígitos.</summary>
/// <remarks>
/// <c>0</c> (ou ausente) estável, <c>1</c> instável, <c>2</c> sobrecarga,
/// <c>3</c> negativo. O byte só conta quando sobra um caractere além do peso —
/// por isso "008470" é status 0 e 8470 g, exatamente como no Python.
/// </remarks>
public sealed class UranoProtocol : ScaleProtocol
{
    public override string Name => "urano";

    public override ScaleReading Parse(byte[] frame, DateTimeOffset readAt)
    {
        var body = Body(frame);
        if (body.Length == 0) throw new ScaleFrameException("urano: quadro vazio");

        var status = ScaleStatus.Stable;
        if (body.Length == WeightDigits + 1 && StatusBytes.TryGetValue(body[0], out var reported))
        {
            status = reported;
            body = body[1..];
        }
        if (status != ScaleStatus.Stable) return Reading(status, 0, frame, readAt);

        var digits = Digits(body);
        if (digits.Length != WeightDigits)
        {
            throw new ScaleFrameException($"urano: esperados {WeightDigits} dígitos, recebido '{body}'");
        }

        var grams = DigitsToGrams(digits);
        return Reading(ZeroOrStable(grams), grams, frame, readAt);
    }

    private static readonly Dictionary<char, ScaleStatus> StatusBytes = new()
    {
        ['0'] = ScaleStatus.Stable,
        ['1'] = ScaleStatus.Unstable,
        ['2'] = ScaleStatus.Overload,
        ['3'] = ScaleStatus.Negative,
    };
}

public static class ScaleProtocols
{
    private static readonly Dictionary<string, Func<ScaleProtocol>> Registry = new(StringComparer.Ordinal)
    {
        ["toledo_prix3"] = () => new ToledoPrix3Protocol(),
        ["filizola"] = () => new FilizolaProtocol(),
        ["urano"] = () => new UranoProtocol(),
    };

    public static IReadOnlyCollection<string> Names => Registry.Keys;

    /// <summary>A fábrica pelo nome gravado em <c>device_settings</c> (<c>scale.protocol</c>).</summary>
    public static ScaleProtocol Build(string name) =>
        Registry.TryGetValue(name, out var factory)
            ? factory()
            : throw new ScaleFrameException(
                $"Protocolo de balança desconhecido: '{name}'. Disponíveis: {string.Join(", ", Registry.Keys.Order(StringComparer.Ordinal))}");

    /// <summary>
    /// O <b>último</b> quadro completo do buffer, e o que sobra dele — o
    /// <c>_extract_last_frame</c> do Python.
    /// </summary>
    /// <remarks>
    /// A balança contínua enche o buffer do sistema. O primeiro quadro pode ter
    /// minutos de idade e ser o peso do cliente anterior. O que sobra é cortado
    /// em 4096 bytes para não crescer sem fim quando ninguém lê.
    /// </remarks>
    public static (byte[]? Frame, byte[] Remaining) ExtractLastFrame(ReadOnlySpan<byte> buffer, byte start, byte end)
    {
        byte[]? last = null;
        var cursor = 0;
        while (true)
        {
            var startAt = buffer[cursor..].IndexOf(start);
            if (startAt < 0) break;
            startAt += cursor;
            var endAt = buffer[(startAt + 1)..].IndexOf(end);
            if (endAt < 0) break;
            endAt += startAt + 1;
            last = buffer[(startAt + 1)..endAt].ToArray();
            cursor = endAt + 1;
        }

        var remaining = buffer[cursor..];
        if (remaining.Length > MaxBufferBytes) remaining = remaining[^MaxBufferBytes..];
        return (last, remaining.ToArray());
    }

    public const int MaxBufferBytes = 4096;
}
