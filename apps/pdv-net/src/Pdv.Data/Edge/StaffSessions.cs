using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using Pdv.Core;
using Pdv.Data.Auth;
using Pdv.Data.Sales;

namespace Pdv.Data.Edge;

/// <summary>Sessão de garçom ausente, vencida, revogada ou de outro aparelho.</summary>
public sealed class StaffAuthException(string message) : Exception(message);

/// <summary>Quem está atendendo, neste aparelho, até quando.</summary>
public sealed record StaffSession(
    string Token, string UserId, string Name, string Login, string Role, string DeviceId, DateTimeOffset ExpiresAt)
{
    public string FirstName => string.IsNullOrEmpty(Name) ? Login : Name.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries)[0];

    /// <summary>O token só sai na resposta do login: repeti-lo o espalharia pelo log de qualquer proxy.</summary>
    public JsonObject ToJson(TimeProvider clock, bool withToken = false)
    {
        var json = new JsonObject
        {
            ["user_id"] = UserId,
            ["name"] = Name,
            ["first_name"] = FirstName,
            ["login"] = Login,
            ["role"] = Role,
            ["expires_at"] = PyIsoFormat(ExpiresAt),
            ["expires_in_seconds"] = Math.Max(0, (long)(ExpiresAt - clock.GetUtcNow()).TotalSeconds),
        };
        if (withToken) json["token"] = Token;
        return json;
    }

    /// <summary>O <c>datetime.isoformat()</c>: microssegundos, e nada de fração quando ela é zero.</summary>
    internal static string PyIsoFormat(DateTimeOffset moment)
    {
        moment = moment.ToUniversalTime();
        var micro = moment.Ticks / 10 % 1_000_000;
        var format = micro == 0 ? "yyyy-MM-dd'T'HH:mm:ss'+00:00'" : "yyyy-MM-dd'T'HH:mm:ss.ffffff'+00:00'";
        return moment.ToString(format, CultureInfo.InvariantCulture);
    }
}

/// <summary>
/// A sessão da pessoa no app do garçom — o <c>edge/staff.py</c>.
/// </summary>
/// <remarks>
/// <para>
/// O token do aparelho diz DE ONDE veio o lançamento; o da sessão diz QUEM
/// lançou. Celular roubado sem PIN não lança nada, PIN vazado sem aparelho
/// pareado também não.
/// </para>
/// <para>
/// Vive em disco, ao contrário da concessão de gerente: ela é identidade, não
/// poder. Evaporar a cada queda de energia faria a loja redigitar PIN com o
/// salão cheio, e o caminho de menor resistência viraria um login só para
/// todos — o problema original de volta.
/// </para>
/// </remarks>
public sealed class StaffSessions(PdvDatabase database, string tenantId, TimeProvider? clock = null)
{
    /// <summary>Um turno de sábado passa das oito horas; o aparelho esquecido não amanhece logado.</summary>
    public static readonly TimeSpan SessionTtl = TimeSpan.FromHours(14);

    /// <summary>Teto contra um app defeituoso pedindo login em laço no banco que grava a venda.</summary>
    public const int MaxSessionsPerDevice = 4;

    private readonly TimeProvider _clock = clock ?? TimeProvider.System;
    private readonly StaffAuthentication _auth = new(database, tenantId, clock);

    public TimeProvider Clock => _clock;

    /// <summary>
    /// Valida a credencial (a mesma do balcão: <c>authenticate</c>, não
    /// <c>authorize</c> — atender mesa não exige poder de autorizar) e abre a
    /// sessão. Entrar derruba quem estava no aparelho.
    /// </summary>
    /// <exception cref="AuthenticationException">Credencial inválida ou freio ativo.</exception>
    public StaffSession Login(string login, string pin, string deviceId)
    {
        var identity = _auth.Authenticate(login, pin);
        var token = EdgeAuth.NewToken();
        var now = _clock.GetUtcNow();
        var expiresAt = now + SessionTtl;
        database.InTransaction(transaction =>
        {
            // Sem isto, a troca de turno deixaria viva a sessão de quem já foi para casa.
            transaction.Command(
                "UPDATE edge_staff_sessions SET revoked_at = $now WHERE device_id = $device AND revoked_at IS NULL",
                ("$now", Iso.Format(now)), ("$device", deviceId)).ExecuteNonQuery();
            transaction.Command(
                """
                INSERT INTO edge_staff_sessions
                    (token_hash, tenant_id, device_id, user_id, user_name, user_login, role, created_at, expires_at, last_seen_at)
                VALUES ($hash, $tenant, $device, $user, $name, $login, $role, $now, $expires, $now)
                """,
                ("$hash", EdgeAuth.Hash(token)), ("$tenant", tenantId), ("$device", deviceId), ("$user", identity.Id),
                ("$name", identity.Name), ("$login", identity.Login), ("$role", identity.Role),
                ("$now", Iso.Format(now)), ("$expires", Iso.Format(expiresAt))).ExecuteNonQuery();
            // O histórico morto do aparelho sai: quem lançou o quê está no pedido.
            transaction.Command(
                """
                DELETE FROM edge_staff_sessions
                 WHERE device_id = $device
                   AND token_hash NOT IN (
                       SELECT token_hash FROM edge_staff_sessions
                        WHERE device_id = $device ORDER BY created_at DESC LIMIT $keep)
                """,
                ("$device", deviceId), ("$keep", MaxSessionsPerDevice)).ExecuteNonQuery();
        });
        return new StaffSession(token, identity.Id, identity.Name, identity.Login, identity.Role, deviceId, expiresAt);
    }

