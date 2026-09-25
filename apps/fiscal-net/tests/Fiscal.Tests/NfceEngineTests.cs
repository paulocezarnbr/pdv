using Fiscal.Service;
using Fiscal.Service.Nfce;

namespace Fiscal.Tests;

/// <summary>
/// O motor NFC-e contra cada resposta que a SEFAZ pode dar — e contra a falta
/// dela. A pergunta de cada teste: a nota foi autorizada uma vez, nenhuma vez, ou
/// ficou em aberto para ser consultada, sem nunca adivinhar?
/// </summary>
public sealed class NfceEngineTests : IDisposable
{
    private readonly Scratch _scratch = new();
    private readonly FakeSefaz _sefaz = new();
    private readonly ResultStore _store;

    public NfceEngineTests()
    {
        _store = new ResultStore(_scratch.State);
        TestPki.Provision(_scratch.Secrets, "loja-centro/a1.pfx").Dispose();
    }

    public void Dispose() => _scratch.Dispose();

    private FiscalWorkflow Workflow(bool production = false, int qr = 3) => new(_store, new NfceEngine(
        new FiscalOptions
        {
            SecretsDirectory = _scratch.Secrets,
            SchemasDirectory = Path.Combine(AppContext.BaseDirectory, "Schemas"),
            ProductionEnabled = production,
            QrCodeVersion = qr,
        },
        new SecretResolver(_scratch.Secrets), _sefaz));

    [Fact]
    public async Task Authorized_the_first_time_and_never_sent_again()
    {
        _sefaz.Authorizations.Enqueue(FakeSefaz.Authorized());
        var workflow = Workflow();

        var result = await workflow.AuthorizeAsync(Intents.Sample());
        var again = await workflow.AuthorizeAsync(Intents.Sample());

        Assert.Equal(FiscalResult.Authorized, result.Status);
        Assert.Equal("100", result.Code);
        Assert.Equal(44, result.AccessKey!.Length);
        Assert.Equal("333260000000001", result.Protocol);
        Assert.StartsWith("<?xml", result.ProcessedXml);
        Assert.Contains("<nfeProc versao=\"4.00\"", result.ProcessedXml);
        Assert.Contains(System.Xml.Linq.XDocument.Parse(_sefaz.Sent[0]).Root!.ToString(System.Xml.Linq.SaveOptions.DisableFormatting)[..60], result.ProcessedXml);
        Assert.Equal(result, again);
        Assert.Single(_sefaz.Sent);
    }

    [Fact]
    public async Task Authorized_late_status_150_counts_as_authorized()
    {
        _sefaz.Authorizations.Enqueue(FakeSefaz.Authorized(150));
        var result = await Workflow().AuthorizeAsync(Intents.Sample());
        Assert.Equal((FiscalResult.Authorized, "150"), (result.Status, result.Code));
    }

    [Fact]
    public async Task A_sefaz_rejection_is_final_with_its_own_code()
    {
        _sefaz.Authorizations.Enqueue(FakeSefaz.Rejected(778, "Rejeição: Informado NCM inexistente"));
        var result = await Workflow().AuthorizeAsync(Intents.Sample());
        Assert.Equal((FiscalResult.Rejected, "778"), (result.Status, result.Code));
        Assert.Equal("Rejeição: Informado NCM inexistente", result.Reason);
        Assert.Equal(result, await Workflow().StatusAsync("req-1"));
    }

    [Fact]
    public async Task Lost_answer_then_the_query_finds_it_authorized()
    {
        _sefaz.Authorizations.Enqueue(FakeSefaz.Unreachable());
        var workflow = Workflow();

        var lost = await workflow.AuthorizeAsync(Intents.Sample());
        Assert.Equal((FiscalResult.Unknown, "SEFAZ_UNREACHABLE"), (lost.Status, lost.Code));

        _sefaz.Queries.Enqueue(FakeSefaz.Found());
        var status = await workflow.StatusAsync("req-1");

        Assert.Equal(FiscalResult.Authorized, status.Status);
        Assert.Single(_sefaz.Sent);                        // não retransmitiu
        Assert.Equal(status.AccessKey, Assert.Single(_sefaz.Asked));
        Assert.Contains("<nfeProc", status.ProcessedXml);
    }

    [Fact]
    public async Task Lost_before_reaching_sefaz_then_the_same_xml_is_sent_again()
    {
        _sefaz.Authorizations.Enqueue(FakeSefaz.Unreachable());
        var workflow = Workflow();
        await workflow.AuthorizeAsync(Intents.Sample());

        _sefaz.Queries.Enqueue(FakeSefaz.NotFound());
        _sefaz.Authorizations.Enqueue(FakeSefaz.Authorized());
        var status = await workflow.StatusAsync("req-1");

        Assert.Equal(FiscalResult.Authorized, status.Status);
        Assert.Equal(2, _sefaz.Sent.Count);
        Assert.Equal(_sefaz.Sent[0], _sefaz.Sent[1]);      // mesma chave, mesma assinatura
    }

