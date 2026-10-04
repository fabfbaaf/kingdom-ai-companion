"""Offline contract, cancellation and spending tests. No game or provider calls."""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from companion.app import create_app
from companion.bridge import BridgeClient, BridgeError
from companion.config import ModelConfigStore, ModelSettings, _dpapi
from companion.contracts import Decision, GameState, Receipt, StartRequest
from companion.controller import ActionJournal, ControlError, ControlLease, ControlManager, playable
from companion.game_installation import launch_game
from companion.installation_store import InstallationStore
from companion.model import ModelClient, ModelError


def state_data():
    return {"bridge_version": "0.1.0", "game_version": "2.4.2", "session_id": "test-session",
            "observation_seq": 1, "captured_at": datetime.now(UTC).isoformat(), "scene": "Island",
            "control_epoch": 0,
            "ready": True, "reason": None, "coop": True, "controlled_player_id": 1,
            "input_released": True, "capabilities": ["move", "stop", "pay", "pay_coin"],
            "players": [{"player_id": 0, "x": 10.0, "coins": 15, "stamina": None, "crown": True,
                         "transaction_pending": False, "current_payable": None},
                        {"player_id": 1, "x": 0.0, "coins": 8, "stamina": 0.5, "crown": True,
                         "transaction_pending": False, "current_payable": {
                             "target_id": "wall-1", "name": "城墙", "x": 0.0,
                             "price": 3, "currency": "coins", "can_pay": True}}],
            "world": {"night": False}, "diagnostics": []}


class FakeBridge:
    def __init__(self):
        self.data = state_data()
        self.commands = []
        self.stops = 0
        self.heartbeats = 0
        self.heartbeat_leases = []
        self.effect = True
        self.unknown = False
        self.change_session = False
        self.heartbeat_fail = False
        self.settled = True
        self.closed = False
        self.state_gate = None
        self.config_path = None
        self.disconnected = False
        self.coop_requests = 0

    async def state(self):
        if self.disconnected:
            raise BridgeError("fake game disconnected")
        if self.state_gate:
            await self.state_gate.wait()
        self.data["captured_at"] = datetime.now(UTC).isoformat()
        self.data["observation_seq"] += 1
        return GameState.model_validate(copy.deepcopy(self.data))

    async def heartbeat(self, *, control_id, session_id, control_epoch):
        self.heartbeats += 1
        uuid.UUID(control_id)
        self.heartbeat_leases.append({"control_id": control_id, "session_id": session_id,
                                      "control_epoch": control_epoch})
        if self.heartbeat_fail:
            raise BridgeError("heartbeat disconnected")
        if session_id != self.data["session_id"] or control_epoch != self.data["control_epoch"]:
            raise BridgeError("lease revoked")

    async def command(self, body):
        self.commands.append(copy.deepcopy(body))
        uuid.UUID(body["control_id"])
        assert body["control_epoch"] == self.data["control_epoch"]
        player = self.data["players"][1]
        if self.unknown:
            raise BridgeError("receipt lost")
        if self.effect and body["operation"] == "move":
            player["x"] += 1 if body["direction"] == "right" else -1
        if self.effect and body["operation"] in {"pay", "pay_coin"}:
            player["coins"] -= body["max_coins"]
            player["transaction_pending"] = not self.settled
        if self.change_session:
            self.data["session_id"] = "other-session"
        return Receipt(action_id=body["action_id"], session_id=body["session_id"], status="completed")

    async def receipt(self, _action_id):
        raise AssertionError("completed fake must not poll")

    async def stop(self):
        self.stops += 1
        self.data["control_epoch"] += 1
        self.data["input_released"] = True

    async def open_coop(self):
        assert self.stops > 0
        self.coop_requests += 1
        return {"status": "queued"}

    async def close(self):
        self.closed = True


class FakeModel:
    def __init__(self, decision=None):
        self.calls = 0
        self.decision = decision or Decision(operation="stop")
        self.entered = asyncio.Event()
        self.gate = None

    async def decide(self, _state, **_kwargs):
        self.calls += 1
        self.entered.set()
        if self.gate:
            await self.gate.wait()
        return self.decision

    async def test(self):
        self.calls += 1

    async def close(self):
        pass


def store_for(tmp_path):
    # Synthetic cipher allows portable tests without reading a user's Windows key store.
    return ModelConfigStore(tmp_path / "model.local.json",
                            protect=lambda data: b"test-cipher:" + data,
                            unprotect=lambda data: data.removeprefix(b"test-cipher:"))


def manager_for(tmp_path, *, bridge=None, model=None, configured=True):
    store = store_for(tmp_path)
    if configured:
        store.update(ModelSettings(endpoint="http://127.0.0.1:9999/v1", model="mock-model"))
    bridge = bridge or FakeBridge()
    model = model or FakeModel()
    manager = ControlManager(bridge, model, store, ActionJournal(tmp_path / "session.local.json"))
    return manager, bridge, model


