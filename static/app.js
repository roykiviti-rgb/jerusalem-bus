"use strict";
// ---------------------------------------------------------------- הגדרות
const TZ = "Asia/Jerusalem";
// כתובת הבית נקראת מ-config.local.json (לא נשמר ב-git). בלי קובץ כזה: מרכז העיר, ואפשר לגרור את 🏠
const CFG_HOME = (window.APP_CONFIG && window.APP_CONFIG.home) || null;
const HOME_DEFAULT = CFG_HOME ? { lat: CFG_HOME.lat, lon: CFG_HOME.lon } : { lat: 31.7818, lon: 35.2197 };
const HOME_NAME = CFG_HOME && CFG_HOME.name ? `בית – ${CFG_HOME.name}` : "הבית";
const PRESETS = [
  { icon: "🎓", name: "האוניברסיטה העברית – הר הצופים", lat: 31.7929, lon: 35.2440 },
  { icon: "🎓", name: "קמפוס גבעת רם", lat: 31.7745, lon: 35.1990 },
  { icon: "🚉", name: "התחנה המרכזית", lat: 31.7890, lon: 35.2033 },
  { icon: "🏙️", name: "מרכז העיר – כיכר ציון", lat: 31.7818, lon: 35.2197 },
  { icon: "🛍️", name: "קניון מלחה", lat: 31.7515, lon: 35.1875 },
  { icon: "🏥", name: "הדסה עין כרם", lat: 31.7650, lon: 35.1480 },
];
const AGENCY_COLORS = {
  "אגד": "#0f7a3d", "סופרבוס": "#1565c0", "אקסטרה": "#e08600", "קווים": "#6a1b9a",
  "אלקטרה אפיקים": "#00838f", "תנופה": "#ad1457", "דן בדרום": "#283593", "אפיקים": "#00838f",
};

// ---------------------------------------------------------------- עזרים
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* ignore */ } },
};
const fmtT = new Intl.DateTimeFormat("he-IL", { hour: "2-digit", minute: "2-digit", timeZone: TZ });
const fmtD = new Intl.DateTimeFormat("he-IL", { weekday: "short", day: "numeric", month: "numeric", timeZone: TZ });
const hhmm = (ms) => fmtT.format(ms);
// מצב תצוגה: ?time=2026-11-22T08:00 מציג את לוח הזמנים כאילו עכשיו השעה הזו (שימושי בשבת)
const DEMO_TIME = new URLSearchParams(location.search).get("time");
const DEMO_MS = DEMO_TIME ? new Date(DEMO_TIME).getTime() : NaN;
function nowMs() { return Number.isFinite(DEMO_MS) ? DEMO_MS : Date.now(); }
function sameDay(a, b) { return fmtD.format(a) === fmtD.format(b); }
const fmtW = new Intl.DateTimeFormat("he-IL", { weekday: "long", timeZone: TZ });
function timeLabel(ms) {
  if (sameDay(ms, nowMs())) return hhmm(ms);
  const days = (ms - nowMs()) / 864e5;
  return days < 6 && days > -1 ? `${fmtW.format(ms)} ${hhmm(ms)}` : `${fmtD.format(ms)} ${hhmm(ms)}`;
}
function minsUntil(ms) { return Math.round((ms - nowMs()) / 60000); }
function untilLabel(ms) {
  const m = minsUntil(ms);
  if (m <= 0) return "עכשיו";
  if (m < 60) return `בעוד ${m} דק'`;
  return timeLabel(ms);
}
async function api(path, params = {}) {
  const u = new URL(path, location.origin);
  if (Number.isFinite(DEMO_MS) && params.time === undefined && !/live|geocode|meta/.test(path)) params = { ...params, time: DEMO_TIME };
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") u.searchParams.set(k, v);
  const r = await fetch(u);
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
function routeColor(r) {
  if (r.type === 0) return "#d7262e"; // רכבת קלה
  if (r.type === 2) return "#1d3f8f"; // רכבת
  return r.color || AGENCY_COLORS[r.agency] || "#455a64";
}
function badge(r) {
  const icon = r.type === 0 ? "🚊" : r.type === 2 ? "🚆" : "";
  return `<span class="badge" style="background:${routeColor(r)}">${icon}${esc(r.short)}</span>`;
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function stopLabel(s) { return `${esc(s.name)}${s.code ? ` <small>(${esc(s.code)})</small>` : ""}`; }

// ---------------------------------------------------------------- מצב
let home = store.get("home", HOME_DEFAULT);
let favorites = store.get("favorites", []);
const state = {
  from: { name: HOME_NAME, lat: home.lat, lon: home.lon, kind: "home" },
  to: null,
  pick: null,
  journeys: [],
  openIdx: -1,
  liveTimer: null,
  nearWhere: "home",
  nearTimer: null,
};

// ---------------------------------------------------------------- מפה
const map = L.map("map", { zoomControl: false }).setView([home.lat, home.lon], 14);
L.control.zoom({ position: "topleft" }).addTo(map);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
}).addTo(map);
new ResizeObserver(() => {
  map.invalidateSize();
  if (routeLayer && routeLayer.getLayers().length) map.fitBounds(routeLayer.getBounds().pad(0.15), { maxZoom: 16 });
}).observe(document.getElementById("map"));
const routeLayer = L.featureGroup().addTo(map);
const liveLayer = L.featureGroup().addTo(map);
const stopsLayer = L.featureGroup().addTo(map);
const icon = (html, cls, size = 28) => L.divIcon({ html, className: cls, iconSize: [size, size], iconAnchor: [size / 2, size - 2] });
const homeMarker = L.marker([home.lat, home.lon], { draggable: true, icon: icon("🏠", "home-icon"), zIndexOffset: 1000 })
  .addTo(map).bindTooltip("הבית (אפשר לגרור כדי לדייק)", { direction: "top", offset: [0, -24] });
