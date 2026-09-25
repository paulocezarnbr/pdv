using System.Globalization;

namespace Pdv.WinUI;

/// <summary>
/// Onde vai o erro que ninguém tratou. O <c>PDV.exe</c> não tem console: sem
/// isto, "abri e sumiu" chega ao suporte sem uma linha de explicação.
/// </summary>
/// <remarks>
/// No perfil do usuário (<c>%LOCALAPPDATA%\ERPFood\PDV</c>), e não em
/// <c>ProgramData\...\logs</c>: aquela pasta é append-only para o operador, e
/// o registro do motivo de uma queda não pode depender da permissão que talvez
/// seja o próprio motivo. <c>PDV_CRASH_LOG</c> aponta outro arquivo (o teste
/// de UI usa).
/// </remarks>
internal static class CrashLog
{
    public static string Path =>
        Environment.GetEnvironmentVariable("PDV_CRASH_LOG") is { Length: > 0 } custom
            ? custom
            : System.IO.Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "ERPFood", "PDV", "pdv-winui.log");

    public static void Write(string context, Exception? error)
    {
        try
        {
            Directory.CreateDirectory(System.IO.Path.GetDirectoryName(Path)!);
            File.AppendAllText(Path, string.Create(CultureInfo.InvariantCulture,
                $"{DateTimeOffset.Now:yyyy-MM-dd HH:mm:ss} {context}: {error}{Environment.NewLine}"));
        }
        catch (IOException)
        {
            // Sem onde escrever: não existe próximo recurso, e derrubar o caixa
            // por não conseguir registrar a queda seria a segunda queda.
        }
        catch (UnauthorizedAccessException)
        {
        }
    }
}
