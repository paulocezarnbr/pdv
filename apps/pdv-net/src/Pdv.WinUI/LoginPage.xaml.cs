using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Pdv.App;

namespace Pdv.WinUI;

/// <summary>Só apresentação: o que a tela faz está no <see cref="LoginViewModel"/>, testado sem janela.</summary>
public sealed partial class LoginPage : UserControl
{
    public LoginPage(LoginViewModel viewModel)
    {
        ViewModel = viewModel;
        InitializeComponent();
        BuildKeypad();
        Loaded += (_, _) => (string.IsNullOrEmpty(ViewModel.Login) ? (Control)LoginBox : PinBox).Focus(FocusState.Programmatic);
    }

    public LoginViewModel ViewModel { get; }

    public bool HasError(string? error) => !string.IsNullOrEmpty(error);

    private void BuildKeypad()
    {
        string[] keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "C", "0", "⌫"];
        for (var i = 0; i < keys.Length; i++)
        {
            var key = keys[i];
            var button = new Button
            {
                Content = key,
                FontSize = 20,
                HorizontalAlignment = HorizontalAlignment.Stretch,
                VerticalAlignment = VerticalAlignment.Stretch,
                Command = key switch
                {
                    "C" => ViewModel.ClearPinCommand,
                    "⌫" => ViewModel.BackspaceCommand,
                    _ => ViewModel.AppendDigitCommand,
                },
                CommandParameter = key,
                IsTabStop = false,
            };
            AutomationProperties.SetAutomationId(button, key switch { "C" => "KeyClear", "⌫" => "KeyBack", _ => "Key" + key });
            AutomationProperties.SetName(button, key switch { "C" => "Limpar PIN", "⌫" => "Apagar", _ => key });
            Grid.SetRow(button, i / 3);
            Grid.SetColumn(button, i % 3);
            Keypad.Children.Add(button);
        }
    }

    private async void OnPinKeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (e.Key != Windows.System.VirtualKey.Enter) return;
        e.Handled = true;
        if (ViewModel.SignInCommand.CanExecute(null)) await ViewModel.SignInCommand.ExecuteAsync(null);
    }
}
