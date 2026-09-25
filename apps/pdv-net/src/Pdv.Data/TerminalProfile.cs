namespace Pdv.Data;

/// <summary>Quem é este caixa — o que a ativação gravou em <c>device_settings</c>.</summary>
/// <remarks>
/// Mesmas chaves do <c>SettingsStore</c> do Python. Sem ativação, os padrões
/// de demonstração do <c>AppConfig</c> (a loja "Confeitaria Demo"): o caixa
/// abre para experimentar, e as vendas de teste são arquivadas na ativação.
/// </remarks>
public sealed record TerminalProfile(
    string TenantId,
    string StoreId,
    string DeviceId,
    string StoreName,
    bool Activated,
    string? CloudBaseUrl)
{
    public const string DemoTenantId = "11111111-1111-1111-1111-111111111111";
    public const string DemoStoreId = "22222222-2222-2222-2222-222222222222";
    public const string DemoDeviceId = "33333333-3333-3333-3333-333333333333";
    public const string DemoStoreName = "Confeitaria Demo";

    public Sales.TerminalIdentity Identity => new(TenantId, StoreId, DeviceId);

    public static TerminalProfile Load(PdvDatabase database)
    {
        var values = new Dictionary<string, string>(StringComparer.Ordinal);
        using (var command = Sql.Command(database.Connection, null, "SELECT key, value FROM device_settings"))
        using (var reader = command.ExecuteReader())
        {
            while (reader.Read()) values[reader.GetString(0)] = reader.GetString(1);
        }

        string Value(string key, string fallback) =>
            values.TryGetValue(key, out var value) && value.Length > 0 ? value : fallback;

        return new TerminalProfile(
            Value("device.tenant_id", DemoTenantId),
            Value("device.store_id", DemoStoreId),
            Value("device.id", DemoDeviceId),
            Value("store.name", DemoStoreName),
            values.GetValueOrDefault("device.activated") == "1",
            values.GetValueOrDefault("cloud.base_url"));
    }

    /// <summary>
    /// Onde o banco mora: <c>PDV_DB_PATH</c> (depuração) ou a pasta que o
    /// instalador cria e o <c>harden.ps1</c> libera para o operador gravar.
    /// </summary>
    public static string DefaultDatabasePath() =>
        Environment.GetEnvironmentVariable("PDV_DB_PATH") is { Length: > 0 } custom
            ? custom
            : Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
                "ERPFood", "PDV", "pdv_local.db");
}
