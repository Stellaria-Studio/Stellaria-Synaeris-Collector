"""Inspect finalized machine collection and gate continuation on real evidence."""
import argparse
import json
from pathlib import Path

from synaeris_collector.dataset import inspect_episode, transitions
from synaeris_collector.storage import atomic_json, iter_rows


from synaeris_collector.qualification import qualify as qualify_core


def qualify(path):
    return qualify_core(path, inspect=inspect_episode)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("episode")
    args = parser.parse_args()
    result = qualify(args.episode)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["continuation_allowed"] else 2)
