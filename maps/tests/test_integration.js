/**
 * End-to-End Integration Test Runner for Geodesic Flight Visualizer
 *
 * Verifies:
 * 1. HTML structure and asset contracts in maps/index.html:
 *    - HTML5 doctype and viewport configuration
 *    - External dependencies via CDN (d3@7, topojson-client@3)
 *    - Relative paths for style.css, app.js, and datasets
 *    - Core container elements (#globe-container, #hud-container, #loading-splash)
 * 2. Mathematical geodesic calculations and telemetry across iconic routes:
 *    - Paris -> Fairbanks (Trans-polar Arctic route)
 *    - New York <-> London (Directional westerly jet-stream wind penalty)
 *    - Tokyo -> San Francisco (Antimeridian 180° crossing)
 *    - Madrid -> Wellington (Antipodal pair ~180° separation)
 * 3. End-to-end application lifecycle via initApp:
 *    - Async data loading and CityDirectory indexation
 *    - Default initial route (Los Angeles -> London)
 *    - Reactive origin/destination selection
 *    - 1-click Swap cities route reversal and wind telemetry update
 *    - Recenter globe camera trigger
 *    - Window resize handling
 *    - Clean teardown via destroy()
 */

import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import { CityDirectory } from '../src/directory.js';
import { computeGeodesic } from '../src/geodesic.js';
import { initApp } from '../src/app.js';
import { ANTIPODAL_CALLOUT_TEXT } from '../src/hud.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Test tracking
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

async function runAsyncTest(name, fn) {
  try {
    await fn();
    console.log(`  ✓ ${name}`);
    passed++;
  } catch (err) {
    console.error(`  ✗ ${name}`);
    console.error(err);
    failed++;
  }
}

// ============================================================================
// Lightweight Headless DOM Mock for E2E Integration Testing
// ============================================================================

class MockClassList {
  constructor(element) {
    this.element = element;
    this._classes = new Set();
  }
  _syncFromClassName(className = '') {
    this._classes = new Set(className.split(/\s+/).filter(Boolean));
  }
  _syncToClassName() {
    this.element._className = Array.from(this._classes).join(' ');
  }
  add(...names) {
    names.forEach((n) => this._classes.add(n));
    this._syncToClassName();
  }
  remove(...names) {
    names.forEach((n) => this._classes.delete(n));
    this._syncToClassName();
  }
  contains(name) {
    return this._classes.has(name);
  }
  toggle(name, force) {
    if (force === true) {
      this.add(name);
      return true;
    } else if (force === false) {
      this.remove(name);
      return false;
    }
    if (this._classes.has(name)) {
      this.remove(name);
      return false;
    } else {
      this.add(name);
      return true;
    }
  }
}

function matchesSelector(el, selector) {
  if (selector.startsWith('.')) {
    return el.classList.contains(selector.slice(1));
  }
  if (selector.startsWith('#')) {
    return el.id === selector.slice(1);
  }
  if (selector.startsWith('[') && selector.endsWith(']')) {
    const attrMatch = selector.slice(1, -1).match(/^([a-zA-Z0-9_-]+)(?:="([^"]*)")?$/);
    if (attrMatch) {
      const attrName = attrMatch[1];
      const attrVal = attrMatch[2];
      if (attrVal !== undefined) {
        return el.getAttribute(attrName) === attrVal;
      }
      return el.hasAttribute(attrName);
    }
  }
  return el.tagName.toLowerCase() === selector.toLowerCase();
}

class MockElement {
  constructor(tagName, doc) {
    this.tagName = tagName.toUpperCase();
    this.ownerDocument = doc;
    this._className = '';
    this.classList = new MockClassList(this);
    this.id = '';
    this.type = '';
    this.value = '';
    this.placeholder = '';
    this.htmlFor = '';
    this.style = {};
    this.attributes = new Map();
    this.children = [];
    this.parentElement = null;
    this.eventListeners = new Map();
    this._textContent = '';
    this.clientWidth = 800;
    this.clientHeight = 600;
    this.width = 800;
    this.height = 600;
  }

  get className() {
    return this._className;
  }

  set className(val) {
    this._className = val || '';
    this.classList._syncFromClassName(this._className);
  }

  get textContent() {
    if (this.children.length === 0) {
      return this._textContent;
    }
    return this.children.map((c) => c.textContent).join('');
  }

