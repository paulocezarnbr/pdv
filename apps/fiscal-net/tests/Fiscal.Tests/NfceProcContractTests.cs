using System.Reflection;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Xml.Linq;
using System.Xml.Serialization;
using Fiscal.Service;
using Fiscal.Service.Nfce;
using NFe.Classes.Informacoes.Pagamento;

namespace Fiscal.Tests;

/// <summary>
/// O <c>nfeProc</c> que o caixa recebe da retaguarda e imprime no DANFE.
/// </summary>
/// <remarks>
/// <para>
/// O serviço fiscal gera a nota e o PDV a lê — duas pontas em C# que nunca se
/// encontram num teste. <c>contracts/nfce-proc.json</c> é uma nota autorizada
/// DE VERDADE, saída deste motor (assinada, validada no XSD, com o protocolo
/// anexado como a SEFAZ o devolve), e o PDV (<c>NfceProcReaderTests</c>) a lê
/// e exige os valores de <c>expected</c>.
/// </para>
/// <para>
/// Assinatura e horário mudam a cada geração, então o arquivo não é comparado
/// byte a byte: o que se exige é a mesma FORMA — os mesmos caminhos de elemento
/// e os mesmos códigos <c>tPag</c>. Mudou o que o motor escreve, este teste
/// falha até o arquivo ser regerado, e aí o PDV o lê de novo.
/// </para>
/// <para>Para regerar: <c>FISCAL_UPDATE_CONTRACT=1 dotnet test --filter NfceProcContract</c>.</para>
/// </remarks>
public sealed class NfceProcContractTests : IDisposable
{
    private readonly Scratch _scratch = new();

    public NfceProcContractTests() => TestPki.Provision(_scratch.Secrets, "loja-centro/a1.pfx").Dispose();

    public void Dispose() => _scratch.Dispose();

    private static string ContractPath()
    {
        var directory = new DirectoryInfo(AppContext.BaseDirectory);
        while (directory is not null && !Directory.Exists(Path.Combine(directory.FullName, "contracts")))
        {
            directory = directory.Parent;
        }
        return Path.Combine(directory?.FullName ?? throw new DirectoryNotFoundException("contracts/"), "contracts", "nfce-proc.json");
    }

    /// <summary>Item pesado e unitário, desconto no pedido, Pix e dinheiro com troco.</summary>
    private static FiscalIntent Intent() => Intents.Sample(
        requestUuid: "contrato-nfce-proc",
        number: 42,
        totalCents: 6000,
        items:
        [
            Intents.Item("Torta de limão", "0.847", 5900, 4997) with { ProductId = "856b3ae4", UnitCode = "KG" },
            Intents.Item("Café coado", "2", 700, 1400) with { ProductId = "fe65fdf6" },
        ],
        payments: [new FiscalPayment("pix", 4000, 0), new FiscalPayment("cash", 2500, 500)]);

    private async Task<FiscalResult> Authorize()
    {
        var sefaz = new FakeSefaz();
        sefaz.Authorizations.Enqueue(FakeSefaz.Authorized());
        var engine = new NfceEngine(
            new FiscalOptions
            {
                SecretsDirectory = _scratch.Secrets,
                SchemasDirectory = Path.Combine(AppContext.BaseDirectory, "Schemas"),
                QrCodeVersion = 3,
            },
            new SecretResolver(_scratch.Secrets), sefaz);
        var result = await new FiscalWorkflow(new ResultStore(_scratch.State), engine).AuthorizeAsync(Intent());
        Assert.Equal(FiscalResult.Authorized, result.Status);
        return result;
    }

    /// <summary>Os códigos <c>tPag</c> como saem no XML — o PDV lê o inverso.</summary>
    private static JsonObject PaymentCodes()
    {
        var codes = new JsonObject();
        foreach (var method in new[] { "cash", "credit", "debit", "pix", "prepaid", "credit_account", "cashback" })
        {
            var value = NfceBuilder.PaymentCode(method);
            var name = typeof(FormaPagamento).GetField(value.ToString())!.GetCustomAttribute<XmlEnumAttribute>()!.Name!;
            codes[method] = name;
        }
        return codes;
    }

