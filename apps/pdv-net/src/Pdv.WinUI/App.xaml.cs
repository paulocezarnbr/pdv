using System.Diagnostics;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Pdv.App;
using Pdv.Data;
using Pdv.Core.Tef;
using Pdv.Data.Auth;
using Pdv.Data.Edge;
using Pdv.Data.Fiscal;
using Pdv.Data.Provisioning;
using Pdv.Data.Sales;
using Pdv.Data.Secrets;
using Pdv.Data.Sync;
using Pdv.Edge;

namespace Pdv.WinUI;

/// <summary>
/// Composição do caixa: abre o banco, lê quem é o terminal, entra pelo login.
/// </summary>
/// <remarks>
/// Toda falha de abertura vira uma mensagem na janela e uma linha no
/// <see cref="CrashLog"/>, nunca um fechamento mudo: o executável não tem
/// console, e "abri e sumiu" é o pior chamado de suporte que existe.
/// </remarks>
public partial class App : Application
{
    private MainWindow? _window;
    private PdvDatabase? _database;
    private SyncStatusViewModel _sync = new() { Disabled = true };

    /// <summary>A venda na tela, para o ciclo de sincronização avisar do que o painel mudou.</summary>
    private SaleViewModel? _sale;
    private CancellationTokenSource? _syncStop;

    /// <summary>O ciclo de sincronização, para o pedido de NFC-e empurrar a fila antes de pedir.</summary>
    private SyncWorker? _syncWorker;

    /// <summary>O token do terminal, lido do cofre uma vez na abertura.</summary>
    private string? _deviceToken;

    /// <summary>O servidor do salão: um por execução do app, não por abertura de caixa.</summary>
    private SalonServer? _salon;

    /// <summary>O certificado em uso, para o painel do salão mostrar a digital (C6f).</summary>
    private TlsMaterial? _salonTls;

    public App()
    {
        InitializeComponent();
        UnhandledException += (_, e) => CrashLog.Write("erro não tratado na interface", e.Exception);
        AppDomain.CurrentDomain.UnhandledException +=
            (_, e) => CrashLog.Write("erro não tratado", e.ExceptionObject as Exception);
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        _window = new MainWindow();
        try
        {
            var path = TerminalProfile.DefaultDatabasePath();
            // Ativação feita na abertura anterior: a demonstração é arquivada e
            // o banco da loja assume, antes de qualquer conexão.
            if (StagedActivation.Promote(path) is { } archived)
            {
                CrashLog.Write($"ativação concluída; demonstração arquivada em {archived}", null);
            }
            _database = new PdvDatabase(path);
            StartSync(path, TerminalProfile.Load(_database));
            ShowLogin(path, _database);
        }
        catch (Exception error) when (error is PdvDatabaseException or StagedActivationException)
        {
            CrashLog.Write("abertura do banco", error);
            _window.ShowFatal(error.Message);
        }
        catch (Exception error)
        {
            CrashLog.Write("abertura do caixa", error);
            _window.ShowFatal(
                $"Erro inesperado ao abrir o caixa: {error.Message}\n\nDetalhes em {CrashLog.Path}. " +
                "Nenhuma venda foi perdida.");
        }
        _window.Activate();
    }

