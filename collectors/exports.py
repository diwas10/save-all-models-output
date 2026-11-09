#!/usr/bin/env python3
"""Import the chat history you export from ChatGPT, Claude and Google (Gemini).

  python3 collectors/exports.py <file.zip | folder | conversations.json> ...

The scheduled collector also imports any export .zip you drop into
Documents/local-data, once per file. Re-importing the same export is safe,
and so is importing an export that overlaps chats already recorded.

  Google Takeout (Gemini)  Takeout/My Activity/Gemini Apps/MyActivity.json
                           Takeout/Gemini in Workspace/Conversation History/*.txt
  ChatGPT export           conversations.json (or conversations-NNN.json)
  Claude export            conversations.json

Only text is imported. Attached images and files are listed by name, but
not copied. They stay in the export.
"""
import hashlib
import html
import json
import re
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "server"))
import store  # noqa: E402


def _h(*parts) -> str:
    return hashlib.sha1("\x00".join(map(str, parts)).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- HTML → text

class _Text(HTMLParser):
    """Turns Gemini's answer HTML back into Markdown-like text."""
    BLOCK = {"p", "div", "ul", "ol", "table", "tr", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.pre, self.lists = [], 0, []

    def nl(self, n=1):
        tail = "".join(self.out[-3:])
        have = len(tail) - len(tail.rstrip("\n"))
        if self.out and have < n:
            self.out.append("\n" * (n - have))

    def handle_starttag(self, tag, attrs):
        if tag == "pre":
            self.nl(2); self.out.append("```\n"); self.pre += 1
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag == "br":
            self.out.append("\n")
        elif tag in ("ul", "ol"):
            self.lists.append([tag, 0]); self.nl()
        elif tag == "li":
            self.nl()
            kind = self.lists[-1] if self.lists else ["ul", 0]
            kind[1] += 1
            self.out.append("  " * max(len(self.lists) - 1, 0) + (f"{kind[1]}. " if kind[0] == "ol" else "- "))
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif re.fullmatch(r"h[1-6]", tag):
            self.nl(2); self.out.append("#" * int(tag[1]) + " ")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag in self.BLOCK:
            self.nl(2)

    def handle_endtag(self, tag):
        if tag == "pre":
            self.pre -= 1; self.nl(); self.out.append("```"); self.nl(2)
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self.nl(2)
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag in self.BLOCK or re.fullmatch(r"h[1-6]", tag):
            self.nl(2 if tag != "tr" else 1)

    def handle_data(self, data):
        self.out.append(data if self.pre else re.sub(r"\s*\n\s*", " ", data) if data.strip() else
                        ("" if not self.out or self.out[-1].endswith(("\n", " ")) else " ") if data else "")


def html_to_text(s: str) -> str:
    p = _Text()
    p.feed(s)
    p.close()
    text = re.sub(r"[ \t ]+\n", "\n", "".join(p.out) + "\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------------------------------------------------------------- Gemini (Google Takeout)

def gemini_activity(items: list):
    """My Activity → Gemini Apps. One entry per prompt, newest first. The chat
    it belongs to is the gemini.google.com/app/<id> link in 'details'."""
    convs: dict[str, list] = {}
    for e in items:
        title = e.get("title") or ""
        if not title.startswith("Prompted "):
            continue  # "Cleared previous feedback", "Used Gemini Apps", ...
        url = next((d.get("url") for d in e.get("details") or [] if "/app/" in (d.get("url") or "")), None)
        cid = url.rsplit("/app/", 1)[1].split("?")[0] if url else "activity-" + (e.get("time") or "")[:10]
        convs.setdefault(cid, []).append(e)

    for cid, entries in convs.items():
        entries.sort(key=lambda e: e.get("time") or "")
        rows, parent = [], None
        for e in entries:
            t, prompt = e.get("time"), e["title"][len("Prompted "):]
            mid = _h(cid, t, prompt)
            files = [s["name"].lstrip("- ").strip() for s in e.get("subtitles") or [] if s.get("url")]
            if files:
                prompt += "\n\n[attached: " + ", ".join(files) + "]"
            rows.append({"message_id": f"{mid}#user", "parent_id": parent, "role": "user", "kind": "text",
                         "content": prompt, "created_at": t})
            parent = f"{mid}#user"
            for i, item in enumerate(e.get("safeHtmlItem") or []):
                text = html_to_text(item.get("html") or "")
                if text:
                    rows.append({"message_id": f"{mid}#answer{i}", "parent_id": parent, "role": "assistant",
                                 "kind": "text", "content": text, "created_at": t})
                    parent = f"{mid}#answer{i}"
        first = entries[0]["title"][len("Prompted "):].strip().splitlines()
        conv = {"id": cid, "title": (first[0] if first else "")[:120] or None,
                "updated_at": entries[-1].get("time"), "project": "Gemini Apps"}
        yield "gemini", conv, rows


def gemini_workspace(name: str, data: dict):
    """Gemini in Workspace (Docs, Gmail, ...): one JSON file per chat, with thoughts."""
    cid = "workspace-" + Path(name).stem.replace("conversation_", "")
    rows, parent = [], None
    for turn in data.get("conversation_turns") or []:
        for side, t in turn.items():
            base, at = f"{t.get('turn_index')}", t.get("turn_last_modified")
            parts = []
            if side == "user_turn":
                parts.append(("user", "text", t.get("prompt")))
            else:
                seen = set()
                for th in t.get("model_thoughts") or []:
                    txt = "\n".join(x for x in (th.get("headline"), th.get("description")) if x)
                    if txt and txt not in seen:
                        seen.add(txt)
                        parts.append(("assistant", "thinking", txt))
                for x in t.get("text") or []:
                    parts.append(("assistant", "text", x.get("data") if isinstance(x, dict) else x))
            for i, (role, kind, txt) in enumerate(parts):
                if txt and str(txt).strip():
                    mid = f"{base}#{i}"
                    rows.append({"message_id": mid, "parent_id": parent, "role": role, "kind": kind,
                                 "content": txt, "created_at": at})
                    parent = mid
    conv = {"id": cid, "title": data.get("title"), "updated_at": data.get("last_modification_time"),
            "project": "Gemini in Workspace"}
    yield "gemini", conv, rows


# ---------------------------------------------------------------- ChatGPT / Claude exports

def conversations_json(items: list):
    for c in items if isinstance(items, list) else []:
        if not isinstance(c, dict):
            continue
        if "mapping" in c:
            conv, rows = store.chatgpt_full(c)
            conv["updated_at"] = store._ts(c.get("update_time"))
            yield "chatgpt", conv, rows
        elif "chat_messages" in c:
            conv, rows = store.claude_full(c)
            conv["updated_at"] = c.get("updated_at")
            yield "claude", conv, rows


# ---------------------------------------------------------------- reading files

def _route(name: str, read):
    """Pick the parser for one file inside an export, by its path."""
    n = name.replace("\\", "/")
    if n.endswith("Gemini Apps/MyActivity.json"):
        return gemini_activity(json.loads(read()))
    if "Gemini in Workspace/" in n and re.search(r"conversation_[^/]*\.txt$", n):
        return gemini_workspace(n, json.loads(read()))
    if re.search(r"(^|/)conversations(-\d+)?\.json$", n):
        return conversations_json(json.loads(read()))
    return None


def iter_export(path: Path):
    path = Path(path)
    if path.is_dir():
        for f in sorted(path.rglob("*")):
            if f.is_file() and f.suffix in (".json", ".txt"):
                got = _route(str(f.relative_to(path)), f.read_bytes)
                if got:
                    yield from got
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                got = _route(info.filename, lambda: z.read(info))
                if got:
                    yield from got
    elif path.suffix in (".json", ".txt"):
        got = _route(path.name, path.read_bytes)
        if got:
            yield from got


def import_export(con, path) -> dict:
    """Import one export. Returns {source: [conversations, messages]}."""
    counts: dict[str, list] = {}
    for source, conv, rows in iter_export(path):
        if not conv.get("id") or not rows:
            continue
        rows = [r for r in rows if r.get("message_id")]
        store.write_conversation(con, source, conv, rows, origin="export")
        c = counts.setdefault(source, [0, 0])
        c[0] += 1
        c[1] += len(rows)
    con.commit()
    return counts


def import_dropped(con, folder: Path) -> int:
    """Used by the scheduled collector: import export zips found in folder, once each."""
    sys.path.insert(0, str(HERE))
    import agents
    total = 0
    for f in sorted(folder.glob("*.zip")):
        if not agents._changed(con, f):
            continue
        try:
            for source, (_, n) in import_export(con, f).items():
                total += n
        except (zipfile.BadZipFile, OSError, ValueError) as e:  # still downloading, or not an export
            con.rollback()
            print(f"exports: skipped {f.name}: {e}", flush=True)
            con.execute("DELETE FROM collector_state WHERE path=?", (str(f),))
            con.commit()
    return total


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    con = store.connect()
    for p in argv:
        counts = import_export(con, Path(p).expanduser())
        if not counts:
            print(f"{p}: no ChatGPT, Claude or Gemini history found")
        for source, (convs, msgs) in sorted(counts.items()):
            print(f"{Path(p).name}: {source:<8} {convs:>6} conversations {msgs:>8} messages")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
