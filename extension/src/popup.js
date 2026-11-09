const api = globalThis.browser ?? globalThis.chrome;
const ORIGINS = ["https://chatgpt.com/*", "https://*.chatgpt.com/*", "https://claude.ai/*", "https://gemini.google.com/*", "http://127.0.0.1:8765/*"];
const $ = (id) => document.getElementById(id);

async function render() {
  const s = await api.storage.local.get({ enabled: true, queue: [], sent: 0, lastError: null });
  $("enabled").checked = s.enabled;
  $("sent").textContent = s.sent;
  $("queue").textContent = s.queue.length;
  $("error").textContent = s.lastError || "";

  try {
    const r = await fetch("http://127.0.0.1:8765/health");
    const j = await r.json();
    $("server").textContent = `running · ${j.captures} saved`;
    $("server").className = "ok";
  } catch {
    $("server").textContent = "not running";
    $("server").className = "bad";
  }

  const granted = await api.permissions.contains({ origins: ORIGINS });
  $("grant").style.display = granted ? "none" : "block";
}

$("enabled").addEventListener("change", (e) => api.storage.local.set({ enabled: e.target.checked }));
$("retry").addEventListener("click", async () => { await api.runtime.sendMessage({ type: "flush" }); render(); });
$("grant").addEventListener("click", async () => { await api.permissions.request({ origins: ORIGINS }); render(); });

render();
