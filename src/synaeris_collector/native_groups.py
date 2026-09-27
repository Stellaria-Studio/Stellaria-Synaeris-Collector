"""Pinned BetterGI serialization and reviewed native route contracts."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re


PROFILES = {"navigation_pickup", "combat_pathing"}
COMBAT_STRATEGY = "万能战斗策略（萌新推荐）"


def merge_defaults(defaults, value):
    result = deepcopy(defaults)
    for key, item in value.items():
        result[key] = (merge_defaults(result[key], item)
                       if isinstance(item, dict) and isinstance(result.get(key), dict) else item)
    return result


def canonical_group(group):
    defaults = json.loads(Path(__file__).with_name("bgi_native_defaults.json").read_text(encoding="utf-8"))
    result = deepcopy(group)
    # BGI renumbers display order whenever it loads/saves its group list.
    result.pop("index", None)
    result["config"] = merge_defaults(defaults, group.get("config", {}))
    result["projects"] = [merge_defaults({"jsScriptSettingsObject": None,
        "allowJsNotification": True, "allowJsHTTPHash": ""}, p) for p in group["projects"]]
    return result


def semantic_hash(group):
    return hashlib.sha256(json.dumps(canonical_group(group), sort_keys=True,
        ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _validate_identity(name, route_names):
    if not re.fullmatch(r"Synaeris_[A-Za-z0-9_]+", name):
        raise ValueError("Invalid collection group identity")
    for item in route_names:
        if Path(item).name != item or any(c in item for c in '\\/:*?"<>|') or not item.endswith(".json"):
            raise ValueError("Invalid route asset filename")


def native_group(name, route_names, profile="navigation_pickup"):
    """Build the exact no-shell/no-JS BetterGI group for a reviewed profile."""
    _validate_identity(name, route_names)
    if profile not in PROFILES:
        raise ValueError("Unsupported native collection profile")
    combat = profile == "combat_pathing"
    return {"index": 100, "name": name, "config": {"enableShellConfig": False,
        "pathingConfig": {"enabled": True, "autoPickEnabled": True,
            "autoFightEnabled": combat, "soloTaskUseFightEnabled": combat,
            "autoSkipEnabled": False, "autoEatEnabled": False, "partyName": "",
            "jsScriptUseEnabled": False,
            "autoFightConfig": {"strategyName": COMBAT_STRATEGY if combat else "根据队伍自动选择",
                "fightFinishDetectEnabled": True, "timeout": 120,
                "pickDropsAfterFightEnabled": True, "pickDropsAfterFightSeconds": 15}}},
        "projects": [{"name": item, "folderName": "StellariaSynaeris\\" + name,
            "index": i + 1, "type": "Pathing", "status": "Enabled", "schedule": "Daily", "runNum": 1}
            for i, item in enumerate(route_names)]}


def food_group(name, route_names):
    """Compatibility name for the original navigation/pickup profile."""
    return native_group(name, route_names, "navigation_pickup")


def route_contract(data, profile="navigation_pickup"):
    """Return bounded route facts when a native asset is admissible, else None.

    ``combat_script`` remains excluded for combat collection: its text is a
    second command language. Combat routes use BetterGI's typed ``fight``
    waypoint and a separately pinned auto-fight configuration instead.
    """
    if profile not in PROFILES:
        return None
    points = data.get("positions", [])
    max_points = 160 if profile == "combat_pathing" else 100
    if (not 2 <= len(points) <= max_points or data.get("info", {}).get("type") != "collect"
            or data.get("info", {}).get("map_name", "Teyvat") != "Teyvat"
            or points[0].get("type") != "teleport"):
        return None
    fight_waypoints = 0
    allowed_modes = {"walk", "run", "climb", "fly"}
    if profile == "combat_pathing":
        allowed_modes.add("dash")
    for point in points:
        if not all(isinstance(point.get(k), (float, int)) and math.isfinite(point[k]) for k in ("x", "y")):
            return None
        action, params = point.get("action", ""), point.get("action_params", "")
        if point.get("move_mode", "walk") not in allowed_modes:
            return None
        if action == "fight" and profile == "combat_pathing" and not params:
            fight_waypoints += 1
        elif action == "combat_script" and profile == "navigation_pickup":
            # Some food routes use the command parser only for a short wait.
            if not isinstance(params, str) or not re.fullmatch(r"wait\((?:0\.[1-9]|[1-3](?:\.0)?)\)", params):
                return None
        elif action == "wait":
            try:
                if params and not 0 < float(params) <= 3:
                    return None
            except (TypeError, ValueError):
                return None
        elif action not in {"", "stop_flying", "jump", "pick_around"} or params:
            return None
    if profile == "combat_pathing" and fight_waypoints < 1:
        return None
    return {"profile": profile, "points": len(points), "fight_waypoints": fight_waypoints}


def admissible_route(data, profile="navigation_pickup"):
    return route_contract(data, profile) is not None


def admissible_food(data):
    return admissible_route(data, "navigation_pickup")
