using System.Net;
using System.Net.Http.Json;
using KingdomAI.Bridge;

var checks = 0;
void Check(bool result, string message) { checks++; if (!result) throw new Exception(message); }
void Reject(Action action, int code)
{
    try { action(); throw new Exception("Expected rejection"); }
    catch (CommandError e) { Check(e.Code == code, "Unexpected rejection status"); }
}
var target = new PayableState("session:target", "Camp", 1, 3, "coins", true);
var p2 = new PlayerState(1, 1, 20, 1, true, target, false, true);
var state = new Observation("test", "2.4.2", "session", 1, DateTimeOffset.UtcNow.ToString("O"), "test", true, "ready", true, 1, true,
    new[] { new PlayerState(0, 2, 0, 1, true, null, false), p2 }, new[] { "move", "move_long", "sprint", "stop", "pay", "pay_coin" }, Array.Empty<string>());
var controlId = Guid.NewGuid().ToString();
var controlEpoch = 0L;
Command Move() => new(Guid.NewGuid().ToString(), "session", 1, "move", "left", 500, ControlId: controlId, ControlEpoch: controlEpoch);
var engine = new CommandEngine();
void Lease(long time) { controlId = Guid.NewGuid().ToString(); controlEpoch = engine.ControlEpoch; engine.Heartbeat(controlId, state.SessionId, controlEpoch, time); }
engine.Observe(state);
var idleEpoch = engine.ControlEpoch;
engine.Tick(500, state);
Check(engine.ControlEpoch == idleEpoch, "idle snapshots do not invalidate a possible new lease");
var delayedInitialControl = Guid.NewGuid().ToString();
engine.Stop("stop before a delayed first heartbeat");
Reject(() => engine.Heartbeat(delayedInitialControl, state.SessionId, idleEpoch, 600), 409);
Reject(() => engine.Submit(Move(), 1000), 409);
Lease(1000);
Reject(() => engine.Submit(Move() with { PlayerId = 0 }, 1000), 403);
Reject(() => engine.Submit(Move() with { SessionId = "old" }, 1000), 409);
Reject(() => engine.Submit(Move() with { DurationMs = 99 }, 1000), 400);
engine.Observe(state with { Players = new[] { state.Players[0], p2 with { TransactionPending = true } } });
Reject(() => engine.Submit(Move(), 1000), 409);
engine.Observe(state);
var move = Move();
var sprintEngine = new CommandEngine();
sprintEngine.Observe(state);
var sprintControl = Guid.NewGuid().ToString();
sprintEngine.Heartbeat(sprintControl, state.SessionId, sprintEngine.ControlEpoch, 1000);
var longSprint = Move() with { DurationMs = 5000, Sprint = true, ControlId = sprintControl, ControlEpoch = sprintEngine.ControlEpoch };
sprintEngine.Submit(longSprint, 1000); sprintEngine.Tick(1000, state);
Check(sprintEngine.ReadInput(1000) is { Direction: -1, Sprint: true }, "long move starts native sprint");
sprintEngine.Heartbeat(sprintControl, state.SessionId, sprintEngine.ControlEpoch, 3000);
Check(sprintEngine.ReadInput(3400) is { Direction: -1, Sprint: true }, "bounded move continues with live heartbeat");
sprintEngine.Observe(state with { Players = new[] { state.Players[0], p2 with { CanSprint = false } } });
Check(sprintEngine.ReadInput(3401) is { Direction: -1, Sprint: true }, "native game handles exhaustion without an additional framework gate");
sprintEngine.Stop("manual mid sprint");
Check(sprintEngine.ReadInput(3402) is null && sprintEngine.GetReceipt(longSprint.ActionId)?.Status == "cancelled", "stop cancels long movement immediately");
sprintEngine.CompleteRelease(true);
sprintControl = Guid.NewGuid().ToString();
sprintEngine.Heartbeat(sprintControl, state.SessionId, sprintEngine.ControlEpoch, 4000);
sprintEngine.Submit(longSprint with { ActionId = Guid.NewGuid().ToString(), ControlId = sprintControl, ControlEpoch = sprintEngine.ControlEpoch }, 4000);
sprintEngine.Tick(4000, state);
Check(sprintEngine.ReadInput(6500) is null, "long sprint never extends watchdog deadline");
sprintEngine.CompleteRelease(true);
sprintControl = Guid.NewGuid().ToString();
sprintEngine.Heartbeat(sprintControl, state.SessionId, sprintEngine.ControlEpoch, 7000);
sprintEngine.Submit(longSprint with { ActionId = Guid.NewGuid().ToString(), ControlId = sprintControl, ControlEpoch = sprintEngine.ControlEpoch }, 7000);
sprintEngine.Tick(7000, state);
sprintEngine.Heartbeat(sprintControl, state.SessionId, sprintEngine.ControlEpoch, 9000);
sprintEngine.Heartbeat(sprintControl, state.SessionId, sprintEngine.ControlEpoch, 11000);
Check(sprintEngine.ReadInput(12001) is { Direction: 0, Sprint: false }, "expired move has neutral input even before next tick");
sprintEngine.Stop("done"); sprintEngine.CompleteRelease(true);
Check(engine.Submit(move, 1000).Status == "queued", "queue");
Reject(() => engine.Submit(Move(), 1000), 409);
engine.Tick(1000, state);
Check(engine.ReadInput(1000)?.Direction == -1, "native left");
Check(engine.ReadInput(1501)?.Direction == 0, "duration bound even between ticks");
engine.Stop("manual");
Check(engine.ReadInput(1000) is null && engine.TakeReleaseRequest(), "immediate release");
Check(!engine.InputReleased, "release awaits main thread acknowledgement");
engine.CompleteRelease(true);
var revokedControl = controlId;
Reject(() => engine.Heartbeat(revokedControl, state.SessionId, engine.ControlEpoch, 1100), 409);
Reject(() => engine.Submit(Move(), 1100), 409);
Lease(5000);
Reject(() => engine.Submit(Move() with { ControlId = revokedControl }, 5000), 409);
var concurrentMove = Move();
engine.Submit(concurrentMove, 5000); engine.Tick(5000, state);
using var nativeEntered = new ManualResetEventSlim();
using var nativeMayReturn = new ManualResetEventSlim();
var nativeTask = Task.Run(() => engine.ExecuteInput(5000, _ => {
    nativeEntered.Set();
    if (!nativeMayReturn.Wait(2000)) throw new Exception("native test callback timed out");
    return true;
}));
Check(nativeEntered.Wait(2000), "native callback entered");
var stopTask = Task.Run(() => engine.Stop("concurrent HTTP stop"));
await Task.Delay(30);
Check(!stopTask.IsCompleted, "stop cannot acknowledge while old native input is executing");
nativeMayReturn.Set(); await nativeTask; await stopTask;
Check(engine.ReadInput(5000) is null, "no stale input after stop acknowledgement");
engine.CompleteRelease(true);
Lease(6000);
Check(engine.Submit(move, 1000).Status == "cancelled", "duplicate receipt");
Reject(() => engine.Submit(move with { Direction = "right" }, 1000), 409);
var pay = new Command(Guid.NewGuid().ToString(), "session", 1, "pay", TargetId: target.TargetId, MaxCoins: 3, ControlId: controlId, ControlEpoch: controlEpoch);
Reject(() => engine.Submit(pay with { Operation = "pay_coin" }, 6000), 409);
engine.Submit(pay, 6000); engine.Tick(6000, state);
Check(engine.ReadInput(6000) is { Pay: true, PayDown: true, MaxCoins: 3 }, "pay budget and rising edge");
Check(engine.ReadInput(6001) is { Pay: true, PayDown: false }, "no repeated rising edge");
Check(engine.ReadInput(8500) is null && engine.TakeReleaseRequest(), "watchdog cancels in native input hook");
engine.CompleteRelease(true);
Check(engine.GetReceipt(pay.ActionId)?.Status == "cancelled", "expired payment not completed");
Reject(() => engine.Heartbeat(controlId, state.SessionId, engine.ControlEpoch, 8501), 409);
Lease(9000);
var pay2 = pay with { ActionId = Guid.NewGuid().ToString(), ControlId = controlId, ControlEpoch = controlEpoch };
engine.Submit(pay2, 9000); engine.Tick(9000, state);
engine.Tick(9100, state with { Players = new[] { state.Players[0], p2 with { Coins = 17 } } }, pay2.ActionId);
Check(engine.GetReceipt(pay2.ActionId)?.After?.Coins == 17 && engine.GetReceipt(pay2.ActionId)?.Status == "completed", "completion has observed snapshot");
engine.Stop("done");
engine.CompleteRelease(true);
engine.Tick(9200, state with { SessionId = "new-session" });
Reject(() => engine.Submit(pay2, 9200), 409);
Reject(() => engine.Heartbeat(controlId, state.SessionId, engine.ControlEpoch, 9200), 409);

