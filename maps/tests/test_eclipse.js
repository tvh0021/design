/**
 * Test suite for the [eclipse-overlay]: maps/src/eclipse.js and
 * maps/data/eclipse-2027-08-02.json.
 *
 * Verifies:
 * 1. The dataset is a FeatureCollection with a totality polygon, a central line, greatest eclipse and 1000 km markers.
 * 2. Known places fall on the right side of the totality limits (Luxor and Cadiz inside, Cairo and Seville outside).
 * 3. Greatest eclipse lands where NASA puts it (25.5N, 33.2E at ~10:07 UT, 6m23s, 13:07 Egypt time).
 * 4. Markers sit on the central line, 1000 km apart, with durations that fall off away from greatest eclipse.
 * 5. parseEclipseData reverses rings d3 would treat as "larger than a hemisphere".
 * 6. createEclipseOverlay draws only when visible, skips overlapping labels, and setVisible notifies the owner.
 * 6b. Marker dots and badges are zoom-gated: hidden at the default globe scale, shown once zoomed in.
 * 7. The HUD builds the eclipse switch only when asked, toggles it, and shows a description card (see test_hud.js).
 */

import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import { createEclipseOverlay, parseEclipseData, normalizeWinding, formatTotality } from '../src/eclipse.js';
import { createGlobeRenderer } from '../src/globe.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const DATA_PATH = resolve(__dirname, '../data/eclipse-2027-08-02.json');

let passed = 0;
let failed = 0;
function test(name, fn) {
  try {
    fn();
    console.log(`  ✓ ${name}`);
    passed++;
  } catch (err) {
    console.error(`  ✗ ${name}`);
    console.error(err);
    failed++;
  }
}

console.log('Running Eclipse Overlay Verification Suite...\n');

