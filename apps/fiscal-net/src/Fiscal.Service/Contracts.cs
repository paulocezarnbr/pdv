using System.Text.Json;
using System.Text.Json.Serialization;

namespace Fiscal.Service;

/// <summary>O emitente, como a retaguarda o cadastrou no painel.</summary>
public sealed record Issuer(
    [property: JsonPropertyName("uf")] string Uf,
    [property: JsonPropertyName("cnpj")] string Cnpj,
    [property: JsonPropertyName("stateRegistration")] string StateRegistration,
    [property: JsonPropertyName("taxRegime")] int TaxRegime,
    [property: JsonPropertyName("legalName")] string LegalName,
    [property: JsonPropertyName("address")] JsonElement Address);

/// <summary>Um item da venda com o perfil tributário que o contador cadastrou.</summary>
public sealed record FiscalItem(
    [property: JsonPropertyName("productId")] string ProductId,
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("quantity")] string Quantity,
    [property: JsonPropertyName("unitPriceCents")] long UnitPriceCents,
    [property: JsonPropertyName("totalCents")] long TotalCents,
    [property: JsonPropertyName("ncm")] string Ncm,
    [property: JsonPropertyName("cfop")] string Cfop,
    [property: JsonPropertyName("cest")] string? Cest,
    [property: JsonPropertyName("unitCode")] string UnitCode,
    [property: JsonPropertyName("origin")] int Origin,
    [property: JsonPropertyName("csosn")] string? Csosn,
    [property: JsonPropertyName("cstIcms")] string? CstIcms,
    [property: JsonPropertyName("cstPis")] string CstPis,
    [property: JsonPropertyName("cstCofins")] string CstCofins);

/// <summary>Um pagamento da venda, com os valores de <c>payments.method</c> do PDV.</summary>
public sealed record FiscalPayment(
    [property: JsonPropertyName("method")] string Method,
    [property: JsonPropertyName("amountCents")] long AmountCents,
    [property: JsonPropertyName("changeCents")] long ChangeCents);

/// <summary>
/// O pedido de autorização que a retaguarda manda. Referências de segredo, nunca
/// segredo: o A1, a senha e o CSC moram no cofre montado neste contêiner.
/// </summary>
public sealed record FiscalIntent(
    [property: JsonPropertyName("documentId")] string DocumentId,
    [property: JsonPropertyName("requestUuid")] string RequestUuid,
    [property: JsonPropertyName("orderId")] string OrderId,
    [property: JsonPropertyName("tenantId")] string TenantId,
    [property: JsonPropertyName("storeId")] string StoreId,
    [property: JsonPropertyName("deviceId")] string DeviceId,
    [property: JsonPropertyName("model")] int Model,
    [property: JsonPropertyName("series")] int Series,
    [property: JsonPropertyName("number")] long Number,
    [property: JsonPropertyName("environment")] string Environment,
    [property: JsonPropertyName("certificateRef")] string CertificateRef,
    [property: JsonPropertyName("cscRef")] string? CscRef,
    [property: JsonPropertyName("cscId")] string? CscId,
    [property: JsonPropertyName("issuer")] Issuer Issuer,
    [property: JsonPropertyName("totalCents")] long TotalCents,
    [property: JsonPropertyName("items")] IReadOnlyList<FiscalItem> Items,
    [property: JsonPropertyName("payments")] IReadOnlyList<FiscalPayment>? Payments)
{
    public bool IsProduction => Environment == "production";
}

public sealed record StatusRequest([property: JsonPropertyName("request_uuid")] string RequestUuid);

/// <summary>A resposta das duas rotas — os mesmos nomes do serviço em Python.</summary>
public sealed record FiscalResult(
    [property: JsonPropertyName("status")] string Status,
    [property: JsonPropertyName("code")] string Code,
    [property: JsonPropertyName("reason")] string Reason,
    [property: JsonPropertyName("access_key")] string? AccessKey = null,
    [property: JsonPropertyName("protocol")] string? Protocol = null,
    [property: JsonPropertyName("processed_xml")] string? ProcessedXml = null)
{
    public const string Authorized = "authorized";
    public const string Rejected = "rejected";
    public const string Unknown = "unknown";

    public static FiscalResult Unknowable(string code, string reason) => new(Unknown, code, reason);
}

internal static class Json
{
    public static readonly JsonSerializerOptions Options = new(JsonSerializerDefaults.Web)
    {
        DefaultIgnoreCondition = JsonIgnoreCondition.Never,
    };
}
