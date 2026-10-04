"""Dialogue and actions share facts without sharing request queues or credentials."""

import asyncio
import json
from datetime import timedelta

import httpx
from test_companion import manager_for, state_data, store_for

from companion.config import ModelSettings
from companion.contracts import Decision, GameState, StartRequest
from companion.gameplay_context import safe_context
from companion.model import ModelClient


def completion(content):
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
        "content": content}}]})


def test_dialogue_cannot_block_action_request_lane(tmp_path):
    async def scenario():
        store = store_for(tmp_path)
        store.update(ModelSettings(model="synthetic", endpoint="http://localhost:9999/v1"))
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            data = json.loads(request.content)
            if data["max_tokens"] == 4096:
                entered.set()
                await release.wait()
                return completion('{"reply":"我在听，继续走吧。","intent":"chat"}')
            return completion('{"operation":"stop"}')

        model = ModelClient(store, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        pending = asyncio.create_task(model.chat("陪我聊聊", [], None, {}))
        await entered.wait()
        result = await asyncio.wait_for(model.decide(GameState.model_validate(state_data()),
                                                     goal="向右", coin_budget=0), 0.5)
        assert result.operation == "stop" and not pending.done()
        release.set()
        assert (await pending).intent == "chat"
        await model.close()

    asyncio.run(scenario())


def test_shared_context_whitelist_and_verified_history(tmp_path):
    async def scenario():
        store = store_for(tmp_path)
        store.update(ModelSettings(model="synthetic", endpoint="http://localhost:9999/v1"))
        observed = []
        context = {"control_goal": "守右边", "mode": "autonomous", "stale": False,
                   "last_action": {"status": "verified", "operation": "move",
                                   "action_id": "private-id", "api_key": "secret"},
                   "current_action": {"operation": "pay", "status": "sent"},
                   "api_key": "secret", "pending_action": {"token": "secret"}}

        def handler(request):
            payload = json.loads(request.content)
            packet = json.loads(payload["messages"][-1]["content"])
            observed.append(packet["context"])
            assert "secret" not in payload["messages"][-1]["content"]
            assert "private-id" not in payload["messages"][-1]["content"]
            if payload["max_tokens"] == 4096:
                return completion('{"reply":"在往右走，付款还没有核验。","intent":"chat"}')
            return completion('{"operation":"stop"}')

        model = ModelClient(store, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        model.set_context_provider(lambda: context)
        await model.decide(GameState.model_validate(state_data()), goal="守右边", coin_budget=0)
        await model.chat("刚做了什么？", [], None, {"context": context})
        action_context = dict(observed[0])
        territory = action_context.pop("territory")
        assert territory["bounds_status"] == "unknown" and territory.get("center_x") is None
        assert action_context == safe_context(context)
        assert observed[1] == {**safe_context(context), "stale": True}
        await model.close()

    asyncio.run(scenario())


def test_target_change_cancels_slow_decision_without_resetting_budget(tmp_path):
    class Model:
        def __init__(self):
            self.entered = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.goals = []

        async def decide(self, state, *, goal, coin_budget):
            self.goals.append(goal)
            if goal == "旧目标":
                self.entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    raise
            return Decision(operation="stop")

    async def scenario():
        model = Model()
        manager, bridge, _ = manager_for(tmp_path, model=model)
        await manager.start(StartRequest(spending_mode="budgeted", mode="autonomous", goal="旧目标", coin_budget=4,
                                         decision_budget=3))
        await model.entered.wait()
        generation = manager.generation
        await manager.change_goal("新目标")
        await asyncio.wait_for(manager.task, 0.5)
        assert model.cancelled.is_set() and model.goals == ["旧目标", "新目标"]
        assert manager.generation == generation and manager.coin_budget == 4
        assert manager.decisions_remaining == 1 and not bridge.commands

    asyncio.run(scenario())


def test_state_staleness_and_session_do_not_reuse_old_context(tmp_path):
    async def scenario():
        manager, bridge, _ = manager_for(tmp_path)
        await manager._state()
        manager.last_result = {"status": "verified", "operation": "move"}
        manager.recent_actions.append({"status": "verified", "operation": "move"})
        assert manager.gameplay_context()["stale"] is False
        manager.latest_state = manager.latest_state.model_copy(update={
            "captured_at": manager.latest_state.captured_at - timedelta(seconds=5)})
        assert manager.gameplay_context()["scene"] is None
        bridge.data["session_id"] = "another-session"
        await manager._state()
        assert manager.gameplay_context()["last_action"] is None
        assert manager.gameplay_context()["recent_actions"] == []

    asyncio.run(scenario())
