from pathlib import Path
import socket
import cv2
import numpy as np
from .config import Config
from .collector import Collector
from .dataset import inspect_episode, build_dataset
from .ocr import LocalOCR
from .storage import atomic_json
from .auto_index import organize


def run(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Self-test telemetry must not collide with a live machine collector.
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    path = Collector(Config(output=str(output / "episodes"), capture="synthetic",
                            bridge_port=port, ocr=True)).run(3)
    quality = inspect_episode(path)
    index = organize(path)
    image = np.full((120, 500, 3), 255, np.uint8)
    cv2.putText(image, "Synaeris OCR TEST 123", (10, 70), 0, 1.1, (0, 0, 0), 2)
    rows, latency = LocalOCR().read(image)
    export = build_dataset(output / "episodes", output / "dataset.json")
    passed = (not quality["issues"] and any("TEST 123" in r["text"] for r in rows)
              and not any(export["splits"].values()) and bool(index['segments'])
              and sum(part['frame_count'] for part in index['segments']) == quality['decoded_frames']
              and all(label['reward'] is None and label['verified'] is False for label in index['labels']))
    atomic_json(output / "selftest.json", {"passed": passed, "episode": str(path), "quality": quality,
        "ocr": rows, "ocr_latency_ms": latency, "synthetic_excluded": not any(export["splits"].values()),
        'auto_index_version': index['version'], 'auto_index_segments': len(index['segments'])})
    if not passed:
        raise RuntimeError("Packaged collector self-test failed")
    return output / "selftest.json"
