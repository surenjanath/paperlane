/* Poll the workspace for changes and refresh the open list, plus the Inbox and notification counts. */
(function () {
  "use strict";
  const shell = document.querySelector("[data-live-url]");
  if (!shell) return;
  const url = shell.getAttribute("data-live-url");
  const notesUrl = shell.getAttribute("data-live-notes");
  let rev = null;
  let dirty = false;
  let busy = false;
  let timer = 0;

  function paint(key, n, urgent) {
    const host = document.querySelector(`[data-live-count="${key}"]`);
    if (!host) return;
    const badge = host.querySelector("[data-count]");
    if (!badge) return;
    const num = Number(n) || 0;
    badge.hidden = num <= 0;
    badge.textContent = String(num);
    if (urgent !== undefined) badge.classList.toggle("urgent", !!urgent);
  }

  function editing() {
    if (document.querySelector("#modal-backdrop.open, #drawer.open, .menu.open")) return true;
    if (document.querySelector(".row-check:checked")) return true;
    const active = document.activeElement;
    if (active && active.matches && active.matches("input, textarea, select, [contenteditable='true']")) return true;
    return [...document.querySelectorAll("textarea, input, select")].some((el) => {
      if (el.type === "hidden" || el.type === "checkbox" || el.type === "radio" || el.readOnly) return false;
      if (el.tagName === "SELECT") {
        const picked = [...el.options].find((o) => o.defaultSelected);
        return el.value !== (picked ? picked.value : "");
      }
      return el.value !== el.defaultValue;
    });
  }

  async function refreshNotes() {
    const panel = document.getElementById("notif-panel");
    if (!panel || !panel.classList.contains("open") || !notesUrl || panel.contains(document.activeElement)) return;
    const res = await fetch(notesUrl, { headers: { "X-Requested-With": "fetch" }, credentials: "same-origin", cache: "no-store" });
    if (res.ok && panel.classList.contains("open")) panel.innerHTML = await res.text();
  }

  async function refreshPage() {
    const current = document.querySelector("[data-live]");
    if (!current) return true;
    const res = await fetch(location.href, { credentials: "same-origin", cache: "no-store", headers: { "X-Live": "1" } });
    if (!res.ok) return false;
    if (res.redirected && /\/accounts\/login\/?/.test(new URL(res.url, location.origin).pathname)) {
      stop();
      return false;
    }
    const doc = new DOMParser().parseFromString(await res.text(), "text/html");
    const next = doc.querySelector("[data-live]");
    if (!next) return false;
    const y = window.scrollY;
    if (next.innerHTML !== current.innerHTML) current.innerHTML = next.innerHTML;
    document.querySelectorAll("[data-live-slot]").forEach((el) => {
      const fresh = doc.querySelector(`[data-live-slot="${el.getAttribute("data-live-slot")}"]`);
      if (fresh && fresh.innerHTML !== el.innerHTML) el.innerHTML = fresh.innerHTML;
    });
    window.scrollTo(0, y);
    return true;
  }

  async function tick() {
    if (busy || document.hidden) return;
    busy = true;
    try {
      const res = await fetch(url, { credentials: "same-origin", cache: "no-store", headers: { "X-Requested-With": "fetch" } });
      if (res.status === 401 || res.status === 403) { stop(); return; }
      if (!res.ok) return;
      const data = await res.json();
      paint("waiting", data.waiting);
      paint("unread", data.unread);
      paint("tickets", data.tickets, data.urgent);
      if (rev === null) { rev = data.rev; return; }
      if (data.rev !== rev) dirty = true;
      if (!dirty) return;
      if (editing()) return;
      await refreshNotes();
      if (await refreshPage()) { rev = data.rev; dirty = false; }
    } catch (_) { /* the next tick tries again */ }
    finally { busy = false; }
  }

  function stop() { if (timer) clearInterval(timer); timer = 0; }
  timer = setInterval(tick, 5000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
  tick();
})();
