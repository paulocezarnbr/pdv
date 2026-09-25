using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Pdv.App;

namespace Pdv.WinUI;

/// <summary>A janela única do caixa. As telas trocam dentro dela.</summary>
public sealed partial class MainWindow : Window
{
    public MainWindow()
    {
        InitializeComponent();
        ExtendsContentIntoTitleBar = false;
        AppWindow.Resize(new Windows.Graphics.SizeInt32(1280, 800));
    }

    public void ShowLogin(LoginViewModel viewModel) => Host.Content = new LoginPage(viewModel);

    public void ShowCounter(SaleViewModel sale) => Host.Content = new CounterPage(sale);

    public void ShowActivation(ActivationViewModel activation) => Host.Content = new ActivationPage(activation);

    public void ShowFatal(string message) =>
        Host.Content = new InfoBar
        {
            Title = "O PDV não conseguiu abrir",
            Message = message,
            Severity = InfoBarSeverity.Error,
            IsOpen = true,
            IsClosable = false,
            Margin = new Thickness(24),
        };
}
