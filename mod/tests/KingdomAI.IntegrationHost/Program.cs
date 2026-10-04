using System.Globalization;
using System.Text.Json;
using KingdomAI.Bridge;

// This executable contains no game, Unity or MelonLoader references. Only the
// production wire contract, command engine and HTTP server are linked above.
var arguments = new Dictionary<string, string>(StringComparer.Ordinal);
for (var i = 0; i < args.Length; i += 2)
{
    if (i + 1 >= args.Length || args[i] is not ("--bridge-config" or "--stop-file"))
        throw new ArgumentException("Usage: --bridge-config <temporary absolute config> [--stop-file <temporary marker>]");
    arguments.Add(args[i], args[i + 1]);
}
if (!arguments.TryGetValue("--bridge-config", out var configPath))
    throw new ArgumentException("A temporary --bridge-config is required.");
var tempRoot = Path.GetFullPath(Path.GetTempPath()).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
void RequireTemporaryPath(string path)
{
    if (!Path.IsPathFullyQualified(path) || !Path.GetFullPath(path).StartsWith(tempRoot, StringComparison.OrdinalIgnoreCase))
        throw new ArgumentException("The integration host accepts only temporary test config and stop-file paths.");
}
RequireTemporaryPath(configPath);
if (new FileInfo(configPath).Length > 16000) throw new ArgumentException("Oversized integration config.");
var config = JsonSerializer.Deserialize<BridgeConfig>(File.ReadAllText(configPath), Wire.Json)
    ?? throw new ArgumentException("Empty integration config.");
var uri = new Uri(config.BaseUrl);
if (uri.Port is 48860 or 48861) throw new ArgumentException("Use a spare loopback port; production companion/bridge ports are reserved.");
arguments.TryGetValue("--stop-file", out var stopFile);
if (stopFile is not null) RequireTemporaryPath(stopFile);

using var cancellation = new CancellationTokenSource();
Console.CancelKeyPress += (_, e) => { e.Cancel = true; cancellation.Cancel(); };
_ = Task.Run(() =>
{
    while (Console.ReadLine() is { } line)
        if (line.Trim().Equals("exit", StringComparison.OrdinalIgnoreCase)) { cancellation.Cancel(); break; }
});
var engine = new CommandEngine();
var simulation = new SyntheticP2();
var initial = simulation.Capture(engine);
engine.Observe(initial);
initial = simulation.Capture(engine);
engine.Observe(initial);
using var server = new BridgeServer(config, engine, initial);
Console.WriteLine($"SIMULATION ONLY: real bridge HTTP contract at {config.BaseUrl}; no game or model requests.");
Console.WriteLine("Stop with Ctrl+C, stdin 'exit', or the configured temporary stop-file.");
var previous = Environment.TickCount64;
try
{
    while (!cancellation.IsCancellationRequested && (stopFile is null || !File.Exists(stopFile)))
    {
        var now = Environment.TickCount64;
        var elapsed = Math.Clamp((now - previous) / 1000.0, 0, 0.1);
        previous = now;
        if (engine.TakeReleaseRequest()) engine.CompleteRelease(simulation.Release());
        engine.Tick(now, simulation.Capture(engine), simulation.CompletedPayment);
        engine.ExecuteInput(now, input => simulation.Apply(input, now, elapsed));
        engine.Tick(now, simulation.Capture(engine), simulation.CompletedPayment);
        if (engine.TakeReleaseRequest()) engine.CompleteRelease(simulation.Release());
        var observation = simulation.Capture(engine);
        engine.Observe(observation);
        server.Publish(observation);
        await Task.Delay(16, cancellation.Token);
    }
}
catch (OperationCanceledException) when (cancellation.IsCancellationRequested) { }
finally
{
    engine.Stop("integration host exit");
    simulation.Release();
    engine.CompleteRelease(true);
}
Console.WriteLine("Synthetic host stopped; all synthetic input released.");

sealed class SyntheticP2
{
    private readonly string _session = "synthetic-http-" + Guid.NewGuid().ToString("N");
    private long _sequence;
    private double _x;
    private int _coins = 8;
    private int _direction;
    private bool _pay;
    private bool _transactionPending;
    private string? _paymentAction;
    private int _inserted;
    private long _nextCoin;
    private bool _targetPaid;
    public string? CompletedPayment { get; private set; }
    private string TargetId => _session + ":three-coin-wall";

    public Observation Capture(CommandEngine engine)
    {
        var target = _targetPaid ? null : new PayableState(TargetId, "SYNTHETIC three-coin target", 0, 3, "coins", true);
        return new("0.3.1", "2.4.2", _session, ++_sequence, DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture),
            "SyntheticOfflineIsland", true, "SIMULATION: no game is connected", true, 1,
            engine.InputReleased && _direction == 0 && !_pay,
            [new(0, 10, 15, 1, true, null, false), new(1, _x, _coins, 1, true, target, _transactionPending, true)],
            ["move", "move_long", "sprint", "stop", "pay", "pay_coin", "dialogue_bubble"], ["Synthetic fixture: success verifies protocol compatibility only"],
            new(false, false, 1, [], []), ControlEpoch: engine.ControlEpoch);
    }

    public bool Apply(ControlInput input, long now, double elapsed)
    {
        _direction = input.Direction;
        _pay = input.Pay;
        _x += _direction * (input.Sprint ? 8 : 4) * elapsed;
        if (!input.Pay) return true;
        if (input.PayDown)
        {
            if (_targetPaid || input.TargetId != TargetId || input.MaxCoins < 3 || _coins < 3) return false;
            _paymentAction = input.ActionId;
            _inserted = 0;
            _transactionPending = true;
            _nextCoin = now + 100;
        }
        if (_transactionPending && _paymentAction == input.ActionId && now >= _nextCoin)
        {
            _coins--;
            _inserted++;
            _nextCoin = now + 100;
            if (_inserted == 3)
            {
                _targetPaid = true;
                _transactionPending = false;
                CompletedPayment = _paymentAction;
                _inserted = 0;
            }
        }
        return true;
    }

    public bool Release()
    {
        _direction = 0;
        _pay = false;
        if (_transactionPending) _coins += _inserted;
        _transactionPending = false;
        _inserted = 0;
        _paymentAction = null;
        return true;
    }
}
