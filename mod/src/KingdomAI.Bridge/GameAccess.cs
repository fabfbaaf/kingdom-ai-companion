using System.Collections;
using System.Reflection;
using HarmonyLib;

namespace KingdomAI.Bridge;

internal sealed partial class GameAccess
{
    private const BindingFlags Flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static | BindingFlags.FlattenHierarchy;
    private readonly Type _player;
    private readonly Type _managers;
    private readonly Type _network;
    private readonly Type _menu;
    private readonly Type _time;
    private readonly Type _input;
    private readonly Type _key;
    private readonly MethodInfo _action;
    private readonly MethodInfo _pay;
    private readonly MethodInfo _release;
    internal readonly MethodInfo Receive;
    internal object? P2 { get; private set; }
    internal long LastP2Input { get; private set; }
    internal bool ManualTakeover { get; private set; }
    internal string BindingError { get; private set; } = "";
    private string _session = Guid.NewGuid().ToString();
    private string _scene = "initializing";
    private string _p2Identity = "";
    private long _sequence;
    private bool _wasControlling;
    private bool _sprinting;
    private int _sprintDirection;
    private bool _unitActionHeld;
    private string _lastInputIdentity = "";
    private object? _lastRewired;
    private string _observedPayableIdentity = "";
    private string? _payActionId;
    private string _payTargetIdentity = "";
    private string? _payCurrency;
    private long _payStarted;
    private int _payPrice;
    private long _payTimeout;
    private bool _payReleased;
    private bool _payCommitted;
    private readonly int _mainThreadId = Environment.CurrentManagedThreadId;
    private long _lastCoopRequest;
    private static WeakReference<GameAccess>? _current;
    internal string? LastCompletedPayActionId { get; private set; }
    internal string? LastPayFailureActionId { get; private set; }
    internal string LastPayFailureReason { get; private set; } = "";
    private readonly string _version;

    internal GameAccess(string gameRoot, HarmonyLib.Harmony harmony)
    {
        var info = File.ReadAllText(Path.Combine(gameRoot, "BuildInfo.txt"));
        _version = info.Split('\n').FirstOrDefault(l => l.StartsWith("Version:"))?.Split(':', 2)[1].Trim() ?? "unknown";
        if (_version != "2.4.2" || !info.Contains("Hash: 116fe7e048"))
            throw new InvalidOperationException("此桥接尚仅核对过游戏 2.4.2 / 116fe7e048；拒绝猜测其它版本接口");
        _player = Find("Il2Cpp.Player") ?? throw new MissingMemberException("缺少 IL2CPP Player 代理");
        _managers = Find("Il2Cpp.Managers") ?? throw new MissingMemberException("缺少 Managers 代理");
        _network = Find("Il2Cpp.NetworkBigBoss") ?? throw new MissingMemberException("缺少 NetworkBigBoss 代理");
        _menu = Find("Il2Cpp.Menu") ?? throw new MissingMemberException("缺少 Menu 代理");
        _time = Find("UnityEngine.Time") ?? throw new MissingMemberException("缺少 Unity Time");
        _input = Find("UnityEngine.Input") ?? throw new MissingMemberException("缺少 Unity Input");
        _key = Find("UnityEngine.KeyCode") ?? throw new MissingMemberException("缺少 KeyCode");
        Receive = Unique(_player, m => m.Name.Contains("ReceiveInput") && m.ReturnType == typeof(void)
            && m.GetParameters() is [{ ParameterType: var p }] && p.FullName == "Il2CppRewired.Player");
        _action = Unique(_player, m => m.Name == "UpdateActionState" && m.GetParameters().Select(p => p.ParameterType)
            .SequenceEqual(new[] { typeof(int), typeof(bool), typeof(bool), typeof(bool), typeof(bool) }));
        if (!_action.GetParameters().Select(p => p.Name).SequenceEqual(new[] {
                "direction", "startSprint", "stopSprint", "sprintKeyPressed", "sprintKeyDoubleTap" }))
            throw new MissingMethodException("疾跑参数语义与核对版本不一致");
        _pay = Unique(_player, m => m.Name == "UpdatePayState" && m.GetParameters().Select(p => p.ParameterType)
            .SequenceEqual(new[] { typeof(bool), typeof(bool), typeof(bool) }));
        _release = Unique(_player, m => m.Name == "ReleaseInput" && m.GetParameters().Length == 0);
        foreach (var member in new[] { "playerId", "hasLocalAuthority", "_tunnel", "wallet", "selectedPayable", "_payState", "_floatingCurrency", "keyDownThreshold" })
            if (!Has(_player, member)) throw new MissingMemberException("Player." + member);
        var payable = Find("Il2Cpp.Payable") ?? throw new MissingMemberException("缺少 Payable 代理");
        var transactionComplete = Unique(payable, m => m.Name == "TransactionComplete" && m.ReturnType == typeof(void) && m.GetParameters().Length == 0);
        _current = new(this);
        harmony.Patch(transactionComplete, postfix: new HarmonyMethod(typeof(GameAccess).GetMethod(nameof(TransactionCompletedPostfix), Flags)!));
        harmony.Patch(Receive, prefix: new HarmonyMethod(typeof(BridgeMod).GetMethod(nameof(BridgeMod.ReceivePrefix))!));
        if (Find("Il2Cpp.Knight") is { } knight)
            foreach (var receiver in knight.GetMethods(Flags).Where(m => m.Name.Contains("ReceiveInput")
                && m.GetParameters() is [{ ParameterType: var p }] && p.FullName == "Il2CppRewired.Player"))
                harmony.Patch(receiver, prefix: new HarmonyMethod(typeof(BridgeMod).GetMethod(nameof(BridgeMod.ReceivePrefix))!));
    }

