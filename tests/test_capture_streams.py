from dataclasses import replace
from http.client import HTTPConnection
import json
import socket
import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from synaeris_collector import bridge, collector as runtime, inputs, perf
from synaeris_collector.auto_index import organize
from synaeris_collector.config import CAPTURE_STREAM_FIELDS, Config
from synaeris_collector.dataset import build_dataset, transitions
from synaeris_collector.decision_units import compile_episode, digest, validate_episode, write_episode_sidecar
from synaeris_collector.qualification import qualify
from synaeris_collector.storage import EpisodeWriter, iter_rows


@pytest.fixture(autouse=True)
def fixture_disk_capacity(monkeypatch):
    # Capture lifecycle tests use isolated ephemeral data. Exercise the space
    # check against a deterministic observation instead of the host's free disk.
    monkeypatch.setattr(runtime.shutil, "disk_usage",
                        lambda _: SimpleNamespace(free=50_000_000_000))


def forbidden(*_, **__):
    pytest.fail("A prohibited capture resource or system probe was reached")


def stub_capture(monkeypatch, collector, *, qualified=None):
    """Exercise the window branch with CPU frames and no OS window or capture."""
    window = SimpleNamespace(initial_rect=(0, 0, 2, 2), pid=0,
                             permissions=forbidden if qualified is None else lambda: {
                                 "input_permission_qualified": qualified})
    capture = SimpleNamespace(window=window, backend="fixture", diagnostic="CPU fixture",
                              width=2, height=2, closed=False)

    class Video:
        codec = "fixture"
        qualifications = {}
        frames = 0
        closed = False
        def __init__(self, *args):
            capture.video = self
        def write(self, frame):
            self.frames += 1
        def close(self):
            self.closed = True

    def grab():
        collector.stop()
        return np.zeros((2, 2, 3), dtype=np.uint8), True, True

    capture.grab = grab
    capture.close = lambda: setattr(capture, "closed", True)
    monkeypatch.setattr(runtime, "GameWindow", lambda _: window)
    monkeypatch.setattr(runtime, "ScreenCapture", lambda *_: capture)
    monkeypatch.setattr(runtime, "VideoWriter", Video)
    return capture


