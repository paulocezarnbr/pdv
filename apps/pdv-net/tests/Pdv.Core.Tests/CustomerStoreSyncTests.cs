using System.Text.Json;
using Pdv.Data;
using Pdv.Data.Customers;
using Pdv.Data.Sales;
using Pdv.Data.Sync;

namespace Pdv.Core.Tests;

/// <summary>
/// O cliente é do estabelecimento: o id sai da loja e do WhatsApp, e o cadastro
/// desce para todos os caixas da loja.
/// </summary>
public sealed class CustomerStoreSyncTests : IDisposable
{
    private static readonly byte[] Secret = Enumerable.Range(0, 32).Select(i => (byte)i).ToArray();
    private static readonly TerminalIdentity CaixaUm = new("tenant-1", "store-1", "device-1");
    private static readonly TerminalIdentity CaixaDois = new("tenant-1", "store-1", "device-2");

    private readonly TestDatabase _fileOne = new(userVersion: PdvDatabase.SupportedSchemaVersion);
    private readonly TestDatabase _fileTwo = new(userVersion: PdvDatabase.SupportedSchemaVersion);
    private readonly PdvDatabase _one;
    private readonly PdvDatabase _two;
    private readonly ManualClock _clock = new(new DateTimeOffset(2026, 9, 26, 15, 0, 0, TimeSpan.Zero));
    private readonly List<string> _log = [];

    public CustomerStoreSyncTests()
    {
        _one = new PdvDatabase(_fileOne.Path);
        _two = new PdvDatabase(_fileTwo.Path);
    }

    public void Dispose()
    {
        _one.Dispose();
        _two.Dispose();
        _fileOne.Dispose();
        _fileTwo.Dispose();
    }

    private CustomerLedgers Customers(PdvDatabase database, TerminalIdentity terminal) =>
        new(database, terminal, new AuditLedger(terminal.TenantId, terminal.StoreId, terminal.DeviceId, Secret, _clock), _clock);

    private static JsonElement Row(object row) => JsonSerializer.SerializeToElement(row);

    /// <summary>Uma linha como o Postgres a devolve: booleano, DATE como meia-noite, instante em "Z".</summary>
    private static JsonElement Cloud(string id, string? phone, string name = "Lia Cliente", string updatedAt = "2026-09-26T16:00:00.000Z",
        string? email = "lia@exemplo.com", string? cpf = null, string tenant = "tenant-1") => Row(new Dictionary<string, object?>
    {
        ["id"] = id, ["tenant_id"] = tenant, ["store_id"] = "store-1", ["client_uuid"] = "cu-" + id, ["name"] = name,
        ["phone"] = phone, ["email"] = email, ["cpf"] = cpf, ["is_resident"] = true, ["unit_block"] = "B",
        ["unit_number"] = "101", ["birth_date"] = "1990-09-26T00:00:00.000Z", ["marketing_opt_in"] = true,
        ["marketing_opt_in_at"] = "2026-09-26T15:00:00.000Z", ["is_active"] = true,
        ["created_at"] = "2026-09-26T15:00:00.000Z", ["updated_at"] = updatedAt, ["server_seq"] = "42",
    });

    private bool Pull(PdvDatabase database, JsonElement row) =>
        PullMapping.MapRow("customers", row, "tenant-1", "store-1") is { } mapped &&
        database.InTransaction(transaction => CustomerPull.Apply(transaction, mapped, _log.Add));

    private static object? Value(PdvDatabase database, string column, string id) =>
        database.Scalar($"SELECT {column} FROM customers WHERE id = $id", ("$id", id)) is var value && value is DBNull ? null : value;

    // -- o id do cliente -------------------------------------------------------

