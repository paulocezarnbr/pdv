using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using Pdv.App;
using Pdv.Core.Printing;
using Pdv.Data;
using Pdv.Data.Sales;
using Pdv.Data.Fiscal;

namespace Pdv.Core.Tests;

/// <summary>
/// A NFC-e pedida pelo caixa (C6d). A pergunta de cada teste: saiu uma nota,
/// nenhuma, ou ficou em aberto para ser consultada — sem nunca pedir uma
/// segunda com outro uuid?
/// </summary>
public sealed class FiscalIssuanceTests : IDisposable
{
    private static readonly JsonElement Contract =
        JsonDocument.Parse(File.ReadAllText(TestDatabase.Contract("nfce-proc.json"))).RootElement.Clone();

    private static readonly TimeZoneInfo Rio = TimeZoneInfo.CreateCustomTimeZone("Rio", TimeSpan.FromHours(-3), "Rio", "Rio");

    private readonly TestDatabase _file = new(userVersion: PdvDatabase.SupportedSchemaVersion);
    private readonly PdvDatabase _database;
    private readonly ManualClock _clock = new(DateTimeOffset.Parse("2026-09-26T12:00:00Z"));
    private readonly ScriptedGateway _cloud = new();

    public FiscalIssuanceTests() => _database = new PdvDatabase(_file.Path);

    public void Dispose()
    {
        _database.Dispose();
        _file.Dispose();
    }

    private FiscalIssuance Service() => new(_database, _cloud, Rio, _clock);

    private string PaidOrder(long totalCents = 6000)
    {
        var id = Guid.NewGuid().ToString();
        _database.Execute(
            "INSERT INTO orders (id, tenant_id, store_id, device_id, local_number, status, operator_id, total_cents, " +
            "opened_at, closed_at, created_at, updated_at, origin_device_id, client_uuid) VALUES " +
            "($id, 't', 's', 'd', 1, 'paid', 'op', $total, 'x', 'x', 'x', 'x', 'd', $id)",
            ("$id", id), ("$total", totalCents));
        return id;
    }

    private static CloudFiscalDocument Authorized(string uuid, string order) =>
        new(uuid, order, "authorized", 1, 42, Contract.GetProperty("expected").GetProperty("access_key").GetString(),
            "333260000000001", "Autorizado", Contract.GetProperty("xml").GetString());

    private string RequestUuid(string order) =>
        (string)_database.Scalar("SELECT request_uuid FROM fiscal_requests WHERE order_id = $id", ("$id", order))!;

    [Fact]
    public async Task Authorized_at_the_counter_prints_the_danfe()
    {
        var order = PaidOrder();
        _cloud.Issues.Enqueue((uuid, id) => Authorized(uuid, id));
        var pushed = 0;

        var outcome = await Service().RequestAsync(order, _ => { pushed++; return Task.CompletedTask; });

        Assert.Equal(FiscalOutcomeKind.Authorized, outcome.Kind);
        Assert.NotNull(outcome.Danfe);
        Assert.Equal(42, outcome.Danfe!.Number);
        Assert.Equal(1, pushed); // a venda precisa estar na nuvem antes do pedido
        Assert.NotEmpty(NfceDanfeLayout.Build(outcome.Danfe, new PrinterLayout()));
    }

    [Fact]
    public async Task Asking_again_for_the_same_sale_reuses_the_uuid_and_never_calls_again_once_decided()
    {
        var order = PaidOrder();
        _cloud.Issues.Enqueue((uuid, id) => Authorized(uuid, id));
        var service = Service();

        await service.RequestAsync(order);
        var again = await service.RequestAsync(order);

        Assert.Equal(FiscalOutcomeKind.Authorized, again.Kind);
        Assert.Single(_cloud.IssuedWith);
    }

    [Fact]
    public async Task A_courtesy_sale_asks_for_nothing_even_online()
    {
        var order = PaidOrder(totalCents: 0);

        var outcome = await Service().RequestAsync(order);

        Assert.Equal(FiscalOutcomeKind.NotRequired, outcome.Kind);
        Assert.Empty(_cloud.IssuedWith);
    }

