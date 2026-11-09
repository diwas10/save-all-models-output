"""Storage + parsing shared by the HTTP server (browser extension) and the
mitmproxy addon (desktop apps).

Two layers:
  captures  - every raw request/response exactly as seen. Never thrown away,
              so parsers can be improved later and re-run (`reparse`).
  messages  - normalised rows: one per text / thinking / tool call / tool
              result block, parsed from captures where the format is known.

Standard library only, so mitmproxy's bundled Python can import it too.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

DATA_DIR = Path(os.environ.get("AI_HISTORY_DIR", Path.home() / "AIHistory"))
DB_PATH = DATA_DIR / "ai_history.sqlite"
# Which laptop a row came from (all laptops end up merged into every copy).
MACHINE = os.environ.get("AI_HISTORY_MACHINE") or re.sub(r"[^A-Za-z0-9-]+", "-", platform.node().split(".")[0]) or "mac"

SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,          -- chatgpt | claude | gemini | ...
    channel       TEXT NOT NULL,          -- browser-extension | desktop-proxy
    method        TEXT,
    url           TEXT,
    status        INTEGER,
    page_url      TEXT,
    captured_at   TEXT NOT NULL,
    request_body  TEXT,
    response_body TEXT,
    sha256        TEXT NOT NULL UNIQUE,
    parsed        INTEGER NOT NULL DEFAULT 0,  -- rows produced in messages
    machine       TEXT
);
CREATE TABLE IF NOT EXISTS conversations (
    source          TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    title           TEXT,
    model           TEXT,
    updated_at      TEXT,
    project         TEXT,                 -- working directory / workspace (coding agents)
    machine         TEXT,                 -- laptop it happened on
    PRIMARY KEY (source, conversation_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    message_id      TEXT NOT NULL,        -- block id, unique within conversation
    parent_id       TEXT,
    role            TEXT,                 -- user | assistant | tool | system
    kind            TEXT,                 -- text | thinking | tool_call | tool_result | other
    content         TEXT,
    model           TEXT,
    created_at      TEXT,
    origin          TEXT,                 -- full | stream (browser/proxy) | transcript | search-index (local files)
    capture_id      INTEGER REFERENCES captures(id),
    UNIQUE (source, conversation_id, message_id)
);
CREATE INDEX IF NOT EXISTS messages_conv ON messages(source, conversation_id, created_at);
CREATE TABLE IF NOT EXISTS collector_state (   -- remembers which local files were imported
    path   TEXT PRIMARY KEY,
    mtime  REAL,
    size   INTEGER,
    offset INTEGER
);
"""


def connect(check_same_thread: bool = True) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)  # chats are private: owner-only
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=check_same_thread)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(SCHEMA)
    # Columns added after the first release:
    for table, col in (("conversations", "project"), ("conversations", "machine"), ("captures", "machine")):
        if col not in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:
            try:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError as e:  # another process added it first
                if "duplicate column" not in str(e):
                    raise
                continue
            if col == "machine":  # rows that existed before were made on this Mac
                con.execute(f"UPDATE {table} SET machine=?", (MACHINE,))
    con.commit()
    return con


