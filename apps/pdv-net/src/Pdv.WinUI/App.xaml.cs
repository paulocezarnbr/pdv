using Microsoft.UI.Xaml;
using Pdv.App;
using Pdv.Data;
using Pdv.Data.Auth;

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
            var database = new PdvDatabase(TerminalProfile.DefaultDatabasePath());
            var profile = TerminalProfile.Load(database);
            var auth = new StaffAuthentication(database, profile.TenantId);
            var login = new LoginViewModel(auth.Authenticate, profile.StoreName, profile.Activated);
            login.SignedIn += (_, identity) => _window.ShowCounter(identity, profile);
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
}
