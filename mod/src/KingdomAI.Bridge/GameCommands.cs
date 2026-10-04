namespace KingdomAI.Bridge;

internal sealed partial class GameAccess
{
    private string? _dropAction;
    private int _drops;
    private long _nextDrop;
    private bool _sailingRequested;

    private static object? InvokeNative(object obj, string name, params object?[] args)
    {
        var method = Unique(obj.GetType(), m => m.Name == name && m.GetParameters().Length == args.Length
            && m.GetParameters().Select((p, i) => args[i] is null ? !p.ParameterType.IsValueType : p.ParameterType.IsInstanceOfType(args[i])).All(v => v));
        return method.Invoke(obj, args);
    }
    private object? ActiveMap()
    {
        var map = Read(Read(_menu, "Inst"), "ActiveMap");
        var state = Int(Read(Read(_menu, "Inst"), "MapState"));
        return Active(map) && (state is 2 or 3 or 4 || _uiOwned && state == 0) ? map : SceneObjects("Il2Cpp.Map").FirstOrDefault(v => Bool(Read(v, "allowUIInput")) == true);
    }
    private bool UiBinding()
    {
        var managers = Read(_managers, "Inst");
        var kingdom = Read(managers, "kingdom");
        var p2 = Call(kingdom, "GetPlayer", 1);
        if (_sailingRequested && ActiveMap() is not null) _uiOwned = true;
        return _uiOwned && !ManualTakeover && BindingError.Length == 0
            && (ActiveMap() is not null || Int(Read(Read(managers, "game"), "state")) == 4)
            && Bool(Read(_network, "IsOnline")) == false && Bool(Read(_network, "IsClientPresent")) == false
            && Bool(Read(_managers, "IsP2Playing")) == true && Active(p2) && Same(p2, P2)
            && Bool(Read(p2, "hasLocalAuthority")) == true;
    }
    private object CaptureUi()
    {
        var menu = Read(_menu, "Inst"); var map = ActiveMap();
        return Facts(("map_open", map is not null), ("ai_owned", _uiOwned),
            ("menu_shown", Bool(Read(menu, "IsMenuShown"))), ("map_state", Read(menu, "MapState")?.ToString()),
            ("can_close", Bool(Read(Read(map, "currentOptions"), "userCanCloseMap"))),
            ("can_confirm", ButtonReady(Read(map, "confirmButton") ?? Read(map, "ActionButton"))),
            ("animating", Bool(Read(map, "isAnimating"))),
            ("focused_land", Int(Read(map, "focusedLand"))), ("selected_land", Int(Read(map, "landResult"))),
            ("lands", Items(Read(map, "lands")).Select(land => Facts(("land", Int(Read(land, "_landIndex"))),
                ("selectable", ButtonReady(Read(land, "_button"))))).ToArray()));
    }
    private static bool? ButtonReady(object? button) => button is null ? null :
        Active(button) && (Bool(Call(button, "IsInteractable")) ?? Bool(Read(button, "interactable"))) == true;

