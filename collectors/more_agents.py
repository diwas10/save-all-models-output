"""Collectors for more coding agents. Each one reads the agent's own local
history files (nothing is intercepted), and re-reads a file only when it changed.

  cline, roo_code,   <editor>/User/globalStorage/<extension>/tasks/*/api_conversation_history.json
  kilo_code          (VS Code, Cursor, Windsurf, VSCodium, Antigravity, Kiro, Trae, ... and
                     their -server folders on remote Linux machines), + ~/.cline/data/tasks
  cursor             Cursor/User/globalStorage/state.vscdb  (composer chats and agent runs)
  copilot            <editor>/User/workspaceStorage/*/chatSessions/*.json(l)  (GitHub Copilot Chat)
  gemini_cli         ~/.gemini/tmp/*/chats/*.json
  qwen_code          ~/.qwen/tmp/*/chats/*.json
  continue           ~/.continue/sessions/*.json
  goose              ~/.local/share/goose/sessions/  (sessions.db, or *.jsonl from older versions)
  amp                ~/.local/share/amp/threads/*.json
  aider              .aider.chat.history.md in your projects (searched for hourly)
  devin              Devin's API (Devin runs in the cloud). Needs an API key, see devin().

Everything here is read from undocumented formats, so it is best-effort: a
collector that finds nothing, or a format it doesn't know, writes nothing.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

from agents import HOME, _changed, _copy_db, _ms

XDG_DATA = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local" / "share")
DATA_DIR = Path(os.environ.get("AI_HISTORY_DIR", HOME / "AIHistory"))


def _config_base() -> Path:
    if sys.platform == "darwin":
        return HOME / "Library" / "Application Support"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or HOME / "AppData" / "Roaming")
    return Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")


EDITORS = ["Code", "Code - Insiders", "VSCodium", "Cursor", "Windsurf", "Antigravity", "Kiro", "Trae",
           "Positron", "Void"]
REMOTE_EDITORS = [".vscode-server", ".vscode-server-insiders", ".cursor-server", ".windsurf-server",
                  ".antigravity-server", ".vscodium-server"]


def editor_user_dirs():
    """(editor name, its User folder) for every VS Code-like editor installed."""
    base = _config_base()
    for name in EDITORS:
        d = base / name / "User"
        if d.is_dir():
            yield name, d
    for name in REMOTE_EDITORS:
        d = HOME / name / "data" / "User"
        if d.is_dir():
            yield name, d


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _text(v) -> str:
    """Content that may be a string, a list of parts, or an object."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "\n".join(t for t in (_text(x) for x in v) if t)
    if isinstance(v, dict):
        for k in ("text", "value", "content", "data", "output"):
            if k in v and v[k] is not None:
                return _text(v[k])
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _uniq(ids: set, mid: str) -> str:
    out, n = mid, 1
    while out in ids:
        n += 1
        out = f"{mid}.{n}"
    ids.add(out)
    return out


def _chain(rows: list[dict]) -> list[dict]:
    """Set parent_id to the previous row where the format has no tree."""
    prev = None
    for r in rows:
        r.setdefault("parent_id", prev)
        prev = r["message_id"]
    return rows


def _unwrap(r: dict) -> dict:
    """Tool output that arrived JSON-encoded ("text" or [{"type":"text",...}]) → plain text."""
    c = r.get("content")
    if r.get("kind") == "tool_result" and isinstance(c, str) and c[:1] in ('"', "["):
        try:
            v = json.loads(c)
        except ValueError:
            return r
        if isinstance(v, str) or (isinstance(v, list) and all(isinstance(x, (str, dict)) for x in v)):
            r["content"] = _text(v)
    return r


def _save(con, store, source, conv, rows) -> int:
    rows = [_unwrap(r) for r in rows if r.get("content") and str(r["content"]).strip()]
    if not conv.get("id") or not rows:
        return 0
    last = None  # rows without their own time take the one before them
    for r in rows:
        r["created_at"] = r.get("created_at") or last
        last = r["created_at"]
    first = next((r["created_at"] for r in rows if r["created_at"]), conv.get("updated_at"))
    for r in rows:
        if not r["created_at"]:
            r["created_at"] = first
        else:
            break
    if conv.get("project", "") and str(conv["project"]).startswith("file://"):
        conv["project"] = urllib.parse.unquote(conv["project"][len("file://"):])
    n = store.write_conversation(con, source, conv, rows, "transcript")
    con.commit()
    return n