@pytest.mark.parametrize("data", [
    {"operation": "move", "direction": "right", "duration_ms": 99},
    {"operation": "move", "direction": "right", "duration_ms": True},
    {"operation": "move", "direction": "up", "duration_ms": 500},
    {"operation": "pay", "target_id": "x", "shell": "anything"},
    {"operation": "stop", "direction": "left"},
    {"operation": "teleport", "x": 100},
])
def test_invalid_model_commands_rejected(data):
    with pytest.raises(ValidationError):
        Decision.model_validate(data)


@pytest.mark.parametrize("field,value", [("ready", False), ("coop", False),
                                         ("controlled_player_id", 0)])
def test_p2_only_ready_contract(field, value):
    data = state_data()
    data[field] = value
    with pytest.raises(ControlError):
        playable(GameState.model_validate(data), "move")


def test_stale_or_naive_states_rejected():
    data = state_data()
    data["captured_at"] = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    with pytest.raises(ControlError):
        playable(GameState.model_validate(data))
    data["captured_at"] = datetime.now(UTC).replace(tzinfo=None).isoformat()
    with pytest.raises(ValidationError):
        GameState.model_validate(data)


def test_secret_is_not_public_and_preserved_on_empty_edit(tmp_path):
    store = store_for(tmp_path)
    secret = "synthetic-only-key"
    store.update(ModelSettings(model="mock", api_key=secret))
    assert secret not in json.dumps(store.public())
    assert "api_key" not in store.settings.model_dump()
    store.update(ModelSettings(model="mock-2", api_key=None))
    assert store.settings.api_key == secret
    restored = store_for(tmp_path)
    assert restored.settings.api_key == secret
    store.update(ModelSettings(model="mock-2", api_key=""))
    assert not store.public()["key_present"]


def test_windows_dpapi_roundtrip():
    import os
    if os.name != "nt":
        pytest.skip("Windows only")
    value = b"synthetic-test-value"
    encrypted = _dpapi(value, decrypt=False)
    assert value not in encrypted
    assert _dpapi(encrypted, decrypt=True) == value


def test_unknown_action_does_not_require_review_to_restart(tmp_path):
    async def scenario():
        manager, bridge, _model = manager_for(tmp_path)
        bridge.unknown = True
        result = await manager.manual(Decision(operation="move", direction="right", duration_ms=500))
        assert result["status"] == "unknown"
        assert manager.last_result["status"] == "unknown"
        assert not manager.status()["needs_review"]
        restarted, _bridge, _model = manager_for(tmp_path)
        await restarted.start(StartRequest(mode="follow"))
        await restarted.stop()
        assert len(bridge.commands) == 1
        assert bridge.stops >= 1
        await manager.close()
    asyncio.run(scenario())


def test_move_returns_input_completion_and_p1_is_untouched(tmp_path):
    async def scenario():
        manager, bridge, _model = manager_for(tmp_path)
        result = await manager.manual(Decision(operation="move", direction="right", duration_ms=500))
        assert result["status"] == "completed"
        assert bridge.commands[0]["player_id"] == 1
        assert bridge.data["players"][0]["x"] == 10
        assert not manager.status()["needs_review"]
        assert bridge.stops >= 1
        await manager.close()
    asyncio.run(scenario())


def test_completed_input_without_motion_is_feedback_without_review_gate(tmp_path):
    async def scenario():
        bridge = FakeBridge()
        bridge.effect = False
        manager, _bridge, _model = manager_for(tmp_path, bridge=bridge)
        result = await manager.manual(Decision(operation="move", direction="right", duration_ms=500))
        assert result["status"] == "completed"
        assert result["before"]["x"] == result["after"]["x"]
        assert not manager.status()["needs_review"]
        await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("mutate", ["target", "budget", "coins", "currency", "pending", "price"])
def test_payment_preflight_rejects_without_dispatch(tmp_path, mutate):
    async def scenario():
        manager, bridge, _model = manager_for(tmp_path)
        player = bridge.data["players"][1]
        target = player["current_payable"]
        manager.mode, manager.session_id, manager.coin_budget = "autonomous", "test-session", 3
        manager.lease = ControlLease(str(uuid.uuid4()), "test-session", 0)
        decision = Decision(operation="pay", target_id="wall-1", max_coins=3)
        if mutate == "target": decision.target_id = "old-target"
        if mutate == "budget": manager.coin_budget = 2
        if mutate == "coins": player["coins"] = 2
        if mutate == "currency": target["currency"] = "gems"
        if mutate == "pending": player["transaction_pending"] = True
        if mutate == "price": target["price"] = None
        with pytest.raises(ControlError):
            await manager._perform(manager.generation, decision)
        assert bridge.commands == []
        assert manager.journal.pending is None
        await manager.close()
    asyncio.run(scenario())


