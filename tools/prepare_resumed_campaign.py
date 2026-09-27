"""Preserve completed probes and prepare the unexecuted immutable queue."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from synaeris_collector.config import Config

from synaeris_collector.storage import atomic_json, iter_rows

ROOT = Path(__file__).resolve().parents[1]


def prepare(campaign_path, report_path, destination):
    campaign_path, report_path, destination = map(Path, (campaign_path, report_path, destination))
    campaign = json.loads(campaign_path.read_text(encoding="utf-8-sig"))
    report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    campaign_hash = hashlib.sha256(campaign_path.read_bytes()).hexdigest()
    if report["campaign_sha256"].lower() != campaign_hash:
        raise ValueError("Report does not match the immutable campaign")
    if destination.exists():
        raise ValueError("Existing resumed queue is preserved")
    executed = {}
    for record in report["stages"]:
        if not record.get("episode_id") or record.get("episode_status") != "complete":
            continue
        identity = record["episode_id"]
        if not re.fullmatch(r"\d{8}T\d{6}Z_[0-9a-f]{10}", identity):
            raise ValueError("Invalid episode identity")
        episode = ROOT / "data/episodes" / identity
        manifest = json.loads((episode / "manifest.json").read_text(encoding="utf-8"))
        stage = next(s for s in campaign["stages"] if s["id"] == record["stage"])
        if (manifest["status"] != "complete" or manifest.get("synthetic")
                or manifest["config"]["quest"] != stage["group"]):
            raise ValueError("Completed probe metadata mismatch")
        returned = {r["payload"]["tool_id"] for r in iter_rows(episode, "tools")
                    if r["source"] == "bettergi_native" and r["kind"] == "TOOL_END"
                    and r["payload"].get("tool") == "Pathing"}
        if len(returned) != stage["route_count"]:
            raise ValueError("Probe did not return all routes; do not automatically skip or replay it")
        executed[stage["id"]] = {"stage": stage["id"], "episode_id": identity,
            "technical_status": record["status"], "training_eligible": False,
            "reason": "Previously executed; preserve evidence and avoid depleted automatic replay"}
    remaining = [s for s in campaign["stages"] if s["id"] not in executed]
    if not remaining or not executed:
        raise ValueError("No evidence-backed completed probe or no remaining queue")
    destination.mkdir(parents=True)
    for stage in remaining:
        config = Config.load(campaign_path.parent / (stage["id"] + ".collector.json"))
        config.rois.setdefault("receipt", Config().rois["receipt"])
        config.save(destination / (stage["id"] + ".collector.json"))
    result = {**campaign, "stages": remaining,
        "total_distinct_routes": sum(s["route_count"] for s in remaining),
        "resumed_from": {"campaign": str(campaign_path.resolve()), "sha256": campaign_hash,
                         "report": str(report_path.resolve()), "excluded_executed": list(executed.values())}}
    atomic_json(destination / "campaign.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("campaign")
    parser.add_argument("report")
    parser.add_argument("destination")
    args = parser.parse_args()
    result = prepare(args.campaign, args.report, args.destination)
    print(json.dumps({"remaining_stages": len(result["stages"]),
        "remaining_routes": result["total_distinct_routes"],
        "preserved": result["resumed_from"]["excluded_executed"]}, ensure_ascii=False, indent=2))
