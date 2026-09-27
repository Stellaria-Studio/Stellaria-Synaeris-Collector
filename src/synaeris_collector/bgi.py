import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from .storage import atomic_json
from .capture import hidden_process_options


def catalog(bgi_dir):
    root = Path(bgi_dir) / "User" / "AutoPathing"
    result = []
    for file in sorted(root.rglob("*.json")):
        try:
            data = json.loads(file.read_text(encoding="utf-8-sig"))
            positions = data.get("positions", data.get("waypoints", []))
            if not positions:
                continue
            result.append({"path": str(file.resolve()), "relative_path": str(file.relative_to(root)),
                "points": len(positions), "info": data.get("info", {}),
                "sha256": hashlib.sha256(file.read_bytes()).hexdigest()})
        except (ValueError, OSError):
            pass
    return result


def make_batch(routes, destination, port=18765):
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Batch destination must be new; existing BGI scripts are preserved")
    if not routes:
        raise ValueError("Select at least one existing route")
    destination.mkdir(parents=True)
    (destination / "assets").mkdir()
    entries = []
    for i, route in enumerate(routes):
        file = Path(route).resolve()
        data = json.loads(file.read_text(encoding="utf-8-sig"))
        if not data.get("positions", data.get("waypoints")):
            raise ValueError(f"Not a BGI route: {file}")
        asset = f"assets/route_{i:04d}.json"
        shutil.copyfile(file, destination / asset)
        entries.append({"asset": asset, "name": file.stem, "sha256": hashlib.sha256(file.read_bytes()).hexdigest()})
    atomic_json(destination / "batch.json", entries)
    atomic_json(destination / "manifest.json", {
        "manifest_version": 1, "name": "StellariaSynaerisCollector", "version": "0.1.0",
        "bgi_version": "0.64.0", "description": "已知路线采集；返回不冒充成功；失败停止批次",
        "authors": [{"name": "Stellaria"}], "main": "main.js",
        "http_allowed_urls": [f"http://127.0.0.1:{port}/*"],
    })
    main = '''(async function () {
    const base = "http://127.0.0.1:__PORT__";
    const response = await http.request("GET", base + "/session");
    if (response.status_code !== 200) throw new Error("Collector not recording");
    const session = JSON.parse(response.body);
    const headers = JSON.stringify({"Content-Type":"application/json", "X-Synaeris-Token":session.token});
    async function emit(kind, payload) {
        const body = JSON.stringify({episode_id:session.episode_id, stream:"tools", kind:kind,
            source:"bettergi_js", wall_ms:Date.now(), payload:payload});
        const result = await http.request("POST", base + "/telemetry", body, headers);
        if (result.status_code !== 200) throw new Error("Collector telemetry rejected");
    }
    const batch = JSON.parse(file.readTextSync("batch.json"));
    for (let i = 0; i < batch.length; ++i) {
        const entry = batch[i];
        const id = session.episode_id + "_route_" + i;
        await emit("TOOL_START", {tool_id:id, tool:"Pathing", route:entry.name, route_sha256:entry.sha256});
        try {
            await pathingScript.runFile(entry.asset);
            // Upstream Run() catches some internal errors: promise completion is NOT success.
            await emit("TOOL_END", {tool_id:id, tool:"Pathing", outcome:"unknown", verified:false,
                reason:"function_returned_requires_native_verifier"});
        } catch (error) {
            await emit("TOOL_FAILED", {tool_id:id, tool:"Pathing", outcome:"failure", verified:false,
                reason:String(error)});
            throw error;
        }
        await sleep(2000);
    }
})();
'''.replace("__PORT__", str(port))
    (destination / "main.js").write_text(main, encoding="utf-8")
    return destination.resolve()


def deploy_batch(batch, bgi_dir):
    batch = Path(batch).resolve()
    destination = Path(bgi_dir) / "User" / "JsScript" / batch.name
    if destination.exists():
        raise ValueError("BGI batch already exists; create a new batch name")
    shutil.copytree(batch, destination)
    return destination


def import_logs(bgi_dir, output):
    """Historical log evidence only; no RGB, QPC alignment or verified rewards invented."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    result, total = [], 0
    for file in sorted((Path(bgi_dir) / "log").glob("*.log")):
        count = 0
        out = output / (file.stem + ".jsonl")
        with file.open(encoding="utf-8-sig", errors="replace") as source, out.open("w", encoding="utf-8") as sink:
            last_wall = None
            for number, line in enumerate(source, 1):
                match = re.search(r"\[(\d{2}:\d{2}:\d{2}\.\d{3})\] \[(\w+)\]", line)
                if match:
                    last_wall = match.group(1)
                terms = {"开始": "LOG_START_CANDIDATE", "结束": "LOG_END_CANDIDATE", "失败": "LOG_FAILURE_CANDIDATE",
                         "取消": "LOG_CANCEL_CANDIDATE", "传送": "LOG_TELEPORT_CANDIDATE", "重试": "LOG_RETRY_CANDIDATE"}
                for term, kind in terms.items():
                    if term in line:
                        sink.write(json.dumps({"source_file": file.name, "source_line": number,
                            "wall_time": last_wall, "kind": kind, "text": line.strip(),
                            "weak_label": True, "verified": False, "monotonic_timestamp": None,
                            "visual_available": False}, ensure_ascii=False) + "\n")
                        count += 1
                        break
        result.append({"source": file.name, "sha256": hashlib.sha256(file.read_bytes()).hexdigest(), "candidates": count})
        total += count
    atomic_json(output / "manifest.json", {"type": "historical_log_only", "files": result,
        "candidates": total, "train_vla_ready": False, "limitation": "No synchronized visual/input/QPC streams"})
    return {"files": len(result), "candidates": total, "output": str(output.resolve())}
