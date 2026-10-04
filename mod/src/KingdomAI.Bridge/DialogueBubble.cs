using System.Globalization;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;

namespace KingdomAI.Bridge;

public sealed record DialogueRequest(string MessageId, string Text, string SessionId);
public sealed record DialogueMessage(string MessageId, string Text, string SessionId, long ReceivedAt);

// HTTP stores pure text only. Unity reads the single latest slot on its own main thread.
public sealed class DialogueMailbox
{
    private readonly object _gate = new();
    private readonly Dictionary<string, string> _seen = new(StringComparer.Ordinal);
    private readonly Queue<long> _recent = new();
    private DialogueMessage? _pending;
    private string _session = "";
    private string _scene = "";
    private bool _ready;
    private long _observedAt;
    private DateTimeOffset _capturedAt;
    private long _revision;
    public long Revision { get { lock (_gate) return _revision; } }

    public void Observe(Observation state, long now)
    {
        lock (_gate)
        {
            if (_session != state.SessionId || _scene != state.Scene)
            {
                _pending = null; _seen.Clear(); _recent.Clear(); _revision++;
            }
            var ready = state.Ready && state.P2 is not null;
            if (_ready && !ready) { _pending = null; _revision++; }
            _session = state.SessionId; _scene = state.Scene; _ready = ready; _observedAt = now;
            if (!DateTimeOffset.TryParse(state.CapturedAt, CultureInfo.InvariantCulture,
                    DateTimeStyles.RoundtripKind, out _capturedAt)) _ready = false;
        }
    }
    private bool Available(long now)
    {
        var age = (DateTimeOffset.UtcNow - _capturedAt).TotalSeconds;
        return _ready && now - _observedAt is >= 0 and <= 2500 && age is >= -2 and <= 2.5;
    }
    public bool CanDisplay(long now) { lock (_gate) return Available(now); }
    public string Submit(DialogueRequest request, long now)
    {
        var id = request.MessageId?.Trim();
        var session = request.SessionId?.Trim();
        var text = request.Text?.Trim();
        if (string.IsNullOrEmpty(id) || id.Length > 100 || id.Any(char.IsControl)
            || string.IsNullOrEmpty(session) || session.Length > 200 || session.Any(char.IsControl)
            || string.IsNullOrEmpty(text) || text.Length > 600
            || text.Any(c => char.IsControl(c) && c is not ('\n' or '\t')))
            throw new CommandError(400, "对话需 message_id 1–100字符、text 1–600字符及 session_id 1–200字符，不能含控制字符");
        var fingerprint = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(text)));
        lock (_gate)
        {
            if (session != _session) throw new CommandError(409, "对话所属游戏会话已变化");
            if (!Available(now)) throw new CommandError(409, "当前 P2 观测不可用，不能显示游戏气泡");
            if (_seen.TryGetValue(id, out var previous))
            {
                if (previous != fingerprint) throw new CommandError(409, "message_id 已用于另一条对话");
                return "duplicate";
            }
            while (_recent.Count > 0 && now - _recent.Peek() >= 2000) _recent.Dequeue();
            if (_recent.Count >= 4) throw new CommandError(429, "游戏气泡更新过快，请稍后发送最新一句");
            if (_seen.Count >= 2048) throw new CommandError(409, "本游戏会话对话历史已满");
            _seen.Add(id, fingerprint); _recent.Enqueue(now);
            _pending = new(id, BubbleText.Limit(text, 180), session, now);
            return "accepted";
        }
    }
    public DialogueMessage? Consume(long now)
    {
        lock (_gate)
        {
            var message = _pending; _pending = null;
            return Available(now) && message is not null && message.SessionId == _session
                && now - message.ReceivedAt is >= 0 and < 10000 ? message : null;
        }
    }
    public void Clear()
    {
        lock (_gate) { _pending = null; _ready = false; _revision++; }
    }
}

public static class BubbleText
{
    public static string Limit(string text, int maximum)
    {
        var starts = StringInfo.ParseCombiningCharacters(text);
        if (starts.Length <= maximum) return text;
        return maximum <= 1 ? "…" : text[..starts[maximum - 1]].TrimEnd() + "…";
    }
    public static float Opacity(long receivedAt, long now)
    {
        var age = now - receivedAt;
        return age < 0 || age >= 10000 ? 0 : age < 8000 ? 1 : (10000 - age) / 2000f;
    }
}

