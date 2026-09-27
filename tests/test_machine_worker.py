from copy import deepcopy
from pathlib import Path
import threading
from types import SimpleNamespace
import uuid

import pytest
from synaeris_collector import machine_worker as worker
from synaeris_collector.config import Config
from synaeris_collector.native_groups import (COMBAT_STRATEGY, canonical_group, food_group,
                                    native_group, route_contract, semantic_hash)
from synaeris_collector.storage import EpisodeWriter, atomic_json


def prepared(tmp_path):
    folder = tmp_path / "data/queue"
    folder.mkdir(parents=True)
    source = tmp_path / "source"
    group = "Synaeris_test"
    assets = source / "User/AutoPathing/StellariaSynaeris" / group
    assets.mkdir(parents=True)
    atomic_json(assets / "r.json", {"info": {"type": "collect"},
        "positions": [{"type": "teleport", "x": 1, "y": 2}, {"type": "target", "x": 3, "y": 4}]})
    groups = source / "User/ScriptGroup"
    groups.mkdir()
    declared = food_group(group, ["r.json"])
    atomic_json(groups / (group + ".json"), declared)
    atomic_json(folder / "routes.json", {"group": group, "routes": [{"name": "r.json", "sha256": worker.sha256(assets / "r.json")}]})
    Config(actor="BGI", quest=group, output=str(tmp_path / "data/episodes")).save(folder / "one.collector.json")
    atomic_json(folder / "campaign.json", {"stages": [{"id": "one", "group": group,
        "route_count": 1, "route_manifest": str(folder / "routes.json"),
        "group_semantic_sha256": semantic_hash(declared)}]})
    policy = {"workspace": str(tmp_path), "source_bgi": str(source), "bgi": str(tmp_path / "installed/BetterGI"), "bgi_binary_hashes": {}}
    request = {"schema_version": 1, "request_id": uuid.uuid4().hex,
        "campaign_relative": "data/queue/campaign.json", "campaign_sha256": worker.sha256(folder / "campaign.json"), "requested_utc": "fixture"}
    return policy, request, folder


def prepared_combat(tmp_path):
    policy, request, folder = prepared(tmp_path)
    group = "Synaeris_test"
    route = Path(policy["source_bgi"]) / "User/AutoPathing/StellariaSynaeris/Synaeris_test/r.json"
    atomic_json(route, {"info": {"type": "collect"}, "positions": [
        {"type": "teleport", "x": 1, "y": 2, "move_mode": "walk"},
        {"type": "path", "x": 3, "y": 4, "move_mode": "dash", "action": "fight"}]})
    declared = native_group(group, ["r.json"], "combat_pathing")
    atomic_json(Path(policy["source_bgi"]) / "User/ScriptGroup/Synaeris_test.json", declared)
    digest = worker.sha256(route)
    atomic_json(folder / "routes.json", {"group": group, "task_profile": "combat_pathing",
        "routes": [{"name": "r.json", "sha256": digest}]})
    strategy_name = COMBAT_STRATEGY + ".txt"
    for base in (Path(policy["source_bgi"]), Path(policy["bgi"])):
        target = base / "User/AutoFight" / strategy_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture strategy", encoding="utf-8")
    strategy_hash = worker.sha256(Path(policy["source_bgi"]) / "User/AutoFight" / strategy_name)
    atomic_json(folder / "campaign.json", {"stages": [{"id": "one", "group": group,
        "task_profile": "combat_pathing", "route_count": 1,
        "route_manifest": str(folder / "routes.json"),
        "group_semantic_sha256": semantic_hash(declared),
        "required_tool_completions": {"Pathing": 1, "AutoFight": 1},
        "combat_strategy": {"name": strategy_name, "sha256": strategy_hash}}]})
    request["campaign_sha256"] = worker.sha256(folder / "campaign.json")
    return policy, request, folder


def test_native_bgi_default_fields_and_display_index_are_benign():
    prepared = food_group("Synaeris_test", ["r.json"])
    serialized = canonical_group(prepared)
    serialized["index"] = 17
    assert semantic_hash(serialized) == semantic_hash(prepared)


