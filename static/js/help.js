/* Help center: instant search, AI chat drawer (conversation kept for the session), table of contents, feedback. */
(function () {
  const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = (t) => String(t).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const store = { get(k, d) { try { return JSON.parse(sessionStorage.getItem(k)) ?? d; } catch (_) { return d; } },
                  set(k, v) { try { sessionStorage.setItem(k, JSON.stringify(v)); } catch (_) {} } };
  let index = [];
  try { index = JSON.parse($("#hc-index").textContent); } catch (_) {}

  /* ---------- instant search over the article index ---------- */
  const STOP = new Set("a an the and or of to in on for with is are be can i my me how do does what where why when it this that you your".split(" "));
  const toks = (t) => (t.toLowerCase().match(/[a-z0-9@]+/g) || []).filter((w) => w.length > 1 && !STOP.has(w));
  function rank(q) {
    const qt = toks(q);
    if (!qt.length) return [];
    return index.map((a) => {
      const T = toks(a.t), K = toks(a.k), S = toks(a.s);
      let s = 0;
      for (const w of qt) { const st = w.slice(0, 5); s += 4 * T.filter((x) => x.startsWith(st)).length + 3 * K.filter((x) => x.startsWith(st)).length + 1.5 * S.filter((x) => x.startsWith(st)).length; }
      return [s, a];
    }).filter((x) => x[0] > 0).sort((a, b) => b[0] - a[0]).slice(0, 6).map((x) => x[1]);
  }
  const mark = (text, q) => { let h = esc(text); for (const w of toks(q)) h = h.replace(new RegExp(`(${w.replace(/[^a-z0-9]/g, "")}[a-z]*)`, "ig"), "<mark>$1</mark>"); return h; };

  $$("[data-hc-search]").forEach((box) => {
    const input = $("input", box), list = $(".hc-results", box);
    let active = -1;
    const render = () => {
      const q = input.value.trim(), hits = rank(q);
      active = -1;
      if (!q) { list.hidden = true; return; }
      list.innerHTML = hits.map((a) => `<a role="option" href="${a.u}"><b>${mark(a.t, q)}</b><small>${esc(a.c)} · ${mark(a.s, q)}</small></a>`).join("")
        + `<button type="button" class="hc-res-ai" data-chat-ask="${esc(q)}"><span class="chat-avatar sm">✦</span><span>Ask the assistant: <b>“${esc(q)}”</b></span></button>`;
      list.hidden = false;
    };
    input.addEventListener("input", render);
    input.addEventListener("focus", render);
    input.addEventListener("keydown", (e) => {
      const items = $$("a, button", list);
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault(); if (!items.length) return;
        active = (active + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
        items.forEach((x, i) => x.classList.toggle("on", i === active));
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (active >= 0 && items[active]) items[active].click();
        else if (input.value.trim()) { list.hidden = true; chat.ask(input.value.trim()); }
      } else if (e.key === "Escape") { list.hidden = true; input.blur(); }
    });
    document.addEventListener("click", (e) => { if (!box.contains(e.target)) list.hidden = true; });
    const ai = $("[data-chat-from-search]", box);
    if (ai) ai.addEventListener("click", () => { const q = input.value.trim(); list.hidden = true; q ? chat.ask(q) : chat.open(); });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !/input|textarea/i.test(document.activeElement.tagName)) { const i = $("[data-hc-search] input"); if (i) { e.preventDefault(); i.focus(); } }
  });

  /* ---------- chat ---------- */
  const root = $("#hc-chat"), backdrop = $(".chat-backdrop");
  const log = $("[data-chat-log]", root), form = $("[data-chat-form]", root), ta = form.question, welcome = $("[data-chat-welcome]", root);
  const KEY = "hc-chat";
  let history = store.get(KEY, []), busy = false, page = "";

  const inline = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>");
  function md(text) {
    let html = "", list = null;
    for (const raw of String(text).split(/\n/)) {
      const ol = raw.match(/^\s*\d+[.)]\s+(.*)$/), ul = raw.match(/^\s*[-*•]\s+(.*)$/);
      const kind = ol ? "ol" : ul ? "ul" : null;
      if (kind) { if (list !== kind) { if (list) html += `</${list}>`; html += `<${kind}>`; list = kind; } html += `<li>${inline((ol || ul)[1])}</li>`; continue; }
      if (list) { html += `</${list}>`; list = null; }
      if (raw.trim()) html += `<p>${inline(raw.replace(/^#+\s*/, ""))}</p>`;
    }
    return html + (list ? `</${list}>` : "");
  }
  const sourcesHtml = (s) => !s || !s.length ? "" :
    `<div class="chat-sources"><small>From the help center</small>${s.map((x) => `<a href="${x.url}">${"📄"} <span><b>${esc(x.title)}</b><small>${esc(x.cat)}</small></span></a>`).join("")}</div>`;
  const SEV = { none: "No problems", low: "Low", medium: "Medium", high: "High", critical: "Critical" };
  function ticketHtml(d) {
    const t = d.ticket;
    if (t) return `<div class="dx-ticket ${t.created ? "new" : ""}"><span>🎫 ${t.created ? (t.auto ? "Support ticket filed automatically" : "Support ticket created") : "Already reported"}: <a href="${t.url}">${esc(t.ref)}</a> · ${esc(t.severity_label)} · ${esc(t.status)}</span></div>`;
    if (d.severity === "none") return "";
    return `<div class="dx-ticket"><button type="button" class="btn btn-sm" data-dx-ticket="${d.ticket_url}">🎫 Create a support ticket</button><small>High and critical problems are reported automatically.</small></div>`;
  }
  function diagnosisHtml(d) {
    if (!d) return "";
    const items = (d.findings || []).map((f) => `<li class="dx-f sev-${f.severity}"><div><span class="dx-sev sev-${f.severity}">${esc(SEV[f.severity] || f.severity)}</span> <b>${esc(f.title)}</b></div>
      <p>${esc(f.detail)}</p><p class="dx-fix"><b>Fix:</b> ${esc(f.fix)}</p>${f.link ? `<a href="${f.link}">Open →</a>` : ""}</li>`).join("");
    return `<div class="dx-card sev-${d.severity}">
      <div class="dx-head"><span class="dx-badge sev-${d.severity}">${d.severity === "none" ? "✓ " : "⚠ "}${esc(d.severity_label)}</span>
        <a href="${d.url}" class="dx-doc">#${d.packet} ${esc(d.document)}</a></div>
      <div class="dx-impact">${esc(d.impact)}</div>
      ${items ? `<ul class="dx-list">${items}</ul>` : ""}
      ${ticketHtml(d)}</div>`;
  }
  function queryHtml(q) {
    if (!q) return "";
    const chips = (q.parts || []).map((x) => `<span>${esc(x)}</span>`).join("") || "<span>all documents you can see</span>";
    const rows = (q.rows || []).map((r) => `<a href="${r.url}"><b>#${r.id} ${esc(r.name)}</b><small>${esc(r.status)}${r.step ? " · " + esc(r.step) : ""}${
      Object.entries(r.values || {}).map(([k, v]) => ` · ${esc(k)}: ${esc(v)}`).join("")}</small></a>`).join("");
    return `<div class="aq-card"><div class="aq-head"><small>Interpreted as</small><div class="aq-chips">${chips}</div></div>${
      q.value != null ? `<div class="aq-big">${Number(q.value).toLocaleString()}<small>${q.aggregate === "avg" ? "average" : "total"} ${esc(q.of || "")}</small></div>` : ""}${
      rows ? `<div class="aq-rows">${rows}</div>` : ""}${q.count > (q.rows || []).length ? `<small class="aq-more">Showing ${(q.rows || []).length} of ${q.count}</small>` : ""}</div>`;
  }
  function bubble(role, html) {
    welcome.hidden = true;
    const el = document.createElement("div");
    el.className = `chat-msg ${role}`;
    el.innerHTML = role === "bot" ? `<span class="chat-avatar sm">✦</span><div class="chat-bubble">${html}</div>` : `<div class="chat-bubble">${html}</div>`;
    log.append(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }
  function replay() {
    $$(".chat-msg", log).forEach((x) => x.remove());
    welcome.hidden = history.length > 0;
    for (const h of history) { bubble("me", esc(h.q)); bubble("bot", md(h.a) + diagnosisHtml(h.d) + queryHtml(h.x) + sourcesHtml(h.s)); }
  }
  function open() {
    root.classList.add("open"); root.setAttribute("aria-hidden", "false"); backdrop.hidden = false;
    document.body.classList.add("chat-on");
    setTimeout(() => ta.focus(), 60);
  }
  function close() { root.classList.remove("open"); root.setAttribute("aria-hidden", "true"); backdrop.hidden = true; document.body.classList.remove("chat-on"); }
  async function ask(q, mode) {
    open();
    if (busy || !q) return;
    busy = true; ta.value = ""; grow();
    bubble("me", esc(q));
    const typing = bubble("bot", '<span class="chat-typing"><i></i><i></i><i></i></span>');
    const fd = new FormData(form); fd.set("question", q); fd.append("history", JSON.stringify(history.map(({ q, a }) => ({ q, a })))); fd.append("page", page); fd.append("path", location.pathname); if (mode) fd.append("mode", mode);
    fd.append("stream", "1");
    try {
      const r = await fetch(root.dataset.url, { method: "POST", body: fd, headers: { "X-Requested-With": "fetch" } });
      let j;
      if ((r.headers.get("content-type") || "").includes("ndjson") && r.body) {
        /* the answer is written as it arrives: {"meta"} then {"t"} pieces, then {"done"} or {"error", "answer"} */
        j = { ai: true, sources: [] };
        const reader = r.body.getReader(), dec = new TextDecoder(), bub = $(".chat-bubble", typing);
        let buf = "", text = "", frame = 0;
        const paint = () => { frame = 0; bub.innerHTML = md(text) + '<span class="ai-caret" aria-hidden="true"></span>'; log.scrollTop = log.scrollHeight; };
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let nl;
          while ((nl = buf.indexOf("\n")) >= 0) {
            const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1);
            if (!line) continue;
            let m; try { m = JSON.parse(line); } catch (_) { continue; }
            if (m.meta) Object.assign(j, m.meta);
            if (m.t) { text += m.t; if (!frame) frame = requestAnimationFrame(paint); }
            if (m.error) { j.ai = false; j.error = m.error; if (!text) j.answer = m.answer; }
          }
        }
        if (frame) cancelAnimationFrame(frame);
        if (text) j.answer = text.trim();
      } else {
        j = await r.json();
      }
      const answer = j.answer || j.error || "Sorry, something went wrong.";
      $(".chat-bubble", typing).innerHTML = md(answer) + diagnosisHtml(j.diagnosis) + queryHtml(j.query) + sourcesHtml(j.sources);
      if (j.ai || j.diagnosis) { history.push({ q, a: answer, s: j.sources || [], d: j.diagnosis || null, x: j.query || null }); store.set(KEY, history.slice(-12)); }
    } catch (_) {
      $(".chat-bubble", typing).innerHTML = "<p>I couldn't reach the server. Check your connection and try again.</p>";
    }
    log.scrollTop = log.scrollHeight;
    busy = false;
  }
  const grow = () => { ta.style.height = "auto"; ta.style.height = Math.min(160, ta.scrollHeight) + "px"; };
  ta.addEventListener("input", grow);
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); } });
  form.addEventListener("submit", (e) => { e.preventDefault(); ask(ta.value.trim()); });
  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-chat-open]")) { open(); return; }
    if (e.target.closest("[data-chat-close]")) { close(); return; }
    if (e.target.closest("[data-chat-new]")) { history = []; store.set(KEY, history); replay(); ta.focus(); return; }
    const a = e.target.closest("[data-chat-ask]");
    if (a) {
      page = a.dataset.chatPage || page;
      const q = a.dataset.chatAsk;
      if (/:\s*$/.test(q)) { open(); ta.value = q; grow(); ta.focus(); } else ask(q, a.dataset.chatMode);
    }
    const tk = e.target.closest("[data-dx-ticket]");
    if (tk) {
      tk.disabled = true; tk.textContent = "Creating…";
      const fd = new FormData(form); fd.delete("question");
      fetch(tk.dataset.dxTicket, { method: "POST", body: fd, headers: { "X-Requested-With": "fetch" } }).then((r) => r.json()).then((j) => {
        const box = tk.closest(".dx-ticket"), t = j.ticket;
        box.outerHTML = ticketHtml({ ticket: t });
        const last = history[history.length - 1]; if (last && last.d) { last.d.ticket = t; store.set(KEY, history); }
      }).catch(() => { tk.disabled = false; tk.textContent = "Try again"; });
    }
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && root.classList.contains("open")) close(); });
  window.HelpChat = { open, ask };
  const chat = window.HelpChat;
  const onDoc = /^\/documents\/\d+\//.test(location.pathname);
  const ds = $("[data-doc-starters]", root), gs = $("[data-general-starters]", root);
  if (ds && gs) { ds.hidden = !onDoc; gs.hidden = onDoc; }
  if (onDoc) ta.placeholder = "Ask about this document — e.g. “what went wrong?”";
  replay();
  if (onDoc && new URLSearchParams(location.search).get("diagnose") === "1") ask("What went wrong with this document?", "diagnose");
  const art = $("[data-hc-body]");
  if (art) page = ($(".hc-article h1") || {}).textContent || "";

  /* ---------- table of contents ---------- */
  const toc = $("[data-hc-toc]");
  if (art && toc) {
    const hs = $$("h3", art);
    if (hs.length > 1) {
      hs.forEach((h, i) => { h.id = h.id || "s" + (i + 1); });
      $("ul", toc).innerHTML = hs.map((h) => `<li><a href="#${h.id}">${esc(h.textContent)}</a></li>`).join("");
      toc.hidden = false;
    }
  }

  /* ---------- was this helpful ---------- */
  const fb = $("[data-helpful]");
  const vote = (yes) => {
    const fd = new FormData(); fd.append("article", fb.dataset.article); fd.append("helpful", yes ? "1" : "0");
    fd.append("csrfmiddlewaretoken", (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || ($("[name=csrfmiddlewaretoken]") || {}).value || "");
    fetch(fb.dataset.url, { method: "POST", body: fd, headers: { "X-Requested-With": "fetch" } }).catch(() => {});
  };
  if (fb) {
    $("[data-helpful-yes]", fb).addEventListener("click", () => vote(true));
    $("[data-helpful-no]", fb).addEventListener("click", () => vote(false));
    $("[data-helpful-yes]", fb).addEventListener("click", () => { $$("button[data-helpful-yes], button[data-helpful-no]", fb).forEach((b) => b.hidden = true); $("[data-helpful-thanks]", fb).hidden = false; });
    $("[data-helpful-no]", fb).addEventListener("click", () => { $$("button[data-helpful-yes], button[data-helpful-no]", fb).forEach((b) => b.hidden = true); $("[data-helpful-ask]", fb).hidden = false; });
  }
})();
