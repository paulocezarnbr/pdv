using Microsoft.Data.Sqlite;

namespace Pdv.Data.Customers;

/// <summary>
/// O cliente que desce da nuvem para este caixa: o cadastro é do
/// estabelecimento, e cada caixa da loja guarda uma cópia.
/// </summary>
/// <remarks>
/// <para>
/// <b>Vence a mudança mais nova.</b> Uma correção feita aqui e ainda não
/// enviada não é desfeita por uma versão mais velha que desce; quando ela subir,
/// a nuvem aplica a mesma regra.
/// </para>
/// <para>
/// <b>WhatsApp e CPF continuam únicos no caixa.</b> Dois cadastros diferentes
/// da mesma pessoa (um feito antes do id derivado, ou um por WhatsApp e outro
/// só por CPF) podem chegar com o mesmo número. O de menor id fica com ele, e o
/// outro perde só esse campo, aqui. É a mesma decisão em todos os caixas, em
/// qualquer ordem de chegada — por isso a regra olha o id, e não quem chegou
/// primeiro. Juntar os dois cadastros é trabalho do painel, com alguém olhando.
/// </para>
/// </remarks>
public static class CustomerPull
{
    private static readonly string[] UniqueFields = ["phone", "cpf"];

    private static readonly string[] Clearable =
        ["phone", "email", "cpf", "unit_block", "unit_number", "birth_date", "marketing_opt_in_at"];

    /// <summary>Aplica uma linha já mapeada. Devolve falso quando a cópia daqui é mais nova.</summary>
    public static bool Apply(SqliteTransaction transaction, IReadOnlyDictionary<string, object> mapped, Action<string>? log = null)
    {
        var row = new Dictionary<string, object?>(mapped.ToDictionary(p => p.Key, p => (object?)p.Value), StringComparer.Ordinal);
        var id = (string)row["id"]!;
        var tenant = (string)row["tenant_id"]!;
        // O cadastro desce inteiro: e-mail apagado na nuvem some aqui também.
        // (Nos outros cadastros, nulo que desce é "sem valor" e fica o do caixa.)
        foreach (var field in Clearable) row.TryAdd(field, null);

        if (transaction.Command("SELECT updated_at FROM customers WHERE id = $id", ("$id", id)).ExecuteScalar() is string local &&
            Moment(local) is { } here && Moment((string)row["updated_at"]!) is { } there && here > there)
        {
            return false;
        }

        foreach (var field in UniqueFields)
        {
            if (!row.TryGetValue(field, out var value) || value is not string text) continue;
            if (transaction.Command(
                    $"SELECT id FROM customers WHERE tenant_id = $tenant AND {field} = $value AND id <> $id",
                    ("$tenant", tenant), ("$value", text), ("$id", id)).ExecuteScalar() is not string other)
            {
                continue;
            }
            if (string.CompareOrdinal(id, other) < 0)
            {
                // Este fica com o número; o outro cadastro perde só o campo, só aqui.
                transaction.Command($"UPDATE customers SET {field} = NULL WHERE id = $other", ("$other", other)).ExecuteNonQuery();
            }
            else
            {
                row[field] = null;
            }
            log?.Invoke($"Cliente {id} e {other} com o mesmo {field}: fica com o de menor id; juntar é no painel.");
        }

        // O que desceu da nuvem já está lá.
        row["is_synced"] = 1;
        var columns = row.Keys.OrderBy(name => name, StringComparer.Ordinal).ToList();
        var updates = string.Join(", ", columns.Where(c => c != "id").Select(c => $"{c} = excluded.{c}"));
        transaction.Command(
            $"INSERT INTO customers ({string.Join(", ", columns)}) " +
            $"VALUES ({string.Join(", ", columns.Select((_, i) => $"$p{i}"))}) " +
            $"ON CONFLICT (id) DO UPDATE SET {updates}",
            [.. columns.Select((c, i) => ($"$p{i}", row[c]))]).ExecuteNonQuery();
        return true;
    }

    private static DateTimeOffset? Moment(string text) =>
        DateTimeOffset.TryParse(text, System.Globalization.CultureInfo.InvariantCulture,
            System.Globalization.DateTimeStyles.AssumeUniversal, out var moment) ? moment : null;
}
