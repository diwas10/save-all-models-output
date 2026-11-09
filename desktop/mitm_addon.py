"""mitmproxy addon: records AI chat traffic from the Mac desktop apps
(ChatGPT/Codex app, Gemini app, Claude app, ...) into the same database as
the browser extension.

Run by the LaunchAgent installed by desktop/install.sh:
    mitmdump -q --listen-host 127.0.0.1 -p 8080 -s desktop/mitm_addon.py

Safety design
  * macOS only routes AI hosts here (proxy.pac); everything else goes DIRECT.
  * Even for those, TLS is only decrypted for hosts in DECRYPT; all others are
    passed through untouched (we only see the host name).
  * If an app rejects our certificate (certificate pinning), the host is
    remembered in pinned_hosts.txt and passed through from then on, so the
    app keeps working - it just can't be recorded.
  * Auth headers and cookies are never stored; only bodies.
"""
from __future__ import annotations

import base64
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from mitmproxy import http, tls

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import store  # noqa: E402

DECRYPT = [
    (re.compile(r"(^|\.)chatgpt\.com$|^chat\.openai\.com$"), "chatgpt"),
    (re.compile(r"(^|\.)claude\.ai$|^api\.anthropic\.com$"), "claude"),
    (re.compile(r"(^|\.)gemini\.google\.com$|^generativelanguage\.googleapis\.com$"), "gemini"),
]
# Noise we never store (analytics, images, static files).
SKIP_PATH = re.compile(r"\.(js|css|png|jpe?g|gif|webp|svg|woff2?|ico|mp4)$|/(ces|statsc|rgstr|sentry|telemetry|log|v1/m)\b", re.I)
SKIP_HOST = re.compile(r"^(ab|events|browser-intake)\.", re.I)

STATE = store.DATA_DIR
PINNED_FILE = STATE / "pinned_hosts.txt"
SEEN_FILE = STATE / "hosts_seen.txt"


def source_for(host: str) -> str | None:
    return next((s for rx, s in DECRYPT if rx.search(host or "")), None)


def text_of(msg: http.Message | None, raw: bytes | None = None) -> str | None:
    data = raw if raw is not None else (msg.get_content(strict=False) if msg else None)
    if not data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return "base64:" + base64.b64encode(data).decode()  # protobuf / gRPC


class Recorder:
    def __init__(self):
        STATE.mkdir(parents=True, exist_ok=True)
        self.pinned = set(PINNED_FILE.read_text().split()) if PINNED_FILE.exists() else set()
        self.seen: set[str] = set(SEEN_FILE.read_text().split()) if SEEN_FILE.exists() else set()
        self.streams: dict[str, list[bytes]] = {}
        self.ws: dict[str, list[str]] = {}
        self.lock = threading.Lock()
        self.con = store.connect(check_same_thread=False)

    # ---- decide per connection whether to decrypt -------------------------
    def tls_clienthello(self, data: tls.ClientHelloData):
        host = data.client_hello.sni or ""
        if host and host not in self.seen:  # discovery log: which hosts apps use
            self.seen.add(host)
            with SEEN_FILE.open("a") as f:
                f.write(host + "\n")
        if not source_for(host) or host in self.pinned:
            data.ignore_connection = True  # plain pass-through, not decrypted

    def tls_failed_client(self, data: tls.TlsData):
        host = data.conn.sni
        if host and host not in self.pinned:
            self.pinned.add(host)
            PINNED_FILE.write_text("\n".join(sorted(self.pinned)) + "\n")
            print(f"[pinned] {host} rejected the proxy certificate; passing it through from now on", flush=True)

    # ---- keep streamed answers flowing to the app while we copy them -------
    def responseheaders(self, flow: http.HTTPFlow):
        ctype = flow.response.headers.get("content-type", "")
        if "event-stream" in ctype or "grpc" in ctype or "stream" in flow.request.path.lower():
            buf = self.streams.setdefault(flow.id, [])

            def tee(chunk: bytes) -> bytes:
                buf.append(chunk)
                return chunk

            flow.response.stream = tee

    def response(self, flow: http.HTTPFlow):
        raw_stream = self.streams.pop(flow.id, None)
        host = flow.request.pretty_host
        src = source_for(host)
        if not src or SKIP_HOST.search(host) or SKIP_PATH.search(flow.request.path):
            return
        if flow.request.method in ("OPTIONS", "HEAD"):
            return
        if raw_stream is not None:
            body = b"".join(raw_stream)
            enc = flow.response.headers.get("content-encoding")
            if enc:  # streamed bodies arrive still compressed
                try:
                    from mitmproxy.net.encoding import decode
                    body = decode(body, enc)
                except Exception:
                    pass
            res_text = text_of(None, body)
        else:
            res_text = text_of(flow.response)
        self._save(flow, src, res_text)

    # ---- websockets (some apps stream answers this way) --------------------
    def websocket_message(self, flow: http.HTTPFlow):
        m = flow.websocket.messages[-1]
        self.ws.setdefault(flow.id, []).append(("<< " if not m.from_client else ">> ") + (m.text if m.is_text else "base64:" + base64.b64encode(m.content).decode()))

    def websocket_end(self, flow: http.HTTPFlow):
        msgs = self.ws.pop(flow.id, None)
        src = source_for(flow.request.pretty_host)
        if msgs and src:
            self._save(flow, src, "\n".join(msgs), method="WEBSOCKET")

    def _save(self, flow, src, res_text, method=None):
        rec = {
            "source": src,
            "method": method or flow.request.method,
            "url": flow.request.pretty_url,
            "status": flow.response.status_code if flow.response else None,
            "page_url": flow.request.headers.get("user-agent", "")[:200],  # tells us which app
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "request_body": text_of(flow.request),
            "response_body": res_text,
        }
        try:
            with self.lock:
                cap_id, n = store.save_capture(self.con, rec, "desktop-proxy")
            if cap_id:
                print(f"[{src}] {rec['method']} {rec['url'][:90]}  #{cap_id} +{n} msgs", flush=True)
        except Exception as e:  # never break the app's traffic
            print(f"[error] {e}", flush=True)


addons = [Recorder()]
