using KingdomAI.Bridge;
using Il2Cpp;

var fixtureRoot = Path.Combine(Path.GetTempPath(), "kingdom-ai-domain-tests-" + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(fixtureRoot);
File.WriteAllText(Path.Combine(fixtureRoot, "BuildInfo.txt"), "Version: 2.4.2\nHash: 116fe7e048\n");
var assertions = 0;
long observationTime = 0;
void Check(bool result, string name)
{
    if (!result) throw new Exception("FAIL: " + name);
    assertions++;
}
(GameAccess Access, Player P2, Il2CppRewired.Player Rewired, Observation State) Fresh(int price = 1)
{
    Managers.Inst = new();
    observationTime = 0;
    UnityEngine.Resources.All.Clear();
    Menu.Inst = new();
    Managers.COOP_ENABLED = Managers.IsP2Playing = true;
    NetworkBigBoss.IsOnline = NetworkBigBoss.IsClientPresent = false;
    UnityEngine.Time.timeScale = 1;
    var p2 = Managers.Inst.kingdom.playerTwo;
    p2.selectedPayable = new() { Price = price };
    var access = new GameAccess(fixtureRoot, new HarmonyLib.Harmony(), () => observationTime);
    access.Capture(true);
    var rewired = new Il2CppRewired.Player(1);
    access.NoteInput(p2, rewired);
    var state = access.Capture(true);
    return (access, p2, rewired, state);
}
try
{
    var f = Fresh();
    Check(f.State.Ready, "only real P2 input enables readiness");
    var campaignIdentity = (Dictionary<string, object?>)f.State.World!.Campaign!;
    Check(campaignIdentity["started_at"] is 1728000123 && campaignIdentity["reign"] is 1,
        "stable campaign identity is read from audited native save members");
    Check(f.State.Diagnostics.Any(d => d.Contains("activePlayersCount=2")), "native IEnumerable player count is readable");
    UnityEngine.Input.F8Pressed = true;
    Check(f.Access.F8(), "F8 selects KeyCode overload when string overload also exists");
    UnityEngine.Input.F8Pressed = false;
    Check(!f.Access.F8(), "F8 does not report a key that is not pressed");
    Managers.Inst.enemies.AllEnemies.Add(new Enemy());
    observationTime += 260;
    Check(f.Access.Capture(true).World!.NearbyEnemies.Length == 1,
        "native ICollection without indexer is cast to typed IEnumerable before enumeration");
    Check(Il2CppSystem.NativeObject.Disposals > 0, "native enumerators are disposed after reads");
    Managers.Inst.payables.AllPayables = new[] { f.P2.selectedPayable! };
    observationTime += 1000;
    Check(f.Access.Capture(true).World!.NearbyPayables.Length == 1, "CLR array payable enumeration still works");
    f.P2._payState = PayState.Holding;
    Check(!f.Access.Apply(f.P2, new(1, false, false), f.Rewired), "takeover cannot cancel an existing human transaction");
    Check(f.P2.GroundDrops == 0 && f.P2.ReleaseCalls == 0 && f.P2.ActionCalls == 0,
        "human payment remains owned by the original game input");
    f.P2._payState = PayState.None;
    Check(f.State.P2!.CurrentPayable!.Currency == "coins", "currency is normalized for backend");
    Managers.Inst.game.state = 4;
    Check(!f.Access.Apply(f.P2, new(1, false, false), f.Rewired), "instant menu transition blocks stale ready snapshot");
    Check(f.P2.ActionCalls == 0, "menu transition invokes no native action");
    Managers.Inst.game.state = 1;
    Check(!f.Access.Capture(true).Ready, "intro is excluded even though game InPlayableState includes it");
    f = Fresh();
    NetworkBigBoss.IsOnline = true;
    Check(!f.Access.Apply(f.P2, new(1, false, false), f.Rewired), "online coop is excluded");
    f = Fresh();
    Check(!f.Access.Apply(f.P2, new(1, false, false), new Il2CppRewired.Player(0)), "Rewired P1 cannot control game P2");
    Managers.Inst.kingdom.Players.Add(new Player(1, 30));
    Check(!f.Access.Capture(true).Ready, "ambiguous duplicate P2 fails closed");
    f = Fresh();
    f.P2.gameObject.activeInHierarchy = false;
    Check(!f.Access.Apply(f.P2, new(1, false, false), f.Rewired), "inactive P2 cannot receive AI input");
    f = Fresh();
    f.P2._tunnel = true;
    Check(!f.Access.Apply(f.P2, new(1, false, false), f.Rewired), "special unit routing is preserved");
    f = Fresh();
    Managers.Inst.game._secondaryControllable = Managers.Inst.kingdom.playerOne;
    Check(!f.Access.Capture(true).Ready, "secondary input owner must be P2");

    foreach (var price in new[] { 1, 3 })
    {
        f = Fresh(price);
        var id = Guid.NewGuid().ToString();
        var input = new ControlInput(0, true, true, id, price, f.State.P2!.CurrentPayable!.TargetId);
        for (var frame = 0; frame < 40 && f.Access.LastCompletedPayActionId != id; frame++)
        {
            f.Access.NoteInput(f.P2, f.Rewired);
            Check(f.Access.Apply(f.P2, input, f.Rewired), "transaction native call allowed");
            if (f.P2._payState == PayState.Completed)
                Check(f.Access.LastCompletedPayActionId != id, "receipt waits for native completion animation");
        }
        Check(f.Access.LastCompletedPayActionId == id, "full native transaction completes");
        Check(f.P2.NativePayments == price && f.P2.wallet.Coins == 10 - price, "spends exact complete price");
        Check(f.P2.HeldAfterFull == 0 && f.P2.GroundDrops == 0, "release after full price cannot spill or repeat a coin");
        Check(f.Access.Capture(false).P2!.TransactionPending == false, "completion is observed only once transaction is idle");
        f.Access.Apply(f.P2, input, f.Rewired);
        Check(f.P2.NativePayments == price, "same action ID cannot replay payment");
    }
    f = Fresh(3);
    var rejectedId = Guid.NewGuid().ToString();
    f.Access.Apply(f.P2, new(0, true, true, rejectedId, 2, f.State.P2!.CurrentPayable!.TargetId), f.Rewired);
    Check(f.Access.LastPayFailureActionId != rejectedId, "legacy max coins does not restrict native payment");
    f.Access.Release();
    f = Fresh(3);
    f.P2.selectedPayable!.PayableNow = false;
    var unavailableId = Guid.NewGuid().ToString();
    f.Access.Apply(f.P2, new(0, true, true, unavailableId, 3, f.State.P2!.CurrentPayable!.TargetId), f.Rewired);
    Check(f.Access.LastPayFailureReason.Contains("CanPay=false") && f.P2.NativePayments == 0,
        "native CanPay gate reports a specific reason without spending");
    Check(f.Access.Capture(true).Diagnostics.Any(d => d.Contains("canPay=False") && d.Contains("lastPayFailure=")),
        "payment gate observations and most recent failure are exposed in diagnostics");
    f = Fresh(3);
    f.P2.keyDownThreshold = float.NaN;
    f.Access.Apply(f.P2, new(0, true, true, Guid.NewGuid().ToString(), 3, f.State.P2!.CurrentPayable!.TargetId), f.Rewired);
    Check(f.Access.LastPayFailureReason.Contains("时间参数") && f.P2.NativePayments == 0,
        "unsupported native timing values are distinguishable from budget and target failures");
    f = Fresh();
    var cancelledId = Guid.NewGuid().ToString();
    f.Access.Apply(f.P2, new(0, true, true, cancelledId, 1, f.State.P2!.CurrentPayable!.TargetId), f.Rewired);
    f.Access.Apply(f.P2, new(0, false, false), f.Rewired);
    Check(f.P2.GroundDrops == 0 && f.P2.ReleaseCalls == 1 && f.Access.LastPayFailureActionId == cancelledId,
        "cancel during Holding uses native ReleaseInput instead of dropping a ground coin");
    f = Fresh();
    Check(f.State.Capabilities.Contains("sprint") && f.State.Capabilities.Contains("move_long") && f.State.P2!.CanSprint == true,
        "sprint capability and native readiness are observable without guessing stamina units");
    Check(f.Access.Apply(f.P2, new(1, false, false, Sprint: true), f.Rewired), "sprint uses native P2 action");
    Check(f.P2.SprintStarts == 1 && f.P2.SprintHeld && !f.P2.SprintStopped && !f.P2.DoubleTap, "single sprint start and held input");
    f.Access.Apply(f.P2, new(1, false, false, Sprint: true), f.Rewired);
    Check(f.P2.SprintStarts == 1, "repeated frame does not repeat the sprint rising edge");
    f.Access.Apply(f.P2, new(-1, false, false, Sprint: true), f.Rewired);
    Check(f.P2.SprintStarts == 2 && f.P2.SprintHeld, "direction change starts a new native sprint edge");
    Check(Managers.Inst.kingdom.playerOne.ActionCalls == 0, "sprint cannot touch P1");
    f.P2.steed.IsTired = true;
    f.Access.Apply(f.P2, new(1, false, false, Sprint: true), f.Rewired);
    Check(f.P2.SprintHeld && f.Access.Capture(false).P2!.CanSprint == false, "framework forwards sprint while exposing native fatigue");
    f.P2.steed.IsTired = false;
    f.P2.steed.Stamina = 0;
    f.Access.Apply(f.P2, new(1, false, false, Sprint: true), f.Rewired);
    Check(f.P2.SprintHeld, "game owns the exhausted sprint behavior");
    f.P2.steed.Stamina = float.NaN;
    Check(f.Access.Capture(false).P2!.CanSprint == null, "unreadable stamina cannot authorize sprint");
    f.P2.steed.Stamina = 1;
    f.Access.Apply(f.P2, new(1, false, false, Sprint: true), f.Rewired);
    f.Access.Apply(f.P2, new(0, false, false), f.Rewired);
    Check(!f.P2.SprintHeld && f.P2.SprintStopped, "neutral input ends sprint");
    f.Access.Apply(f.P2, new(1, false, false, Sprint: true), f.Rewired);
    f.Access.Release();
    Check(!f.P2.SprintHeld, "takeover releases native sprint");
    f = Fresh();
    f.Access.Apply(f.P2, new(1, false, false), f.Rewired);
    f.Access.Release();
    f.Access.Release();
    Check(f.P2.ReleaseCalls == 1, "stop releases native ownership once");
    f = Fresh();
    var uncommittedId = Guid.NewGuid().ToString();
    var uncommittedInput = new ControlInput(0, true, true, uncommittedId, 1, f.State.P2!.CurrentPayable!.TargetId);
    while (f.P2._payState != PayState.Completed) f.Access.Apply(f.P2, uncommittedInput, f.Rewired);
    f.P2._payState = PayState.None;
    f.P2._floatingCurrency.Clear();
    f.Access.Apply(f.P2, uncommittedInput, f.Rewired);
    Check(f.Access.LastCompletedPayActionId != uncommittedId && f.Access.LastPayFailureActionId == uncommittedId,
        "cancelled transaction with lower wallet cannot impersonate native commit");
    f = Fresh();
    Check(f.Access.RequestLocalCoop() is not null && Managers.Inst.game.CoopPromptCalls == 0,
        "cannot re-open coop while P2 is joined");
    Managers.COOP_ENABLED = Managers.IsP2Playing = false;
    Check(f.Access.RequestLocalCoop() is null && Managers.Inst.game.CoopPromptCalls == 1,
        "coop request calls official game prompt only");
    Check(!Managers.COOP_ENABLED && !Managers.IsP2Playing, "opening prompt never directly enables coop");
    Check(f.Access.RequestLocalCoop() is not null && Managers.Inst.game.CoopPromptCalls == 1,
        "duplicate coop requests are rate limited");
    f = Fresh();
    Managers.COOP_ENABLED = Managers.IsP2Playing = false;
    NetworkBigBoss.IsOnline = true;
    Check(f.Access.RequestLocalCoop() is not null, "online mode cannot request local coop");
    NetworkBigBoss.IsOnline = false;
    Menu.Inst.CoopAllowed = false;
    Check(f.Access.RequestLocalCoop() is not null, "native coop availability is respected");
    Menu.Inst.CoopAllowed = true;
    Managers.Inst.game.blockStateProgression = true;
    Check(f.Access.RequestLocalCoop() is not null, "blocked game progression cannot request coop");
    Managers.Inst.game.blockStateProgression = false;
    var offThread = Task.Run(() => f.Access.RequestLocalCoop()).GetAwaiter().GetResult();
    Check(offThread is not null, "coop request cannot touch native game from HTTP worker thread");
    f = Fresh(30);
    f.P2.wallet.Coins = 100;
    var largeId = Guid.NewGuid().ToString();
    var largeInput = new ControlInput(0, true, true, largeId, 1, f.State.P2!.CurrentPayable!.TargetId);
    for (var frame = 0; frame < 400 && f.Access.LastCompletedPayActionId != largeId; frame++) f.Access.Apply(f.P2, largeInput, f.Rewired);
    Check(f.Access.LastCompletedPayActionId == largeId && f.P2.wallet.Coins == 70, "native target above 20 coins completes without a framework cap");
    f = Fresh(3);
    f.P2.selectedPayable!.Currency = CurrencyType.Gems;
    f.Access.Capture(true);
    var gemId = Guid.NewGuid().ToString();
    for (var frame = 0; frame < 60 && f.Access.LastCompletedPayActionId != gemId; frame++)
        f.Access.Apply(f.P2, new(0, true, true, gemId, 1, f.State.P2!.CurrentPayable!.TargetId, Currency: "gems"), f.Rewired);
    Check(f.Access.LastCompletedPayActionId == gemId && f.P2.wallet.Gems == 2 && f.P2.wallet.Coins == 10, "native gem payment leaves coins untouched");
    f = Fresh();
    var drop = new Command(Guid.NewGuid().ToString(), f.State.SessionId, 1, "drop", Currency: "coins", Amount: 1);
    Check(f.Access.ExecuteCommand(drop)?.Success == true && f.P2.wallet.Coins == 9, "ground drops use native player interaction");
    f.P2.currencyDroppingEnabled = false;
    Check(f.Access.ExecuteCommand(drop with { ActionId = Guid.NewGuid().ToString() })?.Success == false, "native drop rejection reports failure without claiming success");
    Check(Managers.Inst.kingdom.playerOne.wallet.Coins == 10, "expanded operations cannot spend P1 wallet");
    var skill = new SteedAbility(f.P2);
    UnityEngine.Resources.All[typeof(SteedAbility)] = [skill];
    observationTime += 2100;
    var skillState = f.Access.Capture(true);
    var ability = skillState.World!.Abilities!.OfType<Dictionary<string, object?>>().Single(v => v.GetValueOrDefault("kind")?.ToString() == "SteedAbility");
    var skillCommand = drop with { ActionId = Guid.NewGuid().ToString(), Operation = "ability", Ability = ability["ability_id"]!.ToString() };
    Check(f.Access.ExecuteCommand(skillCommand)?.Success == true && skill.Activations == 1, "only captured P2 skill can activate");
    skill.IsAbilityReady = false;
    Check(f.Access.ExecuteCommand(skillCommand with { ActionId = Guid.NewGuid().ToString() })?.Success == false && skill.Activations == 1, "native skill cooldown is respected");
    var mapOpen = drop with { ActionId = Guid.NewGuid().ToString(), Operation = "map", MapAction = "open" };
    Check(f.Access.ExecuteCommand(mapOpen)?.Success == true, "map opens through native menu event");
    var menuState = f.Access.Capture(false);
    Check(!menuState.Ready && menuState.UiReady, "AI map remains operable while world is paused");
    var mapSelect = mapOpen with { ActionId = Guid.NewGuid().ToString(), MapAction = "select", Land = 2 };
    Check(f.Access.ExecuteCommand(mapSelect)?.Success == true && Menu.Inst.ActiveMap.focusedLand == 2, "selection uses native island button callback");
    Menu.Inst.ActiveMap.lands[2]._button.interactable = false;
    Check(f.Access.ExecuteCommand(mapSelect)?.Success == false, "native island button state controls available selection");
    Menu.Inst.ActiveMap.confirmButton.gameObject.activeInHierarchy = false;
    Check(f.Access.ExecuteCommand(mapOpen with { ActionId = Guid.NewGuid().ToString(), MapAction = "confirm" })?.Success == false, "view-only map with hidden confirm button cannot initiate travel");
    Check(f.Access.ExecuteCommand(mapOpen with { ActionId = Guid.NewGuid().ToString(), MapAction = "close" })?.Success == true && !f.Access.Capture(false).UiReady, "native map close restores normal game input");
    f.P2.isOnBoat = true;
    Check(((Dictionary<string, object?>)f.Access.Capture(false).World!.Control!)["on_boat"] is true, "P2 boarding is observable before the AI chooses sail");
    Check(f.Access.ExecuteCommand(drop with { ActionId = Guid.NewGuid().ToString(), Operation = "sail" })?.Success == true
        && Managers.Inst.kingdom.boat.SailPlayer == f.P2, "sail routes the P2 native boat interaction");
    f = Fresh();
    for (var index = 0; index < 30; index++) Managers.Inst.enemies.AllEnemies.Add(new Enemy { transform = { position = { x = 1000 + index } } });
    Managers.Inst.payables.AllPayables = Enumerable.Range(0, 30).Select(index => new Payable { Pointer = 500 + index }).ToArray();
    observationTime += 1000;
    var fullWorld = f.Access.Capture(true).World!;
    Check(fullWorld.Enemies!.Length == 30 && fullWorld.Targets!.Length == 30, "full registry observations have no 60-unit or 16-object visibility cutoffs");
    f = Fresh();
    var controlledUnit = new IUnitControllable();
    f.P2._tunnel = true; f.P2._inputChannel = controlledUnit;
    Check(f.Access.Apply(f.P2, new(1, false, false), f.Rewired) && controlledUnit.Direction == 1, "P2 movement follows native special unit routing");
    Check(f.Access.ExecuteCommand(new(Guid.NewGuid().ToString(), f.State.SessionId, 1, "ability", Ability: "unit_action"))?.Success == true, "special unit interaction uses the native P2 channel");
    f.Access.Apply(f.P2, new(1, false, false), f.Rewired);
    Check(controlledUnit.PayHeld, "native unit held action persists while moving");
    f.Access.Release();
    Check(controlledUnit.Direction == 0 && !controlledUnit.PayHeld, "stop releases both special unit motion and held action");
    assertions += PerformanceTests.Run(fixtureRoot);
    assertions += HomeRangeTests.Run(fixtureRoot);
    Console.WriteLine($"PASS: {assertions} reflection, ownership, observation cadence and native transaction assertions (fakes; no game execution).");
}
finally
{
    File.Delete(Path.Combine(fixtureRoot, "BuildInfo.txt"));
    Directory.Delete(fixtureRoot);
}
