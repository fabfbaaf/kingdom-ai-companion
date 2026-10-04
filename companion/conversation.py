"""Bounded dialogue dispatch independent of the P2 movement/planning loop."""

from __future__ import annotations

import asyncio
import re
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass

from companion.bridge import BridgeError
from companion.contracts import CAMPAIGN_GOAL, StartRequest
from companion.controller import ControlError, ControlManager
from companion.model import ModelError

_QUOTES = frozenset("\"'“”‘’「」『』《》")
_PREFIX = re.compile(r"^(?:(?:请|麻烦|你|先|现在|马上|立刻|立即|赶紧|帮我|给我|陪玩|ai)){0,4}")
_SUFFIX = re.compile(r"(?:吧|啊|呀|了|好吗|好不好)$")
_STOP_PHRASES = {
    "停", "停下", "停止", "停一下", "停下来", "停一停", "停住", "暂停", "暂停一下",
    "停在这里", "停在这儿", "别动", "不要动", "别走", "别再走", "停止陪玩",
    "停止行动", "停止跟随", "别跟", "接管", "stop",
    "停下先别动", "停下来让我看看", "停下听我说", "别动等一下",
}
_FOLLOW_PHRASES = {
    "跟着我", "跟随我", "跟我走", "跟上我", "跟随", "跟着我走", "跟我一起走",
    "继续跟着我", "继续跟随我", "跟紧我", "陪我走", "follow", "followme",
}


def explicit_goal(text: str) -> str | None:
    match = re.fullmatch(r"\s*目标\s*[:：]\s*(.{1,500})\s*", text, re.DOTALL)
    if match:
        goal = match.group(1).strip()
        if goal:
            return goal
    return None


def quick_intent(text: str) -> str | None:
    # Full-phrase matching keeps negations, quotations and hypothetical discussion conversational.
    if any(character in _QUOTES for character in text):
        return None
    compact = re.sub(r"[\s，。！？、；,.!?;]", "", text).casefold()
    compact = _PREFIX.sub("", compact)
    compact = _SUFFIX.sub("", compact)
    if compact in _STOP_PHRASES:
        return "stop"
    if compact in _FOLLOW_PHRASES:
        return "follow"
    return None


def natural_goal(text: str) -> str | None:
    """Common arrangements are goals even without an imperative verb or '目标：'."""
    if any(character in _QUOTES for character in text):
        return None
    if re.search(r"如果|假如|要是|比如|举例|有人说|他说|她说|[？?]|怎么样|好不好|可以吗|要不要", text):
        return None
    compact = re.sub(r"[\s，。！、；,.!;]", "", text)
    if re.fullmatch(r"(?:我们|你|帮我|请|现在|先|接下来|还是|优先){0,4}(?:发展经济|搞经济|发展营地|赚金币|攒钱)(?:吧|啊)?", compact):
        return text.strip()[:500]
    if re.fullmatch(r"(?:你|帮我|请|现在|先|今晚|今天|接下来){0,4}(?:守住?|保护|防守)(?:左边|右边|营地|左侧|右侧)(?:吧|啊)?", compact):
        return re.sub(r"^(?:帮我|请|你)", "", text.strip())[:500]
    if re.fullmatch(r"(?:金币|钱|宝石)(?:先|都|暂时)?(?:留着|留给|留|保留)(?:修船|造船|出航|修墙|升级城堡)(?:用|吧)?", compact):
        return text.strip()[:500]
    return None


def merge_arrangement(goal: str, previous: str) -> str:
    reserve = re.compile(r"(?:金币|钱|宝石)(?:先|都|暂时)?(?:留着|留给|留|保留)(?:修船|造船|出航|修墙|升级城堡)(?:用|吧)?")
    new_preference = reserve.fullmatch(re.sub(r"[\s，。！、；,.!;]", "", goal))
    old_preference = reserve.search(previous)
    if new_preference:
        base = previous.split("；玩家安排：")[0] or CAMPAIGN_GOAL
        return base[:450] + "；玩家安排：" + goal[:40]
    if old_preference:
        return goal[:450] + "；玩家安排：" + old_preference.group(0)
    return goal