    /// <summary>A forma do documento: todo caminho de elemento, sem os valores.</summary>
    private static SortedSet<string> Shape(string xml)
    {
        var paths = new SortedSet<string>(StringComparer.Ordinal);
        foreach (var element in XDocument.Parse(xml).Descendants())
        {
            paths.Add(string.Join('/', element.AncestorsAndSelf().Reverse().Select(e => e.Name.LocalName)));
        }
        return paths;
    }

    [Fact]
    public async Task The_contract_is_what_the_engine_authorizes_today()
    {
        var result = await Authorize();
        var intent = Intent();
        var document = new JsonObject
        {
            ["descricao"] = "nfeProc autorizado pelo motor do fiscal-net (SEFAZ de teste). Gerado por " +
                            "apps/fiscal-net/tests/Fiscal.Tests/NfceProcContractTests.cs; não edite à mão.",
            ["xml"] = result.ProcessedXml,
            ["payment_codes"] = PaymentCodes(),
            ["expected"] = new JsonObject
            {
                ["access_key"] = result.AccessKey,
                ["protocol"] = result.Protocol,
                ["authorized_at"] = "2026-09-25T18:00:00-03:00",
                ["environment"] = "homologation",
                ["emission"] = "normal",
                ["series"] = intent.Series,
                ["number"] = intent.Number,
                ["legal_name"] = intent.Issuer.LegalName,
                ["cnpj"] = intent.Issuer.Cnpj,
                ["state_registration"] = "12345678",
                ["address"] = "Rua do Ouvidor, 50 - Centro - Rio de Janeiro/RJ - CEP 20040-030",
                // Em homologação a SEFAZ exige este texto no primeiro item, e o DANFE
                // imprime o que está na nota: é o aviso de que o papel não vale nada.
                ["items"] = new JsonArray(intent.Items.Select((item, index) => (JsonNode)new JsonObject
                {
                    ["code"] = item.ProductId,
                    ["description"] = index == 0 ? NfceBuilder.HomologationDescription : item.Name,
                    ["quantity"] = item.Quantity,
                    ["unit"] = item.UnitCode, ["unit_price_cents"] = item.UnitPriceCents, ["total_cents"] = item.TotalCents,
                }).ToArray()),
                ["payments"] = new JsonArray(intent.Payments!.Select(payment => (JsonNode)new JsonObject
                {
                    ["method"] = payment.Method, ["amount_cents"] = payment.AmountCents,
                }).ToArray()),
                ["discount_cents"] = intent.Items.Sum(item => item.TotalCents) - intent.TotalCents,
                ["change_cents"] = intent.Payments!.Sum(payment => payment.ChangeCents),
            },
        };

        var path = ContractPath();
        if (Environment.GetEnvironmentVariable("FISCAL_UPDATE_CONTRACT") == "1")
        {
            File.WriteAllText(path, document.ToJsonString(new JsonSerializerOptions
            {
                WriteIndented = true,
                Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
            }) + "\n");
        }

        Assert.True(File.Exists(path), "rode com FISCAL_UPDATE_CONTRACT=1 para gerar contracts/nfce-proc.json");
        var saved = JsonNode.Parse(File.ReadAllText(path))!;
        Assert.Equal(Shape(result.ProcessedXml!), Shape(saved["xml"]!.GetValue<string>()));
        Assert.Equal(PaymentCodes().ToJsonString(), saved["payment_codes"]!.ToJsonString());
        Assert.Equal(document["expected"]!.ToJsonString().Replace(result.AccessKey!, "")
                         .Replace(result.Protocol!, ""),
                     saved["expected"]!.ToJsonString()
                         .Replace(saved["expected"]!["access_key"]!.GetValue<string>(), "")
                         .Replace(saved["expected"]!["protocol"]!.GetValue<string>(), ""));
    }
}