    /// <summary>Resolve o token numa sessão viva DESTE aparelho: token lido de uma tela não vale em outro.</summary>
    /// <exception cref="StaffAuthException">Sem sessão, vencida, revogada ou de outro aparelho.</exception>
    public StaffSession Require(string? token, string deviceId)
    {
        if (string.IsNullOrEmpty(token))
        {
            throw new StaffAuthException("Entre com o seu login para lançar pedidos neste aparelho.");
        }
        var digest = EdgeAuth.Hash(token);
        using var command = Sql.Command(database.Connection, null,
            "SELECT token_hash, device_id, user_id, user_name, user_login, role, expires_at, revoked_at " +
            "FROM edge_staff_sessions WHERE token_hash = $hash AND tenant_id = $tenant",
            ("$hash", digest), ("$tenant", tenantId));
        using var reader = command.ExecuteReader();
        if (!reader.Read()) throw new StaffAuthException("Sessão não reconhecida. Entre de novo.");
        if (!CryptographicOperations.FixedTimeEquals(Encoding.ASCII.GetBytes(reader.GetString(0)), Encoding.ASCII.GetBytes(digest)))
        {
            throw new StaffAuthException("Sessão não reconhecida.");
        }
        if (!reader.IsDBNull(7) && reader.GetValue(7).ToString() is { Length: > 0 })
        {
            throw new StaffAuthException("Sua sessão foi encerrada no caixa. Entre de novo.");
        }
        // Data ilegível vira sessão vencida, nunca eterna.
        var expiresAt = EdgeAuth.TryParse(reader.GetString(6), out var parsed) ? parsed : _clock.GetUtcNow().AddSeconds(-1);
        if (expiresAt <= _clock.GetUtcNow()) throw new StaffAuthException("Seu turno expirou. Entre de novo para continuar.");
        if (reader.GetString(1) != deviceId)
        {
            // Não se revoga: quem abriu legitimamente continua com ela.
            throw new StaffAuthException("Esta sessão não vale para este aparelho.");
        }
        var session = new StaffSession(token, reader.GetString(2), reader.GetString(3), reader.GetString(4),
            reader.GetString(5), reader.GetString(1), expiresAt);
        reader.Close();
        database.InTransaction(transaction => transaction.Command(
            "UPDATE edge_staff_sessions SET last_seen_at = $now WHERE token_hash = $hash",
            ("$now", Iso.Now(_clock)), ("$hash", digest)).ExecuteNonQuery());
        return session;
    }

    /// <summary>Encerra a sessão. Devolve se havia algo a encerrar.</summary>
    public bool Logout(string? token)
    {
        if (string.IsNullOrEmpty(token)) return false;
        return database.InTransaction(transaction => transaction.Command(
            "UPDATE edge_staff_sessions SET revoked_at = $now WHERE token_hash = $hash AND revoked_at IS NULL",
            ("$now", Iso.Now(_clock)), ("$hash", EdgeAuth.Hash(token))).ExecuteNonQuery()) == 1;
    }

    /// <summary>Derruba todas as sessões de uma pessoa, em todos os aparelhos. Devolve quantas caíram.</summary>
    public int RevokeUser(string userId) => database.InTransaction(transaction => transaction.Command(
        "UPDATE edge_staff_sessions SET revoked_at = $now WHERE user_id = $user AND tenant_id = $tenant AND revoked_at IS NULL",
        ("$now", Iso.Now(_clock)), ("$user", userId), ("$tenant", tenantId)).ExecuteNonQuery());

    /// <summary>Quem está em turno agora, para o painel do caixa.</summary>
    public IReadOnlyList<StaffSessionRow> ListActive()
    {
        using var command = Sql.Command(database.Connection, null,
            "SELECT user_id, user_name, role, device_id, created_at, expires_at, last_seen_at FROM edge_staff_sessions " +
            "WHERE tenant_id = $tenant AND revoked_at IS NULL AND expires_at > $now ORDER BY created_at",
            ("$tenant", tenantId), ("$now", Iso.Now(_clock)));
        using var reader = command.ExecuteReader();
        var rows = new List<StaffSessionRow>();
        while (reader.Read())
        {
            rows.Add(new StaffSessionRow(reader.GetString(0), reader.GetString(1), reader.GetString(2), reader.GetString(3),
                reader.GetString(4), reader.GetString(5), reader.IsDBNull(6) ? null : reader.GetString(6)));
        }
        return rows;
    }
}

public sealed record StaffSessionRow(
    string UserId, string UserName, string Role, string DeviceId, string CreatedAt, string ExpiresAt, string? LastSeenAt);