public sealed record BubblePlacement(float X, float Y, float Width, float Height, bool Below, float TailX)
{
    public static float ActorGap(float screenHeight) => Math.Clamp(screenHeight * .12f, 64, 120);
    public static BubblePlacement Place(float screenWidth, float screenHeight, float anchorX,
        float anchorY, float preferredWidth, float preferredHeight)
    {
        const float margin = 12;
        var width = Math.Clamp(preferredWidth, 1, Math.Max(1, screenWidth - margin * 2));
        var gap = ActorGap(screenHeight);
        var above = Math.Max(0, anchorY - gap - margin);
        var below = Math.Max(0, screenHeight - anchorY - gap - margin);
        var placeBelow = preferredHeight > above && below > above;
        var height = Math.Min(preferredHeight, Math.Max(1, placeBelow ? below : above));
        var x = Math.Clamp(anchorX - width / 2, margin, Math.Max(margin, screenWidth - margin - width));
        var y = placeBelow ? anchorY + gap : anchorY - gap - height;
        y = Math.Clamp(y, margin, Math.Max(margin, screenHeight - margin - height));
        return new(x, y, width, height, placeBelow, Math.Clamp(anchorX, x + 10, x + Math.Max(10, width - 10)));
    }
}

// Runtime reflection keeps game-generated Unity DLLs out of the build/package. The signatures
// are checked against the local 6000.0.66 interop metadata. Box/Label never capture game input.
internal sealed class BubbleRenderer
{
    private const BindingFlags Flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static;
    private DialogueMessage? _message;
    private long _revision = -1;
    private Type? _gui, _screen, _camera, _event, _rect, _color, _content;
    private object? _labelStyle, _boxStyle, _font;
    internal string Error { get; private set; } = "";

