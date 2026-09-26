using System.Net;
using System.Text;
using System.Text.Json;
using Pdv.Data;
using Pdv.Data.Sync;

namespace Pdv.Core.Tests;

/// <summary>
/// A sincronização contra <c>contracts/sync.json</c> (gerado pelo Python) e
/// contra cada falha de rede que decide entre venda perdida, duplicada ou segura.
/// </summary>
public sealed class SyncTests : IDisposable
{
    private static readonly JsonElement Contract =
        JsonDocument.Parse(File.ReadAllText(TestDatabase.Contract("sync.json"))).RootElement.Clone();

    private static readonly byte[] Secret = Enumerable.Range(0, 32).Select(i => (byte)i).ToArray();
    private static readonly TerminalProfile Terminal = new(
        "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222",
        "33333333-3333-3333-3333-333333333333", "Pool Bar", true, "https://teste.rsrassessoria.com.br/api");

    private readonly TestDatabase _file = new();
    private readonly PdvDatabase _database;
    private readonly ManualClock _clock = new(new DateTimeOffset(2026, 9, 25, 12, 0, 0, TimeSpan.Zero));
    private readonly FakeCloud _cloud = new();
    private readonly List<string> _log = [];

    public SyncTests()
    {
        _database = new PdvDatabase(_file.Path);
    }

    public void Dispose()
    {
        _database.Dispose();
        _file.Dispose();
    }

    private SyncEngine Engine(int batchSize = 200) => new(_database, _cloud, Terminal, _clock, batchSize, _log.Add);

    /// <summary>Elos de auditoria: cada um entra no outbox, como numa venda.</summary>
    private List<string> Sales(int count)
    {
        var ledger = new AuditLedger(Terminal.TenantId, Terminal.StoreId, Terminal.DeviceId, Secret, _clock);
        var seqs = _database.InTransaction(tx => Enumerable.Range(1, count)
            .Select(i => ledger.Append(tx, "sale_closed", "user-1", new Dictionary<string, object?> { ["total_cents"] = i * 100 }).Seq)
            .ToList());
        return seqs.Select(seq => (string)_database.Scalar(
            "SELECT client_uuid FROM audit_ledger WHERE seq = $seq", ("$seq", seq))!).ToList();
    }

    private long Pending() => Convert.ToInt64(_database.Scalar("SELECT COUNT(*) FROM sync_outbox"));

    private long Synced() => Convert.ToInt64(_database.Scalar("SELECT COUNT(*) FROM audit_ledger WHERE is_synced = 1 AND synced_at IS NOT NULL"));

    /// <summary>
    /// A nuvem de mentira: responde conforme o roteiro do teste, e guarda o
    /// que recebeu — inclusive o que "aplicou" antes de a resposta se perder.
    /// </summary>
    private sealed class FakeCloud : ISyncTransport
    {
        public List<PushBatch> Pushes { get; } = [];
        public HashSet<string> Stored { get; } = [];
        public Func<PushBatch, IReadOnlyList<ItemAck>>? Answer { get; set; }
        public Exception? Failure { get; set; }
        public bool ApplyThenLoseAnswer { get; set; }
        public Dictionary<string, Queue<PullResponse>> Pulls { get; } = [];
        public List<PullRequest> PullRequests { get; } = [];
        public List<TerminalHealth> Heartbeats { get; } = [];
        public long Drift { get; set; }

        /// <summary>Com atraso, dois envios simultâneos se sobreporiam — e o contador acusa.</summary>
        public bool SlowPush { get; set; }
        public int MaxConcurrentPushes { get; private set; }
        private int _inFlight;

        public async Task<IReadOnlyList<ItemAck>> PushAsync(PushBatch batch, CancellationToken cancellation)
        {
            var now = Interlocked.Increment(ref _inFlight);
            MaxConcurrentPushes = Math.Max(MaxConcurrentPushes, now);
            try
            {
                if (SlowPush) await Task.Delay(20, cancellation);
                return await PushCoreAsync(batch);
            }
            finally
            {
                Interlocked.Decrement(ref _inFlight);
            }
        }

