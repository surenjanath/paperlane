/* Recipient strings come from the page (translated server-side). */
const FILL_T = (() => { try { return JSON.parse(document.getElementById("fill-i18n").textContent); } catch (_) { return {}; } })();
const ft = (k, fallback, n) => (FILL_T[k] || fallback).replace("%(n)s", n);
/* Paperlane recipient experience: signature pads, pagination, conditional fields, formulas, dynamic rows. */
(function () {
  "use strict";
  const { $, $$ } = window.SD;

  /* ---------- clear a field's validation error once the recipient changes it ---------- */
  function clearError(e) {
    const ff = e.target.closest && e.target.closest(".ff.err");
    if (!ff) return;
    ff.classList.remove("err");
    const msg = ff.querySelector(":scope > .err-msg");
    if (msg) msg.remove();
  }
  document.addEventListener("input", clearError);
  document.addEventListener("change", clearError);

  /* ---------- signature pads ---------- */
  $$("[data-sig]").forEach((box) => {
    const canvas = box.querySelector("canvas");
    const hidden = box.querySelector("input[type=hidden]");
    const ctx = canvas.getContext("2d");
    let drawing = false, dirty = false, last = null;
    function size() {
      const r = canvas.getBoundingClientRect();
      const ratio = window.devicePixelRatio || 1;
      canvas.width = r.width * ratio; canvas.height = r.height * ratio;
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.lineWidth = 2.2; ctx.lineCap = "round"; ctx.lineJoin = "round"; ctx.strokeStyle = "#1a237e";
      if (hidden.value) drawImage(hidden.value);
    }
    function drawImage(src) {
      const img = new Image();
      img.onload = () => {
        const r = canvas.getBoundingClientRect();
        ctx.clearRect(0, 0, r.width, r.height);
        const scale = Math.min(r.width / img.width, r.height / img.height, 1);
        ctx.drawImage(img, (r.width - img.width * scale) / 2, (r.height - img.height * scale) / 2, img.width * scale, img.height * scale);
      };
      img.src = src;
    }
    const pos = (e) => { const r = canvas.getBoundingClientRect(); const p = e.touches ? e.touches[0] : e; return { x: p.clientX - r.left, y: p.clientY - r.top }; };
    const start = (e) => { e.preventDefault(); drawing = true; last = pos(e); };
    const move = (e) => {
      if (!drawing) return;
      e.preventDefault();
      const p = pos(e);
      ctx.beginPath(); ctx.moveTo(last.x, last.y); ctx.lineTo(p.x, p.y); ctx.stroke();
      last = p; dirty = true;
    };
    const end = () => { if (drawing && dirty) hidden.value = canvas.toDataURL("image/png"); drawing = false; };
    canvas.addEventListener("mousedown", start); canvas.addEventListener("mousemove", move); window.addEventListener("mouseup", end);
    canvas.addEventListener("touchstart", start, { passive: false }); canvas.addEventListener("touchmove", move, { passive: false }); canvas.addEventListener("touchend", end);
    box.querySelector("[data-sig-clear]").addEventListener("click", () => { const r = canvas.getBoundingClientRect(); ctx.clearRect(0, 0, r.width, r.height); hidden.value = ""; dirty = false; });
    box.querySelector("[data-sig-type]").addEventListener("click", () => {
      window.SD.openModal(`<div class="modal-head"><h2>Type your ${box.dataset.sig === "initials" ? "initials" : "signature"}</h2><button class="modal-close" data-close>×</button></div>
        <div class="modal-body"><input type="text" id="typed-sig" class="sig-type" style="height:56px" placeholder="Your name"></div>
        <div class="modal-foot"><button class="btn" data-close>Cancel</button><button class="btn btn-dark" id="typed-ok">Apply</button></div>`);
      $("#typed-ok").addEventListener("click", () => {
        const text = $("#typed-sig").value.trim();
        if (!text) return;
        const c = document.createElement("canvas");
        c.width = 600; c.height = 160;
        const x = c.getContext("2d");
        x.fillStyle = "#1a237e"; x.font = "64px Caveat, 'Brush Script MT', cursive"; x.textBaseline = "middle";
        x.fillText(text, 20, 85);
        hidden.value = c.toDataURL("image/png");
        drawImage(hidden.value);
        window.SD.closeModal();
      });
    });
    const def = box.querySelector("[data-sig-default]");
    if (def) def.addEventListener("click", () => { if (def.dataset.sigDefault) { hidden.value = def.dataset.sigDefault; drawImage(hidden.value); } });
    size();
    window.addEventListener("resize", size);
  });

  /* ---------- pagination (one document on screen; pages stay inside that document) ---------- */
  const wraps = $$("[data-doc-wrap]");
  function showDoc(el, scroll) {
    const wrap = el && el.closest ? (el.closest("[data-doc-wrap]") || el) : el;
    const i = wraps.indexOf(wrap);
    if (i < 0) return;
    wraps.forEach((w, j) => { w.hidden = j !== i; });
    $$("[data-doc-tab]").forEach((t) => {  // document tabs follow whichever document is on screen
      const on = Number(t.dataset.docTab) === i;
      t.classList.toggle("on", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
    });
    wraps.forEach((w) => {
      const prev = w.querySelector("[data-doc-prev]"), next = w.querySelector("[data-doc-next]");
      if (prev) prev.disabled = i === 0;
      if (next) next.disabled = i === wraps.length - 1;
    });
    requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
    if (scroll) window.scrollTo(0, 0);
  }
  window.SD.showFillDoc = showDoc;
  window.SD.revealFillField = (ff) => {
    const pg = ff.closest(".fill-pg"), doc = ff.closest("[data-doc]");
    if (doc && pg) $$(".fill-pg", doc).forEach((p) => { p.hidden = p !== pg; });
    showDoc(ff, false);
  };
  $$("[data-doc-tab]").forEach((t) => t.addEventListener("click", () => showDoc(wraps[Number(t.dataset.docTab)], false)));
  wraps.forEach((w, idx) => {
    const prev = w.querySelector("[data-doc-prev]"), next = w.querySelector("[data-doc-next]");
    if (prev) prev.addEventListener("click", () => showDoc(wraps[idx - 1], true));
    if (next) next.addEventListener("click", () => showDoc(wraps[idx + 1], true));
  });
  $$("[data-doc]").forEach((doc) => {
    const pages = $$(".fill-pg", doc);
    const show = (i) => { pages.forEach((p, j) => (p.hidden = j !== i)); doc.scrollIntoView({ behavior: "smooth", block: "start" }); };
    pages.forEach((p, i) => {
      const prev = p.querySelector("[data-prev]"), next = p.querySelector("[data-next]");
      if (prev) prev.addEventListener("click", () => show(i - 1));
      if (next) next.addEventListener("click", () => show(i + 1));
    });
    const errPage = pages.findIndex((p) => p.querySelector(".ff.err"));
    if (errPage > 0) show(errPage);
  });
  const errWrap = wraps.find((w) => w.querySelector(".ff.err"));
  if (errWrap) showDoc(errWrap, false);

  /* ---------- values / conditions / formulas ---------- */
  function valueOf(ff) {
    const inputs = $$("input, select, textarea", ff).filter((i) => i.name && i.name.startsWith("f_"));
    if (!inputs.length) {
      const ro = ff.querySelector(".ro");
      return ro ? ro.textContent.trim() : "";
    }
    const type = ff.dataset.type;
    if (type === "checkbox") {
      const opts = inputs.filter((i) => i.type === "checkbox");
      return opts.length === 1 && !opts[0].value.length ? opts[0].checked : opts.filter((i) => i.checked).map((i) => i.value);
    }
    if (type === "clickwrap") return inputs[0].checked;
    const radios = inputs.filter((i) => i.type === "radio");
    if (radios.length) { const c = radios.find((i) => i.checked); return c ? c.value : ""; }
    if (inputs.length === 1) return inputs[0].value;
    return inputs.map((i) => i.value).filter(Boolean).join(", ");
  }
  function values() {
    const out = {};
    $$(".ff[data-field]").forEach((ff) => {
      const v = valueOf(ff);
      out[ff.dataset.field] = v;
      if (ff.dataset.name) out[ff.dataset.name] = v;
      if (ff.dataset.label && !(ff.dataset.label in out)) out[ff.dataset.label] = v;
      if (ff.dataset.optionKeys) {
        const keys = JSON.parse(ff.dataset.optionKeys);
        Object.entries(keys).forEach(([label, key]) => (out[key] = Array.isArray(v) && v.includes(label)));
      }
    });
    return out;
  }
  const num = (v) => { const n = parseFloat(String(v).replace(/[,$\s]/g, "")); return isNaN(n) ? null : n; };
  const text = (v) => (Array.isArray(v) ? v.join(", ") : typeof v === "boolean" ? (v ? "yes" : "no") : String(v ?? "")).trim().toLowerCase();
  function check(rule, vals) {
    if (!rule.field) return true;
    const a = vals[rule.field], e = String(rule.value ?? "").trim().toLowerCase();
    switch (rule.op) {
      case "empty": return a === "" || a === false || a == null || (Array.isArray(a) && !a.length);
      case "not_empty": return !(a === "" || a === false || a == null || (Array.isArray(a) && !a.length));
      case "gt": case "lt": case "gte": case "lte": {
        const x = num(a), y = num(rule.value);
        if (x === null || y === null) return false;
        return { gt: x > y, lt: x < y, gte: x >= y, lte: x <= y }[rule.op];
      }
      case "neq": return Array.isArray(a) ? !a.map((x) => x.toLowerCase()).includes(e) : text(a) !== e;
      case "contains": return text(a).includes(e);
      case "not_contains": return !text(a).includes(e);
      default: return Array.isArray(a) ? a.map((x) => x.toLowerCase()).includes(e) : text(a) === e;
    }
  }
  function evaluate(cond, vals) {
    const rules = (cond.rules || []).filter((r) => r.field);
    if (!rules.length) return true;
    const res = rules.map((r) => check(r, vals));
    return cond.logic === "or" ? res.some(Boolean) : res.every(Boolean);
  }
  function computeFormula(expr, vals) {
    if (!expr) return "";
    const src = expr.replace(/\{([^}]+)\}/g, (_, k) => { const n = num(vals[k.trim()]); return n === null ? "0" : String(n); });
    const toks = src.match(/\d+(\.\d+)?(e[-+]?\d+)?|[a-z]+|\*\*|[-+*/%(),]/gi) || [];
    let i = 0;
    const FN = { min: Math.min, max: Math.max, abs: Math.abs, round: (v, d = 0) => +Number(v).toFixed(d), sum: (...a) => a.reduce((s, x) => s + x, 0) };
    const peek = () => toks[i], eat = () => toks[i++];
    function expr_() { let v = term(); while (peek() === "+" || peek() === "-") v = eat() === "+" ? v + term() : v - term(); return v; }
    function term() { let v = factor(); while (["*", "/", "%"].includes(peek())) { const op = eat(), r = factor(); v = op === "*" ? v * r : op === "/" ? v / r : v % r; } return v; }
    // same rules as the server (Python): ** binds tighter than a leading minus and groups right to left
    function factor() {
      if (peek() === "-") { eat(); return -factor(); }
      if (peek() === "+") { eat(); return factor(); }
      const b = atom();
      if (peek() === "**") { eat(); const e = factor(); if (Math.abs(e) > 1000) throw 0; return Math.pow(b, e); }
      return b;
    }
    function atom() {
      const t = eat();
      if (t === "(") { const v = expr_(); if (eat() !== ")") throw 0; return v; }
      if (t && FN[t.toLowerCase()]) {
        if (eat() !== "(") throw 0;
        const args = [expr_()];
        while (peek() === ",") { eat(); args.push(expr_()); }
        if (eat() !== ")") throw 0;
        return FN[t.toLowerCase()](...args);
      }
      const n = parseFloat(t);
      if (isNaN(n)) throw 0;
      return n;
    }
    try {
      const r = expr_();
      if (i !== toks.length || !Number.isFinite(r)) return "";
      return String(Math.round(r * 100) / 100);
    } catch (_) { return ""; }
  }
  function refresh() {
    const vals = values();
    $$("[data-formula]").forEach((inp) => {
      const v = computeFormula(inp.dataset.formula, vals);
      if (inp.value !== v) { inp.value = v; }
    });
    const vals2 = values();
    $$(".ff[data-cond]").forEach((ff) => {
      try { ff.hidden = !evaluate(JSON.parse(ff.dataset.cond), vals2); } catch (_) { /* ignore */ }
    });
  }
  document.addEventListener("input", (e) => { if (e.target.closest("#fill-form")) refresh(); });
  document.addEventListener("change", (e) => { if (e.target.closest("#fill-form")) refresh(); });
  refresh();

  /* ---------- dynamic rows ---------- */
  document.addEventListener("click", (e) => {
    const addRow = e.target.closest("[data-add-row]");
    if (addRow) {
      const table = addRow.closest(".ff").querySelector("table[data-grow]");
      const rows = $$("tr", table);
      const lastRow = rows[rows.length - 1];
      const clone = lastRow.cloneNode(true);
      $$("input", clone).forEach((i) => { i.value = ""; i.name = i.name.replace(/__r(\d+)__/, (_, n) => `__r${+n + 1}__`); });
      table.appendChild(clone);
    }
    const addRep = e.target.closest("[data-add-repeat]");
    if (addRep) {
      const wrap = addRep.closest(".ff").querySelector("[data-repeat]");
      const rows = $$("[data-repeat-row]", wrap);
      const clone = rows[rows.length - 1].cloneNode(true);
      $$("input", clone).forEach((i) => { i.value = ""; i.name = i.name.replace(/__(\d+)__/, (_, n) => `__${+n + 1}__`); });
      wrap.appendChild(clone);
    }
  });

  /* scroll to first error */
  const firstErr = $(".ff.err");
  if (firstErr) setTimeout(() => {
    if (window.SD.revealFillField) window.SD.revealFillField(firstErr);
    firstErr.scrollIntoView({ behavior: "smooth", block: "center" });
  }, 200);
})();

