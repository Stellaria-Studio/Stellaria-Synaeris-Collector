"""Real C# transport stress; synthetic source events never send game input."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
from synaeris_collector.bridge import TelemetryBridge
from synaeris_collector.config import Config
from synaeris_collector.storage import EpisodeWriter, atomic_json, iter_rows

ROOT = Path(__file__).resolve().parents[1]


def run(name, disconnect=False):
    writer = EpisodeWriter(Config(output=str(ROOT / "reports/local/transport_stress"), capture="synthetic"))
    bridge = TelemetryBridge(writer, 0)
    fault_count = 0
    if disconnect:
        handler = bridge.server.RequestHandlerClass
        original_reply = handler.reply
        def fault_reply(self, code, data):
            nonlocal fault_count
            # Commit has already happened; the client must retry without making
            # a second training row. Lose precisely one ACK per selected event.
            if (self.path == "/telemetry" and code == 200
                    and not data.get("duplicate", False) and fault_count < 8):
                fault_count += 1
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            original_reply(self, code, data)
        handler.reply = fault_reply
    bridge.start()
    try:
        env = dict(os.environ, SYNAERIS_TELEMETRY_URL=f"http://127.0.0.1:{bridge.server.server_port}")
        subprocess.run(["dotnet", str(ROOT / "tools/telemetry_probe/bin/Release/net9.0/TelemetryProbe.dll"),
                        "--burst", "1000"], env=env, check=True, timeout=25)
    finally:
        bridge.close()
        writer.close()
    rows = list(iter_rows(writer.path, "events"))
    received = [r["payload"]["index"] for r in rows if r["kind"] == "SYNTHETIC_STRESS"]
    marker = next((r for r in rows if r["kind"] == "SYNTHETIC_STRESS_END"), None)
    diagnostics = (marker or rows[-1])["payload"].get("producer_diagnostics", {})
    report = {"test": name, "synthetic": True, "real_gameplay": False, "episode": str(writer.path),
        "expected": 1000, "received": len(received), "unique": len(set(received)),
        "missing": sorted(set(range(1000)) - set(received)), "injected_ack_failures": fault_count,
        "producer_drops": (marker or rows[-1])["payload"].get("producer_telemetry_drops"),
        "diagnostics": diagnostics, "marker_received": marker is not None,
        "passed": (len(received) == 1000 and set(received) == set(range(1000))
                   and marker is not None and marker["payload"].get("producer_telemetry_drops") == 0)}
    atomic_json(ROOT / f"reports/local/transport_stress/{name}.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--disconnect", action="store_true")
    args = parser.parse_args()
    raise SystemExit(0 if run(args.name, args.disconnect)["passed"] else 1)