    [Fact]
    public async Task Duplicate_key_is_resolved_by_query_not_by_assumption()
    {
        _sefaz.Authorizations.Enqueue(FakeSefaz.Rejected(204, "Rejeição: Duplicidade de NF-e"));
        _sefaz.Queries.Enqueue(FakeSefaz.Found());
        var result = await Workflow().AuthorizeAsync(Intents.Sample());
        Assert.Equal(FiscalResult.Authorized, result.Status);
        Assert.Single(_sefaz.Asked);
    }

    [Fact]
    public async Task A_paralysed_sefaz_leaves_the_request_open_for_later()
    {
        _sefaz.Authorizations.Enqueue(_ => new SefazAnswer(108, "Serviço paralisado momentaneamente"));
        var workflow = Workflow();
        var result = await workflow.AuthorizeAsync(Intents.Sample());
        Assert.Equal((FiscalResult.Unknown, "SEFAZ_PROCESSING"), (result.Status, result.Code));

        _sefaz.Queries.Enqueue(_ => throw new SefazUnreachableException("SEFAZ inacessível"));
        Assert.Equal("IN_FLIGHT", (await workflow.StatusAsync("req-1")).Code);

        _sefaz.Queries.Enqueue(FakeSefaz.Found());
        Assert.Equal(FiscalResult.Authorized, (await workflow.StatusAsync("req-1")).Status);
    }

    [Fact]
    public async Task A_denied_note_is_final()
    {
        _sefaz.Authorizations.Enqueue(FakeSefaz.Rejected(302, "Uso Denegado: Irregularidade fiscal do destinatário"));
        var result = await Workflow().AuthorizeAsync(Intents.Sample());
        Assert.Equal((FiscalResult.Rejected, "302"), (result.Status, result.Code));
    }

    [Fact]
    public async Task A_lot_level_schema_rejection_is_final()
    {
        _sefaz.Authorizations.Enqueue(_ => new SefazAnswer(225, "Rejeição: Falha no Schema XML do lote de NFe"));
        var result = await Workflow().AuthorizeAsync(Intents.Sample());
        Assert.Equal((FiscalResult.Rejected, "225"), (result.Status, result.Code));
    }

    [Fact]
    public async Task A_certificate_refused_at_the_door_is_discarded_and_signed_again_after_the_fix()
    {
        _sefaz.Authorizations.Enqueue(_ => throw new SefazCertificateRefusedException(403));
        var workflow = Workflow();

        var refused = await workflow.AuthorizeAsync(Intents.Sample());
        Assert.Equal((FiscalResult.Unknown, "FISCAL_SETUP"), (refused.Status, refused.Code));
        Assert.Contains("ICP-Brasil", refused.Reason);
        // Nada foi processado: a retaguarda pode retransmitir o mesmo número.
        Assert.Equal("NOT_FOUND", (await workflow.StatusAsync("req-1")).Code);

        // A loja troca o A1; a retransmissão assina de novo, com o certificado novo.
        TestPki.Provision(_scratch.Secrets, "loja-centro/a1.pfx").Dispose();
        _sefaz.Authorizations.Enqueue(FakeSefaz.Authorized());
        var retried = await workflow.AuthorizeAsync(Intents.Sample());

        Assert.Equal(FiscalResult.Authorized, retried.Status);
        Assert.Equal(2, _sefaz.Sent.Count);
        Assert.NotEqual(_sefaz.Sent[0], _sefaz.Sent[1]);
    }

    [Fact]
    public async Task A_certificate_refused_on_the_query_keeps_the_request_open()
    {
        _sefaz.Authorizations.Enqueue(FakeSefaz.Unreachable());
        var workflow = Workflow();
        await workflow.AuthorizeAsync(Intents.Sample());

        // A tentativa anterior pode ter passado com o certificado de antes: nada é descartado.
        _sefaz.Queries.Enqueue(_ => throw new SefazCertificateRefusedException(403));
        Assert.Equal("IN_FLIGHT", (await workflow.StatusAsync("req-1")).Code);
        _sefaz.Queries.Enqueue(FakeSefaz.Found());
        Assert.Equal(FiscalResult.Authorized, (await workflow.StatusAsync("req-1")).Status);
        Assert.Single(_sefaz.Sent);
    }

    // -- antes de reivindicar: nada consumido -------------------------------------

    [Fact]
    public async Task Missing_certificate_is_a_setup_problem_and_consumes_nothing()
    {
        var result = await Workflow().AuthorizeAsync(Intents.Sample() with { CertificateRef = "outra-loja/a1.pfx" });
        Assert.Equal((FiscalResult.Unknown, "FISCAL_SETUP"), (result.Status, result.Code));
        Assert.Contains("outra-loja/a1.pfx", result.Reason);
        Assert.Equal("NOT_FOUND", (await Workflow().StatusAsync("req-1")).Code);
        Assert.Empty(_sefaz.Sent);
    }

