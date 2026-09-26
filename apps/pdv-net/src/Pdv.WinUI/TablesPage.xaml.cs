using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Pdv.App;
using Pdv.Data.Sales;

namespace Pdv.WinUI;

/// <summary>Só apresentação: o que as mesas fazem está no <see cref="TablesViewModel"/>, testado sem janela.</summary>
public sealed partial class TablesPage : UserControl
{
    /// <summary>
    /// As formas da mesa, como no Python: a maquininha avulsa, sem o TEF do
    /// balcão. Pré-pago e fiado não aparecem aqui — a mesa não tem cliente
    /// identificado.
    /// </summary>
    private static readonly (string Method, string Label)[] Methods =
    [
        (PaymentMethods.Cash, "Dinheiro"),
        (PaymentMethods.Debit, "Cartão de débito"),
        (PaymentMethods.Credit, "Cartão de crédito"),
        (PaymentMethods.Pix, "PIX"),
    ];

    /// <summary>Relê o salão a cada dois segundos: o garçom pede a conta e a mesa muda de cor sozinha.</summary>
    private readonly DispatcherTimer _timer = new() { Interval = TimeSpan.FromSeconds(2) };

    public TablesPage(TablesViewModel viewModel)
    {
        ViewModel = viewModel;
        InitializeComponent();
        ViewModel.Confirm = ConfirmAsync;
        ViewModel.Inform = InformAsync;
        ViewModel.AskTip = AskTipAsync;
        ViewModel.AskPayments = AskPaymentsAsync;
        ViewModel.PickItems = PickItemsAsync;
        ViewModel.PickTarget = PickTargetAsync;
        _timer.Tick += (_, _) =>
        {
            // Com um diálogo aberto, a conta que o operador confere não muda embaixo dele.
            if (!_dialogOpen) ViewModel.Refresh();
        };
        Loaded += (_, _) => _timer.Start();
        Unloaded += (_, _) => _timer.Stop();
    }

    private bool _dialogOpen;

    public TablesViewModel ViewModel { get; }

    public bool Has(string? text) => !string.IsNullOrEmpty(text);

    /// <summary>A mesa que pediu a conta é a única com gente de pé esperando.</summary>
    public static Brush StatusBrush(bool billRequested) => (Brush)Application.Current.Resources[
        billRequested ? "SystemFillColorCautionBrush" : "TextFillColorSecondaryBrush"];

    public void Stop() => _timer.Stop();

    private void OnSelected(object sender, SelectionChangedEventArgs e) =>
        ViewModel.Selected = ((ListView)sender).SelectedItem as TableRow;

    /// <summary>Duplo clique recebe: é o gesto que o operador tenta sozinho na primeira vez.</summary>
    private async void OnDoubleTapped(object sender, DoubleTappedRoutedEventArgs e) =>
        await ViewModel.ReceiveCommand.ExecuteAsync(null);

    private async Task<ContentDialogResult> ShowAsync(ContentDialog dialog)
    {
        dialog.XamlRoot = XamlRoot;
        _dialogOpen = true;
        try
        {
            return await dialog.ShowAsync();
        }
        finally
        {
            _dialogOpen = false;
        }
    }

    private async Task<bool> ConfirmAsync(string title, string question) =>
        await ShowAsync(new ContentDialog
        {
            Title = title,
            Content = new TextBlock { Text = question, TextWrapping = TextWrapping.Wrap },
            PrimaryButtonText = "Sim",
            CloseButtonText = "Não",
            DefaultButton = ContentDialogButton.Close,
        }) == ContentDialogResult.Primary;

    private async Task InformAsync(string title, string message) =>
        await ShowAsync(new ContentDialog
        {
            Title = title,
            Content = new TextBlock { Text = message, TextWrapping = TextWrapping.Wrap },
            CloseButtonText = "OK",
        });

    private static TextBlock Error() => new()
    {
        Foreground = (Brush)Application.Current.Resources["SystemFillColorCriticalBrush"],
        TextWrapping = TextWrapping.Wrap,
    };

