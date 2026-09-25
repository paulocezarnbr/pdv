using Microsoft.UI.Xaml;
using Pdv.App;
using Pdv.Data;
using Pdv.Core.Tef;
using Pdv.Data.Auth;
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
            var database = new PdvDatabase(path);
            var profile = TerminalProfile.Load(database);
            var auth = new StaffAuthentication(database, profile.TenantId);
            var login = new LoginViewModel(auth.Authenticate, profile.StoreName, profile.Activated);
            login.SignedIn += (_, identity) => OpenCounter(path, database, profile, identity);
            _window.ShowLogin(login);
        }
        catch (PdvDatabaseException error)
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
