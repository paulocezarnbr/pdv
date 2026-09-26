using System.Globalization;
using Pdv.Core.Printing;

namespace Pdv.Data.Fiscal;

public enum FiscalOutcomeKind
{
    /// <summary>Nota autorizada: sai o DANFE no lugar do cupom.</summary>
    Authorized,

    /// <summary>Total zero: venda concluída, sem nota. Sai o cupom.</summary>
    NotRequired,

    /// <summary>Ainda sem decisão: sai o cupom, e a nota é consultada em segundo plano.</summary>
    Pending,

    /// <summary>A SEFAZ ou a retaguarda recusou de vez. Sai o cupom, e alguém precisa agir.</summary>
    Rejected,
}

/// <param name="Danfe">Só com <see cref="FiscalOutcomeKind.Authorized"/> e XML legível.</param>
/// <param name="Notice">O que o operador lê. Nunca descreve a infraestrutura.</param>
public sealed record FiscalOutcome(string OrderId, FiscalOutcomeKind Kind, NfceDanfe? Danfe, string Notice);

/// <summary>
/// A NFC-e pedida pelo caixa à retaguarda, depois da venda fechada.
/// </summary>
/// <remarks>
/// <para>
/// <b>Um <c>request_uuid</c> por venda, para sempre.</b> Ele é a chave de
/// idempotência da retaguarda: pedir de novo com o MESMO uuid devolve a mesma
/// nota, nunca uma segunda. Por isso ele nasce gravado em
/// <c>fiscal_requests</c> antes do primeiro envio e sobrevive a queda de
/// energia — um uuid novo depois de um timeout seria a nota em duplicidade.
/// </para>
/// <para>
/// Sem conexão nunca vira contingência aqui: a contingência offline exige o A1
/// em cada caixa e é decisão do dono (ver <c>docs/handoff_csharp.md</c>). Até
/// lá, sem conexão o cupom sai e a nota é pedida de novo quando a rede voltar.
/// </para>
/// <para>
/// Tabela do C#, com <c>CREATE TABLE IF NOT EXISTS</c> e sem mexer no
/// <c>user_version</c> (regra 1 do porte). Não sobe para a nuvem: lá a nota
/// já está em <c>fiscal_documents</c>, que é a verdade.
/// </para>
/// </remarks>
public sealed class FiscalIssuance
{
    public const string EnabledKey = "fiscal.enabled";

    /// <summary>No balcão o cliente espera: o pedido tem prazo curto e o resto fica para o segundo plano.</summary>
    public static readonly TimeSpan CounterDeadline = TimeSpan.FromSeconds(8);

    private static readonly TimeSpan FirstRetry = TimeSpan.FromSeconds(15);
    private static readonly TimeSpan MaxRetry = TimeSpan.FromHours(1);

    private static readonly HashSet<string> Open = ["pending", "unknown", "processing"];

    private readonly PdvDatabase _database;
    private readonly IFiscalGateway _gateway;
    private readonly TimeZoneInfo _local;
    private readonly TimeProvider _clock;
    private readonly Action<string> _log;

    public FiscalIssuance(
        PdvDatabase database, IFiscalGateway gateway, TimeZoneInfo? local = null,
        TimeProvider? clock = null, Action<string>? log = null)
    {
        _database = database;
        _gateway = gateway;
        _local = local ?? TimeZoneInfo.Local;
        _clock = clock ?? TimeProvider.System;
        _log = log ?? (_ => { });
        EnsureSchema(database);
    }

    /// <summary>
    /// Ligado só com o terminal ativado e <c>fiscal.enabled = 1</c>. Fica
    /// desligado até a homologação na SEFAZ-RJ: o padrão é o cupom.
    /// </summary>
    public static bool IsEnabled(PdvDatabase database, TerminalProfile profile) =>
        profile.Activated && profile.CloudBaseUrl is { Length: > 0 } &&
        database.Scalar("SELECT value FROM device_settings WHERE key = $key", ("$key", EnabledKey)) as string == "1";

    public static void EnsureSchema(PdvDatabase database) => database.Execute(
        """
        CREATE TABLE IF NOT EXISTS fiscal_requests (
            order_id      TEXT PRIMARY KEY REFERENCES orders(id),
            request_uuid  TEXT NOT NULL UNIQUE,
            status        TEXT NOT NULL CHECK (status IN
                ('pending','unknown','processing','authorized','rejected','canceled','not_required')),
            series        INTEGER,
            number        INTEGER,
            access_key    TEXT,
            protocol      TEXT,
            processed_xml TEXT,
            reason        TEXT,
            attempts      INTEGER NOT NULL DEFAULT 0,
            next_check_at TEXT,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_fiscal_requests_due ON fiscal_requests(status, next_check_at);
        """);

