using System.Globalization;
using System.Text;

namespace Pdv.Core.Printing;

public static class Align
{
    public const byte Left = 0;
    public const byte Center = 1;
    public const byte Right = 2;
}

/// <summary>
/// O payload ESC/POS da Epson TM-T20X (80 mm), montado byte a byte — o
/// <c>EscPosBuilder</c> do Python, conferido contra <c>contracts/receipts.json</c>.
/// </summary>
/// <remarks>
/// <para>
/// Bytes puros e determinísticos: o cupom inteiro é testável sem impressora.
/// Cupom errado só aparece no papel, geralmente na frente do cliente.
/// </para>
/// <para>
/// Duas armadilhas de linguagem ficam aqui. A primeira é a PC850 com "?" para
/// o que não existe nela: o .NET, por padrão, faria "best fit" (€ → E). A
/// segunda é contar em pontos de código, como o Python: em UTF-16 um emoji
/// conta dois, e o nome do produto sairia cortado ou desalinhado.
/// </para>
/// </remarks>
public sealed class EscPosBuilder
{
    public const byte Esc = 0x1B;
    public const byte Gs = 0x1D;
    public const byte Lf = 0x0A;
    public const int DefaultColumns = 48;
    public const byte CodepagePc850 = 2;

    private static readonly Encoding Pc850 = CreatePc850();

    private readonly List<byte> _buffer = [];
    private readonly byte _codepage;

    public EscPosBuilder(int columns = DefaultColumns, byte codepage = CodepagePc850)
    {
        Columns = columns;
        _codepage = codepage;
    }

    public int Columns { get; }

    private static Encoding CreatePc850()
    {
        Encoding.RegisterProvider(CodePagesEncodingProvider.Instance);
        return Encoding.GetEncoding(850, new EncoderReplacementFallback("?"), DecoderFallback.ReplacementFallback);
    }

    public byte[] Build() => [.. _buffer];

    private EscPosBuilder Raw(params byte[] values)
    {
        _buffer.AddRange(values);
        return this;
    }

    /// <summary>ESC @ e a code page. Sem o reset, o cupom herda o negrito de uma impressão que falhou no meio.</summary>
    public EscPosBuilder Initialize() => Raw(Esc, 0x40).Raw(Esc, 0x74, _codepage);

    public EscPosBuilder AlignTo(byte mode) => Raw(Esc, 0x61, mode);

    public EscPosBuilder Bold(bool enabled = true) => Raw(Esc, 0x45, (byte)(enabled ? 1 : 0));

    public EscPosBuilder Underline(bool enabled = true) => Raw(Esc, 0x2D, (byte)(enabled ? 1 : 0));

    /// <summary>GS ! — multiplicador de 1 a 8 em cada eixo.</summary>
    public EscPosBuilder Size(int width = 1, int height = 1)
    {
        var w = Math.Clamp(width, 1, 8) - 1;
        var h = Math.Clamp(height, 1, 8) - 1;
        return Raw(Gs, 0x21, (byte)((w << 4) | h));
    }

    public EscPosBuilder ResetStyle() => Bold(false).Underline(false).Size(1, 1).AlignTo(Align.Left);

    /// <summary>Texto na PC850. Acento que não existe vira "?": nome exótico não impede o cupom.</summary>
    public EscPosBuilder Text(string value)
    {
        foreach (var rune in value.EnumerateRunes())
        {
            // Fora da BMP (emoji) o .NET trocaria o par por "??"; o Python, por um "?".
            if (rune.IsBmp) _buffer.AddRange(Pc850.GetBytes(rune.ToString()));
            else _buffer.Add((byte)'?');
        }
        return this;
    }

    public EscPosBuilder Line(string value = "") => Text(value).Raw(Lf);

    public EscPosBuilder Feed(int lines = 1) => Raw(Esc, 0x64, (byte)Math.Clamp(lines, 0, 255));

    public EscPosBuilder Separator(char fill = '-') => Line(new string(fill, Columns));