# ---------------------------------------------------------------- Cline, Roo Code, Kilo Code

CLINE_FAMILY = {"cline": "saoudrizwan.claude-dev", "roo_code": "rooveterinaryinc.roo-cline",
                "kilo_code": "kilocode.kilo-code"}
_TOOL_RESULT_TEXT = re.compile(r"^\[[^\]\n]{1,300}\] Result:")


def cline_rows(history: list, store) -> tuple[list[dict], str | None]:
    """api_conversation_history.json: Anthropic-style messages. Tool results
    are either tool_result blocks or user text like "[read_file for 'x'] Result:"."""
    rows, ids, model = [], set(), None
    for i, m in enumerate(history if isinstance(history, list) else []):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        mid = _uniq(ids, str(m.get("ts") or f"i{i}"))
        blocks = m.get("content")
        if isinstance(blocks, str):
            blocks = [{"type": "text", "text": blocks}]
        msg_model = (m.get("modelInfo") or {}).get("modelId")
        model = msg_model or model
        for r in store._claude_blocks(mid, role, blocks or [], _ms(m.get("ts")), None):
            if role == "user" and (r["kind"] == "tool_result" or
                                   (r["kind"] == "text" and _TOOL_RESULT_TEXT.match(r["content"]))):
                r["role"], r["kind"] = "tool", "tool_result"
            if role == "assistant":
                r["model"] = msg_model
            rows.append(r)
    return _chain(rows), model


def _cline_task_dirs(ext_id: str):
    for _, user in editor_user_dirs():
        d = user / "globalStorage" / ext_id
        if (d / "tasks").is_dir():
            yield d
    if ext_id == CLINE_FAMILY["cline"]:  # the Cline CLI / JetBrains plugin
        d = HOME / ".cline" / "data"
        if (d / "tasks").is_dir():
            yield d


def _cline(con, store, source: str) -> int:
    total = 0
    for base in _cline_task_dirs(CLINE_FAMILY[source]):
        hist = {}
        for f in (base / "state" / "taskHistory.json", base / "tasks" / "_index.json"):
            for h in (_load(f) or []) if f.exists() else []:
                if isinstance(h, dict) and h.get("id"):
                    hist[str(h["id"])] = h
        for f in sorted((base / "tasks").glob("*/api_conversation_history.json")):
            if not _changed(con, f):
                continue
            tid = f.parent.name
            rows, model = cline_rows(_load(f) or [], store)
            h = hist.get(tid, {})
            meta = _load(f.parent / "task_metadata.json") or {}
            first = next((r["content"] for r in rows if r["role"] == "user"), "")
            task = h.get("task") or re.sub(r"</?task>", "", first.split("</task>")[0]).strip()
            conv = {"id": tid, "title": (task.strip().splitlines() or [""])[0][:120] or None,
                    "model": h.get("modelId") or model,
                    "updated_at": _ms(int(h["ts"])) if str(h.get("ts", "")).isdigit() else (rows[-1]["created_at"] if rows else None),
                    "project": h.get("cwdOnTaskInitialization") or h.get("workspace") or meta.get("cwd")}
            total += _save(con, store, source, conv, rows)
        con.commit()
    return total


def cline(con, store):
    return _cline(con, store, "cline")


def roo_code(con, store):
    return _cline(con, store, "roo_code")


def kilo_code(con, store):
    return _cline(con, store, "kilo_code")


# ---------------------------------------------------------------- Cursor

def _ts_any(v):
    if isinstance(v, (int, float)):
        return _ms(v) if v > 1e11 else _ms(v * 1000)
    return v if isinstance(v, str) and v else None


