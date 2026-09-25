using Microsoft.Data.Sqlite;

namespace Pdv.Data.Provisioning;

public sealed class StagedActivationException(string message) : Exception(message);

/// <summary>
/// Ativação a partir do modo demonstração: banco novo, demonstração arquivada —
/// os mesmos arquivos e a mesma troca do <c>pdv.provisioning.staging</c> do Python.
/// </summary>
/// <remarks>
/// <para>
/// O banco de demonstração tem vendas de teste na fila de saída, sob o tenant
/// de demonstração. Ativar no mesmo banco as mandaria para a loja real no
/// primeiro ciclo; apagá-las contraria a regra de que nada some. Então a
/// ativação grava em <c>pdv_local.ativacao.db</c>, e na próxima abertura, antes
/// de qualquer conexão, a demonstração vira <c>pdv_demo-AAAAMMDD-HHMMSS.db</c>
/// e o banco novo assume o lugar.
/// </para>
/// <para>
/// Até a fase C7 as migrations são do Python. O banco novo é criado com o
/// schema do banco aberto — que o <see cref="PdvDatabase"/> já garantiu estar
/// na versão suportada —, sem nenhuma linha: é o que o <c>migrate()</c> do
/// Python produz num arquivo vazio.
/// </para>
/// </remarks>
public static class StagedActivation
{
    private static readonly string[] Companions = ["-wal", "-shm"];

    public static string StagedPath(string databasePath) =>
        Path.Combine(
            Path.GetDirectoryName(databasePath) ?? "",
            $"{Path.GetFileNameWithoutExtension(databasePath)}.ativacao{Path.GetExtension(databasePath)}");

    /// <summary>Hora local, como o <c>datetime.now()</c> do Python.</summary>
    public static string ArchivePath(string databasePath, DateTime now) =>
        Path.Combine(
            Path.GetDirectoryName(databasePath) ?? "",
            $"pdv_demo-{now:yyyyMMdd-HHmmss}{Path.GetExtension(databasePath)}");

    /// <summary>
    /// Cria o banco da loja ao lado do atual, com o mesmo schema e vazio, e o
    /// abre. A sobra de uma tentativa anterior é descartada.
    /// </summary>
    public static PdvDatabase CreateStaged(PdvDatabase current)
    {
        var staged = StagedPath(current.Path);
        Discard(staged);

        var objects = new List<(string Type, string Sql)>();
        using (var command = current.Connection.CreateCommand())
        {
            // Tabelas antes de índices e gatilhos, na ordem em que foram
            // criadas; os internos do SQLite (sqlite_*) ele mesmo recria.
            command.CommandText =
                "SELECT type, sql FROM sqlite_master " +
                "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' " +
                "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 WHEN 'view' THEN 2 ELSE 3 END, rowid";
            using var reader = command.ExecuteReader();
            while (reader.Read()) objects.Add((reader.GetString(0), reader.GetString(1)));
        }

        var builder = new SqliteConnectionStringBuilder
        {
            DataSource = staged,
            Mode = SqliteOpenMode.ReadWriteCreate,
            Pooling = false,
        };
        using (var connection = new SqliteConnection(builder.ToString()))
        {
            connection.Open();
            using var transaction = connection.BeginTransaction();
            foreach (var (_, sql) in objects)
            {
                using var command = connection.CreateCommand();
                command.Transaction = transaction;
                command.CommandText = sql;
                command.ExecuteNonQuery();
            }
            using (var version = connection.CreateCommand())
            {
                version.Transaction = transaction;
                version.CommandText = $"PRAGMA user_version = {current.SchemaVersion}";
                version.ExecuteNonQuery();
            }
            transaction.Commit();
        }
        return new PdvDatabase(staged);
    }

    /// <summary>A ativação não se concluiu: o banco novo não pode ser promovido na próxima abertura.</summary>
    public static void Discard(string stagedPath)
    {
        foreach (var candidate in new[] { stagedPath }.Concat(Companions.Select(suffix => stagedPath + suffix)))
        {
            File.Delete(candidate);
        }
    }

    /// <summary>
    /// Troca a demonstração pelo banco da loja, se houver ativação pendente.
    /// </summary>
    /// <returns>O arquivo da demonstração arquivada, ou <c>null</c> se não havia troca.</returns>
    /// <remarks>
    /// Espera o processo anterior soltar o arquivo: o caixa reinicia sozinho
    /// depois de ativar, e no Windows o processo antigo pode ainda estar
    /// fechando. O banco é movido primeiro: se ele está travado, nada foi
    /// movido, e a próxima tentativa começa do mesmo estado.
    /// </remarks>
    /// <exception cref="StagedActivationException">O arquivo continuou travado.</exception>
    public static string? Promote(string databasePath, DateTime? now = null, TimeSpan? wait = null)
    {
        var staged = StagedPath(databasePath);
        if (!File.Exists(staged)) return null;

        var archive = ArchivePath(databasePath, now ?? DateTime.Now);
        var deadline = DateTime.UtcNow + (wait ?? TimeSpan.FromSeconds(15));
        while (true)
        {
            try
            {
                if (File.Exists(databasePath))
                {
                    File.Move(databasePath, archive, overwrite: true);
                    MoveCompanions(databasePath, archive);
                }
                File.Move(staged, databasePath, overwrite: true);
                MoveCompanions(staged, databasePath);
                return File.Exists(archive) ? archive : null;
            }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException)
            {
                if (DateTime.UtcNow >= deadline)
                {
                    throw new StagedActivationException(
                        "O PDV anterior ainda está usando o banco de dados. Feche todas as janelas do PDV " +
                        "e abra de novo.");
                }
                Thread.Sleep(500);
            }
        }
    }

    private static void MoveCompanions(string from, string to)
    {
        foreach (var suffix in Companions)
        {
            if (File.Exists(from + suffix)) File.Move(from + suffix, to + suffix, overwrite: true);
        }
    }
}
