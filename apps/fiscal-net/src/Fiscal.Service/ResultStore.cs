using System.Text.Json;
using Microsoft.Data.Sqlite;

namespace Fiscal.Service;

/// <summary>
/// Um pedido reivindicado e ainda não concluído. Com <see cref="SignedXml"/>, a
/// nota pode ter chegado à SEFAZ; sem ele, nada foi transmitido.
/// </summary>
public sealed record PendingRequest(
    string RequestUuid,
    string DocumentId,
    string? AccessKey,
    string? SignedXml,
    string? ContextJson,
    bool Stale);

/// <summary>
/// A segunda trava idempotente, independente do Postgres da retaguarda — o
/// mesmo arquivo e a mesma tabela do serviço em Python.
/// </summary>
/// <remarks>
/// <para>
/// O <see cref="Claim"/> é a única porta do motor: duas chamadas com o mesmo
/// <c>request_uuid</c> — a original atrasada na rede e a retransmissão — disputam
/// a mesma linha, e só uma transmite.
/// </para>
/// <para>
/// O XML assinado e a chave são gravados <b>antes</b> de transmitir
/// (<see cref="Prepare"/>). É o que permite, depois de uma queda ou de uma
/// resposta perdida, perguntar à SEFAZ por aquela chave em vez de adivinhar — e
/// retransmitir o <b>mesmo</b> XML, que a SEFAZ reconhece como duplicado se já o
/// tiver recebido.
/// </para>
/// <para>
/// Um pedido reivindicado sem XML assinado há mais de <see cref="StaleAfter"/>
/// morreu antes de transmitir: é seguro retomá-lo. Com uma réplica só do
/// serviço — o SQLite num volume não é compartilhado entre réplicas.
/// </para>
/// </remarks>
public sealed class ResultStore
{
    public static readonly TimeSpan StaleAfter = TimeSpan.FromMinutes(2);

    private readonly string _connectionString;
    private readonly Lock _lock = new();

    public ResultStore(string path)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
        _connectionString = new SqliteConnectionStringBuilder
        {
            DataSource = path,
            Mode = SqliteOpenMode.ReadWriteCreate,
            DefaultTimeout = 10,
            Pooling = false,
        }.ToString();

