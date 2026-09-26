using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Pdv.Data.Provisioning;

namespace Pdv.Data.Fiscal;

/// <summary>A nota como a retaguarda a descreve (<c>/fiscal/issue</c> e <c>/fiscal/status</c>).</summary>
/// <param name="Status">processing, unknown, authorized, rejected, canceled ou not_required.</param>
/// <param name="ProcessedXml">O <c>nfeProc</c>, só com a nota autorizada.</param>
public sealed record CloudFiscalDocument(
    string RequestUuid,
    string OrderId,
    string Status,
    int Series,
    long Number,
    string? AccessKey = null,
    string? Protocol = null,
    string? Reason = null,
    string? ProcessedXml = null);

public class FiscalCloudException(string message, Exception? inner = null) : Exception(message, inner);

/// <summary>Não houve conexão: o pedido comprovadamente não saiu deste caixa.</summary>
public sealed class FiscalOfflineException(string message, Exception? inner = null) : FiscalCloudException(message, inner);

/// <summary>
/// O pedido pode ter sido processado: timeout, 5xx ou resposta ilegível.
/// <b>Nunca</b> se emite outra nota por causa disto — só se consulta.
/// </summary>
public sealed class FiscalResultUnknownException(string message, Exception? inner = null) : FiscalCloudException(message, inner);

public sealed class FiscalAuthException(string message) : FiscalCloudException(message);

/// <summary>A retaguarda recusou (4xx): venda que ainda não chegou, configuração incompleta.</summary>
public sealed class FiscalRefusedException(int status, string message) : FiscalCloudException(message)
{
    public int Status { get; } = status;
}

public interface IFiscalGateway
{
    Task<CloudFiscalDocument> IssueAsync(string requestUuid, string orderId, CancellationToken cancellation);

    Task<CloudFiscalDocument> StatusAsync(string requestUuid, CancellationToken cancellation);
}

/// <summary>
/// O canal até <c>/fiscal/*</c> na retaguarda — a classificação do
/// <c>fiscal/cloud.py</c> do Python, erro a erro.
/// </summary>
/// <remarks>
/// <para>
/// A pergunta de cada erro é uma só: <b>a SEFAZ pode ter autorizado?</b> Sem
/// conexão (TCP ou nome), não: o pedido não saiu daqui. Timeout, 5xx e resposta
/// ilegível, pode: a nota talvez exista e só a resposta se perdeu. Tratar o
/// segundo caso como o primeiro é o caminho da nota em duplicidade.
/// </para>
/// <para>
/// O prazo é do chamador: no balcão, o cliente está esperando e o prazo é curto;
/// no segundo plano, pode ser longo. Prazo estourado é "resultado desconhecido",
/// nunca "offline".
/// </para>
/// </remarks>
public sealed class HttpFiscalGateway : IFiscalGateway, IDisposable
{
    private readonly HttpClient _client;
    private readonly bool _ownsClient;
    private readonly string _root;
    private readonly TimeSpan _timeout;

    public HttpFiscalGateway(string baseUrl, string deviceToken, TimeSpan timeout, HttpClient? client = null)
    {
        _root = Activation.CloudApiRoot(baseUrl);
        _timeout = timeout;
        _ownsClient = client is null;
        _client = client ?? new HttpClient { Timeout = System.Threading.Timeout.InfiniteTimeSpan };
        _client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", deviceToken);
    }

    public Task<CloudFiscalDocument> IssueAsync(string requestUuid, string orderId, CancellationToken cancellation)
    {
        var body = new JsonObject { ["request_uuid"] = requestUuid, ["order_id"] = orderId };
        return SendAsync(() => new HttpRequestMessage(HttpMethod.Post, $"{_root}/fiscal/issue")
        {
            Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json"),
        }, cancellation);
    }

    public Task<CloudFiscalDocument> StatusAsync(string requestUuid, CancellationToken cancellation) =>
        SendAsync(() => new HttpRequestMessage(
            HttpMethod.Get, $"{_root}/fiscal/status?request_uuid={Uri.EscapeDataString(requestUuid)}"), cancellation);

    private async Task<CloudFiscalDocument> SendAsync(Func<HttpRequestMessage> build, CancellationToken cancellation)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellation);
        deadline.CancelAfter(_timeout);
        int status;
        string text;
        try
        {
            using var request = build();
            using var response = await _client.SendAsync(request, deadline.Token);
            status = (int)response.StatusCode;
            text = await response.Content.ReadAsStringAsync(deadline.Token);
        }
        catch (OperationCanceledException) when (cancellation.IsCancellationRequested)
        {
            throw;
        }
        catch (OperationCanceledException error)
        {
            throw new FiscalResultUnknownException("A retaguarda não respondeu a tempo; a nota será consultada.", error);
        }
        // Sem conexão, nome que não resolve e TLS recusado: tudo antes de o pedido
        // HTTP sair — o que o httpx do Python chama de ConnectError.
        catch (HttpRequestException error) when (error.HttpRequestError is HttpRequestError.ConnectionError
                                                     or HttpRequestError.NameResolutionError
                                                     or HttpRequestError.SecureConnectionError)
        {
            throw new FiscalOfflineException("Sem conexão com a retaguarda; o pedido da nota não saiu deste caixa.", error);
        }
        catch (Exception error) when (error is HttpRequestException or IOException)
        {
            throw new FiscalResultUnknownException("A conexão caiu no meio do pedido da nota; ela será consultada.", error);
        }

        if (status is 401 or 403) throw new FiscalAuthException("Terminal não autorizado a pedir nota fiscal.");
        if (status >= 500) throw new FiscalResultUnknownException("A retaguarda falhou depois de receber o pedido; a nota será consultada.");
        if (status >= 400) throw new FiscalRefusedException(status, Detail(text) ?? $"Pedido de nota recusado (HTTP {status}).");
        return Parse(text);
    }

    private static string? Detail(string text)
    {
        try
        {
            using var document = JsonDocument.Parse(text);
            return document.RootElement.ValueKind == JsonValueKind.Object &&
                   document.RootElement.TryGetProperty("detail", out var detail) && detail.ValueKind == JsonValueKind.String
                ? detail.GetString()
                : null;
        }
        catch (JsonException)
        {
            return null;
        }
    }

    /// <summary>Resposta 2xx que não se lê é tão ambígua quanto um timeout.</summary>
    internal static CloudFiscalDocument Parse(string text)
    {
        try
        {
            using var document = JsonDocument.Parse(text);
            var body = document.RootElement;
            string? Text(string name) =>
                body.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() : null;
            long Number(string name) =>
                body.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.Number ? value.GetInt64() : 0;
            return new CloudFiscalDocument(
                Text("request_uuid") ?? throw new KeyNotFoundException("request_uuid"),
                Text("order_id") ?? throw new KeyNotFoundException("order_id"),
                Text("status") ?? throw new KeyNotFoundException("status"),
                (int)Number("series"), Number("number"),
                Text("access_key"), Text("protocol"), Text("provider_reason"), Text("processed_xml"));
        }
        catch (Exception error) when (error is JsonException or KeyNotFoundException or InvalidOperationException or FormatException)
        {
            throw new FiscalResultUnknownException("Resposta ilegível da retaguarda; a nota será consultada.", error);
        }
    }

    public void Dispose()
    {
        if (_ownsClient) _client.Dispose();
    }
}
