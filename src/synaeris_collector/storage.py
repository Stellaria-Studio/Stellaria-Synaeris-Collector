"""Bounded row-group storage plus a recoverable, append-only event journal."""
import json
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import asdict
from contextlib import ExitStack, contextmanager
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


@contextmanager
def episode_exclusive(path):
    # The same file must survive the lock holder and every retry. Removing it
    # lets concurrent processes lock different files for the same episode.
    with (path / ".episode.lock").open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Episode already in use or its lock is unavailable") from exc
        # Closing the descriptor releases the OS lock, including on process exit.
        yield


class EpisodeWriter:
    def __init__(self, config, provenance=None):
        self.episode_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:10]
        self.path = Path(config.output).resolve() / self.episode_id
        self.path.mkdir(parents=True, exist_ok=False)
        with ExitStack() as resources:
            resources.enter_context(episode_exclusive(self.path))
            self._open(config, provenance, resources)
            self.resources = resources.pop_all()

    def _open(self, config, provenance, resources):
        self.origin_ns = time.perf_counter_ns()
        self.lock = threading.RLock()
        self.closed = False
        self.seq = 0
        self.counts = {s: 0 for s in STREAMS}
        self.buffers = {s: [] for s in STREAMS}
        self.writers = {s: resources.enter_context(pq.ParquetWriter(
            self.path / f"{s}.parquet", SCHEMA, compression="zstd")) for s in STREAMS}
        self.journal = resources.enter_context((self.path / "journal.jsonl").open("a", encoding="utf-8", buffering=1))
        self.annotations = resources.enter_context(
            (self.path / "annotations.jsonl").open("a", encoding="utf-8", buffering=1))
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
            with self.resources:
                try:
                    self.emit("events", "EPISODE_END", {"status": status})
                    for stream in STREAMS:
                        self._flush(stream)
                        self.writers[stream].close()
                    self.journal.close()
                    self.annotations.close()
                    self.manifest.update(status=status, duration_ns=self.now(), counts=self.counts)
                    self.manifest.update(extra or {})
                    self.manifest["files"] = {
                        p.name: {"bytes": p.stat().st_size} for p in self.path.iterdir()
                        if p.is_file() and p.name not in ("manifest.json", ".episode.lock")
                    }
                    atomic_json(self.path / "manifest.json", self.manifest)
                finally:
                    self.closed = True


def iter_rows(path, stream):
    path = Path(path)
    manifest = path / "manifest.json"
    if manifest.exists() and _episode_status(path) == "recovering":
        raise RuntimeError("Episode recovery is incomplete")
    file = path / f"{stream}.parquet"
    for batch in pq.ParquetFile(file).iter_batches(batch_size=512):
        for row in batch.to_pylist():
            row["payload"] = json.loads(row.pop("payload_json"))
            yield row


def recover(path):
    path = Path(path)
    if _episode_status(path) == "complete":
        raise ValueError("Complete episodes do not need recovery")
    with episode_exclusive(path):
        return _recover_locked(path)


def _episode_status(path):
    return json.loads((path / "manifest.json").read_text(encoding="utf-8"))["status"]


def _recover_locked(path):
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    if manifest["status"] == "complete":
        raise ValueError("Complete episodes do not need recovery")
    with tempfile.TemporaryDirectory(prefix=".recovery-", dir=path) as staging:
        staging = Path(staging)
        with ExitStack() as files:
            writers = {s: files.enter_context(pq.ParquetWriter(
                staging / f"{s}.parquet", SCHEMA, compression="zstd")) for s in STREAMS}
            counts, corrupt = _replay_journal(path, manifest["episode_id"], writers)

        originals = manifest.get("recovery_original_streams")
        if originals is None:
            # A legacy recovered cache cannot reveal which originals were missing.
            suffix = ".pre-recovery.parquet" if manifest["status"] == "recovered_partial" else ".parquet"
            originals = [s for s in STREAMS if (path / (s + suffix)).exists()]
        if not isinstance(originals, list) or any(s not in STREAMS for s in originals):
            raise ValueError("Invalid recovery source inventory")
        if "files" in manifest:
            manifest.setdefault("pre_recovery_files", dict(manifest["files"]))
        manifest.update(status="recovering", recovery_original_streams=originals)
        atomic_json(path / "manifest.json", manifest)
        for stream in STREAMS:
            original = path / f"{stream}.parquet"
            backup = path / f"{stream}.pre-recovery.parquet"
            if stream in originals and not backup.exists():
                # Link without overwriting. The original remains reachable until
                # the staged replacement is installed, including on interruption.
                os.link(original, backup)
            (staging / original.name).replace(original)

    manifest.update(status="recovered_partial", counts=counts, corrupt_journal_lines=corrupt)
    files = manifest.setdefault("files", {})
    for stream in STREAMS:
        for suffix in (".parquet", ".pre-recovery.parquet"):
            file = path / (stream + suffix)
            if file.exists():
                files[file.name] = {"bytes": file.stat().st_size}
    atomic_json(path / "manifest.json", manifest)
    return manifest


def _replay_journal(path, episode_id, writers):
    buffers = {s: [] for s in STREAMS}
    counts = {s: 0 for s in STREAMS}
    corrupt = 0
    with (path / "journal.jsonl").open("rb") as f:
        for line in f:
            try:
                row = json.loads(line.decode("utf-8"))
                if not isinstance(row, dict):
                    raise ValueError("Journal row is not an object")
                stream = row.pop("stream")
                if stream not in STREAMS or row["episode_id"] != episode_id:
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
    return counts, corrupt
