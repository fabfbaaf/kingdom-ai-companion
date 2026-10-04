"""Opt-in, factual event notices. No model requests and no authority to control P2."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from companion.bridge import BridgeError
from companion.gameplay_context import fresh


class NoticeService:
    def __init__(self, manager, conversation, *, clock: Callable[[], float] = time.monotonic):
        self.manager, self.conversation, self.clock = manager, conversation, clock
        self.enabled = False
        self.last_event: str | None = None
        self.last_message: str | None = None
        self._task: asyncio.Task | None = None
        self._closed = False
        self._generation = 0
        self._baseline: dict | None = None
        self._last_notice = float("-inf")
        self._seen: dict[str, float] = {}
        self._quiet_until = 0.0

    def status(self) -> dict:
        return {"enabled": self.enabled, "last_event": self.last_event,
                "last_message": self.last_message,
                "message": "仅游玩期间提醒重要变化，每条间隔至少 12 秒；同类事件 60 秒内不重复"
                if self.enabled else "主动交流未开启"}

    def dialogue_activity(self) -> None:
        self._quiet_until = self.clock() + 8

    async def configure(self, enabled: bool) -> dict:
        if self._closed:
            return self.status()
        if type(enabled) is not bool:
            raise ValueError("主动交流开关需要布尔值")
        self._generation += 1
        self.enabled = enabled
        self._baseline = None
        task, self._task = self._task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if enabled:
            self._task = asyncio.create_task(self._run(self._generation))
        return self.status()

    async def _run(self, generation: int) -> None:
        while self.enabled and generation == self._generation and not self._closed:
            try:
                await self.tick(generation)
            except BridgeError:
                self._baseline = None
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- notice failures must not affect gameplay
                self._baseline = None
            await asyncio.sleep(2)

    async def tick(self, generation: int | None = None) -> None:
        generation = self._generation if generation is None else generation
        if not self.enabled or self._closed or generation != self._generation:
            return
        control = self.manager.status()
        if not control.get("running"):
            if self._baseline is not None and control.get("error"):
                await self._emit("control_error", "行动已停止，后台报告需要检查连接或执行结果。", generation)
            self._baseline = None
            return
        # A new user control run starts a new baseline; old-session events cannot leak into it.
        state = await self.manager._state()
        if not self.enabled or generation != self._generation or not fresh(state):
            return
        lease = self.manager.lease
        if (lease is None or state.session_id != lease.session_id
                or state.control_epoch != lease.control_epoch or state.controlled_player_id != 1):
            self._baseline = None
            return
        player = state.player(1)
        if player is None:
            self._baseline = None
            return
        world = state.world or {}
        enemies = world.get("nearby_enemies")
        if isinstance(enemies, list):
            enemies = len(enemies)
        sample = {"session": state.session_id, "generation": self.manager.generation,
                  "seq": state.observation_seq, "enemies": enemies if type(enemies) is int else None,
                  "sprint": player.can_sprint, "night": world.get("is_night"),
                  "coins": control.get("coin_budget_remaining"),
                  "paused": world.get("is_paused")}
        previous = self._baseline
        if (previous is None or (previous["session"], previous["generation"]) !=
                (sample["session"], sample["generation"])):
            self._baseline = sample
            return
        if sample["seq"] <= previous["seq"]:
            return
        self._baseline = sample
        event = None
        if (type(sample["enemies"]) is int and type(previous["enemies"]) is int
                and sample["enemies"] > previous["enemies"] and sample["enemies"] > 0):
            event = ("enemy", "附近敌人的观测数量增加了，留意营地。我会继续按当前任务行动。")
        elif sample["paused"] is True and previous["paused"] is False:
            event = ("paused", "游戏已暂停，我先等你。")
        elif sample["sprint"] is False and previous["sprint"] is True:
            event = ("fatigue", "坐骑现在不能疾跑，我先按步行处理。别催它。")
        elif sample["night"] is True and previous["night"] is False:
            event = ("night", "天黑了，留意营地防守。")
        elif (type(sample["coins"]) is int and type(previous["coins"]) is int
              and sample["coins"] == 0 and previous["coins"] > 0):
            event = ("budget", "本次投币额度已经用完，接下来不会继续花钱。")
        if event:
            await self._emit(*event, generation)

    async def _emit(self, event: str, message: str, generation: int) -> None:
        now = self.clock()
        chat = self.conversation.public()
        if (generation != self._generation or not self.enabled or self._closed
                or chat.get("busy") or chat.get("queued")
                or now < self._quiet_until or now - self._last_notice < 12
                or now - self._seen.get(event, float("-inf")) < 60):
            return
        self._last_notice = now
        self._seen[event] = now
        self.last_event, self.last_message = event, message
        await self.conversation.notify(message)

    async def close(self) -> None:
        await self.configure(False)
        self._closed = True
