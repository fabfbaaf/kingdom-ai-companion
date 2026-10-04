namespace KingdomAI.Bridge;

// All command/lease transitions share one lock. HTTP never reads Unity objects.
public sealed class CommandEngine
{
    private readonly object _gate = new();
    private readonly Dictionary<string, (Command Command, Receipt Receipt)> _receipts = new();
    private readonly Queue<string> _order = new();
    private readonly HashSet<string> _seen = new();
    private readonly HashSet<string> _revokedControls = new();
    private string? _controlId;
    private long _controlEpoch;
    public long ControlEpoch { get { lock (_gate) return _controlEpoch; } }
    private string? _historySession;
    private Command? _active;
    private long _started;
    private double? _motionX;
    private long _motionProgress;
    private long _heartbeat = long.MinValue;
    private bool _payDownConsumed;
    private bool _lease;
    private bool _release;
    private bool _releasePending;
    private readonly List<string> _stopReceipts = new();
    private Observation? _state;
    public bool InputReleased { get { lock (_gate) return !_lease && !_releasePending; } }

    public void Observe(Observation state) { lock (_gate) SetState(state); }
    private void SetState(Observation state)
    {
        if (_historySession != state.SessionId)
        {
            StopLocked("游戏会话变化");
            _receipts.Clear(); _order.Clear(); _seen.Clear(); _revokedControls.Clear(); _historySession = state.SessionId;
        }
        _state = state;
    }
    public void Heartbeat(string controlId, string sessionId, long controlEpoch, long now)
    {
        lock (_gate)
        {
            if (!Guid.TryParse(controlId, out _)) throw new CommandError(400, "control_id 必须是 UUID");
            if (_state is null || sessionId != _state.SessionId || !(_state.Ready || _state.UiReady))
                throw new CommandError(409, "游戏会话已变化或不可操作");
            if (controlEpoch != _controlEpoch) throw new CommandError(409, "停止后控制代次已变化，旧心跳无法授权");
            if (_revokedControls.Contains(controlId)) throw new CommandError(409, "此控制编号已撤销，禁止恢复旧请求");
            if (_revokedControls.Count >= 65536) throw new CommandError(409, "控制历史已满，请结束当前游戏会话");
            if (_releasePending) throw new CommandError(409, "等待游戏主线程释放上一控制");
            if (_controlId is not null && (_controlId != controlId || !HeartbeatLive(now)))
            {
                if (!HeartbeatLive(now)) StopLocked("后台心跳超时");
                throw new CommandError(409, "上一控制仍有效或已过期，请先停止并创建新的控制编号");
            }
            _controlId = controlId;
            _heartbeat = now;
        }
    }
    private bool HeartbeatLive(long now) => _heartbeat != long.MinValue && now - _heartbeat < 2500;

