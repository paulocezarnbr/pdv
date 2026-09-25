using Pdv.App;
using Pdv.Data;
using Pdv.Data.Auth;

namespace Pdv.Core.Tests;

/// <summary>A tela de login, sem janela: o que o operador vê e o que acontece.</summary>
public sealed class LoginViewModelTests : IDisposable
{
    private readonly TestDatabase _file = new();
    private readonly PdvDatabase _database;

    public LoginViewModelTests()
    {
        _database = new PdvDatabase(_file.Path);
        _database.Execute(
            "INSERT INTO users (id, tenant_id, name, login, role, pin_hash, can_authorize, is_active, updated_at) " +
            "VALUES ('u-1', 'tenant-1', 'Ana Caixa', 'ana', 'cashier', $hash, 0, 1, '2026-09-25T12:00:00.000+00:00')",
            ("$hash", PinHasher.Hash("480362")));
    }

    public void Dispose()
    {
        _database.Dispose();
        _file.Dispose();
    }

    private LoginViewModel Screen(bool activated = true)
    {
        var auth = new StaffAuthentication(_database, "tenant-1");
        return new LoginViewModel(auth.Authenticate, "Dolce Affetto", activated);
    }

    private static void Type(LoginViewModel screen, string pin)
    {
        foreach (var digit in pin) screen.AppendDigitCommand.Execute(digit.ToString());
    }

    [Fact]
    public async Task The_right_pin_signs_in_and_clears_the_field()
    {
        var screen = Screen();
        Identity? signedIn = null;
        screen.SignedIn += (_, identity) => signedIn = identity;

        screen.Login = "ana";
        Type(screen, "480362");
        await screen.SignInCommand.ExecuteAsync(null);

        Assert.Equal("u-1", signedIn?.Id);
        Assert.Equal("", screen.Pin);
        Assert.Null(screen.Error);
        Assert.False(screen.IsBusy);
    }

    [Fact]
    public async Task A_wrong_pin_shows_why_clears_the_pin_and_keeps_the_login()
    {
        var screen = Screen();
        var signedIn = false;
        screen.SignedIn += (_, _) => signedIn = true;

        screen.Login = "ana";
        Type(screen, "999999");
        await screen.SignInCommand.ExecuteAsync(null);

        Assert.False(signedIn);
        Assert.Equal("Login ou PIN inválido.", screen.Error);
        Assert.Equal("", screen.Pin);
        Assert.Equal("ana", screen.Login);
    }

    [Fact]
    public void Nothing_to_submit_until_login_and_pin_are_there()
    {
        var screen = Screen();
        Assert.False(screen.SignInCommand.CanExecute(null));
        screen.Login = "ana";
        Assert.False(screen.SignInCommand.CanExecute(null));
        Type(screen, "4");
        Assert.True(screen.SignInCommand.CanExecute(null));
    }

    [Fact]
    public void The_keypad_only_types_digits_and_stops_at_the_maximum()
    {
        var screen = Screen();
        Type(screen, "1234567890123456");
        screen.AppendDigitCommand.Execute("x");
        screen.AppendDigitCommand.Execute("12");
        Assert.Equal("123456789012", screen.Pin);

        screen.BackspaceCommand.Execute(null);
        Assert.Equal("12345678901", screen.Pin);
        screen.ClearPinCommand.Execute(null);
        Assert.Equal("", screen.Pin);
    }

    [Fact]
    public void An_unactivated_terminal_says_it_is_a_demo()
    {
        Assert.NotNull(Screen(activated: false).DemoNotice);
        Assert.Null(Screen(activated: true).DemoNotice);
    }

    [Fact]
    public void The_profile_falls_back_to_the_python_demo_store()
    {
        var profile = TerminalProfile.Load(_database);
        Assert.Equal(TerminalProfile.DemoTenantId, profile.TenantId);
        Assert.False(profile.Activated);

        _database.Execute(
            "INSERT INTO device_settings (key, value, updated_at) VALUES " +
            "('device.tenant_id', 't-real', 'x'), ('device.activated', '1', 'x'), ('store.name', 'Dolce Affetto', 'x')");
        profile = TerminalProfile.Load(_database);
        Assert.Equal(("t-real", true, "Dolce Affetto"), (profile.TenantId, profile.Activated, profile.StoreName));
    }
}
