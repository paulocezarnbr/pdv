using System.Net.Http.Json;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.Json.Serialization;
using Pdv.Core;
using Pdv.Data.Secrets;

namespace Pdv.Data.Provisioning;

/// <summary>Código inválido ou falha de comunicação: a mensagem vai para o balcão.</summary>
public class ActivationException(string message, Exception? inner = null) : Exception(message, inner);

/// <summary>A retaguarda recusou o código (4xx): reenviar o mesmo dá o mesmo resultado.</summary>
public sealed class ActivationRefusedException(string message) : ActivationException(message);

/// <summary>A troca de loja levaria vendas ainda não sincronizadas para o CNPJ errado.</summary>
public sealed class ActivationBlockedException(string message) : ActivationException(message);

/// <summary>O que a retaguarda devolve ao trocar o código pelo token.</summary>
public sealed record ActivationResult(
    string TenantId,
    string StoreId,
    string DeviceId,
    string SyncToken,
    string StoreName,
    string CloudBaseUrl);

/// <summary>Rótulo da máquina para o painel. Conveniência, não segurança: quem autentica é o token.</summary>
public sealed record MachineFingerprint(
    [property: JsonPropertyName("hostname")] string Hostname,
    [property: JsonPropertyName("os")] string Os,
    [property: JsonPropertyName("arch")] string Arch)
{
    /// <summary>Os mesmos rótulos do <c>platform</c> do Python: <c>Windows 11</c>, <c>AMD64</c>.</summary>
    public static MachineFingerprint Current() => new(
        Environment.MachineName,
        $"Windows {(Environment.OSVersion.Version.Build >= 22000 ? "11" : Environment.OSVersion.Version.Major.ToString())}",
        RuntimeInformation.OSArchitecture switch
        {
            Architecture.X64 => "AMD64",
            Architecture.Arm64 => "ARM64",
            Architecture.X86 => "x86",
            var other => other.ToString(),
        });
}

public interface IActivationTransport
{
    Task<ActivationResult> ActivateAsync(
        string code, MachineFingerprint fingerprint, byte[] deviceSecret, CancellationToken cancellation);
}

/// <summary>
/// Endereço e código como o <c>pdv.provisioning.activation</c> do Python os trata,
/// regra por regra (<c>contracts/activation.json</c>).
/// </summary>
public static class Activation
{
    /// <summary>O nome do token de sincronização no cofre.</summary>
    public const string SyncTokenName = "sync_token";

    public const int MinCodeLength = 6;
    public const int MaxCodeLength = 32;

    /// <summary>O endereço de exemplo do <c>AppConfig</c>; nunca é uma retaguarda de verdade.</summary>
    public const string PlaceholderCloudUrl = "https://api.erpfood.local";

    private static readonly HashSet<string> LocalHosts = new(StringComparer.Ordinal) { "localhost", "127.0.0.1", "::1" };

    /// <summary>
    /// Só letras e números ASCII, em maiúsculas — o painel mostra <c>ABCD-EFGH-JKMN</c>
    /// e o técnico digita como quiser.
    /// </summary>
    /// <exception cref="ActivationException">Sobrou um tamanho implausível.</exception>
    public static string NormalizeCode(string raw)
    {
        var cleaned = new string(raw.Where(char.IsAsciiLetterOrDigit).ToArray()).ToUpperInvariant();
        if (cleaned.Length is < MinCodeLength or > MaxCodeLength)
        {
            throw new ActivationException(
                $"Código de ativação inválido: esperados entre {MinCodeLength} e {MaxCodeLength} " +
                $"caracteres, recebidos {cleaned.Length}.");
        }
        return cleaned;
    }

