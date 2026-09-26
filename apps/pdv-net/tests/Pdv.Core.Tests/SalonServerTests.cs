using System.Net;
using System.Net.Http.Json;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json.Nodes;
using Pdv.Data;
using Pdv.Data.Edge;
using Pdv.Edge;

namespace Pdv.Core.Tests;

/// <summary>
/// O que o contrato de rotas não alcança: o WebSocket da cozinha, o salão cheio
/// (vários celulares ao mesmo tempo), a porta ocupada e o nome de arquivo que
/// vem da URL.
/// </summary>
public sealed class SalonServerTests : IAsyncLifetime
{
    private static readonly JsonObject Contract =
        JsonNode.Parse(File.ReadAllText(TestDatabase.Contract("salon-http.json")))!.AsObject();

    private const string Cafe = "5a1a0000-0000-4000-8000-0000000000b1";

    private readonly TestDatabase _file = new(userVersion: PdvDatabase.SupportedSchemaVersion);
    private PdvDatabase _database = null!;
    private SalonServices _services = null!;
    private SalonServer _server = null!;
    private HttpClient _client = null!;

    public async Task InitializeAsync()
    {
        _database = new PdvDatabase(_file.Path);
        SalonScript.Seed(_database, Contract);
        var profile = new TerminalProfile(
            Contract["tenant_id"]!.GetValue<string>(), Contract["store_id"]!.GetValue<string>(),
            Contract["device_id"]!.GetValue<string>(), "Confeitaria Aurora", true, null);
        var ledger = new AuditLedger(profile.TenantId, profile.StoreId, profile.DeviceId, Encoding.UTF8.GetBytes("chave"));
        _services = new SalonServices(_database, profile, ledger, new EventHub());
        _services.Tables.SeedDefaultTables(4);
        _server = new SalonServer(_services);
        Assert.True(await _server.StartAsync(IPAddress.Loopback, 0));
        _client = new HttpClient { BaseAddress = new Uri($"http://127.0.0.1:{_server.Port}") };
    }

    public async Task DisposeAsync()
    {
        _client.Dispose();
        await _server.DisposeAsync();
        _database.Dispose();
        _file.Dispose();
    }

    private async Task<(string Device, string Token)> PairAsync(string kind = "waiter")
    {
        var (code, _) = _services.Auth.CreatePairingCode();
        using var response = await _client.PostAsJsonAsync("/pair", new { code, device_name = "Aparelho", kind });
        var body = (await response.Content.ReadFromJsonAsync<JsonObject>())!;
        return (body["device_id"]!.GetValue<string>(), body["token"]!.GetValue<string>());
    }

    private async Task<HttpClient> WaiterAsync()
    {
        var (_, token) = await PairAsync();
        var client = new HttpClient { BaseAddress = _client.BaseAddress };
        client.DefaultRequestHeaders.Add("Authorization", $"Bearer {token}");
        using var login = await client.PostAsJsonAsync("/staff/session", new { login = "joao", pin = "4826" });
        client.DefaultRequestHeaders.Add("X-Staff-Token",
            (await login.Content.ReadFromJsonAsync<JsonObject>())!["token"]!.GetValue<string>());
        return client;
    }

    private static async Task<JsonObject> ReceiveAsync(ClientWebSocket socket)
    {
        var buffer = new byte[64 * 1024];
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        var received = await socket.ReceiveAsync(buffer, timeout.Token);
        return JsonNode.Parse(Encoding.UTF8.GetString(buffer, 0, received.Count))!.AsObject();
    }

    [Fact]
    public async Task The_kitchen_stream_refuses_an_unpaired_device_at_the_handshake()
    {
        using var socket = new ClientWebSocket();

        var error = await Assert.ThrowsAsync<WebSocketException>(() =>
            socket.ConnectAsync(new Uri($"ws://127.0.0.1:{_server.Port}/kds/stream?token=inventado"), CancellationToken.None));

        Assert.Contains("403", error.Message);
    }