homeMarker.on("dragend", () => {
  const p = homeMarker.getLatLng();
  home = { lat: p.lat, lon: p.lng };
  store.set("home", home);
  if (state.from.kind === "home") Object.assign(state.from, home);
  if (state.to && state.to.kind === "home") Object.assign(state.to, home);
});
map.on("click", (e) => {
  if (!state.pick) return;
  const target = state.pick;
  setPick(null);
  setPlace(target, { name: "נקודה שנבחרה במפה", lat: e.latlng.lat, lon: e.latlng.lng, kind: "map" });
});
function setPick(target) {
  state.pick = target;
  $("#map-hint").hidden = !target;
  map.getContainer().style.cursor = target ? "crosshair" : "";
}

// ---------------------------------------------------------------- טאבים
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(name) {
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tabpane").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  clearMapExtras();
  stopLive();
  clearInterval(state.nearTimer);
  if (name === "near") loadNear();
  if (name === "plan" && state.openIdx >= 0) drawJourney(state.journeys[state.openIdx]);
}
function clearMapExtras() { routeLayer.clearLayers(); liveLayer.clearLayers(); stopsLayer.clearLayers(); }

// ---------------------------------------------------------------- שדות מוצא/יעד
function placeOptions() {
  return [
    { icon: "🏠", name: HOME_NAME, lat: home.lat, lon: home.lon, kind: "home" },
    ...favorites.map((f) => ({ ...f, icon: "⭐", kind: "fav" })),
    ...PRESETS.map((p) => ({ ...p, kind: "preset" })),
  ];
}
function setupField(which) {
  const input = $(`#${which}`);
  const box = $(`#${which}-sugg`);
  let items = [];
  let sel = -1;
  let seq = 0;
  const render = () => {
    let html = "";
    let lastHdr = null;
    items.forEach((it, i) => {
      if (it.hdr && it.hdr !== lastHdr) { html += `<div class="hdr">${esc(it.hdr)}</div>`; lastHdr = it.hdr; }
      html += `<div class="item${i === sel ? " sel" : ""}" data-i="${i}"><span>${it.icon || "📍"}</span><div>${esc(it.name)}${it.sub ? `<small>${esc(it.sub)}</small>` : ""}</div></div>`;
    });
    box.innerHTML = html;
    box.classList.toggle("open", items.length > 0);
  };
  const base = (q) => {
    const list = [
      { icon: "📍", name: "המיקום הנוכחי שלי", action: "gps", hdr: "" },
      { icon: "🗺️", name: "בחירה על המפה", action: "map", hdr: "" },
    ];
    for (const p of placeOptions()) if (!q || p.name.includes(q)) list.push({ ...p, hdr: "מקומות" });
    return list;
  };
  const update = async () => {
    const q = input.value.trim();
    const my = ++seq;
    items = base(q);
    sel = -1;
    render();
    if (q.length < 2) return;
    try {
      const stops = await api("/api/stops", { q });
      if (my !== seq) return;
      items = items.concat(stops.slice(0, 8).map((s) => ({
        icon: "🚏", name: s.name, sub: [s.street, s.code && `תחנה ${s.code}`].filter(Boolean).join(" · "),
        lat: s.lat, lon: s.lon, kind: "stop", hdr: "תחנות",
      })));
      render();
    } catch { /* ignore */ }
    geocodeLater(q, my);
  };
  const geocodeLater = debounce(async (q, my) => {
    if (q.length < 3 || my !== seq) return;
    try {
      const res = await api("/api/geocode", { q });
      if (my !== seq) return;
      items = items.concat(res.map((r) => ({ icon: "🏠", name: r.name.split(",")[0], sub: r.name, lat: r.lat, lon: r.lon, kind: "addr", hdr: "כתובות" })));
      render();
    } catch { /* ignore */ }
  }, 500);
  const choose = (it) => {
    box.classList.remove("open");
    input.blur();
    if (it.action === "gps") return useGps(which);
    if (it.action === "map") { input.value = ""; return setPick(which); }
    setPlace(which, { name: it.name, lat: it.lat, lon: it.lon, kind: it.kind });
  };
  input.addEventListener("focus", () => { input.select(); update(); });
  input.addEventListener("input", debounce(update, 200));
  input.addEventListener("blur", () => setTimeout(() => box.classList.remove("open"), 180));
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { sel = Math.min(items.length - 1, sel + 1); render(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { sel = Math.max(0, sel - 1); render(); e.preventDefault(); }
    else if (e.key === "Enter") {
      const it = items[sel >= 0 ? sel : items.findIndex((x) => x.lat)];
      if (it) choose(it);
      e.preventDefault();
    } else if (e.key === "Escape") box.classList.remove("open");
  });
  box.addEventListener("mousedown", (e) => {
    const el = e.target.closest(".item");
    if (el) { e.preventDefault(); choose(items[+el.dataset.i]); }
  });
}
function setPlace(which, place) {
  state[which] = place;
  $(`#${which}`).value = place ? place.name : "";
  renderQuick();
  if (state.from && state.to) search();
}
function useGps(which) {
  if (!navigator.geolocation) return alert("הדפדפן לא תומך באיתור מיקום");
  $(`#${which}`).value = "מאתר מיקום...";
  navigator.geolocation.getCurrentPosition(
    (p) => setPlace(which, { name: "המיקום שלי", lat: p.coords.latitude, lon: p.coords.longitude, kind: "gps" }),
    () => { $(`#${which}`).value = ""; alert("לא הצלחתי לאתר את המיקום. אפשר לבחור על המפה."); },
    { enableHighAccuracy: true, timeout: 10000 });
}
setupField("from");
setupField("to");
$("#from").value = state.from.name;
$("#swap").addEventListener("click", () => {
  [state.from, state.to] = [state.to, state.from];
  $("#from").value = state.from ? state.from.name : "";
  $("#to").value = state.to ? state.to.name : "";
  if (state.from && state.to) search();
});
$("#when-mode").addEventListener("change", (e) => {
  const at = e.target.value === "at";
  $("#when-time").hidden = !at;
  if (at && !$("#when-time").value) {
    const d = new Date(nowMs() + 15 * 60000);
    const p = (n) => String(n).padStart(2, "0");
    $("#when-time").value = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  }
});
$("#go").addEventListener("click", () => search());

