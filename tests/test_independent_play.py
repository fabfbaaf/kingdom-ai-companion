"""P2 remains autonomous while P1 is idle; observations and providers are synthetic."""

import asyncio
import copy
import json

import httpx
import pytest
from pydantic import ValidationError
from test_autonomous_play import SequenceModel, continuous, move
from test_companion import manager_for, state_data, store_for

from companion.config import ModelSettings
from companion.contracts import INDEPENDENT_GOAL, Decision, GameState, StartRequest
from companion.controller import MotionPlan
from companion.gameplay_context import safe_context
from companion.model import ModelClient


def test_independent_defaults_preserve_explicit_goal_and_legacy_clients():
    assert StartRequest(mode="autonomous").play_style == "cooperate"
    assert StartRequest(mode="autonomous", play_style="independent").goal == INDEPENDENT_GOAL
    assert StartRequest(mode="autonomous", play_style="independent", goal="守右边").goal == "守右边"
    for style in (None, True, "solo", {"player_id": 0}):
        with pytest.raises(ValidationError):
            StartRequest(mode="autonomous", play_style=style)


def test_idle_distant_p1_does_not_end_p2_play_after_thirty_decisions(tmp_path):
    async def scenario():
        model = SequenceModel([move()] * 31 + [Decision(operation="stop"), move()])
        manager, bridge, _ = manager_for(tmp_path, model=model)
        bridge.data["players"][0]["x"] = -10000
        original_p1 = copy.deepcopy(bridge.data["players"][0])
        try:
            await manager.start(continuous(play_style="independent"))
            await asyncio.wait_for(model.exhausted.wait(), 9)
            status = manager.status()
            assert status["running"] and status["continuous"] and status["play_style"] == "independent"
            assert status["decisions_made"] == 34 and len(bridge.commands) == 32
            assert bridge.data["players"][0] == original_p1
            assert all(command["player_id"] == 1 for command in bridge.commands)
            assert manager.coin_budget is None and manager.journal.pending is None
            assert manager.gameplay_context()["play_style"] == "independent"
        finally:
            await manager.stop()
        assert manager.status()["play_style"] is None
    asyncio.run(scenario())


def test_unreadable_p1_position_is_not_an_independent_start_requirement(tmp_path):
    async def scenario():
        model = SequenceModel([move()])
        manager, bridge, _ = manager_for(tmp_path, model=model)
        bridge.data["players"][0]["x"] = None
        try:
            await manager.start(continuous(play_style="independent"))
            await asyncio.wait_for(model.exhausted.wait(), 1)
            assert manager.status()["running"] and len(bridge.commands) == 1
        finally:
            await manager.stop()
    asyncio.run(scenario())


def test_unavailable_target_replans_without_spending_or_waiting_for_p1(tmp_path):
    async def scenario():
        model = SequenceModel([Decision(operation="pay", target_id="wall-1", max_coins=3), move()])
        manager, bridge, _ = manager_for(tmp_path, model=model)
        bridge.data["players"][1]["current_payable"]["can_pay"] = False
        try:
            await manager.start(continuous(play_style="independent", coin_budget=3))
            await asyncio.wait_for(model.exhausted.wait(), 1)
            assert manager.status()["running"] and manager.coin_budget == 3
            assert [item["operation"] for item in bridge.commands] == ["move"]
        finally:
            await manager.stop()
    asyncio.run(scenario())


def test_independent_prefetch_ignores_p1_wallet_but_checks_p2_and_world(tmp_path):
    async def scenario():
        manager, bridge, _ = manager_for(tmp_path)
        manager.play_style = "independent"
        before = await bridge.state()
        plan = MotionPlan(move(), before, manager.goal_revision)
        bridge.data["players"][0]["coins"] += 1
        state = await bridge.state()
        assert manager._usable_plan(plan, state)
        manager.play_style = "cooperate"
        assert not manager._usable_plan(plan, state)
        manager.play_style = "independent"
        bridge.data["players"][1]["coins"] += 1
        assert not manager._usable_plan(plan, await bridge.state())
        bridge.data["players"][1]["coins"] -= 1
        bridge.data["world"]["nearby_enemies"] = [{"x": 1, "attacking": True}]
        assert not manager._usable_plan(plan, await bridge.state())
    asyncio.run(scenario())


def test_follow_remains_explicit_and_goal_updates_preserve_independence(tmp_path):
    async def scenario():
        model = SequenceModel([])
        manager, _, _ = manager_for(tmp_path, model=model)
        try:
            await manager.start(continuous(play_style="independent", coin_budget=4))
            await asyncio.wait_for(model.exhausted.wait(), 1)
            await manager.change_goal("巡视右侧")
            assert manager.play_style == "independent" and manager.coin_budget == 4
        finally:
            await manager.stop()
        try:
            await manager.start(StartRequest(spending_mode="budgeted", mode="follow", play_style="independent", coin_budget=4))
            assert manager.status()["play_style"] is None and manager.coin_budget == 0
        finally:
            await manager.stop()
    asyncio.run(scenario())


def test_decision_and_chat_receive_independent_style_without_new_authority(tmp_path):
    async def scenario():
        store = store_for(tmp_path)
        store.update(ModelSettings(model="fixture", endpoint="http://localhost:9999/v1"))
        packets = []

        def handler(request):
            data = json.loads(request.content)
            packets.append((data["messages"][0]["content"], json.loads(data["messages"][-1]["content"])))
            reply = ('{"reply":"我自己巡视，你歇着吧。","intent":"chat"}'
                     if data["max_tokens"] == 4096 else '{"operation":"stop"}')
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                                                         "message": {"content": reply}}]})

        model = ModelClient(store, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        model.set_context_provider(lambda: {"play_style": "independent", "continuous": True,
                                            "mode": "autonomous", "token": "private"})
        await model.decide(GameState.model_validate(state_data()), goal=INDEPENDENT_GOAL, coin_budget=0)
        await model.chat("我先歇会儿", [], None, {"play_style": "independent"})
        assert packets[0][1]["context"]["play_style"] == "independent"
        assert "不因P1静止或距离较远而停下" in packets[0][0]
        assert packets[1][1]["control"]["play_style"] == "independent"
        assert "普通闲聊不切回跟随" in packets[1][0]
        assert "private" not in json.dumps(packets)
        assert "play_style" not in safe_context({"play_style": "single-player"})
        await model.close()
    asyncio.run(scenario())
