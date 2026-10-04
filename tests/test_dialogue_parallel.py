"""Offline dialogue queuing, instruction precedence and speech callback checks."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from companion.controller import ControlError
from companion.conversation import Conversation, quick_intent
from companion.conversation_contracts import ChatReply
from companion.model import ModelError


class Voice:
    def __init__(self):
        self.state = "ready"
        self.partial = ""
        self.items = []

    def status(self):
        return {"state": self.state, "cursor": len(self.items), "partial": self.partial}

    def events(self, after):
        return {"events": self.items[after:], "cursor": len(self.items)}

    def final(self, text):
        self.partial = ""
        self.items.append({"text": text, "seq": len(self.items) + 1})

    async def start(self):
        self.state = "listening"
        return self.status()

    async def stop(self):
        self.state = "ready"
        return self.status()

    async def close(self):
        await self.stop()


class ChatModel:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.gate = None
        self.replies = {}
        self.active = 0
        self.max_active = 0

    async def chat(self, text, history, state, control):
        self.calls.append((text, history, state, control))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            if self.gate:
                await self.gate.wait()
            reply = self.replies.get(text, ChatReply(reply=f"回答：{text}"))
            if isinstance(reply, Exception):
                raise reply
            return reply
        finally:
            self.active -= 1


class Manager:
    def __init__(self):
        self.model = ChatModel()
        self.bridge = SimpleNamespace(state=self.state)
        self.generation = 0
        self.mode = "idle"
        self.release_error = None
        self.accepting = True
        self.stops = 0
        self.starts = 0
        self.start_gate = None
        self.start_entered = asyncio.Event()
        self.goal = ""
        self.changed_goals = []
        self.update_calls = 0
        self.state_reads = 0

    async def state(self):
        self.state_reads += 1
        return {"night": False, "coins": 8}

    def ensure_accepting(self):
        if not self.accepting:
            raise ControlError("后台正在退出")

    def gameplay_context(self):
        return {"goal": self.goal, "recent_action": "已向右移动", "coins": 8}

    def status(self):
        return {"mode": self.mode, "context": self.gameplay_context()}

    async def stop(self):
        self.stops += 1
        self.generation += 1
        self.mode = "idle"
        return self.status()

    async def start(self, request, *, guard=None):
        self.start_entered.set()
        if self.start_gate:
            await self.start_gate.wait()
        if guard and not guard():
            raise ControlError("开始操作已被后续停止或关闭监听撤销")
        self.generation += 1
        self.starts += 1
        self.mode = request.mode
        return self.status()

    def update_goal(self, goal):
        self.update_calls += 1
        self.goal = goal

    async def change_goal(self, goal):
        self.changed_goals.append(goal)
        self.goal = goal


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("text,intent", [
    ("你先停一下吧！", "stop"), ("麻烦你立即停止。", "stop"),
    ("现在停止跟随", "stop"), ("别再走了", "stop"), ("请不要动", "stop"),
    ("停下，先别动", "stop"), ("先停下来让我看看", "stop"),
    ("停下听我说", "stop"), ("先别动，等一下", "stop"),
    ("你现在跟着我走吧", "follow"), ("麻烦跟我一起走", "follow"),
    ("先跟紧我", "follow"), ("Follow me!", "follow"),
    ("别跟着我", None), ("不要停止", None), ("不要跟随我", None),
    ("不用跟我走", None), ("如果有人说停止怎么办", None),
    ("他说跟着我", None), ("“停止”", None), ("请你说‘跟着我’", None),
    ("停止是什么意思", None), ("不要再跟随我了", None),
    ("不要停下来让我看看", None), ("他说‘停下听我说’", None),
])
def test_only_explicit_short_control_phrases(text, intent):
    assert quick_intent(text) == intent


def test_queue_allows_three_waiting_and_preserves_fifo_history():
    async def scenario():
        manager = Manager()
        manager.model.gate = asyncio.Event()
        chat = Conversation(manager, Voice())
        first = asyncio.create_task(chat.send("第一条"))
        await manager.model.entered.wait()
        queued = [asyncio.create_task(chat.send(f"排队{i}")) for i in range(3)]
        await asyncio.sleep(0)
        assert chat.public()["busy"] and chat.public()["queued"] == 3
        with pytest.raises(ControlError, match="最多 3 条"):
            await chat.send("超限")
        manager.model.gate.set()
        await asyncio.gather(first, *queued)
        assert [call[0] for call in manager.model.calls] == ["第一条", "排队0", "排队1", "排队2"]
        assert manager.model.max_active == 1
        assert manager.model.calls[1][1] == [
            {"role": "user", "content": "第一条"},
            {"role": "assistant", "content": "回答：第一条"},
        ]
        assert manager.model.calls[0][3]["context"]["coins"] == 8
        assert not chat.public()["busy"] and chat.public()["queued"] == 0
        await chat.close()
    asyncio.run(scenario())


def test_full_queue_does_not_delay_follow_or_stop_or_play_late_answers():
    async def scenario():
        manager, spoken = Manager(), []
        manager.model.gate = asyncio.Event()
        manager.model.replies["旧目标"] = ChatReply(reply="旧计划", intent="goal", goal="向右修墙")

        async def say(message):
            spoken.append(message)

        chat = Conversation(manager, Voice(), on_reply=say)
        pending = asyncio.create_task(chat.send("旧目标"))
        await manager.model.entered.wait()
        queued = [asyncio.create_task(chat.send(f"旧队列{i}")) for i in range(3)]
        await asyncio.sleep(0)
        followed = await asyncio.wait_for(chat.send("现在跟着我吧"), 0.3)
        assert followed["intent"] == "follow" and manager.mode == "follow"
        stopped = await asyncio.wait_for(chat.send("先停下来"), 0.3)
        assert stopped["intent"] == "stop" and manager.mode == "idle"
        manager.model.gate.set()
        result = await pending
        await asyncio.gather(*queued)
        assert "已取消" in result["control_result"]
        assert chat.goal == "" and manager.changed_goals == []
        assert len(spoken) == 2 and all(message["playable"] for message in spoken)
        assert all(message["playable"] is False for message in chat.messages
                   if message["role"] == "assistant" and message.get("stale"))
        await chat.close()
    asyncio.run(scenario())


def test_queued_follow_snapshots_revision_before_later_stop():
    async def scenario():
        manager = Manager()
        manager.model.gate = asyncio.Event()
        manager.model.replies["陪我去左边"] = ChatReply(reply="一起走", intent="follow")
        chat = Conversation(manager, Voice())
        first = asyncio.create_task(chat.send("随便聊聊"))
        await manager.model.entered.wait()
        old_follow = asyncio.create_task(chat.send("陪我去左边"))
        await asyncio.sleep(0)
        await chat.send("停止")
        manager.model.gate.set()
        await first
        result = await old_follow
        assert "已取消" in result["control_result"]
        assert manager.starts == 0 and manager.mode == "idle"
        await chat.close()
    asyncio.run(scenario())


def test_follow_start_is_revoked_by_fast_stop():
    async def scenario():
        manager = Manager()
        manager.start_gate = asyncio.Event()
        chat = Conversation(manager, Voice())
        following = asyncio.create_task(chat.send("请跟着我"))
        await manager.start_entered.wait()
        await chat.send("停下")
        manager.start_gate.set()
        result = await following
        assert "撤销" in result["control_result"]
        assert manager.starts == 0 and manager.mode == "idle"
        await chat.close()
    asyncio.run(scenario())


def test_voice_preserves_busy_sentences_in_order():
    async def scenario():
        manager, voice = Manager(), Voice()
        manager.model.gate = asyncio.Event()
        chat = Conversation(manager, voice)
        await chat.start_voice()
        voice.final("第一句")
        await manager.model.entered.wait()
        voice.final("中间一句")
        voice.final("最新一句")
        await until(lambda: chat.public()["voice_pending"])
        manager.model.gate.set()
        await until(lambda: len(manager.model.calls) == 3)
        assert [call[0] for call in manager.model.calls] == ["第一句", "中间一句", "最新一句"]
        assert any(message["content"] == "中间一句" for message in chat.messages)
        await chat.close()
    asyncio.run(scenario())


def test_microphone_restart_does_not_replay_latest_or_queued_voice():
    async def scenario():
        manager, voice = Manager(), Voice()
        manager.model.gate = asyncio.Event()
        chat = Conversation(manager, voice)
        await chat.start_voice()
        active = asyncio.create_task(chat.send("文字聊天"))
        await manager.model.entered.wait()
        queued = asyncio.create_task(chat.send("旧语音队列", source="voice",
                                               voice_epoch=chat._voice_epoch))
        voice.final("旧待处理语音")
        await until(lambda: chat.public()["voice_pending"])
        chat.cancel_voice()
        await voice.stop()
        assert (await queued)["control_result"] == "已取消"
        manager.model.gate.set()
        await active
        await chat.start_voice()
        voice.final("新会话语音")
        await until(lambda: len(manager.model.calls) == 2)
        assert [call[0] for call in manager.model.calls] == ["文字聊天", "新会话语音"]
        await chat.close()
    asyncio.run(scenario())


def test_close_cancels_active_and_entire_queue_without_replay():
    async def scenario():
        manager = Manager()
        manager.model.gate = asyncio.Event()
        chat = Conversation(manager, Voice())
        first = asyncio.create_task(chat.send("正在处理"))
        await manager.model.entered.wait()
        queued = [asyncio.create_task(chat.send(f"等待{i}")) for i in range(3)]
        await asyncio.sleep(0)
        await chat.close()
        results = await asyncio.gather(first, *queued)
        assert all(result["control_result"] == "已取消" for result in results)
        manager.model.gate.set()
        assert (await chat.send("跟随我"))["control_result"] == "已取消"
        assert len(manager.model.calls) == 1 and manager.starts == 0
        assert not chat.public()["busy"] and chat.public()["queued"] == 0
        with pytest.raises(ControlError, match="已关闭"):
            await chat.start_voice()
    asyncio.run(scenario())


@pytest.mark.parametrize("text,intent", [
    ("别跟着我", "follow"), ("不要停止", "stop"),
    ("他说‘跟着我’是什么意思", "follow"), ("如果有人说停止怎么办", "stop"),
])
def test_model_cannot_turn_negation_or_discussion_into_control(text, intent):
    async def scenario():
        manager = Manager()
        manager.model.replies[text] = ChatReply(reply="模型错误地提议操作", intent=intent)
        chat = Conversation(manager, Voice())
        result = await chat.send(text)
        assert "未执行" in result["control_result"]
        assert manager.stops == 0 and manager.starts == 0
        assert not chat.messages[-1]["playable"]
        await chat.close()
    asyncio.run(scenario())


def test_reply_callback_only_receives_success_and_notify_uses_shared_history():
    async def scenario():
        manager, spoken = Manager(), []
        manager.model.replies["报错"] = ModelError("模型连接失败")

        async def say(message):
            spoken.append(message)

        chat = Conversation(manager, Voice(), on_reply=say)
        await chat.send("你好")
        await chat.send("报错")
        notice = await chat.notify("  天黑了，回城吧。  ")
        await chat.send("提醒我什么了")
        assert len(spoken) == 3
        assert notice["source"] == "proactive" and notice["content"] == "天黑了，回城吧。"
        assert notice in chat.public()["messages"]
        assert {"role": "assistant", "content": "天黑了，回城吧。"} in manager.model.calls[-1][1]
        assert chat.public()["context"]["recent_action"] == "已向右移动"
        await chat.close()
    asyncio.run(scenario())


def test_async_goal_change_is_used_instead_of_legacy_update_goal():
    async def scenario():
        manager = Manager()
        manager.mode = "autonomous"
        manager.model.replies["先回家"] = ChatReply(reply="回家", intent="goal", goal="回城")
        chat = Conversation(manager, Voice())
        result = await chat.send("先回家")
        assert "已调整" in result["control_result"]
        assert manager.changed_goals == ["回城"] and manager.update_calls == 0
        assert chat.goal == "回城"
        await chat.close()
    asyncio.run(scenario())


def test_voice_partial_interrupts_once_per_change_and_final_also_interrupts():
    async def scenario():
        manager, voice, interruptions = Manager(), Voice(), []

        async def interrupt():
            interruptions.append(True)

        chat = Conversation(manager, voice, on_voice_activity=interrupt)
        await chat.start_voice()
        voice.partial = "我想"
        await until(lambda: len(interruptions) == 1)
        await asyncio.sleep(0.2)
        assert len(interruptions) == 1
        voice.partial = "我想问"
        await until(lambda: len(interruptions) == 2)
        voice.final("现在到哪了")
        await until(lambda: len(interruptions) == 3 and len(manager.model.calls) == 1)
        assert manager.stops == 0
        voice.partial = "我想"
        await until(lambda: len(interruptions) == 4)
        await chat.close()
    asyncio.run(scenario())


def test_voice_stop_releases_control_before_slow_speech_interrupt():
    async def scenario():
        manager, voice = Manager(), Voice()
        interrupt_entered, interrupt_release = asyncio.Event(), asyncio.Event()

        async def interrupt():
            interrupt_entered.set()
            await interrupt_release.wait()

        chat = Conversation(manager, voice, on_voice_activity=interrupt)
        await chat.start_voice()
        # A partial in the same poll must not delay the final stop either.
        voice.final("停下听我说")
        voice.partial = "停下"
        await interrupt_entered.wait()
        assert manager.stops == 1 and manager.mode == "idle"
        interrupt_release.set()
        await chat.close()
    asyncio.run(scenario())


def test_voice_stop_is_heard_while_previous_partial_speech_shutdown_is_busy():
    async def scenario():
        manager, voice = Manager(), Voice()
        interrupt_entered, interrupt_release = asyncio.Event(), asyncio.Event()

        async def interrupt():
            interrupt_entered.set()
            await interrupt_release.wait()

        chat = Conversation(manager, voice, on_voice_activity=interrupt)
        await chat.start_voice()
        voice.partial = "我想问"
        await interrupt_entered.wait()
        voice.final("先停下来让我看看")
        await until(lambda: manager.stops == 1)
        assert not interrupt_release.is_set() and manager.mode == "idle"
        interrupt_release.set()
        await chat.close()
    asyncio.run(scenario())


def test_voice_model_stop_is_not_cancelled_by_its_own_microphone_revocation():
    async def scenario():
        manager, voice, spoken = Manager(), Voice(), []
        stopping, release = asyncio.Event(), asyncio.Event()
        original_stop = manager.stop

        async def slow_stop():
            result = await original_stop()
            stopping.set()
            await release.wait()
            return result

        async def say(message):
            spoken.append(message)

        manager.stop = slow_stop
        manager.model.replies["我先接手自己走"] = ChatReply(reply="交给你", intent="stop")
        chat = Conversation(manager, voice, on_reply=say)
        await chat.start_voice()
        voice.final("我先接手自己走")
        await stopping.wait()
        release.set()
        await until(lambda: bool(spoken))
        assert "已停止" in spoken[-1]["content"] and manager.mode == "idle"
        await chat.close()
    asyncio.run(scenario())
