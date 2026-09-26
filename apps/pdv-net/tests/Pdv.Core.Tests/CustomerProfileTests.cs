using System.Text.Json;
using Microsoft.Data.Sqlite;
using Pdv.Data;
using Pdv.Data.Customers;
using Pdv.Data.Sales;

namespace Pdv.Core.Tests;

/// <summary>O cadastro do cliente (schema 15): morador e apartamento, contato, CPF e consentimento.</summary>
public sealed class CustomerProfileTests : IDisposable
{
    private static readonly byte[] Secret = Enumerable.Range(0, 32).Select(i => (byte)i).ToArray();
    private static readonly TerminalIdentity Terminal = new("tenant-1", "store-1", "device-1");
    private static readonly DateOnly Today = new(2026, 9, 26);

    /// <summary>Um CPF válido de exemplo (dígitos verificadores certos), e o mesmo com o último dígito trocado.</summary>
    private const string Cpf = "52998224725";

    private readonly TestDatabase _file = new(userVersion: PdvDatabase.SupportedSchemaVersion);
    private readonly PdvDatabase _database;
    private readonly ManualClock _clock = new(new DateTimeOffset(2026, 9, 26, 15, 0, 0, TimeSpan.Zero));
    private readonly CustomerLedgers _customers;

    public CustomerProfileTests()
    {
        _database = new PdvDatabase(_file.Path);
        _customers = new CustomerLedgers(_database, Terminal,
            new AuditLedger(Terminal.TenantId, Terminal.StoreId, Terminal.DeviceId, Secret, _clock), _clock);
    }

    public void Dispose()
    {
        _database.Dispose();
        _file.Dispose();
    }

    private static CustomerProfile Resident(string name = "Lia Cliente", string? phone = "(21) 99876-5432") =>
        new(name, phone, "Lia@Exemplo.com ", "529.982.247-25", IsResident: true, UnitBlock: " b ", UnitNumber: "101",
            BirthDate: new DateOnly(1990, 9, 26), MarketingOptIn: true);

    private List<(string Operation, string Uuid, JsonElement Payload)> Outbox() =>
        [.. ReadOutbox()];

    private IEnumerable<(string, string, JsonElement)> ReadOutbox()
    {
        using var command = _database.Connection.CreateCommand();
        command.CommandText = "SELECT operation, client_uuid, payload_json FROM sync_outbox WHERE entity_table = 'customers' ORDER BY seq";
        using var reader = command.ExecuteReader();
        while (reader.Read())
        {
            yield return (reader.GetString(0), reader.GetString(1), JsonDocument.Parse(reader.GetString(2)).RootElement.Clone());
        }
    }

    // -- regras ----------------------------------------------------------------

    [Theory]
    [InlineData("529.982.247-25", true)]
    [InlineData("52998224725", true)]
    [InlineData("111.444.777-35", true)]
    [InlineData("529.982.247-24", false)]
    [InlineData("529.982.247-15", false)]
    [InlineData("111.111.111-11", false)]
    [InlineData("0000000000", false)]
    [InlineData("", false)]
    public void The_cpf_needs_both_check_digits(string cpf, bool valid) =>
        Assert.Equal(valid, CustomerRules.IsValidCpf(cpf));

    [Fact]
    public void Normalizing_cleans_what_the_operator_typed()
    {
        var clean = CustomerRules.Normalize(Resident("  Lia   Cliente ", "+55 21 99876-5432") with { Email = " Lia@Exemplo.COM " }, Today);

        Assert.Equal(new CustomerProfile("Lia Cliente", "21998765432", "lia@exemplo.com", Cpf, true, "B", "101",
            new DateOnly(1990, 9, 26), true), clean);
        Assert.Equal("Bloco B · apto 101", clean.Unit);
    }

    [Fact]
    public void A_non_resident_keeps_no_apartment()
    {
        var clean = CustomerRules.Normalize(Resident() with { IsResident = false }, Today);
        Assert.Equal((false, null, null, ""), (clean.IsResident, clean.UnitBlock, clean.UnitNumber, clean.Unit));
    }

    [Theory]
    [InlineData("nome", "Nome do cliente é obrigatório.")]
    [InlineData("whatsapp", "WhatsApp precisa do DDD")]
    [InlineData("cpf", "CPF inválido")]
    [InlineData("contato", "Informe o WhatsApp ou o CPF")]
    [InlineData("email", "E-mail inválido")]
    [InlineData("apto", "Morador precisa do número do apartamento.")]
    [InlineData("nascimento", "Data de nascimento inválida.")]
    [InlineData("ofertas", "Para receber ofertas, informe o WhatsApp ou o e-mail.")]
    public void Each_refusal_says_what_to_fix(string broken, string message)
    {
        var profile = broken switch
        {
            "nome" => Resident(name: "   "),
            "whatsapp" => Resident(phone: "99876-5432"),
            "cpf" => Resident() with { Cpf = "529.982.247-24" },
            "contato" => Resident(phone: null) with { Cpf = null, MarketingOptIn = false },
            "email" => Resident() with { Email = "lia@exemplo" },
            "apto" => Resident() with { UnitNumber = "  " },
            "nascimento" => Resident() with { BirthDate = Today.AddDays(1) },
            _ => Resident(phone: null) with { Email = null },
        };

        var error = Assert.Throws<CustomerException>(() => CustomerRules.Normalize(profile, Today));
        Assert.StartsWith(message, error.Message);
    }

