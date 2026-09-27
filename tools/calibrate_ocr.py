"""Evaluate/save OCR ROIs against a real mother-video frame, with explicit expected text."""
import argparse
import json
from pathlib import Path
import cv2
import numpy as np
from synaeris_collector.config import Config
from synaeris_collector.dataset import export_frame
from synaeris_collector.ocr import LocalOCR, roi_crop
from synaeris_collector.storage import atomic_json


def calibrate(episode, timestamp, config, output, expected=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    mapping = export_frame(episode, round(timestamp*1e9), output / "frame.png")
    image = cv2.imdecode(np.fromfile(output / "frame.png", dtype=np.uint8), cv2.IMREAD_COLOR)
    engine = LocalOCR(config.ocr_threads)
    reports = {}
    for name, roi in config.rois.items():
        crop, offset = roi_crop(image, roi)
        cv2.imencode(".png", crop)[1].tofile(output / (name + ".png"))
        rows, cold = engine.read(crop, offset)
        rows, warm = engine.read(crop, offset)
        target = (expected or {}).get(name)
        text = "".join(row["text"] for row in rows)
        reports[name] = {"normalized_roi": roi, "rows": rows,
            "latency_ms_first": cold, "latency_ms_warm": warm,
            "expected_text": target, "expected_found": target in text if target else None,
            "status": "verified_target" if target and target in text else "needs_live_review"}
    result = {"source_episode": str(Path(episode).resolve()), "frame_mapping": mapping,
        "frame_size": [image.shape[1], image.shape[0]], "rois": reports,
        "limitation": "Only explicit targets are verified; this is not full-scene OCR accuracy"}
    atomic_json(output / "calibration.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("episode")
    parser.add_argument("--timestamp", required=True, type=float)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected", default="{}")
    args = parser.parse_args()
    result = calibrate(args.episode, args.timestamp, Config.load(args.config), args.output,
        json.loads(args.expected))
    print(json.dumps({name: {k: row[k] for k in ("status", "latency_ms_warm", "expected_found")}
        for name, row in result["rois"].items()}, ensure_ascii=False, indent=2))
