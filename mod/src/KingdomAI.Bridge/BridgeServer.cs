using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace KingdomAI.Bridge;

internal sealed record BridgeConfig(string BaseUrl, string Token);
internal sealed record HeartbeatRequest(string ControlId, string SessionId, long ControlEpoch);

internal sealed class BridgeServer : IDisposable
{
    private readonly HttpListener _listener = new();
    private readonly CommandEngine _engine;
    private readonly byte[] _tokenHash;
    private readonly CancellationTokenSource _cancel = new();
    private volatile string _state;
    private int _coopRequested;
    internal DialogueMailbox Dialogue { get; } = new();
    internal BridgeServer(BridgeConfig config, CommandEngine engine, Observation initial)
    {
        if (!Uri.TryCreate(config.BaseUrl, UriKind.Absolute, out var uri)
            || uri.Scheme != "http" || uri.Host != "127.0.0.1" || uri.AbsolutePath != "/"
            || !string.IsNullOrEmpty(uri.Query) || !string.IsNullOrEmpty(uri.UserInfo)
            || config.Token.Length < 32) throw new InvalidOperationException("桥接配置必须使用 127.0.0.1 HTTP 地址与至少32字符 token");
        _tokenHash = SHA256.HashData(Encoding.UTF8.GetBytes("Bearer " + config.Token));
        _engine = engine;
        _state = Wire.Serialize(initial);
        Dialogue.Observe(initial, Environment.TickCount64);
        _listener.Prefixes.Add(config.BaseUrl.TrimEnd('/') + "/");
        _listener.Start();
        _ = Task.Run(Listen);
    }
    internal void Publish(Observation state)
    {
        Dialogue.Observe(state, Environment.TickCount64);
        _state = Wire.Serialize(state);
    }
    internal bool ConsumeCoopRequest() => Interlocked.Exchange(ref _coopRequested, 0) == 1;
    private async Task Listen()
    {
        while (!_cancel.IsCancellationRequested)
        {
            HttpListenerContext context;
            try { context = await _listener.GetContextAsync(); }
            catch (Exception) when (_cancel.IsCancellationRequested) { break; }
            _ = Handle(context);
        }
    }
    private async Task Handle(HttpListenerContext context)
    {
        try
        {
            var request = context.Request;
            // Browser origins are deliberately refused: the companion owns the token.
            if (request.Headers["Origin"] is not null || request.RemoteEndPoint?.Address is not { } address
                || !IPAddress.IsLoopback(address)
                || !CryptographicOperations.FixedTimeEquals(_tokenHash, SHA256.HashData(Encoding.UTF8.GetBytes(request.Headers["Authorization"] ?? ""))))
                throw new CommandError(401, "未授权");
            var path = request.Url?.AbsolutePath ?? "";
            if (request.HttpMethod == "GET" && path == "/state") { await Send(context, 200, _state); return; }
            if (request.HttpMethod == "GET" && path.StartsWith("/commands/", StringComparison.Ordinal))
            {
                var receipt = _engine.GetReceipt(path[10..]);
                if (receipt is null) throw new CommandError(404, "动作不存在，禁止重放未知结果");
                await Send(context, 200, Wire.Serialize(receipt)); return;
            }
            if (request.HttpMethod != "POST") throw new CommandError(404, "接口不存在");
            if (path == "/dialogue")
            {
                using var document = JsonDocument.Parse(await ReadBody(request));
                if (document.RootElement.ValueKind != JsonValueKind.Object)
                    throw new CommandError(400, "对话必须是 JSON 对象");
                var fields = new Dictionary<string, string>(StringComparer.Ordinal);
                foreach (var property in document.RootElement.EnumerateObject())
                {
                    if (property.Name is not ("message_id" or "text" or "session_id")
                        || property.Value.ValueKind != JsonValueKind.String
                        || !fields.TryAdd(property.Name, property.Value.GetString()!))
                        throw new CommandError(400, "对话只接受唯一的 message_id、text、session_id 纯文本字段");
                }
                if (fields.Count != 3) throw new CommandError(400, "对话字段不完整");
                var status = Dialogue.Submit(new(fields["message_id"], fields["text"], fields["session_id"]), Environment.TickCount64);
                await Send(context, 200, Wire.Serialize(new { status })); return;
            }
            if (path == "/heartbeat")
            {
                var heartbeat = JsonSerializer.Deserialize<HeartbeatRequest>(await ReadBody(request), Wire.Json)
                    ?? throw new CommandError(400, "空心跳");
                _engine.Heartbeat(heartbeat.ControlId, heartbeat.SessionId, heartbeat.ControlEpoch, Environment.TickCount64);
                await Send(context, 200, "{\"ok\":true}"); return;
            }
            if (path == "/stop")
            {
                _engine.Stop("后台停止");
                await Send(context, 200, Wire.Serialize(new { ok = true, queued = !_engine.InputReleased, input_released = _engine.InputReleased })); return;
            }
            if (path == "/coop/open")
            {
                _engine.Stop("打开游戏本地合作提示");
                Interlocked.Exchange(ref _coopRequested, 1);
                await Send(context, 202, "{\"queued\":true}"); return;
            }
            if (path != "/command") throw new CommandError(404, "接口不存在");
            var command = JsonSerializer.Deserialize<Command>(await ReadBody(request), Wire.Json) ?? throw new CommandError(400, "空指令");
            var result = _engine.Submit(command, Environment.TickCount64);
            await Send(context, 200, Wire.Serialize(result));
        }
        catch (CommandError ex) { await TryError(context, ex.Code, ex.Message); }
        catch (JsonException) { await TryError(context, 400, "JSON格式错误"); }
        catch (Exception) { await TryError(context, 500, "本机桥接错误"); }
        finally { context.Response.Close(); }
    }
    private async Task<byte[]> ReadBody(HttpListenerRequest request)
    {
        if (request.ContentLength64 > 8192 || request.ContentType?.StartsWith("application/json", StringComparison.OrdinalIgnoreCase) != true)
            throw new CommandError(400, "仅接受小于8KB的JSON");
        using var body = new MemoryStream();
        var buffer = new byte[1024];
        while (true)
        {
            var count = await request.InputStream.ReadAsync(buffer, _cancel.Token);
            if (count == 0) break;
            if (body.Length + count > 8192) throw new CommandError(413, "请求过大");
            body.Write(buffer, 0, count);
        }
        return body.ToArray();
    }
    private static Task TryError(HttpListenerContext context, int status, string message) => Send(context, status, Wire.Serialize(new { error = message }));
    private static async Task Send(HttpListenerContext context, int status, string value)
    {
        var data = Encoding.UTF8.GetBytes(value);
        context.Response.StatusCode = status;
        context.Response.ContentType = "application/json; charset=utf-8";
        context.Response.Headers["Cache-Control"] = "no-store";
        context.Response.ContentLength64 = data.Length;
        try { await context.Response.OutputStream.WriteAsync(data); } catch (Exception) { }
    }
    public void Dispose() { Dialogue.Clear(); _engine.Stop("游戏退出"); _cancel.Cancel(); _listener.Close(); _cancel.Dispose(); }
}
