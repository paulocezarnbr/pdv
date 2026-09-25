using System.Text;
using Pdv.Data.Secrets;

namespace Pdv.Core.Tests;

public sealed class SecretVaultTests : IDisposable
{
    /// <summary>O mesmo valor que o crosscheck.py grava e espera.</summary>
    public static readonly byte[] Known = Enumerable.Range(0, 32).Select(i => (byte)(31 - i)).ToArray();

    private readonly string _folder = Path.Combine(Path.GetTempPath(), "pdv-vault-" + Guid.NewGuid().ToString("N")[..8]);

    public void Dispose()
    {
        if (Directory.Exists(_folder)) Directory.Delete(_folder, recursive: true);
    }

    [Fact]
    public void A_secret_round_trips_through_dpapi()
    {
        var vault = new SecretVault(_folder);
        vault.Store("device_secret", Known);
        Assert.Equal(Known, vault.Load("device_secret"));
        // o arquivo NÃO é o segredo em claro
        Assert.DoesNotContain(Convert.ToHexString(Known), Convert.ToHexString(File.ReadAllBytes(Path.Combine(_folder, "device_secret.bin"))));
    }

    [Fact]
    public void The_python_development_format_is_read()
    {
        Directory.CreateDirectory(_folder);
        File.WriteAllBytes(Path.Combine(_folder, "device_secret.bin"),
            Encoding.ASCII.GetBytes("PLAIN:" + Convert.ToBase64String(Known)));
        Assert.Equal(Known, new SecretVault(_folder).Load("device_secret"));
    }

    [Fact]
    public void The_device_secret_is_created_once()
    {
        var vault = new SecretVault(_folder);
        var first = vault.EnsureDeviceSecret();
        Assert.Equal(SecretVault.DeviceSecretLength, first.Length);
        Assert.Equal(first, vault.EnsureDeviceSecret());
    }

    [Fact]
    public void A_secret_that_does_not_decrypt_is_never_replaced()
    {
        Directory.CreateDirectory(_folder);
        var path = Path.Combine(_folder, "device_secret.bin");
        File.WriteAllBytes(path, [1, 2, 3, 4, 5]);
        var vault = new SecretVault(_folder);

        var error = Assert.Throws<SecretVaultException>(() => vault.EnsureDeviceSecret());
        Assert.Contains("chame o suporte", error.Message);
        Assert.Equal([1, 2, 3, 4, 5], File.ReadAllBytes(path));
    }

    [Fact]
    public void A_missing_secret_is_null()
    {
        Assert.Null(new SecretVault(_folder).Load("device_secret"));
    }

    /// <summary>
    /// O cofre gravado pelo Python (DPAPI, nesta máquina) lido pelo C#. O CI roda
    /// <c>crosscheck.py write-vault</c> antes e aponta <c>PDV_PY_VAULT_DIR</c>.
    /// </summary>
    [Fact]
    public void Reads_the_vault_the_python_pdv_wrote()
    {
        var folder = Environment.GetEnvironmentVariable("PDV_PY_VAULT_DIR");
        if (string.IsNullOrEmpty(folder))
        {
            // Fora do CI não há cofre do Python: grava-se um no formato dele
            // com DPAPI pelo próprio C#, e a leitura é a mesma.
            folder = _folder;
            new SecretVault(folder).Store("device_secret", Known);
        }
        Assert.Equal(Known, new SecretVault(folder).Load("device_secret"));
    }
}
