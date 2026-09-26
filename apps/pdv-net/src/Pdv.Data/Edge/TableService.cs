using System.Text.Json.Nodes;
using Microsoft.Data.Sqlite;
using Pdv.Core;
using Pdv.Data.Sales;

namespace Pdv.Data.Edge;

/// <summary>Problema com a mesa: rótulo repetido, mesa inexistente ou ocupada.</summary>
public sealed class TableException(string message) : Exception(message);

/// <summary>Uma mesa do mapa, com a ocupação (a comanda aberta nela, se houver).</summary>
public sealed record StoreTable(
    string Id,
    string Label,
    string Area,
    int Seats,
    int SortOrder,
    bool IsActive,
    string? OrderId = null,
    long? LocalNumber = null,
    long TotalCents = 0,
    int ItemCount = 0,
    string? OpenedAt = null,
    string? BillRequestedAt = null)
{
    public bool Occupied => OrderId is not null;

    /// <summary>O estado que o app pinta na mesa.</summary>
    public string Status => !Occupied ? "free" : BillRequestedAt is not null ? "billing" : "busy";

    /// <summary>As chaves e a ordem do <c>to_json</c> do Python: o app do garçom lê isto.</summary>
    public JsonObject ToJson() => new()
    {
        ["id"] = Id,
        ["label"] = Label,
        ["area"] = Area,
        ["seats"] = Seats,
        ["sort_order"] = SortOrder,
        ["is_active"] = IsActive,
        ["status"] = Status,
        ["order_id"] = OrderId,
        ["local_number"] = LocalNumber,
        ["total_cents"] = TotalCents,
        ["item_count"] = ItemCount,
        ["opened_at"] = OpenedAt,
        ["bill_requested_at"] = BillRequestedAt,
    };
}

/// <summary>
/// O mapa de mesas do salão — o <c>edge/tables.py</c> do Python, recusa a recusa.
/// </summary>
/// <remarks>
/// <para>
/// A mesa é entidade (não texto no pedido): dois garçons não abrem a "Mesa 5"
/// duas vezes, e "mesa 5", "Mesa 5" e "M5" não viram três mesas. O pedido
/// guarda a cópia do rótulo — renomear hoje não reescreve o cupom de ontem.
/// </para>
/// <para>
/// Desativar, nunca apagar: comandas antigas apontam para a mesa. Mesa ocupada
/// não sai do mapa, senão a comanda aberta ficaria sem caminho até ela.
/// </para>
/// <para>Conferido contra <c>contracts/salon.json</c>.</para>
/// </remarks>
public sealed class TableService(PdvDatabase database, TerminalIdentity terminal, TimeProvider? clock = null)
{
    /// <summary>Teto contra um laço defeituoso do app criar dez mil mesas, não regra de negócio.</summary>
    public const int MaxTables = 300;

    private readonly TimeProvider _clock = clock ?? TimeProvider.System;
    private readonly Outbox _outbox = new(clock);

    /// <summary>O mapa numa consulta só: o app atualiza com o celular no bolso, no Wi-Fi da loja.</summary>
    public IReadOnlyList<StoreTable> List(bool includeInactive = false)
    {
        using var command = Sql.Command(database.Connection, null,
            """
            SELECT t.id, t.label, t.area, t.seats, t.sort_order, t.is_active,
                   o.id AS order_id, o.local_number, o.total_cents, o.opened_at, o.bill_requested_at,
                   (SELECT COUNT(*) FROM order_items i
                     WHERE i.order_id = o.id AND i.canceled_at IS NULL) AS items
              FROM store_tables t
              LEFT JOIN orders o
                     ON o.table_id = t.id
                    AND o.status = 'open'
                    AND o.tenant_id = t.tenant_id
             WHERE t.tenant_id = $tenant AND t.store_id = $store
               AND (t.is_active = 1 OR $all)
             ORDER BY t.sort_order, t.label
            """,
            ("$tenant", terminal.TenantId), ("$store", terminal.StoreId), ("$all", includeInactive ? 1 : 0));
        using var reader = command.ExecuteReader();
        var tables = new List<StoreTable>();
        while (reader.Read())
        {
            var occupied = !reader.IsDBNull(6);
            tables.Add(new StoreTable(
                reader.GetString(0), reader.GetString(1), reader.GetString(2), reader.GetInt32(3), reader.GetInt32(4),
                reader.GetInt64(5) != 0,
                occupied ? reader.GetString(6) : null,
                occupied ? reader.GetInt64(7) : null,
                occupied && !reader.IsDBNull(8) ? reader.GetInt64(8) : 0,
                occupied ? Convert.ToInt32(reader.GetInt64(11)) : 0,
                occupied ? reader.GetString(9) : null,
                occupied && !reader.IsDBNull(10) ? reader.GetString(10) : null));
        }
        return tables;
    }

