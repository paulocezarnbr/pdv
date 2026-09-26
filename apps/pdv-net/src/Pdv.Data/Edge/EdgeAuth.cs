using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using Microsoft.Data.Sqlite;
using Pdv.Core;
using Pdv.Data.Sales;

namespace Pdv.Data.Edge;

/// <summary>Código de pareamento inválido, expirado, já usado — ou o freio ativo.</summary>
public sealed class PairingException(string message) : Exception(message);

/// <summary>Token de aparelho ausente, desconhecido ou revogado.</summary>
public sealed class DeviceAuthException(string message) : Exception(message);

/// <summary>Um código vivo na tela do caixa, e quanto falta para ele vencer. Nunca o código.</summary>
public sealed record PairingCode(DateTimeOffset ExpiresAt, TimeProvider Clock)
{
    public int RemainingSeconds => Math.Max(0, (int)(ExpiresAt - Clock.GetUtcNow()).TotalSeconds);

    public bool IsAlive => RemainingSeconds > 0;
}

public sealed record PairedDevice(string Id, string Name, string Kind, string? OperatorId);

/// <summary>
/// Pareamento e autenticação dos aparelhos do salão — o <c>edge/auth.py</c>.
/// </summary>
/// <remarks>
/// <para>
/// <b>A rede da loja não é confiável:</b> é a mesma do Wi-Fi do cliente, com a
/// senha num cartaz. Estar nela não autoriza nada. O aparelho entra por um
/// código mostrado na tela do caixa — o acesso físico ao balcão é a âncora.
/// </para>
/// <para>
/// Só os hashes do token e do código ficam no banco: um dump do
/// <c>pdv_local.db</c> não entrega credencial de aparelho nenhum. E não há cache
/// de token: revogar o celular perdido vale na próxima requisição.
/// </para>
/// <para>Conferido contra <c>contracts/salon.json</c>.</para>
/// </remarks>
public sealed class EdgeAuth(PdvDatabase database, TerminalIdentity terminal, TimeProvider? clock = null)
{
    /// <summary>Curto porque o código fica visível na tela do caixa.</summary>
    public static readonly TimeSpan PairingTtl = TimeSpan.FromMinutes(5);

    /// <summary>Oito dígitos: a conta que importa é quantas tentativas cabem na janela de cinco minutos.</summary>
    public const int PairingCodeDigits = 8;

    /// <summary>O freio é do terminal inteiro: quem adivinha código ainda não tem aparelho por quem separar.</summary>
    public const int MaxPairingAttempts = 10;

    public const int PairingLockoutSeconds = 120;

    /// <summary>O escopo na <c>auth_throttle</c>, que já sobrevive ao restart.</summary>
    internal const string PairingScope = "edge:pair";

    private const int TokenBytes = 32;

    private readonly TimeProvider _clock = clock ?? TimeProvider.System;

    /// <summary>Piso monotônico do freio: sobrevive ao relógio atrasado dentro da sessão.</summary>
    private long _pairFloor;