def test_full_payment_limits_cost_to_observed_price_without_post_action_review(tmp_path):
    async def scenario():
        manager, bridge, _model = manager_for(tmp_path)
        manager.mode, manager.session_id, manager.coin_budget = "autonomous", "test-session", 5
        manager.lease = ControlLease(str(uuid.uuid4()), "test-session", 0)
        result = await manager._perform(manager.generation, Decision(operation="pay", target_id="wall-1", max_coins=20))
        assert result["status"] == "completed"
        assert bridge.commands[0]["max_coins"] == 3
        assert all(value is not None for value in bridge.commands[0].values())
        assert manager.coin_budget == 2
        assert not manager.status()["needs_review"]
        bridge.settled = False
        manager.coin_budget = 3
        result = await manager._perform(manager.generation, Decision(operation="pay", target_id="wall-1", max_coins=3))
        assert result["status"] == "completed"
        assert not manager.status()["needs_review"]
        await manager.close()
    asyncio.run(scenario())


def test_stop_cancels_pending_model_and_prevents_dispatch(tmp_path):
    async def scenario():
        model = FakeModel(Decision(operation="move", direction="right", duration_ms=500))
        model.gate = asyncio.Event()
        manager, bridge, _model = manager_for(tmp_path, model=model)
        await manager.start(StartRequest(mode="autonomous"))
        await asyncio.wait_for(model.entered.wait(), 1)
        await manager.stop()
        model.gate.set()
        await asyncio.sleep(0)
        assert bridge.commands == []
        assert model.calls == 1
        assert manager.mode == "idle"
        assert bridge.stops >= 1
        await manager.close()
    asyncio.run(scenario())


def test_stop_invalidates_start_during_connection_check(tmp_path):
    async def scenario():
        manager, bridge, _model = manager_for(tmp_path)
        bridge.state_gate = asyncio.Event()
        start = asyncio.create_task(manager.start(StartRequest(mode="follow")))
        await asyncio.sleep(0)
        await manager.stop()
        bridge.state_gate.set()
        with pytest.raises(ControlError, match="撤销"):
            await start
        assert bridge.commands == []
        assert manager.task is None
        await manager.close()
    asyncio.run(scenario())


def test_follow_works_without_model_and_stops_on_heartbeat_loss(tmp_path):
    async def scenario():
        manager, bridge, model = manager_for(tmp_path, configured=False)
        await manager.start(StartRequest(mode="follow"))
        for _ in range(100):
            if bridge.commands:
                break
            await asyncio.sleep(0.01)
        assert bridge.commands
        assert model.calls == 0
        bridge.heartbeat_fail = True
        await asyncio.sleep(0.55)
        assert manager.mode == "idle"
        assert "心跳失联" in manager.error
        assert bridge.stops >= 1
        await manager.close()
    asyncio.run(scenario())


def test_session_switch_ends_old_action_without_manual_review(tmp_path):
    async def scenario():
        bridge = FakeBridge()
        bridge.change_session = True
        manager, _bridge, _model = manager_for(tmp_path, bridge=bridge)
        result = await manager.manual(Decision(operation="move", direction="right", duration_ms=500))
        assert result["status"] == "unknown"
        assert not manager.status()["needs_review"]
        await manager.close()
    asyncio.run(scenario())


