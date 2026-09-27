import importlib.metadata
import queue
import threading
import time
import cv2
import numpy as np


class LocalOCR:
    def __init__(self, threads=2):
        from rapidocr import RapidOCR
        # OpenCV defaulted to 20 workers on the collection machine, independently
        # of ONNX's configured thread budget. Bound preprocessing as well.
        cv2.setNumThreads(threads)
        self.engine = RapidOCR(params={
            "EngineConfig.onnxruntime.intra_op_num_threads": threads,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            "Det.limit_type": "max",
            "Det.limit_side_len": 960,
            "Global.log_level": "warning",
        })
        self.version = importlib.metadata.version("rapidocr")

    def read(self, image, offset=(0, 0)):
        start = time.perf_counter_ns()
        result = self.engine(image)
        rows = []
        if result.txts is not None:
            for text, box, score in zip(result.txts, result.boxes, result.scores):
                bbox = np.asarray(box).astype(float) + np.array(offset, dtype=float)
                rows.append(dict(text=str(text), bbox=bbox.tolist(), confidence=float(score),
                                 source="rapidocr_cpu", version=self.version))
        return rows, (time.perf_counter_ns() - start) / 1e6


def roi_crop(frame, roi):
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = int(roi[0]*w), int(roi[1]*h), int(roi[2]*w), int(roi[3]*h)
    return frame[y1:y2, x1:x2], (x1, y1)


