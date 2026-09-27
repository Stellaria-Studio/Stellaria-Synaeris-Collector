import pytest

from synaeris_collector.decision_units import (SECOND, _goal_at, _physical_input,
    _pose_at, boundaries)


def test_event_boundaries_are_variable_duration_and_bounded():
    cuts = boundaries(7*SECOND, [100_000_000, 700_000_000,
        730_000_000, 2_500_000_000, 4_900_000_000],
        min_ns=500_000_000, max_ns=2*SECOND)
    assert cuts[0] == 0 and cuts[-1] == 7*SECOND
    assert 700_000_000 in cuts and 730_000_000 not in cuts
    assert all(0 < b-a <= 2*SECOND for a,b in zip(cuts,cuts[1:]))
    assert len(set(b-a for a,b in zip(cuts,cuts[1:]))) > 1


def test_old_raw_mouse_cannot_be_promoted_and_injected_hooks_are_excluded():
    def row(kind, stamp, actor, source, payload):
        return {'kind': kind, 'monotonic_timestamp': stamp, 'sequence': stamp,
            'actor': actor, 'source': source, 'payload': payload}
    physical = [row('KEY_DOWN', 10, 'HUMAN', 'win32_low_level_hook',
        {'vk':87, 'injected':False}),
        row('KEY_UP', 20, 'UNKNOWN', 'win32_low_level_hook',
        {'vk':87, 'injected':True}),
        row('KEY_UP', 30, 'HUMAN', 'win32_low_level_hook',
        {'vk':87, 'injected':False})]
    raw = row('MOUSE_DELTA', 15, 'UNKNOWN', 'win32_raw_input',
        {'dx':9, 'dy':-3, 'absolute':False, 'provenance_qualified':False})
    events, mouse = _physical_input(physical+[raw])
    assert [event[2]['kind'] for event in events] == ['KEY_DOWN', 'KEY_UP']
    assert events[0][2]['held_keys'] == [87] and events[-1][2]['held_keys'] == []
    assert mouse == [(15,9,-3)]
    with pytest.raises(ValueError, match='raw mouse'):
        _physical_input([dict(raw, actor='HUMAN')])


def test_ocr_goal_remains_zero_weight_candidate_and_pose_has_freshness():
    labels = [{'timestamp_ns': 1, 'detail': {'text': '去往目标地点'},
        'score': .6, 'evidence': {'stream': 'ocr', 'sequence': 2}}]
    goal = _goal_at(labels, [1], SECOND)
    assert goal['status'] == 'derived_candidate'
    assert goal['training_weight'] == 0 and goal['value']['quest_id'] is None
    assert _goal_at(labels, [1], 40*SECOND)['status'] == 'unknown'
    pose = {'kind': 'POSE', 'monotonic_timestamp': SECOND, 'sequence': 3,
        'source': 'bettergi_native', 'actor': 'BGI',
        'payload': {'valid':True, 'x':1., 'y':2., 'yaw':.5, 'confidence':.8}}
    assert _pose_at([pose], [SECOND], 2*SECOND)['status'] == 'observed_sensor'
    assert _pose_at([pose], [SECOND], 4*SECOND)['status'] == 'unknown'