    private static void TransactionCompletedPostfix(object __instance)
    {
        if (_current?.TryGetTarget(out var access) == true && access._payActionId is not null
            && Identity(__instance) == access._payTargetIdentity)
            access._payCommitted = true;
    }

    private static Type? Find(string name)
    {
        foreach (var assemblyName in new[] { "Assembly-CSharp", "UnityEngine.CoreModule", "UnityEngine.InputLegacyModule", "Il2CppRewired_Core" })
        {
            try { Assembly.Load(assemblyName); } catch { }
        }
        return AppDomain.CurrentDomain.GetAssemblies().Select(a => a.GetType(name, false)).FirstOrDefault(t => t is not null);
    }
    private static MethodInfo Unique(Type type, Func<MethodInfo, bool> predicate)
    {
        var matches = type.GetMethods(Flags).Where(predicate).ToArray();
        if (matches.Length != 1) throw new MissingMethodException(type.FullName, "接口签名不唯一或不存在");
        return matches[0];
    }
    private static bool Has(Type type, string name) => type.GetProperty(name, Flags) is not null || type.GetField(name, Flags) is not null;
    internal static object? Read(object? obj, string name)
    {
        if (obj is null) return null;
        var type = obj as Type ?? obj.GetType();
        var instance = obj is Type ? null : obj;
        try { return type.GetProperty(name, Flags)?.GetValue(instance) ?? type.GetField(name, Flags)?.GetValue(instance); }
        catch { return null; }
    }
    private static object? Call(object? obj, string name, params object?[] args)
    {
        if (obj is null) return null;
        var type = obj as Type ?? obj.GetType();
        try
        {
            var method = type.GetMethods(Flags).SingleOrDefault(m => m.Name == name
                && m.GetParameters().Length == args.Length && m.GetParameters().Select((p, i) =>
                    args[i] is null ? !p.ParameterType.IsValueType : p.ParameterType.IsInstanceOfType(args[i])).All(match => match));
            return method?.Invoke(obj is Type ? null : obj, args);
        }
        catch { return null; }
    }
    private static bool? Bool(object? value) => value is bool b ? b : null;
    private static int? Int(object? value) { try { return value is null ? null : Convert.ToInt32(value); } catch { return null; } }
    private static double? Number(object? value)
    {
        try { var n = value is null ? double.NaN : Convert.ToDouble(value); return double.IsFinite(n) ? n : null; } catch { return null; }
    }
    private static string Identity(object? obj) => Read(obj, "Pointer")?.ToString() ?? "";
    private static bool Same(object? left, object? right)
    {
        var identity = Identity(left);
        return identity is not ("" or "0") && identity == Identity(right);
    }
    private static bool Active(object? obj) => Identity(obj) is not ("" or "0")
        && Bool(Read(Read(obj, "gameObject"), "activeInHierarchy")) == true;
    private static double? X(object? obj) => Number(Read(Read(Read(obj, "transform") ?? Call(obj, "GetTransform"), "position"), "x"));
    private static string Name(object? obj) => Read(obj, "name")?.ToString() ?? Read(Read(obj, "GetGO"), "name")?.ToString() ?? obj?.GetType().Name ?? "unknown";

