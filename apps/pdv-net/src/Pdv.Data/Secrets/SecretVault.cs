using System.Security.Cryptography;
using System.Text;

namespace Pdv.Data.Secrets;

public sealed class SecretVaultException(string message) : Exception(message);

/// <summary>
/// O cofre de segredos do terminal — o mesmo arquivo do <c>SecretVault</c> do Python.
/// </summary>
/// <remarks>
/// <para>
/// Cada segredo é <c>secrets/&lt;nome&gt;.bin</c>: um blob DPAPI em escopo de
/// MÁQUINA com a entropia <c>ERPFood.PDV.v1</c> (ou <c>PLAIN:</c>+base64, o
/// modo de desenvolvimento do Python fora do Windows). Escopo de máquina porque
/// o caixa roda como o operador do turno, e o segredo do terminal é do
/// terminal, não de quem entrou.
/// </para>
/// <para>
/// DPAPI prende o blob à máquina, então não há contrato em arquivo: o CI grava
/// pelo Python e lê pelo C#, e o contrário (<c>crosscheck.py</c>).
/// </para>
/// </remarks>
public sealed class SecretVault(string directory)
{
    public const int DeviceSecretLength = 32;

    private static readonly byte[] Entropy = Encoding.ASCII.GetBytes("ERPFood.PDV.v1");
    private static readonly byte[] PlainPrefix = Encoding.ASCII.GetBytes("PLAIN:");

    public string Directory { get; } = directory;

    private string PathOf(string name) => Path.Combine(Directory, name + ".bin");

    public bool Exists(string name) => File.Exists(PathOf(name));

    /// <returns>O segredo, ou <c>null</c> se não existe.</returns>
    /// <exception cref="SecretVaultException">Existe e não decifra (outra máquina, arquivo corrompido).</exception>
    public byte[]? Load(string name)
    {
        var path = PathOf(name);
        if (!File.Exists(path)) return null;

        var blob = File.ReadAllBytes(path);
        if (blob.AsSpan().StartsWith(PlainPrefix))
        {
            return Convert.FromBase64String(Encoding.ASCII.GetString(blob, PlainPrefix.Length, blob.Length - PlainPrefix.Length));
        }
        try
        {
            return ProtectedData.Unprotect(blob, Entropy, DataProtectionScope.LocalMachine);
        }
        catch (CryptographicException)
        {
            throw new SecretVaultException(
                $"O segredo '{name}' deste terminal existe mas não pôde ser lido. " +
                "Não reinstale nem apague a pasta de dados: chame o suporte.");
        }
    }

    /// <summary>Grava pelo arquivo temporário: uma queda no meio não deixa um segredo pela metade.</summary>
    public void Store(string name, byte[] value)
    {
        var blob = ProtectedData.Protect(value, Entropy, DataProtectionScope.LocalMachine);
        var temp = PathOf(name) + ".tmp";
        try
        {
            System.IO.Directory.CreateDirectory(Directory);
            File.WriteAllBytes(temp, blob);
            File.Move(temp, PathOf(name), overwrite: true);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            // Quase sempre permissão da pasta de dados: o balcão precisa do
            // caminho, não do nome da exceção.
            throw new SecretVaultException(
                $"Não foi possível gravar o segredo '{name}' em {Directory}: {error.Message} " +
                "Confira as permissões da pasta de dados do PDV (reinstale pelo instalador, opção Reparar).");
        }
    }

    /// <summary>
    /// O segredo que assina a cadeia de auditoria. Existe e não decifra → erro,
    /// nunca um segredo novo: recriar faria a cadeia inteira acusar adulteração.
    /// </summary>
    public byte[] EnsureDeviceSecret()
    {
        if (Exists("device_secret"))
        {
            return Load("device_secret")!;
        }
        var generated = RandomNumberGenerator.GetBytes(DeviceSecretLength);
        Store("device_secret", generated);
        return generated;
    }
}
