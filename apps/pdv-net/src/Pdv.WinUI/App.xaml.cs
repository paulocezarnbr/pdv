using Microsoft.UI.Xaml;
using Pdv.App;
using Pdv.Data;
using Pdv.Data.Auth;

namespace Pdv.WinUI;

/// <summary>
/// Composição do caixa: abre o banco, lê quem é o terminal, entra pelo login.
/// </summary>
/// <remarks>
/// Toda falha de abertura vira uma mensagem na janela, nunca um fechamento
/// mudo: o executável não tem console, e "abri e sumiu" é o pior chamado de
/// suporte que existe.
/// </remarks>
public partial class App : Application
{
    private MainWindow? _window;

    public App()
    {
        InitializeComponent();
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
            _window.ShowFatal(error.Message);
        }
        _window.Activate();
    }
}
