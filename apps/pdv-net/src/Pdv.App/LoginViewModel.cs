using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using Pdv.Data.Auth;

namespace Pdv.App;

/// <summary>A tela de entrada do caixa: login e PIN, com teclado na tela.</summary>
/// <remarks>
/// <para>
/// O PIN é conferido com Argon2id (64 MiB, ~0,3 s) FORA da thread da tela: no
/// balcão, uma janela que congela a cada tentativa é uma janela que o operador
/// clica de novo — e cada clique é mais uma tentativa no freio.
/// </para>
/// <para>
/// PIN errado limpa o campo; o login fica. É o que o operador espera, e não
/// deixa o PIN errado à vista de quem está do lado.
/// </para>
/// </remarks>
public sealed partial class LoginViewModel(
    Func<string, string, Identity> authenticate, string storeName, bool activated) : ObservableObject
{
    public string StoreName { get; } = storeName;

    public bool IsDemo { get; } = !activated;

    public string? DemoNotice => IsDemo
        ? "Terminal não ativado: modo demonstração. As vendas de teste são arquivadas na ativação."
        : null;

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(SignInCommand))]
    public partial string Login { get; set; } = "";

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(SignInCommand))]
    public partial string Pin { get; set; } = "";

    [ObservableProperty]
    public partial string? Error { get; set; }

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(SignInCommand))]
    public partial bool IsBusy { get; set; }

    /// <summary>Quem entrou. A casca troca a tela ao receber.</summary>
    public event EventHandler<Identity>? SignedIn;

    [RelayCommand]
    private void AppendDigit(string digit)
    {
        if (digit is { Length: 1 } && char.IsAsciiDigit(digit[0]) && Pin.Length < PinPolicy.MaxLength)
        {
            Pin += digit;
        }
    }

    [RelayCommand]
    private void Backspace()
    {
        if (Pin.Length > 0) Pin = Pin[..^1];
    }

    [RelayCommand]
    private void ClearPin() => Pin = "";

    private bool CanSignIn() => !IsBusy && Login.Trim().Length > 0 && Pin.Length > 0;

    [RelayCommand(CanExecute = nameof(CanSignIn))]
    private async Task SignInAsync()
    {
        IsBusy = true;
        Error = null;
        var login = Login;
        var pin = Pin;
        try
        {
            var identity = await Task.Run(() => authenticate(login, pin));
            Pin = "";
            SignedIn?.Invoke(this, identity);
        }
        catch (AuthenticationException error)
        {
            Error = error.Message;
            Pin = "";
        }
        finally
        {
            IsBusy = false;
        }
    }
}
