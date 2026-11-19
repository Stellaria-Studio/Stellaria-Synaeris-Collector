import shutil
import subprocess
import threading
import psutil
from .capture import hidden_process_options


def gpu_sample():
    executable = shutil.which("nvidia-smi")
    if not executable:
        return {"available": False}
    fields = ["utilization.gpu", "memory.used", "memory.total", "power.draw", "clocks.current.graphics"]
    p = subprocess.run([executable, "--query-gpu="+",".join(fields), "--format=csv,noheader,nounits"],
                       capture_output=True, timeout=3, **hidden_process_options())
    if p.returncode:
        return {"available": False, "error": p.stderr.decode(errors="replace")[-200:]}
    values = p.stdout.decode().splitlines()[0].split(",")
    result = {"available": True}
    for field, value in zip(["gpu_utilization_pct", "vram_used_mib", "vram_total_mib", "gpu_power_w", "gpu_clock_mhz"], values):
        try:
            result[field] = float(value.strip())
        except ValueError:
            result[field] = None
    return result


class PerfWorker:
    def __init__(self, writer, config, metrics, failure):
        self.writer, self.config, self.metrics, self.failure = writer, config, metrics, failure
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.successful_samples = self.failed_samples = self.consecutive_failures = 0
        self.open_gap_start_ns = None
        self.worker_failed = False

    def start(self):
        self.thread.start()

    def run(self):
        try:
            process = psutil.Process()
        except Exception as exc:
            self.worker_failed = True
            self.failure("PERFORMANCE_WORKER", exc)
            return
        while not self.stop_event.is_set():
            start = self.writer.now()
            try:
                gpu = gpu_sample()
                ram = psutil.virtual_memory()
                data = {**gpu, "cpu_pct": psutil.cpu_percent(), "ram_used_bytes": ram.used,
                        "collector_rss_bytes": process.memory_info().rss,
                        "collector_cpu_pct": process.cpu_percent(), "game_fps": None,
                        "game_frame_time_ms": None, "game_fps_source": "unavailable",
                        **self.metrics(), "sample_duration_ms": (self.writer.now()-start)/1e6}
                if self.writer.emit("perf", "PERF_SAMPLE", data, source="psutil+nvidia-smi", timestamp=start):
                    self.successful_samples += 1
                    self.consecutive_failures = 0
                    self.open_gap_start_ns = None
            except Exception as exc:
                self.failed_samples += 1
                self.consecutive_failures += 1
                if self.open_gap_start_ns is None:
                    self.open_gap_start_ns = start
                self.failure("PERFORMANCE_SAMPLE", exc, fatal=False, timestamp=start)
            self.stop_event.wait(1 / self.config.perf_hz)

    def close(self):
        self.stop_event.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("Performance worker shutdown timeout")
        if self.worker_failed or (self.failed_samples and not self.successful_samples) or self.consecutive_failures >= 2:
            state = "unavailable"
        elif self.failed_samples:
            state = "partial"
        elif self.successful_samples:
            state = "sampled"
        else:
            state = "no_samples"
        return {"state": state, "successful_samples": self.successful_samples, "failed_samples": self.failed_samples,
                "consecutive_failures": self.consecutive_failures, "open_gap_start_ns": self.open_gap_start_ns}