    public StoreTable Get(string tableId) =>
        List(includeInactive: true).FirstOrDefault(table => table.Id == tableId)
        ?? throw new TableException("Mesa não encontrada.");

    public StoreTable? FindByLabel(string label)
    {
        var wanted = label.Trim().ToLowerInvariant();
        return List().FirstOrDefault(table => table.Label.ToLowerInvariant() == wanted);
    }

    public StoreTable Create(string label, string area = "Salão", int seats = 4, int? sortOrder = null)
    {
        label = Clean(label, "O rótulo da mesa não pode ficar em branco.", 32);
        area = Clean(area, "A área não pode ficar em branco.", 32);
        seats = Math.Clamp(seats, 1, 99);

        var active = List();
        if (active.Count >= MaxTables) throw new TableException($"Limite de {MaxTables} mesas por loja atingido.");
        sortOrder ??= active.Count == 0 ? 1 : active.Max(table => table.SortOrder) + 1;

        var tableId = Iso.NewId();
        var clientUuid = Iso.NewId();
        var now = Iso.Now(_clock);
        try
        {
            database.InTransaction(transaction =>
            {
                transaction.Command(
                    """
                    INSERT INTO store_tables
                        (id, tenant_id, store_id, label, area, seats, sort_order, is_active, created_at, updated_at, client_uuid)
                    VALUES ($id, $tenant, $store, $label, $area, $seats, $sort, 1, $now, $now, $uuid)
                    """,
                    ("$id", tableId), ("$tenant", terminal.TenantId), ("$store", terminal.StoreId),
                    ("$label", label), ("$area", area), ("$seats", seats), ("$sort", sortOrder),
                    ("$now", now), ("$uuid", clientUuid)).ExecuteNonQuery();
                Enqueue(transaction, tableId, clientUuid, "insert", new Dictionary<string, object?>
                {
                    ["label"] = label, ["area"] = area, ["seats"] = seats, ["sort_order"] = sortOrder, ["is_active"] = true,
                });
            });
        }
        catch (SqliteException error) when (error.SqliteErrorCode == 19)
        {
            throw new TableException($"Já existe uma mesa chamada {PyRepr(label)}.");
        }
        return Get(tableId);
    }

    /// <summary>Edita o cadastro. Renomear mesa ocupada pode: a comanda guarda a própria cópia do rótulo.</summary>
    public StoreTable Update(string tableId, string? label = null, string? area = null, int? seats = null, int? sortOrder = null)
    {
        var current = Get(tableId);
        if (!current.IsActive) throw new TableException("Mesa desativada. Reative antes de editar.");

        // Na ordem do dicionário do Python: é a ordem das colunas no UPDATE e das chaves no outbox.
        var fields = new Dictionary<string, object?>();
        if (label is not null) fields["label"] = Clean(label, "O rótulo não pode ficar em branco.", 32);
        if (area is not null) fields["area"] = Clean(area, "A área não pode ficar em branco.", 32);
        if (seats is not null) fields["seats"] = Math.Clamp(seats.Value, 1, 99);
        if (sortOrder is not null) fields["sort_order"] = sortOrder.Value;
        if (fields.Count == 0) return current;

        var assignments = string.Join(", ", fields.Keys.Select(name => $"{name} = ${name}"));
        try
        {
            database.InTransaction(transaction =>
            {
                transaction.Command(
                    $"UPDATE store_tables SET {assignments}, updated_at = $now, is_synced = 0 WHERE id = $id AND tenant_id = $tenant",
                    [.. fields.Select(field => ("$" + field.Key, field.Value)),
                     ("$now", Iso.Now(_clock)), ("$id", tableId), ("$tenant", terminal.TenantId)]).ExecuteNonQuery();
                Enqueue(transaction, tableId, Iso.NewId(), "update", fields);
            });
        }
        catch (SqliteException error) when (error.SqliteErrorCode == 19)
        {
            // O Python mostra o rótulo como veio, não o limpo.
            throw new TableException($"Já existe uma mesa chamada {PyRepr(label)}.");
        }
        return Get(tableId);
    }

