using System.Globalization;
using KingdomAI.Bridge;

internal static class DialogueTests
{
    internal static int Run(Observation fixture)
    {
        var checks = 0;
        void Check(bool result, string message) { checks++; if (!result) throw new Exception(message); }
        void Reject(Action action, int code)
        {
            try { action(); throw new Exception("Expected dialogue rejection"); }
            catch (CommandError error) { Check(error.Code == code, "Unexpected dialogue rejection status"); }
        }
        var state = fixture with { CapturedAt = DateTimeOffset.UtcNow.ToString("O") };
        var mailbox = new DialogueMailbox();
        mailbox.Observe(state, 1000);
        Check(mailbox.Submit(new("first", "你好，我在你旁边。", state.SessionId), 1000) == "accepted", "dialogue needs no command lease");
        Check(mailbox.Submit(new("first", "你好，我在你旁边。", state.SessionId), 1001) == "duplicate", "same message ID is not redisplayed");
        Reject(() => mailbox.Submit(new("first", "另一句话", state.SessionId), 1002), 409);
        Reject(() => mailbox.Submit(new("old", "旧存档", "old-session"), 1002), 409);
        Reject(() => mailbox.Submit(new("empty", "  ", state.SessionId), 1002), 400);
        Reject(() => mailbox.Submit(new(new string('a', 101), "过长标识", state.SessionId), 1002), 400);
        Reject(() => mailbox.Submit(new("long", new string('字', 601), state.SessionId), 1002), 400);
        Reject(() => mailbox.Submit(new("control", "禁止\u0001控制字符", state.SessionId), 1002), 400);
        var original = mailbox.Consume(1003);
        Check(original?.Text == "你好，我在你旁边。" && mailbox.Consume(1003) is null, "latest slot is consumed once");
        for (var i = 0; i < 3; i++) mailbox.Submit(new("next-" + i, "最新" + i, state.SessionId), 1010 + i);
        Check(mailbox.Consume(1013)?.MessageId == "next-2", "only latest pending text is retained");
        Reject(() => mailbox.Submit(new("limited", "过快", state.SessionId), 1014), 429);
        Check(mailbox.Submit(new("next-2", "最新2", state.SessionId), 1015) == "duplicate", "duplicate is harmless even when rate limited");
        mailbox.Observe(state, 3000);
        mailbox.Submit(new("after-window", "可以继续", state.SessionId), 3001);
        mailbox.Observe(state with { SessionId = "new" }, 3002);
        Check(mailbox.Consume(3003) is null, "scene/session change clears pending dialogue");
        Reject(() => mailbox.Submit(new("late", "旧回复", state.SessionId), 3003), 409);
        mailbox.Submit(new("new-id", "新存档", "new"), 3004);
        mailbox.Observe(state with { SessionId = "new", Ready = false }, 3005);
        Check(!mailbox.CanDisplay(3005) && mailbox.Consume(3005) is null, "unavailable P2 clears dialogue immediately");
        Reject(() => mailbox.Submit(new("paused", "不能显示", "new"), 3006), 409);
        mailbox.Observe(state, 5000);
        Reject(() => mailbox.Submit(new("stale", "过期快照", state.SessionId), 7501), 409);
        mailbox.Observe(state with { CapturedAt = DateTimeOffset.UtcNow.AddSeconds(-5).ToString("O") }, 8000);
        Reject(() => mailbox.Submit(new("stale-wall-clock", "过期游戏数据", state.SessionId), 8001), 409);
        mailbox.Observe(state, 9000);
        Parallel.For(0, 16, _ => mailbox.Submit(new("same-concurrent", "同一句", state.SessionId), 9001));
        Check(mailbox.Consume(9002)?.MessageId == "same-concurrent" && mailbox.Consume(9002) is null,
            "concurrent HTTP requests do not create an unbounded backlog");
        mailbox.Clear();
        Check(!mailbox.CanDisplay(9002) && mailbox.Consume(9002) is null, "disconnect clears mailbox");
        var emoji = string.Concat(Enumerable.Repeat("🌻", 190));
        var limited = BubbleText.Limit(emoji, 180);
        Check(StringInfo.ParseCombiningCharacters(limited).Length == 180 && limited.EndsWith('…')
            && !char.IsHighSurrogate(limited[^2]), "display limit preserves whole Unicode text elements");
        Check(BubbleText.Opacity(1000, 1000) == 1 && BubbleText.Opacity(1000, 8999) == 1
            && Math.Abs(BubbleText.Opacity(1000, 10000) - .5f) < .001
            && BubbleText.Opacity(1000, 11000) == 0, "bubble fades during final two of ten seconds");
        foreach (var screen in new[] { (1920f, 1080f), (800f, 600f), (320f, 240f) })
        foreach (var anchor in new[] { (15f, 25f), (screen.Item1 - 5, screen.Item2 - 20), (screen.Item1 / 2, screen.Item2 / 2) })
        {
            var placement = BubblePlacement.Place(screen.Item1, screen.Item2, anchor.Item1, anchor.Item2, 440, 150);
            Check(placement.X >= 12 && placement.Y >= 12 && placement.X + placement.Width <= screen.Item1 - 12
                && placement.Y + placement.Height <= screen.Item2 - 12, "bubble respects screen boundaries");
            Check(placement.Below ? placement.Y > anchor.Item2 + 20 : placement.Y + placement.Height < anchor.Item2 - 20,
                "bubble leaves room around the character");
        }
        return checks;
    }
}
