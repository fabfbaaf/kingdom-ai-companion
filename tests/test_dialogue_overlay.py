"""Offline display forwarding tests: no real bridge, game, sound or microphone."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from companion.bridge import BridgeClient, BridgeError
from companion.contracts import GameState
from companion.dialogue_overlay import MAX_SEEN, DialogueOverlay, _bubble_text


def game_state(**changes):
    value = {"bridge_version": "0.2.1", "game_version": "fixture", "session_id": "fixture-session",
             "observation_seq": 1, "control_epoch": 0, "captured_at": datetime.now(UTC),
             "scene": "fixture-island", "ready": True, "coop": True, "controlled_player_id": 1,
             "input_released": True, "players": [{"player_id": 0}, {"player_id": 1}],
             "capabilities": ["dialogue_bubble"], "diagnostics": []}
    value.update(changes)
    return GameState.model_validate(value)


class FakeBridge:
    def __init__(self):
        self.cached = game_state()
        self.current = self.cached
        self.state_calls = 0
        self.dialogues = []
        self.state_entered = asyncio.Event()
        self.send_entered = asyncio.Event()
        self.state_gate = None
        self.send_gate = None
        self.send_error = False
        self.result = {"status": "accepted"}
        self.cancelled = False

    async def state(self):
        self.state_calls += 1
        self.state_entered.set()
        if self.state_gate is not None:
            await self.state_gate.wait()
        return self.current

    async def dialogue(self, **body):
        self.dialogues.append(body)
        self.send_entered.set()
        try:
            if self.send_gate is not None:
                await self.send_gate.wait()
            if self.send_error:
                raise BridgeError("fixture private path/token must not escape")
            return self.result
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def command(self, *_args, **_kwargs):
        raise AssertionError("display must not issue game commands")

    async def heartbeat(self, *_args, **_kwargs):
        raise AssertionError("display must not claim a control lease")

    async def stop(self):
        raise AssertionError("display must not stop the game controller")


def reply(message_id=1, content="我会陪你一起走。", **fields):
    return {"id": message_id, "role": "assistant", "content": content,
            "source": "text", "playable": True, **fields}


async def finished(overlay):
    for _ in range(150):
        if overlay._worker is None:
            return
        await asyncio.sleep(0.005)
    pytest.fail("fake dialogue worker did not finish")


def test_publish_and_status_do_not_wait_for_or_query_game():
    async def scenario():
        bridge = FakeBridge()
        bridge.state_gate = asyncio.Event()
        overlay = DialogueOverlay(bridge, lambda: bridge.cached)
        assert overlay.status()["shown"] is None
        assert bridge.state_calls == 0
        result = await overlay.publish(reply())
        assert result["queued"] is True
        assert bridge.state_calls == 0 and bridge.dialogues == []
        await bridge.state_entered.wait()
        assert overlay.status()["sending"] is True
        await overlay.close()
        assert bridge.dialogues == []

    asyncio.run(scenario())


def test_only_latest_reply_occupies_pending_slot():
    async def scenario():
        bridge = FakeBridge()
        overlay = DialogueOverlay(bridge, lambda: bridge.cached)
        for message_id in range(1, 50):
            await overlay.publish(reply(message_id, f"回复 {message_id}"))
        assert overlay.status()["queued"] is True
        await finished(overlay)
        assert len(bridge.dialogues) == 1
        assert bridge.dialogues[0]["text"] == "回复 49"
        assert bridge.dialogues[0]["session_id"] == "fixture-session"
        assert set(bridge.dialogues[0]) == {"message_id", "text", "session_id"}
        assert overlay.status()["sent_message_id"] == 49
        assert overlay.status()["shown"] is None
        assert "实际" in overlay.status()["message"]
        await overlay.close()

    asyncio.run(scenario())


def test_new_reply_supersedes_reply_still_waiting_for_game_state():
    async def scenario():
        bridge = FakeBridge()
        bridge.state_gate = asyncio.Event()
        overlay = DialogueOverlay(bridge, lambda: bridge.cached)
        await overlay.publish(reply(1, "旧回复"))
        await bridge.state_entered.wait()
        for message_id in range(2, 6):
            await overlay.publish(reply(message_id, f"新回复 {message_id}"))
        bridge.state_gate.set()
        await finished(overlay)
        assert [item["text"] for item in bridge.dialogues] == ["新回复 5"]
        assert overlay.status()["queued"] is False
        await overlay.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"bridge_version": "0.2.0", "capabilities": ["move", "stop"]},
    {"ready": False}, {"coop": False}, {"controlled_player_id": 0},
    {"players": [{"player_id": 0}]},
])
def test_unavailable_display_is_harmless(changes):
    async def scenario():
        bridge = FakeBridge()
        bridge.cached = game_state(**changes)
        overlay = DialogueOverlay(bridge, lambda: bridge.cached)
        result = await overlay.publish(reply())
        assert result["available"] is False
        assert result["error"] is None
        assert bridge.dialogues == [] and bridge.state_calls == 0
        if "capabilities" in changes:
            assert "更新模组" in result["message"]
            assert result["reason"] == "unsupported"
        await overlay.close()

    asyncio.run(scenario())


def test_slow_reply_can_bind_cached_session_but_requires_fresh_worker_state():
    async def scenario():
        bridge = FakeBridge()
        bridge.cached = game_state(captured_at=datetime.now(UTC) - timedelta(seconds=30))
        bridge.current = game_state()
        overlay = DialogueOverlay(bridge, lambda: bridge.cached)
        assert overlay.status()["reason"] == "stale"
        status = await overlay.publish(reply())
        assert status["queued"] is True and bridge.state_calls == 0
        await finished(overlay)
        assert len(bridge.dialogues) == 1
        assert bridge.dialogues[0]["session_id"] == bridge.cached.session_id
        assert overlay.status()["sent_message_id"] == 1
        await overlay.close()

    asyncio.run(scenario())


def test_missing_game_state_is_distinct_from_unsupported_mod():
    async def scenario():
        bridge = FakeBridge()
        overlay = DialogueOverlay(bridge, lambda: None)
        status = await overlay.publish(reply())
        assert status["reason"] == "disconnected"
        assert status["available"] is False
        assert status["error"] is None and status["last_error"] is None
        assert bridge.state_calls == 0 and bridge.dialogues == []
        await overlay.close()
        assert overlay.status()["reason"] == "closed"

    asyncio.run(scenario())


def test_only_playable_assistant_replies_are_forwarded():
    async def scenario():
        bridge = FakeBridge()
        overlay = DialogueOverlay(bridge, lambda: bridge.cached)
        for message in [reply(playable=False), reply(playable=1), reply(role="user"),
                        reply(source="system"), reply(content=None), reply(id=True),
                        reply(id=None), reply(id=" "), {}, None]:
            await overlay.publish(message)
        assert bridge.dialogues == [] and bridge.state_calls == 0
        await overlay.publish(reply(source="proactive"))
        await finished(overlay)
        assert len(bridge.dialogues) == 1
        await overlay.close()

    asyncio.run(scenario())


def test_worker_binds_to_original_session_and_rechecks_freshness():
    async def scenario():
        for current in [game_state(session_id="new-session"),
                        game_state(captured_at=datetime.now(UTC) - timedelta(seconds=9)),
                        game_state(capabilities=["move", "stop"])]:
            bridge = FakeBridge()
            bridge.current = current
            overlay = DialogueOverlay(bridge, lambda bridge=bridge: bridge.cached)
            await overlay.publish(reply())
            await finished(overlay)
            assert bridge.state_calls == 1 and bridge.dialogues == []
            assert overlay.status()["shown"] is None
            await overlay.close()

    asyncio.run(scenario())


def test_unknown_send_result_is_never_retried():
    async def scenario():
        bridge = FakeBridge()
        bridge.send_error = True
        overlay = DialogueOverlay(bridge, lambda: bridge.cached)
        await overlay.publish(reply())
        await finished(overlay)
        assert len(bridge.dialogues) == 1
        status = overlay.status()
        assert status["error"] == "send_unconfirmed"
        assert status["sent_message_id"] is None and status["shown"] is None
        assert "private" not in str(status)
        bridge.send_error = False
        await overlay.publish(reply())
        await finished(overlay)
        assert len(bridge.dialogues) == 1
        await overlay.publish(reply(2))
        await finished(overlay)
        assert len(bridge.dialogues) == 2
        await overlay.close()

    asyncio.run(scenario())


def test_duplicate_is_accepted_without_claiming_render_confirmation():
    async def scenario():
        bridge = FakeBridge()
        bridge.result = {"status": "duplicate"}
        overlay = DialogueOverlay(bridge, lambda: bridge.cached)
        await overlay.publish(reply())
        await finished(overlay)
        assert overlay.status()["sent_count"] == 1
        assert overlay.status()["shown"] is None
        await overlay.publish(reply())
        await finished(overlay)
        assert len(bridge.dialogues) == 1
        await overlay.close()

    asyncio.run(scenario())


def test_seen_ids_are_bounded():
    async def scenario():
        bridge = FakeBridge()
        overlay = DialogueOverlay(bridge, lambda: bridge.cached)
        for message_id in range(MAX_SEEN + 5):
            await overlay.publish(reply(message_id))
        assert len(overlay._seen) == MAX_SEEN
        await overlay.close()

    asyncio.run(scenario())


def test_wire_text_is_bounded_in_utf16_and_controls_are_removed():
    async def scenario():
        bridge = FakeBridge()
        overlay = DialogueOverlay(bridge, lambda: bridge.cached)
        await overlay.publish(reply(content="\x00聊天\r\n\t" + "🙂" * 900 + "\x7f"))
        await finished(overlay)
        text = bridge.dialogues[0]["text"]
        assert 1 <= len(text.encode("utf-16-le")) // 2 <= 600
        assert text.endswith("…")
        assert "\x00" not in text and "\r" not in text and "\x7f" not in text
        assert "\n\t" in text
        assert _bubble_text("🙂" * 300) == "🙂" * 300
        assert _bubble_text("字" * 600) == "字" * 600
        await overlay.close()

    asyncio.run(scenario())


def test_close_cancels_sender_and_prevents_late_work():
    async def scenario():
        bridge = FakeBridge()
        bridge.send_gate = asyncio.Event()
        overlay = DialogueOverlay(bridge, lambda: bridge.cached)
        await overlay.publish(reply())
        await bridge.send_entered.wait()
        await overlay.publish(reply(2))
        await overlay.close()
        bridge.send_gate.set()
        await overlay.publish(reply(3))
        await asyncio.sleep(0)
        assert bridge.cancelled
        assert len(bridge.dialogues) == 1
        assert overlay.status()["queued"] is False
        assert overlay.status()["sending"] is False
        assert overlay.status()["available"] is False
        assert overlay.status()["sent_message_id"] is None
        await overlay.close()

    asyncio.run(scenario())


def test_sender_timeout_is_bounded_and_does_not_retry():
    async def scenario():
        bridge = FakeBridge()
        bridge.send_gate = asyncio.Event()
        overlay = DialogueOverlay(bridge, lambda: bridge.cached, request_timeout=0.01)
        await overlay.publish(reply())
        await finished(overlay)
        assert bridge.cancelled and len(bridge.dialogues) == 1
        assert overlay.status()["error"] == "send_unconfirmed"
        await overlay.publish(reply())
        await finished(overlay)
        assert len(bridge.dialogues) == 1
        await overlay.close()

    asyncio.run(scenario())


def bridge_client(tmp_path, handler):
    config = tmp_path / "fixture-config.json"
    config.write_text(json.dumps({"base_url": "http://127.0.0.1:48861",
                                 "token": "fixture-token-only-123456"}), encoding="utf-8")
    return BridgeClient(config, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_bridge_dialogue_exact_authenticated_display_protocol(tmp_path):
    async def scenario():
        requests = []

        def handler(request):
            requests.append(request)
            assert request.method == "POST" and request.url.path == "/dialogue"
            assert request.headers["authorization"] == "Bearer fixture-token-only-123456"
            assert json.loads(request.content) == {
                "message_id": "fixture-id", "text": "只显示文字，不支付金币。",
                "session_id": "fixture-session"
            }
            return httpx.Response(200, json={"status": "accepted"})

        bridge = bridge_client(tmp_path, handler)
        assert await bridge.dialogue(message_id=" fixture-id ", text="只显示文字，不支付金币。",
                                     session_id="fixture-session") == {"status": "accepted"}
        assert len(requests) == 1
        await bridge.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("field, value", [
    ("message_id", ""), ("message_id", "x" * 101), ("message_id", "🙂" * 51),
    ("text", "x" * 601), ("text", "🙂" * 301), ("text", "a\x00b"),
    ("text", "a\rb"), ("text", "\ud800"), ("text", "  "),
    ("session_id", "x" * 201), ("session_id", None),
])
def test_bridge_dialogue_rejects_invalid_values_before_network(tmp_path, field, value):
    async def scenario():
        def handler(_request):
            pytest.fail("invalid display request reached transport")

        bridge = bridge_client(tmp_path, handler)
        fields = {"message_id": "fixture-id", "text": "测试", "session_id": "fixture-session"}
        fields[field] = value
        with pytest.raises(BridgeError):
            await bridge.dialogue(**fields)
        await bridge.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("body", [{}, {"status": "shown"}, {"status": []}])
def test_bridge_does_not_claim_unverified_display_response(tmp_path, body):
    async def scenario():
        bridge = bridge_client(tmp_path, lambda _request: httpx.Response(200, json=body))
        with pytest.raises(BridgeError, match="不.*重发"):
            await bridge.dialogue(message_id="fixture-id", text="测试", session_id="fixture-session")
        await bridge.close()

    asyncio.run(scenario())
