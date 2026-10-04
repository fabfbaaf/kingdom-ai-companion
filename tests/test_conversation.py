"""Dialogue/control race tests; use no game, microphone or external model service."""

import asyncio

import httpx
import pytest
from test_companion import FakeBridge, FakeModel, manager_for, store_for

from companion.app import create_app
from companion.contracts import Decision, StartRequest
from companion.controller import ActionJournal, ControlError
from companion.conversation import Conversation, quick_intent
from companion.conversation_contracts import ChatReply


class FakeVoice:
    def __init__(self):
        self.state = "ready"
        self.items = []

    def status(self):
        return {"state": self.state, "cursor": len(self.items), "model_ready": True}

    def events(self, after):
        return {"events": self.items[after:], "cursor": len(self.items), "dropped": False}

    async def start(self):
        self.state = "listening"
        return self.status()

    async def stop(self):
        self.state = "ready"
        return self.status()

    async def prepare_model(self):
        return self.status()

    async def close(self):
        await self.stop()

    def final(self, text):
        self.items.append({"seq": len(self.items) + 1, "text": text})


class ChatModel(FakeModel):
    def __init__(self, intent="chat", goal=None):
        super().__init__()
        self.reply = ChatReply(reply="知道了。", intent=intent, goal=goal)
        self.chat_gate = None
        self.chat_entered = asyncio.Event()
        self.chat_calls = 0

    async def chat(self, *_args):
        self.chat_calls += 1
        self.chat_entered.set()
        if self.chat_gate:
            await self.chat_gate.wait()
        return self.reply


def test_quick_control_requires_complete_explicit_phrase():
    assert quick_intent("停止！") == "stop"
    assert quick_intent("跟着我。") == "follow"
    assert quick_intent("如果有人说停止怎么办") is None
    assert quick_intent("别跟着我") is None


def test_follow_needs_no_model_and_spends_nothing(tmp_path):
    async def scenario():
        manager, bridge, _ = manager_for(tmp_path, configured=False)
        chat = Conversation(manager, FakeVoice())
        result = await chat.send("跟着我")
        assert result["intent"] == "follow"
        assert manager.mode == "follow" and manager.coin_budget == 0
        assert not bridge.commands
        await manager.stop()
        await chat.close()
    asyncio.run(scenario())


def test_emergency_stop_bypasses_busy_chat_and_late_follow(tmp_path):
    async def scenario():
        model = ChatModel("follow")
        model.chat_gate = asyncio.Event()
        manager, bridge, _ = manager_for(tmp_path, model=model)
        chat = Conversation(manager, FakeVoice())
        pending = asyncio.create_task(chat.send("现在陪我一起走"))
        await model.chat_entered.wait()
        stopped = await chat.send("停止")
        assert stopped["intent"] == "stop"
        model.chat_gate.set()
        reply = await pending
        assert "已取消" in reply["control_result"]
        assert manager.mode == "idle" and not bridge.commands
        await chat.close()
    asyncio.run(scenario())


