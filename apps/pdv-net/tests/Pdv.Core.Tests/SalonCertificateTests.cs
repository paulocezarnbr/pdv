using System.Net;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using Pdv.Data;
using Pdv.Data.Edge;
using Pdv.Edge;

namespace Pdv.Core.Tests;

/// <summary>O certificado do salão: gerado, reaproveitado, renovado — e o servidor falando TLS com ele.</summary>
public sealed class SalonCertificateTests : IDisposable
{
    private static readonly string[] Hosts = ["localhost", "127.0.0.1", "192.168.0.14"];

    private readonly string _directory = Directory.CreateTempSubdirectory("pdv-tls-").FullName;

    public void Dispose() => Directory.Delete(_directory, recursive: true);

    [Fact]
    public void Generates_the_same_pem_files_the_python_pdv_reads()
    {
        var material = SalonCertificate.Ensure(_directory, "Confeitaria Aurora", Hosts)!;

        Assert.StartsWith("-----BEGIN CERTIFICATE-----", File.ReadAllText(Path.Combine(_directory, "edge-cert.pem")));
        // PKCS#8 sem senha, como o `PrivateFormat.PKCS8` + `NoEncryption` do Python.
        Assert.StartsWith("-----BEGIN PRIVATE KEY-----", File.ReadAllText(Path.Combine(_directory, "edge-key.pem")));
        var certificate = material.Certificate;
        Assert.Equal("CN=Confeitaria Aurora, O=PDV Balcão", certificate.Subject);
        Assert.Equal(certificate.Subject, certificate.Issuer);
        // P-256, como o `ec.SECP256R1()` do Python.
        using var ecdsa = certificate.GetECDsaPublicKey()!;
        Assert.Equal(256, ecdsa.KeySize);
        Assert.True(certificate.Extensions.OfType<X509BasicConstraintsExtension>().Single() is { CertificateAuthority: false, Critical: true });
        Assert.Contains(certificate.Extensions.OfType<X509EnhancedKeyUsageExtension>().Single().EnhancedKeyUsages.Cast<Oid>(),
            oid => oid.Value == "1.3.6.1.5.5.7.3.1");
        var san = certificate.Extensions.OfType<X509SubjectAlternativeNameExtension>().Single();
        Assert.Equal(["localhost"], san.EnumerateDnsNames());
        Assert.Equal(["127.0.0.1", "192.168.0.14"], san.EnumerateIPAddresses().Select(a => a.ToString()));
        Assert.InRange((certificate.NotAfter - certificate.NotBefore).TotalDays, 398, 398.01);
        Assert.Matches("^([0-9a-f]{2}:){31}[0-9a-f]{2}$", material.Fingerprint);
        Assert.Matches("^[0-9A-F]{2} [0-9A-F]{2} [0-9A-F]{2} [0-9A-F]{2}$", material.ShortFingerprint);
        Assert.True(certificate.HasPrivateKey);
    }

    [Fact]
    public void Reuses_the_certificate_while_it_still_serves()
    {
        var first = SalonCertificate.Ensure(_directory, "Loja", Hosts)!;

        var again = SalonCertificate.Ensure(_directory, "Loja", Hosts)!;

        // A digital que o garçom conferiu no pareamento continua valendo.
        Assert.Equal(first.Fingerprint, again.Fingerprint);
    }

    [Fact]
    public void A_new_address_gets_a_new_certificate()
    {
        var first = SalonCertificate.Ensure(_directory, "Loja", Hosts)!;
        string[] moved = ["localhost", "127.0.0.1", "192.168.0.23"];

        var renewed = SalonCertificate.Ensure(_directory, "Loja", moved)!;

        Assert.NotEqual(first.Fingerprint, renewed.Fingerprint);
        Assert.Contains("192.168.0.23",
            renewed.Certificate.Extensions.OfType<X509SubjectAlternativeNameExtension>().Single()
                .EnumerateIPAddresses().Select(a => a.ToString()));
    }

    [Fact]
    public void Renews_a_month_before_it_expires()
    {
        var first = SalonCertificate.Ensure(_directory, "Loja", Hosts)!;
        var clock = new FakeClock(first.NotAfter.AddDays(-SalonCertificate.RenewBeforeDays).AddHours(1));

        var renewed = SalonCertificate.Ensure(_directory, "Loja", Hosts, clock)!;

        Assert.NotEqual(first.Fingerprint, renewed.Fingerprint);
    }