  set textContent(val) {
    this.children = [];
    this._textContent = String(val ?? '');
  }

  get firstChild() {
    return this.children[0] || null;
  }

  setAttribute(name, val) {
    this.attributes.set(name, String(val));
    if (name === 'id') this.id = String(val);
    if (name === 'class') this.className = String(val);
    if (name === 'type') this.type = String(val);
    if (name === 'placeholder') this.placeholder = String(val);
  }

  getAttribute(name) {
    return this.attributes.get(name) ?? null;
  }

  hasAttribute(name) {
    return this.attributes.has(name);
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  set innerHTML(htmlString) {
    this.children = [];
    this._textContent = '';
    if (!htmlString || typeof htmlString !== 'string') return;

    const tagRegex = /<([a-zA-Z0-9]+)([^>]*)>([\s\S]*?)<\/\1>|<([a-zA-Z0-9]+)([^>]*)\/>/g;
    let match;

    while ((match = tagRegex.exec(htmlString)) !== null) {
      const tagName = match[1] || match[4];
      const attrsStr = match[2] || match[5] || '';
      const innerContent = match[3] || '';

      const child = this.ownerDocument.createElement(tagName);

      const classMatch = attrsStr.match(/class="([^"]*)"/);
      if (classMatch) child.className = classMatch[1];

      const idMatch = attrsStr.match(/id="([^"]*)"/);
      if (idMatch) child.id = idMatch[1];

      const dataTelemetryMatch = attrsStr.match(/data-telemetry="([^"]*)"/);
      if (dataTelemetryMatch) child.setAttribute('data-telemetry', dataTelemetryMatch[1]);

      const roleMatch = attrsStr.match(/role="([^"]*)"/);
      if (roleMatch) child.setAttribute('role', roleMatch[1]);

      const titleMatch = attrsStr.match(/title="([^"]*)"/);
      if (titleMatch) child.setAttribute('title', titleMatch[1]);

      const ariaLabelMatch = attrsStr.match(/aria-label="([^"]*)"/);
      if (ariaLabelMatch) child.setAttribute('aria-label', ariaLabelMatch[1]);

      if (innerContent.trim()) {
        if (/<[a-z]/i.test(innerContent)) {
          child.innerHTML = innerContent;
        } else {
          child.textContent = innerContent.trim();
        }
      }

      this.appendChild(child);
    }
  }

  appendChild(child) {
    if (child.parentElement) {
      child.parentElement.removeChild(child);
    }
    child.parentElement = this;
    this.children.push(child);
    return child;
  }

  removeChild(child) {
    const idx = this.children.indexOf(child);
    if (idx !== -1) {
      this.children.splice(idx, 1);
      child.parentElement = null;
    }
    return child;
  }

  addEventListener(event, handler) {
    if (!this.eventListeners.has(event)) {
      this.eventListeners.set(event, []);
    }
    this.eventListeners.get(event).push(handler);
  }

  removeEventListener(event, handler) {
    if (this.eventListeners.has(event)) {
      const list = this.eventListeners.get(event).filter((h) => h !== handler);
      this.eventListeners.set(event, list);
    }
  }

  dispatchEvent(event) {
    const handlers = this.eventListeners.get(event.type) || [];
    event.target = this;
    handlers.forEach((h) => h(event));
    return !event.defaultPrevented;
  }

  focus() {
    this.dispatchEvent({ type: 'focus', target: this });
  }

  blur() {
    this.dispatchEvent({ type: 'blur', target: this });
  }

  scrollIntoView() {}

  querySelector(selector) {
    const results = this.querySelectorAll(selector);
    return results[0] || null;
  }

  querySelectorAll(selector) {
    const matches = [];
    const walk = (node) => {
      for (const child of node.children) {
        if (matchesSelector(child, selector)) {
          matches.push(child);
        }
        walk(child);
      }
    };
    walk(this);
    return matches;
  }

  getContext(type) {
    if (type !== '2d') return null;
    return {
      save: () => {},
      restore: () => {},
      beginPath: () => {},
      arc: () => {},
      moveTo: () => {},
      lineTo: () => {},
      stroke: () => {},
      fill: () => {},
      clearRect: () => {},
      fillRect: () => {},
      rect: () => {},
      roundRect: () => {},
      setTransform: () => {},
      translate: () => {},
      closePath: () => {},
      rotate: () => {},
      scale: () => {},
      scale: () => {},
      measureText: (text) => ({ width: (text || '').length * 7 }),
      fillText: () => {},
      createRadialGradient: () => ({ addColorStop: () => {} })
    };
  }
}

