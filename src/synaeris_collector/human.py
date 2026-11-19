"""Portable human defaults and a reproducible sampling policy, separate from labels."""
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import socket
import time
from .config import CAPTURE_STREAM_FIELDS, Config

PROFILE_ID = 'human-standard-v4'
SCENARIOS = ['自由采集', '首次探索', '解谜与机关', '剧情与任务', '战斗与拾取', '错误与恢复', '熟练操作']
SAMPLING_KEYS = ('purpose', 'actor', 'capture', 'capture_background', 'codec', 'fps',
                 'observation_hz', 'perf_hz', 'ocr_threads', 'ocr_max_hz',
                 'window_title', 'bridge_port', 'ffmpeg', 'rois')
POLICY_KEYS = (*SAMPLING_KEYS, *CAPTURE_STREAM_FIELDS.values())
LEGACY_POLICY_KEYS = (*SAMPLING_KEYS, 'ocr')

def user_directory():
    return Path(os.environ.get('LOCALAPPDATA') or Path.home() / '.local/share') / 'Stellaria Synaeris'

def recommended_config(config=None):
    defaults = Config(capture='wgc', ocr_max_hz=4.0)
    values = asdict(defaults)
    return replace(config or defaults, **{key: values[key] for key in SAMPLING_KEYS}).validate()

def legacy_v2_policy():
    legacy = recommended_config()
    rois = {key: list(value) for key, value in legacy.rois.items()}
    rois['dialogue'] = [0.10, 0.68, 0.91, 0.94]
    values = asdict(replace(legacy, rois=rois))
    return {key: values[key] for key in LEGACY_POLICY_KEYS}

def profile_descriptor(config):
    values = asdict(config)
    policy = {key: values[key] for key in POLICY_KEYS}
    recommended = asdict(recommended_config())
    standard = policy == {key: recommended[key] for key in POLICY_KEYS}
    sampling_standard = all(values[key] == recommended[key] for key in SAMPLING_KEYS)
    fingerprint = {'version': PROFILE_ID, 'policy': policy}
    digest = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, ensure_ascii=False,
                                      separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return {'id': PROFILE_ID if standard else 'custom', 'standard': standard,
            'sampling_standard': sampling_standard, 'policy_version': PROFILE_ID,
            'policy_sha256': digest, 'policy': policy,
            'scope': 'Requested sampling policy. Actual codec/backend/geometry are recorded separately; labels are declarations.'}

def load_preferences():
    default = recommended_config(Config(output=str(user_directory() / 'data/episodes'),
                                        scenario=SCENARIOS[0])).video_only()
    path = user_directory() / 'collector-preferences.json'
    if not path.exists():
        return default, ''
    try:
        fields = json.loads(path.read_text(encoding='utf-8-sig'))
        saved = Config(**fields).validate()
        values = asdict(saved)
        notes = []
        if {key: values[key] for key in LEGACY_POLICY_KEYS} == legacy_v2_policy():
            saved = recommended_config(saved)
            notes.append('已更新旧版 OCR 裁剪参数，保留原采集流选择')
        if any(name not in fields for name in CAPTURE_STREAM_FIELDS.values()):
            notes.append('旧配置的缺省采集流沿用原行为，请在开始前核对采集流')
        return saved, '；'.join(notes)
    except (OSError, ValueError, TypeError) as error:
        return default, '旧配置未能读取，使用 Video-only；原文件已保留：' + str(error)

def save_preferences(config):
    from .storage import atomic_json
    target = user_directory()
    target.mkdir(parents=True, exist_ok=True)
    atomic_json(target / 'collector-preferences.json', asdict(config))

def ensure_port_available(port):
    with socket.socket() as connection:
        connection.settimeout(.5)
        if connection.connect_ex(('127.0.0.1', port)) == 0:
            raise RuntimeError('已有采集器正在运行，请先停止机器批次或其他录像，再开始人工采集')

def wait_for_human_game(config, stopped, notify, timeout=120, window_factory=None):
    if window_factory is None:
        from .capture import GameWindow
        window_factory = GameWindow
    deadline, restore_attempted = time.monotonic() + timeout, False
    notify('正在检查原神，恢复后将自动开始；可回到游戏，停止可取消等待')
    while not stopped.is_set():
        try:
            window = window_factory(config.window_title)
        except RuntimeError:
            window = None
        if window is not None:
            if config.capture_input and not window.permissions()['input_permission_qualified']:
                raise RuntimeError('游戏输入权限不足；请双击 SynaerisCollector.exe 并完成 Windows 确认')
            if config.capture_input and not restore_attempted:
                restore_attempted = True
                window.restore_for_collection()
            rect = window.rect()
            if window.active() and rect[2]-rect[0] >= 640 and rect[3]-rect[1] >= 360:
                return True
        if time.monotonic() >= deadline:
            raise RuntimeError('等待游戏超时，请进入原神后重试；未生成空录像')
        stopped.wait(.25)
    return False
