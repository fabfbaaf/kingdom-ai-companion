using System.Reflection;
using System.Reflection.Emit;
using Il2Cpp;
using KingdomAI.Bridge;

internal static class PerformanceTests
{
    internal static int Run(string root)
    {
        var checks = 0;
        void Check(bool value, string name)
        { if (!value) throw new Exception("FAIL: " + name); checks++; }
        long now = 0;
        Managers.Inst = new(); Menu.Inst = new();
        Managers.COOP_ENABLED = Managers.IsP2Playing = true;
        NetworkBigBoss.IsOnline = NetworkBigBoss.IsClientPresent = false;
        UnityEngine.Time.timeScale = 1;
        UnityEngine.Resources.All.Clear(); UnityEngine.Resources.Reads.Clear();
        var p2 = Managers.Inst.kingdom.playerTwo;
        var registry = Managers.Inst.payables;
        var targets = Enumerable.Range(0, 200).Select(i => new Payable { Pointer = 500 + i }).ToArray();
        registry.AllPayables = targets;
        var tower = new Tower(); var skill = new SteedAbility(p2);
        UnityEngine.Resources.All[typeof(Tower)] = [tower];
        UnityEngine.Resources.All[typeof(SteedAbility)] = [skill];
        Managers.Inst.game.state = 0;
        var access = new GameAccess(root, new HarmonyLib.Harmony(), () => now);
        Observation state = access.Capture(true);
        for (var i = 0; i < 40; i++) { now += 50; state = access.Capture(true); }
        Check(registry.Reads == 0 && UnityEngine.Resources.Reads.Count == 0,
            "loading frames do not enumerate world registries or scene objects");
        Check(!state.Ready && state.World!.Targets is null && access.ObservationIntervalMs == 250,
            "loading publishes lightweight non-playable state at an idle cadence");
        foreach (var phase in new[] { 1, 32 })
        { Managers.Inst.game.state = phase; now += 500; access.Capture(true); }
        Check(registry.Reads == 0 && UnityEngine.Resources.Reads.Count == 0,
            "intro and transition never start full world scans");

        Managers.Inst.game.state = 2;
        var input = new Il2CppRewired.Player(1);
        access.NoteInput(p2, input);
        state = access.Capture(true);
        var initialId = state.World!.Targets!.OfType<Dictionary<string, object?>>().First()["target_id"];
        Check(state.Ready && access.ObservationIntervalMs == 50 && state.World.Targets!.Length == 200,
            "play resumes all objects and the fast P2 cadence without reducing visibility");
        Check(registry.Reads == 1 && UnityEngine.Resources.Reads[typeof(Tower)] == 1,
            "first playable frame observes the world immediately");
        var start = now;
        targets[^1].PayableNow = false;
        p2.wallet.Coins = 27;
        p2.selectedPayable = targets[0]; targets[0].Price = 4;
        Managers.Inst.enemies.AllEnemies.Add(new Enemy { transform = { position = { x = 18 } } });
        for (var i = 1; i <= 19; i++)
        {
            now = start + i * 50;
            access.NoteInput(p2, input);
            state = access.Capture(true);
            if (i == 1)
                Check(state.P2!.Coins == 27 && state.P2.CurrentPayable!.Price == 4,
                    "wallet and selected payment stay live between strategic refreshes");
            if (i == 5)
                Check(state.World!.Enemies!.Length == 1,
                    "enemies refresh at 250 ms while strategic facts remain cached");
        }
        Check(registry.Reads == 1 && targets[^1].CanPayCalls == 1,
            "19 player updates do not repeat the 200-object target scan");
        Check(UnityEngine.Resources.Reads[typeof(Tower)] == 1 && UnityEngine.Resources.Reads[typeof(SteedAbility)] == 1,
            "scene discovery is not repeated by fast player or enemy observations");
        now = start + 1000;
        tower.gameObject.activeInHierarchy = false;
        state = access.Capture(true);
        Check(registry.Reads == 2 && targets[^1].CanPayCalls == 2
            && state.World!.Targets!.OfType<Dictionary<string, object?>>().Last()["can_pay"] is false,
            "strategic native values refresh after one second rather than freezing in metadata caches");
        Check(!state.World!.Structures!.OfType<Dictionary<string, object?>>().Any(v => v["kind"]?.ToString() == "tower"),
            "inactive scene objects are filtered even when discovery is cached");
        Check(UnityEngine.Resources.Reads[typeof(Tower)] == 1,
            "one-second building updates reuse scene discovery");
        now = start + 2000;
        tower.gameObject.activeInHierarchy = true;
        state = access.Capture(true);
        Check(UnityEngine.Resources.Reads[typeof(Tower)] == 2 && UnityEngine.Resources.Reads[typeof(SteedAbility)] == 2,
            "scene discovery runs at two-second intervals");
        Check(state.World!.Structures!.OfType<Dictionary<string, object?>>().Any(v => v["kind"]?.ToString() == "tower"),
            "reactivated objects reappear in observations");

        var map = new Command(Guid.NewGuid().ToString(), state.SessionId, 1, "map", MapAction: "open");
        access.NoteInput(p2, input);
        Check(access.ExecuteCommand(map)?.Success == true, "native map can open after cached observations");
        var readsBeforePause = registry.Reads;
        var discoveryBeforePause = UnityEngine.Resources.Reads[typeof(Tower)];
        for (var i = 0; i < 30; i++) { now += 50; state = access.Capture(false); }
        Check(state.UiReady && !state.Ready && registry.Reads == readsBeforePause
            && UnityEngine.Resources.Reads[typeof(Tower)] == discoveryBeforePause,
            "owned map stays operable without any strategic or building scan while paused");
        Check(access.ExecuteCommand(map with { ActionId = Guid.NewGuid().ToString(), MapAction = "close" })?.Success == true,
            "native map closes while heavy observation is suspended");
        access.NoteInput(p2, input); state = access.Capture(true);
        Check(state.Ready && registry.Reads == readsBeforePause + 1,
            "unpause immediately refreshes strategic facts");

        access.ChangeScene("loading"); Managers.Inst.game.state = 0;
        var beforeLoad = registry.Reads;
        state = access.Capture(true);
        Check(state.World!.Targets is null && state.World.Campaign is null && registry.Reads == beforeLoad,
            "scene change clears old island data and does not scan the loading island");
        access.ChangeScene("main"); Managers.Inst.game.state = 2;
        registry.AllPayables = [new Payable { Pointer = 900 }];
        state = access.Capture(true); access.NoteInput(p2, input); state = access.Capture(true);
        Check(state.World!.Targets!.Length == 1 && !Equals(initialId,
            state.World.Targets.OfType<Dictionary<string, object?>>().First()["target_id"]),
            "new scene rebuilds object identities and discovers the new island immediately");
        Managers.COOP_ENABLED = Managers.IsP2Playing = false;
        var beforeSingle = registry.Reads;
        for (var i = 0; i < 20; i++) { now += 250; state = access.Capture(true); }
        Check(!state.Ready && registry.Reads == beforeSingle,
            "single player mode does not continually scan an AI P2 world");

        var absentName = "KingdomAI.LateBinding_" + Guid.NewGuid().ToString("N");
        Check(ReflectionCache.Find(absentName) is null, "missing type lookup is cacheable");
        var assembly = AssemblyBuilder.DefineDynamicAssembly(new AssemblyName("Late_" + Guid.NewGuid().ToString("N")), AssemblyBuilderAccess.Run);
        var lateType = assembly.DefineDynamicModule("main").DefineType(absentName).CreateType();
        Check(ReflectionCache.Find(absentName) == lateType,
            "a later assembly load invalidates negative type cache entries");
        Check(GameAccess.Read(p2.wallet, "Coins") is 27, "member cache reads the current native value");
        p2.wallet.Coins = 31;
        Check(GameAccess.Read(p2.wallet, "Coins") is 31, "member cache never caches currency values");
        return checks;
    }
}