    [Fact]
    public async Task Offline_prints_the_receipt_and_asks_again_later_with_the_same_uuid()
    {
        var order = PaidOrder();
        _cloud.Issues.Enqueue((_, _) => throw new FiscalOfflineException("sem rede"));
        var service = Service();

        var counter = await service.RequestAsync(order);
        Assert.Equal(FiscalOutcomeKind.Pending, counter.Kind);
        Assert.Empty(await service.CheckDueAsync()); // ainda na espera

        _clock.Advance(TimeSpan.FromMinutes(1));
        _cloud.Issues.Enqueue((uuid, id) => Authorized(uuid, id));
        var later = await service.CheckDueAsync();

        Assert.Equal(FiscalOutcomeKind.Authorized, Assert.Single(later).Kind);
        Assert.Equal(2, _cloud.IssuedWith.Count);
        Assert.All(_cloud.IssuedWith, uuid => Assert.Equal(RequestUuid(order), uuid));
    }

    [Fact]
    public async Task An_ambiguous_answer_is_only_ever_queried_never_issued_again()
    {
        var order = PaidOrder();
        _cloud.Issues.Enqueue((_, _) => throw new FiscalResultUnknownException("timeout"));
        var service = Service();

        Assert.Equal(FiscalOutcomeKind.Pending, (await service.RequestAsync(order)).Kind);
        _clock.Advance(TimeSpan.FromMinutes(1));
        _cloud.Statuses.Enqueue((uuid, id) => Authorized(uuid, id));
        var later = await service.CheckDueAsync();

        Assert.Equal(FiscalOutcomeKind.Authorized, Assert.Single(later).Kind);
        Assert.Single(_cloud.IssuedWith);
        Assert.Equal([RequestUuid(order)], _cloud.QueriedWith);
    }

    [Fact]
    public async Task A_query_the_cloud_never_saw_is_issued_again_with_the_same_uuid()
    {
        var order = PaidOrder();
        _cloud.Issues.Enqueue((_, _) => throw new FiscalResultUnknownException("caiu antes de chegar"));
        var service = Service();
        await service.RequestAsync(order);

        _clock.Advance(TimeSpan.FromMinutes(1));
        _cloud.Statuses.Enqueue((_, _) => throw new FiscalRefusedException(404, "Solicitação fiscal não encontrada."));
        _cloud.Issues.Enqueue((uuid, id) => Authorized(uuid, id));
        await service.CheckDueAsync();

        Assert.Equal(FiscalOutcomeKind.Authorized, service.Outcome(order).Kind);
        Assert.Equal([RequestUuid(order), RequestUuid(order)], _cloud.IssuedWith);
    }

    [Fact]
    public async Task Still_processing_keeps_asking_with_growing_waits()
    {
        var order = PaidOrder();
        _cloud.Issues.Enqueue((uuid, id) => new CloudFiscalDocument(uuid, id, "unknown", 1, 42));
        var service = Service();
        await service.RequestAsync(order);

        for (var round = 0; round < 3; round++)
        {
            _cloud.Statuses.Enqueue((uuid, id) => new CloudFiscalDocument(uuid, id, "processing", 1, 42));
            _clock.Advance(TimeSpan.FromHours(2));
            Assert.Empty(await service.CheckDueAsync());
        }

        var next = DateTimeOffset.Parse((string)_database.Scalar(
            "SELECT next_check_at FROM fiscal_requests WHERE order_id = $id", ("$id", order))!);
        Assert.True(next - _clock.GetUtcNow() > TimeSpan.FromMinutes(1));
        Assert.True(next - _clock.GetUtcNow() <= TimeSpan.FromHours(1));
    }