@pytest.mark.parametrize("field,value", [("type", "Javascript"), ("runNum", 2), ("schedule", "Weekly"), ("folderName", "elsewhere")])
def test_native_execution_changes_are_not_benign(field, value):
    prepared = food_group("Synaeris_test", ["r.json"])
    changed = deepcopy(prepared)
    changed["projects"][0][field] = value
    assert semantic_hash(changed) != semantic_hash(prepared)


def test_prepared_native_request_is_accepted_after_bgi_reserialization(tmp_path):
    policy, request, folder = prepared(tmp_path)
    group_file = Path(policy["source_bgi"]) / "User/ScriptGroup/Synaeris_test.json"
    serialized = canonical_group(worker.read_json(group_file))
    serialized["index"] = 3
    atomic_json(group_file, serialized)
    _, queue = worker.validate_request(policy, request)
    assert len(queue) == 1 and queue[0]["config"].bgi_dir == policy["bgi"]


def test_typed_combat_route_and_pinned_strategy_are_accepted(tmp_path):
    policy, request, _ = prepared_combat(tmp_path)
    _, queue = worker.validate_request(policy, request)
    assert queue[0]["profile"] == "combat_pathing"
    assert queue[0]["required"] == {"Pathing": 1, "AutoFight": 1}
    assert queue[0]["group"]["config"]["pathingConfig"]["autoFightEnabled"] is True


def test_combat_profile_rejects_command_language_and_changed_strategy(tmp_path):
    policy, request, folder = prepared_combat(tmp_path)
    route = Path(policy["source_bgi"]) / "User/AutoPathing/StellariaSynaeris/Synaeris_test/r.json"
    data = worker.read_json(route)
    data["positions"][1].update(action="combat_script", action_params="keypress(F)")
    assert route_contract(data, "combat_pathing") is None
    installed = Path(policy["bgi"]) / "User/AutoFight" / (COMBAT_STRATEGY + ".txt")
    installed.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="strategy changed"):
        worker.validate_request(policy, request)


@pytest.mark.parametrize("field,value", [("ffmpeg", "arbitrary.exe"), ("capture", "synthetic"), ("actor", "HUMAN"), ("purpose", "calibration")])
def test_worker_does_not_accept_arbitrary_programs_or_incomplete_action_capture(tmp_path, field, value):
    policy, request, folder = prepared(tmp_path)
    config = worker.read_json(folder / "one.collector.json")
    config[field] = value
    atomic_json(folder / "one.collector.json", config)
    with pytest.raises(ValueError, match="Unsupported gameplay"):
        worker.validate_request(policy, request)


def test_worker_rejects_shell_requests_traversal_and_changed_route_bytes(tmp_path):
    policy, request, folder = prepared(tmp_path)
    with pytest.raises(ValueError, match="typed collection"):
        worker.validate_request(policy, {**request, "command": "anything"})
    with pytest.raises(ValueError, match="local prepared"):
        worker.validate_request(policy, {**request, "campaign_relative": "data/../../other/campaign.json"})
    with pytest.raises(ValueError, match="escapes"):
        worker.inside(tmp_path / "outside", tmp_path / "data")
    route = Path(policy["source_bgi"]) / "User/AutoPathing/StellariaSynaeris/Synaeris_test/r.json"
    route.write_text(route.read_text() + " ")
    with pytest.raises(ValueError, match="Changed or duplicate"):
        worker.validate_request(policy, request)


def test_shell_and_combat_settings_remain_checked(tmp_path):
    policy, request, folder = prepared(tmp_path)
    group_file = Path(policy["source_bgi"]) / "User/ScriptGroup/Synaeris_test.json"
    declared = worker.read_json(group_file)
    declared["config"]["enableShellConfig"] = True
    atomic_json(group_file, declared)
    with pytest.raises(ValueError, match="execution configuration changed"):
        worker.validate_request(policy, request)


