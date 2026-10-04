"""Factual home and expansion geography; no extra action permission checks."""

from __future__ import annotations

import math


def _number(value):
    return value if type(value) in {int, float} and math.isfinite(value) else None


def _range(value):
    if not isinstance(value, dict):
        return None
    left, right = _number(value.get("left")), _number(value.get("right"))
    return {"left": left, "right": right} if left is not None and right is not None and left < right else None


def _contains(bounds, x):
    return bounds is not None and x is not None and bounds["left"] <= x <= bounds["right"]


def region(x, territory):
    x = _number(x)
    if x is None:
        return "unknown"
    camp, intact = territory.get("camp_bounds"), territory.get("intact_bounds")
    if _contains(intact, x):
        return "inside_defenses"
    if _contains(camp, x):
        return "inside_camp"
    if camp:
        return "outside_camp_left" if x < camp["left"] else "outside_camp_right"
    return "unknown"


def distance_from_camp(x, territory):
    x, camp = _number(x), territory.get("camp_bounds")
    if x is None or not camp:
        return None
    return max(camp["left"] - x, x - camp["right"], 0)


def is_building(target):
    if not isinstance(target, dict) or target.get("kind") != "upgrade":
        return False
    names = " ".join(str(target.get(key) or "") for key in ("name", "next_building")).lower()
    return any(name in names for name in ("wall", "tower", "farm", "castle", "camp", "shop"))


def territory_context(state):
    world = state.world or {}
    environment = world.get("environment") or {}
    centers = [_number(item.get("x")) for item in world.get("structures") or []
               if isinstance(item, dict) and item.get("kind") == "castle"]
    centers = [x for x in centers if x is not None]
    center = centers[0] if len(set(centers)) == 1 else None
    camp, intact, island = (_range(environment.get(key)) for key in ("borders", "intact_borders", "world_bounds"))
    issues = []
    if camp and center is not None and not _contains(camp, center):
        camp = None
        issues.append("camp_bounds_exclude_castle")
    if island and center is not None and not _contains(island, center):
        island = None
        issues.append("island_bounds_exclude_castle")
    if camp and island and not (_contains(island, camp["left"]) and _contains(island, camp["right"])):
        camp = intact = None
        issues.append("camp_bounds_outside_island")
    if intact and ((center is not None and not _contains(intact, center))
                   or (camp and not (_contains(camp, intact["left"]) and _contains(camp, intact["right"])))):
        intact = None
        issues.append("intact_bounds_inconsistent")
    result = {"source": "live_game", "observed_at": environment.get("observed_at") or state.captured_at.isoformat(),
              "center_x": center, "camp_bounds": camp, "intact_bounds": intact, "island_bounds": island,
              "bounds_status": "known" if camp else "unknown", "issues": issues, "advisory": True}
    p2 = state.player(1)
    result["p2_region"] = region(p2.x if p2 else None, result)
    result["p2_distance_from_camp"] = distance_from_camp(p2.x if p2 else None, result)
    buildings = [item for item in world.get("targets") or [] if is_building(item)]
    result["camp_building_count"] = sum(region(item.get("x"), result).startswith("inside_") for item in buildings)
    result["outside_building_count"] = sum(region(item.get("x"), result).startswith("outside_") for item in buildings)
    frontier = []
    for side in ("left", "right"):
        outside = [item for item in buildings if region(item.get("x"), result) == "outside_camp_" + side]
        if outside:
            item = min(outside, key=lambda item: distance_from_camp(item.get("x"), result))
            frontier.append({"target_id": item.get("target_id"), "name": item.get("name"), "x": item.get("x"),
                             "side": side, "distance_from_camp": distance_from_camp(item.get("x"), result)})
    result["frontier_candidates"] = frontier
    return result


def annotated_state(state, territory):
    data = state.model_dump(mode="json")
    world = data.get("world") or {}
    for key in ("targets", "nearby_payables"):
        if not isinstance(world.get(key), list):
            continue
        for item in world[key]:
            if isinstance(item, dict):
                item["region"] = region(item.get("x"), territory)
                item["distance_from_camp"] = distance_from_camp(item.get("x"), territory)
                if is_building(item):
                    item["purpose"] = "camp_construction" if item["region"].startswith("inside_") else (
                        "expansion" if item["region"].startswith("outside_") else "unknown")
    return data
