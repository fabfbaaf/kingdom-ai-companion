using Il2Cpp;
using KingdomAI.Bridge;

internal static class HomeRangeTests
{
    internal static int Run(string root)
    {
        int checks = 0; long now = 0;
        void Check(bool value, string name)
        { if (!value) throw new Exception("FAIL: " + name); checks++; }
        Managers.Inst = new(); Menu.Inst = new(); UnityEngine.Resources.All.Clear();
        Managers.COOP_ENABLED = Managers.IsP2Playing = true;
        NetworkBigBoss.IsOnline = NetworkBigBoss.IsClientPresent = false; UnityEngine.Time.timeScale = 1;
        Managers.Inst.world.worldBounds = new(-240f, 190f);
        Managers.Inst.kingdom.border = new(-5f, 25f);
        Managers.Inst.kingdom.borderIntact = new(0f, 20f);
        var access = new GameAccess(root, new HarmonyLib.Harmony(), () => now);
        var world = access.Capture(true).World!;
        var environment = (Dictionary<string, object?>)world.Environment!;
        Dictionary<string, object?> Range(string key) => (Dictionary<string, object?>)environment[key]!;
        Check(Range("world_bounds")["left"] is -240d && Range("world_bounds")["right"] is 190d,
            "island bounds use native side indexer rather than doubly boxed generic float fields");
        Check(Range("borders")["left"] is -5d && Range("borders")["right"] is 25d,
            "camp borders preserve actual left and right values");
        Check(Range("intact_borders")["left"] is 0d && Range("intact_borders")["right"] is 20d,
            "intact defense range is distinct from camp and island ranges");
        foreach (var range in new[] { new Sided<float>(30f, 10f), new Sided<float>(10f, 10f), new Sided<float>(float.NaN, 20f) })
        {
            Managers.Inst.kingdom.border = range; now += 1000;
            environment = (Dictionary<string, object?>)access.Capture(true).World!.Environment!;
            Check(Range("borders")["left"] is null && Range("borders")["right"] is null,
                "invalid, collapsed or unreadable bounds are unknown rather than fake home coordinates");
        }
        return checks;
    }
}
