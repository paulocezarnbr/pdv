using System.Text.Json;
using Pdv.Core.Scale;
using Pdv.Core.Stock;

namespace Pdv.Core.Tests;

/// <summary>
/// A leitura da balança e o preço por peso contra <c>contracts/scale-weighing.json</c>,
/// gerado pelo Python: o mesmo quadro tem de dar o mesmo peso, o mesmo status e
/// a mesma prova gravada.
/// </summary>
public sealed class ScaleContractTests
{
    private static readonly JsonElement Contract =
        JsonDocument.Parse(File.ReadAllText(TestDatabase.Contract("scale-weighing.json"))).RootElement.Clone();

    private static readonly DateTimeOffset At = new(2026, 9, 25, 12, 0, 0, TimeSpan.Zero);

    public static TheoryData<string, int> Frames()
    {
        var data = new TheoryData<string, int>();
        foreach (var protocol in Contract.GetProperty("frames").EnumerateObject())
        {
            for (var index = 0; index < protocol.Value.GetArrayLength(); index++) data.Add(protocol.Name, index);
        }
        return data;
    }

    [Theory]
    [MemberData(nameof(Frames))]
    public void Every_frame_reads_as_in_python(string protocol, int index)
    {
        var expected = Contract.GetProperty("frames").GetProperty(protocol)[index];
        var frame = Convert.FromHexString(expected.GetProperty("frame_hex").GetString()!);
        var parser = ScaleProtocols.Build(protocol);

        if (expected.TryGetProperty("error", out _))
        {
            Assert.Throws<ScaleFrameException>(() => parser.Parse(frame, At));
            return;
        }

        var reading = parser.Parse(frame, At);
        Assert.Equal(expected.GetProperty("status").GetString(), reading.StatusName);
        Assert.Equal(expected.GetProperty("weight_grams").GetInt64(), reading.WeightGrams);
        Assert.Equal(expected.GetProperty("raw_frame").GetString(), reading.RawFrame);
    }

    [Fact]
    public void The_contract_covers_every_protocol_and_every_status()
    {
        Assert.Equal(ScaleProtocols.Names.Order(), Contract.GetProperty("frames").EnumerateObject().Select(p => p.Name).Order());
        var statuses = Contract.GetProperty("frames").EnumerateObject()
            .SelectMany(protocol => protocol.Value.EnumerateArray())
            .Where(frame => frame.TryGetProperty("status", out _))
            .Select(frame => frame.GetProperty("status").GetString())
            .ToHashSet();
        Assert.Superset(new HashSet<string?> { "stable", "unstable", "overload", "negative", "zero" }, statuses);
    }

    [Fact]
    public void The_last_complete_frame_wins_and_the_buffer_is_capped()
    {
        foreach (var expected in Contract.GetProperty("buffers").EnumerateArray())
        {
            var buffer = Convert.FromHexString(expected.GetProperty("buffer_hex").GetString()!);
            var (frame, remaining) = ScaleProtocols.ExtractLastFrame(buffer, ScaleProtocol.Stx, ScaleProtocol.Etx);

            var frameHex = expected.GetProperty("frame_hex");
            if (frameHex.ValueKind == JsonValueKind.Null) Assert.Null(frame);
            else Assert.Equal(frameHex.GetString(), Convert.ToHexString(frame!).ToLowerInvariant());
            Assert.Equal(expected.GetProperty("remaining_hex").GetString(), Convert.ToHexString(remaining).ToLowerInvariant());
        }
    }

    [Fact]
    public void Net_weight_and_price_are_the_pythons()
    {
        foreach (var expected in Contract.GetProperty("weights").EnumerateArray())
        {
            var price = expected.GetProperty("price_cents_per_kg").GetInt64();
            var gross = expected.GetProperty("gross_grams").GetInt64();
            var tare = expected.GetProperty("tare_grams").GetInt64();

            if (expected.TryGetProperty("error", out _))
            {
                Assert.Throws<InvalidQuantityException>(() => WeightPricing.NetWeight(gross, tare));
                continue;
            }
            var net = WeightPricing.NetWeight(gross, tare);
            Assert.Equal(expected.GetProperty("net_grams").GetInt64(), net);
            Assert.Equal(expected.GetProperty("total_cents").GetInt64(), WeightPricing.PriceForWeight(price, net));
        }
    }

    [Fact]
    public void The_discount_rounds_half_to_even_as_in_python()
    {
        foreach (var expected in Contract.GetProperty("discounts").EnumerateArray())
        {
            var percent = decimal.Parse(expected.GetProperty("percent").GetString()!, System.Globalization.CultureInfo.InvariantCulture);
            Assert.Equal(
                expected.GetProperty("discount_cents").GetInt64(),
                WeightPricing.DiscountFor(expected.GetProperty("subtotal_cents").GetInt64(), percent));
        }
        Assert.Throws<InvalidQuantityException>(() => WeightPricing.DiscountFor(1000, 100.01m));
        Assert.Throws<InvalidQuantityException>(() => WeightPricing.DiscountFor(1000, -1m));
    }

