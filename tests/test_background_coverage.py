import threading
import time
from types import SimpleNamespace
import numpy as np
import pytest
from synaeris_collector.config import Config
from synaeris_collector.coverage import select_diverse
from synaeris_collector.dataset import build_dataset
from synaeris_collector.storage import atomic_json
from synaeris_collector.window_capture import WindowCapture


def capture(background=True, minimized=False, age=0):
    value = WindowCapture.__new__(WindowCapture)
    value.window = SimpleNamespace(active=lambda: False, hwnd=1,
        user=SimpleNamespace(IsIconic=lambda _: minimized), rect=lambda: (0, 0, 64, 64))
    value.rect = (0, 0, 64, 64)
    value.background = background
    value.error = None
    value.closed = False
    value.lock = threading.Lock()
    value.last = np.full((64, 64, 3), 110, dtype=np.uint8)
    value.callback_qpc_ns = time.perf_counter_ns() - age
    value.native_timespan = 123
    value.generation, value.read_generation = 1, 0
    return value


def test_occluded_window_frame_preserves_focus_and_source_clock():
    value = capture()
    pixels, focused, fresh = value.grab()
    assert not focused and fresh and value.available and pixels.mean() == 110
    assert value.metadata["source_callback_qpc_ns"] == value.callback_qpc_ns
    assert value.metadata["source_native_timespan"] == 123
    assert not value.grab()[2]  # A cached frame is not a new capture.


@pytest.mark.parametrize("kwargs", [{"background": False}, {"minimized": True}, {"age": 3_000_000_000}])
def test_minimized_stale_or_foreground_only_frames_redact(kwargs):
    value = capture(**kwargs)
    pixels, focused, fresh = value.grab()
    assert not value.available and not focused and not fresh and pixels.max() == 0


def test_background_requires_targeted_window_capture_and_is_excluded_from_training(tmp_path):
    with pytest.raises(ValueError, match="window-targeted"):
        Config(capture="mss", capture_background=True).validate()
    episode = tmp_path / "episode"
    episode.mkdir()
    atomic_json(episode / "manifest.json", {"episode_id": "episode", "status": "complete", "synthetic": False,
                "config": {"capture_background": True}, "capabilities": {"input_permission_qualified": True}})
    result = build_dataset(tmp_path, tmp_path / "dataset.json")
    assert result["excluded"] == [{"episode_id": "episode", "reason": "background_action_alignment_unqualified"}]


def test_diverse_selection_covers_distinct_locality_movement_and_folders():
    candidates = [
        {"sha256": "a", "points": 2, "coverage_features": ["route_folder:mint", "movement:walk", "origin_tile:1:1"]},
        {"sha256": "b", "points": 3, "coverage_features": ["route_folder:mint", "movement:walk", "origin_tile:1:1"]},
        {"sha256": "c", "points": 60, "coverage_features": ["route_folder:crab", "movement:fly", "origin_tile:2:1"]},
    ]
    selected = select_diverse(candidates, 2)
    assert {item["sha256"] for item in selected} == {"a", "c"}