    [Fact]
    public async Task The_kitchen_stream_opens_with_the_queue_and_follows_the_kitchen()
    {
        using var waiter = await WaiterAsync();
        var table = _services.Tables.List()[0];
        using var opened = await waiter.PostAsJsonAsync("/orders", new { client_uuid = Guid.NewGuid().ToString(), table_id = table.Id });
        var orderId = (await opened.Content.ReadFromJsonAsync<JsonObject>())!["order_id"]!.GetValue<string>();
        await waiter.PostAsJsonAsync($"/orders/{orderId}/items", new { client_uuid = Guid.NewGuid().ToString(), product_id = Cafe });

        var (_, kitchen) = await PairAsync("kds");
        using var socket = new ClientWebSocket();
        await socket.ConnectAsync(new Uri($"ws://127.0.0.1:{_server.Port}/kds/stream?token={kitchen}"), CancellationToken.None);

        // Quem conecta (ou reconecta) recebe a fila inteira primeiro.
        var snapshot = await ReceiveAsync(socket);
        Assert.Equal("snapshot", snapshot["kind"]!.GetValue<string>());
        var ticket = snapshot["tickets"]!.AsArray().Single()!["ticket_id"]!.GetValue<string>();

        // Depois, só o que a cozinha precisa saber, na ordem em que aconteceu.
        await waiter.PostAsJsonAsync($"/orders/{orderId}/items", new { client_uuid = Guid.NewGuid().ToString(), product_id = Cafe });
        var queued = await ReceiveAsync(socket);
        Assert.Equal("ticket.queued", queued["kind"]!.GetValue<string>());
        Assert.Equal(orderId, queued["order_id"]!.GetValue<string>());

        using var kds = new HttpClient { BaseAddress = _client.BaseAddress };
        kds.DefaultRequestHeaders.Add("Authorization", $"Bearer {kitchen}");
        await kds.PostAsync($"/kds/tickets/{ticket}/bump", null);
        var changed = await ReceiveAsync(socket);
        Assert.Equal(("ticket.changed", ticket, "preparing"),
            (changed["kind"]!.GetValue<string>(), changed["ticket_id"]!.GetValue<string>(), changed["status"]!.GetValue<string>()));

        // Pedir a conta não é assunto da cozinha: não chega por aqui.
        await waiter.PostAsync($"/orders/{orderId}/bill", null);
        await kds.PostAsync($"/kds/tickets/{ticket}/bump", null);
        Assert.Equal("ticket.changed", (await ReceiveAsync(socket))["kind"]!.GetValue<string>());

        await socket.CloseAsync(WebSocketCloseStatus.NormalClosure, null, CancellationToken.None);
    }

    [Fact]
    public async Task A_full_salon_launching_at_once_loses_nothing()
    {
        // Oito celulares lançando juntos na mesma comanda: sem a trava, a mesma
        // conexão SQLite em várias threads corromperia o total ou derrubaria a rota.
        // Pareados um a um: gerar código novo revoga o anterior, como no caixa.
        var waiters = new List<HttpClient>();
        for (var i = 0; i < 8; i++) waiters.Add(await WaiterAsync());
        var table = _services.Tables.List()[1];
        using var opened = await waiters[0].PostAsJsonAsync("/orders", new { client_uuid = Guid.NewGuid().ToString(), table_id = table.Id });
        var orderId = (await opened.Content.ReadFromJsonAsync<JsonObject>())!["order_id"]!.GetValue<string>();

        var responses = await Task.WhenAll(Enumerable.Range(0, 40).Select(i =>
            waiters[i % waiters.Count].PostAsJsonAsync($"/orders/{orderId}/items",
                new { client_uuid = Guid.NewGuid().ToString(), product_id = Cafe })));

        Assert.All(responses, r => Assert.Equal(HttpStatusCode.OK, r.StatusCode));
        var order = _services.Orders.GetOrder(orderId);
        Assert.Equal((40, 40 * 700L), (order.ItemCount, order.TotalCents));
        foreach (var waiter in waiters) waiter.Dispose();
    }

    [Theory]
    [InlineData("/vendor/..%2Fpdv_local.db")]
    [InlineData("/vendor/..%5Cindex.html")]
    [InlineData("/vendor/%2E%2E%2Findex.html")]
    [InlineData("/vendor/index.html")]
    public async Task Vendor_serves_only_its_own_files(string path)
    {
        using var response = await _client.GetAsync(path);

        Assert.Equal(HttpStatusCode.NotFound, response.StatusCode);
    }

    [Fact]
    public async Task A_busy_port_leaves_the_counter_selling()
    {
        using var occupied = new TcpListener(IPAddress.Loopback, 0);
        occupied.Start();
        var port = ((IPEndPoint)occupied.LocalEndpoint).Port;
        var messages = new List<string>();
        await using var second = new SalonServer(_services, messages.Add);

        Assert.False(await second.StartAsync(IPAddress.Loopback, port));
        Assert.False(second.IsRunning);
        Assert.Contains(messages, m => m.Contains("não subiu", StringComparison.Ordinal));
    }

    [Fact]
    public async Task An_unexpected_failure_is_a_plain_500_without_details()
    {
        using var waiter = await WaiterAsync();
        _database.Execute("DROP TABLE kds_tickets");

        using var response = await waiter.GetAsync("/kds/tickets");

        Assert.Equal(HttpStatusCode.InternalServerError, response.StatusCode);
        // Nada de pilha nem SQL para quem está na rede da loja.
        Assert.Equal("Internal Server Error", await response.Content.ReadAsStringAsync());
    }
}
