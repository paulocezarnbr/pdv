using System.Text.Json;
using Pdv.Core.Remote;
using Pdv.Core.Scale;
using Pdv.Data;
using Pdv.Data.Auth;
using Pdv.Data.Remote;
using Pdv.Data.Sales;
using Pdv.Data.Sync;

namespace Pdv.Core.Tests;

/// <summary>Os comandos do painel e as sete travas, sobre o banco real (C5b-3).</summary>
public sealed class RemoteCommandTests : IDisposable
{
    private static readonly byte[] Secret = Enumerable.Range(0, 32).Select(i => (byte)i).ToArray();
    private static readonly TerminalProfile Terminal = new("tenant-1", "store-1", "device-1", "Pool Bar", true, "https://x/api");

    private static readonly JsonElement Pins =
        JsonDocument.Parse(File.ReadAllText(TestDatabase.Contract("pin-hashes.json"))).RootElement.GetProperty("hashes")[0].Clone();

    private static readonly string Pin = Pins.GetProperty("pin").GetString()!;

    private readonly TestDatabase _file = new();
    private readonly PdvDatabase _database;
    private readonly ManualClock _clock = new(new DateTimeOffset(2026, 9, 25, 12, 0, 0, TimeSpan.Zero));
    private readonly AuditLedger _ledger;
    private readonly RemoteCommandService _service;
    private readonly List<string> _changed = [];
    private readonly List<string> _log = [];

    public RemoteCommandTests()
    {
        _database = new PdvDatabase(_file.Path);
        Fixtures.SeedCatalog(_database);
        var hash = Pins.GetProperty("hash").GetString()!;
        foreach (var (id, name, login, role, auth, discount) in new[]
                 {
                     ("u-caixa", "Ana Caixa", "ana", "cashier", 0, "0"),
                     ("u-gerente", "Bruno Gerente", "bruno", "manager", 1, "30"),
                     ("u-dona", "Carla Dona", "carla", "owner", 1, "100"),
                     ("u-garcom", "Davi Garçom", "davi", "waiter", 0, "0"),
                 })
        {
            _database.Execute(
                "INSERT INTO users (id, tenant_id, name, login, role, pin_hash, max_discount_percent, can_authorize, is_active, updated_at) " +
                "VALUES ($id, 'tenant-1', $name, $login, $role, $hash, $discount, $auth, 1, 'x')",
                ("$id", id), ("$name", name), ("$login", login), ("$role", role), ("$hash", hash),
                ("$discount", discount), ("$auth", auth));
        }
        _ledger = new AuditLedger(Terminal.TenantId, Terminal.StoreId, Terminal.DeviceId, Secret, _clock);
        _service = new RemoteCommandService(
            _database, Terminal, Secret, _ledger, new StaffAuthentication(_database, Terminal.TenantId, _clock), _clock, _log.Add);
        _service.OrderChanged += _changed.Add;
    }

    public void Dispose()
    {
        _database.Dispose();
        _file.Dispose();
    }

    // -- apoio ---------------------------------------------------------------

    private ItemResult Torta(string? orderId = null) =>
        new ItemRegistration(_database, Terminal.Identity, _ledger, clock: _clock).RegisterWeighedItem(
            orderId, new Catalog(_database.Connection, "tenant-1").Get("p-torta-kg")!,
            new ToledoPrix3Protocol().Parse("00847"u8.ToArray(), _clock.GetUtcNow()), "u-caixa");

    private ItemResult Refri(string orderId) =>
        new ItemRegistration(_database, Terminal.Identity, _ledger, clock: _clock).RegisterUnitItem(
            orderId, new Catalog(_database.Connection, "tenant-1").Get("p-refri")!, 1m, "u-caixa");

    private int _sequence;