    public Receipt Submit(Command command, long now)
    {
        lock (_gate)
        {
            if (!Guid.TryParse(command.ActionId, out _)) throw new CommandError(400, "action_id 必须是 UUID");
            if (_receipts.TryGetValue(command.ActionId, out var previous))
            {
                if (previous.Command != command) throw new CommandError(409, "action_id 已用于另一条指令");
                return previous.Receipt;
            }
            if (_seen.Contains(command.ActionId)) throw new CommandError(409, "动作历史已归档，禁止重放");
            if (_seen.Count >= 65536) throw new CommandError(409, "会话动作历史已满，请结束本次会话");
            if (command.PlayerId != 1) throw new CommandError(403, "仅可控制 P2");
            if (_state is null || command.SessionId != _state.SessionId) throw new CommandError(409, "场景会话已变化");
            if (command.Operation == "stop")
            {
                StopLocked("用户停止");
                if (_releasePending) _stopReceipts.Add(command.ActionId);
                return Remember(command, new(command.ActionId, command.SessionId, _releasePending ? "queued" : "completed",
                    _releasePending ? "停止请求已接受，等待游戏主线程释放" : "AI 输入未持有", _state.P2, _releasePending ? null : _state.P2));
            }
            if (!(_state.Ready || command.Operation == "map" && _state.UiReady) || !HeartbeatLive(now)) throw new CommandError(409, "游戏未就绪或后台心跳失联");
            if (command.ControlEpoch != _controlEpoch || command.ControlId is null || command.ControlId != _controlId || _revokedControls.Contains(command.ControlId))
                throw new CommandError(409, "控制编号已失效，迟到的旧指令不会执行");
            if (_active is not null) throw new CommandError(409, "上一动作尚未完成");
            if (_releasePending) throw new CommandError(409, "等待游戏主线程确认释放，暂不接收新动作");
            if (command.Operation is "move" or "move_to")
            {
                if (command.Operation == "move" && (command.Direction is not ("left" or "right") || command.DurationMs < 100))
                    throw new CommandError(400, "移动需 left/right 和正整数时长");
                if (command.Operation == "move_to" && (command.TargetX is null || !double.IsFinite(command.TargetX.Value) || _state.P2?.X is null))
                    throw new CommandError(400, "移动到位置需要有效目标坐标和 P2 位置");
                if (command.Sprint && !_state.Capabilities.Contains("sprint"))
                    throw new CommandError(409, "当前游戏没有提供疾跑能力");
                if (_state.P2?.TransactionPending != false)
                    throw new CommandError(409, "等待 P2 的原生交易结束，避免接管时取消付款");
            }
            else if (command.Operation is "pay_coin" or "pay")
            {
                if (command.Sprint) throw new CommandError(400, "支付不能包含疾跑");
                var target = _state.P2?.CurrentPayable;
                if (target is null) throw new CommandError(409, "P2 当前没有原生选中的支付对象");
                if (command.TargetId != target.TargetId) throw new CommandError(409, "当前支付对象已变化，请重新核对");
                if (target.Price is not > 0) throw new CommandError(409, "目标价格不可用");
                if (command.Operation == "pay_coin" && target.Price != 1)
                    throw new CommandError(409, "单枚付款仅用于价格恰好1枚的目标");
                var currency = command.Currency ?? target.Currency;
                if (currency != target.Currency) throw new CommandError(409, "支付币种与目标不匹配");
                var balance = currency == "coins" ? _state.P2?.Coins : _state.P2?.Currencies?.GetValueOrDefault(currency ?? "");
                if (balance is null || balance < target.Price) throw new CommandError(409, "P2 对应货币不足或不可读");
                if (_state.P2?.TransactionPending != false) throw new CommandError(409, "P2 上一原生交易尚未结算");
                if (target.Currency is null) throw new CommandError(409, "当前目标币种无法读取");
                if (target.CanPay != true) throw new CommandError(409, "当前目标 CanPay=false 或不可读，游戏尚未允许付款");
            }
            else if (command.Operation is "drop" or "ability" or "map" or "sail")
            {
                if (!_state.Capabilities.Contains(command.Operation)) throw new CommandError(409, "当前游戏没有提供此操作");
                if (command.Operation == "drop" && command.Amount < 1) throw new CommandError(400, "丢币数量必须为正整数");
                if (command.Operation == "ability" && string.IsNullOrWhiteSpace(command.Ability)) throw new CommandError(400, "缺少技能编号");
                if (command.Operation == "map" && command.MapAction is not ("open" or "close" or "left" or "right" or "select" or "confirm")) throw new CommandError(400, "未知地图操作");
            }
            else throw new CommandError(400, "不支持的操作");
            _active = command;
            _started = -1;
            _motionX = _state.P2?.X;
            _motionProgress = now;
            _payDownConsumed = false;
            _lease = true;
            return Remember(command, new(command.ActionId, command.SessionId, "queued", "等待游戏主线程", _state.P2, null));
        }
    }

    private Receipt Remember(Command command, Receipt receipt)
    {
        _receipts[command.ActionId] = (command, receipt);
        _seen.Add(command.ActionId);
        _order.Enqueue(command.ActionId);
        while (_order.Count > 256) _receipts.Remove(_order.Dequeue());
        return receipt;
    }

    public Receipt? GetReceipt(string id) { lock (_gate) return _receipts.TryGetValue(id, out var value) ? value.Receipt : null; }
    public void Stop(string reason) { lock (_gate) StopLocked(reason); }
    private void StopLocked(string reason)
    {
        _controlEpoch++;
        if (_controlId is not null) _revokedControls.Add(_controlId);
        _controlId = null;
        _heartbeat = long.MinValue;
        if (_active is { } command)
        {
            var item = _receipts[command.ActionId];
            _receipts[command.ActionId] = (command, item.Receipt with { Status = "cancelled", Message = reason, After = _state?.P2 });
        }
        _active = null;
        if (_lease) { _release = true; _releasePending = true; }
        _lease = false;
        _payDownConsumed = true;
    }
    public bool TakeReleaseRequest() { lock (_gate) { var value = _release; _release = false; return value; } }
    public void CompleteRelease(bool success)
    {
        lock (_gate)
        {
            if (!success) { _release = true; return; }
            _releasePending = false;
            foreach (var id in _stopReceipts)
                if (_receipts.TryGetValue(id, out var item))
                    _receipts[id] = (item.Command, item.Receipt with { Status = "completed", Message = "游戏主线程已释放 AI 输入", After = _state?.P2 });
            _stopReceipts.Clear();
        }
    }

    // Serialize the small main-thread native input call with HTTP stop. A stop
    // acknowledged after this lock returns cannot race with an old queued input.
    public bool? ExecuteInput(long now, Func<ControlInput, bool> apply)
    {
        lock (_gate)
        {
            var input = ReadInput(now);
            if (input is null) return null;
            var applied = apply(input);
            if (!applied) StopLocked("游戏输入绑定失效");
            return applied;
        }
    }

