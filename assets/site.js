(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];

  // copy buttons: data-copy="text" or data-copy-panel (copies the panel's commands without prompts or comments)
  const flash = (btn) => {
    const label = btn.lastChild;
    const before = label.textContent;
    btn.classList.add("done");
    label.textContent = "Copied";
    setTimeout(() => { btn.classList.remove("done"); label.textContent = before; }, 1600);
  };
  const write = async (text) => {
    try { await navigator.clipboard.writeText(text); return true; } catch {
      const ta = Object.assign(document.createElement("textarea"), { value: text });
      ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.append(ta); ta.select();
      const ok = document.execCommand("copy"); ta.remove(); return ok;
    }
  };
  $$("[data-copy], [data-copy-panel]").forEach((btn) => btn.addEventListener("click", async () => {
    let text = btn.dataset.copy;
    if (text === undefined) {
      text = $("pre", btn.closest(".panel")).innerText.split("\n")
        .filter((l) => !l.trim().startsWith("#")).map((l) => l.replace(/^\$ /, "")).join("\n").trim();
    }
    if (await write(text)) flash(btn);
  }));

  // tabs (arrow keys move between them)
  const tabs = $$(".tab");
  const select = (tab) => {
    tabs.forEach((t) => {
      const on = t === tab;
      t.setAttribute("aria-selected", on); t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
    });
  };
  tabs.forEach((t, i) => {
    t.addEventListener("click", () => select(t));
    t.addEventListener("keydown", (e) => {
      const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (!d) return;
      const next = tabs[(i + d + tabs.length) % tabs.length];
      select(next); next.focus(); e.preventDefault();
    });
  });

  // nav: solid once scrolled; mobile menu
  const nav = $("#nav");
  const onScroll = () => nav.classList.toggle("is-stuck", scrollY > 8);
  onScroll(); addEventListener("scroll", onScroll, { passive: true });
  const menu = $("#menu");
  const setMenu = (open) => {
    nav.classList.toggle("open", open);
    menu.setAttribute("aria-expanded", open);
    menu.setAttribute("aria-label", open ? "Close menu" : "Open menu");
    $("use", menu).setAttribute("href", open ? "#i-close" : "#i-menu");
  };
  menu.addEventListener("click", () => setMenu(!nav.classList.contains("open")));
  $$(".nav-links a, .nav-cta a").forEach((a) => a.addEventListener("click", () => setMenu(false)));
  addEventListener("keydown", (e) => { if (e.key === "Escape") setMenu(false); });

  // reveal on scroll (CSS skips the motion for prefers-reduced-motion)
  const items = $$(".reveal");
  if ("IntersectionObserver" in window) {
    const io = new IntersectionObserver((entries) => entries.forEach((en) => {
      if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); }
    }), { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
    items.forEach((el) => io.observe(el));
  } else items.forEach((el) => el.classList.add("in"));

  // live star count (cached for an hour; silently skipped if GitHub is unreachable or rate-limited)
  const show = (n) => $$("[data-stars]").forEach((el) => { el.textContent = n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n); });
  try {
    const cached = JSON.parse(localStorage.getItem("pl-stars") || "null");
    if (cached && Date.now() - cached.t < 3600e3) show(cached.n);
    else fetch("https://api.github.com/repos/surenjanath/paperlane", { headers: { Accept: "application/vnd.github+json" } })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d && typeof d.stargazers_count === "number") {
          show(d.stargazers_count);
          try { localStorage.setItem("pl-stars", JSON.stringify({ n: d.stargazers_count, t: Date.now() })); } catch {}
        }
      }).catch(() => {});
  } catch {}

  const yr = $("#yr"); if (yr) yr.textContent = new Date().getFullYear();
})();
