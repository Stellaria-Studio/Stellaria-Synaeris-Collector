"""Create a finite, immutable gameplay queue from installed native routes.

Selection is a prerequisite screen, not proof of unlocks, pickups or success.
No repeated routes, JS permissions, story skipping or arbitrary shell tasks.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

from synaeris_collector.bgi import catalog
from synaeris_collector.config import Config
from synaeris_collector.storage import atomic_json
from synaeris_collector.native_groups import semantic_hash, admissible_food
from synaeris_collector.coverage import route_features, select_diverse
from synaeris_collector.machine_worker import read_json, sha256
from synaeris_collector.storage import iter_rows
from dataclasses import asdict
from prepare_live_groups import prepare

ROOT = Path(__file__).resolve().parents[1]
REGIONS = [("nodkrai", "挪德卡莱"), ("natlan", "纳塔"), ("mondstadt", "蒙德")]
ALL_REGIONS = REGIONS + [("liyue", "璃月"), ("inazuma", "稻妻"), ("sumeru", "须弥"), ("fontaine", "枫丹")]
SAFE_ACTIONS = {"", "stop_flying", "jump", "wait", "pick_around"}


def admissible(data):
    points = data.get("positions", [])
    if not 2 <= len(points) <= 100:
        return False
    if data.get("info", {}).get("type") != "collect":
        return False
    if data.get("info", {}).get("map_name", "Teyvat") != "Teyvat":
        return False
    if points[0].get("type") != "teleport":
        return False
    for point in points:
        action, params = point.get("action", ""), point.get("action_params", "")
        if point.get("move_mode", "walk") not in {"walk", "run", "climb", "fly"}:
            return False
        if action == "combat_script":
            # Some food routes use the combat-script parser only for a short wait.
            if not re.fullmatch(r"wait\((?:0\.[1-9]|[1-3](?:\.0)?)\)", params):
                return False
        elif action not in SAFE_ACTIONS or (params and action != "wait"):
            return False
    return True


def attempted_routes():
    blocked = set()
    metadata = ROOT / "reports/local/machine_worker/installation.json"
    if metadata.exists():
        runtime = Path(read_json(metadata)["runtime"])
        blocked.update(read_json(runtime / "machine-policy.json").get("previously_attempted_hashes", []))
        history = runtime / "machine-history.json"
        if history.exists():
            blocked.update(read_json(history).get("attempted_hashes", []))
    source = ROOT / ".external/bettergi-portable/User/AutoPathing"
    for manifest in (ROOT / "data/episodes").glob("*/manifest.json"):
        if read_json(manifest).get("synthetic"):
            continue
        # An unfinished recording is preserved rather than read as a closed table.
        if read_json(manifest)["status"] == "recording":
            raise RuntimeError("Finish active collection before preparing another queue")
        for row in iter_rows(manifest.parent, "tools"):
            payload = row["payload"]
            folder, route = payload.get("folder", ""), payload.get("route", "")
            if (row["source"] == "bettergi_native" and row["kind"] == "TOOL_START"
                    and re.fullmatch(r"StellariaSynaeris\\Synaeris_[A-Za-z0-9_]+", folder)
                    and Path(route).name == route):
                path = source / folder / route
                if path.is_file():
                    blocked.add(sha256(path))
    return blocked


def build(name, per_region=12, diverse=False, max_routes=48):
    if not re.fullmatch(r"[A-Za-z0-9_]+", name) or not 2 <= per_region <= 50:
        raise ValueError("Invalid queue name or route count")
    if diverse and not 7 <= max_routes <= 84:
        raise ValueError("Invalid bounded diversity queue size")
    destination = ROOT / "data" / name
    if destination.exists():
        raise ValueError("Existing campaign is preserved; choose a new name")
    routes = catalog("C:/Program Files/BetterGI")
    blocked = attempted_routes()
    regions = ALL_REGIONS if diverse else REGIONS
    # Previously harvested qualification routes remain available as evidence,
    # but are not replayed today to inflate duration with depleted items.
    harvested = {"02-鸟蛋-挪德卡莱-2窝.json", "04-鸟蛋-挪德卡莱-2窝.json"}
    selected, seen = [], set()
    # Balanced quotas preserve regional priority while reserving samples for older regions.
    quotas = {code: max_routes // len(regions) + (i < max_routes % len(regions)) for i, (code, _) in enumerate(regions)}
    candidate_counts = {}
    for code, region in regions:
        candidates = []
        for route in routes:
            file = Path(route["path"])
            if region not in route["relative_path"] or file.name in harvested or route["sha256"] in blocked:
                continue
            # Start with food routes; mining/character-specific and high-risk
            # scripts need separate live precondition qualification.
            if "食材与炼金" not in route["relative_path"] or any(
                term in route["relative_path"] for term in ("高危", "中危", "地下", "洞穴", "一条龙", "水下")
            ):
                continue
            data = json.loads(file.read_text(encoding="utf-8-sig"))
            if admissible_food(data) and route["sha256"] not in seen:
                route["coverage_features"] = route_features(route, data)
                candidates.append(route)
        candidate_counts[region] = len(candidates)
        chosen = select_diverse(candidates, quotas[code]) if diverse else sorted(candidates, key=lambda r: (r["points"], r["relative_path"]))[:per_region]
        for route in chosen:
            seen.add(route["sha256"])
        selected.extend((code, region, chosen[i:i+2])
                        for i in range(0, len(chosen), 2))
    if not selected:
        raise ValueError("No admissible installed routes")
    destination.mkdir(parents=True)
    bgi = ROOT / ".external" / "bettergi-portable"
    stages = []
    for index, (code, region, batch) in enumerate(selected):
        stage = f"{code}_{index+1:02d}"
        group = f"Synaeris_{name}_{stage}"
        route_manifest = destination / f"{stage}.routes.json"
        group_file = prepare([r["path"] for r in batch], bgi, group, route_manifest)
        config = Config(actor="BGI", region=region, quest=group, poi=group, novel=True,
                        output=str(ROOT / "data" / "episodes"), bgi_dir=str(bgi))
        # Only the actual bird-prompt sample was calibrated; keep the broad
        # interaction ROI for different food layouts pending live calibration.
        # Existing protected Round06 worker has the base Config schema; preserve
        # compatibility until a deliberate packaged-worker software update.
        values = asdict(config)
        values.pop("scenario", None)
        values.pop("capture_background", None)
        atomic_json(destination / f"{stage}.collector.json", values)
        stages.append({"id": stage, "region": region, "group": group,
            "task_profile": "navigation_pickup",
            "route_count": len(batch), "group_sha256": hashlib.sha256(group_file.read_bytes()).hexdigest(),
            "group_semantic_sha256": semantic_hash(json.loads(group_file.read_text(encoding="utf-8"))),
            "required_tool_completions": {"Pathing": len(batch)},
            "anchor_unlocked_verified": False, "party_preconditions_verified": False,
            "status": "prepared_not_executed", "route_manifest": str(route_manifest),
            "route_coverage_candidates": [{"name": r["relative_path"], "features": r["coverage_features"], "verified": False} for r in batch],
            "coverage_candidates": ["navigation", "teleport", "pickup", "map_ui"]})
    result = {"schema_version": 2, "priority": [r[1] for r in regions], "stages": stages,
        "selection": "diverse_movement_locality_folder_length" if diverse else "short_routes",
        "candidate_counts": candidate_counts, "excluded_attempted_route_hashes": len(blocked),
        "total_distinct_routes": sum(s["route_count"] for s in stages),
        "target_bgi_hours": [30, 50], "target_human_hours": [20, 30],
        "stage_bound_minutes": 10, "session_bound_minutes": 120,
        "success_verified": False, "training_hours": 0,
        "limitation": "Queue covers food/navigation probes only. Story, quest, combat and puzzles remain required; native returns are unverified outcomes."}
    atomic_json(destination / "campaign.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--per-region", type=int, default=12)
    parser.add_argument("--diverse", action="store_true")
    parser.add_argument("--max-routes", type=int, choices=range(7, 85), default=48)
    args = parser.parse_args()
    result = build(args.name, args.per_region, args.diverse, args.max_routes)
    print(json.dumps(result, ensure_ascii=False, indent=2))
