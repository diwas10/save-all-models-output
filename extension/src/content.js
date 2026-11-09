// Runs in the extension's isolated world. Relays records posted by inject.js
// (page world) to the background script, which talks to the local server.
const api = globalThis.browser ?? globalThis.chrome;

window.addEventListener("message", (event) => {
  if (event.source !== window || event.origin !== location.origin) return;
  const data = event.data;
  if (!data || data.__aiHistory !== true || !data.record) return;
  try {
    api.runtime.sendMessage({ type: "capture", record: data.record });
  } catch {
    // Extension was reloaded/updated while the tab stayed open; reload the tab.
  }
});
