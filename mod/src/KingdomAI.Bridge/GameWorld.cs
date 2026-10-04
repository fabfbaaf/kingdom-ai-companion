using System.Reflection;

namespace KingdomAI.Bridge;

// Game-owned registries and scene objects are read only on Unity's main thread.
// Refresh the global model separately from the 50 ms player/input observation.
internal sealed partial class GameAccess
{
    private long _worldRefreshAt;
    private long _strategicRefreshAt;
    private WorldState? _worldCache;
    private bool _worldObservable;
    private string _observedSteed = "";
    private readonly Dictionary<string, (long RefreshAt, object[] Items)> _sceneObjects = new();
    private bool _uiOwned;
    private readonly Dictionary<string, object> _abilityObjects = new();

    private void ResetWorld()
    {
        _worldRefreshAt = _strategicRefreshAt = 0;
        _worldCache = null;
        _worldObservable = false;
        _observedSteed = "";
        _sceneObjects.Clear();
        _abilityObjects.Clear();
    }

    private static Dictionary<string, object?> Facts(params (string Key, object? Value)[] pairs)
        => pairs.ToDictionary(pair => pair.Key, pair => pair.Value);
    private string EntityId(object item) => _session + ":" + (Identity(item) is "" or "0"
        ? Call(item, "GetInstanceID")?.ToString() ?? System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(item).ToString()
        : Identity(item));
    private static string GameType(object item) => Read(Call(item, "GetIl2CppType"), "Name")?.ToString() ?? item.GetType().Name;
    private static object? Component(object? item, string name)
    {
        if (item is null || Find(name) is not { } type) return null;
        try { return ReflectionCache.Generic(item.GetType(), "GetComponent", type)?.Invoke(item, null); } catch { return null; }
    }
    private IEnumerable<object> SceneObjects(string name)
    {
        var now = _observationClock();
        if (_sceneObjects.TryGetValue(name, out var cached) && now < cached.RefreshAt)
            return cached.Items.Where(Active);
        var type = Find(name);
        var resources = Find("UnityEngine.Resources");
        if (type is null || resources is null) return Array.Empty<object>();
        try
        {
            var items = Items(ReflectionCache.Generic(resources, "FindObjectsOfTypeAll", type)?.Invoke(null, null)).ToArray();
            _sceneObjects[name] = (now + 2000, items);
            return items.Where(Active);
        }
        catch { return Array.Empty<object>(); }
    }
    private static object? As(object? item, string type) => item is null || Find(type) is not { } wanted ? null :
        wanted.IsInstanceOfType(item) ? item : NativeCast(item, wanted);
    private static object? SideValue(object? value, string side)
    {
        if (value is null) return null;
        var type = value.GetType();
        // The audited Sided<float> field proxy boxes the value twice in
        // PointerToValueGeneric(true, false). Its native indexer returns a boxed
        // value once. Prefer that getter; never accept the broken field fallback.
        if (type.IsGenericType && type.GetGenericTypeDefinition().FullName == "Il2Cpp.Sided`1")
        {
            try
            {
                var indexer = ReflectionCache.GetMember(type, "Item").Property;
                var parameter = indexer?.GetIndexParameters().SingleOrDefault()?.ParameterType;
                if (parameter?.IsEnum != true || parameter.FullName != "Il2Cpp.Side") return null;
                return indexer!.GetValue(value, new[] { Enum.Parse(parameter, side, ignoreCase: true) });
            }
            catch { return null; }
        }
        return Read(value, side);
    }
    private static object Bounds(object? value)
    {
        var left = Number(SideValue(value, "left")); var right = Number(SideValue(value, "right"));
        return left is not null && right is not null && left < right
            ? Facts(("left", left), ("right", right)) : Facts(("left", null), ("right", null));
    }
    private object? ControlledUnit(object? player)
    {
        if (Bool(Read(player, "_tunnel")) != true) return null;
        var channel = Read(player, "_inputChannel");
        if (Int(Read(channel, "PlayerId")) != 1) return null;
        foreach (var kind in new[] { "Knight", "Archer", "Berserker", "Worker", "Farmer", "WarriorPeasant" })
            if (As(channel, "Il2Cpp." + kind) is { } unit) return unit;
        return channel;
    }
    private Dictionary<string, object?> Entity(object item, string kind)
    {
        var health = Read(item, "damageable") ?? Read(item, "Damageable") ?? Read(item, "_damageable") ?? Component(item, "Il2Cpp.Damageable");
        return Facts(("entity_id", EntityId(item)), ("kind", kind), ("type", GameType(item)), ("name", Name(item)), ("x", X(item)),
            ("state", Read(item, "state")?.ToString() ?? Read(item, "CurrentMode")?.ToString()),
            ("level", Int(Read(item, "level"))), ("health", Int(Read(health, "hitPoints"))),
            ("health_ratio", Number(Read(health, "HealthPercentage"))), ("dead", Bool(Read(health, "isDead"))));
    }
    private Dictionary<string, object?> Target(object item, object? player)
    {
        var target = Entity(item, "payable");
        target["target_id"] = _session + ":" + Identity(item);
        target["x"] = Number(Call(item, "PlayerPayPoint")) ?? X(item);
        target["price"] = Int(Read(item, "Price")); target["currency"] = Currency(item);
        target["can_pay"] = Bool(Call(item, "CanPay", player));
        target["can_select"] = Bool(Call(item, "CanSelect", player));
        var blockers = Read(Read(_managers, "Inst"), "moveBlockers");
        if (X(player) is { } start && target["x"] is double end)
            target["path_blocked"] = Bool(Call(blockers, "IsPathBlocked", (float)start, (float)end));
        if (As(item, "Il2Cpp.PayableUpgrade") is { } upgrade)
        {
            target["kind"] = "upgrade";
            target["has_upgrade"] = Bool(Call(upgrade, "HasUpgrade"));
            target["next_building"] = Read(upgrade, "nextPrefab") is { } prefab ? Name(prefab) : null;
            try
            {
                var method = Unique(upgrade.GetType(), m => m.Name == "IsLocked" && m.GetParameters().Length == 2);
                object?[] args = { player, Activator.CreateInstance(method.GetParameters()[1].ParameterType.GetElementType()!) };
                target["locked"] = method.Invoke(upgrade, args);
                target["lock_reason"] = args[1]?.ToString();
            }
            catch { target["locked"] = null; target["lock_reason"] = null; }
            target["requires_passenger"] = Read(upgrade, "passengerUpgrades") is { } requirements ? Items(requirements).Any() : null;
        }
        foreach (var name in new[] { "PayableShop", "PayableTree", "PayableBoat", "PayableBombPurchase", "PayableTeleporter", "PayableHorn", "PayableShieldWallActivator" })
            if (As(item, "Il2Cpp." + name) is { } typed)
            {
                target["kind"] = name; target["state"] = Read(typed, "currentState")?.ToString() ?? Read(typed, "state")?.ToString();
                if (name == "PayableShop") target["stock"] = Read(typed, "_items") is { } stock ? Items(stock).Count() : null;
                break;
            }
        return target;
    }
    private static Dictionary<string, int?> ReadCurrencies(object? player)
    {
        var wallet = Read(player, "wallet");
        var result = new Dictionary<string, int?> { ["coins"] = Int(Read(wallet, "Coins")), ["gems"] = Int(Read(wallet, "Gems")) };
        if (Find("Il2Cpp.CurrencyType") is { IsEnum: true } currency)
            foreach (var value in Enum.GetValues(currency)) result[value.ToString()!.ToLowerInvariant()] =
                Int(Call(wallet, "GetCurrency", value)) ?? Int(Read(wallet, value.ToString()!));
        if (result.GetValueOrDefault("crown") is null) result["crown"] = Bool(Read(player, "hasCrown")) is { } crown ? crown ? 1 : 0 : null;
        return result;
    }
    private static double? PaymentTimeout(object? player, int? price)
    {
        var hold = Number(Read(player, "keyDownThreshold")); var between = Number(Read(player, "timeBetweenCoins"));
        var finish = Number(Read(player, "timeBeforeTransaction"));
        return hold is >= 0 && between is >= 0 && finish is >= 0 && price is >= 0 ? (hold + price * between + finish + 3) * 1000 : null;
    }
    private object[] CaptureAbilities(object? player)
    {
        _abilityObjects.Clear();
        var result = new List<object>();
        result.Add(Facts(("ability_id", "rear"), ("kind", "player"), ("name", "坐骑后仰/原生特殊动作")));
        if (Bool(Read(player, "_tunnel")) == true && Int(Read(Read(player, "_inputChannel"), "PlayerId")) == 1)
        {
            result.Add(Facts(("ability_id", "unit_action"), ("kind", "unit"), ("name", "当前特殊单位原生交互")));
            result.Add(Facts(("ability_id", "release_unit"), ("kind", "unit"), ("name", "退出特殊单位控制")));
        }
        if (Bool(Read(player, "IsFormationActive")) is { } formation)
            result.Add(Facts(("ability_id", "formation"), ("kind", "player"), ("name", "指挥阵型"), ("active", formation)));
        foreach (var type in new[] { "SteedAbility", "RulerAbility", "ItemOfPower" })
            foreach (var item in SceneObjects("Il2Cpp." + type))
            {
                if (!Same(Read(item, "_rider") ?? Read(item, "player") ?? Read(item, "_player") ?? Read(item, "controllerInput"), player)
                    && !Same(Read(item, "_steed"), Read(player, "steed"))) continue;
                var id = EntityId(item); _abilityObjects[id] = item;
                result.Add(Facts(("ability_id", id), ("kind", type), ("name", Name(item)),
                    ("ready", Bool(Read(item, "IsAbilityReady")) ?? Bool(Call(item, "CanActivate"))),
                    ("active", Bool(Read(item, "IsAbilityInProgress")) ?? Bool(Read(item, "_isAbilityActive"))),
                    ("cooldown_seconds", Number(Call(item, "CooldownRemaining"))),
                    ("channelled", Bool(Read(item, "IsChanneledAbility")))));
            }
        return result.ToArray();
    }
    private WorldState CaptureWorld(object? managers, object? game, object? director, object? player, bool paused)
    {
        var now = _observationClock();
        var phase = Int(Read(game, "state"));
        var observable = _scene != "loading" && phase == 2 && !paused
            && Bool(Read(game, "blockStateProgression")) == false && Active(player)
            && Bool(Read(_managers, "COOP_ENABLED")) == true && Bool(Read(_managers, "IsP2Playing")) == true
            && Bool(Read(_network, "IsOnline")) == false && Bool(Read(_network, "IsClientPresent")) == false;
        if (!observable)
        {
            // Pause/map retains same-scene facts for the map flow, with their
            // original observation time. Loading never leaks the previous island.
            _worldObservable = false;
            _worldRefreshAt = _strategicRefreshAt = 0;
            if (_scene == "loading" || phase is 0 or 1 or 32 || !Active(player)) ResetWorld();
            return (_worldCache ?? new WorldState(null, paused, null, Array.Empty<object>(), Array.Empty<object>())) with
            {
                IsNight = Bool(Read(director, "IsNight")), IsPaused = paused,
                Day = Number(Read(director, "TotalDaysInReign")), Ui = CaptureUi(), Control = CaptureControl(game)
            };
        }
        var steed = Identity(Read(player, "steed"));
        if (!_worldObservable || steed != _observedSteed)
        {
            _sceneObjects.Clear();
            _worldRefreshAt = _strategicRefreshAt = 0;
            _observedSteed = steed;
        }
        _worldObservable = true;
        if (_worldCache is null || now >= _strategicRefreshAt)
        {
            _worldCache = CaptureStrategicWorld(managers, game, director, player, paused);
            _strategicRefreshAt = now + 1000;
            _worldRefreshAt = 0;
        }
        if (now < _worldRefreshAt)
            return _worldCache with { IsNight = Bool(Read(director, "IsNight")), IsPaused = paused,
                Day = Number(Read(director, "TotalDaysInReign")), Ui = CaptureUi(), Control = CaptureControl(game) };
        _worldRefreshAt = now + 250;
        var enemies = Items(Read(Read(managers, "enemies"), "AllEnemies")).Select(item => {
            var entity = Entity(item, "enemy"); entity["threat"] = Bool(Read(item, "IsThreat"));
            entity["attacking"] = Bool(Read(item, "isAttacking")); entity["enemy_type"] = Read(item, "Type")?.ToString(); return (object)entity;
        }).ToArray();
        var drops = Items(Read(Read(managers, "dropManager"), "_droppedItemList")).Select(item => {
            var entity = Entity(item, "drop"); var money = As(item, "Il2Cpp.DroppableCurrency");
            entity["currency"] = money is null ? null : Currency(money) ?? Read(money, "CurrencyType")?.ToString()?.ToLowerInvariant();
            entity["picked_up"] = Bool(Read(item, "pickedUp")); entity["fake"] = Bool(Call(item, "IsFake"));
            return (object)entity;
        }).ToArray();
        _worldCache = _worldCache with
        {
            IsNight = Bool(Read(director, "IsNight")), IsPaused = paused, Day = Number(Read(director, "TotalDaysInReign")),
            NearbyEnemies = enemies, Enemies = enemies, DroppedItems = drops, Abilities = CaptureAbilities(player),
            Ui = CaptureUi(), Control = CaptureControl(game)
        };
        return _worldCache;
    }
    private WorldState CaptureStrategicWorld(object? managers, object? game, object? director, object? player, bool paused)
    {
        var kingdom = Read(managers, "kingdom"); var campaign = Read(Find("Il2Cpp.CampaignSaveData"), "current");
        var progress = Read(managers, "progressHelper"); var environment = Read(managers, "world");
        var targets = Items(Read(Read(managers, "payables"), "AllPayables")).Select(item => (object)Target(item, player)).ToArray();
        var units = new List<object>();
        foreach (var name in new[] { "Archers", "Knights", "Workers", "Farmers", "Beggars", "BeggarCamps", "Farmlands", "FleetBoats" })
            foreach (var unit in Items(Read(kingdom, name))) units.Add(Entity(unit, name.ToLowerInvariant()));
        foreach (var unit in Items(Call(kingdom, "GetFarmHouses"))) units.Add(Entity(unit, "farmhouse"));
        if (Read(kingdom, "banker") is { } banker)
        { var entity = Entity(banker, "banker"); entity["stored_coins"] = Int(Read(banker, "_stashedCoins")); units.Add(entity); }
        var structures = new List<object>();
        if (Read(kingdom, "castle") is { } castle) structures.Add(Entity(castle, "castle"));
        foreach (var wall in Items(Read(kingdom, "Walls")))
        { var entity = Entity(wall, "wall"); entity["fully_repaired"] = Bool(Read(wall, "fullyRepaired")); structures.Add(entity); }
        foreach (var type in new[] { "Tower", "ConstructionBuildingComponent" })
            foreach (var item in SceneObjects("Il2Cpp." + type))
            {
                var entity = Entity(item, type == "Tower" ? "tower" : "construction");
                entity["needs_work"] = Bool(Read(item, "NeedsMoreWork")); entity["build_points"] = Number(Read(item, "_currentBuildPoints"));
                entity["required_build_points"] = Int(Read(item, "_buildPoints")); structures.Add(entity);
            }
        foreach (var portal in Items(Read(kingdom, "AllPortals")))
        { var entity = Entity(portal, "portal"); entity["portal_type"] = Read(portal, "type")?.ToString(); structures.Add(entity); }
        if (Read(kingdom, "boat") is { } boat)
        {
            var entity = Entity(boat, "boat"); entity["passengers_summoned"] = Bool(Read(boat, "HasSummonedPassengers"));
            entity["player_position_x"] = Number(Read(Read(boat, "playerPosition"), "x"));
            entity["deck_left_x"] = Number(Read(Read(boat, "DeckLeft"), "x")); entity["deck_right_x"] = Number(Read(Read(boat, "DeckRight"), "x"));
            structures.Add(entity);
        }
        foreach (var item in SceneObjects("Il2Cpp.CliffPortalState"))
        { var entity = Entity(item, "cave"); entity["state"] = Read(item, "CurrentState")?.ToString(); entity["in_cave"] = Bool(Read(item, "InCave")); structures.Add(entity); }
        var questType = Find("Il2Cpp.QuestManager");
        var questEntries = Items(Read(questType, "QuestData")).Select(entry => (Key: Read(entry, "Key")?.ToString(), Item: Read(entry, "Value"))).ToArray();
        if (questEntries.Length == 0) questEntries = Items(Read(campaign, "QuestStates")).Select((item, index) => (Key: (string?)index.ToString(), Item: (object?)item)).ToArray();
        var quests = questEntries.Select(entry => (object)Facts(("type", entry.Key),
            ("step", Int(Read(entry.Item, "questState"))), ("keywords", Items(Read(entry.Item, "questKeywords")).Select(v => v.ToString()).ToArray()),
            ("completed", Bool(Read(entry.Item, "completed"))))).ToArray();
        return new(Bool(Read(director, "IsNight")), paused, Number(Read(director, "TotalDaysInReign")), Array.Empty<object>(), targets,
            Facts(("theme", Read(Read(Find("Il2Cpp.BiomeData"), "Current"), "blockName")?.ToString()),
                ("started_at", Int(Read(campaign, "realStartDateTime"))), ("reign", Int(Read(campaign, "reign"))),
                ("challenge_id", Int(Read(campaign, "challengeId"))),
                ("biome_index", Int(Read(campaign, "BiomeIndex"))), ("land", Int(Read(game, "currentLand"))),
                ("max_islands", Int(Read(campaign, "MaxIslands"))), ("furthest_land", Int(Read(campaign, "FurthestUnlockedLand"))),
                ("completed_islands", Int(Read(campaign, "NumIslandsCompleted"))), ("completed", Bool(Read(progress, "AreAllLandsComplete"))),
                ("secured_islands", Items(Read(campaign, "securedIslands")).Select(Bool).ToArray()),
                ("visited_islands", Items(Read(campaign, "visitedIslands")).Select(Int).ToArray()),
                ("technology", Read(campaign, "FurthestUnlockedTech")?.ToString()), ("lost", Bool(Read(game, "HasLost")))),
            Facts(("time", Number(Read(director, "currentTime"))), ("season", Read(director, "CurrentSeason")?.ToString()),
                ("island_days", Int(Read(director, "CurrentIslandDays"))), ("world_bounds", Bounds(Read(environment, "worldBounds"))),
                ("borders", Bounds(Read(kingdom, "border"))), ("intact_borders", Bounds(Read(kingdom, "borderIntact"))),
                ("observed_at", DateTimeOffset.UtcNow.ToString("O"))),
            targets, Array.Empty<object>(), Array.Empty<object>(), units.ToArray(), structures.ToArray(), Array.Empty<object>(), null, null, quests);
    }
    private object CaptureControl(object? game) => Facts(("manual_takeover", ManualTakeover), ("phase", Int(Read(game, "state"))),
        ("transition", _scene == "loading" || Int(Read(game, "state")) is 0 or 1 or 32),
        ("on_boat", Bool(Read(P2, "isOnBoat"))), ("teleporting", Bool(Read(P2, "isInteractingWithTeleporter"))),
        ("can_interact", Bool(Read(P2, "CanInteractWithWorld"))), ("currency_dropping_enabled", Bool(Read(P2, "currencyDroppingEnabled"))),
        ("in_cave", Bool(Read(Read(Read(_managers, "Inst"), "caveHelper"), "IsInCave"))),
        ("special_unit", Bool(Read(P2, "_tunnel"))), ("unit", ControlledUnit(P2) is { } unit ? Entity(unit, "controlled_unit") : null),
        ("menu_owned", _uiOwned));
}
