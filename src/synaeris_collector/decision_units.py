"""Compile variable-duration decisions from sealed gameplay evidence.

Version 1 deliberately leaves intent, expected effect, reward and game input
receipt unknown. Weak OCR or assistant hypotheses never become task truth.
"""
from bisect import bisect_left, bisect_right
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path

from .storage import iter_rows
from .storage import atomic_json


VERSION = 'synaeris.decision-unit.v1'
SECOND = 1_000_000_000
CONTROL = {'KEY_DOWN', 'KEY_UP', 'BUTTON_DOWN', 'BUTTON_UP'}
GOAL_LABELS = {'quest_text_candidate', 'quest_text_changed_candidate'}


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(part)
    return value.hexdigest()


def unknown(reason):
    return {'status': 'unknown', 'value': None, 'reason': reason,
        'training_weight': 0.0, 'evidence': []}


def candidate(value, source, reference, score=None):
    # Source scores in the existing auto-index are uncalibrated heuristics.
    return {'status': 'derived_candidate', 'value': value, 'source': source,
        'heuristic_score': score, 'training_weight': 0.0,
        'evidence': [reference]}


def reference(stream, row):
    return {'stream': stream, 'sequence': row['sequence'],
        'timestamp_ns': row['monotonic_timestamp'], 'source': row['source'],
        'actor': row['actor']}


def boundaries(duration, event_times, min_ns=500_000_000, max_ns=4*SECOND):
    """Coalesce nearby event cuts; split long quiet spans without fixed frames."""
    if duration <= 0 or min_ns <= 0 or max_ns < min_ns:
        raise ValueError('Invalid decision duration')
    cuts = [0]
    for stamp in sorted(set(int(t) for t in event_times if 0 < t < duration)):
        while stamp-cuts[-1] > max_ns:
            cuts.append(cuts[-1]+max_ns)
        if stamp-cuts[-1] >= min_ns:
            cuts.append(stamp)
    while duration-cuts[-1] > max_ns:
        cuts.append(cuts[-1]+max_ns)
    if cuts[-1] != duration:
        cuts.append(duration)
    return cuts


def _scene_at(segments, stamp):
    for segment in segments:
        if segment['start_ns'] <= stamp < segment['end_ns']:
            name = segment['scene']
            if name in {'unknown', 'unavailable'}:
                return unknown('Scene evidence unavailable')
            return candidate(name, segment.get('evidence_source', 'auto_index'),
                {'stream': 'auto_index', 'segment_id': segment['segment_id'],
                 'start_ns': segment['start_ns'], 'end_ns': segment['end_ns']},
                segment.get('score'))
    return unknown('No covering scene segment')


def _goal_at(labels, times, stamp):
    index = bisect_right(times, stamp)-1
    if index < 0 or stamp-times[index] > 30*SECOND:
        return unknown('No recent task text')
    row = labels[index]
    value = str(row.get('detail', {}).get('text') or '').strip()
    if len(value) < 4 or '\ufffd' in value:
        return unknown('Task OCR is too short or damaged')
    return candidate({'text': value, 'quest_id': None}, 'ocr_task_text',
        row.get('evidence') or {'stream': 'auto_index',
            'timestamp_ns': row['timestamp_ns']}, row.get('score'))


def _physical_input(rows):
    physical, raw = [], []
    held_keys, held_buttons = set(), set()
    for row in sorted(rows, key=lambda r: (r['monotonic_timestamp'], r['sequence'])):
        kind, payload = row['kind'], row['payload']
        if kind == 'INPUT_STATE_RESET':
            held_keys.clear(); held_buttons.clear()
            physical.append((row['monotonic_timestamp'], reference('input', row),
                {'kind': kind, 'held_keys': [], 'held_buttons': []}))
        elif kind in CONTROL and row['source'] == 'win32_low_level_hook' and (
                row['actor'] == 'HUMAN' and payload.get('injected') is False):
            value = payload.get('vk') if kind.startswith('KEY') else payload.get('button')
            target = held_keys if kind.startswith('KEY') else held_buttons
            if kind.endswith('DOWN'):
                target.add(value)
            else:
                target.discard(value)
            physical.append((row['monotonic_timestamp'], reference('input', row),
                {'kind': kind, 'value': value,
                 'held_keys': sorted(held_keys), 'held_buttons': sorted(held_buttons)}))
        elif kind == 'MOUSE_DELTA' and row['source'] == 'win32_raw_input' and not payload.get('absolute'):
            if row['actor'] != 'UNKNOWN' or payload.get('provenance_qualified') is not False:
                raise ValueError('Old raw mouse must not be relabeled as HUMAN')
            raw.append((row['monotonic_timestamp'], int(payload['dx']), int(payload['dy'])))
    return physical, raw


