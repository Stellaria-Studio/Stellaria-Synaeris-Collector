import os
from pathlib import Path
import sys
from synaeris_collector.gui import App
from synaeris_collector.config import Config
from synaeris_collector.version import APP_VERSION

if __name__ == "__main__":
    if getattr(sys, "frozen", False):
        os.chdir(Path(sys.executable).parent)
        if not any(arg in sys.argv for arg in ("--self-test", "--compile-decision-units",
                                              "--machine-worker", "--machine-probe")):
            from synaeris_collector.updater import relaunch_newer
            if relaunch_newer(sys.argv[1:], current=APP_VERSION):
                raise SystemExit(0)
    local = Path("config.local.json")
    if sys.argv[1:] in (["--machine-worker"], ["--machine-probe"]):
        from synaeris_collector.machine_worker import installed_main
        try:
            installed_main(probe=sys.argv[1] == "--machine-probe")
        except Exception:
            # installed_main records the failure in last_error.json. A frozen
            # windowed executable must exit instead of leaving a modal crash
            # dialog that keeps the scheduled task and GPU session alive.
            raise SystemExit(1) from None
    elif "--self-test" in sys.argv:
        from synaeris_collector.selftest import run
        index = sys.argv.index("--self-test")
        output = sys.argv[index+1] if len(sys.argv) > index+1 else "selftest"
        run(output)
    elif "--compile-decision-units" in sys.argv:
        from synaeris_collector.decision_units import write_episode_sidecar
        index = sys.argv.index("--compile-decision-units")
        if len(sys.argv) <= index+1:
            raise SystemExit("Episode directory required")
        write_episode_sidecar(sys.argv[index+1])
    else:
        from synaeris_collector.human import load_preferences
        human_config, startup_note = load_preferences()
        App(human_config, startup_note=startup_note).run()
