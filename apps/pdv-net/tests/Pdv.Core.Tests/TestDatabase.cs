using Microsoft.Data.Sqlite;

namespace Pdv.Core.Tests;

/// <summary>Banco de teste criado do schema que o Python de fato produz.</summary>
/// <remarks>
/// A fonte é <c>contracts/pdv-schema.sql</c>, gerado pelo <c>migrate()</c> do
/// PDV em Python — nunca uma cópia escrita à mão, que divergiria na primeira
/// migration nova.
/// </remarks>
public sealed class TestDatabase : IDisposable
{
    private readonly string _folder;

    public TestDatabase(int? userVersion = null)
    {
        _folder = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "pdv-net-" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(_folder);
        Path = System.IO.Path.Combine(_folder, "pdv_local.db");

        using var connection = new SqliteConnection($"Data Source={Path};Pooling=False");
        connection.Open();
        using var command = connection.CreateCommand();
        command.CommandText = File.ReadAllText(Contract("pdv-schema.sql"));
        command.ExecuteNonQuery();
        if (userVersion is not null)
        {
            command.CommandText = $"PRAGMA user_version = {userVersion}";
            command.ExecuteNonQuery();
        }
    }

    public string Path { get; }

    public static string Contract(string name)
    {
        var folder = new DirectoryInfo(AppContext.BaseDirectory);
        while (folder is not null && !File.Exists(System.IO.Path.Combine(folder.FullName, "contracts", name)))
        {
            folder = folder.Parent;
        }
        return folder is null
            ? throw new FileNotFoundException($"contracts/{name} não encontrado acima de {AppContext.BaseDirectory}")
            : System.IO.Path.Combine(folder.FullName, "contracts", name);
    }

    public void Dispose()
    {
        SqliteConnection.ClearAllPools();
        try
        {
            Directory.Delete(_folder, recursive: true);
        }
        catch (IOException)
        {
            // arquivo ainda preso por um leitor atrasado: a pasta é temporária
        }
    }
}
