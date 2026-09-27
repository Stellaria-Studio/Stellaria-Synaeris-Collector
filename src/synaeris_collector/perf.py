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

    def start(self):
        self.thread.start()

    def run(self):
        process = psutil.Process()
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
                self.writer.emit("perf", "PERF_SAMPLE", data, source="psutil+nvidia-smi", timestamp=start)
            except Exception as exc:
                self.failure("PERFORMANCE_SAMPLE", exc, fatal=False)
            self.stop_event.wait(1 / self.config.perf_hz)

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("Performance worker shutdown timeout")
