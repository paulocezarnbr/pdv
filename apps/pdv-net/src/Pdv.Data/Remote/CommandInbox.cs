using System.Text.Json;
using Microsoft.Data.Sqlite;
using Pdv.Core;
using Pdv.Core.Audit;
using Pdv.Core.Remote;

namespace Pdv.Data.Remote;

/// <summary>O que o terminal responde à nuvem sobre um comando.</summary>
public sealed record CommandResult(string CommandUuid, string Status, string Message, string SettledAt);

/// <summary>Aviso à nuvem de que o comando espera o aceite de alguém no caixa. Não é resultado.</summary>
public sealed record AwaitingNotice(string CommandUuid, string Message, string RequestedAt);

/// <summary>Um comando parado na frente do caixa, com o que quem decide precisa ler.</summary>
public sealed record AwaitingCommand(RemoteCommand Command, string RequestedAt, string Note);

/// <summary>A fila de entrada dos comandos — o <c>InboxRepository</c> do Python, na mesma tabela.</summary>
/// <remarks>
/// <para>
/// É o espelho do outbox, e falha para o lado oposto. No outbox, a dúvida
/// manda reenviar: perder venda é irreversível, e a nuvem deduplica. Aqui, a
/// dúvida manda <b>não aplicar</b>: desconto aplicado duas vezes é dinheiro
/// que ninguém reclama. O status sai de <c>pending</c> na mesma transação do
/// efeito (<see cref="SettleIn"/>).
/// </para>
/// <para>
/// <c>reported_at</c> é separado de <c>settled_at</c>: avisar a nuvem pode
/// falhar, e falhar ao avisar não desfaz nem repete o que foi aplicado.
/// </para>
/// </remarks>
public sealed class CommandInbox(PdvDatabase database, TimeProvider? clock = null)
{
    private readonly TimeProvider _clock = clock ?? TimeProvider.System;

    /// <summary>Grava um comando recebido. Repetido não é erro: é a reentrega funcionando.</summary>
    /// <returns>Se ele é novo.</returns>
    public bool Accept(RemoteCommand command) =>
        database.Execute(
            """
            INSERT OR IGNORE INTO remote_commands
                (command_uuid, tenant_id, store_id, device_id, kind, payload_json, issued_by_user_id,
                 issued_by_name, issued_at, signature, status, received_at)
            VALUES ($uuid, $tenant, $store, $device, $kind, $payload, $by, $name, $at, $signature, 'pending', $now)
            """,
            ("$uuid", command.CommandUuid), ("$tenant", command.TenantId), ("$store", command.StoreId),
            ("$device", command.DeviceId), ("$kind", command.Kind),
            // Como o Python grava (sort_keys, separadores padrão): o texto relido
            // tem os mesmos valores, e a assinatura é refeita sobre eles.
            ("$payload", CanonicalJson.SerializeForOutbox(command.Payload)),
            ("$by", command.IssuedByUserId), ("$name", command.IssuedByName), ("$at", command.IssuedAt),
            ("$signature", command.Signature), ("$now", Iso.Now(_clock))) > 0;

    /// <summary>Ainda não decididos, na ordem em que chegaram.</summary>
    public IReadOnlyList<RemoteCommand> Pending(int limit = 50) =>
        Query($"SELECT {Columns} FROM remote_commands WHERE status = 'pending' ORDER BY received_at, rowid LIMIT $limit",
            ("$limit", limit));

    public RemoteCommand? GetPending(string commandUuid) =>
        Query($"SELECT {Columns} FROM remote_commands WHERE command_uuid = $uuid AND status = 'pending'",
            ("$uuid", commandUuid)).FirstOrDefault();

    public long PendingCount() => Convert.ToInt64(database.Scalar("SELECT COUNT(*) FROM remote_commands WHERE status = 'pending'"));

    public long AwaitingCount() => Convert.ToInt64(database.Scalar(
        "SELECT COUNT(*) FROM remote_commands WHERE status = 'pending' AND confirmation_requested_at IS NOT NULL"));

    public bool IsAwaiting(string commandUuid) => database.Scalar(
        "SELECT 1 FROM remote_commands WHERE command_uuid = $uuid AND status = 'pending' " +
        "AND confirmation_requested_at IS NOT NULL", ("$uuid", commandUuid)) is not null;

    public string? StatusOf(string commandUuid) =>
        database.Scalar("SELECT status FROM remote_commands WHERE command_uuid = $uuid", ("$uuid", commandUuid)) as string;

    /// <summary>O que espera o aceite de alguém no caixa, mais antigo antes.</summary>
    public IReadOnlyList<AwaitingCommand> AwaitingConfirmation(int limit = 50)
    {
        using var command = Sql.Command(
            database.Connection, null,
            $"SELECT {Columns}, confirmation_requested_at, COALESCE(confirmation_note, '') FROM remote_commands " +
            "WHERE status = 'pending' AND confirmation_requested_at IS NOT NULL " +
            "ORDER BY confirmation_requested_at, rowid LIMIT $limit",
            ("$limit", limit));
        using var reader = command.ExecuteReader();
        var awaiting = new List<AwaitingCommand>();
        while (reader.Read()) awaiting.Add(new AwaitingCommand(Read(reader), reader.GetString(10), reader.GetString(11)));
        return awaiting;
    }

