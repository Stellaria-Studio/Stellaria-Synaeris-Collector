"""Inventory every HUMAN mother episode without inventing missing game semantics."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from synaeris_collector.storage import atomic_json


def sha256(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def run(args):
    labels=[json.loads(line) for line in Path(args.window_labels).read_text(
        encoding='utf-8').splitlines()]
    window_counts=Counter(row['episode_id'] for row in labels)
    episodes=[]
    for directory in sorted(Path(args.episodes_root).iterdir()):
        manifest_file=directory/'manifest.json'
        if not directory.is_dir() or not manifest_file.exists():continue
        manifest=json.loads(manifest_file.read_text(encoding='utf-8-sig'))
        if manifest.get('config',{}).get('actor')!='HUMAN':continue
        index_file=directory/'auto_index.json'
        index=json.loads(index_file.read_text(encoding='utf-8-sig')) if index_file.exists() else None
        task_review_file=directory/'task_reviews.jsonl'
        task_reviews=[json.loads(line) for line in task_review_file.read_text(
            encoding='utf-8').splitlines()] if task_review_file.exists() else []
        if index and index['episode_id']!=directory.name:
            raise ValueError('Auto index identity mismatch')
        episodes.append({'episode_id':directory.name,'status':manifest['status'],
            'manifest_sha256':sha256(manifest_file),
            'auto_index_sha256':sha256(index_file) if index else None,
            'auto_index_scene_segments':dict(Counter(s['scene'] for s in index['segments'])) if index else {},
            'auto_index_label_candidates':len(index.get('labels',[])) if index else 0,
            'diagnostic_window_labels':window_counts[directory.name],
            'human_task_declarations':len(task_reviews),
            'physical_button_window_labels':sum(row['episode_id']==directory.name and
                row.get('physical_button_action_valid',False) for row in labels),
            'formal_semantic_review_present':False,
            'unresolved':['verified_quest_region_puzzle_poi','reward','success','mouse_motion']})
    if set(window_counts)-{row['episode_id'] for row in episodes}:
        raise ValueError('Window labels contain an unknown HUMAN episode')
    report={'schema':'synaeris.human-label-coverage.v1','human_episodes':len(episodes),
        'complete_episodes':sum(row['status']=='complete' for row in episodes),
        'partial_episodes':sum(row['status']!='complete' for row in episodes),
        'window_label_episodes':sum(row['diagnostic_window_labels']>0 for row in episodes),
        'window_labels':len(labels),'formal_dataset':False,'episodes':episodes}
    atomic_json(args.output,report)
    print(json.dumps({k:v for k,v in report.items() if k!='episodes'},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--episodes-root',required=True)
    parser.add_argument('--window-labels',required=True)
    parser.add_argument('--output',required=True)
    run(parser.parse_args())
