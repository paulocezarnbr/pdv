using System.Diagnostics;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Pdv.App;
using Pdv.Data;
using Pdv.Core.Tef;
using Pdv.Data.Auth;
using Pdv.Data.Provisioning;
using Pdv.Data.Sales;
using Pdv.Data.Secrets;
using Pdv.Data.Sync;

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

        _syncStop = new CancellationTokenSource();
        var stop = _syncStop.Token;
        var loop = Task.Run(() => worker.RunAsync(stop), stop);
        _window.Closed += (_, _) =>
        {
            _syncStop.Cancel();
            // Um ciclo em andamento termina a transação dele antes de o banco fechar.
            loop.Wait(TimeSpan.FromSeconds(5));
            transport.Dispose();
            database.Dispose();
        };
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
            var ledger = new AuditLedger(profile.TenantId, profile.StoreId, profile.DeviceId, vault.EnsureDeviceSecret());
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
            scale.Start();
            var released = false;
            async Task ReleaseAsync()
            {
                if (released) return;
                released = true;
                // A porta da balança precisa estar livre para o próximo login abri-la.
                await scale.DisposeAsync();
                journal.Dispose();
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
        }
        catch (Exception error) when (error is SecretVaultException or PdvDatabaseException)
        {
            CrashLog.Write("abertura do caixa", error);
            _window!.ShowFatal(error.Message);
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
