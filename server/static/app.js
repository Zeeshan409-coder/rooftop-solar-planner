const $ = (s) => document.querySelector(s);

const MODE = {
  use_battery: "Battery powers the house",
  hold: "Battery held for later",
  charge_solar: "Charging from solar",
  charge_grid: "Charging from grid",
  idle: "Solar covers the house",
  grid: "Battery empty",
};
const STRATEGY = {
  grid_only: "No solar",
  solar_only: "Solar, no battery",
  self_use: "Solar + battery, standard mode",
  planner: "Planner",
  planner_perfect: "Planner with perfect forecasts",
};
const DEFAULT_TASKS = [
  { name: "Water pump", kw: 0.75, hours: 1, earliest: 6, latest_end: 20, habit_start: 7, days: [0, 1, 2, 3, 4, 5, 6] },
  { name: "Washing machine", kw: 0.5, hours: 2, earliest: 8, latest_end: 20, habit_start: 10, days: [0, 3, 5] },
  { name: "Iron", kw: 1.0, hours: 1, earliest: 7, latest_end: 22, habit_start: 7, days: [0, 1, 2, 3, 4, 5, 6] },
];
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

let tasks = JSON.parse(JSON.stringify(DEFAULT_TASKS));
let source = "tomorrow";
let places = [];          // provinces > divisions > districts
let suppliers = {};       // code -> {name, flat, rate, net_billing, note}
let point = null;         // {lat, lon} when the user shares an exact location
let rangeFor = null;      // place whose weather history range is loaded
let dayChart = null;
let monthChart = null;

const rs = (v) => (v < 0 ? "−Rs " : "Rs ") + Math.abs(Math.round(v)).toLocaleString("en-US");
const hhmm = (h) => `${String(h).padStart(2, "0")}:00`;

async function api(path, body) {
  const res = await fetch(path, body ? {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  } : {});
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k != null) n.append(k instanceof Node ? k : String(k));
  return n;
}

/* ---------- settings ---------- */

function config() {
  const num = (id) => parseFloat($("#" + id).value);
  const cfg = {
    place: $("#district").value,
    supplier: $("#supplier").value || null,
    export: $("#export").value,
    pv_kw: num("pv_kw"), inverter_kw: num("inverter_kw"), tilt: num("tilt"), azimuth: num("azimuth"),
    battery_kwh: num("battery_kwh"), battery_now: num("battery_now"), reserve: num("reserve"),
    acs: parseInt($("#acs").value, 10), tasks,
  };
  if (point) Object.assign(cfg, { lat: point.lat, lon: point.lon });
  if (!$("#rate-row").hidden && $("#rate").value) cfg.rate = num("rate");
  return cfg;
}

/* ---------- location and supplier ---------- */

const option = (value, text) => el("option", { value }, text);
const province = () => places.find((p) => p.province === $("#province").value);
const division = () => province().divisions.find((d) => d.division === $("#division").value);
const district = () => division().districts.find((d) => d.id === $("#district").value);

function fillDivisions(keep) {
  $("#division").replaceChildren(...province().divisions.map((d) => option(d.division, d.division)));
  if (keep) $("#division").value = keep;
}

function fillDistricts(keep) {
  $("#district").replaceChildren(...division().districts.map((d) =>
    option(d.id, d.town !== d.district && !d.district.startsWith(d.town) ? `${d.district} (${d.town})` : d.district)));
  if (keep) $("#district").value = keep;
}

function selectPlace(id) {
  for (const p of places) for (const d of p.divisions) for (const x of d.districts) {
    if (x.id !== id) continue;
    $("#province").value = p.province;
    fillDivisions(d.division);
    fillDistricts(x.id);
    placeChanged();
    return true;
  }
  return false;
}

function placeChanged() {
  point = null;
  rangeFor = null;
  const d = district();
  $("#place-note").textContent = `${d.district}, ${division().division} division. Electricity: ${suppliers[d.supplier].name}.`;
  $("#supplier").options[0].textContent = `Automatic: ${d.supplier}`;
  supplierChanged();
  if (source === "replay" || !$("#tab-year").hidden) loadRange();
}