    public void ExecuteAuxiliary(long now, Func<Command, NativeResult?> execute)
    {
        lock (_gate)
        {
            if (_active is not { } command || command.Operation is "move" or "move_to" or "pay" or "pay_coin" || _started < 0) return;
            if (!HeartbeatLive(now)) { StopLocked("后台心跳超时"); return; }
            var result = execute(command);
            if (result is null) return;
            var item = _receipts[command.ActionId];
            _receipts[command.ActionId] = (command, item.Receipt with { Status = result.Success ? "completed" : "failed",
                Message = result.Message, After = _state?.P2, Effect = result.Effect });
            _active = null; _payDownConsumed = true;
        }
    }

    public void Tick(long now, Observation state, string? completedPayActionId = null, string? failedPayActionId = null, string? payFailureReason = null)
    {
        lock (_gate)
        {
            SetState(state);
            if (!(state.Ready || state.UiReady) || !HeartbeatLive(now) || (_active is { } a && a.SessionId != state.SessionId))
            {
                // Idle observations must not rotate the epoch: only a real
                // cancellation, stop request or session transition revokes it.
                if (_controlId is not null || _lease || _active is not null)
                    StopLocked(state.Ready ? "后台心跳超时" : state.Reason);
                return;
            }
            if (_active is not { } command) return;
            var item = _receipts[command.ActionId];
            if (_started < 0)
            {
                if (command.Operation is "pay_coin" or "pay" && state.P2?.CurrentPayable?.TargetId != command.TargetId)
                {
                    StopLocked("支付目标已变化");
                    return;
                }
                _started = now;
                _receipts[command.ActionId] = (command, item.Receipt with { Status = "running", Message = "游戏原生输入执行中", Before = state.P2 });
            }
            if (command.Operation is "move" or "move_to")
            {
                if (state.P2?.X is { } position && (_motionX is null || Math.Abs(position - _motionX.Value) >= 0.05))
                { _motionX = position; _motionProgress = now; }
                if (command.Operation == "move_to" && now - _motionProgress >= 5000)
                {
                    _receipts[command.ActionId] = (command, item.Receipt with { Status = "failed", Message = "目的地移动没有位置进展，请根据障碍重新选择路线", After = state.P2 });
                    _active = null;
                    return;
                }
            }
            if (command.Operation is "pay_coin" or "pay")
            {
                if (failedPayActionId == command.ActionId || (completedPayActionId != command.ActionId && now - _started >= (state.P2?.PaymentTimeoutMs ?? 10000)))
                {
                    var reason = payFailureReason ?? "支付未出现可核对变化；已取消，禁止自动重试";
                    StopLocked(reason);
                    _receipts[command.ActionId] = (command, item.Receipt with { Status = "failed", Message = reason, After = state.P2 });
                    return;
                }
                if (completedPayActionId != command.ActionId) return;
            }
            else if (command.Operation == "move_to")
            {
                if (state.P2?.X is not { } x || command.TargetX is not { } targetX || Math.Abs(x - targetX) > 0.5) return;
            }
            else if (command.Operation != "move" || now - _started < command.DurationMs) return;
            _receipts[command.ActionId] = (command, item.Receipt with { Status = "completed", Message = "输入结束，须核对实际状态", After = state.P2 });
            _active = null;
            // Retain idle lease until stop or heartbeat expiry; this sends neutral input,
            // so a held movement/pay never leaks into a later model decision.
            _payDownConsumed = true;
        }
    }

    public ControlInput? ReadInput(long now)
    {
        lock (_gate)
        {
            if (!_lease) return null;
            if (!HeartbeatLive(now) || !(_state?.Ready == true || _state?.UiReady == true))
            {
                StopLocked("输入租约过期或游戏不可操作");
                return null;
            }
            if (_active is not { } command || _started < 0) return new(0, false, false);
            if (!_state.Ready) return null;
            if (command.Operation == "move_to")
            {
                var distance = (command.TargetX ?? 0) - (_state.P2?.X ?? 0);
                return new(Math.Abs(distance) <= 0.5 ? 0 : Math.Sign(distance), false, false, Sprint: command.Sprint, Operation: "move_to");
            }
            if (command.Operation == "move")
            {
                var moving = now - _started < command.DurationMs;
                return new(moving ? (command.Direction == "left" ? -1 : 1) : 0, false, false,
                    Sprint: moving && command.Sprint);
            }
            if (command.Operation is not ("pay" or "pay_coin")) return new(0, false, false);
            var down = !_payDownConsumed;
            _payDownConsumed = true;
            return new(0, true, down, command.ActionId, command.MaxCoins, command.TargetId, Currency: command.Currency);
        }
    }
}
