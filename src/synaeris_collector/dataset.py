"""Evidence-based QC, transition mining, component splits and retention proposals."""
import bisect
import hashlib
import json
import math
import subprocess
import uuid
from pathlib import Path
import cv2
import numpy as np
from .storage import STREAMS, iter_rows, atomic_json

REWARDS = {"QUEST_STAGE_CHANGE": 10, "PUZZLE_COMPLETE": 8, "WAYPOINT_UNLOCKED": 8,
           "CHEST_UNLOCKED": 5, "SUBGOAL_COMPLETE": 2, "USEFUL_DISCOVERY": 1,
           "TOOL_FAILED": -1, "REPEAT_FAILED_ACTION": -1, "NO_PROGRESS": -2, "DEATH": -5}


def decoded_frame_count(path, expected_frames):
    """Fully decode, preferring measured NVDEC; never substitute container estimates."""
    from .capture import find_ffmpeg
    diagnostics = []
    try:
        executable = find_ffmpeg()
        for backend, acceleration in (("ffmpeg_cuda", ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]),
                                      ("ffmpeg_cpu", [])):
            command = [executable, "-hide_banner", "-loglevel", "error", "-xerror", *acceleration,
                       "-i", str(path), "-map", "0:v:0", "-an", "-f", "null", "-",
                       "-progress", "pipe:1", "-nostats"]
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=max(60, expected_frames / 30 + 30))
            frames = [int(line.split("=", 1)[1]) for line in result.stdout.splitlines()
                      if line.startswith("frame=")]
            if result.returncode == 0 and "progress=end" in result.stdout and frames:
                return frames[-1], backend, diagnostics
            diagnostics.append({"backend": backend, "error": result.stderr[-2000:]})
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        diagnostics.append({"backend": "ffmpeg", "error": str(exc)})
    # Independent fallback remains a real decode, with count agreement checked by QC.
    video = cv2.VideoCapture(str(path))
    count = 0
    while True:
        ok, _ = video.read()
        if not ok:
            break
        count += 1
    video.release()
    return count, "opencv_cpu", diagnostics


def inspect_episode(path):
    path = Path(path)
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    issues, counts = [], {}
    for stream in STREAMS:
        try:
            count, previous_sequence = 0, -1
            for row in iter_rows(path, stream):
                count += 1
                if row["episode_id"] != manifest["episode_id"]:
                    issues.append(f"{stream}: episode identity mismatch")
                if row["monotonic_timestamp"] < 0:
                    issues.append(f"{stream}: negative timestamp")
                if row["sequence"] <= previous_sequence:
                    issues.append(f"{stream}: invalid sequence")
                if stream == "input" and row["source"] == "win32_raw_input" and row["actor"] == "HUMAN":
                    issues.append("input: raw mouse producer identity was not qualified")
                previous_sequence = row["sequence"]
            counts[stream] = count
            if manifest.get("counts", {}).get(stream) != count:
                issues.append(f"{stream}: manifest count mismatch")
        except Exception as exc:
            issues.append(f"{stream}: {exc}")
    frame_rows = list(iter_rows(path, "frames")) if "frames" in counts else []
    timestamps = [r["monotonic_timestamp"] for r in frame_rows]
    intervals = np.diff(timestamps) / 1e6 if len(timestamps) > 1 else np.array([])
    if len(timestamps) > 1 and np.any(np.diff(timestamps) <= 0):
        issues.append("frames: non-increasing acquisition timestamps")
    for i, row in enumerate(frame_rows):
        if row["payload"].get("frame_index") != i:
            issues.append("frames: non-contiguous video index")
            break
    video_path = path / "visual.mkv"
    decoded, decode_backend, decode_diagnostics = 0, None, []
    if video_path.exists():
        decoded, decode_backend, decode_diagnostics = decoded_frame_count(video_path, len(frame_rows))
        if decode_backend == "opencv_cpu" and any(d.get("backend") == "ffmpeg_cpu" for d in decode_diagnostics):
            issues.append("video: strict software decode failed; fallback count is insufficient")
        if decoded != len(frame_rows):
            issues.append("video/frame-table count mismatch")
    else:
        issues.append("visual.mkv missing")
    jumps, previous = [], None
    for row in iter_rows(path, "pose") if "pose" in counts else []:
        pose = row["payload"]
        if not pose.get("valid"):
            continue
        if previous:
            prevrow, prev = previous
            dt = (row["monotonic_timestamp"] - prevrow["monotonic_timestamp"]) / 1e9
            distance = math.hypot(pose["x"]-prev["x"], pose["y"]-prev["y"])
            if 0 < dt <= 2 and distance > 150:
                jumps.append({"timestamp_ns": row["monotonic_timestamp"], "distance": distance,
                              "classification": "needs_review_teleport_or_localization_jump"})
        previous = row, pose
    texts = [r["payload"] for r in iter_rows(path, "ocr") if r["kind"] == "OCR_TEXT"] if "ocr" in counts else []
    fingerprint = hashlib.sha256("|".join(r["payload"].get("visual_hash", "")
        for r in iter_rows(path, "events") if r["kind"] == "OBSERVATION").encode()).hexdigest()
    report = {"episode_id": manifest["episode_id"], "status": manifest["status"],
              "synthetic": manifest.get("synthetic", False), "issues": sorted(set(issues)),
              "counts": counts, "decoded_frames": decoded, "pose_jumps": jumps,
              "video_decode_backend": decode_backend, "video_decode_diagnostics": decode_diagnostics,
              "low_confidence_ocr": sum(t.get("confidence", 0) < 0.65 for t in texts),
              "ocr_texts": len(texts), "visual_fingerprint": fingerprint,
              "capture_frame_interval_p50_ms": float(np.percentile(intervals, 50)) if len(intervals) else None,
              "capture_frame_interval_p95_ms": float(np.percentile(intervals, 95)) if len(intervals) else None}
    atomic_json(path / "quality.json", report)
    return report