    /// <summary>Um comando como a nuvem emite: assinado com a chave do terminal.</summary>
    private RemoteCommand Issue(
        string kind, object payload, string issuedBy = "u-gerente", string name = "Bruno Gerente",
        string? issuedAt = null, string device = "device-1", string tenant = "tenant-1", byte[]? secret = null)
    {
        var element = JsonSerializer.SerializeToElement(payload);
        var uuid = $"cmd-{++_sequence}";
        var at = issuedAt ?? "2026-09-25T11:59:00.000Z";
        return new RemoteCommand(uuid, tenant, "store-1", device, kind, element, issuedBy, name, at,
            CommandProtocol.Sign(secret ?? Secret, uuid, device, kind, element, at));
    }

    private ApplyReport Deliver(params RemoteCommand[] commands)
    {
        foreach (var command in commands) _service.Inbox.Accept(command);
        return _service.ApplyPending();
    }

    private string? Result(RemoteCommand command) =>
        _database.Scalar("SELECT result_message FROM remote_commands WHERE command_uuid = $u", ("$u", command.CommandUuid)) as string;

    private (string Type, string Severity, string Actor, JsonElement Payload) LastAudit()
    {
        using var read = _database.Connection.CreateCommand();
        read.CommandText = "SELECT event_type, severity, actor_user_id, payload_json FROM audit_ledger ORDER BY seq DESC LIMIT 1";
        using var reader = read.ExecuteReader();
        reader.Read();
        return (reader.GetString(0), reader.GetString(1), reader.GetString(2),
            JsonDocument.Parse(reader.GetString(3)).RootElement.Clone());
    }

    private long Scalar(string sql) => Convert.ToInt64(_database.Scalar(sql));

    // -- desconto ------------------------------------------------------------

    [Fact]
    public void A_signed_discount_within_the_ceiling_is_applied_and_audited_as_remote()
    {
        var torta = Torta();
        var command = Issue(CommandProtocol.ApplyDiscount, new { order_id = torta.Order.Id, percent = 10, reason = "Cliente fiel" });

        Assert.Equal(new ApplyReport(Applied: 1), Deliver(command));

        Assert.Equal(423, Scalar($"SELECT discount_cents FROM orders WHERE id = '{torta.Order.Id}'"));
        Assert.Equal("applied", _service.Inbox.StatusOf(command.CommandUuid));
        var audit = LastAudit();
        Assert.Equal(("discount_applied", "warning", "u-gerente"), (audit.Type, audit.Severity, audit.Actor));
        Assert.Equal("remote_panel", audit.Payload.GetProperty("channel").GetString());
        Assert.Equal("device-1", audit.Payload.GetProperty("target_device_id").GetString());
        Assert.Equal("10", audit.Payload.GetProperty("percent").GetString());
        Assert.Equal([torta.Order.Id], _changed);
        Assert.Contains("desconto de 10% (R$ 4.23)", string.Join(" ", _log));
        _ledger.Verify(_database.Connection);
    }

    [Fact]
    public void Redelivery_never_doubles_the_discount()
    {
        var torta = Torta();
        var command = Issue(CommandProtocol.ApplyDiscount, new { order_id = torta.Order.Id, percent = "10", reason = "x" });
        Deliver(command);

        Assert.False(_service.Inbox.Accept(command));
        Assert.Equal(new ApplyReport(), _service.ApplyPending());
        Assert.Equal(1, Scalar("SELECT COUNT(*) FROM audit_ledger WHERE event_type = 'discount_applied'"));
    }

    [Fact]
    public void The_second_of_two_racing_executions_settles_nothing()
    {
        // Duas execuções do ciclo leram o mesmo pendente; a primeira fechou.
        var torta = Torta();
        var command = Issue(CommandProtocol.ApplyDiscount, new { order_id = torta.Order.Id, percent = 10, reason = "x" });
        _service.Inbox.Accept(command);

        Assert.True(_database.InTransaction(tx => _service.Inbox.SettleIn(tx, command.CommandUuid, "applied", "aplicado")));
        Assert.False(_database.InTransaction(tx => _service.Inbox.SettleIn(tx, command.CommandUuid, "refused", "tarde")));

        Assert.Equal("applied", _service.Inbox.StatusOf(command.CommandUuid));
        Assert.Equal("aplicado", Result(command));
    }