class OCRWorker:
    MAX_ROIS_PER_FRAME = 2
    KNOWN_MODE_HOLD_SECONDS = 1.5

    def __init__(self, config, writer, failure):
        self.config, self.writer, self.failure = config, writer, failure
        self.queue = queue.Queue(maxsize=1)
        self.stop_event = threading.Event()
        self.mode = "UNKNOWN"
        self.mode_source = "unknown"
        self.last_known_mode_at = None
        self.unknown_modes_suppressed = 0
        self.last = {}
        self.signatures = {}
        self.cache = {}
        self.cache_counts = {}
        self.dropped = 0
        self.submitted = 0
        self.processed_frames = 0
        self.scanned_regions = 0
        self.cached_regions = 0
        self.latency_ms = None
        self.thread = threading.Thread(target=self.run, name="synaeris-ocr", daemon=True)

    def start(self):
        self.thread.start()

    def submit(self, frame, timestamp, frame_index):
        item = (frame.copy(), timestamp, frame_index)
        self.submitted += 1
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            # OCR is an observation side-channel. Keeping an older frame makes
            # labels stale while adding no video/input evidence, so replace it
            # with the newest frame rather than blocking capture.
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            self.dropped += 1
            try:
                self.queue.put_nowait(item)
            except queue.Full:
                # The worker won the race and another producer cannot exist,
                # but keep the capture path non-blocking if that changes.
                self.dropped += 1

    def update_mode(self, mode, source="unknown", now=None):
        """Keep short BGI recognition gaps from turning combat into dialogue OCR.

        Raw UI_STATE telemetry is still stored unchanged. This only stabilizes
        the OCR crop scheduler; a positive Talk or BigMap result takes effect
        immediately.
        """
        value = str(mode or "UNKNOWN").upper()
        now = time.monotonic() if now is None else now
        if value != "UNKNOWN":
            self.mode = value
            self.last_known_mode_at = now
        elif (source == "bettergi" and self.last_known_mode_at is not None
              and now - self.last_known_mode_at < self.KNOWN_MODE_HOLD_SECONDS):
            self.unknown_modes_suppressed += 1
        else:
            self.mode = "UNKNOWN"
        self.mode_source = source
        return self.mode

    def selected_rois(self, mode, timestamp):
        if "TITLE" in mode:
            configured = {"title": 1}
        elif "DIALOG" in mode or "TALK" in mode:
            configured = {"dialogue": 6, "options": 4}
        elif "MAP" in mode:
            configured = {"map": 3}
        elif "WORLD" in mode or "MAIN" in mode:
            configured = {"quest": 1.5, "interaction": 8, "receipt": 1}
        else:
            # Standalone human capture has no BGI UI source. Keep slow map and
            # receipt probes so those scenes remain indexable. The old title
            # crop covered the bottom-center HP/level strip and is therefore
            # not scheduled without a positive title-mode source.
            configured = {"quest": 1, "interaction": 5, "dialogue": 2, "options": .5,
                          "receipt": 1, "map": .25}
        now = timestamp / 1e9
        due = []
        for order, (name, hz) in enumerate(configured.items()):
            if name not in self.config.rois:
                continue
            interval = 1 / min(hz, self.config.ocr_max_hz)
            due_at = self.last.get(name, -1e9) + interval
            if now >= due_at:
                due.append((name in self.last, due_at, order, name))
        # Oldest deadline first gives every UNKNOWN-mode ROI a turn. A bounded
        # per-frame budget keeps two CPU OCR threads inside the 4 Hz envelope.
        due.sort(key=lambda item: (item[0], item[2] if not item[0] else item[1], item[2]))
        return [name for _, _, _, name in due[:self.MAX_ROIS_PER_FRAME]]

    def stats(self):
        return {"ocr_queue_drops": self.dropped,
                "ocr_requests_coalesced": self.dropped,
                "ocr_requests_submitted": self.submitted,
                "ocr_frames_processed": self.processed_frames,
                "ocr_regions_scanned": self.scanned_regions,
                "ocr_regions_cached": self.cached_regions,
                "ocr_unknown_modes_suppressed": self.unknown_modes_suppressed,
                "ocr_latest_frame_wins": True,
                "ocr_max_rois_per_frame": self.MAX_ROIS_PER_FRAME}

    def run(self):
        try:
            engine = LocalOCR(self.config.ocr_threads)
            while not self.stop_event.is_set() or not self.queue.empty():
                try:
                    frame, timestamp, index = self.queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                mode = self.mode.upper()
                self.processed_frames += 1
                queue_delay_ms = max(0.0, (self.writer.now() - timestamp) / 1e6)
                for name in self.selected_rois(mode, timestamp):
                    now = timestamp / 1e9
                    self.last[name] = now
                    crop, offset = roi_crop(frame, self.config.rois[name])
                    signature = cv2.resize(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), (64, 24))
                    previous = self.signatures.get(name)
                    unchanged = previous is not None and float(np.mean(np.abs(signature.astype(float)-previous))) < 1.0
                    if unchanged and now - self.cache.get(name, -1e9) < 2.0:
                        self.cached_regions += 1
                        self.writer.emit("ocr", "OCR_CACHE", dict(roi=name, frame_index=index,
                            reused_timestamp_ns=self.cache[name] * 1e9, text_count=self.cache_counts.get(name, 0),
                            mode=mode, queue_delay_ms=queue_delay_ms,
                            observation_age_ms=max(0.0, (self.writer.now() - timestamp) / 1e6)),
                            source="rapidocr_cpu", timestamp=timestamp)
                        continue
                    self.signatures[name] = signature
                    rows, latency = engine.read(crop, offset)
                    self.cache[name] = now
                    self.cache_counts[name] = len(rows)
                    self.latency_ms = latency
                    self.scanned_regions += 1
                    self.writer.emit("ocr", "OCR_SCAN", dict(roi=name, frame_index=index,
                        mode=mode, latency_ms=latency, text_count=len(rows), version=engine.version,
                        opencv_threads=cv2.getNumThreads(), onnx_threads=self.config.ocr_threads,
                        queue_delay_ms=queue_delay_ms,
                        observation_age_ms=max(0.0, (self.writer.now() - timestamp) / 1e6)),
                        source="rapidocr_cpu", timestamp=timestamp)
                    for row in rows:
                        self.writer.emit("ocr", "OCR_TEXT", {**row, "roi": name, "frame_index": index,
                            "weak_label": True}, source="rapidocr_cpu", timestamp=timestamp)
                        candidate = {**row, "roi": name, "frame_index": index, "ui_mode": mode,
                            "weak_label": True, "semantic_role_verified": False}
                        if name == "quest":
                            self.writer.emit("quest", "QUEST_TEXT_CANDIDATE", {
                                **candidate, "quest_id": None, "stage": None},
                                source="rapidocr_cpu", timestamp=timestamp)
                        elif name == "interaction":
                            self.writer.emit("objects", "UI_AFFORDANCE_CANDIDATE", {
                                **candidate, "coordinate_frame": "game_client_pixels",
                                "world_position": None}, source="rapidocr_cpu", timestamp=timestamp)
                        elif name == "receipt":
                            self.writer.emit("objects", "ITEM_RECEIPT_CANDIDATE", {
                                **candidate, "coordinate_frame": "game_client_pixels",
                                "item_id": None, "quantity": None,
                                "inventory_delta_verified": False},
                                source="rapidocr_cpu", timestamp=timestamp)
        except Exception as exc:
            self.failure("OCR", exc)

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=30)
        if self.thread.is_alive():
            raise RuntimeError("OCR worker did not stop")