    /// <summary>
    /// Sincronização desde a abertura, antes do login: a venda de ontem que
    /// ficou na fila não espera alguém entrar no caixa para subir.
    /// </summary>
    /// <remarks>
    /// Conexão própria ao banco (o WAL deixa ler e escrever em paralelo, com um
    /// escritor por vez) e laço fora da thread da tela. Terminal em
    /// demonstração não sincroniza: não há token nem loja.
    /// </remarks>
    private void StartSync(string path, TerminalProfile profile)
    {
        if (!profile.Activated || string.IsNullOrEmpty(profile.CloudBaseUrl)) return;
        byte[]? token;
        try
        {
            token = new SecretVault(Path.Combine(Path.GetDirectoryName(path)!, "secrets")).Load(Activation.SyncTokenName);
        }
        catch (SecretVaultException error)
        {
            CrashLog.Write("token de sincronização", error);
            token = null;
        }
        if (token is null)
        {
            _sync = new SyncStatusViewModel();
            _sync.Update(online: false);
            CrashLog.Write("terminal ativado sem token de sincronização no cofre: reative o terminal", null);
            return;
        }

        _sync = new SyncStatusViewModel();
        var database = new PdvDatabase(path);
        var transport = new HttpSyncTransport(profile.CloudBaseUrl, System.Text.Encoding.UTF8.GetString(token));
        var ui = _window!.DispatcherQueue;
        var commands = RemoteCommands(path, database, profile);
        if (commands is not null)
        {
            // O ciclo roda fora da tela; a venda aberta relê na thread dela.
            commands.OrderChanged += orderId => ui.TryEnqueue(() => _sale?.ReloadIfOpen(orderId));
        }
        var worker = new SyncWorker(
            new SyncEngine(database, transport, profile, log: line => CrashLog.Write($"sincronização: {line}", null),
                commands: commands),
            line => CrashLog.Write($"sincronização: {line}", null));
        worker.CommandsChanged += (_, _) => ui.TryEnqueue(() => _sale?.RefreshRemote());
        worker.ConnectionChanged += (_, online) => ui.TryEnqueue(() => _sync.Update(online: online));
        worker.QueueChanged += (_, queue) => ui.TryEnqueue(() => _sync.Update(pending: queue.Pending, quarantined: queue.Quarantined));

        _syncWorker = worker;
        _deviceToken = System.Text.Encoding.UTF8.GetString(token);

        _syncStop = new CancellationTokenSource();
        var stop = _syncStop.Token;
        var loop = Task.Run(() => worker.RunAsync(stop), stop);
        var fiscal = StartFiscalChecks(path, profile, _deviceToken, stop);
        _window.Closed += (_, _) =>
        {
            _syncStop.Cancel();
            // Um ciclo em andamento termina a transação dele antes de o banco fechar.
            loop.Wait(TimeSpan.FromSeconds(5));
            fiscal?.Wait(TimeSpan.FromSeconds(5));
            transport.Dispose();
            database.Dispose();
        };
    }

    /// <summary>
    /// A NFC-e em segundo plano: pede de novo o que não saiu e consulta o que
    /// ficou sem resposta. Conexão própria ao banco e prazo longo — aqui ninguém
    /// está esperando no balcão.
    /// </summary>
    /// <remarks>Desligado com <c>fiscal.enabled</c> fora de 1, o padrão até a homologação na SEFAZ-RJ.</remarks>
    private Task? StartFiscalChecks(string path, TerminalProfile profile, string token, CancellationToken stop)
    {
        PdvDatabase database;
        try
        {
            database = new PdvDatabase(path);
            if (!FiscalIssuance.IsEnabled(database, profile))
            {
                database.Dispose();
                return null;
            }
        }
        catch (PdvDatabaseException error)
        {
            CrashLog.Write("consulta fiscal", error);
            return null;
        }
        var ui = _window!.DispatcherQueue;
        var gateway = new HttpFiscalGateway(profile.CloudBaseUrl!, token, TimeSpan.FromSeconds(25));
        var service = new FiscalIssuance(database, gateway, log: line => CrashLog.Write(line, null));
        return Task.Run(async () =>
        {
            try
            {
                while (!stop.IsCancellationRequested)
                {
                    try
                    {
                        foreach (var decided in await service.CheckDueAsync(cancellation: stop))
                        {
                            var text = decided is { Kind: FiscalOutcomeKind.Authorized, Danfe: { } danfe }
                                ? $"NFC-e nº {danfe.Number} autorizada."
                                : decided.Notice;
                            ui.TryEnqueue(() =>
                            {
                                if (_sale is { } sale) sale.Notice = text;
                            });
                        }
                    }
                    catch (Exception error) when (error is not OperationCanceledException)
                    {
                        CrashLog.Write("consulta fiscal", error);
                    }
                    await Task.Delay(TimeSpan.FromSeconds(30), stop);
                }
            }
            catch (OperationCanceledException)
            {
            }
            finally
            {
                gateway.Dispose();
                database.Dispose();
            }
        }, CancellationToken.None);
    }

    /// <summary>
    /// O serviço de comandos do painel sobre uma conexão. Sem a chave do
    /// terminal no cofre não há como conferir assinatura: sem serviço, o caixa
    /// não obedece a nada — que é o lado seguro.
    /// </summary>
    private static Data.Remote.RemoteCommandService? RemoteCommands(string path, PdvDatabase database, TerminalProfile profile)
    {
        try
        {
            var secret = new SecretVault(Path.Combine(Path.GetDirectoryName(path)!, "secrets")).EnsureDeviceSecret();
            var ledger = new AuditLedger(profile.TenantId, profile.StoreId, profile.DeviceId, secret);
            return new Data.Remote.RemoteCommandService(
                database, profile, secret, ledger, new StaffAuthentication(database, profile.TenantId),
                log: line => CrashLog.Write($"painel: {line}", null));
        }
        catch (SecretVaultException error)
        {
            CrashLog.Write("comandos do painel", error);
            return null;
        }
    }

