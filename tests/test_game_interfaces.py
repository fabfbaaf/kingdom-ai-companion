"""Expanded native interface contract and campaign lifecycle; no live game calls."""

import asyncio
import copy

import pytest
from pydantic import ValidationError
from test_autonomous_play import SequenceModel, move
from test_companion import FakeBridge, manager_for

from companion.contracts import CAMPAIGN_GOAL, Decision, StartRequest
from companion.controller import ControlError, playable


class ExpandedBridge(FakeBridge):
    def __init__(self):
        super().__init__()
        self.data["bridge_version"] = "0.4.0"
        self.data["capabilities"] += ["extended_world", "move_long", "move_to", "sprint", "drop", "ability", "map", "sail"]
        self.data["players"][1]["currencies"] = {"coins": 100, "gems": 10}
        self.data["players"][1]["coins"] = 100
        self.data["world"] = {"campaign": {"completed": False}, "control": {"transition": False, "manual_takeover": False}}

    async def command(self, body):
        player = self.data["players"][1]
        original_coins = player["coins"]
        receipt = await super().command(body)
        if body["operation"] == "pay" and body.get("currency") != "coins":
            player["coins"] = original_coins
            player["currencies"][body["currency"]] -= body["max_coins"]
        if body["operation"] == "move_to":
            player["x"] = body["target_x"]
        if body["operation"] == "map":
            opening = body["map_action"] != "close"
            self.data.update(ready=not opening, ui_ready=opening)
            self.data["world"]["ui"] = {"map_open": opening}
        return receipt


@pytest.mark.parametrize("body", [
    {"operation": "move", "direction": "right", "duration_ms": 30000, "sprint": True},
    {"operation": "move_to", "target_x": 1000.0, "sprint": True},
    {"operation": "pay", "target_id": "building", "currency": "gems"},
    {"operation": "pay", "target_id": "building", "max_coins": 100},
    {"operation": "drop", "currency": "coins", "amount": 30},
    {"operation": "ability", "ability": "native-skill", "ability_action": "channel"},
    {"operation": "map", "map_action": "select", "land": 3},
    {"operation": "sail"},
])
def test_expanded_game_decisions_accept_native_operations(body):
    assert Decision.model_validate(body).operation == body["operation"]


@pytest.mark.parametrize("body", [
    {"operation": "move_to", "target_x": float("inf")},
    {"operation": "drop", "currency": "coins", "amount": 0},
    {"operation": "ability", "ability": "native-skill", "target_id": "building"},
    {"operation": "map", "map_action": "select"},
    {"operation": "map", "map_action": "open", "land": 3},
    {"operation": "sail", "direction": "right"},
])
def test_expanded_operations_do_not_mix_unrelated_parameters(body):
    with pytest.raises(ValidationError):
        Decision.model_validate(body)


def test_default_goal_is_campaign_completion_with_wallet_authority():
    request = StartRequest(mode="autonomous")
    assert CAMPAIGN_GOAL in request.goal and request.spending_mode == "wallet"


@pytest.mark.parametrize("currency,price", [("coins", 50), ("gems", 3)])
def test_native_current_currency_payment_has_no_twenty_coin_cap(tmp_path, currency, price):
    async def scenario():
        bridge = ExpandedBridge()
        bridge.data["players"][1]["current_payable"].update(currency=currency, price=price)
        before_p1 = copy.deepcopy(bridge.data["players"][0])
        manager, _, _ = manager_for(tmp_path, bridge=bridge)
        result = await manager.manual(Decision(operation="pay", target_id="wall-1"))
        assert result["status"] == "completed"
        assert bridge.commands[0]["currency"] == currency and bridge.commands[0]["max_coins"] == price
        assert manager.coin_budget is None and bridge.data["players"][0] == before_p1
        await manager.close()
    asyncio.run(scenario())


def test_new_bridge_keeps_long_move_and_destination(tmp_path):
    async def scenario():
        manager, bridge, _ = manager_for(tmp_path, bridge=ExpandedBridge())
        await manager.manual(Decision(operation="move", direction="right", duration_ms=20000))
        assert bridge.commands[0]["duration_ms"] == 20000
        await manager.manual(Decision(operation="move_to", target_x=350.0, sprint=True))
        assert bridge.commands[-1]["target_x"] == 350.0 and bridge.data["players"][1]["x"] == 350
        await manager.close()
    asyncio.run(scenario())


