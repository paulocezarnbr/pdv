using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Pdv.Data;
using Pdv.Data.Edge;
using Pdv.Edge;

namespace Pdv.Core.Tests;

/// <summary>
/// As rotas do servidor do salão em C# contra <c>contracts/salon-http.json</c>,
/// que o FastAPI respondeu: mesmo status, mesmo tipo, mesmos cabeçalhos que o
/// app lê e o mesmo corpo, passo a passo.
/// </summary>
/// <remarks>
/// O servidor sobe de verdade (Kestrel em <c>127.0.0.1</c>, porta livre) e o
/// cliente fala HTTP com ele — serviço testado não prova rota montada, como o
/// Python aprendeu com o 422 em toda rota. O harness espelha
/// <c>salon_http_script.py</c> linha a linha.
/// </remarks>
public sealed class SalonHttpContractTests : IAsyncLifetime
{
    private static readonly JsonObject Contract =
        JsonNode.Parse(File.ReadAllText(TestDatabase.Contract("salon-http.json")))!.AsObject();

    private static readonly string[] KeptHeaders = ["x-auth-scope", "cache-control"];

    private readonly TestDatabase _file = new(userVersion: PdvDatabase.SupportedSchemaVersion);
    private PdvDatabase _database = null!;
    private SalonServer _server = null!;
    private HttpClient _client = null!;
    private EdgeAuth _caixa = null!;

    public async Task InitializeAsync()
    {
        _database = new PdvDatabase(_file.Path);
        SalonScript.Seed(_database, Contract);
        var profile = new TerminalProfile(
            Contract["tenant_id"]!.GetValue<string>(), Contract["store_id"]!.GetValue<string>(),
            Contract["device_id"]!.GetValue<string>(), Contract["store_name"]!.GetValue<string>(), true, null);
        var ledger = new AuditLedger(profile.TenantId, profile.StoreId, profile.DeviceId,
            Encoding.UTF8.GetBytes(Contract["device_secret"]!.GetValue<string>()));
        _server = new SalonServer(new SalonServices(_database, profile, ledger, new EventHub()));
        Assert.True(await _server.StartAsync(IPAddress.Loopback, 0));
        _client = new HttpClient { BaseAddress = new Uri($"http://127.0.0.1:{_server.Port}") };
        // O caixa gera o código na própria tela: outra instância, o mesmo banco.
        _caixa = new EdgeAuth(_database, profile.Identity);
    }

    public async Task DisposeAsync()
    {
        _client.Dispose();
        await _server.DisposeAsync();
        _database.Dispose();
        _file.Dispose();
    }

    private static string Dig(JsonNode? node, string path)
    {
        foreach (var part in path.Split('.'))
        {
            node = int.TryParse(part, out var index) ? node![index] : node![part];
        }
        return node!.GetValue<string>();
    }

    private static JsonObject FileDigest(byte[] body)
    {
        var text = Encoding.Latin1.GetBytes(Encoding.Latin1.GetString(body).Replace("\r\n", "\n"));
        return new JsonObject { ["sha256"] = Convert.ToHexStringLower(SHA256.HashData(text)), ["bytes"] = text.Length };
    }

    private static async Task<JsonObject> Outcome(HttpResponseMessage response)
    {
        var media = response.Content.Headers.ContentType?.MediaType ?? "";
        var outcome = new JsonObject { ["status"] = (int)response.StatusCode, ["type"] = media };
        var headers = new JsonObject();
        foreach (var name in KeptHeaders)
        {
            if (response.Headers.TryGetValues(name, out var values) || response.Content.Headers.TryGetValues(name, out values))
            {
                headers[name] = string.Join(", ", values);
            }
        }
        if (headers.Count > 0) outcome["headers"] = headers;
        var bytes = await response.Content.ReadAsByteArrayAsync();
        if (media == "application/json")
        {
            var body = JsonNode.Parse(bytes);
            if (response.StatusCode == HttpStatusCode.UnprocessableEntity && body?["detail"] is JsonArray)
            {
                body = "<validação>";
            }
            outcome["body"] = body;
        }
        else
        {
            outcome["body"] = FileDigest(bytes);
        }
        return outcome;
    }

    private async Task<JsonArray> Run()
    {
        var saved = new Dictionary<string, string>();
        string Resolve(string text)
        {
            foreach (var name in saved.Keys.OrderByDescending(k => k.Length)) text = text.Replace("$" + name, saved[name]);
            return text;
        }
        JsonNode? ResolveNode(JsonNode? node) => node switch
        {
            JsonValue v when v.GetValueKind() == JsonValueKind.String => Resolve(v.GetValue<string>()),
            JsonArray array => new JsonArray([.. array.Select(ResolveNode)]),
            JsonObject obj => new JsonObject(obj.Select(p => KeyValuePair.Create(p.Key, ResolveNode(p.Value)))),
            _ => node?.DeepClone(),
        };

        var normalizer = new SalonScript.Normalizer();
        var results = new JsonArray();
        foreach (var step in Contract["script"]!.AsArray().Select(s => s!.AsObject()))
        {
            switch (step["op"]?.GetValue<string>())
            {
                case "code":
                    saved[step["save"]!.GetValue<string>()] = _caixa.CreatePairingCode().Code;
                    results.Add(new JsonObject { ["done"] = "code" });
                    continue;
                case "revoke":
                    results.Add(new JsonObject
                    {
                        ["done"] = "revoke", ["result"] = _caixa.Revoke(Resolve(step["device"]!.GetValue<string>())),
                    });
                    continue;
            }

            using var request = new HttpRequestMessage(
                new HttpMethod(step["method"]!.GetValue<string>()), Resolve(step["path"]!.GetValue<string>()));
            var names = (JsonObject?)ResolveNode(step["headers"]) ?? [];
            if (names["device"] is { } device) request.Headers.TryAddWithoutValidation("Authorization", $"Bearer {device}");
            if (names["staff"] is { } staff) request.Headers.TryAddWithoutValidation("X-Staff-Token", staff.GetValue<string>());
            if (names["manager"] is { } manager) request.Headers.TryAddWithoutValidation("X-Manager-Token", manager.GetValue<string>());
            foreach (var (name, value) in step["raw_headers"]?.AsObject() ?? [])
            {
                request.Headers.TryAddWithoutValidation(name, value!.GetValue<string>());
            }
            if (step["json"] is { } json)
            {
                request.Content = new StringContent(ResolveNode(json)!.ToJsonString(), Encoding.UTF8, "application/json");
            }
            if (step["raw"] is { } raw)
            {
                request.Content = new StringContent(raw.GetValue<string>(), Encoding.UTF8, "application/json");
            }

            using var response = await _client.SendAsync(request);
            var outcome = await Outcome(response);
            foreach (var (name, path) in step["save"]?.AsObject() ?? [])
            {
                saved[name] = Dig(outcome["body"], path!.GetValue<string>());
            }
            results.Add(normalizer.Value(outcome));
        }
        return results;
    }

    [Fact]
    public async Task Every_route_answers_what_the_fastapi_server_answered()
    {
        var results = await Run();
        var script = Contract["script"]!.AsArray();
        var expected = Contract["results"]!.AsArray();

        Assert.Equal(expected.Count, results.Count);
        for (var i = 0; i < expected.Count; i++)
        {
            var step = script[i]!.ToJsonString();
            Assert.Equal((i, step, expected[i]!.ToJsonString()), (i, step, results[i]!.ToJsonString()));
        }
    }
}
