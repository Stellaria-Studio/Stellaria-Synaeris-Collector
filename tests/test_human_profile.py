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
    assert descriptor['policy_version'] == PROFILE_ID == 'human-standard-v4'
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
    legacy = {name: value for name, value in vars(replace(current, rois=old_rois)).items()
              if name not in {'capture_input', 'capture_telemetry', 'capture_performance'}}
    path.write_text(json.dumps(legacy), encoding='utf-8')
    migrated, note = load_preferences()
    assert note and profile_descriptor(migrated)['standard']
    assert migrated.output == 'D:/kept' and migrated.region == '挪德卡莱'
    assert migrated.scenario == '剧情与任务'
    assert migrated.capture_input and migrated.capture_telemetry and migrated.capture_performance
    assert json.loads(path.read_text(encoding='utf-8')) == legacy

def test_restore_profile_removes_background_and_custom_sampling_without_changing_group_labels():
    original = Config(capture='wgc', capture_background=True, actor='BGI', purpose='calibration',
                      fps=60, ocr=False, region='挪德卡莱', output='D:/collection')
    restored = recommended_config(original)
    assert restored.actor == 'HUMAN' and restored.purpose == 'gameplay'
    assert restored.fps == 30 and not restored.ocr and restored.ocr_max_hz == 4
    assert restored.capture == 'wgc' and not restored.capture_background
    assert restored.region == original.region and restored.output == original.output

def test_portable_preferences_use_recipient_directory_and_preserve_invalid_file(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    config, warning = load_preferences()
    assert not warning and config.output == str(user_directory() / 'data/episodes')
    assert profile_descriptor(config)['sampling_standard']
    assert config.capture_streams()['mode'] == 'video_only'
    save_preferences(replace(config, region='蒙德'))
    assert load_preferences()[0].region == '蒙德'
    path = user_directory() / 'collector-preferences.json'
    path.write_text('{broken', encoding='utf-8')
    restored, warning = load_preferences()
    assert warning and restored.capture_streams()['mode'] == 'video_only'
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

def test_mixed_stream_choices_survive_sampling_reset_and_preferences(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    original = Config(fps=60, capture_input=False, capture_telemetry=True, capture_performance=False, ocr=False)
    restored = recommended_config(original)
    assert restored.fps == 30
    assert (restored.capture_input, restored.capture_telemetry, restored.capture_performance, restored.ocr) == (
        False, True, False, False)
    assert profile_descriptor(restored)['sampling_standard'] and not profile_descriptor(restored)['standard']
    assert profile_descriptor(restored)['policy_sha256'] != profile_descriptor(recommended_config())['policy_sha256']
    save_preferences(restored)
    assert load_preferences() == (restored, '')


def test_video_only_wait_has_no_input_preflight_or_foreground_restore():
    def forbidden():
        pytest.fail("Video-only must not probe keyboard integrity or restore the window")

    window = SimpleNamespace(permissions=forbidden, restore_for_collection=forbidden,
                             rect=lambda: (0, 0, 2560, 1440), active=lambda: True)
    assert wait_for_human_game(Config().video_only(), threading.Event(), lambda _: None,
                               window_factory=lambda _: window)