def test_new_request_preserves_active_campaign_and_existing_request(tmp_path, monkeypatch):
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location("request_machine_test", Path(__file__).resolve().parents[1] / "tools/request_machine_collection.py")
    tool = module_from_spec(spec)
    spec.loader.exec_module(tool)
    monkeypatch.setattr(tool, "ROOT", tmp_path)
    monkeypatch.setattr(worker.psutil, "pid_exists", lambda _: True)
    reports = tmp_path / "reports/local/campaign"
    reports.mkdir(parents=True)
    atomic_json(reports / "running.json", {"status": "recording", "pid": 123})
    atomic_json(reports / "latest.json", {"report": str(reports / "running.json")})
    request_file = tmp_path / "reports/local/machine_worker/request.json"
    request_file.parent.mkdir()
    atomic_json(request_file, {"preserve": True})
    before = request_file.read_bytes()
    with pytest.raises(RuntimeError, match="Active collection preserved"):
        tool.request(tmp_path / "data/new/campaign.json")
    assert request_file.read_bytes() == before


def test_failed_quality_preserves_episode_stops_job_and_records_attempt(tmp_path, monkeypatch):
    policy, request, folder = prepared(tmp_path)
    runtime = tmp_path / "installed"
    runtime.mkdir()
    request_path = tmp_path / "reports/local/machine_worker"
    request_path.mkdir(parents=True)
    atomic_json(request_path / "request.json", request)
    launches = []
    class FakeCollector:
        def __init__(self, config):
            self.writer = None
            self.frames = 0
            self.stop_event = threading.Event()
            self.bridge = SimpleNamespace(lock=threading.Lock(), completed_pathing_routes=1,
                completed_tools={"Pathing": 1}, failed_tools={}, active_tools=set())
        def run(self, duration, stop):
            self.writer = EpisodeWriter(Config(output=str(tmp_path / "data/episodes"), capture="synthetic"))
            self.writer.emit("tools", "TOOL_START", {"tool": "Pathing", "route": "r.json"}, source="bettergi_native")
            self.frames = 1
            self.stop_event.wait(2)
            self.writer.close()
            return self.writer.path
        def stop(self):
            self.stop_event.set()
    class Clock:
        value = 0
        def monotonic(self):
            self.value += 1
            return self.value
        def time(self):
            return 1_000_000
        def sleep(self, _):
            threading.Event().wait(.001)
    monkeypatch.setattr(worker, "time", Clock())
    monkeypatch.setattr(worker, "Collector", FakeCollector)
    monkeypatch.setattr(worker, "GameWindow", lambda *_: SimpleNamespace(active=lambda: True,
        rect=lambda: (0, 0, 640, 360), restore_for_collection=lambda: True))
    monkeypatch.setattr(worker.psutil, "process_iter", lambda *_: [])
    monkeypatch.setattr(worker.subprocess, "Popen", lambda args, **_: launches.append(args) or SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(worker, "close_owned_bgi", lambda *_: None)
    # Real qualification rejects the synthetic fixture, while exercising the
    # production stop/ledger/finalization path without sending any game input.
    real_qualify = worker.qualify
    monkeypatch.setattr(worker, "qualify", lambda path: real_qualify(path, inspect=lambda _: {
        "issues": [], "decoded_frames": 0, "video_decode_backend": "fixture"}))
    with pytest.raises(RuntimeError, match="quality gate"):
        worker.execute(policy, runtime)
    assert len(launches) == 1 and launches[0][1:] == ["--startGroups", "Synaeris_test"]
    latest = worker.read_json(tmp_path / "reports/local/campaign/latest.json")
    report = worker.read_json(latest["report"])
    assert report["status"] == "failed_preserved" and report["training_hours_verified"] == 0
    assert report["stages"][0]["episode_status"] == "complete"
    history = worker.read_json(runtime / "machine-history.json")
    assert len(history["attempted_hashes"]) == 1
    with pytest.raises(ValueError, match="already been dispatched"):
        worker.execute(policy, runtime)