// Planar point-in-polygon is adequate here: no ring crosses the antimeridian or a pole.
function pointInRing(lon, lat, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if ((yi > lat) !== (yj > lat) && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

assert.ok(existsSync(DATA_PATH), 'eclipse dataset must exist (run maps/scripts/build_eclipse.py)');
const data = JSON.parse(readFileSync(DATA_PATH, 'utf-8'));
const feature = (kind) => data.features.find((f) => f.properties.kind === kind);

const markers = () => data.features.filter((f) => f.properties.kind === 'marker');

test('dataset is a FeatureCollection with totality, central line, greatest eclipse and markers', () => {
  assert.equal(data.type, 'FeatureCollection');
  assert.equal(data.properties.date, '2027-08-02');
  assert.equal(data.features.find((f) => f.properties.kind === 'partial'), undefined, 'partial region was removed');
  assert.equal(feature('total').geometry.type, 'Polygon');
  assert.equal(feature('central').geometry.type, 'LineString');
  assert.equal(feature('greatest').geometry.type, 'Point');
  assert.ok(markers().length >= 12, `expected markers every 1000 km, got ${markers().length}`);
  for (const kind of ['total']) {
    const ring = feature(kind).geometry.coordinates[0];
    assert.ok(ring.length > 100, `${kind} ring should be smooth`);
    assert.deepEqual(ring[0], ring[ring.length - 1], `${kind} ring must be closed`);
    for (const [lon, lat] of ring) {
      assert.ok(lon >= -180 && lon <= 180 && lat >= -90 && lat <= 90);
    }
  }
});

test('formatTotality renders minutes and zero-padded seconds', () => {
  assert.equal(formatTotality(383), '6m 23s');
  assert.equal(formatTotality(197), '3m 17s');
  assert.equal(formatTotality(240), '4m 00s');
  assert.equal(formatTotality(undefined), '');
});

test('path of totality covers Luxor, Cadiz and Jeddah but not Cairo, Seville or Rome', () => {
  const total = feature('total').geometry.coordinates[0];
  assert.ok(pointInRing(32.64, 25.69, total), 'Luxor is in totality');
  assert.ok(pointInRing(-6.29, 36.53, total), 'Cadiz is in totality');
  assert.ok(!pointInRing(-5.99, 37.39, total), 'Seville sits just north of the northern limit');
  assert.ok(pointInRing(39.19, 21.54, total), 'Jeddah is in totality');
  assert.ok(!pointInRing(31.24, 30.04, total), 'Cairo is outside totality');
  assert.ok(!pointInRing(12.50, 41.90, total), 'Rome is outside totality');
});

test('greatest eclipse matches NASA (25.5N 33.2E, ~10:07 UT, 6m23s)', () => {
  const g = feature('greatest');
  const [lon, lat] = g.geometry.coordinates;
  assert.ok(Math.abs(lat - 25.5) < 0.1, `lat ${lat}`);
  assert.ok(Math.abs(lon - 33.18) < 0.1, `lon ${lon}`);
  assert.ok(/^10:0[67]:/.test(g.properties.utc), g.properties.utc);
  assert.equal(g.properties.durationSeconds, 383);
  assert.equal(g.properties.localTime, '13:07');
  assert.equal(g.properties.utcOffset, 3);
  assert.equal(g.properties.pathWidthKm, 258);
});

function haversineKm(a, b) {
  const r = Math.PI / 180;
  const h = Math.sin(((b[1] - a[1]) * r) / 2) ** 2 + Math.cos(a[1] * r) * Math.cos(b[1] * r) * Math.sin(((b[0] - a[0]) * r) / 2) ** 2;
  return 2 * 6371 * Math.asin(Math.sqrt(h));
}

// Distance from a point to a lon/lat polyline, in km, using a local equirectangular frame.
function distanceToPolylineKm(pt, line) {
  const r = Math.PI / 180;
  const kx = 111.32 * Math.cos(pt[1] * r);
  const ky = 110.57;
  let best = Infinity;
  for (let i = 1; i < line.length; i++) {
    const ax = (line[i - 1][0] - pt[0]) * kx, ay = (line[i - 1][1] - pt[1]) * ky;
    const bx = (line[i][0] - pt[0]) * kx, by = (line[i][1] - pt[1]) * ky;
    const dx = bx - ax, dy = by - ay;
    const len2 = dx * dx + dy * dy || 1;
    const t = Math.max(0, Math.min(1, -(ax * dx + ay * dy) / len2));
    const d = Math.hypot(ax + t * dx, ay + t * dy);
    if (d < best) best = d;
  }
  return best;
}

test('markers lie on the central line every 1000 km with duration and local time', () => {
  const line = feature('central').geometry.coordinates;
  const greatest = feature('greatest');
  const list = markers();
  const distances = list.map((m) => m.properties.distanceKm);
  assert.deepEqual(distances, distances.slice().sort((x, y) => x - y), 'sorted by distance');
  assert.ok(distances.includes(-1000) && distances.includes(1000));
  for (const m of list) {
    const p = m.properties;
    assert.ok(Number.isInteger(p.distanceKm) && p.distanceKm % 1000 === 0 && p.distanceKm !== 0);
    assert.ok(p.durationSeconds > 150 && p.durationSeconds < 383, `duration ${p.durationSeconds}`);
    assert.match(p.localTime, /^\d{2}:\d{2}$/);
    assert.match(p.utc, /^\d{2}:\d{2}:\d{2} UT$/);
    assert.ok(Number.isInteger(p.utcOffset));
    const nearest = distanceToPolylineKm(m.geometry.coordinates, line);
    assert.ok(nearest < 5, `marker ${p.distanceKm} is ${nearest.toFixed(1)} km off the central line`);
  }
  // Consecutive markers are ~1000 km apart along the line (chord is a little shorter).
  const all = [...list.filter((m) => m.properties.distanceKm < 0), greatest, ...list.filter((m) => m.properties.distanceKm > 0)];
  for (let i = 1; i < all.length; i++) {
    const d = haversineKm(all[i - 1].geometry.coordinates, all[i].geometry.coordinates);
    assert.ok(d > 960 && d <= 1001, `spacing ${d.toFixed(0)} km between ${i - 1} and ${i}`);
  }
  // Durations fall off monotonically away from greatest eclipse.
  const west = list.filter((m) => m.properties.distanceKm < 0).map((m) => m.properties.durationSeconds);
  const east = list.filter((m) => m.properties.distanceKm > 0).map((m) => m.properties.durationSeconds);
  for (let i = 1; i < west.length; i++) assert.ok(west[i] > west[i - 1]);
  for (let i = 1; i < east.length; i++) assert.ok(east[i] < east[i - 1]);
  // Spot checks against NASA's table: 09:00 UT central duration 5m22s, 11:40 UT 4m01s.
  const m3000w = list.find((m) => m.properties.distanceKm === -3000);
  assert.match(m3000w.properties.utc, /^09:00:/);
  assert.equal(m3000w.properties.durationSeconds, 323);
  assert.equal(formatTotality(m3000w.properties.durationSeconds), '5m 23s');
});

test('parseEclipseData reverses rings d3 reports as larger than a hemisphere', () => {
  const ring = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]];
  const make = () => ({
    type: 'FeatureCollection',
    features: [
      { type: 'Feature', properties: { kind: 'total' }, geometry: { type: 'Polygon', coordinates: [ring.map((p) => p.slice())] } }
    ]
  });
  const bigD3 = { geoArea: () => 3 * Math.PI };
  const smallD3 = { geoArea: () => 0.1 };
  const flipped = parseEclipseData(make(), bigD3);
  assert.deepEqual(flipped.total.geometry.coordinates[0], ring.slice().reverse());
  const kept = parseEclipseData(make(), smallD3);
  assert.deepEqual(kept.total.geometry.coordinates[0], ring);
  assert.equal(normalizeWinding(null, bigD3), null);
  assert.throws(() => parseEclipseData({ type: 'FeatureCollection', features: [] }), /total/);
});