function renderQuick() {
  const el = $("#quick");
  const chips = [];
  if (state.to && !["home", "preset", "fav"].includes(state.to.kind)) chips.push(`<button class="chip" data-act="save">⭐ שמירת היעד</button>`);
  const dests = [{ icon: "🏠", name: "הביתה", lat: home.lat, lon: home.lon, kind: "home", full: HOME_NAME },
    ...favorites.map((f, i) => ({ ...f, icon: "⭐", kind: "fav", fi: i })), ...PRESETS.map((p) => ({ ...p, kind: "preset" }))];
  dests.forEach((d, i) => {
    chips.push(`<button class="chip" data-i="${i}">${d.icon} ${esc(d.name.split(" – ").pop())}${d.kind === "fav" ? `<span class="x" data-del="${d.fi}">✕</span>` : ""}</button>`);
  });
  el.innerHTML = chips.join("");
  el.onclick = (e) => {
    const del = e.target.closest("[data-del]");
    if (del) { favorites.splice(+del.dataset.del, 1); store.set("favorites", favorites); return renderQuick(); }
    const b = e.target.closest(".chip");
    if (!b) return;
    if (b.dataset.act === "save") {
      const name = prompt("שם למקום:", state.to.name);
      if (name) { favorites.push({ name, lat: state.to.lat, lon: state.to.lon }); store.set("favorites", favorites); state.to.kind = "fav"; state.to.name = name; $("#to").value = name; renderQuick(); }
      return;
    }
    const d = dests[+b.dataset.i];
    if (d.kind === "home" && state.from && state.from.kind === "home") {
      // כבר בבית: נוסעים הביתה מהמיקום הנוכחי
      return useGpsThen(() => setPlace("to", { name: HOME_NAME, lat: home.lat, lon: home.lon, kind: "home" }));
    }
    setPlace("to", { name: d.full || d.name, lat: d.lat, lon: d.lon, kind: d.kind });
  };
}
function useGpsThen(cb) {
  if (!navigator.geolocation) return;
  navigator.geolocation.getCurrentPosition((p) => {
    state.from = { name: "המיקום שלי", lat: p.coords.latitude, lon: p.coords.longitude, kind: "gps" };
    $("#from").value = state.from.name;
    cb();
  }, () => alert("כדי לנסוע הביתה צריך מוצא: הפעילו מיקום או בחרו מוצא אחר."));
}
renderQuick();

