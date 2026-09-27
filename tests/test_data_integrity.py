import json
from dataclasses import replace
from pathlib import Path
import time
import urllib.request
import pytest
from synaeris_collector.config import Config
from synaeris_collector.storage import EpisodeWriter, iter_rows, recover
from synaeris_collector.bridge import TelemetryBridge
from synaeris_collector.dataset import transitions, decision_windows, build_dataset


def episode(tmp_path, **kwargs):
    return EpisodeWriter(Config(output=str(tmp_path), **kwargs))


def test_stream_identity_and_nanosecond_clock_survive_row_groups(tmp_path):
    writer = episode(tmp_path)
    for i in range(260):
        writer.emit("input", "KEY_DOWN", {"vk": 87}, actor="HUMAN", timestamp=i*1_000_000)
    writer.close()
    rows = list(iter_rows(writer.path, "input"))
    assert len(rows) == 260
    assert {r["episode_id"] for r in rows} == {writer.episode_id}
    assert rows[-1]["monotonic_timestamp"] == 259_000_000
    assert all(r["actor"] == "HUMAN" and "optimal_action" not in r["payload"] for r in rows)


def test_marker_clips_after_window_at_episode_end(tmp_path):
    writer = episode(tmp_path)
    writer.mark("MISTAKE", "wrong approach", timestamp=0)
    writer.close()
    windows = decision_windows(writer.path)
    assert windows[0]["start_ns"] == 0
    assert windows[0]["post_window_truncated"] is True
    assert windows[0]["end_ns"] < 20_000_000_000


def test_returning_tool_does_not_create_false_success_reward(tmp_path):
    writer = episode(tmp_path)
    writer.emit("frames", "VIDEO_FRAME", {"frame_index": 0}, timestamp=0)
    writer.emit("tools", "TOOL_START", {"tool_id": "a", "tool": "Pathing"}, timestamp=1)
    writer.emit("tools", "TOOL_END", {"tool_id": "a", "outcome": "unknown", "verified": False}, timestamp=2)
    writer.emit("frames", "VIDEO_FRAME", {"frame_index": 1}, timestamp=3)
    writer.close()
    result = transitions(writer.path)
    assert result[0]["state_frame"] == 0 and result[0]["next_state_frame"] == 1
    assert result[0]["reward"] is None and result[0]["weak_label"] is True


def test_verified_failure_reward_and_unavailable_next_frame(tmp_path):
    writer = episode(tmp_path)
    writer.emit("tools", "TOOL_START", {"tool_id": "a"}, timestamp=0)
    writer.emit("tools", "TOOL_FAILED", {"tool_id": "a", "outcome": "failure", "verified": True}, timestamp=1)
    writer.close()
    result = transitions(writer.path)
    assert result[0]["reward"] == -1
    assert result[0]["next_state_frame"] is None


def test_recovery_rebuilds_multiple_streams_and_ignores_torn_tail(tmp_path):
    writer = episode(tmp_path)
    writer.emit("input", "KEY_UP", {"vk": 87})
    writer.emit("ocr", "OCR_TEXT", {"text": "调查机关", "confidence": .9})
    writer.close("failed")
    with (writer.path / "journal.jsonl").open("a", encoding="utf-8") as file:
        file.write('{"stream":')
    result = recover(writer.path)
    assert result["status"] == "recovered_partial" and result["corrupt_journal_lines"] == 1
    assert len(list(iter_rows(writer.path, "input"))) == 1
    assert list(iter_rows(writer.path, "ocr"))[0]["payload"]["text"] == "调查机关"
    assert (writer.path / "ocr.pre-recovery.parquet").exists()


def test_bridge_rejects_cross_episode_and_clock_mismatch(tmp_path):
    writer = episode(tmp_path)
    bridge = TelemetryBridge(writer, 0)
    with pytest.raises(ValueError, match="stale episode"):
        bridge.ingest({"episode_id": "other", "stream": "pose"})
    with pytest.raises(ValueError, match="clock"):
        bridge.ingest({"episode_id": writer.episode_id, "stream": "pose", "qpc_ns": 0})
    bridge.server.server_close()
    writer.close()


def test_native_qpc_alignment_and_invalid_pose_semantics(tmp_path):
    writer = episode(tmp_path)
    bridge = TelemetryBridge(writer, 0)
    producer_ns = time.perf_counter_ns()
    bridge.ingest({"episode_id": writer.episode_id, "stream": "pose", "qpc_ns": producer_ns,
                   "payload": {"valid": False}})
    bridge.server.server_close()
    writer.close()
    row = list(iter_rows(writer.path, "pose"))[0]
    assert row["monotonic_timestamp"] == producer_ns-writer.origin_ns
    assert row["payload"]["confidence"] is None and row["payload"]["valid"] is False
    assert row["payload"]["clock_quality"] == "QPC"


def test_http_sink_requires_session_token(tmp_path):
    writer = episode(tmp_path)
    bridge = TelemetryBridge(writer, 0)
    bridge.start()
    url = "http://127.0.0.1:"+str(bridge.server.server_port)
    body = json.dumps({"episode_id": writer.episode_id, "stream": "events", "kind": "TEST"}).encode()
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(urllib.request.Request(url+"/telemetry", data=body))
    assert exc.value.code == 403
    request = urllib.request.Request(url+"/telemetry", data=body, headers={"X-Synaeris-Token": bridge.token})
    assert json.loads(urllib.request.urlopen(request).read())["accepted"]
    bridge.close()
    writer.close()


def test_synthetic_data_cannot_enter_training_export(tmp_path):
    writer = episode(tmp_path, capture="synthetic")
    writer.close()
    result = build_dataset(tmp_path, tmp_path / "dataset.json")
    assert not any(result["splits"].values())
    assert len(result["excluded"]) == 1


def test_split_groups_are_transitive_and_ood_component_is_quarantined(tmp_path, monkeypatch):
    # Real split logic on controlled metadata; video QC is irrelevant to the leakage test.
    from synaeris_collector import dataset
    monkeypatch.setattr(dataset, "inspect_episode", lambda path: {"issues": [], "visual_fingerprint": Path(path).name})
    monkeypatch.setattr(dataset, "transitions", lambda path: [])
    monkeypatch.setattr(dataset, "decision_windows", lambda path: [])
    configs = [dict(region="a", quest="one"), dict(region="a", quest="two"),
               dict(region="b", quest="two", ood=True), dict(region="c", quest="three")]
    for config in configs:
        writer = episode(tmp_path, **config)
        writer.close()
    result = build_dataset(tmp_path, tmp_path / "dataset.json")
    assert result["component_count"] == 2
    assert len(result["splits"]["ood_test"]) == 3
    assert len(result["splits"]["train"]) == 1
    assert result["locked_test_rl_allowed"] is False


def test_invalid_roi_and_negative_timestamps_rejected(tmp_path):
    with pytest.raises(ValueError):
        Config(rois={"quest": [0.8, 0, 0.2, 1]}).validate()
    writer = episode(tmp_path)
    with pytest.raises(ValueError):
        writer.emit("pose", "POSE", timestamp=-1)
    writer.close()