def cursor_bubble_rows(b: dict, bid: str) -> list[dict]:
    """One Cursor 'bubble': type 1 = you, 2 = the AI (text, thinking, a tool call)."""
    role = "user" if b.get("type") == 1 else "assistant"
    at = _ts_any(b.get("createdAt") or (b.get("timingInfo") or {}).get("clientStartTime"))
    model = (b.get("modelInfo") or {}).get("modelName")
    rows = []
    thinking = b.get("thinking")
    if thinking:
        rows.append({"message_id": f"{bid}#thinking", "role": role, "kind": "thinking",
                     "content": _text(thinking), "created_at": at, "model": model})
    if b.get("text"):
        rows.append({"message_id": bid, "role": role, "kind": "text", "content": b["text"],
                     "created_at": at, "model": model})
    tool = b.get("toolFormerData")
    if isinstance(tool, dict) and tool.get("name"):
        args = tool.get("rawArgs") or tool.get("params")
        rows.append({"message_id": f"{bid}#call", "role": "assistant", "kind": "tool_call", "created_at": at,
                     "content": json.dumps({"name": tool["name"], "input": args}, ensure_ascii=False)})
        if tool.get("result"):
            rows.append({"message_id": f"{bid}#result", "role": "tool", "kind": "tool_result",
                         "content": _text(tool["result"]), "created_at": at})
    return rows


def cursor(con, store) -> int:
    total = 0
    for name, user in editor_user_dirs():
        db = user / "globalStorage" / "state.vscdb"
        if not name.lower().startswith(("cursor", ".cursor")) or not db.exists() or not _changed(con, db):
            continue
        with tempfile.TemporaryDirectory() as tmp:
            src = sqlite3.connect(_copy_db(db, Path(tmp)))
            try:
                src.execute("SELECT 1 FROM cursorDiskKV LIMIT 1")
            except sqlite3.Error:
                src.close()
                continue
            for key, val in src.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"):
                try:
                    c = json.loads(val)
                except (TypeError, ValueError):
                    continue
                cid = c.get("composerId") or key.split(":", 1)[1]
                bubbles = []
                if c.get("conversation"):  # older versions keep the bubbles inline
                    bubbles = [(b.get("bubbleId") or f"i{i}", b) for i, b in enumerate(c["conversation"])]
                else:
                    for h in c.get("fullConversationHeadersOnly") or []:
                        row = src.execute("SELECT value FROM cursorDiskKV WHERE key=?",
                                          (f"bubbleId:{cid}:{h.get('bubbleId')}",)).fetchone()
                        try:
                            bubbles.append((h["bubbleId"], json.loads(row[0]) if row and row[0] else None))
                        except (TypeError, ValueError, KeyError):
                            pass
                rows = _chain([r for bid, b in bubbles if isinstance(b, dict) for r in cursor_bubble_rows(b, bid)])
                conv = {"id": cid, "title": c.get("name"), "updated_at": _ts_any(c.get("lastUpdatedAt") or c.get("createdAt")),
                        "model": (c.get("modelConfig") or {}).get("modelName")}
                total += _save(con, store, "cursor", conv, rows)
            src.close()
        con.commit()
    return total


# ---------------------------------------------------------------- GitHub Copilot Chat (VS Code)

def _replay_jsonl(path: Path):
    """Newer VS Code writes chat sessions as a log: line 1 is the whole
    session (kind 0), later lines set (1), append to (2) or delete (3) a path."""
    state = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            op = json.loads(line)
        except ValueError:
            continue
        kind, keys, v = op.get("kind"), op.get("k") or [], op.get("v")
        if kind == 0:
            state = v
            continue
        if state is None or not keys:
            continue
        obj = state
        try:
            for k in keys[:-1]:
                obj = obj[k]
            last = keys[-1]
            if kind == 1:
                obj[last] = v
            elif kind == 2:
                target = obj.setdefault(last, []) if isinstance(obj, dict) else obj[last]
                i = op.get("i")
                if i is not None:
                    del target[i:]
                target.extend(v if isinstance(v, list) else [v])
            elif kind == 3:
                del obj[last]
        except (KeyError, IndexError, TypeError):
            continue
    return state