def _pose_at(rows, times, stamp):
    index = bisect_right(times, stamp)-1
    if index < 0 or stamp-times[index] > 2*SECOND:
        return unknown('No fresh valid localization')
    row = rows[index]
    payload = row['payload']
    if not all(isinstance(payload.get(key), (int, float)) and
            math.isfinite(payload[key]) for key in ('x', 'y')):
        return unknown('Localization coordinates invalid')
    return {'status': 'observed_sensor',
        'value': {key: payload.get(key) for key in ('x', 'y', 'yaw')},
        'source': row['source'], 'sensor_confidence': payload.get('confidence'),
        'training_weight': 0.0, 'evidence': [reference('pose', row)]}


def compile_episode(path, source_reference=None, min_ns=500_000_000,
        max_ns=4*SECOND):
    path = Path(path)
    manifest = json.loads((path/'manifest.json').read_text(encoding='utf-8-sig'))
    index = json.loads((path/'auto_index.json').read_text(encoding='utf-8-sig'))
    episode = manifest['episode_id']
    if (path.name != episode or index['episode_id'] != episode or
            manifest['status'] != 'complete' or manifest.get('synthetic') or
            manifest['config'].get('actor') != 'HUMAN'):
        raise ValueError(f'Expected sealed real HUMAN episode: {path}')
    source_hashes = {name: digest(path/file) for name, file in (
        ('manifest', 'manifest.json'), ('input', 'input.parquet'),
        ('frames', 'frames.parquet'), ('auto_index', 'auto_index.json'),
        ('video', 'visual.mkv'))}
    if source_reference is not None:
        if any(source_reference.get(key) != value for key, value in source_hashes.items()):
            raise ValueError(f'Sealed source differs from reference plan: {episode}')
    duration = int(manifest['duration_ns'])
    frames = list(iter_rows(path, 'frames'))
    frame_times = [row['monotonic_timestamp'] for row in frames]
    if not frames or any(a >= b for a, b in zip(frame_times, frame_times[1:])):
        raise ValueError(f'Invalid frame chronology: {episode}')
    if any(row['payload']['frame_index'] != i for i, row in enumerate(frames)):
        raise ValueError(f'Invalid frame ordinal mapping: {episode}')
    physical, raw = _physical_input(iter_rows(path, 'input'))
    physical_times = [item[0] for item in physical]
    raw_times = [item[0] for item in raw]
    goal_labels = sorted((row for row in index.get('labels', [])
        if row['label'] in GOAL_LABELS), key=lambda row: row['timestamp_ns'])
    goal_times = [row['timestamp_ns'] for row in goal_labels]
    poses = sorted((row for row in iter_rows(path, 'pose') if row['payload'].get('valid')),
        key=lambda row: (row['monotonic_timestamp'], row['sequence']))
    pose_times = [row['monotonic_timestamp'] for row in poses]
    tools = sorted(iter_rows(path, 'tools'),
        key=lambda row: (row['monotonic_timestamp'], row['sequence']))
    tool_times = [row['monotonic_timestamp'] for row in tools]
    signals = physical_times + [row['timestamp_ns'] for row in goal_labels]
    signals.extend(tool_times)
    for segment in index['segments']:
        signals.extend((segment['start_ns'], segment['end_ns']))
    cuts = boundaries(duration, signals, min_ns, max_ns)
    result = []
    for number, (start, end) in enumerate(zip(cuts, cuts[1:])):
        first, last = bisect_left(frame_times, start), bisect_left(frame_times, end)
        local_frames = frames[first:last]
        available = [row for row in local_frames if row['payload'].get('capture_available')
            and row['payload'].get('game_focused') and row['payload'].get('fresh')]
        before = bisect_right(frame_times, start)-1
        after = bisect_right(frame_times, end)-1
        p0, p1 = bisect_left(physical_times, start), bisect_left(physical_times, end)
        r0, r1 = bisect_left(raw_times, start), bisect_left(raw_times, end)
        controls = physical[p0:p1]
        raw_slice = raw[r0:r1]
        t0, t1 = bisect_left(tool_times, start), bisect_left(tool_times, end)
        previous_state = physical[p0-1][2] if p0 else {'held_keys': [], 'held_buttons': []}
        scene_before = _scene_at(index['segments'], start)
        scene_after = _scene_at(index['segments'], max(start, end-1))
        pose_before, pose_after = (_pose_at(poses, pose_times, stamp)
            for stamp in (start, end-1))
        effects = {}
        if (scene_before['status'] != 'unknown' and scene_after['status'] != 'unknown'
                and scene_before['value'] != scene_after['value']):
            effects['scene_change_candidate'] = {'from': scene_before['value'],
                'to': scene_after['value']}
        if pose_before['status'] == pose_after['status'] == 'observed_sensor':
            effects['pose_displacement_sensor'] = {
                key: pose_after['value'][key]-pose_before['value'][key]
                for key in ('x', 'y')}
        effect = (candidate(effects, 'observational_state_difference',
            {'stream': 'frames/pose/auto_index', 'start_ns': start, 'end_ns': end})
            if effects else unknown('No independently established state change'))
        result.append({'schema': VERSION, 'unit_id': f'{episode}:{number:06d}',
            'episode_id': episode, 'start_ns': start, 'end_ns': end,
            'source_kind': 'sealed_human_evidence', 'formal_training_eligible': False,
            'goal': _goal_at(goal_labels, goal_times, start),
            'world_state': {'scene': scene_before,
                'pose': pose_before},
            'affordance': unknown('No reviewed affordance'),
            'intent': unknown('Behavior does not reveal intent'),
            'expected_effect': unknown('No explicit pre-action expectation'),
            'action': {'physical_status': 'observed_hook',
                'held_keys_at_start': previous_state['held_keys'],
                'held_buttons_at_start': previous_state['held_buttons'],
                'qualified_physical_events': [dict(evidence=ref, **value)
                    for _, ref, value in controls],
                'raw_mouse_candidate': {'event_count': len(raw_slice),
                    'dx_counts': sum(item[1] for item in raw_slice),
                    'dy_counts': sum(item[2] for item in raw_slice),
                    'producer': 'unknown', 'provenance_qualified': False,
                    'training_weight': 0.0},
                'tool_events': [{'kind': row['kind'],
                    'tool_id': row['payload'].get('tool_id'),
                    'reported_outcome': row['payload'].get('outcome'),
                    'outcome_verified': False, 'evidence': reference('tools', row)}
                    for row in tools[t0:t1]],
                'game_receipt': unknown('Game provides no per-input receipt')},
            'observed_effect': effect,
            'progress': unknown('No verified goal-distance or task progress'),
            'outcome': unknown('No verified result or reward'),
            'observation': {'first_frame': frames[before]['payload']['frame_index']
                    if before >= 0 else None,
                'last_frame': frames[after]['payload']['frame_index']
                    if after >= 0 else None,
                'frames_in_unit': len(local_frames),
                'focused_fresh_available_frames': len(available),
                'capture_quality': 'available' if available else 'unavailable'},
            'evidence': {'source_sha256': source_hashes,
                'auto_index_version': index['version'],
                'compiler_version': VERSION},
            'semantic_supervision_admitted': False})
    return result