    internal void ChangeScene(string scene)
    {
        _scene = scene;
        BindingError = "";
        _wasControlling = false;
        _sprinting = false;
        _unitActionHeld = false;
        _sailingRequested = false;
        _worldRefreshAt = 0;
        _uiOwned = false;
        _abilityObjects.Clear();
        _session = Guid.NewGuid().ToString();
        P2 = null;
        LastP2Input = 0;
        _p2Identity = "";
        _lastInputIdentity = "";
        _lastRewired = null;
        _observedPayableIdentity = "";
        ResetPayment();
    }
    internal void ToggleTakeover() => ManualTakeover = !ManualTakeover;
    internal bool F8() => Call(_input, "GetKeyDown", Enum.Parse(_key, "F8")) is true;
    internal bool Release()
    {
        if (P2 is not null && _wasControlling)
        {
            try {
                if (Read(P2, "_inputChannel") is { } channel && Int(Read(channel, "PlayerId")) == 1)
                {
                    InvokeNative(channel, "UpdateActionState", 0, false, true, false, false);
                    InvokeNative(channel, "UpdatePayState", false, false, false);
                }
                _release.Invoke(P2, null);
            }
            catch { BindingError = "原生释放输入失败，需人工接管"; return false; }
        }
        if (_payActionId is { } id && id != LastCompletedPayActionId && id != LastPayFailureActionId)
        {
            LastPayFailureActionId = id;
            LastPayFailureReason = "支付输入已中止";
        }
        _payActionId = null;
        _unitActionHeld = false;
        _wasControlling = false;
        _sprinting = false;
        _sprintDirection = 0;
        return true;
    }
    internal bool IsP2(object player) => Int(Read(player, "playerId") ?? Read(player, "PlayerId")) == 1;
    internal void NoteInput(object player, object? rewiredPlayer = null)
    {
        if (!IsP2(player) || Int(Read(rewiredPlayer, "id")) != 1) return;
        var p2 = Call(Read(Read(_managers, "Inst"), "kingdom"), "GetPlayer", 1);
        if (!Same(player, p2) && !Same(player, Read(p2, "_inputChannel"))) return;
        _lastInputIdentity = Identity(p2);
        _lastRewired = rewiredPlayer;
        LastP2Input = Environment.TickCount64;
    }
    // Called only from the game's own ReceiveInput on its main thread. Re-check
    // live ownership here: a 50 ms observation can straddle a pause or P2 exit.
    internal bool Apply(object player, ControlInput input, object? rewiredPlayer = null)
    {
        var binding = Binding(player, rewiredPlayer ?? _lastRewired);
        if (!binding.Ready)
            return false;
        try
        {
            _wasControlling = true;
            if (Bool(Read(P2, "_tunnel")) == true)
            {
                var channel = Read(P2, "_inputChannel");
                if (Int(Read(channel, "PlayerId")) != 1) return false;
                InvokeNative(channel!, "UpdateActionState", input.Direction, input.Sprint, !input.Sprint, input.Sprint, false);
                InvokeNative(channel!, "UpdatePayState", _unitActionHeld, false, false);
                return true;
            }
            var sprinting = input.Sprint && !input.Pay && input.Direction != 0;
            _action.Invoke(player, new object[] { Math.Clamp(input.Direction, -1, 1),
                sprinting && (!_sprinting || _sprintDirection != input.Direction), !sprinting, sprinting, false });
            _sprinting = sprinting;
            _sprintDirection = sprinting ? input.Direction : 0;
            if (!input.Pay && _payActionId is null)
            {
                _pay.Invoke(player, new object[] { false, false, false });
                return true;
            }
            var actionId = input.ActionId ?? _payActionId;
            if (string.IsNullOrEmpty(actionId)) return FailPayment(player, actionId, "支付缺少动作标识");
            if (actionId == LastPayFailureActionId || actionId == LastCompletedPayActionId)
            {
                _pay.Invoke(player, new object[] { false, false, false });
                return true;
            }
            var target = Read(player, "selectedPayable");
            var currency = _payActionId == actionId ? _payCurrency : Currency(target);
            var coins = ReadCurrencies(player).GetValueOrDefault(currency ?? "");
            var floating = Int(Read(Read(player, "_floatingCurrency"), "Count"));
            if (_payActionId != actionId)
            {
                if (_payActionId is not null) return FailPayment(player, actionId, "上一支付尚未结束");
                var threshold = Number(Read(player, "keyDownThreshold"));
                var price = Int(Read(target, "Price"));
                var between = Number(Read(player, "timeBetweenCoins"));
                var completion = Number(Read(player, "timeBeforeTransaction"));
                if (!input.Pay) return FailPayment(player, actionId, "支付按键请求已取消");
                if (Identity(target) is "" or "0") return FailPayment(player, actionId, "P2 当前没有原生选中的支付对象");
                if (Identity(target) != _observedPayableIdentity || input.TargetId != _session + ":" + Identity(target))
                    return FailPayment(player, actionId, "当前支付对象已变化，请重新核对目标");
                if (coins is null) return FailPayment(player, actionId, "P2 实时货币余额无法读取");
                if (price is not > 0) return FailPayment(player, actionId, "目标价格不可用");
                if (coins < price) return FailPayment(player, actionId, $"P2 货币不足: balance={coins}; price={price}");
                if (floating != 0 || Int(Read(player, "_payState")) != 0)
                    return FailPayment(player, actionId, $"P2 原生交易尚未空闲: payState={Read(player, "_payState")}; floating={floating?.ToString() ?? "unknown"}");
                if (currency is null || input.Currency is not null && input.Currency != currency) return FailPayment(player, actionId, "目标币种不可读或不匹配");
                if (Bool(Call(target, "CanPay", player)) != true)
                    return FailPayment(player, actionId, "当前目标 CanPay=false 或不可读，游戏尚未允许付款");
                if (threshold is not >= 0 || between is not >= 0 || completion is not >= 0)
                    return FailPayment(player, actionId, $"原生支付时间参数超出核对范围: threshold={threshold}; between={between}; completion={completion}");
                _payActionId = actionId;
                _payCurrency = currency;
                _payTargetIdentity = Identity(target);
                _payPrice = price.Value;
                _payStarted = Environment.TickCount64;
                _payTimeout = (long)Math.Min(long.MaxValue, (threshold.Value + price.Value * between.Value + completion.Value + 3) * 1000);
                _payReleased = false;
                _payCommitted = false;
                LastPayFailureReason = "";
                _pay.Invoke(player, new object[] { true, true, false });
            }
            else
            {
                if (coins is null || floating is null || (!_payReleased && (Identity(target) != _payTargetIdentity
                    || Currency(target) != _payCurrency)))
                    return FailPayment(player, actionId, "支付目标或实时钱包观测已变化");
                if (Environment.TickCount64 - _payStarted >= _payTimeout)
                    return FailPayment(player, actionId, "交易等待超时，已取消且未自动重试");
                if (_payReleased && Int(Read(player, "_payState")) is not (0 or 3))
                    return FailPayment(player, actionId, "游戏取消了目标交易，未报告支付成功");
                if (!_payReleased && !input.Pay)
                    return FailPayment(player, actionId, "交易未付满前已取消");
                // Do not call held input again after the game has collected the
                // full price. The original completed transaction owns its timer.
                if (!_payReleased && (Int(Read(player, "_payState")) == 3 || floating >= _payPrice))
                {
                    if (Identity(Read(player, "_completingPayable")) != _payTargetIdentity || Int(Read(player, "_payState")) != 3)
                        return FailPayment(player, actionId, "游戏未进入目标交易完成阶段");
                    _payReleased = true;
                }
                if (!_payReleased && coins < _payPrice - floating)
                    return FailPayment(player, actionId, "剩余货币不足以完成交易");
                _pay.Invoke(player, new object[] { !_payReleased, false, false });
            }
            coins = ReadCurrencies(player).GetValueOrDefault(_payCurrency ?? "");
            floating = Int(Read(Read(player, "_floatingCurrency"), "Count"));
            if (coins is null || floating is null) return FailPayment(player, actionId, "支付后钱包观测不可用");
            if (!_payReleased && (Int(Read(player, "_payState")) == 3 || floating >= _payPrice))
            {
                if (!Same(Read(player, "_completingPayable"), target) || Int(Read(player, "_payState")) != 3)
                    return FailPayment(player, actionId, "游戏未进入目标交易完成阶段");
                _payReleased = true;
                _pay.Invoke(player, new object[] { false, false, false });
                coins = ReadCurrencies(player).GetValueOrDefault(_payCurrency ?? "");
                floating = Int(Read(Read(player, "_floatingCurrency"), "Count"));
            }
            if (_payReleased && Int(Read(player, "_payState")) == 0 && floating == 0)
            {
                if (!_payCommitted)
                    return FailPayment(player, actionId, "未观察到目标 TransactionComplete");
                LastCompletedPayActionId = actionId;
                _payActionId = null;
            }
            return true;
        }
        catch (Exception ex)
        {
            BindingError = "原生输入调用失败: " + (ex.InnerException ?? ex).GetType().Name;
            Release();
            return false;
        }
    }

