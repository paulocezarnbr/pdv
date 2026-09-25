using System.Text.Json;
using Pdv.Core;
using Pdv.Core.Audit;

namespace Pdv.Core.Tests;

/// <summary>
/// O PDV em C# contra o contrato gerado pelo PDV em Python
/// (<c>contracts/audit-chain.json</c>). Os dois gravam o mesmo ledger durante a
/// transição: qualquer byte diferente aqui é um caixa que não abre lá.
/// </summary>
public sealed class AuditContractTests
{
    private static readonly JsonElement Contract = Load();

    private static JsonElement Load()
    {
        var folder = new DirectoryInfo(AppContext.BaseDirectory);
        while (folder is not null && !File.Exists(Path.Combine(folder.FullName, "contracts", "audit-chain.json")))
        {
            folder = folder.Parent;
        }
        Assert.NotNull(folder);
        var text = File.ReadAllText(Path.Combine(folder!.FullName, "contracts", "audit-chain.json"));
        return JsonDocument.Parse(text).RootElement.Clone();
    }

    private static byte[] Secret => Convert.FromHexString(Contract.GetProperty("secret_hex").GetString()!);

    public static TheoryData<string> Cases()
    {
        var data = new TheoryData<string>();
        foreach (var item in Contract.GetProperty("cases").EnumerateArray())
        {
            data.Add(item.GetProperty("name").GetString()!);
        }
        return data;
    }

    private static JsonElement Case(string name) =>
        Contract.GetProperty("cases").EnumerateArray().Single(c => c.GetProperty("name").GetString() == name);

    [Theory]
    [MemberData(nameof(Cases))]
    public void The_canonical_json_is_byte_for_byte_the_pythons(string name)
    {
        var item = Case(name);
        Assert.Equal(item.GetProperty("canonical").GetString(), CanonicalJson.Serialize(item.GetProperty("payload")));
    }

    [Theory]
    [MemberData(nameof(Cases))]
    public void The_hash_is_the_pythons(string name)
    {
        var item = Case(name);
        var hash = AuditChain.ComputeHash(
            Secret,
            Contract.GetProperty("genesis_hash").GetString()!,
            1,
            Contract.GetProperty("event_type").GetString()!,
            item.GetProperty("canonical").GetString()!,
            Contract.GetProperty("created_at").GetString()!);
        Assert.Equal(item.GetProperty("hash").GetString(), hash);
    }

    [Fact]
    public void The_genesis_is_the_pythons() =>
        Assert.Equal(AuditChain.GenesisHash, Contract.GetProperty("genesis_hash").GetString());

    private static List<LedgerLink> Chain() =>
        Contract.GetProperty("chain").EnumerateArray().Select(link => new LedgerLink(
            link.GetProperty("seq").GetInt64(),
            link.GetProperty("event_type").GetString()!,
            link.GetProperty("payload_json").GetString()!,
            link.GetProperty("prev_hash").GetString()!,
            link.GetProperty("hash").GetString()!,
            link.GetProperty("created_at").GetString()!)).ToList();

    [Fact]
    public void A_chain_written_by_python_verifies_in_csharp() => AuditChain.Verify(Chain(), Secret);

    [Fact]
    public void An_edited_payload_is_caught()
    {
        var chain = Chain();
        chain[1] = chain[1] with { PayloadJson = chain[1].PayloadJson.Replace("10", "50") };
        var error = Assert.Throws<AuditChainException>(() => AuditChain.Verify(chain, Secret));
        Assert.Contains("seq 2", error.Message);
    }

    [Fact]
    public void A_deleted_link_is_caught()
    {
        var chain = Chain();
        chain.RemoveAt(1);
        var error = Assert.Throws<AuditChainException>(() => AuditChain.Verify(chain, Secret));
        Assert.Contains("Buraco", error.Message);
    }

    [Fact]
    public void Another_terminals_secret_does_not_verify()
    {
        var other = Secret.ToArray();
        other[0] ^= 0xFF;
        Assert.Throws<AuditChainException>(() => AuditChain.Verify(Chain(), other));
    }

    [Fact]
    public void Objects_built_in_csharp_serialize_like_the_contract()
    {
        // O PDV em C# não monta payload a partir de JSON: monta de objetos.
        var payload = new Dictionary<string, object?>
        {
            ["total_cents"] = 810,
            ["payments"] = new object[] { new Dictionary<string, object?> { ["nsu"] = "000123", ["method"] = "debit" } },
        };
        var expected = Contract.GetProperty("chain")[2].GetProperty("payload_json").GetString();
        Assert.Equal(expected, CanonicalJson.Serialize(payload));
    }

    [Fact]
    public void A_date_in_the_payload_must_already_be_text() =>
        Assert.Throws<ArgumentException>(() =>
            CanonicalJson.Serialize(new Dictionary<string, object?> { ["at"] = DateTimeOffset.UtcNow }));

    [Fact]
    public void Timestamps_have_the_python_shape() =>
        Assert.Equal(
            "2026-09-25T03:49:25.134+00:00",
            Iso.Format(new DateTimeOffset(2026, 9, 25, 0, 49, 25, 134, TimeSpan.FromHours(-3))));
}
