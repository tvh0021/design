/**
 * Test suite for maps/src/hud.js and maps/src/style.css
 *
 * Verifies:
 * 1. Helper formatting functions: formatCoordinate, formatDistance, formatDuration, formatWindPenalty.
 * 2. HUD Component instantiation and DOM element hierarchy.
 * 3. Combobox search, autocomplete rendering, and keyboard navigation (ArrowDown, ArrowUp, Enter, Escape).
 * 4. 1-click Swap Cities button functionality and visual feedback.
 * 5. Recenter Globe button callback.
 * 6. Telemetry HUD card updates (distance, duration, wind delay, coordinates).
 * 7. Antipodal educational warning badge toggling and text verification.
 * 8. Programmatic API: setOrigin, setDest, getOrigin, getDest, destroy.
 */

import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  formatCoordinate,
  formatDistance,
  formatDuration,
  formatWindPenalty,
  FlightHUD,
  createFlightHUD,
  ANTIPODAL_CALLOUT_TEXT
} from '../src/hud.js';

import { CityDirectory } from '../src/directory.js';
import { computeGeodesic } from '../src/geodesic.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Lightweight DOM Mock for Node.js environment
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
  }

  get className() {
    return this._className;
  }

  set className(val) {
    this._className = val || '';
    this.classList._syncFromClassName(this._className);
  }

  get firstChild() {
    return this.children[0] || null;
  }

  get textContent() {
    if (this.children.length === 0) {
      return this._textContent;
    }
    return this.children.map((c) => c.textContent).join('');
  }

  set textContent(val) {
    this.children = [];
    this._textContent = String(val);
  }

  get innerHTML() {
    return this.textContent;
  }

  set innerHTML(htmlString) {
    this.children = [];
    this._textContent = '';
    if (!htmlString || typeof htmlString !== 'string') return;

    // Fast mock parser for innerHTML structure
    const tagRegex = /<([a-zA-Z0-9]+)([^>]*)>([\s\S]*?)<\/\1>|<([a-zA-Z0-9]+)([^>]*)\/?>/g;
    let match;

    while ((match = tagRegex.exec(htmlString)) !== null) {
      const tagName = match[1] || match[4];
      const attrsStr = match[2] || match[5] || '';
      const innerContent = match[3] || '';

      const child = this.ownerDocument.createElement(tagName);

      // Parse common attributes
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

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name === 'class') this.className = value;
    if (name === 'id') this.id = value;
  }

  getAttribute(name) {
    if (name === 'class') return this.className;
    if (name === 'id') return this.id;
    return this.attributes.get(name) ?? null;
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  hasAttribute(name) {
    return this.attributes.has(name);
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

class MockDocument {
  createElement(tagName) {
    return new MockElement(tagName, this);
  }
}

function createTestEnvironment() {
  const doc = new MockDocument();
  const container = doc.createElement('div');
  container.className = 'test-container';
  return { doc, container };
}

// Test runner tracking
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

console.log('Running HUD & Frontend Styling Test Suite...\n');

// 1. Verify CSS Asset File
test('style.css exists, contains cosmic theme tokens and responsive rules', () => {
  const cssPath = resolve(__dirname, '../src/style.css');
  assert.ok(existsSync(cssPath), 'maps/src/style.css must exist');

  const css = readFileSync(cssPath, 'utf-8');
  assert.ok(css.includes('#070a13'), 'Must contain obsidian deep space background #070a13');
  assert.ok(css.includes('#06b6d4'), 'Must contain cyan accent #06b6d4');
  assert.ok(css.includes('#f59e0b'), 'Must contain amber origin accent #f59e0b');
  assert.ok(css.includes('#10b981'), 'Must contain emerald destination accent #10b981');
  assert.ok(css.includes('backdrop-filter: var(--blur-glass)'), 'Must contain glassmorphism backdrop-filter');
  assert.ok(css.includes('@media (max-width: 639px)'), 'Must contain mobile responsive breakpoint');
  assert.ok(css.includes('tabular-nums'), 'Must utilize tabular numerals for metrics');
});

// 2. Unit Tests for Formatting Functions
test('formatCoordinate formats lat/lng with hemisphere symbols', () => {
  assert.equal(formatCoordinate(48.8566, 2.3522), '48.86° N, 2.35° E');
  assert.equal(formatCoordinate(-33.8688, 151.2093), '33.87° S, 151.21° E');
  assert.equal(formatCoordinate(37.7749, -122.4194), '37.77° N, 122.42° W');
  assert.equal(formatCoordinate(-41.2865, -174.7762), '41.29° S, 174.78° W');
  assert.equal(formatCoordinate(NaN, 0), '—');
  assert.equal(formatCoordinate(undefined, null), '—');
});

test('formatDistance produces formatted km and miles with thousands separators', () => {
  const d1 = formatDistance(5570.4, 3461.2);
  assert.equal(d1.km, '5,570 km');
  assert.equal(d1.miles, '3,461 mi');

  const d2 = formatDistance(19825.8);
  assert.equal(d2.km, '19,826 km');
  assert.ok(d2.miles.endsWith('mi'));

  const dZero = formatDistance(0);
  assert.equal(dZero.km, '0 km');
  assert.equal(dZero.miles, '0 mi');
});

test('formatDuration converts decimal hours to "Xh Ym"', () => {
  assert.equal(formatDuration(8.5), '8h 30m');
  assert.equal(formatDuration(1.05), '1h 03m');
  assert.equal(formatDuration(0), '0h 00m');
  assert.equal(formatDuration(-2), '0h 00m');
  assert.equal(formatDuration(12.99), '12h 59m');
  assert.equal(formatDuration(13.0), '13h 00m');
});

test('formatWindPenalty details prevailing jet stream effects', () => {
  // Westbound flight with penalty
  const headwind = formatWindPenalty(0.85, 7.5);
  assert.equal(headwind.type, 'headwind');
  assert.ok(headwind.penaltyMinutes > 0);
  assert.ok(headwind.text.includes('headwind delay'));
  assert.ok(headwind.text.includes('westerly jet stream'));

  // Eastbound flight with no penalty
  const tailwind = formatWindPenalty(0, 6.2);
  assert.equal(tailwind.type, 'tailwind');
  assert.equal(tailwind.penaltyMinutes, 0);
  assert.ok(tailwind.text.includes('tailwind') || tailwind.text.includes('optimal'));

  // Degenerate/empty
  const empty = formatWindPenalty(0, 0);
  assert.equal(empty.type, 'neutral');
});

// 3. HUD Component Initialization & DOM Element Generation
test('FlightHUD builds complete accessible DOM structure', () => {
  const { doc, container } = createTestEnvironment();
  const hud = createFlightHUD(container, { document: doc });

  const elements = hud.getElements();
  assert.ok(elements.hud, 'HUD root element must be created');
  assert.ok(elements.originInput, 'Origin input must be created');
  assert.ok(elements.originDropdown, 'Origin dropdown listbox must be created');
  assert.ok(elements.destInput, 'Destination input must be created');
  assert.ok(elements.destDropdown, 'Destination dropdown listbox must be created');
  assert.ok(elements.swapBtn, '1-click swap button must be created');
  assert.ok(elements.recenterBtn, 'Recenter button must be created');
  assert.ok(elements.antipodalBadge, 'Antipodal warning badge must be created');
  assert.ok(elements.telemetryCard, 'Telemetry card must be created');

  assert.equal(elements.hud.parentElement, container);
  assert.equal(elements.antipodalBadge.style.display, 'none');

  hud.destroy();
});

// 4. Initial Values & Pre-population
test('FlightHUD initializes with initialOrigin and initialDest', () => {
  const { doc, container } = createTestEnvironment();
  const initialOrigin = { id: 1, name: 'Paris', country: 'France', lat: 48.8566, lng: 2.3522 };
  const initialDest = ['Tokyo', 'Japan', 35.6895, 139.6917];

  const hud = createFlightHUD(container, {
    document: doc,
    initialOrigin,
    initialDest
  });

  const elements = hud.getElements();
  assert.equal(elements.originInput.value, 'Paris, France');
  assert.equal(elements.destInput.value, 'Tokyo, Japan');
  assert.equal(hud.getOrigin().name, 'Paris');
  assert.equal(hud.getDest().name, 'Tokyo');

  hud.destroy();
});

// 5. Combobox Search & Autocomplete
await runAsyncTest('Combobox search queries directory and renders dropdown options', async () => {
  const { doc, container } = createTestEnvironment();
  const directory = new CityDirectory();
  directory.load([
    ['Tokyo', 'Japan', 35.6895, 139.6917],
    ['Yokohama', 'Japan', 35.4437, 139.6380],
    ['Paris', 'France', 48.8566, 2.3522],
    ['Papeete', 'French Polynesia', -17.5350, -149.5696]
  ]);

  let selectedOrigin = null;
  const hud = createFlightHUD(container, {
    document: doc,
    directory,
    onOriginSelect: (c) => { selectedOrigin = c; }
  });

  const { originInput, originDropdown } = hud.getElements();

  // Simulate typing 'Par'
  originInput.value = 'Par';
  originInput.dispatchEvent({ type: 'input' });

  // Wait for debounce timeout (50ms)
  await new Promise((r) => setTimeout(r, 80));

  assert.equal(originDropdown.style.display, 'block', 'Dropdown should open on search results');
  assert.ok(originDropdown.children.length >= 1, 'Dropdown should contain suggestion items');

  const firstOption = originDropdown.children[0];
  assert.ok(firstOption.textContent.includes('Paris'), 'First suggestion should be Paris');

  // Click on first option
  firstOption.dispatchEvent({ type: 'click' });

  assert.equal(originInput.value, 'Paris, France');
  assert.equal(originDropdown.style.display, 'none', 'Dropdown should close on selection');
  assert.equal(selectedOrigin?.name, 'Paris');

  hud.destroy();
});

// 6. Keyboard Navigation in Combobox
await runAsyncTest('Combobox handles ArrowDown, ArrowUp, Enter, and Escape', async () => {
  const { doc, container } = createTestEnvironment();
  const directory = new CityDirectory();
  directory.load([
    ['Madrid', 'Spain', 40.4168, -3.7038],
    ['Manila', 'Philippines', 14.5995, 120.9842],
    ['Melbourne', 'Australia', -37.8136, 144.9631]
  ]);

  let selectedCity = null;
  const hud = createFlightHUD(container, {
    document: doc,
    directory,
    onDestSelect: (c) => { selectedCity = c; }
  });

  const { destInput, destDropdown } = hud.getElements();

  destInput.value = 'M';
  destInput.dispatchEvent({ type: 'input' });
  await new Promise((r) => setTimeout(r, 80));

  assert.equal(destDropdown.style.display, 'block');
  assert.equal(destDropdown.children.length, 3);

  // ArrowDown to highlight Madrid (index 0)
  destInput.dispatchEvent({ type: 'keydown', key: 'ArrowDown', preventDefault: () => {} });
  assert.ok(destDropdown.children[0].classList.contains('active'));

  // ArrowDown to highlight Manila (index 1)
  destInput.dispatchEvent({ type: 'keydown', key: 'ArrowDown', preventDefault: () => {} });
  assert.ok(destDropdown.children[1].classList.contains('active'));
  assert.ok(!destDropdown.children[0].classList.contains('active'));

  // ArrowUp back to Madrid (index 0)
  destInput.dispatchEvent({ type: 'keydown', key: 'ArrowUp', preventDefault: () => {} });
  assert.ok(destDropdown.children[0].classList.contains('active'));

  // Enter to select Madrid
  destInput.dispatchEvent({ type: 'keydown', key: 'Enter', preventDefault: () => {} });
  assert.equal(destInput.value, 'Madrid, Spain');
  assert.equal(destDropdown.style.display, 'none');
  assert.equal(selectedCity?.name, 'Madrid');

  // Re-trigger dropdown and test Escape key
  destInput.value = 'Mel';
  destInput.dispatchEvent({ type: 'input' });
  await new Promise((r) => setTimeout(r, 80));
  assert.equal(destDropdown.style.display, 'block');

  destInput.dispatchEvent({ type: 'keydown', key: 'Escape', preventDefault: () => {} });
  assert.equal(destDropdown.style.display, 'none', 'Escape should close dropdown');

  hud.destroy();
});

// 7. 1-Click Swap Cities Button
test('Swap button reverses origin and destination and triggers callback', () => {
  const { doc, container } = createTestEnvironment();
  let swappedOrigin = null;
  let swappedDest = null;

  const hud = createFlightHUD(container, {
    document: doc,
    initialOrigin: { name: 'London', country: 'United Kingdom', lat: 51.5074, lng: -0.1278 },
    initialDest: { name: 'New York', country: 'United States', lat: 40.7128, lng: -74.0060 },
    onSwap: (orig, dest) => {
      swappedOrigin = orig;
      swappedDest = dest;
    }
  });

  const { swapBtn, originInput, destInput } = hud.getElements();

  assert.equal(originInput.value, 'London, United Kingdom');
  assert.equal(destInput.value, 'New York, United States');

  // Trigger swap
  swapBtn.dispatchEvent({ type: 'click' });

  assert.equal(originInput.value, 'New York, United States');
  assert.equal(destInput.value, 'London, United Kingdom');
  assert.equal(hud.getOrigin().name, 'New York');
  assert.equal(hud.getDest().name, 'London');
  assert.equal(swappedOrigin?.name, 'New York');
  assert.equal(swappedDest?.name, 'London');
  assert.ok(swapBtn.classList.contains('swapping'), 'Swap button should have animated class');

  hud.destroy();
});

// 8. Recenter Globe Button
test('Recenter button calls onRecenter callback', () => {
  const { doc, container } = createTestEnvironment();
  let recenterCalled = false;

  const hud = createFlightHUD(container, {
    document: doc,
    onRecenter: () => {
      recenterCalled = true;
    }
  });

  const { recenterBtn } = hud.getElements();
  recenterBtn.dispatchEvent({ type: 'click' });

  assert.ok(recenterCalled, 'Recenter callback must be fired on button click');
  hud.destroy();
});

// 9. Telemetry HUD Card Updating
test('updateTelemetry renders distance, flight times, wind effect, and coordinates', () => {
  const { doc, container } = createTestEnvironment();
  const origin = { name: 'London', country: 'United Kingdom', lat: 51.5074, lng: -0.1278 };
  const dest = { name: 'New York', country: 'United States', lat: 40.7128, lng: -74.0060 };

  const geodesic = computeGeodesic(origin, dest);
  const hud = createFlightHUD(container, {
    document: doc,
    initialOrigin: origin,
    initialDest: dest
  });

  hud.updateTelemetry(geodesic, origin, dest);
  const el = hud.getElements();

  // Distance verification
  assert.ok(el.distanceKm.textContent.includes('5,570 km') || el.distanceKm.textContent.includes('5,571 km'));
  assert.ok(el.distanceMiles.textContent.includes('3,461 mi') || el.distanceMiles.textContent.includes('3,462 mi'));

  // Flight duration
  assert.ok(el.durationEffective.textContent.includes('h '));
  assert.ok(el.durationNominal.textContent.includes('Nominal:'));

  // Jet stream westbound headwind penalty
  assert.ok(el.windBadge.textContent.includes('Headwind'));
  assert.ok(el.windDesc.textContent.includes('westerly jet stream'));

  // Coordinates
  assert.ok(el.originCoord.textContent.includes('51.51° N'));
  assert.ok(el.destCoord.textContent.includes('40.71° N'));
  assert.ok(el.midpointCoord.textContent.includes('N'));

  // Not antipodal
  assert.equal(el.antipodalBadge.style.display, 'none');

  hud.destroy();
});

// 10. Antipodal Educational Warning Badge
test('updateTelemetry displays educational warning badge for antipodal pair', () => {
  const { doc, container } = createTestEnvironment();
  const madrid = { name: 'Madrid', country: 'Spain', lat: 40.4168, lng: -3.7038 };
  const wellington = { name: 'Wellington', country: 'New Zealand', lat: -41.2865, lng: 174.7762 };

  const antipodalGeodesic = computeGeodesic(madrid, wellington);
  assert.ok(antipodalGeodesic.isAntipodal, 'Madrid/Wellington must be detected as antipodal');

  const hud = createFlightHUD(container, { document: doc });
  hud.updateTelemetry(antipodalGeodesic, madrid, wellington);

  const { antipodalBadge } = hud.getElements();
  assert.equal(antipodalBadge.style.display, 'block', 'Antipodal badge must be visible');
  assert.ok(antipodalBadge.textContent.includes(ANTIPODAL_CALLOUT_TEXT), 'Must contain exact required educational callout text');

  // Clearing route hides badge
  hud.updateTelemetry(null);
  assert.equal(antipodalBadge.style.display, 'none', 'Antipodal badge must hide when route is cleared');

  hud.destroy();
});

// 11. Programmatic API: setOrigin, setDest, destroy
test('Programmatic setters update values and clean up on destroy', () => {
  const { doc, container } = createTestEnvironment();
  const hud = createFlightHUD(container, { document: doc });

  hud.setOrigin({ name: 'Berlin', country: 'Germany', lat: 52.52, lng: 13.405 });
  hud.setDest({ name: 'Rome', country: 'Italy', lat: 41.9028, lng: 12.4964 });

  assert.equal(hud.getOrigin().name, 'Berlin');
  assert.equal(hud.getDest().name, 'Rome');
  assert.equal(hud.getElements().originInput.value, 'Berlin, Germany');
  assert.equal(hud.getElements().destInput.value, 'Rome, Italy');

  hud.destroy();
  assert.equal(container.children.length, 0, 'Container should be empty after destroy()');
});


// 12. Collapsible Drawer & Minimize State
test('FlightHUD supports collapse, expand, toggleCollapse, and onCollapseChange', () => {
  const { doc, container } = createTestEnvironment();
  let collapseState = null;

  const hud = createFlightHUD(container, {
    document: doc,
    onCollapseChange: (isCollapsed) => {
      collapseState = isCollapsed;
    }
  });

  const { hud: hudEl, floatingTrigger, toggleBtn } = hud.getElements();

  assert.equal(hud.getIsCollapsed(), false, 'HUD should initially be expanded');
  assert.equal(hudEl.classList.contains('is-minimized'), false);
  assert.ok(floatingTrigger.classList.contains('hidden'));

  // Collapse
  hud.collapse();
  assert.equal(hud.getIsCollapsed(), true);
  assert.ok(hudEl.classList.contains('is-minimized'));
  assert.equal(floatingTrigger.classList.contains('hidden'), false);
  assert.equal(collapseState, true);

  // Expand
  hud.expand();
  assert.equal(hud.getIsCollapsed(), false);
  assert.equal(hudEl.classList.contains('is-minimized'), false);
  assert.ok(floatingTrigger.classList.contains('hidden'));
  assert.equal(collapseState, false);

  // Toggle Collapse
  hud.toggleCollapse();
  assert.equal(hud.getIsCollapsed(), true);
  hud.toggleCollapse();
  assert.equal(hud.getIsCollapsed(), false);

  hud.destroy();
});

// 13. Mobile Drawer Controls & Floating Pill Interaction
test('Drawer handle, toggle button, and floating trigger fire collapse/expand', () => {
  const { doc, container } = createTestEnvironment();
  const hud = createFlightHUD(container, { document: doc });
  const { hud: hudEl, handleBar, toggleBtn, floatingTrigger } = hud.getElements();

  // Click on Toggle Button in header
  toggleBtn.dispatchEvent({ type: 'click' });
  assert.equal(hud.getIsCollapsed(), true, 'Clicking toggle button should collapse HUD');
  assert.ok(hudEl.classList.contains('is-minimized'));

  // Click on Floating Trigger Pill
  floatingTrigger.dispatchEvent({ type: 'click' });
  assert.equal(hud.getIsCollapsed(), false, 'Clicking floating trigger should expand HUD');
  assert.equal(hudEl.classList.contains('is-minimized'), false);

  // Click on Handle Bar
  handleBar.dispatchEvent({ type: 'click' });
  assert.equal(hud.getIsCollapsed(), true, 'Clicking handle bar should collapse HUD');

  // Enter key on Handle Bar
  handleBar.dispatchEvent({ type: 'keydown', key: 'Enter' });
  assert.equal(hud.getIsCollapsed(), false, 'Enter key on handle bar should expand HUD');

  hud.destroy();
});

test('Projection toggle switches between Globe and Robinson and triggers callback', () => {
  const { doc, container } = createTestEnvironment();
  let changedProj = null;
  const hud = createFlightHUD(container, {
    document: doc,
    initialProjection: 'orthographic',
    onProjectionChange: (type) => { changedProj = type; }
  });

  const elements = hud.getElements();
  assert.ok(elements.projToggle, 'Projection toggle group must exist');
  assert.ok(elements.projToggle.parentElement === container, 'Projection switch must live outside the console');
  hud.collapse();
  assert.ok(elements.projToggle.parentElement === container, 'Hiding the console must leave projection controls mounted');
  assert.ok(elements.orthoBtn, 'Ortho button must exist');
  assert.ok(elements.robinsonBtn, 'Robinson button must exist');

  assert.equal(hud.getProjection(), 'orthographic');
  assert.equal(elements.orthoBtn.getAttribute('aria-pressed'), 'true');
  assert.equal(elements.robinsonBtn.getAttribute('aria-pressed'), 'false');

  // Click Robinson button
  elements.robinsonBtn.dispatchEvent({ type: 'click' });
  assert.equal(hud.getProjection(), 'robinson');
  assert.equal(changedProj, 'robinson');
  assert.equal(elements.robinsonBtn.getAttribute('aria-pressed'), 'true');
  assert.equal(elements.orthoBtn.getAttribute('aria-pressed'), 'false');

  // Click Ortho button
  elements.orthoBtn.dispatchEvent({ type: 'click' });
  assert.equal(hud.getProjection(), 'orthographic');
  assert.equal(changedProj, 'orthographic');
  hud.expand();
  assert.ok(elements.projToggle.parentElement === container);

  hud.destroy();
  assert.equal(container.children.length, 0, 'Destroy must remove the independent projection switch');
});

console.log(`\nTest results: ${passed} passed, ${failed} failed.`);
if (failed > 0) {
  process.exit(1);
}