    /// <summary>
    /// O endereço da retaguarda como o terminal usa: <c>https://host[/...]/api</c>.
    /// </summary>
    /// <remarks>
    /// O lojista digita o que vê no navegador, com ou sem <c>https://</c> e barra
    /// no fim. HTTPS é obrigatório fora desta máquina: por aqui passam o token e as
    /// vendas. Analisado à mão, e não com <see cref="Uri"/>, porque o <c>Uri</c>
    /// reescreve o endereço (tira a porta 443, converte IDN) e o Python não.
    /// </remarks>
    /// <exception cref="ActivationException">Vazio, malformado ou sem HTTPS.</exception>
    public static string NormalizeServerUrl(string raw)
    {
        var text = raw.Trim();
        if (text.Length == 0) throw new ActivationException("Informe o endereço da retaguarda.");
        if (!text.Contains("://", StringComparison.Ordinal)) text = "https://" + text;

        var separator = text.IndexOf("://", StringComparison.Ordinal);
        var scheme = text[..separator].ToLowerInvariant();
        var rest = text[(separator + 3)..];
        var netlocEnd = rest.IndexOfAny(['/', '?', '#']);
        var netloc = netlocEnd < 0 ? rest : rest[..netlocEnd];
        var tail = netlocEnd < 0 ? "" : rest[netlocEnd..];
        var pathEnd = tail.IndexOfAny(['?', '#']);
        var path = pathEnd < 0 ? tail : tail[..pathEnd];

        var hasUserInfo = netloc.Contains('@');
        var host = HostOf(hasUserInfo ? netloc[(netloc.LastIndexOf('@') + 1)..] : netloc);

        if (scheme is not ("http" or "https") || host.Length == 0 || text.Contains(' '))
        {
            throw new ActivationException($"Endereço inválido: '{raw.Trim()}'.");
        }
        if (hasUserInfo) throw new ActivationException("O endereço não pode conter usuário ou senha.");
        if (scheme == "http" && !LocalHosts.Contains(host))
        {
            throw new ActivationException(
                "Use https:// — por este endereço vão passar o token do terminal e as vendas. " +
                "http:// só é aceito para localhost.");
        }

        path = path.TrimEnd('/');
        if (!path.EndsWith("/api", StringComparison.Ordinal)) path += "/api";
        return $"{scheme}://{netloc.ToLowerInvariant()}{path}";
    }

    private static string HostOf(string hostAndPort)
    {
        if (hostAndPort.StartsWith('['))
        {
            var close = hostAndPort.IndexOf(']');
            return (close < 0 ? hostAndPort[1..] : hostAndPort[1..close]).ToLowerInvariant();
        }
        var colon = hostAndPort.IndexOf(':');
        return (colon < 0 ? hostAndPort : hostAndPort[..colon]).ToLowerInvariant();
    }

    /// <summary>O endereço como o lojista o reconhece: sem o <c>/api</c> do final.</summary>
    public static string DisplayServerUrl(string? apiUrl)
    {
        if (string.IsNullOrEmpty(apiUrl) || apiUrl == PlaceholderCloudUrl) return "";
        return apiUrl.EndsWith("/api", StringComparison.Ordinal) ? apiUrl[..^"/api".Length] : apiUrl;
    }

    /// <summary>A raiz <c>/api</c> da nuvem, aceite quem digitou a origem ou a raiz.</summary>
    public static string CloudApiRoot(string baseUrl)
    {
        var trimmed = baseUrl.TrimEnd('/');
        return trimmed.EndsWith("/api", StringComparison.Ordinal) ? trimmed : trimmed + "/api";
    }

    /// <summary>
    /// Ativa o terminal e grava a identidade recebida — no banco e no cofre que
    /// o PDV em Python também lê.
    /// </summary>
    /// <param name="force">Reapontar para outro tenant mesmo com a fila cheia. Só o suporte.</param>
    /// <exception cref="ActivationException">Código inválido ou falha de comunicação.</exception>
    /// <exception cref="ActivationRefusedException">A retaguarda recusou o código.</exception>
    /// <exception cref="ActivationBlockedException">A troca levaria vendas para o CNPJ errado.</exception>
    public static async Task<ActivationResult> ActivateAsync(
        string code,
        PdvDatabase database,
        SecretVault vault,
        IActivationTransport transport,
        bool force = false,
        TimeProvider? clock = null,
        CancellationToken cancellation = default)
    {
        var normalized = NormalizeCode(code);
        var currentTenant = database.Scalar(
            "SELECT value FROM device_settings WHERE key = 'device.tenant_id'") as string;

        var result = await transport.ActivateAsync(
            normalized, MachineFingerprint.Current(), vault.EnsureDeviceSecret(), cancellation);

        var changingTenant = !string.IsNullOrEmpty(currentTenant) && currentTenant != result.TenantId;
        if (changingTenant && !force)
        {
            var pending = Convert.ToInt64(database.Scalar("SELECT COUNT(*) FROM sync_outbox"));
            if (pending > 0)
            {
                throw new ActivationBlockedException(
                    $"Este terminal tem {pending} registro(s) ainda não sincronizado(s) com a loja atual. " +
                    "Sincronize antes de reapontá-lo para outra loja — caso contrário essas vendas seriam " +
                    "enviadas para o CNPJ errado.");
            }
        }

        // Token primeiro: gravar as configurações antes deixaria o terminal
        // "ativado" sem credencial se o cofre falhasse, sincronizando contra um
        // 401 eterno.
        vault.Store(SyncTokenName, System.Text.Encoding.UTF8.GetBytes(result.SyncToken));

        var values = new List<(string Key, string Value)>
        {
            ("device.tenant_id", result.TenantId),
            ("device.store_id", result.StoreId),
            ("device.id", result.DeviceId),
            ("device.activated", "1"),
            ("cloud.base_url", result.CloudBaseUrl),
        };
        // O nome aparece no login e no cupom; sem ele o terminal seguia "Confeitaria Demo".
        if (result.StoreName.Length > 0) values.Add(("store.name", result.StoreName));

        var now = Iso.Now(clock);
        database.InTransaction(transaction =>
        {
            foreach (var (key, value) in values)
            {
                using var command = transaction.Command(
                    "INSERT INTO device_settings (key, value, updated_at) VALUES ($key, $value, $now) " +
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    ("$key", key), ("$value", value), ("$now", now));
                command.ExecuteNonQuery();
            }
        });
        return result;
    }
}