def _discussed_intent(text: str, intent: str) -> bool:
    """A model must not turn an explicitly negated/quoted command into an action."""
    if intent not in {"stop", "follow"}:
        return False
    if any(character in _QUOTES for character in text):
        return True
    if re.search(r"如果|假如|要是|比如|举例|有人说|他说|她说", text):
        return True
    verb = r"(?:停|停止|暂停|接管)" if intent == "stop" else r"(?:跟|跟随|陪我走)"
    return bool(re.search(r"(?:不要|别|不用|无需|不必)(?:再)?" + verb, text))


def _discussed_goal(text: str) -> bool:
    if re.search(r"比如|举例|有人说|他说|她说|你觉得|会怎样|会怎么样", text):
        return True
    if re.match(r"\s*[\"“「『].*[\"”」』]\s*(?:是什么意思|呢|[？?])?\s*$", text):
        return True
    # A conditional arrangement ('如果敌人来了就守右边') can be a real goal.
    return bool(re.search(r"如果|假如|要是", text) and not re.search(r"就|则", text))


@dataclass
class _ChatRequest:
    text: str
    source: str
    revision: int
    generation: int
    voice_epoch: int | None
    future: asyncio.Future
    sender: asyncio.Task | None


class Conversation:
    MAX_QUEUED = 3

    def __init__(self, manager: ControlManager, voice,
                 on_reply: Callable[[dict], Awaitable[None]] | None = None,
                 on_voice_activity: Callable[[], Awaitable[None]] | None = None) -> None:
        self.manager, self.voice = manager, voice
        self.on_reply = on_reply
        self.on_voice_activity = on_voice_activity
        self.messages: list[dict] = []
        self.goal = ""
        self._sequence = 0
        self._revision = 0
        self._voice_epoch = 0
        self._worker: asyncio.Task | None = None
        self._voice_tasks: set[asyncio.Task] = set()
        self._voice_cursor = 0
        self._voice_pending: deque[tuple[str, int]] = deque()
        self._voice_error: str | None = None
        self._voice_dropped = 0
        self._voice_partial = ""
        self._activity_task: asyncio.Task | None = None
        self._chat_queue: deque[_ChatRequest] = deque()
        self._active_chat: _ChatRequest | None = None
        self._chat_task: asyncio.Task | None = None
        self._closed = False

    def _append(self, role: str, content: str, source: str, *, playable: bool = False) -> dict:
        self._sequence += 1
        message = {"id": self._sequence, "role": role, "content": content, "source": source}
        if role == "assistant":
            message["playable"] = playable
        self.messages.append(message)
        self.messages = self.messages[-100:]
        return message

    def public(self) -> dict:
        result = {"messages": list(self.messages), "goal": self.goal,
                  "busy": self._active_chat is not None, "queued": len(self._chat_queue),
                  "voice_pending": bool(self._voice_pending),
                  "voice_queued": len(self._voice_pending), "voice_error": self._voice_error,
                  "voice_dropped": self._voice_dropped}
        if hasattr(self.manager, "gameplay_context"):
            result["context"] = self.manager.gameplay_context()
        return result

    async def _publish(self, message: dict) -> None:
        if self.on_reply and message.get("playable") and not self._closed:
            # Speech availability cannot turn a completed dialogue into a failed request.
            with suppress(Exception):
                await self.on_reply(dict(message))

    async def notify(self, text: str) -> dict:
        """Add an observation/reminder to the same dialogue and optional speech stream."""
        if self._closed:
            raise ControlError("对话已关闭，不能添加提醒")
        text = text.strip()
        if not text:
            raise ControlError("提醒内容不能为空")
        message = self._append("assistant", text[:1500], "proactive", playable=True)
        await self._publish(message)
        return message

    def cancel_actions(self) -> None:
        self._revision += 1

    def _cancelled(self, reply: str = "对话已关闭，本条请求已取消。") -> dict:
        return {"reply": reply, "intent": "chat", "control_result": "已取消", **self.public()}

    def cancel_voice(self) -> None:
        self._voice_epoch += 1
        self._voice_pending.clear()
        self._voice_partial = ""
        self._voice_cursor = self.voice.status().get("cursor", self._voice_cursor)
        current = asyncio.current_task()
        active_sender = (self._active_chat.sender if self._active_chat
                         and self._chat_task is current else None)
        for task in tuple(self._voice_tasks):
            if task is not current and task is not active_sender:
                task.cancel()
        # Voice requests can also enter through send(); remove them before the next chat starts.
        for request in tuple(self._chat_queue):
            if request.voice_epoch is not None:
                self._chat_queue.remove(request)
                if not request.future.done():
                    request.future.set_result(self._cancelled("语音监听已关闭，本条指令已取消。"))
        request = self._active_chat
        if request and request.voice_epoch is not None and self._chat_task is not current:
            if not request.future.done():
                request.future.set_result(self._cancelled("语音监听已关闭，本条指令已取消。"))
            if self._chat_task and self._chat_task is not current:
                self._chat_task.cancel()

    async def start_voice(self) -> dict:
        if self._closed:
            raise ControlError("对话已关闭，不能开启监听")
        self.cancel_voice()
        self._voice_error = None
        self._voice_dropped = 0
        result = await self.voice.start()
        self.start_worker()
        return result

    def start_worker(self) -> None:
        if not self._closed and (self._worker is None or self._worker.done()):
            self._worker = asyncio.create_task(self._listen())

    def _dispatch_voice(self, text: str, epoch: int) -> None:
        task = asyncio.create_task(self.send(text, source="voice", voice_epoch=epoch))
        self._voice_tasks.add(task)
        task.add_done_callback(self._voice_done)

    async def _listen(self) -> None:
        while not self._closed:
            status = self.voice.status()
            if status["state"] == "listening":
                partial = status.get("partial", "").strip()
                partial_changed = partial != self._voice_partial
                self._voice_partial = partial
                batch_epoch = self._voice_epoch
                batch = self.voice.events(self._voice_cursor)
                self._voice_cursor = batch["cursor"]
                if batch.get("dropped"):
                    self._voice_dropped += int(batch["dropped"])
                    self._voice_error = "语音事件已积压，部分识别结果未能读取，请重说遗漏内容。"
                finals = [event["text"].strip()[:1000] for event in batch["events"]]
                urgent_stop = next((text for text in finals if quick_intent(text) == "stop"), None)
                if urgent_stop:
                    # Revocation precedes any potentially slow speech process shutdown. It also
                    # invalidates every other final in this microphone batch.
                    await self.send(urgent_stop, source="voice", voice_epoch=batch_epoch)
                    self._dispatch_voice_activity()
                elif partial_changed and partial:
                    self._dispatch_voice_activity()
                for text in finals:
                    if (batch_epoch != self._voice_epoch
                            or self.voice.status()["state"] != "listening"):
                        break
                    if not text:
                        continue
                    intent = quick_intent(text)
                    if intent == "stop":
                        await self.send(text, source="voice", voice_epoch=batch_epoch)
                        self._dispatch_voice_activity()
                        continue
                    self._dispatch_voice_activity()
                    if intent == "follow" or explicit_goal(text) or natural_goal(text):
                        # A slow follow start must leave the listener free to hear an urgent stop.
                        self._dispatch_voice(text, batch_epoch)
                    elif self._active_chat or self._voice_tasks or self._voice_pending:
                        if len(self._voice_pending) < 64:
                            self._voice_pending.append((text, batch_epoch))
                        else:
                            self._voice_dropped += 1
                            self._voice_error = "语音等待队列已满，这句话未发送；请等回复后重说。"
                    else:
                        self._dispatch_voice(text, batch_epoch)
                if (self._voice_pending and not self._active_chat and not self._voice_tasks
                        and batch_epoch == self._voice_epoch):
                    text, epoch = self._voice_pending.popleft()
                    self._dispatch_voice(text, epoch)
            else:
                # A stopped microphone never retains a sentence for the next listening session.
                self._voice_pending.clear()
                self._voice_partial = ""
            await asyncio.sleep(0.15)

    async def _voice_activity(self) -> None:
        if self.on_voice_activity:
            with suppress(Exception):
                await self.on_voice_activity()

    def _dispatch_voice_activity(self) -> None:
        # Speech shutdown may await a child process; hearing an urgent stop must stay responsive.
        # Another partial while shutdown is in flight already has the same interruption effect.
        if self.on_voice_activity and (self._activity_task is None or self._activity_task.done()):
            self._activity_task = asyncio.create_task(self._voice_activity())

    def _voice_done(self, task: asyncio.Task) -> None:
        self._voice_tasks.discard(task)
        if not task.cancelled():
            try:
                task.result()
            except Exception:  # noqa: BLE001 -- failed voice requests must be visible in status
                self._voice_error = "这条识别文字未能完成对话，请检查模型或后台状态。"

    def _stale(self, revision: int, generation: int, voice_epoch: int | None) -> bool:
        return (self._closed or revision != self._revision or generation != self.manager.generation
                or (voice_epoch is not None and voice_epoch != self._voice_epoch))

    async def _control(self, intent: str, goal: str | None, revision: int, generation: int,
                       voice_epoch: int | None) -> tuple[str, bool]:
        if self._stale(revision, generation, voice_epoch):
            return "控制状态已变化，本条操作已取消，请重新说出指令。", False
        try:
            self.manager.ensure_accepting()
            if intent == "stop":
                self.cancel_actions()
                revision = self._revision
                self.cancel_voice()
                expected_generation = self.manager.generation + 1
                await self.manager.stop()
                if self._stale(revision, expected_generation, None):
                    return "停止请求之后收到新指令，本条确认已取消。", False
                return self.manager.release_error or "已停止 P2，输入已释放。", not bool(
                    self.manager.release_error)
            if intent == "follow":
                self.cancel_actions()
                revision = self._revision
                expected_generation = self.manager.generation + 1
                await self.manager.stop()
                if self._stale(revision, expected_generation, voice_epoch):
                    raise ControlError("跟随请求已被后续停止或关闭监听撤销")
                await self.manager.start(
                    StartRequest(mode="follow", coin_budget=0),
                    guard=lambda: not self._stale(revision, expected_generation, voice_epoch))
                if self._stale(revision, expected_generation + 1, voice_epoch):
                    raise ControlError("跟随请求已被后续停止或关闭监听撤销")
                return "P2 已开始跟随你，不会投币。", True
            if intent == "goal":
                if self.manager.status()["mode"] == "autonomous":
                    if hasattr(self.manager, "change_goal"):
                        await self.manager.change_goal(goal)
                    else:
                        self.manager.update_goal(goal)
                    if self._stale(revision, generation, voice_epoch):
                        return "控制状态已变化，本条操作已取消，请重新说出指令。", False
                    self.goal = goal
                    return "已调整自主目标，当前金币支配方式和决策设置保持不变。", True
                self.goal = goal
                if hasattr(self.manager, "memory"):
                    self.manager.memory.set_goal(goal, source="dialogue")
                return "目标已记下，点击“开始自主陪玩”后按此目标行动。", True
        except (ControlError, BridgeError) as exc:
            return str(exc), False
        return "仅对话，未执行游戏操作", True

    async def _explicit(self, text: str, source: str, voice_epoch: int | None, intent: str,
                        goal: str | None = None) -> dict:
        self._append("user", text, source)
        if intent == "goal":
            self.cancel_actions()
        result, succeeded = await self._control(
            intent, goal, self._revision, self.manager.generation, voice_epoch)
        if intent == "stop":
            reply = "停下了。你来接管，我先不动。" if succeeded else result
        elif intent == "follow":
            reply = f"听到了，跟着你。\n{result}" if succeeded else result
        else:
            reply = f"记下了：{goal}\n{result}" if succeeded else result
        message = self._append("assistant", reply, source, playable=succeeded)
        await self._publish(message)
        return {"reply": reply, "intent": intent, "goal": goal,
                "control_result": result, **self.public()}

    def _start_chat(self, request: _ChatRequest) -> None:
        self._active_chat = request
        self._chat_task = asyncio.create_task(self._run_chat(request))

    async def _run_chat(self, request: _ChatRequest) -> None:
        try:
            result = await self._respond(request)
            if not request.future.done():
                request.future.set_result(result)
        except asyncio.CancelledError:
            if not request.future.done():
                request.future.set_result(self._cancelled())
        except Exception as exc:  # noqa: BLE001 -- forward failures to this request's caller
            if not request.future.done():
                request.future.set_exception(exc)
        finally:
            self._active_chat = None
            self._chat_task = None
            while self._chat_queue and not self._closed:
                following = self._chat_queue.popleft()
                if not following.future.done():
                    self._start_chat(following)
                    break
            if request.future.done() and not request.future.cancelled():
                with suppress(Exception):
                    result = request.future.result()
                    if isinstance(result, dict):
                        result.update(self.public())

    async def send(self, text: str, *, source: str = "text", voice_epoch: int | None = None) -> dict:
        if self._closed:
            return self._cancelled()
        if voice_epoch is not None and voice_epoch != self._voice_epoch:
            return self._cancelled("语音监听已关闭，本条指令已取消。")
        intent = quick_intent(text)
        if intent in {"stop", "follow"}:
            return await self._explicit(text, source, voice_epoch, intent)
        goal = explicit_goal(text)
        if goal is None and (arrangement := natural_goal(text)) is not None:
            goal = merge_arrangement(arrangement, self.manager.goal or self.goal)
        if goal is not None:
            return await self._explicit(text, source, voice_epoch, "goal", goal)
        self.manager.ensure_accepting()
        if self._active_chat and len(self._chat_queue) >= self.MAX_QUEUED:
            raise ControlError("聊天等待队列已满（最多 3 条），请稍后再发；停止和跟随指令始终可用")
        request = _ChatRequest(text, source, self._revision, self.manager.generation, voice_epoch,
                               asyncio.get_running_loop().create_future(), asyncio.current_task())
        if self._active_chat:
            self._chat_queue.append(request)
        else:
            self._start_chat(request)
        try:
            return await request.future
        except asyncio.CancelledError:
            if request in self._chat_queue:
                self._chat_queue.remove(request)
            elif self._active_chat is request and self._chat_task:
                self._chat_task.cancel()
            raise

    async def _respond(self, request: _ChatRequest) -> dict:
        self.manager.ensure_accepting()
        if request.voice_epoch is not None and request.voice_epoch != self._voice_epoch:
            return self._cancelled("语音监听已关闭，本条指令已取消。")
        history = [{"role": item["role"], "content": item["content"]}
                   for item in self.messages[-12:]]
        if not history and hasattr(self.manager, "memory"):
            history = self.manager.memory.dialogue_history()
        self._append("user", request.text, request.source)
        state = None
        try:
            read_state = getattr(self.manager, "_state", self.manager.bridge.state)
            state = await read_state()
        except BridgeError:
            pass
        try:
            response = await self.manager.model.chat(
                request.text, history, state, self.manager.status())
        except ModelError as exc:
            reply = str(exc)
            self._append("assistant", reply, request.source)
            return {"reply": reply, "intent": "chat", "goal": None,
                    "control_result": "未执行游戏操作", **self.public()}
        reply, intent, goal = response.reply, response.intent, response.goal
        stale = self._stale(request.revision, request.generation, request.voice_epoch)
        playable = not stale
        result = "仅对话，未执行游戏操作"
        if intent != "chat":
            if _discussed_intent(request.text, intent) or (intent == "goal" and _discussed_goal(request.text)):
                result, playable = "这句话包含否定、引用或假设，未执行游戏操作。", False
            else:
                if intent == "goal" and not stale:
                    self.cancel_actions()
                    request.revision = self._revision
                result, playable = await self._control(
                    intent, goal, request.revision, request.generation, request.voice_epoch)
            reply = f"{reply}\n{result}"
        message = self._append("assistant", reply, request.source, playable=playable)
        if stale:
            message["stale"] = True
        await self._publish(message)
        if hasattr(self.manager, "memory") and not stale:
            self.manager.memory.record_dialogue(request.text, reply)
        return {"reply": reply, "intent": intent, "goal": goal,
                "control_result": result, **self.public()}

    async def close(self) -> None:
        self._closed = True
        self.cancel_actions()
        self.cancel_voice()
        for request in tuple(self._chat_queue):
            if not request.future.done():
                request.future.set_result(self._cancelled())
        self._chat_queue.clear()
        if self._active_chat and not self._active_chat.future.done():
            self._active_chat.future.set_result(self._cancelled())
        tasks = set(self._voice_tasks)
        for task in (self._worker, self._chat_task, self._activity_task):
            if task:
                tasks.add(task)
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._voice_tasks.clear()
        await self.voice.close()
