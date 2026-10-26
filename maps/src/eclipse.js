/**
 * Eclipse Overlay — Total Solar Eclipse of 2027 August 2
 *
 * Self-contained map overlay drawing the path of totality, the central line,
 * the point of greatest eclipse, and markers every 1000 km along the central
 * line giving the duration of totality and the local clock time of
 * mid-totality. Data comes from maps/data/eclipse-2027-08-02.json (built by
 * maps/scripts/build_eclipse.py).
 *
 * The overlay is deliberately isolated so it can be removed in one sweep:
 *   1. delete this file, maps/data/eclipse-2027-08-02.json and maps/scripts/build_eclipse.py
 *   2. delete the "[eclipse-overlay]" blocks in app.js, globe.js, hud.js, style.css
 *   3. delete maps/tests/test_eclipse.js and its entry in package.json
 * `grep -rn "eclipse-overlay" maps/ package.json` lists every touch point.
 */

const DEFAULT_URL = 'data/eclipse-2027-08-02.json';

export const ECLIPSE_PALETTE = {
  totalFill: 'rgba(17, 24, 39, 0.55)',
  totalStroke: 'rgba(253, 224, 71, 0.95)',
  totalGlow: 'rgba(253, 224, 71, 0.35)',
  central: 'rgba(255, 251, 235, 0.85)',
  marker: '#fde047',
  markerOutline: 'rgba(8, 12, 28, 0.9)',
  labelBg: 'rgba(7, 10, 19, 0.88)',
  labelBorder: 'rgba(253, 224, 71, 0.45)',
  labelText: '#fef3c7',
  labelMuted: 'rgba(254, 243, 199, 0.7)'
};

/**
 * d3-geo fills the side of a spherical polygon that lies to the right of its
 * winding. A ring that encloses more than a hemisphere was therefore wound the
 * wrong way for its intended interior; flip it.
 */
export function normalizeWinding(feature, d3) {
  if (!feature || !feature.geometry || feature.geometry.type !== 'Polygon') return feature;
  if (!d3 || typeof d3.geoArea !== 'function') return feature;
  const area = d3.geoArea(feature);
  if (area > 2 * Math.PI) {
    feature.geometry.coordinates = feature.geometry.coordinates.map((ring) => ring.slice().reverse());
  }
  return feature;
}