/* ---------- required-fields progress: “N left · Next” jumps to the next empty required field ---------- */
(function () {
  const btn = document.querySelector("[data-next-field]");
  if (!btn) return;
  const filled = (ff) => {
    if ([...ff.querySelectorAll("input[type=radio], input[type=checkbox]")].some((i) => i.checked)) return true;
    if ([...ff.querySelectorAll("input[type=file]")].some((i) => i.files && i.files.length)) return true;
    if (ff.querySelector(".file-list li, .att-list li, [data-file-id]")) return true;
    return [...ff.querySelectorAll("input:not([type=radio]):not([type=checkbox]):not([type=file]), textarea, select")]
      .some((i) => (i.value || "").trim() !== "");
  };
  /* Condition-hidden fields set hidden on the field. Other documents are hidden on their card, so they still count. */
  const counts = (ff) => ff.querySelector(".req") && !ff.hidden;
  const todo = () => [...document.querySelectorAll(".ff:not(.locked)")].filter((ff) => counts(ff) && !filled(ff));
  const update = () => {
    const left = todo();
    const total = [...document.querySelectorAll(".ff:not(.locked)")].filter(counts).length;
    document.querySelectorAll(".ff:not(.locked)").forEach((ff) => ff.classList.toggle("ff-ok", counts(ff) && filled(ff)));
    const wraps = [...document.querySelectorAll("[data-doc-wrap]")];
    const line = document.querySelector("[data-todo-line]");
    if (line && total) line.textContent = left.length ? (left.length > 1 ? ft("todo_many", "%(n)s required fields left to fill.", left.length) : ft("todo_one", "1 required field left to fill.")) : ft("todo_done", "Everything required is filled in. Check it, then complete this step.");
    document.querySelectorAll("[data-doc-left]").forEach((b) => {  // per-document “N left” on the tabs
      const w = wraps[Number(b.dataset.docLeft)];
      const n = w ? left.filter((ff) => w.contains(ff)).length : 0;
      b.textContent = n ? ft("tab_left", "%(n)s left", n) : ft("tab_done", "✓ Done");
      b.classList.toggle("done", !n);
    });
    if (!total) { btn.hidden = true; return; }
    btn.hidden = false;
    btn.classList.toggle("done", !left.length);
    btn.textContent = left.length ? (left.length > 1 ? ft("left_many", "%(n)s required fields left · Next ↓", left.length) : ft("left_one", "1 required field left · Next ↓")) : ft("all_done", "✓ All required fields filled");
  };
  btn.addEventListener("click", () => {
    const next = todo()[0];
    if (!next) return;
    if (window.SD.revealFillField) window.SD.revealFillField(next);
    next.scrollIntoView({ behavior: "smooth", block: "center" });
    next.classList.remove("ff-flash"); void next.offsetWidth; next.classList.add("ff-flash");
    const input = next.querySelector("input:not([type=hidden]), textarea, select, canvas");
    setTimeout(() => input && input.focus && input.focus({ preventScroll: true }), 350);
  });
  document.addEventListener("input", update);
  document.addEventListener("change", update);
  document.addEventListener("click", () => setTimeout(update, 50));
  update();
})();