        using var db = Open();
        Execute(db, """
            CREATE TABLE IF NOT EXISTS fiscal_results (
                request_uuid TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('processing','settled')),
                result_json TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """);
        // Colunas novas, acrescentadas a um banco criado pelo serviço em Python.
        var columns = new HashSet<string>(StringComparer.Ordinal);
        using (var info = db.CreateCommand())
        {
            info.CommandText = "PRAGMA table_info(fiscal_results)";
            using var reader = info.ExecuteReader();
            while (reader.Read()) columns.Add(reader.GetString(1));
        }
        foreach (var column in new[] { "access_key", "signed_xml", "context_json" })
        {
            if (!columns.Contains(column)) Execute(db, $"ALTER TABLE fiscal_results ADD COLUMN {column} TEXT");
        }
    }

    private SqliteConnection Open()
    {
        var connection = new SqliteConnection(_connectionString);
        connection.Open();
        return connection;
    }

    private static int Execute(SqliteConnection db, string sql, params (string Name, object? Value)[] parameters)
    {
        using var command = db.CreateCommand();
        command.CommandText = sql;
        foreach (var (name, value) in parameters) command.Parameters.AddWithValue(name, value ?? DBNull.Value);
        return command.ExecuteNonQuery();
    }

    /// <returns><c>true</c> se esta chamada reivindicou o pedido.</returns>
    public bool Claim(string requestUuid, string documentId)
    {
        lock (_lock)
        {
            using var db = Open();
            return Execute(db,
                "INSERT OR IGNORE INTO fiscal_results(request_uuid, document_id, state) VALUES ($uuid, $doc, 'processing')",
                ("$uuid", requestUuid), ("$doc", documentId)) > 0;
        }
    }

    /// <summary>
    /// Retoma um pedido que morreu antes de transmitir. Atômico: de duas
    /// retransmissões simultâneas, uma retoma e a outra recebe <c>false</c>.
    /// </summary>
    public bool TakeOver(string requestUuid)
    {
        lock (_lock)
        {
            using var db = Open();
            return Execute(db,
                "UPDATE fiscal_results SET updated_at = CURRENT_TIMESTAMP " +
                "WHERE request_uuid = $uuid AND state = 'processing' AND signed_xml IS NULL " +
                $"AND updated_at < datetime('now', '-{(int)StaleAfter.TotalSeconds} seconds')",
                ("$uuid", requestUuid)) > 0;
        }
    }

    /// <summary>Grava a chave e o XML assinado antes de transmitir.</summary>
    public void Prepare(string requestUuid, string accessKey, string signedXml, string contextJson)
    {
        lock (_lock)
        {
            using var db = Open();
            Execute(db,
                "UPDATE fiscal_results SET access_key = $key, signed_xml = $xml, context_json = $ctx, " +
                "updated_at = CURRENT_TIMESTAMP WHERE request_uuid = $uuid AND state = 'processing'",
                ("$key", accessKey), ("$xml", signedXml), ("$ctx", contextJson), ("$uuid", requestUuid));
        }
    }

    /// <summary>Solta um pedido que não chegou a transmitir: a retaguarda pode retransmitir.</summary>
    public void Release(string requestUuid)
    {
        lock (_lock)
        {
            using var db = Open();
            Execute(db,
                "DELETE FROM fiscal_results WHERE request_uuid = $uuid AND state = 'processing' AND signed_xml IS NULL",
                ("$uuid", requestUuid));
        }
    }

    /// <summary>
    /// Descarta um pedido assinado que a SEFAZ com certeza não processou
    /// (certificado barrado na porta). Só o motor decide isso, nunca a rede.
    /// </summary>
    public void Discard(string requestUuid)
    {
        lock (_lock)
        {
            using var db = Open();
            Execute(db, "DELETE FROM fiscal_results WHERE request_uuid = $uuid AND state = 'processing'", ("$uuid", requestUuid));
        }
    }

    /// <summary>
    /// Conclui o pedido. Um resultado <c>unknown</c> gravado pelo serviço anterior
    /// pode ser substituído pelo veredito da SEFAZ; um veredito, nunca.
    /// </summary>
    public void Settle(string requestUuid, FiscalResult result)
    {
        lock (_lock)
        {
            using var db = Open();
            var current = ReadResult(db, requestUuid);
            if (current is not null && current.Status != FiscalResult.Unknown) return;
            Execute(db,
                "UPDATE fiscal_results SET state = 'settled', result_json = $json, updated_at = CURRENT_TIMESTAMP " +
                "WHERE request_uuid = $uuid",
                ("$json", JsonSerializer.Serialize(result, Json.Options)), ("$uuid", requestUuid));
        }
    }

    /// <summary>A linha existe, concluída ou não?</summary>
    public bool Known(string requestUuid)
    {
        using var db = Open();
        using var command = db.CreateCommand();
        command.CommandText = "SELECT 1 FROM fiscal_results WHERE request_uuid = $uuid";
        command.Parameters.AddWithValue("$uuid", requestUuid);
        return command.ExecuteScalar() is not null;
    }

    /// <summary>O resultado concluído, ou <c>null</c>.</summary>
    public FiscalResult? Get(string requestUuid)
    {
        using var db = Open();
        return ReadResult(db, requestUuid);
    }

    private static FiscalResult? ReadResult(SqliteConnection db, string requestUuid)
    {
        using var command = db.CreateCommand();
        command.CommandText = "SELECT state, result_json FROM fiscal_results WHERE request_uuid = $uuid";
        command.Parameters.AddWithValue("$uuid", requestUuid);
        using var reader = command.ExecuteReader();
        if (!reader.Read() || reader.GetString(0) != "settled" || reader.IsDBNull(1)) return null;
        return JsonSerializer.Deserialize<FiscalResult>(reader.GetString(1), Json.Options);
    }

    /// <summary>
    /// O que dá para reconciliar: pedido em andamento, ou concluído como
    /// <c>unknown</c> depois de assinado.
    /// </summary>
    public PendingRequest? Pending(string requestUuid)
    {
        using var db = Open();
        using var command = db.CreateCommand();
        command.CommandText =
            "SELECT document_id, access_key, signed_xml, context_json, state, result_json, " +
            $"updated_at < datetime('now', '-{(int)StaleAfter.TotalSeconds} seconds') " +
            "FROM fiscal_results WHERE request_uuid = $uuid";
        command.Parameters.AddWithValue("$uuid", requestUuid);
        using var reader = command.ExecuteReader();
        if (!reader.Read()) return null;
        string? Text(int index) => reader.IsDBNull(index) ? null : reader.GetString(index);
        if (reader.GetString(4) == "settled")
        {
            var result = JsonSerializer.Deserialize<FiscalResult>(Text(5) ?? "null", Json.Options);
            if (result?.Status != FiscalResult.Unknown || Text(2) is null) return null;
        }
        return new PendingRequest(requestUuid, reader.GetString(0), Text(1), Text(2), Text(3), reader.GetInt64(6) == 1);
    }

    /// <summary>Para os testes: envelhece um pedido como se o processo tivesse caído há tempo.</summary>
    internal void Age(string requestUuid, TimeSpan by)
    {
        using var db = Open();
        Execute(db,
            $"UPDATE fiscal_results SET updated_at = datetime(updated_at, '-{(int)by.TotalSeconds} seconds') WHERE request_uuid = $uuid",
            ("$uuid", requestUuid));
    }
}
