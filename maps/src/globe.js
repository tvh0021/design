/**
 * Geodesic Map - Interactive Orthographic Globe Renderer
 *
 * Renders an orthographic Earth globe onto an HTML5 Canvas using D3.js (d3-geo, topojson-client).
 * Supports mouse drag rotation, pinch / wheel zoom, smooth slerp transitions,
 * glowing great-circle geodesic paths, landmasses, graticules, and city waypoint pins.
 */

/**
 * Resolves D3 and TopoJSON from options, global scope, or window.
 */
function resolveDependencies(options = {}) {
  const d3 = options.d3 || (typeof window !== 'undefined' ? window.d3 : null) || globalThis.d3;
  const topojson = options.topojson || (typeof window !== 'undefined' ? window.topojson : null) || globalThis.topojson;
  return { d3, topojson };
}

/**
 * Normalizes city coordinates to [lng, lat].
 * Supports { lat, lng }, { lat, lon }, or [name, country, lat, lng] format.
 */
function getCityCoords(city) {
  if (!city) return null;
  if (typeof city.lng === 'number' && typeof city.lat === 'number') {
    return [city.lng, city.lat];
  }
  if (typeof city.lon === 'number' && typeof city.lat === 'number') {
    return [city.lon, city.lat];
  }
  if (Array.isArray(city) && city.length >= 4 && typeof city[2] === 'number' && typeof city[3] === 'number') {
    return [city[3], city[2]]; // [lng, lat]
  }
  return null;
}

/**
 * Normalizes city name.
 */
function getCityName(city) {
  if (!city) return '';
  if (typeof city.name === 'string') return city.name;
  if (Array.isArray(city) && typeof city[0] === 'string') return city[0];
  return '';
}

/**
 * Normalizes spherical midpoint coordinate to { lat, lng }.
 */
function normalizeMidpoint(mid) {
  if (!mid) return null;
  if (typeof mid.lat === 'number' && typeof mid.lng === 'number') {
    return { lat: mid.lat, lng: mid.lng };
  }
  if (typeof mid.lat === 'number' && typeof mid.lon === 'number') {
    return { lat: mid.lat, lng: mid.lon };
  }
  if (Array.isArray(mid) && mid.length >= 2) {
    return { lat: mid[1], lng: mid[0] };
  }
  return null;
}

/**
 * Checks if a geographical coordinate [lng, lat] is located on the visible
 * front hemisphere of the orthographic projection (angular distance <= 90 deg).
 */
function isCoordVisible(coord, projection) {
  if (!coord || !projection) return false;
  const rot = projection.rotate();
  // Projection center coordinates in degrees
  const centerLng = rot[0] === 0 ? 0 : -rot[0];
  const centerLat = rot[1] === 0 ? 0 : -rot[1];

  const toRad = Math.PI / 180;
  const phi0 = centerLat * toRad;
  const lam0 = centerLng * toRad;
  const phi = coord[1] * toRad;
  const lam = coord[0] * toRad;

  // cos(angular_distance) = sin(phi0)*sin(phi) + cos(phi0)*cos(phi)*cos(lam - lam0)
  const cosDist = Math.sin(phi0) * Math.sin(phi) +
                  Math.cos(phi0) * Math.cos(phi) * Math.cos(lam - lam0);

  return cosDist > 0.005;
}

/**
 * Creates an interactive orthographic globe renderer mounted in `containerElement`.
 *
 * @param {HTMLElement} containerElement - DOM container to attach canvas to
 * @param {object} worldTopology - TopoJSON topology (e.g. world-110m.json) or GeoJSON feature
 * @param {object} options - Configuration options:
 *   - d3: D3 object containing geoOrthographic, geoPath, etc. (falls back to window.d3)
 *   - topojson: TopoJSON client containing feature() (falls back to window.topojson)
 *   - width: initial width in px
 *   - height: initial height in px
 *   - initialRotation: [yaw, pitch, roll], default [0, -20, 0]
 *   - onRotate: callback (rotation) => {}
 *   - onZoom: callback (scale) => {}
 *   - colors: override palette
 * @returns {object} Controller with setRoute, recenter, rotateTo, resize, destroy
 */