def transitions(path):
    path = Path(path)
    frames = list(iter_rows(path, "frames"))
    stamps = [r["monotonic_timestamp"] for r in frames]
    starts, result = {}, []
    for row in sorted(iter_rows(path, "tools"), key=lambda r: (r["monotonic_timestamp"], r["sequence"])):
        data = row["payload"]
        tool_id = data.get("tool_id")
        if not tool_id:
            continue
        if row["kind"] == "TOOL_START":
            starts[tool_id] = row
        elif row["kind"] in {"TOOL_END", "TOOL_FAILED", "TOOL_CANCELLED"} and tool_id in starts:
            start = starts.pop(tool_id)
            before = bisect.bisect_right(stamps, start["monotonic_timestamp"])-1
            after = bisect.bisect_left(stamps, row["monotonic_timestamp"])
            outcome = data.get("outcome", "unknown")
            verified = data.get("verified", False) is True
            failure = row["kind"] == "TOOL_FAILED" or (verified and outcome == "failure")
            reward = -1 if failure else (2 if verified and outcome == "success" else None)
            result.append({"episode_id": start["episode_id"], "tool_id": tool_id,
                "start_ns": start["monotonic_timestamp"], "end_ns": row["monotonic_timestamp"],
                "state_frame": frames[before]["payload"]["frame_index"] if before >= 0 else None,
                "next_state_frame": frames[after]["payload"]["frame_index"] if after < len(frames) else None,
                "action": start["payload"], "result": data, "outcome": outcome,
                "reward": reward, "reward_source": "verified_result" if verified else "exception" if failure else "unavailable",
                "terminal": False, "weak_label": not verified})
    out = path / "transitions.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for item in result:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    return result


def decision_windows(path):
    path = Path(path)
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    duration = manifest.get("duration_ns", 0)
    result = []
    for line in (path / "annotations.jsonl").read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        result.append({**item, "start_ns": item["requested_start_ns"],
                       "end_ns": min(duration, item["requested_end_ns"]),
                       "post_window_truncated": duration < item["requested_end_ns"]})
    atomic_json(path / "decision_windows.json", result)
    return result


