namespace Fiscal.Service;

/// <summary>O responsável técnico pelo sistema emissor (grupo <c>infRespTec</c>), exigido por várias SEFAZ.</summary>
public sealed record TechnicalContact(string Cnpj, string Contact, string Email, string Phone);

/// <summary>A configuração do serviço, toda por variável de ambiente (Coolify).</summary>
public sealed record FiscalOptions
{
    /// <summary>O token que a retaguarda manda. Vazio = toda rota interna recusa.</summary>
    public string Token { get; init; } = "";

    public string StateDatabase { get; init; } = "data/fiscal-state.sqlite3";

    public string SecretsDirectory { get; init; } = "/run/secrets/fiscal";

    /// <summary><c>nfce</c> (padrão) ou <c>gate</c> (recusa tudo, como o serviço em Python).</summary>
    public string Engine { get; init; } = "nfce";

    /// <summary>
    /// Produção só com <c>FISCAL_PRODUCTION_ENABLED=true</c>, aqui e na retaguarda:
    /// as duas travas precisam ser abertas de propósito.
    /// </summary>
    public bool ProductionEnabled { get; init; }

    /// <summary>
    /// QR Code da NFC-e: 3 (padrão, NT 2025.001, sem CSC) ou 2 (com CSC). O v2
    /// fica como volta atrás se alguma SEFAZ recusar o v3.
    /// </summary>
    public int QrCodeVersion { get; init; } = 3;

    public string SchemasDirectory { get; init; } = Path.Combine(AppContext.BaseDirectory, "Schemas");

    public TimeSpan SefazTimeout { get; init; } = TimeSpan.FromSeconds(30);

    public TechnicalContact? TechnicalContact { get; init; }

    public static FiscalOptions FromEnvironment()
    {
        static string? Env(string name) => Environment.GetEnvironmentVariable(name) is { Length: > 0 } value ? value.Trim() : null;

        var contact = Env("FISCAL_RESP_TEC_CNPJ") is { } cnpj
            ? new TechnicalContact(cnpj, Env("FISCAL_RESP_TEC_CONTATO") ?? "", Env("FISCAL_RESP_TEC_EMAIL") ?? "", Env("FISCAL_RESP_TEC_FONE") ?? "")
            : null;
        var qr = Env("FISCAL_QRCODE_VERSION") ?? "3";
        if (qr is not ("2" or "3")) throw new InvalidOperationException($"FISCAL_QRCODE_VERSION precisa ser 2 ou 3, veio '{qr}'.");
        var engine = Env("FISCAL_ENGINE") ?? "nfce";
        if (engine is not ("nfce" or "gate")) throw new InvalidOperationException($"FISCAL_ENGINE precisa ser nfce ou gate, veio '{engine}'.");

        return new FiscalOptions
        {
            Token = Env("FISCAL_SERVICE_TOKEN") ?? "",
            StateDatabase = Env("FISCAL_STATE_DB") ?? "data/fiscal-state.sqlite3",
            SecretsDirectory = Env("FISCAL_SECRETS_DIR") ?? "/run/secrets/fiscal",
            Engine = engine,
            ProductionEnabled = string.Equals(Env("FISCAL_PRODUCTION_ENABLED"), "true", StringComparison.OrdinalIgnoreCase),
            QrCodeVersion = int.Parse(qr),
            SchemasDirectory = Env("FISCAL_SCHEMAS_DIR") ?? Path.Combine(AppContext.BaseDirectory, "Schemas"),
            SefazTimeout = TimeSpan.FromMilliseconds(int.TryParse(Env("FISCAL_SEFAZ_TIMEOUT_MS"), out var ms) && ms > 0 ? ms : 30_000),
            TechnicalContact = contact,
        };
    }
}