    [Theory]
    [InlineData("26/09/1990", "1990-09-26")]
    [InlineData("1990-09-26", "1990-09-26")]
    [InlineData("", null)]
    public void Reads_the_birth_date_as_the_operator_types_it(string text, string? expected)
    {
        Assert.True(CustomerRules.TryParseBirthDate(text, out var date));
        Assert.Equal(expected, date?.ToString("yyyy-MM-dd"));
        Assert.False(CustomerRules.TryParseBirthDate("31/02/1990", out _));
    }

    [Theory]
    [InlineData("bloco B apto 101", "B", "101")]
    [InlineData("b 101", "B", "101")]
    [InlineData("Torre 2 / ap. 304", "2", "304")]
    [InlineData("101", null, "101")]
    [InlineData("apto 12a", null, "12A")]
    [InlineData("Lia", null, null)]
    [InlineData("Lia Cliente Souza", null, null)]
    public void Reads_an_apartment_from_what_was_said(string text, string? block, string? unit) =>
        Assert.Equal((block, unit), CustomerLedgers.ParseUnit(text));

    // -- gravação --------------------------------------------------------------

    [Fact]
    public void Registering_stores_the_profile_and_sends_it_to_the_cloud()
    {
        var id = _customers.Register(Resident());

        var record = _customers.Get(id)!;
        Assert.Equal(CustomerRules.Normalize(Resident(), Today), record.Profile);
        Assert.Equal("2026-09-26T15:00:00.000+00:00", record.MarketingOptInAt);
        Assert.Equal(new Customer(id, "Lia Cliente", "21998765432"), record.Customer);

        var (operation, _, payload) = Assert.Single(Outbox());
        Assert.Equal("insert", operation);
        // Booleano como booleano: a coluna da nuvem é BOOLEAN.
        Assert.Equal(JsonValueKind.True, payload.GetProperty("is_resident").ValueKind);
        Assert.Equal(JsonValueKind.True, payload.GetProperty("marketing_opt_in").ValueKind);
        Assert.Equal((Cpf, "B", "101", "1990-09-26", "lia@exemplo.com"),
            (payload.GetProperty("cpf").GetString(), payload.GetProperty("unit_block").GetString(),
             payload.GetProperty("unit_number").GetString(), payload.GetProperty("birth_date").GetString(),
             payload.GetProperty("email").GetString()));
    }

    [Fact]
    public void Without_consent_there_is_no_consent_time()
    {
        var id = _customers.Register(Resident() with { MarketingOptIn = false });
        Assert.Null(_customers.Get(id)!.MarketingOptInAt);
        Assert.Equal(JsonValueKind.False, Outbox()[0].Payload.GetProperty("marketing_opt_in").ValueKind);
    }

    [Fact]
    public void Whatsapp_and_cpf_belong_to_one_customer_and_the_refusal_names_who()
    {
        _customers.CreateCustomer("Nina Antiga", "21998765432");

        var phone = Assert.Throws<CustomerException>(() => _customers.Register(Resident()));
        Assert.Equal("Este WhatsApp já está no cadastro de Nina Antiga.", phone.Message);

        _customers.Register(Resident(phone: "21912345678"));
        var cpf = Assert.Throws<CustomerException>(() => _customers.Register(Resident("Outra", "21955554444")));
        Assert.Equal("Este CPF já está no cadastro de Lia Cliente.", cpf.Message);
    }

    [Fact]
    public void Editing_keeps_the_original_consent_time_and_withdrawing_clears_it()
    {
        var id = _customers.Register(Resident());
        _clock.Advance(TimeSpan.FromDays(3));

        // A própria pessoa não é duplicata de si mesma.
        _customers.Update(id, Resident() with { Email = "nova@exemplo.com", UnitNumber = "202" });
        var edited = _customers.Get(id)!;
        Assert.Equal(("nova@exemplo.com", "202"), (edited.Profile.Email, edited.Profile.UnitNumber));
        Assert.Equal("2026-09-26T15:00:00.000+00:00", edited.MarketingOptInAt);

        _customers.Update(id, Resident() with { MarketingOptIn = false });
        Assert.Null(_customers.Get(id)!.MarketingOptInAt);

        _clock.Advance(TimeSpan.FromDays(1));
        _customers.Update(id, Resident());
        Assert.Equal("2026-09-30T15:00:00.000+00:00", _customers.Get(id)!.MarketingOptInAt);

        var outbox = Outbox();
        Assert.Equal(["insert", "update", "update", "update"], outbox.Select(o => o.Operation));
        Assert.Equal(4, outbox.Select(o => o.Uuid).Distinct().Count());
        Assert.False(outbox[1].Payload.TryGetProperty("created_at", out _));
        Assert.Equal("2026-09-29T15:00:00.000+00:00", outbox[1].Payload.GetProperty("updated_at").GetString());
        Assert.Equal(0L, _database.Scalar("SELECT is_synced FROM customers WHERE id = $id", ("$id", id)));
    }

