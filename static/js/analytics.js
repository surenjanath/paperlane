/* Analytics widgets — Chart.js rendering with per-widget range & aggregation. */
(function () {
  const palette = ["#c0504d", "#4f9be0", "#5b5bd6", "#1f9d55", "#e0a030", "#8c5bd6", "#3bb3a8"];
  const charts = {};
  async function load(widget, params = {}) {
    const url = new URL(widget.dataset.url, location.origin);
    if (widget.dataset.days) url.searchParams.set("days", widget.dataset.days);
    const agg = widget.querySelector("[data-agg]");
    if (agg) url.searchParams.set("aggregation", agg.value);
    Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
    const data = await (await fetch(url)).json();
    widget.querySelector("[data-range]").textContent = data.range;
    const legend = widget.querySelector("[data-legend]");
    legend.innerHTML = data.series.map((s, i) => `<span style="margin-right:10px"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${palette[i % palette.length]};margin-right:4px"></span>${SD.escapeHtml(s.label)}</span>`).join("");
    const type = data.chart === "bar" ? "bar" : "line";
    const datasets = data.series.map((s, i) => {
      const c = palette[i % palette.length];
      return { label: s.label, data: s.data, borderColor: c, backgroundColor: data.chart === "area" ? c + "33" : c + (type === "bar" ? "cc" : "00"),
               fill: data.chart === "area", tension: 0, pointRadius: 0, borderWidth: 1.6 };
    });
    const id = widget.dataset.widget;
    if (charts[id]) charts[id].destroy();
    charts[id] = new Chart(widget.querySelector("canvas"), {
      type, data: { labels: data.labels, datasets },
      options: { responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
        plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true, ticks: { precision: 0, font: { size: 10 } }, grid: { color: "#eee" } },
                  x: { ticks: { font: { size: 10 }, maxRotation: 0, autoSkip: true }, grid: { display: false } } } },
    });
  }
  document.querySelectorAll("[data-widget]").forEach((w) => {
    load(w);
    w.querySelector("[data-agg]").addEventListener("change", () => load(w));
    w.querySelectorAll("[data-days]").forEach((b) => b.addEventListener("click", () => { w.dataset.days = b.dataset.days; load(w); }));
    w.querySelector("[data-fullscreen]").addEventListener("click", () => {
      w.classList.toggle("fullscreen");
      setTimeout(() => charts[w.dataset.widget] && charts[w.dataset.widget].resize(), 50);
    });
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") document.querySelectorAll(".widget.fullscreen").forEach((w) => w.classList.remove("fullscreen")); });
})();