    [Fact]
    public void The_id_is_a_standard_uuid_version_5() =>
        // O vetor da RFC 4122: espaço de nomes DNS e "www.example.com".
        Assert.Equal("2ed6657d-e927-568b-95e1-2665a8aea6a2",
            CustomerIdentity.UuidV5(new Guid("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), "www.example.com"));

    [Fact]
    public void The_same_person_in_the_same_store_is_the_same_id_in_any_terminal()
    {
        var one = Customers(_one, CaixaUm).Register(new CustomerProfile("Lia", "21998765432"));
        var two = Customers(_two, CaixaDois).Register(new CustomerProfile("Lia Souza", "(21) 99876-5432"));

        Assert.Equal(one, two);
        Assert.Equal(CustomerIdentity.IdFor("tenant-1", "store-1", "21998765432", null), one);
        Assert.NotEqual(one, CustomerIdentity.IdFor("tenant-1", "store-2", "21998765432", null));
        // Sem WhatsApp, o CPF identifica.
        Assert.Equal(CustomerIdentity.IdFor("tenant-1", "store-1", null, "52998224725"),
            Customers(_one, CaixaUm).Register(new CustomerProfile("Rui", Cpf: "529.982.247-25")));
    }

    [Fact]
    public void A_number_that_changed_owner_gets_a_new_id()
    {
        var customers = Customers(_one, CaixaUm);
        var first = customers.Register(new CustomerProfile("Dona Antiga", "21998765432"));
        customers.Update(first, new CustomerProfile("Dona Antiga", "21911112222"));

        var second = customers.Register(new CustomerProfile("Dono Novo", "21998765432"));

        Assert.NotEqual(first, second);
        Assert.Equal("Dono Novo", customers.Get(second)!.Profile.Name);
    }

    // -- a descida para os caixas da loja --------------------------------------

    [Fact]
    public void A_customer_registered_in_another_terminal_arrives_here_whole()
    {
        Assert.True(Pull(_one, Cloud("c-1", "21998765432")));

        var record = Customers(_one, CaixaUm).Get("c-1")!;
        Assert.Equal(new CustomerProfile("Lia Cliente", "21998765432", "lia@exemplo.com", null, true, "B", "101",
            new DateOnly(1990, 9, 26), true), record.Profile);
        Assert.Equal("2026-09-26T15:00:00.000+00:00", record.MarketingOptInAt);
        Assert.Equal("2026-09-26T16:00:00.000+00:00", Value(_one, "updated_at", "c-1"));
        Assert.Equal(1L, Value(_one, "is_synced", "c-1"));
        Assert.Single(Customers(_one, CaixaUm).Find("21998765432"));
    }

    [Fact]
    public void A_newer_edit_here_is_not_undone_by_an_older_version_from_the_cloud()
    {
        Pull(_one, Cloud("c-1", "21998765432"));
        _clock.SetWallClock(new DateTimeOffset(2026, 9, 26, 17, 0, 0, TimeSpan.Zero));
        Customers(_one, CaixaUm).Update("c-1", new CustomerProfile("Lia Corrigida", "21998765432"));

        Assert.False(Pull(_one, Cloud("c-1", "21998765432", name: "Lia Velha", updatedAt: "2026-09-26T16:30:00.000Z")));
        Assert.Equal("Lia Corrigida", Value(_one, "name", "c-1"));

        Assert.True(Pull(_one, Cloud("c-1", "21998765432", name: "Lia Nova", updatedAt: "2026-09-26T18:00:00.000Z")));
        Assert.Equal("Lia Nova", Value(_one, "name", "c-1"));
    }

    [Fact]
    public void An_email_erased_in_the_cloud_is_erased_here()
    {
        Pull(_one, Cloud("c-1", "21998765432"));
        Pull(_one, Cloud("c-1", "21998765432", email: null, updatedAt: "2026-09-26T18:00:00.000Z"));
        Assert.Null(Value(_one, "email", "c-1"));
    }

    [Fact]
    public void Another_network_is_never_applied() =>
        Assert.False(Pull(_one, Cloud("c-1", "21998765432", tenant: "outra-rede")));

    [Fact]
    public void Two_records_with_the_same_whatsapp_end_the_same_in_every_terminal()
    {
        // Um cadastro antigo (id aleatório) e o derivado, da mesma pessoa. Cada
        // caixa recebe os dois numa ordem; os dois terminam iguais.
        var older = Cloud("a-antigo", "21998765432", name: "Lia (antigo)");
        var derived = Cloud("f-derivado", "21998765432", name: "Lia");

        Pull(_one, older);
        Pull(_one, derived);
        Pull(_two, derived);
        Pull(_two, older);

        foreach (var database in new[] { _one, _two })
        {
            Assert.Equal("21998765432", Value(database, "phone", "a-antigo"));
            Assert.Null(Value(database, "phone", "f-derivado"));
            Assert.Equal("Lia", Value(database, "name", "f-derivado"));
        }
        Assert.Contains(_log, line => line.Contains("mesmo phone"));
    }

    [Fact]
    public void The_cpf_is_resolved_the_same_way()
    {
        Pull(_one, Cloud("b-2", null, cpf: "52998224725"));
        Pull(_one, Cloud("a-1", "21998765432", cpf: "52998224725"));

        Assert.Equal("52998224725", Value(_one, "cpf", "a-1"));
        Assert.Null(Value(_one, "cpf", "b-2"));
    }

    [Fact]
    public async Task The_sync_cycle_asks_for_the_customers_of_the_store()
    {
        var cloud = new CustomersCloud(Cloud("c-9", "21955554444"));
        var engine = new SyncEngine(_one, cloud, new TerminalProfile("tenant-1", "store-1", "device-1", "Loja", true, null),
            _clock, 200, _log.Add);

        Assert.Equal(1, await engine.PullOnceAsync());

        Assert.Contains("customers", cloud.Asked);
        Assert.Equal("21955554444", Value(_one, "phone", "c-9"));
        Assert.Equal(42L, new CursorStore(_one).Get("customers"));
    }

    private sealed class CustomersCloud(JsonElement row) : ISyncTransport
    {
        public List<string> Asked { get; } = [];

        public Task<IReadOnlyList<ItemAck>> PushAsync(PushBatch batch, CancellationToken cancellation) =>
            Task.FromResult<IReadOnlyList<ItemAck>>([]);

        public Task<PullResponse> PullAsync(PullRequest request, CancellationToken cancellation)
        {
            Asked.Add(request.EntityTable);
            return Task.FromResult(request.EntityTable == "customers"
                ? new PullResponse("customers", [row], 42)
                : new PullResponse(request.EntityTable, [], request.SinceServerSeq));
        }

        public Task<long> HeartbeatAsync(TerminalHealth health, CancellationToken cancellation) => Task.FromResult(0L);
    }
}
