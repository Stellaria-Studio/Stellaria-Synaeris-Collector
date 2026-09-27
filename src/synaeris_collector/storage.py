"""Bounded row-group storage plus a recoverable, append-only event journal."""
import hashlib
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import asdict
import pyarrow as pa
import pyarrow.parquet as pq

STREAMS = ("input", "ocr", "ui", "pose", "objects", "quest", "tools", "events", "perf", "frames")
SCHEMA = pa.schema([
    ("schema_version", pa.int32()), ("episode_id", pa.string()),
    ("monotonic_timestamp", pa.int64()), ("sequence", pa.int64()),
    ("kind", pa.string()), ("source", pa.string()), ("actor", pa.string()),
    ("payload_json", pa.string()),
])


def json_text(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temp.replace(path)


class EpisodeWriter:
    def __init__(self, config, provenance=None):
        self.episode_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:10]
        self.path = Path(config.output).resolve() / self.episode_id
        self.path.mkdir(parents=True, exist_ok=False)
        self.origin_ns = time.perf_counter_ns()
        self.lock = threading.RLock()
        self.closed = False
        self.seq = 0
        self.counts = {s: 0 for s in STREAMS}
        self.buffers = {s: [] for s in STREAMS}
        self.writers = {s: pq.ParquetWriter(self.path / f"{s}.parquet", SCHEMA, compression="zstd") for s in STREAMS}
        self.journal = (self.path / "journal.jsonl").open("a", encoding="utf-8", buffering=1)
        self.annotations = (self.path / "annotations.jsonl").open("a", encoding="utf-8", buffering=1)
        self.manifest = {
            "schema_version": 1, "episode_id": self.episode_id, "status": "recording",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "clock": {"unit": "nanoseconds", "origin": "episode-relative",
                      "source": "perf_counter/QPC", "qpc_origin_ns": self.origin_ns},
            "config": asdict(config), "provenance": provenance or {},
            "synthetic": config.capture == "synthetic", "streams": list(STREAMS),
            "unavailable": ["game_fps_without_presentmon", "model_metrics_before_model_exists"],
        }
        from .human import profile_descriptor
        self.manifest['collection_profile'] = profile_descriptor(config)
        atomic_json(self.path / "manifest.json", self.manifest)

    def now(self):
        return time.perf_counter_ns() - self.origin_ns

    def emit(self, stream, kind, payload=None, source="collector", actor="UNKNOWN", timestamp=None):
        if stream not in STREAMS:
            raise ValueError("Unknown stream")
        with self.lock:
            if self.closed:
                return False
            stamp = self.now() if timestamp is None else int(timestamp)
            if stamp < 0:
                raise ValueError("Negative episode timestamp")
            self.seq += 1
            row = dict(schema_version=1, episode_id=self.episode_id, monotonic_timestamp=stamp,
                       sequence=self.seq, kind=kind, source=source, actor=actor,
                       payload_json=json_text(payload or {}))
            # Journal every stream: abrupt process termination can rebuild complete Parquet.
            self.journal.write(json_text({"stream": stream, **row}) + "\n")
            self.buffers[stream].append(row)
            self.counts[stream] += 1
            if len(self.buffers[stream]) >= 128:
                self._flush(stream)
            return True

    def _flush(self, stream):
        if self.buffers[stream]:
            self.writers[stream].write_table(pa.Table.from_pylist(self.buffers[stream], schema=SCHEMA))
            self.buffers[stream].clear()

    def mark(self, kind, text="", timestamp=None):
        stamp = self.now() if timestamp is None else int(timestamp)
        with self.lock:
            if self.closed:
                return
            item = dict(episode_id=self.episode_id, monotonic_timestamp=stamp, marker=kind,
                        text=text, requested_start_ns=max(0, stamp - 15_000_000_000),
                        requested_end_ns=stamp + 20_000_000_000, actor="HUMAN")
            self.annotations.write(json_text(item) + "\n")
            self.emit("events", kind, item, actor="HUMAN", timestamp=stamp)

    def close(self, status="complete", extra=None):
        with self.lock:
            if self.closed:
                return
            self.emit("events", "EPISODE_END", {"status": status})
            self.closed = True
            for stream in STREAMS:
                self._flush(stream)
                self.writers[stream].close()
            self.journal.close()
            self.annotations.close()
            self.manifest.update(status=status, duration_ns=self.now(), counts=self.counts)
            self.manifest.update(extra or {})
            self.manifest["files"] = {
                p.name: {"bytes": p.stat().st_size} for p in self.path.iterdir()
                if p.is_file() and p.name != "manifest.json"
            }
            atomic_json(self.path / "manifest.json", self.manifest)


def iter_rows(path, stream):
    file = Path(path) / f"{stream}.parquet"
    for batch in pq.ParquetFile(file).iter_batches(batch_size=512):
        for row in batch.to_pylist():
            row["payload"] = json.loads(row.pop("payload_json"))
            yield row


def recover(path):
    path = Path(path)
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    if manifest["status"] == "complete":
        raise ValueError("Complete episodes do not need recovery")
    writers = {s: pq.ParquetWriter(path / f"{s}.recovered.parquet", SCHEMA, compression="zstd") for s in STREAMS}
    buffers = {s: [] for s in STREAMS}
    counts = {s: 0 for s in STREAMS}
    corrupt = 0
    with (path / "journal.jsonl").open(encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                stream = row.pop("stream")
                if stream not in STREAMS or row["episode_id"] != manifest["episode_id"]:
                    raise ValueError("Journal identity mismatch")
                buffers[stream].append(row)
                counts[stream] += 1
                if len(buffers[stream]) >= 128:
                    writers[stream].write_table(pa.Table.from_pylist(buffers[stream], schema=SCHEMA))
                    buffers[stream].clear()
            except (ValueError, KeyError):
                corrupt += 1
    for stream in STREAMS:
        if buffers[stream]:
            writers[stream].write_table(pa.Table.from_pylist(buffers[stream], schema=SCHEMA))
        writers[stream].close()
        original = path / f"{stream}.parquet"
        if original.exists():
            original.replace(path / f"{stream}.pre-recovery.parquet")
        (path / f"{stream}.recovered.parquet").replace(original)
    manifest.update(status="recovered_partial", counts=counts, corrupt_journal_lines=corrupt)
    atomic_json(path / "manifest.json", manifest)
    return manifest
