#!/usr/bin/env python3
"""Cross-platform setup (macOS, Linux, Windows). Safe to run again.

  python3 scripts/setup.py              # install / update everything
  python3 scripts/setup.py --no-proxy   # skip the macOS desktop-app proxy
  python3 scripts/setup.py --uninstall  # remove jobs (data is kept)
  python3 scripts/setup.py --dry-run    # show what would be done

Background jobs (all run `python scripts/job.py <name>`):
  server   always on    receives browser-extension captures on 127.0.0.1:8765
  collect  every 10 min imports Claude Code / Codex / Antigravity, writes the
                        snapshot to Documents/local-data
  update   daily        git fast-forward + re-run setup when the code changed

Schedulers: macOS launchd · Linux systemd --user (cron if no systemd) ·
Windows Task Scheduler + Startup folder (no admin rights needed).
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOME = Path.home()
DATA = Path(os.environ.get("AI_HISTORY_DIR", HOME / "AIHistory"))
JOB = REPO / "scripts" / "job.py"
ARGS = set(sys.argv[1:])
DRY = "--dry-run" in ARGS
FROM_UPDATER = "--from-updater" in ARGS
PLATFORM = os.environ.get("AI_HISTORY_PLATFORM") or ("windows" if os.name == "nt" else sys.platform)
JOBS = [("server", None), ("collect", 600), ("update", 86400)]  # (name, interval seconds | None = always on)


def python_exe() -> str:
    exe = sys.executable
    if PLATFORM == "windows":  # pythonw = no console window popping up
        w = Path(exe).with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return exe


def run(cmd, check=False, **kw):
    if DRY:
        print("  $", " ".join(map(str, cmd)))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return subprocess.run(list(map(str, cmd)), check=check, capture_output=True, text=True, **kw)


def write(path: Path, text: str):
    if DRY:
        print(f"  write {path}:\n" + "".join(f"    | {line}\n" for line in text.splitlines()))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def remove(path: Path):
    if DRY:
        print(f"  rm {path}")
    elif path.exists():
        path.unlink()


def stop_server():
    """Stop a server started without a service manager (Windows, cron)."""
    pidfile = DATA / "server.pid"
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return
    if DRY:
        print(f"  stop server pid {pid}")
        return
    if PLATFORM == "windows":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    pidfile.unlink(missing_ok=True)


# ---- macOS: launchd -----------------------------------------------------------

def mac_label(name):
    return f"local.aihistory.{name}"


def mac_install(name, interval):
    label = mac_label(name)
    plist = HOME / "Library" / "LaunchAgents" / f"{label}.plist"
    sched = "<key>KeepAlive</key><true/>" if interval is None else f"<key>StartInterval</key><integer>{interval}</integer>"
    write(plist, f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array><string>{python_exe()}</string><string>{JOB}</string><string>{name}</string></array>
  <key>RunAtLoad</key><true/>
  {sched}
  <key>LowPriorityIO</key><true/>
</dict>
</plist>
""")
    uid = os.getuid()
    run(["launchctl", "bootout", f"gui/{uid}/{label}"])
    run(["launchctl", "bootstrap", f"gui/{uid}", plist], check=not DRY)


def mac_uninstall(name):
    run(["launchctl", "bootout", f"gui/{os.getuid()}/{mac_label(name)}"])
    remove(HOME / "Library" / "LaunchAgents" / f"{mac_label(name)}.plist")


# ---- Linux: systemd --user, else cron --------------------------------------------

SYSTEMD_DIR = HOME / ".config" / "systemd" / "user"
CRON_TAG = "# ai-history"


def has_systemd() -> bool:
    if not shutil.which("systemctl"):
        return False
    r = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True)
    return r.stdout.strip() in ("running", "degraded", "starting")