    /// <summary>Resultados decididos que a nuvem ainda não confirmou.</summary>
    public IReadOnlyList<CommandResult> Unreported(int limit = 50)
    {
        using var command = Sql.Command(
            database.Connection, null,
            "SELECT command_uuid, status, COALESCE(result_message, ''), COALESCE(settled_at, '') FROM remote_commands " +
            "WHERE status <> 'pending' AND reported_at IS NULL ORDER BY settled_at LIMIT $limit",
            ("$limit", limit));
        using var reader = command.ExecuteReader();
        var results = new List<CommandResult>();
        while (reader.Read()) results.Add(new CommandResult(reader.GetString(0), reader.GetString(1), reader.GetString(2), reader.GetString(3)));
        return results;
    }

    /// <summary>
    /// Esperas que a nuvem ainda não conhece. Só de comando ainda pendente: avisar
    /// que um comando decidido "espera" faria o painel voltar no tempo.
    /// </summary>
    public IReadOnlyList<AwaitingNotice> UnreportedAwaiting(int limit = 50)
    {
        using var command = Sql.Command(
            database.Connection, null,
            "SELECT command_uuid, COALESCE(confirmation_note, ''), confirmation_requested_at FROM remote_commands " +
            "WHERE status = 'pending' AND confirmation_requested_at IS NOT NULL AND confirmation_reported_at IS NULL " +
            "ORDER BY confirmation_requested_at LIMIT $limit",
            ("$limit", limit));
        using var reader = command.ExecuteReader();
        var notices = new List<AwaitingNotice>();
        while (reader.Read()) notices.Add(new AwaitingNotice(reader.GetString(0), reader.GetString(1), reader.GetString(2)));
        return notices;
    }

    /// <summary>
    /// Fecha o comando <b>dentro da transação que aplicou o efeito</b>. A
    /// cláusula <c>status = 'pending'</c> é a trava final contra aplicação
    /// dupla: numa corrida, a segunda execução atualiza zero linhas.
    /// </summary>
    public bool SettleIn(SqliteTransaction transaction, string commandUuid, string status, string message)
    {
        using var update = transaction.Command(
            "UPDATE remote_commands SET status = $status, result_message = $message, settled_at = $now " +
            "WHERE command_uuid = $uuid AND status = 'pending'",
            ("$status", status), ("$message", message), ("$now", Iso.Now(_clock)), ("$uuid", commandUuid));
        return update.ExecuteNonQuery() > 0;
    }

    /// <summary>
    /// Põe o comando para esperar o caixa. A data da primeira espera não é
    /// reescrita (é ela que diz há quanto tempo está parado); a nota, sim — o
    /// prato pode ter ficado pronto desde o último ciclo.
    /// </summary>
    /// <returns>Se a espera é nova.</returns>
    public bool RequestConfirmation(string commandUuid, string note) =>
        database.InTransaction(transaction =>
        {
            object? before;
            using (var read = transaction.Command(
                       "SELECT COALESCE(confirmation_requested_at, '') FROM remote_commands " +
                       "WHERE command_uuid = $uuid AND status = 'pending'", ("$uuid", commandUuid)))
            {
                before = read.ExecuteScalar();
            }
            if (before is null) return false;
            using (var update = transaction.Command(
                       "UPDATE remote_commands SET confirmation_requested_at = COALESCE(confirmation_requested_at, $now), " +
                       "confirmation_note = $note WHERE command_uuid = $uuid AND status = 'pending'",
                       ("$now", Iso.Now(_clock)), ("$note", note), ("$uuid", commandUuid)))
            {
                update.ExecuteNonQuery();
            }
            return (string)before == "";
        });

    /// <summary>Marca os resultados que a nuvem <b>nomeou</b> como recebidos.</summary>
    public void MarkReported(IEnumerable<string> commandUuids) => Mark("reported_at", commandUuids);

    public void MarkAwaitingReported(IEnumerable<string> commandUuids) => Mark("confirmation_reported_at", commandUuids);

    private void Mark(string column, IEnumerable<string> commandUuids)
    {
        var uuids = commandUuids.ToList();
        if (uuids.Count == 0) return;
        var now = Iso.Now(_clock);
        database.InTransaction(transaction =>
        {
            foreach (var uuid in uuids)
            {
                using var update = transaction.Command(
                    $"UPDATE remote_commands SET {column} = $now WHERE command_uuid = $uuid AND {column} IS NULL",
                    ("$now", now), ("$uuid", uuid));
                update.ExecuteNonQuery();
            }
        });
    }

    private const string Columns =
        "command_uuid, tenant_id, store_id, device_id, kind, payload_json, issued_by_user_id, issued_by_name, issued_at, signature";

    private List<RemoteCommand> Query(string sql, params (string Name, object? Value)[] parameters)
    {
        using var command = Sql.Command(database.Connection, null, sql, parameters);
        using var reader = command.ExecuteReader();
        var commands = new List<RemoteCommand>();
        while (reader.Read()) commands.Add(Read(reader));
        return commands;
    }

    private static RemoteCommand Read(SqliteDataReader reader)
    {
        JsonElement payload;
        try
        {
            payload = JsonDocument.Parse(reader.GetString(5)).RootElement.Clone();
        }
        catch (JsonException)
        {
            // Payload ilegível no banco: segue como objeto vazio, e a
            // assinatura (feita sobre o original) não confere — recusa.
            payload = JsonDocument.Parse("{}").RootElement.Clone();
        }
        return new RemoteCommand(
            reader.GetString(0), reader.GetString(1), reader.GetString(2), reader.GetString(3), reader.GetString(4),
            payload, reader.GetString(6), reader.GetString(7), reader.GetString(8), reader.GetString(9));
    }
}
