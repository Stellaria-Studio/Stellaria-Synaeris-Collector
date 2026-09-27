import json

import pytest

from synaeris_collector.task_review import append_task_review


def test_task_review_is_append_only_and_never_verifies_outcome(tmp_path):
    manifest={'episode_id':'human-a','status':'complete','synthetic':False,
        'duration_ns':10_000_000_000,'config':{'actor':'HUMAN'}}
    (tmp_path/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    with pytest.raises(ValueError,match='evidence note'):
        append_task_review(tmp_path,start_seconds=0,end_seconds=5,
            scenario='战斗与拾取',outcome='completed')
    entry=append_task_review(tmp_path,start_seconds=1,end_seconds=5,
        scenario='战斗与拾取',quest='test-quest',outcome='completed',
        evidence_note='Visible completion prompt at end')
    assert entry['semantic_groups']['quest']=='test-quest'
    assert entry['outcome_declared']=='completed' and not entry['verified_outcome']
    assert not entry['formal_training_eligible']
    append_task_review(tmp_path,start_seconds=5,end_seconds=9,
        scenario='首次探索',outcome='unfinished')
    lines=(tmp_path/'task_reviews.jsonl').read_text(encoding='utf-8').splitlines()
    assert len(lines)==2 and json.loads(lines[0])['record_id']!=json.loads(lines[1])['record_id']
    with pytest.raises(ValueError,match='within the episode'):
        append_task_review(tmp_path,start_seconds=9,end_seconds=11,scenario='首次探索')
