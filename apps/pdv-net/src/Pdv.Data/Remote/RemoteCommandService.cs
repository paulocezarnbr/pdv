using System.Globalization;
using System.Text.Json;
using Microsoft.Data.Sqlite;
using Pdv.Core;
using Pdv.Core.Remote;
using Pdv.Core.Scale;
using Pdv.Data.Auth;
using Pdv.Data.Sales;

namespace Pdv.Data.Remote;

/// <summary>Um comando recusado. A severidade separa engano de operação de ataque ou defeito grave.</summary>
public sealed class CommandRefusedException(string message, string severity = "warning") : Exception(message)
{
    public string Severity { get; } = severity;
}

/// <summary>
/// O aceite no caixa não foi aceito — e isso <b>não</b> decide o comando. Um
/// PIN digitado errado não pode desfazer, por engano, o que o gerente mandou.
/// </summary>
public sealed class ConfirmationException(string message) : Exception(message);

public sealed record ApplyReport(int Applied = 0, int Refused = 0, int Deferred = 0, int Awaiting = 0)
{
    public int Total => Applied + Refused + Deferred + Awaiting;
}

/// <summary>Aplica os comandos do painel — o <c>RemoteCommandService</c> do Python, com as mesmas sete travas.</summary>
/// <remarks>
/// <para>
/// Este serviço <b>inverte o modelo de confiança</b>: o caixa passa a aceitar
/// ordens de fora. Quem comprometer o painel tentaria conceder descontos e
/// cancelar itens em todas as lojas de uma vez. As travas:
/// </para>
/// <list type="number">
/// <item><b>Assinatura HMAC por terminal</b>: a nuvem emitiu aquele comando para este terminal.</item>
/// <item><b>Janela de validade</b>: 12 h, com 5 min de folga para relógio adiantado.</item>
/// <item><b>Teto do perfil de quem emitiu</b>, lido da réplica local de <c>users</c>, nunca do comando.</item>
/// <item><b>Só pedido aberto</b>: venda fechada se corrige por estorno, não pelo painel.</item>
/// <item><b>Idempotência por <c>command_uuid</c></b>: o status sai de <c>pending</c> junto do efeito.</item>
/// <item><b>Chave local</b> (<c>remote.commands_enabled = 0</c>): desliga sem depender da nuvem.</item>
/// <item><b>Aceite presencial</b> para cancelar o que já foi para a cozinha: o comando espera o PIN de alguém no caixa.</item>
/// </list>
/// <para>
/// Toda recusa vira evento no ledger: ninguém emite por acidente um desconto
/// acima do próprio teto, e o padrão de recusas denuncia a credencial vazada
/// antes do prejuízo.
/// </para>
/// </remarks>
public sealed class RemoteCommandService
{
    /// <summary>Chave de desligamento. Ausente é <b>ligado</b>; "0" desliga.</summary>
    public const string EnabledKey = "remote.commands_enabled";

    /// <summary>Canal gravado na auditoria: o relatório separa presencial de remoto por ele.</summary>
    public const string Channel = "remote_panel";

    /// <summary>
    /// Quem pode dar o aceite: gente do balcão. O garçom fica de fora — no furto
    /// de salão é ele quem leva o prato, e o aceite dele fecharia o circuito.
    /// </summary>
    public static readonly IReadOnlySet<string> ConfirmerRoles =
        new HashSet<string>(StringComparer.Ordinal) { "cashier", "manager", "owner" };

    private static readonly Dictionary<string, string> KitchenLabels = new(StringComparer.Ordinal)
    {
        ["queued"] = "já na fila da cozinha",
        ["preparing"] = "em preparo na cozinha",
        ["ready"] = "pronto na cozinha",
        ["delivered"] = "já entregue à mesa",
    };

    private readonly PdvDatabase _database;
    private readonly TerminalProfile _terminal;
    private readonly byte[] _deviceSecret;
    private readonly AuditLedger _ledger;
    private readonly StaffAuthentication _authentication;
    private readonly TimeProvider _clock;
    private readonly Action<string> _log;
    private readonly CommandInbox _inbox;
    private readonly SaleAdjustments _adjustments;

