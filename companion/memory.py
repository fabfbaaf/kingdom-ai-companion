"""Local campaign memory containing observed game facts and action outcomes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import secrets
import time
from collections import Counter
from copy import deepcopy
from pathlib import Path

from companion.gameplay_context import action_summary, fresh
from companion.planning import StagePlanner

CAMPAIGN_FIELDS = {"theme", "biome_index", "started_at", "reign", "challenge_id", "land",
                   "max_islands", "furthest_land", "completed_islands", "completed",
                   "secured_islands", "visited_islands", "technology", "lost"}
ENTITY_FIELDS = {"kind", "name", "x", "y", "price", "currency", "can_pay", "has_upgrade",
                 "next_building", "fully_repaired", "needs_work", "build_points",
                 "required_build_points", "portal_type", "state", "stored_coins"}
ENVIRONMENT_FIELDS = {"time", "season", "island_days", "world_bounds", "borders", "intact_borders"}


def _value(value, depth=0):
    if depth > 3:
        return None
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:300]
    if isinstance(value, list):
        return [_value(item, depth + 1) for item in value[:256]]
    if isinstance(value, dict):
        return {key: _value(value[key], depth + 1) for key in ("left", "right") if key in value}
    return None


def facts(value, fields):
    return {key: _value(item) for key, item in value.items() if key in fields} if isinstance(value, dict) else {}


class GameplayMemory:
    def __init__(self, path: Path):
        self.path = path
        self.campaigns: dict[str, dict] = {}
        self.active_key: str | None = None
        self.planner = StagePlanner()
        self.error: str | None = None
        self._revision = 0
        self._saved_revision = 0
        self._last_save = 0.0
        self._last_observation = 0.0
        self._save_lock = asyncio.Lock()
        self._dialogue: list[dict] = []
        self._restored_plans: dict[str, dict] = {}
        try:
            if path.is_file():
                if path.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError("oversized memory")
                value = json.loads(path.read_text(encoding="utf-8"))
                if value.get("schema") != 1 or not isinstance(value.get("campaigns"), dict):
                    raise ValueError("invalid memory")
                self.campaigns = {key: item for key, item in list(value["campaigns"].items())[-10:]
                                  if isinstance(key, str) and self._valid_entry(item)}
                self._restored_plans = {key: deepcopy(item["last_plan"]) for key, item in self.campaigns.items()
                                        if isinstance(item.get("last_plan"), dict)}
        except (OSError, ValueError, TypeError, AttributeError):
            self.error = "本地记忆未能恢复；已使用空记忆，当前控制不会因此自动启动。"

    @staticmethod
    def _valid_entry(item) -> bool:
        if not isinstance(item, dict) or item.get("identity_verified") is not True:
            return False
        if not isinstance(item.get("campaign"), dict) or not isinstance(item.get("islands"), dict):
            return False
        try:
            json.dumps(item, allow_nan=False)
        except (TypeError, ValueError):
            return False
        for island in item["islands"].values():
            if (not isinstance(island, dict) or not isinstance(island.get("entities"), dict)
                    or not all(isinstance(entity, dict) for entity in island["entities"].values())):
                return False
            explored = island.get("explored")
            if explored is not None and (not isinstance(explored, list) or len(explored) != 2
                    or any(type(value) not in {int, float} or not math.isfinite(value) for value in explored)):
                return False
        return all(isinstance(item.get(field, []), list) and all(isinstance(v, dict) for v in item.get(field, []))
                   for field in ("actions", "goals", "quests", "completed_stages"))

    def _campaign_key(self, state, campaign: dict) -> tuple[str, bool]:
        started = campaign.get("started_at")
        if type(started) is int and started > 0:
            identity = [started, campaign.get("theme"), campaign.get("biome_index"), campaign.get("challenge_id")]
            return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24], True
        return "session:" + state.session_id, False

    def observe(self, state, *, force=False) -> None:
        if not fresh(state) or not (state.ready or state.ui_ready):
            return
        world = state.world or {}
        campaign = facts(world.get("campaign"), CAMPAIGN_FIELDS)
        key, verified = self._campaign_key(state, campaign)
        if key != self.active_key:
            self.active_key = key
            self.planner = StagePlanner()
            previous = self.campaigns.get(key, {})
            self.planner.completed = [facts(item, {"stage", "objective", "evidence"})
                                      for item in previous.get("completed_stages", [])[-20:]]
            self._last_observation = 0
        current_land = self.campaigns.get(key, {}).get("campaign", {}).get("land")
        if not force and current_land == campaign.get("land") and time.monotonic() - self._last_observation < 0.5:
            return
        self._last_observation = time.monotonic()
        entry = self.campaigns.setdefault(key, {"identity_verified": verified, "islands": {},
                                               "actions": [], "goals": []})
        stamp = state.captured_at.isoformat()
        entry.update(campaign=campaign, observed_at=stamp)
        land = campaign.get("land")
        zone = "cave" if (world.get("control") or {}).get("in_cave") is True else "surface"
        island_key = f"{land if land is not None else state.scene}:{zone}"
        islands = entry.setdefault("islands", {})
        island = islands.setdefault(island_key, {"land": land, "zone": zone, "entities": {}, "explored": None})
        island.update(observed_at=stamp, day=world.get("day"),
                      environment=facts(world.get("environment"), ENVIRONMENT_FIELDS))
        p2 = state.player(1)
        if p2:
            island["resources"] = {"coins": p2.coins, "currencies": {
                name: amount for name, amount in (p2.currencies or {}).items()
                if isinstance(name, str) and type(amount) is int and 0 <= amount <= 2**31 - 1}}
            if p2.x is not None:
                previous = island.get("explored")
                island["explored"] = [min(previous[0], p2.x), max(previous[1], p2.x)] if previous else [p2.x, p2.x]
        entities = island.setdefault("entities", {})
        for entity in entities.values():
            entity["present_in_last_observation"] = False
        for raw in (world.get("structures") or []) + (world.get("targets") or []):
            item = facts(raw, ENTITY_FIELDS)
            if not item:
                continue
            entity_key = json.dumps([item.get("kind"), item.get("name"), item.get("x")], ensure_ascii=False)
            entities[entity_key] = {**item, "last_seen_at": stamp, "present_in_last_observation": True}
        if len(entities) > 1024:
            island["entities"] = dict(list(entities.items())[-1024:])
        if isinstance(world.get("units"), list):
            island["unit_counts"] = dict(Counter(str(item.get("kind", "unknown"))[:60]
                                                   for item in world["units"] if isinstance(item, dict)))
        island["drops"] = [facts(item, {"kind", "name", "x", "currency", "picked_up", "fake"})
                           for item in (world.get("dropped_items") or [])[:128]]
        entry["quests"] = [facts(item, {"type", "step", "keywords", "completed"})
                           for item in (world.get("quests") or [])[:64]]
        entry["current_island"] = island_key
        if len(islands) > 64:
            entry["islands"] = dict(list(islands.items())[-64:])
        self.planner.observe(state)
        entry["completed_stages"] = list(self.planner.completed)
        entry["last_plan"] = self.planner.public()
        self._revision += 1

    def set_goal(self, goal: str, *, source="control") -> None:
        self.planner.set_goal(goal)
        saved = self._restored_plans.get(self.active_key, {})
        if source == "control" and goal and saved.get("goal") == goal:
            from companion.planning import PlanProposal

            try:
                proposal = PlanProposal.model_validate({
                    "stage": saved.get("stage"), "objective": saved.get("objective"),
                    "fallback": saved.get("fallback"), "criteria": [facts(condition, {
                        "metric", "comparison", "value", "description"}) for condition in saved.get("criteria", [])]})
                self.planner.plan = proposal
                self.planner.land = saved.get("land")
                self.planner._automatic = False
                self.planner._recorded = saved.get("status") == "completed"
                self._restored_plans.pop(self.active_key, None)
            except (TypeError, ValueError):
                self._restored_plans.pop(self.active_key, None)
        entry = self.campaigns.get(self.active_key)
        if entry is not None:
            goals = entry.setdefault("goals", [])
            if not goals or goals[-1].get("goal") != goal:
                goals.append({"goal": goal[:500], "source": source})
                entry["goals"] = goals[-30:]
                self._revision += 1

    def record_result(self, result: dict) -> None:
        self.planner.record_result(result)
        entry = self.campaigns.get(self.active_key)
        summary = action_summary(result)
        if entry is not None and summary:
            entry.setdefault("actions", []).append({**summary, "land": entry.get("campaign", {}).get("land"),
                                                    "observed_at": entry.get("observed_at")})
            entry["actions"] = entry["actions"][-100:]
            self._revision += 1

    def record_dialogue(self, text: str, reply: str) -> None:
        # Full conversation stays in RAM; the durable file holds game facts and goals only.
        self._dialogue.extend([{"role": "user", "content": text[:1000]},
                               {"role": "assistant", "content": reply[:1500]}])
        self._dialogue = self._dialogue[-12:]

    def dialogue_history(self) -> list[dict]:
        return list(self._dialogue)

    def public(self) -> dict:
        entry = self.campaigns.get(self.active_key, {})
        islands = entry.get("islands", {})
        current = islands.get(entry.get("current_island"), {})
        return deepcopy({"identity_verified": entry.get("identity_verified") is True,
                "campaign": facts(entry.get("campaign"), CAMPAIGN_FIELDS),
                "observed_at": entry.get("observed_at"), "error": self.error,
                "islands": [{"land": item.get("land"), "zone": item.get("zone"),
                             "explored": item.get("explored"), "observed_at": item.get("observed_at"),
                             "known_objects": len(item.get("entities", {})),
                             "resources": item.get("resources"), "unit_counts": item.get("unit_counts"),
                             "landmarks": [facts(entity, ENTITY_FIELDS | {"last_seen_at", "present_in_last_observation"})
                                           for entity in list(item.get("entities", {}).values())[-24:]]} for item in islands.values()],
                "current_island": {"land": current.get("land"), "zone": current.get("zone"),
                    "observed_at": current.get("observed_at"), "explored": current.get("explored"),
                    "resources": current.get("resources"), "unit_counts": current.get("unit_counts"),
                    "environment": facts(current.get("environment"), ENVIRONMENT_FIELDS),
                    "known_objects": [facts(item, ENTITY_FIELDS | {"last_seen_at", "present_in_last_observation"})
                                      for item in list(current.get("entities", {}).values())[-128:]]},
                "quests": [facts(item, {"type", "step", "keywords", "completed"}) for item in entry.get("quests", [])],
                "recent_goals": [facts(item, {"goal", "source"}) for item in entry.get("goals", [])[-8:]],
                "action_history": [action_summary(item) for item in entry.get("actions", [])[-24:]],
                "last_plan": entry.get("last_plan"), "historical": True})

    async def flush(self, *, force=False) -> None:
        if self._saved_revision == self._revision or (not force and time.monotonic() - self._last_save < 5):
            return
        async with self._save_lock:
            revision = self._revision
            campaigns = {key: value for key, value in self.campaigns.items()
                         if value.get("identity_verified") is True}
            campaigns = deepcopy(dict(list(campaigns.items())[-10:]))
            active = campaigns.get(self.active_key)
            if active is not None:
                active["last_plan"] = self.planner.public()
                active["completed_stages"] = list(self.planner.completed)
            payload = json.dumps({"schema": 1, "campaigns": campaigns}, ensure_ascii=False, allow_nan=False)
            while len(payload.encode("utf-8")) > 3_500_000 and len(campaigns) > 1:
                old = next(key for key in campaigns if key != self.active_key)
                campaigns.pop(old)
                payload = json.dumps({"schema": 1, "campaigns": campaigns}, ensure_ascii=False, allow_nan=False)
            if len(payload.encode("utf-8")) > 3_500_000:
                for campaign in campaigns.values():
                    for island in campaign["islands"].values():
                        island["entities"] = dict(list(island["entities"].items())[-128:])
                payload = json.dumps({"schema": 1, "campaigns": campaigns}, ensure_ascii=False, allow_nan=False)
            def save():
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_name(f".memory-{secrets.token_hex(8)}.tmp")
                try:
                    temporary.write_text(payload, encoding="utf-8")
                    os.replace(temporary, self.path)
                finally:
                    temporary.unlink(missing_ok=True)
            try:
                await asyncio.to_thread(save)
                self._saved_revision = revision
                self._last_save = time.monotonic()
                self.error = None
            except OSError:
                self.error = "本地记忆写入失败；当前使用内存记忆，请检查目录权限与剩余空间。"