        private Task<IReadOnlyList<ItemAck>> PushCoreAsync(PushBatch batch)
        {
            Pushes.Add(batch);
            if (Failure is not null) throw Failure;
            if (ApplyThenLoseAnswer)
            {
                foreach (var item in batch.Items) Stored.Add(item.ClientUuid);
                throw new TransportException("Falha de rede: timeout depois do commit");
            }
            if (Answer is not null) return Task.FromResult(Answer(batch));
            var acks = batch.Items.Select(item => new ItemAck(
                item.ClientUuid, Stored.Add(item.ClientUuid) ? ItemStatus.Applied : ItemStatus.Duplicate)).ToList();
            return Task.FromResult<IReadOnlyList<ItemAck>>(acks);
        }

        public Task<PullResponse> PullAsync(PullRequest request, CancellationToken cancellation)
        {
            PullRequests.Add(request);
            if (Failure is not null) throw Failure;
            return Task.FromResult(Pulls.TryGetValue(request.EntityTable, out var queue) && queue.Count > 0
                ? queue.Dequeue()
                : new PullResponse(request.EntityTable, [], request.SinceServerSeq));
        }

        public Task<long> HeartbeatAsync(TerminalHealth health, CancellationToken cancellation)
        {
            Heartbeats.Add(health);
            if (Failure is not null) throw Failure;
            return Task.FromResult(Drift);
        }
    }

    // -- contrato com o Python ---------------------------------------------------

    [Fact]
    public void The_idempotency_key_is_the_python_one()
    {
        foreach (var batch in Contract.GetProperty("idempotency").EnumerateArray())
        {
            var uuids = batch.GetProperty("client_uuids").EnumerateArray().Select(u => u.GetString()!).ToList();
            Assert.Equal(batch.GetProperty("key").GetString(),
                PushBatch.IdempotencyKeyFor(batch.GetProperty("device_id").GetString()!, uuids));
        }
    }

    [Fact]
    public void An_unknown_status_is_never_success()
    {
        foreach (var entry in Contract.GetProperty("statuses").EnumerateArray())
        {
            var status = entry.GetProperty("status");
            var parsed = ItemStatuses.Parse(status.ValueKind == JsonValueKind.Null ? null : status.GetString());
            Assert.Equal(entry.GetProperty("verdict").GetString(), parsed.Wire());
        }
    }

    [Fact]
    public void Cloud_rows_are_mapped_like_the_python_pdv()
    {
        var config = Contract.GetProperty("config");
        var index = 0;
        foreach (var entry in Contract.GetProperty("rows").EnumerateArray())
        {
            index++;
            var mapped = PullMapping.MapRow(
                entry.GetProperty("table").GetString()!, entry.GetProperty("row"),
                config.GetProperty("tenant_id").GetString()!, config.GetProperty("store_id").GetString()!);
            var expected = entry.GetProperty("mapped");
            if (expected.ValueKind == JsonValueKind.Null)
            {
                Assert.True(mapped is null, $"linha {index} deveria ser descartada");
                continue;
            }
            Assert.True(mapped is not null, $"linha {index} deveria ser aplicada");
            var wanted = expected.EnumerateObject().ToDictionary(
                p => p.Name, p => p.Value.ValueKind == JsonValueKind.Number ? (object)p.Value.GetInt64() : p.Value.GetString()!);
            var actual = mapped!.ToDictionary(p => p.Key, p => p.Value is int i ? (long)i : p.Value);
            Assert.Equal(wanted.OrderBy(p => p.Key), actual.OrderBy(p => p.Key));
        }
    }

