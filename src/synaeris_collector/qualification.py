import json
from pathlib import Path
from .dataset import inspect_episode, transitions
from .storage import atomic_json, iter_rows


def qualify(path, inspect=None):
    path = Path(path)
    quality = (inspect or inspect_episode)(path)
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    accepted = rejected = main_ui = failures = 0
    producer_drops = 0
    diagnostics = {}
    latest_diagnostic_ns = -1
    for stream in manifest["streams"]:
        for row in iter_rows(path, stream):
            payload = row["payload"]
            if row["source"] != "bettergi_native":
                continue
            producer_drops = max(producer_drops, payload.get("producer_telemetry_drops", 0))
            if (payload.get("producer_diagnostics")
                    and row["monotonic_timestamp"] >= latest_diagnostic_ns):
                diagnostics = payload["producer_diagnostics"]
                latest_diagnostic_ns = row["monotonic_timestamp"]
            if row["kind"] == "BGI_INPUT_DISPATCH":
                if payload.get("dispatch_accepted") is True:
                    accepted += 1
                else:
                    rejected += 1
            if stream == "ui" and payload.get("mode") == "Main":
                main_ui += 1
            if row["kind"] == "TOOL_FAILED":
                failures += 1
    problems = list(quality["issues"])
    if manifest.get("synthetic") or manifest.get("status") != "complete":
        problems.append("synthetic_or_incomplete")
    if manifest.get("capabilities", {}).get("input_permission_qualified") is not True:
        problems.append("input_permission_unqualified")
    if accepted == 0:
        problems.append("no_native_accepted_input_dispatch")
    if rejected:
        problems.append("native_input_dispatch_rejected")
    if failures:
        problems.append("native_tool_failed")
    if producer_drops:
        problems.append("native_telemetry_dropped")
    if main_ui == 0:
        problems.append("no_native_world_ui_observation")
    if not quality["decoded_frames"]:
        problems.append("empty_video")
    frame_count = focused_count = 0
    for frame in iter_rows(path, "frames"):
        frame_count += 1
        focused_count += frame["payload"].get("game_focused") is True
    focused_fraction = focused_count / frame_count if frame_count else 0
    if focused_fraction < 0.8:
        problems.append("game_focused_frame_fraction_below_0.8")
    mined = transitions(path)
    report = {"episode_id": manifest["episode_id"], "continuation_allowed": not problems,
        "problems": sorted(set(problems)), "raw_seconds": manifest.get("duration_ns", 0) / 1e9,
        "native_accepted_input_events": accepted, "native_rejected_input_events": rejected,
        "native_main_ui_events": main_ui, "native_tool_failures": failures,
        "producer_drops": producer_drops, "producer_diagnostics": diagnostics,
        "decoded_frames": quality["decoded_frames"], "decode_backend": quality["video_decode_backend"],
        "game_focused_frame_fraction": focused_fraction,
        "transition_count": len(mined), "verified_transition_count": sum(not t["weak_label"] for t in mined),
        "training_eligible": False, "review_required": True,
        "limitation": "Continuation qualification is technical. Route success, actual regions, item receipts and action alignment require mother-video review."}
    atomic_json(path / "machine_qualification.json", report)
    return report
