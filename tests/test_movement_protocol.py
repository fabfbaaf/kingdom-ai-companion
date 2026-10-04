"""Movement protocol and overlapping planning use only synthetic observations."""

import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError
from test_companion import FakeBridge, FakeModel, manager_for, state_data, store_for

from companion.config import ModelSettings
from companion.contracts import Decision, GameState, Receipt, StartRequest
from companion.model import ModelClient


@pytest.mark.parametrize("data", [
    {"operation": "move", "direction": "right", "duration_ms": 99},
    {"operation": "move", "direction": "right", "duration_ms": 3000, "sprint": 1},
    {"operation": "move", "direction": "right", "duration_ms": 3000, "sprint": "true"},
    {"operation": "stop", "sprint": False},
    {"operation": "pay", "target_id": "wall", "max_coins": 3, "sprint": True},
])
def test_invalid_movement_protocol(data):
    with pytest.raises(ValidationError):
        Decision.model_validate(data)


def test_sprint_default_and_nonmovement_wire():
    decision = Decision(operation="move", direction="right", duration_ms=5000)
    assert decision.sprint is False
    assert "sprint" not in Decision(operation="stop").model_dump()
    assert "sprint" not in Decision(operation="pay", target_id="wall", max_coins=3).model_dump()
    assert StartRequest(mode="autonomous").decision_interval_seconds == 1


