/* Paperlane document editor: elements, widgets, pages, roles, properties, design, conditions, undo/redo. */
(function () {
  "use strict";
  const { $, $$, csrf, escapeHtml: esc, toast, openModal, closeModal } = window.SD;
  const root = $("#editor");
  const read = (id) => JSON.parse($(id).textContent);
  let schema = read("#schema-data");
  if (!Array.isArray(schema.fields)) schema.fields = [];
  let design = read("#design-data") || {};
  const stepRoles = read("#roles-data");
  const COMPOSITES = read("#composites-data");
  const OPS = read("#ops-data");
  let newRoles = [];
  let activeRole = stepRoles.length ? stepRoles[0].key : "anyone";
  let selected = null;
  let dirty = false;
  const history = [], future = [];
  const ROLE_COLORS = ["#2f6fdb", "#e0731f", "#1f9d55", "#c43a8a", "#7a5bd6", "#2aa3a3", "#b7791f"];

  const LABELS = {};
  $$(".pal-item, .widget-tile").forEach((el) => (LABELS[el.dataset.type] = el.lastChild.textContent.trim()));
  const DISPLAY = new Set(["heading", "description", "divider", "page_break", "image", "youtube", "vimeo", "html"]);
  const CHOICE = new Set(["radio", "dropdown", "checkbox", "image_choice"]);

  const uid = () => Math.random().toString(16).slice(2, 12);
  const allRoles = () => [...stepRoles, ...newRoles];
  const roleName = (k) => (k === "anyone" || !k ? "Anyone" : (allRoles().find((r) => r.key === k) || { name: "Removed role" }).name);
  const roleColor = (k) => { const i = allRoles().findIndex((r) => r.key === k); return i < 0 ? "#9a9aa5" : ROLE_COLORS[i % ROLE_COLORS.length]; };

  /* ---------- history ---------- */
  function snapshot() { return JSON.stringify({ schema, design }); }
  function commit() {
    history.push(lastState);
    if (history.length > 80) history.shift();
    future.length = 0;
    lastState = snapshot();
    dirty = true;
    syncUndo();
  }
  let lastState = snapshot();
  let savedState = lastState, nameChanged = false;  // dirty = differs from the last save
  function syncUndo() { $("#undo").disabled = !history.length; $("#redo").disabled = !future.length; }
  $("#undo").addEventListener("click", () => { if (!history.length) return; future.push(snapshot()); restore(history.pop()); });
  $("#redo").addEventListener("click", () => { if (!future.length) return; history.push(snapshot()); restore(future.pop()); });
  function restore(s) { const o = JSON.parse(s); schema = o.schema; design = o.design; lastState = s; dirty = nameChanged || newRoles.length > 0 || s !== savedState; render(); syncUndo(); }
  document.addEventListener("keydown", (e) => {
    if (!(e.metaKey || e.ctrlKey) || ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
    if (e.key.toLowerCase() === "z") { e.preventDefault(); (e.shiftKey ? $("#redo") : $("#undo")).click(); }
    if (e.key.toLowerCase() === "s") { e.preventDefault(); save(); }
  });

  /* ---------- new fields ---------- */
  function counterName(type) {
    const base = type.replace(/_/g, "");
    let n = 1;
    while (schema.fields.some((f) => f.name === `${base}${n}`)) n++;
    return `${base}${n}`;
  }
  function newField(type) {
    const f = { id: uid(), type, label: LABELS[type] || type, name: "", required: false, role: activeRole };
    if (!DISPLAY.has(type)) f.name = counterName(type);
    if (type === "heading") { f.label = "Heading"; f.text = "Description"; }
    if (type === "description") { f.label = "Description"; f.text = "Add some explanatory text here."; }
    if (type === "page_break") { f.label = "New page"; }
    if (["text", "textarea", "email", "number", "phone", "currency"].includes(type)) { f.label = "Type a question"; f.placeholder = "Enter your answer here"; }
    if (CHOICE.has(type)) f.options = ["Option 1", "Option 2"];
    if (type === "image_choice") f.images = ["", ""];
    if (type === "matrix") { f.rows = ["Row 1", "Row 2"]; f.columns = ["Poor", "Fair", "Good"]; }
    if (type === "table") { f.columns = ["Column 1", "Column 2"]; f.min_rows = 3; }
    if (type === "group" || type === "repeatable") f.subfields = [{ id: "s1", type: "text", label: "Field 1" }, { id: "s2", type: "text", label: "Field 2" }];
    if (type === "formula") { f.label = "Total"; f.formula = ""; }
    if (type === "rating") f.max = 5;
    if (type === "clickwrap") { f.label = "Agreement"; f.text = "I have read and agree to the terms and conditions."; f.required = true; }
    if (type === "html") f.html = "<p><b>Custom HTML</b> content.</p>";
    if (type === "image") f.url = "";
    if (type === "youtube" || type === "vimeo") f.url = "";
    if (type === "signature" || type === "initials") f.required = true;
    return f;
  }
  function addField(type, index) {
    const f = newField(type);
    if (index === undefined) {
      const si = selected ? schema.fields.findIndex((x) => x.id === selected) : -1;
      index = si >= 0 ? si + 1 : schema.fields.length;
    }
    schema.fields.splice(index, 0, f);
    selected = f.id;
    commit();
    render();
    const el = $(`[data-fid="${f.id}"]`);
    if (el) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  /* ---------- palette interactions ---------- */
  $$(".pal-item, .widget-tile").forEach((el) => {
    el.addEventListener("click", () => addField(el.dataset.type));
    el.addEventListener("dragstart", (e) => { e.dataTransfer.setData("text/new-type", el.dataset.type); e.dataTransfer.effectAllowed = "copy"; });
  });
  $$("[data-left]").forEach((b) => b.addEventListener("click", () => {
    $$("[data-left]").forEach((x) => x.classList.toggle("on", x === b));
    ["elements", "widgets", "pages", "library"].forEach((k) => ($("#left-" + k).hidden = k !== b.dataset.left));
    if (b.dataset.left === "library") document.dispatchEvent(new Event("sd:library"));
  }));
  $$("[data-right]").forEach((b) => b.addEventListener("click", () => showRight(b.dataset.right)));
  function showRight(k) {
    $$("[data-right]").forEach((x) => x.classList.toggle("on", x.dataset.right === k));
    ["properties", "design", "conditions"].forEach((n) => ($("#right-" + n).hidden = n !== k));
  }

  /* ---------- roles ---------- */
  function renderRoles() {
    const box = $("#roles");
    box.innerHTML = [...allRoles(), { key: "anyone", name: "Anyone" }].map((r) =>
      `<div class="role-opt ${r.key === activeRole ? "on" : ""}" data-role="${r.key}"><span class="sw" style="background:${roleColor(r.key)}"></span>${esc(r.name)}${r.key === activeRole ? " ✓" : ""}</div>`).join("");
    $$(".role-opt", box).forEach((el) => el.addEventListener("click", () => {
      activeRole = el.dataset.role;
      if (selected) { const f = fieldById(selected); if (f && !DISPLAY.has(f.type)) { f.role = activeRole; commit(); } }
      render();
    }));
  }
  $("#add-role").addEventListener("click", () => {
    openModal(`<div class="modal-head"><h2>Add role</h2><button class="modal-close" data-close>×</button></div>
      <div class="modal-body"><p class="muted">A new role becomes a recipient step in the workflow when you save.</p><input type="text" id="role-name" placeholder="e.g. Manager"></div>
      <div class="modal-foot"><button class="btn" data-close>Cancel</button><button class="btn btn-dark" id="role-ok">Add role</button></div>`);
    $("#role-ok").addEventListener("click", () => {
      const name = $("#role-name").value.trim();
      if (!name) return;
      const r = { key: uid(), name };
      newRoles.push(r);
      activeRole = r.key;
      dirty = true;
      closeModal();
      render();
    });
  });

  /* ---------- canvas ---------- */
  const fieldById = (id) => schema.fields.find((f) => f.id === id);
  function pages() {
    const out = [[]];
    schema.fields.forEach((f) => { if (f.type === "page_break") out.push([f]); else out[out.length - 1].push(f); });
    return out;
  }
  function preview(f) {
    const ph = (t) => `<div class="ph">${esc(t || "")}</div>`;
    const req = f.required ? ' <span class="req">*</span>' : "";
    const lbl = `<div class="lbl">${esc(f.label || "")}${req}</div>`;
    switch (f.type) {
      case "heading": return `<h2 style="font-size:22px;text-align:${esc(f.align || "left")}">${esc(f.label)}</h2><div class="muted" style="border-bottom:1px solid var(--line);padding-bottom:6px">${esc(f.text || "")}</div>`;
      case "description": return `<div style="white-space:pre-wrap">${esc(f.text || "")}</div>`;
      case "divider": return `<hr style="margin:6px 0">`;
      case "page_break": return `<div class="muted" style="text-align:center;font-size:12px">— Page break: ${esc(f.label || "")} —</div>`;
      case "textarea": return lbl + `<div class="ph" style="height:64px">${esc(f.placeholder || "")}</div>`;
      case "signature": return lbl + `<div class="ph" style="height:70px;display:grid;place-items:center">✍ Sign here</div>`;
      case "initials": return lbl + `<div class="ph" style="height:50px;width:140px;display:grid;place-items:center">Initials</div>`;
      case "date": return lbl + ph("dd/mm/yyyy");
      case "currency": return lbl + ph("$ 0.00");
      case "formula": return lbl + `<div class="ph">ƒx ${esc(f.formula || "Set a formula")}</div>`;
      case "checkbox": case "radio":
        return lbl + (f.options || []).map((o) => `<div style="font-size:13px;margin:3px 0">${f.type === "radio" ? "◯" : "☐"} ${esc(o)}</div>`).join("");
      case "dropdown": return lbl + ph((f.options || [])[0] ? "Select…" : "No options");
      case "image_choice": return lbl + `<div class="row">${(f.options || []).map((o, i) => `<div class="ph" style="width:90px;text-align:center">${f.images && f.images[i] ? `<img src="${esc(f.images[i])}" style="width:100%;height:50px;object-fit:cover">` : "🖼"}<br>${esc(o)}</div>`).join("")}</div>`;
      case "matrix": return lbl + `<table class="table matrix"><tr><th></th>${(f.columns || []).map((c) => `<th>${esc(c)}</th>`).join("")}</tr>${(f.rows || []).map((r) => `<tr><td>${esc(r)}</td>${(f.columns || []).map(() => "<td>◯</td>").join("")}</tr>`).join("")}</table>`;
      case "table": return lbl + `<table class="table"><tr>${(f.columns || []).map((c) => `<th>${esc(c)}</th>`).join("")}</tr><tr>${(f.columns || []).map(() => "<td></td>").join("")}</tr></table>`;
      case "group": case "repeatable":
        return lbl + `<div style="border:1px dashed var(--line-strong);border-radius:6px;padding:8px">${(f.subfields || []).map((s) => `<div class="muted" style="font-size:12px">${esc(s.label)}</div>${ph("")}`).join("")}${f.type === "repeatable" ? '<div class="link" style="font-size:12px;margin-top:4px">+ Add another</div>' : ""}</div>`;
      case "attachment": return lbl + `<div class="ph" style="text-align:center">📎 Upload a file</div>`;
      case "rating": return lbl + `<div style="font-size:22px;color:#d4d4d8">${"★".repeat(+f.max || 5)}</div>`;
      case "image": return f.url ? `<img src="${esc(f.url)}" style="max-width:100%;border-radius:6px">` : `<div class="ph" style="text-align:center;height:80px;display:grid;place-items:center">🖼 Set an image URL</div>`;
      case "youtube": case "vimeo": return `<div class="ph" style="height:90px;display:grid;place-items:center">▶ ${f.type === "youtube" ? "YouTube" : "Vimeo"} ${esc(f.url || "— set a URL")}</div>`;
      case "html": return `<div style="border:1px dashed var(--line-strong);padding:6px;border-radius:6px">${f.html || ""}</div>`;
      case "clickwrap": return `<div style="font-size:13px">☐ ${esc(f.text || "")}${req}</div>`;
      default:
        if (COMPOSITES[f.type]) return lbl + `<div class="grid-2">${COMPOSITES[f.type].map(([, l]) => ph(l)).join("")}</div>`;
        return lbl + ph(f.placeholder || "");
    }
  }
  function render() {
    renderRoles();
    const canvas = $("#canvas");
    const wide = design.width === "wide" ? " wide" : "";
    const customW = design.width === "custom" && design.custom_width ? `style="max-width:${parseInt(design.custom_width)}px;background:${esc(design.background || "#fff")}"` : `style="background:${esc(design.background || "#fff")}"`;
    const pgs = pages();
    canvas.innerHTML = pgs.map((pg, pi) => `
      <div class="page-label">#${pi + 1}${pg[0] && pg[0].type === "page_break" ? " · " + esc(pg[0].label || "") : ""}</div>
      <div class="paper${wide}" ${customW} data-page="${pi}">
        ${pi === 0 && (design.logo_url || design.header_color) ? `<div style="margin:-26px -28px 18px;padding:14px 28px;background:${esc(design.header_color || "#fff")};border-radius:4px 4px 0 0">${design.logo_url ? `<img src="${esc(design.logo_url)}" style="height:36px">` : ""}</div>` : ""}
        <div class="ed-grid">${pg.map((f) => `
          <div class="ed-field ${f.width === "half" ? "half" : ""} ${f.id === selected ? "sel" : ""} ${f.condition && f.condition.rules && f.condition.rules.length ? "cond-on" : ""}" data-fid="${f.id}" draggable="true">
            ${DISPLAY.has(f.type) ? "" : `<span class="role-bar" style="background:${roleColor(f.role)}" title="${esc(roleName(f.role))}"></span>`}
            <div class="tools"><button data-act="up" title="Move up">↑</button><button data-act="down" title="Move down">↓</button><button data-act="dup" title="Duplicate">⧉</button><button data-act="del" title="Delete">🗑</button></div>
            ${preview(f)}
            ${f.hidden ? '<div class="badge badge-blue" style="margin-top:6px">Hidden for recipients</div>' : ""}${f.readonly ? '<div class="badge badge-gray" style="margin-top:6px">Disabled for recipients</div>' : ""}${f.condition && f.condition.rules && f.condition.rules.length ? '<div class="badge badge-amber" style="margin-top:6px">Used in form conditions</div>' : ""}
          </div>`).join("")}</div>
        ${pg.length <= (pi === 0 ? 0 : 1) ? `<div class="empty" style="padding:30px">Drag elements here or click them in the left panel</div>` : ""}
      </div>`).join("");
    canvas.style.fontFamily = design.font ? `"${design.font}", Inter, sans-serif` : "";
    bindCanvas();
    renderPages();
    renderProps();
    renderDesign();
    renderConditions();
  }

  function bindCanvas() {
    $$(".ed-field").forEach((el) => {
      el.addEventListener("click", (e) => {
        const act = e.target.closest("[data-act]");
        const id = el.dataset.fid;
        const i = schema.fields.findIndex((f) => f.id === id);
        if (act) {
          e.stopPropagation();
          const a = act.dataset.act;
          if (a === "del") { schema.fields.splice(i, 1); if (selected === id) selected = null; }
          if (a === "dup") { const c = JSON.parse(JSON.stringify(schema.fields[i])); c.id = uid(); if (c.name) c.name = counterName(c.type); schema.fields.splice(i + 1, 0, c); selected = c.id; }
          if (a === "up" && i > 0) [schema.fields[i - 1], schema.fields[i]] = [schema.fields[i], schema.fields[i - 1]];
          if (a === "down" && i < schema.fields.length - 1) [schema.fields[i + 1], schema.fields[i]] = [schema.fields[i], schema.fields[i + 1]];
          commit(); render(); return;
        }
        selected = id;
        const f = fieldById(id);
        if (f && f.role && !DISPLAY.has(f.type)) activeRole = f.role;
        showRight($("#right-conditions").hidden ? "properties" : "conditions");
        render();
      });
      el.addEventListener("dragstart", (e) => { e.dataTransfer.setData("text/move-id", el.dataset.fid); e.dataTransfer.effectAllowed = "move"; });
    });
    $$(".paper").forEach((paper) => {
      paper.addEventListener("dragover", (e) => { e.preventDefault(); showDropHint(paper, e.clientY); });
      paper.addEventListener("dragleave", () => $$(".drop-hint").forEach((h) => h.remove()));
      paper.addEventListener("drop", (e) => {
        e.preventDefault();
        const idx = dropIndex(paper, e.clientY);
        $$(".drop-hint").forEach((h) => h.remove());
        const t = e.dataTransfer.getData("text/new-type");
        const mv = e.dataTransfer.getData("text/move-id");
        if (t) addField(t, idx);
        else if (mv) {
          const from = schema.fields.findIndex((f) => f.id === mv);
          const [f] = schema.fields.splice(from, 1);
          schema.fields.splice(from < idx ? idx - 1 : idx, 0, f);
          commit(); render();
        }
      });
    });
  }
  function dropIndex(paper, y) {
    const items = $$(".ed-field", paper);
    for (const el of items) {
      const r = el.getBoundingClientRect();
      if (y < r.top + r.height / 2) return schema.fields.findIndex((f) => f.id === el.dataset.fid);
    }
    // end of this page: index after its last field (or before next page break)
    const pi = +paper.dataset.page;
    const pbs = schema.fields.map((f, i) => (f.type === "page_break" ? i : -1)).filter((i) => i >= 0);
    return pi < pbs.length ? pbs[pi] : schema.fields.length;
  }
  function showDropHint(paper, y) {
    $$(".drop-hint").forEach((h) => h.remove());
    const hint = document.createElement("div");
    hint.className = "drop-hint";
    const items = $$(".ed-field", paper);
    const before = items.find((el) => { const r = el.getBoundingClientRect(); return y < r.top + r.height / 2; });
    before ? paper.insertBefore(hint, before) : paper.appendChild(hint);
  }

  /* ---------- pages panel ---------- */
  function renderPages() {
    const pgs = pages();
    $("#pages-list").innerHTML = pgs.map((pg, i) => `
      <div class="row" style="padding:6px 0;border-bottom:1px solid var(--line)"><span class="grow"><b>Page ${i + 1}</b><br><small class="muted">${pg.filter((f) => f.type !== "page_break").length} element(s)${pg[0] && pg[0].type === "page_break" ? " · " + esc(pg[0].label || "") : ""}</small></span>
      ${i > 0 ? `<button class="btn btn-sm btn-icon" data-del-page="${i}" title="Delete page break">×</button>` : ""}</div>`).join("");
    $$("[data-del-page]").forEach((b) => b.addEventListener("click", () => {
      const pbs = schema.fields.map((f, i) => (f.type === "page_break" ? i : -1)).filter((i) => i >= 0);
      schema.fields.splice(pbs[+b.dataset.delPage - 1], 1);
      commit(); render();
    }));
  }
  $("#add-page").addEventListener("click", () => { selected = null; addField("page_break", schema.fields.length); });

  /* ---------- properties ---------- */
  function input(label, key, f, type = "text", extra = "") {
    const v = f[key] === undefined || f[key] === null ? "" : f[key];
    if (type === "textarea") return `<div class="field"><label>${label}</label><textarea data-k="${key}" rows="3" ${extra}>${esc(v)}</textarea></div>`;
    if (type === "checkbox") return `<label class="check"><input type="checkbox" data-k="${key}" ${v ? "checked" : ""}> ${label}</label>`;
    return `<div class="field"><label>${label}</label><input type="${type}" data-k="${key}" value="${esc(v)}" ${extra}></div>`;
  }
  function listEditor(label, key, f) {
    const arr = f[key] || [];
    return `<div class="field"><label>${label}</label><div data-list="${key}">${arr.map((o, i) => `<div class="opt-row"><input type="text" value="${esc(o)}" data-li="${i}"><button class="btn btn-sm btn-icon" data-ldel="${i}">×</button></div>`).join("")}</div><button class="btn btn-sm" data-ladd="${key}">+ Add</button></div>`;
  }
  function renderProps() {
    const box = $("#right-properties");
    const f = selected && fieldById(selected);
    if (!f) { box.innerHTML = `<div class="empty-props">Select a field to edit its content and properties</div>`; return; }
    const t = f.type;
    let h = `<div class="row" style="margin-bottom:10px"><b class="grow">${esc(LABELS[t] || t)}</b><span class="badge badge-gray">${esc(t)}</span></div>`;
    if (t === "page_break") h += input("Next page title", "label", f);
    else if (!["divider", "html", "image", "youtube", "vimeo"].includes(t)) h += input(t === "heading" ? "Heading" : "Label", "label", f);
    if (t === "heading" || t === "description") h += input(t === "heading" ? "Subtitle" : "Text", "text", f, "textarea");
    if (t === "clickwrap") h += input("Agreement text", "text", f, "textarea");
    if (!DISPLAY.has(t)) {
      h += input("Field name (used by bots, formulas & CSV)", "name", f);
      if (["text", "textarea", "email", "number", "phone", "currency", "date", "dropdown"].includes(t)) h += input("Placeholder", "placeholder", f);
      h += input("Help text", "help", f);
      h += `<div class="field"><label>Filled by</label><select data-k="role">${[...allRoles(), { key: "anyone", name: "Anyone" }].map((r) => `<option value="${r.key}" ${f.role === r.key ? "selected" : ""}>${esc(r.name)}</option>`).join("")}</select></div>`;
      h += input("Required", "required", f, "checkbox");
      h += input("Disabled for recipients (read-only, bots can still fill it)", "readonly", f, "checkbox");
      h += input("Hidden for recipients", "hidden", f, "checkbox");
      h += input("🔒 Sensitive — never sent to AI; masked for people who don't need it", "sensitive", f, "checkbox");
      if (!["signature", "initials", "attachment", "formula", "matrix", "table", "group", "repeatable", "clickwrap"].includes(t) && !COMPOSITES[t]) h += input("Default value", "default", f);
    }
    h += `<div class="field" style="margin-top:10px"><label>Width</label><select data-k="width"><option value="">Full width</option><option value="half" ${f.width === "half" ? "selected" : ""}>Half width (two columns)</option></select></div>`;
    h += input("Line above", "divider_before", f, "checkbox") + input("Line below", "divider_after", f, "checkbox");
    if (t === "heading") h += `<div class="field"><label>Alignment</label><select data-k="align">${["left", "center", "right"].map((a) => `<option ${f.align === a ? "selected" : ""}>${a}</option>`).join("")}</select></div>`;
    if (t === "date") h += `<div class="field"><label>Date format</label><select data-k="date_format"><option value="">Browser default</option>${["MM/dd/yyyy", "dd/MM/yyyy", "yyyy-MM-dd", "dd MMM yyyy"].map((d) => `<option ${f.date_format === d ? "selected" : ""}>${d}</option>`).join("")}</select></div>`;
    if (t === "attachment") h += `<div class="grid-2">${input("Max attachments", "max_files", f, "number", 'min="1"')}${input("Max file size (MB)", "max_mb", f, "number", 'min="1"')}</div>`;
    if (CHOICE.has(t)) h += listEditor("Options", "options", f);
    if (t === "checkbox" || t === "radio") h += listEditor("Option keys (optional, for conditions: name-key)", "option_values", f);
    if (t === "image_choice") h += listEditor("Image URLs (same order as options)", "images", f);
    if (t === "matrix") { h += listEditor("Rows", "rows", f); h += listEditor("Columns", "columns", f); }
    if (t === "table") { h += listEditor("Columns", "columns", f); h += input("Minimum rows", "min_rows", f, "number"); }
    if (t === "group" || t === "repeatable") {
      h += `<div class="field"><label>Sub-fields</label>${(f.subfields || []).map((s, i) => `<div class="opt-row"><input type="text" value="${esc(s.label)}" data-sub="${i}"><select data-subtype="${i}" style="width:110px">${["text", "number", "email", "date", "phone", "currency"].map((x) => `<option ${s.type === x ? "selected" : ""}>${x}</option>`).join("")}</select><button class="btn btn-sm btn-icon" data-subdel="${i}">×</button></div>`).join("")}<button class="btn btn-sm" data-subadd>+ Add sub-field</button></div>`;
    }
    if (t === "formula") {
      const names = schema.fields.filter((x) => ["number", "currency", "formula"].includes(x.type) && x.id !== f.id).map((x) => x.name || x.label);
      h += input("Formula", "formula", f, "text", 'placeholder="{Quantity} * {Unit price}"');
      h += `<div class="help" style="margin:-8px 0 12px">Use + − * / ( ), min(), max(), round(). Insert: ${names.map((n) => `<a class="link" data-ins="${esc(n)}">{${esc(n)}}</a>`).join(" ") || "add number fields first"}</div>`;
    }
    if (["number", "currency"].includes(t)) h += `<div class="grid-2">${input("Min", "min", f, "number")}${input("Max", "max", f, "number")}</div>`;
    if (["text", "textarea"].includes(t)) h += input("Max length", "max_length", f, "number");
    if (t === "text") h += input("Validation pattern (regex)", "pattern", f) + input("Pattern error message", "pattern_message", f);
    if (t === "rating") h += input("Number of stars", "max", f, "number", 'min="3" max="10"');
    if (["image", "youtube", "vimeo"].includes(t)) h += input(t === "image" ? "Image URL" : "Video URL", "url", f, "url");
    if (t === "html") h += input("HTML", "html", f, "textarea", 'rows="8" style="font-family:monospace;font-size:12px"');
    box.innerHTML = h;
    // bindings
    $$("[data-k]", box).forEach((el) => {
      const ev = el.type === "checkbox" || el.tagName === "SELECT" ? "change" : "input";
      el.addEventListener(ev, () => {
        const k = el.dataset.k;
        f[k] = el.type === "checkbox" ? el.checked : el.value;
        if (k === "role") activeRole = el.value;
        dirty = true;
        renderCanvasOnly();
      });
      el.addEventListener("change", () => commit());
    });
    $$("[data-list]", box).forEach((list) => {
      const key = list.dataset.list;
      $$("[data-li]", list).forEach((inp) => {
        inp.addEventListener("input", () => { f[key][+inp.dataset.li] = inp.value; dirty = true; renderCanvasOnly(); });
        inp.addEventListener("change", () => commit());
      });
      $$("[data-ldel]", list).forEach((b) => b.addEventListener("click", () => { f[key].splice(+b.dataset.ldel, 1); commit(); render(); }));
    });
    $$("[data-ladd]", box).forEach((b) => b.addEventListener("click", () => {
      const key = b.dataset.ladd;
      f[key] = f[key] || [];
      f[key].push(key === "images" ? "" : `${key === "rows" ? "Row" : key === "columns" ? "Column" : "Option"} ${f[key].length + 1}`);
      commit(); render();
    }));
    $$("[data-sub]", box).forEach((inp) => inp.addEventListener("input", () => { f.subfields[+inp.dataset.sub].label = inp.value; dirty = true; renderCanvasOnly(); }));
    $$("[data-subtype]", box).forEach((s) => s.addEventListener("change", () => { f.subfields[+s.dataset.subtype].type = s.value; commit(); }));
    $$("[data-subdel]", box).forEach((b) => b.addEventListener("click", () => { f.subfields.splice(+b.dataset.subdel, 1); commit(); render(); }));
    const sa = $("[data-subadd]", box);
    if (sa) sa.addEventListener("click", () => { f.subfields.push({ id: "s" + uid().slice(0, 5), type: "text", label: `Field ${f.subfields.length + 1}` }); commit(); render(); });
    $$("[data-ins]", box).forEach((a) => a.addEventListener("click", () => { f.formula = (f.formula || "") + `{${a.dataset.ins}}`; commit(); render(); }));
  }
  function renderCanvasOnly() {
    const keepFocus = document.activeElement;
    const f = selected && fieldById(selected);
    const el = f && $(`[data-fid="${f.id}"]`);
    if (el) {
      const tools = el.querySelector(".tools").outerHTML;
      const bar = el.querySelector(".role-bar");
      el.innerHTML = (bar ? `<span class="role-bar" style="background:${roleColor(f.role)}"></span>` : "") + tools + preview(f);
    }
    if (keepFocus) keepFocus.focus();
  }

  /* ---------- design ---------- */
  function renderDesign() {
    const box = $("#right-design");
    const d = design;
    box.innerHTML = `
      <h3 style="margin-bottom:10px">Form style</h3>
      <div class="field"><label>Form width</label><div class="seg">${["default", "wide", "custom"].map((w) => `<input type="radio" id="w-${w}" name="dw" value="${w}" ${((d.width || "default") === w) ? "checked" : ""}><label for="w-${w}">${w[0].toUpperCase() + w.slice(1)}</label>`).join("")}</div></div>
      ${d.width === "custom" ? `<div class="field"><label>Custom width (px)</label><input type="number" data-d="custom_width" value="${esc(d.custom_width || 720)}"></div>` : ""}
      <div class="field"><label>Custom font</label><select data-d="font">${["", "Plus Jakarta Sans", "Inter", "Roboto", "Open Sans", "Lato", "Georgia", "Merriweather"].map((f) => `<option value="${f}" ${d.font === f ? "selected" : ""}>${f || "Default"}</option>`).join("")}</select></div>
      <label class="check"><input type="checkbox" id="use-ws-logo" ${d.use_workspace_logo ? "checked" : ""}> Show the workspace logo</label>
      <div class="field"><label>Logo URL</label><input type="url" data-d="logo_url" value="${esc(d.logo_url || "")}" placeholder="https://…/logo.png"></div>
      <div class="field"><label>Header design</label><div class="row"><input type="color" data-d="header_color" value="${esc(d.header_color || "#ffffff")}"><span class="muted">Header background</span></div></div>
      <div class="field"><label>Accent color</label><div class="row"><input type="color" data-d="accent" value="${esc(d.accent || "#c8461b")}"><span class="muted">Buttons & header line</span></div></div>
      <div class="field"><label>Background color</label><div class="row"><input type="color" data-d="background" value="${esc(d.background || "#ffffff")}"><span class="muted">Paper</span></div></div>
      <div class="field"><label>Page background</label><div class="row"><input type="color" data-d="page_background" value="${esc(d.page_background || "#f3f3f6")}"></div></div>
      <label class="check"><input type="checkbox" id="set-default"> Set as default for new forms</label>
      <button class="btn btn-sm" id="reset-design" style="margin-top:8px">Reset design</button>`;
    $$("[name=dw]", box).forEach((r) => r.addEventListener("change", () => { design.width = r.value; commit(); render(); }));
    $$("[data-d]", box).forEach((el) => el.addEventListener("change", () => { design[el.dataset.d] = el.value; commit(); render(); showRight("design"); }));
    $("#use-ws-logo").addEventListener("change", (e) => { design.use_workspace_logo = e.target.checked; commit(); render(); showRight("design"); });
    $("#reset-design").addEventListener("click", () => { design = {}; commit(); render(); showRight("design"); });
  }

  /* ---------- conditions ---------- */
  function renderConditions() {
    const box = $("#right-conditions");
    const f = selected && fieldById(selected);
    if (!f) { box.innerHTML = `<div class="empty-props">Select a field to show it only when other answers match.</div>`; return; }
    const cond = f.condition || { logic: "and", rules: [] };
    const candidates = schema.fields.filter((x) => x.id !== f.id && !DISPLAY.has(x.type));
    box.innerHTML = `<h3 style="margin-bottom:6px">Show “${esc(f.label || f.type)}” when</h3>
      <p class="muted" style="font-size:12px">Hidden fields are not required. Leave empty to always show.</p>
      <div class="row" style="margin-bottom:8px;font-size:12.5px">Match <select id="c-logic" style="width:auto;height:28px"><option value="and" ${cond.logic !== "or" ? "selected" : ""}>all</option><option value="or" ${cond.logic === "or" ? "selected" : ""}>any</option></select></div>
      <div id="c-rules">${(cond.rules.length ? cond.rules : []).map((r, i) => `
        <div class="card" style="padding:8px;margin-bottom:6px">
          <select data-r="${i}" data-rk="field"><option value="">Field…</option>${candidates.map((c) => `<option value="${c.id}" ${r.field === c.id ? "selected" : ""}>${esc(c.label || c.name)}</option>`).join("")}</select>
          <div class="row" style="margin-top:6px;gap:6px;flex-wrap:nowrap"><select data-r="${i}" data-rk="op" style="width:48%">${OPS.map(([k, l]) => `<option value="${k}" ${r.op === k ? "selected" : ""}>${l}</option>`).join("")}</select>
          <input type="text" data-r="${i}" data-rk="value" value="${esc(r.value || "")}" placeholder="Value"><button class="btn btn-sm btn-icon" data-rdel="${i}">×</button></div>
        </div>`).join("")}</div>
      <button class="btn btn-sm" id="c-add">+ Add rule</button>`;
    const save = () => { f.condition = cond; commit(); renderCanvasOnly(); };
    $("#c-logic").addEventListener("change", (e) => { cond.logic = e.target.value; save(); });
    $$("[data-r]", box).forEach((el) => el.addEventListener("change", () => { cond.rules[+el.dataset.r][el.dataset.rk] = el.value; save(); }));
    $$("[data-rdel]", box).forEach((b) => b.addEventListener("click", () => { cond.rules.splice(+b.dataset.rdel, 1); save(); renderConditions(); }));
    $("#c-add").addEventListener("click", () => { cond.rules.push({ field: "", op: "eq", value: "" }); f.condition = cond; renderConditions(); });
  }

  /* ---------- assistant (rule-based field builder) ---------- */
  $("#assistant-btn").addEventListener("click", () => {
    openModal($("#assistant-tpl").innerHTML);
    $("#assistant-go").addEventListener("click", () => {
      const lines = $("#assistant-text").value.split("\n").map((l) => l.trim()).filter(Boolean);
      lines.forEach((line) => {
        const low = line.toLowerCase();
        let type = "text", options = null, label = line;
        const m = line.match(/\[(.+)\]\s*$/);
        if (m) { options = m[1].split(",").map((s) => s.trim()).filter(Boolean); label = line.replace(m[0], "").trim(); type = options.length > 4 ? "dropdown" : "radio"; }
        else if (/\(yes\/no\)|^(do|does|is|are|have|has|will|did)\b/.test(low)) { type = "radio"; options = ["Yes", "No"]; label = line.replace(/\(yes\/no\)/i, "").trim(); }
        else if (/signature|sign here/.test(low)) type = "signature";
        else if (/initials/.test(low)) type = "initials";
        else if (/e-?mail/.test(low)) type = "email";
        else if (/phone|mobile|tel/.test(low)) type = "phone";
        else if (/date|dob|birth/.test(low)) type = "date";
        else if (/amount|price|cost|salary|premium|fee|\$/.test(low)) type = "currency";
        else if (/number of|quantity|qty|age|count/.test(low)) type = "number";
        else if (/address/.test(low)) type = "address";
        else if (/full name/.test(low)) type = "full_name";
        else if (/upload|attach|document copy|receipt/.test(low)) type = "attachment";
        else if (/comment|describe|explain|details|reason/.test(low)) type = "textarea";
        else if (/agree|terms/.test(low)) type = "clickwrap";
        const f = newField(type);
        f.label = label.replace(/[?:]\s*$/, (s) => (s.includes("?") ? "?" : ""));
        if (type === "clickwrap") f.text = label;
        f.name = label.replace(/[?:()]/g, "").trim().slice(0, 60) || f.name;
        if (options) f.options = options;
        schema.fields.push(f);
      });
      commit(); closeModal(); render();
      toast(`${lines.length} field(s) added`);
    });
  });

  /* ---------- preview / save ---------- */
  async function save(then) {
    const payload = { schema: { fields: schema.fields }, design, name: $("#doc-name").value.trim(), new_roles: newRoles,
                      set_default_design: !!($("#set-default") && $("#set-default").checked) };
    const res = await fetch(root.dataset.saveUrl, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() }, body: JSON.stringify(payload) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) { toast(data.error || "Could not save (" + res.status + ")", "error"); return false; }
    stepRoles.push(...newRoles);
    newRoles = [];
    dirty = false;
    savedState = lastState = snapshot();
    nameChanged = false;
    if (!then) toast("Saved");
    return true;
  }
  $("#doc-name").addEventListener("input", () => { dirty = nameChanged = true; });
  $("#save-btn").addEventListener("click", () => save());
  $("#continue-btn").addEventListener("click", async () => { if (await save(true)) location.href = root.dataset.continueUrl; });
  $("#preview-btn").addEventListener("click", async () => { if (await save(true)) window.open(root.dataset.previewUrl, "_blank"); });
  $$("[data-leave]").forEach((a) => a.addEventListener("click", (e) => {
    if (!dirty) return;
    e.preventDefault();
    openModal(`<div class="modal-head"><h2>Unsaved changes</h2><button class="modal-close" data-close>×</button></div>
      <div class="modal-body"><p>Save your changes before leaving?</p></div>
      <div class="modal-foot"><button class="btn" id="leave-discard">Discard</button><button class="btn btn-dark" id="leave-save">Save & leave</button></div>`);
    $("#leave-discard").addEventListener("click", () => { dirty = false; location.href = a.href; });
    $("#leave-save").addEventListener("click", async () => { if (await save(true)) location.href = a.href; });
  }));
  window.addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });

  // small API for the AI builder below (it changes fields only through commit(), so undo works)
  window.SDEditor = {
    fields: () => schema.fields,
    setFields: (fields) => { schema.fields = fields; selected = null; commit(); render(); },
    roles: () => allRoles(), activeRole: () => activeRole, roleName, roleColor, showRight,
    selectedId: () => selected,
    insertFields: (block) => {  // fresh ids (conditions remapped), unique names, the active role
      const map = {};
      const copy = JSON.parse(JSON.stringify(block));
      copy.forEach((f) => { map[f.id] = uid(); });
      copy.forEach((f) => {
        f.id = map[f.id]; f.role = activeRole;
        if (f.name) { let n = f.name, i = 2; while (schema.fields.some((x) => x.name === n)) n = `${f.name} ${i++}`; f.name = n; }
        (((f.condition || {}).rules) || []).forEach((r) => { if (map[r.field]) r.field = map[r.field]; });
      });
      const si = selected ? schema.fields.findIndex((x) => x.id === selected) : -1;
      schema.fields.splice(si >= 0 ? si + 1 : schema.fields.length, 0, ...copy);
      selected = copy[0] ? copy[0].id : null; commit(); render();
      const el = $(`[data-fid="${selected}"]`); if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
      return copy.length;
    },
    docName: () => $("#doc-name").value.trim(),
    setDocName: (n) => { $("#doc-name").value = n; $("#doc-name").dispatchEvent(new Event("input", { bubbles: true })); },
  };
  render();
})();

/* ---------- ✦ AI document builder (Build · Edit · Review) — every change goes through undo ---------- */
(function () {
  "use strict";
  const { $, $$, csrf, escapeHtml: esc, toast } = window.SD;
  const root = $("#editor"), url = root.dataset.aiUrl, ed = window.SDEditor;
  if (!url || !ed) return;
  const box = $("#right-ai"), main = $(".editor-main");
  let tab = "build", pending = null, suggestions = null, busy = false, lastChanges = [];
  const EX_BUILD = ["Death claim for life insurance with bank details for direct deposit", "Change of address with proof of address upload",
                    "Employee onboarding: personal details, emergency contact, bank, policies acknowledgement", "Customer complaint with incident date, category and desired resolution"];
  const EX_EDIT = ["Make all contact fields required", "Add a declaration and signature at the end", "Put short fields side by side",
                   "Add bank details shown only when payment method is direct deposit", "Rewrite the labels to be clearer and shorter"];
  const TYPE_ICON = { heading: "H", text: "T", textarea: "¶", email: "@", date: "▦", signature: "✍", initials: "IN", number: "7", phone: "☎",
                      currency: "$", formula: "fx", checkbox: "☑", radio: "◉", dropdown: "▾", attachment: "📎", address: "📍", full_name: "👤",
                      clickwrap: "✔", table: "▤", description: "≡", contact_details: "☏", personal_info: "🪪" };

  function open() {
    $$("[data-right]").forEach((x) => x.classList.remove("on"));
    ["properties", "design", "conditions"].forEach((k) => ($("#right-" + k).hidden = true));
    box.hidden = false; main.classList.add("ai-wide"); $("#assistant-btn").classList.add("on"); render();
  }
  function close() { box.hidden = true; main.classList.remove("ai-wide"); $("#assistant-btn").classList.remove("on"); ed.showRight("properties"); }
  $("#assistant-btn").addEventListener("click", (e) => { e.stopImmediatePropagation(); box.hidden ? open() : close(); }, true);
  $$("[data-right]").forEach((b) => b.addEventListener("click", () => { box.hidden = true; main.classList.remove("ai-wide"); $("#assistant-btn").classList.remove("on"); }));

  const chips = (list, attr) => `<div class="aib-chips">${list.map((x) => `<button type="button" ${attr}="${esc(x)}">${esc(x)}</button>`).join("")}</div>`;
  const fieldRow = (f) => `<li class="${f.type === "heading" ? "h" : ""}"><span class="ti">${TYPE_ICON[f.type] || "•"}</span><span class="lb">${esc(f.label || f.type)}</span>${
    f.required ? '<em class="req">required</em>' : ""}${f.condition ? '<em class="cond">conditional</em>' : ""}${f.width === "half" ? '<em>½</em>' : ""}${
    f.role && f.role !== "anyone" ? `<em class="role" style="--rc:${ed.roleColor(f.role)}">${esc(ed.roleName(f.role))}</em>` : ""}</li>`;

  function render() {
    const n = ed.fields().length;
    let body = "";
    if (tab === "build") {
      body = pending ? `
        <div class="aib-result">
          <div class="aib-rh"><b>${esc(pending.title || "Your document")}</b><small>${pending.fields.length} fields · ${pending.fields.filter((f) => f.type === "heading").length} sections</small></div>
          ${(pending.notes || []).length ? `<ul class="aib-notes">${pending.notes.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
          <ol class="aib-fields">${pending.fields.map(fieldRow).join("")}</ol>
          <div class="aib-actions"><button class="btn btn-sm" data-aib="discard">Discard</button>
            ${n ? '<button class="btn btn-sm" data-aib="append">Add to the end</button>' : ""}
            <button class="btn btn-sm btn-dark" data-aib="replace">${n ? "Replace the form" : "Use this form"}</button></div>
        </div>` : `
        <form class="aib-form" data-aib-form="build">
          <label>Describe the document</label>
          <textarea name="text" rows="4" placeholder="e.g. Death claim for a life policy: claimant, deceased and policy, cause of death, documents, payment by cheque or direct deposit, declaration and signature."></textarea>
          ${chips(EX_BUILD, "data-ex")}
          <details class="aib-src"><summary>✚ Start from an existing form <small>(PDF, Word, photo or pasted text)</small></summary>
            <label class="aib-drop"><input type="file" name="file" accept=".pdf,.docx,.txt,.png,.jpg,.jpeg,.webp"><span>Drop or choose a file</span></label>
            <textarea name="source" rows="4" placeholder="…or paste the questions from a paper or PDF form"></textarea>
          </details>
          <p class="aib-hint">New fields are filled by <b style="color:${ed.roleColor(ed.activeRole())}">${esc(ed.roleName(ed.activeRole()))}</b> unless the AI assigns another role. You'll preview the result before anything changes.</p>
          <button class="btn btn-dark btn-block" data-busy="Designing…">✦ Design the document</button>
        </form>`;
    } else if (tab === "edit") {
      body = `<form class="aib-form" data-aib-form="edit">
          <label>What should change?</label>
          <textarea name="text" rows="4" placeholder="e.g. Add bank details shown only when the payment method is direct deposit"></textarea>
          ${chips(EX_EDIT, "data-ex")}
          <button class="btn btn-dark btn-block" data-busy="Editing…" ${n ? "" : "disabled"}>✦ Apply change</button>
          ${n ? "" : '<p class="aib-hint">The form is empty — use Build first.</p>'}
        </form>
        ${lastChanges.length ? `<div class="aib-done"><b>Done</b> <small>— press ⌘Z / Undo to revert</small><ul>${lastChanges.map((c) => `<li>${esc(c)}</li>`).join("")}</ul></div>` : ""}`;
    } else {
      body = suggestions ? (suggestions.length ? `<div class="aib-sugs">${suggestions.map((s, i) => `
          <div class="aib-sug ${s.applied ? "applied" : ""}"><div><b>${esc(s.title)}</b><small>${esc(s.why)}</small></div>
            <button class="btn btn-sm" data-sug="${i}" ${s.applied ? "disabled" : ""}>${s.applied ? "✓ Applied" : "Apply"}</button></div>`).join("")}
          <button class="btn btn-sm btn-block" data-aib="apply-all">Apply all</button>
          <button class="btn btn-sm btn-ghost btn-block" data-aib="review">↻ Review again</button></div>`
        : `<p class="aib-hint">No suggestions — this form looks solid.</p><button class="btn btn-sm" data-aib="review">↻ Review again</button>`)
        : `<p class="aib-hint">The AI checks for missing questions, wrong field types, required fields, validation, unclear labels, missing declarations and follow-ups that should be conditional.</p>
           <button class="btn btn-dark btn-block" data-aib="review" data-busy="Reviewing…" ${n ? "" : "disabled"}>✦ Review this form</button>`;
    }
    box.innerHTML = `<div class="aib">
      <div class="aib-head"><span class="aib-mark">✦</span><b>AI document builder</b><button type="button" class="aib-x" data-aib="close" aria-label="Close">×</button></div>
      <nav class="aib-tabs">${["build", "edit", "review"].map((t) => `<button type="button" data-tab="${t}" class="${tab === t ? "on" : ""}">${{ build: "Build", edit: "Edit", review: "Review" }[t]}</button>`).join("")}</nav>
      <div class="aib-body">${body}</div></div>`;
  }

  async function call(data, btn) {
    if (busy) return null;
    busy = true;
    let t0 = Date.now(), timer;
    if (btn) { btn.disabled = true; const lbl = btn.dataset.busy || "Working…"; btn.textContent = lbl; timer = setInterval(() => { btn.textContent = `${lbl} ${Math.round((Date.now() - t0) / 1000)}s`; }, 1000); }
    data.append("fields", JSON.stringify(ed.fields())); data.append("roles", JSON.stringify(ed.roles())); data.append("role", ed.activeRole());
    try {
      const r = await fetch(url, { method: "POST", body: data, headers: { "X-CSRFToken": csrf(), "X-Requested-With": "fetch" } });
      const j = await r.json();
      if (j.error) { toast(j.error, "error"); return null; }
      return j;
    } catch (e) { toast("The AI model could not be reached.", "error"); return null; }
    finally { busy = false; clearInterval(timer); if (btn && btn.isConnected) { btn.disabled = false; } }
  }
  function applyFields(fields, msg) {
    const before = new Set(ed.fields().map((f) => f.id));
    ed.setFields(fields);
    const fresh = fields.filter((f) => !before.has(f.id)).map((f) => f.id);
    setTimeout(() => fresh.forEach((id) => { const el = document.querySelector(`[data-fid="${id}"]`); if (el) el.classList.add("ai-new"); }), 30);
    if (fresh[0]) setTimeout(() => { const el = document.querySelector(`[data-fid="${fresh[0]}"]`); if (el) el.scrollIntoView({ block: "center", behavior: "smooth" }); }, 60);
    toast(msg);
  }

  box.addEventListener("click", async (e) => {
    const t = e.target.closest("[data-tab]"); if (t) { tab = t.dataset.tab; render(); return; }
    const ex = e.target.closest("[data-ex]"); if (ex) { const ta = box.querySelector("textarea[name=text]"); ta.value = ex.dataset.ex; ta.focus(); return; }
    const a = e.target.closest("[data-aib]");
    const sg = e.target.closest("[data-sug]");
    if (a && a.dataset.aib === "close") return close();
    if (a && a.dataset.aib === "discard") { pending = null; render(); return; }
    if (a && (a.dataset.aib === "replace" || a.dataset.aib === "append")) {
      const fields = a.dataset.aib === "replace" ? pending.fields : [...ed.fields(), ...pending.fields];
      if (pending.title && (!ed.docName() || /^(untitled|new form|document)/i.test(ed.docName()))) ed.setDocName(pending.title);
      applyFields(fields, `${pending.fields.length} fields ${a.dataset.aib === "replace" ? "in place" : "added"} — Undo to revert`);
      pending = null; render(); return;
    }
    if (a && a.dataset.aib === "review") {
      const fd = new FormData(); fd.append("action", "review");
      const j = await call(fd, a); if (j) { suggestions = j.suggestions || []; render(); }
      return;
    }
    if ((sg || (a && a.dataset.aib === "apply-all")) && suggestions) {
      const idx = sg ? [+sg.dataset.sug] : suggestions.map((_, i) => i).filter((i) => !suggestions[i].applied);
      const fd = new FormData(); fd.append("action", "apply"); fd.append("edits", JSON.stringify(idx.flatMap((i) => suggestions[i].edits)));
      const j = await call(fd, sg || a);
      if (j) { idx.forEach((i) => (suggestions[i].applied = true)); applyFields(j.fields, `${j.changes.length} change(s) applied — Undo to revert`); render(); }
    }
  });
  box.addEventListener("submit", async (e) => {
    const form = e.target.closest("[data-aib-form]"); if (!form) return;
    e.preventDefault();
    const fd = new FormData(form); fd.append("action", form.dataset.aibForm);
    const j = await call(fd, form.querySelector("button[data-busy]"));
    if (!j) return;
    if (form.dataset.aibForm === "build") { pending = j; render(); }
    else { lastChanges = [...(j.changes || []), ...(j.notes || [])]; if ((j.changes || []).length) applyFields(j.fields, `${j.changes.length} change(s) — Undo to revert`); else toast("Nothing changed — try rephrasing.", "error"); render(); }
  });
  box.addEventListener("change", (e) => {
    if (e.target.name === "file" && e.target.files[0]) e.target.closest(".aib-drop").querySelector("span").textContent = "📄 " + e.target.files[0].name;
  });
  box.addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && e.target.tagName === "TEXTAREA") e.target.form && e.target.form.requestSubmit(); });
})();


