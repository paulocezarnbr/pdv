using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;

namespace Pdv.Edge;

/// <summary>O certificado em uso e a digital que o caixa mostra para conferência.</summary>
public sealed record TlsMaterial(X509Certificate2 Certificate, string CertificatePath, string KeyPath, string Fingerprint,
    DateTimeOffset NotAfter, IReadOnlyList<string> Hosts)
{
    /// <summary>
    /// Os quatro primeiros blocos da digital: é o que alguém lê em voz alta e
    /// confere sem errar, olhando para um celular.
    /// </summary>
    public string ShortFingerprint => string.Join(' ', Fingerprint.Split(':').Take(4)).ToUpperInvariant();
}

/// <summary>
/// O certificado TLS autoassinado do salão — o <c>edge/tls.py</c>, nos mesmos
/// arquivos PEM, para os dois PDVs usarem o mesmo certificado na transição.
/// </summary>
/// <remarks>
/// <para>
/// A LAN da loja é a rede do Wi-Fi do cliente. Em HTTP, o token do aparelho, o
/// PIN do garçom e o do gerente passam legíveis por ela, e toda a defesa do
/// pareamento é contornada por quem só escuta. O autoassinado entrega a
/// confidencialidade; a autenticidade vem da digital, que o caixa mostra e o
/// celular confere no pareamento — a mesma âncora física do código.
/// </para>
/// <para>
/// Falhou gerar ou ler, o salão sobe em HTTP: nenhum acessório impede a venda.
/// </para>
/// </remarks>
public static class SalonCertificate
{
    /// <summary>O teto que os navegadores aceitam para certificado público.</summary>
    public const int ValidityDays = 398;

    /// <summary>Renovar antes que o certificado vire no meio de um sábado.</summary>
    public const int RenewBeforeDays = 30;

    public const string CertName = "edge-cert.pem";
    public const string KeyName = "edge-key.pem";

    /// <summary>
    /// O certificado do terminal: o que está em disco enquanto valer e cobrir os
    /// endereços pedidos, ou um novo. O IP vem de DHCP e muda quando o roteador
    /// reinicia; certificado do endereço antigo o celular recusa com um erro que
    /// ninguém no balcão sabe ler.
    /// </summary>
    /// <returns><c>null</c> quando não deu: o salão sobe em HTTP.</returns>
    public static TlsMaterial? Ensure(string directory, string storeName, IReadOnlyList<string> hosts,
        TimeProvider? clock = null, Action<string>? log = null)
    {
        clock ??= TimeProvider.System;
        try
        {
            Directory.CreateDirectory(directory);
            var certPath = Path.Combine(directory, CertName);
            var keyPath = Path.Combine(directory, KeyName);
            return Load(certPath, keyPath, hosts, clock, log) ?? Generate(certPath, keyPath, storeName, hosts, clock, log);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or CryptographicException)
        {
            log?.Invoke($"Salão: certificado indisponível, o salão sobe em HTTP: {error.Message}");
            return null;
        }
    }

    private static TlsMaterial? Load(string certPath, string keyPath, IReadOnlyList<string> hosts, TimeProvider clock,
        Action<string>? log)
    {
        if (!File.Exists(certPath) || !File.Exists(keyPath)) return null;
        X509Certificate2 certificate;
        try
        {
            certificate = X509Certificate2.CreateFromPemFile(certPath, keyPath);
        }
        catch (CryptographicException)
        {
            log?.Invoke("Salão: certificado ilegível; um novo será gerado.");
            return null;
        }
        if (certificate.NotAfter.ToUniversalTime() <= clock.GetUtcNow().UtcDateTime.AddDays(RenewBeforeDays))
        {
            log?.Invoke("Salão: certificado perto de vencer; renovando.");
            return null;
        }
        if (!Covers(certificate, hosts))
        {
            log?.Invoke("Salão: o endereço da loja mudou; emitindo certificado novo.");
            return null;
        }
        return Material(certificate, certPath, keyPath, hosts);
    }

