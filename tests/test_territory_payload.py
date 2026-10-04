"""Synthetic territory recognition, compact model packets and provider usage."""

import asyncio
import copy
import json

import httpx
import pytest
from test_companion import state_data

from companion.config import ModelConfigStore, ModelSettings
from companion.contracts import GameState
from companion.model import ModelClient
from companion.model_payload import model_json, model_packet
from companion.territory import annotated_state, territory_context


def observation():
    data = state_data()
    data["players"][0]["x"] = 999
    data["players"][1]["x"] = -10
    targets = [{"target_id": str(i), "entity_id": str(i), "kind": "upgrade", "type": "PayableUpgrade",
                "name": "Tower0", "x": x, "price": 3, "currency": "coins", "can_pay": True,
                "health": None, "level": None, "next_building": "Tower1"}
               for i, x in enumerate((-100, -11, 0, 10, 20, 31, 200))]
    data["world"] = {"environment": {"borders": {"left": 0, "right": 30},
                    "intact_borders": {"left": 5, "right": 25}, "world_bounds": {"left": -240, "right": 240}},
                    "structures": [{"kind": "castle", "name": "Castle", "x": 10}],
                    "campaign": {"land": 0}, "targets": targets, "nearby_payables": targets,
                    "enemies": [], "nearby_enemies": []}
    return GameState.model_validate(data)


def test_home_comes_from_native_borders_and_castle_not_players():
    state = observation()
    context = territory_context(state)
    assert context["center_x"] == 10
    assert context["camp_bounds"] == {"left": 0, "right": 30}
    assert context["p2_region"] == "outside_camp_left"
    assert context["p2_distance_from_camp"] == 10
    assert context["camp_building_count"] == 3 and context["outside_building_count"] == 4
    assert [item["x"] for item in context["frontier_candidates"]] == [-11, 31]
    assert context["advisory"] is True


@pytest.mark.parametrize("bad", [None, {}, {"left": 0, "right": 0}, {"left": 30, "right": 0},
    {"left": float("nan"), "right": 30}, {"left": False, "right": 30}, {"left": "0", "right": 30}])
def test_bad_borders_never_invent_home_radius(bad):
    state = observation()
    state.world["environment"]["borders"] = bad
    state.world["environment"]["intact_borders"] = None
    context = territory_context(state)
    assert context["camp_bounds"] is None and context["bounds_status"] == "unknown"
    assert context["center_x"] == 10 and context["p2_region"] == "unknown"
    assert context["frontier_candidates"] == []


def test_inconsistent_home_or_defenses_are_unknown():
    state = observation()
    state.world["environment"]["borders"] = {"left": 100, "right": 200}
    context = territory_context(state)
    assert context["camp_bounds"] is None and "camp_bounds_exclude_castle" in context["issues"]
    state = observation()
    state.world["environment"]["intact_borders"] = {"left": -5, "right": 40}
    assert territory_context(state)["intact_bounds"] is None


def test_annotations_keep_all_targets_and_do_not_mutate_bridge_facts():
    state = observation()
    before = copy.deepcopy(state.world)
    packet = annotated_state(state, territory_context(state))
    targets = packet["world"]["targets"]
    assert len(targets) == 7
    assert targets[0]["purpose"] == "expansion" and targets[0]["distance_from_camp"] == 100
    assert targets[2]["region"] == "inside_camp" and targets[2]["purpose"] == "camp_construction"
    assert targets[3]["region"] == "inside_defenses"
    assert state.world == before


