// Um turno curto de loja com o PDV em C#, contra a retaguarda de verdade.
//
// Nada de payload escrito à mão: é o código do PDV — ativação, registro de
// item, fechamento com TEF, outbox, SyncEngine e HttpSyncTransport — falando
// HTTP com a nuvem. O que se confere depois é o veredito dela, item por item.
//
// Uso:
//   dotnet run --project tools/CloudE2E -- <endereço da retaguarda> <código de ativação> [pasta]
//
// Sai com 0 só se todo item subiu aplicado, nenhum foi recusado e a fila esvaziou.

using Microsoft.Data.Sqlite;
using Pdv.Core;
using Pdv.Core.Tef;
using Pdv.Data;
using Pdv.Data.Provisioning;
using Pdv.Data.Sales;
using Pdv.Data.Secrets;
using Pdv.Data.Sync;

if (args.Length < 2)
{
    Console.Error.WriteLine("uso: CloudE2E <endereço da retaguarda> <código de ativação> [pasta de trabalho]");
    return 2;
}

var server = Activation.NormalizeServerUrl(args[0]);
var work = args.Length > 2 ? args[2] : Path.Combine(Path.GetTempPath(), "pdv-e2e-" + Guid.NewGuid().ToString("N")[..8]);
Directory.CreateDirectory(work);
var path = Path.Combine(work, "pdv_local.db");
var failures = 0;

void Check(string what, bool ok, string detail = "")
{
    Console.WriteLine($"{(ok ? "ok    " : "FALHOU")}  {what}{(ok || detail.Length == 0 ? "" : $": {detail}")}");
    if (!ok) failures++;
}

// Banco novo com o schema do contrato: o mesmo que o migrate() produz.
var schema = new DirectoryInfo(AppContext.BaseDirectory);
while (schema is not null && !File.Exists(Path.Combine(schema.FullName, "contracts", "pdv-schema.sql"))) schema = schema.Parent;
if (schema is null) throw new FileNotFoundException("contracts/pdv-schema.sql não encontrado acima do executável");
using (var create = new SqliteConnection($"Data Source={path};Pooling=False"))
{
    create.Open();
    using var command = create.CreateCommand();
    command.CommandText = File.ReadAllText(Path.Combine(schema.FullName, "contracts", "pdv-schema.sql")) +
                          $";PRAGMA user_version = {PdvDatabase.SupportedSchemaVersion};";
    command.ExecuteNonQuery();
}

using var database = new PdvDatabase(path);
var vault = new SecretVault(Path.Combine(work, "secrets"));

// 1. ativação
var granted = await Activation.ActivateAsync(args[1], database, vault, new HttpActivationTransport(server));
var profile = TerminalProfile.Load(database);
Check($"ativado para '{granted.StoreName}' (terminal {granted.DeviceId[..8]}…)", profile.Activated);

// 2. catálogo local e duas vendas: dinheiro e débito pelo TEF simulado
database.Execute(
    "INSERT INTO products (id, tenant_id, store_id, sku, barcode, name, category, pricing_mode, price_cents, is_active, updated_at) " +
    "VALUES ($id, $t, $s, 'E2E-TORTA', '7890000000011', 'Fatia de torta', 'Doces', 'unit', 1450, 1, $now)",
    ("$id", Iso.NewId()), ("$t", profile.TenantId), ("$s", profile.StoreId), ("$now", Iso.Now()));
var catalog = new Catalog(database.Connection, profile.TenantId);
var ledger = new AuditLedger(profile.TenantId, profile.StoreId, profile.DeviceId, vault.EnsureDeviceSecret());
var items = new ItemRegistration(database, profile.Identity, ledger);
using var journal = new SqliteTefJournal(path);
var checkout = new Checkout(database, profile.Identity, ledger, new TefCoordinator(new TefSimulator(), journal));
var operatorId = Iso.NewId();
var torta = catalog.FindByBarcode("7890000000011")!;

var cash = items.RegisterUnitItem(null, torta, 2m, operatorId).Order;
var paidCash = await checkout.CloseAsync(cash.Id, operatorId, [PaymentIntent.Cash(5000)], new Silent());
Check("venda 1 em dinheiro (R$ 29,00, troco R$ 21,00)", paidCash is CheckoutResult.Closed { AllConfirmed: true }, paidCash.ToString()!);

var card = items.RegisterUnitItem(null, torta, 1m, operatorId).Order;
var paidCard = await checkout.CloseAsync(card.Id, operatorId, [PaymentIntent.Card(TefCardType.Debit, 1450)], new Silent());
Check("venda 2 no débito pelo TEF", paidCard is CheckoutResult.Closed { AllConfirmed: true }, paidCard.ToString()!);

// 3. sincronização pelo HTTP de verdade
var queued = new OutboxReader(database).PendingCount();
var token = System.Text.Encoding.UTF8.GetString(vault.Load(Activation.SyncTokenName)!);
using var transport = new HttpSyncTransport(profile.CloudBaseUrl ?? server, token);
var log = new List<string>();
var engine = new SyncEngine(database, transport, profile, log: log.Add);
var report = await engine.DrainAsync();
Check($"{queued} item(ns) na fila, {report.Settled} aplicado(s) pela nuvem",
    report.Settled == queued && report.Rejected == 0 && report.Error is null,
    $"{report} {string.Join(" | ", log)}");
Check("fila vazia depois do envio", engine.PendingCount() == 0, $"{engine.PendingCount()} pendente(s)");

// 4. o reenvio do mesmo conteúdo não duplica (a nuvem guarda a resposta do lote)
var again = await engine.DrainAsync();
Check("nada a reenviar", again.Sent == 0, again.ToString());

// 5. saúde e cadastro
var drift = await engine.HeartbeatAsync();
Check("relato de saúde aceito", drift is not null, string.Join(" | ", log));
var pulled = await engine.PullOnceAsync();
Check($"pull de cadastro respondeu ({pulled} linha(s) aplicada(s))", !log.Any(line => line.StartsWith("Pull de", StringComparison.Ordinal)), string.Join(" | ", log));

Console.WriteLine(failures == 0 ? $"Turno de loja em C#: ok ({work})" : $"{failures} falha(s) ({work})");
return failures == 0 ? 0 : 1;

sealed class Silent : ITefInteraction
{
    public void Show(string message) => Console.WriteLine($"        TEF: {message}");

    public Task<int?> ChooseAsync(string title, IReadOnlyList<string> options, CancellationToken cancellationToken) =>
        Task.FromResult<int?>(0);

    public Task<string?> AskAsync(string prompt, CancellationToken cancellationToken) => Task.FromResult<string?>("");
}