    private bool FailPayment(object player, string? actionId, string reason)
    {
        LastPayFailureActionId = actionId;
        LastPayFailureReason = reason;
        _payActionId = null;
        _payReleased = false;
        _payCommitted = false;
        _release.Invoke(player, null);
        _wasControlling = false;
        return true;
    }
    private void ResetPayment()
    {
        _payActionId = null;
        _payReleased = false;
        _payCommitted = false;
        LastCompletedPayActionId = null;
        LastPayFailureActionId = null;
        LastPayFailureReason = "";
    }
    private (bool Ready, bool Coop, bool Paused, string Reason) Binding(object? inputPlayer = null, object? rewiredPlayer = null)
    {
        var managers = Read(_managers, "Inst");
        var kingdom = Read(managers, "kingdom");
        var game = Read(managers, "game");
        var director = Read(managers, "director");
        var p1 = Call(kingdom, "GetPlayer", 0);
        var p2 = Call(kingdom, "GetPlayer", 1);
        var coop = Bool(Read(_managers, "COOP_ENABLED")) == true && Bool(Read(_managers, "IsP2Playing")) == true;
        var paused = Bool(Read(director, "IsTimePaused")) != false || Number(Read(_time, "timeScale")) is not > 0;
        var activePlayers = Items(Read(kingdom, "ActivePlayers")).Where(Active).Take(4).ToArray();
        var unique = activePlayers.Length == 2 && activePlayers.Count(p => Int(Read(p, "playerId")) == 1) == 1
            && activePlayers.Count(p => Int(Read(p, "playerId")) == 0) == 1
            && activePlayers.Any(p => Same(p, p2)) && activePlayers.Any(p => Same(p, p1)) && !Same(p1, p2);
        var fresh = LastP2Input > 0 && Environment.TickCount64 - LastP2Input is >= 0 and < 1500
            && _lastInputIdentity == Identity(p2);
        var reason = ManualTakeover ? "F8 人工接管；再按 F8 允许 AI"
            : Bool(Read(_network, "IsOnline")) != false || Bool(Read(_network, "IsClientPresent")) != false ? "只支持本机离线合作"
            : !coop || !unique || !Active(p1) || !Active(p2) ? "请进入存档并开启本地合作，使唯一 P2 加入"
            : Int(Read(game, "state")) != 2 || paused || Bool(Read(game, "blockStateProgression")) != false ? "游戏菜单、过场或暂停中"
            : Bool(Read(p2, "hasLocalAuthority")) != true ? "P2 不是本机控制角色"
            : Bool(Read(p2, "_tunnel")) != false && Int(Read(Read(p2, "_inputChannel"), "PlayerId")) != 1 ? "特殊单位控制身份不可读"
            : _payActionId is null && (Int(Read(p2, "_payState")) != 0 || Int(Read(Read(p2, "_floatingCurrency"), "Count")) != 0) ? "等待 P2 的原生交易结束后再接管"
            : !Same(Read(game, "_secondaryControllable"), p2) && !Same(Read(game, "_secondaryControllable"), Read(p2, "_inputChannel")) ? "游戏未向 P2 路由输入"
            : inputPlayer is not null && ((!Same(inputPlayer, p2) && !Same(inputPlayer, Read(p2, "_inputChannel"))) || Int(Read(rewiredPlayer, "id")) != 1) ? "P2 输入身份不匹配"
            : !fresh ? "等待真实 P2 输入循环"
            : BindingError;
        return (reason.Length == 0, coop, paused, reason.Length == 0 ? "P2 原生输入已绑定" : reason);
    }

