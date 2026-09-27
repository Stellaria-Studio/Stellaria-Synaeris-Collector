import hmac
import hashlib
import json
import secrets
import threading
from collections import Counter, OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ALLOWED = {"pose", "ui", "objects", "quest", "tools", "events", "ocr", "input"}


class TelemetryBridge:
    """Read-only loopback sink. Never sends game input or executes commands."""
    def __init__(self, writer, port, on_state=None):
        self.writer = writer
        self.token = secrets.token_urlsafe(24)
        self.on_state = on_state or (lambda stream, kind, data: None)
        self.latest_pose = None
        self.latest_pose_ns = None
        self.latest_ui = None
        self.latest_ui_ns = None
        self.latest_ui_source = None
        self.active_tools = set()
        self.completed_pathing_routes = 0
        self.completed_tools = Counter()
        self.failed_tools = Counter()
        self.lock = threading.Lock()
        self.seen_native_events = OrderedDict()
        self.duplicate_events = 0
        self.closing = threading.Event()
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                self.connection.settimeout(3)

            def log_message(self, *_):
                pass

            def handle(self):
                try:
                    super().handle()
                except (ConnectionResetError, BrokenPipeError):
                    # A disconnected loopback client can reconnect and retry
                    # its stable event identity. There is no partial row here.
                    self.close_connection = True

            def reply(self, code, data):
                encoded = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                if bridge.closing.is_set() or code >= 400:
                    self.close_connection = True
                    self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self):
                if bridge.closing.is_set():
                    self.reply(503, {"error": "episode finalizing"})
                    return
                if self.path == "/session":
                    self.reply(200, {"episode_id": writer.episode_id, "token": bridge.token,
                                    "qpc_origin_ns": writer.origin_ns, "timestamp_unit": "ns"})
                elif self.path == "/health":
                    with bridge.lock:
                        state = {"status": "recording", "episode_id": writer.episode_id,
                                 "active_tool_count": len(bridge.active_tools),
                                 "completed_pathing_routes": bridge.completed_pathing_routes,
                                 "completed_tools": dict(bridge.completed_tools),
                                 "failed_tools": dict(bridge.failed_tools),
                                 "duplicate_native_events": bridge.duplicate_events}
                    self.reply(200, state)
                else:
                    self.reply(404, {"error": "unknown endpoint"})

            def do_POST(self):
                if bridge.closing.is_set():
                    self.reply(503, {"error": "episode finalizing"})
                    return
                if self.path != "/telemetry":
                    self.reply(404, {"error": "unknown endpoint"})
                    return
                if not hmac.compare_digest(self.headers.get("X-Synaeris-Token", ""), bridge.token):
                    self.reply(403, {"error": "session token required"})
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 262144:
                        self.reply(413, {"error": "invalid payload size"})
                        return
                    self.connection.settimeout(3)
                    item = json.loads(self.rfile.read(size))
                    fresh = bridge.ingest(item)
                    self.reply(200, {"accepted": True, "duplicate": fresh is False})
                except (ValueError, KeyError, TypeError, OverflowError) as exc:
                    self.reply(400, {"error": str(exc)})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def ingest(self, item):
        if item.get("episode_id") != self.writer.episode_id:
            raise ValueError("stale episode")
        stream = item["stream"]
        if stream not in ALLOWED:
            raise ValueError("stream not allowed")
        data = item.get("payload", {})
        if not isinstance(data, dict):
            raise ValueError("payload must be object")
        event_id = item.get("producer_event_id")
        fingerprint = None
        if event_id is not None:
            if item.get("source") != "bettergi_native" or not isinstance(event_id, str) or not 1 <= len(event_id) <= 128:
                raise ValueError("invalid native event identity")
            fingerprint = hashlib.sha256(json.dumps({key: item.get(key) for key in
                ("episode_id", "stream", "kind", "source", "qpc_ns", "payload")},
                sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            data = {**data, "producer_event_id": event_id,
                    "producer_sequence": item.get("producer_sequence")}
        if "telemetry_drops" in item:
            data = {**data, "producer_telemetry_drops": int(item["telemetry_drops"])}
        if isinstance(item.get("telemetry_diagnostics"), dict):
            data = {**data, "producer_diagnostics": item["telemetry_diagnostics"]}
        kind = item.get("kind", "TELEMETRY")
        if not isinstance(kind, str) or len(kind) > 128:
            raise ValueError("invalid kind")
        arrival = self.writer.now()
        stamp = arrival
        if "qpc_ns" in item:
            stamp = int(item["qpc_ns"]) - self.writer.origin_ns
            if stamp < 0 or stamp > arrival + 100_000_000 or arrival-stamp > 5_000_000_000:
                raise ValueError("producer clock outside synchronized window")
            data = {**data, "bridge_delay_ns": arrival - stamp, "clock_quality": "QPC"}
        else:
            data = {**data, "clock_quality": "arrival_only", "producer_wall_ms": item.get("wall_ms")}
        if stream == "pose":
            data = {"x": None, "y": None, "yaw": None, "valid": False, "confidence": None, **data}
            if data["valid"] and (data["x"] is None or data["y"] is None):
                raise ValueError("valid pose requires coordinates")
            if data["confidence"] is not None and not 0 <= float(data["confidence"]) <= 1:
                raise ValueError("invalid confidence")
        source = item.get("source", "bettergi")
        if not isinstance(source, str) or len(source) > 128:
            raise ValueError("invalid source")
        if stream == "input" and (source != "bettergi_native" or kind != "BGI_INPUT_DISPATCH"):
            raise ValueError("input telemetry requires native dispatch observation")
        actor = "AGENT" if source == "codex_ui" else (
            "BGI" if source in {"bettergi", "bettergi_native", "bettergi_js"} else "UNKNOWN")
        if source == "codex_ui":
            data = {**data, "annotation_provenance": "agent_declared", "weak_label": True}
        with self.lock:
            if event_id in self.seen_native_events:
                if self.seen_native_events[event_id] != fingerprint:
                    raise ValueError("native event identity reused with different content")
                self.duplicate_events += 1
                return False
            if self.closing.is_set() or not self.writer.emit(stream, kind, data, source=source, actor=actor, timestamp=stamp):
                raise ValueError("episode finalizing")
            if event_id is not None:
                self.seen_native_events[event_id] = fingerprint
                if len(self.seen_native_events) > 8192:
                    self.seen_native_events.popitem(last=False)
            if stream == "pose":
                self.latest_pose, self.latest_pose_ns = data, stamp
            elif stream == "ui":
                self.latest_ui, self.latest_ui_ns = data, stamp
                self.latest_ui_source = source
            elif stream == "tools":
                tool_id = data.get("tool_id", data.get("tool", "unknown"))
                if kind == "TOOL_START":
                    self.active_tools.add(tool_id)
                elif kind in {"TOOL_END", "TOOL_FAILED", "TOOL_CANCELLED"}:
                    was_active = tool_id in self.active_tools
                    self.active_tools.discard(tool_id)
                    if was_active and source == "bettergi_native":
                        tool = str(data.get("tool", "unknown"))
                        if kind == "TOOL_END":
                            self.completed_tools[tool] += 1
                            if tool == "Pathing":
                                self.completed_pathing_routes += 1
                        else:
                            self.failed_tools[tool] += 1
        self.on_state(stream, kind, data)
        return True

    def close(self):
        self.closing.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
