import importlib.util
import json
from pathlib import Path

import pytest

from synaeris_collector.config import Config
from synaeris_collector.storage import EpisodeWriter


def load_qualifier():
    spec = importlib.util.spec_from_file_location("machine_qualifier", Path(__file__).parents[1] / "tools" / "qualify_live_episode.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("accepted,drops,allowed", [(True, 0, True), (False, 0, False), (True, 2, False)])
def test_machine_continuation_needs_accepted_native_input_without_loss(tmp_path, monkeypatch, accepted, drops, allowed):
    module = load_qualifier()
    writer = EpisodeWriter(Config(output=str(tmp_path), actor="BGI"))
    writer.manifest["capabilities"] = {"input_permission_qualified": True}
    writer.emit("input", "BGI_INPUT_DISPATCH", {"dispatch_accepted": accepted,
        "producer_telemetry_drops": drops}, source="bettergi_native", actor="BGI")
    writer.emit("ui", "UI_STATE", {"mode": "Main"}, source="bettergi_native", actor="BGI")
    writer.emit("frames", "VIDEO_FRAME", {"game_focused": True})
    writer.close()
    monkeypatch.setattr(module, "inspect_episode", lambda _: {"issues": [], "decoded_frames": 1,
        "video_decode_backend": "mock_for_test"})
    report = module.qualify(writer.path)
    assert report["continuation_allowed"] == allowed
    assert report["native_main_ui_events"] == 1
    assert not report["training_eligible"] and report["review_required"]
    assert json.loads((writer.path / "machine_qualification.json").read_text())["continuation_allowed"] == allowed


def test_machine_continuation_does_not_accept_unknown_injected_hooks(tmp_path, monkeypatch):
    module = load_qualifier()
    writer = EpisodeWriter(Config(output=str(tmp_path), actor="BGI"))
    writer.manifest["capabilities"] = {"input_permission_qualified": True}
    writer.emit("input", "KEY_DOWN", {}, source="win32_hook", actor="UNKNOWN_INJECTED")
    writer.close()
    monkeypatch.setattr(module, "inspect_episode", lambda _: {"issues": [], "decoded_frames": 30,
        "video_decode_backend": "mock_for_test"})
    report = module.qualify(writer.path)
    assert "no_native_accepted_input_dispatch" in report["problems"]
    assert not report["continuation_allowed"]


def test_drop_gate_checks_all_native_streams_and_latest_source_diagnostics(tmp_path, monkeypatch):
    module = load_qualifier()
    writer = EpisodeWriter(Config(output=str(tmp_path), actor="BGI"))
    writer.manifest["capabilities"] = {"input_permission_qualified": True}
    writer.emit("input", "BGI_INPUT_DISPATCH", {"dispatch_accepted": True,
        "producer_diagnostics": {"transport_failures": 0}}, source="bettergi_native", timestamp=0)
    writer.emit("ui", "UI_STATE", {"mode": "Main"}, source="bettergi_native", timestamp=1)
    writer.emit("frames", "VIDEO_FRAME", {"game_focused": True}, timestamp=2)
    writer.emit("ocr", "OCR_TEXT", {"producer_telemetry_drops": 3,
        "producer_diagnostics": {"transport_failures": 3}}, source="bettergi_native", timestamp=3)
    writer.close()
    monkeypatch.setattr(module, "inspect_episode", lambda _: {"issues": [], "decoded_frames": 1,
        "video_decode_backend": "mock_for_test"})
    report = module.qualify(writer.path)
    assert not report["continuation_allowed"] and report["producer_drops"] == 3
    assert report["producer_diagnostics"]["transport_failures"] == 3


def test_machine_continuation_preserves_black_unfocused_recording(tmp_path, monkeypatch):
    module = load_qualifier()
    writer = EpisodeWriter(Config(output=str(tmp_path), actor="BGI"))
    writer.manifest["capabilities"] = {"input_permission_qualified": True}
    writer.emit("input", "BGI_INPUT_DISPATCH", {"dispatch_accepted": True}, source="bettergi_native", actor="BGI")
    writer.emit("ui", "UI_STATE", {"mode": "Main"}, source="bettergi_native", actor="BGI")
    writer.emit("frames", "VIDEO_FRAME", {"game_focused": False})
    writer.close()
    monkeypatch.setattr(module, "inspect_episode", lambda _: {"issues": [], "decoded_frames": 1,
        "video_decode_backend": "mock_for_test"})
    report = module.qualify(writer.path)
    assert "game_focused_frame_fraction_below_0.8" in report["problems"]
    assert report["game_focused_frame_fraction"] == 0
    assert not report["continuation_allowed"]


@pytest.mark.parametrize("qualified,game_elevated", [(False, True), (False, None), (True, None)])
def test_game_restore_preserves_permission_precondition(qualified, game_elevated):
    from synaeris_collector.capture import GameWindow
    window = object.__new__(GameWindow)
    window.permissions = lambda: {"input_permission_qualified": qualified, "game_elevated": game_elevated}
    # No user32 API exists in this fixture: an attempt would raise immediately.
    assert window.restore_for_collection() is False


@pytest.mark.parametrize("metadata_name,reason", [("review.json", "review_quarantined"),
    ("machine_qualification.json", "machine_review_pending")])
def test_pending_or_quarantined_machine_data_never_enters_default_export(tmp_path, monkeypatch, metadata_name, reason):
    from synaeris_collector import dataset
    writer = EpisodeWriter(Config(output=str(tmp_path), actor="BGI"))
    writer.close()
    (writer.path / metadata_name).write_text(json.dumps({"training_eligible": False}), encoding="utf-8")
    def forbidden(_):
        raise AssertionError("Explicit exclusion must be checked before expensive decoding")
    monkeypatch.setattr(dataset, "inspect_episode", forbidden)
    export = dataset.build_dataset(tmp_path, tmp_path / "export.json")
    assert export["excluded"][0]["reason"] == reason
    assert not any(export["splits"].values())