function recordingContext() {
  const calls = [];
  const ctx = {};
  for (const m of ['save', 'restore', 'beginPath', 'fill', 'stroke', 'setLineDash', 'arc', 'rect', 'roundRect', 'fillText', 'measureText']) {
    ctx[m] = (...args) => { calls.push(m); return m === 'measureText' ? { width: 50 } : undefined; };
  }
  return { ctx, calls };
}

test('overlay draws totality, central line and markers only while visible; overlapping labels are skipped', () => {
  const d3 = { geoArea: () => 0.1, geoDistance: () => 0 };
  const overlay = createEclipseOverlay({ data, d3, visible: true });
  assert.ok(overlay.isLoaded());
  const drawn = [];
  const path = (geo) => drawn.push(geo.properties.kind);
  const proj = Object.assign((c) => [100, 100], { rotate: () => [0, 0, 0], clipAngle: () => 90 });
  const { ctx, calls } = recordingContext();

  overlay.draw(ctx, path, proj);
  assert.deepEqual(drawn, ['total', 'central']);
  const markerCount = 1 + data.features.filter((f) => f.properties.kind === 'marker').length;
  assert.equal(calls.filter((c) => c === 'arc').length, markerCount, 'one dot per marker plus greatest eclipse');
  // Every marker projects to the same pixel here: the greatest-eclipse badge (above, two lines)
  // and one marker badge below it fit; every other label collides and is skipped.
  const labels = calls.filter((c) => c === 'fillText');
  assert.equal(labels.length, 3, 'greatest badge (two lines) plus a single badge below the dot');

  // Spread the markers out and every badge gets drawn.
  calls.length = 0;
  let k = 0;
  const spread = Object.assign(() => [100 + 400 * k++, 100], { rotate: () => [0, 0, 0], clipAngle: () => 90 });
  overlay.draw(ctx, path, spread);
  assert.equal(calls.filter((c) => c === 'fillText').length, markerCount + 1);
  assert.ok(overlay.getSummary().greatest.duration === '6m 23s');
  assert.equal(overlay.getSummary().greatest.localTime, '13:07');

  drawn.length = 0;
  calls.length = 0;
  let changes = 0;
  const hidden = createEclipseOverlay({ data, d3, visible: false, onChange: () => changes++ });
  hidden.draw(ctx, path, proj);
  assert.equal(drawn.length, 0, 'nothing drawn while hidden');
  assert.equal(changes, 1, 'onChange fires once when data is set');
  hidden.setVisible(true);
  hidden.setVisible(true);
  assert.equal(changes, 2, 'onChange fires once per actual flip');
  assert.ok(hidden.isVisible());
  hidden.draw(ctx, path, proj, { labels: false });
  assert.deepEqual(drawn, ['total', 'central']);
  assert.ok(!calls.includes('fillText'), 'label suppressed while interacting');
});

