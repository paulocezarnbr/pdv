using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text.Json;
using Fiscal.Service;
using Fiscal.Service.Nfce;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;

namespace Fiscal.Tests;

/// <summary>Uma pasta temporária por teste: estado, cofre e certificados.</summary>
public sealed class Scratch : IDisposable
{
    public Scratch()
    {
        Root = Path.Combine(Path.GetTempPath(), "fiscal-" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(Path.Combine(Root, "secrets"));
    }

    public string Root { get; }

    public string Secrets => Path.Combine(Root, "secrets");

    public string State => Path.Combine(Root, "state.sqlite3");

    public void Dispose()
    {
        Microsoft.Data.Sqlite.SqliteConnection.ClearAllPools();
        try
        {
            Directory.Delete(Root, recursive: true);
        }
        catch (IOException)
        {
        }
    }
}

/// <summary>Certificados de teste: assinam de verdade, mas a SEFAZ nunca os aceitaria.</summary>
public static class TestPki
{
    public const string Password = "senha-do-a1-de-teste";

    public static X509Certificate2 Create(DateTimeOffset? notAfter = null)
    {
        using var rsa = RSA.Create(2048);
        var request = new CertificateRequest("CN=CONFEITARIA AURORA LTDA:11222333000181", rsa, HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1);
        using var generated = request.CreateSelfSigned(DateTimeOffset.UtcNow.AddDays(-30), notAfter ?? DateTimeOffset.UtcNow.AddYears(1));
        return X509CertificateLoader.LoadPkcs12(generated.Export(X509ContentType.Pfx, Password), Password, X509KeyStorageFlags.Exportable);
    }

    /// <summary>Grava o A1 e a senha no cofre, como o técnico faria.</summary>
    public static X509Certificate2 Provision(string secrets, string reference, DateTimeOffset? notAfter = null, string? password = Password)
    {
        var certificate = Create(notAfter);
        var path = Path.Combine(secrets, reference);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllBytes(path, certificate.Export(X509ContentType.Pfx, password));
        if (password is not null) File.WriteAllText(path + ".senha", password + "\n");
        return certificate;
    }
}

public static class Intents
{
    public const string Tenant = "7b0c3a52-0000-4000-8000-000000000001";

    public static FiscalIntent Sample(
        string requestUuid = "req-1",
        string environment = "homologation",
        IReadOnlyList<FiscalItem>? items = null,
        IReadOnlyList<FiscalPayment>? payments = null,
        long totalCents = 2900,
        int taxRegime = 1,
        long number = 1)
    {
        var address = JsonDocument.Parse("""
            {"street": "Rua do Ouvidor", "number": "50", "district": "Centro", "city_code": "3304557",
             "city": "Rio de Janeiro", "zip": "20040-030"}
            """).RootElement.Clone();
        return new FiscalIntent(
            "doc-1", requestUuid, "order-1", Tenant, "store-1", "device-1", 65, 1, number, environment,
            "loja-centro/a1.pfx", null, null,
            new Issuer("RJ", "11222333000181", "12.345.678", taxRegime, "Confeitaria Aurora Ltda", address),
            totalCents,
            items ?? [Item("Fatia de torta", "2", 1450, 2900)],
            payments ?? [new FiscalPayment("cash", 5000, 2100)]);
    }

    public static FiscalItem Item(
        string name, string quantity, long unitPriceCents, long totalCents,
        string? csosn = "102", string? cstIcms = null, string cstPis = "49", string cstCofins = "49") =>
        new("p-" + name.GetHashCode().ToString("x"), name, quantity, unitPriceCents, totalCents,
            "19059090", "5102", null, "UN", 0, csosn, cstIcms, cstPis, cstCofins);