/** "383" -> "6m 23s" */
export function formatTotality(seconds) {
  if (!Number.isFinite(seconds)) return '';
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${String(s).padStart(2, '0')}s`;
}

/**
 * Splits a GeoJSON FeatureCollection into the named layers the renderer draws.
 */
export function parseEclipseData(collection, d3) {
  if (!collection || !Array.isArray(collection.features)) {
    throw new Error('Eclipse data must be a GeoJSON FeatureCollection');
  }
  const byKind = {};
  const markers = [];
  for (const f of collection.features) {
    const kind = f && f.properties && f.properties.kind;
    if (kind === 'marker') markers.push(f);
    else if (kind) byKind[kind] = f;
  }
  if (!byKind.total) {
    throw new Error('Eclipse data must contain a "total" polygon');
  }
  markers.sort((a, b) => (a.properties.distanceKm || 0) - (b.properties.distanceKm || 0));
  return {
    name: collection.name || 'Solar eclipse',
    date: collection.properties && collection.properties.date,
    meta: collection.properties || {},
    total: normalizeWinding(byKind.total, d3),
    central: byKind.central || null,
    greatest: byKind.greatest || null,
    markers
  };
}

/**
 * Creates the overlay. `draw` is called by the globe renderer once per frame,
 * between the political borders and the city dots.
 *
 * @param {object} [options]
 * @param {boolean} [options.visible=true]
 * @param {object}  [options.data]   Pre-parsed GeoJSON (skips fetching)
 * @param {string}  [options.url]    Data URL (default data/eclipse-2027-08-02.json)
 * @param {Function} [options.fetch]
 * @param {object}  [options.d3]
 * @param {Function} [options.onChange]  Called after data loads or visibility flips
 */
export function createEclipseOverlay(options = {}) {
  const d3 = options.d3 || (typeof window !== 'undefined' ? window.d3 : undefined);
  const palette = { ...ECLIPSE_PALETTE, ...(options.palette || {}) };
  let visible = options.visible !== undefined ? !!options.visible : true;
  let layers = null;
  let loadError = null;
  const onChange = typeof options.onChange === 'function' ? options.onChange : () => {};

  function setData(collection) {
    layers = parseEclipseData(collection, d3);
    onChange();
  }

  if (options.data) {
    setData(options.data);
  } else {
    const url = options.url || DEFAULT_URL;
    const fetchFn = options.fetch || (typeof fetch === 'function' ? fetch : null);
    if (fetchFn) {
      Promise.resolve()
        .then(() => fetchFn(url))
        .then((r) => {
          if (!r || !r.ok) throw new Error(`HTTP error ${r && r.status} fetching ${url}`);
          return r.json();
        })
        .then(setData)
        .catch((err) => { loadError = err; });
    }
  }

  function isCoordVisible(coord, proj) {
    if (!coord) return false;
    const rot = proj.rotate();
    const center = [-rot[0], -rot[1]];
    if (d3 && typeof d3.geoDistance === 'function' && proj.clipAngle && proj.clipAngle() !== null) {
      return d3.geoDistance(coord, center) < Math.PI / 2 - 0.02;
    }
    const pt = proj(coord);
    return !!pt && isFinite(pt[0]) && isFinite(pt[1]);
  }

  /**
   * @param {CanvasRenderingContext2D} ctx
   * @param {Function} path   d3.geoPath bound to ctx and the active projection
   * @param {object} proj     the active d3 projection
   * @param {object} [opts]   { labels: boolean } — draw the marker badges
   */
  function draw(ctx, path, proj, opts = {}) {
    if (!visible || !layers || !ctx || typeof path !== 'function') return;

    ctx.save();
    ctx.lineJoin = 'round';

    // Path of totality: darkened band with a bright glowing edge.
    ctx.beginPath();
    path(layers.total);
    ctx.fillStyle = palette.totalFill;
    ctx.fill();
    ctx.strokeStyle = palette.totalGlow;
    ctx.lineWidth = 4;
    ctx.stroke();
    ctx.strokeStyle = palette.totalStroke;
    ctx.lineWidth = 1.25;
    ctx.stroke();

    // Central line.
    if (layers.central) {
      ctx.beginPath();
      path(layers.central);
      ctx.strokeStyle = palette.central;
      ctx.lineWidth = 0.9;
      ctx.setLineDash([6, 5]);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // Markers: greatest eclipse first (it always gets its label), then the
    // 1000 km markers nearest to it outward. Labels that would overlap an
    // already-drawn label are skipped; the dot is still drawn.
    const placed = [];
    const ordered = [];
    if (layers.greatest) ordered.push({ feature: layers.greatest, greatest: true });
    layers.markers
      .slice()
      .sort((a, b) => Math.abs(a.properties.distanceKm) - Math.abs(b.properties.distanceKm))
      .forEach((feature) => ordered.push({ feature, greatest: false }));

    for (const { feature, greatest } of ordered) {
      const coord = feature.geometry && feature.geometry.coordinates;
      if (!isCoordVisible(coord, proj)) continue;
      const pt = proj(coord);
      if (!pt || !isFinite(pt[0]) || !isFinite(pt[1])) continue;
      const [x, y] = pt;
      ctx.beginPath();
      ctx.arc(x, y, greatest ? 4.5 : 3, 0, Math.PI * 2);
      ctx.fillStyle = palette.marker;
      ctx.fill();
      ctx.lineWidth = greatest ? 1.5 : 1.2;
      ctx.strokeStyle = palette.markerOutline;
      ctx.stroke();
      if (opts.labels !== false) {
        const p = feature.properties || {};
        const text = `${formatTotality(p.durationSeconds)} · ${p.localTime || ''}`.trim();
        const rect = badgeRect(ctx, x, y, text, greatest ? 'above' : (p.distanceKm > 0 ? 'below' : 'above'));
        if (!placed.some((r) => overlaps(r, rect))) {
          drawBadge(ctx, rect, text, greatest ? 'Greatest eclipse' : null);
          placed.push(rect);
        }
      }
    }

    ctx.restore();
  }

  function badgeRect(ctx, x, y, text, side) {
    ctx.font = '600 10px Inter, system-ui, sans-serif';
    const padX = 6;
    const h = 18;
    const w = (ctx.measureText ? ctx.measureText(text).width : text.length * 6) + padX * 2;
    const bx = x - w / 2;
    const by = side === 'below' ? y + 8 : y - 8 - h;
    return { x: bx, y: by, w, h, cx: x, side };
  }

  function overlaps(a, b) {
    const pad = 3;
    return a.x < b.x + b.w + pad && a.x + a.w + pad > b.x && a.y < b.y + b.h + pad && a.y + a.h + pad > b.y;
  }

  function drawBadge(ctx, rect, text, title) {
    const { x, y, w, h } = rect;
    ctx.beginPath();
    if (typeof ctx.roundRect === 'function') ctx.roundRect(x, y, w, h, 4);
    else ctx.rect(x, y, w, h);
    ctx.fillStyle = palette.labelBg;
    ctx.fill();
    ctx.strokeStyle = palette.labelBorder;
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.font = '600 10px Inter, system-ui, sans-serif';
    ctx.fillStyle = palette.labelText;
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'center';
    ctx.fillText(text, x + w / 2, y + h / 2);
    if (title) {
      ctx.font = '500 9px Inter, system-ui, sans-serif';
      ctx.fillStyle = palette.labelMuted;
      ctx.fillText(title, x + w / 2, y - 7);
    }
  }

  /** Short facts for the HUD description card. */
  function getSummary() {
    if (!layers) return null;
    const g = (layers.greatest && layers.greatest.properties) || {};
    return {
      name: layers.name,
      date: layers.date,
      greatest: {
        coordinates: layers.greatest ? layers.greatest.geometry.coordinates : null,
        duration: formatTotality(g.durationSeconds),
        localTime: g.localTime,
        utc: g.utc,
        zone: g.zone,
        pathWidthKm: g.pathWidthKm
      },
      totalityBegins: layers.meta.totalityBegins,
      totalityEnds: layers.meta.totalityEnds,
      markerCount: layers.markers.length
    };
  }

  return {
    draw,
    setVisible: (v) => {
      const next = !!v;
      if (next === visible) return;
      visible = next;
      onChange();
    },
    isVisible: () => visible,
    isLoaded: () => !!layers,
    getLayers: () => layers,
    getSummary,
    getError: () => loadError,
    setData
  };
}