class MockDocument {
  constructor() {
    this.elementsById = new Map();
  }
  createElement(tagName) {
    const el = new MockElement(tagName, this);
    return el;
  }
  getElementById(id) {
    return this.elementsById.get(id) || null;
  }
  registerElement(id, el) {
    el.id = id;
    this.elementsById.set(id, el);
  }
}

// Mock D3 Projection and Path Generator
function createMockD3() {
  const createProj = () => {
    let rotation = [0, -20, 0];
    let scale = 300;
    let translate = [400, 300];
    let center = [0, 0];

    const projection = (coord) => {
      const x = translate[0] + (coord[0] + rotation[0]) * 2;
      const y = translate[1] - (coord[1] + rotation[1]) * 2;
      return [x, y];
    };

    projection.clipAngle = () => projection;
    projection.precision = () => projection;
    projection.scale = (s) => {
      if (s === undefined) return scale;
      scale = s;
      return projection;
    };
    projection.translate = (t) => {
      if (t === undefined) return translate;
      translate = t;
      return projection;
    };
    projection.rotate = (r) => {
      if (r === undefined) return rotation.slice();
      rotation = r.slice();
      return projection;
    };
    projection.center = (c) => {
      if (c === undefined) return center.slice();
      center = c.slice();
      return projection;
    };

    return projection;
  };

  const ortho = createProj();
  const robinson = createProj();

  return {
    geoOrthographic: () => ortho,
    geoRobinson: () => robinson,
    geoPath: () => () => {},
    geoGraticule10: () => ({ type: 'MultiLineString', coordinates: [] })
  };
}

const mockTopojson = {
  feature: (topology, object) => ({
    type: 'FeatureCollection',
    features: [{ type: 'Feature', geometry: { type: 'Polygon', coordinates: [] } }]
  })
};

console.log('====================================================');
console.log('Running Geodesic Visualizer End-to-End Test Suite...');
console.log('====================================================\n');

// ============================================================================
// Test Suite 1: Static HTML & Asset Contracts
// ============================================================================
console.log('[Suite 1] Verifying maps/index.html & Asset Integrity...');

test('index.html exists and is non-empty', () => {
  const htmlPath = resolve(__dirname, '../index.html');
  assert.ok(existsSync(htmlPath), 'maps/index.html must exist');
  const html = readFileSync(htmlPath, 'utf-8');
  assert.ok(html.length > 200, 'index.html must not be empty');
});

test('index.html is valid standalone HTML5 with meta viewport', () => {
  const html = readFileSync(resolve(__dirname, '../index.html'), 'utf-8');
  assert.ok(html.includes('<!DOCTYPE html>'), 'Must declare HTML5 DOCTYPE');
  assert.ok(html.includes('<html'), 'Must have <html> tag');
  assert.ok(html.includes('name="viewport"'), 'Must define viewport meta tag');
  assert.ok(html.includes('width=device-width'), 'Viewport must be responsive for mobile/iframe embedding');
});

test('index.html loads Inter with preconnects and all requested weights', () => {
  const html = readFileSync(resolve(__dirname, '../index.html'), 'utf-8');
  assert.ok(html.includes('rel="preconnect" href="https://fonts.googleapis.com"'));
  assert.ok(html.includes('rel="preconnect" href="https://fonts.gstatic.com" crossorigin'));
  assert.ok(html.includes('family=Inter:wght@400;500;600;700'));
});

test('index.html includes required external CDN scripts', () => {
  const html = readFileSync(resolve(__dirname, '../index.html'), 'utf-8');
  assert.ok(
    html.includes('https://cdn.jsdelivr.net/npm/d3@7'),
    'Must include d3@7 CDN script tag'
  );
  assert.ok(
    html.includes('https://cdn.jsdelivr.net/npm/topojson-client@3'),
    'Must include topojson-client@3 CDN script tag'
  );
});

