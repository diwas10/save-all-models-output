#!/usr/bin/env python3
"""Daily self-update: fast-forward to the latest code on GitHub and, if it
changed, re-run setup so every machine stays in sync. Never overwrites local
edits (a fast-forward that isn't possible is simply skipped)."""
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True)


def main():
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    before = git("rev-parse", "HEAD").stdout.strip()
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
    if git("fetch", "-q", "origin", branch).returncode or git("merge", "--ff-only", "-q", "FETCH_HEAD").returncode:
        print(f"{stamp}: update skipped (offline, or local changes that can't fast-forward)")
        return
    after = git("rev-parse", "HEAD").stdout.strip()
    if before == after:
        print(f"{stamp}: up to date")
        return
    print(f"{stamp}: updated {before[:7]} -> {after[:7]}, re-running setup", flush=True)
    subprocess.run([sys.executable, str(REPO / "scripts" / "setup.py"), "--from-updater"])


if __name__ == "__main__":
    main()