def test_human_stop_endpoint_suppresses_inflight_intent(tmp_path):
    async def scenario():
        model = ChatModel("follow")
        model.chat_gate = asyncio.Event()
        store, bridge, voice = store_for(tmp_path), FakeBridge(), FakeVoice()
        app = create_app(store=store, bridge=bridge, model=model, voice=voice,
                         journal=ActionJournal(tmp_path / "session.local.json"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:48860") as client:
            token = (await client.get("/api/session")).json()["token"]
            headers = {"X-Kingdom-Session": token}
            assert (await client.post("/api/chat", json={"text": "你好"})).status_code == 403
            pending = asyncio.create_task(client.post(
                "/api/chat", headers=headers, json={"text": "陪我一起走"}))
            await model.chat_entered.wait()
            assert (await client.post("/api/control/stop", headers=headers)).status_code == 200
            model.chat_gate.set()
            result = (await pending).json()
            assert "已取消" in result["control_result"]
            assert not bridge.commands and app.state.manager.mode == "idle"
            assert (await client.post("/api/chat", headers=headers,
                                      json={"text": "  "})).status_code == 422
        await app.state.conversation.close()
        await app.state.manager.close()
    asyncio.run(scenario())


def test_idle_goal_records_plan_without_start_or_budget(tmp_path):
    async def scenario():
        manager, bridge, _ = manager_for(tmp_path, model=ChatModel("goal", "守住右侧"))
        chat = Conversation(manager, FakeVoice())
        result = await chat.send("帮我守住右侧")
        assert result["goal"] == "守住右侧"
        assert "开始自主陪玩" in result["control_result"]
        assert manager.mode == "idle" and manager.coin_budget == 0 and not bridge.commands
        await chat.close()
    asyncio.run(scenario())


def test_goal_change_discards_old_decision_without_refunding_budget(tmp_path):
    async def scenario():
        model = ChatModel("goal", "先回到我身边")
        model.decision = Decision(operation="pay", target_id="wall-1", max_coins=3)
        model.gate = asyncio.Event()
        manager, bridge, _ = manager_for(tmp_path, model=model)
        chat = Conversation(manager, FakeVoice())
        await manager.start(StartRequest(spending_mode="budgeted", mode="autonomous", coin_budget=4, decision_budget=5))
        await model.entered.wait()
        remaining = manager.decisions_remaining
        result = await chat.send("先回来，不修墙")
        assert "已调整" in result["control_result"]
        assert manager.coin_budget == 4 and manager.decisions_remaining == remaining
        model.decision = Decision(operation="stop")
        model.gate.set()
        await manager.task
        assert not bridge.commands
        assert manager.coin_budget == 4
        await chat.close()
    asyncio.run(scenario())


def test_goal_change_during_predispatch_state_cancels_payment(tmp_path):
    async def scenario():
        manager, bridge, model = manager_for(tmp_path)
        model.gate = asyncio.Event()
        await manager.start(StartRequest(spending_mode="budgeted", mode="autonomous", coin_budget=4))
        await model.entered.wait()
        bridge.state_gate = asyncio.Event()
        pending = asyncio.create_task(manager._perform(
            manager.generation, Decision(operation="pay", target_id="wall-1", max_coins=3),
            goal_revision=manager.goal_revision))
        await asyncio.sleep(0)
        manager.update_goal("不要修墙，回来")
        bridge.state_gate.set()
        from companion.controller import GoalChanged
        with pytest.raises(GoalChanged):
            await pending
        assert not bridge.commands and manager.coin_budget == 4 and manager.journal.pending is None
        await manager.stop()
    asyncio.run(scenario())


def test_voice_final_dispatched_once_and_old_session_not_replayed(tmp_path):
    async def scenario():
        model = ChatModel()
        manager, _, _ = manager_for(tmp_path, model=model)
        voice = FakeVoice()
        chat = Conversation(manager, voice)
        voice.final("旧会话语音")
        await chat.start_voice()
        voice.final("现在是什么时候")
        await asyncio.wait_for(model.chat_entered.wait(), 2)
        await asyncio.sleep(0.35)
        assert model.chat_calls == 1
        assert [m["content"] for m in chat.messages if m["role"] == "user"] == ["现在是什么时候"]
        await chat.close()
    asyncio.run(scenario())


def test_microphone_off_cancels_pending_voice_action(tmp_path):
    async def scenario():
        model = ChatModel("follow")
        model.chat_gate = asyncio.Event()
        manager, bridge, _ = manager_for(tmp_path, model=model)
        voice = FakeVoice()
        chat = Conversation(manager, voice)
        await chat.start_voice()
        voice.final("陪我一起走")
        await asyncio.wait_for(model.chat_entered.wait(), 2)
        chat.cancel_voice()
        await voice.stop()
        model.chat_gate.set()
        await asyncio.sleep(0.2)
        assert not bridge.commands and manager.mode == "idle"
        await chat.close()
    asyncio.run(scenario())


def test_busy_chat_keeps_bounded_backlog(tmp_path):
    async def scenario():
        model = ChatModel()
        model.chat_gate = asyncio.Event()
        manager, _, _ = manager_for(tmp_path, model=model)
        chat = Conversation(manager, FakeVoice())
        pending = asyncio.create_task(chat.send("第一条"))
        await model.chat_entered.wait()
        waiting = asyncio.create_task(chat.send("第二条"))
        await asyncio.sleep(0)
        assert chat.public()["queued"] == 1
        model.chat_gate.set()
        await asyncio.gather(pending, waiting)
        assert model.chat_calls == 2
        await chat.close()
    asyncio.run(scenario())


def test_follow_waiting_on_lifecycle_lock_cannot_restart_after_stop(tmp_path):
    async def scenario():
        manager, bridge, _ = manager_for(tmp_path, model=ChatModel())
        chat = Conversation(manager, FakeVoice())
        async with manager._lifecycle_lock:
            pending = asyncio.create_task(chat.send("跟着我"))
            # Fake release is immediate; following start is now queued on the lock.
            await asyncio.sleep(0)
            await chat.send("停止")
        result = await pending
        assert "撤销" in result["control_result"]
        assert manager.mode == "idle" and not bridge.heartbeat_leases
        await chat.close()
    asyncio.run(scenario())


def test_voice_stop_batch_does_not_reauthorize_follow_after_mic_off(tmp_path):
    async def scenario():
        manager, bridge, _ = manager_for(tmp_path, model=ChatModel())
        voice = FakeVoice()
        chat = Conversation(manager, voice)
        stopping, release = asyncio.Event(), asyncio.Event()
        original_stop = bridge.stop

        async def slow_stop():
            stopping.set()
            await release.wait()
            await original_stop()

        bridge.stop = slow_stop
        await chat.start_voice()
        voice.final("停止")
        voice.final("跟随我")
        await asyncio.wait_for(stopping.wait(), 2)
        chat.cancel_voice()
        await voice.stop()
        release.set()
        await asyncio.sleep(0.3)
        assert not bridge.heartbeat_leases and manager.mode == "idle"
        assert len([m for m in chat.messages if m["role"] == "user"]) == 1
        await chat.close()
    asyncio.run(scenario())


def test_chat_revocation_during_initial_state_read_cancels_start(tmp_path):
    async def scenario():
        manager, bridge, _ = manager_for(tmp_path)
        valid = True
        bridge.state_gate = asyncio.Event()
        pending = asyncio.create_task(manager.start(
            StartRequest(mode="follow"), guard=lambda: valid))
        await asyncio.sleep(0)
        valid = False
        bridge.state_gate.set()
        with pytest.raises(ControlError):
            await pending
        assert manager.mode == "idle" and not bridge.heartbeat_leases
    asyncio.run(scenario())


def test_shutdown_blocks_starts_before_slow_microphone_close(tmp_path):
    async def scenario():
        store, bridge, voice = store_for(tmp_path), FakeBridge(), FakeVoice()
        app = create_app(store=store, bridge=bridge, model=ChatModel(), voice=voice,
                         journal=ActionJournal(tmp_path / "session.local.json"))
        app.state.request_shutdown = lambda: None
        closing, release = asyncio.Event(), asyncio.Event()
        original_stop = voice.stop

        async def slow_close():
            closing.set()
            await release.wait()
            return await original_stop()

        voice.stop = slow_close
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:48860") as client:
            token = (await client.get("/api/session")).json()["token"]
            headers = {"X-Kingdom-Session": token}
            pending = asyncio.create_task(client.post("/api/shutdown", headers=headers))
            await closing.wait()
            result = await client.post("/api/control/start", headers=headers,
                                       json={"mode": "follow"})
            assert result.status_code == 409
            assert not bridge.heartbeat_leases
            release.set()
            assert (await pending).status_code == 200
        await app.state.conversation.close()
        await app.state.manager.close()
    asyncio.run(scenario())