test('index.html uses relative paths for style.css, app.js, and no absolute/localhost URLs', () => {
  const html = readFileSync(resolve(__dirname, '../index.html'), 'utf-8');

  // Relative stylesheet link
  assert.ok(
    html.includes('href="src/style.css"'),
    'Must reference src/style.css via relative path'
  );
  assert.ok(existsSync(resolve(__dirname, '../src/style.css')), 'Referenced src/style.css must exist');

  // Relative module script link
  assert.ok(
    html.includes('type="module"') && html.includes('src="src/app.js"'),
    'Must reference src/app.js as an ES module'
  );
  assert.ok(existsSync(resolve(__dirname, '../src/app.js')), 'Referenced src/app.js must exist');

  // No absolute local file system or hardcoded host URLs
  assert.ok(!html.includes('localhost'), 'Must not contain hardcoded localhost URLs');
  assert.ok(!html.includes('127.0.0.1'), 'Must not contain 127.0.0.1 URLs');
  assert.ok(!html.includes('file:///'), 'Must not contain absolute file:/// paths');
  assert.ok(!html.includes('/Users/'), 'Must not contain user workspace directory paths');
});

test('index.html provides all required container elements', () => {
  const html = readFileSync(resolve(__dirname, '../index.html'), 'utf-8');
  assert.ok(html.includes('id="globe-container"'), 'Must contain #globe-container element');
  assert.ok(html.includes('id="hud-container"'), 'Must contain #hud-container element');
  assert.ok(
    html.includes('id="loading-splash"') || html.includes('class="loading-splash"'),
    'Must contain loading splash container'
  );
});

test('data assets cities10k.json and world-110m.json exist and are valid JSON', () => {
  const citiesPath = resolve(__dirname, '../data/cities10k.json');
  const worldPath = resolve(__dirname, '../data/world-110m.json');

  assert.ok(existsSync(citiesPath), 'data/cities10k.json must exist');
  assert.ok(existsSync(worldPath), 'data/world-110m.json must exist');

  const cities = JSON.parse(readFileSync(citiesPath, 'utf-8'));
  const world = JSON.parse(readFileSync(worldPath, 'utf-8'));

  assert.equal(cities.length, 10109, 'cities10k.json must have 10,000 cities plus 109 curated locations');
  assert.equal(world.type, 'Topology', 'world-110m.json must be a Topology');
});

// ============================================================================
// Test Suite 2: Iconic Routes Geodesic Mathematics & Telemetry Contracts
// ============================================================================
console.log('\n[Suite 2] Verifying Iconic Route Geodesic Telemetry...');

const rawCities = JSON.parse(readFileSync(resolve(__dirname, '../data/cities10k.json'), 'utf-8'));
const directory = new CityDirectory();
directory.load(rawCities);

test('Iconic Route 1: Paris -> Fairbanks (Trans-polar great circle)', () => {
  const paris = directory.search('Paris')[0];
  const fairbanks = directory.search('Fairbanks')[0];

  assert.ok(paris, 'Paris must exist in directory');
  assert.ok(fairbanks, 'Fairbanks must exist in directory');

  const result = computeGeodesic(paris, fairbanks);

  // Distance: ~7,110 km (~4,418 mi)
  assert.ok(result.distanceKm >= 7050 && result.distanceKm <= 7180, `Expected ~7,110 km, got ${result.distanceKm.toFixed(1)}`);
  assert.ok(result.distanceMiles >= 4380 && result.distanceMiles <= 4460, `Expected ~4,418 mi, got ${result.distanceMiles.toFixed(1)}`);

  // Polar midpoint: crosses high latitude Arctic region (> 70° N)
  assert.ok(result.sphericalMidpoint.lat > 70, `Expected polar midpoint lat > 70° N, got ${result.sphericalMidpoint.lat}°`);

  // Flight time: approx 7.9 hours @ 900 km/h
  assert.ok(result.nominalFlightTimeHours >= 8.3 && result.nominalFlightTimeHours <= 8.7, `Expected ~8.5h, got ${result.nominalFlightTimeHours}`);

  // Not antipodal
  assert.equal(result.isAntipodal, false);

  // Path coordinates: 100 points
  assert.equal(result.pathCoordinates.length, 100);
});