    /// <summary>Duas colunas. O valor da direita é dinheiro: se falta espaço, quem perde é o texto.</summary>
    public EscPosBuilder Columns2(string left, string right, char filler = ' ')
    {
        var available = Columns - Length(right);
        if (available < 1) return Line(Slice(right, Columns));
        var leftText = Length(left) > available ? Slice(left, available) : left;
        return Line(leftText + new string(filler, available - Length(leftText)) + right);
    }

    /// <summary>Três colunas: descrição, quantidade, total.</summary>
    public EscPosBuilder Columns3(string left, string middle, string right)
    {
        var leftWidth = Columns - Length(right) - Length(middle) - 2;
        if (leftWidth < 1) return Columns2(left, right);
        var leftText = Slice(left, leftWidth);
        leftText += new string(' ', leftWidth - Length(leftText));
        return Line($"{leftText} {middle} {right}");
    }

    public EscPosBuilder Centered(string value) => AlignTo(Align.Center).Line(value).AlignTo(Align.Left);

    /// <summary>
    /// GS V — guilhotina. Parcial por padrão: a ponte de papel segura o cupom
    /// até o cliente puxar. O avanço antes é obrigatório: a lâmina fica acima
    /// da cabeça térmica.
    /// </summary>
    public EscPosBuilder Cut(int feedLines = 4, bool partial = true) =>
        Raw(Gs, 0x56, (byte)(partial ? 66 : 65), (byte)Math.Clamp(feedLines, 0, 255));

    /// <summary>ESC p — pulso na gaveta pelo conector DK. Pino 2 é o padrão; tempos em unidades de 2 ms.</summary>
    public EscPosBuilder OpenDrawer(int pin = 2, int onMs = 25, int offMs = 250) =>
        Raw(Esc, 0x70, (byte)(pin == 2 ? 0 : 1), (byte)Math.Clamp(onMs / 2, 1, 255), (byte)Math.Clamp(offMs / 2, 1, 255));

    /// <summary>QR Code nativo (GS ( k), modelo 2, correção M — o da NFC-e.</summary>
    public EscPosBuilder QrCode(string data, byte moduleSize = 6)
    {
        var payload = Encoding.UTF8.GetBytes(data);
        var length = payload.Length + 3;
        Raw(Gs, 0x28, 0x6B, 0x04, 0x00, 0x31, 0x41, 0x32, 0x00);
        Raw(Gs, 0x28, 0x6B, 0x03, 0x00, 0x31, 0x43, moduleSize);
        Raw(Gs, 0x28, 0x6B, 0x03, 0x00, 0x31, 0x45, 0x31);
        Raw(Gs, 0x28, 0x6B, (byte)(length & 0xFF), (byte)((length >> 8) & 0xFF), 0x31, 0x50, 0x30);
        _buffer.AddRange(payload);
        return Raw(Gs, 0x28, 0x6B, 0x03, 0x00, 0x31, 0x51, 0x30);
    }

    // -- como o Python conta ------------------------------------------------

    /// <summary>Tamanho em pontos de código (o <c>len()</c> do Python), não em UTF-16.</summary>
    public static int Length(string value) => value.EnumerateRunes().Count();

    /// <summary>Os primeiros <paramref name="count"/> pontos de código (o <c>s[:n]</c> do Python).</summary>
    public static string Slice(string value, int count) =>
        count <= 0 ? "" : string.Concat(value.EnumerateRunes().Take(count).Select(rune => rune.ToString()));

    // -- formatos ----------------------------------------------------------

    /// <summary>Centavos → "1.234,56", sem passar por ponto flutuante.</summary>
    public static string FormatCents(long cents)
    {
        var value = Math.Abs(cents);
        var text = (value / 100).ToString("#,0", CultureInfo.InvariantCulture).Replace(',', '.') + "," +
                   (value % 100).ToString("00", CultureInfo.InvariantCulture);
        return cents < 0 ? "-" + text : text;
    }

    /// <summary>Gramas → "0,847 kg".</summary>
    public static string FormatGrams(long grams)
    {
        var value = Math.Abs(grams);
        return $"{(grams < 0 ? "-" : "")}{value / 1000},{value % 1000:000} kg";
    }
}