    [Fact]
    public async Task A_rejection_is_final_and_says_why()
    {
        var order = PaidOrder();
        _cloud.Issues.Enqueue((uuid, id) => new CloudFiscalDocument(uuid, id, "rejected", 1, 42, Reason: "Rejeição 539: duplicidade"));
        var service = Service();

        var outcome = await service.RequestAsync(order);
        _clock.Advance(TimeSpan.FromHours(2));

        Assert.Equal(FiscalOutcomeKind.Rejected, outcome.Kind);
        Assert.Contains("duplicidade", outcome.Notice);
        Assert.Empty(await service.CheckDueAsync());
    }

    [Fact]
    public async Task An_answer_for_another_request_decides_nothing()
    {
        var order = PaidOrder();
        _cloud.Issues.Enqueue((_, id) => Authorized(Guid.NewGuid().ToString(), id));

        var outcome = await Service().RequestAsync(order);

        Assert.Equal(FiscalOutcomeKind.Pending, outcome.Kind);
        Assert.Equal("unknown", _database.Scalar("SELECT status FROM fiscal_requests WHERE order_id = $id", ("$id", order)));
    }

    [Fact]
    public async Task Authorized_without_the_xml_waits_for_it_instead_of_printing_nothing()
    {
        var order = PaidOrder();
        _cloud.Issues.Enqueue((uuid, id) => Authorized(uuid, id) with { ProcessedXml = null });

        var outcome = await Service().RequestAsync(order);

        Assert.Equal(FiscalOutcomeKind.Pending, outcome.Kind);
    }

    [Fact]
    public async Task A_sale_not_yet_in_the_cloud_is_asked_again_later()
    {
        var order = PaidOrder();
        _cloud.Issues.Enqueue((_, _) => throw new FiscalRefusedException(404, "Venda paga não encontrada neste terminal."));
        var service = Service();

        Assert.Equal(FiscalOutcomeKind.Pending, (await service.RequestAsync(order)).Kind);
        _clock.Advance(TimeSpan.FromMinutes(1));
        _cloud.Issues.Enqueue((uuid, id) => Authorized(uuid, id));

        Assert.Equal(FiscalOutcomeKind.Authorized, Assert.Single(await service.CheckDueAsync()).Kind);
    }

    [Fact]
    public async Task A_failing_push_does_not_stop_the_request()
    {
        var order = PaidOrder();
        _cloud.Issues.Enqueue((uuid, id) => Authorized(uuid, id));

        var outcome = await Service().RequestAsync(order, _ => throw new HttpRequestException("fila presa"));

        Assert.Equal(FiscalOutcomeKind.Authorized, outcome.Kind);
    }

    [Fact]
    public async Task An_open_sale_never_asks_for_a_note()
    {
        var order = PaidOrder();
        _database.Execute("UPDATE orders SET status = 'open' WHERE id = $id", ("$id", order));

        await Assert.ThrowsAsync<InvalidOperationException>(() => Service().RequestAsync(order));
        Assert.Empty(_cloud.IssuedWith);
    }

    [Fact]
    public void The_switch_is_off_until_someone_turns_it_on()
    {
        var active = new TerminalProfile("t", "s", "d", "Loja", Activated: true, CloudBaseUrl: "https://painel.exemplo");
        var demo = active with { Activated = false };

        Assert.False(FiscalIssuance.IsEnabled(_database, active));
        _database.Execute("INSERT INTO device_settings (key, value, updated_at) VALUES ('fiscal.enabled', '1', 'x')");
        Assert.True(FiscalIssuance.IsEnabled(_database, active));
        Assert.False(FiscalIssuance.IsEnabled(_database, demo));
    }

    // -- o papel da venda ----------------------------------------------------

    private readonly List<(byte[] Payload, string Job)> _printed = [];

    private SaleDocuments Documents(bool fiscal = true) => new(
        _ => Encoding.ASCII.GetBytes("CUPOM"), (payload, job) => _printed.Add((payload, job)),
        new PrinterLayout(), fiscal ? Service() : null);

    private static ClosedSale Closed(string order) => new(order, new CheckoutResult.Closed([], [], true), null, "Ana");

