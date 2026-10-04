"""Advisory stage plans; completion is measured from observed game facts only."""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from companion.contracts_types import FiniteNumber

Metric = Literal["coins", "workers", "farmers", "archers", "damaged_walls",
                 "construction_remaining", "portals", "land", "island_secured",
                 "campaign_completed", "completed_islands", "is_night", "night_survived"]


class CompletionCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    metric: Metric
    comparison: Literal["gte", "lte", "eq", "neq"] = "gte"
    value: FiniteNumber
    description: str = Field(min_length=1, max_length=200)


class PlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    stage: Literal["explore", "economy", "defense", "attack", "sail", "complete"]
    objective: str = Field(min_length=1, max_length=500)
    criteria: list[CompletionCondition] = Field(min_length=1, max_length=8)
    fallback: str = Field(default="失败后重新观察，调整路线或更换目标。", max_length=300)

    @model_validator(mode="after")
    def real_campaign_completion(self):
        if self.stage == "complete" and not any(condition.metric == "campaign_completed"
                and condition.comparison == "eq" and condition.value == 1 for condition in self.criteria):
            raise ValueError("通关阶段必须以原生战役完成标记为条件")
        return self


def observed_metrics(state) -> dict:
    world = state.world or {}
    campaign = world.get("campaign") or {}
    metrics = {}
    p2 = state.player(1)
    if p2 and p2.coins is not None:
        metrics["coins"] = p2.coins
    for name, key in (("land", "land"), ("completed_islands", "completed_islands"),
                      ("campaign_completed", "completed")):
        value = campaign.get(key)
        if type(value) in {int, float, bool}:
            metrics[name] = int(value) if type(value) is bool else value
    if type(world.get("is_night")) is bool:
        metrics["is_night"] = int(world["is_night"])
    secured = campaign.get("secured_islands")
    land = campaign.get("land")
    if (isinstance(secured, list) and type(land) is int and 0 <= land < len(secured)
            and type(secured[land]) is bool):
        metrics["island_secured"] = int(secured[land])
    if isinstance(world.get("units"), list):
        counts = Counter(item.get("kind") for item in world["units"] if isinstance(item, dict))
        for role in ("workers", "farmers", "archers"):
            metrics[role] = counts[role]
    if isinstance(world.get("structures"), list):
        structures = [item for item in world["structures"] if isinstance(item, dict)]
        walls = [item for item in structures if item.get("kind") == "wall"]
        if walls and all(type(item.get("fully_repaired")) is bool for item in walls):
            metrics["damaged_walls"] = sum(not item["fully_repaired"] for item in walls)
        construction = [item for item in structures if item.get("kind") == "construction"]
        if all(type(item.get("needs_work")) is bool for item in construction):
            metrics["construction_remaining"] = sum(item["needs_work"] for item in construction)
        metrics["portals"] = sum(item.get("kind") in {"portal", "cave"} for item in structures)
    return metrics