def copilot_rows(sess: dict) -> list[dict]:
    rows = []
    for i, req in enumerate(sess.get("requests") or []):
        rid = req.get("requestId") or f"r{i}"
        at, model = _ms(req.get("timestamp")), req.get("modelId")
        msg = req.get("message") or {}
        rows.append({"message_id": f"{rid}#user", "role": "user", "kind": "text",
                     "content": msg.get("text") if isinstance(msg, dict) else _text(msg), "created_at": at})
        buf = []

        def flush(n):
            if "".join(buf).strip():
                rows.append({"message_id": f"{rid}#{n}text", "role": "assistant", "kind": "text",
                             "content": "".join(buf).strip(), "created_at": at, "model": model})
            buf.clear()

        for n, part in enumerate(req.get("response") or []):
            if not isinstance(part, dict):
                continue
            kind = part.get("kind")
            if kind in (None, "markdownContent", "markdownVuln"):
                buf.append(_text(part.get("value") if "value" in part else part.get("content")))
            elif kind == "inlineReference":
                ref = part.get("inlineReference") or {}
                buf.append(f"`{ref.get('name') or ref.get('path') or _text(ref)}`" if isinstance(ref, dict) else str(ref))
            elif kind == "thinking":
                flush(n)
                if _text(part.get("value")).strip():
                    rows.append({"message_id": f"{rid}#{n}", "role": "assistant", "kind": "thinking",
                                 "content": _text(part.get("value")), "created_at": at, "model": model})
            elif kind in ("toolInvocationSerialized", "toolInvocation"):
                flush(n)
                data = {k: part.get(k) for k in ("toolId", "invocationMessage", "pastTenseMessage",
                                                 "toolSpecificData", "resultDetails") if part.get(k)}
                rows.append({"message_id": f"{rid}#{n}", "role": "assistant", "kind": "tool_call",
                             "content": json.dumps(data, ensure_ascii=False), "created_at": at, "model": model})
            elif kind == "textEditGroup":
                flush(n)
                rows.append({"message_id": f"{rid}#{n}", "role": "assistant", "kind": "tool_call", "created_at": at,
                             "content": json.dumps({"name": "edit", "uri": part.get("uri"), "edits": part.get("edits")},
                                                   ensure_ascii=False), "model": model})
        flush("end")
    return _chain(rows)


def copilot(con, store) -> int:
    total = 0
    for _, user in editor_user_dirs():
        files = list((user / "workspaceStorage").glob("*/chatSessions/*.json*")) + \
            list((user / "globalStorage" / "emptyWindowChatSessions").glob("*.json*"))
        for f in sorted(files):
            if f.suffix not in (".json", ".jsonl") or not _changed(con, f):
                continue
            try:
                sess = _replay_jsonl(f) if f.suffix == ".jsonl" else json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(sess, dict):
                continue
            project = None
            ws = _load(f.parent.parent / "workspace.json") if f.parent.name == "chatSessions" else None
            if isinstance(ws, dict):
                project = urllib.parse.unquote((ws.get("folder") or ws.get("workspace") or "").replace("file://", "")) or None
            rows = copilot_rows(sess)
            first = next((r["content"] for r in rows if r["role"] == "user"), "") or ""
            conv = {"id": sess.get("sessionId") or f.stem, "project": project,
                    "title": sess.get("customTitle") or (first.strip().splitlines() or [""])[0][:120] or None,
                    "updated_at": _ms(sess.get("lastMessageDate")) or (rows[-1]["created_at"] if rows else None)}
            total += _save(con, store, "copilot", conv, rows)
    return total


# ---------------------------------------------------------------- Gemini CLI, Qwen Code

