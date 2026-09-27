import json
from pathlib import Path
import importlib.util
import sys
from types import SimpleNamespace
import numpy as np
import pytest
from synaeris_collector.capture import ScreenCapture
from synaeris_collector.config import Config
from synaeris_collector.bridge import TelemetryBridge
from synaeris_collector.storage import EpisodeWriter, iter_rows


def test_unfocused_geometry_is_not_treated_as_a_real_resize():
    class Window:
        def active(self): return False
        def rect(self): raise AssertionError("Unfocused geometry must not be queried")
    capture = object.__new__(ScreenCapture)
    capture.window = Window()
    capture.last = np.ones((2, 2, 3), dtype=np.uint8)
    frame, active, fresh = capture.grab()
    assert not active and not fresh and not frame.any()


def test_focus_loss_during_desktop_grab_masks_other_application_pixels():
    class Window:
        calls = 0
        def active(self):
            self.calls += 1
            return self.calls == 1
        def rect(self): return (0, 0, 2, 2)
    class Camera:
        def grab(self, **kwargs): return np.full((2, 2, 3), 123, dtype=np.uint8)
    capture = object.__new__(ScreenCapture)
    capture.window, capture.rect, capture.local_rect = Window(), (0, 0, 2, 2), (0, 0, 2, 2)
    capture.width = capture.height = 2
    capture.last = np.zeros((2, 2, 3), dtype=np.uint8)
    capture.camera, capture.allow_fallback = Camera(), False
    frame, active, fresh = capture.grab()
    assert not active and not fresh and not frame.any()
    assert not capture.last.any()


def test_codex_annotations_are_not_attributed_to_bgi(tmp_path):
    writer = EpisodeWriter(Config(output=str(tmp_path)))
    bridge = TelemetryBridge(writer, 0)
    bridge.ingest({"episode_id": writer.episode_id, "stream": "quest",
        "source": "codex_ui", "kind": "QUEST_OBSERVATION",
        "payload": {"text": "调查机关"}})
    bridge.server.server_close()
    writer.close()
    row = next(iter_rows(writer.path, "quest"))
    assert row["actor"] == "AGENT" and row["payload"]["weak_label"]
    assert row["payload"]["annotation_provenance"] == "agent_declared"


def test_runtime_dxcam_failure_falls_back_without_fabricating_a_frame(monkeypatch):
    class Window:
        def active(self): return True
        def rect(self): return (0, 0, 2, 2)
    class Camera:
        def grab(self, **kwargs): raise RuntimeError("DXGI invalid call")
        def release(self): pass
    class Fallback:
        def grab(self, box): return np.full((2, 2, 4), 123, dtype=np.uint8)
    monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(mss=Fallback))
    capture = object.__new__(ScreenCapture)
    capture.window, capture.rect, capture.local_rect = Window(), (0, 0, 2, 2), (0, 0, 2, 2)
    capture.width = capture.height = 2
    capture.last = np.zeros((2, 2, 3), dtype=np.uint8)
    capture.camera, capture.allow_fallback = Camera(), True
    frame, active, fresh = capture.grab()
    assert active and fresh and np.all(frame == 123)
    assert capture.backend == "mss" and "DXGI invalid call" in capture.diagnostic


def test_calibration_video_is_excluded_from_training(tmp_path):
    from synaeris_collector.dataset import build_dataset
    writer = EpisodeWriter(Config(output=str(tmp_path), purpose="calibration"))
    writer.close()
    result = build_dataset(tmp_path, tmp_path / "dataset.json")
    assert not any(result["splits"].values())
    assert result["excluded"][0]["reason"] == "calibration_only"


def test_input_permission_gap_is_excluded_even_if_video_completed(tmp_path):
    from synaeris_collector.dataset import build_dataset
    writer = EpisodeWriter(Config(output=str(tmp_path)))
    writer.manifest["capabilities"] = {"input_permission_qualified": False}
    writer.close()
    result = build_dataset(tmp_path, tmp_path / "dataset.json")
    assert result["excluded"][0]["reason"] == "input_permission_unqualified"
    assert not any(result["splits"].values())