    [Fact]
    public void Queue_and_worker_numbers_are_the_python_ones()
    {
        var outbox = Contract.GetProperty("outbox");
        Assert.Equal(outbox.GetProperty("syncable_tables").EnumerateArray().Select(t => t.GetString()).Order(),
            OutboxReader.SyncableTables.Order());
        Assert.Equal(OutboxReader.MaxAttempts, outbox.GetProperty("max_attempts").GetInt32());
        Assert.Equal(OutboxReader.MaxBackoffSeconds, outbox.GetProperty("max_backoff_seconds").GetInt32());
        foreach (var attempt in outbox.GetProperty("backoff_by_attempt").EnumerateObject())
        {
            Assert.Equal(attempt.Value.GetInt32(), OutboxReader.BackoffSeconds(int.Parse(attempt.Name)));
        }

        var pull = Contract.GetProperty("pull");
        Assert.Equal(pull.GetProperty("pullable").EnumerateArray().Select(t => t.GetString()), PullMapping.PullableTables);
        Assert.Equal(pull.GetProperty("mapped").EnumerateArray().Select(t => t.GetString()), PullMapping.MappedTables.Order());
        Assert.Equal(pull.GetProperty("not_applied").EnumerateArray().Select(t => t.GetString()), PullMapping.NotApplied.Keys.Order());

        var worker = Contract.GetProperty("worker");
        Assert.Equal(SyncWorker.IdleInterval, TimeSpan.FromSeconds(worker.GetProperty("idle_seconds").GetInt32()));
        Assert.Equal(SyncWorker.BusyInterval, TimeSpan.FromSeconds(worker.GetProperty("busy_seconds").GetInt32()));
        Assert.Equal(SyncWorker.ErrorInterval, TimeSpan.FromSeconds(worker.GetProperty("error_seconds").GetInt32()));
        Assert.Equal(SyncWorker.PullEveryNCycles, worker.GetProperty("pull_every_n_cycles").GetInt32());
        Assert.Equal(SyncEngine.ClockSkewWarningMs, worker.GetProperty("clock_skew_warning_ms").GetInt64());
    }

    // -- envio ------------------------------------------------------------------

    [Fact]
    public async Task Confirmed_items_leave_the_queue_and_the_entity_is_marked()
    {
        var uuids = Sales(3);
        var report = await Engine().PushOnceAsync();

        Assert.Equal(new SyncReport(3, 3), report);
        Assert.Equal(0, Pending());
        Assert.Equal(3, Synced());
        var batch = Assert.Single(_cloud.Pushes);
        Assert.Equal(uuids, batch.Items.Select(item => item.ClientUuid));
        Assert.Equal((Terminal.DeviceId, Terminal.TenantId, Terminal.StoreId), (batch.DeviceId, batch.TenantId, batch.StoreId));
    }

    [Fact]
    public async Task The_answer_lost_after_the_cloud_commit_is_resent_and_settles_as_duplicate()
    {
        var uuids = Sales(2);
        _cloud.ApplyThenLoseAnswer = true;

        var lost = await Engine().PushOnceAsync();
        Assert.Equal(2, lost.Deferred);
        Assert.NotNull(lost.Error);
        Assert.Equal(2, Pending());   // nada saiu da fila
        Assert.Equal(0, Synced());

        _cloud.ApplyThenLoseAnswer = false;
        _clock.Advance(TimeSpan.FromSeconds(3));
        var resent = await Engine().PushOnceAsync();

        Assert.Equal(2, resent.Settled);
        Assert.Equal(0, Pending());
        // Mesmo conteúdo, mesma chave: a nuvem reconhece a repetição.
        Assert.Equal(_cloud.Pushes[0].IdempotencyKey, _cloud.Pushes[1].IdempotencyKey);
        Assert.Equal(uuids.ToHashSet(), _cloud.Stored);
    }

    [Fact]
    public async Task A_deferred_batch_waits_for_its_backoff()
    {
        Sales(1);
        _cloud.Failure = new TransportException("HTTP 502: Bad Gateway");
        await Engine().PushOnceAsync();

        _cloud.Failure = null;
        Assert.Equal(new SyncReport(), await Engine().PushOnceAsync());   // antes dos 2 s
        _clock.Advance(TimeSpan.FromSeconds(2));
        Assert.Equal(1, (await Engine().PushOnceAsync()).Settled);
    }

