using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Pdv.App;
using Pdv.Core.Tef;
using Pdv.Data.Auth;
using Pdv.Data.Sales;

namespace Pdv.WinUI;

/// <summary>Só apresentação: o que a venda faz está no <see cref="SaleViewModel"/>, testado sem janela.</summary>
public sealed partial class CounterPage : UserControl
{
    public CounterPage(SaleViewModel viewModel, SyncStatusViewModel sync)
    {
        ViewModel = viewModel;
        Sync = sync;
        InitializeComponent();
        PayDebit.CommandParameter = TefCardType.Debit;
        PayCredit.CommandParameter = TefCardType.Credit;
        PayPix.CommandParameter = TefCardType.Pix;
        ViewModel.AskText = AskTextAsync;
        ViewModel.AskAuthorizer = AskAuthorizerAsync;
        Loaded += async (_, _) =>
        {
            QueryBox.Focus(FocusState.Programmatic);
            // Abertura do caixa: pendências do TEF antes da primeira venda.
            await ViewModel.StartCommand.ExecuteAsync(null);
        };
    }

    public SaleViewModel ViewModel { get; }

    public SyncStatusViewModel Sync { get; }

    public bool Has(string? text) => !string.IsNullOrEmpty(text);

    private void OnQueryKeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (e.Key != Windows.System.VirtualKey.Enter) return;
        e.Handled = true;
        ViewModel.ScanCommand.Execute(null);
    }

    private void OnLineSelected(object sender, SelectionChangedEventArgs e) =>
        ViewModel.SelectedLine = LinesList.SelectedItem as SaleLine;

    /// <summary>Uma pergunta de texto (motivo, percentual, o que o TEF pedir). Cancelar devolve <c>null</c>.</summary>
    private async Task<string?> AskTextAsync(string prompt)
    {
        var box = new TextBox { AcceptsReturn = false };
        AutomationProperties.SetAutomationId(box, "DialogText");
        var dialog = new ContentDialog
        {
            XamlRoot = XamlRoot,
            Title = prompt,
            Content = box,
            PrimaryButtonText = "Confirmar",
            CloseButtonText = "Cancelar",
            DefaultButton = ContentDialogButton.Primary,
        };
        box.KeyDown += (_, e) =>
        {
            if (e.Key != Windows.System.VirtualKey.Enter) return;
            e.Handled = true;
            _confirmedByEnter = true;
            dialog.Hide();
        };
        _confirmedByEnter = false;
        var result = await dialog.ShowAsync();
        return result == ContentDialogResult.Primary || _confirmedByEnter ? box.Text : null;
    }

    private bool _confirmedByEnter;

    /// <summary>
    /// Login e PIN de quem libera. O PIN é conferido aqui dentro, com o freio:
    /// errar mostra a mensagem e mantém o diálogo aberto para outra tentativa.
    /// </summary>
    private async Task<Identity?> AskAuthorizerAsync(AuthorizationRequest request)
    {
        var login = new ComboBox
        {
            ItemsSource = request.Logins,
            IsEditable = true,
            Header = "Login",
            HorizontalAlignment = HorizontalAlignment.Stretch,
            SelectedIndex = request.Logins.Count == 1 ? 0 : -1,
        };
        var pin = new PasswordBox { Header = "PIN", MaxLength = 12 };
        var error = new TextBlock
        {
            Foreground = (Microsoft.UI.Xaml.Media.Brush)Application.Current.Resources["SystemFillColorCriticalBrush"],
            TextWrapping = TextWrapping.Wrap,
        };
        AutomationProperties.SetAutomationId(login, "AuthorizerLogin");
        AutomationProperties.SetAutomationId(pin, "AuthorizerPin");
        AutomationProperties.SetAutomationId(error, "AuthorizerError");

        Identity? authorized = null;
        var dialog = new ContentDialog
        {
            XamlRoot = XamlRoot,
            Title = "Autorização",
            Content = new StackPanel
            {
                Spacing = 12,
                MinWidth = 320,
                Children = { new TextBlock { Text = request.Operation, TextWrapping = TextWrapping.Wrap }, login, pin, error },
            },
            PrimaryButtonText = "Autorizar",
            CloseButtonText = "Cancelar",
            DefaultButton = ContentDialogButton.Primary,
        };
        dialog.PrimaryButtonClick += (_, args) =>
        {
            var typed = (login.SelectedItem as string ?? login.Text ?? "").Trim();
            if (typed.Length == 0 || pin.Password.Length == 0)
            {
                error.Text = "Informe login e PIN.";
                args.Cancel = true;
                return;
            }
            try
            {
                authorized = request.Authorize(typed, pin.Password);
            }
            catch (AuthenticationException failure)
            {
                // A mensagem do serviço já é vaga para credencial errada e
                // específica para limite estourado: repassar sem enfeitar.
                error.Text = failure.Message;
                pin.Password = "";
                args.Cancel = true;
            }
        };
        await dialog.ShowAsync();
        return authorized;
    }

    private void OnResultClick(object sender, ItemClickEventArgs e)
    {
        if (e.ClickedItem is Product product)
        {
            ViewModel.AddCommand.Execute(product);
            QueryBox.Focus(FocusState.Programmatic);
        }
    }
}
