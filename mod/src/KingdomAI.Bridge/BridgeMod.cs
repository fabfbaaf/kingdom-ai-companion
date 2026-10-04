using System.Text.Json;
using MelonLoader;
using MelonLoader.Utils;

[assembly: MelonInfo(typeof(KingdomAI.Bridge.BridgeMod), "Kingdom AI Companion", "0.5.2", "桔梗")]
[assembly: MelonGame("noio", "KingdomTwoCrowns")]

namespace KingdomAI.Bridge;

public sealed class BridgeMod : MelonMod
{
    private static BridgeMod? _instance;
    private readonly CommandEngine _engine = new();
    private BridgeServer? _server;
    private GameAccess? _game;
    private long _nextObservation;
    private bool _enabled;
    private string? _coopRequestResult;
    private Observation? _lastState;
    private readonly BubbleRenderer _bubble = new();

    public override void OnLateInitializeMelon()
    {
        LoggerInstance.Msg("正在绑定本机游戏接口...");
        _instance = this;
        try
        {
            var root = MelonEnvironment.GameRootDirectory;
            var configPath = Path.Combine(root, "UserData", "KingdomAI", "bridge.local.json");
            var config = JsonSerializer.Deserialize<BridgeConfig>(File.ReadAllText(configPath), Wire.Json)
                ?? throw new InvalidOperationException("桥接配置为空");
            _game = new(root, HarmonyInstance);
            var initial = _game.Capture(true);
            _lastState = initial;
            _engine.Observe(initial);
            initial = initial with { ControlEpoch = _engine.ControlEpoch };
            _lastState = initial;
            _server = new(config, _engine, initial);
            _enabled = true;
            LoggerInstance.Msg("本机 P2 桥接就绪。后台启用后接管 P2；F8 可随时人工接管。默认不会自主行动。");
        }
        catch (Exception ex)
        {
            LoggerInstance.Error("桥接未启用：" + (ex.InnerException ?? ex).Message);
            _engine.Stop("初始化失败");
            HarmonyInstance.UnpatchSelf();
        }
    }
    public override void OnUpdate()
    {
        if (!_enabled || _game is null || _server is null) return;
        try
        {
            var now = Environment.TickCount64;
            if (_game.F8()) { _game.ToggleTakeover(); _engine.Stop("F8 人工接管"); _nextObservation = 0; }
            if (_engine.TakeReleaseRequest()) _engine.CompleteRelease(_game.Release());
            if (_server.ConsumeCoopRequest())
            {
                _game.Release();
                _coopRequestResult = _game.RequestLocalCoop() ?? "已请求游戏本地合作提示，请在游戏中确认和选择 P2 外观";
                _nextObservation = 0;
            }
            if (now < _nextObservation) return;
            var state = _game.Capture(_engine.InputReleased);
            _nextObservation = now + _game.ObservationIntervalMs;
            if (_bubble.Error.Length > 0)
                state = state with { Diagnostics = state.Diagnostics.Append(_bubble.Error).ToArray() };
            _lastState = state;
            _engine.Tick(now, state, _game.LastCompletedPayActionId, _game.LastPayFailureActionId, _game.LastPayFailureReason);
            _engine.ExecuteAuxiliary(now, _game.ExecuteCommand);
            if (_engine.TakeReleaseRequest()) _engine.CompleteRelease(_game.Release());
            _server.Publish(state with { InputReleased = _engine.InputReleased, CoopRequestResult = _coopRequestResult, ControlEpoch = _engine.ControlEpoch });
            _bubble.Update(_server.Dialogue, now);
        }
        catch (Exception ex)
        {
            _engine.Stop("状态读取异常");
            _game.Release();
            _enabled = false;
            _bubble.Clear();
            if (_lastState is { } last) _server.Publish(last with { Ready = false, Reason = "桥接状态读取失败，AI 已停用", InputReleased = _engine.InputReleased, Capabilities = Array.Empty<string>(), ControlEpoch = _engine.ControlEpoch });
            LoggerInstance.Error("已停止 AI：" + (ex.InnerException ?? ex).Message);
        }
    }
    public override void OnGUI()
    {
        if (_enabled && _game is not null) _bubble.Draw(_game.P2, Environment.TickCount64);
    }
    public static bool ReceivePrefix(object __instance, object __0)
    {
        var mod = _instance;
        if (mod?._enabled != true || mod._game is null || !mod._game.IsP2(__instance)) return true;
        mod._game.NoteInput(__instance, __0);
        var applied = mod._engine.ExecuteInput(Environment.TickCount64, input => mod._game.Apply(__instance, input, __0));
        if (applied is null)
        {
            if (mod._engine.TakeReleaseRequest()) mod._engine.CompleteRelease(mod._game.Release());
            return true;
        }
        if (applied == true) return false;
        mod._engine.Stop("特殊控制路由或接口调用失败");
        mod._engine.CompleteRelease(mod._game.Release());
        return true;
    }
    public override void OnSceneWasLoaded(int buildIndex, string sceneName)
    {
        _engine.Stop("场景切换"); _game?.Release(); _game?.ChangeScene(sceneName); _nextObservation = 0;
        _server?.Dialogue.Clear(); _bubble.Clear();
    }
    public override void OnSceneWasUnloaded(int buildIndex, string sceneName)
    {
        _engine.Stop("场景退出"); _game?.Release(); _game?.ChangeScene("loading"); _nextObservation = 0;
        _server?.Dialogue.Clear(); _bubble.Clear();
    }
    public override void OnDeinitializeMelon()
    {
        _enabled = false; _bubble.Clear(); _engine.Stop("游戏退出"); _game?.Release(); _server?.Dispose(); HarmonyInstance.UnpatchSelf(); _instance = null;
    }
}