function supplierChanged() {
  const code = $("#supplier").value || (point ? point.supplier : district().supplier);
  const s = suppliers[code];
  $("#rate-row").hidden = !s.flat;
  if (s.flat) $("#rate").value = s.rate ?? "";
  $("#export-row").hidden = !s.net_billing;
  const peak = code === "K-Electric" ? "6:30–10:30 pm (Apr–Oct), 6–10 pm (Nov–Mar)"
    : "7–11 pm in summer, 5–9 pm in winter, 6–10 pm in between";
  $("#tariff-hint").textContent = s.flat
    ? `${s.name} charges one flat price all day. ${s.note}`
    : `Grid power: Rs 46.85 peak, Rs 34.53 off-peak per unit, plus 18% GST. Peak hours: ${peak}.`;
}

$("#province").addEventListener("change", () => { fillDivisions(); fillDistricts(); placeChanged(); });
$("#division").addEventListener("change", () => { fillDistricts(); placeChanged(); });
$("#district").addEventListener("change", placeChanged);
$("#supplier").addEventListener("change", supplierChanged);
$("#search").addEventListener("change", (e) => {
  const v = e.target.value.trim().toLowerCase();
  for (const p of places) for (const d of p.divisions) for (const x of d.districts) {
    if ([x.district, x.town, `${x.district} (${x.town})`].some((n) => n.toLowerCase() === v)) {
      selectPlace(x.id);
      e.target.value = "";
      return;
    }
  }
});
$("#use-location").addEventListener("click", () => {
  if (!navigator.geolocation) { $("#place-note").textContent = "This browser can't share its location."; return; }
  $("#place-note").textContent = "Finding your location…";
  navigator.geolocation.getCurrentPosition((pos) => {
    const { latitude: lat, longitude: lon } = pos.coords;
    // nearest district sets the menus and the supplier; the exact point is used for the weather
    let best = null;
    for (const p of places) for (const d of p.divisions) for (const x of d.districts) {
      const dist = (x.lat - lat) ** 2 + (x.lon - lon) ** 2;
      if (!best || dist < best.dist) best = { id: x.id, dist, supplier: x.supplier };
    }
    selectPlace(best.id);
    point = { lat: +lat.toFixed(4), lon: +lon.toFixed(4), supplier: best.supplier };
    $("#place-note").textContent = `Using your exact location (${lat.toFixed(3)}, ${lon.toFixed(3)}), in ${district().district} district.`;
  }, () => { $("#place-note").textContent = "Location not shared; pick your district instead."; });
});

function renderTasks() {
  const box = $("#tasks");
  box.replaceChildren(...tasks.map((t, i) => {
    const field = (label, key, attrs = {}) => el("label", {}, label, el("input", {
      type: "number", value: t[key], ...attrs,
      onchange: (e) => { t[key] = parseFloat(e.target.value); },
    }));
    return el("div", { class: "task" },
      el("input", { class: "name", value: t.name, onchange: (e) => { t.name = e.target.value; } }),
      el("button", { class: "remove", title: "Remove", onclick: () => { tasks.splice(i, 1); renderTasks(); } }, "×"),
      el("div", { class: "grid3" },
        field("kW", "kw", { step: 0.05, min: 0.05 }),
        field("Hours", "hours", { min: 1, max: 8 }),
        field("Usually at", "habit_start", { min: 0, max: 23 })),
      el("div", { class: "grid3" },
        field("Not before", "earliest", { min: 0, max: 23 }),
        field("Done by", "latest_end", { min: 1, max: 24 }),
        el("label", {}, "Days", el("input", {
          value: t.days.length === 7 ? "every day" : t.days.map((d) => DAYS[d]).join(" "),
          title: "e.g. Mon Thu Sat, or every day",
          onchange: (e) => {
            const v = e.target.value.toLowerCase();
            t.days = v.includes("every") ? [0, 1, 2, 3, 4, 5, 6]
              : DAYS.map((d, j) => (v.includes(d.toLowerCase()) ? j : -1)).filter((j) => j >= 0);
          },
        }))));
  }));
}

$("#add-task").addEventListener("click", () => {
  tasks.push({ name: "New job", kw: 1, hours: 1, earliest: 8, latest_end: 20, habit_start: 18, days: [0, 1, 2, 3, 4, 5, 6] });
  renderTasks();
});
$("#pv_kw").addEventListener("change", () => { $("#inverter_kw").value = Math.max(5, parseFloat($("#pv_kw").value) || 5); });

/* ---------- tabs and source ---------- */

document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
  $("#tab-plan").hidden = b.dataset.tab !== "plan";
  $("#tab-year").hidden = b.dataset.tab !== "year";
  if (b.dataset.tab === "year") loadRange();
}));