def write_conversation(con, source: str, conv: dict, rows: list[dict], origin: str,
                       capture_id: int | None = None, default_time: str | None = None) -> int:
    """Upsert a conversation and its message rows. Shared by every source."""
    con.execute(
        """INSERT INTO conversations (source, conversation_id, title, model, updated_at, project, machine)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(source, conversation_id) DO UPDATE SET
             title=COALESCE(excluded.title, title), model=COALESCE(excluded.model, model),
             updated_at=COALESCE(excluded.updated_at, updated_at), project=COALESCE(excluded.project, project)""",
        (source, conv["id"], conv.get("title"), conv.get("model"), conv.get("updated_at"), conv.get("project"),
         conv.get("machine") or MACHINE),
    )
    for r in rows:
        con.execute(
            """INSERT INTO messages (source, conversation_id, message_id, parent_id, role, kind, content, model,
                                     created_at, origin, capture_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source, conversation_id, message_id) DO UPDATE SET
                 parent_id=excluded.parent_id, role=excluded.role, kind=excluded.kind, content=excluded.content,
                 model=COALESCE(excluded.model, model), created_at=COALESCE(excluded.created_at, created_at),
                 origin=excluded.origin, capture_id=excluded.capture_id""",
            (
                source, conv["id"], r["message_id"], r.get("parent_id"), r.get("role"), r.get("kind"),
                r.get("content"), r.get("model") or conv.get("model"), r.get("created_at") or default_time,
                origin, capture_id,
            ),
        )
    return len(rows)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_capture(con: sqlite3.Connection, rec: dict, channel: str) -> tuple[int | None, int]:
    """Store one raw capture and parse it. Returns (capture_id, messages_written).
    capture_id is None when this exact capture was already stored."""
    digest = hashlib.sha256(
        json.dumps([rec.get("method"), rec.get("url"), rec.get("request_body"), rec.get("response_body")]).encode()
    ).hexdigest()
    cur = con.execute(
        """INSERT OR IGNORE INTO captures
           (source, channel, method, url, status, page_url, captured_at, request_body, response_body, sha256, machine)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            rec.get("source") or "unknown", channel, rec.get("method"), rec.get("url"), rec.get("status"),
            rec.get("page_url"), rec.get("captured_at") or now(), rec.get("request_body"),
            rec.get("response_body"), digest, MACHINE,
        ),
    )
    if cur.rowcount == 0:
        con.commit()
        return None, 0
    cap_id = cur.lastrowid
    n = parse_capture(con, cap_id)
    con.commit()
    return cap_id, n


# --------------------------------------------------------------------------
# Parsing. These target the *internal* web formats of each site, which are
# undocumented and change. Every parser is best-effort and never raises;
# raw captures are always kept so `python3 server/server.py reparse` can
# rebuild the messages table after a parser is fixed.
# --------------------------------------------------------------------------

def parse_capture(con: sqlite3.Connection, cap_id: int) -> int:
    row = con.execute(
        "SELECT source, method, url, request_body, response_body, captured_at FROM captures WHERE id=?", (cap_id,)
    ).fetchone()
    if not row:
        return 0
    source, method, url, req, res, captured_at = row
    path = urlparse(url or "").path
    rows: list[dict] = []
    conv: dict | None = None
    full = False
    try:
        if source == "chatgpt":
            if method == "GET" and re.fullmatch(r"/backend-api/conversation/[0-9a-f-]+/?", path):
                conv, rows = chatgpt_full(json.loads(res))
                full = True
            elif method == "POST" and re.fullmatch(r"/backend-api/(f/)?conversation/?", path):
                conv, rows = chatgpt_stream(req, res)
        elif source == "claude":
            m = re.search(r"/chat_conversations/([^/?]+)", path)
            cid = m.group(1) if m else None
            if method == "GET" and cid and not path.rstrip("/").endswith(("completion", "retry_completion")):
                conv, rows = claude_full(json.loads(res))
                full = True
            elif method == "POST" and cid and path.rstrip("/").endswith(("completion", "retry_completion")):
                conv, rows = claude_stream(cid, req, res, cap_id)
        # gemini: raw only for now (nested batchexecute arrays); parser to be
        # written against real captures.
    except Exception:
        rows = []
    if not rows or not conv:
        return 0

    if full:
        # A saved conversation is authoritative: drop rows we built from live streams.
        con.execute("DELETE FROM messages WHERE source=? AND conversation_id=? AND origin='stream'", (source, conv["id"]))
    conv.setdefault("updated_at", captured_at)
    write_conversation(con, source, conv, rows, "full" if full else "stream", cap_id, default_time=captured_at)
    con.execute("UPDATE captures SET parsed=? WHERE id=?", (len(rows), cap_id))
    return len(rows)


def _ts(v) -> str | None:
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, timezone.utc).isoformat()
    return v if isinstance(v, str) else None


def _sse_events(text: str):
    for line in (text or "").splitlines():
        if line.startswith("data:"):
            data = line[5:].strip()
            if data and data != "[DONE]":
                try:
                    yield json.loads(data)
                except ValueError:
                    pass


# ---- ChatGPT --------------------------------------------------------------

_CHATGPT_KIND = {
    "text": "text", "multimodal_text": "text",
    "thoughts": "thinking", "reasoning_recap": "thinking",
    "code": "tool_call", "execution_output": "tool_result",
    "tether_browsing_display": "tool_result", "tether_quote": "tool_result",
}


def _chatgpt_message(msg: dict, parent: str | None = None) -> dict | None:
    content = msg.get("content") or {}
    ctype = content.get("content_type", "")
    if "parts" in content:
        text = "\n".join(p if isinstance(p, str) else json.dumps(p) for p in content["parts"] or [])
    elif ctype == "thoughts":
        text = "\n\n".join(
            "\n".join(x for x in (t.get("summary"), t.get("content")) if x) for t in content.get("thoughts", [])
        )
    elif "text" in content:
        text = content["text"]
    else:
        text = json.dumps(content) if content else ""
    if not text or not text.strip():
        return None
    role = (msg.get("author") or {}).get("role")
    meta = msg.get("metadata") or {}
    if role == "system" or meta.get("is_visually_hidden_from_conversation"):
        return None
    return {
        "message_id": msg.get("id"),
        "parent_id": parent,
        "role": role,
        "kind": _CHATGPT_KIND.get(ctype, "tool_result" if role == "tool" else "other"),
        "content": text,
        "model": meta.get("model_slug"),
        "created_at": _ts(msg.get("create_time")),
    }


def chatgpt_full(data: dict):
    conv = {"id": data.get("conversation_id") or data.get("id"), "title": data.get("title"),
            "model": data.get("default_model_slug")}
    rows = []
    for node in (data.get("mapping") or {}).values():
        if node.get("message"):
            r = _chatgpt_message(node["message"], node.get("parent"))
            if r and r["message_id"]:
                rows.append(r)
    return conv, rows


def chatgpt_stream(req: str, res: str):
    """Live answer stream. Handles both the older 'full message per event'
    format and the newer JSON-patch delta format ({"p","o","v"})."""
    conv_id = None
    msgs: dict[str, dict] = {}
    current: str | None = None

    def append(text):
        if current and isinstance(text, str):
            parts = msgs[current].setdefault("content", {}).setdefault("parts", [""])
            parts[-1] = (parts[-1] if isinstance(parts[-1], str) else "") + text

    def apply(op):
        if not isinstance(op, dict):
            return
        is_text_append = op.get("p", "").endswith("/parts/0") and op.get("o") in ("append", None)
        is_shorthand = "p" not in op and isinstance(op.get("v"), str)  # continues the last path
        if is_text_append or is_shorthand:
            append(op.get("v"))
        elif op.get("o") == "patch" and isinstance(op.get("v"), list):
            for sub in op["v"]:
                apply(sub)

    for ev in _sse_events(res):
        if not isinstance(ev, dict):
            continue
        conv_id = ev.get("conversation_id") or conv_id
        holder = ev.get("v") if isinstance(ev.get("v"), dict) else ev
        if isinstance(holder.get("message"), dict):
            m = holder["message"]
            conv_id = holder.get("conversation_id") or conv_id
            if m.get("id"):
                msgs[m["id"]] = json.loads(json.dumps(m))
                current = m["id"]
            continue
        apply(ev)

    rows = []
    # The user's prompt is in the request body.
    try:
        body = json.loads(req or "{}")
        conv_id = conv_id or body.get("conversation_id")
        for m in body.get("messages", []):
            r = _chatgpt_message(m, body.get("parent_message_id"))
            if r and r["message_id"]:
                rows.append(r)
    except ValueError:
        pass
    for m in msgs.values():
        r = _chatgpt_message(m)
        if r and r["message_id"]:
            rows.append(r)
    if not conv_id:
        return None, []
    return {"id": conv_id, "model": next((r["model"] for r in rows if r.get("model")), None)}, rows


# ---- Claude ---------------------------------------------------------------

def _claude_blocks(msg_id: str, role: str, blocks: list, created_at, parent):
    rows = []
    for i, b in enumerate(blocks or []):
        t = b.get("type")
        if t == "text":
            kind, text = "text", b.get("text")
        elif t in ("thinking", "redacted_thinking"):
            kind = "thinking"
            text = b.get("thinking") or "\n".join(s.get("summary", "") for s in b.get("summaries", []) or [])
        elif t in ("tool_use", "server_tool_use"):
            kind, text = "tool_call", json.dumps({"name": b.get("name"), "input": b.get("input")})
        elif t in ("tool_result", "web_search_tool_result"):
            kind, text = "tool_result", json.dumps(b.get("content"))
        else:
            kind, text = "other", json.dumps(b)
        if text:
            rows.append({"message_id": f"{msg_id}#{i}", "parent_id": parent, "role": role, "kind": kind,
                         "content": text, "created_at": created_at})
    return rows


def claude_full(data: dict):
    conv = {"id": data.get("uuid"), "title": data.get("name"), "model": data.get("model")}
    rows = []
    for m in data.get("chat_messages") or []:
        role = "user" if m.get("sender") == "human" else "assistant"
        blocks = m.get("content") or ([{"type": "text", "text": m.get("text")}] if m.get("text") else [])
        rows += _claude_blocks(m.get("uuid"), role, blocks, m.get("created_at"), m.get("parent_message_uuid"))
    return conv, rows


def claude_stream(conv_id: str, req: str, res: str, cap_id: int):
    rows, model, msg_id = [], None, f"stream-{cap_id}"
    blocks: dict[int, dict] = {}
    for ev in _sse_events(res):
        t = ev.get("type")
        if t == "message_start":
            m = ev.get("message") or {}
            msg_id = m.get("uuid") or m.get("id") or msg_id
            model = m.get("model")
        elif t == "content_block_start":
            blocks[ev.get("index", len(blocks))] = dict(ev.get("content_block") or {})
        elif t == "content_block_delta":
            b = blocks.setdefault(ev.get("index", 0), {"type": "text"})
            d = ev.get("delta") or {}
            if "text" in d:
                b["text"] = b.get("text", "") + d["text"]
            elif "thinking" in d:
                b["thinking"] = b.get("thinking", "") + d["thinking"]
            elif "partial_json" in d:
                b["_json"] = b.get("_json", "") + d["partial_json"]
    for b in blocks.values():
        if "_json" in b:
            try:
                b["input"] = json.loads(b.pop("_json"))
            except ValueError:
                b["input"] = b.pop("_json")
    try:
        body = json.loads(req or "{}")
        model = model or body.get("model")
        if body.get("prompt"):
            rows.append({"message_id": f"prompt-{cap_id}#0", "role": "user", "kind": "text",
                         "content": body["prompt"], "parent_id": body.get("parent_message_uuid")})
    except ValueError:
        pass
    rows += _claude_blocks(msg_id, "assistant", [blocks[k] for k in sorted(blocks)], None, None)
    return {"id": conv_id, "model": model}, rows


def reparse_all(con: sqlite3.Connection) -> int:
    con.execute("DELETE FROM messages WHERE capture_id IS NOT NULL")  # keep local-file imports
    total = 0
    for (cid,) in con.execute("SELECT id FROM captures ORDER BY id").fetchall():
        total += parse_capture(con, cid)
    con.commit()
    return total
