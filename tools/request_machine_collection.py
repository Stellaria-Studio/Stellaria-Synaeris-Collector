"""Write one typed local collection job for the user-authorized app task."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import uuid
from synaeris_collector.machine_worker import sha256
from synaeris_collector.storage import atomic_json

ROOT = Path(__file__).resolve().parents[1]


def ensure_idle():
    import json
    import psutil
    latest = ROOT / "reports/local/campaign/latest.json"
    if not latest.exists():
        return
    reference = json.loads(latest.read_text(encoding="utf-8-sig"))
    report = Path(reference["report"]).resolve()
    if report.parent != latest.parent.resolve():
        raise ValueError("Campaign report escapes its local directory")
    state = json.loads(report.read_text(encoding="utf-8-sig"))
    if state["status"] in {"starting", "preflight", "recording", "qualifying"} and psutil.pid_exists(state["pid"]):
        raise RuntimeError("Active collection preserved; do not replace its pending request")


def request(campaign):
    ensure_idle()
    campaign = Path(campaign).resolve()
    relative = campaign.relative_to(ROOT).as_posix()
    import re
    if not re.fullmatch(r"data/[A-Za-z0-9_]+/campaign\.json", relative):
        raise ValueError("Only a local prepared collection queue is accepted")
    value = {"schema_version": 1, "request_id": uuid.uuid4().hex,
        "campaign_relative": relative, "campaign_sha256": sha256(campaign),
        "requested_utc": datetime.now(timezone.utc).isoformat()}
    target = ROOT / "reports/local/machine_worker"
    target.mkdir(parents=True, exist_ok=True)
    atomic_json(target / (value["request_id"] + ".request.json"), value)
    atomic_json(target / "request.json", value)
    # Seed a fresh protected runtime's no-replay ledger from real, native
    # route-start evidence, including previous quarantined attempts.
    from synaeris_collector.storage import iter_rows
    bgi = ROOT / ".external/bettergi-portable"
    attempted = set()
    import json
    for episode in (ROOT / "data/episodes").glob("*"):
        metadata = episode / "manifest.json"
        if not metadata.exists() or json.loads(metadata.read_text(encoding="utf-8")).get("synthetic"):
            continue
        for row in iter_rows(episode, "tools"):
            payload = row["payload"]
            route, folder = payload.get("route", ""), payload.get("folder", "")
            if (row["source"] == "bettergi_native" and row["kind"] == "TOOL_START"
                    and re.fullmatch(r"StellariaSynaeris\\Synaeris_[A-Za-z0-9_]+", folder)
                    and Path(route).name == route and route.endswith(".json")):
                asset = bgi / "User/AutoPathing" / folder / route
                if asset.exists():
                    attempted.add(sha256(asset))
    atomic_json(target / "previous_attempts.json", {"hashes": sorted(attempted)})
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("campaign")
    args = parser.parse_args()
    import json
    print(json.dumps(request(args.campaign), ensure_ascii=False, indent=2))
