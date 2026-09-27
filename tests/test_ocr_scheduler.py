import numpy as np

from synaeris_collector.config import Config
from synaeris_collector.ocr import OCRWorker


class Writer:
    def now(self):
        return 0

    def emit(self, *_args, **_kwargs):
        return True


def worker():
    config = Config(ocr_max_hz=4)
    return OCRWorker(config, Writer(), lambda *_: None)


def test_busy_ocr_queue_keeps_latest_frame_without_blocking_capture():
    item = worker()
    item.submit(np.full((2, 2, 3), 1, dtype=np.uint8), 1, 10)
    item.submit(np.full((2, 2, 3), 2, dtype=np.uint8), 2, 11)
    frame, timestamp, index = item.queue.get_nowait()
    assert frame[0, 0, 0] == 2 and timestamp == 2 and index == 11
    assert item.stats()["ocr_requests_coalesced"] == 1
    assert item.stats()["ocr_requests_submitted"] == 2


def test_unknown_mode_roi_budget_rotates_oldest_deadlines():
    item = worker()
    first = item.selected_rois("UNKNOWN", 0)
    assert first == ["quest", "interaction"]
    for name in first:
        item.last[name] = 0
    second = item.selected_rois("UNKNOWN", 250_000_000)
    assert second == ["dialogue", "options"]
    assert len(first) == len(second) == item.MAX_ROIS_PER_FRAME


def test_world_mode_never_schedules_more_than_two_rois_per_frame():
    item = worker()
    assert item.selected_rois("MAIN", 0) == ["quest", "interaction"]


def test_short_bgi_unknown_gap_keeps_last_positive_mode_for_ocr_only():
    item = worker()
    assert item.update_mode("Main", "bettergi", now=10.0) == "MAIN"
    assert item.update_mode("Unknown", "bettergi", now=11.0) == "MAIN"
    assert item.unknown_modes_suppressed == 1
    assert item.update_mode("Unknown", "bettergi", now=11.6) == "UNKNOWN"


def test_unknown_mode_does_not_schedule_unverified_bottom_hud_title_crop():
    item = worker()
    seen = set()
    for step in range(40):
        timestamp = step * 250_000_000
        selected = item.selected_rois("UNKNOWN", timestamp)
        seen.update(selected)
        for name in selected:
            item.last[name] = timestamp / 1e9
    assert "title" not in seen
    assert {"dialogue", "options", "map", "receipt"} <= seen