var now = Environment.TickCount64;
engine.Observe(state); Lease(now);
var listenerProbe = new System.Net.Sockets.TcpListener(IPAddress.Loopback, 0);
listenerProbe.Start(); var port = ((IPEndPoint)listenerProbe.LocalEndpoint).Port; listenerProbe.Stop();
var token = new string('a', 48);
using var server = new BridgeServer(new($"http://127.0.0.1:{port}", token), engine, state);
using var client = new HttpClient { BaseAddress = new Uri($"http://127.0.0.1:{port}"), Timeout = TimeSpan.FromSeconds(3) };
Check((await client.GetAsync("/state")).StatusCode == HttpStatusCode.Unauthorized, "token required");
client.DefaultRequestHeaders.Authorization = new("Bearer", token);
Check((await client.GetAsync("/state")).IsSuccessStatusCode, "authenticated snapshot");
client.DefaultRequestHeaders.Add("Origin", $"http://127.0.0.1:{port}");
Check((await client.PostAsync("/stop", null)).StatusCode == HttpStatusCode.Unauthorized, "browser cannot use game token");
client.DefaultRequestHeaders.Remove("Origin");
Check((await client.PostAsJsonAsync("/dialogue", new { message_id = "old", text = "旧会话", session_id = "old" })).StatusCode == HttpStatusCode.Conflict,
    "dialogue refuses stale game session");