    /// <summary>
    /// O pedido do balcão, logo depois de a venda fechar.
    /// </summary>
    /// <param name="pushOutbox">
    /// Empurra a fila de sincronização: a retaguarda só emite nota de venda que
    /// ela já recebeu. Falhar aqui não impede o pedido — ele volta 404 e fica
    /// para o segundo plano.
    /// </param>
    public async Task<FiscalOutcome> RequestAsync(
        string orderId, Func<CancellationToken, Task>? pushOutbox = null, CancellationToken cancellation = default)
    {
        var total = _database.Scalar(
            "SELECT total_cents FROM orders WHERE id = $id AND status = 'paid'", ("$id", orderId));
        if (total is null) throw new InvalidOperationException($"A venda {orderId} não está paga; não se pede nota.");

        var request = Ensure(orderId);
        if (!Open.Contains(request.Status)) return Outcome(orderId);

        // Antes da rede: a cortesia de 100% não pede nota, com ou sem conexão.
        if (Convert.ToInt64(total, CultureInfo.InvariantCulture) == 0)
        {
            Finish(orderId, "not_required", reason: "Venda com total zero: não se emite NFC-e.");
            return Outcome(orderId);
        }

        if (pushOutbox is not null)
        {
            try
            {
                await pushOutbox(cancellation);
            }
            catch (Exception error) when (error is not OperationCanceledException || !cancellation.IsCancellationRequested)
            {
                _log($"fiscal: fila não empurrada antes do pedido da nota: {error.Message}");
            }
        }

        await AttemptAsync(orderId, request.RequestUuid, request.Status, cancellation);
        return Outcome(orderId);
    }

    /// <summary>
    /// O segundo plano: pede de novo o que não saiu e consulta o que ficou sem
    /// resposta. Devolve as vendas que chegaram a uma decisão neste ciclo.
    /// </summary>
    public async Task<IReadOnlyList<FiscalOutcome>> CheckDueAsync(int limit = 20, CancellationToken cancellation = default)
    {
        var due = new List<(string OrderId, string RequestUuid, string Status)>();
        using (var command = Sql.Command(_database.Connection, null,
                   "SELECT order_id, request_uuid, status FROM fiscal_requests " +
                   "WHERE status IN ('pending','unknown','processing') AND (next_check_at IS NULL OR next_check_at <= $now) " +
                   "ORDER BY next_check_at LIMIT $limit",
                   ("$now", Now()), ("$limit", limit)))
        using (var reader = command.ExecuteReader())
        {
            while (reader.Read()) due.Add((reader.GetString(0), reader.GetString(1), reader.GetString(2)));
        }

        var decided = new List<FiscalOutcome>();
        foreach (var (orderId, requestUuid, status) in due)
        {
            cancellation.ThrowIfCancellationRequested();
            await AttemptAsync(orderId, requestUuid, status, cancellation);
            var outcome = Outcome(orderId);
            if (outcome.Kind != FiscalOutcomeKind.Pending) decided.Add(outcome);
        }
        return decided;
    }

    /// <summary>O DANFE de uma venda já autorizada — a reimpressão.</summary>
    public FiscalOutcome Outcome(string orderId)
    {
        using var command = Sql.Command(_database.Connection, null,
            "SELECT status, processed_xml, reason FROM fiscal_requests WHERE order_id = $id", ("$id", orderId));
        using var reader = command.ExecuteReader();
        if (!reader.Read()) return new FiscalOutcome(orderId, FiscalOutcomeKind.Pending, null, "A nota desta venda ainda não foi pedida.");
        var status = reader.GetString(0);
        var xml = reader.IsDBNull(1) ? null : reader.GetString(1);
        var reason = reader.IsDBNull(2) ? null : reader.GetString(2);

        switch (status)
        {
            case "authorized":
                try
                {
                    return new FiscalOutcome(orderId, FiscalOutcomeKind.Authorized, NfceProcReader.Read(xml ?? "", _local), "NFC-e autorizada.");
                }
                catch (DanfeException error)
                {
                    // A nota existe e vale; só o papel dela não se monta. O
                    // cupom sai, e o motivo vai para quem corrige.
                    _log($"fiscal: nota autorizada da venda {orderId} sem DANFE: {error.Message}");
                    return new FiscalOutcome(orderId, FiscalOutcomeKind.Rejected, null,
                        $"A NFC-e foi autorizada, mas o DANFE não pôde ser montado: {error.Message}");
                }
            case "not_required":
                return new FiscalOutcome(orderId, FiscalOutcomeKind.NotRequired, null, "Venda com total zero: sem NFC-e.");
            case "rejected" or "canceled":
                return new FiscalOutcome(orderId, FiscalOutcomeKind.Rejected, null,
                    $"A NFC-e não foi autorizada{(reason is null ? "" : $": {reason}")}. Avise o responsável pelo fiscal.");
            default:
                return new FiscalOutcome(orderId, FiscalOutcomeKind.Pending, null,
                    "A NFC-e ainda não foi autorizada. O cupom foi impresso; a nota é consultada sozinha e o DANFE sai na reimpressão.");
        }
    }

    // -- miolo ----------------------------------------------------------------

