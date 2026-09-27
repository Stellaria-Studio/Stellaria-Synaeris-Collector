import argparse
import json
from pathlib import Path
from synaeris_collector.native_groups import semantic_hash

parser = argparse.ArgumentParser()
parser.add_argument("path")
parser.add_argument("expected")
args = parser.parse_args()
actual = json.loads(Path(args.path).read_text(encoding="utf-8-sig"))
if semantic_hash(actual) != args.expected:
    raise SystemExit("Native execution configuration changed after preparation")