    public RemoteCommandService(
        PdvDatabase database, TerminalProfile terminal, byte[] deviceSecret, AuditLedger ledger,
        StaffAuthentication authentication, TimeProvider? clock = null, Action<string>? log = null)
    {
        _database = database;
        _terminal = terminal;
        _deviceSecret = deviceSecret;
        _ledger = ledger;
        _authentication = authentication;
        _clock = clock ?? TimeProvider.System;
        _log = log ?? (_ => { });
        _inbox = new CommandInbox(database, clock);
        _adjustments = new SaleAdjustments(database, terminal.Identity, ledger, clock);
    }

    public CommandInbox Inbox => _inbox;

    /// <summary>
    /// Um pedido mudou por comando do painel. A tela que o tem aberto relê do
    /// banco — senão mostraria o total antigo ao cliente.
    /// </summary>
    public event Action<string>? OrderChanged;

    public bool Enabled =>
        _database.Scalar("SELECT value FROM device_settings WHERE key = $key", ("$key", EnabledKey)) as string != "0";

    /// <summary>Aplica os pendentes, cada um na sua transação.</summary>
    public ApplyReport ApplyPending(int limit = 50)
    {
        int applied = 0, refused = 0, deferred = 0, awaiting = 0;
        foreach (var command in _inbox.Pending(limit))
        {
            try
            {
                var message = ApplyOne(command, confirmedBy: null);
                _log($"Comando {command.CommandUuid} aplicado: {message}");
                applied++;
            }
            catch (NeedsConfirmation wait)
            {
                if (_inbox.RequestConfirmation(command.CommandUuid, wait.Note))
                {
                    _log($"Comando {command.CommandUuid} espera aceite no caixa: {wait.Note}");
                }
                awaiting++;
            }
            catch (CommandRefusedException refusal)
            {
                try
                {
                    Refuse(command, refusal.Message, refusal.Severity);
                    refused++;
                }
                catch (AlreadySettled)
                {
                    deferred++;
                }
            }
            catch (AlreadySettled)
            {
                // Corrida com outra execução: quem chegou primeiro decidiu.
                deferred++;
            }
            catch (Exception error)
            {
                // Falha nossa não vira recusa definitiva de um comando legítimo:
                // continua pendente e tenta de novo no próximo ciclo.
                _log($"Falha ao aplicar comando {command.CommandUuid}: {error.Message}");
                deferred++;
            }
        }
        return new ApplyReport(applied, refused, deferred, awaiting);
    }

    // -- aceite no caixa -----------------------------------------------------

    public IReadOnlyList<AwaitingCommand> Awaiting(int limit = 50) => _inbox.AwaitingConfirmation(limit);

    /// <summary>Logins que podem dar o aceite, para o diálogo.</summary>
    public IReadOnlyList<string> ConfirmerLogins()
    {
        using var command = Sql.Command(
            _database.Connection, null,
            "SELECT login FROM users WHERE tenant_id = $tenant AND is_active = 1 " +
            "AND role IN ('cashier', 'manager', 'owner') ORDER BY name",
            ("$tenant", _terminal.TenantId));
        using var reader = command.ExecuteReader();
        var logins = new List<string>();
        while (reader.Read()) logins.Add(reader.GetString(0));
        return logins;
    }