    [Fact]
    public void An_unknown_protocol_names_the_known_ones()
    {
        var error = Assert.Throws<ScaleFrameException>(() => ScaleProtocols.Build("toledo"));
        Assert.Contains("filizola, toledo_prix3, urano", error.Message);
    }

    // -- estabilidade --------------------------------------------------------

    private static ScaleReading Stable(long grams) => new(ScaleStatus.Stable, grams, grams.ToString("00000"), At);

    private static ScaleReading Unstable() => new(ScaleStatus.Unstable, 0, "IIIII", At);

    [Fact]
    public void The_weight_is_only_sellable_after_three_equal_readings()
    {
        var tracker = new StabilityTracker();
        Assert.Null(tracker.Evaluate(Stable(847)).Stable);
        Assert.Null(tracker.Evaluate(Stable(847)).Stable);
        Assert.Null(tracker.LastStable);

        var third = tracker.Evaluate(Stable(847));
        Assert.Equal(847, third.Stable!.WeightGrams);
        Assert.Equal(847, tracker.LastStable!.WeightGrams);

        // A quarta leitura igual não anuncia de novo.
        Assert.Null(tracker.Evaluate(Stable(847)).Stable);
    }

    [Fact]
    public void A_wobble_restarts_the_count()
    {
        var tracker = new StabilityTracker();
        tracker.Evaluate(Stable(847));
        tracker.Evaluate(Stable(847));
        tracker.Evaluate(Stable(850));
        Assert.Null(tracker.Evaluate(Stable(850)).Stable);
        Assert.Equal(850, tracker.Evaluate(Stable(850)).Stable!.WeightGrams);
    }

    [Fact]
    public void Taking_the_goods_off_invalidates_the_stable_weight()
    {
        var tracker = new StabilityTracker();
        for (var i = 0; i < 3; i++) tracker.Evaluate(Stable(847));

        var (changed, stable) = tracker.Evaluate(Stable(0));
        Assert.True(changed);
        Assert.Null(stable);
        Assert.Null(tracker.LastStable);

        for (var i = 0; i < 3; i++) tracker.Evaluate(Stable(500));
        Assert.True(tracker.Evaluate(Unstable()).Changed);
        Assert.Null(tracker.LastStable);
        // Sem estável anunciado, a instabilidade não é "mudança".
        Assert.False(tracker.Evaluate(Unstable()).Changed);
    }

    // -- o leitor ------------------------------------------------------------

    private sealed class ScriptedDriver(params Func<ScaleReading>[] script) : IScaleDriver
    {
        private int _next;

        public bool IsOpen { get; private set; }

        public bool FailOpen { get; init; }

        public void Open()
        {
            if (FailOpen) throw new ScaleNotConnectedException("COM9 não existe");
            IsOpen = true;
        }

        public void Close() => IsOpen = false;

        public ScaleReading Read() => script[Math.Min(_next++, script.Length - 1)]();

        public void Dispose() => Close();
    }

    [Fact]
    public async Task A_timeout_is_reported_only_when_it_persists()
    {
        var errors = new List<string>();
        var monitor = new ScaleMonitor(
            new ScriptedDriver(() => throw new ScaleTimeoutException("sem resposta")), TimeSpan.FromMilliseconds(1));
        monitor.ErrorOccurred += errors.Add;

        for (var i = 0; i < 30; i++) monitor.Tick();

        Assert.Equal(2, errors.Count); // na 3ª e na 30ª
        await monitor.DisposeAsync();
    }

    [Fact]
    public async Task The_monitor_announces_stable_then_change()
    {
        var events = new List<string>();
        var monitor = new ScaleMonitor(
            new ScriptedDriver(() => Stable(847), () => Stable(847), () => Stable(847), () => Stable(0)),
            TimeSpan.FromMilliseconds(1));
        monitor.StableWeight += reading => events.Add($"estável {reading.WeightGrams}");
        monitor.WeightChanged += () => events.Add("mudou");

        for (var i = 0; i < 4; i++) monitor.Tick();

        Assert.Equal(["estável 847", "mudou"], events);
        Assert.Null(monitor.LastStable);
        await monitor.DisposeAsync();
    }

    [Fact]
    public async Task A_port_that_does_not_open_is_an_error_not_a_crash()
    {
        var errors = new List<string>();
        var connected = new List<bool>();
        var monitor = new ScaleMonitor(new ScriptedDriver(() => Stable(1)) { FailOpen = true }, TimeSpan.FromMilliseconds(1));
        monitor.ErrorOccurred += errors.Add;
        monitor.ConnectionChanged += connected.Add;

        monitor.Start();
        await monitor.StopAsync();

        Assert.Equal(["COM9 não existe"], errors);
        Assert.Equal([false], connected);
    }

    [Fact]
    public async Task The_simulated_scale_settles_through_the_toledo_parser()
    {
        var scale = new SimulatedScale(targetGrams: 1234, settleAfter: 2);
        var monitor = new ScaleMonitor(scale, TimeSpan.FromMilliseconds(1));
        scale.Open();
        for (var i = 0; i < 5; i++) monitor.Tick();

        Assert.Equal(1234, monitor.LastStable!.WeightGrams);
        Assert.Equal("01234", monitor.LastStable.RawFrame);
        await monitor.DisposeAsync();
    }
}
