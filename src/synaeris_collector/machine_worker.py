"""Protected, on-demand gameplay worker; no shell or generic command endpoint."""
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid
import psutil
from .capture import GameWindow, hidden_process_options, process_elevated
from .collector import Collector
from .config import Config
from .native_groups import COMBAT_STRATEGY, canonical_group, native_group, route_contract, semantic_hash
from .qualification import qualify
from .storage import atomic_json, iter_rows


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inside(path, parent):
    approved = Path(parent).absolute()
    parent = approved.resolve()
    if parent != approved:
        raise ValueError("Approved collection directory changed through a link")
    path = Path(path).resolve()
    if not path.is_relative_to(parent) or path == parent:
        raise ValueError("Collection path escapes its approved directory")
    return path


def validate_request(policy, request):
    if set(request) != {"schema_version", "request_id", "campaign_relative", "campaign_sha256", "requested_utc"}:
        raise ValueError("Only typed collection requests are supported")
    if request["schema_version"] != 1 or not re.fullmatch(r"[0-9a-f]{32}", request["request_id"]):
        raise ValueError("Invalid request identity")
    if not re.fullmatch(r"data/[A-Za-z0-9_]+/campaign\.json", request["campaign_relative"]):
        raise ValueError("Only a local prepared collection queue is accepted")
    root = Path(policy["workspace"]).absolute()
    if root.resolve() != root:
        raise ValueError("Approved workspace changed through a link")
    inside(root / "data", root)
    campaign_file = inside(root / request["campaign_relative"], root / "data")
    if sha256(campaign_file) != request["campaign_sha256"]:
        raise ValueError("Requested queue changed")
    campaign = read_json(campaign_file)
    if not campaign["stages"] or len(campaign["stages"]) > 50:
        raise ValueError("Invalid bounded collection queue")
    queue, seen, identities = [], set(), set()
    source_bgi = Path(policy["source_bgi"])
    for stage in campaign["stages"]:
        identity, group = stage["id"], stage["group"]
        if not re.fullmatch(r"[a-z0-9_]+", identity) or identity in identities:
            raise ValueError("Invalid or repeated stage identity")
        identities.add(identity)
        profile = stage.get("task_profile", "navigation_pickup")
        manifest = read_json(inside(stage["route_manifest"], root / "data"))
        if manifest["group"] != group or not 1 <= stage["route_count"] == len(manifest["routes"]) <= 2:
            raise ValueError("Native route count or group mismatch")
        if manifest.get("task_profile", profile) != profile:
            raise ValueError("Native route profile mismatch")
        expected = native_group(group, [r["name"] for r in manifest["routes"]], profile)
        if stage.get("group_semantic_sha256") != semantic_hash(expected):
            raise ValueError("Prepared native execution contract mismatch")
        actual = read_json(inside(source_bgi / "User/ScriptGroup" / (group + ".json"), source_bgi / "User"))
        if semantic_hash(actual) != semantic_hash(expected):
            raise ValueError("Native execution configuration changed")
        assets, fight_waypoints = [], 0
        for route in manifest["routes"]:
            asset = inside(source_bgi / "User/AutoPathing/StellariaSynaeris" / group / route["name"], source_bgi / "User")
            raw = asset.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest != route["sha256"] or digest in seen:
                raise ValueError("Changed or duplicate route asset")
            contract = route_contract(json.loads(raw.decode("utf-8-sig")), profile)
            if contract is None:
                raise ValueError("Unqualified route actions require a worker update")
            fight_waypoints += contract["fight_waypoints"]
            seen.add(digest)
            assets.append({"name": route["name"], "sha256": digest, "bytes": raw})
        required = stage.get("required_tool_completions", {"Pathing": stage["route_count"]})
        if (not isinstance(required, dict) or set(required) - {"Pathing", "AutoFight"}
                or any(not isinstance(value, int) or value < 0 for value in required.values())
                or required.get("Pathing") != stage["route_count"]):
            raise ValueError("Invalid native tool completion contract")
        if profile == "combat_pathing" and required.get("AutoFight") != fight_waypoints:
            raise ValueError("Combat completion contract does not match route actions")
        if profile == "navigation_pickup" and required.get("AutoFight", 0):
            raise ValueError("Navigation profile cannot require combat")
        strategy = stage.get("combat_strategy")
        if profile == "combat_pathing":
            expected_name = COMBAT_STRATEGY + ".txt"
            if (not isinstance(strategy, dict) or set(strategy) != {"name", "sha256"}
                    or strategy["name"] != expected_name
                    or not re.fullmatch(r"[0-9a-f]{64}", strategy["sha256"])):
                raise ValueError("Invalid pinned combat strategy contract")
            for bgi_root in (source_bgi, Path(policy["bgi"])):
                strategy_file = inside(bgi_root / "User/AutoFight" / expected_name, bgi_root / "User")
                if sha256(strategy_file) != strategy["sha256"]:
                    raise ValueError("Pinned combat strategy changed")
        elif strategy is not None:
            raise ValueError("Navigation profile cannot declare a combat strategy")
        config = Config.load(inside(campaign_file.parent / (identity + ".collector.json"), root / "data"))
        output = inside(config.output, root / "data")
        if (output != (root / "data/episodes").resolve() or config.actor != "BGI"
                or config.purpose != "gameplay" or config.capture in {"synthetic", "wgc"} or config.capture_background or config.ffmpeg
                or config.window_title != "原神" or config.quest != group or config.bridge_port != 18765
                or config.fps != 30 or config.ocr_threads > 2):
            raise ValueError("Unsupported gameplay collector configuration")
        config.bgi_dir = policy["bgi"]
        queue.append({"stage": stage, "config": config, "group": canonical_group(expected),
                      "assets": assets, "profile": profile, "required": required,
                      "strategy": strategy})
    return campaign_file, queue


