"""Small factual context shared by dialogue and action planning; never includes credentials."""

from __future__ import annotations

from datetime import UTC, datetime

from companion.contracts import GameState


def fresh(state: GameState | None) -> bool:
    if state is None:
        return False
    age = (datetime.now(UTC) - state.captured_at.astimezone(UTC)).total_seconds()
    return -2 <= age <= 2.5


def action_summary(value: dict | None) -> dict | None:
    if not isinstance(value, dict):
        return None
    result = {}
    for key, choices in (("operation", {"move", "move_to", "pay", "pay_coin", "stop", "drop", "ability", "map", "sail"}),
                         ("status", {"sent", "running", "completed", "failed", "cancelled", "verified", "unverified", "unknown", "stopped", "waiting", "unavailable", "transition"}),
                         ("direction", {"left", "right"})):
        if isinstance(value.get(key), str) and value[key] in choices:
            result[key] = value[key]
    if isinstance(value.get("message"), str):
        result["message"] = value["message"][:300]
    if isinstance(value.get("target_id"), str):
        result["target_id"] = value["target_id"][:200]
    if type(value.get("sprint")) is bool:
        result["sprint"] = value["sprint"]
    if type(value.get("duration_ms")) is int:
        result["duration_ms"] = max(100, value["duration_ms"])
    for key in ("currency", "ability", "ability_action", "map_action"):
        if isinstance(value.get(key), str):
            result[key] = value[key][:200]
    for key in ("target_x", "land", "amount"):
        if type(value.get(key)) in {int, float}:
            result[key] = value[key]
    return result or None


def safe_context(value: dict | None) -> dict:
    """Rebuild a whitelist rather than forwarding controller/journal dictionaries."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, limit in (("control_goal", 500), ("message", 300), ("scene", 200),
                       ("observed_at", 60)):
        if isinstance(value.get(key), str):
            result[key] = value[key][:limit]
    if isinstance(value.get("mode"), str) and value["mode"] in {"idle", "follow", "autonomous", "manual"}:
        result["mode"] = value["mode"]
    if isinstance(value.get("play_style"), str) and value["play_style"] in {"cooperate", "independent"}:
        result["play_style"] = value["play_style"]
    if isinstance(value.get("spending_mode"), str) and value["spending_mode"] in {"budgeted", "wallet"}:
        result["spending_mode"] = value["spending_mode"]
    for key in ("stale", "continuous"):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    for key in ("last_action", "current_action"):
        result[key] = action_summary(value.get(key))
    actions = value.get("recent_actions")
    if isinstance(actions, list):
        result["recent_actions"] = [item for raw in actions[-6:]
                                    if (item := action_summary(raw)) is not None]
    cooldowns = value.get("payment_cooldowns")
    if isinstance(cooldowns, list):
        result["payment_cooldowns"] = [{"target_id": item["target_id"][:200],
                                        "remaining_seconds": item["remaining_seconds"]}
                                       for item in cooldowns[:16] if isinstance(item, dict)
                                       and isinstance(item.get("target_id"), str)
                                       and type(item.get("remaining_seconds")) in {int, float}
                                       and 0 < item["remaining_seconds"] <= 20]
    for key in ("memory", "plan", "territory"):
        if isinstance(value.get(key), dict):
            result[key] = _shared_facts(value[key])
    return result


_SHARED_FIELDS = frozenset({
    "identity_verified", "campaign", "theme", "biome_index", "started_at", "reign", "challenge_id",
    "land", "max_islands", "furthest_land", "completed_islands", "completed", "secured_islands",
    "visited_islands", "technology", "lost", "observed_at", "error", "islands", "zone", "explored",
    "known_objects", "current_island", "resources", "coins", "currencies", "gems", "crowns", "skulls",
    "shades", "merchandise", "candle", "egg", "unit_counts", "workers", "farmers", "archers", "knights",
    "beggars", "beggarcamps", "farmlands", "fleetboats", "farmhouse", "banker", "environment", "time",
    "season", "island_days", "world_bounds", "borders", "intact_borders", "left", "right", "kind", "name",
    "x", "y", "price", "currency", "can_pay", "has_upgrade", "next_building", "fully_repaired", "needs_work",
    "build_points", "required_build_points", "portal_type", "state", "stored_coins", "last_seen_at",
    "present_in_last_observation", "quests", "type", "step", "keywords", "recent_goals", "goal", "source",
    "action_history", "historical", "operation", "status", "direction", "sprint", "duration_ms", "message",
    "target_id", "ability", "ability_action", "map_action", "amount", "target_x", "stage", "objective", "criteria",
    "metric", "comparison", "value", "description", "observed", "fallback", "failures", "feedback",
    "completed_stages", "evidence", "advisory", "landmarks", "last_plan",
    "center_x", "camp_bounds", "intact_bounds", "island_bounds", "bounds_status", "issues",
    "p2_region", "p2_distance_from_camp", "camp_building_count", "outside_building_count",
    "frontier_candidates", "side", "distance_from_camp", "region", "purpose",
})


def _shared_facts(value, depth=0):
    import math

    if depth > 7:
        return None
    if value is None or type(value) in {int, bool}:
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, list):
        return [_shared_facts(item, depth + 1) for item in value[:128]]
    if isinstance(value, dict):
        return {key: _shared_facts(item, depth + 1) for key, item in value.items() if key in _SHARED_FIELDS}
    return None