def gemini_cli_rows(messages: list) -> list[dict]:
    rows, ids = [], set()
    for i, m in enumerate(messages or []):
        if not isinstance(m, dict):
            continue
        t, at = m.get("type") or m.get("role"), m.get("timestamp")
        mid = _uniq(ids, str(m.get("id") or f"i{i}"))
        if t in ("info", "error", "warning"):
            continue
        role = "user" if t == "user" else "assistant"
        for n, th in enumerate(m.get("thoughts") or []):
            txt = "\n".join(x for x in (th.get("subject"), th.get("description")) if x) if isinstance(th, dict) else _text(th)
            rows.append({"message_id": f"{mid}#thought{n}", "role": role, "kind": "thinking", "content": txt,
                         "created_at": th.get("timestamp") if isinstance(th, dict) else at, "model": m.get("model")})
        rows.append({"message_id": mid, "role": role, "kind": "text", "content": _text(m.get("content")),
                     "created_at": at, "model": m.get("model")})
        for n, tc in enumerate(m.get("toolCalls") or []):
            tid = tc.get("id") or str(n)
            rows.append({"message_id": f"{mid}#call{tid}", "role": "assistant", "kind": "tool_call",
                         "created_at": tc.get("timestamp") or at,
                         "content": json.dumps({"name": tc.get("name"), "input": tc.get("args")}, ensure_ascii=False)})
            result = tc.get("resultDisplay") or tc.get("result")
            if result:
                rows.append({"message_id": f"{mid}#result{tid}", "role": "tool", "kind": "tool_result",
                             "content": _text(result), "created_at": tc.get("timestamp") or at})
    return _chain(rows)


def _gemini_like(con, store, source: str, root: Path) -> int:
    total = 0
    for f in sorted(root.glob("tmp/*/chats/*.json")) if root.exists() else []:
        if not _changed(con, f):
            continue
        d = _load(f)
        if not isinstance(d, dict):
            continue
        rows = gemini_cli_rows(d.get("messages"))
        pr = f.parent.parent / ".project_root"
        first = next((r["content"] for r in rows if r["role"] == "user"), "")
        conv = {"id": d.get("sessionId") or f.stem, "updated_at": d.get("lastUpdated"),
                "title": (first.strip().splitlines() or [""])[0][:120] or None,
                "project": pr.read_text().strip() if pr.exists() else d.get("projectHash")}
        total += _save(con, store, source, conv, rows)
    return total


def gemini_cli(con, store):
    return _gemini_like(con, store, "gemini_cli", HOME / ".gemini")


def qwen_code(con, store):
    return _gemini_like(con, store, "qwen_code", HOME / ".qwen")


# ---------------------------------------------------------------- Continue

def continue_rows(history: list) -> list[dict]:
    rows = []
    for i, item in enumerate(history or []):
        m = item.get("message") if isinstance(item, dict) else None
        if not isinstance(m, dict):
            continue
        role, mid = m.get("role"), m.get("id") or f"i{i}"
        if role == "thinking":
            rows.append({"message_id": mid, "role": "assistant", "kind": "thinking", "content": _text(m.get("content"))})
            continue
        if role == "tool":
            rows.append({"message_id": mid, "role": "tool", "kind": "tool_result", "content": _text(m.get("content"))})
            continue
        if role == "system":
            continue
        rows.append({"message_id": mid, "role": role, "kind": "text", "content": _text(m.get("content"))})
        for n, tc in enumerate(m.get("toolCalls") or []):
            fn = tc.get("function") or {}
            rows.append({"message_id": f"{mid}#call{n}", "role": "assistant", "kind": "tool_call",
                         "content": json.dumps({"name": fn.get("name"), "input": fn.get("arguments"), "id": tc.get("id")},
                                               ensure_ascii=False)})
        for n, out in enumerate(item.get("toolCallStates") or []):
            if isinstance(out, dict) and out.get("output"):
                rows.append({"message_id": f"{mid}#result{n}", "role": "tool", "kind": "tool_result",
                             "content": _text(out["output"])})
    return _chain(rows)