def test_live_motion_context_sent_without_untrusted_extra_fields(tmp_path):
    async def scenario():
        store = store_for(tmp_path)
        store.update(ModelSettings(model="synthetic", endpoint="http://localhost:9999/v1"))

        def handler(request):
            payload = json.loads(request.content)
            packet = json.loads(payload["messages"][1]["content"])
            assert packet["in_flight"] == {"direction": "right", "sprint": True,
                                            "remaining_ms": 1500}
            assert "P2.can_sprint" in payload["messages"][0]["content"]
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "content": '{"operation":"move","direction":"right","duration_ms":3000,"sprint":true}'}}]})

        model = ModelClient(store, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        decision = await model.decide(GameState.model_validate(state_data()), goal="follow",
                                      coin_budget=0, in_flight={"direction": "right", "sprint": True,
                                                               "remaining_ms": 1500, "secret": "x"})
        assert decision.sprint and decision.duration_ms == 3000
        await model.close()
    asyncio.run(scenario())


class MovingBridge(FakeBridge):
    def __init__(self):
        super().__init__()
        self.data["bridge_version"] = "0.2.0"
        self.data["capabilities"] += ["sprint", "move_long"]
        self.data["players"][1]["can_sprint"] = True
        self.complete = asyncio.Event()
        self.submitted = asyncio.Event()
        self.command_times = []

    async def command(self, body):
        result = await super().command(body)
        self.command_times.append(asyncio.get_running_loop().time())
        self.submitted.set()
        return result.model_copy(update={"status": "running"})

    async def receipt(self, action_id):
        return Receipt(action_id=action_id, session_id=self.data["session_id"],
                       status="completed" if self.complete.is_set() else "running")


class PlanningModel(FakeModel):
    def __init__(self, next_decision=None):
        super().__init__(Decision(operation="move", direction="right", duration_ms=1500, sprint=True))
        self.next = next_decision or Decision(operation="stop")
        self.prefetch_entered = asyncio.Event()
        self.prefetch_gate = None
        self.prefetch_cancelled = False
        self.make_stale = False
        self.context = None

    async def decide(self, state, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return self.decision
        if kwargs.get("in_flight"):
            self.context = (state, kwargs["in_flight"])
            if self.make_stale:
                state.captured_at -= timedelta(seconds=10)
            self.prefetch_entered.set()
            try:
                if self.prefetch_gate:
                    await self.prefetch_gate.wait()
            except asyncio.CancelledError:
                self.prefetch_cancelled = True
                raise
            return self.next
        return Decision(operation="stop")


def test_planning_overlaps_live_movement_and_removes_post_move_delay(tmp_path):
    async def scenario():
        bridge, model = MovingBridge(), PlanningModel()
        manager, _, _ = manager_for(tmp_path, bridge=bridge, model=model)
        await manager.start(StartRequest(mode="autonomous", decision_interval_seconds=0.2))
        await asyncio.wait_for(model.prefetch_entered.wait(), 1)
        assert not bridge.complete.is_set()
        assert manager.journal.pending is not None and len(bridge.commands) == 1
        assert model.context[0].observation_seq > 1
        assert model.context[1]["remaining_ms"] > 0
        assert bridge.commands[0]["duration_ms"] == 1500 and bridge.commands[0]["sprint"]
        finished_at = asyncio.get_running_loop().time()
        bridge.complete.set()
        await asyncio.wait_for(manager.task, 0.6)
        assert asyncio.get_running_loop().time() - finished_at < 0.6
        assert model.calls == 2 and manager.journal.pending is None
        assert manager.last_result["status"] == "stopped"
    asyncio.run(scenario())


def test_prefetched_next_move_waits_for_verified_receipt(tmp_path):
    async def scenario():
        bridge = MovingBridge()
        model = PlanningModel(Decision(operation="move", direction="right", duration_ms=500))
        manager, _, _ = manager_for(tmp_path, bridge=bridge, model=model)
        await manager.start(StartRequest(mode="autonomous", decision_interval_seconds=0.2,
                                         decision_budget=2))
        await asyncio.wait_for(model.prefetch_entered.wait(), 1)
        assert len(bridge.commands) == 1
        finished_at = asyncio.get_running_loop().time()
        bridge.complete.set()
        await asyncio.wait_for(manager.task, 0.8)
        assert len(bridge.commands) == 2
        assert bridge.command_times[1] - finished_at < 0.4
        assert manager.decisions_remaining == 0
        assert manager.journal.pending is None
    asyncio.run(scenario())


def test_prefetched_payment_is_discarded_and_replanned_after_movement(tmp_path):
    async def scenario():
        bridge = MovingBridge()
        model = PlanningModel(Decision(operation="pay", target_id="wall-1", max_coins=3))
        manager, _, _ = manager_for(tmp_path, bridge=bridge, model=model)
        await manager.start(StartRequest(spending_mode="budgeted", mode="autonomous", coin_budget=3,
                                         decision_interval_seconds=0.2, decision_budget=3))
        await asyncio.wait_for(model.prefetch_entered.wait(), 1)
        bridge.complete.set()
        await asyncio.wait_for(manager.task, 1)
        assert [body["operation"] for body in bridge.commands] == ["move"]
        assert manager.coin_budget == 3 and manager.decisions_remaining == 0 and model.calls == 3
    asyncio.run(scenario())


def test_expired_motion_plan_is_not_dispatched(tmp_path):
    async def scenario():
        bridge = MovingBridge()
        model = PlanningModel(Decision(operation="move", direction="left", duration_ms=500))
        model.make_stale = True
        manager, _, _ = manager_for(tmp_path, bridge=bridge, model=model)
        await manager.start(StartRequest(mode="autonomous", decision_interval_seconds=0.2,
                                         decision_budget=3))
        await asyncio.wait_for(model.prefetch_entered.wait(), 1)
        bridge.complete.set()
        await asyncio.wait_for(manager.task, 1)
        assert len(bridge.commands) == 1 and model.calls == 3
        assert manager.decisions_remaining == 0
    asyncio.run(scenario())


def test_stop_cancels_pending_planner_and_cannot_resume_movement(tmp_path):
    async def scenario():
        bridge, model = MovingBridge(), PlanningModel()
        model.prefetch_gate = asyncio.Event()
        manager, _, _ = manager_for(tmp_path, bridge=bridge, model=model)
        await manager.start(StartRequest(mode="autonomous", decision_interval_seconds=0.2))
        await asyncio.wait_for(model.prefetch_entered.wait(), 1)
        await manager.stop()
        assert model.prefetch_cancelled and manager.planner_task is None
        assert manager.mode == "idle" and len(bridge.commands) == 1
        model.prefetch_gate.set()
        bridge.complete.set()
        await asyncio.sleep(0)
        assert len(bridge.commands) == 1
        assert manager.journal.pending is None  # stopping requires no manual unlock
    asyncio.run(scenario())


def test_goal_change_cancels_planner_and_keeps_budgets(tmp_path):
    async def scenario():
        bridge, model = MovingBridge(), PlanningModel()
        model.prefetch_gate = asyncio.Event()
        manager, _, _ = manager_for(tmp_path, bridge=bridge, model=model)
        await manager.start(StartRequest(spending_mode="budgeted", mode="autonomous", coin_budget=4,
                                         decision_interval_seconds=0.2, decision_budget=3))
        await asyncio.wait_for(model.prefetch_entered.wait(), 1)
        manager.update_goal("不要往右，停下")
        bridge.complete.set()
        await asyncio.wait_for(manager.task, 1)
        assert model.prefetch_cancelled and len(bridge.commands) == 1
        assert manager.coin_budget == 4 and manager.decisions_remaining == 0
    asyncio.run(scenario())


def test_current_move_without_position_change_can_continue_ready_next_plan(tmp_path):
    async def scenario():
        bridge = MovingBridge()
        bridge.effect = False
        model = PlanningModel(Decision(operation="move", direction="left", duration_ms=500))
        manager, _, _ = manager_for(tmp_path, bridge=bridge, model=model)
        await manager.start(StartRequest(mode="autonomous", decision_interval_seconds=0.2))
        await asyncio.wait_for(model.prefetch_entered.wait(), 1)
        bridge.complete.set()
        await asyncio.wait_for(manager.task, 1)
        assert len(bridge.commands) == 2 and manager.mode == "idle"
        assert manager.journal.pending is None and manager.planner_task is None
    asyncio.run(scenario())


def test_new_threat_discards_previous_motion_plan(tmp_path):
    async def scenario():
        bridge = MovingBridge()
        model = PlanningModel(Decision(operation="move", direction="right", duration_ms=500))
        manager, _, _ = manager_for(tmp_path, bridge=bridge, model=model)
        await manager.start(StartRequest(mode="autonomous", decision_interval_seconds=0.2,
                                         decision_budget=3))
        await asyncio.wait_for(model.prefetch_entered.wait(), 1)
        bridge.data["world"]["nearby_enemies"] = [{"x": 1, "name": "new threat"}]
        bridge.complete.set()
        await asyncio.wait_for(manager.task, 1)
        assert len(bridge.commands) == 1 and model.calls == 3
    asyncio.run(scenario())


@pytest.mark.parametrize("modern,ready,allowed", [(True, True, True), (True, False, True),
                                                 (False, True, True), (True, True, False)])
def test_sprint_and_long_duration_require_capability_and_native_readiness(tmp_path, modern,
                                                                         ready, allowed):
    async def scenario():
        bridge, model = FakeBridge(), FakeModel(Decision(operation="move", direction="right",
                                                        duration_ms=5000, sprint=True))
        if modern:
            bridge.data["capabilities"] += ["sprint", "move_long"]
        bridge.data["players"][1]["can_sprint"] = ready
        manager, _, _ = manager_for(tmp_path, bridge=bridge, model=model)
        await manager.start(StartRequest(mode="autonomous", allow_sprint=allowed,
                                         decision_interval_seconds=0.2, decision_budget=1))
        await asyncio.wait_for(manager.task, 1)
        assert bridge.commands[0]["duration_ms"] == (5000 if modern else 1000)
        assert bridge.commands[0]["sprint"] is (modern and ready and allowed)
    asyncio.run(scenario())
