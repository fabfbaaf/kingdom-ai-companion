"""Campaign memory and stage evidence tests using only synthetic observations."""

import asyncio
import json

import pytest
from pydantic import ValidationError
from test_companion import manager_for, state_data

from companion.contracts import CAMPAIGN_GOAL, Decision, GameState
from companion.gameplay_context import safe_context
from companion.memory import GameplayMemory
from companion.planning import PlanProposal, StagePlanner


def observation(*, started=101, land=0, coins=8, session="session-1", night=False):
    data = state_data()
    data["session_id"] = session
    data["players"][1]["coins"] = coins
    data["world"] = {"is_night": night, "day": 5, "campaign": {
        "started_at": started, "theme": "classic", "biome_index": 0, "challenge_id": 0,
        "land": land, "secured_islands": [False, False], "completed": False},
        "units": [{"kind": "workers"}],
        "structures": [{"kind": "wall", "name": "wall", "x": 10, "fully_repaired": True}],
        "targets": [], "quests": [], "dropped_items": []}
    return GameState.model_validate(data)


def proposal():
    return PlanProposal(stage="economy", objective="先获得金币", criteria=[{
        "metric": "coins", "comparison": "gte", "value": 10, "description": "余额达到十枚"}])


def test_memory_survives_restart_and_island_changes_without_mixing_saves(tmp_path):
    async def scenario():
        path = tmp_path / "memory.local.json"
        memory = GameplayMemory(path)
        memory.observe(observation())
        memory.set_goal("发展经济")
        memory.record_result({"operation": "pay", "status": "completed", "target_id": "wall-1", "api_key": "secret"})
        memory.record_dialogue("private full conversation", "reply")
        memory.observe(observation(land=1, session="new-scene", coins=3))
        assert len(memory.public()["islands"]) == 2
        await memory.flush(force=True)
        content = path.read_text(encoding="utf-8")
        assert "secret" not in content and "private full conversation" not in content
        restored = GameplayMemory(path)
        restored.observe(observation(land=0, session="after-restart"))
        assert len(restored.public()["islands"]) == 2
        assert restored.public()["recent_goals"][0]["goal"] == "发展经济"
        assert restored.public()["action_history"][0]["target_id"] == "wall-1"
        restored.observe(observation(started=202, land=0))
        assert len(restored.public()["islands"]) == 1 and restored.public()["action_history"] == []
    asyncio.run(scenario())


def test_objects_are_historical_and_missing_observation_is_not_destruction(tmp_path):
    memory = GameplayMemory(tmp_path / "memory.local.json")
    memory.observe(observation())
    state = observation()
    state.world["structures"] = []
    memory.observe(state, force=True)
    object_ = memory.public()["current_island"]["known_objects"][0]
    assert object_["fully_repaired"] is True and object_["present_in_last_observation"] is False
    assert object_["last_seen_at"]


def test_unknown_campaign_identity_is_not_persisted(tmp_path):
    async def scenario():
        path = tmp_path / "memory.local.json"
        memory = GameplayMemory(path)
        memory.observe(observation(started=None))
        assert memory.public()["identity_verified"] is False
        await memory.flush(force=True)
        assert json.loads(path.read_text(encoding="utf-8"))["campaigns"] == {}
    asyncio.run(scenario())


def test_corrupt_memory_does_not_block_new_observations(tmp_path):
    path = tmp_path / "memory.local.json"
    path.write_text('{"schema":1,"campaigns":{"bad":{"identity_verified":true,"islands":[]}}}', encoding="utf-8")
    memory = GameplayMemory(path)
    memory.observe(observation())
    assert len(memory.public()["islands"]) == 1


def test_plan_completion_uses_observations_and_failure_requires_replanning():
    planner = StagePlanner()
    planner.propose(proposal(), observation(coins=8))
    planner.record_result({"operation": "pay", "status": "completed"})
    assert planner.status == "in_progress"
    planner.record_result({"status": "failed"})
    planner.record_result({"status": "unknown"})
    assert planner.public()["failures"] == 2 and "重新选" in planner.public()["feedback"]
    planner.observe(observation(coins=10))
    assert planner.status == "completed" and len(planner.completed) == 1
    planner.observe(observation(coins=10))
    assert len(planner.completed) == 1