    /// <summary>O mesmo pedido que o teste em Python usava, em JSON.</summary>
    public static object PythonShaped(string requestUuid = "req-1") => new
    {
        documentId = "doc-1", requestUuid, orderId = "order-1", tenantId = "tenant", storeId = "store", deviceId = "device",
        model = 65, series = 1, number = 1, environment = "homologation",
        certificateRef = "loja/a1.pfx", cscRef = "loja/csc", cscId = "1",
        issuer = new { uf = "RJ", cnpj = "12345678000190", stateRegistration = "123", taxRegime = 1, legalName = "Loja Teste", address = new { } },
        totalCents = 700,
        items = new[]
        {
            new
            {
                productId = "p1", name = "Cafe", quantity = "1", unitPriceCents = 700, totalCents = 700, ncm = "21011200",
                cfop = "5102", unitCode = "UN", origin = 0, csosn = "102", cstPis = "49", cstCofins = "49",
            },
        },
        payments = new[] { new { method = "cash", amountCents = 700, changeCents = 0 } },
    };
}

/// <summary>O serviço de verdade (rotas, DI, JSON), com o motor e o estado trocados pelo teste.</summary>
public sealed class ServiceHost(Scratch scratch, IFiscalEngine engine, string token = "secret") : WebApplicationFactory<Program>
{
    public ResultStore Store { get; } = new(scratch.State);

    protected override void ConfigureWebHost(Microsoft.AspNetCore.Hosting.IWebHostBuilder builder) =>
        builder.ConfigureServices(services =>
        {
            services.RemoveAll<FiscalOptions>();
            services.AddSingleton(new FiscalOptions { Token = token, StateDatabase = scratch.State, SecretsDirectory = scratch.Secrets });
            services.RemoveAll<ResultStore>();
            services.AddSingleton(Store);
            services.RemoveAll<IFiscalEngine>();
            services.AddSingleton(engine);
        });
}

/// <summary>A SEFAZ de mentira: responde o roteiro do teste e guarda o que recebeu.</summary>
public sealed class FakeSefaz : ISefazGateway
{
    public Queue<Func<string, SefazAnswer>> Authorizations { get; } = new();
    public Queue<Func<string, SefazAnswer>> Queries { get; } = new();
    public List<string> Sent { get; } = [];
    public List<string> Asked { get; } = [];

    public Task<SefazAnswer> AuthorizeAsync(SefazTarget target, string signedXml, CancellationToken cancellation)
    {
        Sent.Add(signedXml);
        var key = System.Xml.Linq.XDocument.Parse(signedXml).Descendants().First(e => e.Name.LocalName == "infNFe").Attribute("Id")!.Value[3..];
        return Task.FromResult(Authorizations.Dequeue()(key));
    }

    public Task<SefazAnswer> QueryAsync(SefazTarget target, string accessKey, CancellationToken cancellation)
    {
        Asked.Add(accessKey);
        return Task.FromResult(Queries.Dequeue()(accessKey));
    }

    public static string Protocol(string key, int status = 100, string number = "333260000000001") =>
        $"<protNFe versao=\"4.00\" xmlns=\"http://www.portalfiscal.inf.br/nfe\"><infProt Id=\"ID{number}\">" +
        "<tpAmb>2</tpAmb><verAplic>SVRS202609251200</verAplic>" +
        $"<chNFe>{key}</chNFe><dhRecbto>2026-09-25T18:00:00-03:00</dhRecbto><nProt>{number}</nProt>" +
        $"<digVal>AAAAAAAAAAAAAAAAAAAAAAAAAAA=</digVal><cStat>{status}</cStat><xMotivo>Autorizado o uso da NF-e</xMotivo>" +
        "</infProt></protNFe>";

    public static Func<string, SefazAnswer> Authorized(int status = 100) =>
        key => new SefazAnswer(104, "Lote processado", status, "Autorizado o uso da NF-e", "333260000000001", Protocol(key, status));

    public static Func<string, SefazAnswer> Rejected(int status, string reason) =>
        _ => new SefazAnswer(104, "Lote processado", status, reason);

    public static Func<string, SefazAnswer> Found() =>
        key => new SefazAnswer(100, "Autorizado o uso da NF-e", 100, "Autorizado o uso da NF-e", "333260000000001", Protocol(key));

    public static Func<string, SefazAnswer> NotFound() => _ => new SefazAnswer(217, "Rejeição: NF-e não consta na base de dados da SEFAZ");

    public static Func<string, SefazAnswer> Unreachable() => _ => throw new SefazUnreachableException("SEFAZ inacessível: WebException");
}
