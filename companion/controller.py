"""P2 control with cancellation, heartbeats and non-blocking action feedback."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from companion.bridge import BridgeClient, BridgeError
from companion.config import ModelConfigStore, default_config_path
from companion.contracts import Decision, GameState, StartRequest
from companion.gameplay_context import action_summary, fresh
from companion.memory import GameplayMemory
from companion.model import ModelClient, ModelError
from companion.territory import territory_context


class ControlError(RuntimeError):
    pass


class ActionUnavailable(ControlError):
    """No input was dispatched; the next observation may offer a different action."""


class GoalChanged(Exception):
    """A decision awaiting dispatch belongs to a superseded conversation goal."""


@dataclass(frozen=True)
class ControlLease:
    control_id: str
    session_id: str
    control_epoch: int

    def wire(self) -> dict:
        return {"control_id": self.control_id, "session_id": self.session_id,
                "control_epoch": self.control_epoch}


@dataclass(frozen=True)
class MotionPlan:
    decision: Decision
    state: GameState
    goal_revision: int


class ActionJournal:
    """Persist in-flight action information for diagnostics, without a review gate."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_config_path().with_name("session.local.json")
        self.pending: dict | None = None
        try:
            if self.path.is_file():
                if self.path.stat().st_size > 32_000:
                    raise ValueError("oversized ledger")
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or (data.get("pending") is not None
                                                 and not isinstance(data["pending"], dict)):
                    raise ValueError("invalid ledger")
                if data.get("pending"):
                    self.pending = data["pending"]
        except (OSError, ValueError, TypeError):
            self.pending = {"operation": "unknown", "reason": "旧动作记录无法恢复"}

    def save(self, pending: dict | None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".session-{secrets.token_hex(8)}.tmp")
        try:
            temporary.write_text(json.dumps({"pending": pending}, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self.pending = pending


def playable(state: GameState, operation: str | None = None) -> None:
    age = (datetime.now(UTC) - state.captured_at.astimezone(UTC)).total_seconds()
    if age > 2.5 or age < -2:
        raise ControlError("游戏状态已过期，已停止控制")
    if not (state.ready or state.ui_ready and operation in {None, "map"}) or not state.coop or state.controlled_player_id != 1:
        raise ControlError("P2 尚不可控制，请进入本地合作存档并核对模组诊断")
    if state.player(1) is None:
        raise ControlError("未找到第二位君主，已停止控制")
    if operation in {"move", "move_to"} and state.player(1).transaction_pending is not False:
        raise ControlError("P2 原生交易尚未结算或状态不可读，不能接管移动")
    if operation and operation not in state.capabilities:
        raise ControlError("当前游戏没有提供该操作能力")


class ControlManager:
    def __init__(self, bridge: BridgeClient, model: ModelClient, store: ModelConfigStore,
                 journal: ActionJournal | None = None) -> None:
        self.bridge = bridge
        self.model = model
        self.store = store
        self.memory = GameplayMemory(store.path.with_name("memory.local.json"))
        self.journal = journal or ActionJournal()
        # Old uncertain-action records no longer require a manual unlock.
        if self.journal.pending is not None:
            self.journal.save(None)
        self.task: asyncio.Task | None = None
        self.heartbeat_task: asyncio.Task | None = None
        self.generation = 0
        self.mode = "idle"
        self.session_id: str | None = None
        self.lease: ControlLease | None = None
        self._stopping = 0
        self.coin_budget: int | None = 0
        self.spending_mode = "budgeted"
        self.decisions_remaining = 0
        self.continuous = False
        self.play_style = "cooperate"
        self.decisions_made = 0
        self.phase = "idle"
        self.phase_started = time.monotonic()
        self.payment_cooldowns: dict[str, float] = {}
        self.last_result: dict | None = None
        self.error: str | None = None
        self.release_error: str | None = None
        self.accepting_control = True
        self._lifecycle_lock = asyncio.Lock()
        self.goal = ""
        self.goal_revision = 0
        self.planner_task: asyncio.Task | None = None
        self.decision_task: asyncio.Task | None = None
        self.latest_state: GameState | None = None
        self.current_decision: Decision | None = None
        self._expected_transition = False
        self._transitioning = False
        self.recent_actions: deque[dict] = deque(maxlen=6)
        if hasattr(model, "set_context_provider"):
            model.set_context_provider(self.gameplay_context)

    async def _state(self) -> GameState:
        state = await self.bridge.state()
        if self.latest_state is not None and self.latest_state.session_id != state.session_id:
            self.recent_actions.clear()
            self.last_result = None
            self.payment_cooldowns.clear()
        self.latest_state = state
        self.memory.observe(state)
        if self.memory.planner.goal != self.goal:
            self.memory.set_goal(self.goal)
            if fresh(state) and (state.ready or state.ui_ready):
                self.memory.planner.observe(state)
        await self.memory.flush()
        self.payment_cooldowns = {target: until for target, until in self.payment_cooldowns.items()
                                  if until > time.monotonic()}
        return state

    def gameplay_context(self) -> dict:
        state = self.latest_state
        current = None
        if self.current_decision is not None:
            current = action_summary({**self.current_decision.model_dump(), "status": "sent",
                                      "message": "动作已派发，等待游戏回执"})
        return {"control_goal": self.goal, "mode": self.mode, "continuous": self.continuous,
                "play_style": self.play_style if self.mode == "autonomous" else None,
                "spending_mode": self.spending_mode,
                "last_action": action_summary(self.last_result), "current_action": current,
                "recent_actions": list(self.recent_actions),
                "memory": self.memory.public(), "plan": self.memory.planner.public(),
                "territory": territory_context(state) if fresh(state) else None,
                "payment_cooldowns": [{"target_id": target, "remaining_seconds": round(until - time.monotonic(), 1)}
                                      for target, until in self.payment_cooldowns.items() if until > time.monotonic()],
                "scene": state.scene if fresh(state) else None,
                "observed_at": state.captured_at.isoformat() if state else None,
                "stale": not fresh(state),
                "message": "当前动作执行中，下一步依据当前目标判断" if current else
                "依据最新游戏观测和动作反馈回答；输入结束不代表建设已完成"}

    def status(self) -> dict:
        return {"mode": self.mode, "running": bool(self.task and not self.task.done()),
                "session_id": self.session_id, "coin_budget_remaining": self.coin_budget,
                "spending_mode": self.spending_mode,
                "decisions_remaining": None if self.continuous else self.decisions_remaining,
                "continuous": self.continuous, "decisions_made": self.decisions_made,
                "phase": self.phase,
                "phase_seconds": round(max(0, time.monotonic() - self.phase_started), 1),
                "play_style": self.play_style if self.mode == "autonomous" else None,
                "last_result": self.last_result,
                "error": self.error, "release_error": self.release_error,
                "accepting_control": self.accepting_control,
                "stopping": bool(self._stopping),
                "control_id": self.lease.control_id if self.lease else None,
                "control_epoch": self.lease.control_epoch if self.lease else None,
                "needs_review": False,
                "pending_action": self.journal.pending, "goal": self.goal,
                "context": self.gameplay_context()}

    def _phase(self, phase: str) -> None:
        if phase != self.phase:
            self.phase, self.phase_started = phase, time.monotonic()

    def update_goal(self, goal: str) -> None:
        self.ensure_accepting()
        if self.mode != "autonomous" or not self.task or self.task.done() or self._stopping:
            raise ControlError("请先开始自主陪玩，再通过对话调整目标")
        self.goal = goal
        self.memory.set_goal(goal, source="dialogue")
        self.goal_revision += 1
        if self.planner_task and not self.planner_task.done():
            self.planner_task.cancel()
        if self.decision_task and not self.decision_task.done():
            self.decision_task.cancel()

    async def change_goal(self, goal: str) -> None:
        # Never release/reacquire control or reset remaining budgets for a target update.
        self.update_goal(goal)

    async def _decide(self, generation: int, revision: int, state: GameState) -> Decision:
        task = asyncio.create_task(self.model.decide(state, goal=self.goal,
                                                   coin_budget=self.coin_budget))
        self.decision_task = task
        try:
            return await task
        except asyncio.CancelledError:
            self._active(generation)
            self._current_goal(revision)
            raise
        finally:
            if self.decision_task is task:
                self.decision_task = None

    def _current_goal(self, revision: int | None) -> None:
        if revision is not None and revision != self.goal_revision:
            raise GoalChanged

    def _active(self, generation: int) -> None:
        if generation != self.generation or self.mode == "idle":
            raise asyncio.CancelledError

    def _available(self) -> None:
        self.ensure_accepting()
        if self._stopping:
            raise ControlError("正在停止并撤销控制租约，请等待输入释放确认")
        if self.task and not self.task.done():
            raise ControlError("当前有操作正在进行，请先停止")

    def ensure_accepting(self) -> None:
        if not self.accepting_control:
            raise ControlError("后台正在退出，不能开始新操作")

    async def _release(self) -> None:
        try:
            await asyncio.wait_for(self.bridge.stop(), timeout=3.5)
            self.release_error = None
        except (BridgeError, TimeoutError):
            self.release_error = "未能确认输入已释放，请在游戏中按 F8 接管；桥接看门狗会撤销输入"

    async def stop(self) -> dict:
        # Invalidate before awaiting any network call or lock. Pending model output cannot dispatch.
        self.generation += 1
        self.mode = "idle"
        self._phase("idle")
        self.continuous = False
        self.lease = None
        self._stopping += 1
        task, heartbeat = self.task, self.heartbeat_task
        for current in (task, heartbeat, self.planner_task, self.decision_task):
            if current and not current.done():
                current.cancel()
        try:
            await self._release()
            if task or heartbeat:
                await asyncio.gather(*(current for current in (task, heartbeat) if current),
                                     return_exceptions=True)
        finally:
            self._stopping -= 1
            self.journal.save(None)
        return self.status()

    async def prepare_shutdown(self) -> None:
        self.accepting_control = False
        await self.stop()

    async def acknowledge(self) -> dict:
        if self.task and not self.task.done():
            raise ControlError("请先停止再核对")
        # Human review explicitly authorizes clearing an uncertain action, not replaying it.
        await self._state()
        self.journal.save(None)
        self.error = None
        return self.status()

    @asynccontextmanager
    async def pause_control(self):
        """Serialize game setup with starts, releasing control before any setup action."""
        async with self._lifecycle_lock:
            self.ensure_accepting()
            await self.stop()
            self.ensure_accepting()
            yield

    async def start(self, request: StartRequest, *,
                    guard: Callable[[], bool] | None = None) -> dict:
        async with self._lifecycle_lock:
            if guard is not None and not guard():
                raise ControlError("开始操作已被后续停止或关闭监听撤销")
            self._available()
            epoch = self.generation
            if request.mode == "autonomous" and not self.store.public()["execution_configured"]:
                raise ControlError("自主模式需要先保存模型配置；跟随模式无需模型")
            state = await self._state()
            playable(state, "move")
            if request.mode == "follow" and (state.player(0) is None or state.player(0).x is None):
                raise ControlError("无法读取 P1 位置，暂不能跟随")
            if state.player(1).x is None:
                raise ControlError("无法读取 P2 位置，暂不能移动")
            if epoch != self.generation or (guard is not None and not guard()):
                raise ControlError("开始操作已被停止请求撤销")
            lease = ControlLease(str(uuid.uuid4()), state.session_id, state.control_epoch)
            await self.bridge.heartbeat(**lease.wire())
            if epoch != self.generation or (guard is not None and not guard()):
                raise ControlError("开始操作已被停止请求撤销")
            self.generation += 1
            generation = self.generation
            self.mode, self.session_id = request.mode, state.session_id
            self.lease = lease
            self.spending_mode = request.spending_mode if request.mode == "autonomous" else "budgeted"
            self.coin_budget = (None if self.spending_mode == "wallet" else request.coin_budget
                                ) if request.mode == "autonomous" else 0
            self.decisions_remaining = request.decision_budget
            self.continuous = request.mode == "autonomous" and request.continuous
            self.play_style = request.play_style if request.mode == "autonomous" else "cooperate"
            self.decisions_made = 0
            self._expected_transition = self._transitioning = False
            self.payment_cooldowns.clear()
            self.goal = request.goal
            self.memory.set_goal(request.goal)
            self.memory.planner.observe(state)
            self.goal_revision += 1
            self._phase("observing")
            self.error, self.release_error = None, None
            self.heartbeat_task = asyncio.create_task(self._heartbeat(generation, lease))
            self.task = asyncio.create_task(self._run(generation, request, lease))
            return self.status()

    async def manual(self, decision: Decision) -> dict:
        if decision.operation == "stop":
            return await self.stop()
        async with self._lifecycle_lock:
            self._available()
            epoch = self.generation
            state = await self._state()
            playable(state, decision.operation)
            if epoch != self.generation:
                raise ControlError("手动操作已被停止请求撤销")
            lease = ControlLease(str(uuid.uuid4()), state.session_id, state.control_epoch)
            await self.bridge.heartbeat(**lease.wire())
            if epoch != self.generation:
                raise ControlError("手动操作已被停止请求撤销")
            self.generation += 1
            generation = self.generation
            self.mode, self.session_id = "manual", state.session_id
            self.continuous = False
            self.spending_mode = "budgeted"
            self.lease = lease
            self.coin_budget = None if decision.operation in {"pay", "pay_coin"} else 0
            self.error = None
            self.heartbeat_task = asyncio.create_task(self._heartbeat(generation, lease))
            self.task = asyncio.create_task(self._manual_run(generation, decision))
            task = self.task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled() or generation != self.generation:
                return {"status": "cancelled", "message": "操作已停止"}
            await self.stop()
            raise

    async def _heartbeat(self, generation: int, lease: ControlLease) -> None:
        try:
            while generation == self.generation and self.mode != "idle":
                await self.bridge.heartbeat(**lease.wire())
                await asyncio.sleep(0.5)
        except BridgeError:
            if generation != self.generation:
                return
            if self.continuous:
                try:
                    state = await self._state()
                    self._active(generation)
                    if self._is_transition(state, lease):
                        self._transitioning = True
                        return
                except (BridgeError, ControlError):
                    pass
            # Older native bridges revoke their lease on a failed payment. Let
            # the action loop renew that lease; F8 cancellation is not renewed.
            pending = self.journal.pending
            if pending is not None:
                try:
                    receipt = await self.bridge.receipt(pending["action_id"])
                    state = await self._state()
                    if (generation == self.generation and receipt.status == "failed"
                            and receipt.session_id == lease.session_id
                            and state.ready and state.session_id == lease.session_id):
                        return
                except (BridgeError, KeyError):
                    pass
            self.error = "游戏心跳失联，已停止控制"
            self.generation += 1
            self.mode = "idle"
            self._phase("idle")
            self.continuous = False
            self.lease = None
            self._stopping += 1
            task = self.task
            if task and not task.done():
                task.cancel()
            try:
                await self._release()
                if task:
                    await asyncio.gather(task, return_exceptions=True)
            finally:
                self._stopping -= 1

    async def _finish(self, generation: int) -> None:
        # _run owns and drains its planner before completing; never cancel another run's task.
        if generation != self.generation:
            return
        self.mode = "idle"
        self._phase("idle")
        self.continuous = False
        self.lease = None
        heartbeat = self.heartbeat_task
        if heartbeat and not heartbeat.done():
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        await self._release()
        self.journal.save(None)

    def _is_transition(self, state: GameState, lease: ControlLease) -> bool:
        control = (state.world or {}).get("control") or {}
        if control.get("manual_takeover") is True or (state.world or {}).get("campaign", {}).get("lost") is True:
            return False
        return (control.get("transition") is True or self._transitioning
                or self._expected_transition and state.session_id != lease.session_id)

    async def _resume_transition(self, generation: int, previous: ControlLease) -> ControlLease:
        self._phase("transitioning")
        self._transitioning = True
        heartbeat = self.heartbeat_task
        if heartbeat and not heartbeat.done():
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        while True:
            self._active(generation)
            state = await self._state()
            self._active(generation)
            control = (state.world or {}).get("control") or {}
            if control.get("manual_takeover") is True:
                raise ControlError("F8 人工接管，跨岛恢复已取消")
            if (state.world or {}).get("campaign", {}).get("lost") is True:
                raise ControlError("当前战役已失败，停止自主游玩")
            if state.ready or state.ui_ready:
                playable(state)
                lease = ControlLease(str(uuid.uuid4()), state.session_id, state.control_epoch)
                await self.bridge.heartbeat(**lease.wire())
                self._active(generation)
                self.lease, self.session_id = lease, state.session_id
                self._expected_transition = self._transitioning = False
                self.journal.save(None)
                self.heartbeat_task = asyncio.create_task(self._heartbeat(generation, lease))
                return lease
            if not self._expected_transition and control.get("transition") is not True:
                raise ControlError(state.reason or "游戏已退出正常游玩，停止控制")
            self.last_result = {"operation": "sail", "status": "waiting", "message": "等待游戏完成跨岛/加载，保留当前通关目标"}
            await asyncio.sleep(0.25)

    async def _recover_action(self, generation: int, previous: ControlLease,
                              *, failed: bool) -> ControlLease:
        """Cancel uncertain input, refresh facts, and continue the same user run."""
        self._active(generation)
        self._phase("recovering")
        heartbeat = self.heartbeat_task
        if heartbeat and not heartbeat.done():
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        state = await self._state()
        self._active(generation)
        playable(state)
        allowed = {previous.control_epoch, previous.control_epoch + 1} if failed else {previous.control_epoch}
        if state.session_id != previous.session_id or state.control_epoch not in allowed:
            raise ControlError("游戏会话或控制已撤销，请重新开始")
        epoch = state.control_epoch
        await self.bridge.stop()
        self._active(generation)
        state = await self._state()
        self._active(generation)
        playable(state)
        if state.session_id != previous.session_id or state.control_epoch != epoch + 1:
            raise ControlError("恢复期间游戏控制已撤销，请重新开始")
        lease = ControlLease(str(uuid.uuid4()), state.session_id, state.control_epoch)
        await self.bridge.heartbeat(**lease.wire())
        self._active(generation)
        self.lease = lease
        self.journal.save(None)
        self.heartbeat_task = asyncio.create_task(self._heartbeat(generation, lease))
        return lease

    async def _manual_run(self, generation: int, decision: Decision) -> dict:
        try:
            return await self._perform(generation, decision)
        except (BridgeError, ControlError) as exc:
            self.error = str(exc)
            return {"status": "failed", "message": self.error}
        finally:
            await self._finish(generation)

    async def _plan_motion(self, generation: int, lease: ControlLease, before: GameState,
                           decision: Decision, revision: int, delay: float) -> MotionPlan | None:
        await asyncio.sleep(delay)
        self._active(generation)
        self._current_goal(revision)
        state = await self._state()
        self._active(generation)
        self._current_goal(revision)
        playable(state)
        if (state.session_id != lease.session_id or state.control_epoch != lease.control_epoch
                or state.observation_seq <= before.observation_seq):
            return None
        if not self.continuous and self.decisions_remaining <= 0:
            return None
        self._take_decision()
        planned = await self.model.decide(
            state, goal=self.goal, coin_budget=self.coin_budget,
            in_flight={"direction": decision.direction, "sprint": decision.sprint,
                       "remaining_ms": max(0, decision.duration_ms - int(delay * 1000))})
        self._active(generation)
        self._current_goal(revision)
        return MotionPlan(planned, state, revision)

    def _take_decision(self) -> None:
        if not self.continuous:
            if self.decisions_remaining <= 0:
                raise ControlError("本次模型决策预算已用完")
            self.decisions_remaining -= 1
        self.decisions_made += 1

    async def _wait_autonomous(self, generation: int, interval: float, message: str) -> None:
        self._phase("waiting")
        self.last_result = {"operation": "stop", "status": "waiting", "message": message}
        # All previous bounded input has completed; retain the lease, with no new action.
        # Even a prefetched stop must leave a paced interval before observing again.
        await asyncio.sleep(interval)
        self._active(generation)

    def _usable_plan(self, plan: MotionPlan, state: GameState) -> bool:
        age = (datetime.now(UTC) - plan.state.captured_at.astimezone(UTC)).total_seconds()
        if (plan.goal_revision != self.goal_revision or not -2 <= age <= 2.5
                or plan.state.session_id != state.session_id
                or plan.state.control_epoch != state.control_epoch
                or plan.decision.operation not in {"move", "stop"}
                or set(plan.state.capabilities) != set(state.capabilities)):
            return False
        for player in plan.state.players:
            if self.play_style == "independent" and player.player_id != 1:
                continue
            current = state.player(player.player_id)
            if current is None or (player.coins, player.crown, player.transaction_pending) != (
                    current.coins, current.crown, current.transaction_pending):
                return False
        old_world, new_world = plan.state.world or {}, state.world or {}
        return all(old_world.get(key) == new_world.get(key)
                   for key in ("is_night", "is_paused", "nearby_enemies"))

    async def _run(self, generation: int, request: StartRequest, lease: ControlLease) -> None:
        planner = None
        plan = None
        model_failures = 0
        last_model_call = float("-inf")
        try:
            while True:
                goal_revision = None
                self._active(generation)
                self._phase("observing")
                state = await self._state()
                self._active(generation)
                if self.continuous and self._is_transition(state, lease):
                    if planner is not None:
                        planner.cancel()
                        await asyncio.gather(planner, return_exceptions=True)
                        planner = self.planner_task = None
                    plan = None
                    lease = await self._resume_transition(generation, lease)
                    continue
                playable(state)
                if (state.session_id != lease.session_id
                        or state.control_epoch != lease.control_epoch):
                    raise ControlError("游戏会话或控制租约已变化，请重新开始")
                if request.mode == "follow":
                    first, second = state.player(0), state.player(1)
                    if first is None or first.x is None or second.x is None:
                        raise ControlError("玩家位置缺失，跟随已停止")
                    distance = first.x - second.x
                    if abs(distance) <= request.follow_distance:
                        await asyncio.sleep(0.25)
                        continue
                    decision = Decision(operation="move", direction="right" if distance > 0 else "left",
                                        duration_ms=250,
                                        sprint=abs(distance) > max(8, request.follow_distance * 2)
                                        and "sprint" in state.capabilities
                                        and second.can_sprint is True)
                else:
                    if (state.world or {}).get("campaign", {}).get("completed") is True:
                        self.last_result = {"operation": "stop", "status": "completed", "message": "游戏已报告当前战役全部岛屿完成，通关目标达成"}
                        return
                    goal_revision = self.goal_revision
                    if plan is not None and self._usable_plan(plan, state):
                        decision = plan.decision
                        plan = None
                    else:
                        plan = None
                        if not self.continuous and self.decisions_remaining <= 0:
                            raise ControlError("本次模型决策预算已用完")
                        # Cadence applies between requests, rather than adding an idle gap
                        # after every movement. Bounded input expires while a slow model thinks.
                        await asyncio.sleep(max(0, request.decision_interval_seconds
                                                - (asyncio.get_running_loop().time() - last_model_call)))
                        self._active(generation)
                        if goal_revision != self.goal_revision:
                            continue
                        state = await self._state()
                        self._active(generation)
                        playable(state)
                        self._take_decision()
                        last_model_call = asyncio.get_running_loop().time()
                        try:
                            self._phase("deciding")
                            decision = await self._decide(generation, goal_revision, state)
                        except GoalChanged:
                            last_model_call = float("-inf")
                            continue
                        except ModelError as exc:
                            if not self.continuous or not exc.retryable:
                                raise
                            model_failures += 1
                            delay = min(15, request.decision_interval_seconds * 2 ** min(model_failures - 1, 6))
                            await self._wait_autonomous(generation, delay,
                                                        f"本轮决策未采用：{exc}；{delay:g}秒后重新决策")
                            continue
                        model_failures = 0
                    self._active(generation)
                    if goal_revision != self.goal_revision:
                        continue
                    if decision.plan is not None:
                        self.memory.planner.propose(decision.plan, state)
                if decision.operation == "stop":
                    if self.continuous:
                        await self._wait_autonomous(generation, request.decision_interval_seconds,
                                                    "本轮暂不行动，持续观察后自主决定下一步")
                        continue
                    self.last_result = {"status": "stopped", "message": "模型选择停止本次自主游玩"}
                    return
                if decision.operation in {"move", "move_to"} and not request.allow_sprint:
                    decision = decision.model_copy(update={"sprint": False})
                try:
                    async def prepare_next(before: GameState, dispatched: Decision,
                                           revision: int | None = goal_revision,
                                           active_lease: ControlLease = lease) -> None:
                        nonlocal planner, last_model_call
                        if (request.mode != "autonomous" or dispatched.operation != "move"
                                or dispatched.duration_ms < 1000
                                or (not self.continuous and self.decisions_remaining <= 0)):
                            return
                        # Sample live state near the end of a movement, not its starting frame.
                        delay = max(0.0, dispatched.duration_ms / 1000 - 1.5,
                                    request.decision_interval_seconds
                                    - (asyncio.get_running_loop().time() - last_model_call))
                        planner = asyncio.create_task(self._plan_motion(
                            generation, active_lease, before, dispatched, revision, delay))
                        self.planner_task = planner
                        last_model_call = asyncio.get_running_loop().time() + delay

                    result = await self._perform(generation, decision, goal_revision=goal_revision,
                                                 on_running=prepare_next)
                except GoalChanged:
                    lease = self.lease or lease
                    continue
                except ActionUnavailable as exc:
                    self.memory.record_result({"operation": decision.operation,
                                               "status": "unavailable", "message": str(exc)})
                    if not self.continuous:
                        raise
                    await self._wait_autonomous(generation, request.decision_interval_seconds,
                                                f"本轮未派发动作：{exc}；重新观察选择下一步")
                    continue
                if result["status"] == "cancelled":
                    state = await self._state()
                    if self.continuous and self._is_transition(state, lease):
                        self._transitioning = True
                        continue
                    raise ControlError("游戏已取消当前控制")
                if result["status"] == "transition":
                    self._transitioning = True
                    continue
                if result["status"] != "completed":
                    if planner is not None:
                        planner.cancel()
                        await asyncio.gather(planner, return_exceptions=True)
                        planner = self.planner_task = None
                    if request.mode != "autonomous" or not self.continuous:
                        return
                    lease = await self._recover_action(generation, lease,
                                                       failed=result["status"] == "failed")
                    # Keep the failed/unknown feedback visible to the next model decision.
                    await asyncio.sleep(request.decision_interval_seconds)
                    continue
                if planner is not None:
                    try:
                        self._phase("deciding")
                        plan = await planner
                        model_failures = 0
                    except ModelError as exc:
                        if not self.continuous or not exc.retryable:
                            raise
                        model_failures += 1
                        delay = min(15, request.decision_interval_seconds * 2 ** min(model_failures - 1, 6))
                        await self._wait_autonomous(generation, delay,
                                                    f"下一步决策未采用：{exc}；{delay:g}秒后重新决策")
                        plan = None
                    except (GoalChanged, asyncio.CancelledError):
                        self._active(generation)
                        plan = None
                    finally:
                        if self.planner_task is planner:
                            self.planner_task = None
                        planner = None
                if request.mode == "follow":
                    await asyncio.sleep(0.05)
        except (BridgeError, ControlError, ModelError) as exc:
            self.error = str(exc)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - stop safely without exposing provider or process details
            self.error = "控制过程中发生异常，已停止；请重新检查连接"
        finally:
            if planner is not None:
                planner.cancel()
                await asyncio.gather(planner, return_exceptions=True)
                if self.planner_task is planner:
                    self.planner_task = None
            await self._finish(generation)

    async def _perform(self, generation: int, decision: Decision, *,
                       goal_revision: int | None = None,
                       on_running: Callable[[GameState, Decision], Awaitable[None]] | None = None
                       ) -> dict:
        self._active(generation)
        self._current_goal(goal_revision)
        lease = self.lease
        if lease is None:
            raise ControlError("当前没有已授予的控制租约，不能派发操作")
        before = await self._state()
        self._active(generation)
        self._current_goal(goal_revision)
        if before.ui_ready and not before.ready and decision.operation != "map":
            raise ActionUnavailable("世界仍在地图/菜单过渡中，请等待恢复或选择地图动作")
        if decision.operation in {"move", "move_to"} and before.player(1) is not None and before.player(1).transaction_pending is not False:
            raise ActionUnavailable("P2 原生交易仍在处理中，等待下一帧再选择动作")
        playable(before, decision.operation)
        if (before.session_id != lease.session_id
                or before.control_epoch != lease.control_epoch):
            raise ControlError("游戏会话或控制租约已变化，旧操作已撤销")
        player = before.player(1)
        if decision.operation in {"move", "move_to"} and player.x is None:
            raise ControlError("P2 位置不可读，不能核验移动")
        if decision.operation == "move":
            # Compatibility is explicit: an older bridge cannot accept long moves or sprint.
            decision = decision.model_copy(update={
                "duration_ms": decision.duration_ms if "extended_world" in before.capabilities else
                min(decision.duration_ms, 5000 if "move_long" in before.capabilities else 1000),
                "sprint": decision.sprint and "sprint" in before.capabilities
                          and ("extended_world" in before.capabilities or player.can_sprint is True),
            })
        payment = decision.operation in {"pay", "pay_coin"}
        price = None
        if payment:
            if self.mode == "autonomous" and self.payment_cooldowns.get(decision.target_id, 0) > time.monotonic():
                raise ActionUnavailable("该付款对象刚失败，暂时避开并选择其他目标或巡视")
            target = player.current_payable
            if target is None or target.target_id != decision.target_id:
                raise ActionUnavailable("当前支付对象已变化，未支付")
            if target.price is None or target.currency is None or target.can_pay is not True:
                raise ActionUnavailable("当前目标价格、币种或可支付状态未确认，未支付")
            price = target.price
            if price < 1:
                raise ActionUnavailable("当前价格不可用，未支付")
            if decision.operation == "pay_coin" and price != 1:
                raise ActionUnavailable("该目标需要完整付款，不能用单枚操作")
            if player.transaction_pending is not False:
                raise ControlError("游戏上一笔交易尚未结算或状态不可读，未支付")
            if target.currency == "coins" and self.coin_budget is not None and self.coin_budget < price:
                raise ActionUnavailable("投币预算不足，未投币")
            balance = player.coins if target.currency == "coins" else (player.currencies or {}).get(target.currency)
            if balance is None or balance < price:
                raise ActionUnavailable("P2 对应货币不足或无法读取，未支付")
            if decision.currency is not None and decision.currency != target.currency:
                raise ActionUnavailable("支付币种与当前目标不匹配")
            decision = decision.model_copy(update={"currency": target.currency})
        self._active(generation)
        self._current_goal(goal_revision)
        body = {"action_id": str(uuid.uuid4()), **lease.wire(), "player_id": 1,
                **decision.model_dump(exclude_none=True)}
        if payment:
            body["max_coins"] = price
        # Reserve before dispatch. Neither a timeout nor restart refunds an uncertain payment.
        self.journal.save(body)
        if payment and target.currency == "coins" and self.coin_budget is not None:
            self.coin_budget -= price
        self._active(generation)
        try:
            self.current_decision = decision
            self._phase("paying" if payment else "moving" if decision.operation in {"move", "move_to"} else "interacting")
            if decision.operation == "sail" or decision.operation == "map" and decision.map_action == "confirm":
                self._expected_transition = True
            receipt = await self.bridge.command(body)
            deadline = asyncio.get_running_loop().time() + (
                (player.payment_timeout_ms or 10000) / 1000 + 2.5 if payment else
                decision.duration_ms / 1000 + 3 if decision.operation == "move" else
                decision.amount * 0.15 + 3 if decision.operation == "drop" else
                5 if decision.operation != "move_to" else float("inf"))
            planned = False
            while receipt.status in {"queued", "running"}:
                self._active(generation)
                if goal_revision is not None and goal_revision != self.goal_revision:
                    await self._recover_action(generation, lease, failed=False)
                    raise GoalChanged
                if receipt.status == "running" and not planned and on_running is not None:
                    await on_running(before, decision)
                    planned = True
                if asyncio.get_running_loop().time() >= deadline:
                    raise ControlError("动作反馈超时")
                await asyncio.sleep(0.1)
                receipt = await self.bridge.receipt(body["action_id"])
            self._active(generation)
            if receipt.action_id != body["action_id"] or receipt.session_id != before.session_id:
                raise ControlError("动作回执不属于当前请求或会话")
            after = await self._state()
            self._active(generation)
            if self.continuous and self._is_transition(after, lease):
                self.journal.save(None)
                return {"operation": decision.operation, "status": "transition", "message": "原生游戏正在切换场景，等待恢复"}
            playable(after)
            after_player = after.player(1)
            if after.session_id != before.session_id:
                raise ControlError("游戏会话已变化，旧动作不会继续")
            result = {"action_id": body["action_id"], "operation": decision.operation,
                      "status": receipt.status,
                      "message": "游戏输入已结束，继续依据实时状态决策" if receipt.status == "completed"
                      else receipt.message or "本轮动作未完成，重新观察后选择下一步",
                      "before": {"x": player.x, "coins": player.coins},
                      "after": {"x": after_player.x, "coins": after_player.coins}}
            if receipt.effect is not None:
                result["effect"] = receipt.effect
            for key in ("currency", "amount", "ability", "ability_action", "map_action", "land", "target_x"):
                if (value := getattr(decision, key)) is not None:
                    result[key] = value
            if payment:
                result["target_id"] = decision.target_id
                if receipt.status in {"failed", "unknown"}:
                    self.payment_cooldowns[decision.target_id] = time.monotonic() + 20
            if decision.operation == "move":
                result.update(direction=decision.direction, sprint=decision.sprint,
                              duration_ms=decision.duration_ms)
            if receipt.status in {"completed", "cancelled"}:
                self.journal.save(None)
            self.last_result = result
            summary = action_summary(result)
            if summary is not None:
                self.recent_actions.append(summary)
            self.memory.record_result(result)
            return result
        except (BridgeError, ControlError) as exc:
            if self.continuous:
                try:
                    state = await self._state()
                    self._active(generation)
                    if self._is_transition(state, lease):
                        self.journal.save(None)
                        return {"operation": decision.operation, "status": "transition", "message": "等待原生游戏场景切换后继续"}
                except BridgeError:
                    pass
            self.last_result = {"action_id": body["action_id"], "operation": decision.operation,
                                "status": "unknown", "message": f"动作反馈未返回：{exc}；重新观察"}
            if payment:
                self.last_result["target_id"] = decision.target_id
                self.payment_cooldowns[decision.target_id] = time.monotonic() + 20
            self.recent_actions.append(action_summary(self.last_result))
            self.memory.record_result(self.last_result)
            return self.last_result
        except asyncio.CancelledError:
            self.last_result = {"action_id": body["action_id"], "operation": decision.operation,
                                "status": "unknown", "message": "已要求停止；本次派发结果需人工核对"}
            raise
        finally:
            self.current_decision = None

    async def close(self) -> None:
        self.accepting_control = False
        if self.task or self.heartbeat_task or self.lease:
            await self.stop()
        await self.bridge.close()
        await self.model.close()
        await self.memory.flush(force=True)