    private void ShowLogin(string path, PdvDatabase database)
    {
        var profile = TerminalProfile.Load(database);
        var auth = new StaffAuthentication(database, profile.TenantId);
        var login = new LoginViewModel(auth.Authenticate, profile.StoreName, profile.Activated);
        login.SignedIn += (_, identity) => OpenCounter(path, database, profile, identity);
        login.ActivationRequested += (_, _) => OpenActivation(path, database, profile);
        _window!.ShowLogin(login);
    }

    /// <summary>
    /// Ativação a partir da demonstração: grava num banco à parte, que a
    /// próxima abertura promove (<see cref="StagedActivation"/>).
    /// </summary>
    private void OpenActivation(string path, PdvDatabase database, TerminalProfile profile)
    {
        PdvDatabase staged;
        try
        {
            staged = StagedActivation.CreateStaged(database);
        }
        catch (Exception error)
        {
            CrashLog.Write("preparo da ativação", error);
            _window!.ShowFatal($"Não foi possível preparar a ativação: {error.Message}");
            return;
        }
        var vault = new SecretVault(Path.Combine(Path.GetDirectoryName(path)!, "secrets"));
        var activation = new ActivationViewModel(
            (api, code, token) => Activation.ActivateAsync(
                code, staged, vault, new HttpActivationTransport(api), cancellation: token),
            profile.CloudBaseUrl);

        activation.Dismissed += (_, _) =>
        {
            staged.Dispose();
            StagedActivation.Discard(staged.Path);
            ShowLogin(path, database);
        };
        activation.Activated += async (_, result) =>
        {
            staged.Dispose();
            database.Dispose();
            var dialog = new ContentDialog
            {
                XamlRoot = _window!.Content.XamlRoot,
                Title = "Terminal ativado",
                Content = $"Terminal ativado para {(result.StoreName.Length > 0 ? result.StoreName : "a loja")}.\n\n" +
                          "O PDV vai reiniciar para começar com os dados da loja.",
                CloseButtonText = "Reiniciar",
            };
            await dialog.ShowAsync();
            Restart();
        };
        _window!.ShowActivation(activation);
    }

    /// <summary>Abre um PDV novo; ele espera este soltar o banco antes de trocá-lo.</summary>
    private void Restart()
    {
        try
        {
            Process.Start(new ProcessStartInfo(Environment.ProcessPath!) { UseShellExecute = false });
        }
        catch (Exception error)
        {
            CrashLog.Write("reinício depois da ativação", error);
        }
        Exit();
    }

    /// <summary>
    /// A balança que a detecção gravou em <c>device_settings</c>, ou a simulada.
    /// Porta que não abre vira aviso no mostrador, não um caixa que não abre:
    /// a loja continua vendendo por unidade.
    /// </summary>
    private static Core.Scale.ScaleMonitor StartScale(PdvDatabase database)
    {
        var settings = Data.Hardware.ScaleSettings.Load(database);
        Core.Scale.IScaleDriver driver;
        try
        {
            driver = settings.BuildDriver();
        }
        catch (Core.Scale.ScaleException error)
        {
            CrashLog.Write("balança", error);
            driver = new Core.Scale.SimulatedScale();
        }
        return new Core.Scale.ScaleMonitor(
            driver, TimeSpan.FromMilliseconds(settings.PollIntervalMilliseconds), settings.StableReadings);
    }