    // Opens the game's official confirmation flow; never spawns a player or
    // flips the co-op flag directly. HTTP callers queue this on the main thread.
    internal string? RequestLocalCoop()
    {
        if (Environment.CurrentManagedThreadId != _mainThreadId) return "合作请求必须由游戏主线程执行";
        var game = Read(Read(_managers, "Inst"), "game");
        var menu = Read(_menu, "Inst");
        if (Bool(Read(_network, "IsOnline")) != false || Bool(Read(_network, "IsClientPresent")) != false)
            return "联网模式中不能打开本地合作请求";
        if (Bool(Read(_managers, "COOP_ENABLED")) != false || Bool(Read(_managers, "IsP2Playing")) != false)
            return "二号玩家已经加入或合作状态不可确认";
        if (Int(Read(game, "state")) is not (2 or 4) || Bool(Read(game, "blockStateProgression")) != false)
            return "请先进入存档的正常游玩或暂停菜单";
        if (Bool(Read(menu, "CoopAllowed")) != true) return "游戏当前不允许本地合作";
        if (_lastCoopRequest > 0 && Environment.TickCount64 - _lastCoopRequest < 2000)
            return "合作提示已经请求，请在游戏中完成确认";
        try
        {
            var trigger = Unique(game!.GetType(), m => m.Name == "TriggerCoopPrompt" && m.ReturnType == typeof(void)
                && m.GetParameters().Select(p => p.ParameterType).SequenceEqual(new[] { typeof(bool) }));
            Release();
            trigger.Invoke(game, new object[] { false });
            _lastCoopRequest = Environment.TickCount64;
            return null;
        }
        catch (Exception ex) { return "无法打开游戏合作提示: " + (ex.InnerException ?? ex).GetType().Name; }
    }