    // Called only on Unity's main thread, never from the HTTP server. Null means
    // a paced native operation is still running (e.g. dropping several coins).
    internal NativeResult? ExecuteCommand(Command command)
    {
        if (Environment.CurrentManagedThreadId != _mainThreadId) return new(false, "操作必须在游戏主线程执行");
        if (command.SessionId != _session || P2 is null) return new(false, "游戏会话或 P2 已变化");
        if (!(command.Operation == "map" && UiBinding()) && !Binding().Ready) return new(false, Binding().Reason);
        try
        {
            if (command.Operation == "drop")
            {
                if (_dropAction != command.ActionId) { _dropAction = command.ActionId; _drops = 0; _nextDrop = 0; }
                if (Environment.TickCount64 < _nextDrop) return null;
                var type = Find("Il2Cpp.CurrencyType")!;
                if (!Enum.TryParse(type, command.Currency ?? "coins", true, out var currency) || !Enum.IsDefined(type, currency!))
                    return new(false, "游戏不支持此货币");
                var wallet = Read(P2, "wallet"); var before = Int(Call(wallet, "GetCurrency", currency))
                    ?? ReadCurrencies(P2).GetValueOrDefault(currency!.ToString()!.ToLowerInvariant());
                if (before is not > 0) return new(false, "当前货币不足或不可读", Facts(("dropped", _drops)));
                InvokeNative(P2, "TryDropCurrency", currency);
                var after = Int(Call(wallet, "GetCurrency", currency))
                    ?? ReadCurrencies(P2).GetValueOrDefault(currency!.ToString()!.ToLowerInvariant());
                if (after is null || after >= before) return new(false, "原生游戏没有执行丢币", Facts(("dropped", _drops)));
                _drops += before.Value - after.Value; _nextDrop = Environment.TickCount64 + 150;
                _worldRefreshAt = 0;
                return _drops >= command.Amount ? new(true, "原生丢币完成", Facts(("currency", command.Currency ?? "coins"), ("dropped", _drops))) : null;
            }
            if (command.Operation == "ability")
            {
                if (command.Ability == "rear") InvokeNative(P2, "Rear", false);
                else if (command.Ability == "formation") InvokeNative(P2, "ActivateFormation");
                else if (command.Ability == "release_unit")
                {
                    var controller = Read(P2, "CurrentUnitController");
                    if (controller is null) return new(false, "P2 未控制特殊单位");
                    _unitActionHeld = false;
                    InvokeNative(controller, "ReleaseControl");
                }
                else if (command.Ability == "unit_action")
                {
                    var channel = Read(P2, "_inputChannel");
                    if (Int(Read(channel, "PlayerId")) != 1) return new(false, "P2 没有当前特殊单位");
                    var down = command.AbilityAction != "deactivate";
                    _unitActionHeld = down; _wasControlling = true;
                    InvokeNative(channel!, "UpdatePayState", down, down, false);
                }
                else
                {
                    if (command.Ability is null || !_abilityObjects.TryGetValue(command.Ability, out var ability) || !Active(ability))
                        return new(false, "当前 P2 没有该技能，请重新读取技能列表");
                    var kind = As(ability, "Il2Cpp.SteedAbility") is not null ? "steed" : As(ability, "Il2Cpp.RulerAbility") is not null ? "ruler" : "item";
                    var action = command.AbilityAction ?? "activate";
                    if (action is "activate" or "channel" or "attack" && (Bool(Read(ability, "IsAbilityReady")) ?? Bool(Call(ability, "CanActivate"))) != true)
                        return new(false, "游戏技能尚未就绪");
                    var method = (kind, action) switch
                    {
                        ("steed", "activate") => "Activate", ("steed", "deactivate") => "Deactivate",
                        ("steed", "attack") => "ManualAttack", ("steed", "release_attack") => "ManualAttackDone",
                        ("ruler", "activate") => "Activate", ("ruler", "deactivate") => "DeactivateRulerAbility", ("ruler", "cancel") => "CancelRulerAbility",
                        ("item", "activate") => "TriggerItemAbility", ("item", "channel") => "StartItemAbilityChanneling",
                        ("item", "deactivate") => "DeactivateAbility", ("item", "cancel") => "CancelAbility", _ => throw new InvalidOperationException("此技能不支持该操作")
                    };
                    InvokeNative(ability, method);
                }
                _worldRefreshAt = 0;
                return new(true, "已调用原生技能，效果以之后的游戏状态为准", Facts(("ability_id", command.Ability), ("action", command.AbilityAction ?? "activate")));
            }
            if (command.Operation == "map")
            {
                var menu = Read(_menu, "Inst")!; var map = ActiveMap();
                switch (command.MapAction)
                {
                    case "open":
                        if (map is not null) return new(false, "地图已经打开");
                        InvokeNative(menu, "OnButtonMap"); _uiOwned = true; break;
                    case "close":
                        if (map is null) return new(false, "地图尚未打开");
                        InvokeNative(map, Has(map.GetType(), "focusedLand") ? "OnButtonClose" : "OnButtonBack");
                        InvokeNative(menu, "Hide");
                        _sailingRequested = false; break;
                    case "left": case "right":
                        if (map is null) return new(false, "请先打开地图");
                        InvokeNative(map, Has(map.GetType(), "focusedLand") ? command.MapAction == "left" ? "OnButtonLeft" : "OnButtonRight"
                            : command.MapAction == "left" ? "OnButtonPreviousLand" : "OnButtonNextLand"); break;
                    case "select":
                        if (map is null || command.Land is null) return new(false, "需已打开的地图和实际岛屿编号");
                        var land = Items(Read(map, "lands")).FirstOrDefault(v => Int(Read(v, "_landIndex")) == command.Land);
                        if (land is not null)
                        {
                            if (ButtonReady(Read(land, "_button")) != true) return new(false, "游戏当前不允许选择此岛");
                            InvokeNative(land, "LandClickedHandler");
                        }
                        else
                        {
                            var select = Unique(map.GetType(), m => m.Name == "TrySelectLand" && m.GetParameters().Length == 2);
                            object?[] args = { command.Land.Value, 0 };
                            if (select.Invoke(map, args) is not true) return new(false, "游戏没有接受此岛屿选择");
                        }
                        break;
                    case "confirm":
                        if (map is null) return new(false, "地图尚未打开");
                        if (ButtonReady(Read(map, "confirmButton") ?? Read(map, "ActionButton")) != true) return new(false, "游戏当前尚不能确认此岛屿");
                        InvokeNative(map, Has(map.GetType(), "focusedLand") ? "OnButtonConfirm" : "OnButtonAction"); break;
                    default: return new(false, "未知地图操作");
                }
                return new(true, "已调用原生地图操作", CaptureUi());
            }
            if (command.Operation == "sail")
            {
                var boat = Read(Read(Read(_managers, "Inst"), "kingdom"), "boat");
                if (boat is null || Bool(Read(P2, "isOnBoat")) != true || Int(Read(boat, "state")) is not (1 or 2))
                    return new(false, "请先完成船只准备并让 P2 登船，游戏当前不能出航");
                InvokeNative(boat, "SailAway", P2); _sailingRequested = true;
                return new(true, "已请求原生出航流程，等待游戏地图或过场");
            }
            return new(false, "不支持的原生操作");
        }
        catch (Exception ex) { return new(false, "原生操作未完成: " + (ex.InnerException ?? ex).Message); }
    }
}
