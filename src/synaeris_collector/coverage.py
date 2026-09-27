"""Route diversity is a selection prior, not verified scene or task coverage."""
import math
from pathlib import Path


def route_features(route, data):
    points = data["positions"]
    first = points[0]
    folder = Path(route["relative_path"].replace("\\", "/")).parent.name
    modes = sorted({p.get("move_mode", "walk") for p in points})
    tags = {"route_folder:" + folder, "length:" + ("short" if len(points) <= 25 else "medium" if len(points) <= 60 else "long"),
            f"origin_tile:{math.floor(first['x']/500)}:{math.floor(first['y']/500)}"}
    tags.update("movement:" + mode for mode in modes)
    tags.update("action:" + p["action"] for p in points if p.get("action"))
    return sorted(tags)


def select_diverse(candidates, limit):
    """Greedily cover distinct movement, locality, item folder and route lengths."""
    remaining = list(candidates)
    selected, covered = [], set()
    while remaining and len(selected) < limit:
        def score(item):
            features = set(item["coverage_features"])
            novelty = sum(3 if tag.startswith("route_folder:") else 2 if tag.startswith(("movement:", "action:")) else 1
                          for tag in features - covered)
            return novelty - .003 * item["points"], item["sha256"]
        best = max(remaining, key=score)
        remaining.remove(best)
        selected.append(best)
        covered.update(best["coverage_features"])
    return selected