    [Fact]
    public async Task A_rejected_item_goes_to_quarantine_and_is_never_deleted()
    {
        var uuids = Sales(2);
        _cloud.Answer = batch =>
        [
            new ItemAck(uuids[0], ItemStatus.Applied),
            new ItemAck(uuids[1], ItemStatus.Rejected, "cadeia quebrada"),
        ];

        var report = await Engine().PushOnceAsync();

        Assert.Equal((1, 1), (report.Settled, report.Rejected));
        Assert.Equal(1, Pending());
        Assert.Equal((long)OutboxReader.MaxAttempts, _database.Scalar("SELECT attempts FROM sync_outbox"));
        Assert.Equal($"audit_ledger#{uuids[1][..8]}: cadeia quebrada", _database.Scalar("SELECT last_error FROM sync_outbox"));
        _clock.Advance(TimeSpan.FromHours(1));
        Assert.Equal(0, (await Engine().PushOnceAsync()).Sent);   // fora do caminho da fila
        Assert.Equal(1, Engine().QuarantinedCount());
    }

    [Fact]
    public async Task Silence_about_an_item_is_not_success()
    {
        var uuids = Sales(2);
        _cloud.Answer = batch => [new ItemAck(uuids[0], ItemStatus.Duplicate)];

        var report = await Engine().PushOnceAsync();

        Assert.Equal((1, 1), (report.Settled, report.Deferred));
        Assert.Equal(uuids[1], _database.Scalar("SELECT client_uuid FROM sync_outbox"));
        Assert.Equal("sem veredito do servidor", _database.Scalar("SELECT last_error FROM sync_outbox"));
    }

    [Fact]
    public async Task A_revoked_terminal_keeps_its_sales()
    {
        Sales(1);
        _cloud.Failure = new AuthException("Terminal não autorizado (HTTP 401)");

        var report = await Engine().PushOnceAsync();

        Assert.Equal("auth: Terminal não autorizado (HTTP 401)", report.Error);
        Assert.Equal(1, Pending());
    }

    [Fact]
    public async Task Batches_go_in_order_and_drain_stops_on_error()
    {
        var uuids = Sales(5);
        var report = await Engine(batchSize: 2).DrainAsync();

        Assert.Equal(new SyncReport(5, 5), report);
        Assert.Equal([2, 2, 1], _cloud.Pushes.Select(p => p.Items.Count));
        Assert.Equal(uuids, _cloud.Pushes.SelectMany(p => p.Items).Select(i => i.ClientUuid));

        Sales(3);
        _cloud.Failure = new TransportException("Falha de rede");
        var failed = await Engine(batchSize: 1).DrainAsync();
        Assert.Equal(1, failed.Sent);   // parou no primeiro erro
    }

    [Fact]
    public async Task After_25_attempts_the_item_stops_blocking_the_queue()
    {
        Sales(1);
        _cloud.Failure = new TransportException("Falha de rede");
        for (var i = 0; i < OutboxReader.MaxAttempts; i++)
        {
            await Engine().PushOnceAsync();
            _clock.Advance(TimeSpan.FromSeconds(OutboxReader.MaxBackoffSeconds));
        }
        Assert.Equal(0, (await Engine().PushOnceAsync()).Sent);
        Assert.Equal(1, Pending());   // nunca apagado
    }

    [Fact]
    public async Task The_payload_goes_up_exactly_as_it_was_queued()
    {
        _database.InTransaction(tx => new Outbox(_clock).Enqueue(tx, "orders", "o-1", "c-1", "insert",
            new Dictionary<string, object?> { ["peso_kg"] = 0.345, ["nome"] = "Pão — ção 🍰", ["n"] = 12345678901234 }));
        await Engine().PushOnceAsync();
        var queued = JsonDocument.Parse("{\"peso_kg\": 0.345, \"nome\": \"Pão — ção 🍰\", \"n\": 12345678901234}").RootElement;
        var sent = Assert.Single(Assert.Single(_cloud.Pushes).Items).Payload;
        Assert.Equal(queued.GetProperty("peso_kg").GetRawText(), sent.GetProperty("peso_kg").GetRawText());
        Assert.Equal("Pão — ção 🍰", sent.GetProperty("nome").GetString());
        Assert.Equal(12345678901234, sent.GetProperty("n").GetInt64());
    }

