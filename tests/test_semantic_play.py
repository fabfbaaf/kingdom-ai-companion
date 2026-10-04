"""Natural arrangements share goals without stopping the parallel play loop."""

import asyncio
import json

import httpx
import pytest
from test_companion import state_data, store_for
from test_dialogue_parallel import Manager, Voice, until

from companion.config import ModelSettings
from companion.contracts import GameState
from companion.conversation import Conversation, natural_goal
from companion.conversation_contracts import ChatReply
from companion.model import ModelClient


@pytest.mark.parametrize("text", ["先发展经济", "今晚守右边", "钱留着修船", "优先攒钱吧"])
def test_common_arrangements_are_goals(text):
    assert natural_goal(text)


@pytest.mark.parametrize("text", ["先发展经济好吗？", "如果今晚守右边呢", "他说钱留着修船",
                                 "你觉得守右边怎么样", "‘先发展经济’是什么意思", "不要守右边"])
def test_questions_negations_and_quotations_are_left_for_contextual_understanding(text):
    assert natural_goal(text) is None


def test_arrangements_keep_spending_preference_and_ordinary_chat_does_not_stop_play():
    async def scenario():
        manager = Manager()
        manager.mode = "autonomous"
        chat = Conversation(manager, Voice())
        result = await chat.send("先发展经济")
        assert result["intent"] == "goal" and manager.goal == "先发展经济"
        await chat.send("钱留着修船")
        assert "先发展经济" in manager.goal and "钱留着修船" in manager.goal
        await chat.send("今晚守右边")
        assert "今晚守右边" in manager.goal and "钱留着修船" in manager.goal
        assert manager.model.calls == [] and manager.stops == 0 and manager.starts == 0
        before = manager.goal
        await chat.send("今天过得怎么样")
        assert manager.mode == "autonomous" and manager.goal == before and manager.stops == 0
        await chat.close()
    asyncio.run(scenario())


def test_spoken_arrangement_bypasses_slow_ordinary_chat():
    async def scenario():
        manager, voice = Manager(), Voice()
        manager.mode = "autonomous"
        manager.model.gate = asyncio.Event()
        chat = Conversation(manager, voice)
        await chat.start_voice()
        waiting = asyncio.create_task(chat.send("讲个故事"))
        await manager.model.entered.wait()
        voice.final("今晚守右边")
        await until(lambda: manager.goal == "今晚守右边")
        assert not waiting.done() and manager.stops == 0
        manager.model.gate.set()
        await waiting
        await chat.close()
    asyncio.run(scenario())


def test_model_requests_share_memory_and_plan_and_can_refine_plan(tmp_path):
    async def scenario():
        store = store_for(tmp_path)
        store.update(ModelSettings(model="synthetic", endpoint="http://localhost:9999/v1"))
        packets = []
        def handler(request):
            payload = json.loads(request.content)
            packets.append(json.loads(payload["messages"][1]["content"]))
            content = ('{"reply":"先招人，继续建设。","intent":"goal","goal":"先招募工人"}'
                       if payload["max_tokens"] == 4096 else
                       '{"operation":"stop","plan":{"stage":"economy","objective":"招募工人",'
                       '"criteria":[{"metric":"workers","comparison":"gte","value":1,"description":"已有工人"}]}}')
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})
        model = ModelClient(store, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        context = {"memory": {"campaign": {"land": 2}, "historical": True},
                   "plan": {"stage": "economy", "objective": "招人", "advisory": True}}
        model.set_context_provider(lambda: context)
        decision = await model.decide(GameState.model_validate(state_data()), goal="经济", coin_budget=None)
        reply = await model.chat("那就按你说的办", [], GameState.model_validate(state_data()), {})
        assert reply.intent == "goal" and decision.plan.stage == "economy"
        for packet in packets:
            assert packet["context"]["memory"] == context["memory"]
            assert packet["context"]["plan"] == context["plan"]
        await model.close()
    asyncio.run(scenario())


def test_conditional_arrangement_is_allowed_but_discussion_cannot_change_plan():
    async def scenario():
        manager = Manager()
        manager.mode = "autonomous"
        chat = Conversation(manager, Voice())
        for text in ("你觉得先守右边好吗", "如果守右边会怎样", "“先发展经济”"):
            manager.model.replies[text] = ChatReply(reply="先守右边", intent="goal", goal="守右边")
            result = await chat.send(text)
            assert "未执行" in result["control_result"] and not manager.changed_goals
        text = "如果敌人来了就守右边"
        manager.model.replies[text] = ChatReply(reply="按这个安排", intent="goal", goal=text)
        result = await chat.send(text)
        assert manager.goal == text and result["intent"] == "goal"
        await chat.close()
    asyncio.run(scenario())