    /// <summary>A conferência da conta com a gorjeta: sugerida por botão, nunca aplicada sozinha.</summary>
    private async Task<long?> AskTipAsync(TipRequest request)
    {
        var tip = new TextBox { Header = "Gorjeta (R$)", Text = "0,00" };
        var preview = new TextBlock { Style = (Style)Application.Current.Resources["BodyStrongTextBlockStyle"] };
        var error = Error();
        AutomationProperties.SetAutomationId(tip, "TipAmount");
        void Update() => preview.Text = Money.Parse(tip.Text) is { } cents
            ? $"A cobrar: {Money.Format(request.TotalCents + cents)}"
            : "Valor inválido";
        tip.TextChanged += (_, _) => Update();
        var suggest = new Button { Content = $"{TablesViewModel.SuggestedTipPercent}%" };
        suggest.Click += (_, _) => tip.Text = (request.SuggestedCents / 100m).ToString("0.00", System.Globalization.CultureInfo.GetCultureInfo("pt-BR"));
        var none = new Button { Content = "Sem gorjeta" };
        none.Click += (_, _) => tip.Text = "0,00";
        Update();

        var dialog = new ContentDialog
        {
            Title = request.Title,
            Content = new StackPanel
            {
                Spacing = 12,
                MinWidth = 380,
                Children =
                {
                    new TextBlock { Text = $"CONTA: {Money.Format(request.TotalCents)}", FontSize = 28 },
                    new TextBlock { Text = request.Details, TextWrapping = TextWrapping.Wrap },
                    tip,
                    new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, Children = { suggest, none } },
                    preview,
                    new TextBlock
                    {
                        Text = "A gorjeta fica registrada no nome de quem atendeu a mesa e não entra no faturamento da loja.",
                        TextWrapping = TextWrapping.Wrap,
                        Style = (Style)Application.Current.Resources["CaptionTextBlockStyle"],
                    },
                    error,
                },
            },
            PrimaryButtonText = "Ir para o pagamento",
            CloseButtonText = "Voltar",
            DefaultButton = ContentDialogButton.Primary,
        };
        dialog.PrimaryButtonClick += (_, args) =>
        {
            if (Money.Parse(tip.Text) is null)
            {
                error.Text = "Digite a gorjeta em reais, como 5,00.";
                args.Cancel = true;
            }
        };
        return await ShowAsync(dialog) == ContentDialogResult.Primary ? Money.Parse(tip.Text) : null;
    }

    /// <summary>Uma ou mais formas, porque a mesa que divide a conta é rotina, não exceção.</summary>
    private async Task<IReadOnlyList<PaymentIntent>?> AskPaymentsAsync(long chargedCents)
    {
        var added = new List<PaymentIntent>();
        var method = new ComboBox { Header = "Forma", ItemsSource = Methods.Select(m => m.Label).ToList(), SelectedIndex = 0, MinWidth = 180 };
        var amount = new TextBox { Header = "Valor (R$)", MinWidth = 120 };
        var add = new Button { Content = "Adicionar", VerticalAlignment = VerticalAlignment.Bottom };
        var list = new ListView { MaxHeight = 160, SelectionMode = ListViewSelectionMode.Single };
        var remove = new Button { Content = "Remover selecionado" };
        var balance = new TextBlock { Style = (Style)Application.Current.Resources["BodyStrongTextBlockStyle"] };
        AutomationProperties.SetAutomationId(method, "PaymentMethod");
        AutomationProperties.SetAutomationId(amount, "PaymentAmount");
        AutomationProperties.SetAutomationId(add, "PaymentAdd");

        void Refresh()
        {
            var paid = added.Sum(p => p.AmountCents);
            list.ItemsSource = added.Select(p => $"{Methods.First(m => m.Method == p.Method).Label} — {Money.Format(p.AmountCents)}").ToList();
            balance.Text = paid >= chargedCents
                ? $"Troco: {Money.Format(paid - chargedCents)}"
                : $"Falta: {Money.Format(chargedCents - paid)}";
            amount.Text = (Math.Max(0, chargedCents - paid) / 100m).ToString("0.00", System.Globalization.CultureInfo.GetCultureInfo("pt-BR"));
        }
        add.Click += (_, _) =>
        {
            if (Money.Parse(amount.Text) is not { } cents || cents <= 0 || method.SelectedIndex < 0) return;
            added.Add(new PaymentIntent(Methods[method.SelectedIndex].Method, cents));
            Refresh();
        };
        remove.Click += (_, _) =>
        {
            if (list.SelectedIndex < 0) return;
            added.RemoveAt(list.SelectedIndex);
            Refresh();
        };
        Refresh();

        var dialog = new ContentDialog
        {
            Title = $"Recebimento — {Money.Format(chargedCents)}",
            Content = new StackPanel
            {
                Spacing = 12,
                MinWidth = 440,
                Children =
                {
                    new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, Children = { method, amount, add } },
                    list,
                    remove,
                    balance,
                },
            },
            PrimaryButtonText = "Confirmar",
            CloseButtonText = "Cancelar",
            DefaultButton = ContentDialogButton.Primary,
        };
        // Confirmar sem adicionar nada usa a forma e o valor que estão na tela:
        // é o caso comum, uma forma só, e dois cliques a mais na fila.
        dialog.PrimaryButtonClick += (_, _) =>
        {
            if (added.Count == 0 && Money.Parse(amount.Text) is { } cents && cents > 0 && method.SelectedIndex >= 0)
            {
                added.Add(new PaymentIntent(Methods[method.SelectedIndex].Method, cents));
            }
        };
        return await ShowAsync(dialog) == ContentDialogResult.Primary && added.Count > 0 ? added : null;
    }

    /// <summary>Marcar itens com o total do que foi marcado à vista: é o que o cliente confere.</summary>
    private async Task<IReadOnlyList<string>?> PickItemsAsync(ItemPickRequest request)
    {
        var list = new ListView
        {
            SelectionMode = ListViewSelectionMode.Multiple,
            MaxHeight = 320,
            // O próprio item, não o texto: dois cafés iguais teriam o mesmo texto.
            ItemsSource = request.Items.ToList(),
            DisplayMemberPath = nameof(TableItemChoice.Label),
        };
        var total = new TextBlock { Style = (Style)Application.Current.Resources["SubtitleTextBlockStyle"] };
        AutomationProperties.SetAutomationId(list, "ItemPicker");
        var dialog = new ContentDialog
        {
            Title = request.Title,
            Content = new StackPanel { Spacing = 12, MinWidth = 440, Children = { list, total } },
            PrimaryButtonText = request.Confirm,
            CloseButtonText = "Voltar",
            DefaultButton = ContentDialogButton.Primary,
            IsPrimaryButtonEnabled = false,
        };
        List<TableItemChoice> Chosen() => [.. list.SelectedItems.OfType<TableItemChoice>()];
        void Update()
        {
            var chosen = Chosen();
            total.Text = $"{chosen.Count} item(ns) · {Money.Format(chosen.Sum(item => item.TotalCents))}";
            dialog.IsPrimaryButtonEnabled = chosen.Count > 0;
        }
        list.SelectionChanged += (_, _) => Update();
        Update();
        return await ShowAsync(dialog) == ContentDialogResult.Primary ? Chosen().Select(item => item.Id).ToList() : null;
    }

    private async Task<int?> PickTargetAsync(string prompt, IReadOnlyList<string> labels)
    {
        var choice = new ComboBox { ItemsSource = labels.ToList(), SelectedIndex = 0, HorizontalAlignment = HorizontalAlignment.Stretch };
        AutomationProperties.SetAutomationId(choice, "TargetOrder");
        var result = await ShowAsync(new ContentDialog
        {
            Title = "Destino",
            Content = new StackPanel
            {
                Spacing = 12,
                MinWidth = 380,
                Children = { new TextBlock { Text = prompt, TextWrapping = TextWrapping.Wrap }, choice },
            },
            PrimaryButtonText = "Continuar",
            CloseButtonText = "Voltar",
            DefaultButton = ContentDialogButton.Primary,
        });
        return result == ContentDialogResult.Primary && choice.SelectedIndex >= 0 ? choice.SelectedIndex : null;
    }
}