def test_api_protects_origin_session_and_never_returns_keys(tmp_path):
    async def scenario():
        store = store_for(tmp_path)
        bridge, model = FakeBridge(), FakeModel()
        app = create_app(store=store, bridge=bridge, model=model,
                         journal=ActionJournal(tmp_path / "session.local.json"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:48860") as client:
            assert (await client.get("/api/status")).status_code == 403
            cross = await client.get("/api/session", headers={"origin": "https://evil.invalid"})
            assert cross.status_code == 403
            token = (await client.get("/api/session")).json()["token"]
            headers = {"X-Kingdom-Session": token, "origin": "http://127.0.0.1:48860"}
            bad = await client.post("/api/control/stop", headers={**headers, "origin": "https://evil.invalid"})
            assert bad.status_code == 403 and bridge.stops == 0
            response = await client.put("/api/model", headers=headers,
                                        json={"endpoint":"http://127.0.0.1:9000/v1", "model":"mock", "api_key":"synthetic-secret"})
            assert response.status_code == 200
            assert "synthetic-secret" not in response.text
            response = await client.put("/api/model", headers=headers,
                                        json={"endpoint":"invalid", "api_key":"synthetic-secret"})
            assert response.status_code == 422 and "synthetic-secret" not in response.text
            status = await client.get("/api/status", headers=headers)
            assert not status.json()["control"]["running"]
            assert not status.json()["validation"]["real_game_verified"]
            assert "synthetic-secret" not in status.text
            assert (await client.post("/api/control/stop", headers=headers)).status_code == 200
            assert bridge.stops == 1
            hostile = await client.get("/api/session", headers={"host":"attacker.invalid"})
            assert hostile.status_code == 403
            assert "frame-ancestors 'none'" in (await client.get("/")).headers["content-security-policy"]
        await app.state.manager.close()
    asyncio.run(scenario())


def test_bridge_uses_bearer_only_to_loopback_and_redacts_error(tmp_path):
    async def scenario():
        config = tmp_path / "bridge.local.json"
        config.write_text(json.dumps({"base_url":"http://127.0.0.1:48861", "token":"synthetic_token_1234567890"}))
        def handler(request):
            assert request.headers["authorization"] == "Bearer synthetic_token_1234567890"
            return httpx.Response(401, json={"token":"synthetic_token_1234567890"})
        bridge = BridgeClient(config, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with pytest.raises(BridgeError) as error:
            await bridge.state()
        assert "synthetic_token" not in str(error.value)
        config.write_text(json.dumps({"base_url":"https://example.invalid", "token":"synthetic_token_1234567890"}))
        with pytest.raises(BridgeError, match="配置"):
            await bridge.state()
        await bridge.close()
    asyncio.run(scenario())


def test_model_mock_response_is_validated_and_error_body_redacted(tmp_path):
    async def scenario():
        store = store_for(tmp_path)
        store.update(ModelSettings(endpoint="http://127.0.0.1:9000/v1", model="mock", api_key="synthetic-secret"))
        responses = [httpx.Response(400, text="synthetic-secret"),
                     httpx.Response(200, json={"choices":[{"message":{"content":'{"operation":"move","direction":"right","duration_ms":500,"shell":"bad"}'}}]}),
                     httpx.Response(200, json={"choices":[{"message":{"content":'{"operation":"stop"}'}}]})]
        def handler(request):
            assert request.url.path == "/v1/chat/completions"
            assert request.headers["authorization"] == "Bearer synthetic-secret"
            assert json.loads(request.content)["stream"] is False
            return responses.pop(0)
        model = ModelClient(store, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with pytest.raises(ModelError) as error:
            await model.test()
        assert str(error.value) == "模型请求失败：HTTP 400"
        with pytest.raises(ModelError, match="格式"):
            await model.decide(GameState.model_validate(state_data()), goal="test", coin_budget=0)
        await model.test()
        await model.close()
    asyncio.run(scenario())


def fake_game_directory(tmp_path):
    game = tmp_path / "Kingdom"
    for name in ("KingdomTwoCrowns.exe", "GameAssembly.dll",
                 "KingdomTwoCrowns_Data/il2cpp_data/Metadata/global-metadata.dat"):
        path = game / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic fixture, never executable")
    config = game / "UserData/KingdomAI/bridge.local.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"base_url": "http://127.0.0.1:48861",
                                  "token": "synthetic_token_1234567890"}))
    return game.resolve(), config.resolve()


def test_installation_store_validates_and_restores_only_selected_directory(tmp_path):
    game, _config = fake_game_directory(tmp_path)
    store = InstallationStore(tmp_path / "installation.local.json")
    with pytest.raises((OSError, ValueError)):
        store.select(str(tmp_path))
    assert store.game is None
    assert store.select(str(game)) == game
    restored = InstallationStore(store.path)
    assert restored.public() == {"game_directory": str(game), "bridge_installed": True}
    assert "synthetic_token" not in store.path.read_text()
    assert list(json.loads(store.path.read_text())) == ["game_directory"]


@pytest.mark.parametrize("via_steam", [False, True])
def test_game_launch_is_explicit_authenticated_and_never_enables_ai(tmp_path, monkeypatch, via_steam):
    game, config = fake_game_directory(tmp_path)
    spawned = []
    def fake_spawn(args, **kwargs):
        spawned.append((args, kwargs))
        return SimpleNamespace(args=args)
    monkeypatch.setattr("companion.game_installation.subprocess.Popen", fake_spawn)
    steam = tmp_path / "Steam"
    if via_steam:
        steam.mkdir()
        (steam / "Steam.exe").write_bytes(b"synthetic client, never executable")
        (game / "steam_api64.dll").write_bytes(b"synthetic Steam dependency")
    monkeypatch.setattr("companion.game_installation.steam_roots", lambda: [steam] if via_steam else [])
    monkeypatch.setattr("companion.app.detect_games", lambda: [game])

    async def scenario():
        store, bridge, model = store_for(tmp_path), FakeBridge(), FakeModel()
        bridge.disconnected = True
        app = create_app(store=store, bridge=bridge, model=model,
                         journal=ActionJournal(tmp_path / "session.local.json"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://127.0.0.1:48860") as client:
            token = (await client.get("/api/session")).json()["token"]
            headers = {"x-kingdom-session": token}
            assert (await client.post("/api/game/launch")).status_code == 403
            assert (await client.post("/api/game/launch", headers=headers)).status_code == 409
            assert spawned == []
            found = await client.post("/api/installation/detect", headers=headers)
            assert found.json()["candidates"][0]["path"] == str(game)
            assert spawned == []
            selected = await client.put("/api/installation", headers=headers,
                                        json={"path": str(game)})
            assert selected.status_code == 200 and spawned == []
            assert bridge.config_path == config
            launched = await client.post("/api/game/launch", headers=headers)
            assert launched.json()["launched"]
            assert launched.json()["launch_method"] == ("steam" if via_steam else "direct")
            expected = ([str(steam / "Steam.exe"), "-applaunch", "701160"], {"cwd": str(steam)}) if via_steam else (
                [str(game / "KingdomTwoCrowns.exe")], {"cwd": str(game)})
            assert spawned == [expected]
            assert not app.state.manager.status()["running"]
            assert bridge.commands == [] and model.calls == 0
            bridge.disconnected = False
            again = await client.post("/api/game/launch", headers=headers)
            assert again.json()["launched"] is False
            assert len(spawned) == 1
        await app.state.manager.close()
    asyncio.run(scenario())


def test_coop_prompt_stops_pending_model_before_queue_and_requires_session(tmp_path):
    async def scenario():
        store, bridge, model = store_for(tmp_path), FakeBridge(), FakeModel()
        store.update(ModelSettings(model="mock"))
        model.gate = asyncio.Event()
        app = create_app(store=store, bridge=bridge, model=model,
                         journal=ActionJournal(tmp_path / "session.local.json"))
        manager = app.state.manager
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://127.0.0.1:48860") as client:
            token = (await client.get("/api/session")).json()["token"]
            headers = {"x-kingdom-session": token}
            assert (await client.post("/api/game/coop")).status_code == 403
            assert bridge.coop_requests == 0
            await manager.start(StartRequest(mode="autonomous"))
            await asyncio.wait_for(model.entered.wait(), 1)
            response = await client.post("/api/game/coop", headers=headers)
            assert response.status_code == 200
            assert bridge.coop_requests == 1 and bridge.commands == []
            assert manager.mode == "idle" and model.calls == 1
        await manager.close()
    asyncio.run(scenario())


def test_shutdown_is_authenticated_and_releases_before_exit_callback(tmp_path):
    async def scenario():
        bridge = FakeBridge()
        app = create_app(store=store_for(tmp_path), bridge=bridge, model=FakeModel(),
                         journal=ActionJournal(tmp_path / "session.local.json"))
        called = asyncio.Event()

        def callback():
            assert bridge.stops > 0
            called.set()

        app.state.request_shutdown = callback
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://127.0.0.1:48860") as client:
            assert (await client.post("/api/shutdown")).status_code == 403
            token = (await client.get("/api/session")).json()["token"]
            response = await client.post("/api/shutdown", headers={"x-kingdom-session": token})
            assert response.status_code == 200
            retry = await client.post("/api/control/start", headers={"x-kingdom-session": token},
                                      json={"mode": "follow"})
            assert retry.status_code == 409 and bridge.commands == []
            for path in ("/api/game/coop", "/api/game/launch", "/api/model/test",
                         "/api/installation/detect"):
                denied = await client.post(path, headers={"x-kingdom-session": token})
                assert denied.status_code == 409
            assert bridge.coop_requests == 0
            # Stop remains available even during shutdown, so release can be requested again.
            again = await client.post("/api/control/stop", headers={"x-kingdom-session": token})
            assert again.status_code == 200
            await asyncio.wait_for(called.wait(), 1)
        await app.state.manager.close()
    asyncio.run(scenario())


def test_high_cost_target_still_checks_real_balance_and_optional_budget(tmp_path):
    async def scenario():
        manager, bridge, _model = manager_for(tmp_path)
        bridge.data["players"][1]["current_payable"]["price"] = 50
        state = await bridge.state()
        assert state.player(1).current_payable.price == 50
        manager.mode, manager.session_id, manager.coin_budget = "autonomous", "test-session", 20
        manager.lease = ControlLease(str(uuid.uuid4()), "test-session", 0)
        with pytest.raises(ControlError, match="预算不足"):
            await manager._perform(manager.generation, Decision(operation="pay", target_id="wall-1", max_coins=20))
        assert bridge.commands == []
        await manager.close()
    asyncio.run(scenario())


def test_native_current_price_is_used_without_legacy_single_payment_cap(tmp_path):
    async def scenario():
        manager, bridge, _model = manager_for(tmp_path)
        bridge.data["players"][1]["current_payable"]["price"] = 3
        result = await manager.manual(Decision(operation="pay", target_id="wall-1", max_coins=1))
        assert result["status"] == "completed"
        assert bridge.commands[0]["max_coins"] == 3
        assert manager.coin_budget is None
        assert manager.journal.pending is None
        await manager.close()
    asyncio.run(scenario())


def test_launch_game_uses_client_in_current_steam_library(tmp_path, monkeypatch):
    root = tmp_path / "Steam"
    game, _config = fake_game_directory(root / "steamapps/common")
    executable = root / "Steam.exe"
    executable.write_bytes(b"synthetic client, never executable")
    spawned = []

    def fake_spawn(args, **kwargs):
        spawned.append((args, kwargs))
        return SimpleNamespace(args=args)

    monkeypatch.setattr("companion.game_installation.steam_roots", lambda: [tmp_path / "missing"])
    monkeypatch.setattr("companion.game_installation.subprocess.Popen", fake_spawn)
    process = launch_game(game)
    assert process.args == [str(executable), "-applaunch", "701160"]
    assert spawned == [(process.args, {"cwd": str(root)})]


@pytest.mark.parametrize("steam_marker", ["library", "native_api", "unity_plugin"])
def test_steam_edition_without_client_never_falls_back_to_game_exe(tmp_path, monkeypatch, steam_marker):
    if steam_marker == "library":
        game, _config = fake_game_directory(tmp_path / "Steam/steamapps/common")
    else:
        game, _config = fake_game_directory(tmp_path)
        marker = game / ("steam_api64.dll" if steam_marker == "native_api"
                         else "KingdomTwoCrowns_Data/Plugins/x86_64/steam_api64.dll")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(b"synthetic Steam dependency")
    spawned = []
    monkeypatch.setattr("companion.game_installation.steam_roots", list)
    monkeypatch.setattr("companion.game_installation.subprocess.Popen",
                        lambda args, **kwargs: spawned.append((args, kwargs)))
    with pytest.raises(ValueError, match="Steam 客户端"):
        launch_game(game)
    assert spawned == []


@pytest.mark.parametrize("acknowledged", [False, True])
def test_stop_waits_for_fresh_post_acknowledgement_release_state(tmp_path, acknowledged):
    async def scenario():
        config = tmp_path / "bridge.local.json"
        config.write_text(json.dumps({"base_url": "http://127.0.0.1:48861",
                                       "token": "synthetic_token_1234567890"}))
        count = {"stop": 0, "state": 0}

        def handler(request):
            if request.url.path == "/stop":
                count["stop"] += 1
                return httpx.Response(200, json={"ok": True, "queued": not acknowledged,
                                                 "input_released": acknowledged})
            assert request.url.path == "/state"
            count["state"] += 1
            value = state_data()
            if count["state"] == 1:
                value["captured_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
                value["input_released"] = True  # old true is not a release confirmation
            elif count["state"] == 2:
                value["input_released"] = False
            return httpx.Response(200, json=value)

        bridge = BridgeClient(config, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        await bridge.stop()
        assert count == {"stop": 1, "state": 3}
        await bridge.close()
    asyncio.run(scenario())


def test_stop_without_new_game_frame_reports_unconfirmed_and_allows_retry(tmp_path, monkeypatch):
    monkeypatch.setattr("companion.bridge.STOP_CONFIRM_TIMEOUT", 0.02)

    async def scenario():
        config = tmp_path / "bridge.local.json"
        config.write_text(json.dumps({"base_url": "http://127.0.0.1:48861",
                                       "token": "synthetic_token_1234567890"}))
        stopped = []
        old = state_data()
        old["captured_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        old["input_released"] = True

        def handler(request):
            if request.url.path == "/stop":
                stopped.append(True)
                return httpx.Response(200, json={"queued": False, "input_released": True})
            return httpx.Response(200, json=old)

        bridge = BridgeClient(config, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        manager = ControlManager(bridge, FakeModel(), store_for(tmp_path),
                                 ActionJournal(tmp_path / "session.local.json"))
        first = await manager.stop()
        assert first["release_error"] and len(stopped) == 1
        again = await manager.stop()
        assert again["release_error"] and len(stopped) == 2
        await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [ModelError("模型请求失败：HTTP 400"), RuntimeError("synthetic failure")])
def test_autonomous_model_failure_stops_without_fallback_dispatch(tmp_path, failure):
    class FailedModel(FakeModel):
        async def decide(self, _state, **_kwargs):
            self.calls += 1
            raise failure

    async def scenario():
        manager, bridge, model = manager_for(tmp_path, model=FailedModel())
        await manager.start(StartRequest(mode="autonomous"))
        await asyncio.wait_for(manager.task, 1)
        assert manager.mode == "idle" and manager.error
        assert model.calls == 1 and bridge.commands == [] and bridge.stops > 0
        assert manager.journal.pending is None
        await asyncio.sleep(0)
        assert model.calls == 1
        await manager.close()
    asyncio.run(scenario())


def test_closed_manager_cannot_start_manual_or_setup_work(tmp_path):
    async def scenario():
        manager, bridge, _model = manager_for(tmp_path)
        await manager.close()
        with pytest.raises(ControlError, match="退出"):
            await manager.start(StartRequest(mode="follow"))
        with pytest.raises(ControlError, match="退出"):
            await manager.manual(Decision(operation="move", direction="right", duration_ms=500))
        with pytest.raises(ControlError, match="退出"):
            async with manager.pause_control():
                raise AssertionError("closed setup must not run")
        assert bridge.commands == []
    asyncio.run(scenario())


def test_standalone_selected_copy_does_not_open_other_steam_edition(tmp_path, monkeypatch):
    game, _config = fake_game_directory(tmp_path)
    steam = tmp_path / "Steam"
    steam.mkdir()
    (steam / "Steam.exe").write_bytes(b"synthetic client")
    spawned = []
    monkeypatch.setattr("companion.game_installation.steam_roots", lambda: [steam])
    monkeypatch.setattr("companion.game_installation.subprocess.Popen",
                        lambda args, **kwargs: spawned.append((args, kwargs)))
    launch_game(game)
    assert spawned == [([str(game / "KingdomTwoCrowns.exe")], {"cwd": str(game)})]


@pytest.mark.parametrize("content", [[], {"pending": "not a command"}])
def test_invalid_ledger_structure_fails_closed_without_startup_crash(tmp_path, content):
    path = tmp_path / "session.local.json"
    path.write_text(json.dumps(content))
    journal = ActionJournal(path)
    assert journal.pending and journal.pending["operation"] == "unknown"


def test_invalid_config_structure_reports_recovery_error(tmp_path):
    (tmp_path / "model.local.json").write_text("[]")
    store = store_for(tmp_path)
    assert store.public()["error"]
    assert not store.public()["configured"]


@pytest.mark.parametrize("pending", [True, None])
def test_native_transaction_blocks_new_follow_and_manual_move_without_cancellation(tmp_path, pending):
    async def scenario():
        manager, bridge, _model = manager_for(tmp_path)
        bridge.data["players"][1]["transaction_pending"] = pending
        with pytest.raises(ControlError, match="交易"):
            await manager.start(StartRequest(mode="follow"))
        with pytest.raises(ControlError, match="交易"):
            await manager.manual(Decision(operation="move", direction="right", duration_ms=500))
        assert bridge.commands == [] and bridge.stops == 0 and bridge.heartbeats == 0
        await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", [None, -1, True, "1"])
def test_bridge_control_epoch_is_required_nonnegative_integer(invalid):
    data = state_data()
    if invalid is None:
        data.pop("control_epoch")
    else:
        data["control_epoch"] = invalid
    with pytest.raises(ValidationError):
        GameState.model_validate(data)


def test_bridge_heartbeat_serializes_run_and_session_epoch(tmp_path):
    async def scenario():
        config = tmp_path / "bridge.local.json"
        config.write_text(json.dumps({"base_url": "http://127.0.0.1:48861",
                                       "token": "synthetic_token_1234567890"}))
        control_id = str(uuid.uuid4())
        observed = []

        def handler(request):
            assert request.method == "POST" and request.url.path == "/heartbeat"
            observed.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})

        bridge = BridgeClient(config, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        await bridge.heartbeat(control_id=control_id, session_id="fixture-session", control_epoch=7)
        assert observed == [{"control_id": control_id, "session_id": "fixture-session", "control_epoch": 7}]
        await bridge.close()
    asyncio.run(scenario())


def test_manual_runs_use_different_uuid_with_one_fixed_identity_per_run(tmp_path):
    async def scenario():
        manager, bridge, _model = manager_for(tmp_path)
        for _ in range(2):
            result = await manager.manual(Decision(operation="move", direction="right", duration_ms=500))
            assert result["status"] == "completed"
            assert manager.lease is None
        first, second = bridge.commands
        assert first["control_id"] != second["control_id"]
        assert first["control_epoch"] == 0 and second["control_epoch"] == 1
        for body in bridge.commands:
            assert uuid.UUID(body["control_id"]).version == 4
            heartbeats = [item for item in bridge.heartbeat_leases if item["control_id"] == body["control_id"]]
            assert heartbeats and all(item["session_id"] == body["session_id"]
                                      and item["control_epoch"] == body["control_epoch"] for item in heartbeats)
        await manager.close()
    asyncio.run(scenario())


def test_stopped_generation_cannot_dispatch_decision_or_heartbeat_under_new_lease(tmp_path):
    async def scenario():
        manager, bridge, _model = manager_for(tmp_path)
        bridge.data["players"][0]["x"] = bridge.data["players"][1]["x"]
        await manager.start(StartRequest(mode="follow"))
        old_lease, old_generation = manager.lease, manager.generation
        await asyncio.sleep(0)
        await manager.stop()
        assert manager.lease is None
        await manager.start(StartRequest(mode="follow"))
        new_lease = manager.lease
        await asyncio.sleep(0)
        count = len(bridge.heartbeat_leases)
        assert old_lease.control_id != new_lease.control_id
        assert old_lease.control_epoch == 0 and new_lease.control_epoch == 1
        await manager._heartbeat(old_generation, old_lease)
        with pytest.raises(asyncio.CancelledError):
            await manager._perform(old_generation, Decision(operation="move", direction="right", duration_ms=500))
        assert bridge.commands == [] and len(bridge.heartbeat_leases) == count
        assert manager.lease is new_lease and manager.mode == "follow"
        await manager.stop()
        stopped_count = len(bridge.heartbeat_leases)
        await asyncio.sleep(0)
        assert len(bridge.heartbeat_leases) == stopped_count
        await manager.close()
    asyncio.run(scenario())


def test_initial_grant_late_after_stop_keeps_original_epoch_and_cannot_arm(tmp_path):
    class DelayedGrantBridge(FakeBridge):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.grant = asyncio.Event()

        async def heartbeat(self, *, control_id, session_id, control_epoch):
            self.entered.set()
            await self.grant.wait()
            await super().heartbeat(control_id=control_id, session_id=session_id, control_epoch=control_epoch)

    async def scenario():
        bridge = DelayedGrantBridge()
        manager, _bridge, _model = manager_for(tmp_path, bridge=bridge)
        bridge.data["players"][0]["x"] = bridge.data["players"][1]["x"]
        starting = asyncio.create_task(manager.start(StartRequest(mode="follow")))
        await asyncio.wait_for(bridge.entered.wait(), 1)
        await manager.stop()
        bridge.grant.set()
        with pytest.raises(BridgeError, match="revoked"):
            await starting
        assert manager.lease is None and manager.task is None and bridge.commands == []
        first = bridge.heartbeat_leases[0]
        assert first["control_epoch"] == 0 and bridge.data["control_epoch"] == 1
        await manager.start(StartRequest(mode="follow"))
        assert manager.lease.control_epoch == 1
        assert manager.lease.control_id != first["control_id"]
        await manager.close()
    asyncio.run(scenario())


def test_observation_epoch_change_does_not_rebind_existing_command(tmp_path):
    async def scenario():
        manager, bridge, _model = manager_for(tmp_path)
        manager.mode, manager.session_id = "manual", "test-session"
        manager.lease = ControlLease(str(uuid.uuid4()), "test-session", 0)
        bridge.data["control_epoch"] = 1
        with pytest.raises(ControlError, match="租约"):
            await manager._perform(manager.generation, Decision(operation="move", direction="right", duration_ms=500))
        assert bridge.commands == [] and manager.journal.pending is None
        assert manager.lease.control_epoch == 0
        await manager.close()
    asyncio.run(scenario())


def test_heartbeat_failure_release_blocks_new_grant_until_old_stop_finishes(tmp_path):
    class DelayedStopBridge(FakeBridge):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def stop(self):
            await super().stop()
            self.entered.set()
            await self.release.wait()

    async def scenario():
        bridge = DelayedStopBridge()
        manager, _bridge, _model = manager_for(tmp_path, bridge=bridge)
        bridge.data["players"][0]["x"] = bridge.data["players"][1]["x"]
        await manager.start(StartRequest(mode="follow"))
        old_id = manager.lease.control_id
        bridge.heartbeat_fail = True
        await asyncio.wait_for(bridge.entered.wait(), 1)
        assert manager.status()["stopping"]
        with pytest.raises(ControlError, match="正在停止"):
            await manager.start(StartRequest(mode="follow"))
        bridge.release.set()
        await asyncio.wait_for(manager.heartbeat_task, 1)
        bridge.heartbeat_fail = False
        assert not manager.status()["stopping"]
        await manager.start(StartRequest(mode="follow"))
        assert manager.lease.control_id != old_id
        assert manager.lease.control_epoch == 1
        await manager.close()
    asyncio.run(scenario())