    internal void Update(DialogueMailbox mailbox, long now)
    {
        if (_revision != mailbox.Revision || !mailbox.CanDisplay(now)) _message = null;
        _revision = mailbox.Revision;
        if (mailbox.Consume(now) is { } incoming) _message = incoming;
        if (_message is { } current && BubbleText.Opacity(current.ReceivedAt, now) == 0) _message = null;
    }
    internal void Clear() => _message = null;
    private static Type? Find(string name)
    {
        foreach (var module in new[] { "UnityEngine.CoreModule", "UnityEngine.IMGUIModule", "UnityEngine.TextRenderingModule" })
            try { Assembly.Load(module); } catch { }
        return AppDomain.CurrentDomain.GetAssemblies().Select(a => a.GetType(name, false)).FirstOrDefault(t => t is not null);
    }
    private static object? Read(object? value, string name)
    {
        if (value is null) return null;
        var type = value as Type ?? value.GetType();
        return type.GetProperty(name, Flags)?.GetValue(value is Type ? null : value)
            ?? type.GetField(name, Flags)?.GetValue(value is Type ? null : value);
    }
    private static void Write(object value, string name, object data) => value.GetType().GetProperty(name, Flags)!.SetValue(value, data);
    private static object? Call(object value, string name, params object[] args)
    {
        var type = value as Type ?? value.GetType();
        var method = type.GetMethods(Flags).Single(m => m.Name == name && m.GetParameters().Length == args.Length
            && m.GetParameters().Select((p, i) => p.ParameterType.IsInstanceOfType(args[i])).All(match => match));
        return method.Invoke(value is Type ? null : value, args);
    }
    private bool Prepare(int fontSize)
    {
        _gui ??= Find("UnityEngine.GUI"); _screen ??= Find("UnityEngine.Screen");
        _camera ??= Find("UnityEngine.Camera"); _event ??= Find("UnityEngine.Event");
        _rect ??= Find("UnityEngine.Rect"); _color ??= Find("UnityEngine.Color");
        _content ??= Find("UnityEngine.GUIContent");
        if (_gui is null || _screen is null || _camera is null || _event is null
            || _rect is null || _color is null || _content is null) return false;
        if (_labelStyle is null)
        {
            var style = Find("UnityEngine.GUIStyle"); var skin = Read(_gui, "skin");
            if (style is null || skin is null) return false;
            _labelStyle = Activator.CreateInstance(style, Read(skin, "label")!);
            _boxStyle = Activator.CreateInstance(style, Read(skin, "box")!);
            if (_labelStyle is null || _boxStyle is null) return false;
            Write(_labelStyle, "richText", false); Write(_boxStyle, "richText", false);
            Write(_labelStyle, "wordWrap", true);
            Write(_labelStyle, "alignment", Enum.Parse(Find("UnityEngine.TextAnchor")!, "MiddleCenter"));
            Write(_labelStyle, "padding", Activator.CreateInstance(Find("UnityEngine.RectOffset")!, 14, 14, 10, 10)!);
            Write(Read(_labelStyle, "normal")!, "textColor", Activator.CreateInstance(_color, .95f, .97f, .91f, 1f)!);
            try
            {
                if (Find("UnityEngine.Font") is { } fontType)
                {
                    _font = Call(fontType, "CreateDynamicFontFromOSFont", "Microsoft YaHei", fontSize);
                    if (_font is not null) Write(_labelStyle, "font", _font);
                }
            }
            catch { /* Retain the game's GUI font if the OS font cannot be created. */ }
        }
        Write(_labelStyle, "fontSize", fontSize);
        return true;
    }
    private object? Project(object position)
    {
        var cameras = new List<object>();
        if (Read(_camera, "main") is { } main) cameras.Add(main);
        if (Read(_camera, "allCameras") is { } all)
        {
            var length = Convert.ToInt32(Read(all, "Length") ?? 0);
            var item = all.GetType().GetProperty("Item", Flags);
            if (item is not null)
                for (var i = 0; i < Math.Min(length, 8); i++)
                    if (item.GetValue(all, new object[] { i }) is { } camera) cameras.Add(camera);
        }
        foreach (var camera in cameras)
        {
            var point = Call(camera, "WorldToScreenPoint", position);
            if (point is null || Convert.ToSingle(Read(point, "z")) <= 0) continue;
            var viewport = Read(camera, "pixelRect");
            var x = Convert.ToSingle(Read(point, "x")); var y = Convert.ToSingle(Read(point, "y"));
            if (viewport is null || (x >= Convert.ToSingle(Read(viewport, "x"))
                && x <= Convert.ToSingle(Read(viewport, "x")) + Convert.ToSingle(Read(viewport, "width"))
                && y >= Convert.ToSingle(Read(viewport, "y"))
                && y <= Convert.ToSingle(Read(viewport, "y")) + Convert.ToSingle(Read(viewport, "height")))) return point;
        }
        return null;
    }
    internal void Draw(object? player, long now)
    {
        if (_message is not { } message || player is null) return;
        object? oldColor = null;
        try
        {
            if (!Prepare(18)) { Error = "游戏气泡绘制不可用: Unity GUI接口未就绪"; return; }
            if (Read(Read(_event, "current"), "type")?.ToString() != "Repaint") return;
            var screenWidth = Convert.ToSingle(Read(_screen, "width"));
            var screenHeight = Convert.ToSingle(Read(_screen, "height"));
            if (screenWidth < 160 || screenHeight < 160) return;
            var position = Read(Read(player, "transform"), "position");
            if (position is null || Project(position) is not { } projected) return;
            var fontSize = (int)Math.Clamp(screenHeight / 42, 14, 22);
            Write(_labelStyle!, "fontSize", fontSize);
            var width = Math.Min(440, screenWidth - 24);
            var anchorX = Convert.ToSingle(Read(projected, "x"));
            var anchorY = screenHeight - Convert.ToSingle(Read(projected, "y"));
            var gap = BubblePlacement.ActorGap(screenHeight);
            var space = Math.Max(anchorY - gap - 12, screenHeight - anchorY - gap - 12);
            var maximumHeight = Math.Min(300, Math.Max(1, space));
            var text = message.Text;
            var content = Activator.CreateInstance(_content!, text)!;
            var height = Convert.ToSingle(Call(_labelStyle!, "CalcHeight", content, width));
            while (height > maximumHeight && StringInfo.ParseCombiningCharacters(text).Length > 2)
            {
                text = BubbleText.Limit(text, StringInfo.ParseCombiningCharacters(text).Length - 1);
                content = Activator.CreateInstance(_content!, text)!;
                height = Convert.ToSingle(Call(_labelStyle!, "CalcHeight", content, width));
            }
            if (height > maximumHeight) return;
            var placement = BubblePlacement.Place(screenWidth, screenHeight, anchorX, anchorY, width, height);
            var rect = Activator.CreateInstance(_rect!, placement.X, placement.Y, placement.Width, placement.Height)!;
            var alpha = BubbleText.Opacity(message.ReceivedAt, now);
            oldColor = Read(_gui, "color");
            _gui!.GetProperty("color", Flags)!.SetValue(null, Activator.CreateInstance(_color!, .25f, .32f, .27f, alpha));
            Call(_gui, "Box", rect, "", _boxStyle!);
            var tail = Activator.CreateInstance(_rect!, placement.TailX - 5,
                placement.Below ? placement.Y - 6 : placement.Y + placement.Height - 2, 10f, 8f)!;
            Call(_gui, "Box", tail, "", _boxStyle!);
            _gui.GetProperty("color", Flags)!.SetValue(null, Activator.CreateInstance(_color!, 1f, 1f, 1f, alpha));
            Call(_gui, "Label", rect, text, _labelStyle!);
            Error = "";
        }
        catch (Exception ex) { Error = "游戏气泡绘制不可用: " + (ex.InnerException ?? ex).GetType().Name; }
        finally
        {
            if (oldColor is not null)
                try { _gui!.GetProperty("color", Flags)!.SetValue(null, oldColor); } catch { }
        }
    }
}