Check((await client.PostAsJsonAsync("/dialogue", new { message_id = "empty", text = " ", session_id = state.SessionId })).StatusCode == HttpStatusCode.BadRequest,
    "dialogue refuses empty text");
Check((await client.PostAsJsonAsync("/dialogue", new { message_id = "inject", text = "不能附带操作", session_id = state.SessionId, operation = "pay" })).StatusCode == HttpStatusCode.BadRequest,
    "dialogue cannot carry a game command");
Check((await client.PostAsJsonAsync("/heartbeat", new { control_id = controlId, session_id = state.SessionId, control_epoch = controlEpoch })).IsSuccessStatusCode, "authenticated heartbeat lease");
var httpMove = Move();
var accepted = await client.PostAsJsonAsync("/command", httpMove, Wire.Json);
Check(accepted.IsSuccessStatusCode, "HTTP command queued");
Check((await client.PostAsync("/stop", null)).IsSuccessStatusCode && !engine.InputReleased, "HTTP stop queues physical release");
engine.CompleteRelease(true);
Check((await client.PostAsJsonAsync("/heartbeat", new { control_id = controlId, session_id = state.SessionId, control_epoch = controlEpoch })).StatusCode == HttpStatusCode.Conflict,
    "late HTTP heartbeat cannot resume a stopped control ID");
Check((await client.PostAsJsonAsync("/command", Move(), Wire.Json)).StatusCode == HttpStatusCode.Conflict,
    "late HTTP command cannot restart stopped input");
var oldHttpControl = controlId;
var oldHttpEpoch = controlEpoch;
controlId = Guid.NewGuid().ToString();
Check((await client.PostAsJsonAsync("/heartbeat", new { control_id = controlId, session_id = state.SessionId, control_epoch = oldHttpEpoch })).StatusCode == HttpStatusCode.Conflict,
    "late first heartbeat cannot acquire control with the pre-stop epoch");
controlEpoch = engine.ControlEpoch;
Check((await client.PostAsJsonAsync("/heartbeat", new { control_id = controlId, session_id = state.SessionId, control_epoch = controlEpoch })).IsSuccessStatusCode,
    "explicit new run can acquire a new control ID");
Check((await client.PostAsJsonAsync("/command", Move() with { ControlId = oldHttpControl }, Wire.Json)).StatusCode == HttpStatusCode.Conflict,
    "old command stays revoked after a new run acquires control");
Check((await client.GetAsync("/commands/" + httpMove.ActionId)).IsSuccessStatusCode, "stable receipt URL");
Check((await client.PostAsync("/coop/open", null)).StatusCode == HttpStatusCode.Accepted && server.ConsumeCoopRequest(), "coop queued, no game access from HTTP thread");
server.Publish(state with { CapturedAt = DateTimeOffset.UtcNow.ToString("O") });
var dialogueEpoch = engine.ControlEpoch;
var dialogue = new { message_id = "assistant:1", text = "你好，我会陪着你。\n<b>这会按纯文本显示。</b>", session_id = state.SessionId };
var dialogueResponse = await client.PostAsJsonAsync("/dialogue", dialogue);
Check(dialogueResponse.IsSuccessStatusCode && (await dialogueResponse.Content.ReadAsStringAsync()).Contains("accepted"),
    "authorized idle dialogue is accepted without a control heartbeat");
Check((await (await client.PostAsJsonAsync("/dialogue", dialogue)).Content.ReadAsStringAsync()).Contains("duplicate"),
    "HTTP dialogue duplicate does not refresh bubble");
Check(server.Dialogue.Consume(Environment.TickCount64)?.Text == dialogue.text && engine.ControlEpoch == dialogueEpoch
    && engine.InputReleased, "dialogue stores pure text and never authorizes movement or spending");
Check((await client.PostAsJsonAsync("/dialogue", dialogue with { })).IsSuccessStatusCode, "duplicate dialogue remains idempotent");
using (var duplicateFields = new StringContent("{\"message_id\":\"first\",\"message_id\":\"second\",\"text\":\"hello\",\"session_id\":\"session\"}", System.Text.Encoding.UTF8, "application/json"))
    Check((await client.PostAsync("/dialogue", duplicateFields)).StatusCode == HttpStatusCode.BadRequest, "dialogue refuses repeated JSON fields");