def continue_dev(con, store) -> int:
    total, root = 0, HOME / ".continue" / "sessions"
    index = {s.get("sessionId"): s for s in (_load(root / "sessions.json") or []) if isinstance(s, dict)}
    for f in sorted(root.glob("*.json")) if root.exists() else []:
        if f.name == "sessions.json" or not _changed(con, f):
            continue
        d = _load(f)
        if not isinstance(d, dict):
            continue
        sid = d.get("sessionId") or f.stem
        meta = index.get(sid, {})
        created = meta.get("dateCreated")
        conv = {"id": sid, "title": d.get("title") or meta.get("title"), "project": d.get("workspaceDirectory"),
                "updated_at": _ts_any(int(created)) if str(created or "").isdigit() else created}
        rows = continue_rows(d.get("history"))
        for r in rows:
            r.setdefault("created_at", conv["updated_at"])
        total += _save(con, store, "continue", conv, rows)
    return total


# ---------------------------------------------------------------- Goose

def goose_rows(messages) -> list[dict]:
    """Goose messages: role + content blocks (text, thinking, toolRequest, toolResponse)."""
    rows, ids = [], set()
    for i, m in enumerate(messages):
        role, at = m.get("role"), _ts_any(m.get("created"))
        mid = _uniq(ids, str(m.get("id") or f"i{i}"))
        for n, b in enumerate(m.get("content") or []):
            t = b.get("type")
            if t == "text":
                row = (role, "text", b.get("text"))
            elif t in ("thinking", "redactedThinking", "reasoning"):
                row = ("assistant", "thinking", b.get("thinking") or b.get("text"))
            elif t == "toolRequest":
                call = (b.get("toolCall") or {}).get("value") or b.get("toolCall") or {}
                row = ("assistant", "tool_call", json.dumps({"name": call.get("name"), "input": call.get("arguments"),
                                                             "id": b.get("id")}, ensure_ascii=False))
            elif t == "toolResponse":
                res = b.get("toolResult") or {}
                row = ("tool", "tool_result", _text(res.get("value", res.get("error"))))
            else:
                continue
            rows.append({"message_id": f"{mid}#{n}", "role": row[0], "kind": row[1], "content": row[2], "created_at": at})
    return _chain(rows)


def _goose_dirs():
    yield XDG_DATA / "goose" / "sessions"
    if os.name == "nt":
        yield Path(os.environ.get("APPDATA") or HOME) / "Block" / "goose" / "data" / "sessions"


def goose(con, store) -> int:
    total = 0
    for root in _goose_dirs():
        db = root / "sessions.db"
        if db.exists() and _changed(con, db):
            with tempfile.TemporaryDirectory() as tmp:
                src = sqlite3.connect(_copy_db(db, Path(tmp)))
                src.row_factory = sqlite3.Row
                try:
                    msgs: dict[str, list] = {}
                    for r in src.execute("SELECT * FROM messages ORDER BY id"):
                        r = dict(r)
                        try:
                            content = json.loads(r.get("content_json") or r.get("content") or "[]")
                        except ValueError:
                            continue
                        msgs.setdefault(r.get("session_id"), []).append(
                            {"id": r.get("message_id") or r.get("id"), "role": r.get("role"), "content": content,
                             "created": r.get("created_timestamp") or r.get("created_at")})
                    for s in src.execute("SELECT * FROM sessions"):
                        s = dict(s)
                        conv = {"id": s.get("id"), "title": s.get("name") or s.get("description"),
                                "project": s.get("working_dir"), "updated_at": _ts_any(s.get("updated_at"))}
                        total += _save(con, store, "goose", conv, goose_rows(msgs.get(s.get("id"), [])))
                except sqlite3.Error:
                    pass
                src.close()
            con.commit()
        for f in sorted(root.glob("*.jsonl")) if root.exists() else []:  # older Goose
            if not _changed(con, f):
                continue
            lines = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip().startswith("{")]
            meta = lines[0] if lines and "role" not in lines[0] else {}
            msgs = [x for x in lines if "role" in x]
            conv = {"id": f.stem, "title": meta.get("description"), "project": meta.get("working_dir"),
                    "updated_at": _ts_any(msgs[-1].get("created")) if msgs else None}
            total += _save(con, store, "goose", conv, goose_rows(msgs))
    return total


# ---------------------------------------------------------------- Amp

