using System.Net;
using System.Text;
using System.Text.Json;
using Microsoft.Data.Sqlite;
using Pdv.Data;
using Pdv.Data.Provisioning;
using Pdv.Data.Secrets;

namespace Pdv.Core.Tests;

/// <summary>
/// Ativação do terminal contra <c>contracts/activation.json</c> (gerado pelo
/// Python), a conversa HTTP com a retaguarda, o que fica gravado e a troca do
/// banco de demonstração.
/// </summary>
public sealed class ActivationTests : IDisposable
{
    private static readonly JsonElement Contract =
        JsonDocument.Parse(File.ReadAllText(TestDatabase.Contract("activation.json"))).RootElement.Clone();

    private static readonly ActivationResult Granted = new(
        "aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002",
        "cccccccc-0000-0000-0000-000000000003", "token-de-sincronizacao-de-teste", "Pool Bar",
        "https://teste.rsrassessoria.com.br/api");

    private readonly TestDatabase _file = new(PdvDatabase.SupportedSchemaVersion);
    private readonly string _vaultFolder;

    public ActivationTests()
    {
        _vaultFolder = Path.Combine(Path.GetDirectoryName(_file.Path)!, "secrets");
    }

    public void Dispose() => _file.Dispose();

    // -- contrato com o Python ---------------------------------------------------

    public static TheoryData<string> Codes() => Cases("codes");

    public static TheoryData<string> Servers() => Cases("servers");

    private static TheoryData<string> Cases(string list)
    {
        var data = new TheoryData<string>();
        foreach (var item in Contract.GetProperty(list).EnumerateArray()) data.Add(item.GetProperty("raw").GetString()!);
        return data;
    }

    private static JsonElement Case(string list, string raw) =>
        Contract.GetProperty(list).EnumerateArray().First(item => item.GetProperty("raw").GetString() == raw);

    [Theory]
    [MemberData(nameof(Codes))]
    public void The_code_is_normalized_like_the_python_pdv(string raw)
    {
        var expected = Case("codes", raw);
        if (expected.GetProperty("ok").GetBoolean())
        {
            Assert.Equal(expected.GetProperty("code").GetString(), Activation.NormalizeCode(raw));
        }
        else
        {
            var error = Assert.Throws<ActivationException>(() => Activation.NormalizeCode(raw));
            Assert.Equal(expected.GetProperty("message").GetString(), error.Message);
        }
    }

    [Theory]
    [MemberData(nameof(Servers))]
    public void The_server_address_is_normalized_like_the_python_pdv(string raw)
    {
        var expected = Case("servers", raw);
        if (expected.GetProperty("ok").GetBoolean())
        {
            var url = Activation.NormalizeServerUrl(raw);
            Assert.Equal(expected.GetProperty("api_url").GetString(), url);
            Assert.Equal(expected.GetProperty("display").GetString(), Activation.DisplayServerUrl(url));
        }
        else
        {
            var error = Assert.Throws<ActivationException>(() => Activation.NormalizeServerUrl(raw));
            Assert.Equal(expected.GetProperty("message").GetString(), error.Message);
        }
    }

    [Fact]
    public void Names_and_roots_match_the_python_pdv()
    {
        foreach (var item in Contract.GetProperty("api_roots").EnumerateArray())
        {
            Assert.Equal(item.GetProperty("root").GetString(), Activation.CloudApiRoot(item.GetProperty("base").GetString()!));
        }
        Assert.Equal(Contract.GetProperty("display_placeholder").GetString(), Activation.DisplayServerUrl(Activation.PlaceholderCloudUrl));
        Assert.Equal(Activation.SyncTokenName, Contract.GetProperty("sync_token_name").GetString());

        var database = @"C:\ProgramData\ERPFood\PDV\pdv_local.db";
        Assert.Equal(Contract.GetProperty("staged_name").GetString(), Path.GetFileName(StagedActivation.StagedPath(database)));
        Assert.Equal(Contract.GetProperty("archive_name").GetString(),
            Path.GetFileName(StagedActivation.ArchivePath(database, new DateTime(2026, 9, 25, 18, 7, 3))));
    }

