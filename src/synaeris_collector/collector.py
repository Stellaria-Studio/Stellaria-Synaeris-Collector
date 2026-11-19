from dataclasses import replace
import hashlib
from pathlib import Path
import shutil
import threading
import time
from .capture import GameWindow, ScreenCapture, SyntheticCapture, VideoWriter
from .storage import EpisodeWriter, atomic_json
from .bridge import TelemetryBridge
from .inputs import InputRecorder
from .ocr import OCRWorker
from .perf import PerfWorker


class Collector:
    def __init__(self, config, notify=None):
        self.config = replace(config).validate()
        self.notify = notify or (lambda value: None)
        self.stop_event = threading.Event()
        self.writer = None
        self.ocr = None
        self.inputs = None
        self.bridge = None
        self.error = None
        self.failed_sources = set()
        self.capture_ms = None
        self.encode_ms = None
        self.frames = 0
        self.start_ns = 0
        self.active = False
        self.latest_preview = None
        self.lock = threading.Lock()

    def stop(self):
        self.stop_event.set()

    def fail(self, source, exc, fatal=True):
        message = f"{source}: {exc}"
        if self.writer:
            self.writer.emit("events", "COLLECTOR_ERROR", {"message": message, "fatal": fatal})
        self.notify(message)
        if fatal:
            with self.lock:
                if self.error is None:
                    self.error = message
            self.stop()

    def metrics(self):
        ocr_stats = self.ocr.stats() if self.ocr else {}
        return dict(capture_latency_ms=self.capture_ms, encode_submit_latency_ms=self.encode_ms,
                    capture_fps=self.frames / max(0.001, (time.perf_counter_ns()-self.start_ns)/1e9),
                    ocr_latency_ms=self.ocr.latency_ms if self.ocr else None,
                    **ocr_stats, game_focused=self.active)

    def storage_check(self):
        root = Path(self.config.output).resolve()
        root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(root).free / 1e9
        if free < self.config.reserve_free_gb:
            raise RuntimeError("Storage reserve reached; collection stopped without deleting data")
        used = sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) / 1e9
        if used >= self.config.storage_budget_gb:
            raise RuntimeError("Dataset storage budget reached")
        return {"dataset_gb": used, "free_gb": free}

    def source_failure(self, name, source, exc, fatal=True):
        if fatal:
            with self.lock:
                self.failed_sources.add(name)
        self.fail(source, exc, fatal)

    def stream_status(self, name, status, reason=None):
        source = self.writer.manifest["capture_streams"]["sources"][name]
        source.update(status=status, reason=reason)
        if status == "enabled":
            source["started"] = True

    def start_stream(self, workers, name, factory):
        try:
            worker = workers[name] = factory()
            worker.start()
        except Exception:
            self.stream_status(name, "unavailable", "startup_failed")
            raise
        self.stream_status(name, "enabled")
        return worker

    def run(self, duration=None, stop_file=None):
        capture = video = None
        workers = {}
        self.start_ns = time.perf_counter_ns()
        try:
            self.storage_check()
            if self.config.capture == "synthetic":
                self.config = replace(self.config, actor="SYNTHETIC")
                capture = SyntheticCapture()
            elif self.config.capture == "wgc":
                from .window_capture import WindowCapture
                capture = WindowCapture(GameWindow(self.config.window_title), self.config.capture_background)
            else:
                capture = ScreenCapture(GameWindow(self.config.window_title), self.config.capture)
            self.writer = EpisodeWriter(self.config, {"capture_backend": capture.backend,
                "capture_diagnostic": capture.diagnostic,
                "desktop_capture_privacy_guard": getattr(capture, "privacy_guard", None),
                "client_rect": capture.window.initial_rect if capture.window else None,
                "game_pid": capture.window.pid if capture.window else None})
            self.writer.manifest["capture_streams"] = self.config.capture_streams()
            manifest_path = self.writer.path / "manifest.json"
            atomic_json(manifest_path, self.writer.manifest)
            input_ready = False
            if self.config.capture_input and capture.window:
                permissions = capture.window.permissions()
                self.writer.manifest["capabilities"] = permissions
                input_ready = permissions["input_permission_qualified"]
                if not input_ready:
                    self.writer.manifest["unavailable"].append("input_permission_unqualified")
                    self.stream_status("input", "unavailable", "input_permission_unqualified")
                    if self.config.purpose == "gameplay":
                        raise RuntimeError("Input permission preflight failed: launch the collector with the same integrity level as the game; local Windows authorization is required")
            else:
                self.writer.manifest["capabilities"] = {"input_permission_qualified": None,
                    "qualification_scope": "Input integrity not evaluated",
                    "input_unavailable_reason": "no_game_window" if self.config.capture_input else "operator_config"}
                if self.config.capture_input:
                    self.stream_status("input", "unavailable", "no_game_window")
            try:
                video = VideoWriter(self.writer.path / "visual.mkv", self.config, capture.width, capture.height)
            except Exception:
                self.stream_status("video", "unavailable", "startup_failed")
                raise
            self.writer.manifest["video"] = {"codec": video.codec, "fps": self.config.fps,
                "width": capture.width, "height": capture.height, "time_mapping": "frames.parquet",
                "encoder_qualification": video.qualifications, "audio": False}
            self.stream_status("video", "enabled")
            if self.config.capture_telemetry:
                self.bridge = self.start_stream(workers, "telemetry", lambda: TelemetryBridge(
                    self.writer, self.config.bridge_port, self.state,
                    allow_input=input_ready, allow_ocr=self.config.ocr))
            if input_ready:
                self.inputs = self.start_stream(workers, "input", lambda: InputRecorder(
                    capture.window, self.writer, self.bridge, self.stop,
                    lambda *args, **kwargs: self.source_failure("input", *args, **kwargs)))
            if self.config.ocr:
                self.ocr = self.start_stream(workers, "ocr", lambda: OCRWorker(
                    self.config, self.writer,
                    lambda *args, **kwargs: self.source_failure("ocr", *args, **kwargs)))
            if self.config.capture_performance:
                self.start_stream(workers, "performance",
                    lambda: PerfWorker(self.writer, self.config, self.metrics, self.fail))
            atomic_json(manifest_path, self.writer.manifest)
            self.writer.emit("events", "EPISODE_START", {"actor": self.config.actor, "novel": self.config.novel})
            self.notify(f"正在采集：{self.writer.path} · {video.codec} · {capture.backend}")
            next_frame = time.perf_counter()
            next_obs = next_ocr = next_storage = next_frame
            previous_active = None
            previous_backend = capture.backend
            while not self.stop_event.is_set():
                if stop_file and Path(stop_file).exists():
                    self.writer.emit("events", "STOP_REQUEST", {"source": "local_stop_file"})
                    break
                now = time.perf_counter()
                if duration is not None and self.writer.now() >= duration * 1e9:
                    break
                if now < next_frame:
                    self.stop_event.wait(next_frame-now)
                    continue
                next_frame += 1 / self.config.fps
                started = time.perf_counter_ns()
                frame, active, fresh = capture.grab()
                available = getattr(capture, "available", active)
                if capture.backend != previous_backend:
                    self.writer.emit("events", "CAPTURE_BACKEND_CHANGED", {
                        "previous": previous_backend, "backend": capture.backend,
                        "diagnostic": capture.diagnostic})
                    previous_backend = capture.backend
                stamp = self.writer.now()
                self.capture_ms = (time.perf_counter_ns()-started) / 1e6
                self.active = active
                if active != previous_active:
                    self.writer.emit("events", "FOCUS_GAINED" if active else "FOCUS_LOST", timestamp=stamp)
                    if not active and self.inputs:
                        self.inputs.focus_lost()
                    previous_active = active
                started = time.perf_counter_ns()
                video.write(frame)
                self.encode_ms = (time.perf_counter_ns()-started) / 1e6
                index = video.frames-1
                self.frames = video.frames
                frame_info = dict(frame_index=index, video_pts_ns=round(index / self.config.fps * 1e9),
                                  game_focused=active, fresh=fresh, capture_available=available)
                frame_info.update({k: v for k, v in getattr(capture, "metadata", {}).items()
                                   if k != "capture_available"})
                if self.config.capture_performance:
                    frame_info["capture_latency_ms"] = self.capture_ms
                self.writer.emit("frames", "VIDEO_FRAME", frame_info, timestamp=stamp)
                if available and self.ocr and now >= next_ocr:
                    self.ocr.submit(frame, stamp, index)
                    next_ocr = now + 1 / self.config.ocr_max_hz
                if now >= next_obs:
                    self.latest_preview = frame.copy()
                    digest = hashlib.blake2b(frame[::16, ::16].tobytes(), digest_size=8).hexdigest()
                    self.writer.emit("events", "OBSERVATION", dict(frame_index=index, game_focused=active,
                        capture_available=available, visual_hash=digest, weak_label=True), timestamp=stamp)
                    if self.bridge is not None:
                        self.observe_telemetry(index, stamp)
                    next_obs = now + 1 / self.config.observation_hz
                if now >= next_storage:
                    self.storage_check()
                    next_storage = now + 10
                if now-next_frame > 0.5:
                    self.writer.emit("events", "CAPTURE_DEADLINE_MISS", {"lag_ms": (now-next_frame)*1000})
                    # Do not fabricate catch-up frames. Exact acquisition -> video mapping stays explicit.
                    next_frame = now + 1 / self.config.fps
        except Exception as exc:
            self.fail("COLLECTOR", exc)
        finally:
            for name in ("input", "performance", "telemetry", "ocr", "capture", "video"):
                resource = {"capture": capture, "video": video}.get(name, workers.get(name))
                if resource is not None:
                    try:
                        resource.close()
                    except Exception as exc:
                        self.fail(name.upper()+"_CLOSE", exc)
                        if self.writer:
                            self.stream_status("video" if name == "capture" else name,
                                               "unavailable", "shutdown_failed")
            if self.writer:
                sources = self.writer.manifest["capture_streams"]["sources"]
                for name in self.failed_sources.copy():
                    if sources[name]["status"] == "enabled":
                        self.stream_status(name, "unavailable", "runtime_failed")
                ocr_stats = self.ocr.stats() if self.ocr else {}
                self.writer.close("failed" if self.error else "complete",
                    dict(error=self.error, encoded_frames=video.frames if video else 0,
                         **ocr_stats))
                self.notify(f"Episode 已保存：{self.writer.path}")
        if self.error:
            raise RuntimeError(self.error)
        return self.writer.path

    def observe_telemetry(self, frame_index, stamp):
        bridge = self.bridge
        if bridge.latest_pose_ns is None or stamp-bridge.latest_pose_ns > 1e9:
            self.writer.emit("pose", "POSE_UNAVAILABLE", dict(x=None, y=None, yaw=None,
                valid=False, confidence=None, source="no_fresh_bgi_pose"), timestamp=stamp)
        ui = bridge.latest_ui if bridge.latest_ui_ns is not None and stamp-bridge.latest_ui_ns < 2e9 else None
        mode = (ui or {}).get("mode", "UNKNOWN")
        self.writer.emit("ui", "UI_OBSERVATION", dict(mode=mode,
            valid=ui is not None and str(mode).upper() != "UNKNOWN", frame_index=frame_index,
            source=bridge.latest_ui_source if ui else "unknown"), timestamp=stamp)

    def state(self, stream, kind, data):
        if stream == "ui" and self.ocr:
            self.ocr.update_mode(data.get("mode", "UNKNOWN"), "bettergi")

    def mark(self, kind, text=""):
        if self.writer:
            self.writer.mark(kind, text)