    /// <summary>Aplica um comando que esperava aceite, com a credencial de quem aceita.</summary>
    /// <remarks>
    /// A credencial é conferida <b>aqui</b>, não no diálogo: a trava 7 não pode
    /// depender de nenhuma tela lembrar dela. As outras travas rodam de novo —
    /// aceitar não ressuscita o que deixou de valer.
    /// </remarks>
    /// <exception cref="AuthenticationException">Login ou PIN errados (com o freio).</exception>
    /// <exception cref="ConfirmationException">Papel que não pode aceitar, ou comando que não espera mais.</exception>
    /// <exception cref="CommandRefusedException">Uma trava recusou; o comando fica recusado.</exception>
    public string Confirm(string commandUuid, string login, string pin)
    {
        var confirmer = Confirmer(login, pin);
        var command = AwaitingCommandOf(commandUuid);
        string message;
        try
        {
            message = ApplyOne(command, confirmer);
        }
        catch (NeedsConfirmation)
        {
            throw new ConfirmationException("O comando ainda não pôde ser aplicado.");
        }
        catch (CommandRefusedException refusal)
        {
            try
            {
                Refuse(command, refusal.Message, refusal.Severity);
            }
            catch (AlreadySettled)
            {
                throw new ConfirmationException("Este comando já foi decidido.");
            }
            throw;
        }
        catch (AlreadySettled)
        {
            throw new ConfirmationException("Este comando já foi decidido.");
        }
        _log($"Comando {commandUuid} aplicado com aceite de {confirmer.Name}: {message}");
        return message;
    }

