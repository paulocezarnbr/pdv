using System.Text.Json;
using Pdv.Core.Remote;

namespace Pdv.Core.Tests;

/// <summary>
/// Assinatura, janela e percentual do comando remoto contra
/// <c>contracts/remote-commands.json</c>, gerado pelo Python — que por sua vez é
/// conferido contra a nuvem (TypeScript) no teste de contrato de lá.
/// </summary>
public sealed class RemoteContractTests
{
    private static readonly JsonElement Contract =
        JsonDocument.Parse(File.ReadAllText(TestDatabase.Contract("remote-commands.json"))).RootElement.Clone();

    private static readonly byte[] Secret = Convert.FromHexString(Contract.GetProperty("secret_hex").GetString()!);

    public static TheoryData<int> Signatures() =>
        [.. Enumerable.Range(0, Contract.GetProperty("signatures").GetArrayLength())];

    [Theory]
    [MemberData(nameof(Signatures))]
    public void The_signed_text_and_the_signature_are_the_pythons(int index)
    {
        var expected = Contract.GetProperty("signatures")[index];
        string Field(string name) => expected.GetProperty(name).GetString()!;
        var payload = expected.GetProperty("payload");

        Assert.Equal(Field("canonical_payload"), CommandProtocol.CanonicalPayload(payload));
        Assert.Equal(Field("material"),
            CommandProtocol.Material(Field("command_uuid"), Field("device_id"), Field("kind"), payload, Field("issued_at")));
        Assert.Equal(Field("signature"),
            CommandProtocol.Sign(Secret, Field("command_uuid"), Field("device_id"), Field("kind"), payload, Field("issued_at")));

        var command = new RemoteCommand(
            Field("command_uuid"), "t", "s", Field("device_id"), Field("kind"), payload, "u", "n", Field("issued_at"),
            Field("signature"));
        Assert.True(CommandProtocol.Verify(command, Secret));
        Assert.False(CommandProtocol.Verify(command with { Signature = Field("signature").ToUpperInvariant() }, Secret));
        Assert.False(CommandProtocol.Verify(command with { IssuedAt = "2026-09-19T12:00:01+00:00" }, Secret));
        Assert.False(CommandProtocol.Verify(command with { DeviceId = "outro" }, Secret));
    }

    [Fact]
    public void The_window_is_twelve_hours_back_and_five_minutes_ahead()
    {
        Assert.Equal(CommandProtocol.MaxAge.TotalSeconds, Contract.GetProperty("max_age_seconds").GetInt32());
        Assert.Equal(CommandProtocol.ClockSkewTolerance.TotalSeconds, Contract.GetProperty("skew_seconds").GetInt32());
        var now = DateTimeOffset.Parse(Contract.GetProperty("now").GetString()!, System.Globalization.CultureInfo.InvariantCulture);

        foreach (var expected in Contract.GetProperty("freshness").EnumerateArray())
        {
            var issuedAt = expected.GetProperty("issued_at").GetString()!;
            Assert.True(expected.GetProperty("fresh").GetBoolean() == CommandProtocol.IsFresh(issuedAt, now), issuedAt);
        }
    }

    [Fact]
    public void The_percent_is_read_as_python_reads_it()
    {
        foreach (var expected in Contract.GetProperty("percents").EnumerateArray())
        {
            var value = expected.GetProperty("value");
            var parsed = CommandProtocol.ReadDecimal(value);
            var valid = parsed is > 0 and <= 100;
            if (expected.TryGetProperty("error", out _))
            {
                Assert.False(valid, value.GetRawText());
                continue;
            }
            Assert.True(valid, value.GetRawText());
            Assert.Equal(expected.GetProperty("plain").GetString(), CommandProtocol.Plain(parsed!.Value));
        }
    }
}
