/* scenic web UI: one page, Leaflet, no build step. The server is scenic/web/server.py.
 *
 * Text from OpenStreetMap or Nominatim (moment labels, road names, place names) goes into
 * the page only through textContent or DOM nodes, never as an HTML string: Leaflet treats a
 * string passed to bindTooltip, bindPopup, setContent or divIcon({html}) as HTML. */
"use strict";

(function () {
  // Route colours by role index (server `role`: 0 fastest, k = k-th budget). Okabe-Ito blue,
  // orange and reddish purple for the budgets; the fastest route is a dashed grey line.
  const ROLE_COLORS = ["#6b6b6b", "#0072B2", "#E69F00", "#CC79A7"];
  const S_COLORS = ["#ffffcc", "#a1dab4", "#41b6c4", "#2c7fb8", "#253494"]; // YlGnBu, light = low S
  const S_WORDS = ["bottom fifth", "second fifth", "middle fifth", "fourth fifth", "top fifth"];
  const MOMENT_NAMES = { water: "Water", vista: "Vista", gorge: "Gorge or cliff", osm: "Mapped viewpoint or landmark" };
  const COORD_RE = /^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$/;
  const HOME_KEY = "scenic.home"; // {lat, lon, label}; generic so a loop mode can start from it too
  const UNITS_KEY = "scenic.units"; // "mi" | "km"
  const KM_PER_MI = 1.609344;
  const SNAP_NOTE_M = 500;

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  };
  const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);

  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage off: keep it for this page */ } },
  };

  const state = {
    ready: false, health: null,
    a: null, b: null, markerA: null, markerB: null, badPin: null,
    gen: 0, // bumped whenever A or B changes; a search answer for an older generation is dropped
    search: null, selected: null, visible: {}, waypoints: {},
    busy: false,
    units: store.get(UNITS_KEY) === "km" ? "km" : "mi",
  };
  let searchToken = 0; // the latest route request; older answers are dropped

  // ---- map -------------------------------------------------------------------

  const map = L.map("map", { zoomControl: true, preferCanvas: false }).setView([42.9, -75.8], 7);
  const streets = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  });
  const topo = L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}", {
      maxZoom: 19,
      attribution: "Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ, TomTom, Intermap, iPC, USGS, FAO, NPS, NRCAN, " +
        "GeoBase, Kadaster NL, Ordnance Survey, Esri Japan, METI, Esri China (Hong Kong), and the GIS User Community",
    });
  const satellite = L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
      maxZoom: 19,
      attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics, and the GIS User Community",
    });
  const BASES = { streets, topo, satellite };
  streets.addTo(map);
  L.control.layers({ "Streets (OpenStreetMap)": streets, "Terrain (Esri World Topo)": topo,
    "Satellite (Esri World Imagery)": satellite }, null, { position: "topright", collapsed: false }).addTo(map);
  L.control.scale({ imperial: true, metric: true }).addTo(map);

  const routeLayer = L.layerGroup().addTo(map);
  const scenicLayer = L.layerGroup().addTo(map);
  const momentLayer = L.layerGroup().addTo(map);
  const waypointLayer = L.layerGroup().addTo(map);

  // small key for the marks drawn over the routes, shown with results
  const mapKey = L.control({ position: "bottomleft" });
  mapKey.onAdd = () => {
    const div = el("div", "map-key");
    const row = (mark, text) => {
      const r = el("div", "row");
      r.appendChild(mark);
      r.appendChild(el("span", null, text));
      div.appendChild(r);
    };
    row(el("span", "k-stop"), "Scenic stop");
    row(el("span", "wp-pin", "1"), "Google Maps waypoint");
    row(el("span", "wp-pin weak", "2"), "Weaker waypoint (near a junction)");
    L.DomEvent.disableClickPropagation(div);
    return div;
  };
  let mapKeyShown = false;
  function showMapKey(on) {
    if (on && !mapKeyShown) mapKey.addTo(map);
    if (!on && mapKeyShown) mapKey.remove();
    mapKeyShown = on;
  }

  function pinIcon(which) {
    const div = el("div", `ab-pin ${which}${state.badPin === which ? " bad" : ""}`, which.toUpperCase());
    return L.divIcon({ className: "", html: div, iconSize: [26, 26], iconAnchor: [13, 13] });
  }

  function setBadPin(which) {
    if (state.badPin === which) return;
    state.badPin = which;
    for (const w of ["a", "b"]) {
      const m = state[w === "a" ? "markerA" : "markerB"];
      if (m) m.setIcon(pinIcon(w));
    }
  }

  function setPoint(which, latlng, label) {
    const ll = L.latLng(latlng);
    const key = which === "a" ? "markerA" : "markerB";
    state[which] = [ll.lat, ll.lng];
    if (!state[key]) {
      state[key] = L.marker(ll, { draggable: true, icon: pinIcon(which), autoPan: true,
        title: which === "a" ? "Start" : "End", zIndexOffset: 1000 }).addTo(map);
      state[key].on("dragend", (e) => {
        const p = e.target.getLatLng();
        state[which] = [p.lat, p.lng];
        $("q-" + which).value = fmt(p.lat, p.lng);
        onPointsChanged();
      });
    } else {
      state[key].setLatLng(ll);
    }
    $("q-" + which).value = label || fmt(ll.lat, ll.lng);
    $("res-" + which).hidden = true;
    onPointsChanged();
  }

  function removePoint(which) {
    const k = which === "a" ? "markerA" : "markerB";
    if (state[k]) { map.removeLayer(state[k]); state[k] = null; }
    state[which] = null;
    $("q-" + which).value = "";
    $("res-" + which).hidden = true;
  }

  function fmt(lat, lon) { return `${lat.toFixed(5)}, ${lon.toFixed(5)}`; }

  function onPointsChanged() {
    state.gen += 1;
    if (state.busy) { searchToken += 1; setBusy(false); } // the pending search is for the old points
    if (state.search) clearResults(); // routes and Maps links were for the old points
    setBadPin(null);
    updateFind();
    updateHome();
    $("hint").textContent = !state.a ? "Click the map to set the start, then the end."
      : !state.b ? "Click the map to set the end."
      : "Drag the markers to adjust, or press Find routes.";
  }

  function updateFind() {
    $("find").disabled = !(state.ready && state.a && state.b && !state.busy);
  }

  map.on("click", (e) => {
    if (!state.a) setPoint("a", e.latlng);
    else if (!state.b) setPoint("b", e.latlng);
    else $("hint").textContent = "Both points are set. Drag a marker to move it, or press Clear to start over.";
  });

  // ---- units -------------------------------------------------------------------

  function dist(km, digits) {
    const v = state.units === "mi" ? km / KM_PER_MI : km;
    return `${v.toFixed(digits === undefined ? 0 : digits)} ${state.units}`;
  }

  function setUnits(u) {
    state.units = u;
    store.set(UNITS_KEY, u);
    $("u-mi").setAttribute("aria-pressed", String(u === "mi"));
    $("u-km").setAttribute("aria-pressed", String(u === "km"));
    $("dist-head").textContent = u;
    if (state.search) { renderTable(); renderDetail(); }
  }

  // ---- home ----------------------------------------------------------------------

  function loadHome() {
    try {
      const h = JSON.parse(store.get(HOME_KEY) || "null");
      if (h && Number.isFinite(h.lat) && Number.isFinite(h.lon)) return h;
    } catch (e) { /* bad entry: no home */ }
    return null;
  }

  function updateHome() {
    const h = loadHome();
    $("home-a").disabled = !h;
    $("home-a").title = h ? `Start from ${h.label || fmt(h.lat, h.lon)}` : "Set a home point first";
    $("home-set-a").disabled = !state.a;
    $("home-set-b").disabled = !state.b;
  }

  function setHome(which) {
    const p = state[which];
    if (!p) return;
    const label = $("q-" + which).value.trim() || fmt(p[0], p[1]);
    store.set(HOME_KEY, JSON.stringify({ lat: p[0], lon: p[1], label }));
    updateHome();
    $("hint").textContent = `Home saved: ${label}. "From home" puts it in A.`;
  }

  function fromHome() {
    const h = loadHome();
    if (!h) return;
    setPoint("a", [h.lat, h.lon], h.label);
    map.panTo([h.lat, h.lon]);
  }

  // ---- API -------------------------------------------------------------------

  async function api(path, body) {
    const opts = body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    let r;
    try {
      r = await fetch(path, opts);
    } catch (e) {
      throw new Error("The scenic server is not responding. Check that `scenic serve` is still running.");
    }
    let data = null;
    try { data = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok) {
      const err = new Error(cap((data && data.error) || `HTTP ${r.status}. Try again.`));
      err.point = data && data.point === "from" ? "a" : data && data.point === "to" ? "b" : null;
      throw err;
    }
    return data;
  }

  function showError(msg, pin) {
    const e = $("error");
    e.textContent = cap(msg || "");
    e.hidden = !msg;
    setBadPin(msg ? pin || null : null);
  }

  function setBusy(on, text) {
    state.busy = on;
    $("busy").hidden = !on;
    if (text) $("busy-text").textContent = text;
    updateFind();
  }

  // ---- health ------------------------------------------------------------------

  async function pollHealth() {
    try {
      const h = await api("/api/health");
      state.health = h;
      if (h.status === "ready") {
        state.ready = true;
        $("loading").hidden = true;
        updateFind();
        maybeRunFromHash();
        return;
      }
      if (h.status === "error") {
        $("loading").className = "banner error";
        $("loading-text").textContent = "The route data failed to load: " + (h.error || "unknown error") +
          ". Check the data folders and restart `scenic serve`.";
        $("loading").querySelector(".spinner")?.remove();
        return;
      }
    } catch (e) {
      $("loading-text").textContent = "Waiting for the server…";
    }
    setTimeout(pollHealth, 1000);
  }

  // ---- geocoding ---------------------------------------------------------------

  function wireSearch(which) {
    $("form-" + which).addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const q = $("q-" + which).value.trim();
      const list = $("res-" + which);
      if (!q) return;
      const m = COORD_RE.exec(q);
      if (m) {
        const lat = parseFloat(m[1]), lon = parseFloat(m[2]);
        setPoint(which, [lat, lon]);
        map.panTo([lat, lon]);
        return;
      }
      showError("");
      list.hidden = false;
      list.replaceChildren(el("li", "none", "Searching…"));
      try {
        const data = await api("/api/geocode", { q });
        list.replaceChildren();
        if (!data.results.length) {
          list.appendChild(el("li", "none", "No places in New York State match that name. Try a town or county name."));
          return;
        }
        for (const r of data.results) {
          const li = el("li", null, r.name); // Nominatim text: textContent only
          li.addEventListener("click", () => {
            setPoint(which, [r.lat, r.lon], r.name);
            map.setView([r.lat, r.lon], Math.max(map.getZoom(), 11));
          });
          list.appendChild(li);
        }
      } catch (e) {
        list.hidden = true;
        showError(e.message);
      }
    });
  }

  // ---- routes ------------------------------------------------------------------

  function roleColor(r) { return ROLE_COLORS[r.role] || "#444"; }
  function routeName(r) { return r.labels.join(" = "); }
  function extraMin(r) {
    const x = Math.round(r.minutes - state.search.t0_min);
    return r.role === 0 || x <= 0 ? "—" : `+${x}`;
  }

  async function findRoutes() {
    if (!state.ready || !state.a || !state.b) return;
    const token = ++searchToken, gen = state.gen;
    showError("");
    setBusy(true, "Searching for scenic routes… (a few seconds)");
    try {
      const data = await api("/api/route", { from: state.a, to: state.b });
      if (token !== searchToken || gen !== state.gen) return; // points changed or a newer search started
      state.search = data;
      state.waypoints = {};
      state.visible = {};
      for (const r of data.routes) state.visible[r.key] = true;
      const pick = data.routes.find((r) => r.labels.includes("+30%")) || data.routes[data.routes.length - 1];
      state.selected = pick.key;
      writeHash();
      renderAll(true);
    } catch (e) {
      if (token !== searchToken || gen !== state.gen) return;
      showError(e.message, e.point);
    } finally {
      if (token === searchToken) setBusy(false);
    }
  }

  function clearResults() {
    state.search = null; state.selected = null; state.waypoints = {}; state.visible = {};
    routeLayer.clearLayers(); scenicLayer.clearLayers(); momentLayer.clearLayers(); waypointLayer.clearLayers();
    $("results").hidden = true;
    showMapKey(false);
  }

  function renderAll(fit) {
    renderTable();
    renderMap(fit);
    renderDetail();
  }

  function textNode(s) { return el("span", null, s); }

  function renderMap(fit) {
    routeLayer.clearLayers(); scenicLayer.clearLayers(); momentLayer.clearLayers(); waypointLayer.clearLayers();
    const s = state.search;
    showMapKey(!!s);
    if (!s) return;
    const bySel = $("by-s").checked;
    const bounds = L.latLngBounds([]);
    // unselected first, so the selected route is drawn on top
    const order = s.routes.filter((r) => r.key !== state.selected).concat(s.routes.filter((r) => r.key === state.selected));
    for (const r of order) {
      if (!state.visible[r.key] || !r.line.length) continue;
      const sel = r.key === state.selected;
      const color = roleColor(r);
      const dashed = r.role === 0;
      bounds.extend(L.latLngBounds(r.line));
      const tip = () => textNode(`${routeName(r)} · ${Math.round(r.minutes)} min`);
      if (!sel) {
        L.polyline(r.line, { color, weight: 3, opacity: 0.8, lineJoin: "round", dashArray: dashed ? "6 6" : null })
          .on("click", (ev) => { L.DomEvent.stopPropagation(ev); select(r.key); })
          .bindTooltip(tip(), { sticky: true })
          .addTo(routeLayer);
        continue;
      }
      if (bySel) {
        // the route's own colour as a casing, scenery classes inside it
        L.polyline(r.line, { color, weight: 12, opacity: 1, lineJoin: "round" })
          .bindTooltip(tip(), { sticky: true }).addTo(routeLayer);
        drawScenery(r);
      } else {
        L.polyline(r.line, { color: "#ffffff", weight: 11, opacity: 0.9, interactive: false }).addTo(routeLayer);
        L.polyline(r.line, { color, weight: 7, opacity: 1, lineJoin: "round", dashArray: dashed ? "10 8" : null })
          .bindTooltip(tip(), { sticky: true }).addTo(routeLayer);
      }
    }
    const sel = s.routes.find((r) => r.key === state.selected);
    if (sel && state.visible[sel.key]) {
      drawMoments(sel);
      drawWaypoints(sel);
    }
    if (fit && bounds.isValid()) map.fitBounds(bounds, { padding: [30, 30] });
  }

  function drawScenery(r) {
    for (const [a, b, c] of r.runs) {
      L.polyline(r.line.slice(a, b + 1), { color: S_COLORS[Math.min(c, S_COLORS.length - 1)], weight: 6,
        opacity: 1, lineJoin: "round", lineCap: "butt" })
        .on("click", (ev) => L.DomEvent.stopPropagation(ev))
        .bindTooltip(textNode(sClassText(c)), { sticky: true })
        .addTo(scenicLayer);
    }
  }

  function sClassText(c) {
    return `${cap(S_WORDS[Math.min(c, S_WORDS.length - 1)])} of NY roads for scenery`;
  }

  function minText(m) { return m.min_along === null || m.min_along === undefined ? "" : `min ${Math.round(m.min_along)} · `; }

  function momentPopup(m) {
    const div = el("div");
    div.appendChild(el("strong", null, m.label));
    const when = m.min_along === null || m.min_along === undefined ? "" : ` · ${Math.round(m.min_along)} min into the drive`;
    div.appendChild(el("div", "ptype", `${MOMENT_NAMES[m.type] || cap(m.type)}${when}`));
    const a = el("a", null, "Satellite view in Google Maps ↗");
    a.href = `https://www.google.com/maps/search/?api=1&query=${m.lat},${m.lon}`;
    a.target = "_blank"; a.rel = "noopener noreferrer";
    const p = el("div"); p.appendChild(a); div.appendChild(p);
    return div;
  }

  function drawMoments(r) {
    for (const m of r.moments) {
      // white with a dark ring: reads on every base layer, and against every route and scenery colour
      L.circleMarker([m.lat, m.lon], { radius: 7, color: "#0b0b0b", weight: 3, fillColor: "#ffffff",
        fillOpacity: 1 }).bindPopup(momentPopup(m)).bindTooltip(textNode(minText(m) + m.label)).addTo(momentLayer);
    }
  }

  function drawWaypoints(r) {
    const w = state.waypoints[r.key];
    if (!w || !w.data) return;
    for (const p of w.data.waypoints) {
      const icon = L.divIcon({ className: "", html: el("div", "wp-pin" + (p.tier > 1 ? " weak" : ""), String(p.n)),
        iconSize: [22, 22], iconAnchor: [11, 11] });
      L.marker([p.lat, p.lon], { icon, zIndexOffset: 500, title: `Waypoint ${p.n}` })
        .bindTooltip(textNode(`Waypoint ${p.n} on ${p.road}${p.tier > 1 ? " (weaker spot, near a junction)" : ""}`))
        .addTo(waypointLayer);
    }
  }

  // cumulative distance (km) along a [[lat, lon], ...] line, for the scenery strip
  function cumKm(line) {
    const out = new Float64Array(line.length);
    for (let i = 1; i < line.length; i++) {
      const [la1, lo1] = line[i - 1], [la2, lo2] = line[i];
      const kx = 111.32 * Math.cos(((la1 + la2) / 2) * Math.PI / 180);
      out[i] = out[i - 1] + Math.hypot((lo2 - lo1) * kx, (la2 - la1) * 110.57);
    }
    return out;
  }

  function sceneryStrip(r) {
    const div = el("div", "strip");
    div.title = "Scenery along this route, start to end";
    if (!r.line.length || !r.runs.length) return div;
    const cum = r._cum || (r._cum = cumKm(r.line));
    const total = cum[cum.length - 1] || 1;
    const stops = r.runs.map(([a, b, c]) => {
      const p0 = (100 * cum[a] / total).toFixed(2), p1 = (100 * cum[b] / total).toFixed(2);
      return `${S_COLORS[Math.min(c, S_COLORS.length - 1)]} ${p0}% ${p1}%`;
    });
    div.style.background = `linear-gradient(to right, ${stops.join(", ")})`;
    return div;
  }

  function roadsText(r) {
    const top = r.roads.slice(0, 3).map(([n, km]) => `${n} ${dist(km)}`);
    return top.join(", ") + (r.roads.length > 3 ? ", …" : "");
  }

  function swatch(r) {
    const sw = el("span", "swatch" + (r.role === 0 ? " dashed" : ""));
    if (r.role !== 0) sw.style.background = roleColor(r);
    return sw;
  }

  function renderTable() {
    const s = state.search;
    $("results").hidden = !s;
    if (!s) return;
    const tb = $("routes").querySelector("tbody");
    tb.replaceChildren();
    for (const r of s.routes) {
      const selCls = r.key === state.selected ? " selected" : "";
      const tr = el("tr", "route" + selCls);
      const name = el("td", "name");
      const cb = el("input"); cb.type = "checkbox"; cb.checked = !!state.visible[r.key];
      cb.title = "Show on map";
      cb.addEventListener("click", (ev) => { ev.stopPropagation(); state.visible[r.key] = cb.checked; renderMap(false); });
      name.appendChild(cb);
      name.appendChild(swatch(r));
      name.appendChild(el("span", "labels", routeName(r)));
      tr.appendChild(name);
      tr.appendChild(el("td", "num", r.minutes.toFixed(0)));
      tr.appendChild(el("td", "num", extraMin(r)));
      tr.appendChild(el("td", "num", dist(r.km).split(" ")[0]));
      tr.appendChild(el("td", "num", r.scenic_minutes.toFixed(0)));
      const sub = el("tr", "sub" + selCls);
      const td = el("td");
      td.colSpan = 5;
      td.appendChild(sceneryStrip(r));
      td.appendChild(el("div", "roads", roadsText(r))); // OSM road names: textContent only
      sub.appendChild(td);
      for (const row of [tr, sub]) row.addEventListener("click", () => select(r.key));
      tb.appendChild(tr);
      tb.appendChild(sub);
    }
    let det = `Details: ${s.kept} candidate routes compared in ${s.search_s} s.`;
    s.snap_m.forEach((m, i) => {
      if (m > SNAP_NOTE_M) det += ` ${i ? "B" : "A"} is ${dist(m / 1000, 1)} from the nearest road.`;
    });
    $("details").textContent = det;
    renderLegend();
  }

  function renderLegend() {
    const lg = $("legend");
    const s = state.search;
    lg.hidden = !s;
    if (lg.hidden) return;
    lg.replaceChildren();
    lg.appendChild(el("div", null, $("by-s").checked
      ? "Scenery along the selected route (inside its colored outline) and in the strips above:"
      : "Scenery in the strips above:"));
    const bar = el("div", "bar");
    S_COLORS.forEach((_, i) => bar.appendChild(el("span", "s" + i)));
    lg.appendChild(bar);
    const ticks = el("div", "ticks");
    S_WORDS.forEach((w) => ticks.appendChild(el("span", null, w)));
    lg.appendChild(ticks);
    lg.appendChild(el("div", "sub", "…of New York road miles, ranked by scenery score."));
  }

  function renderDetail() {
    const d = $("detail");
    d.replaceChildren();
    const s = state.search;
    const r = s && s.routes.find((x) => x.key === state.selected);
    if (!r) return;
    const extra = extraMin(r);
    d.appendChild(el("h3", null, `${routeName(r)}: ${Math.round(r.minutes)} min` +
      `${extra === "—" ? "" : ` (${extra} min)`}, ${dist(r.km)}`));
    const scen = r.mean_S_pct === null || r.mean_S_pct === undefined
      ? `Mean scenery score ${r.mean_S}.`
      : `On average, scenier than ${r.mean_S_pct}% of NY road miles.`;
    d.appendChild(el("div", "note", `${scen} ${Math.round(r.scenic_minutes)} scenic minutes.`));
    d.appendChild(mapsBlock(r));

    const n = r.moments.length;
    d.appendChild(el("h3", null, n ? `${n} scenic stop${n === 1 ? "" : "s"}, in drive order` : "No scenic stops on this route"));
    if (n) {
      const ol = el("ol", "moments");
      for (const m of r.moments) {
        const li = el("li");
        const link = el("a", null, minText(m) + m.label); // OSM text: textContent only
        link.href = "#";
        link.addEventListener("click", (ev) => {
          ev.preventDefault();
          map.setView([m.lat, m.lon], Math.max(map.getZoom(), 13));
          L.popup().setLatLng([m.lat, m.lon]).setContent(momentPopup(m)).openOn(map);
        });
        li.appendChild(link);
        li.appendChild(el("span", "mtype mt-" + m.type, (MOMENT_NAMES[m.type] || m.type).toLowerCase()));
        ol.appendChild(li);
      }
      d.appendChild(ol);
    }
  }

  // Google Maps: one action for the selected route. The first click reduces the route to
  // waypoints and shows the result here; the link that opens Google is shown only after that.
  function mapsBlock(r) {
    const box = el("div", "gm");
    const w = state.waypoints[r.key];
    if (!w || w.error) {
      const btn = el("button", "primary", w && w.error ? "Try the Google Maps link again" : "Get a Google Maps link");
      btn.type = "button";
      btn.addEventListener("click", () => getWaypoints(r.key));
      box.appendChild(btn);
      if (w && w.error) box.appendChild(el("div", "banner error", w.error));
      else box.appendChild(el("div", "note", "Places up to 9 waypoints so Google Maps follows this route."));
      return box;
    }
    if (w.pending) {
      const btn = el("button", "primary");
      btn.type = "button"; btn.disabled = true;
      btn.append(el("span", "spinner"), document.createTextNode(" Placing waypoints…"));
      box.appendChild(btn);
      return box;
    }
    const x = w.data;
    const pct = Math.round(x.summary.overlap * 100);
    box.appendChild(el("div", "note", `${x.waypoints.length} waypoint${x.waypoints.length === 1 ? "" : "s"} ` +
      `(numbered on the map). With them, Google's route follows ${pct}% of this one.`));
    if (!x.meets_target) {
      box.appendChild(el("div", "banner warn",
        `Google may not follow this route closely: the waypoints reproduce ${pct}% of it ` +
        `(target ${Math.round(x.follow_target * 100)}%). Check the route in Google Maps before driving.`));
    }
    if (x.notes.length) { // technical notes from the waypoint placer, folded away
      const det = el("details", "note");
      det.appendChild(el("summary", null, `Notes on ${x.notes.length} waypoint${x.notes.length === 1 ? "" : "s"}`));
      const ul = el("ul");
      for (const n of x.notes) ul.appendChild(el("li", null, n));
      det.appendChild(ul);
      box.appendChild(det);
    }
    const a = el("a", "open", "Open in Google Maps ↗");
    a.href = x.url; a.target = "_blank"; a.rel = "noopener noreferrer";
    box.appendChild(a);
    return box;
  }

  async function getWaypoints(key) {
    const s = state.search;
    if (!s) return;
    const sid = s.id; // the answer belongs to this search only
    const cur = state.waypoints[key];
    if (cur && (cur.pending || cur.data)) return;
    state.waypoints[key] = { pending: true };
    renderDetail();
    let result;
    try {
      result = { data: await api("/api/waypoints", { id: sid, key }) };
    } catch (e) {
      result = { error: e.message };
    }
    if (!state.search || state.search.id !== sid) return; // a newer search (or Clear) replaced it
    state.waypoints[key] = result;
    if (state.selected === key) { renderMap(false); renderDetail(); }
  }

  function select(key) {
    if (!state.search) return;
    state.selected = key;
    state.visible[key] = true;
    writeHash();
    renderAll(false);
  }

  // ---- URL hash: #from=lat,lon&to=lat,lon&route=+30%&scenery=0&layer=topo ----------

  function currentBase() {
    for (const [name, layer] of Object.entries(BASES)) if (map.hasLayer(layer)) return name;
    return "streets";
  }

  function writeHash() {
    const parts = [];
    if (state.a && state.b) {
      parts.push(`from=${state.a[0].toFixed(5)},${state.a[1].toFixed(5)}`, `to=${state.b[0].toFixed(5)},${state.b[1].toFixed(5)}`);
      const sel = state.search && state.search.routes.find((r) => r.key === state.selected);
      if (sel) parts.push("route=" + encodeURIComponent(sel.labels[0]));
    }
    if (!$("by-s").checked) parts.push("scenery=0");
    const base = currentBase();
    if (base !== "streets") parts.push("layer=" + base);
    history.replaceState(null, "", parts.length ? "#" + parts.join("&") : location.pathname);
  }

  (function readViewFromHash() {
    const h = new URLSearchParams(location.hash.slice(1));
    if (h.get("scenery") === "0") $("by-s").checked = false;
    const base = BASES[h.get("layer")];
    if (base && base !== streets) { map.removeLayer(streets); base.addTo(map); }
  })();
  map.on("baselayerchange", writeHash);

  let hashDone = false;
  function maybeRunFromHash() {
    if (hashDone) return;
    hashDone = true;
    const h = new URLSearchParams(location.hash.slice(1));
    const pa = COORD_RE.exec(h.get("from") || ""), pb = COORD_RE.exec(h.get("to") || "");
    if (!pa || !pb) return;
    setPoint("a", [parseFloat(pa[1]), parseFloat(pa[2])]);
    setPoint("b", [parseFloat(pb[1]), parseFloat(pb[2])]);
    const want = h.get("route");
    findRoutes().then(() => {
      if (!want || !state.search) return;
      const r = state.search.routes.find((x) => x.labels.includes(want));
      if (r) select(r.key);
    });
  }

  // ---- wiring ----------------------------------------------------------------

  wireSearch("a");
  wireSearch("b");
  $("find").addEventListener("click", findRoutes);
  $("clear").addEventListener("click", () => {
    removePoint("a");
    removePoint("b");
    clearResults();
    showError("");
    writeHash();
    onPointsChanged();
  });
  $("swap").addEventListener("click", () => {
    const a = state.a, b = state.b, qa = $("q-a").value, qb = $("q-b").value;
    if (!a && !b) return;
    for (const [w, p, q] of [["a", b, qb], ["b", a, qa]]) {
      if (p) setPoint(w, p, q);
      else removePoint(w);
    }
    clearResults();
    showError("");
    onPointsChanged();
  });
  $("by-s").addEventListener("change", () => { renderLegend(); renderMap(false); writeHash(); });
  $("u-mi").addEventListener("click", () => setUnits("mi"));
  $("u-km").addEventListener("click", () => setUnits("km"));
  $("home-a").addEventListener("click", fromHome);
  $("home-set-a").addEventListener("click", () => setHome("a"));
  $("home-set-b").addEventListener("click", () => setHome("b"));
  $("tab-loop").addEventListener("click", (ev) => ev.preventDefault());

  setUnits(state.units);
  onPointsChanged();
  pollHealth();
})();