def close_owned_bgi(process, executable):
    if process is None or process.poll() is not None:
        return
    if Path(psutil.Process(process.pid).exe()).resolve() != Path(executable).resolve():
        raise RuntimeError("BGI ownership changed; process preserved")
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    @callback_type
    def callback(hwnd, _):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == process.pid:
            user.PostMessageW(hwnd, 0x10, 0, 0)
        return True
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows(callback, 0)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Owned BGI did not close normally; no force termination")


def attempted_hashes(episode, assets):
    attempted = {row["payload"].get("route") for row in iter_rows(episode, "tools")
                 if row["source"] == "bettergi_native" and row["kind"] == "TOOL_START"
                 and row["payload"].get("tool") == "Pathing"}
    return {a["sha256"] for a in assets if a["name"] in attempted}


def execute(policy, runtime):
    root, runtime = Path(policy["workspace"]).resolve(), Path(runtime).resolve()
    reports = inside(root / "reports/local/campaign", root)
    reports.mkdir(parents=True, exist_ok=True)
    request = read_json(inside(root / "reports/local/machine_worker/request.json", root))
    campaign_file, queue = validate_request(policy, request)
    history_file = runtime / "machine-history.json"
    history = read_json(history_file) if history_file.exists() else {"requests": [], "attempted_hashes": []}
    if request["request_id"] in history["requests"]:
        raise ValueError("This request has already been dispatched; no automatic replay")
    blocked = set(history["attempted_hashes"]) | set(policy.get("previously_attempted_hashes", []))
    if any(a["sha256"] in blocked for job in queue for a in job["assets"]):
        raise ValueError("Previously attempted routes require deliberate new collection design")
    executable = Path(policy["bgi"]) / "BetterGI.exe"
    if any(p.info["name"].lower() == "bettergi.exe" for p in psutil.process_iter(["name"]) if p.info["name"]):
        raise RuntimeError("Existing BGI preserved; close it before dispatch")
    for relative, digest in policy["bgi_binary_hashes"].items():
        if sha256(inside(Path(policy["bgi"]) / relative, policy["bgi"])) != digest:
            raise ValueError("Installed BGI executable changed")
    deadline = time.monotonic() + 7200
    stop = inside(reports / "stop.request", root)
    if stop.exists():
        raise RuntimeError("Preserved stop request is present")
    report_file = reports / (uuid.uuid4().hex + ".json")
    state = {"schema_version": 4, "status": "starting", "pid": os.getpid(),
        "campaign_path": str(campaign_file), "campaign_sha256": sha256(campaign_file),
        "request_id": request["request_id"], "stages": [], "verified_success": False,
        "deadline_utc": datetime.fromtimestamp(time.time() + 7200, timezone.utc).isoformat(),
        "stop_file": str(stop), "training_hours_verified": 0, "worker": "installed_task"}
    def save():
        state["updated_utc"] = datetime.now(timezone.utc).isoformat()
        atomic_json(report_file, state)
        atomic_json(reports / "latest.json", {"report": str(report_file), "pid": os.getpid(), "stop_file": str(stop)})
    history["requests"].append(request["request_id"])
    atomic_json(history_file, history)
    save()
    collector = process = thread = job = None
    try:
        for job in queue:
            if stop.exists() or time.monotonic() >= deadline:
                state["status"] = "bounded_or_requested_stop"
                break
            stage, config = job["stage"], job["config"]
            record = {"stage": stage["id"], "status": "preflight", "route_count": stage["route_count"],
                      "task_profile": job["profile"], "required_tool_completions": job["required"],
                      "episode_id": None, "verified_success": False}
            state["stages"].append(record)
            state["status"] = "preflight"
            save()
            window = GameWindow(config.window_title)
            window.restore_for_collection()
            ready_limit = min(deadline, time.monotonic() + 600)
            while True:
                rect = window.rect()
                if window.active() and rect[2] - rect[0] >= 640 and rect[3] - rect[1] >= 360:
                    break
                if stop.exists() or time.monotonic() >= ready_limit:
                    raise RuntimeError("Focused game preflight ended; no fabricated episode")
                time.sleep(.25)
            destination = inside(Path(policy["bgi"]) / "User/AutoPathing/StellariaSynaeris" / stage["group"], policy["bgi"])
            destination.mkdir(parents=True, exist_ok=True)
            for asset in job["assets"]:
                (destination / asset["name"]).write_bytes(asset["bytes"])
            group_file = Path(policy["bgi"]) / "User/ScriptGroup" / (stage["group"] + ".json")
            group_file.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(group_file, job["group"])
            atomic_json(Path(policy["bgi"]) / "synaeris.telemetry.json", {"url": "http://127.0.0.1:18765"})
            collector = Collector(config)
            result = {}
            def record_episode():
                try:
                    result["episode"] = collector.run(min(600, max(1, deadline - time.monotonic())), stop)
                except Exception as error:
                    result["error"] = str(error)
            thread = threading.Thread(target=record_episode, name="machine-episode")
            thread.start()
            limit = time.monotonic() + 30
            while not collector.frames:
                if not thread.is_alive() or stop.exists() or time.monotonic() >= min(limit, deadline):
                    raise RuntimeError("Recorder did not become ready: " + str(result))
                time.sleep(.1)
            process = subprocess.Popen([str(executable), "--startGroups", stage["group"]],
                cwd=str(executable.parent), env=dict(os.environ, SYNAERIS_TELEMETRY_URL="http://127.0.0.1:18765"),
                **hidden_process_options())
            record["episode_id"] = collector.writer.episode_id
            record["status"] = state["status"] = "recording"
            save()
            returned_at = None
            while thread.is_alive() and not stop.exists() and time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("BGI exited during recording")
                with collector.bridge.lock:
                    completed = dict(collector.bridge.completed_tools)
                    failed = dict(collector.bridge.failed_tools)
                    returned = (all(completed.get(tool, 0) >= count for tool, count in job["required"].items())
                                and not collector.bridge.active_tools)
                record["observed_tool_completions"] = completed
                record["observed_tool_failures"] = failed
                failed_required = {tool: failed.get(tool, 0) for tool in job["required"] if failed.get(tool, 0)}
                if failed_required:
                    record["status"] = "required_tool_failed_preserved"
                    raise RuntimeError("Required native tool failed; episode preserved: " + str(failed_required))
                returned_at = (returned_at or time.monotonic()) if returned else None
                if returned_at is not None and time.monotonic() - returned_at >= 8:
                    break
                time.sleep(.5)
            collector.stop()
            thread.join(timeout=30)
            if thread.is_alive():
                raise RuntimeError("Episode still finalizing; no force termination")
            close_owned_bgi(process, executable)
            process = None
            if "error" in result or "episode" not in result:
                raise RuntimeError("Episode failed: " + str(result))
            episode = result["episode"]
            record["episode_status"] = read_json(episode / "manifest.json")["status"]
            history["attempted_hashes"] = sorted(set(history["attempted_hashes"]) | attempted_hashes(episode, job["assets"]))
            atomic_json(history_file, history)
            record["status"] = state["status"] = "qualifying"
            save()
            quality = qualify(episode)
            record["qualification_log"] = str(episode / "machine_qualification.json")
            if not quality["continuation_allowed"]:
                record["status"] = "technical_qualification_failed"
                raise RuntimeError("Live quality gate stopped collection: " + str(quality["problems"]))
            if returned_at is None:
                record["status"] = "routes_incomplete_preserved"
                raise RuntimeError("Routes incomplete; no automatic replay")
            record["status"] = "technical_qualified_review_pending"
            save()
        else:
            state["status"] = "queue_finished_review_pending"
    except Exception as error:
        state["status"] = "failed_preserved"
        state["error"] = str(error)
        raise
    finally:
        if collector:
            collector.stop()
        if thread:
            thread.join(timeout=30)
        if process:
            try:
                close_owned_bgi(process, executable)
            except Exception as error:
                state["cleanup_error"] = str(error)
        if collector and collector.writer and collector.writer.closed and job:
            history["attempted_hashes"] = sorted(set(history["attempted_hashes"]) | attempted_hashes(collector.writer.path, job["assets"]))
            atomic_json(history_file, history)
        save()
    return state


def installed_main(probe=False):
    if not getattr(sys, "frozen", False):
        raise RuntimeError("The installed worker requires its protected packaged executable")
    runtime = Path(sys.executable).resolve().parent
    policy = read_json(runtime / "machine-policy.json")
    root = Path(policy["workspace"]).absolute()
    if root.resolve() != root:
        raise ValueError("Approved workspace changed through a link")
    diagnostics = inside(root / "reports/local/machine_worker", root)
    diagnostics.mkdir(parents=True, exist_ok=True)
    if process_elevated(os.getpid()) is not True:
        raise RuntimeError("Use the ordinarily authorized scheduled task")
    if probe:
        atomic_json(diagnostics / "probe.json", {"passed": True, "elevated": True,
            "pid": os.getpid(), "runtime": str(runtime), "real_gameplay": False,
            "utc": datetime.now(timezone.utc).isoformat()})
        return
    try:
        execute(policy, runtime)
    except Exception as error:
        atomic_json(diagnostics / "last_error.json", {"error": str(error), "pid": os.getpid(),
            "utc": datetime.now(timezone.utc).isoformat()})
        raise
