#!/usr/bin/env python3
"""Entry point for the background jobs on every OS (launchd, systemd, cron,
Windows Task Scheduler all just run `python scripts/job.py <name>`).
Output goes to ~/AIHistory/logs/<name>.log."""
import os
import runpy
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
JOBS = {
    "server": [REPO / "server" / "server.py"],
    "collect": [REPO / "collectors" / "run.py", "--scheduled"],
    "update": [REPO / "scripts" / "update.py"],
}
LOG_DIR = Path(os.environ.get("AI_HISTORY_DIR", Path.home() / "AIHistory")) / "logs"
MAX_LOG = 5 * 1024 * 1024
DATA_DIR = LOG_DIR.parent


def loop():
    """Used only on Linux machines with neither systemd nor cron: runs collect
    every 10 minutes and update daily, each as a fresh process."""
    import subprocess
    import time
    (DATA_DIR / "loop.pid").write_text(str(os.getpid()))
    last_update = 0.0
    while True:
        subprocess.run([sys.executable, __file__, "collect"])
        if time.time() - last_update > 86400:
            subprocess.run([sys.executable, __file__, "update"])
            last_update = time.time()
        time.sleep(600)


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    if name == "loop":
        return loop()
    if name not in JOBS:
        sys.exit(f"usage: job.py {{{','.join(JOBS)}}}")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / f"{name}.log"
    if log.exists() and log.stat().st_size > MAX_LOG:
        log.replace(log.with_suffix(".log.1"))
    out = open(log, "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = out
    script, *args = JOBS[name]
    sys.argv = [str(script), *args]
    sys.path.insert(0, str(Path(script).parent))
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
