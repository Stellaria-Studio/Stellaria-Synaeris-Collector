"""Prepare a bounded regional native combat qualification campaign.

Only typed BetterGI ``fight`` waypoints are admitted. Route command strings,
JavaScript, shell projects, story skipping and unknown move modes stay out.
Preparation does not claim that the account preconditions or combat outcomes
are valid; the first live pass remains review pending.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re

from synaeris_collector.bgi import catalog
from synaeris_collector.config import Config
from synaeris_collector.coverage import route_features, select_diverse
from synaeris_collector.native_groups import COMBAT_STRATEGY, route_contract, semantic_hash
from synaeris_collector.storage import atomic_json
from prepare_live_groups import prepare
from prepare_machine_queue import attempted_routes


ROOT = Path(__file__).resolve().parents[1]
SOURCE_BGI = Path("C:/Program Files/BetterGI")
PORTABLE_BGI = ROOT / ".external/bettergi-portable"
REGIONS = {"nodkrai": "挪德卡莱", "natlan": "纳塔"}


def build(name, max_routes=6, region_code="nodkrai", max_fights_per_route=4):
    if not re.fullmatch(r"[A-Za-z0-9_]+", name) or not 3 <= max_routes <= 12:
        raise ValueError("Invalid combat campaign name or route bound")
    if not 1 <= max_fights_per_route <= 12:
        raise ValueError("Invalid per-route fight bound")
    if region_code not in REGIONS:
        raise ValueError("Unsupported combat collection region")
    region = REGIONS[region_code]
    destination = ROOT / "data" / name
    if destination.exists():
        raise ValueError("Existing campaign is preserved; choose a new name")
    strategy_file = PORTABLE_BGI / "User/AutoFight" / (COMBAT_STRATEGY + ".txt")
    if not strategy_file.is_file():
        raise ValueError("Pinned universal combat strategy is unavailable")
    strategy = {"name": strategy_file.name,
                "sha256": hashlib.sha256(strategy_file.read_bytes()).hexdigest()}
    blocked = attempted_routes()
    candidates = []
    for route in catalog(SOURCE_BGI):
        in_region = ("挪德卡莱锄地小怪" in route["relative_path"] if region_code == "nodkrai"
                     else region in route["relative_path"])
        if not in_region or route["sha256"] in blocked:
            continue
        data = json.loads(Path(route["path"]).read_text(encoding="utf-8-sig"))
        contract = route_contract(data, "combat_pathing")
        if contract is None or contract["fight_waypoints"] > max_fights_per_route:
            continue
        route["contract"] = contract
        route["coverage_features"] = route_features(route, data)
        candidates.append(route)
    selected = select_diverse(candidates, max_routes)
    if len(selected) < 3:
        raise ValueError("Fewer than three unattempted admissible combat routes")
    destination.mkdir(parents=True)
    stages = []
    # One route per stage makes the first qualification failures attributable
    # and prevents a successful route from hiding a failed second route.
    for index, route in enumerate(selected):
        stage_id = f"{region_code}_combat_{index + 1:02d}"
        group = f"Synaeris_{name}_{stage_id}"
        route_manifest = destination / f"{stage_id}.routes.json"
        group_file = prepare([route["path"]], PORTABLE_BGI, group, route_manifest, "combat_pathing")
        config = Config(actor="BGI", region=region, quest=group, poi=group,
                        scenario="战斗与拾取", novel=False,
                        output=str(ROOT / "data/episodes"), bgi_dir=str(PORTABLE_BGI),
                        ocr_max_hz=4)
        atomic_json(destination / f"{stage_id}.collector.json", asdict(config))
        fights = route["contract"]["fight_waypoints"]
        stages.append({"id": stage_id, "region": region, "group": group,
            "task_profile": "combat_pathing", "route_count": 1,
            "route_manifest": str(route_manifest),
            "group_sha256": hashlib.sha256(group_file.read_bytes()).hexdigest(),
            "group_semantic_sha256": semantic_hash(json.loads(group_file.read_text(encoding="utf-8"))),
            "required_tool_completions": {"Pathing": 1, "AutoFight": fights},
            "combat_strategy": strategy, "anchor_unlocked_verified": False,
            "party_preconditions_verified": False, "status": "prepared_not_executed",
            "route_coverage_candidates": [{"name": route["relative_path"],
                "features": route["coverage_features"], "fight_waypoints": fights,
                "verified": False}],
            "coverage_candidates": ["navigation", "teleport", "combat", "pickup", "recovery"]})
    result = {"schema_version": 3, "priority": [region], "stages": stages,
        "selection": "strict_typed_fight_waypoints_diverse_short_first",
        "source_candidate_count": len(candidates), "excluded_attempted_route_hashes": len(blocked),
        "total_distinct_routes": len(stages),
        "required_auto_fight_invocations": sum(s["required_tool_completions"]["AutoFight"] for s in stages),
        "stage_bound_minutes": 10, "session_bound_minutes": 120,
        "success_verified": False, "training_hours": 0,
        "limitation": "Prepared routes and tool completions are technical evidence only. Enemy defeat, drops, rewards, party compatibility and account unlocks require live review."}
    atomic_json(destination / "campaign.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--max-routes", type=int, default=6)
    parser.add_argument("--region", choices=sorted(REGIONS), default="nodkrai")
    parser.add_argument("--max-fights-per-route", type=int, default=4)
    args = parser.parse_args()
    print(json.dumps(build(args.name, args.max_routes, args.region, args.max_fights_per_route), ensure_ascii=False, indent=2))
