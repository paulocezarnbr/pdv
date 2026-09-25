using Microsoft.Data.Sqlite;
using Pdv.Core;

namespace Pdv.Data.Sync;

/// <summary>
/// O ciclo push/pull — o <c>SyncEngine</c> do Python, sem threads: toda a
/// concorrência mora no <see cref="SyncWorker"/>, e o que acontece quando a
/// rede cai entre o commit da nuvem e a resposta é testável sem corrida.
/// </summary>
/// <remarks>
/// A regra: o cliente NUNCA decide que algo foi sincronizado; só o veredito
/// da nuvem decide, e na dúvida o lote sobe de novo. Reenviar é seguro porque
/// <c>client_uuid</c> é chave de idempotência lá; não reenviar seria perda.
/// </remarks>
public sealed class SyncEngine(
    PdvDatabase database,
    ISyncTransport transport,
    TerminalProfile terminal,
    TimeProvider? clock = null,
    int batchSize = 200,
    Action<string>? log = null)
{
    /// <summary>Desvio de relógio que vira aviso (o painel usa o mesmo).</summary>
    public const long ClockSkewWarningMs = 120_000;

    private readonly OutboxReader _reader = new(database, clock);
    private readonly CursorStore _cursors = new(database, clock);
    private readonly Action<string> _log = log ?? (_ => { });

    /// <summary>Envia um lote.</summary>
    public async Task<SyncReport> PushOnceAsync(CancellationToken cancellation = default)
    {
        var items = _reader.ClaimBatch(batchSize);
        if (items.Count == 0) return new SyncReport();

        var batch = new PushBatch(terminal.DeviceId, terminal.TenantId, terminal.StoreId, items);
        IReadOnlyList<ItemAck> answers;
        try
        {
            answers = await transport.PushAsync(batch, cancellation);
        }
        catch (AuthException error)
        {
            // Credencial revogada: reenviar não resolve, mas os dados não podem
            // ser descartados. Ficam na fila até alguém reativar o terminal.
            _log($"Sincronização bloqueada por autenticação: {error.Message}");
            _reader.Defer(items, $"auth: {error.Message}");
            return new SyncReport(items.Count, Deferred: items.Count, Error: $"auth: {error.Message}");
        }
        catch (TransportException error)
        {
            // O caso perigoso: a nuvem PODE ter aplicado e a resposta se perdido.
            // Nada sai da fila; o reenvio recebe `duplicate` e fecha o ciclo.
            _log($"Falha de transporte, lote volta à fila: {error.Message}");
            _reader.Defer(items, error.Message);
            return new SyncReport(items.Count, Deferred: items.Count, Error: error.Message);
        }

        var acks = new Dictionary<string, ItemAck>(StringComparer.Ordinal);
        foreach (var ack in answers) acks[ack.ClientUuid] = ack;

        var settled = _reader.Settle(items, acks);

        var rejected = items
            .Where(item => acks.TryGetValue(item.ClientUuid, out var ack) && ack.Status == ItemStatus.Rejected)
            .ToList();
        if (rejected.Count > 0)
        {
            // Veredito da nuvem: o mesmo payload teria o mesmo resultado. Sai do
            // caminho da fila e espera gente — mas não é apagado.
            var messages = string.Join("; ", rejected.Select(item =>
                $"{item.EntityTable}#{item.ClientUuid[..Math.Min(8, item.ClientUuid.Length)]}: " +
                (acks[item.ClientUuid].Message ?? "None")));
            _log($"Itens rejeitados pelo servidor: {messages}");
            _reader.Quarantine(rejected, messages);
        }

        // Silêncio nunca é sucesso: item sem veredito volta à fila.
        var unanswered = items.Where(item => !acks.ContainsKey(item.ClientUuid)).ToList();
        if (unanswered.Count > 0)
        {
            _log($"{unanswered.Count} item(ns) sem resposta do servidor");
            _reader.Defer(unanswered, "sem veredito do servidor");
        }

        return new SyncReport(items.Count, settled, rejected.Count, unanswered.Count);
    }

    /// <summary>
    /// Lotes até esvaziar, falhar ou bater o teto — o teto devolve o controle ao
    /// worker quando um lote sempre falha.
    /// </summary>
    public async Task<SyncReport> DrainAsync(int maxCycles = 100, CancellationToken cancellation = default)
    {
        var total = new SyncReport();
        for (var cycle = 0; cycle < maxCycles; cycle++)
        {
            var report = await PushOnceAsync(cancellation);
            total = new SyncReport(
                total.Sent + report.Sent, total.Settled + report.Settled, total.Rejected + report.Rejected,
                total.Deferred + report.Deferred, report.Error ?? total.Error);
            if (report.Sent == 0 || report.Error is not null) break;
        }
        return total;
    }

    /// <summary>
    /// Baixa o cadastro alterado na retaguarda. LWW por <c>updated_at</c>:
    /// aplicar duas vezes é inofensivo, então basta um cursor por tabela.
    /// </summary>
    /// <returns>Quantas linhas foram aplicadas.</returns>
    public async Task<int> PullOnceAsync(CancellationToken cancellation = default)
    {
        var applied = 0;
        foreach (var table in PullMapping.PullableTables)
        {
            // Não pedir o que não se vai aplicar: avançar o cursor sem aplicar
            // perderia essas linhas quando o caixa aprender a aplicá-las.
            if (PullMapping.NotApplied.ContainsKey(table)) continue;

            PullResponse response;
            try
            {
                response = await transport.PullAsync(
                    new PullRequest(terminal.TenantId, terminal.StoreId, table, _cursors.Get(table)), cancellation);
            }
            catch (SyncException error)
            {
                _log($"Pull de {table} falhou: {error.Message}");
                break;
            }
            if (response.Rows.Count == 0) continue;

            try
            {
                applied += ApplyPulledRows(table, response.Rows);
            }
            catch (SqliteException error)
            {
                // Uma tabela que não aplica não impede as outras; o cursor fica,
                // e a próxima tentativa pega as mesmas linhas.
                _log($"Pull de {table} não pôde ser aplicado: {error.Message}");
                continue;
            }
            _cursors.Set(table, response.LastServerSeq);
        }
        return applied;
    }

    private int ApplyPulledRows(string table, IReadOnlyList<System.Text.Json.JsonElement> rows)
    {
        if (!PullMapping.IsMapped(table)) return 0;
        return database.InTransaction(transaction =>
        {
            var applied = 0;
            foreach (var raw in rows)
            {
                var row = PullMapping.MapRow(table, raw, terminal.TenantId, terminal.StoreId);
                if (row is null)
                {
                    _log($"Linha de {table} descartada no pull (incompleta ou de outro tenant)");
                    continue;
                }
                // Colunas e tabela vêm do mapeamento, nunca da resposta.
                var columns = row.Keys.OrderBy(name => name, StringComparer.Ordinal).ToList();
                var updates = string.Join(", ", columns.Where(c => c != "id").Select(c => $"{c} = excluded.{c}"));
                using var command = transaction.Command(
                    $"INSERT INTO {table} ({string.Join(", ", columns)}) " +
                    $"VALUES ({string.Join(", ", columns.Select((_, i) => $"$p{i}"))}) " +
                    $"ON CONFLICT (id) DO UPDATE SET {updates}",
                    columns.Select((c, i) => ($"$p{i}", (object?)row[c])).ToArray());
                command.ExecuteNonQuery();
                applied++;
            }
            return applied;
        });
    }

    /// <summary>
    /// Conta à nuvem como a fila está — mesmo quando o envio falhou, que é
    /// quando mais importa. Falha aqui não é erro de venda.
    /// </summary>
    /// <returns>O desvio do relógio em ms, ou <c>null</c> se não foi possível relatar.</returns>
    public async Task<long?> HeartbeatAsync(CancellationToken cancellation = default)
    {
        var (live, dead, oldest, reason) = _reader.Health();
        var health = new TerminalHealth(
            terminal.DeviceId, terminal.TenantId, Iso.Now(clock), live, dead, oldest,
            reason is null ? null : string.Concat(reason.EnumerateRunes().Take(300).Select(rune => rune.ToString())));
        long drift;
        try
        {
            drift = await transport.HeartbeatAsync(health, cancellation);
        }
        catch (SyncException error)
        {
            _log($"Relato de saúde não enviado: {error.Message}");
            return null;
        }
        if (Math.Abs(drift) > ClockSkewWarningMs)
        {
            _log($"Relógio do caixa {drift / 1000:+0;-0} s em relação à nuvem: a hora das vendas sai errada nos relatórios.");
        }
        return drift;
    }

    public long PendingCount() => _reader.PendingCount();

    public long QuarantinedCount() => _reader.DeadLetterCount();
}
