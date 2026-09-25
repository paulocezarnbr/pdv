using System.Globalization;
using Pdv.Core;

namespace Pdv.Data.Auth;

/// <summary>Quem entrou. Responde "quem é você", não "o que você pode liberar".</summary>
public sealed record Identity(string Id, string Name, string Login, string Role, bool CanAuthorize, decimal MaxDiscountPercent)
{
    public string FirstName => string.IsNullOrWhiteSpace(Name) ? Login : Name.Split(' ', 2)[0];
}

public sealed class AuthenticationException(string message) : Exception(message);

/// <summary>Os papéis que liberam cada operação, como no Python.</summary>
public static class Roles
{
    /// <summary>Cancelamento de item no balcão: só gerente (<c>allowed_roles={"manager"}</c> no Python).</summary>
    public static readonly IReadOnlySet<string> ItemCancel = new HashSet<string>(StringComparer.Ordinal) { "manager" };

    public static readonly IReadOnlySet<string> OwnerOnly = new HashSet<string>(StringComparer.Ordinal) { "owner" };

    public static readonly IReadOnlySet<string> Manager = new HashSet<string>(StringComparer.Ordinal) { "manager", "owner" };
}

/// <summary>
/// Login por PIN contra a réplica local, sem internet — o <c>AuthorizationService</c> do Python.
/// </summary>
/// <remarks>
/// <para>
/// O freio mora na mesma tabela <c>auth_throttle</c> e segue os mesmos números
/// (<c>contracts/pin-hashes.json</c>): 5 tentativas por login e 20 no terminal,
/// bloqueio que dobra a partir de 30 s até 300 s, janela de 1 h. Durante a
/// transição um operador bloqueado no Python continua bloqueado no C#.
/// </para>
/// <para>
/// Acertar limpa o contador do login e <b>não</b> o global: senão errar
/// dezenove vezes e acertar o próprio login zeraria o freio.
/// </para>
/// </remarks>
public sealed class StaffAuthentication(PdvDatabase database, string tenantId, TimeProvider? clock = null)
{
    public const int MaxAttempts = 5;
    public const int MaxGlobalAttempts = 20;
    public const int LockoutBaseSeconds = 30;
    public const int LockoutMaxSeconds = 300;
    public const int MaxExponent = 16;
    public static readonly TimeSpan FailureWindow = TimeSpan.FromHours(1);
    public const string GlobalScope = "*";

    /// <summary>Hash descartável: login inexistente gasta o mesmo tempo que PIN errado.</summary>
    private const string DummyHash =
        "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHRzb21lc2FsdA$RdescudvJCsgt3ub+b+dWRWJTmaaJObG";

    private readonly TimeProvider _clock = clock ?? TimeProvider.System;

    /// <summary>
    /// Piso monotônico por escopo. O banco guarda o bloqueio entre execuções;
    /// isto impede que atrasar o relógio do Windows encurte um bloqueio dentro
    /// da sessão em andamento.
    /// </summary>
    private readonly Dictionary<string, long> _floor = new(StringComparer.Ordinal);

    /// <summary>Confere a credencial de qualquer usuário ativo.</summary>
    /// <exception cref="AuthenticationException">Credencial inválida ou freio ativo.</exception>
    public Identity Authenticate(string login, string pin) => Check(login, pin, requireAuthorizer: false);

    /// <summary>Confere a credencial de quem pode autorizar (cancelamento, desconto).</summary>
    public Identity Authorize(string login, string pin) => Check(login, pin, requireAuthorizer: true);

    /// <summary>Autoriza só quando a credencial é do papel exigido — o <c>authorize_role</c> do Python.</summary>
    /// <remarks>
    /// <c>can_authorize</c> responde se a pessoa tem algum poder; o papel
    /// responde <b>qual</b>. Sem esta segunda conferência, o PIN de um gerente
    /// valeria como o de um proprietário só porque os dois têm o primeiro bit
    /// ligado. Papel errado conta como falha no freio.
    /// </remarks>
    public Identity AuthorizeRole(string login, string pin, IReadOnlySet<string> allowedRoles)
    {
        var identity = Check(login, pin, requireAuthorizer: true);
        if (allowedRoles.Contains(identity.Role)) return identity;

        RegisterFailure(identity.Login.Trim().ToLowerInvariant());
        var expected = allowedRoles.SetEquals(Roles.OwnerOnly) ? "proprietário" : "gerente";
        throw new AuthenticationException($"Esta operação exige a credencial de um {expected}.");
    }