    /// <summary>O caixa depois do login: segredo do terminal, auditoria, TEF.</summary>
    /// <remarks>
    /// TEF pelo simulador até o provedor ser escolhido (docs/port_csharp.md):
    /// o ciclo de pendência, confirmação e desfazimento já é o de produção.
    /// </remarks>
    private async void OpenCounter(string path, PdvDatabase database, TerminalProfile profile, Identity identity)
    {
        try
        {
            var vault = new SecretVault(Path.Combine(Path.GetDirectoryName(path)!, "secrets"));
            var secret = vault.EnsureDeviceSecret();
            var ledger = new AuditLedger(profile.TenantId, profile.StoreId, profile.DeviceId, secret);
            var terminal = profile.Identity;

            // A gaveta antes da venda: fundo de troco na abertura, e a de outro
            // operador não é tomada.
            var sessions = new CashSessionService(database, terminal, ledger);
            var (outcome, message) = await new CashOpening(sessions).EnsureOpenAsync(identity, AskTextAsync);
            if (outcome != CashOpening.Outcome.Ready)
            {
                if (message is not null) await InformAsync("Caixa em uso", message);
                ShowLogin(path, database);
                return;
            }

            var journal = new SqliteTefJournal(path);
            var tef = new TefCoordinator(new TefSimulator(), journal);
            var customers = new Data.Customers.CustomerLedgers(database, terminal, ledger);
            // Cupom: fila própria, fora da tela. Sem impressora configurada, vai
            // para a pasta "cupons" ao lado do banco (demonstração).
            var printerSettings = Data.Hardware.PrinterSettings.Load(database, Path.Combine(Path.GetDirectoryName(path)!, "cupons"));
            var printer = new Data.Hardware.PrintService(printerSettings.Build());
            var receipts = new ReceiptComposer(database, profile, printerSettings);
            // NFC-e: só com fiscal.enabled = 1 (desligado até a homologação). Prazo
            // curto: o cliente está no balcão, o resto fica para o segundo plano.
            HttpFiscalGateway? fiscalGateway = null;
            FiscalIssuance? fiscal = null;
            if (_deviceToken is { } deviceToken && FiscalIssuance.IsEnabled(database, profile))
            {
                fiscalGateway = new HttpFiscalGateway(profile.CloudBaseUrl!, deviceToken, FiscalIssuance.CounterDeadline);
                fiscal = new FiscalIssuance(database, fiscalGateway, log: line => CrashLog.Write(line, null));
            }
            var worker = _syncWorker;
            var documents = new SaleDocuments(
                closed => receipts.Compose(closed.OrderId, closed.OperatorName, closed.Customer?.Name,
                    closed.Result.Cashback?.AmountCents ?? 0, closed.Result.PrepaidBalanceCents),
                (payload, job) => printer.Submit(payload, job),
                printerSettings.Layout,
                fiscal,
                worker is null ? null : new Func<CancellationToken, Task>(worker.PushNowAsync),
                line => CrashLog.Write(line, null));
            string? lastOrderId = null;
            var scale = StartScale(database);
            // Conexão da tela: o aceite no caixa não disputa a do ciclo de sincronização.
            var remote = profile.Activated ? RemoteCommands(path, database, profile) : null;
            var sale = new SaleViewModel(
                new ItemRegistration(database, terminal, ledger),
                new Catalog(database.Connection, profile.TenantId),
                new Checkout(database, terminal, ledger, tef, customers: customers),
                identity,
                token => tef.RecoverPendingAsync(
                    entry => SaleRepository.WasRecorded(database.Connection, entry.TransactionId), token),
                new SaleAdjustments(database, terminal, ledger),
                new StaffAuthentication(database, profile.TenantId),
                () => scale.LastStable,
                remote,
                sessions,
                customers,
                new Data.Customers.DiscountTierService(database, terminal, ledger));
            _sale = sale;
            sale.RefreshRemote();

            sale.SaleClosed += async (_, closed) =>
            {
                lastOrderId = closed.OrderId;
                try
                {
                    // DANFE se a nota sair autorizada, cupom em todo o resto.
                    var notice = await documents.PrintAsync(closed);
                    if (notice is not null) sale.Notice = string.IsNullOrEmpty(sale.Notice) ? notice : $"{sale.Notice} {notice}";
                }
                catch (Exception error)
                {
                    // Cupom que não se monta não desfaz a venda gravada.
                    CrashLog.Write("montagem do cupom", error);
                    sale.Error = "A venda foi gravada, mas o cupom não pôde ser montado. Use Reimprimir.";
                }
            };
            sale.ReprintRequested += (_, _) =>
            {
                // A nota autorizada em segundo plano sai aqui como DANFE.
                if (documents.Reprint(lastOrderId, printer.LastPrinted) is { } again) printer.Submit(again, "PDV 2a via");
                else sale.Notice = "Nenhum cupom impresso ainda nesta sessão.";
            };

            // Os eventos saem da thread da balança; a tela só é tocada pela dela.
            var ui = _window!.DispatcherQueue;
            scale.ReadingReceived += reading => ui.TryEnqueue(() => sale.ShowReading(reading));
            scale.WeightChanged += () => ui.TryEnqueue(() => sale.ShowReading(
                new Core.Scale.ScaleReading(Core.Scale.ScaleStatus.Unstable, 0, "", DateTimeOffset.UtcNow)));
            scale.ErrorOccurred += message =>
            {
                CrashLog.Write($"balança: {message}", null);
                ui.TryEnqueue(() => sale.ShowScaleError(message));
            };
            printer.Failed += message =>
            {
                CrashLog.Write($"impressora: {message}", null);
                ui.TryEnqueue(() => sale.Error = message + " A venda está gravada: confira o papel e use Reimprimir.");
            };
            scale.Start();
            var released = false;
            async Task ReleaseAsync()
            {
                if (released) return;
                released = true;
                // A porta da balança precisa estar livre para o próximo login abri-la.
                await scale.DisposeAsync();
                journal.Dispose();
                printer.Dispose();
                fiscalGateway?.Dispose();
            }
            _window.Closed += (_, _) => ReleaseAsync().Wait(TimeSpan.FromSeconds(2));

            // Caixa fechado: resultado na tela e de volta ao login. Sessão
            // encerrada não recebe mais venda.
            sale.CashClosed += async (_, result) =>
            {
                _sale = null;
                await ReleaseAsync();
                await InformAsync("Caixa fechado", SaleViewModel.Describe(result));
                ShowLogin(path, database);
            };

            _window.ShowCounter(sale, _sync);
            await StartSalonAsync(path, profile, secret);
        }
        catch (Exception error) when (error is SecretVaultException or PdvDatabaseException)
        {
            CrashLog.Write("abertura do caixa", error);
            _window!.ShowFatal(error.Message);
        }
    }

