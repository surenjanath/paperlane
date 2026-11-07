/* Paperlane — shared UI behaviour (menus, modals, drawers, confirm, search, notifications, copy, bulk). */
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const csrf = () => (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || ($("[name=csrfmiddlewaretoken]") || {}).value || "";
  window.SD = { $, $$, csrf };

  /* ---------- menus ---------- */
  document.addEventListener("click", (e) => {
    const trigger = e.target.closest("[data-menu]");
    $$(".menu.open").forEach((m) => { if (!trigger || m !== menuFor(trigger)) m.classList.remove("open"); });
    if (trigger) {
      e.preventDefault();
      e.stopPropagation();
      const menu = menuFor(trigger);
      menu.classList.remove("flip-up");
      menu.classList.toggle("open");
      // open upwards when there is not enough room below (rows near the bottom of the page)
      if (menu.classList.contains("open") && !menu.classList.contains("up")) {
        const r = menu.getBoundingClientRect();
        const t = trigger.getBoundingClientRect();
        if (r.bottom > window.innerHeight - 8 && t.top - r.height - 8 > 0) menu.classList.add("flip-up");
      }
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") $$(".menu.open").forEach((m) => m.classList.remove("open"));
  });
  /* show / hide password */
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-pw-toggle]");
    if (!t) return;
    const input = t.parentElement.querySelector("input");
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    t.textContent = show ? "Hide" : "Show";
    t.setAttribute("aria-label", show ? "Hide password" : "Show password");
    input.focus();
  });
  function menuFor(trigger) {
    const sel = trigger.getAttribute("data-menu");
    return sel ? $(sel) : trigger.parentElement.querySelector(".menu");
  }

  /* ---------- modal ---------- */
  const backdrop = $("#modal-backdrop");
  function openModal(html, opts = {}) {
    if (!backdrop) return;
    const box = $("#modal", backdrop);
    box.className = "modal" + (opts.wide ? " wide" : "");
    box.innerHTML = html;
    backdrop.classList.add("open");
    hydrate(box);
    const first = box.querySelector("input:not([type=hidden]):not([type=checkbox]), textarea, select");
    if (first && !opts.noFocus) setTimeout(() => first.focus(), 30);
  }
  function closeModal() { if (backdrop) { backdrop.classList.remove("open"); $("#modal", backdrop).innerHTML = ""; } }
  window.SD.openModal = openModal;
  window.SD.closeModal = closeModal;
  if (backdrop) backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop) closeModal(); });

  async function loadInto(url, fn) {
    const res = await fetch(url, { headers: { "X-Requested-With": "fetch" } });
    if (!res.ok) { toast(res.status === 403 ? "You don't have permission to do that." : "Something went wrong (" + res.status + ").", "error"); return; }
    fn(await res.text());
  }

  /* ---------- drawer ---------- */
  const drawer = $("#drawer"), drawerBackdrop = $("#drawer-backdrop");
  function openDrawer(html) {
    drawer.innerHTML = html;
    drawer.classList.add("open");
    drawerBackdrop.classList.add("open");
    hydrate(drawer);
  }
  function closeDrawer() { if (drawer) { drawer.classList.remove("open"); drawerBackdrop.classList.remove("open"); } }
  window.SD.openDrawer = openDrawer;
  window.SD.closeDrawer = () => closeDrawer();
  if (drawerBackdrop) drawerBackdrop.addEventListener("click", closeDrawer);

  document.addEventListener("click", (e) => {
    const m = e.target.closest("[data-modal-url]");
    if (m) { e.preventDefault(); loadInto(m.getAttribute("data-modal-url"), (h) => openModal(h, { wide: m.hasAttribute("data-wide") })); return; }
    const d = e.target.closest("[data-drawer-url]");
    if (d) { e.preventDefault(); loadInto(d.getAttribute("data-drawer-url"), openDrawer); return; }
    const t = e.target.closest("[data-modal-template]");
    if (t) {
      e.preventDefault();
      const tpl = $(t.getAttribute("data-modal-template"));
      openModal(tpl.innerHTML, { wide: t.hasAttribute("data-wide") });
      const box = $("#modal");
      // allow passing values: data-set-<name>="value" fills [name=<name>]
      for (const attr of t.attributes) {
        if (attr.name.startsWith("data-set-")) {
          const el = box.querySelector(`[name="${attr.name.slice(9)}"]`);
          if (el) el.value = attr.value;
        }
        if (attr.name === "data-action") { const f = box.querySelector("form"); if (f) f.action = attr.value; }
      }
      return;
    }
    if (e.target.closest("[data-close]")) { e.preventDefault(); closeModal(); closeDrawer(); }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeModal(); closeDrawer(); closePanel(); $$(".menu.open").forEach((m) => m.classList.remove("open")); }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openSearch(); }
  });

  /* ---------- confirm (never uses window.confirm) ---------- */
  document.addEventListener("submit", (e) => {
    const form = e.target;
    const msg = form.getAttribute("data-confirm") || (e.submitter && e.submitter.getAttribute("data-confirm"));
    if (!msg || form.dataset.confirmed) return;
    e.preventDefault();
    const submitter = e.submitter;
    // a form inside a modal would be wiped out when the confirm dialog takes the modal's place: keep it, hidden
    if (form.closest("#modal")) { form.hidden = true; document.body.appendChild(form); }
    openModal(`<div class="modal-head"><h2>${escapeHtml(form.getAttribute("data-confirm-title") || "Are you sure?")}</h2><button class="modal-close" data-close>×</button></div>
      <div class="modal-body"><p>${escapeHtml(msg)}</p></div>
      <div class="modal-foot"><button class="btn" data-close>Cancel</button><button class="btn ${form.hasAttribute("data-danger") || (submitter && submitter.hasAttribute("data-danger")) ? "btn-danger" : "btn-dark"}" id="confirm-ok">${escapeHtml(form.getAttribute("data-confirm-ok") || (submitter && submitter.getAttribute("data-confirm-ok")) || "Confirm")}</button></div>`, { noFocus: true });
    $("#confirm-ok").addEventListener("click", () => {
      form.dataset.confirmed = "1";
      if (submitter && submitter.hasAttribute("formaction")) form.action = submitter.formAction;
      if (submitter && submitter.name) {
        const h = document.createElement("input");
        h.type = "hidden"; h.name = submitter.name; h.value = submitter.value; form.appendChild(h);
      }
      closeModal();
      form.submit();
    });
  });

  /* ---------- toasts ---------- */
  function toast(msg, kind = "success") {
    let wrap = $(".toast-wrap");
    if (!wrap) { wrap = document.createElement("div"); wrap.className = "toast-wrap"; document.body.appendChild(wrap); }
    const el = document.createElement("div");
    el.className = "alert alert-" + kind;
    el.textContent = msg;
    wrap.appendChild(el);
    setTimeout(() => el.remove(), 4200);
  }
  window.SD.toast = toast;
  $$(".toast-wrap .alert").forEach((el) => setTimeout(() => el.remove(), 5000));

  /* ---------- ✦ Write with AI (step invitation email; the drawer's HTML is injected, so the handler lives here) ---------- */
  document.addEventListener("click", async (e) => {
    const go = e.target.closest("[data-ai-write-go]");
    if (!go) return;
    const box = go.closest("[data-ai-write]"), form = go.closest("form"), msg = box.querySelector("[data-ai-write-msg]");
    const fd = new FormData(); fd.append("tone", box.querySelector("[data-tone]").value);
    fd.append("csrfmiddlewaretoken", (form.querySelector("[name=csrfmiddlewaretoken]") || {}).value || "");
    go.disabled = true; msg.textContent = "Writing…";
    try {
      const j = await (await fetch(box.dataset.aiWrite, { method: "POST", body: fd, headers: { "X-Requested-With": "fetch" } })).json();
      if (j.error) { msg.textContent = j.error; }
      else { form.message_subject.value = j.subject; form.message_body.value = j.body; msg.textContent = "Written — edit it if you like, then Apply."; }
    } catch (_) { msg.textContent = "The AI model could not be reached."; }
    go.disabled = false;
  });

  /* ---------- copy ---------- */
  document.addEventListener("click", async (e) => {
    const c = e.target.closest("[data-copy]");
    if (!c) return;
    e.preventDefault();
    const v = c.getAttribute("data-copy");
    const text = v.startsWith("#") ? ($(v).value || $(v).textContent) : v;
    try { await navigator.clipboard.writeText(text.startsWith("/") ? location.origin + text : text); toast(c.dataset.copyToast || "Link copied"); }
    catch (_) { toast("Copy failed — select and copy manually.", "error"); }
  });

  /* ---------- search palette ---------- */
  function openSearch() {
    const tpl = $("#search-template");
    if (!tpl) return;
    openModal(tpl.innerHTML);
    $("#modal").classList.add("palette");
    const input = $("#palette-q"), list = $("#palette-results");
    let sel = -1, timer;
    const render = (items) => {
      sel = -1;
      list.innerHTML = items.length ? items.map((r) => `<a class="res" href="${r.url}"><span class="badge badge-gray">${escapeHtml(r.type)}</span><span class="t">${escapeHtml(r.title)}</span><small class="muted">${escapeHtml(r.meta || "")}</small></a>`).join("")
        : `<div class="muted" style="padding:16px;text-align:center">${input.value ? "No results" : "Type to search workflows, templates, documents and contacts"}</div>`;
    };
    render([]);
    input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        if (!input.value.trim()) return render([]);
        const r = await fetch("/search/?format=json&q=" + encodeURIComponent(input.value.trim()));
        render((await r.json()).results);
      }, 160);
    });
    input.addEventListener("keydown", (e) => {
      const items = $$(".res", list);
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        sel = Math.max(0, Math.min(items.length - 1, sel + (e.key === "ArrowDown" ? 1 : -1)));
        items.forEach((x, i) => x.classList.toggle("sel", i === sel));
      }
      if (e.key === "Enter" && items.length) { e.preventDefault(); location.href = items[Math.max(sel, 0)].href; }
    });
  }
  window.SD.openSearch = openSearch;
  document.addEventListener("click", (e) => { if (e.target.closest("[data-open-search]")) { e.preventDefault(); openSearch(); } });

  /* ---------- notifications panel ---------- */
  const panel = $("#notif-panel");
  function closePanel() { if (panel) panel.classList.remove("open"); }
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-open-notifications]");
    if (b) {
      e.preventDefault();
      if (panel.classList.contains("open")) return closePanel();
      loadInto("/notifications/?panel=1", (h) => { panel.innerHTML = h; panel.classList.add("open"); hydrate(panel); });
      return;
    }
    if (panel && panel.classList.contains("open") && !e.target.closest("#notif-panel")) closePanel();
    if (e.target.closest("[data-close-panel]")) closePanel();
  });
  document.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-mark-all-read]");
    if (!b) return;
    e.preventDefault();
    await fetch("/notifications/read/", { method: "POST", headers: { "X-CSRFToken": csrf(), "X-Requested-With": "fetch" } });
    $$(".unread-dot").forEach((d) => d.remove());
    const dot = $("[data-open-notifications] .dot"); if (dot) dot.remove();
  });

  /* ---------- bulk selection ---------- */
  document.addEventListener("change", (e) => {
    if (e.target.matches("[data-select-all]")) {
      const scope = e.target.getAttribute("data-select-all");
      $$(scope).forEach((c) => (c.checked = e.target.checked));
    }
    if (e.target.matches("[data-select-all], .row-check")) {
      const n = $$(".row-check:checked").length;
      $$("[data-bulk-count]").forEach((x) => (x.textContent = n));
      $$("[data-bulk-bar]").forEach((x) => (x.hidden = n === 0));
    }
  });
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-bulk-submit]");
    if (!b) return;
    const form = $(b.getAttribute("data-bulk-submit"));
    $$("input[name=ids]", form).forEach((x) => x.remove());
    $$(".row-check:checked").forEach((c) => {
      const h = document.createElement("input"); h.type = "hidden"; h.name = "ids"; h.value = c.value; form.appendChild(h);
    });
    form.querySelector("[name=action]").value = b.getAttribute("data-bulk-action");
    if (b.hasAttribute("data-bulk-tags")) {
      const tags = $("#bulk-tags-input");
      form.querySelector("[name=tags]").value = tags ? tags.value : "";
    }
    form.submit();
  });

  /* ---------- show/hide by checkbox / select ---------- */
  function syncToggles(root) {
    $$("[data-show-when]", root).forEach((el) => {
      const [name, val] = el.getAttribute("data-show-when").split("=");
      const inputs = $$(`[name="${name}"]`, root.closest("form") || root);
      let v = "";
      inputs.forEach((i) => { if ((i.type === "checkbox" || i.type === "radio") ? i.checked : true) v = i.type === "checkbox" ? "on" : i.value; });
      el.hidden = val === undefined ? !v : !val.split("|").includes(v);
    });
  }
  document.addEventListener("change", (e) => syncToggles(e.target.closest("form") || document));

  /* ---------- condition rule builder ---------- */
  document.addEventListener("click", (e) => {
    const add = e.target.closest("[data-add-rule]");
    if (add) {
      e.preventDefault();
      const box = $(add.getAttribute("data-add-rule"));
      const proto = box.querySelector(".rule-row");
      const clone = proto.cloneNode(true);
      clone.querySelectorAll("input,select").forEach((i) => { if (i.tagName === "SELECT") i.selectedIndex = 0; else i.value = ""; });
      box.appendChild(clone);
    }
    const rm = e.target.closest("[data-remove-rule]");
    if (rm) {
      e.preventDefault();
      const row = rm.closest(".rule-row");
      const box = row.parentElement;
      if (box.querySelectorAll(".rule-row").length > 1) row.remove();
      else row.querySelectorAll("input,select").forEach((i) => { if (i.tagName === "SELECT") i.selectedIndex = 0; else i.value = ""; });
    }
  });

  /* ---------- sidebar ---------- */
  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-toggle-sidebar]")) { $(".sidebar") && $(".sidebar").classList.toggle("open"); }
    const more = e.target.closest("[data-show-more]");
    if (more) { e.preventDefault(); $$(more.getAttribute("data-show-more")).forEach((x) => (x.hidden = false)); more.remove(); }
    const moreTags = e.target.closest("[data-show-more-tags]");
    if (moreTags) { e.preventDefault(); moreTags.closest("[data-tags]").querySelectorAll("[data-more-tag]").forEach((x) => { x.hidden = false; x.removeAttribute("data-more-tag"); }); moreTags.remove(); }
    const coll = e.target.closest("[data-collapse]");
    if (coll) { const t = $(coll.getAttribute("data-collapse")); t.hidden = !t.hidden; coll.classList.toggle("closed", t.hidden); }
  });

  /* ---------- auto-submit selects ---------- */
  document.addEventListener("change", (e) => { if (e.target.matches("[data-autosubmit]")) e.target.form.submit(); });

  function escapeHtml(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  window.SD.escapeHtml = escapeHtml;

  function hydrate(root) { syncToggles(root); }
  window.SD.hydrate = hydrate;
  hydrate(document);

  /* tag rows show a single line, the rest behind "Show more" */
  function fitTags() {
    $$("[data-tags]").forEach((row) => {
      const more = row.querySelector("[data-show-more-tags]");
      if (!more) return;
      const tags = $$(".pk-tag:not(.pk-tag-more)", row);
      tags.forEach((t) => { if (t.hasAttribute("data-more-tag")) { t.hidden = false; t.removeAttribute("data-more-tag"); } });
      more.hidden = true;
      const top = tags.length ? tags[0].offsetTop : 0;
      if (!tags.some((t) => t.offsetTop > top + 4)) return;
      more.hidden = false;
      for (let i = tags.length - 1; i >= 0 && (more.offsetTop > top + 4 || tags.some((t) => !t.hidden && t.offsetTop > top + 4)); i--) {
        tags[i].hidden = true; tags[i].setAttribute("data-more-tag", "");
      }
    });
  }
  fitTags();
  window.addEventListener("resize", fitTags);

  /* open a drawer/modal requested via ?open=<id> (e.g. right after adding a step) */
  const opener = $("[data-auto-open]");
  if (opener) opener.click();
})();

