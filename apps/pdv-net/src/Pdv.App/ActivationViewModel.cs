using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using Pdv.Data.Provisioning;

namespace Pdv.App;

/// <summary>
/// Ativação do terminal: o endereço do painel e o código que ele gerou.
/// </summary>
/// <remarks>
/// <para>
/// A tela coleta e mostra; quem ativa é a função recebida — na casca, o
/// <see cref="Activation.ActivateAsync"/> sobre o banco novo da loja. A
/// chamada de rede roda fora da thread da tela: são até 20 s, e uma janela que
/// congela é uma janela que o lojista fecha e reabre, queimando um código válido.
/// </para>
/// <para>
/// Endereço e código são conferidos antes de ir à rede, com as mesmas regras do
/// Python: o erro de digitação aparece na hora, sem gastar tentativa no limite
/// por IP da retaguarda.
/// </para>
/// </remarks>
public sealed partial class ActivationViewModel(
    Func<string, string, CancellationToken, Task<ActivationResult>> activate, string? knownServer) : ObservableObject
{
    public const string DemoWarning =
        "As vendas, produtos e usuários de demonstração serão arquivados — não vão para a loja. " +
        "O caixa reinicia com os dados da loja.";

    private CancellationTokenSource? _running;

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(ActivateCommand))]
    public partial string Server { get; set; } = Activation.DisplayServerUrl(knownServer);

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(ActivateCommand))]
    public partial string Code { get; set; } = "";

    [ObservableProperty]
    public partial string? Error { get; set; }

    [ObservableProperty]
    public partial string? Status { get; set; }

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(ActivateCommand))]
    [NotifyCanExecuteChangedFor(nameof(LaterCommand))]
    public partial bool IsBusy { get; set; }

    /// <summary>Ativou: a casca avisa e reinicia o caixa.</summary>
    public event EventHandler<ActivationResult>? Activated;

    /// <summary>"Ativar depois": volta ao login em modo demonstração.</summary>
    public event EventHandler? Dismissed;

    /// <summary>Digitado em minúscula aparece em maiúscula: é como o painel mostra.</summary>
    partial void OnCodeChanged(string value)
    {
        var upper = value.ToUpperInvariant();
        if (upper != value) Code = upper;
    }

    private bool CanActivate() => !IsBusy && Server.Trim().Length > 0 && Code.Trim().Length > 0;

    [RelayCommand(CanExecute = nameof(CanActivate))]
    private async Task ActivateAsync()
    {
        string server;
        string code;
        try
        {
            server = Activation.NormalizeServerUrl(Server);
            code = Activation.NormalizeCode(Code);
        }
        catch (ActivationException error)
        {
            Error = error.Message;
            return;
        }

        IsBusy = true;
        Error = null;
        Status = "Falando com a retaguarda…";
        _running = new CancellationTokenSource();
        try
        {
            var token = _running.Token;
            var result = await Task.Run(() => activate(server, code, token), token);
            Status = $"Terminal ativado para {(result.StoreName.Length > 0 ? result.StoreName : "a loja")}.";
            Activated?.Invoke(this, result);
        }
        catch (OperationCanceledException)
        {
            Status = null;
        }
        catch (Exception error) when (error is ActivationException or Pdv.Data.Secrets.SecretVaultException)
        {
            Status = null;
            Error = error.Message;
        }
        catch (Exception error)
        {
            // Vira mensagem, não fechamento: o código ainda pode valer.
            Status = null;
            Error = $"Erro inesperado ao ativar: {error.Message}";
        }
        finally
        {
            _running.Dispose();
            _running = null;
            IsBusy = false;
        }
    }

    private bool CanLater() => !IsBusy;

    [RelayCommand(CanExecute = nameof(CanLater))]
    private void Later() => Dismissed?.Invoke(this, EventArgs.Empty);
}
