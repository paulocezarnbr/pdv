using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using Fiscal.Service;

namespace Fiscal.Tests;

/// <summary>
/// Os testes do serviço em Python que este substituiu (<c>apps/fiscal-service/tests/test_service.py</c>,
/// removido; está no histórico do git), um a um, pelo HTTP do serviço em C#: a
/// retaguarda não pode perceber a troca.
/// </summary>
public sealed class ServiceParityTests : IDisposable
{
    private readonly Scratch _scratch = new();

    public void Dispose() => _scratch.Dispose();

    private sealed class FakeEngine : IFiscalEngine
    {
        public int Calls;

        public string Name => "fake";

        public FiscalResult? Preflight(FiscalIntent intent) => null;

        public Task<EngineOutcome> AuthorizeAsync(FiscalIntent intent, Action<string, string, string> prepare, CancellationToken cancellation)
        {
            Interlocked.Increment(ref Calls);
            return Task.FromResult(EngineOutcome.Settled(new FiscalResult("authorized", "100", "Autorizado", new string('3', 44), "123")));
        }

        public Task<EngineOutcome> ReconcileAsync(PendingRequest pending, CancellationToken cancellation) =>
            throw new InvalidOperationException("não deveria reconciliar");
    }

    private sealed class BrokenEngine : IFiscalEngine
    {
        public string Name => "broken";

        public FiscalResult? Preflight(FiscalIntent intent) => null;

        public Task<EngineOutcome> AuthorizeAsync(FiscalIntent intent, Action<string, string, string> prepare, CancellationToken cancellation) =>
            throw new InvalidOperationException("senha-a1-super-secreta");

        public Task<EngineOutcome> ReconcileAsync(PendingRequest pending, CancellationToken cancellation) =>
            throw new InvalidOperationException("senha-a1-super-secreta");
    }

    private static HttpRequestMessage Post(string path, object body, string? token = "secret")
    {
        var request = new HttpRequestMessage(HttpMethod.Post, path) { Content = JsonContent.Create(body) };
        if (token is not null) request.Headers.Add("Authorization", $"Bearer {token}");
        return request;
    }

    private static async Task<JsonElement> Json(HttpResponseMessage response) =>
        JsonDocument.Parse(await response.Content.ReadAsStringAsync()).RootElement.Clone();

    [Fact]
    public async Task Internal_routes_require_token()
    {
        using var host = new ServiceHost(_scratch, new FakeEngine());
        var http = host.CreateClient();
        Assert.Equal(HttpStatusCode.Unauthorized, (await http.SendAsync(Post("/v1/fiscal/authorize", Intents.PythonShaped(), token: null))).StatusCode);
        Assert.Equal(HttpStatusCode.Unauthorized, (await http.SendAsync(Post("/v1/fiscal/authorize", Intents.PythonShaped(), token: "wrong"))).StatusCode);
        Assert.Equal(HttpStatusCode.Unauthorized, (await http.SendAsync(Post("/v1/fiscal/status", new { request_uuid = "x" }, token: "wrong"))).StatusCode);
    }