// ---------------------------------------------------------------- תכנון
function timeParam() {
  return $("#when-mode").value === "at" && $("#when-time").value ? $("#when-time").value : undefined;
}
async function search() {
  if (!state.from || !state.to) return;
  const res = $("#results");
  const notice = $("#notice");
  notice.hidden = true;
  stopLive();
  routeLayer.clearLayers();
  res.innerHTML = `<div class="loading">מחפש מסלולים...</div>`;
  try {
    const r = await api("/api/plan", { flat: state.from.lat, flon: state.from.lon, tlat: state.to.lat, tlon: state.to.lon, time: timeParam() });
    if (r.notice) { notice.textContent = r.notice; notice.hidden = false; }
    state.journeys = r.journeys;
    state.openIdx = -1;
    if (!r.journeys.length) {
      res.innerHTML = `<div class="empty">לא נמצאו מסלולים. נסו שעה אחרת או נקודה קרובה יותר לתחנה.</div>`;
      return;
    }
    renderJourneys();
    openJourney(0);
  } catch (e) {
    res.innerHTML = `<div class="empty">שגיאה: ${esc(e.message)}</div>`;
  }
}
function renderJourneys() {
  $("#results").innerHTML = state.journeys.map((j, i) => {
    const rides = j.legs.filter((l) => l.type === "ride");
    const chain = j.legs.map((l) => l.type === "ride" ? badge(l.route)
      : (l.secs >= 60 ? `<span class="walkchip">🚶${Math.round(l.secs / 60)}</span>` : "")).filter(Boolean)
      .join('<span class="sep">‹</span>');
    const first = rides[0];
    const sub = first
      ? `<span class="leave">יציאה ${untilLabel(j.dep)}</span> · ${esc(first.route.short)} מ${esc(first.from.name)} ב-${hhmm(first.dep)}`
      : `הליכה של ${(j.walk_m / 1000).toFixed(1)} ק"מ`;
    const tr = j.transfers > 0 ? ` · ${j.transfers} החלפ${j.transfers > 1 ? "ות" : "ה"}` : j.transfers === 0 ? " · ישיר" : "";
    return `<div class="card${i === state.openIdx ? " open" : ""}" data-i="${i}">
      <div class="card-top"><span class="times">${hhmm(j.dep)} – ${hhmm(j.arr)}</span><span class="dur">${j.duration_min} <small>דק'</small></span></div>
      <div class="chain">${chain}</div>
      <div class="sub">${sub}${tr} · 🚶 ${j.walk_m} מ'</div>
      <div class="livebox" id="live-${i}"></div>
      ${i === state.openIdx ? stepsHtml(j) : ""}
    </div>`;
  }).join("");
  $("#results").onclick = (e) => {
    if (e.target.closest("details")) return;
    const c = e.target.closest(".card");
    if (c) openJourney(+c.dataset.i === state.openIdx ? -1 : +c.dataset.i);
  };
}
function stepsHtml(j) {
  const rows = [];
  const fromName = state.from ? state.from.name : "מוצא";
  const toName = state.to ? state.to.name : "יעד";
  j.legs.forEach((l, i) => {
    if (l.type === "walk") {
      const startName = i === 0 ? fromName : l.from ? l.from.name : "";
      const endName = l.to ? l.to.name : toName;
      if (i === 0) rows.push(step(hhmm(l.dep), "", `<b>${esc(fromName)}</b>`, true));
      rows.push(step("", "walk", `🚶 הליכה ${Math.max(1, Math.round(l.secs / 60))} דק' (${l.meters} מ') אל <b>${esc(endName)}</b>`));
      if (i === j.legs.length - 1) rows.push(step(hhmm(l.arr), "", `<b>${esc(toName)}</b> 🏁`, true));
      void startName;
    } else {
      const c = routeColor(l.route);
      const inner = l.stops.slice(1, -1);
      rows.push(step(hhmm(l.dep), "", `${badge(l.route)} <b>${stopLabel(l.from)}</b><div class="sub">כיוון: ${esc(l.route.headsign)} · ${esc(l.route.agency)}</div><div class="live-leg" data-trip="${l.route.trip}"></div>`, true, c));
      rows.push(step("", "", `<details><summary>${l.stops.length - 1} תחנות · ${Math.round((l.arr - l.dep) / 60000)} דק'</summary><ol>${inner.map((s) => `<li>${hhmm(s.t)} ${esc(s.name)}</li>`).join("")}</ol></details>`, false, c));
      rows.push(step(hhmm(l.arr), "", `ירידה ב<b>${stopLabel(l.to)}</b>`, true, c));
    }
  });
  return `<div class="steps">${rows.join("")}</div>`;
}
function step(t, railCls, body, node, color) {
  return `<div class="step"><div class="t">${t}</div><div class="rail ${railCls}" ${color ? `style="--c:${color}"` : ""}>${node ? '<span class="node"></span>' : ""}</div><div class="body">${body}</div></div>`;
}
function openJourney(i) {
  state.openIdx = i;
  renderJourneys();
  stopLive();
  routeLayer.clearLayers();
  if (i < 0) return;
  drawJourney(state.journeys[i]);
  startLive(state.journeys[i], i);
}
function drawJourney(j) {
  routeLayer.clearLayers();
  const pt = (p) => [p.lat, p.lon];
  for (const l of j.legs) {
    if (l.type === "walk") {
      const a = l.from_point || l.from;
      const b = l.to_point || l.to;
      if (a && b) L.polyline([pt(a), pt(b)], { color: "#5f6b7a", weight: 4, dashArray: "2 8", lineCap: "round" }).addTo(routeLayer);
    } else {
      const c = routeColor(l.route);
      L.polyline(l.stops.map(pt), { color: "#fff", weight: 9, opacity: 0.9 }).addTo(routeLayer);
      L.polyline(l.stops.map(pt), { color: c, weight: 6 }).addTo(routeLayer);
      for (const s of l.stops.slice(1, -1)) L.circleMarker(pt(s), { radius: 3, color: c, fillColor: "#fff", fillOpacity: 1, weight: 2 }).bindTooltip(`${hhmm(s.t)} ${esc(s.name)}`).addTo(routeLayer);
      [l.from, l.to].forEach((s, k) => L.circleMarker(pt(s), { radius: 7, color: c, fillColor: "#fff", fillOpacity: 1, weight: 3 })
        .bindPopup(`<b>${esc(s.name)}</b><br>${k ? "ירידה" : `עלייה לקו ${esc(l.route.short)}`} ב-${hhmm(k ? l.arr : l.dep)}${s.code ? `<br>תחנה ${esc(s.code)}` : ""}`).addTo(routeLayer));
    }
  }
  if (state.to) L.marker([state.to.lat, state.to.lon], { icon: icon("📍", "pin-icon") }).addTo(routeLayer);
  map.fitBounds(routeLayer.getBounds().pad(0.15), { maxZoom: 16 });
}

