"""Prepare source-aligned review evidence; never approve training automatically."""
import argparse
from collections import Counter
import json
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw
from synaeris_collector.dataset import export_frame
from synaeris_collector.storage import atomic_json, iter_rows


def percentiles(values):
    return {name: float(np.percentile(values, percentile)) if values else None
            for name, percentile in (("p50", 50), ("p95", 95), ("max", 100))}


def prepare(episode, output):
    episode, output = Path(episode).resolve(), Path(output).resolve()
    manifest = json.loads((episode / "manifest.json").read_text(encoding="utf-8"))
    if manifest["status"] != "complete" or manifest.get("synthetic"):
        raise ValueError("Review preparation requires a closed real episode")
    output.mkdir(parents=True, exist_ok=False)
    rows = {stream: list(iter_rows(episode, stream)) for stream in manifest["streams"]}
    native = [r for stream in rows.values() for r in stream if r["source"] == "bettergi_native"]
    receipts = [r for r in rows["objects"] if r["kind"] == "ITEM_RECEIPT_CANDIDATE"]
    scans = [r["payload"] for r in rows["ocr"] if r["kind"] == "OCR_SCAN"]
    sources = Counter((r["source"], r["actor"]) for r in rows["input"])
    summary = {
        "episode_id": manifest["episode_id"], "source_episode": str(episode),
        "raw_seconds": manifest["duration_ns"] / 1e9,
        "encoded_frames": manifest["encoded_frames"],
        "input_provenance": [{"source": s, "actor": a, "count": n}
                             for (s, a), n in sources.items()],
        "ui_modes": dict(Counter(r["payload"].get("mode", "UNKNOWN") for r in rows["ui"])),
        "native_event_count": len(native),
        "bridge_delay_ms": percentiles([r["payload"]["bridge_delay_ns"] / 1e6 for r in native
                                         if "bridge_delay_ns" in r["payload"]]),
        "ocr_latency_ms": percentiles([r["latency_ms"] for r in scans]),
        "ocr_latency_by_roi_ms": {roi: percentiles([r["latency_ms"] for r in scans if r["roi"] == roi])
                                  for roi in sorted({r["roi"] for r in scans})},
        "ocr_observation_queue_drops": manifest.get("ocr_queue_drops"),
        "tool_events": [r for r in rows["tools"] if r["kind"] in
                        {"TOOL_START", "TOOL_END", "TOOL_FAILED", "TOOL_CANCELLED"}],
        "interaction_attempts": [r for r in rows["objects"] if r["kind"] == "INTERACTION_ATTEMPT"],
        "receipt_candidates": receipts,
        "performance": {key: percentiles([r["payload"][key] for r in rows["perf"]
                                           if isinstance(r["payload"].get(key), (int, float))])
                        for key in ("collector_rss_bytes", "collector_cpu_pct", "vram_used_mib",
                                    "capture_latency_ms", "encode_submit_latency_ms")},
        "training_eligible": False, "review_required": True,
        "limitation": "Prepared samples are evidence for review, not full-video review or reward verification.",
    }
    frames = rows["frames"]
    selected = [(frames[int((len(frames) - 1) * fraction)]["monotonic_timestamp"], label)
                for fraction, label in ((0.03, "start"), (0.5, "middle"), (0.97, "end"))]
    maps = [r for r in rows["ui"] if r["payload"].get("mode") == "BigMap"]
    if maps:
        selected.append((maps[len(maps) // 2]["monotonic_timestamp"], "map"))
    # Pick at most four separated candidate timestamps; retain every candidate in JSON.
    for row in receipts:
        stamp = row["monotonic_timestamp"]
        if sum(label == "receipt_candidate" for _, label in selected) >= 4:
            break
        if all(abs(stamp - t) >= 5_000_000_000 for t, label in selected if label == "receipt_candidate"):
            selected.append((stamp, "receipt_candidate"))
    samples, seen = [], set()
    for stamp, label in sorted(selected):
        if stamp in seen:
            continue
        seen.add(stamp)
        mapping = export_frame(episode, stamp, output / f"sample_{len(samples):02d}.png")
        samples.append({"purpose": label, **mapping})
    width, height = 960, 540
    sheet = Image.new("RGB", (width * 2, (height + 34) * ((len(samples) + 1) // 2)), "#141414")
    draw = ImageDraw.Draw(sheet)
    for index, sample in enumerate(samples):
        x, y = (index % 2) * width, (index // 2) * (height + 34)
        with Image.open(sample["path"]) as source:
            sheet.paste(source.resize((width, height)), (x, y))
        draw.text((x + 8, y + height + 8),
                  f"{sample['purpose']} | frame {sample['frame_index']} | QPC {sample['acquisition_timestamp_ns']/1e9:.3f}s",
                  fill="white")
    sheet.save(output / "contact_sheet.png")
    summary["samples"] = samples
    summary["contact_sheet"] = str(output / "contact_sheet.png")
    atomic_json(output / "evidence.json", summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("episode")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = prepare(args.episode, args.output)
    print(json.dumps({key: result[key] for key in ("episode_id", "raw_seconds", "encoded_frames",
                     "bridge_delay_ms", "contact_sheet", "training_eligible")}, ensure_ascii=False, indent=2))