    [Fact]
    public async Task Without_a_configured_token_nothing_is_accepted()
    {
        using var host = new ServiceHost(_scratch, new FakeEngine(), token: "");
        var response = await host.CreateClient().SendAsync(Post("/v1/fiscal/authorize", Intents.PythonShaped(), token: ""));
        Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode);
    }

    [Fact]
    public async Task Same_request_is_authorized_once_even_after_restart()
    {
        var engine = new FakeEngine();
        JsonElement first;
        using (var host = new ServiceHost(_scratch, engine))
        {
            first = await Json(await host.CreateClient().SendAsync(Post("/v1/fiscal/authorize", Intents.PythonShaped())));
        }
        // "Reinício": outro host sobre o mesmo arquivo de estado.
        using var restarted = new ServiceHost(_scratch, engine);
        var second = await Json(await restarted.CreateClient().SendAsync(Post("/v1/fiscal/authorize", Intents.PythonShaped())));

        Assert.Equal("authorized", first.GetProperty("status").GetString());
        Assert.Equal(first.GetRawText(), second.GetRawText());
        Assert.Equal(1, engine.Calls);
    }

    [Fact]
    public async Task Status_returns_durable_result()
    {
        using var host = new ServiceHost(_scratch, new FakeEngine());
        var http = host.CreateClient();
        await http.SendAsync(Post("/v1/fiscal/authorize", Intents.PythonShaped()));
        var status = await Json(await http.SendAsync(Post("/v1/fiscal/status", new { request_uuid = "req-1" })));
        Assert.Equal("100", status.GetProperty("code").GetString());
        Assert.Equal(new string('3', 44), status.GetProperty("access_key").GetString());
    }

    [Fact]
    public void Secret_reference_cannot_escape_mount()
    {
        var resolver = new SecretResolver(_scratch.Secrets);
        File.WriteAllText(Path.Combine(_scratch.Root, "outside.pfx"), "x");
        Assert.Throws<SecretException>(() => resolver.PathOf("../outside.pfx"));
        Assert.Throws<SecretException>(() => resolver.PathOf(Path.Combine(_scratch.Root, "outside.pfx")));
        Assert.Throws<SecretException>(() => resolver.PathOf("loja/../../outside.pfx"));
        Assert.Throws<SecretException>(() => resolver.PathOf(""));
    }

    [Fact]
    public async Task Engine_exception_becomes_unknown_without_leaking_secret()
    {
        using var host = new ServiceHost(_scratch, new BrokenEngine());
        var response = await host.CreateClient().SendAsync(Post("/v1/fiscal/authorize", Intents.PythonShaped()));
        var text = await response.Content.ReadAsStringAsync();
        Assert.Equal("unknown", JsonDocument.Parse(text).RootElement.GetProperty("status").GetString());
        Assert.DoesNotContain("senha-a1", text);
    }

    [Fact]
    public async Task Status_of_a_request_never_received_is_not_found()
    {
        var engine = new FakeEngine();
        using var host = new ServiceHost(_scratch, engine);
        var response = await host.CreateClient().SendAsync(Post("/v1/fiscal/status", new { request_uuid = "nunca-visto" }));
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        var body = await Json(response);
        Assert.Equal("unknown", body.GetProperty("status").GetString());
        Assert.Equal("NOT_FOUND", body.GetProperty("code").GetString());
        Assert.Equal(0, engine.Calls);
    }

    [Fact]
    public async Task Status_of_a_request_claimed_but_unsettled_is_in_flight()
    {
        using var host = new ServiceHost(_scratch, new FakeEngine());
        // O processo caiu depois de reivindicar e antes de gravar o resultado.
        Assert.True(host.Store.Claim("req-1", "doc-1"));
        var body = await Json(await host.CreateClient().SendAsync(Post("/v1/fiscal/status", new { request_uuid = "req-1" })));
        Assert.Equal("unknown", body.GetProperty("status").GetString());
        Assert.Equal("IN_FLIGHT", body.GetProperty("code").GetString());
    }

    [Fact]
    public async Task Retransmitting_a_not_found_request_runs_the_engine_once()
    {
        var engine = new FakeEngine();
        using var host = new ServiceHost(_scratch, engine);
        var http = host.CreateClient();
        Assert.Equal("NOT_FOUND", (await Json(await http.SendAsync(Post("/v1/fiscal/status", new { request_uuid = "req-1" })))).GetProperty("code").GetString());
        var first = await Json(await http.SendAsync(Post("/v1/fiscal/authorize", Intents.PythonShaped())));
        // A chamada original, atrasada na rede, chega depois da retransmissão.
        var late = await Json(await http.SendAsync(Post("/v1/fiscal/authorize", Intents.PythonShaped())));
        Assert.Equal("authorized", first.GetProperty("status").GetString());
        Assert.Equal(first.GetRawText(), late.GetRawText());
        Assert.Equal(1, engine.Calls);
    }

    // -- além do Python -------------------------------------------------------------

    [Fact]
    public async Task Concurrent_calls_with_the_same_request_run_the_engine_once()
    {
        var engine = new FakeEngine();
        using var host = new ServiceHost(_scratch, engine);
        var http = host.CreateClient();
        var answers = await Task.WhenAll(Enumerable.Range(0, 8).Select(_ => http.SendAsync(Post("/v1/fiscal/authorize", Intents.PythonShaped()))));
        Assert.All(answers, answer => Assert.Equal(HttpStatusCode.OK, answer.StatusCode));
        Assert.Equal(1, engine.Calls);
    }

    [Fact]
    public async Task A_python_state_file_is_read_and_upgraded()
    {
        // O arquivo que o serviço em Python deixou no volume: mesma tabela, sem as colunas novas.
        using (var db = new Microsoft.Data.Sqlite.SqliteConnection($"Data Source={_scratch.State};Pooling=False"))
        {
            db.Open();
            using var command = db.CreateCommand();
            command.CommandText = """
                CREATE TABLE fiscal_results (request_uuid TEXT PRIMARY KEY, document_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('processing','settled')), result_json TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                INSERT INTO fiscal_results (request_uuid, document_id, state, result_json) VALUES
                    ('antigo', 'doc-0', 'settled',
                     '{"status":"rejected","code":"FISCAL_ENGINE_NOT_HOMOLOGATED","reason":"trava","access_key":null,"protocol":null,"processed_xml":null}');
                """;
            command.ExecuteNonQuery();
        }
        using var host = new ServiceHost(_scratch, new FakeEngine());
        var body = await Json(await host.CreateClient().SendAsync(Post("/v1/fiscal/status", new { request_uuid = "antigo" })));
        Assert.Equal("FISCAL_ENGINE_NOT_HOMOLOGATED", body.GetProperty("code").GetString());
    }

    [Fact]
    public async Task Invalid_intent_is_refused_before_anything_is_claimed()
    {
        using var host = new ServiceHost(_scratch, new FakeEngine());
        var http = host.CreateClient();
        var broken = JsonSerializer.SerializeToNode(Intents.PythonShaped())!;
        broken["issuer"]!["cnpj"] = "123";
        broken["payments"] = new System.Text.Json.Nodes.JsonArray();
        var response = await http.SendAsync(Post("/v1/fiscal/authorize", broken));
        Assert.Equal(HttpStatusCode.UnprocessableEntity, response.StatusCode);
        var detail = (await Json(response)).GetProperty("detail").GetString()!;
        Assert.Contains("issuer.cnpj", detail);
        Assert.Contains("payments", detail);
        Assert.False(host.Store.Known("req-1"));
    }

    [Fact]
    public async Task Health_names_the_engine()
    {
        using var host = new ServiceHost(_scratch, new FakeEngine());
        var body = await Json(await host.CreateClient().GetAsync("/health"));
        Assert.True(body.GetProperty("ok").GetBoolean());
        Assert.Equal("fake", body.GetProperty("engine").GetString());
    }
}