    [Fact]
    public void Being_far_away_does_not_raise_the_ceiling()
    {
        var torta = Torta();
        var command = Issue(CommandProtocol.ApplyDiscount, new { order_id = torta.Order.Id, percent = 31, reason = "x" });

        Assert.Equal(new ApplyReport(Refused: 1), Deliver(command));
        Assert.Equal("Bruno Gerente pode conceder até 30% — o comando pede 31%.", Result(command));
        Assert.Equal(0, Scalar($"SELECT discount_cents FROM orders WHERE id = '{torta.Order.Id}'"));
        var audit = LastAudit();
        Assert.Equal(("remote_command_refused", "warning"), (audit.Type, audit.Severity));
    }

    public static TheoryData<string, string, string> Forgeries() => new()
    {
        { "assinatura de outra chave", "Assinatura inválida.", "critical" },
        { "payload alterado depois de assinado", "Assinatura inválida.", "critical" },
        { "outro terminal", "Comando endereçado a outro terminal.", "critical" },
        { "outra rede", "Comando de outra rede.", "critical" },
        { "emitido há 13 h", "Comando fora da janela de validade — emita novamente.", "warning" },
        { "emitido daqui a 10 min", "Comando fora da janela de validade — emita novamente.", "warning" },
    };

    [Theory]
    [MemberData(nameof(Forgeries))]
    public void A_forged_or_stale_command_is_refused_and_recorded(string forgery, string message, string severity)
    {
        var torta = Torta();
        object payload = new { order_id = torta.Order.Id, percent = 10, reason = "x" };
        var command = forgery switch
        {
            "assinatura de outra chave" => Issue(CommandProtocol.ApplyDiscount, payload, secret: new byte[32]),
            "payload alterado depois de assinado" => Issue(CommandProtocol.ApplyDiscount, payload) with
            {
                Payload = JsonSerializer.SerializeToElement(new { order_id = torta.Order.Id, percent = 30, reason = "x" }),
            },
            "outro terminal" => Issue(CommandProtocol.ApplyDiscount, payload, device: "device-2"),
            "outra rede" => Issue(CommandProtocol.ApplyDiscount, payload, tenant: "tenant-2"),
            "emitido há 13 h" => Issue(CommandProtocol.ApplyDiscount, payload, issuedAt: "2026-09-24T23:00:00+00:00"),
            _ => Issue(CommandProtocol.ApplyDiscount, payload, issuedAt: "2026-09-25T12:10:00+00:00"),
        };

        Assert.Equal(new ApplyReport(Refused: 1), Deliver(command));
        Assert.Equal(message, Result(command));
        Assert.Equal(("remote_command_refused", severity), (LastAudit().Type, LastAudit().Severity));
        Assert.Equal(0, Scalar($"SELECT discount_cents FROM orders WHERE id = '{torta.Order.Id}'"));
        Assert.Empty(_changed);
    }

    [Fact]
    public void The_local_switch_turns_the_channel_off_without_asking_the_cloud()
    {
        var torta = Torta();
        _database.Execute("INSERT INTO device_settings (key, value, updated_at) VALUES ('remote.commands_enabled', '0', 'x')");
        var command = Issue(CommandProtocol.ApplyDiscount, new { order_id = torta.Order.Id, percent = 10, reason = "x" });

        Deliver(command);
        Assert.Equal("Canal de comando remoto desligado neste terminal.", Result(command));
    }

    [Fact]
    public void A_closed_sale_is_never_rewritten_from_the_panel()
    {
        var torta = Torta();
        _database.Execute($"UPDATE orders SET status = 'paid' WHERE id = '{torta.Order.Id}'");
        var command = Issue(CommandProtocol.ApplyDiscount, new { order_id = torta.Order.Id, percent = 10, reason = "x" });

        Deliver(command);
        Assert.Equal("O pedido já foi fechado. Venda fechada se corrige por estorno, não pelo painel.", Result(command));
    }

