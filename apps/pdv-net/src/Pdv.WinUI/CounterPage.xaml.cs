using Microsoft.UI.Xaml.Controls;
using Pdv.Data;
using Pdv.Data.Auth;

namespace Pdv.WinUI;

public sealed partial class CounterPage : UserControl
{
    public CounterPage(Identity operatorIdentity, TerminalProfile profile)
    {
        InitializeComponent();
        Greeting.Text = $"Olá, {operatorIdentity.FirstName}";
        Store.Text = $"Caixa aberto — {profile.StoreName}";
    }
}