    /// <summary>Tira a mesa do mapa, ou devolve. Mesa ocupada não sai.</summary>
    public StoreTable SetActive(string tableId, bool active)
    {
        var table = Get(tableId);
        if (!active && table.Occupied)
        {
            throw new TableException(
                $"A {table.Label} tem a comanda {table.LocalNumber} aberta. " +
                "Receba ou cancele a conta antes de tirar a mesa do mapa.");
        }
        if (table.IsActive == active) return table;

        try
        {
            database.InTransaction(transaction =>
            {
                transaction.Command(
                    "UPDATE store_tables SET is_active = $active, updated_at = $now, is_synced = 0 WHERE id = $id AND tenant_id = $tenant",
                    ("$active", active ? 1 : 0), ("$now", Iso.Now(_clock)), ("$id", tableId),
                    ("$tenant", terminal.TenantId)).ExecuteNonQuery();
                Enqueue(transaction, tableId, Iso.NewId(), "update", new Dictionary<string, object?> { ["is_active"] = active });
            });
        }
        catch (SqliteException error) when (error.SqliteErrorCode == 19)
        {
            // Reativar esbarra no índice único: o rótulo foi reaproveitado enquanto ela estava fora.
            throw new TableException(
                $"Já existe outra mesa ativa chamada {PyRepr(table.Label)}. Renomeie uma das duas antes de reativar.");
        }
        return Get(tableId);
    }

    /// <summary>"Mesa 1".."Mesa N", pulando as que já existem: o primeiro dia não começa com a tela vazia.</summary>
    public int SeedDefaultTables(int count = 12, string area = "Salão")
    {
        var created = 0;
        for (var number = 1; number <= Math.Max(0, count); number++)
        {
            var label = $"Mesa {number}";
            if (FindByLabel(label) is not null) continue;
            Create(label, area, 4, number);
            created++;
        }
        return created;
    }

    private void Enqueue(SqliteTransaction transaction, string tableId, string clientUuid, string operation, Dictionary<string, object?> payload)
    {
        var body = new Dictionary<string, object?> { ["id"] = tableId };
        foreach (var (key, value) in payload) body[key] = value;
        // `updated_at` decide, na nuvem, qual de duas mudanças é a mais nova.
        body["updated_at"] = Iso.Now(_clock);
        _outbox.Enqueue(transaction, "store_tables", tableId, clientUuid, operation, body);
    }

    /// <summary>O <c>" ".join(value.split())[:limit]</c> do Python.</summary>
    internal static string Clean(string value, string message, int limit)
    {
        var cleaned = string.Join(' ', value.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
        cleaned = new string(cleaned.EnumerateRunes().Take(limit).SelectMany(rune => rune.ToString()).ToArray());
        return cleaned.Length > 0 ? cleaned : throw new TableException(message);
    }

    /// <summary>O <c>repr()</c> de texto do Python, que as mensagens de erro usam.</summary>
    internal static string PyRepr(string? value)
    {
        if (value is null) return "None";
        var quote = value.Contains('\'') && !value.Contains('"') ? '"' : '\'';
        var escaped = value.Replace("\\", "\\\\");
        if (quote == '\'') escaped = escaped.Replace("'", "\\'");
        return $"{quote}{escaped}{quote}";
    }
}