for (var i = 0; i < 3; i++)
    Check((await client.PostAsJsonAsync("/dialogue", new { message_id = "burst:" + i, text = "最新一句" + i, session_id = state.SessionId })).IsSuccessStatusCode,
        "bounded dialogue burst accepted");
Check((await client.PostAsJsonAsync("/dialogue", new { message_id = "too-fast", text = "超限", session_id = state.SessionId })).StatusCode == HttpStatusCode.TooManyRequests,
    "HTTP dialogue rate limit is independent of command engine");
client.DefaultRequestHeaders.Add("Origin", $"http://127.0.0.1:{port}");
Check((await client.PostAsJsonAsync("/dialogue", dialogue)).StatusCode == HttpStatusCode.Unauthorized, "browser origin cannot access dialogue");
client.DefaultRequestHeaders.Remove("Origin");
client.DefaultRequestHeaders.Authorization = null;
Check((await client.PostAsJsonAsync("/dialogue", dialogue)).StatusCode == HttpStatusCode.Unauthorized, "dialogue requires existing bridge token");
checks += DialogueTests.Run(state);
checks += await SnapshotTests.Run(state);
var expanded = state with { Capabilities = ["move", "move_to", "sprint", "pay", "drop", "ability", "map", "sail", "extended_world"],
    Players = [state.Players[0], p2 with { Coins = 100, Currencies = new() { ["coins"] = 100, ["gems"] = 50 }, CurrentPayable = target with { Currency = "gems", Price = 30 } }] };
var extendedEngine = new CommandEngine(); extendedEngine.Observe(expanded);
var extendedControl = Guid.NewGuid().ToString();
extendedEngine.Heartbeat(extendedControl, expanded.SessionId, extendedEngine.ControlEpoch, 1000);
var extendedMove = Move() with { DurationMs = 20000, ControlId = extendedControl, ControlEpoch = extendedEngine.ControlEpoch };
extendedEngine.Submit(extendedMove, 1000); extendedEngine.Tick(1000, expanded);
for (var tick = 2000; tick <= 18000; tick += 2000) extendedEngine.Heartbeat(extendedControl, expanded.SessionId, extendedEngine.ControlEpoch, tick);
Check(extendedEngine.ReadInput(18001)?.Direction == -1, "movement can continue beyond five seconds with a live heartbeat");
extendedEngine.Stop("test stop"); extendedEngine.CompleteRelease(true);
extendedControl = Guid.NewGuid().ToString();
extendedEngine.Heartbeat(extendedControl, expanded.SessionId, extendedEngine.ControlEpoch, 19000);
var expandedPay = new Command(Guid.NewGuid().ToString(), expanded.SessionId, 1, "pay", TargetId: target.TargetId, Currency: "gems",
    ControlId: extendedControl, ControlEpoch: extendedEngine.ControlEpoch);
extendedEngine.Submit(expandedPay, 19000); extendedEngine.Tick(19000, expanded);
Check(extendedEngine.ReadInput(19001) is { Pay: true, Currency: "gems" }, "native payment supports other currencies and prices above twenty");
extendedEngine.Tick(19002, expanded, expandedPay.ActionId);
var auxiliary = new Command(Guid.NewGuid().ToString(), expanded.SessionId, 1, "map", MapAction: "open", ControlId: extendedControl, ControlEpoch: extendedEngine.ControlEpoch);
extendedEngine.Submit(auxiliary, 19003); extendedEngine.Tick(19003, expanded);
extendedEngine.ExecuteAuxiliary(19003, _ => new(true, "native map called", new { map_open = true }));
Check(extendedEngine.GetReceipt(auxiliary.ActionId)?.Effect is not null, "main thread auxiliary result is carried in the receipt");
var menuExpanded = expanded with { Ready = false, UiReady = true, Capabilities = ["map", "stop", "extended_world"] };
extendedEngine.Tick(19004, menuExpanded);
extendedEngine.Heartbeat(extendedControl, expanded.SessionId, extendedEngine.ControlEpoch, 19004);
Check(extendedEngine.Submit(auxiliary with { ActionId = Guid.NewGuid().ToString(), MapAction = "close" }, 19004).Status == "queued", "owned map accepts commands while world movement is unavailable");
extendedEngine.Stop("manual stop"); extendedEngine.CompleteRelease(true);
Reject(() => extendedEngine.Heartbeat(extendedControl, expanded.SessionId, extendedEngine.ControlEpoch, 19005), 409);
Console.WriteLine($"PASS: {checks} command isolation, cancellation, payment budget, heartbeat and HTTP boundary checks (no game execution).");
