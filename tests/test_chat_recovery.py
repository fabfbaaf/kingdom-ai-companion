"""Reproduce provider chat annotations and one-action failures using local fixtures."""

import asyncio

import httpx
import pytest
from test_autonomous_play import SequenceModel, continuous, move
from test_companion import manager_for
from test_conversation import ChatModel, FakeVoice
from test_model_voice_chat import completion, make_store
from test_movement_protocol import MovingBridge, PlanningModel
from test_open_autonomy import RecoverBridge

from companion.contracts import Decision
from companion.conversation import Conversation, explicit_goal
from companion.model import ModelClient, ModelError


@pytest.mark.parametrize("content", [
    '{"type":"dialogue","reply":"附近还没有新观测。","intent":"chat"}',
    '{"type":"chat","reply":"我在。","intent":"chat","goal":""}',
    '```json\n{"reply":"我在。","intent":"chat","goal":null}\n```',
    '```json\r\n{"reply":"我在。","intent":"chat"}\r\n```',
    '{"reply":"我在。","intent":"chat","reason":"玩家只是提问","confidence":0.9}',
])
def test_harmless_chat_annotations_and_empty_optional_goals_are_accepted(tmp_path, content):
    async def scenario():
        model = ModelClient(make_store(tmp_path), httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: completion(content))))
        try:
            result = await model.chat("现在是什么情况？", [], None, {})
            assert result.reply and result.intent == "chat" and result.goal is None
        finally:
            await model.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("content", [
    '{"reply":"我来。","intent":"chat","intent":"follow"}',
    '{"reply":"我来。","intent":"goal","goal":"往右","coin_budget":20}',
    '```json\n{"reply":"我来。","intent":"chat"}\n```\n{"intent":"follow"}',
])
def test_ambiguous_or_extra_control_authority_is_not_accepted(tmp_path, content):
    async def scenario():
        model = ModelClient(make_store(tmp_path), httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: completion(content))))
        try:
            with pytest.raises(ModelError):
                await model.chat("现在是什么情况？", [], None, {})
        finally:
            await model.close()
    asyncio.run(scenario())


def test_decision_json_fence_and_annotations_keep_only_valid_operation(tmp_path):
    async def scenario():
        model = ModelClient(make_store(tmp_path), httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: completion('```json\n{"type":"action","operation":"move","direction":"right","duration_ms":3000,"reason":"巡视"}\n```'))))
        try:
            result = await model._generate([])
            assert result.operation == "move" and result.duration_ms == 3000
            assert "reason" not in result.model_dump() and "type" not in result.model_dump()
        finally:
            await model.close()
    asyncio.run(scenario())


def test_target_prefix_records_goal_even_when_model_is_unavailable(tmp_path):
    async def scenario():
        manager, bridge, model = manager_for(tmp_path, model=ChatModel(), configured=False)
        chat = Conversation(manager, FakeVoice())
        try:
            result = await chat.send("目标：保护营地，先保留金币。")
            assert result["intent"] == "goal" and result["goal"] == "保护营地，先保留金币。"
            assert chat.goal == result["goal"] and "目标已记下" in result["control_result"]
            assert model.chat_calls == 0 and manager.mode == "idle" and not bridge.commands
        finally:
            await chat.close()
            await manager.stop()
    asyncio.run(scenario())


def test_goal_prefix_does_not_capture_quoted_or_discussed_goals():
    assert explicit_goal("如果我说目标：保护营地呢") is None
    assert explicit_goal("‘目标：保护营地’是什么意思") is None
    assert explicit_goal("目标：   ") is None
    assert explicit_goal("目标：" + "长" * 501) is None


def test_explicit_goal_supersedes_an_older_model_proposed_goal(tmp_path):
    async def scenario():
        model = ChatModel("goal", "旧目标")
        model.chat_gate = asyncio.Event()
        manager, _, _ = manager_for(tmp_path, model=model)
        chat = Conversation(manager, FakeVoice())
        try:
            pending = asyncio.create_task(chat.send("你帮我想个目标"))
            await model.chat_entered.wait()
            await chat.send("目标：保护营地，先保留金币。")
            model.chat_gate.set()
            result = await pending
            assert "已取消" in result["control_result"]
            assert chat.goal == "保护营地，先保留金币。"
        finally:
            await chat.close()
            await manager.stop()
    asyncio.run(scenario())


