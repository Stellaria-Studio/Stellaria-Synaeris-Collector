import argparse
import json
from pathlib import Path
import signal
import time
from .config import Config


def main():
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="synaeris")
    commands = parser.add_subparsers(dest="command", required=True)
    gui = commands.add_parser("gui")
    gui.add_argument("--config")
    record = commands.add_parser("record")
    record.add_argument("--config")
    record.add_argument("--output")
    record.add_argument("--duration", type=float)
    record.add_argument("--stop-file", help="Local stop request file; finalizes all streams safely")
    record.add_argument("--wait-game", type=float, default=0, help="Seconds to wait for a focused, restored game window")
    record.add_argument("--restore-game", action="store_true", help="Try one normal game-window restore during startup, after integrity checks")
    record.add_argument("--synthetic", action="store_true")
    record.add_argument("--no-ocr", action="store_true")
    for cmd in ("inspect", "mine", "recover", "windows", "organize"):
        item = commands.add_parser(cmd)
        item.add_argument("episode")
    frame = commands.add_parser("frame")
    frame.add_argument("episode")
    frame.add_argument("--timestamp", type=float, required=True, help="episode-relative seconds")
    frame.add_argument("--output", required=True)
    dataset = commands.add_parser("dataset")
    dataset.add_argument("root")
    dataset.add_argument("--output", required=True)
    retention = commands.add_parser("retention")
    retention.add_argument("root")
    retention.add_argument("--output", required=True)
    catalog = commands.add_parser("bgi-catalog")
    catalog.add_argument("--bgi-dir", default=Config().bgi_dir)
    catalog.add_argument("--output", required=True)
    logs = commands.add_parser("bgi-logs")
    logs.add_argument("--bgi-dir", default=Config().bgi_dir)
    logs.add_argument("--output", default="data/bgi_logs")
    batch = commands.add_parser("bgi-batch")
    batch.add_argument("routes", nargs="+")
    batch.add_argument("--output", required=True)
    batch.add_argument("--port", type=int, default=18765)
    deploy = commands.add_parser("bgi-deploy")
    deploy.add_argument("batch")
    deploy.add_argument("--bgi-dir", default=Config().bgi_dir)
    args = parser.parse_args()
    result = None
    if args.command == "gui":
        from .gui import App
        App(Config.load(args.config)).run()
    elif args.command == "record":
        from .collector import Collector
        config = Config.load(args.config)
        if args.output:
            config.output = args.output
        if args.synthetic:
            config.capture = "synthetic"
        if args.no_ocr:
            config.ocr = False
        if args.duration is not None and args.duration <= 0:
            parser.error("duration must be positive")
        collector = Collector(config, print)
        signal.signal(signal.SIGINT, lambda *_: collector.stop())
        if args.wait_game > 0 and not args.synthetic:
            from .capture import GameWindow
            deadline = time.monotonic() + args.wait_game
            print("Waiting for the game to be focused and restored", flush=True)
            restore_attempted = False
            while True:
                if collector.stop_event.is_set() or (args.stop_file and Path(args.stop_file).exists()):
                    return
                try:
                    window = GameWindow(config.window_title)
                    if args.restore_game and not restore_attempted:
                        restore_attempted = True
                        restored = window.restore_for_collection()
                        print(f"Normal game-window startup restore attempted; focused={restored}", flush=True)
                    rect = window.rect()
                    ready = window.active() and rect[2]-rect[0] >= 640 and rect[3]-rect[1] >= 360
                except RuntimeError:
                    ready = False
                if ready:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Game focus/geometry preflight timed out; no gameplay episode was fabricated")
                collector.stop_event.wait(.25)
        result = str(collector.run(args.duration, args.stop_file))
    elif args.command == "organize":
        from .auto_index import organize
        index = organize(args.episode)
        result = {'episode_id': index['episode_id'], 'segments': len(index['segments']),
                  'label_counts': index['label_counts'], 'scene_counts': index['scene_counts']}
    elif args.command == "retention":
        from .retention import plan
        result = plan(args.root, args.output)
    elif args.command == "recover":
        from .storage import recover
        result = recover(args.episode)
    elif args.command in {"inspect", "mine", "windows", "frame", "dataset"}:
        from . import dataset
        if args.command == "inspect":
            result = dataset.inspect_episode(args.episode)
        elif args.command == "mine":
            result = dataset.transitions(args.episode)
        elif args.command == "windows":
            result = dataset.decision_windows(args.episode)
        elif args.command == "frame":
            result = dataset.export_frame(args.episode, round(args.timestamp*1e9), args.output)
        else:
            result = dataset.build_dataset(args.root, args.output)
    else:
        from . import bgi
        if args.command == "bgi-catalog":
            result = bgi.catalog(args.bgi_dir)
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            from .storage import atomic_json
            atomic_json(args.output, result)
            result = {"routes": len(result), "output": args.output}
        elif args.command == "bgi-logs":
            result = bgi.import_logs(args.bgi_dir, args.output)
        elif args.command == "bgi-batch":
            result = str(bgi.make_batch(args.routes, args.output, args.port))
        elif args.command == "bgi-deploy":
            result = str(bgi.deploy_batch(args.batch, args.bgi_dir))
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