    private string[] Diagnostics(object? managers, object? game, object? p2) => new[]
    {
        "输入来源：P2 IControllable.ReceiveInput → 游戏原生 Action/Pay；F8 人工接管",
        "付款支持原生货币与完整价格，无固定单笔上限；确认原生 TransactionComplete 后回报",
        $"game.state={Read(game, "state") ?? "unknown"}; online={Read(_network, "IsOnline") ?? "unknown"}; clientPresent={Read(_network, "IsClientPresent") ?? "unknown"}; coopEnabled={Read(_managers, "COOP_ENABLED") ?? "unknown"}",
        $"activePlayersCount={Items(Read(Read(managers, "kingdom"), "ActivePlayers")).Take(4).Count()}; P2authority={Read(p2, "hasLocalAuthority") ?? "unknown"}; tunnel={Read(p2, "_tunnel") ?? "unknown"}; P2active={Active(p2)}",
        $"P2identity={Identity(p2)}; inputIdentity={_lastInputIdentity}; inputAgeMs={(LastP2Input == 0 ? "never" : (Environment.TickCount64 - LastP2Input).ToString())}; secondaryMatches={Same(Read(game, "_secondaryControllable"), p2)}; payState={Read(p2, "_payState") ?? "unknown"}",
        $"paymentTarget={Name(Read(p2, "selectedPayable"))}; price={Read(Read(p2, "selectedPayable"), "Price") ?? "unknown"}; currency={Currency(Read(p2, "selectedPayable")) ?? "unknown"}; canPay={Call(Read(p2, "selectedPayable"), "CanPay", p2) ?? "unknown"}; walletCoins={Read(Read(p2, "wallet"), "Coins") ?? "unknown"}; floatingCount={Read(Read(p2, "_floatingCurrency"), "Count") ?? "unknown"}; lastPayFailure={LastPayFailureReason}"
    };

