using System.Net;
using System.Net.Sockets;
using System.Text.Json;
using KingdomAI.Bridge;

internal static class SnapshotTests
{
    private sealed class SerializationProbe
    {
        internal int Reads;
        public int Value => Interlocked.Increment(ref Reads);
    }
    internal static async Task<int> Run(Observation initial)
    {
        var checks = 0;
        void Check(bool value, string name)
        { if (!value) throw new Exception("FAIL: " + name); checks++; }
        var socket = new TcpListener(IPAddress.Loopback, 0);
        socket.Start(); var port = ((IPEndPoint)socket.LocalEndpoint).Port; socket.Stop();
        var engine = new CommandEngine(); engine.Observe(initial);
        using var server = new BridgeServer(new($"http://127.0.0.1:{port}", new string('b', 48)), engine, initial);
        using var client = new HttpClient { BaseAddress = new Uri($"http://127.0.0.1:{port}") };
        client.DefaultRequestHeaders.Authorization = new("Bearer", new string('b', 48));
        var probe = new SerializationProbe();
        var world = new WorldState(false, false, 1, [], [], Environment: probe);
        server.Publish(initial with { ObservationSeq = 100, World = world });
        Check(probe.Reads == 0, "main-thread Publish does not serialize a world snapshot");
        var replies = await Task.WhenAll(Enumerable.Range(0, 24).Select(_ => client.GetStringAsync("/state")));
        Check(probe.Reads == 1 && replies.Distinct().Count() == 1,
            "concurrent readers share one serialized snapshot without duplicate work");
        using (var json = JsonDocument.Parse(replies[0]))
            Check(json.RootElement.GetProperty("observation_seq").GetInt64() == 100,
                "HTTP observes the newly published detached facts");
        server.Publish(initial with { ObservationSeq = 101, World = world });
        Check(probe.Reads == 1, "a new Publish also leaves serialization to the HTTP thread");
        using (var json = JsonDocument.Parse(await client.GetStringAsync("/state")))
            Check(probe.Reads == 2 && json.RootElement.GetProperty("observation_seq").GetInt64() == 101,
                "serialized cache refreshes when a newer snapshot is requested");
        return checks;
    }
}