    // -- recebimento --------------------------------------------------------------

    private static JsonElement Row(string json) => JsonDocument.Parse(json).RootElement.Clone();

    [Fact]
    public async Task Pull_applies_users_and_products_and_advances_the_cursor()
    {
        _cloud.Pulls["users"] = new([new PullResponse("users",
        [
            Row($$"""{"id":"u-1","tenant_id":"{{Terminal.TenantId}}","name":"Ana","login":"ana","role":"cashier","pin_hash":"$argon2id$x","can_authorize":"t","updated_at":"2026-09-25T12:00:00.000+00:00","server_seq":"9"}"""),
            Row("""{"id":"u-2","tenant_id":"outro","name":"Intrusa","login":"x","role":"owner","updated_at":"x"}"""),
        ], 9)]);
        _cloud.Pulls["products"] = new([new PullResponse("products",
        [
            Row($$"""{"id":"p-1","tenant_id":"{{Terminal.TenantId}}","sku":"F1","name":"Fatia","pricing_mode":"unit","price_cents":"1450","updated_at":"2026-09-25T12:00:00.000+00:00","server_seq":700,"novidade":"ignorada"}"""),
        ], 700)]);

        var applied = await Engine().PullOnceAsync();

        Assert.Equal(2, applied);
        Assert.Equal("Ana", _database.Scalar("SELECT name FROM users WHERE id = 'u-1'"));
        Assert.Equal(1L, _database.Scalar("SELECT can_authorize FROM users WHERE id = 'u-1'"));
        Assert.Null(_database.Scalar("SELECT id FROM users WHERE id = 'u-2'"));
        Assert.Equal(Terminal.StoreId, _database.Scalar("SELECT store_id FROM products WHERE id = 'p-1'"));
        Assert.Equal(1450L, _database.Scalar("SELECT price_cents FROM products WHERE id = 'p-1'"));
        Assert.Equal(9L, new CursorStore(_database).Get("users"));
        Assert.Equal(700L, new CursorStore(_database).Get("products"));
        // O que o caixa não aplica, nem pede.
        Assert.Equal(["products", "users"], _cloud.PullRequests.Select(r => r.EntityTable));
    }

    [Fact]
    public async Task The_cloud_wins_on_catalog_and_a_null_keeps_the_local_value()
    {
        _database.Execute(
            "INSERT INTO products (id, tenant_id, store_id, sku, barcode, name, pricing_mode, price_cents, is_active, updated_at) " +
            "VALUES ('p-1', $t, $s, 'F1', '789', 'Antigo', 'unit', 1000, 1, 'x')", ("$t", Terminal.TenantId), ("$s", Terminal.StoreId));
        _cloud.Pulls["products"] = new([new PullResponse("products",
            [Row($$"""{"id":"p-1","tenant_id":"{{Terminal.TenantId}}","sku":"F1","barcode":null,"name":"Novo","pricing_mode":"unit","price_cents":1200,"updated_at":"y"}""")], 3)]);

        await Engine().PullOnceAsync();

        Assert.Equal("Novo", _database.Scalar("SELECT name FROM products WHERE id = 'p-1'"));
        Assert.Equal(1200L, _database.Scalar("SELECT price_cents FROM products WHERE id = 'p-1'"));
        Assert.Equal("789", _database.Scalar("SELECT barcode FROM products WHERE id = 'p-1'"));
    }

    [Fact]
    public async Task A_table_that_cannot_be_applied_keeps_its_cursor()
    {
        // pricing_mode fora do CHECK do schema: o SQLite recusa a linha.
        _cloud.Pulls["products"] = new([new PullResponse("products",
            [Row($$"""{"id":"p-1","tenant_id":"{{Terminal.TenantId}}","sku":"F1","name":"X","pricing_mode":"por_hora","price_cents":1,"updated_at":"y"}""")], 5)]);
        _cloud.Pulls["users"] = new([new PullResponse("users",
            [Row($$"""{"id":"u-1","tenant_id":"{{Terminal.TenantId}}","name":"Ana","login":"ana","role":"cashier","updated_at":"y"}""")], 4)]);

        var applied = await Engine().PullOnceAsync();

        Assert.Equal(1, applied);
        Assert.Equal(0L, new CursorStore(_database).Get("products"));
        Assert.Equal(4L, new CursorStore(_database).Get("users"));
    }