/* ---------- field library (starter + workspace sections) ---------- */
(function () {
  "use strict";
  const { $, csrf, escapeHtml: esc, toast } = window.SD;
  const box = $("#left-library"), ed = window.SDEditor;
  if (!box || !ed) return;
  let blocks = null;
  async function load() {
    const j = await (await fetch(box.dataset.url, { headers: { "X-Requested-With": "fetch" } })).json();
    blocks = j.blocks || []; render();
  }
  function sectionOf(id) {
    const fs = ed.fields(), i = fs.findIndex((f) => f.id === id);
    if (i < 0) return [];
    if (fs[i].type !== "heading") return [fs[i]];
    let j = i + 1; while (j < fs.length && fs[j].type !== "heading" && fs[j].type !== "page_break") j++;
    return fs.slice(i, j);
  }
  function render() {
    const sel = ed.selectedId(), sec = sel ? sectionOf(sel) : [];
    const row = (b) => `<div class="lib-item"><button type="button" class="lib-add" data-add="${b.custom ? "c" + b.id : b.key}" title="Insert after the selected field">
        <b>${esc(b.name)}</b><small>${esc(b.description || "")}${b.description ? " · " : ""}${b.count} field${b.count === 1 ? "" : "s"}</small></button>
        ${b.custom ? `<button type="button" class="lib-del" data-del="${b.id}" aria-label="Remove ${esc(b.name)} from the library" title="Remove">×</button>` : ""}</div>`;
    box.innerHTML = `<div class="lib">
      <div class="lib-save">${sec.length ? `<b>Save ${sec[0].type === "heading" ? `the “${esc(sec[0].label)}” section (${sec.length} fields)` : `“${esc(sec[0].label)}”`}</b>
        <input type="text" id="lib-name" value="${esc(sec[0].label || "")}" maxlength="120" aria-label="Library name"><button type="button" class="btn btn-sm btn-block" id="lib-save">＋ Save to library</button>`
        : `<small class="muted">Select a heading (to save its section) or a field to add it to your library.</small>`}</div>
      ${blocks === null ? '<p class="muted small" style="padding:10px">Loading…</p>' : `
      ${blocks.some((b) => b.custom) ? `<div class="lib-h">Your workspace</div>${blocks.filter((b) => b.custom).map(row).join("")}` : ""}
      <div class="lib-h">Starter sections</div>${blocks.filter((b) => !b.custom).map(row).join("")}`}</div>`;
  }
  document.addEventListener("sd:library", () => (blocks === null ? (render(), load()) : render()));
  document.addEventListener("click", (e) => { if (!box.hidden && e.target.closest("#canvas")) setTimeout(render, 0); });
  box.addEventListener("click", async (e) => {
    const add = e.target.closest("[data-add]");
    if (add) {
      const k = add.dataset.add, b = blocks.find((x) => (x.custom ? "c" + x.id : x.key) === k);
      const n = ed.insertFields(b.fields);
      toast(`${b.name}: ${n} field${n === 1 ? "" : "s"} added — Undo to revert`);
      if (b.custom) fetch(box.dataset.url + "?used=" + b.id, { method: "POST", headers: { "X-CSRFToken": csrf() } });
      render(); return;
    }
    const del = e.target.closest("[data-del]");
    if (del) {
      await fetch(box.dataset.url + "?delete=" + del.dataset.del, { method: "POST", headers: { "X-CSRFToken": csrf() } });
      blocks = blocks.filter((b) => !(b.custom && String(b.id) === del.dataset.del)); render(); return;
    }
    if (e.target.id === "lib-save") {
      const sec = sectionOf(ed.selectedId());
      const r = await fetch(box.dataset.url, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
        body: JSON.stringify({ name: $("#lib-name").value.trim(), fields: sec }) });
      const j = await r.json();
      if (j.error) { toast(j.error, "error"); return; }
      toast("Saved to the library — available in every form of this workspace."); blocks = null; load();
    }
  });
})();
