// Background (Chrome service worker / Firefox event page).
// Sends each captured record to the local server. If the server is not
// running, records wait in a queue in extension storage and are retried.
const api = globalThis.browser ?? globalThis.chrome;

const SERVER = "http://127.0.0.1:8765";
const MAX_QUEUE = 2000;

async function getState() {
  const s = await api.storage.local.get({ enabled: true, queue: [], sent: 0, lastError: null, lastSentAt: null });
  return s;
}

async function post(record) {
  const res = await fetch(`${SERVER}/ingest`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-AI-History": "1" },
    body: JSON.stringify(record),
  });
  if (!res.ok) throw new Error(`server replied ${res.status}`);
}

let flushing = false;
async function flush(extra) {
  // Serialise flushes so the queue is never written by two callers at once.
  while (flushing) await new Promise((r) => setTimeout(r, 50));
  flushing = true;
  try {
    const s = await getState();
    const queue = s.queue;
    if (extra) {
      if (!s.enabled) return;
      queue.push(extra);
    }
    let sent = s.sent, lastError = s.lastError, lastSentAt = s.lastSentAt;
    while (queue.length) {
      try {
        await post(queue[0]);
        queue.shift();
        sent += 1;
        lastError = null;
        lastSentAt = new Date().toISOString();
      } catch (e) {
        lastError = `${new Date().toLocaleTimeString()}: ${e.message} (is the server running?)`;
        break;
      }
    }
    while (queue.length > MAX_QUEUE) queue.shift();
    await api.storage.local.set({ queue, sent, lastError, lastSentAt });
  } finally {
    flushing = false;
  }
}

api.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "capture") {
    flush(msg.record);
  } else if (msg && msg.type === "flush") {
    flush().then(() => sendResponse({ ok: true }));
    return true;
  }
});

api.alarms.create("retry", { periodInMinutes: 1 });
api.alarms.onAlarm.addListener((a) => { if (a.name === "retry") flush(); });
