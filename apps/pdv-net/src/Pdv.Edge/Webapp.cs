using System.Text;

namespace Pdv.Edge;

/// <summary>O app do garçom, embutido no PDV.exe — o <c>edge/webapp</c> do Python, o mesmo arquivo.</summary>
/// <remarks>
/// Sem CDN: a loja opera sem internet, e uma tag apontando para fora faria "a
/// internet caiu" virar "o app do garçom não mostra mais nenhum aviso". Os
/// arquivos vêm dentro do executável e atualizam com ele.
/// </remarks>
internal static class Webapp
{
    /// <summary>O alarme de que alguém embutiu um framework num arquivo que carrega pelo Wi-Fi da loja.</summary>
    public const int MaxIndexBytes = 512 * 1024;

    private static readonly Lazy<string> IndexText = new(() =>
    {
        var bytes = Read("webapp/index.html") ?? throw new InvalidOperationException("index.html não foi embutido no PDV.");
        if (bytes.Length > MaxIndexBytes)
        {
            throw new InvalidOperationException(
                $"index.html passou de {MaxIndexBytes / 1024} KiB — o celular do garçom carrega isso pelo Wi-Fi da loja.");
        }
        // O `read_text` do Python: quebras de linha normalizadas. No Windows o
        // Git entrega o arquivo com CRLF, e os dois servidores precisam
        // mandar o mesmo app.
        return Encoding.UTF8.GetString(bytes).Replace("\r\n", "\n").Replace('\r', '\n');
    });

    public static string Index => IndexText.Value;

    /// <summary>Um arquivo de <c>vendor/</c>, só pelo nome exato: o nome vem da URL, e nada fora dele sai.</summary>
    public static byte[]? Vendor(string filename) =>
        filename.Contains('/') || filename.Contains('\\') || filename.Contains("..") ? null : Read($"webapp/vendor/{filename}");

    private static byte[]? Read(string name)
    {
        using var stream = typeof(Webapp).Assembly.GetManifestResourceStream(name);
        if (stream is null) return null;
        using var buffer = new MemoryStream();
        stream.CopyTo(buffer);
        return buffer.ToArray();
    }
}
