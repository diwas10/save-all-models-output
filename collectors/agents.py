"""Import local transcripts of coding agents into the shared database.

  claude_code  ~/.claude/projects/**/*.jsonl              (incremental, by byte offset)
  codex        ~/.codex/sessions/**/*.jsonl + archived     (incremental, by byte offset)
  antigravity  ~/.gemini/antigravity/conversations/*.db    (protobuf steps, re-read on change)
               + conversation_summaries.db search index    (text of older, encrypted .pb chats)
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pbtext

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude" / "projects"
CODEX_DIRS = [HOME / ".codex" / "sessions", HOME / ".codex" / "archived_sessions"]
AGY_DIR = HOME / ".gemini" / "antigravity"


# ---- incremental JSONL reading ---------------------------------------------

def new_lines(con, path: Path):
    """Yield JSON objects appended to `path` since the last run."""
    st = path.stat()
    row = con.execute("SELECT size, offset FROM collector_state WHERE path=?", (str(path),)).fetchone()
    offset = row[1] if row and st.st_size >= row[0] else 0  # shrank -> rewritten -> start over
    if row and st.st_size == row[0]:
        return
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n") + 1  # only complete lines; a partial last line is read next time
    for line in data[:end].splitlines():
        try:
            yield json.loads(line)
        except ValueError:
            continue
    con.execute(
        "INSERT OR REPLACE INTO collector_state (path, mtime, size, offset) VALUES (?,?,?,?)",
        (str(path), st.st_mtime, offset + end, offset + end),
    )


def _s(v) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, list):  # list of content blocks
        return "\n".join(b.get("text", "") if isinstance(b, dict) and b.get("type") == "text" else json.dumps(b) for b in v)
    return json.dumps(v)


# ---- Claude Code ------------------------------------------------------------

def claude_code(con, store) -> int:
    total = 0
    for path in sorted(CLAUDE_DIR.rglob("*.jsonl")):
        sub = path.parent.name == "subagents"
        rows, conv = [], None
        for e in new_lines(con, path):
            if e.get("type") not in ("user", "assistant") or not isinstance(e.get("message"), dict):
                continue
            sid = e.get("sessionId") or path.stem
            cid = f"{sid}/{path.stem}" if sub else sid
            if conv is None or conv["id"] != cid:
                if conv and rows:
                    total += store.write_conversation(con, "claude_code", conv, rows, "transcript")
                rows = []
                conv = {"id": cid, "project": e.get("cwd"), "updated_at": e.get("timestamp"),
                        "title": "subagent" if sub else None}
            conv["updated_at"] = e.get("timestamp") or conv["updated_at"]
            m = e["message"]
            content = m.get("content")
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
            for i, b in enumerate(blocks):
                t = b.get("type")
                role = "tool" if t == "tool_result" else (m.get("role") or e["type"])
                if t == "text":
                    kind, text = "text", b.get("text")
                elif t in ("thinking", "redacted_thinking"):
                    kind, text = "thinking", b.get("thinking") or b.get("data")
                elif t == "tool_use":
                    kind, text = "tool_call", json.dumps({"name": b.get("name"), "input": b.get("input"), "id": b.get("id")})
                elif t == "tool_result":
                    kind, text = "tool_result", _s(b.get("content"))
                elif t == "image":
                    kind, text = "other", "[image]"
                else:
                    kind, text = "other", json.dumps(b)
                if text:
                    rows.append({"message_id": f"{e.get('uuid')}#{i}", "parent_id": e.get("parentUuid"),
                                 "role": role, "kind": kind, "content": text, "model": m.get("model"),
                                 "created_at": e.get("timestamp")})
        if conv and rows:
            total += store.write_conversation(con, "claude_code", conv, rows, "transcript")
        con.commit()
    return total


# ---- Codex ------------------------------------------------------------------

def codex(con, store) -> int:
    total = 0
    for d in CODEX_DIRS:
        for path in sorted(d.rglob("*.jsonl")) if d.exists() else []:
            conv = {"id": path.stem}
            rows = []
            for n, e in enumerate(new_lines(con, path)):
                p = e.get("payload") or {}
                ts = e.get("timestamp")
                if e.get("type") == "session_meta":
                    conv.update(id=p.get("id") or conv["id"], project=p.get("cwd"), updated_at=ts)
                    continue
                if e.get("type") == "turn_context":
                    conv["model"] = p.get("model") or conv.get("model")
                    continue
                if e.get("type") != "response_item":
                    continue
                t = p.get("type")
                if t == "message":
                    role, kind = p.get("role"), "text"
                    text = "\n".join(c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict))
                elif t == "reasoning":
                    role, kind = "assistant", "thinking"
                    text = "\n".join(s.get("text", "") for s in p.get("summary") or [])  # raw reasoning is encrypted
                elif t in ("function_call", "custom_tool_call", "local_shell_call", "web_search_call"):
                    role, kind = "assistant", "tool_call"
                    text = json.dumps({"name": p.get("name") or t, "input": p.get("arguments") or p.get("input") or p.get("action"),
                                       "id": p.get("call_id")})
                elif t in ("function_call_output", "custom_tool_call_output"):
                    role, kind, text = "tool", "tool_result", _s(p.get("output"))
                else:
                    role, kind, text = "assistant", "other", json.dumps(p)
                if text and text.strip():
                    rows.append({"message_id": f"{path.name}:{ts}:{n}", "role": role, "kind": kind,
                                 "content": text, "created_at": ts})
                conv["updated_at"] = ts or conv.get("updated_at")
            if rows:
                total += store.write_conversation(con, "codex", conv, rows, "transcript")
            con.commit()
    return total


# ---- Antigravity --------------------------------------------------------------
# Steps are protobuf with no public schema. Field numbers below were worked out
# from real data: 14 = user input (field 19), 15 = model response (field 20:
# .1 text, .3 thinking), tool steps carry name/args in 5.4.2 / 5.4.3 and output
# in their own top-level field. Fixed system reminders (90, 98) are skipped.
SKIP_STEP_TYPES = {90, 98}


def _copy_db(path: Path, tmp: Path) -> Path:
    """Antigravity holds its databases open; read a private copy (+WAL)."""
    dst = tmp / path.name
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(path) + suffix)
        if src.exists():
            shutil.copy2(src, str(dst) + suffix)
    return dst


def _changed(con, path: Path) -> bool:
    paths = [p for p in (path, Path(str(path) + "-wal")) if p.exists()]
    mtime = max(p.stat().st_mtime for p in paths)
    size = sum(p.stat().st_size for p in paths)
    row = con.execute("SELECT mtime, size FROM collector_state WHERE path=?", (str(path),)).fetchone()
    if row and row[0] == mtime and row[1] == size:
        return False
    con.execute("INSERT OR REPLACE INTO collector_state (path, mtime, size, offset) VALUES (?,?,?,0)",
                (str(path), mtime, size))
    return True


def _step_time(items) -> str | None:
    for f, v in items:
        if f == 5 and isinstance(v, list):
            for f2, v2 in v:
                if f2 == 1 and isinstance(v2, list):
                    secs = dict(v2).get(1)
                    if isinstance(secs, int) and secs > 1_500_000_000:
                        return datetime.fromtimestamp(secs, timezone.utc).isoformat()
    return None


def _agy_step(idx: int, step_type: int, payload: bytes) -> list[dict]:
    try:
        items = pbtext.decode(payload)
    except ValueError:
        return []
    ts = _step_time(items)
    strings = pbtext.strings(payload, 1)
    body = [(p, s) for p, s in strings if not p.startswith("5.")]

    def first(prefix):
        return next((s for p, s in body if p == prefix), None)

    rows = []
    if step_type == 14:
        text = first("19.2")
        if text:
            rows.append({"role": "user", "kind": "text", "content": text})
    elif step_type == 15:
        if first("20.3"):
            rows.append({"role": "assistant", "kind": "thinking", "content": first("20.3")})
        if first("20.1"):
            rows.append({"role": "assistant", "kind": "text", "content": first("20.1")})
    else:
        name = next((s for p, s in strings if p == "5.4.2"), None)
        args = next((s for p, s in strings if p == "5.4.3"), None)
        if name:
            rows.append({"role": "assistant", "kind": "tool_call",
                         "content": json.dumps({"name": name, "input": args})})
        # tool output: the longest readable strings outside the metadata field
        out = [s for p, s in body if len(s.strip()) > 1]
        if out:
            rows.append({"role": "tool", "kind": "tool_result",
                         "content": "\n".join(dict.fromkeys(out))[:200_000]})
    for i, r in enumerate(rows):
        r.update(message_id=f"step{idx}#{i}", created_at=ts)
        r["content"] = r["content"].strip()
    return [r for r in rows if r["content"]]


def antigravity(con, store) -> int:
    conv_dir = AGY_DIR / "conversations"
    if not conv_dir.exists():
        return 0
    total = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        titles, projects = {}, {}
        summ = AGY_DIR / "conversation_summaries.db"
        sdb = None
        if summ.exists():
            sdb = sqlite3.connect(_copy_db(summ, tmp))
            for cid, title, ws in sdb.execute("SELECT conversation_id, title, workspace_uris FROM conversation_summaries"):
                titles[cid], projects[cid] = title or None, ws or None

        have_steps = set()
        for db in sorted(conv_dir.glob("*.db")):
            have_steps.add(db.stem)
            if not _changed(con, db):
                continue
            try:
                src = sqlite3.connect(_copy_db(db, tmp))
                rows = []
                for idx, st, payload in src.execute("SELECT idx, step_type, step_payload FROM steps ORDER BY idx"):
                    if st not in SKIP_STEP_TYPES and payload:
                        rows += _agy_step(idx, st, payload)
                src.close()
            except sqlite3.DatabaseError:
                continue
            conv = {"id": db.stem, "title": titles.get(db.stem), "project": projects.get(db.stem),
                    "updated_at": next((r["created_at"] for r in reversed(rows) if r.get("created_at")), None)}
            con.execute("DELETE FROM messages WHERE source='antigravity' AND conversation_id=?", (db.stem,))
            total += store.write_conversation(con, "antigravity", conv, rows, "transcript")
            con.commit()

        # Older chats only exist as encrypted .pb files; their text survives in
        # Antigravity's own search index, chunked into user / agent bodies.
        if sdb is not None and _changed(con, summ):
            q = """SELECT conversation_id, chunk_index, last_modified_time, user_body, agent_body
                   FROM conversations_fts ORDER BY conversation_id, chunk_index"""
            by_conv: dict[str, list] = {}
            for cid, chunk, mtime, ub, ab in sdb.execute(q):
                if cid in have_steps:
                    continue
                r = by_conv.setdefault(cid, [])
                if ub:
                    r.append({"message_id": f"index{chunk}#user", "role": "user", "kind": "text", "content": ub, "created_at": mtime})
                if ab:
                    r.append({"message_id": f"index{chunk}#agent", "role": "assistant", "kind": "text", "content": ab, "created_at": mtime})
            for cid, rows in by_conv.items():
                if not rows:
                    continue
                conv = {"id": cid, "title": titles.get(cid), "project": projects.get(cid), "updated_at": rows[-1]["created_at"]}
                total += store.write_conversation(con, "antigravity", conv, rows, "search-index")
            con.commit()
        if sdb is not None:
            sdb.close()
    return total


COLLECTORS = {"claude_code": claude_code, "codex": codex, "antigravity": antigravity}