    // -- saúde e worker ------------------------------------------------------------

    [Fact]
    public async Task The_heartbeat_tells_the_truth_about_a_stuck_queue()
    {
        var uuids = Sales(2);
        _cloud.Answer = batch => [new ItemAck(uuids[0], ItemStatus.Rejected, "cadeia quebrada"), new ItemAck(uuids[1], ItemStatus.Rejected, "x")];
        await Engine().PushOnceAsync();
        Sales(1);
        _cloud.Drift = -200_000;

        var drift = await Engine().HeartbeatAsync();

        Assert.Equal(-200_000, drift);
        var health = Assert.Single(_cloud.Heartbeats);
        Assert.Equal((1L, 2L), (health.PendingItems, health.QuarantinedItems));
        Assert.Equal("2026-09-25T12:00:00.000+00:00", health.TerminalClock);
        Assert.NotNull(health.OldestPendingAt);
        Assert.StartsWith("audit_ledger#", health.LastQuarantineReason);
        Assert.Contains(_log, line => line.StartsWith("Relógio do caixa -200 s", StringComparison.Ordinal));
    }

    [Fact]
    public async Task The_worker_speeds_up_with_a_queue_and_backs_off_on_error()
    {
        var worker = new SyncWorker(Engine(batchSize: 1));
        var online = new List<bool>();
        worker.ConnectionChanged += (_, value) => online.Add(value);

        Assert.Equal(SyncWorker.IdleInterval, await worker.TickAsync());
        Sales(20);
        Assert.Equal(SyncWorker.BusyInterval, await worker.TickAsync());   // 10 lotes de 1; sobra fila

        _cloud.Failure = new TransportException("Falha de rede");
        _clock.Advance(TimeSpan.FromMinutes(1));
        Assert.Equal(SyncWorker.ErrorInterval, await worker.TickAsync());
        Assert.Equal([true, false], online);
        Assert.NotEmpty(_cloud.Heartbeats);   // relata mesmo depois de falhar
    }

    [Fact]
    public async Task Push_now_sends_the_queue_and_never_overlaps_a_cycle()
    {
        var worker = new SyncWorker(Engine(batchSize: 1));
        Sales(2);
        _cloud.SlowPush = true;

        await Task.WhenAll(
            Task.Run(() => worker.PushNowAsync()), Task.Run(() => worker.TickAsync()), Task.Run(() => worker.PushNowAsync()));

        Assert.Equal(0, Engine().PendingCount());
        Assert.Equal(1, _cloud.MaxConcurrentPushes);
    }

    [Fact]
    public async Task The_worker_pulls_every_20_cycles()
    {
        var worker = new SyncWorker(Engine());
        for (var i = 0; i < SyncWorker.PullEveryNCycles - 1; i++) await worker.TickAsync();
        Assert.Empty(_cloud.PullRequests);
        await worker.TickAsync();
        Assert.NotEmpty(_cloud.PullRequests);
    }

    // -- HTTP -------------------------------------------------------------------------

