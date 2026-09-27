"""Local campaign status and safe stop, without any elevated command endpoint."""
import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def status(stop=False):
    latest = ROOT / "reports" / "local" / "campaign" / "latest.json"
    attempts = list((ROOT / "reports" / "local" / "launch").glob("*.authorization.json"))
    authorization = None
    if attempts:
        attempt = json.loads(max(attempts, key=lambda p: p.stat().st_mtime).read_text(encoding="utf-8-sig"))
        authorization = {k: attempt.get(k) for k in ("status", "started_utc", "campaign_path", "error")}
    if not latest.exists():
        return {"status": "not_started", "authorization": authorization, "training_hours_verified": 0}
    link = json.loads(latest.read_text(encoding="utf-8-sig"))
    report_file = Path(link["report"]).resolve()
    campaign_directory = latest.parent.resolve()
    if report_file.parent != campaign_directory:
        raise ValueError("Campaign report must remain inside the local report directory")
    report = json.loads(report_file.read_text(encoding="utf-8-sig"))
    if report.get("worker") == "installed_task":
        authorization = {"status": "installed_task_dispatch", "request_id": report["request_id"],
                         "campaign_path": report["campaign_path"], "error": report.get("error")}
    current_status = report["status"]
    if (authorization and authorization.get("campaign_path") != report.get("campaign_path")
            and authorization["status"] in {"awaiting_windows_authorization", "not_dispatched"}):
        current_status = authorization["status"]
    if stop:
        # Fixed request file only; no arbitrary path or command from the report.
        (campaign_directory / "stop.request").touch()
    health = None
    try:
        with urllib.request.urlopen("http://127.0.0.1:18765/health", timeout=1) as response:
            health = json.load(response)
    except (OSError, urllib.error.URLError):
        pass
    return {"status": current_status, "previous_campaign_status": report["status"],
        "pid": report["pid"], "report": str(report_file),
        "authorization": authorization,
        "deadline_utc": report["deadline_utc"], "stop_requested": (campaign_directory / "stop.request").exists(),
        "training_hours_verified": report.get("training_hours_verified", 0), "live": health,
        "stages": [{k: s.get(k) for k in ("stage", "status", "episode_id", "error")}
                   for s in report.get("stages", [])]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop", action="store_true", help="Request current episode finalization and halt further stages")
    args = parser.parse_args()
    print(json.dumps(status(args.stop), ensure_ascii=False, indent=2))