    [Theory]
    [InlineData("{\"percent\":10,\"reason\":\"x\"}", "Comando sem `order_id`.")]
    [InlineData("{\"order_id\":\"ORDER\",\"percent\":\"abc\",\"reason\":\"x\"}", "Comando com `percent` inválido.")]
    [InlineData("{\"order_id\":\"ORDER\",\"percent\":0,\"reason\":\"x\"}", "`percent` fora da faixa (maior que 0, até 100).")]
    [InlineData("{\"order_id\":\"ORDER\",\"percent\":10,\"reason\":\"   \"}", "Desconto remoto exige motivo.")]
    [InlineData("{\"order_id\":\"nao-existe\",\"percent\":10,\"reason\":\"x\"}", "Pedido não encontrado neste terminal.")]
    public void A_payload_that_makes_no_sense_is_refused_with_the_reason(string json, string message)
    {
        var torta = Torta();
        var command = Issue(CommandProtocol.ApplyDiscount,
            JsonDocument.Parse(json.Replace("ORDER", torta.Order.Id)).RootElement);
        Deliver(command);
        Assert.Equal(message, Result(command));
    }

    [Fact]
    public void An_issuer_deactivated_here_releases_nothing()
    {
        var torta = Torta();
        _database.Execute("UPDATE users SET is_active = 0 WHERE id = 'u-gerente'");
        var command = Issue(CommandProtocol.ApplyDiscount, new { order_id = torta.Order.Id, percent = 5, reason = "x" });
        Deliver(command);
        Assert.Equal("Quem emitiu o comando não existe ou está inativo neste terminal.", Result(command));
    }

    // -- cancelamento --------------------------------------------------------

    [Fact]
    public void A_manager_cancels_from_the_panel_and_the_stock_comes_back()
    {
        var torta = Torta();
        Refri(torta.Order.Id);
        var before = Scalar("SELECT balance_mg FROM inventory_items WHERE id = 'farinha'");
        var command = Issue(CommandProtocol.CancelItem,
            new { order_id = torta.Order.Id, order_item_id = torta.Item.Id, reason = "Pedido trocado" });

        Assert.Equal(new ApplyReport(Applied: 1), Deliver(command));

        Assert.Equal("[remote_panel] Pedido trocado",
            _database.Scalar($"SELECT cancel_reason FROM order_items WHERE id = '{torta.Item.Id}'"));
        Assert.Equal(333, Scalar($"SELECT total_cents FROM orders WHERE id = '{torta.Order.Id}'"));
        Assert.True(Scalar("SELECT balance_mg FROM inventory_items WHERE id = 'farinha'") > before);
        var audit = LastAudit();
        Assert.Equal(("item_canceled", "critical", "u-gerente"), (audit.Type, audit.Severity, audit.Actor));
        Assert.False(audit.Payload.TryGetProperty("kitchen_status", out _));
        _ledger.Verify(_database.Connection);
    }

    [Fact]
    public void An_owner_does_not_cancel_from_the_panel_as_in_python()
    {
        var torta = Torta();
        var command = Issue(CommandProtocol.CancelItem,
            new { order_id = torta.Order.Id, order_item_id = torta.Item.Id, reason = "x" }, "u-dona", "Carla Dona");
        Deliver(command);
        Assert.Equal("Cancelamento de item exige autorização de gerente.", Result(command));
    }

    [Fact]
    public void An_item_canceled_twice_is_refused_the_second_time()
    {
        var torta = Torta();
        Deliver(Issue(CommandProtocol.CancelItem, new { order_id = torta.Order.Id, order_item_id = torta.Item.Id, reason = "a" }));
        var again = Issue(CommandProtocol.CancelItem, new { order_id = torta.Order.Id, order_item_id = torta.Item.Id, reason = "b" });
        Deliver(again);
        Assert.Equal("O item já estava cancelado.", Result(again));
    }

    // -- aceite no caixa (trava 7) -------------------------------------------