test('Iconic Route 2: New York <-> London (Directional jet-stream wind penalty)', () => {
  const newYork = directory.search('New York')[0];
  const london = directory.search('London')[0];

  assert.ok(newYork, 'New York must exist in directory');
  assert.ok(london, 'London must exist in directory');

  // Eastbound: New York -> London
  const eastResult = computeGeodesic(newYork, london);
  // Westbound: London -> New York
  const westResult = computeGeodesic(london, newYork);

  // Distance symmetry (Haversine distance is symmetric)
  assert.ok(Math.abs(eastResult.distanceKm - westResult.distanceKm) < 1e-4, 'Distances must be equal');
  assert.ok(eastResult.distanceKm >= 5500 && eastResult.distanceKm <= 5650, `Expected ~5,570 km, got ${eastResult.distanceKm}`);

  // Eastbound benefits from westerly jet-stream tailwind assist
  assert.ok(eastResult.prevailingWindAdjustmentHours < 0, 'Eastbound should have tailwind assist (< 0)');
  assert.ok(eastResult.effectiveFlightTimeHours < eastResult.nominalFlightTimeHours);

  // Westbound experiences westerly jet-stream headwind penalty
  assert.ok(
    westResult.prevailingWindAdjustmentHours > 0.40 && westResult.prevailingWindAdjustmentHours < 0.85,
    `Westbound penalty should be ~0.66h (40 min), got ${westResult.prevailingWindAdjustmentHours.toFixed(2)}h`
  );
  assert.ok(
    westResult.effectiveFlightTimeHours > eastResult.effectiveFlightTimeHours,
    'Westbound flight time must exceed eastbound flight time due to jet stream'
  );
});

test('Iconic Route 3: Tokyo -> San Francisco (Antimeridian 180° crossing)', () => {
  const tokyo = directory.search('Tokyo')[0];
  const sf = directory.search('San Francisco')[0];

  assert.ok(tokyo, 'Tokyo must exist in directory');
  assert.ok(sf, 'San Francisco must exist in directory');

  const result = computeGeodesic(tokyo, sf);

  // Great-circle minor arc distance: approx 8,280 km (NOT > 30,000 km wraparound!)
  assert.ok(result.distanceKm >= 8200 && result.distanceKm <= 8400, `Expected ~8,280 km, got ${result.distanceKm.toFixed(1)}`);
  assert.ok(result.distanceMiles >= 5100 && result.distanceMiles <= 5250);

  // Midpoint latitude in North Pacific Aleutian arc (~45° - 55° N)
  assert.ok(result.sphericalMidpoint.lat >= 45 && result.sphericalMidpoint.lat <= 55, `Expected midpoint lat in [45, 55]° N, got ${result.sphericalMidpoint.lat}°`);

  // Path coordinates properly formed
  assert.equal(result.pathCoordinates.length, 100);
  for (const [lng, lat] of result.pathCoordinates) {
    assert.ok(lng >= -180 && lng <= 180, `Longitude ${lng} must be normalized to [-180, 180]`);
    assert.ok(lat >= -90 && lat <= 90, `Latitude ${lat} must be in [-90, 90]`);
  }
});

test('Iconic Route 4: Madrid -> Wellington (Antipodal pair edge case)', () => {
  const madrid = directory.search('Madrid')[0];
  const wellington = directory.search('Wellington')[0];

  assert.ok(madrid, 'Madrid must exist in directory');
  assert.ok(wellington, 'Wellington must exist in directory');

  const result = computeGeodesic(madrid, wellington);

  // Must detect antipodal singularity
  assert.equal(result.isAntipodal, true, 'Madrid and Wellington must be detected as antipodal');

  // Fallback polar routing midpoint at North Pole
  assert.equal(result.sphericalMidpoint.lat, 90, 'Midpoint must route to North Pole (lat 90)');
  assert.equal(result.sphericalMidpoint.lng, 0);

  // Bearings heading North from Madrid and arriving from North into Wellington
  assert.equal(result.initialBearingDeg, 0);
  assert.equal(result.finalBearingDeg, 180);

  // Distance ~19,850 km
  assert.ok(result.distanceKm >= 19700 && result.distanceKm <= 20000, `Expected ~19,850 km, got ${result.distanceKm.toFixed(1)}`);
});

// ============================================================================
// Test Suite 3: Full End-to-End Application Lifecycle (initApp)
// ============================================================================
console.log('\n[Suite 3] Verifying initApp Application Lifecycle & Reactive Events...');