test('marker dots and badges are hidden at the default zoom and appear when zoomed in', () => {
  const d3 = { geoArea: () => 0.1, geoDistance: () => 0 };
  const overlay = createEclipseOverlay({ data, d3, visible: true });
  const markerCount = data.features.filter((f) => f.properties.kind === 'marker').length;
  // Orthographic-like mock: 1 degree of longitude at the centre spans scale * pi / 180 px.
  const makeProj = (scale) => {
    const proj = (c) => [100 + c[0] * scale * Math.PI / 180, 100 - c[1] * scale * Math.PI / 180];
    return Object.assign(proj, {
      scale: () => scale, rotate: () => [0, 0, 0], clipAngle: () => 90,
      translate: () => [400, 300], invert: () => [0, 0]
    });
  };
  const { ctx, calls } = recordingContext();

  // Default globe on a 600 px viewport: 252 px scale, about 4.4 px/deg.
  assert.equal(overlay.detailLevel(makeProj(252)), 'none');
  overlay.draw(ctx, () => {}, makeProj(252));
  assert.equal(calls.filter((c) => c === 'arc').length, 1, 'only the greatest-eclipse dot');
  assert.equal(calls.filter((c) => c === 'fillText').length, 0, 'no badges at default zoom');

  calls.length = 0;
  assert.equal(overlay.detailLevel(makeProj(500)), 'dots');
  overlay.draw(ctx, () => {}, makeProj(500));
  assert.equal(calls.filter((c) => c === 'arc').length, 1 + markerCount, 'dots once zoomed a little');
  assert.equal(calls.filter((c) => c === 'fillText').length, 0, 'still no badges');

  calls.length = 0;
  assert.equal(overlay.detailLevel(makeProj(900)), 'labels');
  overlay.draw(ctx, () => {}, makeProj(900));
  assert.equal(calls.filter((c) => c === 'arc').length, 1 + markerCount);
  assert.ok(calls.filter((c) => c === 'fillText').length >= 3, 'badges once zoomed in');

  // A projection with no scale() is treated as zoomed in (tests and unknown projections).
  assert.equal(overlay.detailLevel({}), 'labels');
});

test('overlay reports fetch failures instead of throwing', async () => {
  const overlay = createEclipseOverlay({ url: 'nope.json', fetch: () => Promise.resolve({ ok: false, status: 404 }) });
  await new Promise((r) => setTimeout(r, 0));
  assert.ok(!overlay.isLoaded());
  assert.match(String(overlay.getError()), /404/);
});

test('globe renderer calls the overlay once per frame with its geoPath', () => {
  let draws = 0;
  let seenPath = null;
  const path = (obj) => {};
  const proj = Object.assign((c) => c, {
    rotate: () => [0, 0, 0], scale: () => 300, translate: () => [400, 300], clipAngle: () => 90,
    precision: () => proj, invert: (p) => p
  });
  ['scale', 'translate', 'rotate', 'clipAngle', 'precision'].forEach((k) => {
    const orig = proj[k];
    proj[k] = (...a) => (a.length ? proj : orig());
  });
  const d3 = {
    geoOrthographic: () => proj, geoPath: () => path, geoGraticule: () => () => ({ type: 'MultiLineString', coordinates: [] }),
    geoInterpolate: () => (t) => [0, 0], geoDistance: () => 0, geoArea: () => 0.1
  };
  const { ctx } = recordingContext();
  Object.assign(ctx, { clearRect() {}, createRadialGradient: () => ({ addColorStop() {} }), moveTo() {}, lineTo() {}, closePath() {}, translate() {}, rotate() {}, scale() {} });
  const canvas = { width: 800, height: 600, style: {}, getContext: () => ctx, addEventListener() {}, removeEventListener() {} };
  const container = { clientWidth: 800, clientHeight: 600, children: [], appendChild(c) { this.children.push(c); c.parentElement = this; }, removeChild() {} };
  const globe = createGlobeRenderer(container, null, { d3, canvas, terrain: false, terrainUrl: null });
  globe.setEclipseOverlay({ draw: (c, p) => { draws++; seenPath = p; } });
  assert.ok(draws >= 1, 'setEclipseOverlay triggers a render');
  const before = draws;
  globe.render();
  assert.equal(draws, before + 1);
  assert.equal(seenPath, path);
  globe.setEclipseOverlay(null);
  globe.render();
  assert.equal(draws, before + 1, 'removed overlay is no longer drawn');
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