def amp_rows(messages: list, store) -> list[dict]:
    rows, ids = [], set()
    for i, m in enumerate(messages or []):
        role = m.get("role")
        mid = _uniq(ids, str(m.get("messageId") or m.get("id") or f"i{i}"))
        at = _ts_any((m.get("meta") or {}).get("sentAt"))
        blocks = []
        for b in m.get("content") or []:
            if b.get("type") == "tool_result" and "run" in b:  # Amp keeps the output under run.result
                b = {"type": "tool_result", "content": (b.get("run") or {}).get("result") or b.get("run")}
            blocks.append(b)
        for r in store._claude_blocks(mid, role, blocks, at, None):
            if r["kind"] == "tool_result":
                r["role"] = "tool"
            rows.append(r)
    return _chain(rows)


def amp(con, store) -> int:
    total, root = 0, XDG_DATA / "amp" / "threads"
    for f in sorted(root.glob("*.json")) if root.exists() else []:
        if not _changed(con, f):
            continue
        d = _load(f)
        if not isinstance(d, dict):
            continue
        rows = amp_rows(d.get("messages"), store)
        trees = (d.get("env") or {}).get("initial", {}).get("trees") or []
        conv = {"id": d.get("id") or f.stem, "title": d.get("title"), "updated_at": _ts_any(d.get("created")),
                "project": (trees[0].get("uri") or "").replace("file://", "") if trees and isinstance(trees[0], dict) else None}
        if rows:
            conv["updated_at"] = max(r["created_at"] or "" for r in rows) or conv["updated_at"]
        total += _save(con, store, "amp", conv, rows)
    return total


# ---------------------------------------------------------------- Aider

SKIP_DIRS = {"node_modules", "Library", "Applications", "AppData", "venv", "site-packages", "__pycache__",
             "Pictures", "Music", "Movies", "Videos", "dist", "build", "target"}
AIDER_FILE = ".aider.chat.history.md"


def _find_aider(root: Path, depth: int = 5):
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for e in entries:
        if e.name == AIDER_FILE and e.is_file():
            yield Path(e.path)
        elif depth and e.is_dir(follow_symlinks=False) and not e.name.startswith(".") and e.name not in SKIP_DIRS:
            yield from _find_aider(Path(e.path), depth - 1)


def aider_sessions(text: str):
    """'# aider chat started at <time>' starts a session; '#### ' lines are
    your prompts, '> ' lines are aider's own output, the rest is the model."""
    for chunk in re.split(r"^# aider chat started at ", text, flags=re.M)[1:]:
        started, _, body = chunk.partition("\n")
        rows, cur = [], None
        for line in body.splitlines():
            if line.startswith("#### "):
                role, kind, line = "user", "text", line[5:]
            elif line.startswith(">"):
                role, kind, line = "tool", "tool_result", line[2:] if line.startswith("> ") else line[1:]
            else:
                role, kind = "assistant", "text"
            if cur and cur["role"] == role:
                cur["content"] += "\n" + line
            else:
                cur = {"message_id": f"{started.strip()}#{len(rows)}", "role": role, "kind": kind,
                       "content": line, "created_at": started.strip()}
                rows.append(cur)
        for r in rows:
            r["content"] = r["content"].strip()
        yield started.strip(), _chain(rows)


def aider(con, store) -> int:
    known = {Path(p[len("aider:"):]) for (p,) in con.execute("SELECT path FROM collector_state WHERE path LIKE 'aider:%'")}
    scan = con.execute("SELECT mtime FROM collector_state WHERE path='aider-scan'").fetchone()
    if not scan or time.time() - scan[0] > 3600:
        for f in _find_aider(HOME):
            if f not in known:
                known.add(f)
                con.execute("INSERT OR IGNORE INTO collector_state (path, mtime, size, offset) VALUES (?,0,0,0)", (f"aider:{f}",))
        con.execute("INSERT OR REPLACE INTO collector_state (path, mtime, size, offset) VALUES ('aider-scan',?,0,0)", (time.time(),))
        con.commit()
    total = 0
    for f in sorted(known):
        if not f.exists() or not _changed(con, f):
            continue
        for started, rows in aider_sessions(f.read_text(encoding="utf-8", errors="replace")):
            first = next((r["content"] for r in rows if r["role"] == "user"), "")
            conv = {"id": f"{f.parent}@{started}", "project": str(f.parent), "updated_at": started,
                    "title": (first.splitlines() or [""])[0][:120] or None}
            total += _save(con, store, "aider", conv, rows)
    return total


