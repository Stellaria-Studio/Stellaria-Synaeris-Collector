from dataclasses import replace
import json
import threading
from types import SimpleNamespace
import pytest
from synaeris_collector.config import Config
from synaeris_collector.human import (PROFILE_ID, legacy_v2_policy, recommended_config,
                           profile_descriptor, load_preferences, save_preferences,
                           user_directory, wait_for_human_game)
from synaeris_collector.storage import EpisodeWriter

def test_policy_identity_ignores_labels_and_local_paths_but_detects_sampling_changes():
    baseline = recommended_config()
    descriptor = profile_descriptor(baseline)
    assert descriptor['standard']
    assert descriptor['policy_version'] == PROFILE_ID == 'human-standard-v3'
    labelled = replace(baseline, output='D:/data', region='纳塔', quest='first-task', scenario='解谜与机关', novel=False)
    assert profile_descriptor(labelled)['policy_sha256'] == descriptor['policy_sha256']
    changed = profile_descriptor(replace(baseline, fps=60))
    assert not changed['standard'] and changed['id'] == 'custom'
    assert changed['policy_sha256'] != descriptor['policy_sha256']
    rois = {**baseline.rois, 'receipt': [0.01, 0.4, 0.2, 0.6]}
    assert not profile_descriptor(replace(baseline, rois=rois))['standard']

def test_v3_shrinks_dialogue_hud_overlap_and_retains_v2_migration_identity():
    assert legacy_v2_policy()['rois']['dialogue'] == [0.10, 0.68, 0.91, 0.94]
    assert recommended_config().rois['dialogue'] == [0.10, 0.68, 0.91, 0.90]

def test_v2_standard_preferences_migrate_without_losing_session_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    current = recommended_config(Config(output='D:/kept', region='挪德卡莱', scenario='剧情与任务'))
    old_rois = {key: list(value) for key, value in current.rois.items()}
    old_rois['dialogue'] = [0.10, 0.68, 0.91, 0.94]
    path = user_directory() / 'collector-preferences.json'
    path.parent.mkdir(parents=True)
    replace(current, rois=old_rois).save(path)
    migrated, note = load_preferences()
    assert note and profile_descriptor(migrated)['standard']
    assert migrated.output == 'D:/kept' and migrated.region == '挪德卡莱'
    assert migrated.scenario == '剧情与任务'

def test_restore_profile_removes_background_and_custom_sampling_without_changing_group_labels():
    original = Config(capture='wgc', capture_background=True, actor='BGI', purpose='calibration',
                      fps=60, ocr=False, region='挪德卡莱', output='D:/collection')
    restored = recommended_config(original)
    assert restored.actor == 'HUMAN' and restored.purpose == 'gameplay'
    assert restored.fps == 30 and restored.ocr and restored.ocr_max_hz == 4
    assert restored.capture == 'wgc' and not restored.capture_background
    assert restored.region == original.region and restored.output == original.output

def test_portable_preferences_use_recipient_directory_and_preserve_invalid_file(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    config, warning = load_preferences()
    assert not warning and config.output == str(user_directory() / 'data/episodes')
    assert profile_descriptor(config)['standard']
    save_preferences(replace(config, region='蒙德'))
    assert load_preferences()[0].region == '蒙德'
    path = user_directory() / 'collector-preferences.json'
    path.write_text('{broken', encoding='utf-8')
    restored, warning = load_preferences()
    assert warning and profile_descriptor(restored)['standard']
    assert path.read_text(encoding='utf-8') == '{broken'

def test_manifest_records_actual_requested_policy(tmp_path):
    config = recommended_config(Config(output=str(tmp_path)))
    writer = EpisodeWriter(config)
    writer.close()
    manifest = json.loads((writer.path / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['collection_profile'] == profile_descriptor(config)

def test_wait_requires_input_permission_before_any_window_restore():
    class Unqualified:
        def permissions(self):
            return {'input_permission_qualified': False}
        def restore_for_collection(self):
            pytest.fail('A failed precondition must not restore the game')
    with pytest.raises(RuntimeError, match='权限不足'):
        wait_for_human_game(Config(), threading.Event(), lambda _: None, window_factory=lambda _: Unqualified())

def test_wait_cancel_or_timeout_never_claims_ready():
    stopped = threading.Event()
    stopped.set()
    assert not wait_for_human_game(Config(), stopped, lambda _: None)
    unfocused = SimpleNamespace(permissions=lambda: {'input_permission_qualified': True},
        restore_for_collection=lambda: False, rect=lambda: (0, 0, 2560, 1440), active=lambda: False)
    with pytest.raises(RuntimeError, match='未生成空录像'):
        wait_for_human_game(Config(), threading.Event(), lambda _: None, timeout=0, window_factory=lambda _: unfocused)
