from pathlib import Path
import argparse
import os
import subprocess
from synaeris_collector.config import Config
from synaeris_collector.storage import EpisodeWriter, iter_rows, atomic_json
from synaeris_collector.bridge import TelemetryBridge

parser = argparse.ArgumentParser()
parser.add_argument("--startup", action="store_true")
args = parser.parse_args()
root = Path(__file__).resolve().parent.parent
writer = EpisodeWriter(Config(output=str(root / "reports/local/native_bridge"), capture="synthetic"))
bridge = TelemetryBridge(writer, 0)
bridge.start()
try:
    env = dict(os.environ, SYNAERIS_TELEMETRY_URL=f"http://127.0.0.1:{bridge.server.server_port}")
    command = ["dotnet", str(root / "tools/telemetry_probe/bin/Release/net9.0/TelemetryProbe.dll")]
    if args.startup:
        command.append("--startup")
    subprocess.run(command, env=env, check=True, timeout=15)
finally:
    bridge.close()
    writer.close()
rows = [row for stream in ["ui", "pose", "tools", "input"] for row in iter_rows(writer.path, stream)]
assert len(rows) == 5 and all(row["payload"]["clock_quality"] == "QPC" for row in rows)
dispatch = next(row for row in rows if row["kind"] == "BGI_INPUT_DISPATCH")
assert dispatch["actor"] == "BGI" and dispatch["payload"]["game_effect_verified"] is False
assert all(-100_000_000 < row["payload"]["bridge_delay_ns"] < 1_000_000_000 for row in rows)
atomic_json(root / ("reports/local/native_bridge_startup_result.json" if args.startup else "reports/local/native_bridge_result.json"), {"passed": True, "events": len(rows),
    "maximum_bridge_delay_ms": max(row["payload"]["bridge_delay_ns"] for row in rows)/1e6,
    "synthetic": True, "real_gameplay": False, "startup_buffer_test": args.startup,
    "producer_drops": max(row["payload"].get("producer_telemetry_drops", 0) for row in rows)})
assert all(row["payload"].get("producer_telemetry_drops", 0) == 0 for row in rows)
print(".NET -> Python source QPC telemetry: passed (5 synthetic events, no input sent)")