def test_continuous_mode_retries_one_invalid_decision_after_movement(tmp_path):
    class RetryModel(SequenceModel):
        async def decide(self, state, **kwargs):
            result = await super().decide(state, **kwargs)
            if isinstance(result, Exception):
                raise result
            return result

    async def scenario():
        model = RetryModel([move(), ModelError("模型指令格式无效", retryable=True), move()])
        manager, bridge, _ = manager_for(tmp_path, model=model)
        try:
            await manager.start(continuous(spending_mode="wallet"))
            await asyncio.wait_for(model.exhausted.wait(), 2)
            assert manager.status()["running"] and len(bridge.commands) == 2 and model.calls == 4
            assert manager.error is None and manager.status()["phase"] == "deciding"
        finally:
            await manager.stop()
        assert manager.status()["phase"] == "idle"
    asyncio.run(scenario())


def test_prefetch_format_failure_replans_after_current_input_ends(tmp_path):
    class FailingPlanModel(PlanningModel):
        async def decide(self, state, **kwargs):
            result = await super().decide(state, **kwargs)
            if kwargs.get("in_flight"):
                raise ModelError("模型指令格式无效", retryable=True)
            return result

    async def scenario():
        bridge, model = MovingBridge(), FailingPlanModel()
        manager, _, _ = manager_for(tmp_path, bridge=bridge, model=model)
        try:
            await manager.start(continuous())
            await asyncio.wait_for(model.prefetch_entered.wait(), 1)
            bridge.complete.set()
            async with asyncio.timeout(1):
                while model.calls < 3:
                    await asyncio.sleep(0.01)
            assert manager.status()["running"] and manager.error is None
            assert len(bridge.commands) == 1
        finally:
            await manager.stop()
    asyncio.run(scenario())


def test_failed_payment_target_is_temporarily_avoided_without_stopping_play(tmp_path):
    async def scenario():
        bridge = RecoverBridge()
        pay = Decision(operation="pay", target_id="wall-1", max_coins=3)
        manager, _, model = manager_for(tmp_path, bridge=bridge, model=SequenceModel([pay, pay, move()]))
        try:
            await manager.start(continuous(spending_mode="wallet"))
            await asyncio.wait_for(model.exhausted.wait(), 2)
            assert [body["operation"] for body in bridge.commands] == ["pay", "move"]
            cooldown = manager.gameplay_context()["payment_cooldowns"][0]
            assert cooldown["target_id"] == "wall-1" and 0 < cooldown["remaining_seconds"] <= 20
            assert manager.status()["running"] and not manager.status()["needs_review"]
        finally:
            await manager.stop()
    asyncio.run(scenario())


def test_provider_errors_mark_only_transient_failures_retryable(tmp_path):
    async def scenario():
        for status in (400, 401, 403, 429, 500):
            model = ModelClient(make_store(tmp_path), httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request, status=status: httpx.Response(status, json={"error": "synthetic"}))))
            try:
                with pytest.raises(ModelError) as error:
                    await model._generate([])
                assert error.value.retryable is (status in {429, 500})
            finally:
                await model.close()
    asyncio.run(scenario())


def test_stop_during_model_retry_wait_prevents_any_later_input(tmp_path):
    class RetryModel(SequenceModel):
        async def decide(self, state, **kwargs):
            self.calls += 1
            raise ModelError("临时格式错误", retryable=True)

    async def scenario():
        model = RetryModel([])
        manager, bridge, _ = manager_for(tmp_path, model=model)
        try:
            await manager.start(continuous())
            async with asyncio.timeout(1):
                while manager.status()["phase"] != "waiting":
                    await asyncio.sleep(0.005)
            calls = model.calls
            await manager.stop()
            await asyncio.sleep(0.3)
            assert model.calls == calls and not bridge.commands
            assert not manager.status()["running"] and manager.status()["phase"] == "idle"
        finally:
            await manager.stop()
    asyncio.run(scenario())


def test_explicit_goal_keeps_running_wallet_and_independent_play(tmp_path):
    async def scenario():
        model = SequenceModel([])
        manager, _, _ = manager_for(tmp_path, model=model)
        chat = Conversation(manager, FakeVoice())
        try:
            await manager.start(continuous(spending_mode="wallet", play_style="independent"))
            await asyncio.wait_for(model.exhausted.wait(), 1)
            result = await chat.send("目标：保护营地，先保留金币。")
            status = manager.status()
            assert result["intent"] == "goal" and status["goal"] == result["goal"]
            assert status["running"] and status["continuous"]
            assert status["spending_mode"] == "wallet" and status["coin_budget_remaining"] is None
            assert status["play_style"] == "independent"
        finally:
            await chat.close()
            await manager.stop()
    asyncio.run(scenario())