def summarize(units):
    return {'units': len(units), 'episodes': len({u['episode_id'] for u in units}),
        'goal_candidates': sum(u['goal']['status'] == 'derived_candidate' for u in units),
        'intent_verified': sum(u['intent']['status'] == 'verified' for u in units),
        'qualified_mouse_units': sum(u['action']['raw_mouse_candidate']['provenance_qualified']
            for u in units),
        'units_with_raw_mouse_candidate': sum(u['action']['raw_mouse_candidate']['event_count'] > 0
            for u in units),
        'units_with_physical_transition': sum(bool(u['action']['qualified_physical_events'])
            for u in units),
        'unavailable_observation_units': sum(u['observation']['capture_quality'] == 'unavailable'
            for u in units),
        'scene_candidates': dict(Counter(u['world_state']['scene'].get('value') or 'unknown'
            for u in units))}


def validate_episode(units, duration):
    if not units or units[0]['start_ns'] != 0 or units[-1]['end_ns'] != duration:
        raise ValueError('Decision units do not cover the complete episode')
    episode = units[0]['episode_id']
    for index, unit in enumerate(units):
        if (unit['schema'] != VERSION or unit['episode_id'] != episode
                or unit['unit_id'] != f'{episode}:{index:06d}'
                or unit['end_ns'] <= unit['start_ns']
                or (index and unit['start_ns'] != units[index-1]['end_ns'])
                or unit['formal_training_eligible']
                or unit['semantic_supervision_admitted']
                or unit['intent']['status'] != 'unknown'
                or unit['expected_effect']['status'] != 'unknown'
                or unit['action']['game_receipt']['status'] != 'unknown'
                or unit['action']['raw_mouse_candidate']['provenance_qualified'] is not False
                or unit['action']['raw_mouse_candidate']['training_weight'] != 0.0):
            raise ValueError(f'Decision unit v1 admission/chronology violation: {index}')
    return True


def write_episode_sidecar(path, *, min_ns=500_000_000, max_ns=4*SECOND):
    """Compile one sealed episode after auto-indexing without editing mother streams."""
    path = Path(path)
    units = compile_episode(path, min_ns=min_ns, max_ns=max_ns)
    manifest = json.loads((path/'manifest.json').read_text(encoding='utf-8-sig'))
    validate_episode(units, int(manifest['duration_ns']))
    output = path/'decision_units.v1.jsonl'
    temporary = output.with_suffix('.jsonl.partial')
    with temporary.open('w', encoding='utf-8') as stream:
        for unit in units:
            stream.write(json.dumps(unit, ensure_ascii=False, separators=(',', ':'))+'\n')
    os.replace(temporary, output)
    audit = {'schema': 'synaeris.decision-unit-audit.v1',
        'status': 'diagnostic_candidate_only', 'formal_training_eligible': False,
        'decision_units_sha256': digest(output),
        'source_sha256': units[0]['evidence']['source_sha256'],
        'total': summarize(units)}
    atomic_json(path/'decision_units.v1.audit.json', audit)
    return audit
