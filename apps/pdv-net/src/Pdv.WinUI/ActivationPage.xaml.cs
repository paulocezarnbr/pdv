using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Pdv.App;

namespace Pdv.WinUI;

/// <summary>Só apresentação: o que a ativação faz está no <see cref="ActivationViewModel"/>.</summary>
public sealed partial class ActivationPage : UserControl
{
    public ActivationPage(ActivationViewModel viewModel)
    {
        ViewModel = viewModel;
        InitializeComponent();
        Loaded += (_, _) => (string.IsNullOrEmpty(ViewModel.Server) ? ServerBox : CodeBox).Focus(FocusState.Programmatic);
    }

    public ActivationViewModel ViewModel { get; }

    public string Warning => ActivationViewModel.DemoWarning;

    public bool Has(string? text) => !string.IsNullOrEmpty(text);

    private async void OnCodeKeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (e.Key != Windows.System.VirtualKey.Enter) return;
        e.Handled = true;
        if (ViewModel.ActivateCommand.CanExecute(null)) await ViewModel.ActivateCommand.ExecuteAsync(null);
    }
}
