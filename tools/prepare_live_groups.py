"""Build immutable native route groups, without requiring JavaScript HTTP permissions."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
from synaeris_collector.native_groups import native_group, semantic_hash


def prepare(routes, bgi_dir, name, output, task_profile="navigation_pickup"):
    if not name or name in {".", ".."} or any(char in name for char in '\\/:*?"<>|'):
        raise ValueError("Invalid native group name")
    root = Path(bgi_dir).resolve()
    assets = root / "User" / "AutoPathing" / "StellariaSynaeris" / name
    group_file = root / "User" / "ScriptGroup" / (name + ".json")
    if assets.exists() or group_file.exists():
        raise ValueError("Existing group must be preserved; choose a new name")
    if not routes:
        raise ValueError("No routes selected")
    validated = []
    for route in routes:
        file = Path(route).resolve()
        data = json.loads(file.read_text(encoding="utf-8-sig"))
        if not data.get("positions"):
            raise ValueError(f"Invalid route: {file}")
        validated.append((file, data))
    assets.mkdir(parents=True)
    projects, metadata = [], []
    for i, (file, data) in enumerate(validated):
        target_name = f"{i+1:02d}-{file.name}"
        shutil.copyfile(file, assets / target_name)
        projects.append({"name": target_name,
            "folderName": "StellariaSynaeris\\" + name, "index": i+1,
            "type": "Pathing", "status": "Enabled", "schedule": "Daily", "runNum": 1})
        metadata.append({"source": str(file), "name": target_name,
            "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
            "points": len(data["positions"]), "info": data.get("info", {}),
            "verified_success": False})
    group = native_group(name, [item["name"] for item in metadata], task_profile)
    group_file.write_text(json.dumps(group, ensure_ascii=False, indent=2), encoding="utf-8")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"group": name, "task_profile": task_profile,
        "routes": metadata, "group_semantic_sha256": semantic_hash(group),
        "launch": ["BetterGI.exe", "--startGroups", name], "no_js_http_permission": True},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return group_file


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("routes", nargs="+")
    parser.add_argument("--bgi-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--task-profile", choices=("navigation_pickup", "combat_pathing"), default="navigation_pickup")
    args = parser.parse_args()
    print(prepare(args.routes, args.bgi_dir, args.name, args.output, args.task_profile))