    /// <summary>Autoriza um desconto dentro do teto do perfil de quem autoriza — o <c>authorize_discount</c>.</summary>
    /// <remarks>
    /// O teto é do <b>perfil</b>: um gerente com limite de 30% não concede 50%
    /// nem com o PIN certo, senão o limite seria decorativo.
    /// </remarks>
    public Identity AuthorizeDiscount(string login, string pin, decimal percent, IReadOnlySet<string>? allowedRoles = null)
    {
        var identity = allowedRoles is null ? Authorize(login, pin) : AuthorizeRole(login, pin, allowedRoles);
        if (percent > identity.MaxDiscountPercent)
        {
            throw new AuthenticationException(
                $"{identity.Name} pode conceder até {Percent(identity.MaxDiscountPercent)}% — o pedido é de {Percent(percent)}%.");
        }
        return identity;
    }

    /// <summary>Logins que podem autorizar, para preencher o diálogo.</summary>
    public IReadOnlyList<string> ListAuthorizers(IReadOnlySet<string>? allowedRoles = null)
    {
        using var command = Sql.Command(
            database.Connection, null,
            "SELECT login, role FROM users WHERE tenant_id = $tenant AND is_active = 1 AND can_authorize = 1 ORDER BY name",
            ("$tenant", tenantId));
        using var reader = command.ExecuteReader();
        var logins = new List<string>();
        while (reader.Read())
        {
            if (allowedRoles is null || allowedRoles.Contains(reader.GetString(1))) logins.Add(reader.GetString(0));
        }
        return logins;
    }

    private static string Percent(decimal value) => value.ToString(CultureInfo.InvariantCulture);

    /// <summary>Segundos restantes de bloqueio para este login. Zero se liberado.</summary>
    public int LockStatus(string login)
    {
        var normalized = login.Trim().ToLowerInvariant();
        return Math.Max(Remaining($"login:{normalized}"), Remaining(GlobalScope));
    }

    private Identity Check(string login, string pin, bool requireAuthorizer)
    {
        login = login.Trim().ToLowerInvariant();
        if (login.Length == 0) throw new AuthenticationException("Informe o login.");

        AssertNotLocked(login);

        var row = FindUser(login);
        if (row is null || string.IsNullOrEmpty(row.Value.PinHash))
        {
            // Responder na hora para login inexistente diria ao atacante quais
            // logins existem antes mesmo de ele tentar um PIN.
            PinHasher.Verify(DummyHash, "pin-que-nao-existe");
            RegisterFailure(login);
            throw new AuthenticationException("Login ou PIN inválido.");
        }

        if (!PinHasher.Verify(row.Value.PinHash, pin))
        {
            RegisterFailure(login);
            throw new AuthenticationException("Login ou PIN inválido.");
        }

        var identity = row.Value.Identity;
        if (requireAuthorizer && !identity.CanAuthorize)
        {
            // Quem chegou aqui provou quem é, então a mensagem pode ser
            // específica. E conta como falha: insistir em liberar o que não se
            // pode é sinal, não engano.
            RegisterFailure(login);
            throw new AuthenticationException($"{identity.Name} não tem permissão para autorizar esta operação.");
        }

        Clear(login);
        return identity;
    }

    private (Identity Identity, string? PinHash)? FindUser(string login)
    {
        using var command = Sql.Command(
            database.Connection, null,
            "SELECT id, name, login, role, pin_hash, can_authorize, max_discount_percent FROM users " +
            "WHERE lower(login) = $login AND tenant_id = $tenant AND is_active = 1",
            ("$login", login), ("$tenant", tenantId));
        using var reader = command.ExecuteReader();
        if (!reader.Read()) return null;

        var discount = decimal.TryParse(
            reader.IsDBNull(6) ? "0" : reader.GetValue(6).ToString(), NumberStyles.Number,
            CultureInfo.InvariantCulture, out var percent) ? percent : 0m;
        return (new Identity(
                reader.GetString(0), reader.GetString(1), reader.GetString(2), reader.GetString(3),
                reader.GetInt64(5) != 0, discount),
            reader.IsDBNull(4) ? null : reader.GetString(4));
    }

