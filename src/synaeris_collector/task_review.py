"""Append-only, post-session human task declarations; outcomes remain unverified."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import uuid

from .human import SCENARIOS

OUTCOMES=('unknown','completed','failed','unfinished')


def append_task_review(episode,*,start_seconds,end_seconds,scenario,region='unknown',
        quest='unknown',puzzle='unknown',poi='unknown',outcome='unknown',evidence_note=''):
    episode=Path(episode).resolve()
    manifest_file=episode/'manifest.json'
    manifest=json.loads(manifest_file.read_text(encoding='utf-8-sig'))
    if (manifest.get('synthetic') or manifest['status']!='complete' or
            manifest['config']['actor']!='HUMAN'):
        raise ValueError('Task review requires a sealed real HUMAN episode')
    if scenario not in SCENARIOS or outcome not in OUTCOMES:
        raise ValueError('Unknown scenario or declared outcome')
    start=int(round(float(start_seconds)*1e9));end=int(round(float(end_seconds)*1e9))
    if not 0<=start<end<=manifest['duration_ns']:
        raise ValueError('Task segment must fit within the episode duration')
    labels={key:str(value).strip() or 'unknown' for key,value in
        (('region',region),('quest',quest),('puzzle',puzzle),('poi',poi))}
    note=str(evidence_note).strip()
    if outcome in ('completed','failed') and not note:
        raise ValueError('Completion/failure declaration requires an evidence note')
    entry={'schema':'synaeris.task-review-declaration.v1',
        'record_id':str(uuid.uuid4()),'episode_id':manifest['episode_id'],
        'start_ns':start,'end_ns':end,'scenario':scenario,
        'semantic_groups':labels,'outcome_declared':outcome,
        'evidence_note':note,'reviewer':'human_post_session',
        'reviewed_utc':datetime.now(timezone.utc).isoformat(),
        'source_manifest_sha256':hashlib.sha256(manifest_file.read_bytes()).hexdigest(),
        'verified_outcome':False,'formal_training_eligible':False,
        'limitation':'Human declaration requires video/event corroboration before formal labels.'}
    destination=episode/'task_reviews.jsonl'
    with destination.open('a',encoding='utf-8',buffering=1) as stream:
        stream.write(json.dumps(entry,ensure_ascii=False,allow_nan=False)+'\n')
    return entry
