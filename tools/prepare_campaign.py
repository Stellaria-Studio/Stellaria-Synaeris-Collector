"""Prepare local all-category coverage targets and three conservative native probe stages."""
import json
from pathlib import Path
from collections import Counter
from synaeris_collector.bgi import catalog
from synaeris_collector.config import Config
from prepare_live_groups import prepare

ROOT = Path(__file__).resolve().parents[1]
BGI = ROOT / ".external" / "bettergi-portable"
OUT = ROOT / "data" / "campaign_round02"


def main():
    routes = catalog("C:/Program Files/BetterGI")
    definitions = [
        ("nodkrai", "挪德卡莱", ["02-鸟蛋-挪德卡莱-2窝.json", "04-鸟蛋-挪德卡莱-2窝.json"]),
        ("natlan", "纳塔", ["F04-薄荷-纳塔-流泉之众3.json", "F05-薄荷-纳塔-流泉之众4.json"]),
        ("mondstadt", "蒙德", ["A05-薄荷-蒙德-摘星崖.json", "A09-薄荷-蒙德-风起地3.json"]),
    ]
    stages = []
    OUT.mkdir(parents=True, exist_ok=True)
    for code, region, names in definitions:
        selected = [next(item for item in routes if Path(item["path"]).name == name) for name in names]
        actions = []
        for item in selected:
            data = json.loads(Path(item["path"]).read_text(encoding="utf-8-sig"))
            actions.extend(position.get("action_params", "") for position in data["positions"])
        group = "Synaeris_R02_" + code
        prepare([item["path"] for item in selected], BGI, group, OUT / (code + ".routes.json"))
        config = Config(actor="BGI", region=region, quest=group, poi=group, novel=True)
        config.bgi_dir = str(BGI)
        config.save(OUT / (code + ".collector.json"))
        stages.append({"id": code, "region": region, "group": group,
            "route_count": len(selected), "action_params": actions,
            "anchor_unlocked_verified": False, "party_preconditions_verified": False,
            "status": "prepared_not_executed", "initial_capture_seconds": 600,
            "manual_verification": "Confirm transfer/world UI, picked prompt change, progress and recovery from mother video"})
    coverage = ["navigation", "teleport", "pickup", "combat", "dialogue", "quest",
        "puzzle", "map_ui", "unknown_ui", "failure_recovery", "takeover", "localization_loss"]
    result = {"schema_version": 1, "priority": ["挪德卡莱", "纳塔", "老地区"], "stages": stages,
        "coverage_targets": {key: {"required": True, "verified_episodes": 0, "minimum_initial_episodes": 3}
            for key in coverage},
        "route_library_count": len(routes),
        "region_route_candidates": {region: sum(region in item["relative_path"] for item in routes)
            for region in ("挪德卡莱", "纳塔", "蒙德")},
        "limitation": "Three probe groups cover transfer/navigation/pickup candidates only; other categories need observed tasks or qualified scripts"}
    (OUT / "campaign.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