function setSource(s) {
  source = s;
  for (const k of ["tomorrow", "now", "replay"]) $("#src-" + k).classList.toggle("active", s === k);
  $("#date").hidden = s !== "replay";
  if (s === "replay") loadRange();
}
for (const k of ["tomorrow", "now", "replay"]) $("#src-" + k).addEventListener("click", () => setSource(k));

/* ---------- tomorrow ---------- */

// keep the mode strip under the chart's plot area, hour for hour
const alignStrip = {
  id: "alignStrip",
  afterLayout(chart) {
    const strip = $("#modes");
    const legend = $("#mode-legend");
    const left = chart.chartArea.left;
    const right = chart.width - chart.chartArea.right;
    strip.style.marginLeft = `${left}px`;
    strip.style.marginRight = `${right}px`;
    legend.style.marginLeft = `${left}px`;
  },
};

const peakShade = {
  id: "peakShade",
  beforeDatasetsDraw(chart, _args, opts) {
    const { ctx, chartArea, scales } = chart;
    if (!opts.hours) return;
    ctx.save();
    ctx.fillStyle = "rgba(209, 73, 91, 0.08)";
    opts.hours.forEach((isPeak, i) => {
      if (!isPeak) return;
      const x0 = scales.x.getPixelForValue(i) - scales.x.width / 48;
      ctx.fillRect(x0, chartArea.top, scales.x.width / 24, chartArea.bottom - chartArea.top);
    });
    ctx.restore();
  },
};

async function makePlan() {
  const btn = $("#make-plan");
  btn.disabled = true;
  $("#plan-status").textContent = "Planning…";
  $("#plan-status").classList.remove("error");
  try {
    if (source === "replay") {
      await loadRange();
      if (!$("#date").value) throw new Error($("#plan-status").textContent || "Pick a day to replay.");
    }
    const body = { config: config(), mode: source };
    if (source === "replay") body.date = $("#date").value;
    const plan = await api("/api/plan", body);
    showPlan(plan);
    $("#plan-status").textContent = "";
  } catch (e) {
    $("#plan-status").textContent = e.message;
    $("#plan-status").classList.add("error");
  } finally {
    btn.disabled = false;
  }
}
$("#make-plan").addEventListener("click", makePlan);