    [Fact]
    public async Task With_the_switch_off_it_is_the_receipt_as_always()
    {
        var notice = await Documents(fiscal: false).PrintAsync(Closed(PaidOrder()));

        Assert.Null(notice);
        Assert.Equal("PDV Cupom", Assert.Single(_printed).Job);
        Assert.Empty(_cloud.IssuedWith);
    }

    [Fact]
    public async Task An_authorized_note_prints_the_danfe_instead_of_the_receipt()
    {
        var order = PaidOrder();
        _cloud.Issues.Enqueue((uuid, id) => Authorized(uuid, id));

        var notice = await Documents().PrintAsync(Closed(order));

        Assert.Equal("NFC-e autorizada.", notice);
        var (payload, job) = Assert.Single(_printed);
        Assert.Equal("PDV DANFE NFC-e", job);
        Assert.Contains(Contract.GetProperty("expected").GetProperty("access_key").GetString()!, Encoding.Latin1.GetString(payload));
    }

    [Fact]
    public async Task No_decision_yet_prints_the_receipt_and_says_the_note_comes_later()
    {
        _cloud.Issues.Enqueue((_, _) => throw new FiscalOfflineException("sem rede"));

        var notice = await Documents().PrintAsync(Closed(PaidOrder()));

        Assert.Equal("PDV Cupom", Assert.Single(_printed).Job);
        Assert.Contains("consultada sozinha", notice);
    }

    [Fact]
    public async Task A_courtesy_prints_the_receipt_without_a_word()
    {
        var notice = await Documents().PrintAsync(Closed(PaidOrder(totalCents: 0)));

        Assert.Null(notice);
        Assert.Equal("PDV Cupom", Assert.Single(_printed).Job);
    }

    [Fact]
    public async Task A_defect_in_the_fiscal_path_still_gives_the_customer_paper()
    {
        var order = PaidOrder();
        _database.Execute("UPDATE orders SET status = 'open' WHERE id = $id", ("$id", order));

        var notice = await Documents().PrintAsync(Closed(order));

        Assert.Equal("PDV Cupom", Assert.Single(_printed).Job);
        Assert.Contains("será pedida de novo", notice);
        Assert.DoesNotContain("InvalidOperation", notice);
    }

    [Fact]
    public async Task The_reprint_is_the_danfe_once_the_background_gets_it_authorized()
    {
        var order = PaidOrder();
        _cloud.Issues.Enqueue((_, _) => throw new FiscalResultUnknownException("timeout"));
        var documents = Documents();
        await documents.PrintAsync(Closed(order));
        var receipt = _printed[0].Payload;
        Assert.Same(receipt, documents.Reprint(order, receipt));

        _clock.Advance(TimeSpan.FromMinutes(1));
        _cloud.Statuses.Enqueue((uuid, id) => Authorized(uuid, id));
        await Service().CheckDueAsync();

        var second = documents.Reprint(order, receipt)!;
        Assert.NotSame(receipt, second);
        Assert.Contains("DANFE", Encoding.Latin1.GetString(second));
    }

    // -- o canal HTTP ---------------------------------------------------------

    private static HttpFiscalGateway Http(HttpMessageHandler handler, TimeSpan? timeout = null) =>
        new("https://painel.exemplo.com.br", "token", timeout ?? TimeSpan.FromSeconds(5), new HttpClient(handler));

    [Fact]
    public async Task No_connection_at_all_is_offline()
    {
        // Uma porta de verdade, fechada: o erro é o que o sistema operacional dá.
        using var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        using var gateway = new HttpFiscalGateway($"http://127.0.0.1:{port}", "token", TimeSpan.FromSeconds(5));

        await Assert.ThrowsAsync<FiscalOfflineException>(() => gateway.IssueAsync("u", "o", CancellationToken.None));
    }

    [Fact]
    public async Task A_slow_answer_is_unknown_not_offline()
    {
        using var gateway = Http(new Handler(async (_, token) =>
        {
            await Task.Delay(TimeSpan.FromSeconds(10), token);
            return new HttpResponseMessage(HttpStatusCode.OK);
        }), TimeSpan.FromMilliseconds(100));

        await Assert.ThrowsAsync<FiscalResultUnknownException>(() => gateway.IssueAsync("u", "o", CancellationToken.None));
    }

