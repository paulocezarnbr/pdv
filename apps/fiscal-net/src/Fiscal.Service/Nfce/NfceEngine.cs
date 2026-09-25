using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text.Json;
using DFe.Classes.Entidades;

namespace Fiscal.Service.Nfce;

/// <summary>
/// NFC-e 4.00 de verdade: monta, assina com o A1 da loja, transmite à SEFAZ e,
/// quando a resposta se perde, pergunta pela chave em vez de adivinhar.
/// </summary>
/// <remarks>
/// <para><b>Os segredos</b>, no cofre montado em <c>FISCAL_SECRETS_DIR</c>:</para>
/// <list type="bullet">
/// <item><c>&lt;certificateRef&gt;</c> — o A1 (.pfx);</item>
/// <item><c>&lt;certificateRef&gt;.senha</c> — a senha dele (sem o arquivo, A1 sem senha);</item>
/// <item><c>&lt;cscRef&gt;</c> — o CSC, só com <c>FISCAL_QRCODE_VERSION=2</c>.</item>
/// </list>
/// <para>
/// <b>Os códigos de volta.</b> <c>authorized</c> exige cStat 100 ou 150, chave,
/// protocolo e XML processado. Recusa da SEFAZ é <c>rejected</c> com o cStat
/// dela. Duplicidade (204, 539) vira consulta pela chave. SEFAZ fora do ar,
/// lote em processamento ou resposta perdida ficam <c>unknown</c> e abertos:
/// a consulta seguinte resolve.
/// </para>
/// </remarks>
public sealed class NfceEngine(
    FiscalOptions options, SecretResolver secrets, ISefazGateway sefaz, TimeProvider? clock = null, Action<string>? log = null)
    : IFiscalEngine
{
    private static readonly HashSet<int> AuthorizedCodes = [100, 150];
    private static readonly HashSet<int> DuplicateCodes = [204, 539];
    private static readonly HashSet<int> DeniedCodes = [110, 301, 302, 303];

    /// <summary>Paralisação, consumo indevido, erro não catalogado: a mesma nota passa depois.</summary>
    private static readonly HashSet<int> TransientLotCodes = [103, 105, 108, 109, 656, 999];

    private readonly TimeProvider _clock = clock ?? TimeProvider.System;
    private readonly Action<string> _log = log ?? (_ => { });

    public string Name => "nfce-4.00";

    private sealed record Context(string CertificateRef, string Environment, string Uf);

    public FiscalResult? Preflight(FiscalIntent intent)
    {
        if (intent.IsProduction && !options.ProductionEnabled)
        {
            return FiscalResult.Unknowable("PRODUCTION_DISABLED",
                "Emissão em produção bloqueada neste serviço (FISCAL_PRODUCTION_ENABLED). Nenhum número foi transmitido.");
        }
        try
        {
            using var certificate = Certificate(intent.CertificateRef);
            if (options.QrCodeVersion == 2)
            {
                if (string.IsNullOrEmpty(intent.CscRef) || string.IsNullOrEmpty(intent.CscId))
                {
                    throw new SecretException("QR Code v2 exige a referência e o ID do CSC no cadastro fiscal.");
                }
                _ = secrets.Text(intent.CscRef);
            }
        }
        catch (Exception error) when (error is SecretException or CryptographicException)
        {
            return FiscalResult.Unknowable("FISCAL_SETUP", $"{error.Message} Nenhum número foi transmitido.");
        }
        return null;
    }

    /// <summary>O A1 do cofre, conferido na validade. A senha nunca sai daqui.</summary>
    private X509Certificate2 Certificate(string reference)
    {
        var password = secrets.OptionalText(reference + ".senha");
        X509Certificate2 certificate;
        try
        {
            certificate = X509CertificateLoader.LoadPkcs12(
                secrets.Bytes(reference), password,
                OperatingSystem.IsWindows() ? X509KeyStorageFlags.Exportable : X509KeyStorageFlags.EphemeralKeySet);
        }
        catch (CryptographicException)
        {
            // A mensagem do .NET pode dizer "senha incorreta" — e só isso deve sair.
            throw new SecretException($"Certificado A1 {reference} não abre: arquivo inválido ou senha errada.");
        }
        var now = _clock.GetUtcNow().UtcDateTime;
        if (now < certificate.NotBefore.ToUniversalTime() || now > certificate.NotAfter.ToUniversalTime())
        {
            var until = certificate.NotAfter;
            certificate.Dispose();
            throw new SecretException($"Certificado A1 {reference} fora da validade (vence/venceu em {until:dd/MM/yyyy}).");
        }
        if (!certificate.HasPrivateKey)
        {
            certificate.Dispose();
            throw new SecretException($"Certificado A1 {reference} sem chave privada.");
        }
        return certificate;
    }

    public async Task<EngineOutcome> AuthorizeAsync(
        FiscalIntent intent, Action<string, string, string> prepare, CancellationToken cancellation)
    {
        using var certificate = Certificate(intent.CertificateRef);
        string signed;
        string key;
        Estado uf;
        try
        {
            var nfe = NfceBuilder.Build(intent, _clock.GetUtcNow(), NfceBuilder.RandomCode(intent.Number), options.TechnicalContact);
            uf = nfe.infNFe.ide.cUF;
            var configuration = NfceDocuments.Configuration(uf, intent.IsProduction, options.SchemasDirectory, options.SefazTimeout);
            signed = options.QrCodeVersion == 2
                ? NfceDocuments.Sign(nfe, configuration, certificate, 2, intent.CscId, secrets.Text(intent.CscRef!))
                : NfceDocuments.Sign(nfe, configuration, certificate, 3);
            key = NfceDocuments.AccessKey(nfe);
        }
        catch (FiscalDataException error)
        {
            // Nada foi transmitido: o pedido é solto, e a retaguarda mostra o motivo.
            return EngineOutcome.Open("INVALID_FISCAL_DATA", $"{error.Message} Nada foi transmitido.");
        }
        catch (SchemaException error)
        {
            _log($"XML de {intent.RequestUuid} reprovado no schema: {error.Message}");
            return EngineOutcome.Open("XML_INVALID", "O XML da NFC-e foi reprovado no schema oficial; nada foi transmitido.");
        }

        prepare(key, signed, JsonSerializer.Serialize(new Context(intent.CertificateRef, intent.Environment, uf.ToString())));
        var target = new SefazTarget(uf, intent.IsProduction, certificate);
        return await Transmit(target, key, signed, mayQuery: true, cancellation);
    }

    public async Task<EngineOutcome> ReconcileAsync(PendingRequest pending, CancellationToken cancellation)
    {
        var context = JsonSerializer.Deserialize<Context>(pending.ContextJson ?? "null")
            ?? throw new InvalidOperationException("Pedido assinado sem contexto.");
        using var certificate = Certificate(context.CertificateRef);
        var target = new SefazTarget(Enum.Parse<Estado>(context.Uf), context.Environment == "production", certificate);
        return await Resolve(target, pending.AccessKey!, pending.SignedXml!, mayRetransmit: true, cancellation);
    }

    private async Task<EngineOutcome> Transmit(SefazTarget target, string key, string signed, bool mayQuery, CancellationToken cancellation)
    {
        SefazAnswer answer;
        try
        {
            answer = await sefaz.AuthorizeAsync(target, signed, cancellation);
        }
        catch (SefazCertificateRefusedException error) when (mayQuery)
        {
            // Barrado na porta: nada foi processado. Descartar o XML assinado com
            // este certificado evita retransmiti-lo depois que a loja trocar o A1.
            return EngineOutcome.NotSent("FISCAL_SETUP",
                $"A SEFAZ recusou o certificado A1 (HTTP {error.Status}): confira se é ICP-Brasil, do CNPJ " +
                "do emitente e dentro da validade. Nada foi autorizado; a venda pode ser retransmitida.");
        }
        catch (Exception error) when (error is SefazUnreachableException or SefazCertificateRefusedException)
        {
            return EngineOutcome.Open("SEFAZ_UNREACHABLE",
                "A SEFAZ não respondeu; a nota pode ter sido autorizada. Consulte antes de reenviar.");
        }

        if (answer.DocumentStatus is { } status)
        {
            if (AuthorizedCodes.Contains(status) && answer.ProtocolXml is not null) return Authorized(key, signed, answer);
            if (DuplicateCodes.Contains(status))
            {
                // A SEFAZ já tem uma nota com esta chave: a tentativa anterior chegou.
                // Só a consulta diz com que protocolo — nunca se presume autorizada.
                return mayQuery
                    ? await Resolve(target, key, signed, mayRetransmit: false, cancellation)
                    : EngineOutcome.Open("IN_FLIGHT", $"Duplicidade na retransmissão: {answer.DocumentReason}");
            }
            return EngineOutcome.Settled(new FiscalResult(FiscalResult.Rejected, status.ToString(), answer.DocumentReason ?? "", key));
        }
        if (TransientLotCodes.Contains(answer.Status))
        {
            return EngineOutcome.Open("SEFAZ_PROCESSING", $"SEFAZ respondeu {answer.Status}: {answer.Reason}");
        }
        return EngineOutcome.Settled(new FiscalResult(FiscalResult.Rejected, answer.Status.ToString(), answer.Reason, key));
    }

    /// <summary>
    /// Pergunta à SEFAZ pela chave e decide. <paramref name="mayRetransmit"/>
    /// limita a um só ciclo consulta → retransmissão → consulta.
    /// </summary>
    private async Task<EngineOutcome> Resolve(SefazTarget target, string key, string signed, bool mayRetransmit, CancellationToken cancellation)
    {
        SefazAnswer answer;
        try
        {
            answer = await sefaz.QueryAsync(target, key, cancellation);
        }
        catch (Exception error) when (error is SefazUnreachableException or SefazCertificateRefusedException)
        {
            // Na consulta, a recusa do certificado não prova nada sobre a tentativa
            // anterior, que pode ter passado com o certificado de antes.
            return EngineOutcome.Open("IN_FLIGHT", "SEFAZ inacessível na consulta; o resultado segue desconhecido.");
        }

        if (AuthorizedCodes.Contains(answer.Status) && answer.ProtocolXml is not null) return Authorized(key, signed, answer);
        if (answer.Status == 217 && mayRetransmit)
        {
            // A SEFAZ não tem esta chave: a nota nunca chegou. Retransmitir o MESMO
            // XML é seguro — se a primeira tentativa aparecer, é duplicidade da mesma chave.
            return await Transmit(target, key, signed, mayQuery: false, cancellation);
        }
        if (DeniedCodes.Contains(answer.Status))
        {
            return EngineOutcome.Settled(new FiscalResult(FiscalResult.Rejected, answer.Status.ToString(), answer.Reason, key));
        }
        return EngineOutcome.Open("IN_FLIGHT", $"Consulta pela chave respondeu {answer.Status}: {answer.Reason}");
    }

    private static EngineOutcome Authorized(string key, string signed, SefazAnswer answer) =>
        EngineOutcome.Settled(new FiscalResult(
            FiscalResult.Authorized,
            (answer.DocumentStatus ?? answer.Status).ToString(),
            answer.DocumentReason ?? answer.Reason,
            key,
            answer.Protocol,
            NfceDocuments.Processed(signed, answer.ProtocolXml!)));
}