def test_owned_map_allows_map_commands_without_world_movement(tmp_path):
    async def scenario():
        model = SequenceModel([Decision(operation="map", map_action="open"),
                               Decision(operation="map", map_action="select", land=2),
                               Decision(operation="map", map_action="close"), move()])
        manager, bridge, _ = manager_for(tmp_path, bridge=ExpandedBridge(), model=model)
        await manager.start(StartRequest(mode="autonomous", continuous=True, decision_interval_seconds=0.2))
        await asyncio.wait_for(model.exhausted.wait(), 2)
        assert [item["operation"] for item in bridge.commands] == ["map", "map", "map", "move"]
        assert manager.status()["running"]
        state = await bridge.state()
        state.ready, state.ui_ready = False, True
        playable(state, "map")
        with pytest.raises(ControlError):
            playable(state, "move")
        await manager.stop()
    asyncio.run(scenario())


def test_native_campaign_completion_finishes_without_another_model_request(tmp_path):
    async def scenario():
        bridge = ExpandedBridge()
        bridge.data["world"]["campaign"]["completed"] = True
        manager, _, model = manager_for(tmp_path, bridge=bridge)
        await manager.start(StartRequest(mode="autonomous", continuous=True))
        await asyncio.wait_for(manager.task, 1)
        assert model.calls == 0 and bridge.commands == []
        assert manager.last_result["status"] == "completed" and "通关目标达成" in manager.last_result["message"]
    asyncio.run(scenario())


def test_premature_world_action_in_owned_map_replans_instead_of_ending_play(tmp_path):
    async def scenario():
        model = SequenceModel([Decision(operation="map", map_action="open"), move(),
                               Decision(operation="map", map_action="close"), move()])
        manager, bridge, _ = manager_for(tmp_path, bridge=ExpandedBridge(), model=model)
        await manager.start(StartRequest(mode="autonomous", continuous=True, decision_interval_seconds=0.2))
        await asyncio.wait_for(model.exhausted.wait(), 2)
        assert manager.status()["running"] and manager.error is None
        assert [item["operation"] for item in bridge.commands] == ["map", "map", "move"]
        await manager.stop()
    asyncio.run(scenario())


class SailingBridge(ExpandedBridge):
    def __init__(self):
        super().__init__()
        self.loading = asyncio.Event()

    async def command(self, body):
        result = await super().command(body)
        if body["operation"] == "sail":
            self.data["ready"] = False
            self.data["control_epoch"] += 1
            self.data["world"]["control"]["transition"] = True
            self.loading.set()
        return result

    def arrive(self):
        self.data.update(session_id="second-island", ready=True)
        self.data["world"]["control"]["transition"] = False


def test_cross_island_resumes_same_goal_with_new_lease(tmp_path):
    async def scenario():
        bridge = SailingBridge()
        model = SequenceModel([Decision(operation="sail"), move()])
        manager, _, _ = manager_for(tmp_path, bridge=bridge, model=model)
        await manager.start(StartRequest(mode="autonomous", continuous=True, decision_interval_seconds=0.2))
        await asyncio.wait_for(bridge.loading.wait(), 1)
        goal = manager.goal
        await asyncio.sleep(0.05)
        bridge.arrive()
        await asyncio.wait_for(model.exhausted.wait(), 2)
        assert manager.status()["running"] and manager.goal == goal
        assert bridge.commands[-1]["session_id"] == "second-island"
        assert bridge.commands[-1]["control_id"] != bridge.commands[0]["control_id"]
        await manager.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("stop_type", ["button", "f8"])
def test_stop_during_loading_prevents_automatic_resume(tmp_path, stop_type):
    async def scenario():
        bridge = SailingBridge()
        model = SequenceModel([Decision(operation="sail"), move()])
        manager, _, _ = manager_for(tmp_path, bridge=bridge, model=model)
        await manager.start(StartRequest(mode="autonomous", continuous=True, decision_interval_seconds=0.2))
        await asyncio.wait_for(bridge.loading.wait(), 1)
        if stop_type == "button":
            await manager.stop()
        else:
            bridge.data["world"]["control"]["manual_takeover"] = True
            await asyncio.wait_for(manager.task, 1)
        bridge.arrive()
        await asyncio.sleep(0.3)
        assert manager.mode == "idle" and len(bridge.commands) == 1 and model.calls == 1
        await manager.close()
    asyncio.run(scenario())