const worldTopology = JSON.parse(readFileSync(resolve(__dirname, '../data/world-110m.json'), 'utf-8'));

function createMockAppEnvironment() {
  const doc = new MockDocument();

  const globeContainer = doc.createElement('div');
  globeContainer.id = 'globe-container';
  globeContainer.className = 'globe-viewport';
  doc.registerElement('globe-container', globeContainer);

  const hudContainer = doc.createElement('div');
  hudContainer.id = 'hud-container';
  doc.registerElement('hud-container', hudContainer);

  const loadingSplash = doc.createElement('div');
  loadingSplash.id = 'loading-splash';
  loadingSplash.className = 'loading-splash';

  const splashTitle = doc.createElement('div');
  splashTitle.className = 'loading-title';
  const splashSubtitle = doc.createElement('div');
  splashSubtitle.className = 'loading-subtitle';
  loadingSplash.appendChild(splashTitle);
  loadingSplash.appendChild(splashSubtitle);
  doc.registerElement('loading-splash', loadingSplash);

  const mockWindowListeners = new Map();
  const mockWindow = {
    devicePixelRatio: 2,
    innerWidth: 1024,
    innerHeight: 768,
    addEventListener: (evt, handler) => {
      if (!mockWindowListeners.has(evt)) mockWindowListeners.set(evt, []);
      mockWindowListeners.get(evt).push(handler);
    },
    removeEventListener: (evt, handler) => {
      if (mockWindowListeners.has(evt)) {
        mockWindowListeners.set(evt, mockWindowListeners.get(evt).filter((h) => h !== handler));
      }
    },
    _dispatch: (evt, data) => {
      const handlers = mockWindowListeners.get(evt) || [];
      handlers.forEach((h) => h(data));
    }
  };

  return { doc, globeContainer, hudContainer, loadingSplash, mockWindow };
}

