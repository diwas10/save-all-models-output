#!/usr/bin/env python3
"""Local receiver for the AI Chat Recorder browser extension.

  python3 server/server.py            # run on http://127.0.0.1:8765
  python3 server/server.py stats      # what has been saved
  python3 server/server.py reparse    # rebuild `messages` from raw captures

Only listens on 127.0.0.1. Also serves /proxy.pac, which tells macOS to send
*only* AI-chat hosts through the desktop-app proxy (see desktop/).
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store  # noqa: E402

HOST, PORT = "127.0.0.1", 8765
PROXY_PORT = 8080
MAX_BODY = 25 * 1024 * 1024

# Hosts the desktop apps talk to. Sent to the proxy; everything else goes
# DIRECT and never touches it. "; DIRECT" = fall back if the proxy is down.
PAC_HOSTS = [
    "chatgpt.com", "*.chatgpt.com", "chat.openai.com", "*.oaiusercontent.com",
    "claude.ai", "*.claude.ai", "api.anthropic.com",
    "gemini.google.com", "*.gemini.google.com",
    "*-pa.googleapis.com", "generativelanguage.googleapis.com",
]
PAC = (
    "function FindProxyForURL(url, host) {\n"
    + "".join(f'  if (shExpMatch(host, "{h}")) return "PROXY 127.0.0.1:{PROXY_PORT}; DIRECT";\n' for h in PAC_HOSTS)
    + '  return "DIRECT";\n}\n'
)

_lock = threading.Lock()
_con = None


def db():
    global _con
    if _con is None:
        _con = store.connect(check_same_thread=False)
    return _con


class Handler(BaseHTTPRequestHandler):
    server_version = "AIHistory/0.1"

    def _host_ok(self) -> bool:
        # Blocks DNS-rebinding: a web page on evil.com resolving to 127.0.0.1
        # would send Host: evil.com.
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        if self.path == "/health":
            with _lock:
                n = db().execute("SELECT COUNT(*) FROM captures").fetchone()[0]
            return self._send(200, {"ok": True, "captures": n, "db": str(store.DB_PATH)})
        if self.path == "/proxy.pac":
            return self._send(200, PAC, "application/x-ns-proxy-autoconfig")
        self._send(404, {"error": "not found"})

    def do_OPTIONS(self):
        # No CORS headers on purpose: web pages can't preflight their way in.
        self._send(403, {"error": "no"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        # A custom header forces a CORS preflight, which we refuse above, so
        # ordinary web pages can't post here. Extensions with host permission can.
        if self.headers.get("X-AI-History") != "1":
            return self._send(403, {"error": "missing header"})
        origin = self.headers.get("Origin", "")
        if origin.startswith(("http://", "https://")):
            return self._send(403, {"error": "web origins not allowed"})
        if self.path != "/ingest":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self._send(413, {"error": "bad size"})
        try:
            rec = json.loads(self.rfile.read(length))
        except ValueError:
            return self._send(400, {"error": "bad json"})
        with _lock:
            cap_id, n = store.save_capture(db(), rec, "browser-extension")
        tag = "dup" if cap_id is None else f"#{cap_id} +{n} msgs"
        print(f"[{rec.get('source')}] {rec.get('method')} {rec.get('url', '')[:90]}  {tag}", flush=True)
        self._send(200, {"ok": True, "capture_id": cap_id, "messages": n})

    def log_message(self, *args):
        pass


def stats():
    con = store.connect()
    print(f"Database: {store.DB_PATH}\n")
    print("Raw captures:")
    for r in con.execute("SELECT source, channel, COUNT(*), SUM(parsed>0) FROM captures GROUP BY 1,2"):
        print(f"  {r[0]:<10} {r[1]:<18} {r[2]:>6} captured, {r[3] or 0:>6} parsed")
    print("\nMessages:")
    for r in con.execute("SELECT source, kind, COUNT(*), COUNT(DISTINCT conversation_id) FROM messages GROUP BY 1,2"):
        print(f"  {r[0]:<10} {r[1]:<12} {r[2]:>6} rows in {r[3]} conversations")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "stats":
        stats()
    elif cmd == "reparse":
        print("messages rebuilt:", store.reparse_all(store.connect()))
    else:
        db()
        print(f"AI History server on http://{HOST}:{PORT}  ->  {store.DB_PATH}", flush=True)
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