    private (string RequestUuid, string Status) Ensure(string orderId)
    {
        var now = Now();
        _database.Execute(
            "INSERT OR IGNORE INTO fiscal_requests (order_id, request_uuid, status, created_at, updated_at) " +
            "VALUES ($order, $uuid, 'pending', $now, $now)",
            ("$order", orderId), ("$uuid", Guid.NewGuid().ToString()), ("$now", now));
        using var command = Sql.Command(_database.Connection, null,
            "SELECT request_uuid, status FROM fiscal_requests WHERE order_id = $id", ("$id", orderId));
        using var reader = command.ExecuteReader();
        reader.Read();
        return (reader.GetString(0), reader.GetString(1));
    }

    /// <summary>
    /// Uma tentativa. <c>pending</c> pede; <c>unknown</c>/<c>processing</c>
    /// consultam — e só pedem se a retaguarda disser que nunca viu o uuid, o
    /// que é seguro pelo mesmo motivo de sempre: o uuid é o mesmo.
    /// </summary>
    private async Task AttemptAsync(string orderId, string requestUuid, string status, CancellationToken cancellation)
    {
        try
        {
            CloudFiscalDocument document;
            if (status == "pending")
            {
                document = await _gateway.IssueAsync(requestUuid, orderId, cancellation);
            }
            else
            {
                try
                {
                    document = await _gateway.StatusAsync(requestUuid, cancellation);
                }
                catch (FiscalRefusedException refused) when (refused.Status == 404)
                {
                    document = await _gateway.IssueAsync(requestUuid, orderId, cancellation);
                }
            }
            Apply(orderId, requestUuid, document);
        }
        catch (FiscalOfflineException error)
        {
            Retry(orderId, status, error.Message);
        }
        catch (FiscalResultUnknownException error)
        {
            // Pode ter sido emitida: daqui em diante, só consulta.
            Retry(orderId, "unknown", error.Message);
        }
        catch (FiscalRefusedException error)
        {
            // Venda que ainda não sincronizou, configuração incompleta: pede de
            // novo mais tarde, com espera crescente. Recusa não é "desconhecido".
            Retry(orderId, status == "pending" ? "pending" : status, error.Message);
        }
        catch (FiscalAuthException error)
        {
            Retry(orderId, status, error.Message);
        }
    }

    private void Apply(string orderId, string requestUuid, CloudFiscalDocument document)
    {
        // Resposta de outro pedido não decide este — trata como sem resposta.
        if (document.RequestUuid != requestUuid || document.OrderId != orderId)
        {
            Retry(orderId, "unknown", "A retaguarda respondeu por outro pedido.");
            return;
        }

        switch (document.Status)
        {
            case "authorized":
                if (string.IsNullOrEmpty(document.ProcessedXml))
                {
                    // Nota autorizada sem o XML: ainda não dá para imprimir. Consulta de novo.
                    Retry(orderId, "processing", "Nota autorizada; aguardando o XML.");
                    return;
                }
                Finish(orderId, "authorized", document);
                break;
            case "rejected" or "canceled":
                Finish(orderId, document.Status, document, document.Reason);
                break;
            case "not_required":
                Finish(orderId, "not_required", document, document.Reason);
                break;
            default:
                Retry(orderId, document.Status == "processing" ? "processing" : "unknown", document.Reason);
                break;
        }
    }

    private void Finish(string orderId, string status, CloudFiscalDocument? document = null, string? reason = null) =>
        _database.Execute(
            "UPDATE fiscal_requests SET status = $status, series = $series, number = $number, access_key = $key, " +
            "protocol = $protocol, processed_xml = $xml, reason = $reason, attempts = attempts + 1, next_check_at = NULL, " +
            "updated_at = $now WHERE order_id = $order AND status IN ('pending','unknown','processing')",
            ("$status", status), ("$series", document?.Series), ("$number", document?.Number),
            ("$key", document?.AccessKey), ("$protocol", document?.Protocol), ("$xml", document?.ProcessedXml),
            ("$reason", reason), ("$now", Now()), ("$order", orderId));

    private void Retry(string orderId, string status, string? reason)
    {
        var attempts = Convert.ToInt32(_database.Scalar(
            "SELECT attempts FROM fiscal_requests WHERE order_id = $id", ("$id", orderId)) ?? 0, CultureInfo.InvariantCulture);
        var wait = TimeSpan.FromTicks(Math.Min(MaxRetry.Ticks, FirstRetry.Ticks * (1L << Math.Min(attempts, 20))));
        _database.Execute(
            "UPDATE fiscal_requests SET status = $status, reason = $reason, attempts = attempts + 1, next_check_at = $next, " +
            "updated_at = $now WHERE order_id = $order AND status IN ('pending','unknown','processing')",
            ("$status", status), ("$reason", reason), ("$next", Iso(_clock.GetUtcNow() + wait)), ("$now", Now()),
            ("$order", orderId));
    }

    private string Now() => Iso(_clock.GetUtcNow());

    private static string Iso(DateTimeOffset moment) =>
        moment.UtcDateTime.ToString("yyyy-MM-dd'T'HH:mm:ss.fff'+00:00'", CultureInfo.InvariantCulture);
}