    [Fact]
    public void An_unreadable_certificate_is_replaced()
    {
        SalonCertificate.Ensure(_directory, "Loja", Hosts);
        File.WriteAllText(Path.Combine(_directory, "edge-cert.pem"), "lixo");

        Assert.NotNull(SalonCertificate.Ensure(_directory, "Loja", Hosts));
    }

    [Fact]
    public async Task The_salon_speaks_tls_with_it()
    {
        var material = SalonCertificate.Ensure(_directory, "Loja", Hosts)!;
        using var file = new TestDatabase(userVersion: PdvDatabase.SupportedSchemaVersion);
        using var database = new PdvDatabase(file.Path);
        var profile = new TerminalProfile("t", "s", "d", "Loja", true, null);
        var services = new SalonServices(database, profile,
            new AuditLedger("t", "s", "d", Encoding.UTF8.GetBytes("chave")), new EventHub());
        await using var server = new SalonServer(services);
        Assert.True(await server.StartAsync(IPAddress.Loopback, 0, material.Certificate));
        Assert.Equal("https", server.Scheme);

        // O celular confere a digital, não uma autoridade certificadora.
        string? seen = null;
        using var handler = new HttpClientHandler
        {
            ServerCertificateCustomValidationCallback = (_, certificate, _, _) =>
            {
                seen = Convert.ToHexStringLower(SHA256.HashData(certificate!.RawData));
                return true;
            },
        };
        using var client = new HttpClient(handler);
        using var response = await client.GetAsync($"https://127.0.0.1:{server.Port}/health");

        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        Assert.Equal(material.Fingerprint.Replace(":", ""), seen);
        // E em claro, a porta TLS não responde como se nada fosse.
        await Assert.ThrowsAnyAsync<HttpRequestException>(() => client.GetAsync($"http://127.0.0.1:{server.Port}/health"));
    }

    /// <summary>Os mesmos de <c>crosscheck.py</c>.</summary>
    private static readonly string[] CrosscheckHosts = ["localhost", "127.0.0.1"];

    /// <summary>
    /// O certificado que o Python gerou é REAPROVEITADO, não trocado: trocar
    /// mudaria a digital que o garçom conferiu no pareamento. O CI roda
    /// <c>crosscheck.py write-tls</c> antes e aponta <c>PDV_PY_TLS_DIR</c>.
    /// </summary>
    [Fact]
    public void Reuses_the_certificate_the_python_pdv_generated()
    {
        var folder = Environment.GetEnvironmentVariable("PDV_PY_TLS_DIR");
        if (string.IsNullOrEmpty(folder))
        {
            // Fora do CI não há Python com cryptography: o C# gera no formato
            // dele, e a leitura é a mesma.
            folder = Path.Combine(_directory, "py");
            var own = SalonCertificate.Ensure(folder, "Loja", CrosscheckHosts)!;
            File.WriteAllText(Path.Combine(folder, "fingerprint.txt"), own.Fingerprint);
        }
        var expected = File.ReadAllText(Path.Combine(folder, "fingerprint.txt")).Trim();

        var material = SalonCertificate.Ensure(folder, "Loja", CrosscheckHosts)!;

        Assert.Equal(expected, material.Fingerprint);
        Assert.True(material.Certificate.HasPrivateKey);
    }

    /// <summary>A volta: o certificado do C# para o <c>crosscheck.py</c> conferir que o Python o reaproveita.</summary>
    [Fact]
    public void Writes_a_certificate_for_the_python_pdv_to_reuse()
    {
        var target = Environment.GetEnvironmentVariable("PDV_CROSSCHECK_OUT");
        var folder = string.IsNullOrEmpty(target)
            ? Path.Combine(_directory, "cs")
            : Path.Combine(Path.GetDirectoryName(target)!, "tls");

        var material = SalonCertificate.Ensure(folder, "Loja", CrosscheckHosts)!;
        File.WriteAllText(Path.Combine(folder, "fingerprint.txt"), material.Fingerprint);

        Assert.True(File.Exists(Path.Combine(folder, SalonCertificate.KeyName)));
    }

    private sealed class FakeClock(DateTimeOffset now) : TimeProvider
    {
        public override DateTimeOffset GetUtcNow() => now;
    }
}
