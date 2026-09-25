using System.Security.Cryptography.X509Certificates;
using System.Xml;
using DFe.Classes.Entidades;
using DFe.Classes.Flags;
using DFe.Utils;
using NFe.Classes;
using NFe.Classes.Informacoes.Identificacao.Tipos;
using NFe.Classes.Servicos.Tipos;
using NFe.Utils;
using NFe.Utils.InformacoesSuplementares;
using NFe.Utils.NFe;
using NfeDocument = NFe.Classes.NFe;

namespace Fiscal.Service.Nfce;

/// <summary>O XML reprovado no schema oficial. Nada é transmitido.</summary>
public sealed class SchemaException(string message) : Exception(message);

/// <summary>
/// Assinatura, QR Code e validação da NFC-e — e a configuração do DFe.NET,
/// criada por chamada e nunca a instância global da biblioteca: cada loja
/// assina com o próprio A1, e duas vendas simultâneas não podem trocar de
/// certificado.
/// </summary>
public static class NfceDocuments
{
    public const string Namespace = "http://www.portalfiscal.inf.br/nfe";

    public static ConfiguracaoServico Configuration(Estado uf, bool production, string schemas, TimeSpan timeout)
    {
        var configuration = new ConfiguracaoServico
        {
            tpAmb = production ? TipoAmbiente.Producao : TipoAmbiente.Homologacao,
            cUF = uf,
            ModeloDocumento = ModeloDocumento.NFCe,
            tpEmis = TipoEmissao.teNormal,
            DiretorioSchemas = schemas,
            ValidarSchemas = true,
            SalvarXmlServicos = false,
            TimeOut = (int)timeout.TotalMilliseconds,
            ProtocoloDeSeguranca = System.Net.SecurityProtocolType.Tls12,
        };
        configuration.VersaoLayout = VersaoServico.Versao400;
        configuration.VersaoNFeAutorizacao = VersaoServico.Versao400;
        configuration.VersaoNFeRetAutorizacao = VersaoServico.Versao400;
        configuration.VersaoNfeConsultaProtocolo = VersaoServico.Versao400;
        configuration.VersaoNfeStatusServico = VersaoServico.Versao400;
        return configuration;
    }

    /// <summary>
    /// Assina, acrescenta o QR Code e valida no XSD. Devolve o XML que vai à
    /// SEFAZ — e que fica guardado, byte a byte, para a reconciliação.
    /// </summary>
    /// <param name="qrVersion">3 (sem CSC, NT 2025.001) ou 2 (com CSC).</param>
    public static string Sign(
        NfeDocument nfe, ConfiguracaoServico configuration, X509Certificate2 certificate,
        int qrVersion, string? cscId = null, string? csc = null)
    {
        nfe.Assina(configuration, certificate);
        var version = qrVersion == 2 ? VersaoQrCode.QrCodeVersao2 : VersaoQrCode.QrCodeVersao3;
        nfe.infNFeSupl = new infNFeSupl();
        nfe.infNFeSupl.urlChave = nfe.infNFeSupl.ObterUrlConsulta(nfe, version);
        nfe.infNFeSupl.qrCode = qrVersion == 2
            ? nfe.infNFeSupl.ObterUrlQrCode(nfe, version, cscId!.TrimStart('0'), csc!)
            // Emissão normal: o v3 é chave|3|ambiente, sem CSC e sem assinatura. O
            // certificado só entra no QR da contingência, que é do PDV, não daqui.
            : nfe.infNFeSupl.ObterUrlQrCode(nfe, version, null, null, new ConfiguracaoCertificado());
        try
        {
            nfe.Valida(configuration);
        }
        catch (Exception error) when (error.GetType().Name == "ValidacaoSchemaException")
        {
            throw new SchemaException(error.Message);
        }
        return nfe.ObterXmlString();
    }

    public static string AccessKey(NfeDocument nfe) => nfe.infNFe.Id[3..];

    /// <summary>
    /// O documento autorizado: o XML assinado, intacto, mais o protocolo como a
    /// SEFAZ o devolveu. Composto por texto, e não reserializado, para que nem um
    /// byte do que foi assinado mude.
    /// </summary>
    public static string Processed(string signedXml, string protocolXml)
    {
        var nfe = new XmlDocument { PreserveWhitespace = true };
        nfe.LoadXml(signedXml);
        return "<?xml version=\"1.0\" encoding=\"UTF-8\"?>" +
               $"<nfeProc versao=\"4.00\" xmlns=\"{Namespace}\">" +
               nfe.DocumentElement!.OuterXml + protocolXml + "</nfeProc>";
    }

    /// <summary>Carrega de volta o XML assinado, para retransmitir exatamente a mesma nota.</summary>
    public static NfeDocument Load(string signedXml) => new NfeDocument().CarregarDeXmlString(signedXml);
}
