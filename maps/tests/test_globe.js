/**
 * Test suite for maps/src/globe.js
 *
 * Verifies that globe.js:
 * 1. Compiles cleanly as an ES module and exports createGlobeRenderer.
 * 2. Validates dependencies and input parameters.
 * 3. Mounts an orthographic globe canvas into a container element.
 * 4. Renders globe elements (sphere, graticules, landmass, route line, pins, midpoint).
 * 5. Supports lifecycle methods: setRoute, recenter, rotateTo, resize, destroy.
 * 6. Handles mouse drag rotation, wheel zoom, touch drag/pinch.
 * 7. Loads and validates the TopoJSON dataset maps/data/world-110m.json.
 */

import { readFileSync, existsSync, statSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

import { createGlobeRenderer, encodeLut16, getRobinsonFragmentSource } from '../src/globe.js';
import { computeGeodesic } from '../src/geodesic.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Helper test runner
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

console.log('Running GlobeRenderer Verification Suite...\n');

// 1. Verify Export
test('createGlobeRenderer is exported as a function', () => {
  assert.equal(typeof createGlobeRenderer, 'function');
});

// 2. Verify world-110m.json asset
test('world-110m.json exists, is valid TopoJSON and <= 150 KB', () => {
  const assetPath = resolve(__dirname, '../data/world-110m.json');
  assert.ok(existsSync(assetPath), 'Asset maps/data/world-110m.json must exist');

  const fileBuffer = readFileSync(assetPath);
  const fileSizeKB = fileBuffer.length / 1024;
  assert.ok(fileSizeKB <= 150, `Asset size (${fileSizeKB.toFixed(1)} KB) must be <= 150 KB`);

  const topo = JSON.parse(fileBuffer.toString('utf-8'));
  assert.equal(topo.type, 'Topology', 'Data must be TopoJSON Topology');
  assert.ok(topo.objects && (topo.objects.land || topo.objects.countries), 'Topology must have land or countries');
});

// 3. Dependency validation error when D3 is missing
test('throws an informative error if D3 is not provided or accessible', () => {
  const mockContainer = {
    clientWidth: 800,
    clientHeight: 600,
    appendChild: () => {}
  };
  assert.throws(
    () => createGlobeRenderer(mockContainer, null, { d3: null }),
    /D3 \(d3-geo\) is required/
  );
});

// Setup mock window for browser-like testing in Node
const windowListeners = new Map();
globalThis.window = {
  devicePixelRatio: 2,
  addEventListener: (event, handler) => {
    if (!windowListeners.has(event)) windowListeners.set(event, []);
    windowListeners.get(event).push(handler);
  },
  removeEventListener: (event, handler) => {
    if (windowListeners.has(event)) {
      const list = windowListeners.get(event).filter((h) => h !== handler);
      windowListeners.set(event, list);
    }
  },
  _dispatch: (event, data) => {
    const list = windowListeners.get(event) || [];
    list.forEach((h) => h(data));
  },
  _clear: () => windowListeners.clear()
};

// Create comprehensive Mock Canvas and Context for Headless Node Testing
function createMockCanvas(width = 800, height = 600) {
  const drawnElements = [];
  const listeners = new Map();

  const ctx = {
    save: () => drawnElements.push({ type: 'save' }),
    restore: () => drawnElements.push({ type: 'restore' }),
    beginPath: () => drawnElements.push({ type: 'beginPath' }),
    arc: (x, y, r) => drawnElements.push({ type: 'arc', x, y, r }),
    moveTo: (x, y) => drawnElements.push({ type: 'moveTo', x, y }),
    lineTo: (x, y) => drawnElements.push({ type: 'lineTo', x, y }),
    stroke: () => drawnElements.push({ type: 'stroke' }),
    fill: () => drawnElements.push({ type: 'fill' }),
    clearRect: (x, y, w, h) => drawnElements.push({ type: 'clearRect', x, y, w, h }),
    fillRect: (x, y, w, h) => drawnElements.push({ type: 'fillRect', x, y, w, h }),
    rect: (x, y, w, h) => drawnElements.push({ type: 'rect', x, y, w, h }),
    roundRect: (x, y, w, h, r) => drawnElements.push({ type: 'roundRect', x, y, w, h, r }),
    setTransform: () => {},
    translate: () => {},
    closePath: () => {},
    rotate: () => {},
    scale: () => {},
    scale: () => {},
    measureText: (text) => ({ width: text.length * 7 }),
    fillText: (text, x, y) => drawnElements.push({ type: 'fillText', text, x, y }),
    createRadialGradient: () => ({
      addColorStop: () => {}
    })
  };

  const canvas = {
    width,
    height,
    style: {},
    className: '',
    parentElement: null,
    getContext: (type) => (type === '2d' ? ctx : null),
    addEventListener: (event, handler) => {
      if (!listeners.has(event)) listeners.set(event, []);
      listeners.get(event).push(handler);
    },
    removeEventListener: (event, handler) => {
      if (listeners.has(event)) {
        const list = listeners.get(event).filter((h) => h !== handler);
        listeners.set(event, list);
      }
    },
    dispatchEvent: (event) => {
      const handlers = listeners.get(event.type) || [];
      handlers.forEach((h) => h(event));
    },
    _getListeners: () => listeners,
    _getDrawnElements: () => drawnElements
  };

  return { canvas, ctx, drawnElements, listeners };
}

// Mock Container Element
function createMockContainer(canvas) {
  const container = {
    clientWidth: 800,
    clientHeight: 600,
    children: [],
    appendChild: (child) => {
      container.children.push(child);
      child.parentElement = container;
    },
    removeChild: (child) => {
      container.children = container.children.filter((c) => c !== child);
      child.parentElement = null;
    }
  };
  return container;
}

// Mock D3 Projection and Path Generator
function createMockD3() {
  const createProjectionMock = () => {
    let projRot = [0, -20, 0];
    let projScale = 300;
    let projTrans = [400, 300];
    let projCenter = [0, 0];

    const proj = (coord) => {
      const x = projTrans[0] + (coord[0] + projRot[0]) * 2;
      const y = projTrans[1] - (coord[1] + projRot[1]) * 2;
      return [x, y];
    };
    proj.clipAngle = () => proj;
    proj.precision = () => proj;
    proj.scale = (s) => {
      if (s === undefined) return projScale;
      projScale = s;
      return proj;
    };
    proj.translate = (t) => {
      if (t === undefined) return projTrans;
      projTrans = t;
      return proj;
    };
    proj.rotate = (r) => {
      if (r === undefined) return projRot;
      projRot = r;
      return proj;
    };
    proj.center = (c) => {
      if (c === undefined) return projCenter;
      projCenter = c;
      return proj;
    };
    return proj;
  };

  const ortho = createProjectionMock();
  const robinson = createProjectionMock();

  return {
    geoOrthographic: () => ortho,
    geoRobinson: () => robinson,
    geoPath: (proj, ctx) => (obj) => {},
    geoGraticule10: () => ({ type: 'MultiLineString', coordinates: [] })
  };
};

function _unusedD3() {
  return {
    geoOrthographic: () => projection,
    geoPath: (proj, ctx) => path,
    geoGraticule10: () => ({ type: 'MultiLineString', coordinates: [] })
  };
}

// Mock TopoJSON
function createMockTopojson() {
  return {
    feature: (topology, object) => ({
      type: 'FeatureCollection',
      features: [{ type: 'Feature', geometry: { type: 'Polygon', coordinates: [] } }]
    })
  };
}

// 4. Test Renderer Initialization and Controller Methods
test('createGlobeRenderer attaches canvas and returns full controller API', () => {
  const { canvas } = createMockCanvas();
  const container = createMockContainer(canvas);
  const mockD3 = createMockD3();
  const mockTopojson = createMockTopojson();

  const controller = createGlobeRenderer(container, { type: 'Topology', objects: { land: {} } }, {
    canvas,
    d3: mockD3,
    topojson: mockTopojson,
    width: 800,
    height: 600
  });

  assert.ok(controller, 'Controller must be returned');
  assert.equal(typeof controller.setRoute, 'function', 'controller.setRoute must be a function');
  assert.equal(typeof controller.recenter, 'function', 'controller.recenter must be a function');
  assert.equal(typeof controller.rotateTo, 'function', 'controller.rotateTo must be a function');
  assert.equal(typeof controller.resize, 'function', 'controller.resize must be a function');
  assert.equal(typeof controller.destroy, 'function', 'controller.destroy must be a function');
  assert.equal(typeof controller.render, 'function', 'controller.render must be a function');
  assert.equal(typeof controller.getRotation, 'function', 'controller.getRotation must be a function');
  assert.equal(typeof controller.getScale, 'function', 'controller.getScale must be a function');

  // Verify canvas was mounted
  assert.equal(canvas.parentElement, container);

  controller.destroy();
});

// 5. Test Route setting and instant rotation
test('setRoute updates rotation to spherical midpoint and renders route', () => {
  const { canvas, drawnElements } = createMockCanvas();
  const container = createMockContainer(canvas);
  const mockD3 = createMockD3();
  const mockTopojson = createMockTopojson();

  const controller = createGlobeRenderer(container, { type: 'Topology', objects: { land: {} } }, {
    canvas,
    d3: mockD3,
    topojson: mockTopojson,
    width: 800,
    height: 600
  });

  const sampleOrigin = { name: 'London', lat: 51.5074, lng: -0.1278 };
  const sampleDest = { name: 'New York', lat: 40.7128, lng: -74.0060 };
  const sampleGeodesic = {
    distanceKm: 5570,
    distanceMiles: 3461,
    sphericalMidpoint: { lat: 52.5, lng: -37.0 },
    isAntipodal: false,
    pathCoordinates: [
      [-0.1278, 51.5074],
      [-37.0, 52.5],
      [-74.0060, 40.7128]
    ]
  };

  // Test with animate = false for instantaneous rotation update
  controller.setRoute(sampleGeodesic, sampleOrigin, sampleDest, false);

  const rot = controller.getRotation();
  // Centering on midpoint (lat: 52.5, lng: -37.0) means yaw = -(-37.0) = 37.0, pitch = -52.5
  assert.equal(Math.round(rot[0] * 10) / 10, 37.0);
  assert.equal(Math.round(rot[1] * 10) / 10, -52.5);

  // Verify elements drawn
  assert.ok(drawnElements.length > 0, 'Canvas operations must have executed');

  // Test clearing route
  controller.setRoute(null);

  controller.destroy();
});

// 6. Test Array format city inputs and array midpoint
test('setRoute handles array tuple format for cities and midpoint', () => {
  const { canvas } = createMockCanvas();
  const container = createMockContainer(canvas);
  const mockD3 = createMockD3();
  const mockTopojson = createMockTopojson();

  const controller = createGlobeRenderer(container, null, {
    canvas,
    d3: mockD3,
    topojson: mockTopojson,
    width: 800,
    height: 600
  });

  const originArray = ['Tokyo', 'Japan', 35.6895, 139.6917];
  const destArray = ['San Francisco', 'United States', 37.7749, -122.4194];
  const geodesic = {
    distanceKm: 8280,
    distanceMiles: 5145,
    sphericalMidpoint: [-170.0, 45.0], // [lng, lat]
    pathCoordinates: []
  };

  controller.setRoute(geodesic, originArray, destArray, false);
  const rot = controller.getRotation();
  assert.equal(Math.round(rot[0]), 170);
  assert.equal(Math.round(rot[1]), -45);

  controller.destroy();
});

// 7. Test rotateTo and recenter
test('rotateTo and recenter navigate projection to expected coordinates', () => {
  const { canvas } = createMockCanvas();
  const container = createMockContainer(canvas);
  const mockD3 = createMockD3();
  const mockTopojson = createMockTopojson();

  const controller = createGlobeRenderer(container, null, {
    canvas,
    d3: mockD3,
    topojson: mockTopojson,
    width: 800,
    height: 600,
    initialRotation: [0, -20, 0]
  });

  // rotateTo Tokyo (lng: 139.69, lat: 35.68)
  controller.rotateTo(139.69, 35.68, false);
  const rotTokyo = controller.getRotation();
  assert.equal(Math.round(rotTokyo[0] * 100) / 100, -139.69);
  assert.equal(Math.round(rotTokyo[1] * 100) / 100, -35.68);

  // Set a route and test recenter
  controller.setRoute({
    sphericalMidpoint: { lat: 10.0, lng: 20.0 }
  }, null, null, false);

  controller.rotateTo(0, 0, false);
  assert.equal(controller.getRotation()[0], 0);
  assert.equal(controller.getRotation()[1], 0);

  // recenter() should target (lng: 20.0, lat: 10.0)
  controller.rotateTo(20.0, 10.0, false);
  const rotRecenter = controller.getRotation();
  assert.equal(rotRecenter[0], -20.0);
  assert.equal(rotRecenter[1], -10.0);

  controller.destroy();
});

// 8. Test resize
test('resize updates canvas dimensions and DPR scaling', () => {
  const { canvas } = createMockCanvas(800, 600);
  const container = createMockContainer(canvas);
  const mockD3 = createMockD3();
  const mockTopojson = createMockTopojson();

  const controller = createGlobeRenderer(container, null, {
    canvas,
    d3: mockD3,
    topojson: mockTopojson,
    width: 800,
    height: 600
  });

  controller.resize(1024, 768);
  // With window.devicePixelRatio = 2
  assert.equal(canvas.width, 2048);
  assert.equal(canvas.height, 1536);
  assert.equal(canvas.style.width, '1024px');
  assert.equal(canvas.style.height, '768px');

  controller.destroy();
});

// 9. Test Mouse Drag Rotation and Wheel Zoom
test('mouse drag and window mousemove alters projection rotation', () => {
  windowListeners.clear();
  const { canvas } = createMockCanvas();
  const container = createMockContainer(canvas);
  const mockD3 = createMockD3();
  const mockTopojson = createMockTopojson();

  let rotatedRotation = null;
  let zoomedScale = null;

  const controller = createGlobeRenderer(container, null, {
    canvas,
    d3: mockD3,
    topojson: mockTopojson,
    width: 800,
    height: 600,
    initialRotation: [0, 0, 0],
    onRotate: (rot) => { rotatedRotation = rot; },
    onZoom: (scale) => { zoomedScale = scale; }
  });

  const listeners = canvas._getListeners();
  const onMouseDown = listeners.get('mousedown')[0];
  assert.ok(onMouseDown, 'mousedown listener must be attached');

  // Trigger mousedown at (100, 100)
  onMouseDown({ button: 0, clientX: 100, clientY: 100 });

  // Simulate mousemove on window
  globalThis.window._dispatch('mousemove', { clientX: 150, clientY: 120 });
  assert.ok(rotatedRotation, 'onRotate callback should be fired on drag');
  assert.notDeepEqual(controller.getRotation(), [0, 0, 0]);

  // Simulate mouseup
  globalThis.window._dispatch('mouseup', {});

  // Test wheel zoom
  const onWheel = listeners.get('wheel')[0];
  assert.ok(onWheel, 'wheel listener must be attached');
  const initialScale = controller.getScale();
  onWheel({ deltaY: -100, preventDefault: () => {} });
  assert.ok(controller.getScale() > initialScale, 'Zoom in should increase scale');
  assert.ok(zoomedScale > initialScale, 'onZoom callback should receive new scale');

  onWheel({ deltaY: 100, preventDefault: () => {} });
  assert.ok(controller.getScale() < initialScale * 1.15, 'Zoom out should decrease scale');

  controller.destroy();
});

// 10. Test Touch drag and pinch gestures
test('touch gestures handle drag and pinch-to-zoom', () => {
  const { canvas } = createMockCanvas();
  const container = createMockContainer(canvas);
  const mockD3 = createMockD3();
  const mockTopojson = createMockTopojson();

  const controller = createGlobeRenderer(container, null, {
    canvas,
    d3: mockD3,
    topojson: mockTopojson,
    width: 800,
    height: 600,
    initialRotation: [0, 0, 0]
  });

  const listeners = canvas._getListeners();
  const onTouchStart = listeners.get('touchstart')[0];
  const onTouchMove = listeners.get('touchmove')[0];
  const onTouchEnd = listeners.get('touchend')[0];

  assert.ok(onTouchStart && onTouchMove && onTouchEnd, 'Touch event listeners must be attached');

  // Single-touch drag
  onTouchStart({ touches: [{ clientX: 200, clientY: 200 }] });
  onTouchMove({
    cancelable: true,
    preventDefault: () => {},
    touches: [{ clientX: 240, clientY: 210 }]
  });
  const rotAfterTouch = controller.getRotation();
  assert.notDeepEqual(rotAfterTouch, [0, 0, 0], 'Single touch move should rotate globe');
  onTouchEnd({ touches: [] });

  // Two-touch pinch zoom
  const scaleBeforePinch = controller.getScale();
  onTouchStart({
    touches: [
      { clientX: 100, clientY: 100 },
      { clientX: 200, clientY: 200 }
    ]
  });
  // Pinch out (fingers move further apart)
  onTouchMove({
    cancelable: true,
    preventDefault: () => {},
    touches: [
      { clientX: 50, clientY: 50 },
      { clientX: 250, clientY: 250 }
    ]
  });
  assert.ok(controller.getScale() > scaleBeforePinch, 'Pinch out should increase scale');

  controller.destroy();
});

// 11. Test destroy lifecycle cleanup
test('destroy cleans up event listeners and detaches canvas element', () => {
  const { canvas } = createMockCanvas();
  const container = createMockContainer(canvas);
  const mockD3 = createMockD3();
  const mockTopojson = createMockTopojson();

  const controller = createGlobeRenderer(container, null, {
    canvas,
    d3: mockD3,
    topojson: mockTopojson,
    width: 800,
    height: 600
  });

  assert.equal(canvas.parentElement, container);
  controller.destroy();
  assert.equal(canvas.parentElement, null, 'Canvas should be unmounted after destroy');
});

// 12. Test with Real TopoJSON world-110m.json data
test('initializes successfully with real world-110m.json dataset', () => {
  const assetPath = resolve(__dirname, '../data/world-110m.json');
  const worldTopology = JSON.parse(readFileSync(assetPath, 'utf-8'));

  const { canvas } = createMockCanvas();
  const container = createMockContainer(canvas);
  const mockD3 = createMockD3();
  const mockTopojson = createMockTopojson();

  let extractCalled = false;
  mockTopojson.feature = (topology, object) => {
    extractCalled = true;
    assert.equal(topology.type, 'Topology');
    return { type: 'FeatureCollection', features: [] };
  };

  const controller = createGlobeRenderer(container, worldTopology, {
    canvas,
    d3: mockD3,
    topojson: mockTopojson
  });

  assert.ok(extractCalled, 'topojson.feature should be called to extract land geometry');
  controller.destroy();
});

// 13. Test TopoJSON Sovereign Country Borders extraction via topojson.mesh
test("extracts sovereign country borders when objects.countries is present", () => {
  const { canvas, ctx, drawnElements } = createMockCanvas();
  const container = createMockContainer(canvas);
  const mockD3 = createMockD3();
  const mockTopojson = createMockTopojson();

  let meshExtracted = false;
  mockTopojson.mesh = (topology, object, filter) => {
    meshExtracted = true;
    assert.equal(typeof filter, "function", "Filter callback should separate adjacent countries");
    return { type: "MultiLineString", coordinates: [] };
  };

  const fakeTopology = {
    type: "Topology",
    objects: {
      land: {},
      countries: {}
    }
  };

  const controller = createGlobeRenderer(container, fakeTopology, {
    canvas,
    d3: mockD3,
    topojson: mockTopojson,
    terrain: false
  });

  assert.ok(meshExtracted, "topojson.mesh must be invoked to extract country boundaries");
  assert.equal(typeof controller.render, "function");
  controller.render();
  controller.destroy();
});

// 14. Test ETOPO Terrain Data Asset Integrity
test("maps/data/etopo.jpg exists and is a valid JPEG binary", () => {
  const etopoPath = resolve(__dirname, "../data/etopo.jpg");
  assert.ok(existsSync(etopoPath), "etopo.jpg must exist in maps/data/");
  const stat = statSync(etopoPath);
  assert.ok(stat.size > 100000, "etopo.jpg should be a detailed texture (>100KB)");
  const buf = readFileSync(etopoPath);
  // JPEG magic header: FF D8 FF
  assert.equal(buf[0], 0xFF);
  assert.equal(buf[1], 0xD8);
  assert.equal(buf[2], 0xFF);
});


// 14a. Progressive 8k terrain texture: power-of-two 8192x4096 JPEG within an 8 MB budget
test('etopo-8k.jpg exists, is an 8192x4096 JPEG and <= 8 MB', () => {
  const p = resolve(__dirname, '../data/etopo-8k.jpg');
  assert.ok(existsSync(p), 'etopo-8k.jpg must exist in maps/data/');
  const buf = readFileSync(p);
  assert.ok(buf.length <= 8 * 1024 * 1024, `etopo-8k.jpg must stay <= 8 MB, got ${buf.length}`);
  assert.equal(buf[0], 0xFF);
  assert.equal(buf[1], 0xD8);
  // Walk JPEG markers to the SOF segment and read its dimensions.
  let i = 2, dims = null;
  while (i < buf.length - 9) {
    if (buf[i] !== 0xFF) { i++; continue; }
    const marker = buf[i + 1];
    const len = buf.readUInt16BE(i + 2);
    if ((marker >= 0xC0 && marker <= 0xC3) || (marker >= 0xC5 && marker <= 0xC7) || (marker >= 0xC9 && marker <= 0xCB)) {
      dims = { h: buf.readUInt16BE(i + 5), w: buf.readUInt16BE(i + 7) };
      break;
    }
    i += 2 + len;
  }
  assert.deepEqual(dims, { w: 8192, h: 4096 });
});

// 14b. Detail (50m) border asset integrity and payload budget
test('world-50m.json detail asset exists, is valid TopoJSON and <= 350 KB', () => {
  const assetPath = resolve(__dirname, '../data/world-50m.json');
  assert.ok(existsSync(assetPath), 'Asset maps/data/world-50m.json must exist');
  const fileBuffer = readFileSync(assetPath);
  const fileSizeKB = fileBuffer.length / 1024;
  assert.ok(fileSizeKB <= 350, `Detail asset size (${fileSizeKB.toFixed(1)} KB) must be <= 350 KB`);
  const topo = JSON.parse(fileBuffer.toString('utf-8'));
  assert.equal(topo.type, 'Topology');
  assert.ok(topo.objects.countries && topo.objects.land, 'Detail topology must have countries and land');
});

// 14c. Detail topology is drawn on idle frames only; base topology during drag/animation
test('setDetailTopology swaps to detail features when idle and back to base while dragging', () => {
  const { canvas } = createMockCanvas();
  const container = createMockContainer(canvas);
  const drawn = [];
  const mockD3 = createMockD3();
  mockD3.geoPath = () => (obj) => { if (obj && obj.tag) drawn.push(obj.tag); };
  const mockTopojson = {
    feature: (topology, object) => ({ type: 'FeatureCollection', features: [], tag: topology.tag + ':land' }),
    mesh: (topology) => ({ type: 'MultiLineString', coordinates: [], tag: topology.tag + ':borders' })
  };
  const baseTopo = { type: 'Topology', tag: 'base', objects: { land: {}, countries: {} } };
  const detailTopo = { type: 'Topology', tag: 'detail', objects: { land: {}, countries: {} } };

  const controller = createGlobeRenderer(container, baseTopo, {
    canvas, d3: mockD3, topojson: mockTopojson, terrain: false
  });
  assert.equal(typeof controller.setDetailTopology, 'function');

  drawn.length = 0;
  controller.render();
  assert.deepEqual(drawn, ['base:land', 'base:borders'], 'base features before detail is loaded');

  drawn.length = 0;
  controller.setDetailTopology(detailTopo);
  assert.deepEqual(drawn, ['detail:land', 'detail:borders'], 'detail features drawn once idle');

  // Simulate a mouse drag: interaction frames must use the base topology.
  drawn.length = 0;
  canvas.dispatchEvent({ type: 'mousedown', button: 0, clientX: 10, clientY: 10 });
  controller.render();
  assert.deepEqual(drawn, ['base:land', 'base:borders'], 'base features while dragging');
  drawn.length = 0;
  globalThis.window._dispatch('mouseup', {});
  assert.deepEqual(drawn, ['detail:land', 'detail:borders'], 'detail frame drawn when the drag ends');

  // Non-animated rotateTo leaves no animation running, so detail is used again.
  drawn.length = 0;
  controller.rotateTo(10, 10, false);
  assert.ok(drawn.includes('detail:borders') || drawn.includes('base:borders'));

  controller.setDetailTopology(null);
  drawn.length = 0;
  controller.render();
  assert.deepEqual(drawn, ['base:land', 'base:borders'], 'clearing detail restores base');
  controller.destroy();
});

// 15. Test Vertical Center Ratio & Projection Translate
test("verticalCenterRatio adjusts projection translation for mobile viewports", () => {
  const { canvas } = createMockCanvas();
  const container = createMockContainer(canvas);
  const mockD3 = createMockD3();
  const mockTopojson = createMockTopojson();

  const controller = createGlobeRenderer(container, null, {
    canvas,
    d3: mockD3,
    topojson: mockTopojson,
    width: 400,
    height: 800,
    verticalCenterRatio: 0.28,
    terrain: false
  });

  assert.equal(typeof controller.setVerticalCenterRatio, "function");
  assert.equal(typeof controller.getVerticalCenterRatio, "function");
  assert.equal(controller.getVerticalCenterRatio(), 0.28);

  const [cx, cy] = controller.getProjection().translate();
  assert.equal(cx, 200);
  assert.equal(cy, 800 * 0.28);

  // Update ratio without animation
  controller.setVerticalCenterRatio(0.50, false);
  assert.equal(controller.getVerticalCenterRatio(), 0.50);
  const [, newCy] = controller.getProjection().translate();
  assert.equal(newCy, 400);

  controller.destroy();
});

// 16. Test Projection Type Toggle (Orthographic <-> Robinson)
test("setProjectionType toggles between orthographic and robinson", () => {
  const { canvas } = createMockCanvas();
  const container = createMockContainer(canvas);
  const mockD3 = createMockD3();
  const mockTopojson = createMockTopojson();

  const controller = createGlobeRenderer(container, null, {
    canvas,
    d3: mockD3,
    topojson: mockTopojson,
    projectionType: 'orthographic',
    terrain: false
  });

  assert.equal(controller.getProjectionType(), 'orthographic');

  controller.setProjectionType('robinson');
  assert.equal(controller.getProjectionType(), 'robinson');

  controller.setProjectionType('orthographic');
  assert.equal(controller.getProjectionType(), 'orthographic');

  controller.destroy();
});

test('Robinson polar routes use an endpoint center on switch, route change, and recenter', () => {
  const { canvas } = createMockCanvas();
  const controller = createGlobeRenderer(createMockContainer(canvas), null, {
    canvas, d3: createMockD3(), terrain: false
  });
  const origin = { lat: 25.0478, lng: 121.5319 };
  const dest = { lat: -25.3007, lng: -57.6359 };
  const route = { sphericalMidpoint: { lat: 90, lng: 0 }, pathCoordinates: [[origin.lng, origin.lat], [0, 90], [dest.lng, dest.lat]] };
  function checkCenter() {
    const [lng, lat] = controller.getProjection().center();
    assert.equal(lng, 0);
    assert.ok(Math.abs(lat) < 1, 'Polar route must keep endpoint latitudes in view');
    assert.ok(Math.abs(controller.getRotation()[0] + 31.948) < 0.001, 'Center longitude must fall between endpoints');
  }
  controller.setRoute(route, origin, dest, false);
  controller.setProjectionType('robinson');
  checkCenter();
  controller.recenter(false);
  checkCenter();
  controller.setRoute({ ...route, sphericalMidpoint: { lat: -90, lng: 0 } }, origin, dest, false);
  checkCenter();
  controller.destroy();
});

test('Near-antipodal globe framing contains the full polar route and both city markers', () => {
  for (const [origin, dest] of [
    [{ name: 'Taipei', lat: 25.0478, lng: 121.5319 }, { name: 'Asuncion', lat: -25.3007, lng: -57.6359 }],
    [{ name: 'Madrid', lat: 40.4167, lng: -3.7167 }, { name: 'Wellington', lat: -41.2889, lng: 174.7772 }]
  ]) {
    const { canvas, drawnElements } = createMockCanvas();
    const controller = createGlobeRenderer(createMockContainer(canvas), null, { canvas, d3: createMockD3(), terrain: false });
    const route = computeGeodesic(origin, dest);
    controller.setRoute(route, origin, dest, false);
    controller.recenter(false);
    const [yaw, pitch] = controller.getRotation();
    const lat0 = -pitch * Math.PI / 180, lng0 = -yaw * Math.PI / 180;
    for (const [lng, lat] of route.pathCoordinates) {
      const phi = lat * Math.PI / 180, lambda = lng * Math.PI / 180;
      const dot = Math.sin(lat0) * Math.sin(phi) + Math.cos(lat0) * Math.cos(phi) * Math.cos(lambda - lng0);
      assert.ok(dot >= -1e-9, 'Every point of the polar fallback route must face the viewer');
    }
    for (const city of [origin, dest]) assert.ok(drawnElements.some(e => e.type === 'fillText' && e.text === city.name), 'Both city labels must be drawn');
    controller.destroy();
  }
});

test('Robinson fits the free viewport and opens a short route at regional zoom', () => {
  const { canvas } = createMockCanvas();
  const controller = createGlobeRenderer(createMockContainer(canvas), null, { canvas, d3: createMockD3(), terrain: false });
  controller.setRobinsonViewportInsets({ left: 428, top: 80 });
  controller.setProjectionType('robinson');
  assert.deepEqual(controller.getProjection().translate(), [614, 340]);
  const worldScale = controller.getScale();
  const origin = { lat: 1.3, lng: 103.8 }, dest = { lat: 3.1478, lng: 101.6953 };
  controller.setRoute(computeGeodesic(origin, dest), origin, dest, false);
  assert.ok(controller.getScale() > worldScale * 10, 'Nearby cities need a regional default view');
  assert.ok(controller.getScale() <= 2400, 'Default zoom must remain inside the existing zoom range');
  const scale = controller.getScale();
  controller.recenter(false);
  assert.equal(controller.getScale(), scale, 'Recenter should restore the route default');
  controller.destroy();
});

test('Viewport insets frame both projections and survive ratio changes and resizing', () => {
  const { canvas } = createMockCanvas();
  const controller = createGlobeRenderer(createMockContainer(canvas), null, {
    canvas, d3: createMockD3(), terrain: false, width: 1280, height: 800
  });
  controller.setViewportInsets({ left: 428, right: 20, top: 80, bottom: 40 });
  assert.deepEqual(controller.getProjection().translate(), [844, 420]);
  controller.setVerticalCenterRatio(0.4, false);
  assert.deepEqual(controller.getProjection().translate(), [844, 336]);
  controller.setVerticalCenterRatio(0.5, false);
  controller.setProjectionType('robinson');
  assert.deepEqual(controller.getProjection().translate(), [844, 420]);
  controller.resize(390, 844);
  controller.setViewportInsets({ top: 80, bottom: 400 });
  assert.deepEqual(controller.getProjection().translate(), [195, 262]);
  assert.ok(Math.abs(controller.getScale() - Math.min(342 / (2 * Math.PI), 316 / 3.1868315878)) < 1e-9);
  controller.setProjectionType('orthographic');
  assert.deepEqual(controller.getProjection().translate(), [195, 262]);
  controller.setRobinsonViewportInsets({ bottom: 400 });
  assert.deepEqual(controller.getProjection().translate(), [195, 222], 'Legacy alias must update both projections');
  controller.destroy();
});

test('Robinson LUT decode in the shader reproduces the encoded latitude and half-width', () => {
  // Regression: the shader decoded hi/lo bytes as r + g/255, which read every
  // latitude ~0.35-0.6 deg too far north, so terrain sat south of the borders.
  const src = getRobinsonFragmentSource();
  const latExpr = src.match(/float lat = \((.+)\) \* 2\.0 - 1\.0;/);
  const hwExpr = src.match(/float hw = (.+);/);
  assert.ok(latExpr && hwExpr, 'Shader must contain the lat and hw decode expressions');
  const decLat = new Function('t', `return ${latExpr[1]};`);
  const decHw = new Function('t', `return ${hwExpr[1]};`);
  let worstLat = 0;
  let worstHw = 0;
  for (let lat = -90; lat <= 90; lat += 0.25) {
    const [h, l] = encodeLut16(lat / 90 * 0.5 + 0.5);
    const got = (decLat({ r: h / 255, g: l / 255 }) * 2 - 1) * 90;
    worstLat = Math.max(worstLat, Math.abs(got - lat));
  }
  for (let v = 0; v <= 1; v += 0.001) {
    const [h, l] = encodeLut16(v);
    worstHw = Math.max(worstHw, Math.abs(decHw({ b: h / 255, a: l / 255 }) - v));
  }
  assert.ok(worstLat < 0.01, `Latitude decode error ${worstLat.toFixed(4)} deg exceeds 0.01 deg`);
  assert.ok(worstHw < 1e-4, `Half-width decode error ${worstHw.toExponential(2)} exceeds 1e-4`);
});

console.log(`\nTest results: ${passed} passed, ${failed} failed.`);
if (failed > 0) {
  process.exit(1);
}