def test_model_packet_deduplicates_and_preserves_native_dynamic_values():
    state = observation()
    packet = {"goal": "发展营地", "remaining_coin_budget": None, "state": annotated_state(state, territory_context(state))}
    before = copy.deepcopy(packet)
    compact = model_packet(packet)
    assert next(iter(compact)) == "world_catalog"
    assert "nearby_payables" not in compact["state"]["world"]
    assert "nearby_enemies" not in compact["state"]["world"]
    assert "diagnostics" not in compact["state"]
    assert compact["remaining_coin_budget"] is None  # Unlimited remains explicit.
    current = compact["state"]["world"]["targets"]
    catalog = {item["target_id"]: item for item in compact["world_catalog"]["targets"]}
    assert len(current) == len(catalog) == 7
    assert current[0]["x"] == -100 and current[0]["can_pay"] is True
    assert catalog[current[0]["target_id"]]["next_building"] == "Tower1"
    assert packet == before
    assert len(model_json(packet)) < len(json.dumps(packet, ensure_ascii=False)) * .7


def test_cache_prefix_survives_timestamp_position_goal_and_registry_order_changes():
    state = observation()
    first = model_json({"state": annotated_state(state, territory_context(state)), "goal": "建营地"})
    state.observation_seq += 1
    state.players[1].x = 99
    state.world["targets"].reverse()
    second = model_json({"state": annotated_state(state, territory_context(state)), "goal": "守家"})
    first_prefix = first[:first.index(',"state":')]
    second_prefix = second[:second.index(',"state":')]
    assert first_prefix == second_prefix
    assert first != second  # Current facts are never substituted with a cached decision.
    state.world["targets"][0]["name"] = "NewTower"
    assert model_packet({"state": state.model_dump(mode="json")})["world_catalog"]["targets"][-1]["name"] == "NewTower"


def test_legacy_nearby_only_and_unknown_chat_state_remain_available():
    state = observation()
    del state.world["targets"]
    packet = model_packet({"state": state.model_dump(mode="json")})
    assert len(packet["state"]["world"]["nearby_payables"]) == 7
    assert model_packet({"state": None, "user_text": "你好"})["state"] is None


def test_model_uses_same_territory_for_chat_and_decisions_and_records_usage(tmp_path):
    async def scenario():
        store = ModelConfigStore(tmp_path / "model.local.json", protect=lambda x: x, unprotect=lambda x: x)
        store.update(ModelSettings(endpoint="https://example.invalid/v1", model="fixture", api_key="fixture-key"))
        packets = []

        def handler(request):
            data = json.loads(request.content)
            packet = json.loads(data["messages"][-1]["content"])
            packets.append(packet)
            assert packet["context"]["territory"]["camp_bounds"] == {"left": 0, "right": 30}
            assert packet["state"]["world"]["targets"][0]["purpose"] == "expansion"
            content = '{"reply":"先发展营地内。","intent":"chat"}' if "user_text" in packet else '{"operation":"stop"}'
            return httpx.Response(200, json={"usage": {"prompt_tokens": 1000, "completion_tokens": 30,
                "prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 200},
                "choices": [{"finish_reason": "stop", "message": {"content": content}}]})

        model = ModelClient(store, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        await model.decide(observation(), goal="建营地", coin_budget=None)
        await model.chat("目前的家在哪", [], observation(), {"context": {"api_key": "must-not-leak"}})
        usage = model.usage_status()
        assert usage["reported_requests"] == 2 and usage["cache_hit_ratio"] == .8
        assert usage["prompt_tokens"] == 2000 and usage["completion_tokens"] == 60
        assert "fixture-key" not in json.dumps(packets) and "must-not-leak" not in json.dumps(packets)
        await model.close()

    asyncio.run(scenario())


def test_missing_provider_usage_is_unknown_not_zero_cache_hits(tmp_path):
    store = ModelConfigStore(tmp_path / "model.local.json")
    model = ModelClient(store)
    model._record_usage(None, 300)
    assert model.usage_status()["last"]["cache_hit_tokens"] is None
    assert model.usage_status()["cache_hit_ratio"] is None
    model._record_usage({"prompt_tokens": 100, "completion_tokens": 5,
                         "prompt_tokens_details": {"cached_tokens": 80}}, 300)
    assert model.usage_status()["last"]["cache_miss_tokens"] == 20
    assert model.usage_status()["cache_hit_ratio"] == .8
    asyncio.run(model.close())
