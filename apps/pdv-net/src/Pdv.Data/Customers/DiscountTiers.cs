using Microsoft.Data.Sqlite;
using Pdv.Core;
using Pdv.Data.Auth;
using Pdv.Data.Sales;

namespace Pdv.Data.Customers;

public sealed record DiscountTier(string Id, string Code, string Name, int PercentBasisPoints, int Priority, bool RequiresManager)
{
    /// <summary>O desconto do nível sobre o subtotal, "meio para cima".</summary>
    public long DiscountFor(long subtotalCents) => Rounding.HalfUp(subtotalCents * (decimal)PercentBasisPoints / 10_000m);

    /// <summary>"Dono" sempre exige a senha do proprietário; os outros, a de gerente quando pedem.</summary>
    public IReadOnlySet<string>? RequiredRoles => Code == "owner" ? Roles.OwnerOnly : RequiresManager ? Roles.Manager : null;
}

/// <summary>Níveis de desconto por cliente — o <c>DiscountTierService</c> do Python.</summary>
/// <remarks>
/// <para>
/// "Funcionário" e "Dono" são protegidos: só um proprietário atribui, e quem
/// foi classificado assim não muda de nível. "Dono" exige senha em todo uso,
/// mesmo que o cadastro diga o contrário — importação, sync ou código antigo
/// também passam por aqui.
/// </para>
/// <para>
/// O nível não acumula com outro desconto: prevalece o maior.
/// </para>
/// </remarks>
public sealed class DiscountTierService(
    PdvDatabase database, TerminalIdentity terminal, AuditLedger ledger, TimeProvider? clock = null)
{
    public static readonly IReadOnlyList<string> Codes = ["bronze", "silver", "gold", "diamond", "employee", "owner"];

    public static readonly IReadOnlySet<string> ProtectedCodes = new HashSet<string>(StringComparer.Ordinal) { "employee", "owner" };

    private readonly TimeProvider _clock = clock ?? TimeProvider.System;
    private readonly Outbox _outbox = new(clock);

    public DiscountTier Configure(string code, string name, decimal percent, int priority, bool requiresManager, string actorUserId)
    {
        code = code.Trim().ToLowerInvariant();
        name = name.Trim();
        if (!Codes.Contains(code)) throw new CustomerException("Nível desconhecido.");
        if (percent < 0 || percent > 100 || name.Length == 0) throw new CustomerException("Nome ou percentual do nível é inválido.");
        if (code == "owner") requiresManager = true;
        var basis = Rounding.BasisPoints(percent);
        var now = Iso.Now(_clock);

        return database.InTransaction(transaction =>
        {
            string? existingId = null, existingUuid = null;
            using (var read = transaction.Command(
                       "SELECT id, client_uuid FROM discount_tiers WHERE tenant_id = $tenant AND store_id = $store AND code = $code",
                       ("$tenant", terminal.TenantId), ("$store", terminal.StoreId), ("$code", code)))
            using (var reader = read.ExecuteReader())
            {
                if (reader.Read())
                {
                    existingId = reader.GetString(0);
                    existingUuid = reader.GetString(1);
                }
            }
            var id = existingId ?? Iso.NewId();
            var clientUuid = existingUuid ?? Iso.NewId();
            using (var upsert = transaction.Command(
                       """
                       INSERT INTO discount_tiers (id, tenant_id, store_id, code, name, percent_basis_points, priority,
                                                   requires_manager, updated_at, client_uuid)
                       VALUES ($id, $tenant, $store, $code, $name, $basis, $priority, $requires, $now, $uuid)
                       ON CONFLICT (tenant_id, store_id, code) DO UPDATE SET name = excluded.name,
                           percent_basis_points = excluded.percent_basis_points, priority = excluded.priority,
                           requires_manager = excluded.requires_manager, is_active = 1, updated_at = excluded.updated_at,
                           is_synced = 0
                       """,
                       ("$id", id), ("$tenant", terminal.TenantId), ("$store", terminal.StoreId), ("$code", code),
                       ("$name", name), ("$basis", basis), ("$priority", priority), ("$requires", requiresManager ? 1 : 0),
                       ("$now", now), ("$uuid", clientUuid)))
            {
                upsert.ExecuteNonQuery();
            }
            _outbox.Enqueue(transaction, "discount_tiers", id,
                existingId is null ? clientUuid : Iso.NewId(), existingId is null ? "insert" : "update",
                new Dictionary<string, object?>
                {
                    ["id"] = id,
                    ["store_id"] = terminal.StoreId,
                    ["code"] = code,
                    ["name"] = name,
                    ["percent_basis_points"] = basis,
                    ["priority"] = priority,
                    ["requires_manager"] = requiresManager,
                    ["is_active"] = true,
                    ["updated_at"] = now,
                });
            ledger.Append(transaction, "discount_tier_configured", actorUserId, new Dictionary<string, object?>
            {
                ["tier_id"] = id,
                ["code"] = code,
                ["percent_basis_points"] = basis,
            }, "warning", actorUserId);
            return new DiscountTier(id, code, name, basis, priority, requiresManager);
        });
    }

    public IReadOnlyList<DiscountTier> ListActive()
    {
        var now = Iso.Now(_clock);
        using var command = Sql.Command(
            database.Connection, null,
            $"SELECT {Columns} FROM discount_tiers WHERE tenant_id = $tenant AND store_id = $store AND is_active = 1 " +
            "AND (valid_from IS NULL OR valid_from <= $now) AND (valid_until IS NULL OR valid_until >= $now) " +
            "ORDER BY priority DESC, name",
            ("$tenant", terminal.TenantId), ("$store", terminal.StoreId), ("$now", now));
        using var reader = command.ExecuteReader();
        var tiers = new List<DiscountTier>();
        while (reader.Read()) tiers.Add(Read(reader));
        return tiers;
    }

    /// <summary>Atribui o nível ao cliente. Os protegidos só por proprietário, e não saem mais.</summary>
    public void Assign(string customerId, string tierId, string actorUserId)
    {
        var now = Iso.Now(_clock);
        database.InTransaction(transaction =>
        {
            string code;
            using (var target = transaction.Command(
                       "SELECT code FROM discount_tiers WHERE id = $id AND tenant_id = $tenant AND store_id = $store AND is_active = 1",
                       ("$id", tierId), ("$tenant", terminal.TenantId), ("$store", terminal.StoreId)))
            {
                code = target.ExecuteScalar() as string ?? throw new CustomerException("Nível não existe ou está inativo.");
            }
            if (ProtectedCodes.Contains(code))
            {
                using var actor = transaction.Command(
                    "SELECT role FROM users WHERE id = $id AND tenant_id = $tenant AND is_active = 1",
                    ("$id", actorUserId), ("$tenant", terminal.TenantId));
                if (actor.ExecuteScalar() as string != "owner")
                {
                    throw new CustomerException("Somente um proprietário pode atribuir os níveis Funcionário ou Dono.");
                }
            }

            string? existingUuid = null, currentTier = null, currentCode = null;
            using (var read = transaction.Command(
                       "SELECT c.client_uuid, c.tier_id, t.code FROM customer_discount_tiers c " +
                       "JOIN discount_tiers t ON t.id = c.tier_id WHERE c.customer_id = $customer AND c.tenant_id = $tenant",
                       ("$customer", customerId), ("$tenant", terminal.TenantId)))
            using (var reader = read.ExecuteReader())
            {
                if (reader.Read())
                {
                    existingUuid = reader.GetString(0);
                    currentTier = reader.GetString(1);
                    currentCode = reader.GetString(2);
                }
            }
            if (currentTier == tierId) return;
            if (currentCode is not null && ProtectedCodes.Contains(currentCode))
            {
                throw new CustomerException("Clientes classificados como Funcionário ou Dono não podem mudar para outro nível.");
            }

            var clientUuid = existingUuid ?? Iso.NewId();
            using (var upsert = transaction.Command(
                       """
                       INSERT INTO customer_discount_tiers (customer_id, tenant_id, tier_id, assigned_by_user_id, assigned_at, client_uuid)
                       VALUES ($customer, $tenant, $tier, $actor, $now, $uuid)
                       ON CONFLICT (customer_id) DO UPDATE SET tier_id = excluded.tier_id,
                           assigned_by_user_id = excluded.assigned_by_user_id, assigned_at = excluded.assigned_at, is_synced = 0
                       """,
                       ("$customer", customerId), ("$tenant", terminal.TenantId), ("$tier", tierId), ("$actor", actorUserId),
                       ("$now", now), ("$uuid", clientUuid)))
            {
                upsert.ExecuteNonQuery();
            }
            _outbox.Enqueue(transaction, "customer_discount_tiers", customerId,
                existingUuid is null ? clientUuid : Iso.NewId(), existingUuid is null ? "insert" : "update",
                new Dictionary<string, object?>
                {
                    ["customer_id"] = customerId,
                    ["tier_id"] = tierId,
                    ["assigned_by_user_id"] = actorUserId,
                    ["assigned_at"] = now,
                });
            ledger.Append(transaction, "discount_tier_assigned", actorUserId, new Dictionary<string, object?>
            {
                ["customer_id"] = customerId,
                ["tier_id"] = tierId,
            }, "warning", actorUserId);
        });
    }

    /// <summary>O nível vigente do cliente. "Dono" exige senha mesmo que o banco diga que não.</summary>
    public DiscountTier? ForCustomer(string customerId)
    {
        var now = Iso.Now(_clock);
        using var command = Sql.Command(
            database.Connection, null,
            $"SELECT {PrefixedColumns} FROM customer_discount_tiers c JOIN discount_tiers t ON t.id = c.tier_id " +
            "WHERE c.tenant_id = $tenant AND c.customer_id = $customer AND t.is_active = 1 " +
            "AND (t.valid_from IS NULL OR t.valid_from <= $now) AND (t.valid_until IS NULL OR t.valid_until >= $now)",
            ("$tenant", terminal.TenantId), ("$customer", customerId), ("$now", now));
        using var reader = command.ExecuteReader();
        if (!reader.Read()) return null;
        var tier = Read(reader);
        return tier.Code == "owner" && !tier.RequiresManager ? tier with { RequiresManager = true } : tier;
    }

    /// <summary>
    /// Aplica o nível à venda aberta, sem acumular: prevalece o maior desconto
    /// já concedido. O papel de quem autorizou é conferido na transação.
    /// </summary>
    /// <returns>O desconto que ficou na venda.</returns>
    public long ApplyToOrder(string orderId, DiscountTier tier, string operatorId, string? authorizerId)
    {
        if (tier.RequiredRoles is { } roles && authorizerId is null)
        {
            throw new CustomerException($"Este nível exige autorização de {(tier.Code == "owner" ? "proprietário" : "gerente")}.");
        }
        return database.InTransaction(transaction =>
        {
            var order = SaleRepository.RecomputeTotals(transaction, orderId, _clock);
            if (order.SubtotalCents <= 0) throw new CustomerException("Não há venda aberta com itens para aplicar o nível");
            var candidate = tier.DiscountFor(order.SubtotalCents);
            if (candidate <= order.DiscountCents) return order.DiscountCents;

            if (tier.RequiredRoles is { } required)
            {
                using var check = transaction.Command(
                    "SELECT role FROM users WHERE id = $id AND tenant_id = $tenant AND is_active = 1 AND can_authorize = 1",
                    ("$id", authorizerId), ("$tenant", terminal.TenantId));
                if (check.ExecuteScalar() is not string role || !required.Contains(role))
                {
                    throw new CustomerException(tier.Code == "owner"
                        ? "O nível Dono exige a senha de um proprietário."
                        : "Este nível exige autorização de gerente.");
                }
            }

            SaleRepository.StoreTotals(transaction, orderId, order.SubtotalCents, candidate, _clock);
            using (var update = transaction.Command(
                       "UPDATE orders SET discount_tier_id = $tier, authorized_by_user_id = $by WHERE id = $id",
                       ("$tier", tier.Id), ("$by", authorizerId), ("$id", orderId)))
            {
                update.ExecuteNonQuery();
            }
            ledger.Append(transaction, "discount_applied", operatorId, new Dictionary<string, object?>
            {
                ["order_id"] = orderId,
                ["tier_id"] = tier.Id,
                ["tier_code"] = tier.Code,
                ["percent_basis_points"] = tier.PercentBasisPoints,
                ["discount_cents"] = candidate,
                ["channel"] = "automatic_tier",
            }, "warning", authorizerId);
            return candidate;
        });
    }

    private const string Columns = "id, code, name, percent_basis_points, priority, requires_manager";
    private const string PrefixedColumns = "t.id, t.code, t.name, t.percent_basis_points, t.priority, t.requires_manager";

    private static DiscountTier Read(SqliteDataReader reader) =>
        new(reader.GetString(0), reader.GetString(1), reader.GetString(2), reader.GetInt32(3), reader.GetInt32(4), reader.GetInt64(5) != 0);
}