// ---------------------------------------------------------------- זמן אמת
function stopLive() { clearInterval(state.liveTimer); state.liveTimer = null; liveLayer.clearLayers(); }
function startLive(j, i) {
  const rides = j.legs.filter((l) => l.type === "ride");
  if (!rides.length) return;
  const ids = [...new Set(rides.map((l) => l.route.gtfs_id))];
  const tick = async () => {
    try {
      const r = await api("/api/live", { routes: ids.join(",") });
      if (state.openIdx !== i) return;
      drawVehicles(r.vehicles);
      for (const l of rides) {
        const el = document.querySelector(`.card.open .live-leg[data-trip="${l.route.trip}"]`);
        const v = r.vehicles.find((x) => x.trip === l.route.trip);
        if (!el) continue;
        if (v) el.innerHTML = liveText(v, "האוטובוס שלך");
        else if (!Number.isFinite(DEMO_MS) && l.dep - nowMs() < 90 * 60000 && l.dep > nowMs() - 60 * 60000) el.innerHTML = `<span class="sub">אין עדיין מיקום חי לנסיעה הזו</span>`;
      }
    } catch { /* ignore */ }
  };
  tick();
  state.liveTimer = setInterval(tick, 30000);
}
function liveText(v, prefix) {
  const ago = Math.max(0, Math.round((Date.now() - Date.parse(v.recorded)) / 60000));
  let d = "";
  if (v.delay_min !== null && v.delay_min !== undefined) {
    d = v.delay_min >= 2 ? ` · מאחר ${v.delay_min} דק'` : v.delay_min <= -2 ? ` · מקדים ${-v.delay_min} דק'` : " · בזמן";
  }
  return `<span class="live${v.delay_min >= 3 ? " late" : ""}">${prefix} בדרך${d}</span> <span class="sub">(עודכן לפני ${ago} דק')</span>`;
}
function drawVehicles(vs) {
  liveLayer.clearLayers();
  for (const v of vs) {
    const rid = v.route_gtfs_id;
    const j = state.journeys[state.openIdx];
    const leg = j && j.legs.find((l) => l.type === "ride" && l.route.gtfs_id === rid);
    const r = leg ? leg.route : { type: 3, short: v.short, agency: "" };
    const c = routeColor(r);
    L.marker([v.lat, v.lon], { icon: L.divIcon({ html: `<div class="bus-icon" style="background:${c};width:28px;height:28px">${esc(v.short)}</div>`, className: "", iconSize: [28, 28] }), zIndexOffset: 500 })
      .bindPopup(`<b>קו ${esc(v.short)}</b> ${esc(v.headsign)}<br>${liveText(v, "")}`).addTo(liveLayer);
  }
}

