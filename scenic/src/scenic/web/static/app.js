/* Maps for Wanderers: one page, Leaflet, no build step. The server is scenic/web/server.py.
 *
 * Two modes. Live (default): the page talks to `scenic serve` through /api/*. Demo
 * (<html data-mode="demo">, written by `scenic demo build`): read-only preset A-to-B routes and
 * precomputed Drive-around loops from data/*.json by relative path; no /api calls, no geocoding.
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
  const DEMO = document.documentElement.dataset.mode === "demo";

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

  const PIN_LETTERS = { a: "A", b: "B", h: "S", x: "P" };
  const PIN_TITLES = { a: "Start", b: "End", h: "Start and finish", x: "Place or scenic feature" };
  const POINT_PINS = { from: "a", to: "b", start: "h", anchor: "x" };
  const LOOP_DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
  const SAMPLES_AB = {
    "finger-lakes": { from: [43.1566, -77.6088], fromLabel: "Rochester", to: [42.3806, -76.8733], toLabel: "Watkins Glen" },
    catskills: { from: [41.9270, -73.9974], fromLabel: "Kingston", to: [42.4529, -75.0638], toLabel: "Oneonta" },
    adirondacks: { from: [43.4262, -73.7123], fromLabel: "Lake George", to: [44.2795, -73.9799], toLabel: "Lake Placid" },
  };
  const SAMPLES_LOOP = {
    auburn: { start: [42.9318, -76.5661], label: "Auburn", minutes: 120 },
    ithaca: { start: [42.4440, -76.5019], label: "Ithaca", minutes: 120 },
    "lake-placid": { start: [44.2795, -73.9799], label: "Lake Placid", minutes: 120 },
  };

  function emptySlot() {
    return { search: null, selected: null, visible: {}, waypoints: {}, lastError: null };
  }

  const state = {
    ready: false, health: null, mode: "ab", loopOpenedOnce: false,
    a: null, b: null, h: null, x: null,
    markers: { a: null, b: null, h: null, x: null }, badPin: null,
    loop: { minutes: 120, steer: "any", dir: null, feature: null, featurePoint: null, chips: [] },
    byMode: { ab: emptySlot(), loop: emptySlot() },
    gen: 0, // bumped whenever A or B changes; a search answer for an older generation is dropped
    loopGen: 0, // bumped whenever loop inputs change
    search: null, selected: null, visible: {}, waypoints: {},
    busy: false, lastError: null, presets: null, preset: null, pendingSampleRun: null,
    units: store.get(UNITS_KEY) === "km" ? "km" : "mi",
  };
  let searchToken = 0; // the latest route request; older answers are dropped
  let chipToken = 0;
  let busyTimer = null;

  // ---- map -------------------------------------------------------------------

  const map = L.map("map", { zoomControl: true, preferCanvas: false }).setView([42.9, -75.8], 7);
  // Base layers and their terms: docs/PUBLISH.md. No API keys. Tiles send the page's origin as
  // Referer (OSM's tile policy asks for one); the page itself never sends a full URL off-site.
  const TILE_OPTS = { maxZoom: 19, referrerPolicy: "strict-origin-when-cross-origin" };
  const streets = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    ...TILE_OPTS,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  });
  const USGS_ATTR = 'Map services and data from the <a href="https://www.usgs.gov/programs/national-geospatial-program/national-map">' +
    "U.S. Geological Survey, National Geospatial Program</a>";
  const topo = L.tileLayer(
    "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}", {
      ...TILE_OPTS, maxNativeZoom: 16, attribution: USGS_ATTR,
    });
  const satellite = L.tileLayer(
    "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}", {
      ...TILE_OPTS, maxNativeZoom: 16, attribution: USGS_ATTR,
    });
  const BASES = { streets, topo, satellite };
  streets.addTo(map);
  L.control.layers({ "Streets (OpenStreetMap)": streets, "Terrain (USGS Topo)": topo,
    "Satellite (USGS Imagery)": satellite }, null, { position: "topright", collapsed: window.matchMedia("(max-width: 900px)").matches }).addTo(map);
  // the routes, stops and place names are OpenStreetMap data whatever the base layer (ODbL)
  map.attributionControl.addAttribution('Routes: <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> data');
  L.control.scale({ imperial: true, metric: true }).addTo(map);

  const routeLayer = L.layerGroup().addTo(map);
  const scenicLayer = L.layerGroup().addTo(map);
  const momentLayer = L.layerGroup().addTo(map);
  const waypointLayer = L.layerGroup().addTo(map);

  // small key for the marks drawn over the routes, shown with results
  let splitKeyRow = null, splitKeyPin = null;
  const mapKey = L.control({ position: "bottomleft" });
  mapKey.onAdd = () => {
    const div = el("div", "map-key");
    const row = (mark, text) => {
      const r = el("div", "row");
      r.appendChild(mark);
      r.appendChild(el("span", null, text));
      div.appendChild(r);
      return r;
    };
    row(el("span", "k-stop"), "Scenic stop");
    row(el("span", "wp-pin", "1"), "Google Maps waypoint");
    splitKeyPin = el("span", "wp-pin split", "7");
    splitKeyRow = row(splitKeyPin, "Part handoff waypoint");
    splitKeyRow.hidden = true;
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
    const div = el("div", `ab-pin ${which}${state.badPin === which ? " bad" : ""}`, PIN_LETTERS[which] || which.toUpperCase());
    return L.divIcon({ className: "", html: div, iconSize: [26, 26], iconAnchor: [13, 13] });
  }

  function setBadPin(which) {
    if (state.badPin === which) return;
    state.badPin = which;
    for (const w of ["a", "b", "h", "x"]) {
      const m = state.markers[w];
      if (m) m.setIcon(pinIcon(w));
    }
  }

  function syncMarkers() {
    const want = {
      a: state.mode === "ab" ? state.a : null,
      b: state.mode === "ab" ? state.b : null,
      h: state.mode === "loop" ? state.h : null,
      x: state.mode === "loop" && state.loop.steer === "anchor" ? (state.x || state.loop.featurePoint) : null,
    };
    for (const [w, pt] of Object.entries(want)) {
      const m = state.markers[w];
      if (!pt) {
        if (m && map.hasLayer(m)) map.removeLayer(m);
        continue;
      }
      if (!m) {
        const mk = L.marker(pt, { draggable: !DEMO, icon: pinIcon(w), autoPan: true,
          title: PIN_TITLES[w], zIndexOffset: w === "x" ? 950 : 1000 }).addTo(map);
        mk.on("dragend", (e) => {
          const p = e.target.getLatLng();
          if (w === "x") {
            state.x = [p.lat, p.lng];
            state.loop.feature = null;
            state.loop.featurePoint = null;
            $("q-x").value = fmt(p.lat, p.lng);
            $("anchor-note").hidden = true;
            syncChipSelection();
            onLoopChanged(false);
          } else if (w === "h") {
            state.h = [p.lat, p.lng];
            $("q-h").value = fmt(p.lat, p.lng);
            onLoopChanged(true);
          } else {
            state[w] = [p.lat, p.lng];
            $("q-" + w).value = fmt(p.lat, p.lng);
            onPointsChanged();
          }
        });
        state.markers[w] = mk;
      } else {
        m.setLatLng(pt);
        m.setIcon(pinIcon(w));
        if (!map.hasLayer(m)) m.addTo(map);
      }
    }
  }

  function setPoint(which, latlng, label) {
    const ll = L.latLng(latlng);
    state[which] = [ll.lat, ll.lng];
    if (which === "x") {
      state.loop.feature = null;
      state.loop.featurePoint = null;
      $("anchor-note").hidden = true;
      syncChipSelection();
    }
    $("q-" + which).value = label || fmt(ll.lat, ll.lng);
    $("res-" + which).hidden = true;
    syncMarkers();
    if (which === "a" || which === "b") onPointsChanged();
    else onLoopChanged(which === "h");
  }

  function removePoint(which) {
    if (state.markers[which]) { map.removeLayer(state.markers[which]); state.markers[which] = null; }
    state[which] = null;
    if (which === "x") {
      state.loop.feature = null;
      state.loop.featurePoint = null;
      $("anchor-note").hidden = true;
      syncChipSelection();
      $("clear-x").hidden = true;
    }
    $("q-" + which).value = "";
    $("res-" + which).hidden = true;
  }

  function fmt(lat, lon) { return `${lat.toFixed(5)}, ${lon.toFixed(5)}`; }

  function syncStarters() {
    $("empty-ab").hidden = DEMO || state.mode !== "ab" || state.busy || !!state.search;
    $("empty-loop").hidden = DEMO || state.mode !== "loop" || state.busy || !!state.search;
  }

  function syncMapCursor() {
    const picking = !DEMO && (state.mode === "ab" ? (!state.a || !state.b)
      : (!state.h || state.loop.steer === "anchor"));
    $("map").classList.toggle("picking", picking);
  }

  function onPointsChanged() {
    state.gen += 1;
    state.pendingSampleRun = null;
    if (state.mode === "ab") {
      if (state.busy) { searchToken += 1; setBusy(false); } // the pending search is for the old points
      if (state.search) clearResults(); // routes and Maps links were for the old points
      if (state.badPin) showError("");
      else setBadPin(null);
    } else {
      state.byMode.ab = emptySlot();
    }
    updateFind();
    updateHome();
    syncStarters();
    syncMapCursor();
    // keep the link in step with the points (not before a #from=…&to=… link has been read)
    if (DEMO || hashDone) writeHash();
    $("hint").textContent = !state.a ? "Click the map to set the start, then the end."
      : !state.b ? "Click the map to set the end."
      : "Drag the markers to adjust, or press Find routes.";
  }

  function onLoopChanged(refreshAnchorChips) {
    state.loopGen += 1;
    state.pendingSampleRun = null;
    if (state.mode === "loop") {
      if (state.busy) { searchToken += 1; setBusy(false); }
      if (state.search) clearResults();
      if (state.badPin) showError("");
      else setBadPin(null);
    } else {
      state.byMode.loop = emptySlot();
    }
    $("clear-x").hidden = !(state.x || state.loop.feature);
    syncMarkers();
    updateFind();
    updateHome();
    updateLoopHint();
    syncStarters();
    syncMapCursor();
    if (refreshAnchorChips && state.mode === "loop" && state.loop.steer === "anchor") refreshChips();
    if (hashDone) writeHash();
  }

  function steerReady() {
    if (state.loop.steer === "compass") return !!state.loop.dir;
    if (state.loop.steer === "anchor") return !!(state.x || state.loop.feature);
    return true;
  }

  function updateFind() {
    $("find").disabled = !(state.ready && state.a && state.b && !state.busy);
    $("find-loop").disabled = !(state.ready && state.h && steerReady() && !state.busy);
  }

  function updateLoopHint() {
    $("hint-loop").textContent = !state.h ? "Click the map to set the start, or use From home."
      : state.loop.steer === "compass" && !state.loop.dir ? "Pick a compass direction above."
      : state.loop.steer === "anchor" && !state.x && !state.loop.feature
        ? "Pick a scenic feature chip, search a place, or click the map (water is fine)."
      : state.loop.steer === "anchor" ? "Click the map or drag P to move the place, or press Find loops."
      : "Drag S to adjust the start, or press Find loops.";
  }

  map.on("click", (e) => {
    if (DEMO) return; // read-only: the presets set A and B
    if (state.mode === "loop") {
      if (!state.h) setPoint("h", e.latlng);
      else if (state.loop.steer === "anchor") setPoint("x", e.latlng);
      else $("hint-loop").textContent = "The start is set. Drag the marker to move it.";
      return;
    }
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
    if (state.lastError) showError(state.lastError); // distances in errors follow the units too
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
    const title = h ? `Start from ${h.label || fmt(h.lat, h.lon)}` : "Set a home point first";
    for (const id of ["home-a", "home-h"]) {
      $(id).disabled = !h;
      $(id).title = title;
    }
    $("home-set-a").disabled = !state.a;
    $("home-set-b").disabled = !state.b;
    $("home-set-h").disabled = !state.h;
  }

  function setHome(which) {
    const p = state[which];
    if (!p) return;
    const label = $("q-" + which).value.trim() || fmt(p[0], p[1]);
    store.set(HOME_KEY, JSON.stringify({ lat: p[0], lon: p[1], label }));
    updateHome();
    if (which === "h") $("hint-loop").textContent = `Home saved: ${label}. "From home" puts it in S.`;
    else $("hint").textContent = `Home saved: ${label}. "From home" puts it in A.`;
  }

  function fromHome(target) {
    const which = typeof target === "string" ? target : "a";
    const h = loadHome();
    if (!h) return;
    setPoint(which, [h.lat, h.lon], h.label);
    map.panTo([h.lat, h.lon]);
  }

  // ---- API -------------------------------------------------------------------

  async function api(path, body) {
    if (DEMO) throw new Error("The demo has no server."); // never reached: demo code paths read data/*.json
    const opts = body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    let r;
    try {
      r = await fetch(path, opts);
    } catch (e) {
      throw new Error("The server is not responding. If you run it, check that `scenic serve` is still running.");
    }
    let data = null;
    try { data = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok) {
      const err = new Error(cap((data && data.error) || `HTTP ${r.status}. Try again.`));
      err.point = (data && POINT_PINS[data.point]) || null;
      err.data = data; // raw numbers (snap_m, limit_m) for messages in the page's units
      throw err;
    }
    return data;
  }

  // The server sends distances as numbers; the words are built here, in the chosen units.
  function errorText(err) {
    const d = err && err.data;
    if (d && d.code === "snap_far" && Number.isFinite(d.snap_m) && Number.isFinite(d.limit_m)) {
      const [which, pin] = d.point === "to" ? ["end", "B"]
        : d.point === "start" ? ["start", "S"]
        : d.point === "anchor" ? ["anchor", "P"]
        : ["start", "A"];
      return `The ${which} point is ${dist(d.snap_m / 1000, 1)} from the nearest road in the graph ` +
        `(limit ${dist(d.limit_m / 1000, 1)}). Drag ${pin} onto a road.`;
    }
    if (d && d.code === "anchor_out_of_reach" && Number.isFinite(d.round_trip_min) && Number.isFinite(d.needed_min)) {
      return `That place is at least ${Math.round(d.round_trip_min)} min out and back from the start. ` +
        `Try ${Math.round(d.needed_min)} min or more, or pick a closer place.`;
    }
    return err && err.message !== undefined ? err.message : err || "";
  }

  // showError(message or Error, pin); an Error is kept so a units switch can re-word it
  function showError(err, pin) {
    const msg = typeof err === "string" ? err : errorText(err);
    state.lastError = msg && typeof err !== "string" ? err : null;
    const e = $("error");
    e.textContent = cap(msg || "");
    e.hidden = !msg;
    setBadPin(msg ? pin || (err && err.point) || null : null);
  }

  function setBusy(on, text) {
    state.busy = on;
    if (busyTimer) { clearInterval(busyTimer); busyTimer = null; }
    $("busy").hidden = !on;
    if (on && text) {
      const t0 = Date.now();
      $("busy-text").textContent = text;
      busyTimer = setInterval(() => {
        const s = Math.floor((Date.now() - t0) / 1000);
        if (s >= 2) $("busy-text").textContent = `${text} (${s} s)`;
      }, 500);
    } else if (text) {
      $("busy-text").textContent = text;
    }
    syncStarters();
    updateFind();
  }

  // ---- health ------------------------------------------------------------------

  async function pollHealth() {
    try {
      const h = await api("/api/health");
      state.health = h;
      if (h.status === "ready") {
        state.ready = true;
        if (h.loop) {
          $("min-custom").min = String(h.loop.min_minutes || 30);
          $("min-custom").max = String(h.loop.max_minutes || 360);
        }
        $("loading").hidden = true;
        updateFind();
        maybeRunFromHash();
        if (state.pendingSampleRun === "ab" && state.mode === "ab" && state.a && state.b) {
          state.pendingSampleRun = null;
          findRoutes();
        } else if (state.pendingSampleRun === "loop" && state.mode === "loop" && state.h && steerReady()) {
          state.pendingSampleRun = null;
          findLoops();
        }
        if (state.mode === "loop" && state.loop.steer === "anchor" && state.h) refreshChips();
        return;
      }
      if (h.status === "error") {
        $("loading").className = "banner error";
        $("loading-text").textContent = "The route data failed to load: " + (h.error || "unknown error") +
          ". Run `scenic setup --check` to inspect the data files, then restart `scenic serve`.";
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
        list.appendChild(el("li", "none attr", "Place search: Nominatim, © OpenStreetMap contributors"));
      } catch (e) {
        list.hidden = true;
        showError(e);
      }
    });
  }

  function setMode(mode, wantRoute) {
    if ((mode !== "ab" && mode !== "loop") || mode === state.mode) return;
    if (DEMO && mode === "loop" && !(state.presets && state.presets.loop)) return;
    if (state.busy) {
      searchToken += 1;
      presetToken += 1;
      setBusy(false);
    }
    state.byMode[state.mode] = {
      search: state.search, selected: state.selected,
      visible: state.visible, waypoints: state.waypoints, lastError: state.lastError,
    };
    state.mode = mode;
    for (const [m, tabId] of [["ab", "tab-ab"], ["loop", "tab-loop"]]) {
      const on = m === mode;
      $(tabId).classList.toggle("active", on);
      $(tabId).setAttribute("aria-selected", String(on));
    }
    $("presets").hidden = !(DEMO && mode === "ab");
    $("panel-ab").hidden = DEMO || mode !== "ab";
    $("panel-loop").hidden = mode !== "loop";
    if (!DEMO && mode === "loop" && !state.loopOpenedOnce) {
      state.loopOpenedOnce = true;
      if (!state.h && state.a) {
        state.h = state.a.slice();
        $("q-h").value = $("q-a").value;
      }
    }
    const slot = state.byMode[mode] || emptySlot();
    state.search = slot.search;
    state.selected = slot.selected;
    state.visible = slot.visible;
    state.waypoints = slot.waypoints;
    state.lastError = slot.lastError;
    if (DEMO) {
      if (mode === "loop") {
        state.h = state.presets.loop.start.slice();
        syncDemoLoopControls();
        if (!state.search || wantRoute) {
          loadDemoLoop(wantRoute || null);
          return;
        }
      } else if (!state.search) {
        const first = state.presets && state.presets.presets && state.presets.presets[0];
        if (first) {
          loadPreset(first.id, wantRoute || null);
          return;
        }
      }
      syncMarkers();
      if (state.lastError) showError(state.lastError);
      else showError("");
      renderAll(true);
      writeHash();
      return;
    }
    syncMarkers();
    if (state.lastError) showError(state.lastError);
    else showError("");
    updateHome();
    updateFind();
    updateLoopHint();
    syncStarters();
    syncMapCursor();
    if (mode === "loop" && state.loop.steer === "anchor" && state.ready && state.h) refreshChips();
    renderAll(false);
    if (hashDone) writeHash();
  }

  function clampMinutes(v) {
    const lo = (state.health && state.health.loop && state.health.loop.min_minutes) || 30;
    const hi = (state.health && state.health.loop && state.health.loop.max_minutes) || 360;
    return Math.max(lo, Math.min(hi, Math.round(v)));
  }

  function setMinutes(mins, fromCustom) {
    if (!Number.isFinite(mins)) return;
    if (DEMO) {
      const lp = state.presets && state.presets.loop;
      const m = Math.round(mins);
      if (!lp || !lp.budgets.includes(m)) return;
      state.loop.minutes = m;
      syncDemoLoopControls();
      if (state.mode === "loop") loadDemoLoop();
      return;
    }
    const m = clampMinutes(mins);
    state.loop.minutes = m;
    for (const b of document.querySelectorAll("#budget .seg button")) {
      b.setAttribute("aria-pressed", String(!fromCustom && Number(b.dataset.min) === m));
    }
    $("min-custom").value = fromCustom || ![60, 120, 180].includes(m) ? String(m) : "";
    onLoopChanged(true);
  }

  function setSteer(steer) {
    if (!["any", "compass", "anchor"].includes(steer)) return;
    if (DEMO) {
      state.loop.steer = steer;
      syncDemoLoopControls();
      if (state.mode === "loop") loadDemoLoop();
      return;
    }
    state.loop.steer = steer;
    for (const b of document.querySelectorAll("#steer > .seg button")) {
      b.setAttribute("aria-pressed", String(b.dataset.steer === steer));
    }
    $("rose").hidden = steer !== "compass";
    $("anchor-box").hidden = steer !== "anchor";
    onLoopChanged(steer === "anchor");
  }

  function setDir(dir) {
    const d = LOOP_DIRS.includes(dir) ? dir : null;
    if (DEMO) {
      if (!d) return;
      state.loop.dir = d;
      syncDemoLoopControls();
      if (state.mode === "loop") loadDemoLoop();
      return;
    }
    state.loop.dir = d;
    for (const b of document.querySelectorAll("#rose button")) {
      b.setAttribute("aria-pressed", String(b.dataset.dir === d));
    }
    onLoopChanged(false);
  }

  function syncChipSelection() {
    for (const b of document.querySelectorAll("#chips button.chip")) {
      b.setAttribute("aria-pressed", String(!!state.loop.feature && b.dataset.feature === state.loop.feature));
    }
  }

  function selectFeature(feat, pan) {
    state.x = null;
    state.loop.feature = feat.name;
    state.loop.featurePoint = Array.isArray(feat.point) ? feat.point.slice() : null;
    $("q-x").value = feat.name;
    $("res-x").hidden = true;
    $("anchor-note").hidden = true;
    syncChipSelection();
    if (pan && state.loop.featurePoint) map.panTo(state.loop.featurePoint);
    onLoopChanged(false);
  }

  async function refreshChips() {
    const ul = $("chips");
    if (!state.ready || !state.h) { ul.replaceChildren(); return; }
    const tok = ++chipToken;
    try {
      const data = await api("/api/loop/features", { start: state.h, minutes: state.loop.minutes, limit: 8 });
      if (tok !== chipToken) return;
      const fits = (data.features || []).filter((f) => f.fits).slice(0, 8);
      state.loop.chips = fits;
      ul.replaceChildren();
      for (const f of fits) {
        const li = el("li");
        const btn = el("button", "chip", f.name); // OSM feature text: textContent only
        btn.type = "button";
        btn.dataset.feature = f.name;
        btn.title = `About ${Math.round(f.round_trip_min)} min out and back`;
        btn.setAttribute("aria-pressed", String(state.loop.feature === f.name));
        btn.addEventListener("click", () => selectFeature(f, true));
        li.appendChild(btn);
        ul.appendChild(li);
      }
      if (state.loop.feature && !state.loop.featurePoint) {
        const hit = fits.find((f) => f.name.toLowerCase() === state.loop.feature.toLowerCase());
        if (hit && Array.isArray(hit.point)) {
          state.loop.featurePoint = hit.point.slice();
          syncMarkers();
        }
      }
    } catch (e) {
      if (tok === chipToken) ul.replaceChildren();
    }
  }

  function wireAnchorSearch() {
    let anchorSearchToken = 0;
    $("form-x").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const q = $("q-x").value.trim();
      const list = $("res-x");
      if (!q) return;
      const m = COORD_RE.exec(q);
      if (m) {
        anchorSearchToken++;
        const lat = parseFloat(m[1]), lon = parseFloat(m[2]);
        setPoint("x", [lat, lon]);
        map.panTo([lat, lon]);
        return;
      }
      const tok = ++anchorSearchToken;
      showError("");
      list.hidden = false;
      list.replaceChildren(el("li", "none", "Searching…"));
      const featCall = state.ready && state.h
        ? api("/api/loop/features", { start: state.h, minutes: state.loop.minutes, q, limit: 8 })
        : Promise.resolve({ features: [] });
      const [fr, gr] = await Promise.allSettled([featCall, api("/api/geocode", { q })]);
      if (tok !== anchorSearchToken) return;
      const feats = fr.status === "fulfilled" && fr.value && Array.isArray(fr.value.features) ? fr.value.features : [];
      const places = gr.status === "fulfilled" && gr.value && Array.isArray(gr.value.results) ? gr.value.results : [];
      list.replaceChildren();
      if (!feats.length && !places.length) {
        if (gr.status === "rejected") {
          list.hidden = true;
          showError(gr.reason);
          return;
        }
        list.appendChild(el("li", "none", "No scenic features or New York places match that name."));
        return;
      }
      if (feats.length) {
        list.appendChild(el("li", "none group-head", "Scenic features"));
        for (const f of feats) {
          const li = el("li");
          li.appendChild(el("span", null, f.name)); // OSM feature text: textContent only
          const meta = f.fits
            ? ` · ~${Math.round(f.round_trip_min)} min out and back`
            : ` · needs about ${Math.round(f.needed_min)} min`;
          li.appendChild(el("span", "meta", meta));
          li.addEventListener("click", () => selectFeature(f, true));
          list.appendChild(li);
        }
      }
      if (places.length) {
        list.appendChild(el("li", "none group-head", "Places"));
        for (const r of places) {
          const li = el("li", null, r.name); // Nominatim text: textContent only
          li.addEventListener("click", () => {
            setPoint("x", [r.lat, r.lon], r.name);
            map.setView([r.lat, r.lon], Math.max(map.getZoom(), 11));
          });
          list.appendChild(li);
        }
        list.appendChild(el("li", "none attr", "Place search: Nominatim, © OpenStreetMap contributors"));
      }
    });
  }

  // ---- routes and loops --------------------------------------------------------

  function roleColor(r) { return ROLE_COLORS[r.role] || "#444"; }
  function routeName(r) { return r.labels.join(" = "); }
  function extraMin(r) {
    const x = Math.round(r.minutes - state.search.t0_min);
    return r.role === 0 || x <= 0 ? "—" : `+${x}`;
  }

  function revealResults() {
    if (DEMO || $("results").hidden) return;
    const panel = $("panel"), res = $("results");
    const pr = panel.getBoundingClientRect(), rr = res.getBoundingClientRect();
    if (rr.top > pr.bottom - 72) res.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  async function findRoutes() {
    if (!state.ready || !state.a || !state.b) return;
    const token = ++searchToken, gen = state.gen;
    showError("");
    setBusy(true, "Searching for scenic routes… (a few seconds)");
    try {
      const data = await api("/api/route", { from: state.a, to: state.b });
      if (token !== searchToken || gen !== state.gen || state.mode !== "ab") return; // points changed or a newer search started
      state.search = data;
      state.waypoints = {};
      state.visible = {};
      for (const r of data.routes) state.visible[r.key] = true;
      const pick = data.routes.find((r) => r.labels.includes("+30%")) || data.routes[data.routes.length - 1];
      state.selected = pick.key;
      writeHash();
      renderAll(true);
      revealResults();
    } catch (e) {
      if (token !== searchToken || gen !== state.gen || state.mode !== "ab") return;
      showError(e, e.point);
    } finally {
      if (token === searchToken) setBusy(false);
    }
  }

  async function findLoops() {
    if (!state.ready || !state.h || !steerReady()) return;
    const token = ++searchToken, gen = state.loopGen;
    showError("");
    $("anchor-note").hidden = true;
    setBusy(true, "Looking for loops… (a few seconds)");
    const body = { start: state.h, minutes: state.loop.minutes, steer: state.loop.steer };
    if (state.loop.steer === "compass") {
      body.direction = state.loop.dir;
    } else if (state.loop.steer === "anchor") {
      if (state.loop.feature) body.feature = state.loop.feature;
      else body.anchor = state.x;
    }
    try {
      const data = await api("/api/loop", body);
      if (token !== searchToken || gen !== state.loopGen || state.mode !== "loop") return;
      if (data.steer && data.steer.mode === "anchor") {
        if (data.steer.kind === "shore" && data.steer.name) {
          state.x = null;
          state.loop.feature = data.steer.name;
          state.loop.featurePoint = Array.isArray(data.steer.point) ? data.steer.point : null;
          $("q-x").value = data.steer.name;
          $("clear-x").hidden = false;
          syncChipSelection();
          syncMarkers();
          if (data.note) {
            $("anchor-note").textContent = data.note;
            $("anchor-note").hidden = false;
          }
        } else if (data.steer.kind === "feature" && Array.isArray(data.steer.point)) {
          state.loop.featurePoint = data.steer.point;
          syncMarkers();
        }
      }
      state.search = data;
      state.waypoints = {};
      state.visible = {};
      for (const r of data.routes) state.visible[r.key] = true;
      state.selected = data.routes[0].key;
      writeHash();
      renderAll(true);
      revealResults();
    } catch (e) {
      if (token !== searchToken || gen !== state.loopGen || state.mode !== "loop") return;
      showError(e, e.point);
    } finally {
      if (token === searchToken) setBusy(false);
    }
  }

  function clearResults() {
    state.search = null; state.selected = null; state.waypoints = {}; state.visible = {};
    routeLayer.clearLayers(); scenicLayer.clearLayers(); momentLayer.clearLayers(); waypointLayer.clearLayers();
    $("results").hidden = true;
    showMapKey(false);
    syncStarters();
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
    if (splitKeyRow) splitKeyRow.hidden = true;
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
    if (fit && bounds.isValid()) {
      if (state.mode === "loop" && state.loop.steer === "anchor") {
        const xp = state.x || state.loop.featurePoint;
        if (xp) bounds.extend(xp);
      }
      map.fitBounds(bounds, { padding: [30, 30] });
    }
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
    const splitAt = new Map();
    if (Array.isArray(w.data.parts)) {
      for (const pt of w.data.parts) {
        if (pt.handoff_n) splitAt.set(pt.handoff_n, pt.part);
      }
    }
    let firstSplitN = 0;
    for (const p of w.data.waypoints) {
      if (p.split && !firstSplitN) firstSplitN = p.n;
      const partEnd = splitAt.get(p.n) || (p.split ? 1 : 0);
      const cls = "wp-pin" + (p.split ? " split" : "") + (p.tier > 1 ? " weak" : "");
      const icon = L.divIcon({ className: "", html: el("div", cls, String(p.n)),
        iconSize: [22, 22], iconAnchor: [11, 11] });
      const splitNote = partEnd ? `, Part ${partEnd} ends and Part ${partEnd + 1} starts here` : "";
      const weakNote = p.tier > 1 ? ", weaker spot near a junction" : "";
      L.marker([p.lat, p.lon], { icon, zIndexOffset: p.split ? 550 : 500, title: `Waypoint ${p.n}` })
        .bindTooltip(textNode(`Waypoint ${p.n} on ${p.road}${splitNote}${weakNote}`))
        .addTo(waypointLayer);
    }
    if (splitKeyRow && firstSplitN) {
      splitKeyPin.textContent = String(firstSplitN);
      splitKeyRow.hidden = false;
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

  function selectFromKeyboard(key) {
    select(key);
    const row = $("routes").querySelector(`tr.route[data-key="${key}"]`);
    if (row) row.focus();
  }

  function renderTable() {
    const s = state.search;
    $("results").hidden = !s;
    syncStarters();
    if (!s) return;
    const isLoop = s.mode === "loop";
    $("route-head").textContent = isLoop ? "Loop" : "Route";
    $("extra-head").hidden = isLoop;
    $("caption").textContent = isLoop
      ? "Scenic min: drive time on roads scenier than a typical New York road, weighted by how much scenier they are."
      : "Scenic min: drive time on roads scenier than the fastest route’s average, weighted by how much scenier they are.";
    const tb = $("routes").querySelector("tbody");
    tb.replaceChildren();
    for (const r of s.routes) {
      const selCls = r.key === state.selected ? " selected" : "";
      const tr = el("tr", "route" + selCls);
      tr.dataset.key = r.key;
      tr.tabIndex = 0;
      tr.setAttribute("aria-selected", String(r.key === state.selected));
      const name = el("td", "name");
      const cb = el("input"); cb.type = "checkbox"; cb.checked = !!state.visible[r.key];
      cb.title = "Show on map";
      cb.addEventListener("click", (ev) => { ev.stopPropagation(); state.visible[r.key] = cb.checked; renderMap(false); });
      name.appendChild(cb);
      name.appendChild(swatch(r));
      name.appendChild(el("span", "labels", routeName(r)));
      tr.appendChild(name);
      tr.appendChild(el("td", "num", r.minutes.toFixed(0)));
      if (!isLoop) tr.appendChild(el("td", "num", extraMin(r)));
      tr.appendChild(el("td", "num", dist(r.km).split(" ")[0]));
      tr.appendChild(el("td", "num", r.scenic_minutes.toFixed(0)));
      const sub = el("tr", "sub" + selCls);
      const td = el("td");
      td.colSpan = isLoop ? 4 : 5;
      td.appendChild(sceneryStrip(r));
      td.appendChild(el("div", "roads", roadsText(r))); // OSM road names: textContent only
      sub.appendChild(td);
      for (const row of [tr, sub]) row.addEventListener("click", () => select(r.key));
      tr.addEventListener("keydown", (ev) => {
        if (ev.target !== tr) return;
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          selectFromKeyboard(r.key);
        } else if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
          ev.preventDefault();
          const idx = s.routes.findIndex((x) => x.key === r.key);
          const next = s.routes[idx + (ev.key === "ArrowDown" ? 1 : -1)];
          if (next) selectFromKeyboard(next.key);
        }
      });
      tb.appendChild(tr);
      tb.appendChild(sub);
    }
    if (isLoop) {
      let det = `Details: ${s.kept} loops compared in ${s.search_s} s.`;
      if (Array.isArray(s.snap_m) && s.snap_m[0] > SNAP_NOTE_M) {
        det += ` Start is ${dist(s.snap_m[0] / 1000, 1)} from the nearest road.`;
      }
      if (s.note) det += ` ${s.note}`;
      const sel = s.routes.find((x) => x.key === state.selected);
      if (sel && Number.isFinite(sel.stem_km) && sel.stem_km >= 1) {
        det += ` Leaves and returns on the same road for the first ${dist(sel.stem_km, 1)}.`;
      }
      $("details").textContent = det;
    } else {
      let det = `Details: ${s.kept} candidate routes compared in ${s.search_s} s.`;
      s.snap_m.forEach((m, i) => {
        if (m > SNAP_NOTE_M) det += ` ${i ? "B" : "A"} is ${dist(m / 1000, 1)} from the nearest road.`;
      });
      $("details").textContent = det;
    }
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
    const extra = s.mode === "loop" ? "—" : extraMin(r);
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
      for (const g of stopGroups(r)) {
        const ms = r.moments.slice(g.first, g.last + 1);
        if (ms.length === 1) { ol.appendChild(stopItem(ms[0])); continue; }
        // several stops in a row at one named lake or river: one line, the stops folded under it
        const li = el("li", "group");
        const det = el("details");
        const sum = el("summary", null, groupText(g.feature, ms)); // OSM text: textContent only
        sum.appendChild(el("span", "mtype mt-" + ms[0].type, (MOMENT_NAMES[ms[0].type] || ms[0].type).toLowerCase()));
        det.appendChild(sum);
        const inner = el("ol", "moments");
        for (const m of ms) inner.appendChild(stopItem(m));
        det.appendChild(inner);
        li.appendChild(det);
        ol.appendChild(li);
      }
      d.appendChild(ol);
    }
  }

  // runs of consecutive stops at the same named feature (server `stop_groups`; one per stop if absent)
  function stopGroups(r) {
    if (Array.isArray(r.stop_groups) && r.stop_groups.length) return r.stop_groups;
    return r.moments.map((m, i) => ({ first: i, last: i, feature: null }));
  }

  function groupText(feature, ms) {
    const mins = ms.map((m) => m.min_along).filter((x) => Number.isFinite(x));
    const when = mins.length ? `min ${Math.round(Math.min(...mins))}–${Math.round(Math.max(...mins))} · ` : "";
    const lake = ms.every((m) => m.type === "water") && /\b(lake|pond|reservoir|bay|harbor|sound)\b/i.test(feature);
    return `${when}${feature}${lake ? " shoreline" : ""}, ${ms.length} stops`;
  }

  function stopItem(m) {
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
    return li;
  }

  // Google Maps: one action for the selected route. The first click reduces the route to
  // waypoints and shows the result here; the link that opens Google is shown only after that.
  function mapsBlock(r) {
    const box = el("div", "gm");
    const w = state.waypoints[r.key];
    if (DEMO && (!w || w.error)) { // precomputed; nothing to retry
      box.appendChild(el("div", "note", "No Google Maps link was made for this route" + (w && w.error ? `: ${w.error}` : ".")));
      return box;
    }
    if (!w || w.error) {
      const btn = el("button", "primary", w && w.error ? "Try the Google Maps link again" : "Get a Google Maps link");
      btn.type = "button";
      btn.addEventListener("click", () => getWaypoints(r.key));
      box.appendChild(btn);
      if (w && w.error) box.appendChild(el("div", "banner error", w.error));
      else box.appendChild(el("div", "note",
        "Places waypoints so Google Maps follows this route. Long drives split into 2 links so Google Maps stays editable."));
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
    const parts = Array.isArray(x.parts) && x.parts.length > 1 ? x.parts : null;
    const pct = Math.round(x.summary.overlap * 100);
    if (parts) {
      box.appendChild(el("div", "note",
        `${x.waypoints.length} waypoints numbered on the map, split into ${parts.length} links so each stays under ` +
        `Google Maps' stop limit and remains editable. Together they follow ${pct}% of this route.`));
    } else {
      box.appendChild(el("div", "note", `${x.waypoints.length} waypoint${x.waypoints.length === 1 ? "" : "s"} ` +
        `(numbered on the map). With them, Google's route follows ${pct}% of this one.`));
    }
    if (!x.meets_target) {
      box.appendChild(el("div", "banner warn",
        `Google may not follow this route closely: the waypoints reproduce ${pct}% of it ` +
        `(target ${Math.round(x.follow_target * 100)}%). Check the route in Google Maps before driving.`));
    }
    if (x.notes.length) { // technical notes from the waypoint placer, folded away
      const det = el("details", "note");
      det.appendChild(el("summary", null, `${x.notes.length} placement note${x.notes.length === 1 ? "" : "s"}`));
      const ul = el("ul");
      for (const n of x.notes) ul.appendChild(el("li", null, n));
      det.appendChild(ul);
      box.appendChild(det);
    }
    if (parts) {
      const wrap = el("div", "gm-parts");
      for (let i = 0; i < parts.length; i++) {
        const p = parts[i];
        const prev = i > 0 ? parts[i - 1] : null;
        const item = el("div", "gm-part");
        const a = el("a", "open", `Open Part ${p.part} in Google Maps ↗`);
        a.href = p.url; a.target = "_blank"; a.rel = "noopener noreferrer";
        item.appendChild(a);
        const fromLbl = prev && prev.handoff_n ? `Waypoint ${prev.handoff_n}` : "Start";
        const toLbl = p.handoff_n
          ? `waypoint ${p.handoff_n}${p.handoff_road ? ` on ${p.handoff_road}` : ""}`
          : "finish";
        item.appendChild(el("div", "note part-sub", // OSM road name: textContent only
          `${fromLbl} to ${toLbl}, ${Math.round(p.eta_min)} min, ${dist(p.km)}`));
        wrap.appendChild(item);
      }
      box.appendChild(wrap);
      if (x.single && x.single.url) {
        const spct = Math.round(x.single.overlap * 100);
        const kind = spct >= 99 ? "Single-link version" : "Rough 1-link overview";
        const sub = el("a", "open-sub",
          `${kind}: ${x.single.waypoints} waypoint${x.single.waypoints === 1 ? "" : "s"}, ${spct}% match ↗`);
        sub.href = x.single.url; sub.target = "_blank"; sub.rel = "noopener noreferrer";
        box.appendChild(sub);
      }
    } else {
      const a = el("a", "open", "Open in Google Maps ↗");
      a.href = x.url; a.target = "_blank"; a.rel = "noopener noreferrer";
      box.appendChild(a);
    }
    return box;
  }

  async function getWaypoints(key) {
    const s = state.search;
    if (!s || DEMO) return;
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
    if (DEMO && state.mode === "loop") {
      parts.push("mode=loop", "min=" + state.loop.minutes);
      if (state.loop.steer === "compass" && state.loop.dir) {
        parts.push("steer=compass", "dir=" + encodeURIComponent(state.loop.dir));
      } else if (state.loop.steer === "anchor" && state.loop.feature) {
        parts.push("steer=anchor", "feature=" + encodeURIComponent(state.loop.feature));
      }
      const sel = state.search && state.search.routes.find((r) => r.key === state.selected);
      if (sel) parts.push("route=" + encodeURIComponent(sel.labels[0]));
    } else if (DEMO && state.preset) {
      parts.push("preset=" + encodeURIComponent(state.preset));
      const sel = state.search && state.search.routes.find((r) => r.key === state.selected);
      if (sel) parts.push("route=" + encodeURIComponent(sel.labels[0]));
    } else if (!DEMO && state.mode === "loop") {
      parts.push("mode=loop");
      if (state.h) {
        parts.push(`start=${state.h[0].toFixed(5)},${state.h[1].toFixed(5)}`);
        parts.push("min=" + state.loop.minutes);
        if (state.loop.steer === "compass") {
          parts.push("steer=compass");
          if (state.loop.dir) parts.push("dir=" + encodeURIComponent(state.loop.dir));
        } else if (state.loop.steer === "anchor") {
          parts.push("steer=anchor");
          if (state.loop.feature) parts.push("feature=" + encodeURIComponent(state.loop.feature));
          else if (state.x) parts.push(`anchor=${state.x[0].toFixed(5)},${state.x[1].toFixed(5)}`);
        }
        const sel = state.search && state.search.routes.find((r) => r.key === state.selected);
        if (sel) parts.push("route=" + encodeURIComponent(sel.labels[0]));
      }
    } else if (!DEMO && state.a && state.b) {
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
    const h = new URLSearchParams(location.hash.slice(1));
    if (h.get("mode") === "loop") {
      state.loopOpenedOnce = true;
      setMode("loop");
      const m = Number(h.get("min"));
      if (Number.isFinite(m) && m > 0) setMinutes(m, ![60, 120, 180].includes(Math.round(m)));
      const st = h.get("steer") || (h.get("dir") ? "compass" : (h.get("feature") || h.get("feat") || h.get("anchor") ? "anchor" : "any"));
      if (st === "compass") {
        setSteer("compass");
        if (h.get("dir")) setDir(h.get("dir").toUpperCase());
      } else if (st === "anchor") {
        setSteer("anchor");
        const feat = (h.get("feature") || h.get("feat") || "").trim();
        const px = COORD_RE.exec(h.get("anchor") || "");
        if (feat) selectFeature({ name: feat, point: null }, false);
        else if (px) setPoint("x", [parseFloat(px[1]), parseFloat(px[2])]);
      }
      const ps = COORD_RE.exec(h.get("start") || h.get("from") || "");
      if (ps) setPoint("h", [parseFloat(ps[1]), parseFloat(ps[2])]);
      hashDone = true;
      writeHash();
      if (!ps || !steerReady()) return;
      const want = h.get("route");
      findLoops().then(() => {
        if (!want || !state.search) return;
        const r = state.search.routes.find((x) => x.labels.includes(want));
        if (r) select(r.key);
      });
      return;
    }
    const pa = COORD_RE.exec(h.get("from") || ""), pb = COORD_RE.exec(h.get("to") || "");
    if (!pa || !pb) {
      hashDone = true;
      return;
    }
    setPoint("a", [parseFloat(pa[1]), parseFloat(pa[2])]);
    setPoint("b", [parseFloat(pb[1]), parseFloat(pb[2])]);
    hashDone = true;
    const want = h.get("route");
    findRoutes().then(() => {
      if (!want || !state.search) return;
      const r = state.search.routes.find((x) => x.labels.includes(want));
      if (r) select(r.key);
    });
  }

  // ---- demo: preset routes and Drive-around loops from data/*.json --------------

  async function demoFetch(file) { // relative path, so the site works under any folder
    const r = await fetch(file, { cache: "no-cache" });
    if (!r.ok) throw new Error(`Could not load ${file} (HTTP ${r.status}).`);
    return r.json();
  }

  function renderPresets() {
    const ul = $("preset-list");
    ul.replaceChildren();
    for (const p of state.presets.presets) {
      const li = el("li", "preset" + (p.id === state.preset ? " selected" : ""));
      const b = el("button", "preset-btn");
      b.type = "button";
      b.setAttribute("aria-pressed", String(p.id === state.preset));
      b.appendChild(el("span", "preset-name", p.name));
      const ends = [p.from_label, p.to_label].filter(Boolean).join(" → ");
      b.appendChild(el("span", "preset-sub", `${ends}${ends ? " · " : ""}${Math.round(p.t0_min)} min fastest, ${dist(p.km)}`));
      b.addEventListener("click", () => loadPreset(p.id));
      li.appendChild(b);
      ul.appendChild(li);
    }
  }

  function syncDemoLoopControls() {
    const lp = state.presets && state.presets.loop;
    if (!lp) return;
    if (!lp.budgets.includes(state.loop.minutes)) state.loop.minutes = lp.default_min;
    const byMin = (lp.by_min && lp.by_min[String(state.loop.minutes)]) || { compass: {}, features: [] };
    const compassMap = byMin.compass || {};
    const feats = byMin.features || [];

    for (const b of document.querySelectorAll("#budget .seg button")) {
      const bm = Number(b.dataset.min);
      b.hidden = !lp.budgets.includes(bm);
      b.setAttribute("aria-pressed", String(bm === state.loop.minutes));
    }

    const hasCompass = Object.keys(compassMap).length > 0;
    const hasFeats = feats.length > 0;
    $("steer-compass").disabled = !hasCompass;
    $("steer-anchor").disabled = !hasFeats;
    if ((state.loop.steer === "compass" && !hasCompass) || (state.loop.steer === "anchor" && !hasFeats)) {
      state.loop.steer = "any";
    }
    for (const b of document.querySelectorAll("#steer > .seg button")) {
      b.setAttribute("aria-pressed", String(b.dataset.steer === state.loop.steer));
    }
    $("rose").hidden = state.loop.steer !== "compass";
    $("anchor-box").hidden = state.loop.steer !== "anchor";

    if (state.loop.steer === "compass" && (!state.loop.dir || !compassMap[state.loop.dir])) {
      state.loop.dir = LOOP_DIRS.find((d) => !!compassMap[d]) || null;
    }
    for (const b of document.querySelectorAll("#rose button")) {
      b.disabled = !compassMap[b.dataset.dir];
      b.setAttribute("aria-pressed", String(b.dataset.dir === state.loop.dir));
    }

    if (state.loop.steer === "anchor" && hasFeats) {
      const cur = (state.loop.feature || "").toLowerCase();
      const match = feats.find((f) => f.name.toLowerCase() === cur || f.id === cur) || feats[0];
      state.loop.feature = match.name;
      state.loop.featurePoint = Array.isArray(match.point) ? match.point.slice() : null;
    }
    const ul = $("chips");
    ul.replaceChildren();
    for (const f of feats) {
      const li = el("li");
      const btn = el("button", "chip", f.name); // OSM feature text: textContent only
      btn.type = "button";
      btn.dataset.feature = f.name;
      btn.title = `About ${Math.round(f.round_trip_min)} min out and back`;
      btn.setAttribute("aria-pressed", String(state.loop.feature === f.name));
      btn.addEventListener("click", () => {
        state.loop.feature = f.name;
        state.loop.featurePoint = Array.isArray(f.point) ? f.point.slice() : null;
        syncChipSelection();
        loadDemoLoop();
      });
      li.appendChild(btn);
      ul.appendChild(li);
    }
  }

  function demoLoopFile() {
    const lp = state.presets && state.presets.loop;
    const byMin = lp && lp.by_min && lp.by_min[String(state.loop.minutes)];
    if (!byMin) return null;
    if (state.loop.steer === "compass") return (state.loop.dir && byMin.compass && byMin.compass[state.loop.dir]) || null;
    if (state.loop.steer === "anchor") {
      const hit = (byMin.features || []).find((f) => f.name === state.loop.feature);
      return (hit && hit.file) || null;
    }
    return byMin.any || null;
  }

  let presetToken = 0;
  async function loadDemoLoop(wantRoute) {
    const file = demoLoopFile();
    if (!file) return;
    const token = ++presetToken;
    showError("");
    $("anchor-note").hidden = true;
    setBusy(true, "Loading the loops…");
    try {
      const data = await demoFetch(file);
      if (token !== presetToken || state.mode !== "loop") return;
      state.h = data.search.start.slice();
      if (state.loop.steer === "anchor" && data.search.steer && Array.isArray(data.search.steer.point)) {
        state.loop.featurePoint = data.search.steer.point.slice();
      }
      if (state.loop.steer === "anchor" && data.search.note) {
        $("anchor-note").textContent = data.search.note;
        $("anchor-note").hidden = false;
      }
      syncMarkers();
      state.search = data.search;
      state.waypoints = {};
      for (const [k, w] of Object.entries(data.waypoints || {})) {
        state.waypoints[k] = w.error ? { error: w.error } : { data: w };
      }
      state.visible = {};
      for (const r of data.search.routes) state.visible[r.key] = true;
      const pick = (wantRoute && data.search.routes.find((r) => r.labels.includes(wantRoute))) || data.search.routes[0];
      state.selected = pick.key;
      writeHash();
      renderAll(true);
    } catch (e) {
      if (token === presetToken) showError(e);
    } finally {
      if (token === presetToken) setBusy(false);
    }
  }

  async function loadPreset(id, wantRoute) {
    const p = state.presets.presets.find((x) => x.id === id);
    if (!p) return;
    const token = ++presetToken;
    showError("");
    setBusy(true, "Loading the preset…");
    try {
      const data = await demoFetch(p.file);
      if (token !== presetToken) return;
      // only now: a failed fetch leaves the shown preset highlighted and in the link
      state.preset = id;
      renderPresets();
      setPoint("a", data.search.from, p.from_label || null);
      setPoint("b", data.search.to, p.to_label || null);
      state.search = data.search;
      state.waypoints = {};
      for (const [k, w] of Object.entries(data.waypoints || {})) state.waypoints[k] = w.error ? { error: w.error } : { data: w };
      state.visible = {};
      for (const r of data.search.routes) state.visible[r.key] = true;
      const pick = (wantRoute && data.search.routes.find((r) => r.labels.includes(wantRoute))) ||
        data.search.routes.find((r) => r.labels.includes("+30%")) || data.search.routes[data.search.routes.length - 1];
      state.selected = pick.key;
      writeHash();
      renderAll(true);
    } catch (e) {
      if (token === presetToken) showError(e);
    } finally {
      if (token === presetToken) setBusy(false);
    }
  }

  async function startDemo() {
    $("demo-note").hidden = false;
    $("demo-foot").hidden = false;
    $("presets").hidden = false;
    $("loading").hidden = true;
    try {
      state.presets = await demoFetch("data/presets.json");
    } catch (e) {
      showError(e);
      return;
    }
    const lp = state.presets.loop || null;
    if (lp) {
      $("demo-loop-name").textContent = lp.name;
      $("demo-loop-origin").hidden = false;
      state.loop.minutes = lp.default_min;
      syncDemoLoopControls();
    } else {
      $("mode-tabs").hidden = true;
    }
    renderPresets();
    const h = new URLSearchParams(location.hash.slice(1));
    if (lp && h.get("mode") === "loop") {
      const wantMin = Number(h.get("min"));
      if (lp.budgets.includes(wantMin)) state.loop.minutes = wantMin;
      const st = h.get("steer") || (h.get("dir") ? "compass" : (h.get("feature") || h.get("feat") ? "anchor" : "any"));
      state.loop.steer = ["any", "compass", "anchor"].includes(st) ? st : "any";
      if (state.loop.steer === "compass" && h.get("dir")) {
        state.loop.dir = h.get("dir").toUpperCase();
      } else if (state.loop.steer === "anchor") {
        const wantFeat = (h.get("feature") || h.get("feat") || "").trim();
        if (wantFeat) state.loop.feature = wantFeat;
      }
      setMode("loop", h.get("route"));
      return;
    }
    const want = h.get("preset");
    const first = state.presets.presets.find((p) => p.id === want) || state.presets.presets[0];
    if (first) loadPreset(first.id, want === first.id ? h.get("route") : null);
  }

  // ---- wiring ----------------------------------------------------------------

  function wireDropdownKeys(inputId, listId) {
    const inp = $(inputId), list = $(listId);
    inp.addEventListener("input", () => {
      for (const li of list.querySelectorAll(".kb-active")) li.classList.remove("kb-active");
      if (!inp.value.trim()) list.hidden = true;
    });
    inp.addEventListener("keydown", (ev) => {
      if (list.hidden) return;
      if (ev.key === "Escape") {
        ev.preventDefault();
        list.hidden = true;
        return;
      }
      const items = [...list.querySelectorAll("li:not(.none)")];
      if (!items.length) return;
      const cur = items.findIndex((li) => li.classList.contains("kb-active"));
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault();
        for (const li of items) li.classList.remove("kb-active");
        const next = cur < 0 ? (ev.key === "ArrowDown" ? 0 : items.length - 1)
          : (cur + (ev.key === "ArrowDown" ? 1 : items.length - 1)) % items.length;
        items[next].classList.add("kb-active");
        items[next].scrollIntoView({ block: "nearest" });
      } else if (ev.key === "Enter" && cur >= 0) {
        ev.preventDefault();
        items[cur].click();
      }
    });
  }

  $("map-toggle").addEventListener("click", () => {
    const on = document.body.classList.toggle("map-expanded");
    $("map-toggle").setAttribute("aria-pressed", String(on));
    $("map-toggle").textContent = on ? "Smaller map" : "Larger map";
    setTimeout(() => map.invalidateSize(), 60);
  });
  $("by-s").addEventListener("change", () => { renderLegend(); renderMap(false); writeHash(); });
  $("u-mi").addEventListener("click", () => { setUnits("mi"); if (DEMO && state.presets) renderPresets(); });
  $("u-km").addEventListener("click", () => { setUnits("km"); if (DEMO && state.presets) renderPresets(); });
  $("tab-ab").addEventListener("click", () => setMode("ab"));
  $("tab-loop").addEventListener("click", () => setMode("loop"));
  $("mode-tabs").addEventListener("keydown", (ev) => {
    if (ev.key !== "ArrowRight" && ev.key !== "ArrowLeft") return;
    ev.preventDefault();
    const next = state.mode === "ab" ? "loop" : "ab";
    setMode(next);
    $(next === "ab" ? "tab-ab" : "tab-loop").focus();
  });
  for (const b of document.querySelectorAll("#budget .seg button")) {
    b.addEventListener("click", () => setMinutes(Number(b.dataset.min), false));
  }
  for (const b of document.querySelectorAll("#steer > .seg button")) {
    b.addEventListener("click", () => setSteer(b.dataset.steer));
  }
  for (const b of document.querySelectorAll("#rose button")) {
    b.addEventListener("click", () => setDir(b.dataset.dir));
  }
  setUnits(state.units);

  if (DEMO) {
    startDemo();
  } else {
    wireSearch("a");
    wireSearch("b");
    wireSearch("h");
    wireAnchorSearch();
    wireDropdownKeys("q-a", "res-a");
    wireDropdownKeys("q-b", "res-b");
    wireDropdownKeys("q-h", "res-h");
    wireDropdownKeys("q-x", "res-x");
    document.addEventListener("click", (ev) => {
      if (ev.target.closest && ev.target.closest(".place")) return;
      for (const id of ["res-a", "res-b", "res-h", "res-x"]) $(id).hidden = true;
    });
    document.addEventListener("keydown", (ev) => {
      if (!(ev.metaKey || ev.ctrlKey) || ev.key !== "Enter") return;
      ev.preventDefault();
      if (state.mode === "ab" && !$("find").disabled) findRoutes();
      else if (state.mode === "loop" && !$("find-loop").disabled) findLoops();
    });
    for (const b of document.querySelectorAll(".sample-ab")) {
      b.addEventListener("click", () => {
        const s = SAMPLES_AB[b.dataset.sample];
        if (!s) return;
        setPoint("a", s.from, s.fromLabel);
        setPoint("b", s.to, s.toLabel);
        map.fitBounds(L.latLngBounds([s.from, s.to]), { padding: [40, 40] });
        if (state.ready) findRoutes();
        else { state.pendingSampleRun = "ab"; $("hint").textContent = "Points set. Searching as soon as the graph finishes loading…"; }
      });
    }
    for (const b of document.querySelectorAll(".sample-loop")) {
      b.addEventListener("click", () => {
        const s = SAMPLES_LOOP[b.dataset.sampleLoop];
        if (!s) return;
        setMinutes(s.minutes, false);
        setSteer("any");
        setPoint("h", s.start, s.label);
        map.setView(s.start, 10);
        if (state.ready) findLoops();
        else { state.pendingSampleRun = "loop"; $("hint-loop").textContent = "Start set. Searching as soon as the graph finishes loading…"; }
      });
    }
    $("find").addEventListener("click", findRoutes);
    $("find-loop").addEventListener("click", findLoops);
    $("clear").addEventListener("click", () => {
      removePoint("a");
      removePoint("b");
      clearResults();
      showError("");
      writeHash();
      onPointsChanged();
    });
    $("clear-loop").addEventListener("click", () => {
      removePoint("h");
      removePoint("x");
      $("chips").replaceChildren();
      clearResults();
      showError("");
      onLoopChanged(false);
      writeHash();
    });
    $("clear-x").addEventListener("click", () => {
      removePoint("x");
      onLoopChanged(false);
    });
    $("min-custom").addEventListener("change", () => {
      const raw = $("min-custom").value.trim();
      if (raw === "") {
        if (![60, 120, 180].includes(Math.round(state.loop.minutes))) {
          $("min-custom").value = String(Math.round(state.loop.minutes));
        }
        return;
      }
      const v = Number(raw);
      if (Number.isFinite(v)) setMinutes(v, true);
    });
    $("min-custom").addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter") return;
      ev.preventDefault();
      const v = Number($("min-custom").value.trim());
      if (Number.isFinite(v) && v > 0) setMinutes(v, true);
      if (!$("find-loop").disabled) findLoops();
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
    $("home-a").addEventListener("click", () => fromHome("a"));
    $("home-h").addEventListener("click", () => fromHome("h"));
    $("home-set-a").addEventListener("click", () => setHome("a"));
    $("home-set-b").addEventListener("click", () => setHome("b"));
    $("home-set-h").addEventListener("click", () => setHome("h"));
    if (new URLSearchParams(location.hash.slice(1)).get("mode") === "loop") {
      state.loopOpenedOnce = true;
      setMode("loop");
    }
    onPointsChanged();
    updateLoopHint();
    syncStarters();
    syncMapCursor();
    pollHealth();
  }
})();