    /// <summary>
    /// O servidor do salão para os celulares dos garçons e a tela da cozinha,
    /// depois de o caixa abrir — como o PDV em Python.
    /// </summary>
    /// <remarks>
    /// <para>
    /// <c>PDV_EDGE=0</c> desliga: uma loja só de balcão não precisa abrir porta
    /// na rede, e superfície que não serve a ninguém é só risco.
    /// <c>PDV_EDGE_TLS=0</c> sobe em HTTP, para diagnóstico de rede — e avisa no
    /// log, porque token de aparelho e PIN passam a trafegar em claro.
    /// </para>
    /// <para>
    /// Conexão própria ao banco, como o ciclo de sincronização. Porta ocupada,
    /// certificado que não se gera ou banco que não abre: o salão fica fora e o
    /// balcão segue vendendo.
    /// </para>
    /// </remarks>
    private async Task StartSalonAsync(string path, TerminalProfile profile, byte[] secret)
    {
        if (_salon is not null || Environment.GetEnvironmentVariable("PDV_EDGE") == "0") return;
        PdvDatabase? database = null;
        try
        {
            database = new PdvDatabase(path);
            var ledger = new AuditLedger(profile.TenantId, profile.StoreId, profile.DeviceId, secret);
            var services = new SalonServices(database, profile, ledger, new EventHub());
            if (Environment.GetEnvironmentVariable("PDV_EDGE_TLS") == "0")
            {
                CrashLog.Write("PDV_EDGE_TLS=0: o salão sobe em HTTP. Token de aparelho e PIN trafegam em claro na rede da loja.", null);
            }
            else
            {
                _salonTls = SalonCertificate.Ensure(Path.Combine(Path.GetDirectoryName(path)!, "tls"), profile.StoreName,
                    SalonCertificate.DefaultHosts(), log: line => CrashLog.Write(line, null));
            }
            var server = new SalonServer(services, line => CrashLog.Write(line, null));
            if (!await server.StartAsync(certificate: _salonTls?.Certificate)) return;
            _salon = server;
            var connection = database;
            database = null;
            if (_salonTls is { } tls) CrashLog.Write($"Salão: digital do certificado {tls.ShortFingerprint}.", null);
            _window!.Closed += (_, _) =>
            {
                // Primeiro o que fala com a rede, depois o banco. Fora da thread da
                // tela: esperar nela a continuação que volta para ela travaria.
                Task.Run(server.StopAsync).Wait(TimeSpan.FromSeconds(5));
                connection.Dispose();
            };
        }
        catch (PdvDatabaseException error)
        {
            CrashLog.Write("servidor do salão", error);
        }
        finally
        {
            database?.Dispose();
        }
    }

    /// <summary>Uma pergunta de texto fora da tela de venda (abertura do caixa).</summary>
    private async Task<string?> AskTextAsync(string prompt)
    {
        var box = new TextBox();
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetAutomationId(box, "DialogText");
        var dialog = new ContentDialog
        {
            XamlRoot = _window!.Content.XamlRoot,
            Title = prompt,
            Content = box,
            PrimaryButtonText = "Confirmar",
            CloseButtonText = "Cancelar",
            DefaultButton = ContentDialogButton.Primary,
        };
        return await dialog.ShowAsync() == ContentDialogResult.Primary ? box.Text : null;
    }

    private async Task InformAsync(string title, string message)
    {
        var dialog = new ContentDialog
        {
            XamlRoot = _window!.Content.XamlRoot,
            Title = title,
            Content = message,
            CloseButtonText = "OK",
        };
        await dialog.ShowAsync();
    }
}