/* ---------- ✦ Fill from text (AI) — fills editable inputs; the recipient still reviews and completes ---------- */
document.addEventListener("submit", async (e) => {
  const form = e.target.closest("[data-ai-fill]");
  if (!form) return;
  e.preventDefault();
  const btn = form.querySelector("button:not([data-close])"), err = form.querySelector("[data-ai-err]");
  btn.disabled = true; btn.textContent = ft("reading", "Reading…"); err.hidden = true;
  try {
    const r = await fetch(form.dataset.aiFill, { method: "POST", body: new FormData(form), headers: { "X-Requested-With": "fetch" } });
    const j = await r.json();
    if (j.error) throw new Error(j.error);
    const page = document.getElementById("fill-form");
    let n = 0;
    for (const [name, value] of Object.entries(j.values || {})) {
      const els = [...page.querySelectorAll(`[name="${CSS.escape(name)}"]`)];
      if (!els.length) continue;
      const vals = Array.isArray(value) ? value.map(String) : [String(value)];
      for (const el of els) {
        if (el.type === "radio" || el.type === "checkbox") el.checked = el.value === "on" ? value === true || /^(yes|true|1)$/i.test(String(value)) : vals.includes(el.value);
        else el.value = vals.join(", ");
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
      }
      const ff = els[0].closest(".ff");
      if (ff) { ff.classList.remove("ff-flash"); void ff.offsetWidth; ff.classList.add("ff-flash"); }
      n++;
    }
    window.SD && window.SD.closeModal();
    window.SD && window.SD.toast && window.SD.toast(n ? (n > 1 ? ft("filled_many", "AI filled %(n)s fields — please check them.", n) : ft("filled_one", "AI filled 1 field — please check it.")) : ft("nothing", "AI found nothing to fill in that text."));
  } catch (ex) {
    err.textContent = ex.message || ft("unreachable", "The AI model could not be reached."); err.hidden = false;
  }
  btn.disabled = false; btn.textContent = "✦ " + ft("fill_fields", "Fill fields");
});