    [Fact]
    public void Editing_into_someone_elses_whatsapp_is_refused()
    {
        _customers.Register(Resident());
        var other = _customers.Register(Resident("Bia", "21912345678") with { Cpf = null });

        var error = Assert.Throws<CustomerException>(() => _customers.Update(other, Resident("Bia") with { Cpf = null }));
        Assert.Equal("Este WhatsApp já está no cadastro de Lia Cliente.", error.Message);
        Assert.Throws<CustomerException>(() => _customers.Update("nao-existe", Resident()));
    }

    [Fact]
    public void Finds_by_whatsapp_cpf_apartment_or_name()
    {
        var lia = _customers.Register(Resident());
        var bia = _customers.Register(new CustomerProfile("Bia Vizinha", "21912345678", IsResident: true, UnitBlock: "C",
            UnitNumber: "101"));
        var rui = _customers.Register(new CustomerProfile("Rui de Fora", "21955554444"));
        _customers.Register(new CustomerProfile("Zé 50%_off", "21933332222"));

        string[] Ids(string text) => [.. _customers.Find(text).Select(r => r.Id)];

        Assert.Equal([lia], Ids("+55 (21) 99876-5432"));
        Assert.Equal([lia], Ids("529.982.247-25"));
        Assert.Equal([bia, lia], Ids("101"));
        Assert.Equal([lia], Ids("bloco b apto 101"));
        Assert.Equal([bia], Ids("C 101"));
        Assert.Equal([rui], Ids("rui"));
        Assert.Empty(Ids("B 999"));
        // Curinga do LIKE digitado é letra, não curinga.
        Assert.Single(Ids("50%_"));
        Assert.Single(Ids("_"));
        Assert.Empty(Ids("   "));

        _database.Execute("UPDATE customers SET is_active = 0 WHERE id = $id", ("$id", rui));
        Assert.Empty(Ids("rui"));
    }

    [Fact]
    public void A_non_resident_is_not_found_by_apartment()
    {
        // Ex-morador: o apartamento ficou no banco (outro caixa, versão antiga), mas ele não mora mais lá.
        var rui = _customers.Register(new CustomerProfile("Rui de Fora", "21955554444", UnitNumber: "101"));
        _database.Execute("UPDATE customers SET unit_number = '101' WHERE id = $id", ("$id", rui));
        Assert.Empty(_customers.Find("101"));
    }

    // -- a migração 14 → 15 no C# ----------------------------------------------

    private static string[] Schema(SqliteConnection connection)
    {
        using var command = connection.CreateCommand();
        command.CommandText = "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY name";
        using var reader = command.ExecuteReader();
        var statements = new List<string>();
        while (reader.Read()) statements.Add(reader.GetString(0).Replace("\r\n", "\n").Trim());
        return [.. statements];
    }

    [Fact]
    public void A_version_14_database_is_upgraded_to_exactly_what_python_produces()
    {
        var folder = Path.Combine(Path.GetTempPath(), "pdv-v14-" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(folder);
        var path = Path.Combine(folder, "pdv_local.db");
        try
        {
            using (var old = new SqliteConnection($"Data Source={path};Pooling=False"))
            {
                old.Open();
                using var create = old.CreateCommand();
                create.CommandText = File.ReadAllText(TestDatabase.Contract("pdv-schema-v14.sql")) +
                    "INSERT INTO customers (id, tenant_id, name, phone, created_at, updated_at, client_uuid) " +
                    "VALUES ('c-1', 'tenant-1', 'Cliente Antigo', '21998765432', 'x', 'x', 'u-1');";
                create.ExecuteNonQuery();
            }

            using (var upgraded = new PdvDatabase(path))
            {
                Assert.Equal(15, upgraded.SchemaVersion);
                Assert.Equal(Schema(new SqliteConnection($"Data Source={_file.Path};Pooling=False").Also(c => c.Open())),
                    Schema(upgraded.Connection));
                var customers = new CustomerLedgers(upgraded, Terminal,
                    new AuditLedger(Terminal.TenantId, Terminal.StoreId, Terminal.DeviceId, Secret, _clock), _clock);
                var old = customers.Get("c-1")!;
                Assert.Equal(new CustomerProfile("Cliente Antigo", "21998765432"), old.Profile);
            }

            // Reabrir não reaplica nada.
            using (var again = new PdvDatabase(path)) Assert.Equal(15, again.SchemaVersion);
        }
        finally
        {
            SqliteConnection.ClearAllPools();
            Directory.Delete(folder, recursive: true);
        }
    }

    [Fact]
    public void Older_than_14_is_still_refused()
    {
        using var file = new TestDatabase(userVersion: 13);
        var error = Assert.Throws<PdvDatabaseException>(() => new PdvDatabase(file.Path));
        Assert.Contains("anterior à 15", error.Message);
    }
}

internal static class ObjectExtensions
{
    public static T Also<T>(this T value, Action<T> action)
    {
        action(value);
        return value;
    }
}