    // -- freio ----------------------------------------------------------------

    private void AssertNotLocked(string login)
    {
        var individual = Remaining($"login:{login}");
        if (individual > 0)
        {
            throw new AuthenticationException($"Muitas tentativas para este login. Aguarde {individual}s.");
        }
        var overall = Remaining(GlobalScope);
        if (overall > 0)
        {
            throw new AuthenticationException(
                $"Muitas tentativas neste terminal. Aguarde {overall}s. Se não foi você, avise o gerente.");
        }
    }

    /// <summary>O maior dos dois relógios: o do banco sobrevive ao reinício; o monotônico, ao relógio atrasado.</summary>
    private int Remaining(string scope)
    {
        var floor = _floor.TryGetValue(scope, out var until)
            ? (until - _clock.GetTimestamp()) / (double)_clock.TimestampFrequency
            : 0;

        var stored = 0.0;
        var lockedUntil = database.Scalar(
            "SELECT locked_until FROM auth_throttle WHERE scope = $scope", ("$scope", scope)) as string;
        if (!string.IsNullOrEmpty(lockedUntil) &&
            DateTimeOffset.TryParse(lockedUntil, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var moment))
        {
            stored = (moment - _clock.GetUtcNow()).TotalSeconds;
        }

        var remaining = Math.Max(floor, stored);
        return remaining > 0 ? (int)remaining + 1 : 0;
    }

    private void RegisterFailure(string login)
    {
        Bump($"login:{login}", MaxAttempts);
        Bump(GlobalScope, MaxGlobalAttempts);
    }

    private void Bump(string scope, int ceiling)
    {
        var now = _clock.GetUtcNow();
        var failures = 1;
        var firstAt = now;

        using (var command = Sql.Command(
                   database.Connection, null,
                   "SELECT failures, first_failure_at FROM auth_throttle WHERE scope = $scope", ("$scope", scope)))
        using (var reader = command.ExecuteReader())
        {
            if (reader.Read() &&
                DateTimeOffset.TryParse(reader.GetString(1), CultureInfo.InvariantCulture,
                    DateTimeStyles.AssumeUniversal, out var previousFirst) &&
                now - previousFirst <= FailureWindow)
            {
                // Falhas antigas deixam de contar: cinco erros espalhados por
                // seis meses não bloqueiam quem nunca foi atacado.
                failures = reader.GetInt32(0) + 1;
                firstAt = previousFirst;
            }
        }

        string? lockedUntil = null;
        if (failures >= ceiling)
        {
            var exponent = Math.Min(failures - ceiling, MaxExponent);
            var delay = Math.Min(LockoutBaseSeconds * (1L << exponent), LockoutMaxSeconds);
            lockedUntil = Iso.Format(now.AddSeconds(delay));
            _floor[scope] = _clock.GetTimestamp() + delay * _clock.TimestampFrequency;
        }

        database.Execute(
            """
            INSERT INTO auth_throttle (scope, failures, locked_until, first_failure_at, last_failure_at)
            VALUES ($scope, $failures, $locked, $first, $last)
            ON CONFLICT (scope) DO UPDATE SET
                failures = excluded.failures, locked_until = excluded.locked_until,
                first_failure_at = excluded.first_failure_at, last_failure_at = excluded.last_failure_at
            """,
            ("$scope", scope), ("$failures", failures), ("$locked", lockedUntil),
            ("$first", Iso.Format(firstAt)), ("$last", Iso.Format(now)));
    }

    private void Clear(string login)
    {
        var scope = $"login:{login}";
        _floor.Remove(scope);
        database.Execute("DELETE FROM auth_throttle WHERE scope = $scope", ("$scope", scope));
    }
}
