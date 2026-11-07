/* Paperlane start forms: in-form bots. When a trigger field is filled, ask the server to look the value up and
   fill the mapped fields (conditions and formulas update through the normal change events). */
(function () {
  "use strict";
  const form = document.querySelector("[data-intake-form]");
  const dataEl = document.getElementById("lookups-data");
  if (!form || !dataEl) return;
  const lookups = JSON.parse(dataEl.textContent || "[]");
  const urlFor = (id) => form.dataset.lookupUrl.replace("LID", encodeURIComponent(id));
  const nameOf = (fid) => `f_form_${fid}`;
  const inputsOf = (fid) => [...form.querySelectorAll(`[name="${nameOf(fid)}"]`)];
  const wrapOf = (fid) => form.querySelector(`.ff[data-field="${CSS.escape(fid)}"]`);

  function currentValue(fid) {
    const els = inputsOf(fid);
    if (!els.length) return "";
    if (els[0].type === "radio") return (els.find((e) => e.checked) || {}).value || "";
    if (els[0].type === "checkbox") return els.filter((e) => e.checked).map((e) => e.value).join(", ");
    return els[0].value;
  }
  function allValues() {
    const out = {};
    form.querySelectorAll("[name^='f_form_']").forEach((el) => {
      const fid = el.name.slice(7).split("__")[0];
      if (el.type === "file") return;
      if ((el.type === "radio" || el.type === "checkbox") && !el.checked) return;
      out[fid] = el.value;
    });
    return out;
  }
  function setValue(fid, value) {
    const els = inputsOf(fid);
    const wrap = wrapOf(fid);
    const v = value == null ? "" : String(value);
    if (!els.length) {  // read-only field: update the displayed value
      const ro = wrap && wrap.querySelector(".ro");
      if (ro) ro.textContent = v || "—";
    } else if (els[0].type === "radio") {
      els.forEach((e) => { e.checked = e.value.toLowerCase() === v.toLowerCase(); });
    } else if (els[0].type === "checkbox") {
      const parts = v.split(",").map((s) => s.trim().toLowerCase());
      els.forEach((e) => { e.checked = parts.includes(e.value.toLowerCase()); });
    } else if (els[0].tagName === "SELECT") {
      const opt = [...els[0].options].find((o) => o.value.toLowerCase() === v.toLowerCase() || o.text.toLowerCase() === v.toLowerCase());
      if (opt) els[0].value = opt.value;
    } else if (els[0].type === "date" && /^\d{1,2}\/\d{1,2}\/\d{4}$/.test(v)) {
      const [d, m, y] = v.split("/");
      els[0].value = `${y}-${m.padStart(2, "0")}-${d.padStart(2, "0")}`;
    } else {
      els[0].value = v;
    }
    els.forEach((e) => { e.dispatchEvent(new Event("input", { bubbles: true })); e.dispatchEvent(new Event("change", { bubbles: true })); });
    if (wrap) { wrap.classList.add("lk-filled"); setTimeout(() => wrap.classList.remove("lk-filled"), 1600); }
  }
  function status(fid, text, cls) {
    const wrap = wrapOf(fid);
    if (!wrap) return;
    let el = wrap.querySelector(".lk-status");
    if (!el) { el = document.createElement("div"); el.className = "lk-status"; el.setAttribute("aria-live", "polite"); wrap.appendChild(el); }
    el.className = "lk-status " + (cls || "");
    el.textContent = text || "";
  }

  const csrf = () => (form.querySelector("[name=csrfmiddlewaretoken]") || {}).value || "";
  const last = {};
  async function run(lk) {
    const value = currentValue(lk.trigger).trim();
    if (value.length < (lk.min_length || 1) || last[lk.id] === value) return;
    last[lk.id] = value;
    status(lk.trigger, "Looking up…", "busy");
    try {
      const res = await fetch(urlFor(lk.id), { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
        body: JSON.stringify({ value, values: allValues() }) });
      const data = await res.json();
      if (currentValue(lk.trigger).trim() !== value) return;  // typing moved on
      if (data.found) Object.entries(data.values || {}).forEach(([fid, v]) => setValue(fid, v));
      status(lk.trigger, data.message || "", data.found ? "ok" : "no");
    } catch (e) {
      status(lk.trigger, "Lookup unavailable — you can fill the fields yourself.", "no");
      last[lk.id] = null;
    }
  }
  lookups.forEach((lk) => {
    let timer = null;
    inputsOf(lk.trigger).forEach((el) => {
      el.addEventListener("change", () => run(lk));
      el.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => run(lk), 600); });
    });
    if (currentValue(lk.trigger)) run(lk);  // e.g. after a failed submit
  });
})();