    // -- a conversa com a retaguarda ------------------------------------------------

    private sealed class StubHandler(Func<HttpRequestMessage, string, HttpResponseMessage> respond) : HttpMessageHandler
    {
        public HttpRequestMessage? Request { get; private set; }
        public string Body { get; private set; } = "";

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            Request = request;
            Body = request.Content is null ? "" : await request.Content.ReadAsStringAsync(cancellationToken);
            return respond(request, Body);
        }
    }

    private static HttpResponseMessage Reply(HttpStatusCode status, string json) =>
        new(status) { Content = new StringContent(json, Encoding.UTF8, "application/json") };

    private static Task<ActivationResult> Call(StubHandler handler, string baseUrl = "https://teste.rsrassessoria.com.br/api") =>
        new HttpActivationTransport(baseUrl, new HttpClient(handler)).ActivateAsync(
            "ABCDEFGHJKMN", new MachineFingerprint("CAIXA-01", "Windows 11", "AMD64"), [0xAB, 0x01, 0xFF],
            CancellationToken.None);

    [Fact]
    public async Task Sends_what_the_cloud_route_validates()
    {
        var handler = new StubHandler((_, _) => Reply(HttpStatusCode.OK,
            """{"tenant_id":"t","store_id":"s","device_id":"d","sync_token":"tok","store_name":"Pool Bar","cloud_base_url":"https://x/api"}"""));
        var result = await Call(handler, "https://teste.rsrassessoria.com.br");

        Assert.Equal(HttpMethod.Post, handler.Request!.Method);
        Assert.Equal("https://teste.rsrassessoria.com.br/api/devices/activate", handler.Request.RequestUri!.ToString());
        using var body = JsonDocument.Parse(handler.Body);
        Assert.Equal("ABCDEFGHJKMN", body.RootElement.GetProperty("activation_code").GetString());
        Assert.Equal("ab01ff", body.RootElement.GetProperty("device_secret_hex").GetString());
        var fingerprint = body.RootElement.GetProperty("fingerprint");
        Assert.Equal("CAIXA-01", fingerprint.GetProperty("hostname").GetString());
        Assert.Equal("Windows 11", fingerprint.GetProperty("os").GetString());
        Assert.Equal("AMD64", fingerprint.GetProperty("arch").GetString());
        Assert.Equal(new ActivationResult("t", "s", "d", "tok", "Pool Bar", "https://x/api"), result);
    }

    [Fact]
    public async Task A_refused_code_shows_the_server_reason()
    {
        var handler = new StubHandler((_, _) => Reply(HttpStatusCode.Gone,
            """{"detail":"Código inválido, expirado ou já utilizado. Gere um novo no painel administrativo."}"""));
        var error = await Assert.ThrowsAsync<ActivationRefusedException>(() => Call(handler));
        Assert.Equal("Código inválido, expirado ou já utilizado. Gere um novo no painel administrativo.", error.Message);
    }

    [Fact]
    public async Task A_server_error_is_transient_and_not_a_verdict()
    {
        var handler = new StubHandler((_, _) => Reply(HttpStatusCode.BadGateway, "<html>502</html>"));
        var error = await Assert.ThrowsAsync<ActivationException>(() => Call(handler));
        Assert.IsNotType<ActivationRefusedException>(error);
        Assert.Equal("A retaguarda respondeu 502. Tente novamente em alguns minutos.", error.Message);
    }

    [Fact]
    public async Task Too_many_attempts_is_not_a_verdict_on_the_code()
    {
        var handler = new StubHandler((_, _) => Reply(HttpStatusCode.TooManyRequests, """{"detail":"Muitas tentativas."}"""));
        var error = await Assert.ThrowsAsync<ActivationException>(() => Call(handler));
        Assert.IsNotType<ActivationRefusedException>(error);
    }

    [Fact]
    public async Task No_network_says_so()
    {
        var handler = new StubHandler((_, _) => throw new HttpRequestException("Nenhuma conexão pôde ser feita"));
        var error = await Assert.ThrowsAsync<ActivationException>(() => Call(handler));
        Assert.StartsWith("Não foi possível falar com a retaguarda:", error.Message);
    }

    [Fact]
    public async Task An_incomplete_answer_is_refused_before_anything_is_saved()
    {
        var handler = new StubHandler((_, _) => Reply(HttpStatusCode.OK, """{"tenant_id":"t","store_id":"s"}"""));
        var error = await Assert.ThrowsAsync<ActivationException>(() => Call(handler));
        Assert.Equal("Resposta de ativação incompleta: falta device_id, sync_token.", error.Message);
    }

    [Fact]
    public async Task Without_a_cloud_address_the_one_typed_is_kept()
    {
        var handler = new StubHandler((_, _) => Reply(HttpStatusCode.OK,
            """{"tenant_id":"t","store_id":"s","device_id":"d","sync_token":"tok","store_name":"","cloud_base_url":""}"""));
        var result = await Call(handler);
        Assert.Equal("https://teste.rsrassessoria.com.br/api", result.CloudBaseUrl);
    }

    // -- o que fica gravado -------------------------------------------------------

    private sealed class FixedTransport(ActivationResult result, Action? before = null) : IActivationTransport
    {
        public string? Code { get; private set; }
        public byte[]? Secret { get; private set; }

        public Task<ActivationResult> ActivateAsync(string code, MachineFingerprint fingerprint, byte[] deviceSecret, CancellationToken cancellation)
        {
            Code = code;
            Secret = deviceSecret;
            before?.Invoke();
            return Task.FromResult(result);
        }
    }

    private static Dictionary<string, string> Settings(PdvDatabase database)
    {
        var values = new Dictionary<string, string>();
        using var command = database.Connection.CreateCommand();
        command.CommandText = "SELECT key, value FROM device_settings";
        using var reader = command.ExecuteReader();
        while (reader.Read()) values[reader.GetString(0)] = reader.GetString(1);
        return values;
    }

    [Fact]
    public async Task Activation_saves_the_identity_and_the_token_where_the_python_pdv_reads_them()
    {
        using var database = new PdvDatabase(_file.Path);
        var vault = new SecretVault(_vaultFolder);
        var transport = new FixedTransport(Granted);

        var result = await Activation.ActivateAsync("abcd-efgh-jkmn", database, vault, transport);

        Assert.Equal(Granted, result);
        Assert.Equal("ABCDEFGHJKMN", transport.Code);
        // O segredo do ledger sobe na ativação — e é o do cofre, não um novo.
        Assert.Equal(vault.Load("device_secret"), transport.Secret);
        Assert.Equal("token-de-sincronizacao-de-teste", Encoding.UTF8.GetString(vault.Load(Activation.SyncTokenName)!));

        var profile = TerminalProfile.Load(database);
        Assert.True(profile.Activated);
        Assert.Equal(Granted.TenantId, profile.TenantId);
        Assert.Equal(Granted.StoreId, profile.StoreId);
        Assert.Equal(Granted.DeviceId, profile.DeviceId);
        Assert.Equal("Pool Bar", profile.StoreName);
        Assert.Equal("https://teste.rsrassessoria.com.br/api", profile.CloudBaseUrl);
        Assert.DoesNotContain(Settings(database).Values, value => value.Contains("token-de-sincronizacao"));
    }

    [Fact]
    public async Task Without_the_token_in_the_vault_the_terminal_is_not_marked_activated()
    {
        using var database = new PdvDatabase(_file.Path);
        var vault = new SecretVault(_vaultFolder);
        // O cofre quebra depois de a retaguarda responder: no lugar do arquivo
        // do token há uma pasta.
        var transport = new FixedTransport(Granted, () => Directory.CreateDirectory(Path.Combine(_vaultFolder, "sync_token.bin")));

        var error = await Assert.ThrowsAsync<SecretVaultException>(() => Activation.ActivateAsync("ABCDEFGHJKMN", database, vault, transport));
        Assert.Contains("sync_token", error.Message);
        Assert.False(TerminalProfile.Load(database).Activated);
        Assert.Empty(Settings(database));
    }

    [Fact]
    public async Task Moving_to_another_store_with_unsent_sales_is_blocked()
    {
        using var database = new PdvDatabase(_file.Path);
        database.Execute("INSERT INTO device_settings (key, value, updated_at) VALUES ('device.tenant_id', 'outro-tenant', 'x')");
        database.Execute(
            "INSERT INTO sync_outbox (entity_table, entity_id, client_uuid, operation, payload_json, available_at, created_at) " +
            "VALUES ('orders', 'o-1', 'c-1', 'insert', '{}', 'x', 'x')");
        var vault = new SecretVault(_vaultFolder);

        var error = await Assert.ThrowsAsync<ActivationBlockedException>(
            () => Activation.ActivateAsync("ABCDEFGHJKMN", database, vault, new FixedTransport(Granted)));
        Assert.Contains("1 registro(s)", error.Message);
        Assert.Equal("outro-tenant", Settings(database)["device.tenant_id"]);
        Assert.False(vault.Exists(Activation.SyncTokenName));

        await Activation.ActivateAsync("ABCDEFGHJKMN", database, vault, new FixedTransport(Granted), force: true);
        Assert.Equal(Granted.TenantId, Settings(database)["device.tenant_id"]);
    }

    [Fact]
    public async Task Reactivating_the_same_store_with_a_queue_is_allowed()
    {
        using var database = new PdvDatabase(_file.Path);
        database.Execute("INSERT INTO device_settings (key, value, updated_at) VALUES ('device.tenant_id', $t, 'x')", ("$t", Granted.TenantId));
        database.Execute(
            "INSERT INTO sync_outbox (entity_table, entity_id, client_uuid, operation, payload_json, available_at, created_at) " +
            "VALUES ('orders', 'o-1', 'c-1', 'insert', '{}', 'x', 'x')");

        await Activation.ActivateAsync("ABCDEFGHJKMN", database, new SecretVault(_vaultFolder), new FixedTransport(Granted));
        Assert.True(TerminalProfile.Load(database).Activated);
    }

    // -- a troca do banco de demonstração -------------------------------------------

    private static List<string> Schema(SqliteConnection connection)
    {
        using var command = connection.CreateCommand();
        command.CommandText = "SELECT type || ' ' || name || ' ' || coalesce(sql, '') FROM sqlite_master ORDER BY type, name";
        using var reader = command.ExecuteReader();
        var rows = new List<string>();
        while (reader.Read()) rows.Add(reader.GetString(0));
        return rows;
    }

    [Fact]
    public void The_staged_database_has_the_same_schema_and_no_demo_data()
    {
        using var current = new PdvDatabase(_file.Path);
        Fixtures.SeedCatalog(current);
        current.Execute("INSERT INTO device_settings (key, value, updated_at) VALUES ('store.name', 'Confeitaria Demo', 'x')");

        using var staged = StagedActivation.CreateStaged(current);

        Assert.Equal(StagedActivation.StagedPath(_file.Path), staged.Path);
        Assert.Equal(PdvDatabase.SupportedSchemaVersion, staged.SchemaVersion);
        Assert.Equal(Schema(current.Connection), Schema(staged.Connection));
        foreach (var table in new[] { "products", "recipes", "inventory_items", "device_settings", "users", "orders" })
        {
            Assert.Equal(0L, staged.Scalar($"SELECT COUNT(*) FROM {table}"));
        }
        // Os gatilhos vieram junto: a cadeia de auditoria continua sem UPDATE.
        Assert.Contains(Schema(staged.Connection), row => row.StartsWith("trigger ", StringComparison.Ordinal));
    }

    [Fact]
    public void A_leftover_from_a_failed_attempt_is_replaced()
    {
        using var current = new PdvDatabase(_file.Path);
        using (var first = StagedActivation.CreateStaged(current))
        {
            first.Execute("INSERT INTO device_settings (key, value, updated_at) VALUES ('device.activated', '1', 'x')");
        }
        using var second = StagedActivation.CreateStaged(current);
        Assert.Equal(0L, second.Scalar("SELECT COUNT(*) FROM device_settings"));
    }

    [Fact]
    public void Promotion_archives_the_demo_and_puts_the_store_database_in_place()
    {
        using (var current = new PdvDatabase(_file.Path))
        {
            current.Execute("INSERT INTO device_settings (key, value, updated_at) VALUES ('store.name', 'Confeitaria Demo', 'x')");
            using var staged = StagedActivation.CreateStaged(current);
            staged.Execute("INSERT INTO device_settings (key, value, updated_at) VALUES ('store.name', 'Pool Bar', 'x')");
        }

        var archive = StagedActivation.Promote(_file.Path, new DateTime(2026, 9, 25, 18, 7, 3));

        Assert.Equal(StagedActivation.ArchivePath(_file.Path, new DateTime(2026, 9, 25, 18, 7, 3)), archive);
        Assert.False(File.Exists(StagedActivation.StagedPath(_file.Path)));
        using (var promoted = new PdvDatabase(_file.Path))
        {
            Assert.Equal("Pool Bar", promoted.Scalar("SELECT value FROM device_settings WHERE key = 'store.name'"));
        }
        using var demo = new PdvDatabase(archive!);
        Assert.Equal("Confeitaria Demo", demo.Scalar("SELECT value FROM device_settings WHERE key = 'store.name'"));
    }

    [Fact]
    public void Without_a_pending_activation_nothing_moves()
    {
        Assert.Null(StagedActivation.Promote(_file.Path));
        Assert.True(File.Exists(_file.Path));
    }

    [Fact]
    public void A_database_still_open_elsewhere_is_not_half_moved()
    {
        using (var current = new PdvDatabase(_file.Path))
        using (StagedActivation.CreateStaged(current))
        {
        }

        using (new FileStream(_file.Path, FileMode.Open, FileAccess.Read, FileShare.None))
        {
            var error = Assert.Throws<StagedActivationException>(
                () => StagedActivation.Promote(_file.Path, wait: TimeSpan.FromMilliseconds(600)));
            Assert.Contains("ainda está usando o banco", error.Message);
        }
        // Nada foi movido: a próxima abertura tenta de novo do mesmo estado.
        Assert.True(File.Exists(StagedActivation.StagedPath(_file.Path)));
        Assert.True(File.Exists(_file.Path));
        Assert.NotNull(StagedActivation.Promote(_file.Path));
    }

    /// <summary>
    /// A volta do contrato: um terminal ativado pelo C#, aberto pelo Python.
    /// Com <c>PDV_CROSSCHECK_OUT</c> definido (o CI define), grava
    /// <c>activation/</c> ao lado dele para o <c>crosscheck.py</c>.
    /// </summary>
    [Fact]
    public async Task Writes_an_activated_terminal_for_the_python_pdv_to_open()
    {
        var target = Environment.GetEnvironmentVariable("PDV_CROSSCHECK_OUT");
        var folder = string.IsNullOrEmpty(target)
            ? Path.Combine(Path.GetDirectoryName(_file.Path)!, "activation")
            : Path.Combine(Path.GetDirectoryName(target)!, "activation");
        if (Directory.Exists(folder)) Directory.Delete(folder, recursive: true);
        Directory.CreateDirectory(folder);
        var path = Path.Combine(folder, "pdv_local.db");
        File.Copy(_file.Path, path);

        using (var demo = new PdvDatabase(path))
        {
            Fixtures.SeedCatalog(demo);
            using var staged = StagedActivation.CreateStaged(demo);
            await Activation.ActivateAsync("ABCD-EFGH-JKMN", staged, new SecretVault(Path.Combine(folder, "secrets")), new FixedTransport(Granted));
            staged.Execute("PRAGMA wal_checkpoint(TRUNCATE)");
        }
        Assert.NotNull(StagedActivation.Promote(path));
        using var promoted = new PdvDatabase(path);
        Assert.True(TerminalProfile.Load(promoted).Activated);
    }
}