    [Fact]
    public async Task Wrong_password_says_so_without_the_password()
    {
        File.WriteAllText(Path.Combine(_scratch.Secrets, "loja-centro/a1.pfx.senha"), "senha-errada-super-secreta");
        var result = await Workflow().AuthorizeAsync(Intents.Sample());
        Assert.Equal("FISCAL_SETUP", result.Code);
        Assert.Contains("senha errada", result.Reason);
        Assert.DoesNotContain("senha-errada-super-secreta", result.Reason);
    }

    [Fact]
    public async Task Expired_certificate_is_refused_before_anything()
    {
        TestPki.Provision(_scratch.Secrets, "vencido/a1.pfx", notAfter: DateTimeOffset.UtcNow.AddDays(-1)).Dispose();
        var result = await Workflow().AuthorizeAsync(Intents.Sample() with { CertificateRef = "vencido/a1.pfx" });
        Assert.Equal("FISCAL_SETUP", result.Code);
        Assert.Contains("fora da validade", result.Reason);
        Assert.Empty(_sefaz.Sent);
    }

    [Fact]
    public async Task A_certificate_without_password_file_opens_when_it_has_no_password()
    {
        TestPki.Provision(_scratch.Secrets, "sem-senha/a1.pfx", password: null).Dispose();
        _sefaz.Authorizations.Enqueue(FakeSefaz.Authorized());
        var result = await Workflow().AuthorizeAsync(Intents.Sample() with { CertificateRef = "sem-senha/a1.pfx" });
        Assert.Equal(FiscalResult.Authorized, result.Status);
    }

    [Fact]
    public async Task Production_needs_its_own_switch_here_too()
    {
        var blocked = await Workflow(production: false).AuthorizeAsync(Intents.Sample(environment: "production"));
        Assert.Equal((FiscalResult.Unknown, "PRODUCTION_DISABLED"), (blocked.Status, blocked.Code));
        Assert.Empty(_sefaz.Sent);

        _sefaz.Authorizations.Enqueue(FakeSefaz.Authorized());
        var allowed = await Workflow(production: true).AuthorizeAsync(Intents.Sample(environment: "production"));
        Assert.Equal(FiscalResult.Authorized, allowed.Status);
        Assert.Contains("<tpAmb>1</tpAmb>", _sefaz.Sent[0]);
        Assert.DoesNotContain(NfceBuilder.HomologationDescription, _sefaz.Sent[0]);
    }

    [Fact]
    public async Task Taxation_that_cannot_be_computed_releases_the_request()
    {
        var intent = Intents.Sample(items: [Intents.Item("Torta", "2", 1450, 2900, csosn: "101")]);
        var result = await Workflow().AuthorizeAsync(intent);
        Assert.Equal((FiscalResult.Unknown, "INVALID_FISCAL_DATA"), (result.Status, result.Code));
        Assert.Contains("CSOSN 101", result.Reason);
        Assert.Empty(_sefaz.Sent);
        // Solto: a retaguarda pode retransmitir depois de corrigir o cadastro.
        Assert.Equal("NOT_FOUND", (await Workflow().StatusAsync("req-1")).Code);
    }

    [Fact]
    public async Task Qr_code_v2_needs_the_csc_and_uses_it()
    {
        var withoutCsc = await Workflow(qr: 2).AuthorizeAsync(Intents.Sample());
        Assert.Equal("FISCAL_SETUP", withoutCsc.Code);

        File.WriteAllText(Path.Combine(_scratch.Secrets, "loja-centro/csc"), "0123456789ABCDEF0123456789ABCDEF0123");
        _sefaz.Authorizations.Enqueue(FakeSefaz.Authorized());
        var result = await Workflow(qr: 2).AuthorizeAsync(Intents.Sample() with { CscRef = "loja-centro/csc", CscId = "1" });
        Assert.Equal(FiscalResult.Authorized, result.Status);
        Assert.Contains("|2|2|1|", _sefaz.Sent[0]);
    }

    [Fact]
    public async Task A_claim_that_died_before_signing_is_taken_over()
    {
        Assert.True(_store.Claim("req-1", "doc-1"));
        var fresh = await Workflow().AuthorizeAsync(Intents.Sample());
        Assert.Equal("PROCESSING", fresh.Code);           // recente: pode estar em voo

        _store.Age("req-1", TimeSpan.FromMinutes(3));
        Assert.Equal("NOT_FOUND", (await Workflow().StatusAsync("req-1")).Code);
        _sefaz.Authorizations.Enqueue(FakeSefaz.Authorized());
        Assert.Equal(FiscalResult.Authorized, (await Workflow().AuthorizeAsync(Intents.Sample())).Status);
    }

    [Fact]
    public async Task A_signed_request_is_never_taken_over_only_queried()
    {
        _sefaz.Authorizations.Enqueue(FakeSefaz.Unreachable());
        await Workflow().AuthorizeAsync(Intents.Sample());
        _store.Age("req-1", TimeSpan.FromHours(1));

        _sefaz.Queries.Enqueue(FakeSefaz.Found());
        var result = await Workflow().AuthorizeAsync(Intents.Sample());
        Assert.Equal(FiscalResult.Authorized, result.Status);
        Assert.Single(_sefaz.Sent);
    }
}
