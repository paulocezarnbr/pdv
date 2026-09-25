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

    /// <summary>O caixa depois do login: segredo do terminal, auditoria, TEF.</summary>
    /// <remarks>
    /// TEF pelo simulador até o provedor ser escolhido (docs/port_csharp.md):
    /// o ciclo de pendência, confirmação e desfazimento já é o de produção.
    /// </remarks>
    private void OpenCounter(string path, PdvDatabase database, TerminalProfile profile, Identity identity)
    {
        try
        {
            var vault = new SecretVault(Path.Combine(Path.GetDirectoryName(path)!, "secrets"));
            var ledger = new AuditLedger(profile.TenantId, profile.StoreId, profile.DeviceId, vault.EnsureDeviceSecret());
            var journal = new SqliteTefJournal(path);
            var tef = new TefCoordinator(new TefSimulator(), journal);
            var terminal = profile.Identity;
            _window!.ShowCounter(new SaleViewModel(
                new ItemRegistration(database, terminal, ledger),
                new Catalog(database.Connection, profile.TenantId),
                new Checkout(database, terminal, ledger, tef),
                identity,
                token => tef.RecoverPendingAsync(
                    entry => SaleRepository.WasRecorded(database.Connection, entry.TransactionId), token)));
        }
        catch (Exception error) when (error is SecretVaultException or PdvDatabaseException)
        {
            CrashLog.Write("abertura do caixa", error);
            _window!.ShowFatal(error.Message);
        }
    }
}