    private RemoteCommand CancelWhatTheKitchenHas(string status = "preparing")
    {
        var torta = Torta();
        _database.Execute(
            "INSERT INTO kds_tickets (id, tenant_id, store_id, order_id, order_item_id, product_name, status, queued_at, " +
            "created_at, updated_at, origin_device_id, client_uuid) " +
            "VALUES ('k-1', 'tenant-1', 'store-1', $order, $item, 'Mousse a granel', $status, 'x', 'x', 'x', 'device-1', 'k-uuid')",
            ("$order", torta.Order.Id), ("$item", torta.Item.Id), ("$status", status));
        return Issue(CommandProtocol.CancelItem,
            new { order_id = torta.Order.Id, order_item_id = torta.Item.Id, reason = "Cliente desistiu" });
    }

    [Fact]
    public void What_the_kitchen_already_has_waits_for_someone_at_the_counter()
    {
        var command = CancelWhatTheKitchenHas();

        Assert.Equal(new ApplyReport(Awaiting: 1), Deliver(command));
        Assert.Equal("pending", _service.Inbox.StatusOf(command.CommandUuid));
        var waiting = Assert.Single(_service.Awaiting());
        Assert.Equal("Bruno Gerente pede cancelar Mousse a granel (R$ 42,27) — em preparo na cozinha. Motivo: Cliente desistiu",
            waiting.Note);
        Assert.Equal(command.CommandUuid, Assert.Single(_service.Inbox.UnreportedAwaiting()).CommandUuid);
        Assert.Empty(_service.Inbox.Unreported());

        // O ciclo seguinte não duplica a espera.
        Assert.Equal(new ApplyReport(Awaiting: 1), _service.ApplyPending());
        Assert.Equal(0, Scalar("SELECT COUNT(*) FROM order_items WHERE canceled_at IS NOT NULL"));
    }

    [Fact]
    public void The_cashier_accepts_with_the_pin_and_the_kitchen_ticket_leaves()
    {
        var command = CancelWhatTheKitchenHas();
        Deliver(command);

        var message = _service.Confirm(command.CommandUuid, "ana", Pin);

        Assert.Equal("item Mousse a granel cancelado com aceite de Ana Caixa no caixa", message);
        Assert.Equal("canceled", _database.Scalar("SELECT status FROM kds_tickets WHERE id = 'k-1'"));
        var audit = LastAudit();
        Assert.Equal("u-caixa", audit.Payload.GetProperty("confirmed_by_user_id").GetString());
        Assert.Equal("preparing", audit.Payload.GetProperty("kitchen_status").GetString());
        Assert.Equal("applied", _service.Inbox.StatusOf(command.CommandUuid));
    }

    [Fact]
    public void The_kitchen_screen_hears_the_cancel_after_the_commit()
    {
        // Com o salão no ar, a tela da cozinha tira o prato da fila na hora — e
        // não só quando reconectar, como fazia antes: até lá ela mandaria
        // preparar um prato que já saiu da conta.
        var hub = new Data.Edge.EventHub(_clock);
        using var kitchen = hub.Subscribe(["ticket.changed"]);
        var service = new RemoteCommandService(
            _database, Terminal, Secret, _ledger, new StaffAuthentication(_database, Terminal.TenantId, _clock), _clock,
            _log.Add, hub);
        var command = CancelWhatTheKitchenHas();
        _service.Inbox.Accept(command);
        Assert.Equal(new ApplyReport(Awaiting: 1), service.ApplyPending());
        Assert.False(kitchen.TryNext(out _));

        service.Confirm(command.CommandUuid, "ana", Pin);

        Assert.True(kitchen.TryNext(out var changed));
        Assert.Equal(("k-1", "canceled"),
            (changed!.Payload["ticket_id"]!.GetValue<string>(), changed.Payload["status"]!.GetValue<string>()));
        Assert.False(kitchen.TryNext(out _));
    }

    [Fact]
    public void A_waiter_cannot_accept_the_cancel_of_his_own_table()
    {
        var command = CancelWhatTheKitchenHas();
        Deliver(command);

        var error = Assert.Throws<ConfirmationException>(() => _service.Confirm(command.CommandUuid, "davi", Pin));
        Assert.Contains("exige alguém do caixa", error.Message);
        Assert.Equal("pending", _service.Inbox.StatusOf(command.CommandUuid));
    }