    private static TlsMaterial Generate(string certPath, string keyPath, string storeName, IReadOnlyList<string> hosts,
        TimeProvider clock, Action<string>? log)
    {
        using var key = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        var name = storeName.Length == 0 ? "PDV" : new string([.. storeName.EnumerateRunes().Take(64).SelectMany(r => r.ToString())]);
        var subject = new X500DistinguishedNameBuilder();
        subject.AddCommonName(name);
        subject.AddOrganizationName("PDV Balcão");
        var request = new CertificateRequest(subject.Build(), key, HashAlgorithmName.SHA256);
        var san = new SubjectAlternativeNameBuilder();
        foreach (var host in hosts)
        {
            if (IPAddress.TryParse(host, out var address)) san.AddIpAddress(address);
            else san.AddDnsName(host);
        }
        request.CertificateExtensions.Add(san.Build());
        request.CertificateExtensions.Add(new X509BasicConstraintsExtension(false, false, 0, true));
        request.CertificateExtensions.Add(new X509EnhancedKeyUsageExtension([new Oid("1.3.6.1.5.5.7.3.1")], false));

        var now = clock.GetUtcNow();
        // Um minuto para trás: relógio de loja atrasa, e "ainda não vale" é recusado igual a vencido.
        using var created = request.CreateSelfSigned(now.AddMinutes(-1), now.AddDays(ValidityDays));
        File.WriteAllText(certPath, created.ExportCertificatePem());
        // Sem senha: quem lê é o próprio processo, sem ninguém para digitar. Uma
        // senha ao lado da chave no mesmo disco não protegeria nada.
        WritePrivate(keyPath, key.ExportPkcs8PrivateKeyPem());
        log?.Invoke($"Salão: certificado gerado para {string.Join(", ", hosts)} (válido até {created.NotAfter:yyyy-MM-dd}).");
        return Material(X509Certificate2.CreateFromPemFile(certPath, keyPath), certPath, keyPath, hosts);
    }

    private static bool Covers(X509Certificate2 certificate, IReadOnlyList<string> hosts)
    {
        var extension = certificate.Extensions.OfType<X509SubjectAlternativeNameExtension>().FirstOrDefault();
        if (extension is null) return false;
        var present = new HashSet<string>(extension.EnumerateDnsNames(), StringComparer.Ordinal);
        foreach (var address in extension.EnumerateIPAddresses()) present.Add(address.ToString());
        return hosts.All(present.Contains);
    }

    private static TlsMaterial Material(X509Certificate2 certificate, string certPath, string keyPath, IReadOnlyList<string> hosts)
    {
        // A chave lida de PEM é efêmera, e o SChannel do Windows recusa chave
        // efêmera no servidor ("no credentials are available"). Passar por
        // PKCS#12 dá a ela um contêiner — no Linux não muda nada.
        var usable = X509CertificateLoader.LoadPkcs12(certificate.Export(X509ContentType.Pkcs12), null);
        var digest = SHA256.HashData(certificate.RawData);
        return new TlsMaterial(usable, certPath, keyPath,
            string.Join(':', digest.Select(b => b.ToString("x2", System.Globalization.CultureInfo.InvariantCulture))),
            new DateTimeOffset(certificate.NotAfter.ToUniversalTime(), TimeSpan.Zero), hosts);
    }

    /// <summary>
    /// A permissão mais fechada que der. No Windows isto não tira ninguém do
    /// ACL, nem pretende: quem tem a máquina tem o banco, a chave e o processo.
    /// A chave protege o tráfego na rede, contra quem não está na máquina.
    /// </summary>
    private static void WritePrivate(string path, string pem)
    {
        File.WriteAllText(path, pem);
        if (!OperatingSystem.IsWindows()) File.SetUnixFileMode(path, UnixFileMode.UserRead | UnixFileMode.UserWrite);
    }

    /// <summary>
    /// Os endereços pelos quais o app chega a este terminal: <c>localhost</c> e
    /// <c>127.0.0.1</c> para o KDS na própria máquina, e o IP da LAN para os
    /// celulares. Ordem estável, para a conferência de cobertura.
    /// </summary>
    public static IReadOnlyList<string> DefaultHosts()
    {
        var hosts = new List<string> { "localhost", "127.0.0.1" };
        var address = LocalIpAddress();
        if (!hosts.Contains(address)) hosts.Add(address);
        return hosts;
    }

    /// <summary>
    /// O IP desta máquina na LAN — o truque do socket UDP do Python: não envia
    /// nada, só pergunta à pilha qual interface sairia. Mais confiável que
    /// resolver pelo nome numa máquina com Wi-Fi, cabo e adaptador virtual.
    /// </summary>
    public static string LocalIpAddress()
    {
        try
        {
            using var socket = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp);
            socket.Connect("8.8.8.8", 80);
            return ((IPEndPoint)socket.LocalEndPoint!).Address.ToString();
        }
        catch (SocketException)
        {
            // Loja com a internet caída ainda tem LAN; sem rota nenhuma, sobra a própria máquina.
            return "127.0.0.1";
        }
    }
}