class StagePlanner:
    def __init__(self):
        self.plan: PlanProposal | None = None
        self.goal = ""
        self.land = None
        self.status = "waiting_observation"
        self.checks: list[dict] = []
        self.completed: list[dict] = []
        self.failures = 0
        self.feedback = ""
        self._recorded = False
        self._automatic = True
        self._night_seen = False

    def set_goal(self, goal: str) -> None:
        if goal != self.goal:
            self.goal = goal
            self.plan = None
            self._automatic = True
            self.status = "waiting_observation"
            self.checks = []
            self.failures = 0
            self.feedback = "目标已更新，按最新观测制定阶段计划。"
            self._night_seen = False

    def propose(self, proposal: PlanProposal, state) -> None:
        self.plan = proposal
        self._automatic = False
        self.land = (state.world or {}).get("campaign", {}).get("land")
        self._recorded = False
        self.failures = 0
        self.observe(state)

    def _default(self, state, metrics: dict) -> PlanProposal:
        goal = self.goal.split("；玩家安排：", 1)[0]
        campaign_goal = goal.startswith(("自主完成当前战役", "完成当前战役", "通关"))
        stage, metric, comparison, value = "explore", "land", "eq", metrics.get("land", -1)
        objective, description = "确认当前岛屿的营地、资源、敌人和可用目标。", "读取当前岛屿编号"
        if metrics.get("campaign_completed") == 1:
            stage, metric, value = "complete", "campaign_completed", 1
            objective, description = "游戏已报告战役完成。", "原生战役完成标记为真"
        elif (not campaign_goal and any(word in goal for word in ("守", "防御", "防守", "保护"))) or metrics.get("is_night") == 1:
            stage, metric, comparison, value = "defense", "damaged_walls", "eq", 0
            objective, description = "保护营地，结合敌人方向安排防御与修复。", "观测到现有城墙全部修复"
            if "今晚" in goal:
                metric, value, description = "night_survived", 1, "安排后已观测到夜晚，随后游戏报告白天"
        elif (not campaign_goal and any(word in goal for word in ("船", "出航", "下一岛"))) or metrics.get("island_secured") == 1:
            stage, metric, comparison = "sail", "land", "neq"
            value = metrics.get("land", -1)
            objective, description = "准备船只和乘员，按战役进度选择目的岛并出航。", "原生岛屿编号发生变化"
        elif not campaign_goal and any(word in goal for word in ("进攻", "清除", "摧毁", "打洞")):
            stage, metric, value = "attack", "island_secured", 1
            objective, description = "组织进攻，依据原生敌人和门户状态清理当前岛屿。", "原生当前岛屿安全标记为真"
        elif (not campaign_goal and any(word in goal for word in ("经济", "赚", "攒钱"))) or metrics.get("workers") == 0 or metrics.get("farmers") == 0:
            stage, metric, comparison, value = "economy", "farmers", "gte", 1
            objective, description = "发展经济，依据可用营地、商店、农田和金币决定建设顺序。", "观测到至少一名农民"
            if metrics.get("workers") == 0:
                metric, description = "workers", "观测到至少一名工人"
        elif metrics.get("portals", 0) > 0:
            stage, metric, value = "attack", "island_secured", 1
            objective, description = "发展防御与兵力后清理门户，推进岛屿目标。", "原生当前岛屿安全标记为真"
        return PlanProposal(stage=stage, objective=objective, criteria=[CompletionCondition(
            metric=metric, comparison=comparison, value=value, description=description)])

    def observe(self, state) -> None:
        metrics = observed_metrics(state)
        land = metrics.get("land")
        if self.land is not None and land is not None and land != self.land:
            self._night_seen = False
        if metrics.get("is_night") == 1:
            self._night_seen = True
        if "is_night" in metrics:
            metrics["night_survived"] = int(self._night_seen and metrics["is_night"] == 0)
        if self.plan is not None and self.land is not None and land is not None and land != self.land:
            if self.plan.stage == "sail":
                self.completed.append({"stage": "sail", "objective": self.plan.objective,
                                       "evidence": f"岛屿由 {self.land} 变为 {land}"})
            self.plan = None
            self.feedback = "已切换岛屿，历史地图保留；重新规划当前岛屿。"
        if metrics.get("campaign_completed") == 1 and (self.plan is None or self.plan.stage != "complete"):
            self.plan = None
        candidate = self._default(state, metrics)
        if self.plan is not None and self._automatic and candidate.stage != self.plan.stage:
            self.plan = None
            self.feedback = "局势发生变化，已依据昼夜、资源或岛屿进度调整阶段。"
        if self.plan is None:
            self.plan = candidate
            self._automatic = True
            self.land = land
            self._recorded = False
        self.checks = []
        for condition in self.plan.criteria:
            actual = metrics.get(condition.metric)
            matched = None if actual is None else {
                "gte": actual >= condition.value, "lte": actual <= condition.value,
                "eq": actual == condition.value, "neq": actual != condition.value,
            }[condition.comparison]
            self.checks.append({**condition.model_dump(), "observed": actual,
                                "status": "unknown" if matched is None else "completed" if matched else "pending"})
        self.status = ("completed" if all(item["status"] == "completed" for item in self.checks)
                       else "waiting_observation" if any(item["status"] == "unknown" for item in self.checks)
                       else "in_progress")
        if self.status == "completed" and not self._recorded:
            self.completed.append({"stage": self.plan.stage, "objective": self.plan.objective,
                                   "evidence": "; ".join(item["description"] for item in self.checks)})
            self._recorded = True
        self.completed = self.completed[-20:]

    def record_result(self, result: dict) -> None:
        if result.get("status") in {"failed", "unknown", "unavailable"}:
            self.failures += 1
            self.feedback = ("本阶段连续多次未取得结果，必须重新选目标或路线，不要重复原动作。"
                             if self.failures >= 2 else "动作未取得结果，先依据最新状态调整下一步。")
        elif result.get("status") == "completed":
            self.failures = 0
            self.feedback = "收到动作完成回执；阶段完成仍取决于游戏观测。"

    def public(self) -> dict:
        return {"stage": self.plan.stage if self.plan else "explore", "goal": self.goal,
                "land": self.land,
                "objective": self.plan.objective if self.plan else "等待真实游戏观测。",
                "status": self.status, "criteria": list(self.checks),
                "fallback": self.plan.fallback if self.plan else "读取实时状态后制定计划。",
                "failures": self.failures, "feedback": self.feedback,
                "completed_stages": list(self.completed), "advisory": True}
