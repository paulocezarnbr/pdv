using System.Text.Json;
using Microsoft.Data.Sqlite;
using Pdv.Core.Audit;
using Pdv.Core.Tef;
using Pdv.Data;

namespace Pdv.Core.Tests;

public sealed class DataTests : IDisposable
{
    private static readonly byte[] Secret = Enumerable.Range(0, 32).Select(i => (byte)i).ToArray();

    private readonly TestDatabase _file = new();

    public void Dispose() => _file.Dispose();

    private AuditLedger Ledger() => new("tenant-1", "store-1", "device-1", Secret);

    private static Dictionary<string, object?> Payload(long total) => new() { ["total_cents"] = total, ["obs"] = "Pão" };

    // -- banco --------------------------------------------------------------

    [Fact]
    public void Opens_the_schema_the_python_pdv_migrates_to()
    {
        using var database = new PdvDatabase(_file.Path);
        Assert.Equal(PdvDatabase.SupportedSchemaVersion, database.SchemaVersion);
        Assert.Equal("wal", database.Scalar("PRAGMA journal_mode"));
        Assert.Equal(1L, database.Scalar("PRAGMA foreign_keys"));
    }

    [Theory]
    [InlineData(0, "não foi inicializado")]
    [InlineData(13, "Abra uma vez o PDV atual")]
    [InlineData(16, "Atualize o PDV")]
    public void Refuses_a_schema_it_does_not_know(int version, string advice)
    {
        using var other = new TestDatabase(version);
        var error = Assert.Throws<PdvDatabaseException>(() => new PdvDatabase(other.Path));
        Assert.Contains(advice, error.Message);
    }

    [Fact]
    public void A_missing_database_is_not_silently_created()
    {
        var path = System.IO.Path.Combine(System.IO.Path.GetTempPath(), Guid.NewGuid() + ".db");
        Assert.Throws<PdvDatabaseException>(() => new PdvDatabase(path));
        Assert.False(File.Exists(path));
    }

    // -- auditoria ----------------------------------------------------------

    [Fact]
    public void The_ledger_written_here_verifies()
    {
        using var database = new PdvDatabase(_file.Path);
        var ledger = Ledger();
        database.InTransaction(tx =>
        {
            ledger.Append(tx, "item_registered", "user-1", Payload(100));
            ledger.Append(tx, "sale_closed", "user-1", Payload(900), authorizerUserId: "gerente-1");
        });
        database.InTransaction(tx => ledger.Append(tx, "drawer_opened", "user-1", Payload(0), severity: "warning"));

        ledger.Verify(database.Connection);
        Assert.Equal(3L, database.Scalar("SELECT MAX(seq) FROM audit_ledger"));
        Assert.Equal(AuditChain.GenesisHash, database.Scalar("SELECT prev_hash FROM audit_ledger WHERE seq = 1"));
    }

    /// <summary>
    /// A volta do contrato: o ledger que o C# grava, verificado pelo código do
    /// Python. Com <c>PDV_CROSSCHECK_OUT</c> definido (o CI define), o banco é
    /// copiado para lá e <c>apps/pdv-net/crosscheck.py</c> roda o
    /// <c>AuditService.verify_chain</c> do PDV em Python sobre ele.
    /// </summary>
    [Fact]
    public void Writes_a_ledger_for_the_python_pdv_to_verify()
    {
        using (var database = new PdvDatabase(_file.Path))
        {
            var ledger = Ledger();
            database.InTransaction(tx =>
            {
                ledger.Append(tx, "item_registered", "user-1", new Dictionary<string, object?>
                {
                    ["produto"] = "Pão de Queijo — ção 🍰",
                    ["peso_kg"] = 0.345,
                    ["qtd"] = 2,
                    ["linhas"] = new object?[] { "a\nb", null, true, 1e-05 },
                });
                ledger.Append(tx, "sale_closed", "user-1", Payload(810), authorizerUserId: "gerente-1");
            });
            ledger.Verify(database.Connection);

            // A volta do contrato de PIN: hash gerado aqui, verificado lá.
            database.Execute(
                "INSERT INTO users (id, tenant_id, name, login, role, pin_hash, can_authorize, is_active, updated_at) " +
                "VALUES ('u-crosscheck', 'tenant-1', 'Cruzado', 'cruzado', 'cashier', $hash, 0, 1, $now)",
                ("$hash", Pdv.Data.Auth.PinHasher.Hash("480362")), ("$now", Iso.Now()));
            database.Execute("PRAGMA wal_checkpoint(TRUNCATE)");
        }

        var target = Environment.GetEnvironmentVariable("PDV_CROSSCHECK_OUT");
        if (!string.IsNullOrEmpty(target))
        {
            File.Copy(_file.Path, target, overwrite: true);
            // O cofre, para o Python abrir: DPAPI de máquina com a mesma entropia.
            new Pdv.Data.Secrets.SecretVault(Path.Combine(Path.GetDirectoryName(target)!, "csharp-vault"))
                .Store("device_secret", SecretVaultTests.Known);
        }
    }