# ---------------------------------------------------------------- Devin (cloud)

DEVIN_API = os.environ.get("DEVIN_API_URL", "https://api.devin.ai/v1")
DEVIN_KEY_FILE = DATA_DIR / "devin_api_key"


def _devin_key() -> str | None:
    if os.environ.get("DEVIN_API_KEY"):
        return os.environ["DEVIN_API_KEY"].strip()
    try:
        return DEVIN_KEY_FILE.read_text().strip() or None
    except OSError:
        return None


def _devin_get(path: str, key: str):
    req = urllib.request.Request(DEVIN_API + path, headers={"Authorization": f"Bearer {key}",
                                                             "User-Agent": "ai-history-collector"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def devin_rows(sess: dict) -> list[dict]:
    rows, ids = [], set()
    for i, m in enumerate(sess.get("messages") or []):
        t = (m.get("type") or "").lower()
        role = "assistant" if "devin" in t else "user"
        rows.append({"message_id": _uniq(ids, str(m.get("event_id") or f"i{i}")), "role": role, "kind": "text",
                     "content": _text(m.get("message")), "created_at": m.get("timestamp")})
    if sess.get("structured_output"):
        rows.append({"message_id": "structured_output", "role": "assistant", "kind": "other",
                     "content": json.dumps(sess["structured_output"], ensure_ascii=False)})
    return _chain(rows)


def devin(con, store) -> int:
    """Devin runs in the cloud, so its history comes from its API. Create a
    key in Devin (Settings → API keys) and save it once with
        python3 collectors/more_agents.py devin-key
    Only sessions whose updated_at changed are fetched again."""
    key = _devin_key()
    if not key:
        return 0
    total, offset = 0, 0
    while True:
        page = _devin_get(f"/sessions?limit=100&offset={offset}", key)
        sessions = page.get("sessions") if isinstance(page, dict) else page
        if not sessions:
            break
        for s in sessions:
            sid, upd = s.get("session_id"), s.get("updated_at") or ""
            state = f"devin:{sid}"
            row = con.execute("SELECT size FROM collector_state WHERE path=?", (state,)).fetchone()
            stamp = zlib.crc32(upd.encode())
            if row and row[0] == stamp:
                continue
            full = _devin_get(f"/session/{urllib.parse.quote(str(sid))}", key)
            conv = {"id": sid, "title": full.get("title") or s.get("title"), "updated_at": upd or full.get("updated_at"),
                    "project": (full.get("pull_request") or {}).get("url") if isinstance(full.get("pull_request"), dict) else None}
            total += _save(con, store, "devin", conv, devin_rows(full))
            con.execute("INSERT OR REPLACE INTO collector_state (path, mtime, size, offset) VALUES (?,?,?,0)",
                        (state, time.time(), stamp))
            con.commit()
        if len(sessions) < 100:
            break
        offset += 100
    return total


COLLECTORS = {"cline": cline, "roo_code": roo_code, "kilo_code": kilo_code, "cursor": cursor, "copilot": copilot,
              "gemini_cli": gemini_cli, "qwen_code": qwen_code, "continue": continue_dev, "goose": goose,
              "amp": amp, "aider": aider, "devin": devin}


if __name__ == "__main__":
    if sys.argv[1:] == ["devin-key"]:
        import getpass
        key = getpass.getpass("Devin API key (input hidden): ").strip()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        DEVIN_KEY_FILE.write_text(key + "\n")
        os.chmod(DEVIN_KEY_FILE, 0o600)
        print(f"Saved to {DEVIN_KEY_FILE}. The next collector run imports your Devin sessions.")
    else:
        print(__doc__)