def test_unreadable_metrics_remain_unknown_and_completion_cannot_be_invented():
    planner = StagePlanner()
    state = observation()
    state.world["structures"][0]["fully_repaired"] = None
    plan = PlanProposal(stage="defense", objective="修复城墙", criteria=[{
        "metric": "damaged_walls", "comparison": "eq", "value": 0, "description": "城墙已修复"}])
    planner.propose(plan, state)
    assert planner.status == "waiting_observation" and not planner.completed
    with pytest.raises(ValidationError):
        PlanProposal(stage="complete", objective="通关", criteria=proposal().criteria)


def test_campaign_default_plan_moves_between_economy_defense_attack_and_sail():
    planner = StagePlanner()
    planner.set_goal(CAMPAIGN_GOAL)
    state = observation()
    planner.observe(state)
    assert planner.public()["stage"] == "economy"
    state.world["is_night"] = True
    planner.observe(state)
    assert planner.public()["stage"] == "defense"
    state.world["is_night"] = False
    state.world["units"].append({"kind": "farmers"})
    state.world["structures"].append({"kind": "portal"})
    planner.observe(state)
    assert planner.public()["stage"] == "attack"
    state.world["campaign"]["secured_islands"][0] = True
    planner.observe(state)
    assert planner.public()["stage"] == "sail"
    planner.observe(observation(land=1))
    assert planner.public()["stage"] == "economy" and any(item["stage"] == "sail" for item in planner.completed)


def test_reserve_preference_does_not_replace_stage_and_tonight_requires_observed_night():
    for goal, stage in (("先发展经济", "economy"), ("今晚守右边", "defense"), ("组织进攻", "attack")):
        planner = StagePlanner()
        planner.set_goal(goal + "；玩家安排：钱留着修船")
        planner.observe(observation())
        assert planner.public()["stage"] == stage
    planner = StagePlanner()
    planner.set_goal("今晚守右边")
    planner.observe(observation(night=False))
    assert planner.status == "in_progress" and not planner.completed
    planner.observe(observation(night=True))
    assert planner.status == "in_progress" and not planner.completed
    planner.observe(observation(night=False))
    assert planner.status == "completed" and len(planner.completed) == 1


def test_advisory_plan_never_enters_native_command_body():
    decision = Decision(operation="move", direction="right", duration_ms=1000, plan=proposal())
    assert decision.plan and "plan" not in decision.model_dump(mode="json")
    with pytest.raises(ValidationError):
        Decision(operation="stop", plan={"stage": "economy", "objective": "bad", "criteria": [{
            "metric": "coins", "value": float("nan"), "description": "invalid"}]})


def test_shared_context_strips_credentials_recursively():
    value = safe_context({"memory": {"campaign": {"land": 1, "api_key": "private"},
        "current_island": {"resources": {"coins": 8, "token": "private"}}},
        "plan": {"stage": "economy", "api_key": "private"}})
    assert "private" not in json.dumps(value)
    assert value["memory"]["campaign"]["land"] == 1


def test_chat_and_action_context_share_campaign_facts(tmp_path):
    async def scenario():
        manager, bridge, _ = manager_for(tmp_path)
        bridge.data = observation().model_dump(mode="json")
        await manager._state()
        context = manager.gameplay_context()
        assert context["memory"]["campaign"]["land"] == 0
        assert context["plan"]["criteria"]
        manager.mode = "autonomous"
        manager.task = asyncio.create_task(asyncio.sleep(10))
        await manager.change_goal("今晚守右边")
        assert manager.memory.planner.goal == "今晚守右边"
        manager.task.cancel()
        await manager.close()
    asyncio.run(scenario())


def test_stage_plan_restores_only_when_same_goal_is_explicitly_selected(tmp_path):
    async def scenario():
        path = tmp_path / "memory.local.json"
        memory = GameplayMemory(path)
        state = observation()
        memory.observe(state)
        memory.set_goal("玩家经济计划")
        memory.planner.propose(proposal(), state)
        await memory.flush(force=True)
        restored = GameplayMemory(path)
        restored.observe(observation(session="new-session"))
        assert restored.planner.goal == ""
        restored.set_goal("玩家经济计划")
        restored.planner.observe(state)
        assert restored.planner.plan.objective == "先获得金币"
        assert restored.planner.status == "in_progress"
    asyncio.run(scenario())