    private sealed class StubHandler(Func<HttpRequestMessage, HttpResponseMessage> respond) : HttpMessageHandler
    {
        public List<(HttpRequestMessage Request, string Body)> Seen { get; } = [];

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            Seen.Add((request, request.Content is null ? "" : await request.Content.ReadAsStringAsync(cancellationToken)));
            return respond(request);
        }
    }

    private static HttpResponseMessage Json(HttpStatusCode status, string body) =>
        new(status) { Content = new StringContent(body, Encoding.UTF8, "application/json") };

    [Fact]
    public async Task Push_over_http_carries_token_key_and_payload()
    {
        var handler = new StubHandler(_ => Json(HttpStatusCode.OK,
            """{"results":[{"client_uuid":"c-1","status":"applied","server_seq":"7"},{"client_uuid":"c-2","status":"novo"}]}"""));
        using var transport = new HttpSyncTransport("https://teste.rsrassessoria.com.br", "tok", new HttpClient(handler));
        var item = new OutboxItem(1, "orders", "o-1", "c-1", "insert", Row("""{"total_cents": 1450, "peso": 0.1}"""), 0);

        var acks = await transport.PushAsync(new PushBatch("d", "t", "s", [item]), CancellationToken.None);

        var (request, body) = Assert.Single(handler.Seen);
        Assert.Equal("https://teste.rsrassessoria.com.br/api/sync/push", request.RequestUri!.ToString());
        Assert.Equal("Bearer tok", request.Headers.Authorization!.ToString());
        Assert.Equal(PushBatch.IdempotencyKeyFor("d", ["c-1"]), request.Headers.GetValues("Idempotency-Key").Single());
        using var sent = JsonDocument.Parse(body);
        Assert.Equal("0.1", sent.RootElement.GetProperty("items")[0].GetProperty("payload").GetProperty("peso").GetRawText());
        Assert.Equal([new ItemAck("c-1", ItemStatus.Applied), new ItemAck("c-2", ItemStatus.Rejected)], acks);
    }

    [Theory]
    [InlineData(HttpStatusCode.Unauthorized, typeof(AuthException))]
    [InlineData(HttpStatusCode.Forbidden, typeof(AuthException))]
    [InlineData(HttpStatusCode.InternalServerError, typeof(TransportException))]
    [InlineData(HttpStatusCode.TooManyRequests, typeof(TransportException))]
    [InlineData(HttpStatusCode.BadRequest, typeof(TransportException))]
    public async Task Http_errors_map_to_retry_or_auth(HttpStatusCode status, Type expected)
    {
        var handler = new StubHandler(_ => Json(status, """{"detail":"x"}"""));
        using var transport = new HttpSyncTransport("https://x", "tok", new HttpClient(handler));
        var error = await Assert.ThrowsAnyAsync<SyncException>(
            () => transport.PushAsync(new PushBatch("d", "t", "s", []), CancellationToken.None));
        Assert.IsType(expected, error);
    }

    [Fact]
    public async Task A_garbled_answer_is_retried_not_trusted()
    {
        var handler = new StubHandler(_ => Json(HttpStatusCode.OK, "<html>proxy</html>"));
        using var transport = new HttpSyncTransport("https://x", "tok", new HttpClient(handler));
        await Assert.ThrowsAsync<TransportException>(
            () => transport.PushAsync(new PushBatch("d", "t", "s", []), CancellationToken.None));
    }

    [Fact]
    public async Task Pull_and_heartbeat_over_http()
    {
        var handler = new StubHandler(request => request.RequestUri!.AbsolutePath.EndsWith("/pull", StringComparison.Ordinal)
            ? Json(HttpStatusCode.OK, """{"rows":[{"id":"u-1"}],"last_server_seq":"42","has_more":true}""")
            : Json(HttpStatusCode.OK, """{"clock_drift_ms":-1500}"""));
        using var transport = new HttpSyncTransport("https://x/api", "tok", new HttpClient(handler));

        var pull = await transport.PullAsync(new PullRequest("t 1", "s", "users", 7), CancellationToken.None);
        var drift = await transport.HeartbeatAsync(new TerminalHealth("d", "t", "2026-09-25T12:00:00.000+00:00", 1, 0, null, null), CancellationToken.None);

        Assert.Equal("https://x/api/sync/pull?tenant_id=t%201&store_id=s&entity_table=users&since=7&limit=500",
            handler.Seen[0].Request.RequestUri!.AbsoluteUri);
        Assert.Equal((42L, true, 1), (pull.LastServerSeq, pull.HasMore, pull.Rows.Count));
        Assert.Equal(-1500, drift);
        using var health = JsonDocument.Parse(handler.Seen[1].Body);
        Assert.Equal(JsonValueKind.Null, health.RootElement.GetProperty("oldest_pending_at").ValueKind);
        Assert.Equal(1, health.RootElement.GetProperty("pending_items").GetInt32());
    }
}
