using System.Net;
using System.Security.Cryptography.X509Certificates;
using System.Xml;
using DFe.Classes.Entidades;
using NFe.Classes.Servicos.Tipos;
using NFe.Servicos;

namespace Fiscal.Service.Nfce;

/// <summary>Para onde e com qual certificado falar.</summary>
public sealed record SefazTarget(Estado Uf, bool Production, X509Certificate2 Certificate);

/// <summary>
/// O que a SEFAZ respondeu, sem interpretação: o status do lote (ou da
/// consulta), e o do documento com o protocolo quando houver.
/// </summary>
public sealed record SefazAnswer(
    int Status,
    string Reason,
    int? DocumentStatus = null,
    string? DocumentReason = null,
    string? Protocol = null,
    string? ProtocolXml = null);

/// <summary>A SEFAZ não respondeu (rede, timeout, TLS): o documento pode ou não ter chegado.</summary>
public sealed class SefazUnreachableException(string message, Exception? inner = null) : Exception(message, inner);

/// <summary>
/// A SEFAZ recusou o certificado na porta (HTTP 401/403, antes de ler o SOAP):
/// o documento com certeza não foi processado.
/// </summary>
public sealed class SefazCertificateRefusedException(int status) : Exception($"SEFAZ recusou o certificado (HTTP {status}).")
{
    public int Status { get; } = status;
}

/// <summary>O canal até a SEFAZ. Real pelo DFe.NET; falso nos testes de cada resposta possível.</summary>
public interface ISefazGateway
{
    /// <summary>Autorização síncrona de um lote com uma NFC-e (indSinc = 1).</summary>
    Task<SefazAnswer> AuthorizeAsync(SefazTarget target, string signedXml, CancellationToken cancellation);

    /// <summary>Consulta a situação de uma chave (consSitNFe).</summary>
    Task<SefazAnswer> QueryAsync(SefazTarget target, string accessKey, CancellationToken cancellation);
}

/// <summary>A SEFAZ de verdade, pelo DFe.NET (Zeus).</summary>
/// <remarks>
/// O DFe.NET é síncrono; cada chamada roda fora da thread de requisição. A
/// biblioteca devolve o SOAP cru, e o protocolo é recortado dali como veio —
/// com a assinatura da SEFAZ, quando houver.
/// </remarks>
public sealed class ZeusSefazGateway(string schemas, TimeSpan timeout) : ISefazGateway
{
    public Task<SefazAnswer> AuthorizeAsync(SefazTarget target, string signedXml, CancellationToken cancellation) =>
        Task.Run(() => Call(() =>
        {
            using var services = new ServicosNFe(NfceDocuments.Configuration(target.Uf, target.Production, schemas, timeout), target.Certificate);
            var lot = (int)(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() % 1_000_000_000);
            var answer = services.NFeAutorizacao(lot, IndicadorSincronizacao.Sincrono, [NfceDocuments.Load(signedXml)], false);
            var result = answer.Retorno;
            var info = result.protNFe?.infProt;
            return new SefazAnswer(
                result.cStat, result.xMotivo, info?.cStat, info?.xMotivo,
                string.IsNullOrWhiteSpace(info?.nProt) ? null : info.nProt,
                ProtocolXml(answer.RetornoCompletoStr));
        }), cancellation);

    public Task<SefazAnswer> QueryAsync(SefazTarget target, string accessKey, CancellationToken cancellation) =>
        Task.Run(() => Call(() =>
        {
            using var services = new ServicosNFe(NfceDocuments.Configuration(target.Uf, target.Production, schemas, timeout), target.Certificate);
            var answer = services.NfeConsultaProtocolo(accessKey);
            var result = answer.Retorno;
            var info = result.protNFe?.infProt;
            return new SefazAnswer(
                result.cStat, result.xMotivo, info?.cStat, info?.xMotivo,
                string.IsNullOrWhiteSpace(info?.nProt) ? null : info.nProt,
                ProtocolXml(answer.RetornoCompletoStr));
        }), cancellation);

    private static SefazAnswer Call(Func<SefazAnswer> call)
    {
        try
        {
            return call();
        }
        catch (Exception error) when (RefusedCertificate(error) is { } status)
        {
            throw new SefazCertificateRefusedException(status);
        }
        catch (Exception error) when (IsTransport(error))
        {
            throw new SefazUnreachableException($"SEFAZ inacessível: {error.GetType().Name}", error);
        }
    }

    /// <summary>401/403 do servidor da SEFAZ: o certificado de cliente foi barrado no TLS/IIS.</summary>
    private static int? RefusedCertificate(Exception error)
    {
        for (var current = error; current is not null; current = current.InnerException)
        {
            if (current is WebException { Response: HttpWebResponse response } &&
                (int)response.StatusCode is 401 or 403)
            {
                return (int)response.StatusCode;
            }
        }
        return null;
    }

    /// <summary>Falha de rede em qualquer camada da biblioteca (ela embrulha as exceções).</summary>
    private static bool IsTransport(Exception error)
    {
        for (var current = error; current is not null; current = current.InnerException)
        {
            if (current is WebException or HttpRequestException or TimeoutException ||
                (current is System.IO.IOException and not System.IO.FileNotFoundException and not System.IO.DirectoryNotFoundException) ||
                current.GetType().Name is "ComunicacaoException" or "ComunicacaoDfeException")
            {
                return true;
            }
        }
        return false;
    }

    /// <summary>O elemento <c>protNFe</c> da resposta, exatamente como a SEFAZ o mandou.</summary>
    internal static string? ProtocolXml(string? response)
    {
        if (string.IsNullOrEmpty(response)) return null;
        var document = new XmlDocument { PreserveWhitespace = true };
        document.LoadXml(response);
        var namespaces = new XmlNamespaceManager(document.NameTable);
        namespaces.AddNamespace("n", NfceDocuments.Namespace);
        return document.SelectSingleNode("//n:protNFe", namespaces)?.OuterXml;
    }
}
