using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Pdv.Core.Audit;

namespace Pdv.Core.Remote;

/// <summary>Um comando do painel para um terminal — <b>não verificado</b> até passar pelo serviço.</summary>
public sealed record RemoteCommand(
    string CommandUuid,
    string TenantId,
    string StoreId,
    string DeviceId,
    string Kind,
    JsonElement Payload,
    string IssuedByUserId,
    string IssuedByName,
    string IssuedAt,
    string Signature);

/// <summary>O contrato do comando remoto — o <c>remote/protocol.py</c> do Python e o <c>crypto/commands.ts</c> da nuvem.</summary>
/// <remarks>
/// <para>
/// A nuvem assina e o terminal confere, com a chave do terminal
/// (<c>device_secret</c>, a mesma do ledger, entregue à nuvem na ativação). A
/// assinatura prova que <b>a nuvem emitiu aquele comando exato para aquele
/// terminal</b>. O token de sincronização só prova quem está falando.
/// </para>
/// <para>
/// Conferido byte a byte contra <c>contracts/remote-commands.json</c>, gerado
/// pelo Python. A nuvem é conferida contra o Python pelo teste de contrato de
/// lá.
/// </para>
/// </remarks>
public static class CommandProtocol
{
    public const string ApplyDiscount = "apply_discount";
    public const string CancelItem = "cancel_item";

    /// <summary>
    /// Os únicos comandos que o terminal aceita de fora. Abrir gaveta e
    /// reimprimir cupom ficaram de fora: são o que um atacante remoto mais
    /// gostaria de ter.
    /// </summary>
    public static readonly IReadOnlySet<string> Kinds = new HashSet<string>(StringComparer.Ordinal) { ApplyDiscount, CancelItem };

    /// <summary>
    /// Validade: um PDV com a internet caída desde a manhã ainda recebe o
    /// desconto do meio-dia, e um comando capturado não vale semanas depois.
    /// </summary>
    public static readonly TimeSpan MaxAge = TimeSpan.FromHours(12);

    /// <summary>Relógio do servidor adiantado não pode recusar comando legítimo.</summary>
    public static readonly TimeSpan ClockSkewTolerance = TimeSpan.FromMinutes(5);

    /// <summary><c>json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)</c>.</summary>
    public static string CanonicalPayload(JsonElement payload) => CanonicalJson.Serialize(payload);

    /// <summary>
    /// O texto exato que entra no HMAC. O separador 0x1F não aparece em uuid,
    /// nome de comando nem ISO-8601: dois campos não conseguem se passar por um.
    /// </summary>
    public static string Material(string commandUuid, string deviceId, string kind, JsonElement payload, string issuedAt) =>
        string.Join('\x1f', commandUuid, deviceId, kind, CanonicalPayload(payload), issuedAt);

    public static string Sign(byte[] secret, string commandUuid, string deviceId, string kind, JsonElement payload, string issuedAt) =>
        Convert.ToHexStringLower(HMACSHA256.HashData(
            secret, Encoding.UTF8.GetBytes(Material(commandUuid, deviceId, kind, payload, issuedAt))));

    /// <summary>Confere a assinatura em tempo constante.</summary>
    public static bool Verify(RemoteCommand command, byte[] secret)
    {
        var expected = Sign(secret, command.CommandUuid, command.DeviceId, command.Kind, command.Payload, command.IssuedAt);
        return CryptographicOperations.FixedTimeEquals(
            Encoding.UTF8.GetBytes(expected), Encoding.UTF8.GetBytes(command.Signature));
    }

    /// <summary>
    /// Ainda dentro da janela? Data ilegível conta como <b>vencida</b>: um campo
    /// que não se consegue interpretar não vira permissão para agir.
    /// </summary>
    public static bool IsFresh(string issuedAt, DateTimeOffset now)
    {
        if (ParsePythonIso(issuedAt, now.Offset) is not { } issued) return false;
        if (issued - now > ClockSkewTolerance) return false;
        return now - issued <= MaxAge;
    }

    private static readonly Regex IsoPattern = new(
        @"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2})(?::(\d{2})(?::(\d{2})(?:[.,](\d+))?)?)?(Z|[+-]\d{2}:\d{2}(?::\d{2})?)?)?$",
        RegexOptions.CultureInvariant);

    /// <summary>
    /// O <c>datetime.fromisoformat</c> do Python, na parte que a nuvem e os
    /// testes usam: data, hora com ou sem segundos e fração, "T" ou espaço, "Z"
    /// ou deslocamento. Sem fuso, vale o do relógio de referência.
    /// </summary>
    public static DateTimeOffset? ParsePythonIso(string text, TimeSpan defaultOffset)
    {
        var match = IsoPattern.Match(text);
        if (!match.Success) return null;
        int Part(int group) => match.Groups[group].Success
            ? int.Parse(match.Groups[group].Value, CultureInfo.InvariantCulture)
            : 0;
        try
        {
            var ticks = 0L;
            if (match.Groups[7].Success)
            {
                // Além de 6 casas o Python trunca; o tick do .NET tem 7.
                var fraction = match.Groups[7].Value.PadRight(7, '0')[..7];
                ticks = long.Parse(fraction, CultureInfo.InvariantCulture);
            }
            var offset = defaultOffset;
            if (match.Groups[8].Success)
            {
                var zone = match.Groups[8].Value;
                offset = zone == "Z"
                    ? TimeSpan.Zero
                    : TimeSpan.ParseExact(zone[1..], zone.Length > 6 ? @"hh\:mm\:ss" : @"hh\:mm", CultureInfo.InvariantCulture)
                      * (zone[0] == '-' ? -1 : 1);
            }
            var local = new DateTime(Part(1), Part(2), Part(3), Part(4), Part(5), Part(6)).AddTicks(ticks);
            return new DateTimeOffset(local, offset);
        }
        catch (ArgumentException)
        {
            return null;
        }
        catch (FormatException)
        {
            return null;
        }
    }

    /// <summary>
    /// O percentual do payload como o <c>Decimal(str(valor))</c> do Python lê:
    /// texto ou número; <c>true</c>, nulo, NaN e infinito não valem.
    /// </summary>
    public static decimal? ReadDecimal(JsonElement value)
    {
        string text;
        switch (value.ValueKind)
        {
            case JsonValueKind.String:
                text = value.GetString()!.Trim();
                break;
            case JsonValueKind.Number:
                // O literal decide, como no json.loads: com ponto é float (e o
                // str() do float é o repr), sem ponto é inteiro.
                text = CanonicalJson.Serialize(value);
                break;
            default:
                return null;
        }
        if (text.Length == 0 || text.Any(c => c is ',' or '_' || char.IsWhiteSpace(c))) return null;
        return decimal.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out var parsed) ? parsed : null;
    }

    /// <summary><c>f"{value.normalize():f}"</c>: "30" e não "30.00"; "12.5" sem notação científica.</summary>
    public static string Plain(decimal value)
    {
        var normalized = value / 1.0000000000000000000000000000m;
        return normalized.ToString("0.############################", CultureInfo.InvariantCulture);
    }
}
