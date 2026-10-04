"""Compact current facts and put stable object metadata before changing state."""

from __future__ import annotations

import json

TABLE_GUIDE = (
    "若另有world_catalog消息，它是本轮对象目录，与末条实时消息按target_id连接；"
    "目录不包含当前价格、可支付状态或位置，不能代替实时观测。"
    "大列表可用columns与rows表格表示：每行按columns顺序对应字段，null表示未知；"
    "空表表示没有记录，所有行均保留，不是范围裁切。"
    "历史记忆中与本轮观测完全相同的对象可能省略，请从state读取当前事实。"
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _deduplicate_memory(data, world):
    context = data.get("context") or {}
    memory = context.get("memory") or {}
    if not isinstance(memory, dict):
        return
    if memory.get("last_plan") == context.get("plan"):
        memory.pop("last_plan", None)
    recent = context.get("recent_actions") or []
    if isinstance(memory.get("action_history"), list):
        memory["action_history"] = [item for item in memory["action_history"] if item not in recent]
    current = memory.get("current_island") or {}
    if not isinstance(current, dict):
        return
    records = [item for key in ("targets", "units", "structures", "nearby_payables")
               for item in world.get(key) or [] if isinstance(item, dict)]
    by_location = {}
    for record in records:
        key = _json([record.get(field) for field in ("kind", "name", "x")])
        by_location.setdefault(key, []).append(record)
    # Remove only matching historical duplicates. Different/absent values and
    # old observations remain available; the stored memory itself is untouched.
    def redundant(item):
        if not isinstance(item, dict) or item.get("present_in_last_observation") is not True:
            return False
        facts = {key: value for key, value in item.items()
                 if key not in {"last_seen_at", "present_in_last_observation"}}
        if not all(key in facts for key in ("kind", "name", "x")):
            return False
        candidates = by_location.get(_json([facts[field] for field in ("kind", "name", "x")]), [])
        return any(all(record.get(key) == value and key in record for key, value in facts.items())
                   for record in candidates)
    if isinstance(current.get("known_objects"), list):
        current["known_objects"] = [item for item in current["known_objects"] if not redundant(item)]
    for island in memory.get("islands") or []:
        if (isinstance(island, dict) and island.get("land") == current.get("land")
                and island.get("zone") == current.get("zone") and isinstance(island.get("landmarks"), list)):
            island["landmarks"] = [item for item in island["landmarks"] if not redundant(item)]


_TABLES = {"targets", "enemies", "dropped_items", "units", "structures", "known_objects", "landmarks"}


def _tables(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            item = _tables(item)
            if (key in _TABLES and isinstance(item, list) and len(item) >= 12
                    and all(isinstance(row, dict) for row in item)):
                fields = set().union(*(row.keys() for row in item))
                leading = [field for field in ("target_id", "entity_id", "kind", "name", "x", "y") if field in fields]
                columns = leading + sorted(fields.difference(leading))
                table = {"columns": columns, "rows": [[row.get(field) for field in columns] for row in item]}
                if len(_json(table)) < len(_json(item)):
                    item = table
            result[key] = item
        return result
    if isinstance(value, list):
        return [_tables(item) for item in value]
    return value


def _compact(value):
    if isinstance(value, dict):
        return {key: _compact(item) for key, item in value.items()
                if item is not None or key in {"state", "remaining_coin_budget", "coin_budget_remaining"}}
    if isinstance(value, list):
        return [_compact(item) for item in value]
    return value


def model_packet(packet):
    data = _compact(packet)
    state = data.get("state")
    if not isinstance(state, dict):
        return data
    state.pop("diagnostics", None)
    world = state.get("world")
    if not isinstance(world, dict):
        return data
    _deduplicate_memory(data, world)
    for legacy, full in (("nearby_payables", "targets"), ("nearby_enemies", "enemies")):
        if full in world and world.get(legacy) == world[full]:
            world.pop(legacy, None)
    catalog = []
    for item in world.get("targets") or []:
        if not isinstance(item, dict) or not isinstance(item.get("target_id"), str):
            continue
        metadata = {key: item.pop(key) for key in ("kind", "name", "type", "next_building") if key in item}
        catalog.append({"target_id": item["target_id"], **metadata})
        if item.get("entity_id") == item["target_id"]:
            item.pop("entity_id", None)
    if not catalog:
        return data
    # Exact current identifiers and metadata are resent, not reused from an old
    # snapshot. Stable ordering allows the provider to reuse an identical prefix.
    catalog.sort(key=lambda item: item["target_id"])
    campaign = world.get("campaign") or {}
    fixed = {"session_id": state.get("session_id"), "land": campaign.get("land"), "targets": catalog}
    return {"world_catalog": fixed, **data}


def model_json(packet):
    return _json(model_packet(packet))


def model_messages(system, packet):
    """Give the provider a complete stable message before changing live facts.

    No old snapshot, prefix or model decision is reused. Equal current catalogs
    serialize identically even if the native registry returns a different order.
    """
    data = model_packet(packet)
    catalog = data.pop("world_catalog", None)
    state = data.get("state")
    if isinstance(state, dict):
        world = state.get("world")
        if isinstance(world, dict):
            slow = ("campaign", "quests", "structures", "targets", "ui", "abilities", "is_night", "day")
            # Keep unchanged strategic facts before clocks and moving entities.
            # These remain fresh, even when the provider can reuse their prefix.
            state["world"] = {**{key: world[key] for key in slow if key in world},
                              **{key: value for key, value in world.items() if key not in slow}}
        slow = ("world", "bridge_version", "game_version", "session_id", "scene", "capabilities", "ready", "coop", "controlled_player_id", "ui_ready")
        data["state"] = {**{key: state[key] for key in slow if key in state},
                         **{key: value for key, value in state.items() if key not in slow}}
    messages = [{"role": "system", "content": system + TABLE_GUIDE}]
    if catalog is not None:
        messages.append({"role": "user", "content": json.dumps(_tables({"world_catalog": catalog}),
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)})
    messages.append({"role": "user", "content": _json(_tables(data))})
    return messages