    /// <summary>
    /// Recusa no caixa. É definitiva e volta ao painel com o nome de quem recusou
    /// e o motivo: "recusado" sem motivo faz o gerente emitir de novo igual.
    /// </summary>
    public void Decline(string commandUuid, string login, string pin, string reason)
    {
        reason = string.Join(' ', reason.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
        reason = string.Concat(reason.EnumerateRunes().Take(200).Select(rune => rune.ToString()));
        if (reason.Length == 0) throw new ConfirmationException("Recusar exige motivo — ele volta para o painel.");

        var confirmer = Confirmer(login, pin);
        var command = AwaitingCommandOf(commandUuid);
        try
        {
            Refuse(command, $"Recusado no caixa por {confirmer.Name}: {reason}", "warning",
                new Dictionary<string, object?>
                {
                    ["declined_by_user_id"] = confirmer.Id,
                    ["declined_by_name"] = confirmer.Name,
                });
        }
        catch (AlreadySettled)
        {
            throw new ConfirmationException("Este comando já foi decidido.");
        }
    }

    private Identity Confirmer(string login, string pin)
    {
        var identity = _authentication.Authenticate(login, pin);
        if (!ConfirmerRoles.Contains(identity.Role))
        {
            throw new ConfirmationException(
                "O aceite de comando do painel exige alguém do caixa: operador, gerente ou proprietário.");
        }
        return identity;
    }

    private RemoteCommand AwaitingCommandOf(string commandUuid)
    {
        var command = _inbox.GetPending(commandUuid) ?? throw new ConfirmationException("Este comando já foi decidido.");
        // Só o que o terminal pôs para esperar: aceitar qualquer pendente pelo
        // caixa pularia a avaliação que decide se ele precisa de aceite.
        if (!_inbox.IsAwaiting(commandUuid)) throw new ConfirmationException("Este comando não está esperando aceite.");
        return command;
    }

    // -- aplicação -----------------------------------------------------------

    private string ApplyOne(RemoteCommand command, Identity? confirmedBy)
    {
        CheckAdmissible(command);
        string? changedOrder = null;
        var message = command.Kind switch
        {
            CommandProtocol.ApplyDiscount => ApplyDiscount(command, out changedOrder),
            CommandProtocol.CancelItem => CancelItem(command, confirmedBy, out changedOrder),
            _ => throw new CommandRefusedException($"Comando desconhecido: {command.Kind}"),
        };
        // Só depois do commit: avisar antes mostraria um desconto que o banco recusou.
        if (changedOrder is not null) OrderChanged?.Invoke(changedOrder);
        return message;
    }

    /// <summary>As travas que não dependem do tipo do comando.</summary>
    private void CheckAdmissible(RemoteCommand command)
    {
        if (!Enabled) throw new CommandRefusedException("Canal de comando remoto desligado neste terminal.");
        if (command.TenantId != _terminal.TenantId) throw new CommandRefusedException("Comando de outra rede.", "critical");
        if (command.DeviceId != _terminal.DeviceId)
        {
            // Endereçado a outro terminal e chegando aqui: replay ou erro de rota.
            throw new CommandRefusedException("Comando endereçado a outro terminal.", "critical");
        }
        if (!CommandProtocol.Verify(command, _deviceSecret))
        {
            // O único caso aqui que nunca é engano de operação.
            throw new CommandRefusedException("Assinatura inválida.", "critical");
        }
        if (!CommandProtocol.IsFresh(command.IssuedAt, _clock.GetUtcNow()))
        {
            throw new CommandRefusedException("Comando fora da janela de validade — emita novamente.");
        }
    }

    private string ApplyDiscount(RemoteCommand command, out string? changedOrder)
    {
        var orderId = Text(command.Payload, "order_id");
        var percent = Percent(command.Payload, "percent");
        var reason = Text(command.Payload, "reason", "Desconto remoto exige motivo.");

        var (discount, _) = _database.InTransaction(transaction =>
        {
            var ceiling = RequireAuthorizer(transaction, command.IssuedByUserId, null).Ceiling;
            if (percent > ceiling)
            {
                throw new CommandRefusedException(
                    $"{command.IssuedByName} pode conceder até {CommandProtocol.Plain(ceiling)}% — " +
                    $"o comando pede {CommandProtocol.Plain(percent)}%.");
            }
            RequireOpenOrder(transaction, orderId);

            var fresh = SaleRepository.RecomputeTotals(transaction, orderId, _clock);
            var cents = WeightPricing.DiscountFor(fresh.SubtotalCents, percent);
            var order = SaleRepository.StoreTotals(transaction, orderId, fresh.SubtotalCents, cents, _clock);

            Audit(transaction, command, "discount_applied", "warning", new Dictionary<string, object?>
            {
                ["order_id"] = orderId,
                ["percent"] = CommandProtocol.Plain(percent),
                ["subtotal_cents"] = order.SubtotalCents,
                ["discount_cents"] = cents,
                ["total_cents"] = order.TotalCents,
                ["reason"] = reason,
            });
            Claim(transaction, command);
            return (cents, order);
        });

        changedOrder = orderId;
        return $"desconto de {CommandProtocol.Plain(percent)}% (R$ {(discount / 100m).ToString("0.00", CultureInfo.InvariantCulture)})";
    }

    private string CancelItem(RemoteCommand command, Identity? confirmedBy, out string? changedOrder)
    {
        var orderId = Text(command.Payload, "order_id");
        var itemId = Text(command.Payload, "order_item_id");
        var reason = Text(command.Payload, "reason", "Cancelamento remoto exige motivo.");

        var productName = _database.InTransaction(transaction =>
        {
            RequireAuthorizer(transaction, command.IssuedByUserId, Roles.ItemCancel);
            var order = RequireOpenOrder(transaction, orderId);

            string name;
            long total;
            bool canceled;
            using (var read = transaction.Command(
                       "SELECT product_name, total_cents, canceled_at IS NOT NULL FROM order_items WHERE id = $id AND order_id = $order",
                       ("$id", itemId), ("$order", orderId)))
            using (var reader = read.ExecuteReader())
            {
                if (!reader.Read()) throw new CommandRefusedException("Item não encontrado neste pedido.");
                name = reader.GetString(0);
                total = reader.GetInt64(1);
                canceled = reader.GetInt64(2) != 0;
            }
            if (canceled) throw new CommandRefusedException("O item já estava cancelado.");

            var kitchen = KitchenStatus(transaction, itemId);
            if (kitchen is not null && confirmedBy is null)
            {
                throw new NeedsConfirmation(ConfirmationNote(command, order, name, total, kitchen, reason));
            }

            _adjustments.CancelWithin(transaction, orderId, itemId, command.IssuedByUserId, $"[{Channel}] {reason}");
            // O ticket sai da fila junto com o item: deixá-lo mandaria a cozinha
            // preparar um prato que já não está na conta.
            using (var tickets = transaction.Command(
                       "UPDATE kds_tickets SET status = 'canceled', updated_at = $now " +
                       "WHERE order_item_id = $item AND status <> 'canceled'",
                       ("$now", Iso.Now(_clock)), ("$item", itemId)))
            {
                tickets.ExecuteNonQuery();
            }
            SaleRepository.RecomputeTotals(transaction, orderId, _clock);

            var payload = new Dictionary<string, object?>
            {
                ["order_id"] = orderId,
                ["order_item_id"] = itemId,
                ["product_name"] = name,
                ["total_cents"] = total,
                ["reason"] = reason,
            };
            if (kitchen is not null) payload["kitchen_status"] = kitchen;
            if (confirmedBy is not null)
            {
                // A terceira identidade: quem estava na loja e concordou.
                payload["confirmed_by_user_id"] = confirmedBy.Id;
                payload["confirmed_by_name"] = confirmedBy.Name;
            }
            Audit(transaction, command, "item_canceled", "critical", payload);
            Claim(transaction, command);
            return name;
        });

        changedOrder = orderId;
        var message = $"item {productName} cancelado";
        return confirmedBy is null ? message : message + $" com aceite de {confirmedBy.Name} no caixa";
    }

    // -- travas --------------------------------------------------------------

    /// <summary>Marca o comando aplicado dentro da transação do efeito; se outro chegou antes, tudo volta.</summary>
    private void Claim(SqliteTransaction transaction, RemoteCommand command)
    {
        if (!_inbox.SettleIn(transaction, command.CommandUuid, "applied", "aplicado")) throw new AlreadySettled();
    }

    private (string Name, decimal Ceiling) RequireAuthorizer(
        SqliteTransaction transaction, string userId, IReadOnlySet<string>? roles)
    {
        using var read = transaction.Command(
            "SELECT name, role, can_authorize, max_discount_percent FROM users " +
            "WHERE id = $id AND tenant_id = $tenant AND is_active = 1",
            ("$id", userId), ("$tenant", _terminal.TenantId));
        using var reader = read.ExecuteReader();
        if (!reader.Read())
        {
            throw new CommandRefusedException("Quem emitiu o comando não existe ou está inativo neste terminal.");
        }
        var name = reader.GetString(0);
        if (reader.GetInt64(2) == 0)
        {
            throw new CommandRefusedException($"{name} não tem permissão para autorizar esta operação.");
        }
        if (roles is not null && !roles.Contains(reader.GetString(1)))
        {
            throw new CommandRefusedException("Cancelamento de item exige autorização de gerente.");
        }
        // Teto ilegível vira zero, não infinito.
        var ceiling = !reader.IsDBNull(3) &&
                      decimal.TryParse(reader.GetValue(3).ToString(), NumberStyles.Number, CultureInfo.InvariantCulture, out var parsed)
            ? parsed
            : 0m;
        return (name, ceiling);
    }

    private (string Channel, string? CustomerId) RequireOpenOrder(SqliteTransaction transaction, string orderId)
    {
        using var read = transaction.Command(
            "SELECT status, channel, customer_id FROM orders WHERE id = $id AND tenant_id = $tenant",
            ("$id", orderId), ("$tenant", _terminal.TenantId));
        using var reader = read.ExecuteReader();
        if (!reader.Read()) throw new CommandRefusedException("Pedido não encontrado neste terminal.");
        if (reader.GetString(0) != "open")
        {
            throw new CommandRefusedException(
                "O pedido já foi fechado. Venda fechada se corrige por estorno, não pelo painel.");
        }
        return (reader.GetString(1), reader.IsDBNull(2) ? null : reader.GetString(2));
    }

    /// <summary>Onde o item está na cozinha, ou <c>null</c> se nunca foi para lá. Ticket cancelado não conta.</summary>
    private static string? KitchenStatus(SqliteTransaction transaction, string itemId)
    {
        using var read = transaction.Command(
            "SELECT status FROM kds_tickets WHERE order_item_id = $item AND status <> 'canceled' " +
            "ORDER BY created_at DESC LIMIT 1",
            ("$item", itemId));
        return read.ExecuteScalar() as string;
    }

    private static string ConfirmationNote(
        RemoteCommand command, (string Channel, string? CustomerId) order, string product, long totalCents,
        string kitchen, string reason)
    {
        // No salão, customer_id guarda a cópia do rótulo da mesa; no balcão é o
        // cliente do cashback, que não se imprime numa nota.
        var where = order.Channel == "waiter" ? (order.CustomerId ?? "").Trim() : "";
        var state = KitchenLabels.GetValueOrDefault(kitchen, "já enviado à cozinha");
        var place = where.Length > 0 ? $" — {where}" : "";
        var who = command.IssuedByName.Length > 0 ? command.IssuedByName : "O painel";
        return $"{who} pede cancelar {product} (R$ {FormatCents(totalCents)}){place} — {state}. Motivo: {reason}";
    }

    /// <summary>O <c>format_cents</c> do Python: 123456 → "1.234,56".</summary>
    private static string FormatCents(long cents)
    {
        var value = Math.Abs(cents);
        var text = (value / 100).ToString("#,0", CultureInfo.InvariantCulture).Replace(',', '.') + "," + (value % 100).ToString("00");
        return cents < 0 ? "-" + text : text;
    }

    private void Refuse(RemoteCommand command, string message, string severity, IDictionary<string, object?>? extra = null)
    {
        _database.InTransaction(transaction =>
        {
            if (!_inbox.SettleIn(transaction, command.CommandUuid, "refused", message)) throw new AlreadySettled();
            var payload = new Dictionary<string, object?> { ["kind"] = command.Kind, ["reason"] = message };
            foreach (var (key, value) in extra ?? new Dictionary<string, object?>()) payload[key] = value;
            Audit(transaction, command, "remote_command_refused", severity, payload);
        });
        _log($"Comando {command.CommandUuid} recusado: {message}");
    }

    /// <summary>
    /// Auditoria com dupla identidade: o ator é quem emitiu no painel; o
    /// terminal alvo e o canal vão no payload. Sem isso, um cancelamento remoto
    /// seria indistinguível de um feito no balcão.
    /// </summary>
    private void Audit(
        SqliteTransaction transaction, RemoteCommand command, string eventType, string severity,
        Dictionary<string, object?> payload)
    {
        payload["channel"] = Channel;
        payload["command_uuid"] = command.CommandUuid;
        payload["issued_by_name"] = command.IssuedByName;
        payload["issued_at"] = command.IssuedAt;
        payload["target_device_id"] = _terminal.DeviceId;
        _ledger.Append(transaction, eventType, command.IssuedByUserId, payload, severity, command.IssuedByUserId);
    }

    // -- leitura do payload --------------------------------------------------

    /// <summary><c>str(payload.get(key) or "").strip()</c>; vazio é recusa.</summary>
    private static string Text(JsonElement payload, string key, string? message = null)
    {
        var value = payload.ValueKind == JsonValueKind.Object && payload.TryGetProperty(key, out var found)
            ? found.ValueKind switch
            {
                JsonValueKind.String => found.GetString()!.Trim(),
                JsonValueKind.Number when found.GetRawText().Trim('-', '0', '.') is { Length: > 0 } =>
                    Core.Audit.CanonicalJson.Serialize(found),
                JsonValueKind.True => "True",
                // Nulo, falso, zero, objeto ou lista: não é texto que se obedeça.
                _ => "",
            }
            : "";
        return value.Length > 0 ? value : throw new CommandRefusedException(message ?? $"Comando sem `{key}`.");
    }

    private static decimal Percent(JsonElement payload, string key)
    {
        if (payload.ValueKind != JsonValueKind.Object || !payload.TryGetProperty(key, out var value) ||
            CommandProtocol.ReadDecimal(value) is not { } percent)
        {
            throw new CommandRefusedException($"Comando com `{key}` inválido.");
        }
        if (percent <= 0 || percent > 100) throw new CommandRefusedException($"`{key}` fora da faixa (maior que 0, até 100).");
        return percent;
    }

    private sealed class AlreadySettled : Exception;

    private sealed class NeedsConfirmation(string note) : Exception(note)
    {
        public string Note { get; } = note;
    }
}
