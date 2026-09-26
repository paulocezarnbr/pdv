using System.Globalization;
using System.Text.Json.Nodes;
using Pdv.Data.Auth;

namespace Pdv.Data.Edge;

/// <summary>Uma autorização de gerente, viva por alguns minutos num aparelho.</summary>
public sealed record ManagerGrant(string Token, Identity Authorizer, string DeviceId, DateTimeOffset ExpiresAt)
{
    public bool IsValid(DateTimeOffset now) => now < ExpiresAt;

    public JsonObject ToJson(TimeProvider clock) => new()
    {
        ["token"] = Token,
        ["user_id"] = Authorizer.Id,
        ["name"] = Authorizer.Name,
        ["role"] = Authorizer.Role,
        ["max_discount_percent"] = Authorizer.MaxDiscountPercent.ToString(CultureInfo.InvariantCulture),
        ["expires_at"] = StaffSession.PyIsoFormat(ExpiresAt),
        ["expires_in_seconds"] = (long)(ExpiresAt - clock.GetUtcNow()).TotalSeconds,
    };
}

/// <summary>
/// Concessões de gerente no app do garçom — o <c>edge/manager.py</c>. Só em memória, de propósito.
/// </summary>
/// <remarks>
/// <para>
/// Cancelar comanda com item lançado é o vetor de furto do salão; isso precisa
/// de pessoa, não de aparelho. E o celular do garçom fica desbloqueado em cima
/// do balcão a noite inteira: uma sessão de gerente que durasse o turno
/// promoveria o aparelho. A concessão vale minutos, é por aparelho, e evapora
/// sozinha.
/// </para>
/// <para>
/// Reiniciar o PDV derruba toda concessão: poder que sobrevive a um restart
/// sobrevive também a um restart provocado.
/// </para>
/// </remarks>
public sealed class ManagerSessions(StaffAuthentication authorization, TimeProvider? clock = null)
{
    public static readonly TimeSpan GrantTtl = TimeSpan.FromMinutes(10);

    /// <summary>Teto contra um app defeituoso pedindo concessão em laço na memória do caixa.</summary>
    public const int MaxGrants = 64;

    /// <summary>Só gerente — não o proprietário: é o <c>allowed_roles</c> do Python.</summary>
    private static readonly IReadOnlySet<string> Allowed = new HashSet<string>(StringComparer.Ordinal) { "manager" };

    private readonly TimeProvider _clock = clock ?? TimeProvider.System;
    private readonly Dictionary<string, ManagerGrant> _grants = new(StringComparer.Ordinal);
    private readonly Lock _lock = new();

    public TimeProvider Clock => _clock;

    /// <summary>Valida a credencial e emite a concessão.</summary>
    /// <exception cref="AuthenticationException">
    /// Credencial inválida, perfil sem poder, tentativas esgotadas ou concessões demais.
    /// </exception>
    public ManagerGrant Authorize(string login, string pin, string deviceId)
    {
        var authorizer = authorization.AuthorizeRole(login, pin, Allowed);
        var grant = new ManagerGrant(EdgeAuth.NewToken(), authorizer, deviceId, _clock.GetUtcNow() + GrantTtl);
        lock (_lock)
        {
            Purge();
            if (_grants.Count >= MaxGrants)
            {
                throw new AuthenticationException(
                    "Autorizações demais em aberto neste terminal. Aguarde um minuto e tente de novo.");
            }
            _grants[grant.Token] = grant;
        }
        return grant;
    }

    /// <summary>Quem autorizou, conferido contra o aparelho que pediu: token vazado não vale em outro.</summary>
    /// <exception cref="AuthenticationException">Sem concessão, vencida ou de outro aparelho.</exception>
    public Identity Require(string? token, string deviceId)
    {
        if (string.IsNullOrEmpty(token))
        {
            throw new AuthenticationException("Esta operação precisa de autorização de gerente.");
        }
        lock (_lock)
        {
            if (!_grants.TryGetValue(token, out var grant) || !grant.IsValid(_clock.GetUtcNow()))
            {
                _grants.Remove(token);
                throw new AuthenticationException("Autorização expirada. Chame o gerente novamente.");
            }
            // Não se apaga: quem obteve legitimamente continua com ela.
            return grant.DeviceId == deviceId
                ? grant.Authorizer
                : throw new AuthenticationException("Autorização não vale para este aparelho.");
        }
    }

    /// <summary>O gerente saiu antes do prazo.</summary>
    public bool Revoke(string? token)
    {
        if (string.IsNullOrEmpty(token)) return false;
        lock (_lock) return _grants.Remove(token);
    }

    public int ActiveCount()
    {
        lock (_lock)
        {
            Purge();
            return _grants.Count;
        }
    }

    private void Purge()
    {
        var now = _clock.GetUtcNow();
        foreach (var token in _grants.Where(pair => !pair.Value.IsValid(now)).Select(pair => pair.Key).ToList())
        {
            _grants.Remove(token);
        }
    }
}
