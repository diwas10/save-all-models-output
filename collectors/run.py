#!/usr/bin/env python3
"""Import every local source into the database, then write one consistent
snapshot file to the Google-Drive-synced folder.

  python3 collectors/run.py                 # collect + snapshot now
  python3 collectors/run.py --scheduled     # collect; snapshot only if data changed
                                            #   and the last one is over an hour old
  python3 collectors/run.py --no-snapshot   # collect only

Live database (written by server, proxy, collectors):  ~/AIHistory/ai_history.sqlite
Snapshot (single file, for you to upload / back up):  Documents/local-data/ai_history-<machine>.sqlite

Several machines: each writes only its own ai_history-<machine>.sqlite and
imports any other machines' files you copy into the same folder, so one
file can end up with the history of all of them.
"""
import os
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "server"))
import agents  # noqa: E402
import exports  # noqa: E402
import store  # noqa: E402

def documents_dir() -> Path:
    """The user's Documents folder on macOS, Linux and Windows (including
    Windows setups where OneDrive has moved Documents)."""
    if os.name == "nt":
        try:
            import winreg
            key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                return Path(os.path.expandvars(winreg.QueryValueEx(k, "Personal")[0]))
        except OSError:
            pass
    elif sys.platform.startswith("linux"):
        try:
            import subprocess
            out = subprocess.run(["xdg-user-dir", "DOCUMENTS"], capture_output=True, text=True).stdout.strip()
            if out and Path(out) != Path.home():
                return Path(out)
        except OSError:
            pass
    return Path.home() / "Documents"


EXPORT_DIR = Path(os.environ.get("AI_HISTORY_EXPORT_DIR") or documents_dir() / "local-data")
SNAPSHOT_EVERY = int(os.environ.get("AI_HISTORY_SNAPSHOT_EVERY", 3600))  # seconds


def fingerprint(con) -> str:
    q = """SELECT (SELECT COUNT(*) || ':' || IFNULL(MAX(id), 0) FROM messages) || '|' ||
                  (SELECT COUNT(*) || ':' || IFNULL(MAX(id), 0) FROM captures) || '|' ||
                  (SELECT IFNULL(MAX(updated_at), '') FROM conversations)"""
    return con.execute(q).fetchone()[0]


def snapshot_due(con) -> bool:
    row = con.execute("SELECT mtime, path FROM collector_state WHERE path LIKE 'snapshot:%'").fetchone()
    if not row or not snapshot_path().exists():
        return True
    last_time, last_fp = row[0], row[1][len("snapshot:"):]
    return last_fp != fingerprint(con) and time.time() - last_time >= SNAPSHOT_EVERY


def snapshot_path() -> Path:
    return EXPORT_DIR / f"ai_history-{store.MACHINE}.sqlite"


def import_peers(con) -> int:
    """Merge other laptops' snapshots (found in the synced folder) into ours."""
    total = 0
    for peer in sorted(EXPORT_DIR.glob("ai_history-*.sqlite")):
        if peer == snapshot_path() or not agents._changed(con, peer):
            continue
        try:
            con.execute("ATTACH DATABASE ? AS peer", (f"file:{peer}?mode=ro",))
        except sqlite3.OperationalError:
            con.execute("ATTACH DATABASE ? AS peer", (str(peer),))
        try:
            pcols = lambda t: {r[1] for r in con.execute(f"PRAGMA peer.table_info({t})")}
            m = "machine" if "machine" in pcols("conversations") else "NULL"
            con.execute(f"""INSERT OR IGNORE INTO conversations
                (source, conversation_id, title, model, updated_at, project, machine)
                SELECT source, conversation_id, title, model, updated_at, project, {m} FROM peer.conversations""")
            cm = "machine" if "machine" in pcols("captures") else "NULL"
            con.execute(f"""INSERT OR IGNORE INTO captures
                (source, channel, method, url, status, page_url, captured_at, request_body, response_body, sha256, parsed, machine)
                SELECT source, channel, method, url, status, page_url, captured_at, request_body, response_body, sha256, parsed, {cm}
                FROM peer.captures""")
            # capture ids differ between databases, so imported rows don't point at one.
            cur = con.execute("""INSERT OR IGNORE INTO messages
                (source, conversation_id, message_id, parent_id, role, kind, content, model, created_at, origin, capture_id)
                SELECT source, conversation_id, message_id, parent_id, role, kind, content, model, created_at, origin, NULL
                FROM peer.messages""")
            total += max(cur.rowcount, 0)
            con.commit()
        finally:
            con.execute("DETACH DATABASE peer")
    return total


def snapshot(con: sqlite3.Connection) -> Path:
    """Copy the live DB with SQLite's online backup API (safe while other
    processes write), into a temp file, then atomically swap it in, so Google
    Drive never uploads a half-written file."""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    final = snapshot_path()
    tmp = EXPORT_DIR / f".{final.name}.tmp"
    tmp.unlink(missing_ok=True)
    dst = sqlite3.connect(tmp)
    con.backup(dst)
    dst.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal
    dst.close()
    os.chmod(tmp, 0o600)
    os.replace(tmp, final)
    con.execute("DELETE FROM collector_state WHERE path LIKE 'snapshot:%'")
    con.execute("INSERT INTO collector_state (path, mtime, size, offset) VALUES (?,?,?,0)",
                ("snapshot:" + fingerprint(con), time.time(), final.stat().st_size))
    con.commit()
    return final


def main():
    con = store.connect()
    for name, fn in agents.COLLECTORS.items():
        t = time.time()
        try:
            n = fn(con, store)
            print(f"{name:<12} {n:>7} rows   ({time.time() - t:.1f}s)", flush=True)
        except Exception as e:  # one broken source must not stop the others
            con.rollback()
            print(f"{name:<12} ERROR {type(e).__name__}: {e}", flush=True)
    if EXPORT_DIR.exists():
        try:
            print(f"{'other laptops':<12} {import_peers(con):>7} rows", flush=True)
        except Exception as e:
            con.rollback()
            print(f"other laptops ERROR {type(e).__name__}: {e}", flush=True)
        try:
            print(f"{'export zips':<12} {exports.import_dropped(con, EXPORT_DIR):>7} rows", flush=True)
        except Exception as e:
            con.rollback()
            print(f"export zips ERROR {type(e).__name__}: {e}", flush=True)
    if "--no-snapshot" in sys.argv:
        return
    if "--scheduled" in sys.argv and not snapshot_due(con):
        print("snapshot: skipped (unchanged or written within the last hour)")
        return
    path = snapshot(con)
    print(f"snapshot -> {path} ({path.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