    internal Observation Capture(bool released)
    {
        var managers = Read(_managers, "Inst");
        var kingdom = Read(managers, "kingdom");
        var director = Read(managers, "director");
        var game = Read(managers, "game");
        var p1 = Call(kingdom, "GetPlayer", 0);
        var p2 = Call(kingdom, "GetPlayer", 1);
        var identity = Identity(p2);
        if (identity != _p2Identity)
        {
            Release();
            _session = Guid.NewGuid().ToString();
            _worldRefreshAt = 0;
            _worldCache = null;
            _uiOwned = false;
            _abilityObjects.Clear();
            _p2Identity = identity;
            LastP2Input = 0;
            _lastInputIdentity = "";
            _lastRewired = null;
            ResetPayment();
        }
        P2 = p2;
        var players = new[] { p1, p2 }.Where(p => p is not null).Select(Player).ToArray();
        var binding = Binding();
        if (binding.Ready && ActiveMap() is null) { _uiOwned = false; _sailingRequested = false; }
        _observedPayableIdentity = Identity(Read(p2, "selectedPayable"));
        var world = CaptureWorld(managers, game, director, p2, binding.Paused);
        var uiReady = UiBinding();
        var capabilities = binding.Ready || uiReady ? new List<string> { "stop", "dialogue_bubble", "map", "extended_world" } : new();
        if (binding.Ready) capabilities.AddRange(new[] { "move", "move_long", "move_to", "sprint", "pay_coin", "pay", "pay_currency", "drop", "ability", "sail" });
        return new("0.5.0", _version, _session, ++_sequence, DateTimeOffset.UtcNow.ToString("O"), _scene,
            binding.Ready, binding.Reason, binding.Coop, 1, released, players, capabilities.ToArray(),
            Diagnostics(managers, game, p2), world, UiReady: uiReady);
    }
    private PlayerState Player(object? player)
    {
        var payable = Read(player, "selectedPayable");
        var target = payable is null || Identity(payable) is "" or "0" ? null : new PayableState(
            _session + ":" + Identity(payable), Name(payable), Number(Call(payable, "PlayerPayPoint")),
            Int(Read(payable, "Price")), Currency(payable), Bool(Call(payable, "CanPay", player)));
        return new(Int(Read(player, "playerId")) ?? -1, X(ControlledUnit(player)) ?? X(player), Int(Read(Read(player, "wallet"), "Coins")),
            Number(Read(Read(player, "steed"), "Stamina")), Bool(Read(player, "hasCrown")), target,
            Int(Read(player, "_payState")) is { } payState ? payState != 0 || Int(Read(Read(player, "_floatingCurrency"), "Count")) != 0 : null,
            CanSprint(player), ReadCurrencies(player), Read(Read(player, "steed"), "steedType")?.ToString(), PaymentTimeout(player, target?.Price));
    }
    private static bool? CanSprint(object? player)
    {
        var steed = Read(player, "steed");
        var tired = Bool(Read(steed, "IsTired"));
        var stamina = Number(Read(steed, "Stamina"));
        return tired is null || stamina is null ? null : !tired.Value && stamina.Value > 0;
    }
    private static string? Currency(object? payable) => Read(payable, "Currency")?.ToString()?.ToLowerInvariant();
    private static object? NativeCast(object? value, Type? target)
    {
        if (value is null || target is null) return null;
        try
        {
            var cast = value.GetType().GetMethods(Flags).SingleOrDefault(m => m.Name == "Cast"
                && m.IsGenericMethodDefinition && m.GetGenericArguments().Length == 1 && m.GetParameters().Length == 0);
            return cast?.MakeGenericMethod(target).Invoke(value, null);
        }
        catch { return null; }
    }
    private static IEnumerable<object> Items(object? collection)
    {
        if (collection is null) yield break;
        if (collection is IEnumerable enumerable)
        {
            foreach (var item in enumerable)
            {
                if (item is not null) yield return item;
            }
            yield break;
        }
        var count = Int(Read(collection, "Count") ?? Read(collection, "Length"));
        var indexer = collection.GetType().GetProperty("Item", Flags);
        if (count is >= 0 && indexer is not null)
        {
            for (var i = 0; i < count.Value; i++)
            {
                object? item;
                try { item = indexer.GetValue(collection, new object[] { i }); } catch { yield break; }
                if (item is not null) yield return item;
            }
            yield break;
        }
        // IL2CPP interface inheritance is not CLR interface inheritance. An
        // ICollection<T> proxy needs an explicit native IEnumerable<T> cast;
        // its typed IEnumerator<T> exposes Current, but MoveNext is on the
        // native non-generic IEnumerator. Keep Current typed as Player/Enemy.
        var source = collection;
        if (!source.GetType().GetMethods(Flags).Any(m => m.Name == "GetEnumerator" && m.GetParameters().Length == 0))
        {
            var arguments = source.GetType().GetGenericArguments();
            var definition = Find("Il2CppSystem.Collections.Generic.IEnumerable`1");
            if (arguments.Length != 1 || definition is null) yield break;
            source = NativeCast(source, definition.MakeGenericType(arguments))!;
            if (source is null) yield break;
        }
        var typed = Call(source, "GetEnumerator");
        if (typed is null) yield break;
        var cursor = typed.GetType().GetMethods(Flags).Any(m => m.Name == "MoveNext" && m.GetParameters().Length == 0)
            ? typed : NativeCast(typed, Find("Il2CppSystem.Collections.IEnumerator"));
        try
        {
            for (var i = 0; i < 1024 && Call(cursor, "MoveNext") is true; i++)
            {
                var item = Read(typed, "Current");
                if (item is not null) yield return item;
            }
        }
        finally
        {
            if (typed is IDisposable disposable) disposable.Dispose();
            else Call(NativeCast(typed, Find("Il2CppSystem.IDisposable")), "Dispose");
        }
    }
    private object[] Nearby(object? collection, double? position, bool payable)
    {
        if (position is null) return Array.Empty<object>();
        var result = new List<(double Distance, object Value)>();
        try
        {
            foreach (var item in Items(collection))
            {
                var x = payable ? Number(Call(item, "PlayerPayPoint")) : X(item);
                if (x is null) continue;
                if (payable) result.Add((Math.Abs(x.Value - position.Value), new {
                    target_id = _session + ":" + Identity(item), name = Name(item), x, price = Int(Read(item, "Price")),
                    currency = Currency(item), can_pay = Bool(Call(item, "CanPay", P2)) }));
                else result.Add((Math.Abs(x.Value - position.Value), new {
                    name = Name(item), x, threat = Bool(Read(item, "IsThreat")), attacking = Bool(Read(item, "isAttacking")) }));
            }
        }
        catch { }
        return result.OrderBy(r => r.Distance).Select(r => r.Value).ToArray();
    }
}