    [Fact]
    public void Editing_the_file_by_hand_is_caught()
    {
        using var database = new PdvDatabase(_file.Path);
        var ledger = Ledger();
        database.InTransaction(tx =>
        {
            ledger.Append(tx, "sale_closed", "user-1", Payload(900));
            ledger.Append(tx, "sale_closed", "user-1", Payload(500));
        });

        // O schema do Python já barra a edição por trigger...
        Assert.Throws<SqliteException>(() =>
            database.Execute("UPDATE audit_ledger SET payload_json = '{}' WHERE seq = 2"));

        // ...então quem adultera derruba o trigger antes, num editor de SQLite.
        database.Execute("DROP TRIGGER trg_audit_no_update");
        database.Execute("UPDATE audit_ledger SET payload_json = replace(payload_json, '500', '5') WHERE seq = 2");
        var error = Assert.Throws<AuditChainException>(() => ledger.Verify(database.Connection));
        Assert.Contains("seq 2", error.Message);
    }

    [Fact]
    public void A_rolled_back_sale_leaves_no_trail_and_nothing_to_upload()
    {
        using var database = new PdvDatabase(_file.Path);
        var ledger = Ledger();
        Assert.Throws<InvalidOperationException>(() => database.InTransaction(tx =>
        {
            ledger.Append(tx, "sale_closed", "user-1", Payload(900));
            throw new InvalidOperationException("impressora sem papel");
        }));

        Assert.Equal(0L, database.Scalar("SELECT COUNT(*) FROM audit_ledger"));
        Assert.Equal(0L, Outbox.PendingCount(database.Connection));
    }

    [Fact]
    public void Each_link_goes_to_the_cloud_in_the_python_shape()
    {
        using var database = new PdvDatabase(_file.Path);
        var link = database.InTransaction(tx => Ledger().Append(tx, "sale_closed", "user-1", Payload(900)));

        var json = (string)database.Scalar("SELECT payload_json FROM sync_outbox WHERE entity_table = 'audit_ledger'")!;
        // json.dumps(sort_keys=True) sem `separators`: ", " e ": ".
        Assert.StartsWith("{\"actor_user_id\": \"user-1\", \"authorizer_user_id\": null, ", json);
        var outbox = JsonDocument.Parse(json).RootElement;
        Assert.Equal(link.Hash, outbox.GetProperty("hash").GetString());
        Assert.Equal(1, outbox.GetProperty("seq").GetInt64());
        Assert.Equal(link.PayloadJson, outbox.GetProperty("payload_json").GetString());
    }

    // -- diário do TEF ------------------------------------------------------

    [Fact]
    public async Task A_pending_card_survives_a_crash()
    {
        var tef = new TefSimulator();
        TefApproval approval;
        using (var journal = new SqliteTefJournal(_file.Path))
        {
            var outcome = await new TefCoordinator(tef, journal).AuthorizeAsync(
                "pedido-1", 4590, TefCardType.Credit, new SilentInteraction(), installments: 3);
            approval = Assert.IsType<TefOutcome.Approved>(outcome).Approval;
        }
        // queda: o processo morreu com a transação aprovada e não confirmada

        using var reopened = new SqliteTefJournal(_file.Path);
        var pending = Assert.Single(reopened.Pending());
        Assert.Equal(TefState.Approved, pending.State);
        Assert.Equal(approval, pending.Approval);

        var recovered = Assert.Single(await new TefCoordinator(tef, reopened).RecoverPendingAsync(_ => false));
        Assert.Equal(TefState.Undone, recovered.Resolution);
        Assert.Equal(HostState.Undone, tef.Host[approval.TransactionId]);
        Assert.Empty(reopened.Pending());
    }

    [Fact]
    public async Task The_journal_does_not_roll_back_with_the_sale()
    {
        using var database = new PdvDatabase(_file.Path);
        using var journal = new SqliteTefJournal(_file.Path);
        var coordinator = new TefCoordinator(new TefSimulator(), journal);

        // A ordem da venda com cartão: ler o cartão (diário), DEPOIS abrir a
        // transação da venda. No WAL só um escreve por vez: com a transação da
        // venda aberta, o diário esperaria a trava e falharia.
        await coordinator.AuthorizeAsync("pedido-1", 1000, TefCardType.Debit, new SilentInteraction());
        using (var sale = database.Connection.BeginTransaction(deferred: false))
        {
            sale.Rollback();  // a gravação da venda falhou
        }

        Assert.Single(journal.Pending());
    }

    [Fact]
    public void The_same_transaction_cannot_be_recorded_twice()
    {
        using var journal = new SqliteTefJournal(_file.Path);
        var entry = new TefJournalEntry("t-1", "pedido-1", 1000, TefCardType.Debit, TefState.Started, Iso.Now());
        journal.Record(entry);
        Assert.Throws<InvalidOperationException>(() => journal.Record(entry));
    }

    private sealed class SilentInteraction : ITefInteraction
    {
        public void Show(string message) { }

        public Task<int?> ChooseAsync(string title, IReadOnlyList<string> options, CancellationToken cancellationToken) =>
            Task.FromResult<int?>(0);

        public Task<string?> AskAsync(string prompt, CancellationToken cancellationToken) => Task.FromResult<string?>("");
    }
}
