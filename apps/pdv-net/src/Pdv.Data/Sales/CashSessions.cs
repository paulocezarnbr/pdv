using Pdv.Core;

namespace Pdv.Data.Sales;

/// <summary>A sessão aberta: quem, quando e o fundo de troco. <b>Nunca</b> o esperado.</summary>
public sealed record OpenCashSession(string Id, string OperatorId, string OpenedAt, long OpeningCents);

/// <summary>O resultado do fechamento — só existe depois de o declarado estar gravado.</summary>
public sealed record CashReconciliation(
    string SessionId, long DeclaredCents, long ExpectedCents, long DifferenceCents, string ClosedAt);

public sealed class CashSessionException(string message) : Exception(message);

/// <summary>Abertura e fechamento cego do caixa — o <c>CashSessionService</c> do Python, na mesma tabela.</summary>
/// <remarks>
/// <para>
/// O valor esperado não sai deste serviço enquanto a sessão está aberta: quem
/// sabe quanto "deveria" ter na gaveta conta até chegar lá. No fechamento, o
/// declarado é gravado na mesma transação em que o esperado é calculado, e só
/// então a divergência vira resultado e auditoria.
/// </para>
/// <para>
/// O esperado é o fundo de troco mais o que entrou em dinheiro menos o troco,
/// nas vendas pagas deste terminal desde a abertura.
/// </para>
/// </remarks>
public sealed class CashSessionService(
    PdvDatabase database, TerminalIdentity terminal, AuditLedger ledger, TimeProvider? clock = null)
{
    private readonly TimeProvider _clock = clock ?? TimeProvider.System;
    private readonly Outbox _outbox = new(clock);

    public OpenCashSession? Current()
    {
        using var command = Sql.Command(
            database.Connection, null,
            "SELECT id, user_id, opened_at, opening_amount_cents FROM cash_sessions " +
            "WHERE tenant_id = $tenant AND device_id = $device AND closed_at IS NULL ORDER BY opened_at DESC LIMIT 1",
            ("$tenant", terminal.TenantId), ("$device", terminal.DeviceId));
        using var reader = command.ExecuteReader();
        return reader.Read()
            ? new OpenCashSession(reader.GetString(0), reader.GetString(1), reader.GetString(2), reader.GetInt64(3))
            : null;
    }

    /// <summary>Abre com o fundo de troco. Se o mesmo operador já tem sessão aberta, é ela.</summary>
    /// <exception cref="CashSessionException">Fundo negativo, ou caixa aberto por outro operador.</exception>
    public OpenCashSession Open(string operatorId, long openingCents)
    {
        if (openingCents < 0) throw new CashSessionException("Fundo de troco não pode ser negativo.");
        if (Current() is { } existing)
        {
            return existing.OperatorId == operatorId
                ? existing
                : throw new CashSessionException("Há um caixa aberto por outro operador.");
        }

        var session = new OpenCashSession(Iso.NewId(), operatorId, Iso.Now(_clock), openingCents);
        database.Execute(
            """
            INSERT INTO cash_sessions
                (id, tenant_id, store_id, device_id, user_id, opened_at, opening_amount_cents, blind_close, client_uuid, is_synced)
            VALUES ($id, $tenant, $store, $device, $user, $at, $opening, 1, $uuid, 0)
            """,
            ("$id", session.Id), ("$tenant", terminal.TenantId), ("$store", terminal.StoreId),
            ("$device", terminal.DeviceId), ("$user", operatorId), ("$at", session.OpenedAt),
            ("$opening", openingCents), ("$uuid", Iso.NewId()));
        return session;
    }

    /// <summary>Fecha às cegas: o declarado entra antes de o esperado existir.</summary>
    /// <param name="authorizerId">Quem liberou o fechamento (gerente). Conferido de novo aqui, contra o cadastro.</param>
    /// <exception cref="CashSessionException">Valor negativo, sem caixa aberto, ou já fechado.</exception>
    public CashReconciliation Close(long declaredCents, string operatorId, string? authorizerId = null)
    {
        if (declaredCents < 0) throw new CashSessionException("Valor contado não pode ser negativo.");

        return database.InTransaction(transaction =>
        {
            if (authorizerId is not null)
            {
                // Regra a mais que o Python: quem autorizou ainda pode autorizar.
                using var check = transaction.Command(
                    "SELECT 1 FROM users WHERE id = $id AND tenant_id = $tenant AND is_active = 1 AND can_authorize = 1",
                    ("$id", authorizerId), ("$tenant", terminal.TenantId));
                if (check.ExecuteScalar() is null)
                {
                    throw new CashSessionException("Quem autorizou o fechamento não pode mais autorizar.");
                }
            }

            string id, user, openedAt, clientUuid;
            long opening;
            using (var read = transaction.Command(
                       "SELECT id, user_id, opened_at, opening_amount_cents, client_uuid FROM cash_sessions " +
                       "WHERE tenant_id = $tenant AND device_id = $device AND closed_at IS NULL ORDER BY opened_at DESC LIMIT 1",
                       ("$tenant", terminal.TenantId), ("$device", terminal.DeviceId)))
            using (var reader = read.ExecuteReader())
            {
                if (!reader.Read()) throw new CashSessionException("Não há caixa aberto.");
                id = reader.GetString(0);
                user = reader.GetString(1);
                openedAt = reader.GetString(2);
                opening = reader.GetInt64(3);
                clientUuid = reader.GetString(4);
            }

            long cash;
            using (var sum = transaction.Command(
                       "SELECT COALESCE(SUM(p.amount_cents - p.change_cents), 0) FROM payments p " +
                       "JOIN orders o ON o.id = p.order_id " +
                       "WHERE p.method = 'cash' AND o.device_id = $device AND p.created_at >= $opened AND o.status = 'paid'",
                       ("$device", terminal.DeviceId), ("$opened", openedAt)))
            {
                cash = Convert.ToInt64(sum.ExecuteScalar());
            }
            var expected = opening + cash;
            var difference = declaredCents - expected;
            var closedAt = Iso.Now(_clock);

            using (var update = transaction.Command(
                       "UPDATE cash_sessions SET closed_at = $at, declared_amount_cents = $declared, " +
                       "expected_amount_cents = $expected, difference_cents = $difference, is_synced = 0 " +
                       "WHERE id = $id AND closed_at IS NULL",
                       ("$at", closedAt), ("$declared", declaredCents), ("$expected", expected),
                       ("$difference", difference), ("$id", id)))
            {
                if (update.ExecuteNonQuery() != 1) throw new CashSessionException("O caixa já foi fechado.");
            }

            ledger.Append(transaction, "session_closed", operatorId, new Dictionary<string, object?>
            {
                ["cash_session_id"] = id,
                ["declared_cents"] = declaredCents,
                ["expected_cents"] = expected,
                ["difference_cents"] = difference,
                ["blind_close"] = true,
            }, severity: difference != 0 ? "warning" : "info", authorizerUserId: authorizerId);

            _outbox.Enqueue(transaction, "cash_sessions", id, clientUuid, "insert", new Dictionary<string, object?>
            {
                ["id"] = id,
                ["store_id"] = terminal.StoreId,
                ["device_id"] = terminal.DeviceId,
                ["operator_id"] = user,
                ["opened_at"] = openedAt,
                ["closed_at"] = closedAt,
                ["opening_cents"] = opening,
                ["declared_cents"] = declaredCents,
                ["expected_cents"] = expected,
                ["difference_cents"] = difference,
                ["blind_close"] = true,
                ["client_uuid"] = clientUuid,
            });

            return new CashReconciliation(id, declaredCents, expected, difference, closedAt);
        });
    }
}
