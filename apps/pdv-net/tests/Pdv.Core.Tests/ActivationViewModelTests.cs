using Pdv.App;
using Pdv.Data.Auth;
using Pdv.Data.Provisioning;

namespace Pdv.Core.Tests;

/// <summary>A tela de ativação sem janela: o que vai para a rede e o que volta para o balcão.</summary>
public sealed class ActivationViewModelTests
{
    private static readonly ActivationResult Granted = new("t", "s", "d", "tok", "Pool Bar", "https://x/api");

    private sealed class Recorder
    {
        public List<(string Api, string Code)> Calls { get; } = [];

        public Func<string, string, CancellationToken, Task<ActivationResult>> Answer(Func<ActivationResult> answer) =>
            (api, code, _) =>
            {
                Calls.Add((api, code));
                return Task.FromResult(answer());
            };
    }

    [Fact]
    public async Task Typing_mistakes_are_caught_before_spending_an_attempt()
    {
        var recorder = new Recorder();
        var screen = new ActivationViewModel(recorder.Answer(() => Granted), null)
        {
            Server = "http://painel.loja.com.br",
            Code = "ABCD-EFGH",
        };
        await screen.ActivateCommand.ExecuteAsync(null);
        Assert.Empty(recorder.Calls);
        Assert.StartsWith("Use https://", screen.Error);

        screen.Server = "painel.loja.com.br";
        screen.Code = "AB-1";
        await screen.ActivateCommand.ExecuteAsync(null);
        Assert.Empty(recorder.Calls);
        Assert.StartsWith("Código de ativação inválido", screen.Error);
    }

    [Fact]
    public async Task A_granted_code_reports_the_store_and_goes_normalized()
    {
        var recorder = new Recorder();
        var screen = new ActivationViewModel(recorder.Answer(() => Granted), null)
        {
            Server = "teste.rsrassessoria.com.br",
            Code = "abcd-efgh-jkmn",
        };
        ActivationResult? activated = null;
        screen.Activated += (_, result) => activated = result;

        await screen.ActivateCommand.ExecuteAsync(null);

        Assert.Equal([("https://teste.rsrassessoria.com.br/api", "ABCDEFGHJKMN")], recorder.Calls);
        Assert.Equal(Granted, activated);
        Assert.Null(screen.Error);
        Assert.Equal("Terminal ativado para Pool Bar.", screen.Status);
        Assert.False(screen.IsBusy);
    }

    [Fact]
    public async Task A_refusal_is_shown_and_the_code_can_be_retyped()
    {
        var recorder = new Recorder();
        var screen = new ActivationViewModel(
            recorder.Answer(() => throw new ActivationRefusedException("Código inválido, expirado ou já utilizado.")), null)
        {
            Server = "teste.rsrassessoria.com.br",
            Code = "ABCDEFGHJKMN",
        };
        var activated = false;
        screen.Activated += (_, _) => activated = true;

        await screen.ActivateCommand.ExecuteAsync(null);

        Assert.False(activated);
        Assert.Equal("Código inválido, expirado ou já utilizado.", screen.Error);
        Assert.Null(screen.Status);
        Assert.True(screen.ActivateCommand.CanExecute(null));
    }

    [Fact]
    public async Task An_unexpected_failure_is_a_message_not_a_crash()
    {
        var screen = new ActivationViewModel(
            (_, _, _) => throw new InvalidOperationException("disco cheio"), null)
        {
            Server = "teste.rsrassessoria.com.br",
            Code = "ABCDEFGHJKMN",
        };
        await screen.ActivateCommand.ExecuteAsync(null);
        Assert.Equal("Erro inesperado ao ativar: disco cheio", screen.Error);
    }

    [Fact]
    public void The_code_is_shown_in_capitals_and_the_known_server_without_api()
    {
        var screen = new ActivationViewModel((_, _, _) => Task.FromResult(Granted), "https://teste.rsrassessoria.com.br/api")
        {
            Code = "abcd-efgh",
        };
        Assert.Equal("ABCD-EFGH", screen.Code);
        Assert.Equal("https://teste.rsrassessoria.com.br", screen.Server);
        Assert.Equal("", new ActivationViewModel((_, _, _) => Task.FromResult(Granted), Activation.PlaceholderCloudUrl).Server);
    }

    [Fact]
    public void Later_goes_back_to_the_demo()
    {
        var screen = new ActivationViewModel((_, _, _) => Task.FromResult(Granted), null);
        var dismissed = false;
        screen.Dismissed += (_, _) => dismissed = true;
        screen.LaterCommand.Execute(null);
        Assert.True(dismissed);
    }

    [Fact]
    public void Only_a_demo_terminal_offers_activation()
    {
        static Identity Nobody(string login, string pin) => throw new AuthenticationException("não");
        Assert.True(new LoginViewModel(Nobody, "Demo", activated: false).RequestActivationCommand.CanExecute(null));
        Assert.False(new LoginViewModel(Nobody, "Pool Bar", activated: true).RequestActivationCommand.CanExecute(null));
    }
}
