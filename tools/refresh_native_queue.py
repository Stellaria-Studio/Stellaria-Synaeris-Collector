"""Reconstruct declared native groups after pinned BGI's benign serialization."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from synaeris_collector.config import Config
from synaeris_collector.native_groups import admissible_food, food_group, semantic_hash
from synaeris_collector.storage import atomic_json

ROOT = Path(__file__).resolve().parents[1]


def refresh(source, destination):
    source, destination = Path(source), Path(destination)
    if destination.exists():
        raise ValueError("Existing queue preserved")
    campaign = json.loads(source.read_text(encoding="utf-8-sig"))
    bgi = ROOT / ".external/bettergi-portable"
    staged = []
    for stage in campaign["stages"]:
        manifest = json.loads(Path(stage["route_manifest"]).read_text(encoding="utf-8-sig"))
        names = [r["name"] for r in manifest["routes"]]
        expected = food_group(stage["group"], names)
        legacy = deepcopy(expected)
        del legacy["config"]["pathingConfig"]["soloTaskUseFightEnabled"]
        group_path = bgi / "User/ScriptGroup" / (stage["group"] + ".json")
        original = group_path.read_bytes()
        actual = json.loads(original.decode("utf-8-sig"))
        if semantic_hash(actual) not in {semantic_hash(expected), semantic_hash(legacy)}:
            raise ValueError("Group controls changed; serialization repair is inappropriate")
        for route in manifest["routes"]:
            raw = (bgi / "User/AutoPathing/StellariaSynaeris" / stage["group"] / route["name"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != route["sha256"] or not admissible_food(json.loads(raw.decode("utf-8-sig"))):
                raise ValueError("Changed or unqualified route asset")
        staged.append((stage, expected, group_path, original))
    destination.mkdir()
    backups = destination / "group_before"
    backups.mkdir()
    for stage, expected, path, original in staged:
        (backups / path.name).write_bytes(original)
        atomic_json(path, expected)
        stage["group_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        stage["group_semantic_sha256"] = semantic_hash(expected)
        config = Config.load(source.parent / (stage["id"] + ".collector.json"))
        config.save(destination / (stage["id"] + ".collector.json"))
    campaign["serialization_contract"] = "Pinned BGI defaults; ignore display group index only; all execution settings and route bytes remain checked"
    atomic_json(destination / "campaign.json", campaign)
    return campaign


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("destination")
    args = parser.parse_args()
    result = refresh(args.source, args.destination)
    print(json.dumps({"stages": len(result["stages"]), "routes": result["total_distinct_routes"]}))
