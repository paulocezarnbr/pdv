using System.Diagnostics;
using System.Text.Json;
using Pdv.Data;
using Pdv.Data.Auth;

namespace Pdv.Core.Tests;

/// <summary>
/// Login por PIN contra <c>contracts/pin-hashes.json</c> (gerado pelo Python) e
/// o freio de tentativas sobre a mesma <c>auth_throttle</c>.
/// </summary>
public sealed class AuthTests : IDisposable
{
    private static readonly JsonElement Contract =
        JsonDocument.Parse(File.ReadAllText(TestDatabase.Contract("pin-hashes.json"))).RootElement.Clone();

    private static readonly JsonElement First = Contract.GetProperty("hashes")[0];
    private static readonly string Pin = First.GetProperty("pin").GetString()!;
    private static readonly string PythonHash = First.GetProperty("hash").GetString()!;

    private readonly TestDatabase _file = new();
    private readonly PdvDatabase _database;
    private readonly ManualClock _clock = new(new DateTimeOffset(2026, 9, 25, 12, 0, 0, TimeSpan.Zero));

    public AuthTests()
    {
        _database = new PdvDatabase(_file.Path);
        AddUser("u-caixa", "Ana Caixa", "ana", "cashier", PythonHash, canAuthorize: false);
        AddUser("u-gerente", "Bruno Gerente", "bruno", "manager", PythonHash, canAuthorize: true, discount: "30");
    }

    public void Dispose()
    {
        _database.Dispose();
        _file.Dispose();
    }

    private void AddUser(string id, string name, string login, string role, string? hash, bool canAuthorize, string discount = "0") =>
        _database.Execute(
            "INSERT INTO users (id, tenant_id, name, login, role, pin_hash, max_discount_percent, can_authorize, is_active, updated_at) " +
            "VALUES ($id, 'tenant-1', $name, $login, $role, $hash, $discount, $auth, 1, '2026-09-25T12:00:00.000+00:00')",
            ("$id", id), ("$name", name), ("$login", login), ("$role", role), ("$hash", hash),
            ("$discount", discount), ("$auth", canAuthorize ? 1 : 0));

    private StaffAuthentication Auth() => new(_database, "tenant-1", _clock);

    // -- contrato com o Python ----------------------------------------------

    public static TheoryData<int> HashCases()
    {
        var data = new TheoryData<int>();
        for (var i = 0; i < Contract.GetProperty("hashes").GetArrayLength(); i++) data.Add(i);
        return data;
    }

    [Theory]
    [MemberData(nameof(HashCases))]
    public void Every_hash_python_wrote_verifies_here(int index)
    {
        var item = Contract.GetProperty("hashes")[index];
        var hash = item.GetProperty("hash").GetString()!;
        Assert.True(PinHasher.Verify(hash, item.GetProperty("pin").GetString()!));
        foreach (var wrong in item.GetProperty("wrong").EnumerateArray())
        {
            Assert.False(PinHasher.Verify(hash, wrong.GetString()!));
        }
    }

    public static TheoryData<int> PolicyCases()
    {
        var data = new TheoryData<int>();
        for (var i = 0; i < Contract.GetProperty("policy").GetArrayLength(); i++) data.Add(i);
        return data;
    }

    [Theory]
    [MemberData(nameof(PolicyCases))]
    public void The_pin_policy_gives_the_pythons_verdict(int index)
    {
        var item = Contract.GetProperty("policy")[index];
        var pin = item.GetProperty("pin").GetString()!;
        if (item.GetProperty("accepted").GetBoolean())
        {
            Assert.Equal(item.GetProperty("normalized").GetString(), PinPolicy.Validate(pin));
        }
        else
        {
            var error = Assert.Throws<WeakPinException>(() => PinPolicy.Validate(pin));
            Assert.Equal(item.GetProperty("message").GetString(), error.Message);
        }
    }

    [Fact]
    public void The_throttle_numbers_are_the_pythons()
    {
        var throttle = Contract.GetProperty("throttle");
        Assert.Equal(StaffAuthentication.MaxAttempts, throttle.GetProperty("max_attempts").GetInt32());
        Assert.Equal(StaffAuthentication.MaxGlobalAttempts, throttle.GetProperty("max_global_attempts").GetInt32());
        Assert.Equal(StaffAuthentication.LockoutBaseSeconds, throttle.GetProperty("lockout_base_seconds").GetInt32());
        Assert.Equal(StaffAuthentication.LockoutMaxSeconds, throttle.GetProperty("lockout_max_seconds").GetInt32());
        Assert.Equal(StaffAuthentication.MaxExponent, throttle.GetProperty("max_exponent").GetInt32());
        Assert.Equal((int)StaffAuthentication.FailureWindow.TotalSeconds, throttle.GetProperty("failure_window_seconds").GetInt32());
        Assert.Equal(StaffAuthentication.GlobalScope, throttle.GetProperty("global_scope").GetString());
    }

    [Fact]
    public void A_hash_made_here_has_the_python_shape_and_verifies()
    {
        var hash = PinHasher.Hash("480362");
        Assert.StartsWith("$argon2id$v=19$m=65536,t=3,p=4$", hash);
        Assert.DoesNotContain("=", hash.Split('$')[4] + hash.Split('$')[5]);
        Assert.True(PinHasher.Verify(hash, "480362"));
        Assert.False(PinHasher.Verify(hash, "480363"));
    }

