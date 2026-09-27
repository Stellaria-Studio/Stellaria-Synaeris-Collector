"""Summarize real stream evidence; frame samples remain mother-video references."""
import argparse
from collections import Counter
import json
from pathlib import Path

import cv2
import numpy as np
from synaeris_collector.storage import atomic_json, iter_rows


def percentiles(values):
    return {f"p{p}": float(np.percentile(values, p)) for p in (50, 95, 100)} if values else None


def analyze(path):
    path = Path(path)
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    native, tools, objects = [], [], []
    modes, input_actors = Counter(), Counter()
    scans, perf = [], []
    for stream in manifest["streams"]:
        for row in iter_rows(path, stream):
            payload = row["payload"]
            if row["source"] == "bettergi_native":
                native.append(row)
            if stream == "tools" and row["kind"] in {"TOOL_START", "TOOL_END", "TOOL_FAILED"}:
                tools.append({"seconds": row["monotonic_timestamp"] / 1e9, "kind": row["kind"], **payload})
            if stream == "objects" and row["kind"] == "INTERACTION_ATTEMPT":
                objects.append({"seconds": row["monotonic_timestamp"] / 1e9,
                                "target": payload.get("target_text"), "verified": False})
            if stream == "ui" and row["source"] == "bettergi_native":
                modes[payload.get("mode", "unknown")] += 1
            if stream == "input":
                input_actors[f"{row['source']}:{row['actor']}"] += 1
            if stream == "ocr" and row["kind"] == "OCR_SCAN":
                scans.append(payload)
            if stream == "perf":
                perf.append(payload)
    native.sort(key=lambda r: (r["monotonic_timestamp"], r["sequence"]))
    drops, previous = [], 0
    for row in native:
        count = row["payload"].get("producer_telemetry_drops", 0)
        if count > previous:
            drops.append({"seconds": row["monotonic_timestamp"] / 1e9, "cumulative": count,
                          "increase": count - previous, "diagnostics": row["payload"].get("producer_diagnostics")})
        previous = max(previous, count)
    report = {"episode_id": manifest["episode_id"], "raw_seconds": manifest["duration_ns"] / 1e9,
        "native_events": len(native), "native_ui_modes": dict(modes), "input_sources": dict(input_actors),
        "bridge_delay_ms": percentiles([r["payload"]["bridge_delay_ns"] / 1e6 for r in native
                                        if "bridge_delay_ns" in r["payload"]]),
        "drop_changes": drops, "tool_events": tools, "interaction_attempts": objects,
        "ocr_scan_payload_example": scans[:1], "perf_payload_example": perf[:1],
        "ocr_latency_ms": percentiles([r["latency_ms"] for r in scans if "latency_ms" in r]),
        "verified_task_success": False}
    atomic_json(path / "analysis.json", report)
    # Six time samples per sheet, without modifying or replacing mother data.
    seconds = sorted(set([0, 5, 20, 40, 60, 80, 100, 120, report["raw_seconds"] - 2] +
                         [max(0, o["seconds"] + .7) for o in objects]))
    frames = list(iter_rows(path, "frames"))
    stamps = np.array([f["monotonic_timestamp"] / 1e9 for f in frames])
    from synaeris_collector.dataset import export_frame
    sample_dir = path / "review_samples"
    sample_dir.mkdir(exist_ok=True)
    for page in range((len(seconds) + 5) // 6):
        sheet = np.full((2 * 306, 3 * 512, 3), 16, dtype=np.uint8)
        for cell, sec in enumerate(seconds[page * 6:(page + 1) * 6]):
            idx = max(0, int(np.searchsorted(stamps, sec, side="right")) - 1)
            sample = sample_dir / f"frame_{idx:06d}.png"
            export_frame(path, int(frames[idx]["monotonic_timestamp"]), sample)
            frame = cv2.imdecode(np.fromfile(sample, dtype=np.uint8), cv2.IMREAD_COLOR)
            thumb = cv2.resize(frame, (512, 288))
            x, y = cell % 3 * 512, cell // 3 * 306
            sheet[y:y + 288, x:x + 512] = thumb
            cv2.putText(sheet, f"{stamps[idx]:.3f}s frame {idx}", (x + 8, y + 303), 0, .46, (230, 230, 230), 1)
        cv2.imencode(".jpg", sheet)[1].tofile(sample_dir / f"sheet_{page:02d}.jpg")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("episode")
    args = parser.parse_args()
    result = analyze(args.episode)
    print(json.dumps({k: v for k, v in result.items() if k != "drop_changes"}, ensure_ascii=False, indent=2))
    print(json.dumps({"drop_changes_first": result["drop_changes"][:5], "last": result["drop_changes"][-2:]}, ensure_ascii=False, indent=2))
