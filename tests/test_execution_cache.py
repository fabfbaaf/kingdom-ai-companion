"""Provider routing, prefix reuse and local execution without real services."""

import asyncio
import copy
import json

import httpx
import pytest
from pydantic import ValidationError
from test_companion import FakeBridge, state_data, store_for
from test_territory_payload import observation

from companion.app import create_app
from companion.config import ExecutionSettings, ModelSettings
from companion.contracts import GameState, StartRequest
from companion.controller import ActionJournal, ControlManager
from companion.model import ModelClient, ModelError
from companion.model_payload import model_messages, model_packet


def local_response(content='{"operation":"stop"}', **extra):
    return httpx.Response(200, json={"done": True, "done_reason": "stop", "message": {"content": content}, **extra})


def configured(tmp_path):
    store = store_for(tmp_path)
    store.update(ModelSettings(endpoint="https://cloud.invalid/v1", model="cloud-fixture", api_key="private-cloud-key"))
    store.update_execution(ExecutionSettings(provider="ollama", model="local:4b"))
    return store


def test_config_migration_and_independent_edits_preserve_protected_key(tmp_path):
    store = store_for(tmp_path)
    store.update(ModelSettings(model="main", api_key="private-cloud-key"))
    data = json.loads(store.path.read_text())
    data.pop("execution")  # A config saved before this optional feature.
    store.path.write_text(json.dumps(data))
    store = store_for(tmp_path)
    assert store.settings.execution.provider == "main"
    store.update_execution(ExecutionSettings(provider="ollama", model="local:4b", endpoint="http://localhost:11434/v1/"))
    store.update(ModelSettings(model="main-edit", api_key=None))
    restored = store_for(tmp_path)
    assert restored.settings.execution.endpoint == "http://localhost:11434"
    assert restored.settings.execution.model == "local:4b"
    assert restored.settings.api_key == "private-cloud-key"
    assert "private-cloud-key" not in json.dumps(restored.public())
    restored.update_execution(ExecutionSettings(provider="main", model="local:4b"))
    assert restored.settings.model == "main-edit" and restored.settings.api_key == "private-cloud-key"


@pytest.mark.parametrize("settings", [
    {"provider": "ollama"}, {"provider": "other"}, {"endpoint": "http://user:pass@localhost"},
    {"endpoint": "http://localhost?key=private"}, {"endpoint": "file:///config"},
    {"context_tokens": "8192"}, {"timeout_seconds": 0},
])
def test_execution_invalid_settings_rejected(settings):
    with pytest.raises(ValidationError):
        ExecutionSettings(**settings)