def export_frame(path, timestamp_ns, output):
    frames = list(iter_rows(path, "frames"))
    if not frames:
        raise ValueError("No frames")
    stamps = [r["monotonic_timestamp"] for r in frames]
    index = max(0, bisect.bisect_right(stamps, timestamp_ns)-1)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    from .capture import find_ffmpeg
    frame_index = frames[index]["payload"]["frame_index"]
    temporary = output.with_name(output.stem + ".extracting-" + uuid.uuid4().hex + output.suffix)
    diagnostics = []
    for backend, acceleration, download in (
        ("ffmpeg_cuda", ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"], ",hwdownload,format=nv12"),
        ("ffmpeg_cpu", [], ""),
    ):
        # Select by encoded frame ordinal, not timestamp seeking: mother-video
        # PTS and irregular source acquisition QPC have different meanings.
        command = [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-xerror", "-y", *acceleration,
                   "-i", str(Path(path) / "visual.mkv"), "-map", "0:v:0", "-an",
                   "-vf", f"select=eq(n\\,{frame_index})" + download,
                   "-frames:v", "1", str(temporary)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=45)
            if result.returncode == 0 and temporary.exists() and temporary.stat().st_size:
                temporary.replace(output)
                break
            diagnostics.append({"backend": backend, "error": result.stderr[-1000:]})
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            diagnostics.append({"backend": backend, "error": str(exc)})
        finally:
            temporary.unlink(missing_ok=True)
    else:
        raise ValueError("Could not decode requested frame: " + str(diagnostics))
    return {"path": str(output.resolve()), "acquisition_timestamp_ns": stamps[index],
            "frame_index": frame_index, "decode_backend": backend, "decode_diagnostics": diagnostics}


def build_dataset(root, output):
    root = Path(root)
    episodes, duplicates, seen = [], [], {}
    excluded = []
    for file in sorted(root.glob("*/manifest.json")):
        manifest = json.loads(file.read_text(encoding="utf-8"))
        if manifest.get("config", {}).get("purpose") == "calibration":
            excluded.append({"episode_id": manifest["episode_id"], "reason": "calibration_only"})
            continue
        if manifest.get("synthetic") or manifest["status"] != "complete":
            excluded.append({"episode_id": manifest["episode_id"], "reason": "synthetic_or_incomplete"})
            continue
        if manifest.get("config", {}).get("capture_background"):
            excluded.append({"episode_id": manifest["episode_id"], "reason": "background_action_alignment_unqualified"})
            continue
        if manifest.get("capabilities", {}).get("input_permission_qualified") is False:
            excluded.append({"episode_id": manifest["episode_id"], "reason": "input_permission_unqualified"})
            continue
        # A technical continuation check never overrides explicit review state.
        # Real machine episodes remain outside the default export until reviewed.
        review_file = file.parent / "review.json"
        machine_file = file.parent / "machine_qualification.json"
        if review_file.exists() or machine_file.exists():
            metadata_file = review_file if review_file.exists() else machine_file
            metadata = json.loads(metadata_file.read_text(encoding="utf-8-sig"))
            if metadata.get("training_eligible") is not True:
                reason = "review_quarantined" if review_file.exists() else "machine_review_pending"
                excluded.append({"episode_id": manifest["episode_id"], "reason": reason})
                continue
        report = inspect_episode(file.parent)
        if report["issues"]:
            excluded.append({"episode_id": manifest["episode_id"], "reason": "quality_errors"})
            continue
        fingerprint = report["visual_fingerprint"]
        if fingerprint in seen:
            duplicates.append({"episode_id": manifest["episode_id"], "duplicate_of": seen[fingerprint]})
        else:
            seen[fingerprint] = manifest["episode_id"]
        transitions(file.parent)
        decision_windows(file.parent)
        episodes.append({"episode_id": manifest["episode_id"], "path": str(file.parent.resolve()),
                         "config": manifest["config"], "duration_ns": manifest.get("duration_ns", 0),
                         "collection_profile": manifest.get("collection_profile"),
                         "auto_index_path": str((file.parent / "auto_index.json").resolve())
                             if (file.parent / "auto_index.json").exists() else None})
    # Transitive connected components prevent indirect Quest/Region/Puzzle/POI leakage.
    parents = list(range(len(episodes)))
    def find(i):
        while i != parents[i]:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    identifiers = {}
    for i, episode in enumerate(episodes):
        for key in ("region", "quest", "puzzle", "poi"):
            value = episode["config"].get(key)
            if not value or value == "unknown":
                continue
            identity = (key, value)
            if identity in identifiers:
                parents[find(i)] = find(identifiers[identity])
            identifiers[identity] = i
    components = {}
    for i, episode in enumerate(episodes):
        components.setdefault(find(i), []).append(episode)
    assigned = {"train": [], "validation": [], "rl_calibration": [], "locked_test": [], "ood_test": []}
    weights = {"train": 0.7, "validation": 0.1, "rl_calibration": 0.1, "locked_test": 0.1}
    groups = sorted(components.values(), key=lambda g: (-sum(e["duration_ns"] for e in g), g[0]["episode_id"]))
    total_duration = max(1, sum(e["duration_ns"] for e in episodes))
    totals = {k: 0 for k in weights}
    for group in groups:
        if any(e["config"].get("ood", False) for e in group):
            split = "ood_test"
        else:
            split = max(weights, key=lambda k: weights[k]*total_duration-totals[k])
            totals[split] += sum(e["duration_ns"] for e in group)
        assigned[split].extend(group)
    result = {"schema_version": 1, "split_unit": "connected Episode/Quest/Region/Puzzle/POI groups",
              "target_ratios": weights, "component_count": len(groups), "splits": assigned,
              "duplicates": duplicates, "excluded": excluded,
              "locked_test_rl_allowed": False,
              "limitations": ["Few or uneven groups cannot achieve 70/10/10/10; acquire more independent regions/tasks",
                              "Capture FPS is not game FPS; unknown rewards are not zeros"]}
    atomic_json(output, result)
    return result