    [Fact]
    public void A_wrong_pin_does_not_decide_the_command()
    {
        var command = CancelWhatTheKitchenHas();
        Deliver(command);

        Assert.Throws<AuthenticationException>(() => _service.Confirm(command.CommandUuid, "ana", "999999"));
        Assert.Equal("pending", _service.Inbox.StatusOf(command.CommandUuid));
    }

    [Fact]
    public void Declining_needs_a_reason_and_goes_back_to_the_panel_with_the_name()
    {
        var command = CancelWhatTheKitchenHas();
        Deliver(command);

        Assert.Throws<ConfirmationException>(() => _service.Decline(command.CommandUuid, "ana", Pin, "  \n "));
        _service.Decline(command.CommandUuid, "ana", Pin, "  prato   já   servido ");

        Assert.Equal("refused", _service.Inbox.StatusOf(command.CommandUuid));
        Assert.Equal("Recusado no caixa por Ana Caixa: prato já servido", Result(command));
        Assert.Equal("u-caixa", LastAudit().Payload.GetProperty("declined_by_user_id").GetString());
        Assert.Equal(0, Scalar("SELECT COUNT(*) FROM order_items WHERE canceled_at IS NOT NULL"));
    }

    [Fact]
    public void Accepting_does_not_revive_what_expired_in_the_meantime()
    {
        var command = CancelWhatTheKitchenHas();
        Deliver(command);
        _clock.Advance(TimeSpan.FromHours(13));

        Assert.Throws<CommandRefusedException>(() => _service.Confirm(command.CommandUuid, "ana", Pin));
        Assert.Equal("refused", _service.Inbox.StatusOf(command.CommandUuid));
    }

    [Fact]
    public void Only_what_the_terminal_put_on_hold_can_be_accepted_at_the_counter()
    {
        var torta = Torta();
        var command = Issue(CommandProtocol.ApplyDiscount, new { order_id = torta.Order.Id, percent = 5, reason = "x" });
        _service.Inbox.Accept(command);

        var error = Assert.Throws<ConfirmationException>(() => _service.Confirm(command.CommandUuid, "ana", Pin));
        Assert.Equal("Este comando não está esperando aceite.", error.Message);
    }

    // -- o ciclo com a nuvem -------------------------------------------------

    private sealed class CommandCloud : ISyncTransport, ICommandTransport
    {
        public List<RemoteCommand> Queue { get; } = [];

        public List<(IReadOnlyList<CommandResult> Results, IReadOnlyList<AwaitingNotice> Awaiting)> Reports { get; } = [];

        public bool FailFetch { get; set; }

        public bool FailReport { get; set; }

        public Func<IReadOnlyList<CommandResult>, IReadOnlyList<AwaitingNotice>, IReadOnlyList<string>>? Accept { get; set; }

        public Task<IReadOnlyList<RemoteCommand>> FetchCommandsAsync(
            string tenantId, string storeId, string deviceId, int limit, CancellationToken cancellation) =>
            FailFetch
                ? throw new TransportException("rede caiu")
                : Task.FromResult<IReadOnlyList<RemoteCommand>>(Queue.ToList());

        public Task<IReadOnlyList<string>> ReportCommandsAsync(
            string tenantId, string storeId, string deviceId, IReadOnlyList<CommandResult> results,
            IReadOnlyList<AwaitingNotice> awaiting, CancellationToken cancellation)
        {
            if (FailReport) throw new TransportException("rede caiu no relato");
            Reports.Add((results, awaiting));
            var named = Accept?.Invoke(results, awaiting)
                        ?? results.Select(r => r.CommandUuid).Concat(awaiting.Select(a => a.CommandUuid)).ToList();
            return Task.FromResult(named);
        }

        public Task<IReadOnlyList<ItemAck>> PushAsync(PushBatch batch, CancellationToken cancellation) =>
            Task.FromResult<IReadOnlyList<ItemAck>>([]);

        public Task<PullResponse> PullAsync(PullRequest request, CancellationToken cancellation) =>
            Task.FromResult(new PullResponse(request.EntityTable, [], request.SinceServerSeq));

