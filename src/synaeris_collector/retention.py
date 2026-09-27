"""Generate reviewable retention plans; never automatically delete visual mother data."""
import json
from pathlib import Path
from .storage import atomic_json, iter_rows
from .dataset import transitions


def plan(root, output):
    entries, seen_routes = [], {}
    protected_events = {"THINK", "MISTAKE", "DISCOVERY", "SUBGOAL_COMPLETE", "TOOL_FAILED",
                        "DEATH", "HUMAN_TAKEOVER", "PUZZLE_COMPLETE"}
    for file in sorted(Path(root).glob("*/manifest.json")):
        manifest = json.loads(file.read_text(encoding="utf-8"))
        config = manifest["config"]
        reasons = []
        if config.get("actor") != "BGI" or config.get("novel") or config.get("ood"):
            reasons.append("human_or_novel_or_ood")
        if manifest["status"] != "complete":
            reasons.append("incomplete_or_failed")
        if config.get("puzzle", "unknown") != "unknown":
            reasons.append("puzzle")
        for stream in ("events", "tools"):
            if any(row["kind"] in protected_events for row in iter_rows(file.parent, stream)):
                reasons.append("valuable_or_failure_event")
        mined = transitions(file.parent)
        if not mined or any(item["reward"] is None or item["outcome"] != "success" or item["weak_label"] for item in mined):
            reasons.append("no_fully_verified_success_transitions")
        fingerprint = tuple(item["action"].get("route_sha256", "") for item in mined)
        duplicate = bool(fingerprint) and all(fingerprint) and fingerprint in seen_routes
        if not duplicate:
            reasons.append("no_matching_verified_route_duplicate")
        if mined and all(item["outcome"] == "success" and not item["weak_label"] for item in mined):
            seen_routes.setdefault(fingerprint, manifest["episode_id"])
        entries.append({"episode_id": manifest["episode_id"], "action": "keep_full_video" if reasons else "review_structured_only_candidate",
                        "reasons": sorted(set(reasons)), "video_bytes": (file.parent/"visual.mkv").stat().st_size if (file.parent/"visual.mkv").exists() else 0})
    result = {"deletion_performed": False, "episodes": entries,
              "policy": "Human/novel/puzzle/failure/takeover protected. Only verified duplicate BGI successes can be reviewed."}
    atomic_json(output, result)
    return result
