import json
import time
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection

import pytest

from synaeris_collector.bridge import TelemetryBridge
from synaeris_collector.config import Config
from synaeris_collector.storage import EpisodeWriter, iter_rows
from synaeris_collector.dataset import export_frame


def native_item(writer, identity="producer:1"):
    return {"episode_id": writer.episode_id, "stream": "input", "kind": "BGI_INPUT_DISPATCH",
            "source": "bettergi_native", "qpc_ns": time.perf_counter_ns(),
            "producer_event_id": identity, "producer_sequence": 1,
            "payload": {"dispatch_accepted": True, "game_effect_verified": False}}


def test_concurrent_ack_retries_preserve_one_source_clock_and_input(tmp_path):
    writer = EpisodeWriter(Config(output=str(tmp_path)))
    bridge = TelemetryBridge(writer, 0)
    item = native_item(writer)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(pool.map(bridge.ingest,
                [{**item, "telemetry_diagnostics": {"transport_retries": i}} for i in range(24)]))
        assert replies.count(True) == 1 and replies.count(False) == 23
        with pytest.raises(ValueError, match="different content"):
            bridge.ingest({**item, "payload": {"dispatch_accepted": False}})
    finally:
        bridge.server.server_close()
        writer.close()
    rows = list(iter_rows(writer.path, "input"))
    assert len(rows) == 1 and rows[0]["actor"] == "BGI"
    assert rows[0]["monotonic_timestamp"] == item["qpc_ns"] - writer.origin_ns
    assert rows[0]["payload"]["producer_event_id"] == item["producer_event_id"]
    assert rows[0]["payload"]["game_effect_verified"] is False


def test_keepalive_retries_do_not_double_count_completed_routes(tmp_path):
    writer = EpisodeWriter(Config(output=str(tmp_path)))
    bridge = TelemetryBridge(writer, 0)
    bridge.start()
    connection = HTTPConnection("127.0.0.1", bridge.server.server_port, timeout=3)
    try:
        connection.request("GET", "/session")
        response = connection.getresponse()
        session = json.loads(response.read())
        assert response.version == 11
        socket_before = connection.sock
        for index, kind in enumerate(("TOOL_START", "TOOL_END")):
            item = {**native_item(writer, f"producer:{index}"), "stream": "tools", "kind": kind,
                    "payload": {"tool_id": "a", "tool": "Pathing", "verified": False}}
            for retry in range(2):
                connection.request("POST", "/telemetry", json.dumps(item),
                    {"X-Synaeris-Token": session["token"], "Content-Type": "application/json"})
                response = connection.getresponse()
                assert response.status == 200
                assert json.loads(response.read())["duplicate"] == bool(retry)
        connection.request("GET", "/health")
        health = json.loads(connection.getresponse().read())
        assert connection.sock is socket_before
        assert health["completed_pathing_routes"] == 1
        assert health["active_tool_count"] == 0 and health["duplicate_native_events"] == 2
    finally:
        connection.close()
        bridge.close()
        writer.close()
    assert len(list(iter_rows(writer.path, "tools"))) == 2


def test_finalizing_episode_never_acknowledges_a_missing_row(tmp_path):
    writer = EpisodeWriter(Config(output=str(tmp_path)))
    bridge = TelemetryBridge(writer, 0)
    writer.close()
    try:
        with pytest.raises(ValueError, match="finalizing"):
            bridge.ingest(native_item(writer))
    finally:
        bridge.server.server_close()


def test_frame_export_uses_acquisition_clock_and_exact_encoded_ordinal(tmp_path):
    import cv2
    import numpy as np
    writer = EpisodeWriter(Config(output=str(tmp_path)))
    video = cv2.VideoWriter(str(writer.path / "visual.mkv"), cv2.VideoWriter_fourcc(*"FFV1"), 30, (32, 32))
    assert video.isOpened()
    for index, stamp in enumerate((0, 8_000_000_000, 15_000_000_000, 30_000_000_000)):
        video.write(np.full((32, 32, 3), index * 50, dtype=np.uint8))
        writer.emit("frames", "VIDEO_FRAME", {"frame_index": index}, timestamp=stamp)
    video.release()
    writer.close()
    output = tmp_path / "采集 frame.png"
    mapping = export_frame(writer.path, 16_000_000_000, output)
    image = cv2.imdecode(np.fromfile(output, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert mapping["frame_index"] == 2 and mapping["acquisition_timestamp_ns"] == 15_000_000_000
    assert np.all(image == 100)
    # The table can outlive a damaged/truncated mother video. A previous
    # preview must not be mistaken for the new extraction's successful output.
    writer2 = EpisodeWriter(Config(output=str(tmp_path)))
    writer2.emit("frames", "VIDEO_FRAME", {"frame_index": 999}, timestamp=0)
    writer2.close()
    import shutil
    shutil.copyfile(writer.path / "visual.mkv", writer2.path / "visual.mkv")
    previous = output.read_bytes()
    with pytest.raises(ValueError, match="Could not decode requested frame"):
        export_frame(writer2.path, 0, output)
    assert output.read_bytes() == previous
    assert not list(tmp_path.glob("*.extracting-*.png"))