// ---------------------------------------------------------------- תחנות קרובות
document.querySelectorAll("#near-where button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("#near-where button").forEach((x) => x.classList.toggle("on", x === b));
  state.nearWhere = b.dataset.w;
  loadNear();
}));
function nearPoint() {
  return new Promise((resolve) => {
    if (state.nearWhere === "home") return resolve(home);
    if (state.nearWhere === "map") { const c = map.getCenter(); return resolve({ lat: c.lat, lon: c.lng }); }
    navigator.geolocation.getCurrentPosition((p) => resolve({ lat: p.coords.latitude, lon: p.coords.longitude }),
      () => { alert("לא הצלחתי לאתר מיקום, מציג ליד הבית"); resolve(home); }, { timeout: 10000 });
  });
}
async function loadNear(stopId) {
  clearInterval(state.nearTimer);
  const list = $("#near-list");
  const notice = $("#near-notice");
  list.innerHTML = `<div class="loading">טוען יציאות...</div>`;
  const p = stopId === undefined ? await nearPoint() : null;
  try {
    const r = await api("/api/departures", stopId !== undefined ? { stop: stopId } : { lat: p.lat, lon: p.lon, radius: 450 });
    notice.hidden = !r.notice;
    notice.textContent = r.notice || "";
    renderNear(r, p, stopId);
  } catch (e) {
    list.innerHTML = `<div class="empty">שגיאה: ${esc(e.message)}</div>`;
  }
  state.nearTimer = setInterval(() => { if ($("#tab-near").classList.contains("active")) loadNear(stopId); }, 60000);
}
function renderNear(r, p, stopId) {
  const list = $("#near-list");
  stopsLayer.clearLayers();
  routeLayer.clearLayers();
  liveLayer.clearLayers();
  if (!r.stops.length) { list.innerHTML = `<div class="empty">אין יציאות בשעה וחצי הקרובות.</div>`; return; }
  list.innerHTML = (stopId !== undefined ? `<button class="back" id="near-back">→ חזרה לכל התחנות</button>` : "") + r.stops.map((s) => `
    <div class="stop">
      <div class="stop-h" data-stop="${s.id}"><div><b>${esc(s.name)}</b><br><small>${esc(s.street)}${s.code ? ` · תחנה ${esc(s.code)}` : ""}</small></div><small>${s.dist ? `${s.dist} מ'` : ""}</small></div>
      ${s.lines.map((g) => {
        const t0 = g.times[0];
        const rest = g.times.slice(1).map((t) => hhmm(t.t)).join(", ");
        return `<div class="dep" data-trip="${t0.trip}" data-date="${t0.date}" data-stop="${s.id}">${badge(g.route)}
          <div class="hs">${esc(g.route.headsign)}<small>${esc(g.route.agency)}</small></div>
          <div class="mins"><b>${untilLabel(t0.t)}</b><small>${rest ? `אח"כ ${rest}` : hhmm(t0.t)}</small></div></div>`;
      }).join("")}
    </div>`).join("");
  const pts = [];
  for (const s of r.stops) {
    pts.push([s.lat, s.lon]);
    L.circleMarker([s.lat, s.lon], { radius: 7, color: "#0f7a3d", fillColor: "#fff", fillOpacity: 1, weight: 3 })
      .bindTooltip(esc(s.name)).on("click", () => loadNear(s.id)).addTo(stopsLayer);
  }
  if (p) pts.push([p.lat, p.lon]);
  map.fitBounds(L.latLngBounds(pts).pad(0.2), { maxZoom: 17 });
  list.onclick = (e) => {
    if (e.target.id === "near-back") return loadNear();
    const d = e.target.closest(".dep");
    if (d) return showTrip(d.dataset.trip, d.dataset.date, +d.dataset.stop, $("#near-list"), () => loadNear(stopId));
    const h = e.target.closest(".stop-h");
    if (h && stopId === undefined) loadNear(+h.dataset.stop);
  };
}

// ---------------------------------------------------------------- תצוגת נסיעה / קו
async function showTrip(trip, date, focusStop, container, back) {
  clearInterval(state.nearTimer);
  container.innerHTML = `<div class="loading">טוען...</div>`;
  const r = await api("/api/trip", { trip, date });
  renderStopList(r, focusStop, container, back);
}
function renderStopList(r, focusStop, container, back) {
  const c = routeColor(r.route);
  container.innerHTML = `<button class="back">→ חזרה</button>
    <div class="variant" style="cursor:default">${badge(r.route)}<div><b>${esc(r.route.headsign || r.route.long)}</b><div class="sub">${esc(r.route.agency)}${r.stops.length ? ` · יציאה ${timeLabel(r.stops[0].t)}` : ""}</div></div></div>
    <div class="livebox" id="trip-live"></div>
    <ol class="stoplist">${r.stops.map((s) => `<li data-lat="${s.lat}" data-lon="${s.lon}" ${s.id === focusStop ? 'style="font-weight:700"' : ""}><span class="tt">${hhmm(s.t)}</span><span>${esc(s.name)}</span></li>`).join("")}</ol>`;
  container.querySelector(".back").onclick = () => { stopLive(); back(); };
  container.querySelector(".stoplist").onclick = (e) => {
    const li = e.target.closest("li");
    if (li) map.setView([+li.dataset.lat, +li.dataset.lon], 17);
  };
  routeLayer.clearLayers();
  stopsLayer.clearLayers();
  const pts = r.stops.map((s) => [s.lat, s.lon]);
  if (pts.length) {
    L.polyline(pts, { color: "#fff", weight: 9 }).addTo(routeLayer);
    L.polyline(pts, { color: c, weight: 6 }).addTo(routeLayer);
    r.stops.forEach((s) => L.circleMarker([s.lat, s.lon], { radius: s.id === focusStop ? 8 : 4, color: c, fillColor: "#fff", fillOpacity: 1, weight: s.id === focusStop ? 4 : 2 })
      .bindTooltip(`${hhmm(s.t)} ${esc(s.name)}`).addTo(routeLayer));
    map.fitBounds(routeLayer.getBounds().pad(0.1));
  }
  // מיקום חי של אוטובוסי הקו
  stopLive();
  const tick = async () => {
    try {
      const lv = await api("/api/live", { routes: r.route.gtfs_id });
      liveLayer.clearLayers();
      for (const v of lv.vehicles) {
        L.marker([v.lat, v.lon], { icon: L.divIcon({ html: `<div class="bus-icon" style="background:${c};width:26px;height:26px">${esc(v.short)}</div>`, className: "", iconSize: [26, 26] }), zIndexOffset: 500 })
          .bindPopup(`<b>קו ${esc(v.short)}</b> ${esc(v.headsign)}<br>${liveText(v, "")}`).addTo(liveLayer);
      }
      const mine = lv.vehicles.find((v) => v.trip === r.route.trip);
      const box = $("#trip-live");
      if (box) box.innerHTML = mine ? liveText(mine, "האוטובוס") : lv.vehicles.length ? `<span class="sub">${lv.vehicles.length} אוטובוסים של הקו על המפה כרגע</span>` : "";
    } catch { /* ignore */ }
  };
  tick();
  state.liveTimer = setInterval(tick, 30000);
}

// ---------------------------------------------------------------- קווים
async function searchLines() {
  const q = $("#line-q").value.trim();
  const box = $("#line-results");
  if (!q) return;
  box.innerHTML = `<div class="loading">מחפש...</div>`;
  const res = await api("/api/lines", { q });
  if (!res.length) { box.innerHTML = `<div class="empty">לא נמצא קו ${esc(q)} באזור ירושלים.</div>`; return; }
  const render = () => {
    box.innerHTML = res.map((r, i) => `<div class="variant" data-i="${i}" data-gtfs="${esc(r.gtfs_id)}">${badge(r)}<div><b>לכיוון ${esc(r.headsign || r.long.split("<->")[1] || "")}</b><div class="sub">${esc(r.agency)} · ${esc(r.long.replace(/-\d+#$/, "").replace("<->", " ← "))}</div></div></div>`).join("");
    box.onclick = async (e) => {
      const v = e.target.closest(".variant");
      if (!v) return;
      box.innerHTML = `<div class="loading">טוען...</div>`;
      const r = await api("/api/line", { route: res[+v.dataset.i].id });
      if (!r.stops.length) { box.innerHTML = `<div class="empty">אין נסיעות של הקו הזה בימים הקרובים.</div>`; return; }
      renderStopList(r, null, box, render);
    };
  };
  render();
}
$("#line-go").addEventListener("click", searchLines);
$("#line-q").addEventListener("keydown", (e) => { if (e.key === "Enter") searchLines(); });

// ---------------------------------------------------------------- התחלה
api("/api/meta").then((m) => {
  $("#feed").textContent = `לוחות זמנים של משרד התחבורה · עודכנו ${m.feed_date}`
    + (Number.isFinite(DEMO_MS) ? ` · מצב תצוגה: ${fmtW.format(DEMO_MS)} ${hhmm(DEMO_MS)}` : "");
  const ageDays = (Date.now() - Date.parse(m.feed_date)) / 864e5;
  const left = (Date.parse(m.service_end) - Date.now()) / 864e5;
  if (left < 3 || ageDays > 21) {
    const n = $("#notice");
    n.textContent = left < 0
      ? "לוחות הזמנים פגו! הריצו את 'עדכון לוחות זמנים.bat' כדי לקבל נתונים עדכניים."
      : "לוחות הזמנים ישנים. כדאי להריץ את 'עדכון לוחות זמנים.bat'.";
    n.hidden = false;
  }
}).catch(() => {});
// קישורים ישירים: ?to=31.79,35.24,שם  |  ?tab=near  |  ?line=18&route=<gtfs route id>
(function deepLinks() {
  const q = new URLSearchParams(location.search);
  if (q.get("to")) {
    const [lat, lon, ...name] = q.get("to").split(",");
    setPlace("to", { name: name.join(",") || "יעד", lat: +lat, lon: +lon, kind: "link" });
  } else if (q.get("tab") === "near") {
    showTab("near");
  } else if (q.get("line")) {
    showTab("lines");
    $("#line-q").value = q.get("line");
    searchLines().then(() => {
      const want = q.get("route");
      const items = [...document.querySelectorAll("#line-results .variant")];
      const el = (want && items.find((v) => v.dataset.gtfs === want)) || items[0];
      if (el) el.click();
    });
  } else {
    setTimeout(() => $("#to").focus(), 300);
  }
})();