def linux_install_all(jobs):
    if has_systemd() or DRY:
        for name, interval in jobs:
            unit = f"ai-history-{name}"
            service = f"[Unit]\nDescription=AI history {name}\n\n[Service]\nExecStart=\"{python_exe()}\" \"{JOB}\" {name}\n"
            if interval is None:
                write(SYSTEMD_DIR / f"{unit}.service", service + "Restart=always\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n")
            else:
                write(SYSTEMD_DIR / f"{unit}.service", service + "Type=oneshot\n")
                write(SYSTEMD_DIR / f"{unit}.timer",
                      f"[Unit]\nDescription=AI history {name} timer\n\n[Timer]\nOnBootSec=60\nOnUnitActiveSec={interval}\n"
                      "Persistent=true\n\n[Install]\nWantedBy=timers.target\n")
        run(["systemctl", "--user", "daemon-reload"])
        for name, interval in jobs:
            unit = f"ai-history-{name}." + ("service" if interval is None else "timer")
            run(["systemctl", "--user", "enable", "--now", unit])
            if interval is None:
                run(["systemctl", "--user", "restart", unit])
        # keep jobs running when you're not logged in (agent / headless machines)
        run(["loginctl", "enable-linger", os.environ.get("USER", "")])
        return
    # cron fallback
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    lines = [ln for ln in cur.splitlines() if CRON_TAG not in ln]
    py = python_exe()
    entries = {"server": f"@reboot {py} {JOB} server", "collect": f"*/10 * * * * {py} {JOB} collect",
               "update": f"17 3 * * * {py} {JOB} update"}
    lines += [f"{entries[n]} {CRON_TAG}" for n, _ in jobs]
    run(["crontab", "-"], input="\n".join(lines) + "\n")
    if any(n == "server" for n, _ in jobs) and not DRY:  # start it now, not only after reboot
        stop_server()
        subprocess.Popen([py, str(JOB), "server"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def linux_uninstall_all(names):
    if has_systemd():
        for n in names:
            for kind in ("timer", "service"):
                run(["systemctl", "--user", "disable", "--now", f"ai-history-{n}.{kind}"])
                remove(SYSTEMD_DIR / f"ai-history-{n}.{kind}")
        run(["systemctl", "--user", "daemon-reload"])
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    if CRON_TAG in cur:
        run(["crontab", "-"], input="\n".join(ln for ln in cur.splitlines() if CRON_TAG not in ln) + "\n")
    stop_server()


# ---- Windows: Startup folder (server) + Task Scheduler (timed jobs) ----------------

def win_startup() -> Path:
    return Path(os.environ.get("APPDATA", HOME)) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def win_install(name, interval):
    py = python_exe()
    if interval is None:
        # ONLOGON tasks need admin; a Startup-folder script doesn't.
        write(win_startup() / f"ai-history-{name}.cmd", f'@start "" "{py}" "{JOB}" {name}\r\n')
        stop_server()
        if not DRY:
            subprocess.Popen([py, str(JOB), name], creationflags=0x00000008 | 0x00000200)  # detached
        return
    tr = f'"{py}" "{JOB}" {name}'
    if interval < 3600:
        sched = ["/SC", "MINUTE", "/MO", str(interval // 60)]
    else:
        sched = ["/SC", "DAILY", "/ST", "03:17"]
    run(["schtasks", "/Create", "/F", "/TN", f"AIHistory\\{name}", "/TR", tr, *sched], check=not DRY)
    run(["schtasks", "/Run", "/TN", f"AIHistory\\{name}"])


def win_uninstall(name):
    remove(win_startup() / f"ai-history-{name}.cmd")
    if name == "server":
        stop_server()
    run(["schtasks", "/Delete", "/F", "/TN", f"AIHistory\\{name}"])


# ---- macOS desktop-app proxy ---------------------------------------------------------

def mac_proxy():
    marker = REPO / ".no-proxy"
    if "--no-proxy" in ARGS:
        marker.touch()
    if marker.exists():
        print("desktop proxy: skipped (--no-proxy)")
        return
    if not shutil.which("mitmdump"):
        if FROM_UPDATER:
            return
        if shutil.which("brew"):
            print("installing mitmproxy with Homebrew …")
            if not DRY:
                subprocess.run(["brew", "install", "mitmproxy"])
        else:
            print("desktop proxy: skipped (Homebrew not found: install it from https://brew.sh and re-run setup)")
            return
    env = dict(os.environ, AIHISTORY_FROM_UPDATER="1" if FROM_UPDATER else "")
    if DRY:
        print(f"  $ bash {REPO / 'desktop' / 'install.sh'}")
    else:
        subprocess.run(["bash", str(REPO / "desktop" / "install.sh")], env=env)


# ---- main ---------------------------------------------------------------------------

def install():
    jobs = [j for j in JOBS if not (FROM_UPDATER and j[0] == "update")]  # don't kill the running updater
    print(f"== Background jobs ({PLATFORM})")
    if PLATFORM == "darwin":
        for name, interval in jobs:
            mac_install(name, interval)
    elif PLATFORM.startswith("linux"):
        linux_install_all(jobs)
    elif PLATFORM == "windows":
        for name, interval in jobs:
            win_install(name, interval)
    else:
        sys.exit(f"Unsupported platform: {PLATFORM}")
    print("   ✓ " + ", ".join(n for n, _ in jobs))

    print("== Browser extension")
    if not DRY:
        subprocess.run([sys.executable, str(REPO / "extension" / "build.py")], check=True, capture_output=True)
    print(f"   ✓ built {REPO / 'extension' / 'dist'}")

    if PLATFORM == "darwin":
        print("== Desktop-app proxy (macOS)")
        mac_proxy()

    if FROM_UPDATER:
        return
    sys.path.insert(0, str(REPO / "collectors"))
    sys.path.insert(0, str(REPO / "server"))
    import run as collector  # noqa: E402
    print(f"""
────────────────────────────────────────────────────────────
✓ Done. Everything runs in the background and starts at login.

Your data:  {collector.snapshot_path()}
            (refreshed at most hourly; upload it wherever you like)

Browsers don't let scripts install extensions; load it once (30 seconds):
  Chrome:  chrome://extensions → Developer mode → Load unpacked →
           {REPO / 'extension' / 'dist' / 'chrome'}
  Firefox: about:debugging → This Firefox → Load Temporary Add-on →
           {REPO / 'extension' / 'dist' / 'firefox' / 'manifest.json'}
────────────────────────────────────────────────────────────""")


def uninstall():
    names = [n for n, _ in JOBS]
    if PLATFORM == "darwin":
        if (REPO / "desktop" / "uninstall.sh").exists():
            run(["bash", REPO / "desktop" / "uninstall.sh"])
        for n in names:
            mac_uninstall(n)
    elif PLATFORM.startswith("linux"):
        linux_uninstall_all(names)
    elif PLATFORM == "windows":
        for n in names:
            win_uninstall(n)
    print(f"Removed background jobs. Your data is kept in {DATA} and Documents/local-data.")


if __name__ == "__main__":
    if sys.version_info < (3, 9):
        sys.exit("Python 3.9 or newer is required.")
    uninstall() if "--uninstall" in ARGS else install()
