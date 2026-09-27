import hashlib
import importlib.util
from pathlib import Path

import pytest

from synaeris_collector.config import Config
from synaeris_collector.storage import EpisodeWriter, atomic_json


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "tools" / (name + ".py"))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def fixture(tmp_path, returned=True):
    queue = tmp_path / "old"
    queue.mkdir()
    writer = EpisodeWriter(Config(output=str(tmp_path / "data/episodes"), quest="Synaeris_first"))
    if returned:
        writer.emit("tools", "TOOL_END", {"tool": "Pathing", "tool_id": "route_a", "verified": False},
                    source="bettergi_native", actor="BGI")
    writer.close()
    campaign = queue / "campaign.json"
    atomic_json(campaign, {"stages": [{"id": "first", "group": "Synaeris_first", "route_count": 1},
                                      {"id": "next", "group": "Synaeris_next", "route_count": 2}]})
    Config().save(queue / "next.collector.json")
    report = tmp_path / "report.json"
    atomic_json(report, {"campaign_sha256": hashlib.sha256(campaign.read_bytes()).hexdigest(),
        "stages": [{"stage": "first", "episode_id": writer.episode_id, "episode_status": "complete",
                    "status": "technical_qualification_failed"}]})
    return campaign, report


def test_resume_excludes_executed_probe_without_promoting_failed_data(tmp_path, monkeypatch):
    loaded = module("prepare_resumed_campaign")
    monkeypatch.setattr(loaded, "ROOT", tmp_path)
    campaign, report = fixture(tmp_path)
    result = loaded.prepare(campaign, report, tmp_path / "new")
    assert [s["id"] for s in result["stages"]] == ["next"]
    excluded = result["resumed_from"]["excluded_executed"][0]
    assert excluded["technical_status"] == "technical_qualification_failed"
    assert excluded["training_eligible"] is False
    assert (tmp_path / "new/next.collector.json").read_bytes() == (campaign.parent / "next.collector.json").read_bytes()


def test_incomplete_route_cannot_be_silently_skipped_or_replayed(tmp_path, monkeypatch):
    loaded = module("prepare_resumed_campaign")
    monkeypatch.setattr(loaded, "ROOT", tmp_path)
    campaign, report = fixture(tmp_path, returned=False)
    with pytest.raises(ValueError, match="did not return all routes"):
        loaded.prepare(campaign, report, tmp_path / "new")
    assert not (tmp_path / "new").exists()


def test_changed_campaign_cannot_use_a_previous_execution_report(tmp_path, monkeypatch):
    loaded = module("prepare_resumed_campaign")
    monkeypatch.setattr(loaded, "ROOT", tmp_path)
    campaign, report = fixture(tmp_path)
    campaign.write_text(campaign.read_text() + " ")
    with pytest.raises(ValueError, match="does not match"):
        loaded.prepare(campaign, report, tmp_path / "new")
