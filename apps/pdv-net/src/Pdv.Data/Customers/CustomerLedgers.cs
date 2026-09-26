using Microsoft.Data.Sqlite;
using Pdv.Core;
using Pdv.Data.Sales;

namespace Pdv.Data.Customers;

public sealed class CustomerException(string message) : Exception(message);

public sealed record Customer(string Id, string Name, string? Phone);

public sealed record CashbackRule(int PercentBasisPoints, long MaxPerSaleCents, int ValidityDays);

public sealed record CashbackCredit(string Id, long AmountCents, string ExpiresAt);

public sealed record CreditPosition(long LimitCents, long OutstandingCents, long AvailableCents, long OverdueCents);

/// <summary>Arredondamentos dos ledgers de cliente: todos "meio para cima", como o Python.</summary>
internal static class Rounding
{
    public static long HalfUp(decimal value) => (long)Math.Round(value, 0, MidpointRounding.AwayFromZero);

    /// <summary>Percentual em pontos-base: 12,345% → 1235.</summary>
    public static int BasisPoints(decimal percent) => (int)HalfUp(percent * 100m);
}

/// <summary>
/// Clientes, cashback, pré-pago e fiado — os serviços do Python que só
/// <b>acrescentam</b> linhas: nenhum saldo é atualizado, todo saldo é soma do
/// ledger. É o que permite sincronizar dois terminais sem perder centavo.
/// </summary>
public sealed class CustomerLedgers(
    PdvDatabase database, TerminalIdentity terminal, AuditLedger ledger, TimeProvider? clock = null)
{
    private readonly TimeProvider _clock = clock ?? TimeProvider.System;
    private readonly Outbox _outbox = new(clock);

    // -- clientes ------------------------------------------------------------

    public string CreateCustomer(string name, string? phone)
    {
        name = name.Trim();
        if (name.Length == 0) throw new CustomerException("Nome do cliente é obrigatório.");
        // Como o Python: telefone só com dígitos; informado sem dígito nenhum fica vazio.
        var normalized = string.IsNullOrEmpty(phone) ? null : new string(phone.Where(char.IsAsciiDigit).ToArray());
        var id = Iso.NewId();
        var clientUuid = Iso.NewId();
        var now = Iso.Now(_clock);
        database.InTransaction(transaction =>
        {
            using (var insert = transaction.Command(
                       "INSERT INTO customers (id, tenant_id, name, phone, created_at, updated_at, client_uuid) " +
                       "VALUES ($id, $tenant, $name, $phone, $now, $now, $uuid)",
                       ("$id", id), ("$tenant", terminal.TenantId), ("$name", name), ("$phone", normalized),
                       ("$now", now), ("$uuid", clientUuid)))
            {
                insert.ExecuteNonQuery();
            }
            _outbox.Enqueue(transaction, "customers", id, clientUuid, "insert", new Dictionary<string, object?>
            {
                ["id"] = id,
                ["name"] = name,
                ["phone"] = normalized,
                ["created_at"] = now,
                ["updated_at"] = now,
            });
        });
        return id;
    }

    public Customer? FindByPhone(string phone)
    {
        var normalized = new string(phone.Where(char.IsAsciiDigit).ToArray());
        if (normalized.Length == 0) return null;
        using var command = Sql.Command(
            database.Connection, null,
            "SELECT id, name, phone FROM customers WHERE tenant_id = $tenant AND phone = $phone AND is_active = 1",
            ("$tenant", terminal.TenantId), ("$phone", normalized));
        using var reader = command.ExecuteReader();
        return reader.Read() ? new Customer(reader.GetString(0), reader.GetString(1), reader.IsDBNull(2) ? null : reader.GetString(2)) : null;
    }

    // -- cadastro completo (schema 15) -----------------------------------------

    private const string ProfileColumns =
        "id, name, phone, email, cpf, is_resident, unit_block, unit_number, birth_date, marketing_opt_in, marketing_opt_in_at";

    /// <summary>Cadastra com o perfil inteiro — morador ou não, contato, CPF e consentimento.</summary>
    /// <exception cref="CustomerException">Dado inválido, ou WhatsApp/CPF de outro cliente.</exception>
    public string Register(CustomerProfile profile)
    {
        var clean = CustomerRules.Normalize(profile, Today());
        var id = Iso.NewId();
        var clientUuid = Iso.NewId();
        var now = Iso.Now(_clock);
        database.InTransaction(transaction =>
        {
            RefuseDuplicate(transaction, clean, exceptId: null);
            var optInAt = clean.MarketingOptIn ? now : null;
            transaction.Command(
                "INSERT INTO customers (id, tenant_id, name, phone, email, cpf, is_resident, unit_block, unit_number, " +
                "birth_date, marketing_opt_in, marketing_opt_in_at, created_at, updated_at, client_uuid) " +
                "VALUES ($id, $tenant, $name, $phone, $email, $cpf, $resident, $block, $unit, $birth, $optIn, $optInAt, " +
                "$now, $now, $uuid)",
                [("$id", id), ("$tenant", terminal.TenantId), ("$uuid", clientUuid), ("$now", now), ("$optInAt", optInAt),
                 .. ProfileParameters(clean)]).ExecuteNonQuery();
            _outbox.Enqueue(transaction, "customers", id, clientUuid, "insert",
                ProfilePayload(id, clean, optInAt, now, createdAt: now));
        });
        return id;
    }

    /// <summary>Corrige o cadastro. Vai à nuvem como mudança, e a mais nova vence.</summary>
    /// <remarks>
    /// A hora do consentimento só muda quando ele muda: reeditar o e-mail não
    /// pode fingir que a pessoa autorizou de novo hoje, e retirar o
    /// consentimento apaga a hora.
    /// </remarks>
    /// <exception cref="CustomerException">Dado inválido, cliente inexistente, ou WhatsApp/CPF de outro cliente.</exception>
    public void Update(string customerId, CustomerProfile profile)
    {
        var clean = CustomerRules.Normalize(profile, Today());
        var now = Iso.Now(_clock);
        database.InTransaction(transaction =>
        {
            var current = Get(customerId, transaction) ?? throw new CustomerException("Cliente não encontrado.");
            RefuseDuplicate(transaction, clean, exceptId: customerId);
            var optInAt = !clean.MarketingOptIn ? null
                : current.Profile.MarketingOptIn ? current.MarketingOptInAt ?? now
                : now;
            transaction.Command(
                "UPDATE customers SET name = $name, phone = $phone, email = $email, cpf = $cpf, is_resident = $resident, " +
                "unit_block = $block, unit_number = $unit, birth_date = $birth, marketing_opt_in = $optIn, " +
                "marketing_opt_in_at = $optInAt, updated_at = $now, is_synced = 0 WHERE id = $id AND tenant_id = $tenant",
                [("$id", customerId), ("$tenant", terminal.TenantId), ("$now", now), ("$optInAt", optInAt),
                 .. ProfileParameters(clean)]).ExecuteNonQuery();
            // Uuid novo por mudança, como os outros cadastros: o da criação já foi usado.
            _outbox.Enqueue(transaction, "customers", customerId, Iso.NewId(), "update",
                ProfilePayload(customerId, clean, optInAt, now, createdAt: null));
        });
    }

    public CustomerRecord? Get(string customerId) => Get(customerId, null);

    private CustomerRecord? Get(string customerId, SqliteTransaction? transaction)
    {
        using var command = Sql.Command(database.Connection, transaction,
            $"SELECT {ProfileColumns} FROM customers WHERE id = $id AND tenant_id = $tenant",
            ("$id", customerId), ("$tenant", terminal.TenantId));
        using var reader = command.ExecuteReader();
        return reader.Read() ? Record(reader) : null;
    }

    /// <summary>
    /// O que o operador tem em mãos: WhatsApp, CPF, apartamento ("101", "B 101",
    /// "bloco B apto 101") ou parte do nome. Só clientes ativos, por nome.
    /// </summary>
    public IReadOnlyList<CustomerRecord> Find(string text, int limit = 20)
    {
        var query = (text ?? "").Trim();
        if (query.Length == 0) return [];
        // Com letra no meio ("B 101", "Lia 2") não é telefone nem CPF.
        var digits = query.Any(char.IsLetter) ? "" : CustomerRules.Digits(query);
        if (digits.Length is 12 or 13 && digits.StartsWith("55", StringComparison.Ordinal)) digits = digits[2..];
        var (block, unit) = ParseUnit(query);
        using var command = Sql.Command(database.Connection, null,
            $"SELECT {ProfileColumns} FROM customers WHERE tenant_id = $tenant AND is_active = 1 AND (" +
            "($digits <> '' AND (phone = $digits OR cpf = $digits)) " +
            "OR name LIKE $name ESCAPE '!' " +
            "OR (is_resident = 1 AND $unit IS NOT NULL AND unit_number = $unit AND ($block IS NULL OR unit_block = $block))" +
            ") ORDER BY name LIMIT $limit",
            ("$tenant", terminal.TenantId), ("$digits", digits),
            ("$name", "%" + query.Replace("!", "!!").Replace("%", "!%").Replace("_", "!_") + "%"),
            ("$unit", unit), ("$block", block), ("$limit", limit));
        using var reader = command.ExecuteReader();
        var found = new List<CustomerRecord>();
        while (reader.Read()) found.Add(Record(reader));
        return found;
    }

    /// <summary>"bloco B apto 101" → (B, 101); "101" → (null, 101); "Lia" → (null, null).</summary>
    public static (string? Block, string? Unit) ParseUnit(string text)
    {
        var noise = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            { "bloco", "bl", "torre", "apto", "apt", "ap", "apartamento", "unidade" };
        var tokens = text.Split([' ', '/', '-', ','], StringSplitOptions.RemoveEmptyEntries)
            .Select(token => token.Trim('.')).Where(token => token.Length > 0 && !noise.Contains(token))
            .Select(token => token.ToUpperInvariant()).ToList();
        // O apartamento tem número: "Lia" não é apartamento nenhum.
        if (tokens.Count is 0 or > 2 || !tokens[^1].Any(char.IsAsciiDigit)) return (null, null);
        return tokens.Count == 2 ? (tokens[0], tokens[1]) : (null, tokens[0]);
    }

    private static CustomerRecord Record(SqliteDataReader reader)
    {
        string? Text(int i) => reader.IsDBNull(i) ? null : reader.GetString(i);
        var birth = Text(8) is { } date &&
                    DateOnly.TryParseExact(date, "yyyy-MM-dd", System.Globalization.CultureInfo.InvariantCulture,
                        System.Globalization.DateTimeStyles.None, out var parsed)
            ? parsed
            : (DateOnly?)null;
        return new CustomerRecord(reader.GetString(0), new CustomerProfile(
            reader.GetString(1), Text(2), Text(3), Text(4), reader.GetInt64(5) != 0, Text(6), Text(7), birth,
            reader.GetInt64(9) != 0), Text(10));
    }

    /// <summary>WhatsApp e CPF são únicos na loja. A recusa diz de quem é, para o operador achar o cadastro certo.</summary>
    private void RefuseDuplicate(SqliteTransaction transaction, CustomerProfile profile, string? exceptId)
    {
        foreach (var (column, value, label) in new[] { ("phone", profile.WhatsApp, "WhatsApp"), ("cpf", profile.Cpf, "CPF") })
        {
            if (value is null) continue;
            if (transaction.Command(
                    $"SELECT name FROM customers WHERE tenant_id = $tenant AND {column} = $value AND id <> $except",
                    ("$tenant", terminal.TenantId), ("$value", value), ("$except", exceptId ?? "")).ExecuteScalar() is string owner)
            {
                throw new CustomerException($"Este {label} já está no cadastro de {owner}.");
            }
        }
    }

    private static (string, object?)[] ProfileParameters(CustomerProfile profile) =>
    [
        ("$name", profile.Name), ("$phone", profile.WhatsApp), ("$email", profile.Email), ("$cpf", profile.Cpf),
        ("$resident", profile.IsResident ? 1 : 0), ("$block", profile.UnitBlock), ("$unit", profile.UnitNumber),
        ("$birth", profile.BirthDate?.ToString("yyyy-MM-dd", System.Globalization.CultureInfo.InvariantCulture)),
        ("$optIn", profile.MarketingOptIn ? 1 : 0),
    ];

    /// <summary>Booleano como booleano: a coluna da nuvem é <c>BOOLEAN</c>.</summary>
    private static Dictionary<string, object?> ProfilePayload(
        string id, CustomerProfile profile, string? optInAt, string now, string? createdAt)
    {
        var payload = new Dictionary<string, object?>
        {
            ["id"] = id,
            ["name"] = profile.Name,
            ["phone"] = profile.WhatsApp,
            ["email"] = profile.Email,
            ["cpf"] = profile.Cpf,
            ["is_resident"] = profile.IsResident,
            ["unit_block"] = profile.UnitBlock,
            ["unit_number"] = profile.UnitNumber,
            ["birth_date"] = profile.BirthDate?.ToString("yyyy-MM-dd", System.Globalization.CultureInfo.InvariantCulture),
            ["marketing_opt_in"] = profile.MarketingOptIn,
            ["marketing_opt_in_at"] = optInAt,
        };
        if (createdAt is not null) payload["created_at"] = createdAt;
        payload["updated_at"] = now;
        return payload;
    }

    /// <summary>O dia na loja, para recusar nascimento no futuro.</summary>
    private DateOnly Today() => DateOnly.FromDateTime(_clock.GetLocalNow().DateTime);

    // -- cashback ------------------------------------------------------------

    /// <summary>A regra da loja. Mudar a regra é evento de auditoria (<c>warning</c>).</summary>
    public CashbackRule ConfigureCashback(decimal percent, long maxPerSaleCents, int validityDays, string actorUserId)
    {
        if (percent < 0 || percent > 100) throw new CustomerException("Percentual precisa estar entre 0 e 100.");
        if (maxPerSaleCents < 0 || validityDays is < 1 or > 3650)
        {
            throw new CustomerException("Teto e validade do cashback são inválidos.");
        }
        var basis = Rounding.BasisPoints(percent);
        database.InTransaction(transaction =>
        {
            using (var upsert = transaction.Command(
                       """
                       INSERT INTO cashback_rules
                           (id, tenant_id, store_id, percent_basis_points, max_per_sale_cents, validity_days, is_active, updated_at)
                       VALUES ($id, $tenant, $store, $basis, $max, $days, 1, $now)
                       ON CONFLICT (tenant_id, store_id) DO UPDATE SET
                           percent_basis_points = excluded.percent_basis_points,
                           max_per_sale_cents = excluded.max_per_sale_cents,
                           validity_days = excluded.validity_days, is_active = 1, updated_at = excluded.updated_at
                       """,
                       ("$id", Iso.NewId()), ("$tenant", terminal.TenantId), ("$store", terminal.StoreId),
                       ("$basis", basis), ("$max", maxPerSaleCents), ("$days", validityDays), ("$now", Iso.Now(_clock))))
            {
                upsert.ExecuteNonQuery();
            }
            ledger.Append(transaction, "cashback_rule_changed", actorUserId, new Dictionary<string, object?>
            {
                ["operation"] = "cashback_rule_changed",
                ["percent_basis_points"] = basis,
                ["max_per_sale_cents"] = maxPerSaleCents,
                ["validity_days"] = validityDays,
            }, "warning", actorUserId);
        });
        return new CashbackRule(basis, maxPerSaleCents, validityDays);
    }

    /// <summary>
    /// Credita uma venda <b>uma vez</b>, na transação do fechamento: repetir o
    /// pedido devolve o crédito que já existe.
    /// </summary>
    public CashbackCredit? EarnWithin(SqliteTransaction transaction, string customerId, string orderId, long eligibleCents, string actorUserId)
    {
        using (var existing = transaction.Command(
                   "SELECT id, amount_cents, expires_at FROM cashback_ledger " +
                   "WHERE tenant_id = $tenant AND order_id = $order AND entry_type = 'credit'",
                   ("$tenant", terminal.TenantId), ("$order", orderId)))
        using (var reader = existing.ExecuteReader())
        {
            if (reader.Read()) return new CashbackCredit(reader.GetString(0), reader.GetInt64(1), reader.GetString(2));
        }

        int basis, days;
        long cap;
        using (var rule = transaction.Command(
                   "SELECT percent_basis_points, max_per_sale_cents, validity_days FROM cashback_rules " +
                   "WHERE tenant_id = $tenant AND store_id = $store AND is_active = 1",
                   ("$tenant", terminal.TenantId), ("$store", terminal.StoreId)))
        using (var reader = rule.ExecuteReader())
        {
            if (!reader.Read() || eligibleCents <= 0) return null;
            basis = reader.GetInt32(0);
            cap = reader.GetInt64(1);
            days = reader.GetInt32(2);
        }

        var amount = Rounding.HalfUp(eligibleCents * (decimal)basis / 10_000m);
        if (cap > 0) amount = Math.Min(amount, cap);
        if (amount <= 0) return null;

        var id = Iso.NewId();
        var clientUuid = Iso.NewId();
        var created = _clock.GetUtcNow();
        var expires = Iso.Format(created.AddDays(days));
        using (var insert = transaction.Command(
                   "INSERT INTO cashback_ledger (id, tenant_id, store_id, customer_id, order_id, entry_type, amount_cents, " +
                   "expires_at, created_at, actor_user_id, client_uuid) " +
                   "VALUES ($id, $tenant, $store, $customer, $order, 'credit', $amount, $expires, $created, $actor, $uuid)",
                   ("$id", id), ("$tenant", terminal.TenantId), ("$store", terminal.StoreId), ("$customer", customerId),
                   ("$order", orderId), ("$amount", amount), ("$expires", expires), ("$created", Iso.Format(created)),
                   ("$actor", actorUserId), ("$uuid", clientUuid)))
        {
            insert.ExecuteNonQuery();
        }
        EnqueueCashback(transaction, id, clientUuid, customerId, orderId, "credit", amount, null, expires, actorUserId);
        return new CashbackCredit(id, amount, expires);
    }

    /// <summary>Saldo = créditos não vencidos menos o que já foi consumido de cada um.</summary>
    public long CashbackBalance(string customerId) =>
        Convert.ToInt64(database.Scalar(
            "SELECT COALESCE(SUM(c.amount_cents - COALESCE((SELECT SUM(d.amount_cents) FROM cashback_ledger d " +
            "WHERE d.source_credit_id = c.id AND d.entry_type = 'debit'), 0)), 0) " +
            "FROM cashback_ledger c WHERE c.tenant_id = $tenant AND c.customer_id = $customer " +
            "AND c.entry_type = 'credit' AND c.expires_at > $now",
            ("$tenant", terminal.TenantId), ("$customer", customerId), ("$now", Iso.Now(_clock))));

    /// <summary>
    /// Resgate FIFO por lote (o que vence antes sai antes), um débito por lote.
    /// </summary>
    /// <remarks>
    /// Como no Python, <b>nenhuma tela chama</b>: se o resgate é pagamento ou
    /// desconto é decisão do contador (<c>docs/plan.md</c>), e ela vem antes de
    /// ligá-lo no caixa.
    /// </remarks>
    public long RedeemCashback(string customerId, string orderId, long amountCents, string actorUserId)
    {
        if (amountCents <= 0) throw new CustomerException("Valor de resgate precisa ser positivo.");
        var now = Iso.Now(_clock);
        database.InTransaction(transaction =>
        {
            var credits = new List<(string Id, long Available)>();
            using (var read = transaction.Command(
                       "SELECT c.id, c.amount_cents - COALESCE((SELECT SUM(d.amount_cents) FROM cashback_ledger d " +
                       "WHERE d.source_credit_id = c.id AND d.entry_type = 'debit'), 0) " +
                       "FROM cashback_ledger c WHERE c.tenant_id = $tenant AND c.customer_id = $customer " +
                       "AND c.entry_type = 'credit' AND c.expires_at > $now ORDER BY c.expires_at, c.created_at",
                       ("$tenant", terminal.TenantId), ("$customer", customerId), ("$now", now)))
            using (var reader = read.ExecuteReader())
            {
                while (reader.Read()) credits.Add((reader.GetString(0), Math.Max(0, reader.GetInt64(1))));
            }
            if (credits.Sum(credit => credit.Available) < amountCents)
            {
                throw new CustomerException("Saldo de cashback insuficiente.");
            }

            var remaining = amountCents;
            foreach (var (creditId, available) in credits)
            {
                var take = Math.Min(remaining, available);
                if (take == 0) continue;
                var id = Iso.NewId();
                var clientUuid = Iso.NewId();
                using (var insert = transaction.Command(
                           "INSERT INTO cashback_ledger (id, tenant_id, store_id, customer_id, order_id, entry_type, amount_cents, " +
                           "source_credit_id, created_at, actor_user_id, client_uuid) " +
                           "VALUES ($id, $tenant, $store, $customer, $order, 'debit', $amount, $source, $now, $actor, $uuid)",
                           ("$id", id), ("$tenant", terminal.TenantId), ("$store", terminal.StoreId), ("$customer", customerId),
                           ("$order", orderId), ("$amount", take), ("$source", creditId), ("$now", now),
                           ("$actor", actorUserId), ("$uuid", clientUuid)))
                {
                    insert.ExecuteNonQuery();
                }
                EnqueueCashback(transaction, id, clientUuid, customerId, orderId, "debit", take, creditId, null, actorUserId);
                remaining -= take;
                if (remaining == 0) break;
            }
        });
        return amountCents;
    }

    private void EnqueueCashback(
        SqliteTransaction transaction, string id, string clientUuid, string customerId, string orderId, string entryType,
        long amount, string? sourceCreditId, string? expiresAt, string actorUserId) =>
        _outbox.Enqueue(transaction, "cashback_ledger", id, clientUuid, "insert", new Dictionary<string, object?>
        {
            ["id"] = id,
            ["store_id"] = terminal.StoreId,
            ["customer_id"] = customerId,
            ["order_id"] = orderId,
            ["entry_type"] = entryType,
            ["amount_cents"] = amount,
            ["source_credit_id"] = sourceCreditId,
            ["expires_at"] = expiresAt,
            ["actor_user_id"] = actorUserId,
        });

    // -- pré-pago ------------------------------------------------------------

    public long PrepaidBalance(string customerId, SqliteTransaction? transaction = null)
    {
        using var command = Sql.Command(
            database.Connection, transaction,
            "SELECT COALESCE(SUM(CASE WHEN entry_type IN ('deposit', 'refund') THEN amount_cents ELSE -amount_cents END), 0) " +
            "FROM prepaid_ledger WHERE tenant_id = $tenant AND customer_id = $customer",
            ("$tenant", terminal.TenantId), ("$customer", customerId));
        return Convert.ToInt64(command.ExecuteScalar());
    }

    /// <summary>Carga de crédito: dinheiro que a loja passa a dever ao cliente. Pede quem autorizou.</summary>
    public long Deposit(string customerId, long amountCents, string actorUserId, string authorizerUserId)
    {
        if (amountCents <= 0) throw new CustomerException("A carga precisa ser maior que zero.");
        return database.InTransaction(transaction =>
        {
            AppendPrepaid(transaction, customerId, "deposit", amountCents, null, actorUserId, authorizerUserId);
            ledger.Append(transaction, "prepaid_credited", actorUserId, new Dictionary<string, object?>
            {
                ["customer_id"] = customerId,
                ["amount_cents"] = amountCents,
            }, "warning", authorizerUserId);
            return PrepaidBalance(customerId, transaction);
        });
    }

    /// <summary>Consome o pré-pago na transação do fechamento. Idempotente por pedido.</summary>
    public long RedeemPrepaidWithin(SqliteTransaction transaction, string customerId, string orderId, long amountCents, string actorUserId)
    {
        if (amountCents <= 0) throw new CustomerException("Valor pré-pago precisa ser positivo.");
        using (var existing = transaction.Command(
                   "SELECT amount_cents FROM prepaid_ledger WHERE tenant_id = $tenant AND order_id = $order AND entry_type = 'debit'",
                   ("$tenant", terminal.TenantId), ("$order", orderId)))
        {
            if (existing.ExecuteScalar() is long already)
            {
                return already == amountCents
                    ? PrepaidBalance(customerId, transaction)
                    : throw new CustomerException("A venda já consumiu outro valor pré-pago.");
            }
        }
        if (PrepaidBalance(customerId, transaction) < amountCents) throw new CustomerException("Saldo pré-pago insuficiente.");
        AppendPrepaid(transaction, customerId, "debit", amountCents, orderId, actorUserId, null);
        ledger.Append(transaction, "prepaid_redeemed", actorUserId, new Dictionary<string, object?>
        {
            ["customer_id"] = customerId,
            ["order_id"] = orderId,
            ["amount_cents"] = amountCents,
        });
        return PrepaidBalance(customerId, transaction);
    }

    private void AppendPrepaid(
        SqliteTransaction transaction, string customerId, string entryType, long amount, string? orderId,
        string actorUserId, string? authorizerUserId)
    {
        var id = Iso.NewId();
        var clientUuid = Iso.NewId();
        var now = Iso.Now(_clock);
        using (var insert = transaction.Command(
                   "INSERT INTO prepaid_ledger (id, tenant_id, store_id, customer_id, entry_type, amount_cents, order_id, " +
                   "actor_user_id, authorizer_user_id, created_at, client_uuid) " +
                   "VALUES ($id, $tenant, $store, $customer, $type, $amount, $order, $actor, $authorizer, $now, $uuid)",
                   ("$id", id), ("$tenant", terminal.TenantId), ("$store", terminal.StoreId), ("$customer", customerId),
                   ("$type", entryType), ("$amount", amount), ("$order", orderId), ("$actor", actorUserId),
                   ("$authorizer", authorizerUserId), ("$now", now), ("$uuid", clientUuid)))
        {
            insert.ExecuteNonQuery();
        }
        _outbox.Enqueue(transaction, "prepaid_ledger", id, clientUuid, "insert", new Dictionary<string, object?>
        {
            ["id"] = id,
            ["store_id"] = terminal.StoreId,
            ["customer_id"] = customerId,
            ["entry_type"] = entryType,
            ["amount_cents"] = amount,
            ["order_id"] = orderId,
            ["actor_user_id"] = actorUserId,
            ["authorizer_user_id"] = authorizerUserId,
            ["created_at"] = now,
        });
    }

    // -- fiado ---------------------------------------------------------------

    public CreditPosition ConfigureCreditAccount(string customerId, long limitCents, int dueDays, string actorUserId, string authorizerUserId)
    {
        if (limitCents < 0 || dueDays is < 1 or > 365) throw new CustomerException("Limite ou prazo do fiado é inválido.");
        var now = Iso.Now(_clock);
        database.InTransaction(transaction =>
        {
            string? existing;
            using (var read = transaction.Command(
                       "SELECT client_uuid FROM customer_credit_accounts WHERE customer_id = $customer", ("$customer", customerId)))
            {
                existing = read.ExecuteScalar() as string;
            }
            var clientUuid = existing ?? Iso.NewId();
            using (var upsert = transaction.Command(
                       """
                       INSERT INTO customer_credit_accounts (customer_id, tenant_id, limit_cents, due_days, is_active, updated_at, client_uuid)
                       VALUES ($customer, $tenant, $limit, $days, 1, $now, $uuid)
                       ON CONFLICT (customer_id) DO UPDATE SET limit_cents = excluded.limit_cents,
                           due_days = excluded.due_days, is_active = 1, updated_at = excluded.updated_at, is_synced = 0
                       """,
                       ("$customer", customerId), ("$tenant", terminal.TenantId), ("$limit", limitCents), ("$days", dueDays),
                       ("$now", now), ("$uuid", clientUuid)))
            {
                upsert.ExecuteNonQuery();
            }
            // Atualização leva identidade própria: com o client_uuid da linha, a
            // nuvem a descartaria como duplicata do cadastro.
            _outbox.Enqueue(transaction, "customer_credit_accounts", customerId,
                existing is null ? clientUuid : Iso.NewId(), existing is null ? "insert" : "update",
                new Dictionary<string, object?>
                {
                    ["customer_id"] = customerId,
                    ["limit_cents"] = limitCents,
                    ["due_days"] = dueDays,
                    ["is_active"] = true,
                    ["updated_at"] = now,
                });
            ledger.Append(transaction, "credit_account_configured", actorUserId, new Dictionary<string, object?>
            {
                ["customer_id"] = customerId,
                ["limit_cents"] = limitCents,
                ["due_days"] = dueDays,
            }, "warning", authorizerUserId);
        });
        return CreditPositionOf(customerId);
    }

    /// <summary>Limite, em aberto, disponível e vencido — tudo derivado do ledger.</summary>
    public CreditPosition CreditPositionOf(string customerId, SqliteTransaction? transaction = null)
    {
        long limit;
        using (var account = Sql.Command(
                   database.Connection, transaction,
                   "SELECT limit_cents FROM customer_credit_accounts WHERE tenant_id = $tenant AND customer_id = $customer AND is_active = 1",
                   ("$tenant", terminal.TenantId), ("$customer", customerId)))
        {
            limit = account.ExecuteScalar() is long value ? value : 0;
        }
        var now = Iso.Now(_clock);
        long outstanding = 0, overdue = 0;
        foreach (var (_, open, dueAt) in Charges(customerId, transaction))
        {
            outstanding += open;
            if (dueAt is not null && string.CompareOrdinal(dueAt, now) < 0) overdue += open;
        }
        return new CreditPosition(limit, outstanding, Math.Max(0, limit - outstanding), overdue);
    }

    private List<(string Id, long Open, string? DueAt)> Charges(string customerId, SqliteTransaction? transaction)
    {
        using var command = Sql.Command(
            database.Connection, transaction,
            "SELECT c.id, c.amount_cents - COALESCE((SELECT SUM(p.amount_cents) FROM credit_account_ledger p " +
            "WHERE p.source_charge_id = c.id AND p.entry_type IN ('payment', 'forgive')), 0), c.due_at " +
            "FROM credit_account_ledger c WHERE c.tenant_id = $tenant AND c.customer_id = $customer AND c.entry_type = 'charge' " +
            "ORDER BY c.due_at, c.created_at",
            ("$tenant", terminal.TenantId), ("$customer", customerId));
        using var reader = command.ExecuteReader();
        var charges = new List<(string, long, string?)>();
        while (reader.Read())
        {
            charges.Add((reader.GetString(0), Math.Max(0, reader.GetInt64(1)), reader.IsDBNull(2) ? null : reader.GetString(2)));
        }
        return charges;
    }

    /// <summary>Lança a venda no fiado, na transação do fechamento. Idempotente por pedido; nunca passa do limite.</summary>
    public CreditPosition ChargeWithin(SqliteTransaction transaction, string customerId, string orderId, long amountCents, string actorUserId)
    {
        if (amountCents <= 0) throw new CustomerException("Valor do fiado precisa ser positivo.");
        using (var existing = transaction.Command(
                   "SELECT amount_cents FROM credit_account_ledger WHERE tenant_id = $tenant AND order_id = $order AND entry_type = 'charge'",
                   ("$tenant", terminal.TenantId), ("$order", orderId)))
        {
            if (existing.ExecuteScalar() is long already)
            {
                return already == amountCents
                    ? CreditPositionOf(customerId, transaction)
                    : throw new CustomerException("A venda já possui outra cobrança no fiado.");
            }
        }
        long? dueDays;
        using (var account = transaction.Command(
                   "SELECT due_days FROM customer_credit_accounts WHERE tenant_id = $tenant AND customer_id = $customer AND is_active = 1",
                   ("$tenant", terminal.TenantId), ("$customer", customerId)))
        {
            dueDays = account.ExecuteScalar() as long?;
        }
        if (dueDays is null || amountCents > CreditPositionOf(customerId, transaction).AvailableCents)
        {
            throw new CustomerException("Limite disponível do fiado é insuficiente.");
        }
        var dueAt = Iso.Format(_clock.GetUtcNow().AddDays(dueDays.Value));
        AppendCredit(transaction, customerId, "charge", amountCents, orderId, null, dueAt, actorUserId);
        ledger.Append(transaction, "credit_account_charged", actorUserId, new Dictionary<string, object?>
        {
            ["customer_id"] = customerId,
            ["order_id"] = orderId,
            ["amount_cents"] = amountCents,
            ["due_at"] = dueAt,
        }, "warning");
        return CreditPositionOf(customerId, transaction);
    }

    /// <summary>Pagamento do fiado: quita as cobranças mais antigas primeiro. Não passa da dívida.</summary>
    public CreditPosition PayCredit(string customerId, long amountCents, string actorUserId)
    {
        if (amountCents <= 0) throw new CustomerException("Pagamento precisa ser positivo.");
        return database.InTransaction(transaction =>
        {
            var charges = Charges(customerId, transaction);
            if (amountCents > charges.Sum(charge => charge.Open)) throw new CustomerException("Pagamento supera a dívida em aberto.");
            var remaining = amountCents;
            foreach (var (chargeId, open, _) in charges)
            {
                var take = Math.Min(remaining, open);
                if (take > 0)
                {
                    AppendCredit(transaction, customerId, "payment", take, null, chargeId, null, actorUserId);
                    remaining -= take;
                }
                if (remaining == 0) break;
            }
            ledger.Append(transaction, "credit_account_paid", actorUserId, new Dictionary<string, object?>
            {
                ["customer_id"] = customerId,
                ["amount_cents"] = amountCents,
            });
            return CreditPositionOf(customerId, transaction);
        });
    }

    private void AppendCredit(
        SqliteTransaction transaction, string customerId, string entryType, long amount, string? orderId,
        string? sourceChargeId, string? dueAt, string actorUserId)
    {
        var id = Iso.NewId();
        var clientUuid = Iso.NewId();
        var now = Iso.Now(_clock);
        using (var insert = transaction.Command(
                   "INSERT INTO credit_account_ledger (id, tenant_id, store_id, customer_id, entry_type, amount_cents, order_id, " +
                   "source_charge_id, due_at, actor_user_id, authorizer_user_id, created_at, client_uuid) " +
                   "VALUES ($id, $tenant, $store, $customer, $type, $amount, $order, $source, $due, $actor, NULL, $now, $uuid)",
                   ("$id", id), ("$tenant", terminal.TenantId), ("$store", terminal.StoreId), ("$customer", customerId),
                   ("$type", entryType), ("$amount", amount), ("$order", orderId), ("$source", sourceChargeId),
                   ("$due", dueAt), ("$actor", actorUserId), ("$now", now), ("$uuid", clientUuid)))
        {
            insert.ExecuteNonQuery();
        }
        _outbox.Enqueue(transaction, "credit_account_ledger", id, clientUuid, "insert", new Dictionary<string, object?>
        {
            ["id"] = id,
            ["store_id"] = terminal.StoreId,
            ["customer_id"] = customerId,
            ["entry_type"] = entryType,
            ["amount_cents"] = amount,
            ["order_id"] = orderId,
            ["source_charge_id"] = sourceChargeId,
            ["due_at"] = dueAt,
            ["actor_user_id"] = actorUserId,
            ["authorizer_user_id"] = null,
            ["created_at"] = now,
        });
    }
}