/* ---------- @mention autocomplete in comment boxes ---------- */
(function () {
  const ta = document.querySelector("textarea[data-mentions]");
  const data = document.getElementById("mention-data");
  if (!ta || !data) return;
  const people = JSON.parse(data.textContent || "[]");
  const menu = ta.parentElement.querySelector(".mention-menu");
  let items = [], idx = 0, start = -1;
  const close = () => { menu.hidden = true; start = -1; };
  const pick = (p) => {
    const before = ta.value.slice(0, start), after = ta.value.slice(ta.selectionStart);
    const ins = "@" + p.token + " ";
    ta.value = before + ins + after;
    const pos = before.length + ins.length;
    ta.focus(); ta.setSelectionRange(pos, pos); close();
  };
  const render = () => {
    menu.innerHTML = "";
    items.forEach((p, i) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "mention-opt" + (i === idx ? " on" : ""); b.setAttribute("role", "option");
      const n = document.createElement("b"); n.textContent = p.name;
      const e = document.createElement("small"); e.textContent = p.email;
      b.append(n, e);
      b.addEventListener("mousedown", (ev) => { ev.preventDefault(); pick(p); });
      menu.appendChild(b);
    });
    menu.hidden = !items.length;
  };
  ta.addEventListener("input", () => {
    const upto = ta.value.slice(0, ta.selectionStart);
    const m = upto.match(/(^|\s)@([\w. ]{0,30})$/);
    if (!m) return close();
    start = upto.length - m[2].length - 1;
    const q = m[2].toLowerCase();
    items = people.filter((p) => p.name.toLowerCase().includes(q) || (p.email || "").toLowerCase().startsWith(q)).slice(0, 6);
    idx = 0; render();
  });
  ta.addEventListener("keydown", (e) => {
    if (menu.hidden) return;
    if (e.key === "ArrowDown") { idx = (idx + 1) % items.length; render(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { idx = (idx - 1 + items.length) % items.length; render(); e.preventDefault(); }
    else if (e.key === "Enter" || e.key === "Tab") { pick(items[idx]); e.preventDefault(); }
    else if (e.key === "Escape") { close(); }
  });
  ta.addEventListener("blur", () => setTimeout(close, 150));
})();

/* ---------- Cards / Compact layout for document lists (remembered in a cookie) ---------- */
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-set-view]");
  if (!b) return;
  document.cookie = `dl_view=${b.dataset.setView}; path=/; max-age=31536000; SameSite=Lax`;
  location.reload();
});

/* ---------- group panel: find, select all / none, count (panel HTML is injected, so delegate) ---------- */
(function () {
  const count = (f) => { const c = f.querySelector("[data-gp-count]"); if (c) c.textContent = `(${f.querySelectorAll("input[name=members]:checked").length} selected)`; };
  document.addEventListener("input", (e) => {
    if (!e.target.matches("[data-gp-find]")) return;
    const q = e.target.value.toLowerCase().trim(), f = e.target.closest("form");
    f.querySelectorAll("[data-gp-item]").forEach((i) => { i.hidden = !!q && !i.dataset.text.includes(q); });
  });
  document.addEventListener("change", (e) => { const f = e.target.closest("[data-group-form]"); if (f) count(f); });
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-gp-all], [data-gp-none]");
    if (!b) return;
    const f = b.closest("form"), on = b.hasAttribute("data-gp-all");
    f.querySelectorAll("[data-gp-item]").forEach((i) => { if (!on || !i.hidden) i.querySelector("input").checked = on; });
    count(f);
  });
})();