    [Theory]
    [InlineData("$argon2i$v=19$m=65536,t=3,p=4$c29tZXNhbHRzb21lc2FsdA$RdescudvJCsgt3ub+b+dWRWJTmaaJObG")]
    [InlineData("$argon2id$v=19$m=4194304,t=3,p=4$c29tZXNhbHRzb21lc2FsdA$RdescudvJCsgt3ub+b+dWRWJTmaaJObG")]
    [InlineData("$argon2id$v=19$m=65536,t=3,p=4$@@@$RdescudvJCsgt3ub+b+dWRWJTmaaJObG")]
    [InlineData("texto-qualquer")]
    [InlineData("")]
    public void A_strange_hash_is_never_authorized_and_never_hangs(string hash)
    {
        var watch = Stopwatch.StartNew();
        Assert.False(PinHasher.Verify(hash, "480362"));
        Assert.True(watch.Elapsed < TimeSpan.FromSeconds(2), "hash plantado com memória absurda travaria o caixa");
    }

    // -- login --------------------------------------------------------------

    [Fact]
    public void An_operator_enters_with_the_pin_python_registered()
    {
        var identity = Auth().Authenticate("  ANA ", Pin);
        Assert.Equal("u-caixa", identity.Id);
        Assert.Equal("Ana", identity.FirstName);
    }

    [Fact]
    public void Enter_without_a_pin_is_just_a_wrong_pin()
    {
        var auth = Auth();
        var error = Assert.Throws<AuthenticationException>(() => auth.Authenticate("ana", ""));
        Assert.Equal("Login ou PIN inválido.", error.Message);
        Assert.Equal(1L, _database.Scalar("SELECT failures FROM auth_throttle WHERE scope = 'login:ana'"));
    }

    [Fact]
    public void A_wrong_pin_and_an_unknown_login_say_the_same_thing()
    {
        var auth = Auth();
        var wrong = Assert.Throws<AuthenticationException>(() => auth.Authenticate("ana", "999999"));
        var unknown = Assert.Throws<AuthenticationException>(() => auth.Authenticate("fantasma", Pin));
        Assert.Equal(wrong.Message, unknown.Message);
    }

    [Fact]
    public void Five_mistakes_lock_the_login_even_for_the_right_pin()
    {
        var auth = Auth();
        for (var i = 0; i < StaffAuthentication.MaxAttempts; i++)
        {
            Assert.Throws<AuthenticationException>(() => auth.Authenticate("ana", "999999"));
        }

        var locked = Assert.Throws<AuthenticationException>(() => auth.Authenticate("ana", Pin));
        Assert.Contains("Aguarde", locked.Message);
        Assert.Equal(31, auth.LockStatus("ana"));

        _clock.Advance(TimeSpan.FromSeconds(31));
        Assert.Equal("u-caixa", auth.Authenticate("ana", Pin).Id);
    }

    [Fact]
    public void A_lock_survives_a_restart()
    {
        var auth = Auth();
        for (var i = 0; i < StaffAuthentication.MaxAttempts; i++)
        {
            Assert.Throws<AuthenticationException>(() => auth.Authenticate("ana", "999999"));
        }
        // Outro processo (ou o PDV em Python) lê o mesmo auth_throttle.
        Assert.Throws<AuthenticationException>(() => Auth().Authenticate("ana", Pin));
    }

    [Fact]
    public void Setting_the_windows_clock_back_does_not_shorten_the_lock()
    {
        var auth = Auth();
        for (var i = 0; i < StaffAuthentication.MaxAttempts; i++)
        {
            Assert.Throws<AuthenticationException>(() => auth.Authenticate("ana", "999999"));
        }
        // Relógio de parede adiantado 1 h (o registro no banco "venceu"), e o
        // monotônico andou só 5 s: continua bloqueado.
        _clock.SetWallClock(_clock.GetUtcNow().AddHours(1));
        Assert.True(auth.LockStatus("ana") > 0);
    }

    [Fact]
    public void A_lock_written_by_the_python_pdv_holds_here()
    {
        _database.Execute(
            "INSERT INTO auth_throttle (scope, failures, locked_until, first_failure_at, last_failure_at) " +
            "VALUES ('login:ana', 5, '2026-09-25T12:02:00.000+00:00', '2026-09-25T11:59:00.000+00:00', '2026-09-25T12:00:00.000+00:00')");
        var error = Assert.Throws<AuthenticationException>(() => Auth().Authenticate("ana", Pin));
        Assert.Contains("Aguarde 121s", error.Message);
    }

    [Fact]
    public void Success_clears_the_login_but_not_the_terminal_count()
    {
        var auth = Auth();
        Assert.Throws<AuthenticationException>(() => auth.Authenticate("ana", "999999"));
        auth.Authenticate("ana", Pin);

        Assert.Null(_database.Scalar("SELECT failures FROM auth_throttle WHERE scope = 'login:ana'"));
        Assert.Equal(1L, _database.Scalar("SELECT failures FROM auth_throttle WHERE scope = '*'"));
    }

    [Fact]
    public void Old_mistakes_stop_counting_after_an_hour()
    {
        var auth = Auth();
        for (var i = 0; i < StaffAuthentication.MaxAttempts - 1; i++)
        {
            Assert.Throws<AuthenticationException>(() => auth.Authenticate("ana", "999999"));
        }
        _clock.Advance(TimeSpan.FromHours(1) + TimeSpan.FromSeconds(1));
        Assert.Throws<AuthenticationException>(() => auth.Authenticate("ana", "999999"));
        Assert.Equal(0, auth.LockStatus("ana"));
    }

    [Fact]
    public void A_cashier_cannot_authorize_and_insisting_counts()
    {
        var auth = Auth();
        var error = Assert.Throws<AuthenticationException>(() => auth.Authorize("ana", Pin));
        Assert.Contains("não tem permissão", error.Message);
        Assert.Equal(1L, _database.Scalar("SELECT failures FROM auth_throttle WHERE scope = 'login:ana'"));

        var manager = auth.Authorize("bruno", Pin);
        Assert.Equal(30m, manager.MaxDiscountPercent);
    }
}