function showPlan(p) {
  $("#plan-empty").hidden = true;
  $("#plan").hidden = false;
  const day = new Date(p.date + "T00:00").toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric" });
  const span = p.start === "00:00" ? day : `${day}, from ${p.start}`;
  $("#source").textContent = `${p.place} · ${p.supplier.name} · ${span} · ${p.source.label}`;

  $("#c-cost").textContent = rs(p.cost_plan);
  const saved = p.cost_usual - p.cost_plan;
  $("#c-save").innerHTML = saved >= 1
    ? `<span class="good">${rs(saved)} less</span> than your usual routine (${rs(p.cost_usual)})`
    : `same as your usual routine (${rs(p.cost_usual)})`;
  $("#c-solar").textContent = `${p.solar_kwh} kWh`;
  $("#c-self").textContent = `${p.self_use_pct}% used at home`;
  $("#c-peak").textContent = p.peak_hours ? `${p.peak_hours[0]}–${p.peak_hours[1]}` : "None";
  $("#c-peak").nextElementSibling.textContent = p.peak_hours ? "grid power costs the most"
    : `flat rate, Rs ${p.supplier.rate} per unit`;
  const last = p.hours[p.hours.length - 1];
  const endAt = p.start === "00:00" ? "midnight" : p.start;
  $("#c-batt-label").textContent = `Battery at ${endAt}`;
  $("#c-batt").textContent = p.battery_kwh > 0 ? `${Math.round(last.battery_pct)}%` : "No battery";
  $("#c-batt-sub").textContent = p.battery_kwh > 0 ? `of ${p.battery_kwh} kWh` : "";

  const labels = p.hours.map((h) => h.time);
  const data = {
    labels,
    datasets: [
      { type: "line", label: "Solar", data: p.hours.map((h) => h.solar), borderColor: "#f2a900",
        backgroundColor: "rgba(242,169,0,0.18)", fill: true, tension: 0.35, pointRadius: 0, order: 3 },
      { type: "bar", label: "House", data: p.hours.map((h) => h.load), backgroundColor: "#5b6572", stack: "use", order: 2 },
      { type: "bar", label: "Flexible jobs", data: p.hours.map((h) => h.jobs), backgroundColor: "#7b5cd6", stack: "use", order: 2 },
      { type: "line", label: "Battery %", data: p.hours.map((h) => h.battery_pct), borderColor: "#1f9d6b",
        borderWidth: 2.5, pointRadius: 0, yAxisID: "pct", order: 1, hidden: p.battery_kwh <= 0 },
    ],
  };
  const options = {
    responsive: true, maintainAspectRatio: false, animation: { duration: 300 },
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false },
      peakShade: { hours: p.hours.map((h) => h.peak) },
      tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${c.parsed.y.toFixed(c.dataset.yAxisID === "pct" ? 0 : 2)}${c.dataset.yAxisID === "pct" ? "%" : " kW"}` } },
    },
    scales: {
      x: { stacked: true, grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 12 } },
      y: { stacked: true, beginAtZero: true, title: { display: true, text: "kW" }, grid: { color: "#f0eee8" } },
      pct: { position: "right", min: 0, max: 100, grid: { display: false }, title: { display: true, text: "battery %" } },
    },
  };
  if (dayChart) dayChart.destroy();
  dayChart = new Chart($("#day-chart"), { data, options, plugins: [peakShade, alignStrip] });
  const legend = [["#f2a900", "Solar"], ["#5b6572", "House"], ["#7b5cd6", "Flexible jobs"], ["#1f9d6b", "Battery %"]];
  if (p.peak_hours) legend.push(["rgba(209,73,91,0.25)", "Peak hours"]);
  $("#chart-legend").replaceChildren(...legend.map(([c, t]) => el("span", {}, el("i", { style: `background:${c}` }), t)));

  $("#modes").replaceChildren(...p.hours.map((h) => el("div", { class: "m-" + h.mode, title: `${h.time} ${MODE[h.mode]}` })));
  const used = [...new Set(p.hours.map((h) => h.mode))];
  $("#mode-legend").replaceChildren(...used.map((m) => el("span", {}, el("i", { class: "m-" + m }), MODE[m])));

  $("#instructions").replaceChildren(...p.instructions.map((line) => {
    const [when, ...rest] = line.split("  ");
    const text = rest.join("  ");
    const isJob = p.jobs.some((j) => text.startsWith(j.name));
    return el("li", {}, el("span", { class: "when" }, when.replace("-", "–")), el("span", { class: isJob ? "job" : "" }, text));
  }));

  $("#jobs").replaceChildren(...(p.jobs.length ? p.jobs.map((j) => {
    const moved = j.start !== j.usual_start;
    return el("div", { class: "job-card" },
      el("div", { class: "t" }, `${j.name}  ${j.start}–${j.end}`),
      el("div", { class: "d" }, moved
        ? `Moved from ${j.usual_start}` + (j.saving > 0 ? ` · saves about ${rs(j.saving)}` : "")
        : "At the usual time: moving it would not save anything today"));
  }) : [el("p", { class: "muted" }, "No flexible jobs on this day.")]));
}

/* ---------- yearly savings ---------- */

async function runYear() {
  const btn = $("#run-year");
  btn.disabled = true;
  $("#year-status").textContent = "";
  $("#year-status").classList.remove("error");
  $("#year-progress").hidden = false;
  const bar = $("#year-progress div");
  bar.style.width = "0";
  try {
    const { id } = await api("/api/savings", { config: config(), year: parseInt($("#year").value, 10) });
    for (;;) {
      await new Promise((r) => setTimeout(r, 1000));
      const job = await api(`/api/savings/${id}`);
      bar.style.width = `${Math.round((job.progress || 0) * 100)}%`;
      $("#year-status").textContent = job.status === "running" ? `Simulating: ${STRATEGY[job.stage] || job.stage}…` : "";
      if (job.status === "done") { showYear(job.result, job.year); break; }
      if (job.status === "failed") throw new Error(job.error);
    }
  } catch (e) {
    $("#year-status").textContent = e.message;
    $("#year-status").classList.add("error");
  } finally {
    btn.disabled = false;
    $("#year-progress").hidden = true;
  }
}
$("#run-year").addEventListener("click", runYear);

function showYear(r, year) {
  $("#year-empty").hidden = true;
  $("#year-results").hidden = false;
  const base = r.self_use ? "self_use" : "solar_only";
  const cost = (s) => r[s].total_with_wear;
  const save = cost(base) - cost("planner");
  const best = cost(base) - cost("planner_perfect");
  $("#y-save").textContent = rs(save);
  $("#y-save-sub").textContent = `vs ${STRATEGY[base].toLowerCase()} (${rs(save / 12)} a month)`;
  $("#y-bill").textContent = rs(r.planner.total);
  $("#y-bill-sub").textContent = `in ${year}, fixed charges included`;
  $("#y-solar").textContent = rs(r.grid_only.total - r.solar_only.total);
  $("#y-captured").textContent = best > 0 ? `${Math.round((100 * save) / best)}%` : "–";

  const rows = Object.keys(STRATEGY).filter((s) => r[s]);
  $("#year-table").replaceChildren(
    el("tr", {}, ["Strategy", "Bought (kWh)", "Sold (kWh)", "Bill", "Battery wear", "Total", "Saved"].map((h) => el("th", {}, h))),
    ...rows.map((s) => el("tr", { class: s === "planner" ? "hl" : "" },
      el("td", {}, STRATEGY[s]),
      el("td", {}, Math.round(r[s].import_kwh).toLocaleString()),
      el("td", {}, Math.round(r[s].export_kwh).toLocaleString()),
      el("td", {}, rs(r[s].total)),
      el("td", {}, r[s].wear_cost ? rs(r[s].wear_cost) : "–"),
      el("td", {}, rs(cost(s))),
      el("td", {}, s.startsWith("planner") ? rs(cost(base) - cost(s)) : ""))));

  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const series = (s) => months.map((_, i) => r[s].monthly_energy_cost[i + 1] ?? 0);
  if (monthChart) monthChart.destroy();
  monthChart = new Chart($("#month-chart"), {
    type: "bar",
    data: { labels: months, datasets: [
      { label: STRATEGY[base], data: series(base), backgroundColor: "#c9c5bb" },
      { label: "Planner", data: series("planner"), backgroundColor: "#f2a900" },
    ] },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: "top", align: "end", labels: { boxWidth: 12 } },
        tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${rs(c.parsed.y)}` } } },
      scales: { x: { grid: { display: false } }, y: { title: { display: true, text: "Rs (negative = credit)" }, grid: { color: "#f0eee8" } } },
    },
  });
}

