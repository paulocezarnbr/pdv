using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Pdv.App;

namespace Pdv.WinUI;

/// <summary>Só apresentação: o que o painel faz está no <see cref="SalonPanelViewModel"/>, testado sem janela.</summary>
public sealed partial class SalonPanelPage : UserControl
{
    /// <summary>
    /// Um segundo para a contagem do código; a cada dois, relê o banco — o ritmo
    /// do <c>salon_panel.py</c>.
    /// </summary>
    private readonly DispatcherTimer _timer = new() { Interval = TimeSpan.FromSeconds(1) };

    private int _ticks;

    public SalonPanelPage(SalonPanelViewModel viewModel)
    {
        ViewModel = viewModel;
        InitializeComponent();
        ViewModel.Confirm = ConfirmAsync;
        _timer.Tick += (_, _) =>
        {
            ViewModel.Tick();
            if (++_ticks % 2 == 0) ViewModel.Refresh();
        };
        Loaded += (_, _) => _timer.Start();
        Unloaded += (_, _) => _timer.Stop();
    }

    public SalonPanelViewModel ViewModel { get; }

    public InfoBarSeverity AddressSeverity => ViewModel.AddressState switch
    {
        SalonAddressState.Secure => InfoBarSeverity.Informational,
        SalonAddressState.Plain => InfoBarSeverity.Warning,
        _ => InfoBarSeverity.Error,
    };

    public bool Has(string? text) => !string.IsNullOrEmpty(text);

    public static string LateText(bool late) => late ? "atrasado" : "";

    public void Stop() => _timer.Stop();

    // A lista volta a seleção pelo SelectionChanged; o view model a devolve à
    // lista pelo x:Bind de ida. O refresh troca a linha que mudou, a lista
    // solta a seleção por um instante, e o view model a repõe pelo id.
    private void OnOrderSelected(object sender, SelectionChangedEventArgs e) =>
        ViewModel.SelectedOrder = ((ListView)sender).SelectedItem as SalonOrderRow;

    private void OnPersonSelected(object sender, SelectionChangedEventArgs e) =>
        ViewModel.SelectedPerson = ((ListView)sender).SelectedItem as SalonPersonRow;

    private void OnDeviceSelected(object sender, SelectionChangedEventArgs e) =>
        ViewModel.SelectedDevice = ((ListView)sender).SelectedItem as SalonDeviceRow;

    private void OnTicketSelected(object sender, SelectionChangedEventArgs e) =>
        ViewModel.SelectedTicket = ((ListView)sender).SelectedItem as SalonTicketRow;

    private async Task<bool> ConfirmAsync(string title, string question)
    {
        var dialog = new ContentDialog
        {
            XamlRoot = XamlRoot,
            Title = title,
            Content = new TextBlock { Text = question, TextWrapping = TextWrapping.Wrap },
            PrimaryButtonText = "Confirmar",
            CloseButtonText = "Cancelar",
            DefaultButton = ContentDialogButton.Close,
        };
        return await dialog.ShowAsync() == ContentDialogResult.Primary;
    }
}