        public Task<long> HeartbeatAsync(TerminalHealth health, CancellationToken cancellation) => Task.FromResult(0L);
    }

    private SyncEngine Engine(CommandCloud cloud) =>
        new(_database, cloud, Terminal, _clock, log: _log.Add, commands: _service);

    [Fact]
    public async Task Fetch_apply_then_report_and_a_failed_report_never_reapplies()
    {
        var torta = Torta();
        var cloud = new CommandCloud { FailReport = true };
        cloud.Queue.Add(Issue(CommandProtocol.ApplyDiscount, new { order_id = torta.Order.Id, percent = 10, reason = "x" }));

        var first = await Engine(cloud).CommandCycleAsync();
        Assert.Equal((1, 1, 1, 0), (first.Fetched, first.Accepted, first.Applied, first.Reported));
        Assert.NotNull(first.Error);
        Assert.Single(_service.Inbox.Unreported());

        // A nuvem reentrega (não soube do resultado) e o relato volta a funcionar.
        cloud.FailReport = false;
        var second = await Engine(cloud).CommandCycleAsync();
        Assert.Equal((0, 0, 1), (second.Accepted, second.Applied, second.Reported));
        Assert.Empty(_service.Inbox.Unreported());
        Assert.Equal(1, Scalar("SELECT COUNT(*) FROM audit_ledger WHERE event_type = 'discount_applied'"));
        Assert.Equal("applied", cloud.Reports.Single().Results.Single().Status);
    }

    [Fact]
    public async Task Only_what_the_cloud_named_leaves_the_report_queue()
    {
        var torta = Torta();
        var cloud = new CommandCloud { Accept = (_, _) => ["inventado", "outro"] };
        cloud.Queue.Add(Issue(CommandProtocol.ApplyDiscount, new { order_id = torta.Order.Id, percent = 10, reason = "x" }));
        cloud.Queue.Add(CancelWhatTheKitchenHas());

        var report = await Engine(cloud).CommandCycleAsync();

        Assert.Equal((1, 1, 0), (report.Applied, report.Awaiting, report.Reported));
        Assert.Single(_service.Inbox.Unreported());
        Assert.Single(_service.Inbox.UnreportedAwaiting());
    }

    [Fact]
    public async Task A_failed_fetch_has_no_effect_and_a_cloud_without_commands_is_skipped()
    {
        var cloud = new CommandCloud { FailFetch = true };
        var report = await Engine(cloud).CommandCycleAsync();
        Assert.Equal("rede caiu", report.Error);
        Assert.Equal(0, Scalar("SELECT COUNT(*) FROM remote_commands"));

        var withoutService = new SyncEngine(_database, cloud, Terminal, _clock);
        Assert.False(withoutService.SpeaksCommands);
        Assert.Equal(new CommandCycleReport(), await withoutService.CommandCycleAsync());
    }

    [Theory]
    [InlineData("{\"kind\":\"open_drawer\"}")]
    [InlineData("{\"kind\":\"apply_discount\",\"payload\":\"x\"}")]
    [InlineData("{\"kind\":\"apply_discount\",\"payload\":{}}")]
    [InlineData("[]")]
    public void A_command_this_terminal_cannot_read_is_dropped_not_obeyed(string json)
    {
        var entry = JsonDocument.Parse(json).RootElement;
        Assert.Null(HttpSyncTransport.ParseCommand(entry));
    }

    [Fact]
    public void A_well_formed_command_is_read_with_its_payload()
    {
        var entry = JsonDocument.Parse(
            """
            {"command_uuid":"c","tenant_id":"t","store_id":"s","device_id":"d","kind":"cancel_item",
             "payload":{"order_id":"o"},"issued_by_user_id":"u","issued_at":"2026-09-25T12:00:00Z","signature":"ab"}
            """).RootElement;
        var command = HttpSyncTransport.ParseCommand(entry)!;
        Assert.Equal(("c", "cancel_item", "", "o"),
            (command.CommandUuid, command.Kind, command.IssuedByName, command.Payload.GetProperty("order_id").GetString()));
    }
}
