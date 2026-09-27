"""Compile evidence-preserving, variable-duration DecisionUnit sidecars."""
import argparse
import json
import os
from pathlib import Path

from synaeris_collector.decision_units import (compile_episode, digest, summarize,
    validate_episode)
from synaeris_collector.storage import atomic_json


def source_plan(path):
    references = {}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        row = json.loads(line)
        if (row['schema'] != 'synaeris.world-window-plan.v1'
                or row['formal_training_eligible']):
            raise ValueError('Expected quarantined source-bound world plan')
        episode, source = row['episode_id'], row['source_sha256']
        if episode in references and references[episode] != source:
            raise ValueError(f'Inconsistent source hashes: {episode}')
        references[episode] = source
    return references


def run(args):
    episodes_root, output = Path(args.episodes_root), Path(args.output)
    references = source_plan(args.source_plan) if args.source_plan else None
    if args.episode:
        paths = [episodes_root/args.episode]
    elif references is not None:
        paths = [episodes_root/episode for episode in sorted(references)]
    else:
        paths = sorted(path for path in episodes_root.iterdir() if path.is_dir()
            and (path/'manifest.json').exists())
    if not paths:
        raise ValueError('No source episodes')
    units = []
    per_episode = {}
    for path in paths:
        rows = compile_episode(path, references[path.name] if references else None,
            int(args.min_seconds*1e9), int(args.max_seconds*1e9))
        manifest = json.loads((path/'manifest.json').read_text(encoding='utf-8-sig'))
        validate_episode(rows, int(manifest['duration_ns']))
        units.extend(rows)
        per_episode[path.name] = summarize(rows)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix+'.partial')
    with temporary.open('w', encoding='utf-8') as stream:
        for unit in units:
            stream.write(json.dumps(unit, ensure_ascii=False, separators=(',', ':'))+'\n')
    os.replace(temporary, output)
    report = {'schema': 'synaeris.decision-unit-audit.v1',
        'status': 'diagnostic_candidate_only', 'formal_training_eligible': False,
        'source_plan_sha256': digest(args.source_plan) if args.source_plan else None,
        'decision_units_sha256': digest(output),
        'segmentation': {'min_seconds': args.min_seconds,
            'max_seconds': args.max_seconds},
        'total': summarize(units), 'per_episode': per_episode,
        'limitations': ['No verified intent, affordance, expected effect, reward or outcome',
            'Raw Input mouse producer and game receipt cannot be retroactively recovered',
            'OCR/scene scores are uncalibrated candidates with zero training weight',
            'No same-start goal contrast or action intervention exists in old episodes']}
    atomic_json(output.with_suffix('.audit.json'), report)
    print(json.dumps(report['total'], ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--episodes-root', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--source-plan')
    parser.add_argument('--episode')
    parser.add_argument('--min-seconds', type=float, default=.5)
    parser.add_argument('--max-seconds', type=float, default=4.0)
    run(parser.parse_args())