/// <summary>Fala com <c>POST {nuvem}/api/devices/activate</c>.</summary>
public sealed class HttpActivationTransport(string baseUrl, HttpClient? client = null) : IActivationTransport
{
    private static readonly TimeSpan Timeout = TimeSpan.FromSeconds(20);

    private readonly HttpClient _client = client ?? new HttpClient { Timeout = Timeout };
    private readonly string _baseUrl = baseUrl.TrimEnd('/');

    public async Task<ActivationResult> ActivateAsync(
        string code, MachineFingerprint fingerprint, byte[] deviceSecret, CancellationToken cancellation)
    {
        int status;
        string body;
        try
        {
            using var response = await _client.PostAsJsonAsync(
                $"{Activation.CloudApiRoot(_baseUrl)}/devices/activate",
                new ActivationRequest(code, fingerprint, Convert.ToHexStringLower(deviceSecret)),
                cancellation);
            status = (int)response.StatusCode;
            body = await response.Content.ReadAsStringAsync(cancellation);
        }
        catch (Exception error) when (error is HttpRequestException or TaskCanceledException)
        {
            // A mensagem de fora do HttpClient é genérica ("Error while copying
            // content to a stream"); a causa que o suporte precisa está dentro.
            var cause = error;
            while (cause.InnerException is not null) cause = cause.InnerException;
            var detail = ReferenceEquals(cause, error) ? error.Message : $"{error.Message} ({cause.Message})";
            throw new ActivationException($"Não foi possível falar com a retaguarda: {detail}", error);
        }

        // 4xx é veredito; 5xx é transitório. A distinção evita que o técnico
        // queime um código válido insistindo contra um servidor fora do ar.
        if (status is 400 or 401 or 403 or 404 or 409 or 410)
        {
            throw new ActivationRefusedException(RefusalMessage(body));
        }
        if (status >= 400)
        {
            throw new ActivationException(
                $"A retaguarda respondeu {status}. Tente novamente em alguns minutos.");
        }
        return Parse(body, _baseUrl);
    }

    private static string RefusalMessage(string body)
    {
        try
        {
            using var document = JsonDocument.Parse(body);
            if (document.RootElement.ValueKind == JsonValueKind.Object)
            {
                foreach (var field in new[] { "detail", "message" })
                {
                    if (document.RootElement.TryGetProperty(field, out var value) &&
                        value.ValueKind == JsonValueKind.String && value.GetString() is { Length: > 0 } text)
                    {
                        return text;
                    }
                }
            }
        }
        catch (JsonException)
        {
        }
        return "Código recusado. Confira se foi digitado corretamente e se ainda não expirou — " +
               "gere um novo no painel administrativo.";
    }

    internal static ActivationResult Parse(string body, string baseUrl)
    {
        JsonElement root;
        try
        {
            using var document = JsonDocument.Parse(body);
            root = document.RootElement.Clone();
        }
        catch (JsonException)
        {
            throw new ActivationException("Resposta de ativação em formato inesperado.");
        }
        if (root.ValueKind != JsonValueKind.Object)
        {
            throw new ActivationException("Resposta de ativação em formato inesperado.");
        }

        string Text(string name) =>
            root.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
                ? value.GetString() ?? ""
                : "";

        var missing = new[] { "tenant_id", "store_id", "device_id", "sync_token" }
            .Where(name => Text(name).Length == 0).ToArray();
        if (missing.Length > 0)
        {
            throw new ActivationException($"Resposta de ativação incompleta: falta {string.Join(", ", missing)}.");
        }
        var cloud = Text("cloud_base_url");
        return new ActivationResult(
            Text("tenant_id"), Text("store_id"), Text("device_id"), Text("sync_token"),
            Text("store_name"), cloud.Length > 0 ? cloud : baseUrl);
    }

    private sealed record ActivationRequest(
        [property: JsonPropertyName("activation_code")] string ActivationCode,
        [property: JsonPropertyName("fingerprint")] MachineFingerprint Fingerprint,
        [property: JsonPropertyName("device_secret_hex")] string DeviceSecretHex);
}