    internal static string Hash(string value) =>
        Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(PyStrip(value))));

    /// <summary>O <c>str.strip()</c> do Python, que o hash aplica antes de tudo.</summary>
    internal static string PyStrip(string value) => value.Trim();

    /// <summary>O <c>secrets.token_urlsafe(32)</c>.</summary>
    internal static string NewToken() =>
        Convert.ToBase64String(RandomNumberGenerator.GetBytes(TokenBytes)).TrimEnd('=').Replace('+', '-').Replace('/', '_');

    // -- pareamento -------------------------------------------------------------

    /// <summary>Gera o código e o devolve em texto uma vez só. Gerar um novo revoga os anteriores.</summary>
    /// <remarks>
    /// Antes não revogava, e o efeito era o contrário do esperado: quem clicava
    /// "gerar outro" porque achou que alguém tinha lido deixava os dois válidos,
    /// inclusive o que acabara de ser lido.
    /// </remarks>
    public (string Code, PairingCode Pairing) CreatePairingCode()
    {
        var code = string.Concat(Enumerable.Range(0, PairingCodeDigits).Select(_ => (char)('0' + RandomNumberGenerator.GetInt32(10))));
        var now = _clock.GetUtcNow();
        var expiresAt = now + PairingTtl;
        database.InTransaction(transaction =>
        {
            ExpireAll(transaction, now);
            transaction.Command(
                "INSERT INTO edge_pairing_codes (code_hash, created_at, expires_at) VALUES ($hash, $now, $expires)",
                ("$hash", Hash(code)), ("$now", Iso.Format(now)), ("$expires", Iso.Format(expiresAt))).ExecuteNonQuery();
        });
        return (code, new PairingCode(expiresAt, _clock));
    }

    /// <summary>O prazo do código vivo, se houver. O texto não se relê: senão o hash não serviria para nada.</summary>
    public PairingCode? ActivePairingCode()
    {
        var expires = database.Scalar(
            "SELECT expires_at FROM edge_pairing_codes WHERE used_at IS NULL AND expires_at > $now " +
            "ORDER BY expires_at DESC LIMIT 1", ("$now", Iso.Now(_clock))) as string;
        return expires is not null && TryParse(expires, out var at) ? new PairingCode(at, _clock) : null;
    }

    /// <summary>Mata os códigos vivos antes do prazo: alguém estranho passou pelo balcão.</summary>
    public int RevokePairingCodes() => database.InTransaction(transaction => ExpireAll(transaction, _clock.GetUtcNow()));

    /// <summary>
    /// Vence pelo prazo em vez de marcar <c>used_at</c>: um código marcado como
    /// usado mentiria sobre ter pareado aparelho, e é essa coluna que se olha
    /// para saber quem entrou.
    /// </summary>
    private static int ExpireAll(SqliteTransaction transaction, DateTimeOffset now) =>
        transaction.Command(
            "UPDATE edge_pairing_codes SET expires_at = $now WHERE used_at IS NULL AND expires_at > $now",
            ("$now", Iso.Format(now))).ExecuteNonQuery();

    /// <summary>Troca um código válido por um token de aparelho, devolvido em texto uma única vez.</summary>
    /// <exception cref="PairingException">Código inválido, expirado, usado, ou freio ativo.</exception>
    public string Pair(string code, string deviceName, string kind = "waiter")
    {
        if (kind is not ("waiter" or "kds"))
        {
            throw new PairingException($"Tipo de aparelho desconhecido: {TableService.PyRepr(kind)}");
        }
        AssertNotThrottled();

        var now = _clock.GetUtcNow();
        var deviceId = Iso.NewId();
        var token = NewToken();
        try
        {
            Consume(code, deviceId, token, deviceName, kind, now);
        }
        catch (PairingException)
        {
            // Fora da transação do consumo: o rollback desfaria justamente o contador.
            RegisterPairingFailure();
            throw;
        }
        ClearPairingFailures();
        return token;
    }

    private void Consume(string code, string deviceId, string token, string deviceName, string kind, DateTimeOffset now)
    {
        var at = Iso.Format(now);
        database.InTransaction(transaction =>
        {
            // Consumo atômico: o WHERE carrega toda condição de validade, e a
            // contagem diz se ESTA transação consumiu. Dois celulares, um código.
            var consumed = transaction.Command(
                "UPDATE edge_pairing_codes SET used_at = $now, used_by = $device " +
                "WHERE code_hash = $hash AND used_at IS NULL AND expires_at > $now",
                ("$now", at), ("$device", deviceId), ("$hash", Hash(code))).ExecuteNonQuery();
            if (consumed != 1)
            {
                // Uma mensagem para inexistente, expirado e usado: separar diria a
                // quem adivinha que acertou o código e errou só o tempo.
                throw new PairingException(
                    "Código inválido, expirado ou já utilizado. Gere um novo na tela do caixa.");
            }
            var name = PyStrip(deviceName);
            name = name.Length == 0 ? "Aparelho sem nome" : Truncate(name, 64);
            transaction.Command(
                """
                INSERT INTO edge_devices
                    (id, tenant_id, store_id, name, kind, token_hash, paired_at, created_at, updated_at, client_uuid)
                VALUES ($id, $tenant, $store, $name, $kind, $hash, $now, $now, $now, $uuid)
                """,
                ("$id", deviceId), ("$tenant", terminal.TenantId), ("$store", terminal.StoreId), ("$name", name),
                ("$kind", kind), ("$hash", Hash(token)), ("$now", at), ("$uuid", Iso.NewId())).ExecuteNonQuery();
        });
    }

    // -- freio ------------------------------------------------------------------

    private void AssertNotThrottled()
    {
        var remaining = PairingLockSeconds();
        if (remaining > 0)
        {
            throw new PairingException(
                $"Tentativas demais de pareamento. Aguarde {remaining}s. Se não foi você, gere um código novo no caixa.");
        }
    }

    /// <summary>Segundos restantes do bloqueio, pelo maior dos dois relógios. Zero se liberado.</summary>
    public int PairingLockSeconds()
    {
        var floor = _pairFloor == 0 ? 0 : _clock.GetElapsedTime(_clock.GetTimestamp(), _pairFloor).TotalSeconds;
        var stored = 0.0;
        if (database.Scalar("SELECT locked_until FROM auth_throttle WHERE scope = $scope", ("$scope", PairingScope))
            is string until && until.Length > 0)
        {
            stored = ((TryParse(until, out var at) ? at : _clock.GetUtcNow()) - _clock.GetUtcNow()).TotalSeconds;
        }
        var remaining = Math.Max(floor, stored);
        return remaining > 0 ? (int)remaining + 1 : 0;
    }

    private void RegisterPairingFailure()
    {
        var now = _clock.GetUtcNow();
        var failures = 1;
        var firstAt = now;
        using (var command = Sql.Command(database.Connection, null,
                   "SELECT failures, first_failure_at FROM auth_throttle WHERE scope = $scope", ("$scope", PairingScope)))
        using (var reader = command.ExecuteReader())
        {
            if (reader.Read())
            {
                var previous = TryParse(reader.GetValue(1).ToString() ?? "", out var parsed) ? parsed : now;
                // A mesma janela do freio de login: erro de hoje não soma com o do mês passado.
                if (now - previous <= TimeSpan.FromHours(1))
                {
                    failures = Convert.ToInt32(reader.GetInt64(0)) + 1;
                    firstAt = previous;
                }
            }
        }

        string? lockedUntil = null;
        if (failures >= MaxPairingAttempts)
        {
            lockedUntil = Iso.Format(now.AddSeconds(PairingLockoutSeconds));
            _pairFloor = _clock.GetTimestamp() + (long)(PairingLockoutSeconds * (double)_clock.TimestampFrequency);
        }
        database.InTransaction(transaction => transaction.Command(
            """
            INSERT INTO auth_throttle (scope, failures, locked_until, first_failure_at, last_failure_at)
            VALUES ($scope, $failures, $locked, $first, $now)
            ON CONFLICT (scope) DO UPDATE SET
               failures = excluded.failures, locked_until = excluded.locked_until,
               first_failure_at = excluded.first_failure_at, last_failure_at = excluded.last_failure_at
            """,
            ("$scope", PairingScope), ("$failures", failures), ("$locked", lockedUntil), ("$first", Iso.Format(firstAt)),
            ("$now", Iso.Format(now))).ExecuteNonQuery());
    }

    /// <summary>
    /// Acertar zera o contador — seguro aqui, ao contrário do login: acertar exige
    /// um código que o caixa acabou de gerar, de uso único.
    /// </summary>
    private void ClearPairingFailures()
    {
        _pairFloor = 0;
        database.InTransaction(transaction => transaction.Command(
            "DELETE FROM auth_throttle WHERE scope = $scope", ("$scope", PairingScope)).ExecuteNonQuery());
    }

    // -- autenticação -----------------------------------------------------------

    /// <summary>Resolve o token num aparelho pareado e ativo, e registra o último contato.</summary>
    /// <exception cref="DeviceAuthException">Token ausente, desconhecido ou revogado.</exception>
    public PairedDevice Authenticate(string? token)
    {
        if (string.IsNullOrEmpty(token)) throw new DeviceAuthException("Aparelho não autenticado.");

        var digest = Hash(token);
        PairedDevice? device = null;
        string? storedHash = null;
        var revoked = false;
        using (var command = Sql.Command(database.Connection, null,
                   "SELECT id, name, kind, operator_id, token_hash, revoked_at FROM edge_devices " +
                   "WHERE token_hash = $hash AND tenant_id = $tenant", ("$hash", digest), ("$tenant", terminal.TenantId)))
        using (var reader = command.ExecuteReader())
        {
            if (reader.Read())
            {
                device = new PairedDevice(reader.GetString(0), reader.GetString(1), reader.GetString(2),
                    reader.IsDBNull(3) ? null : reader.GetString(3));
                storedHash = reader.GetString(4);
                revoked = !reader.IsDBNull(5) && reader.GetValue(5).ToString() is { Length: > 0 };
            }
        }
        if (device is null) throw new DeviceAuthException("Aparelho não reconhecido. Pareie novamente.");
        // Tempo constante mesmo tendo casado no WHERE: o banco não vira oráculo de timing.
        if (!CryptographicOperations.FixedTimeEquals(Encoding.ASCII.GetBytes(storedHash!), Encoding.ASCII.GetBytes(digest)))
        {
            throw new DeviceAuthException("Aparelho não reconhecido.");
        }
        if (revoked) throw new DeviceAuthException("Este aparelho foi revogado. Procure o responsável pelo caixa.");

        database.InTransaction(transaction => transaction.Command(
            "UPDATE edge_devices SET last_seen_at = $now WHERE id = $id", ("$now", Iso.Now(_clock)), ("$id", device.Id))
            .ExecuteNonQuery());
        return device;
    }

    /// <summary>Revoga um aparelho — celular perdido. Devolve se algo mudou.</summary>
    public bool Revoke(string deviceId)
    {
        var now = Iso.Now(_clock);
        return database.InTransaction(transaction => transaction.Command(
            "UPDATE edge_devices SET revoked_at = $now, updated_at = $now WHERE id = $id AND revoked_at IS NULL",
            ("$now", now), ("$id", deviceId)).ExecuteNonQuery()) == 1;
    }

    /// <summary>Os aparelhos, para o painel do caixa.</summary>
    public IReadOnlyList<EdgeDeviceRow> ListDevices()
    {
        using var command = Sql.Command(database.Connection, null,
            "SELECT id, name, kind, paired_at, last_seen_at, revoked_at FROM edge_devices WHERE tenant_id = $tenant " +
            "ORDER BY paired_at", ("$tenant", terminal.TenantId));
        using var reader = command.ExecuteReader();
        var rows = new List<EdgeDeviceRow>();
        while (reader.Read())
        {
            string? Text(int i) => reader.IsDBNull(i) ? null : reader.GetValue(i).ToString();
            rows.Add(new EdgeDeviceRow(reader.GetString(0), reader.GetString(1), reader.GetString(2), Text(3), Text(4), Text(5)));
        }
        return rows;
    }

    internal static bool TryParse(string value, out DateTimeOffset moment) =>
        DateTimeOffset.TryParse(value, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out moment);

    internal static string Truncate(string value, int limit) =>
        new(value.EnumerateRunes().Take(limit).SelectMany(rune => rune.ToString()).ToArray());
}

public sealed record EdgeDeviceRow(string Id, string Name, string Kind, string? PairedAt, string? LastSeenAt, string? RevokedAt);