/* ---------- start ---------- */

async function init() {
  renderTasks();
  [places, suppliers] = await Promise.all([api("/api/places"), api("/api/suppliers")]);
  suppliers = Object.fromEntries(suppliers.map((x) => [x.code, x]));
  $("#supplier").replaceChildren(option("", "Automatic"),
    ...Object.values(suppliers).map((x) => option(x.code, `${x.code}: ${x.name}`)));
  $("#province").replaceChildren(...places.map((p) => option(p.province, p.province)));
  const names = [];
  for (const p of places) for (const d of p.divisions) for (const x of d.districts) {
    names.push(x.district);
    if (x.town !== x.district && !names.includes(x.town)) names.push(x.town);
  }
  $("#place-list").replaceChildren(...names.sort().map((n) => el("option", { value: n })));
  selectPlace("lahore");
  $("#year").replaceChildren(...[2025, 2024, 2023, 2022, 2021, 2020].map((y) => option(y, y)));
  $("#year").value = "2024";
}

let rangeLoading = null;

function loadRange() {
  const key = point ? `${point.lat},${point.lon}` : $("#district").value;
  if (rangeFor === key) return Promise.resolve();
  if (rangeLoading && rangeLoading.key === key) return rangeLoading;
  rangeLoading = fetchRange(key);
  rangeLoading.key = key;
  return rangeLoading;
}

async function fetchRange(key) {
  const status = $("#tab-year").hidden ? $("#plan-status") : $("#year-status");
  status.classList.remove("error");
  status.textContent = `Loading weather history for ${district().district} (the first time takes a few seconds)…`;
  try {
    const q = point ? `lat=${point.lat}&lon=${point.lon}` : `place=${encodeURIComponent($("#district").value)}`;
    const r = await api(`/api/history-range?${q}`);
    const now = point ? `${point.lat},${point.lon}` : $("#district").value;
    if (now !== key) return;          // the place changed while this was loading
    rangeFor = key;
    const d = $("#date");
    d.min = r.first; d.max = r.last;
    if (!d.value || d.value < r.first || d.value > r.last) d.value = r.last >= "2025-06-02" ? "2025-06-02" : r.last;
    const year = $("#year").value;
    $("#year").replaceChildren(...r.years.filter((y) => y >= r.years[0] + 2).reverse().map((y) => option(y, y)));
    if (r.years.includes(+year)) $("#year").value = year;
    status.textContent = "";
  } catch (e) {
    rangeLoading = null;              // allow another try
    status.textContent = e.message;
    status.classList.add("error");
  }
}

setSource("tomorrow");
init();