def test_native_group_copies_routes_and_preserves_existing_group(tmp_path):
    path = Path(__file__).parents[1] / "tools" / "prepare_live_groups.py"
    spec = importlib.util.spec_from_file_location("prepare_live_groups", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "bgi"
    (root / "User" / "ScriptGroup").mkdir(parents=True)
    route = tmp_path / "route.json"
    route.write_text(json.dumps({"positions": [{"x": 1, "y": 2}]}))
    target = module.prepare([route], root, "Synaeris_probe", tmp_path / "manifest.json")
    group = json.loads(target.read_text(encoding="utf-8"))
    assert group["projects"][0]["type"] == "Pathing"
    copied = root / "User" / "AutoPathing" / "StellariaSynaeris" / "Synaeris_probe" / "01-route.json"
    assert copied.read_bytes() == route.read_bytes()
    with pytest.raises(ValueError, match="preserved"):
        module.prepare([route], root, "Synaeris_probe", tmp_path / "manifest.json")


def test_raw_mouse_does_not_create_a_false_human_action_label():
    from synaeris_collector.inputs import InputRecorder
    records = []
    recorder = object.__new__(InputRecorder)
    recorder.window = SimpleNamespace(active=lambda: True)
    recorder.bridge = SimpleNamespace(active_tools={"route"})
    recorder.writer = SimpleNamespace(emit=lambda *args, **kwargs: records.append((args, kwargs)))
    recorder.delta(27, -9, False, 0, 42)
    args, metadata = records[0]
    assert metadata["actor"] == "UNKNOWN"
    assert args[2]["injection_status"] == "unavailable"
    assert args[2]["device_handle"] == 0 and args[2]["bgi_tool_active"]


def test_native_dispatch_observation_is_distinct_from_unknown_injected_hooks(tmp_path):
    writer = EpisodeWriter(Config(output=str(tmp_path)))
    bridge = TelemetryBridge(writer, 0)
    event = {"episode_id": writer.episode_id, "stream": "input", "source": "bettergi_js",
             "kind": "BGI_INPUT_DISPATCH", "qpc_ns": writer.origin_ns + writer.now(),
             "payload": {"dx": 20, "game_effect_verified": False}}
    with pytest.raises(ValueError, match="native dispatch"):
        bridge.ingest(event)
    bridge.ingest({**event, "source": "bettergi_native"})
    bridge.server.server_close()
    writer.close()
    row = next(iter_rows(writer.path, "input"))
    assert row["actor"] == "BGI" and row["payload"]["clock_quality"] == "QPC"
    assert row["payload"]["game_effect_verified"] is False


def test_legacy_raw_mouse_human_labels_are_quarantined(tmp_path):
    from synaeris_collector.dataset import inspect_episode
    writer = EpisodeWriter(Config(output=str(tmp_path)))
    writer.emit("input", "MOUSE_DELTA", {"dx": 2, "dy": 0}, source="win32_raw_input", actor="HUMAN")
    writer.close()
    report = inspect_episode(writer.path)
    assert "input: raw mouse producer identity was not qualified" in report["issues"]


def test_route_health_does_not_count_duplicate_returns_as_another_route(tmp_path):
    import urllib.request
    writer = EpisodeWriter(Config(output=str(tmp_path)))
    bridge = TelemetryBridge(writer, 0)
    bridge.start()
    event = {"episode_id": writer.episode_id, "stream": "tools", "source": "bettergi_native",
             "payload": {"tool_id": "route-1", "tool": "Pathing"},
             "telemetry_diagnostics": {"http_rejections": 2}}
    for kind in ("TOOL_START", "TOOL_END", "TOOL_END"):
        bridge.ingest({**event, "kind": kind})
    health = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{bridge.server.server_port}/health").read())
    bridge.close()
    writer.close()
    assert health["completed_pathing_routes"] == 1 and health["active_tool_count"] == 0
    assert next(iter_rows(writer.path, "tools"))["payload"]["producer_diagnostics"]["http_rejections"] == 2


def test_cuda_decode_failure_keeps_diagnostics_and_fully_decodes_on_cpu(monkeypatch):
    from synaeris_collector.dataset import decoded_frame_count
    import subprocess
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        if "-hwaccel" in command:
            return subprocess.CompletedProcess(command, 1, "", "CUDA decoder unavailable")
        return subprocess.CompletedProcess(command, 0, "frame=12\nprogress=end\n", "")
    monkeypatch.setattr(subprocess, "run", run)
    count, backend, diagnostics = decoded_frame_count("video.mkv", 9000)
    assert count == 12 and backend == "ffmpeg_cpu"
    assert diagnostics[0]["backend"] == "ffmpeg_cuda" and len(commands) == 2
    assert all("-xerror" in command for command in commands)


def test_failed_strict_decode_cannot_be_qualified_by_a_matching_fallback_count(tmp_path, monkeypatch):
    from synaeris_collector import dataset
    writer = EpisodeWriter(Config(output=str(tmp_path)))
    writer.emit("frames", "VIDEO_FRAME", {"frame_index": 0})
    writer.close()
    (writer.path / "visual.mkv").write_bytes(b"corrupt")
    monkeypatch.setattr(dataset, "decoded_frame_count", lambda *args:
        (1, "opencv_cpu", [{"backend": "ffmpeg_cpu", "error": "corrupt frame"}]))
    result = dataset.build_dataset(tmp_path, tmp_path / "dataset.json")
    assert result["excluded"][0]["reason"] == "quality_errors"
    assert not any(result["splits"].values())
