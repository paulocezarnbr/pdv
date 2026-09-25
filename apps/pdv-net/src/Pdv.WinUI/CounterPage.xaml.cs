using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Pdv.App;
using Pdv.Core.Tef;
using Pdv.Data.Sales;

namespace Pdv.WinUI;

/// <summary>Só apresentação: o que a venda faz está no <see cref="SaleViewModel"/>, testado sem janela.</summary>
public sealed partial class CounterPage : UserControl
{
    public CounterPage(SaleViewModel viewModel)
    {
        ViewModel = viewModel;
        InitializeComponent();
        PayDebit.CommandParameter = TefCardType.Debit;
        PayCredit.CommandParameter = TefCardType.Credit;
        PayPix.CommandParameter = TefCardType.Pix;
        Loaded += async (_, _) =>
        {
            QueryBox.Focus(FocusState.Programmatic);
            // Abertura do caixa: pendências do TEF antes da primeira venda.
            await ViewModel.StartCommand.ExecuteAsync(null);
        };
    }

    public SaleViewModel ViewModel { get; }

    public bool Has(string? text) => !string.IsNullOrEmpty(text);

    private void OnQueryKeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (e.Key != Windows.System.VirtualKey.Enter) return;
        e.Handled = true;
        ViewModel.ScanCommand.Execute(null);
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