def test_parallel_chat_keeps_main_provider_and_execution_uses_native_ollama(tmp_path):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        requests = []

        async def handler(request):
            requests.append(request)
            payload = json.loads(request.content)
            if request.url.host == "127.0.0.1":
                assert request.url.path == "/api/chat"
                assert "authorization" not in request.headers
                assert "private-cloud-key" not in request.content.decode()
                assert payload["think"] is False and payload["stream"] is False
                assert payload["truncate"] is False and payload["shift"] is False
                assert payload["keep_alive"] == "5m" and payload["options"]["num_ctx"] == 8192
                assert payload["model"] == "local:4b"
                variants = {item["properties"]["operation"]["const"]: item for item in payload["format"]["anyOf"]}
                assert "sprint" not in variants["stop"]["properties"]
                assert "sprint" in variants["move"]["properties"]
                assert len(variants) == 9
                entered.set()
                await release.wait()
                return local_response(prompt_eval_count=100, eval_count=4, prompt_eval_cached_count=80)
            assert request.headers["authorization"] == "Bearer private-cloud-key"
            assert payload["model"] == "cloud-fixture"
            return httpx.Response(200, json={"usage": {"prompt_tokens": 1000, "completion_tokens": 20,
                "prompt_cache_hit_tokens": 700, "prompt_cache_miss_tokens": 300},
                "choices": [{"message": {"content": '{"reply":"我在，继续玩吧。","intent":"chat"}'}}]})

        model = ModelClient(configured(tmp_path), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        pending = asyncio.create_task(model.decide(observation(), goal="守家", coin_budget=None))
        await entered.wait()
        reply = await asyncio.wait_for(model.chat("聊聊天", [], observation(), {}), .5)
        assert reply.intent == "chat" and not pending.done()
        release.set()
        assert (await pending).operation == "stop"
        routes = model.usage_status()["routes"]
        assert len(routes) == 2
        assert next(item for item in routes if item["provider"] == "ollama")["cache_hit_ratio"] == .8
        assert next(item for item in routes if item["provider"] == "main")["cache_hit_ratio"] == .7
        assert len(requests) == 2
        await model.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("response, expected", [
    (httpx.Response(404), "不存在"), (httpx.Response(400, text="private-cloud-key"), "拒绝"),
    (httpx.Response(500, text="private-cloud-key"), "HTTP 500"),
    (local_response(done_reason="length"), "截断"), (local_response(done=False), "正常完成"),
    (local_response(content="not-json"), "格式无效"),
    (local_response(content='{"operation":"stop","sprint":false}'), "格式无效"),
    (httpx.Response(200, json={"done": True, "message": None}), "格式无效"),
])
def test_ollama_failure_never_falls_back_to_cloud_or_executes_partial_output(tmp_path, response, expected):
    async def scenario():
        calls = []
        def handler(request):
            calls.append(request)
            return response
        model = ModelClient(configured(tmp_path), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with pytest.raises(ModelError, match=expected) as error:
            await model.test_execution()
        assert "private-cloud-key" not in str(error.value)
        assert len(calls) == 1 and calls[0].url.host == "127.0.0.1"
        await model.close()
    asyncio.run(scenario())


def test_main_connection_test_and_local_test_are_independent(tmp_path):
    async def scenario():
        paths = []
        def handler(request):
            paths.append(request.url.path)
            if request.url.path == "/api/chat":
                return local_response(prompt_eval_count=50, eval_count=5)
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"operation":"stop"}'}}]})
        model = ModelClient(configured(tmp_path), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        await model.test()
        await model.test_execution()
        assert paths == ["/v1/chat/completions", "/api/chat"]
        assert model.usage_status()["last"]["cache_hit_tokens"] is None
        assert model.usage_status()["routes"][-1]["cache_hit_ratio"] is None
        await model.close()
    asyncio.run(scenario())


def test_authenticated_discovery_save_and_test_do_not_start_game_or_microphone(tmp_path):
    async def scenario():
        requests = []
        def handler(request):
            requests.append((request.method, request.url.path))
            if request.method == "GET":
                return httpx.Response(200, json={"models": [{"name": "local:4b", "details": {
                    "parameter_size": "4.0B", "quantization_level": "Q4_K_M"}}]})
            return local_response()
        store, bridge = store_for(tmp_path), FakeBridge()
        model = ModelClient(store, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        app = create_app(store=store, bridge=bridge, model=model, journal=ActionJournal(tmp_path / "journal"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:48860") as client:
            for path in ("/api/execution", "/api/execution/models", "/api/execution/test"):
                assert (await client.post(path)).status_code in (403, 405)
            assert not requests
            token = (await client.get("/api/session")).json()["token"]
            headers = {"x-kingdom-session": token}
            result = await client.post("/api/execution/models", headers=headers, json={"endpoint": "http://localhost:11434/v1"})
            assert result.status_code == 200 and result.json()["models"][0]["quantization"] == "Q4_K_M"
            invalid = await client.post("/api/execution/models", headers=headers, json={"endpoint": "http://user:pass@localhost"})
            assert invalid.status_code == 422
            saved = await client.put("/api/execution", headers=headers, json={"provider": "ollama", "model": "local:4b"})
            assert saved.status_code == 200 and saved.json()["execution_configured"] is True
            assert saved.json()["configured"] is False  # Local play does not require a chat model.
            assert (await client.post("/api/execution/test", headers=headers)).status_code == 200
            assert app.state.manager.status()["running"] is False
            assert bridge.commands == [] and app.state.voice.status()["state"] != "listening"
            assert requests == [("GET", "/api/tags"), ("POST", "/api/chat")]
        await app.state.manager.close()
        await model.close()
    asyncio.run(scenario())


def test_stop_cancels_pending_local_execution_and_preserves_no_game_input(tmp_path):
    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def handler(_request):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        store, bridge = configured(tmp_path), FakeBridge()
        model = ModelClient(store, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        manager = ControlManager(bridge, model, store, ActionJournal(tmp_path / "journal"))
        await manager.start(StartRequest(mode="autonomous", continuous=True))
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(manager.stop(), 1)
        assert cancelled.is_set() and bridge.commands == [] and bridge.stops > 0
        assert not manager.status()["running"]
        await manager.close()
        await model.close()
    asyncio.run(scenario())


def test_switch_is_rejected_during_active_play(tmp_path):
    async def scenario():
        gate = asyncio.Event()
        async def handler(_request):
            await gate.wait()
            return local_response()
        store, bridge = configured(tmp_path), FakeBridge()
        model = ModelClient(store, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        app = create_app(store=store, bridge=bridge, model=model, journal=ActionJournal(tmp_path / "journal"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:48860") as client:
            headers = {"x-kingdom-session": (await client.get("/api/session")).json()["token"]}
            await app.state.manager.start(StartRequest(mode="autonomous", continuous=True))
            assert (await client.put("/api/execution", headers=headers, json={"provider": "main"})).status_code == 409
            assert (await client.post("/api/execution/test", headers=headers)).status_code == 409
            assert store.settings.execution.provider == "ollama"
        await app.state.manager.close()
        await model.close()
    asyncio.run(scenario())


def test_large_current_catalog_has_stable_message_and_lossless_tables():
    data = state_data()
    data["world"]["targets"] = [{"target_id": f"wall-{i:03}", "kind": "upgrade", "name": "Wall1", "x": i * 2.5,
        "price": 3, "can_pay": False, "currency": "coins", "needs_work": False} for i in range(200)]
    packet = {"state": GameState.model_validate(data).model_dump(mode="json"), "goal": "守营地"}
    original = copy.deepcopy(packet)
    first = model_messages("fixed protocol", packet)
    catalog = json.loads(first[1]["content"])["world_catalog"]["targets"]
    table = json.loads(first[-1]["content"])["state"]["world"]["targets"]
    assert len(catalog["rows"]) == len(table["rows"]) == 200
    records = [dict(zip(table["columns"], row)) for row in table["rows"]]
    assert records[137]["target_id"] == "wall-137" and records[137]["x"] == 342.5
    assert records[137]["can_pay"] is False and records[137]["needs_work"] is False
    assert packet == original
    packet["state"]["players"][1]["x"] = 999
    packet["state"]["observation_seq"] += 1
    packet["state"]["captured_at"] = "2026-10-04T13:30:00Z"
    packet["state"]["world"]["targets"].reverse()
    second = model_messages("fixed protocol", packet)
    assert first[:2] == second[:2] and first[-1] != second[-1]
    assert sum(len(item["content"]) for item in first) < len(json.dumps(original, ensure_ascii=False)) * .65


def test_duplicate_observation_history_is_removed_but_different_and_old_facts_remain():
    record = {"kind": "upgrade", "name": "Wall1", "x": 10.0, "price": 3}
    history = [{**record, "last_seen_at": "now", "present_in_last_observation": True},
               {**record, "price": 5, "last_seen_at": "old", "present_in_last_observation": True},
               {**record, "last_seen_at": "old", "present_in_last_observation": False}]
    packet = {"state": {"world": {"targets": [{**record, "target_id": "wall"}]}},
              "context": {"memory": {"current_island": {"land": 1, "known_objects": history},
                  "islands": [{"land": 1, "landmarks": history}], "last_plan": {"stage": "defense"}},
                  "plan": {"stage": "defense"}}}
    original = copy.deepcopy(packet)
    result = model_packet(packet)
    memory = result["context"]["memory"]
    assert memory["current_island"]["known_objects"] == history[1:]
    assert memory["islands"][0]["landmarks"] == history[1:]
    assert "last_plan" not in memory and packet == original


def test_strategic_facts_precede_fast_values_but_all_fast_values_are_current():
    state = observation().model_dump(mode="json")
    before = model_messages("protocol", {"goal": "守家", "state": state})
    state["captured_at"] = "2026-10-04T15:30:00Z"
    state["observation_seq"] += 1
    state["players"][1]["x"] = 123
    after = model_messages("protocol", {"goal": "守家", "state": state})
    for text in (before[-1]["content"], after[-1]["content"]):
        assert text.index('"world"') < text.index('"observation_seq"')
    assert before[-1]["content"].split('"observation_seq"')[0] == after[-1]["content"].split('"observation_seq"')[0]
    current = json.loads(after[-1]["content"])["state"]
    assert current["players"][1]["x"] == 123 and current["captured_at"] == state["captured_at"]
    assert before != after
