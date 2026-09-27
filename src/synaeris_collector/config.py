from dataclasses import asdict, dataclass, field
from pathlib import Path
import json


@dataclass
class Config:
    output: str = "data/episodes"
    purpose: str = "gameplay"
    actor: str = "HUMAN"
    region: str = "unknown"
    quest: str = "unknown"
    puzzle: str = "unknown"
    poi: str = "unknown"
    novel: bool = True
    ood: bool = False
    capture: str = "auto"
    capture_background: bool = False
    scenario: str = "unspecified"
    codec: str = "auto"
    fps: int = 30
    observation_hz: float = 5.0
    perf_hz: float = 2.0
    ocr: bool = True
    ocr_threads: int = 2
    ocr_max_hz: float = 8.0
    window_title: str = "原神"
    ffmpeg: str = ""
    bgi_dir: str = "C:/Program Files/BetterGI"
    bridge_port: int = 18765
    storage_budget_gb: float = 200.0
    reserve_free_gb: float = 25.0
    rois: dict = field(default_factory=lambda: {
        "quest": [0.015, 0.20, 0.32, 0.40],
        "interaction": [0.48, 0.36, 0.85, 0.68],
        "receipt": [0.005, 0.42, 0.25, 0.65],
        # Dialogue text ends above the bottom HUD. 0.94 included the HP/level
        # strip in real 2560x1440 world and combat recordings.
        "dialogue": [0.10, 0.68, 0.91, 0.90],
        "options": [0.57, 0.30, 0.95, 0.76],
        "map": [0.65, 0.10, 0.99, 0.95],
        "title": [0.40, 0.92, 0.60, 0.975],
    })

    def validate(self):
        if self.purpose not in {"gameplay", "calibration"}:
            raise ValueError("Invalid collection purpose")
        if self.actor not in {"HUMAN", "BGI", "AGENT", "SYNTHETIC"}:
            raise ValueError("Unsupported actor")
        if not 1 <= self.fps <= 120 or not 0 < self.observation_hz <= self.fps:
            raise ValueError("Invalid capture/observation frequency")
        if not 0 < self.ocr_max_hz <= 10 or not 0 < self.perf_hz <= 10:
            raise ValueError("Invalid OCR/performance frequency")
        if not 1 <= self.ocr_threads <= 16 or not 1024 <= self.bridge_port <= 65535:
            raise ValueError("Invalid threads/bridge port")
        if self.storage_budget_gb <= 0 or self.reserve_free_gb < 0:
            raise ValueError("Invalid storage budget")
        if self.capture not in {"auto", "dxcam", "mss", "wgc", "synthetic"}:
            raise ValueError("Invalid capture backend")
        if self.capture_background and self.capture != "wgc":
            raise ValueError("Background recording requires window-targeted WGC")
        if self.codec not in {"auto", "av1_nvenc", "hevc_nvenc", "libx264"}:
            raise ValueError("Invalid encoder")
        for roi in self.rois.values():
            if len(roi) != 4 or not (0 <= roi[0] < roi[2] <= 1 and 0 <= roi[1] < roi[3] <= 1):
                raise ValueError("ROI must be normalized [left, top, right, bottom]")
        return self

    @classmethod
    def load(cls, path=None):
        return cls(**json.loads(Path(path).read_text(encoding="utf-8-sig"))).validate() if path else cls()

    def save(self, path):
        Path(path).write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