await runAsyncTest('initApp initializes application, dismisses splash, and renders initial Los Angeles -> London route', async () => {
  const { doc, globeContainer, hudContainer, loadingSplash, mockWindow } = createMockAppEnvironment();
  const mockD3 = createMockD3();

  const app = await initApp({
    globeContainer,
    hudContainer,
    loadingSplash,
    document: doc,
    citiesData: rawCities,
    worldTopology,
    d3: mockD3,
    topojson: mockTopojson
  });

  assert.ok(app, 'initApp should return app controller object');
  assert.ok(app.directory instanceof CityDirectory, 'Directory should be initialized');
  assert.ok(app.globe, 'Globe renderer should be initialized');
  assert.ok(app.hud, 'FlightHUD should be initialized');

  // Verify default initial iconic route is Los Angeles -> London
  const origin = app.getOrigin();
  const dest = app.getDest();
  assert.equal(origin?.name, 'Los Angeles');
  assert.equal(dest?.name, 'London');

  // Verify geodesic calculation
  const geodesic = app.getGeodesic();
  assert.ok(geodesic, 'Geodesic calculation must be active');
  assert.ok(geodesic.distanceKm > 8700 && geodesic.distanceKm < 8800, `Los Angeles->London distance ~8,760 km, got ${geodesic.distanceKm}`);

  // Verify loading splash dismissal
  assert.ok(loadingSplash.classList.contains('hidden'), 'Loading splash should have .hidden class applied');

  // Verify HUD telemetry card values
  const hudElements = app.hud.getElements();
  assert.equal(hudElements.distanceKm.textContent, `${Math.round(geodesic.distanceKm).toLocaleString('en-US')} km`);
  assert.equal(hudElements.distanceMiles.textContent, `${Math.round(geodesic.distanceMiles).toLocaleString('en-US')} mi`);
  assert.ok(hudElements.durationEffective.textContent.includes('h '), 'Effective duration must be displayed');
  assert.ok(hudElements.originCoord.textContent.includes('34.11° N'), 'Origin coordinates must be displayed');
  assert.ok(hudElements.destCoord.textContent.includes('51.51° N'), 'Destination coordinates must be displayed');

  assert.equal(origin.lng, -118.4068);
  assert.equal(dest.country, 'United Kingdom');
  assert.equal(dest.lng, -0.1275);
  app.hud.originCombobox.clearBtn.dispatchEvent({ type: 'click' });
  assert.equal(app.getGeodesic(), geodesic, 'Clearing search text must preserve telemetry and route');
  assert.equal(app.getOrigin(), origin);
  app.hud.originCombobox.input.blur();
  assert.equal(hudElements.originInput.value, 'Los Angeles, United States');

  // Verify swap button reverse action
  const { swapBtn } = hudElements;
  assert.ok(swapBtn, 'Swap button must exist');
  swapBtn.dispatchEvent({ type: 'click' });

  // Route is now London -> Los Angeles
  assert.equal(app.getOrigin()?.name, 'London');
  assert.equal(app.getDest()?.name, 'Los Angeles');

  // Telemetry updates when the route is reversed
  assert.ok(app.getGeodesic().prevailingWindAdjustmentHours > 0, 'Reversed westbound route must carry a headwind penalty');
  assert.ok(hudElements.windBadge.textContent.includes('Headwind'), 'HUD should display headwind badge');

  // Verify Recenter button action
  let recentered = false;
  const originalRecenter = app.globe.recenter;
  app.globe.recenter = () => {
    recentered = true;
    originalRecenter();
  };

  const { recenterBtn } = hudElements;
  recenterBtn.dispatchEvent({ type: 'click' });
  assert.ok(recentered, 'Recenter button must trigger globe.recenter()');

  // Verify Projection Toggle action
  assert.equal(app.getProjection(), 'orthographic');
  app.setProjection('robinson');
  assert.equal(app.getProjection(), 'robinson');
  app.setProjection('orthographic');
  assert.equal(app.getProjection(), 'orthographic');

  // Verify Window Resize reactive listener
  let resized = false;
  const originalResize = app.globe.resize;
  app.globe.resize = (w, h) => {
    resized = true;
    originalResize(w, h);
  };

  app.resize(1024, 768);
  assert.ok(resized, 'Resize must trigger globe.resize()');

  // Verify Antipodal Route warning badge presentation
  const madrid = app.directory.search('Madrid')[0];
  const wellington = app.directory.search('Wellington')[0];
  app.updateRoute(madrid, wellington, false);

  assert.equal(app.getGeodesic().isAntipodal, true);
  assert.equal(hudElements.antipodalBadge.style.display, 'block');
  assert.ok(hudElements.antipodalBadge.textContent.includes(ANTIPODAL_CALLOUT_TEXT));

  // Verify clean destruction
  app.destroy();
  assert.equal(globeContainer.children.length, 0, 'Globe canvas should be cleaned up on destroy');
  assert.equal(hudContainer.children.length, 0, 'HUD aside should be cleaned up on destroy');
});

await runAsyncTest('Default fallbacks match dataset coordinates and mobile insets frame both projections', async () => {
  const { doc, globeContainer, hudContainer, mockWindow } = createMockAppEnvironment();
  const previousWindow = globalThis.window;
  mockWindow.innerWidth = 390;
  mockWindow.innerHeight = 844;
  globalThis.window = mockWindow;
  let app;
  try {
    app = await initApp({
      document: doc, globeContainer, hudContainer, citiesData: [], worldTopology,
      d3: createMockD3(), topojson: mockTopojson,
      globeOptions: { width: 390, height: 844, terrain: false, fixedSize: true }
    });
    for (const city of [app.getOrigin(), app.getDest()]) {
      const tuple = rawCities.find(row => row[0] === city.name && row[1] === city.country);
      assert.deepEqual([city.name, city.country, city.lat, city.lng], tuple);
    }
    app.hud.hudElement.offsetHeight = 400;
    app.updateRoute(app.getOrigin(), app.getDest(), false);
    assert.deepEqual(app.globe.getProjection().translate(), [195, 256]);
    app.setProjection('robinson');
    assert.deepEqual(app.globe.getProjection().translate(), [195, 256]);
    app.hud.collapse();
    assert.deepEqual(app.globe.getProjection().translate(), [195, 462]);
    app.hud.expand();
    assert.deepEqual(app.globe.getProjection().translate(), [195, 256]);
  } finally {
    if (app) app.destroy();
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});

// Run summary
setTimeout(() => {
  console.log(`\n====================================================`);
  console.log(`Integration Test Suite: ${passed} passed, ${failed} failed.`);
  console.log(`====================================================`);
  if (failed > 0) {
    process.exit(1);
  }
}, 50);
