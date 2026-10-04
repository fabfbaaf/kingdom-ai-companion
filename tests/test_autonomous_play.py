"""Continuous play uses only synthetic data, with no chat or live game calls."""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError
from test_companion import FakeModel, manager_for, state_data, store_for
from test_movement_protocol import MovingBridge, PlanningModel

from companion.config import ModelSettings
from companion.contracts import Decision, GameState, StartRequest
from companion.model import ModelClient


class SequenceModel(FakeModel):
    def __init__(self, decisions):
        super().__init__()
        self.decisions = iter(decisions)
        self.exhausted = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.budgets = []
        self.times = []

    async def decide(self, state, **kwargs):
        self.calls += 1
        self.times.append(asyncio.get_running_loop().time())
        self.budgets.append(kwargs["coin_budget"])
        decision = next(self.decisions, None)
        if decision is not None:
            return decision
        self.exhausted.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


def move():
    return Decision(operation="move", direction="right", duration_ms=300)


def continuous(**kwargs):
    if "coin_budget" in kwargs:
        kwargs.setdefault("spending_mode", "budgeted")
    return StartRequest(mode="autonomous", continuous=True, decision_budget=1,
                        decision_interval_seconds=0.2, **kwargs)


def test_wait_then_move_and_pay_without_chat_or_decision_limit(tmp_path):
    async def scenario():
        model = SequenceModel([Decision(operation="stop"), move(),
                               Decision(operation="pay", target_id="wall-1", max_coins=3),
                               Decision(operation="stop")])
        manager, bridge, _ = manager_for(tmp_path, model=model)
        try:
            await manager.start(continuous(coin_budget=3))
            await asyncio.wait_for(model.exhausted.wait(), 2)
            status = manager.status()
            assert status["running"] and status["continuous"]
            assert status["decisions_remaining"] is None and status["decisions_made"] == 5
            assert status["last_result"]["status"] == "waiting"
            assert [c["operation"] for c in bridge.commands] == ["move", "pay"]
            assert manager.coin_budget == 0 and bridge.data["players"][1]["coins"] == 5
            assert not manager.journal.pending and model.budgets == [3, 3, 3, 0, 0]
            assert all(b - a >= 0.18 for a, b in zip(model.times, model.times[1:]))
        finally:
            await manager.stop()
        assert model.cancelled.is_set() and not manager.status()["continuous"]
        assert bridge.stops >= 1 and len(bridge.commands) == 2
    asyncio.run(scenario())


def test_unavailable_pay_reobserves_and_moves_without_ending_play(tmp_path):
    async def scenario():
        model = SequenceModel([Decision(operation="pay", target_id="old-target", max_coins=3),
                               move()])
        manager, bridge, _ = manager_for(tmp_path, model=model)
        try:
            await manager.start(continuous(coin_budget=3))
            await asyncio.wait_for(model.exhausted.wait(), 1)
            assert manager.status()["running"] and manager.error is None
            assert [c["operation"] for c in bridge.commands] == ["move"]
            assert manager.coin_budget == 3 and not manager.journal.pending
        finally:
            await manager.stop()
    asyncio.run(scenario())


def test_spent_coin_budget_never_resets_on_wait_or_goal_change(tmp_path):
    async def scenario():
        pay = Decision(operation="pay", target_id="wall-1", max_coins=3)
        model = SequenceModel([pay, Decision(operation="stop"), pay, move()])
        manager, bridge, _ = manager_for(tmp_path, model=model)
        try:
            await manager.start(continuous(coin_budget=3))
            await asyncio.wait_for(model.exhausted.wait(), 2)
            assert [c["operation"] for c in bridge.commands] == ["pay", "move"]
            assert model.budgets == [3, 0, 0, 0, 0]
            await manager.change_goal("继续巡视")
            await asyncio.sleep(0)
            assert manager.coin_budget == 0
        finally:
            await manager.stop()
    asyncio.run(scenario())


def test_lost_feedback_does_not_end_continuous_play_or_require_review(tmp_path):
    async def scenario():
        model = SequenceModel([move(), move()])
        manager, bridge, _ = manager_for(tmp_path, model=model)
        bridge.unknown = True
        try:
            await manager.start(continuous())
            await asyncio.wait_for(model.exhausted.wait(), 1)
            assert manager.status()["running"] and manager.status()["continuous"]
            assert not manager.status()["needs_review"] and len(bridge.commands) == 2
            assert model.calls == 3
        finally:
            await manager.stop()
    asyncio.run(scenario())


def test_prefetched_wait_does_not_end_continuous_play(tmp_path):
    async def scenario():
        bridge, model = MovingBridge(), PlanningModel()
        manager, _, _ = manager_for(tmp_path, model=model, bridge=bridge)
        try:
            await manager.start(continuous())
            await asyncio.wait_for(model.prefetch_entered.wait(), 1)
            bridge.complete.set()
            async with asyncio.timeout(1):
                while model.calls < 3:
                    await asyncio.sleep(0.01)
            assert manager.status()["running"] and manager.status()["continuous"]
            assert manager.last_result["status"] == "waiting" and len(bridge.commands) == 1
            assert manager.decisions_made >= 3
        finally:
            await manager.stop()
    asyncio.run(scenario())


def test_finite_mode_keeps_stop_and_decision_budget_behavior(tmp_path):
    async def scenario():
        manager, bridge, model = manager_for(tmp_path)
        await manager.start(StartRequest(mode="autonomous", decision_budget=1))
        await asyncio.wait_for(manager.task, 1)
        assert manager.mode == "idle" and manager.decisions_remaining == 0
        assert not manager.continuous and model.calls == 1 and not bridge.commands
    asyncio.run(scenario())


def test_continuous_is_strict_and_follow_does_not_enable_model_loop(tmp_path):
    with pytest.raises(ValidationError):
        StartRequest(mode="autonomous", continuous="true")

    async def scenario():
        manager, _, model = manager_for(tmp_path)
        try:
            await manager.start(StartRequest(mode="follow", continuous=True))
            assert not manager.status()["continuous"] and model.calls == 0
        finally:
            await manager.stop()
    asyncio.run(scenario())


def test_prompts_explain_autonomy_and_current_review_status(tmp_path):
    async def scenario():
        store = store_for(tmp_path)
        store.update(ModelSettings(model="mock", endpoint="http://localhost:9999/v1"))
        observed = []

        def handler(request):
            payload = json.loads(request.content)
            packet = json.loads(payload["messages"][-1]["content"])
            observed.append((payload["messages"][0]["content"], packet))
            text = ('{"reply":"我继续巡视。","intent":"chat"}'
                    if payload["max_tokens"] == 4096 else '{"operation":"stop"}')
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                                                          "message": {"content": text}}]})

        model = ModelClient(store, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        model.set_context_provider(lambda: {"continuous": True, "mode": "autonomous"})
        await model.decide(GameState.model_validate(state_data()), goal="经营营地", coin_budget=3)
        await model.chat("你继续", [], None, {"continuous": True, "needs_review": False})
        assert "仅表示本轮等待" in observed[0][0]
        assert observed[0][1]["context"]["continuous"] is True
        assert observed[1][1]["control"]["needs_review"] is False
        assert "不能因此谎称" in observed[1][0]
        await model.close()
    asyncio.run(scenario())
