using System.Security.Cryptography;
using System.Text;

namespace Fiscal.Service;

/// <summary>O token que só a retaguarda conhece, comparado em tempo constante.</summary>
public static class BearerAuthentication
{
    public static bool Authenticate(string? offered, string expected)
    {
        if (string.IsNullOrEmpty(expected) || offered is null || !offered.StartsWith("Bearer ", StringComparison.Ordinal))
        {
            return false;
        }
        var token = offered["Bearer ".Length..].Trim();
        return token.Length > 0 &&
               CryptographicOperations.FixedTimeEquals(Encoding.UTF8.GetBytes(token), Encoding.UTF8.GetBytes(expected));
    }
}

/// <summary>Referência de segredo inválida, fora do cofre, ausente ou vazia.</summary>
/// <remarks>A mensagem nunca traz o conteúdo do segredo — só o que falta.</remarks>
public sealed class SecretException(string message) : Exception(message);

/// <summary>
/// Resolve referências (<c>loja-centro/a1.pfx</c>) dentro da pasta montada do
/// cofre, sem aceitar caminho absoluto nem escapar dela.
/// </summary>
public sealed class SecretResolver(string root)
{
    private readonly string _root = Path.GetFullPath(root);

    public string PathOf(string reference)
    {
        if (string.IsNullOrWhiteSpace(reference) || Path.IsPathRooted(reference))
        {
            throw new SecretException("Referência de segredo inválida.");
        }
        var candidate = Path.GetFullPath(Path.Combine(_root, reference));
        var relative = Path.GetRelativePath(_root, candidate);
        if (relative.StartsWith("..", StringComparison.Ordinal) || Path.IsPathRooted(relative))
        {
            throw new SecretException("Referência de segredo fora da área permitida.");
        }
        if (!File.Exists(candidate)) throw new SecretException($"Segredo fiscal não provisionado: {reference}.");
        return candidate;
    }

    public byte[] Bytes(string reference) => File.ReadAllBytes(PathOf(reference));

    public string Text(string reference)
    {
        var value = File.ReadAllText(PathOf(reference), Encoding.UTF8).Trim();
        if (value.Length == 0) throw new SecretException($"Segredo fiscal vazio: {reference}.");
        return value;
    }

    /// <summary>Texto opcional: o arquivo pode não existir (A1 sem senha).</summary>
    public string? OptionalText(string reference)
    {
        try
        {
            return File.ReadAllText(PathOf(reference), Encoding.UTF8).Trim();
        }
        catch (SecretException error) when (error.Message.StartsWith("Segredo fiscal não provisionado", StringComparison.Ordinal))
        {
            return null;
        }
    }
}