    [Theory]
    [InlineData(500, typeof(FiscalResultUnknownException))]
    [InlineData(502, typeof(FiscalResultUnknownException))]
    [InlineData(401, typeof(FiscalAuthException))]
    [InlineData(403, typeof(FiscalAuthException))]
    [InlineData(409, typeof(FiscalRefusedException))]
    [InlineData(404, typeof(FiscalRefusedException))]
    public async Task Each_http_status_has_one_meaning(int status, Type expected)
    {
        using var gateway = Http(new Handler((_, _) => Task.FromResult(
            new HttpResponseMessage((HttpStatusCode)status) { Content = new StringContent("{\"detail\":\"Série não configurada.\"}") })));

        var error = await Assert.ThrowsAnyAsync<FiscalCloudException>(() => gateway.IssueAsync("u", "o", CancellationToken.None));

        Assert.IsType(expected, error);
    }

    [Fact]
    public async Task A_connection_dropped_mid_answer_is_unknown()
    {
        using var gateway = Http(new Handler((_, _) =>
            throw new HttpRequestException(HttpRequestError.ResponseEnded, "resposta cortada")));

        await Assert.ThrowsAsync<FiscalResultUnknownException>(() => gateway.IssueAsync("u", "o", CancellationToken.None));
    }

    [Fact]
    public async Task An_illegible_success_is_unknown()
    {
        using var gateway = Http(new Handler((_, _) => Task.FromResult(
            new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent("<html>proxy</html>") })));

        await Assert.ThrowsAsync<FiscalResultUnknownException>(() => gateway.StatusAsync("u", CancellationToken.None));
    }

    [Fact]
    public async Task The_request_carries_the_uuid_the_order_and_the_token()
    {
        HttpRequestMessage? seen = null;
        string? body = null;
        using var gateway = Http(new Handler(async (request, token) =>
        {
            seen = request;
            body = await request.Content!.ReadAsStringAsync(token);
            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(
                    """{"request_uuid":"u-1","order_id":"o-1","status":"authorized","series":1,"number":42,"processed_xml":"<nfeProc/>"}"""),
            };
        }));

        var document = await gateway.IssueAsync("u-1", "o-1", CancellationToken.None);

        Assert.Equal("https://painel.exemplo.com.br/api/fiscal/issue", seen!.RequestUri!.ToString());
        Assert.Equal("Bearer token", seen.Headers.Authorization!.ToString());
        Assert.Equal("""{"request_uuid":"u-1","order_id":"o-1"}""", body);
        Assert.Equal(("authorized", 42L, "<nfeProc/>"), (document.Status, document.Number, document.ProcessedXml));
    }

    private sealed class Handler(Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>> answer) : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken) =>
            answer(request, cancellationToken);
    }

    /// <summary>A retaguarda de mentira: responde o roteiro e guarda com que uuid foi chamada.</summary>
    private sealed class ScriptedGateway : IFiscalGateway
    {
        public Queue<Func<string, string, CloudFiscalDocument>> Issues { get; } = new();
        public Queue<Func<string, string, CloudFiscalDocument>> Statuses { get; } = new();
        public List<string> IssuedWith { get; } = [];
        public List<string> QueriedWith { get; } = [];
        private readonly Dictionary<string, string> _orders = [];

        public Task<CloudFiscalDocument> IssueAsync(string requestUuid, string orderId, CancellationToken cancellation)
        {
            IssuedWith.Add(requestUuid);
            _orders[requestUuid] = orderId;
            return Task.FromResult(Issues.Dequeue()(requestUuid, orderId));
        }

        public Task<CloudFiscalDocument> StatusAsync(string requestUuid, CancellationToken cancellation)
        {
            QueriedWith.Add(requestUuid);
            return Task.FromResult(Statuses.Dequeue()(requestUuid, _orders[requestUuid]));
        }
    }
}