def read_manifest(path):
    return json.loads((path / "manifest.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("field", CAPTURE_STREAM_FIELDS.values())
def test_capture_gate_rejects_non_boolean_config(tmp_path, field):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({field: "false"}), encoding="utf-8")
    with pytest.raises(ValueError, match=f"{field} must be a JSON boolean"):
        Config.load(path)


def test_video_only_does_not_create_optional_resources_or_emit_observations(tmp_path, monkeypatch):
    # Keep the proposed port occupied throughout the run. Video must still seal.
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        config = replace(Config(output=str(tmp_path)).video_only(), bridge_port=occupied.getsockname()[1])
        collector = runtime.Collector(config)
        capture = stub_capture(monkeypatch, collector)
        for resource in ("InputRecorder", "TelemetryBridge", "PerfWorker", "OCRWorker"):
            monkeypatch.setattr(runtime, resource, forbidden)
        monkeypatch.setattr(inputs, "RawMouse", forbidden)
        monkeypatch.setattr(bridge, "_TelemetryServer", forbidden)
        monkeypatch.setattr(perf, "gpu_sample", forbidden)
        monkeypatch.setattr(perf.psutil, "cpu_percent", forbidden)
        monkeypatch.setattr(perf.psutil, "virtual_memory", forbidden)
        path = collector.run()
    data = read_manifest(path)
    assert data["status"] == "complete" and capture.closed and capture.video.closed
    assert data["capture_streams"]["version"] == 1 and data["capture_streams"]["mode"] == "video_only"
    assert data["capture_streams"]["sources"]["video"] == {
        "requested": True, "started": True, "status": "enabled", "reason": None}
    for name in CAPTURE_STREAM_FIELDS:
        assert data["capture_streams"]["sources"][name] == {
            "requested": False, "started": False, "status": "disabled", "reason": "operator_config"}
    assert data["capabilities"]["input_permission_qualified"] is None
    for stream in ("input", "pose", "ui", "quest", "objects", "tools", "ocr", "perf"):
        assert not list(iter_rows(path, stream))
    assert "ocr_queue_drops" not in data
    frame, = iter_rows(path, "frames")
    assert frame["payload"]["fresh"] and "capture_latency_ms" not in frame["payload"]


def test_ocr_runs_independently_of_other_sources(tmp_path, monkeypatch):
    c = runtime.Collector(replace(Config(output=str(tmp_path)).video_only(), ocr=True))
    stub_capture(monkeypatch, c)
    for resource in ("InputRecorder", "TelemetryBridge", "PerfWorker"):
        monkeypatch.setattr(runtime, resource, forbidden)
    lifecycle = []

    class OCR:
        def __init__(self, config, writer, failure):
            self.writer = writer
        def start(self):
            lifecycle.append("start")
        def submit(self, frame, stamp, frame_index):
            lifecycle.append("submit")
            self.writer.emit("ocr", "OCR_TEXT", {"text": "fixture", "weak_label": True},
                             source="test_fixture", timestamp=stamp)
        def close(self):
            lifecycle.append("close")
        def stats(self):
            return {}

    monkeypatch.setattr(runtime, "OCRWorker", OCR)
    path = c.run()
    assert lifecycle == ["start", "submit", "close"]
    assert read_manifest(path)["capture_streams"]["sources"]["ocr"]["status"] == "enabled"
    assert len(list(iter_rows(path, "ocr"))) == 1
    assert not list(iter_rows(path, "input")) and not list(iter_rows(path, "perf"))


@pytest.mark.parametrize("stream", ["input", "ocr"])
def test_telemetry_ingress_cannot_reenable_restricted_stream(tmp_path, stream):
    writer = EpisodeWriter(Config(output=str(tmp_path)).video_only())
    sink = bridge.TelemetryBridge(writer, 0, allow_input=False, allow_ocr=False)
    try:
        with pytest.raises(ValueError, match=f"{stream} capture disabled"):
            sink.ingest({"episode_id": writer.episode_id, "stream": stream,
                         "source": "bettergi_native", "kind": "BGI_INPUT_DISPATCH"})
        assert sink.ingest({"episode_id": writer.episode_id, "stream": "ui",
                            "payload": {"mode": "Main"}})
    finally:
        sink.close()
        writer.close()
    assert sink.server.socket.fileno() == -1
    assert not list(iter_rows(writer.path, stream))
    assert len(list(iter_rows(writer.path, "ui"))) == 1


def test_input_without_bridge_marks_tool_state_unknown(tmp_path):
    writer = EpisodeWriter(Config(output=str(tmp_path)))
    recorder = inputs.InputRecorder(SimpleNamespace(active=lambda: True), writer, None, forbidden, forbidden)
    try:
        recorder.key_filter(0x100, SimpleNamespace(flags=0, vkCode=65, scanCode=30))
        recorder.delta(1, 2, False)
    finally:
        recorder.close()  # .start() was not called. No OS hook or Raw Input registration.
        writer.close()
    assert [row["payload"]["bgi_tool_active"] for row in iter_rows(writer.path, "input")] == [None, None]


@pytest.mark.parametrize("purpose", ["gameplay", "calibration"])
def test_requested_unqualified_input_does_not_create_listener(tmp_path, monkeypatch, purpose):
    config = replace(Config(output=str(tmp_path)).video_only(), capture_input=True, purpose=purpose)
    c = runtime.Collector(config)
    stub_capture(monkeypatch, c, qualified=False)
    monkeypatch.setattr(runtime, "InputRecorder", forbidden)
    if purpose == "gameplay":
        with pytest.raises(RuntimeError, match="Input permission preflight failed"):
            c.run()
    else:
        c.run()
    data = read_manifest(c.writer.path)
    assert data["status"] == ("failed" if purpose == "gameplay" else "complete")
    assert data["capture_streams"]["sources"]["input"] == {
        "requested": True, "started": False, "status": "unavailable",
        "reason": "input_permission_unqualified"}
    assert not list(iter_rows(c.writer.path, "input"))


@pytest.mark.parametrize("fail_at", ["telemetry", "input"])
def test_partial_startup_releases_bridge_and_other_started_resources(tmp_path, monkeypatch, fail_at):
    config = replace(Config(output=str(tmp_path)).video_only(), capture_telemetry=True, capture_input=True)
    c = runtime.Collector(config)
    capture = stub_capture(monkeypatch, c, qualified=True)
    bridges, closed_inputs = [], []

    class Bridge(bridge.TelemetryBridge):
        def __init__(self, writer, port, on_state, **options):
            super().__init__(writer, 0, on_state, **options)
            bridges.append(self)
        def start(self):
            if fail_at == "telemetry":
                raise RuntimeError("fixture failure")
            super().start()

    class Input:
        def __init__(self, *a):
            pass
        def start(self):
            raise RuntimeError("fixture failure")
        def close(self):
            closed_inputs.append(True)

    monkeypatch.setattr(runtime, "TelemetryBridge", Bridge)
    monkeypatch.setattr(runtime, "InputRecorder", Input)
    with pytest.raises(RuntimeError, match="fixture failure"):
        c.run()
    b, = bridges
    assert not b.thread.is_alive() and b.server.socket.fileno() == -1
    with socket.socket() as probe:
        probe.settimeout(0.5)
        assert probe.connect_ex(b.server.server_address) != 0
    assert capture.closed and capture.video.closed
    assert closed_inputs == ([True] if fail_at == "input" else [])
    data = read_manifest(c.writer.path)
    assert data["status"] == "failed"
    assert data["capture_streams"]["sources"][fail_at]["reason"] == "startup_failed"
    assert data["capture_streams"]["sources"][fail_at]["started"] is False


def test_input_close_continues_to_raw_input_after_hook_failure(monkeypatch):
    closed = []
    def raise_stop():
        raise RuntimeError("fixture hook stop")
    listener = SimpleNamespace(ident=1, stop=raise_stop)
    other = SimpleNamespace(ident=2, stop=lambda: closed.append("hook"), join=lambda **_: None,
                            is_alive=lambda: False)
    monkeypatch.setattr(inputs, "RawMouse", lambda *_: SimpleNamespace(close=lambda: closed.append("raw")))
    device = inputs.InputRecorder(None, None, None, forbidden, forbidden)
    device.keyboard, device.mouse = listener, other
    with pytest.raises(RuntimeError, match="fixture hook stop"):
        device.close()
    assert closed == ["hook", "raw"]


def test_failed_ocr_worker_is_unavailable_in_sealed_manifest(tmp_path, monkeypatch):
    c = runtime.Collector(replace(Config(output=str(tmp_path)).video_only(), ocr=True))
    stub_capture(monkeypatch, c)
    closed = []

    class FailedOCR:
        def __init__(self, config, writer, fail):
            self.fail = fail
        def start(self):
            self.fail("OCR", RuntimeError("fixture initialization failed"))
        def close(self):
            closed.append(True)
        def stats(self):
            return {}

    monkeypatch.setattr(runtime, "OCRWorker", FailedOCR)
    with pytest.raises(RuntimeError, match="fixture initialization failed"):
        c.run()
    assert closed == [True]
    data = read_manifest(c.writer.path)
    assert data["status"] == "failed"
    assert data["capture_streams"]["sources"]["ocr"] == {
        "requested": True, "started": True, "status": "unavailable", "reason": "runtime_failed"}


def fixture_episode(tmp_path, *, new_policy):
    config = Config(output=str(tmp_path), capture="synthetic").video_only()
    writer = EpisodeWriter(config)
    if new_policy:
        writer.manifest["capture_streams"] = config.capture_streams()
    for i in range(5):
        writer.emit("frames", "VIDEO_FRAME", {"frame_index": i, "video_pts_ns": i*10,
             "capture_available": True, "game_focused": True, "fresh": True}, timestamp=i*1_000_000_000)
    writer.now = lambda: 5_000_000_000
    (writer.path / "visual.mkv").write_bytes(b"fixture, no game video")
    return writer


def test_disabled_input_has_no_quiet_inference_or_observed_action(tmp_path, monkeypatch):
    writer = fixture_episode(tmp_path, new_policy=True)
    writer.close()
    index = organize(writer.path)
    assert index["input_source_status"] == "disabled"
    assert "input_quiet_candidate" not in index["label_counts"]
    assert all(segment["input_event_count"] is None for segment in index["segments"])
    with pytest.raises(ValueError, match="sealed real HUMAN"):
        write_episode_sidecar(writer.path)
    assert not (writer.path / "decision_units.v1.jsonl").exists()

    # A counterfactual HUMAN-shaped manifest is visible only to the test
    # compiler in memory. Existing synthetic files and hashes remain intact.
    manifest_path = writer.path / "manifest.json"
    read_text = type(manifest_path).read_text

    def read_test_view(path, *args, **kwargs):
        value = read_text(path, *args, **kwargs)
        if path == manifest_path:
            manifest = json.loads(value)
            manifest["synthetic"] = False
            manifest["config"]["actor"] = "HUMAN"
            return json.dumps(manifest)
        return value

    mother_hash = digest(manifest_path)
    with monkeypatch.context() as scope:
        scope.setattr(type(manifest_path), "read_text", read_test_view)
        units = compile_episode(writer.path)
        assert validate_episode(units, writer.now())
        assert units[0]["evidence"]["source_sha256"]["manifest"] == mother_hash
        with pytest.raises(ValueError, match="Sealed source differs"):
            compile_episode(writer.path, source_reference={"video": "0"*64})
    assert digest(manifest_path) == mother_hash and read_manifest(writer.path)["synthetic"]
    for unit in units:
        assert not unit["formal_training_eligible"] and not unit["semantic_supervision_admitted"]
        action = unit["action"]
        assert action["physical_status"] == "unavailable"
        assert action["input_source_status"] == action["telemetry_source_status"] == "disabled"
        assert action["held_keys_at_start"] is None and action["held_buttons_at_start"] is None
        assert action["raw_mouse_candidate"]["event_count"] is None
        assert action["game_receipt"]["status"] == unit["outcome"]["status"] == "unknown"


def test_legacy_episode_index_keeps_existing_diagnostic_behavior(tmp_path):
    writer = fixture_episode(tmp_path, new_policy=False)
    writer.close()
    index = organize(writer.path)
    assert index["input_source_status"] == "legacy_unspecified"
    assert index["label_counts"]["input_quiet_candidate"] == 1
    assert index["segments"][0]["input_event_count"] == 0


def test_legacy_tool_rewards_do_not_qualify_synthetic_data(tmp_path):
    writer = fixture_episode(tmp_path, new_policy=True)
    for tool_id, completion, data in [
        ("exception", "TOOL_FAILED", {}),
        ("declared", "TOOL_END", {"verified": True, "outcome": "success"}),
    ]:
        writer.emit("tools", "TOOL_START", {"tool_id": tool_id}, timestamp=10)
        writer.emit("tools", completion, {"tool_id": tool_id, **data}, timestamp=20)
    writer.close()
    old = transitions(writer.path)
    assert [item["reward"] for item in old] == [-1, 2]
    assert all(not item["terminal"] for item in old)
    qualification = qualify(writer.path, inspect=lambda _: {"issues": [], "decoded_frames": 5,
                                                             "video_decode_backend": "fixture"})
    assert qualification["verified_transition_count"] == 1
    assert not qualification["training_eligible"]
    assert not any(build_dataset(tmp_path, tmp_path / "dataset.json")["splits"].values())


@pytest.mark.parametrize("args", [
    ["--video-only"], ["--no-input", "--no-telemetry", "--no-performance", "--no-ocr"],
])
def test_cli_declines_all_optional_resources_before_recording(tmp_path, monkeypatch, args):
    from synaeris_collector.cli import main

    class FakeCollector:
        def __init__(self, config, notify):
            assert config.capture == "synthetic"
            assert config.capture_streams()["mode"] == "video_only"
            self.config = config
        def run(self, *a):
            return tmp_path
        def stop(self):
            pytest.fail("No interrupt requested")

    monkeypatch.setattr(runtime, "Collector", FakeCollector)
    monkeypatch.setattr("signal.signal", lambda *_: None)
    monkeypatch.setattr(sys, "argv", ["collector", "record", "--synthetic", *args])
    main()

@pytest.mark.parametrize("disabled", CAPTURE_STREAM_FIELDS)
def test_each_source_can_be_disabled_with_other_sources_running(tmp_path, monkeypatch, disabled):
    field = CAPTURE_STREAM_FIELDS[disabled]
    config = replace(Config(output=str(tmp_path)), **{field: False})
    c = runtime.Collector(config)
    stub_capture(monkeypatch, c, qualified=True)
    started = set()
    closed = set()

    class Worker:
        latest_pose_ns = latest_ui_ns = None

        def __init__(self, name):
            self.name = name

        def start(self):
            started.add(self.name)

        def close(self):
            closed.add(self.name)

        def submit(self, *_):
            pass

        def stats(self):
            return {}

    classes = {"input": "InputRecorder", "telemetry": "TelemetryBridge",
               "performance": "PerfWorker", "ocr": "OCRWorker"}
    for name, target in classes.items():
        factory = forbidden if name == disabled else (
            lambda *args, name=name, **kwargs: Worker(name))
        monkeypatch.setattr(runtime, target, factory)
    sources = read_manifest(c.run())["capture_streams"]["sources"]
    assert started == closed == set(classes) - {disabled}
    assert {name for name in classes if sources[name]["started"]} == started
    assert sources[disabled]["status"] == "disabled"


def test_gui_video_only_survives_reset_and_does_not_check_telemetry_port(tmp_path, monkeypatch):
    from synaeris_collector import gui

    class Var:
        def __init__(self, value=None):
            self.value = value
        def get(self):
            return self.value
        def set(self, value):
            self.value = value

    app = object.__new__(gui.App)
    app.config = Config(output=str(tmp_path))
    app.stream_fields = {field: Var(True) for field in CAPTURE_STREAM_FIELDS.values()}
    app.stream_summary = Var()
    app.busy = app.recording = False
    app.standard = Var(True)
    app.fields = {key: Var(getattr(app.config, key)) for key in (
        "output", "actor", "region", "quest", "puzzle", "poi", "capture", "codec")}
    app.numeric_fields = {"fps": Var("60")}
    app.ood = app.novel = app.background = Var(False)
    app.scenario = Var("fixture")
    app.sync_settings = app.update_stream_summary
    app.status = Var()
    saved = []
    app.start_button = app.stop_button = SimpleNamespace(configure=lambda **_: None)
    app.root = SimpleNamespace(iconify=forbidden)
    app.write_log = lambda _: None
    monkeypatch.setattr(gui, "Collector", lambda config, notify: SimpleNamespace(config=config))
    monkeypatch.setattr(gui, "ensure_port_available", forbidden)
    monkeypatch.setattr(gui, "save_preferences", saved.append)
    # Validate the start handoff only. The worker target does not run.
    monkeypatch.setattr(gui.threading, "Thread", lambda **_: SimpleNamespace(start=lambda: None))

    app.select_video_only()
    app.restore_defaults()
    app.start()
    assert app.busy and app.stream_summary.get().startswith("Video-only")
    assert len(saved) == 1 and saved[0] == app.config == app.collector.config
    assert not any(getattr(saved[0], field) for field in CAPTURE_STREAM_FIELDS.values())

@pytest.mark.parametrize("explicit", [False, True])
def test_gui_cli_respects_preferences_and_explicit_import(tmp_path, monkeypatch, explicit):
    from synaeris_collector import gui, human
    from synaeris_collector.cli import main

    received = []
    config = Config(output=str(tmp_path)).video_only()
    path = tmp_path / "config.json"
    config.save(path)
    monkeypatch.setattr(human, "load_preferences", forbidden if explicit else lambda: (config, "fixture"))
    monkeypatch.setattr(gui, "App", lambda cfg, startup_note: SimpleNamespace(
        run=lambda: received.append((cfg, startup_note))))
    monkeypatch.setattr(sys, "argv", ["collector", "gui", *(["--config", str(path)] if explicit else [])])
    main()
    assert received == [(config, "" if explicit else "fixture")]

@pytest.mark.parametrize("failed_attempts, expected_state, expected_status", [
    ((False,), "sampled", "enabled"),
    ((False, True, False), "partial", "enabled"),
    ((False, True), "partial", "enabled"),
    ((True, True), "unavailable", "unavailable"),
    ((False, True, True), "unavailable", "unavailable"),
])
def test_performance_sampling_faults_are_preserved_in_sealed_episode(
        tmp_path, monkeypatch, failed_attempts, expected_state, expected_status):
    config = replace(Config(output=str(tmp_path)).video_only(), capture_performance=True, perf_hz=10)
    c = runtime.Collector(config)
    capture = stub_capture(monkeypatch, c)
    original_grab = capture.grab
    finished = threading.Event()
    attempts = iter(failed_attempts)
    rows = []
    emitted_frame = threading.Event()

    def sample():
        if next(attempts):
            raise TimeoutError("fixture probe timeout")
        return {"available": False}

    def grab():
        assert finished.wait(3), "Performance worker did not finish the fixture attempts"
        frame = original_grab()
        emitted_frame.set()
        return frame

    original_emit = EpisodeWriter.emit

    def emit(writer, stream, kind, payload=None, **fields):
        recorded = original_emit(writer, stream, kind, payload, **fields)
        if kind == "PERF_SAMPLE" or (
                kind == "COLLECTOR_ERROR" and payload["message"].startswith("PERFORMANCE_SAMPLE:")):
            rows.append(kind)
            if len(rows) == len(failed_attempts):
                finished.set()
                emitted_frame.wait(3)
        return recorded

    capture.grab = grab
    monkeypatch.setattr(perf, "gpu_sample", sample)
    monkeypatch.setattr(EpisodeWriter, "emit", emit)
    path = c.run()
    manifest = read_manifest(path)
    source = manifest["capture_streams"]["sources"]["performance"]
    assert source["requested"] and source["started"]
    assert source["status"] == expected_status
    assert source["reason"] == ("sampling_unavailable" if expected_status == "unavailable" else None)
    assert manifest["status"] == "complete" and capture.video.closed
    assert manifest["counts"]["frames"] == 1
    assert manifest["capture_streams"]["sources"]["video"]["status"] == "enabled"
    assert all(manifest["capture_streams"]["sources"][name]["status"] == "disabled"
               for name in ("input", "telemetry", "ocr"))

    errors = [row for row in iter_rows(path, "events") if row["kind"] == "COLLECTOR_ERROR"]
    samples = list(iter_rows(path, "perf"))
    assert len(errors) == sum(failed_attempts)
    assert len(samples) == len(failed_attempts) - len(errors)
    assert all(row["payload"] == {"message": "PERFORMANCE_SAMPLE: fixture probe timeout",
                                  "fatal": False, "capture_source": "performance",
                                  "operation": "PERFORMANCE_SAMPLE"} for row in errors)
    chronology = sorted((*errors, *samples), key=lambda row: row["monotonic_timestamp"])
    assert [row["kind"] == "COLLECTOR_ERROR" for row in chronology] == list(failed_attempts)
    trailing_failures = len(failed_attempts) - max(
        (i + 1 for i, failed in enumerate(failed_attempts) if not failed), default=0)
    assert source["sample_coverage"] == {
        "state": expected_state,
        "successful_samples": len(samples),
        "failed_samples": len(errors),
        "consecutive_failures": trailing_failures,
        "open_gap_start_ns": errors[-trailing_failures]["monotonic_timestamp"] if trailing_failures else None,
    }


@pytest.mark.parametrize("fault, reason, operation, started", [
    ("startup", "startup_failed", "COLLECTOR", False),
    ("worker", "runtime_failed", "PERFORMANCE_WORKER", True),
    ("shutdown", "shutdown_failed", "PERFORMANCE_CLOSE", True),
])
def test_performance_lifecycle_errors_seal_and_release_other_resources(
        tmp_path, monkeypatch, fault, reason, operation, started):
    c = runtime.Collector(replace(Config(output=str(tmp_path)).video_only(), capture_performance=True))
    capture = stub_capture(monkeypatch, c)
    workers = []

    def broken():
        raise RuntimeError("fixture lifecycle failure")

    class Worker(perf.PerfWorker):
        def __init__(self, *args):
            super().__init__(*args)
            workers.append(self)

        def start(self):
            if fault == "startup":
                broken()
            super().start()

        def close(self):
            summary = super().close()
            if fault == "shutdown":
                broken()
            return summary

    monkeypatch.setattr(runtime, "PerfWorker", Worker)
    monkeypatch.setattr(perf, "gpu_sample", lambda: {"available": False})
    if fault == "worker":
        monkeypatch.setattr(perf.psutil, "Process", broken)
    with pytest.raises(RuntimeError, match="fixture lifecycle failure"):
        c.run()
    path = c.writer.path
    manifest = read_manifest(path)
    performance = manifest["capture_streams"]["sources"]["performance"]
    assert manifest["status"] == "failed"
    assert performance["started"] is started and performance["requested"]
    assert (performance["status"], performance["reason"]) == ("unavailable", reason)
    if fault == "worker":
        assert performance["sample_coverage"]["state"] == "unavailable"
        assert performance["sample_coverage"]["successful_samples"] == 0
    assert len(workers) == 1 and not workers[0].thread.is_alive()
    assert capture.closed and capture.video.closed
    assert manifest["capture_streams"]["sources"]["video"]["status"] == "enabled"
    errors = [row["payload"] for row in iter_rows(path, "events") if row["kind"] == "COLLECTOR_ERROR"]
    assert errors and all(item["fatal"] for item in errors)
    if fault != "startup":
        assert errors[0]["capture_source"] == "performance"
        assert errors[0]["operation"] == operation


@pytest.mark.parametrize("kind, required_stream", [
    ("BGI_INPUT_DISPATCH", "input"),
    ("OCR_TEXT", "ocr"),
    ("OCR_SCAN", "ocr"),
    ("OCR_CACHE", "ocr"),
    ("ITEM_RECEIPT_CANDIDATE", "ocr"),
])
def test_telemetry_blocks_disabled_source_records_relabelled_as_generic_events(tmp_path, kind, required_stream):
    writer = EpisodeWriter(Config(output=str(tmp_path)).video_only())
    sink = bridge.TelemetryBridge(writer, 0, allow_input=False, allow_ocr=False)
    sink.start()
    connection = HTTPConnection(*sink.server.server_address, timeout=2)
    try:
        request = {"episode_id": writer.episode_id, "stream": "events", "kind": kind,
                   "source": "bettergi_native", "payload": {"fixture": True}}
        connection.request("POST", "/telemetry", json.dumps(request), {
            "X-Synaeris-Token": sink.token, "Content-Type": "application/json", "Connection": "close"})
        result = connection.getresponse()
        body = json.loads(result.read())
        assert result.status == 400
        assert required_stream + " capture disabled" in body["error"]
    finally:
        connection.close()
        sink.close()
        writer.close()
    assert not sink.thread.is_alive() and sink.server.socket.fileno() == -1
    assert all(row["kind"] != kind for row in iter_rows(writer.path, "events"))


def test_telemetry_close_releases_a_test_owned_keepalive_connection(tmp_path, monkeypatch):
    writer = EpisodeWriter(Config(output=str(tmp_path)).video_only())
    sink = bridge.TelemetryBridge(writer, 0)
    handlers = []
    original_handle = sink.server.RequestHandlerClass.handle

    def handle(handler):
        handlers.append(threading.current_thread())
        original_handle(handler)

    monkeypatch.setattr(sink.server.RequestHandlerClass, "handle", handle)
    sink.start()
    connection = HTTPConnection(*sink.server.server_address, timeout=2)
    try:
        connection.request("GET", "/session")
        result = connection.getresponse()
        assert result.status == 200 and not result.will_close
        result.read()
        client = connection.sock
        assert client is not None
        sink.close()
        assert not sink.thread.is_alive() and sink.server.socket.fileno() == -1
        client.settimeout(0.15)
        assert client.recv(1) == b""
        assert len(handlers) == 1 and not handlers[0].is_alive()
    finally:
        connection.close()
        if not sink.closing.is_set():
            sink.close()
        writer.close()
