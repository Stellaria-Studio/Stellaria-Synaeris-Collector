"""Compare preprocessing budgets on an existing real calibration frame only."""
import argparse
import json
import time
from pathlib import Path
import cv2
import numpy as np
from synaeris_collector.ocr import LocalOCR, roi_crop
from synaeris_collector.config import Config
from synaeris_collector.storage import atomic_json

parser = argparse.ArgumentParser()
parser.add_argument("frame")
parser.add_argument("--config", required=True)
parser.add_argument("--roi", default="map")
parser.add_argument("--expected", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()
image = cv2.imdecode(np.fromfile(args.frame, dtype=np.uint8), cv2.IMREAD_COLOR)
if image is None:
    raise ValueError("Unable to decode source frame")
config = Config.load(args.config)
crop, offset = roi_crop(image, config.rois[args.roi])
engine = LocalOCR(config.ocr_threads)
results = []
for threads in (20, 2, 20, 2):
    cv2.setNumThreads(threads)
    engine.read(crop, offset)
    samples = []
    for _ in range(4):
        start = time.process_time_ns()
        rows, latency = engine.read(crop, offset)
        samples.append({"wall_ms": latency, "process_cpu_ms": (time.process_time_ns()-start)/1e6,
                        "expected_found": args.expected in "".join(r["text"] for r in rows)})
    results.append({"opencv_threads": threads, "onnx_threads": config.ocr_threads, "samples": samples})
report = {"source_frame": str(Path(args.frame).resolve()), "roi": args.roi,
          "expected_text": args.expected, "results": results,
          "scope": "Repeated single-frame OCR; not end-to-end gameplay performance"}
atomic_json(args.output, report)
print(json.dumps({"budgets": [{"threads": r["opencv_threads"],
    "wall_ms_median": float(np.median([s["wall_ms"] for s in r["samples"]])),
    "cpu_ms_median": float(np.median([s["process_cpu_ms"] for s in r["samples"]])),
    "all_targets_found": all(s["expected_found"] for s in r["samples"])} for r in results]}, ensure_ascii=True))