export function createGlobeRenderer(containerElement, worldTopology, options = {}) {
  const { d3, topojson } = resolveDependencies(options);

  if (!d3 || typeof d3.geoOrthographic !== 'function') {
    throw new Error('createGlobeRenderer: D3 (d3-geo) is required. Ensure d3 is available globally or passed via options.d3.');
  }

  // Determine initial canvas dimensions
  let width = options.width || (containerElement && containerElement.clientWidth) || 800;
  let height = options.height || (containerElement && containerElement.clientHeight) || 600;

  // Palette styling per Anthropic / SPEC guidelines
  const palette = {
    ocean: '#0d1424',
    land: '#1e293b',
    landStroke: '#334155',
    graticule: 'rgba(255, 255, 255, 0.08)',
    haloOuter: 'rgba(6, 182, 212, 0.16)',
    haloMid: 'rgba(6, 182, 212, 0.05)',
    haloBorder: 'rgba(6, 182, 212, 0.35)',
    geodesic: '#06b6d4',
    geodesicGlow: 'rgba(6, 182, 212, 0.35)',
    geodesicInnerGlow: 'rgba(6, 182, 212, 0.7)',
    origin: '#f59e0b',
    originAura: 'rgba(245, 158, 11, 0.25)',
    dest: '#10b981',
    destAura: 'rgba(16, 185, 129, 0.25)',
    midpoint: '#06b6d4',
    midpointAura: 'rgba(6, 182, 212, 0.85)',
    ...(options.colors || {})
  };

  // Create canvas element
  const canvas = options.canvas || (typeof document !== 'undefined' ? document.createElement('canvas') : null);
  if (!canvas) {
    throw new Error('createGlobeRenderer: Unable to create or locate canvas element.');
  }
  canvas.className = options.canvasClass || 'globe-canvas';
  canvas.style.display = 'block';
  canvas.style.cursor = 'grab';
  canvas.style.userSelect = 'none';
  canvas.style.webkitUserSelect = 'none';
  canvas.style.touchAction = 'none';

  if (containerElement && canvas.parentElement !== containerElement) {
    containerElement.appendChild(canvas);
  }

  const ctx = canvas.getContext('2d');
  if (!ctx) {
    throw new Error('createGlobeRenderer: Unable to acquire 2D canvas context.');
  }

  // Animation frame and timer references
  const reqAnim = typeof requestAnimationFrame === 'function'
    ? requestAnimationFrame
    : (cb) => setTimeout(() => cb(Date.now()), 16);
  const cancelAnim = typeof cancelAnimationFrame === 'function'
    ? cancelAnimationFrame
    : (id) => clearTimeout(id);

  let animFrameId = null;

  // Convert TopoJSON to GeoJSON land feature once on initialization
  let landFeature = null;
  if (worldTopology) {
    if (worldTopology.type === 'Topology' && worldTopology.objects) {
      if (!topojson || typeof topojson.feature !== 'function') {
        throw new Error('createGlobeRenderer: topojson-client is required to extract geometry from Topology.');
      }
      const topoObj = worldTopology.objects.land || worldTopology.objects.countries;
      if (topoObj) {
        landFeature = topojson.feature(worldTopology, topoObj);
      }
    } else if (worldTopology.type === 'FeatureCollection' || worldTopology.type === 'Feature') {
      landFeature = worldTopology;
    }
  }

  // Pre-generate graticule
  let graticuleFeature = null;
  if (typeof d3.geoGraticule10 === 'function') {
    graticuleFeature = d3.geoGraticule10();
  } else if (typeof d3.geoGraticule === 'function') {
    graticuleFeature = d3.geoGraticule()();
  }

  // Projection setup
  let minScale = Math.min(width, height) * 0.2;
  let maxScale = Math.min(width, height) * 3.5;
  let currentScale = options.scale || Math.min(width, height) * 0.42;
  const initialRot = options.initialRotation || [0, -20, 0];

  const projection = d3.geoOrthographic()
    .clipAngle(90)
    .precision(0.3)
    .scale(currentScale)
    .translate([width / 2, height / 2])
    .rotate(initialRot.slice());

  const path = d3.geoPath(projection, ctx);

  // Application state
  let currentGeodesic = null;
  let currentOrigin = null;
  let currentDest = null;
  let currentRouteGeoJson = null;
  let currentMidpoint = null;

  // Configure high-DPI scaling
  function updateCanvasDimensions() {
    const dpr = typeof window !== 'undefined' ? (window.devicePixelRatio || 1) : 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.scale(dpr, dpr);

    projection.translate([width / 2, height / 2]);
  }

  updateCanvasDimensions();

  // Rendering routines
  function render() {
    ctx.clearRect(0, 0, width, height);

    const [cx, cy] = projection.translate();
    const r = projection.scale();

    // 1. Atmosphere halo ring outside sphere
    if (r > 0) {
      ctx.save();
      const haloGrad = ctx.createRadialGradient(cx, cy, r * 0.96, cx, cy, r * 1.09);
      haloGrad.addColorStop(0, palette.haloOuter);
      haloGrad.addColorStop(0.5, palette.haloMid);
      haloGrad.addColorStop(1, 'rgba(6, 182, 212, 0)');
      ctx.beginPath();
      ctx.arc(cx, cy, r * 1.09, 0, Math.PI * 2);
      ctx.fillStyle = haloGrad;
      ctx.fill();
      ctx.restore();
    }

    // 2. Outer sphere / ocean background
    ctx.save();
    ctx.beginPath();
    path({ type: 'Sphere' });
    ctx.fillStyle = palette.ocean;
    ctx.fill();
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = palette.haloBorder;
    ctx.stroke();
    ctx.restore();

    // 3. Graticule lines (meridians and parallels)
    if (graticuleFeature) {
      ctx.save();
      ctx.beginPath();
      path(graticuleFeature);
      ctx.strokeStyle = palette.graticule;
      ctx.lineWidth = 0.75;
      ctx.stroke();
      ctx.restore();
    }

    // 4. Landmass polygons
    if (landFeature) {
      ctx.save();
      ctx.beginPath();
      path(landFeature);
      ctx.fillStyle = palette.land;
      ctx.fill();
      ctx.strokeStyle = palette.landStroke;
      ctx.lineWidth = 0.8;
      ctx.stroke();
      ctx.restore();
    }

    // 5. Geodesic flight path line (with illuminated cyan glow)
    if (currentRouteGeoJson) {
      ctx.save();
      // Outer soft glow
      ctx.beginPath();
      path(currentRouteGeoJson);
      ctx.strokeStyle = palette.geodesicGlow;
      ctx.lineWidth = 6;
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      ctx.stroke();

      // Inner concentrated glow
      ctx.beginPath();
      path(currentRouteGeoJson);
      ctx.strokeStyle = palette.geodesicInnerGlow;
      ctx.lineWidth = 3.5;
      ctx.stroke();

      // Sharp central line
      ctx.beginPath();
      path(currentRouteGeoJson);
      ctx.strokeStyle = palette.geodesic;
      ctx.lineWidth = 2.5;
      ctx.stroke();
      ctx.restore();
    }

    // 6. Spherical midpoint marker (subtle crosshair / ring)
    if (currentMidpoint) {
      const midCoord = [currentMidpoint.lng, currentMidpoint.lat];
      if (isCoordVisible(midCoord, projection)) {
        const pt = projection(midCoord);
        if (pt && isFinite(pt[0]) && isFinite(pt[1])) {
          drawMidpointMarker(pt[0], pt[1]);
        }
      }
    }

    // 7. City pins and labels (origin and destination)
    if (currentOrigin) {
      const coord = getCityCoords(currentOrigin);
      if (coord && isCoordVisible(coord, projection)) {
        const pt = projection(coord);
        if (pt && isFinite(pt[0]) && isFinite(pt[1])) {
          const name = getCityName(currentOrigin);
          drawCityPin(pt[0], pt[1], name, palette.origin, palette.originAura, 'left');
        }
      }
    }

    if (currentDest) {
      const coord = getCityCoords(currentDest);
      if (coord && isCoordVisible(coord, projection)) {
        const pt = projection(coord);
        if (pt && isFinite(pt[0]) && isFinite(pt[1])) {
          const name = getCityName(currentDest);
          drawCityPin(pt[0], pt[1], name, palette.dest, palette.destAura, 'right');
        }
      }
    }
  }

  // Draw spherical midpoint marker
  function drawMidpointMarker(x, y) {
    ctx.save();
    // Midpoint ring
    ctx.beginPath();
    ctx.arc(x, y, 6, 0, Math.PI * 2);
    ctx.strokeStyle = palette.midpointAura;
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // Crosshair ticks
    ctx.beginPath();
    ctx.strokeStyle = palette.geodesic;
    ctx.lineWidth = 1;
    ctx.moveTo(x - 9, y);
    ctx.lineTo(x - 3, y);
    ctx.moveTo(x + 3, y);
    ctx.lineTo(x + 9, y);
    ctx.moveTo(x, y - 9);
    ctx.lineTo(x, y - 3);
    ctx.moveTo(x, y + 3);
    ctx.lineTo(x, y + 9);
    ctx.stroke();

    // Center dot
    ctx.beginPath();
    ctx.arc(x, y, 1.5, 0, Math.PI * 2);
    ctx.fillStyle = palette.midpoint;
    ctx.fill();
    ctx.restore();
  }

  // Draw waypoint city pin with badge label
  function drawCityPin(x, y, name, color, haloColor, align = 'right') {
    ctx.save();
    // Glowing aura
    ctx.beginPath();
    ctx.arc(x, y, 8, 0, Math.PI * 2);
    ctx.fillStyle = haloColor;
    ctx.fill();

    // Outer circle
    ctx.beginPath();
    ctx.arc(x, y, 4.5, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
    ctx.lineWidth = 1.2;
    ctx.strokeStyle = '#ffffff';
    ctx.stroke();

    // Core dot
    ctx.beginPath();
    ctx.arc(x, y, 1.5, 0, Math.PI * 2);
    ctx.fillStyle = '#ffffff';
    ctx.fill();

    // Typography badge label
    if (name) {
      ctx.font = "600 11px 'Space Grotesk', 'Inter', -apple-system, sans-serif";
      ctx.textBaseline = 'middle';
      const textWidth = ctx.measureText(name).width;
      const padX = 6;
      const badgeH = 18;
      const badgeW = textWidth + padX * 2;

      let badgeX = align === 'left' ? x - 8 - badgeW : x + 8;
      // Clamp within canvas bounds
      if (badgeX < 4) badgeX = x + 8;
      if (badgeX + badgeW > width - 4) badgeX = x - 8 - badgeW;

      const badgeY = y - badgeH / 2;

      // Dark card pill
      ctx.fillStyle = 'rgba(13, 20, 36, 0.9)';
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;

      ctx.beginPath();
      if (typeof ctx.roundRect === 'function') {
        ctx.roundRect(badgeX, badgeY, badgeW, badgeH, 4);
      } else {
        ctx.rect(badgeX, badgeY, badgeW, badgeH);
      }
      ctx.fill();
      ctx.stroke();

      ctx.fillStyle = '#f8fafc';
      ctx.fillText(name, badgeX + padX, y);
    }
    ctx.restore();
  }

  // Smooth spherical transition
  function animateRotationTo(targetYaw, targetPitch, duration = 850) {
    if (animFrameId) {
      cancelAnim(animFrameId);
      animFrameId = null;
    }

    const startRot = projection.rotate();
    const startYaw = startRot[0] === 0 ? 0 : startRot[0];
    const startPitch = startRot[1] === 0 ? 0 : startRot[1];

    // Wrap delta yaw to take the shortest angular path along the minor arc
    let deltaYaw = (targetYaw - startYaw) % 360;
    if (deltaYaw > 180) deltaYaw -= 360;
    if (deltaYaw < -180) deltaYaw += 360;

    const deltaPitch = targetPitch - startPitch;

    if (Math.abs(deltaYaw) < 0.01 && Math.abs(deltaPitch) < 0.01) {
      projection.rotate([targetYaw, targetPitch, 0]);
      render();
      return;
    }

    const startTime = typeof performance !== 'undefined' ? performance.now() : Date.now();

    function step(now) {
      const elapsed = now - startTime;
      const t = Math.min(1, Math.max(0, elapsed / duration));
      // Cubic ease-in-out
      const ease = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;

      let curYaw = startYaw + deltaYaw * ease;
      let curPitch = startPitch + deltaPitch * ease;
      if (Math.abs(curYaw) < 1e-9) curYaw = 0;
      if (Math.abs(curPitch) < 1e-9) curPitch = 0;

      projection.rotate([curYaw, curPitch, 0]);
      render();

      if (typeof options.onRotate === 'function') {
        options.onRotate(projection.rotate());
      }

      if (t < 1) {
        animFrameId = reqAnim(step);
      } else {
        animFrameId = null;
        projection.rotate([targetYaw, targetPitch, 0]);
        render();
      }
    }

    animFrameId = reqAnim(step);
  }

  // Rotate to specific longitude / latitude coordinate
  function rotateTo(lng, lat, animate = true) {
    const targetYaw = lng === 0 ? 0 : -lng;
    const targetPitch = lat === 0 ? 0 : -lat;

    if (animate) {
      animateRotationTo(targetYaw, targetPitch);
    } else {
      if (animFrameId) {
        cancelAnim(animFrameId);
        animFrameId = null;
      }
      projection.rotate([targetYaw, targetPitch, 0]);
      render();
      if (typeof options.onRotate === 'function') {
        options.onRotate(projection.rotate());
      }
    }
  }

  // Recenter orthographic view on route midpoint
  function recenter() {
    if (currentMidpoint) {
      rotateTo(currentMidpoint.lng, currentMidpoint.lat, true);
    } else {
      const initLng = initialRot[0] === 0 ? 0 : -initialRot[0];
      const initLat = initialRot[1] === 0 ? 0 : -initialRot[1];
      rotateTo(initLng, initLat, true);
    }
  }

  // Update displayed route
  function setRoute(geodesicResult, originCity, destCity, animate = true) {
    currentGeodesic = geodesicResult;
    currentOrigin = originCity;
    currentDest = destCity;

    if (geodesicResult) {
      currentMidpoint = normalizeMidpoint(geodesicResult.sphericalMidpoint);

      if (geodesicResult.pathCoordinates && geodesicResult.pathCoordinates.length >= 2) {
        currentRouteGeoJson = {
          type: 'LineString',
          coordinates: geodesicResult.pathCoordinates
        };
      } else {
        const originCoord = getCityCoords(originCity);
        const destCoord = getCityCoords(destCity);
        if (originCoord && destCoord) {
          currentRouteGeoJson = {
            type: 'LineString',
            coordinates: [originCoord, destCoord]
          };
        } else {
          currentRouteGeoJson = null;
        }
      }
    } else {
      currentMidpoint = null;
      currentRouteGeoJson = null;
    }

    if (currentMidpoint && animate) {
      rotateTo(currentMidpoint.lng, currentMidpoint.lat, true);
    } else if (currentMidpoint && !animate) {
      rotateTo(currentMidpoint.lng, currentMidpoint.lat, false);
    } else {
      render();
    }
  }

  // Resize canvas and update projection viewport
  function resize(newWidth, newHeight) {
    if (typeof newWidth === 'number' && typeof newHeight === 'number') {
      width = newWidth;
      height = newHeight;
    } else if (containerElement && containerElement.clientWidth && containerElement.clientHeight) {
      width = containerElement.clientWidth;
      height = containerElement.clientHeight;
    }
    minScale = Math.min(width, height) * 0.2;
    maxScale = Math.min(width, height) * 3.5;

    updateCanvasDimensions();
    render();
  }

  // Interactivity: Drag to rotate
  let isDragging = false;
  let dragStartX = 0;
  let dragStartY = 0;
  let dragStartRot = [0, 0, 0];

  function onMouseDown(e) {
    if (e.button !== 0) return;
    if (animFrameId) {
      cancelAnim(animFrameId);
      animFrameId = null;
    }
    isDragging = true;
    dragStartX = e.clientX;
    dragStartY = e.clientY;
    dragStartRot = projection.rotate().slice();
    canvas.style.cursor = 'grabbing';

    if (typeof window !== 'undefined') {
      window.addEventListener('mousemove', onMouseMove);
      window.addEventListener('mouseup', onMouseUp);
    }
  }

  function onMouseMove(e) {
    if (!isDragging) return;
    const dx = e.clientX - dragStartX;
    const dy = e.clientY - dragStartY;

    const k = 75 / projection.scale();
    let newYaw = dragStartRot[0] + dx * k;
    let newPitch = dragStartRot[1] - dy * k;
    newPitch = Math.max(-89, Math.min(89, newPitch));

    if (Math.abs(newYaw) < 1e-9) newYaw = 0;
    if (Math.abs(newPitch) < 1e-9) newPitch = 0;

    projection.rotate([newYaw, newPitch, 0]);
    render();

    if (typeof options.onRotate === 'function') {
      options.onRotate(projection.rotate());
    }
  }

  function onMouseUp() {
    if (!isDragging) return;
    isDragging = false;
    canvas.style.cursor = 'grab';
    if (typeof window !== 'undefined') {
      window.removeEventListener('mousemove', onMouseMove);
      window.removeEventListener('mouseup', onMouseUp);
    }
  }

  // Interactivity: Touch drag & Pinch zoom
  let isPinching = false;
  let touchStartX = 0;
  let touchStartY = 0;
  let touchStartRot = [0, 0, 0];
  let touchStartDist = 0;
  let touchStartScale = 0;

  function onTouchStart(e) {
    if (animFrameId) {
      cancelAnim(animFrameId);
      animFrameId = null;
    }

    if (e.touches.length === 1) {
      isDragging = true;
      isPinching = false;
      touchStartX = e.touches[0].clientX;
      touchStartY = e.touches[0].clientY;
      touchStartRot = projection.rotate().slice();
    } else if (e.touches.length === 2) {
      isDragging = false;
      isPinching = true;
      const dx = e.touches[0].clientX - e.touches[1].clientX;
      const dy = e.touches[0].clientY - e.touches[1].clientY;
      touchStartDist = Math.hypot(dx, dy);
      touchStartScale = projection.scale();
    }
  }

  function onTouchMove(e) {
    if (e.cancelable) e.preventDefault();

    if (e.touches.length === 1 && isDragging) {
      const dx = e.touches[0].clientX - touchStartX;
      const dy = e.touches[0].clientY - touchStartY;

      const k = 75 / projection.scale();
      let newYaw = touchStartRot[0] + dx * k;
      let newPitch = touchStartRot[1] - dy * k;
      newPitch = Math.max(-89, Math.min(89, newPitch));

      if (Math.abs(newYaw) < 1e-9) newYaw = 0;
      if (Math.abs(newPitch) < 1e-9) newPitch = 0;

      projection.rotate([newYaw, newPitch, 0]);
      render();

      if (typeof options.onRotate === 'function') {
        options.onRotate(projection.rotate());
      }
    } else if (e.touches.length === 2 && isPinching && touchStartDist > 0) {
      const dx = e.touches[0].clientX - e.touches[1].clientX;
      const dy = e.touches[0].clientY - e.touches[1].clientY;
      const dist = Math.hypot(dx, dy);
      const factor = dist / touchStartDist;
      const newScale = Math.max(minScale, Math.min(maxScale, touchStartScale * factor));

      projection.scale(newScale);
      render();

      if (typeof options.onZoom === 'function') {
        options.onZoom(newScale);
      }
    }
  }

  function onTouchEnd(e) {
    if (e.touches.length === 0) {
      isDragging = false;
      isPinching = false;
      touchStartDist = 0;
    } else if (e.touches.length === 1) {
      // Transition from pinch to single-touch drag
      isPinching = false;
      isDragging = true;
      touchStartX = e.touches[0].clientX;
      touchStartY = e.touches[0].clientY;
      touchStartRot = projection.rotate().slice();
    }
  }

  // Interactivity: Wheel zoom
  function onWheel(e) {
    if (e.cancelable) e.preventDefault();
    const factor = e.deltaY < 0 ? 1.12 : 0.89;
    const nextScale = Math.max(minScale, Math.min(maxScale, projection.scale() * factor));

    projection.scale(nextScale);
    render();

    if (typeof options.onZoom === 'function') {
      options.onZoom(nextScale);
    }
  }

  // Attach event listeners
  canvas.addEventListener('mousedown', onMouseDown);
  canvas.addEventListener('touchstart', onTouchStart, { passive: false });
  canvas.addEventListener('touchmove', onTouchMove, { passive: false });
  canvas.addEventListener('touchend', onTouchEnd);
  canvas.addEventListener('touchcancel', onTouchEnd);
  canvas.addEventListener('wheel', onWheel, { passive: false });

  // Optional ResizeObserver
  let resizeObserver = null;
  if (typeof ResizeObserver !== 'undefined' && containerElement && !options.fixedSize) {
    resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const cr = entry.contentRect;
        if (cr.width > 0 && cr.height > 0) {
          resize(cr.width, cr.height);
        }
      }
    });
    resizeObserver.observe(containerElement);
  }

  // Initial draw
  render();

  // Teardown routine
  function destroy() {
    if (animFrameId) {
      cancelAnim(animFrameId);
      animFrameId = null;
    }

    canvas.removeEventListener('mousedown', onMouseDown);
    canvas.removeEventListener('touchstart', onTouchStart);
    canvas.removeEventListener('touchmove', onTouchMove);
    canvas.removeEventListener('touchend', onTouchEnd);
    canvas.removeEventListener('touchcancel', onTouchEnd);
    canvas.removeEventListener('wheel', onWheel);

    if (typeof window !== 'undefined') {
      window.removeEventListener('mousemove', onMouseMove);
      window.removeEventListener('mouseup', onMouseUp);
    }

    if (resizeObserver) {
      resizeObserver.disconnect();
      resizeObserver = null;
    }

    if (canvas.parentElement) {
      canvas.parentElement.removeChild(canvas);
    }
  }

  return {
    setRoute,
    recenter,
    rotateTo,
    resize,
    destroy,
    render,
    getRotation: () => projection.rotate().slice(),
    getScale: () => projection.scale(),
    getCanvas: () => canvas
  };
}
