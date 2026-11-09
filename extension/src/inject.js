// Runs in the PAGE's JavaScript world (world: "MAIN") at document_start.
// It wraps window.fetch and XMLHttpRequest so that, for the few endpoints that
// carry chat content, a *copy* of the request and response bodies is handed to
// the extension. The page itself keeps working exactly as before.
(() => {
  if (window.__aiHistoryInstalled) return;
  window.__aiHistoryInstalled = true;

  // Which network calls are chat traffic. Everything else is ignored.
  const RULES = [
    // ChatGPT: POST /backend-api/(f/)conversation (streamed answer)
    //          GET  /backend-api/conversation/<id> (full saved conversation)
    { source: "chatgpt", host: /(^|\.)chatgpt\.com$/, path: /^\/backend-api\/(f\/)?conversation(\/|$)/ },
    // Claude: GET  .../chat_conversations/<id>?tree=True (full conversation)
    //         POST .../chat_conversations/<id>/completion (streamed answer)
    { source: "claude", host: /^claude\.ai$/, path: /^\/api\/organizations\/[^/]+\/chat_conversations\/[^/]+(\/(completion|retry_completion))?\/?$/ },
    // Gemini: StreamGenerate (answers) and batchexecute (history loads)
    { source: "gemini", host: /^gemini\.google\.com$/, path: /(StreamGenerate|batchexecute)/ },
  ];

  const MAX_BODY = 20 * 1024 * 1024; // skip anything over 20 MB

  function match(rawUrl) {
    let u;
    try { u = new URL(rawUrl, location.href); } catch { return null; }
    const rule = RULES.find((r) => r.host.test(u.hostname) && r.path.test(u.pathname));
    return rule ? { source: rule.source, url: u.href } : null;
  }

  function bodyToText(body) {
    if (body == null) return null;
    if (typeof body === "string") return body;
    if (body instanceof URLSearchParams) return body.toString();
    if (typeof FormData !== "undefined" && body instanceof FormData) {
      const o = {};
      for (const [k, v] of body.entries()) o[k] = typeof v === "string" ? v : "[file]";
      return JSON.stringify(o);
    }
    return "[binary body not captured]";
  }

  function emit(rec) {
    if (rec.response_body && rec.response_body.length > MAX_BODY) return;
    rec.page_url = location.href;
    rec.captured_at = new Date().toISOString();
    window.postMessage({ __aiHistory: true, record: rec }, location.origin);
  }

  // ---- fetch -------------------------------------------------------------
  const origFetch = window.fetch;
  window.fetch = async function (input, init) {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input && input.url;
    const m = url && match(url);
    if (!m) return origFetch.apply(this, arguments);

    const method = ((init && init.method) || (input && input.method) || "GET").toUpperCase();
    let reqBodyP = Promise.resolve(bodyToText(init && init.body));
    if (!(init && init.body) && input instanceof Request && method !== "GET") {
      reqBodyP = input.clone().text().catch(() => null);
    }

    const res = await origFetch.apply(this, arguments);
    try {
      // clone() tees the stream: the page reads one branch, we read the other
      // to the end (for streamed answers this resolves when the answer is done).
      const copy = res.clone();
      Promise.all([reqBodyP, copy.text()])
        .then(([request_body, response_body]) =>
          emit({ ...m, method, status: res.status, request_body, response_body, transport: "fetch" }))
        .catch(() => {});
    } catch { /* never break the page */ }
    return res;
  };

  // ---- XMLHttpRequest (Gemini uses this) ----------------------------------
  const XHR = XMLHttpRequest.prototype;
  const origOpen = XHR.open;
  const origSend = XHR.send;
  XHR.open = function (method, url) {
    this.__aiHistory = match(String(url));
    if (this.__aiHistory) this.__aiHistory.method = String(method).toUpperCase();
    return origOpen.apply(this, arguments);
  };
  XHR.send = function (body) {
    const m = this.__aiHistory;
    if (m) {
      const request_body = bodyToText(body);
      this.addEventListener("loadend", () => {
        try {
          let response_body = null;
          if (this.responseType === "" || this.responseType === "text") response_body = this.responseText;
          else if (this.responseType === "json") response_body = JSON.stringify(this.response);
          emit({ ...m, status: this.status, request_body, response_body, transport: "xhr" });
        } catch { /* ignore */ }
      });
    }
    return origSend.apply(this, arguments);
  };
})();
