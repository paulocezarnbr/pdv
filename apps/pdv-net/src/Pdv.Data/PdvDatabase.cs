using Microsoft.Data.Sqlite;

namespace Pdv.Data;

public sealed class PdvDatabaseException(string message) : Exception(message);

/// <summary>O <c>pdv_local.db</c> — o mesmo arquivo que o PDV em Python usa.</summary>
/// <remarks>
/// <para>
/// Até a fase C7 do porte as migrations continuam no Python. Este lado só abre
/// um banco na versão que conhece (<see cref="SupportedSchemaVersion"/>) e recusa
/// o resto com uma mensagem que diz o que fazer — um PDV que abre um schema que
/// não conhece grava venda onde não devia.
/// </para>
/// <para>
/// Os PRAGMAs são os mesmos do Python: WAL, <c>synchronous=FULL</c> (a venda
/// confirmada está no disco antes de o cupom sair), chaves estrangeiras e
/// espera de 10 s por trava.
/// </para>
/// </remarks>
public sealed class PdvDatabase : IDisposable
{
    /// <summary>O <c>SCHEMA_VERSION</c> do Python que este código foi testado contra.</summary>
    public const int SupportedSchemaVersion = 14;

    public PdvDatabase(string path)
    {
        Path = path;
        var builder = new SqliteConnectionStringBuilder
        {
            DataSource = path,
            Mode = SqliteOpenMode.ReadWrite,
            // Sem pool: fechar o banco precisa soltar o arquivo de verdade (a
            // ativação troca o arquivo inteiro; o instalador o substitui).
            Pooling = false,
        };
        Connection = new SqliteConnection(builder.ToString());
        try
        {
            Connection.Open();
        }
        catch (SqliteException error)
        {
            Connection.Dispose();
            throw new PdvDatabaseException(
                $"Não foi possível abrir o banco do PDV em {path}: {error.Message}. " +
                "Se é a primeira abertura, instale o PDV pelo instalador.");
        }

        foreach (var pragma in new[]
                 {
                     "PRAGMA journal_mode = WAL",
                     "PRAGMA synchronous = FULL",
                     "PRAGMA foreign_keys = ON",
                     "PRAGMA busy_timeout = 10000",
                     "PRAGMA temp_store = MEMORY",
                 })
        {
            Execute(pragma);
        }
        EnsureCompatible();
    }

    public string Path { get; }

    public SqliteConnection Connection { get; }

    public int SchemaVersion => Convert.ToInt32(Scalar("PRAGMA user_version"));

    private void EnsureCompatible()
    {
        var version = SchemaVersion;
        if (version == SupportedSchemaVersion) return;

        Connection.Dispose();
        throw new PdvDatabaseException(version switch
        {
            0 => $"O banco em {Path} não foi inicializado. Instale o PDV pelo instalador.",
            < SupportedSchemaVersion =>
                $"O banco está na versão {version}, anterior à {SupportedSchemaVersion}. " +
                "Abra uma vez o PDV atual para ele atualizar o banco.",
            _ =>
                $"O banco está na versão {version}, mais nova que a {SupportedSchemaVersion} que esta versão " +
                "do PDV conhece. Atualize o PDV antes de abrir o caixa — nenhuma venda foi perdida.",
        });
    }

    /// <summary>
    /// Transação com <c>BEGIN IMMEDIATE</c>: a trava de escrita é pega na
    /// entrada, e não no meio da venda, onde a espera viraria erro.
    /// </summary>
    public T InTransaction<T>(Func<SqliteTransaction, T> work)
    {
        using var transaction = Connection.BeginTransaction(deferred: false);
        var result = work(transaction);
        transaction.Commit();
        return result;
    }

    public void InTransaction(Action<SqliteTransaction> work) =>
        InTransaction<object?>(transaction =>
        {
            work(transaction);
            return null;
        });

    public int Execute(string sql, params (string Name, object? Value)[] parameters)
    {
        using var command = Sql.Command(Connection, null, sql, parameters);
        return command.ExecuteNonQuery();
    }

    public object? Scalar(string sql, params (string Name, object? Value)[] parameters)
    {
        using var command = Sql.Command(Connection, null, sql, parameters);
        return command.ExecuteScalar();
    }

    public void Dispose() => Connection.Dispose();
}

internal static class Sql
{
    public static SqliteCommand Command(
        SqliteConnection connection, SqliteTransaction? transaction, string sql,
        params (string Name, object? Value)[] parameters)
    {
        var command = connection.CreateCommand();
        command.CommandText = sql;
        command.Transaction = transaction;
        foreach (var (name, value) in parameters)
        {
            command.Parameters.AddWithValue(name, value ?? DBNull.Value);
        }
        return command;
    }

    public static SqliteCommand Command(
        this SqliteTransaction transaction, string sql, params (string Name, object? Value)[] parameters) =>
        Command(transaction.Connection!, transaction, sql, parameters);
}
