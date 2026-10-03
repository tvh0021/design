/**
 * Geodesic Map - Interactive Orthographic & Robinson Globe Renderer
 *
 * Renders an orthographic Earth globe or 2D Robinson flat map onto an HTML5 Canvas
 * using D3.js (d3-geo, topojson-client) and GPU-accelerated WebGL physical ETOPO relief
 * terrain with crisp sovereign country borders.
 * Supports toggle between 3D Orthographic and 2D Robinson (centered on route midpoint),
 * mouse drag rotation/panning, pinch / wheel zoom, smooth slerp transitions,
 * glowing great-circle geodesic paths, sovereign borders, graticules, and city waypoint pins.
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
  if (typeof mid.lng === 'number' && typeof mid.lat === 'number') {
    return mid;
  }
  if (typeof mid.lon === 'number' && typeof mid.lat === 'number') {
    return { lat: mid.lat, lng: mid.lon };
  }
  if (Array.isArray(mid) && mid.length >= 2) {
    return { lat: mid[1], lng: mid[0] };
  }
  return null;
}

/**
 * Determines whether a given [lng, lat] coordinate is visible.
 * In orthographic mode, checks if it lies on the visible front hemisphere (angular distance <= 90°).
 * In Robinson mode, checks if it lies within latitude [-90, 90] and projected viewport bounds.
 */
function isCoordVisible(coord, proj, type = 'orthographic', vpWidth = 800, vpHeight = 600) {
  if (!coord || !proj) return false;
  if (type === 'robinson') {
    if (Math.abs(coord[1]) > 90) return false;
    const pt = proj(coord);
    if (!pt || !isFinite(pt[0]) || !isFinite(pt[1])) return false;
    return pt[0] >= -80 && pt[0] <= vpWidth + 80 && pt[1] >= -80 && pt[1] <= vpHeight + 80;
  }

  const rot = proj.rotate();
  const centerLng = rot[0] === 0 ? 0 : -rot[0];
  const centerLat = rot[1] === 0 ? 0 : -rot[1];

  const toRad = Math.PI / 180;
  const phi0 = centerLat * toRad;
  const lam0 = centerLng * toRad;
  const phi = coord[1] * toRad;
  const lam = coord[0] * toRad;

  const cosDist =
    Math.sin(phi0) * Math.sin(phi) +
    Math.cos(phi0) * Math.cos(phi) * Math.cos(lam - lam0);

  return cosDist >= -1e-9;
}

/**
 * Interpolates two 3D points on a unit sphere using SLERP.
 */
function slerp3D(v0, v1, t) {
  let dot = v0[0] * v1[0] + v0[1] * v1[1] + v0[2] * v1[2];
  dot = Math.max(-1, Math.min(1, dot));

  if (dot > 0.9995) {
    const res = [
      v0[0] + t * (v1[0] - v0[0]),
      v0[1] + t * (v1[1] - v0[1]),
      v0[2] + t * (v1[2] - v0[2])
    ];
    const n = Math.hypot(res[0], res[1], res[2]) || 1;
    return [res[0] / n, res[1] / n, res[2] / n];
  }

  const theta0 = Math.acos(dot);
  const theta = theta0 * t;
  const sinTheta = Math.sin(theta);
  const sinTheta0 = Math.sin(theta0);

  const s0 = Math.cos(theta) - (dot * sinTheta) / sinTheta0;
  const s1 = sinTheta / sinTheta0;

  return [
    s0 * v0[0] + s1 * v1[0],
    s0 * v0[1] + s1 * v1[1],
    s0 * v0[2] + s1 * v1[2]
  ];
}

/**
 * Converts geographic [lng, lat] in degrees to 3D Cartesian coordinates on unit sphere.
 */
function geoToCartesian(lng, lat) {
  const toRad = Math.PI / 180;
  const phi = lat * toRad;
  const lam = lng * toRad;
  return [
    Math.cos(phi) * Math.cos(lam),
    Math.cos(phi) * Math.sin(lam),
    Math.sin(phi)
  ];
}

/**
 * Converts 3D Cartesian coordinates to [lng, lat] in degrees.
 */
function cartesianToGeo(v) {
  const toDeg = 180 / Math.PI;
  const lat = Math.asin(Math.max(-1, Math.min(1, v[2]))) * toDeg;
  const lng = Math.atan2(v[1], v[0]) * toDeg;
  return [lng, lat];
}

/**
 * WebGL Physical Terrain Layer
 * Raymarches NOAA ETOPO1 relief on a true sphere beneath vector borders.
 */
function initWebGLTerrain(container, parentCanvas, width, height, dpr, textureUrl, onLoaded) {
  if (typeof document === 'undefined') return null;

  const glCanvas = document.createElement('canvas');
  glCanvas.className = 'globe-webgl-terrain';
  glCanvas.style.position = 'absolute';
  glCanvas.style.top = '0';
  glCanvas.style.left = '0';
  glCanvas.style.width = `${width}px`;
  glCanvas.style.height = `${height}px`;
  glCanvas.style.pointerEvents = 'none';
  glCanvas.style.zIndex = '0';

  if (parentCanvas && parentCanvas.parentElement) {
    parentCanvas.parentElement.insertBefore(glCanvas, parentCanvas);
  } else if (container) {
    container.appendChild(glCanvas);
  }

  const gl = glCanvas.getContext('webgl', { alpha: true, antialias: true, premultipliedAlpha: false });
  if (!gl) {
    if (glCanvas.parentElement) glCanvas.parentElement.removeChild(glCanvas);
    return null;
  }

  const vsSource = `
    attribute vec2 a_position;
    varying vec2 v_uv;
    void main() {
      v_uv = a_position * 0.5 + 0.5;
      gl_Position = vec4(a_position, 0.0, 1.0);
    }
  `;

  const fsSource = `
    precision mediump float;
    varying vec2 v_uv;

    uniform sampler2D u_texture;
    uniform vec2 u_resolution;
    uniform vec2 u_center;
    uniform float u_radius;
    uniform mat3 u_rotMatrix;
    uniform float u_loaded;

    #define PI 3.141592653589793

    void main() {
      if (u_loaded < 0.5) {
        discard;
      }
      vec2 screenCoord = vec2(v_uv.x * u_resolution.x, (1.0 - v_uv.y) * u_resolution.y);
      vec2 p = (screenCoord - u_center) / u_radius;
      float r2 = dot(p, p);
      if (r2 > 1.0) {
        discard;
      }

      float z = sqrt(1.0 - r2);
      vec3 sphereNorm = vec3(p.x, -p.y, z);
      // The unflipped texture has north at v=0, so its inverse rotation
      // uses screen-down Y. Lighting still uses the screen-up normal.
      vec3 rotatedNorm = u_rotMatrix * vec3(p.x, p.y, z);

      float phi = asin(clamp(rotatedNorm.y, -1.0, 1.0));
      float lambda = atan(rotatedNorm.x, rotatedNorm.z);

      float texU = fract((lambda / (2.0 * PI)) + 0.5);
      float texV = clamp((phi / PI) + 0.5, 0.001, 0.999);

      vec4 texColor = texture2D(u_texture, vec2(texU, texV));

      vec3 lightDir = normalize(vec3(0.3, 0.5, 0.8));
      float diff = clamp(dot(sphereNorm, lightDir), 0.0, 1.0) * 0.45 + 0.55;

      float edge = smoothstep(0.99, 0.94, r2);
      float rim = pow(1.0 - z, 3.0) * 0.28;
      vec3 rimColor = vec3(0.02, 0.71, 0.83);

      vec3 color = texColor.rgb * diff + rimColor * rim;
      gl_FragColor = vec4(color, edge * 0.88);
    }
  `;

  function createShader(type, src) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, src);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      gl.deleteShader(shader);
      return null;
    }
    return shader;
  }

  const vs = createShader(gl.VERTEX_SHADER, vsSource);
  const fs = createShader(gl.FRAGMENT_SHADER, fsSource);
  if (!vs || !fs) return null;

  const program = gl.createProgram();
  gl.attachShader(program, vs);
  gl.attachShader(program, fs);
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) return null;

  gl.useProgram(program);

  const quad = new Float32Array([
    -1, -1,
     1, -1,
    -1,  1,
    -1,  1,
     1, -1,
     1,  1
  ]);
  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, quad, gl.STATIC_DRAW);

  const posLoc = gl.getAttribLocation(program, 'a_position');
  gl.enableVertexAttribArray(posLoc);
  gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 0, 0);

  const uRes = gl.getUniformLocation(program, 'u_resolution');
  const uCenter = gl.getUniformLocation(program, 'u_center');
  const uRadius = gl.getUniformLocation(program, 'u_radius');
  const uRotMat = gl.getUniformLocation(program, 'u_rotMatrix');
  const uLoaded = gl.getUniformLocation(program, 'u_loaded');

  const texture = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, texture);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([13, 20, 36, 255]));

  let isLoaded = false;
  const img = new Image();
  img.crossOrigin = 'anonymous';
  img.onload = () => {
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    isLoaded = true;
    if (typeof onLoaded === 'function') onLoaded();
  };
  img.onerror = () => {
    isLoaded = false;
  };
  img.src = textureUrl;

  function resize(newW, newH, newDpr) {
    glCanvas.width = Math.round(newW * newDpr);
    glCanvas.height = Math.round(newH * newDpr);
    glCanvas.style.width = `${newW}px`;
    glCanvas.style.height = `${newH}px`;
    gl.viewport(0, 0, glCanvas.width, glCanvas.height);
  }

  resize(width, height, dpr);

  function getEulerRotationMatrix(yawDeg, pitchDeg, rollDeg) {
    const toRad = Math.PI / 180;
    const y = yawDeg * toRad;
    const p = pitchDeg * toRad;
    const r = rollDeg * toRad;

    const cy = Math.cos(y), sy = Math.sin(y);
    const cp = Math.cos(p), sp = Math.sin(p);
    const cr = Math.cos(r), sr = Math.sin(r);

    const m00 = cy * cr + sy * sp * sr;
    const m01 = -cy * sr + sy * sp * cr;
    const m02 = sy * cp;

    const m10 = cp * sr;
    const m11 = cp * cr;
    const m12 = -sp;

    const m20 = -sy * cr + cy * sp * sr;
    const m21 = sy * sr + cy * sp * cr;
    const m22 = cy * cp;

    return new Float32Array([
      m00, m10, m20,
      m01, m11, m21,
      m02, m12, m22
    ]);
  }

  function render(w, h, currentDpr, cx, cy, r, rot) {
    if (!isLoaded) return;
    gl.viewport(0, 0, glCanvas.width, glCanvas.height);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);

    gl.useProgram(program);
    gl.uniform2f(uRes, w * currentDpr, h * currentDpr);
    gl.uniform2f(uCenter, cx * currentDpr, cy * currentDpr);
    gl.uniform1f(uRadius, r * currentDpr);
    gl.uniform1f(uLoaded, isLoaded ? 1.0 : 0.0);

    const rotMat = getEulerRotationMatrix(-rot[0], -rot[1], rot[2] || 0);
    gl.uniformMatrix3fv(uRotMat, false, rotMat);

    gl.drawArrays(gl.TRIANGLES, 0, 6);
  }

  function destroy() {
    if (glCanvas.parentElement) {
      glCanvas.parentElement.removeChild(glCanvas);
    }
  }

  return {
    glCanvas,
    resize,
    render,
    destroy,
    isLoaded: () => isLoaded
  };
}

/**
 * Creates and initializes an interactive Orthographic / Robinson Earth globe renderer.
 *
 * @param {HTMLElement} containerElement
 * @param {object} worldTopology TopoJSON world topology
 * @param {object} [options]
 * @returns {object} Controller API
 */
export function createGlobeRenderer(containerElement, worldTopology, options = {}) {
  const { d3, topojson } = resolveDependencies(options);

  if (!d3 || typeof d3.geoOrthographic !== 'function') {
    throw new Error(
      'D3 (d3-geo) is required to create a map renderer. Provide it in options.d3 or load d3 globally.'
    );
  }

  // Theme Palette
  const palette = {
    spaceBg: '#070a13',
    ocean: '#0d1424',
    land: '#1e293b',
    landStroke: 'rgba(255, 255, 255, 0.12)',
    countryBorder: 'rgba(255, 255, 255, 0.28)',
    graticule: 'rgba(255, 255, 255, 0.06)',
    haloOuter: 'rgba(6, 182, 212, 0.22)',
    haloMid: 'rgba(6, 182, 212, 0.08)',
    haloBorder: 'rgba(6, 182, 212, 0.45)',
    geodesic: '#06b6d4',
    geodesicGlow: 'rgba(6, 182, 212, 0.35)',
    geodesicInnerGlow: 'rgba(6, 182, 212, 0.70)',
    origin: '#f59e0b',
    originAura: 'rgba(245, 158, 11, 0.35)',
    dest: '#10b981',
    destAura: 'rgba(16, 185, 129, 0.35)',
    midpoint: '#38bdf8',
    midpointAura: 'rgba(56, 189, 248, 0.35)',
    pinTextBg: 'rgba(7, 10, 19, 0.88)',
    pinTextBorder: 'rgba(255, 255, 255, 0.22)',
    pinText: '#f8fafc',
    ...(options.palette || {})
  };

  // Dimensions
  let width = options.width || (containerElement ? containerElement.clientWidth : 800) || 800;
  let height = options.height || (containerElement ? containerElement.clientHeight : 600) || 600;

  // Mount primary HTML5 Canvas
  const doc = options.document || (typeof document !== 'undefined' ? document : null);
  let canvas = options.canvas || null;

  if (!canvas && doc) {
    canvas = doc.createElement('canvas');
  }

  if (canvas) {
    if (typeof canvas.className === 'string') {
      canvas.className = 'geodesic-globe-canvas';
    }
    if (canvas.style) {
      canvas.style.display = 'block';
      canvas.style.cursor = 'grab';
      canvas.style.position = 'relative';
      canvas.style.zIndex = '1';
    }

    if (containerElement && canvas.parentElement !== containerElement && typeof containerElement.appendChild === 'function') {
      containerElement.appendChild(canvas);
    }
  } else {
    canvas = {
      width,
      height,
      style: {},
      getContext: () => null,
      addEventListener: () => {},
      removeEventListener: () => {}
    };
  }

  const ctx = canvas.getContext ? canvas.getContext('2d') : null;

  // Extract Landmass & Sovereign Country Borders from TopoJSON
  let landFeature = null;
  let countryBordersFeature = null;

  if (worldTopology && topojson && typeof topojson.feature === 'function') {
    if (worldTopology.objects) {
      if (worldTopology.objects.land) {
        landFeature = topojson.feature(worldTopology, worldTopology.objects.land);
      }
      if (worldTopology.objects.countries) {
        if (!landFeature) {
          landFeature = topojson.feature(worldTopology, worldTopology.objects.countries);
        }
        try {
          if (typeof topojson.mesh === 'function') {
            countryBordersFeature = topojson.mesh(
              worldTopology,
              worldTopology.objects.countries,
              (a, b) => a !== b
            );
          }
        } catch (e) {
          countryBordersFeature = null;
        }
      }
    } else if (worldTopology.type === 'FeatureCollection' || worldTopology.type === 'Feature') {
      landFeature = worldTopology;
    }
  }

  // Pre-generate graticule (parallels and meridians)
  let graticuleFeature = null;
  if (typeof d3.geoGraticule10 === 'function') {
    graticuleFeature = d3.geoGraticule10();
  } else if (typeof d3.geoGraticule === 'function') {
    graticuleFeature = d3.geoGraticule()();
  }

  // Projection setup
  let projectionType = options.projectionType || 'orthographic'; // 'orthographic' | 'robinson'
  let verticalCenterRatio = typeof options.verticalCenterRatio === 'number' ? options.verticalCenterRatio : 0.5;

  let minScale = Math.min(width, height) * 0.2;
  let maxScale = Math.min(width, height) * 3.5;
  let currentScale = options.scale || Math.min(width, height) * 0.42;
  const initialRot = options.initialRotation || [0, -20, 0];

  const orthoProj = d3.geoOrthographic()
    .clipAngle(90)
    .precision(0.3)
    .scale(currentScale)
    .translate([width / 2, height * verticalCenterRatio])
    .rotate(initialRot.slice());

  let robinsonViewportInsets = { left: 0, right: 0, top: 0, bottom: 0 };
  let robinsonRouteDistance = 0;
  // D3's Robinson unit sphere spans 2π horizontally and 3.18683 vertically.
  function worldRobinsonScale() {
    const availableWidth = Math.max(1, width - robinsonViewportInsets.left - robinsonViewportInsets.right - 48);
    const availableHeight = Math.max(1, height - robinsonViewportInsets.top - robinsonViewportInsets.bottom - 48);
    return Math.max(1, Math.min(availableWidth / (2 * Math.PI), availableHeight / 3.1868315878));
  }
  function defaultRobinsonScale() {
    const availableWidth = Math.max(1, width - robinsonViewportInsets.left - robinsonViewportInsets.right - 48);
    const availableHeight = Math.max(1, height - robinsonViewportInsets.top - robinsonViewportInsets.bottom - 48);
    if (robinsonRouteDistance > 0 && robinsonRouteDistance < 1500) {
      // A world overview makes nearby city pins overlap. Open short routes at
      // a regional scale, within the same zoom range as the wheel/pinch controls.
      return Math.min(Math.min(width, height) * 4,
        Math.min(availableWidth, availableHeight) * 6371.0088 / robinsonRouteDistance * 0.55);
    }
    return worldRobinsonScale();
  }
  let robinsonScale = options.robinsonScale || defaultRobinsonScale();
  let minRobinsonScale = worldRobinsonScale() * 0.4;
  let maxRobinsonScale = Math.min(width, height) * 4.0;
  let robinsonCenterLng = 0;
  let robinsonCenterLat = 0;

  let robinsonProj = null;
  if (typeof d3.geoRobinson === 'function') {
    robinsonProj = d3.geoRobinson()
      .precision(0.3)
      .scale(robinsonScale)
      .translate([width / 2, height * verticalCenterRatio])
      .rotate([-robinsonCenterLng, 0, 0])
      .center([0, robinsonCenterLat]);
  }

  function getActiveProjection() {
    return projectionType === 'robinson' && robinsonProj ? robinsonProj : orthoProj;
  }

  // Initialize WebGL ETOPO terrain layer if enabled
  const terrainEnabled = options.terrain !== false;
  const terrainUrl = options.terrainTextureUrl || 'data/etopo.jpg';
  let webglTerrain = null;

  if (terrainEnabled) {
    const initDpr = typeof window !== 'undefined' ? (window.devicePixelRatio || 1) : 1;
    webglTerrain = initWebGLTerrain(containerElement, canvas, width, height, initDpr, terrainUrl, () => {
      render();
    });
  }

  // Application state
  let currentGeodesic = null;
  let currentOrigin = null;
  let currentDest = null;
  let currentRouteGeoJson = null;
  let currentMidpoint = null;

  function robinsonTranslation() {
    const { left, right, top, bottom } = robinsonViewportInsets;
    return [(width + left - right) / 2, (height + top - bottom) / 2];
  }

  function setRobinsonViewportInsets(insets = {}) {
    const zoomRatio = robinsonScale / defaultRobinsonScale();
    robinsonViewportInsets = {
      left: Math.max(0, insets.left || 0), right: Math.max(0, insets.right || 0),
      top: Math.max(0, insets.top || 0), bottom: Math.max(0, insets.bottom || 0)
    };
    robinsonScale = defaultRobinsonScale() * zoomRatio;
    minRobinsonScale = worldRobinsonScale() * 0.4;
    if (robinsonProj) robinsonProj.scale(robinsonScale).translate(robinsonTranslation());
    if (projectionType === 'robinson') render();
  }

  // A polar fallback path includes both endpoints and the North Pole. Center
  // its smallest enclosing hemisphere, rather than hiding a southern endpoint.
  function orthographicRouteCenter() {
    if (!currentGeodesic?.isAntipodal || !currentOrigin || !currentDest) return currentMidpoint;
    const origin = getCityCoords(currentOrigin), dest = getCityCoords(currentDest);
    if (!origin || !dest) return currentMidpoint;
    const a = geoToCartesian(...origin), b = geoToCartesian(...dest), pole = [0, 0, 1];
    const u = a.map((v, i) => v - b[i]);
    const v = a.map((value, i) => value - pole[i]);
    let normal = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]];
    const length = Math.hypot(...normal);
    if (length < 1e-10) return currentMidpoint;
    normal = normal.map(value => value / length);
    const dot = normal.reduce((sum, value, i) => sum + value * a[i], 0);
    if (Math.abs(dot) < 1e-10) return currentMidpoint;
    if (dot < 0) normal = normal.map(value => -value);
    const [lng, lat] = cartesianToGeo(normal);
    return { lng, lat };
  }

  // Configure high-DPI scaling
  function updateCanvasDimensions() {
    const dpr = typeof window !== 'undefined' ? (window.devicePixelRatio || 1) : 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;

    if (ctx && typeof ctx.setTransform === 'function') {
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.scale(dpr, dpr);
    }

    orthoProj.translate([width / 2, height * verticalCenterRatio]);
    if (robinsonProj) {
      robinsonProj.translate(robinsonTranslation());
    }

    if (webglTerrain) {
      webglTerrain.resize(width, height, dpr);
    }
  }

  updateCanvasDimensions();

  // Rendering routines
  function render() {
    if (!ctx) return;
    const dpr = typeof window !== 'undefined' ? (window.devicePixelRatio || 1) : 1;
    const activeProj = getActiveProjection();
    const path = d3.geoPath(activeProj, ctx);

    const isRobinson = projectionType === 'robinson';
    const [cx, cy] = activeProj.translate();
    const r = activeProj.scale();
    const rot = activeProj.rotate();

    // Render WebGL physical terrain sphere only in orthographic mode
    const hasTerrain = !isRobinson && webglTerrain && webglTerrain.isLoaded();
    if (hasTerrain) {
      webglTerrain.render(width, height, dpr, cx, cy, r, rot);
    }

    ctx.clearRect(0, 0, width, height);

    if (isRobinson) {
      // 1. Draw the curved world outline, leaving space outside the map.
      ctx.save();
      ctx.beginPath();
      path({ type: 'Sphere' });
      ctx.fillStyle = palette.ocean;
      ctx.fill();
      ctx.lineWidth = 1.4;
      ctx.strokeStyle = palette.haloBorder;
      ctx.stroke();
      ctx.restore();
    } else {
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
      if (!hasTerrain) {
        ctx.fillStyle = palette.ocean;
        ctx.fill();
      }
      ctx.lineWidth = 1.4;
      ctx.strokeStyle = palette.haloBorder;
      ctx.stroke();
      ctx.restore();
    }

    // 3. Graticule lines
    if (graticuleFeature) {
      ctx.save();
      ctx.beginPath();
      path(graticuleFeature);
      ctx.strokeStyle = palette.graticule;
      ctx.lineWidth = isRobinson ? 0.5 : 0.65;
      ctx.stroke();
      ctx.restore();
    }

    // 4. Landmass polygons & coastlines
    if (landFeature) {
      ctx.save();
      ctx.beginPath();
      path(landFeature);
      if (!hasTerrain) {
        ctx.fillStyle = palette.land;
        ctx.fill();
      }
      ctx.strokeStyle = palette.landStroke;
      ctx.lineWidth = 0.85;
      ctx.stroke();
      ctx.restore();
    }

    // 5. Sovereign Country Borders
    if (countryBordersFeature) {
      ctx.save();
      ctx.beginPath();
      path(countryBordersFeature);
      ctx.strokeStyle = palette.countryBorder;
      ctx.lineWidth = 0.75;
      ctx.stroke();
      ctx.restore();
    }

    // 6. Geodesic flight path line (with illuminated cyan glow)
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

    // 7. Spherical midpoint marker
    if (currentMidpoint) {
      const midCoord = [currentMidpoint.lng, currentMidpoint.lat];
      if (isCoordVisible(midCoord, activeProj, projectionType, width, height)) {
        const pt = activeProj(midCoord);
        if (pt && isFinite(pt[0]) && isFinite(pt[1])) {
          drawMidpointMarker(pt[0], pt[1]);
        }
      }
    }

    // 8. City pins and labels (origin and destination)
    if (currentOrigin) {
      const coord = getCityCoords(currentOrigin);
      if (coord && isCoordVisible(coord, activeProj, projectionType, width, height)) {
        const pt = activeProj(coord);
        if (pt && isFinite(pt[0]) && isFinite(pt[1])) {
          const name = getCityName(currentOrigin);
          drawCityPin(pt[0], pt[1], name, palette.origin, palette.originAura, 'left');
        }
      }
    }

    if (currentDest) {
      const coord = getCityCoords(currentDest);
      if (coord && isCoordVisible(coord, activeProj, projectionType, width, height)) {
        const pt = activeProj(coord);
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
    ctx.beginPath();
    ctx.arc(x, y, 6, 0, Math.PI * 2);
    ctx.strokeStyle = palette.midpointAura;
    ctx.lineWidth = 1.5;
    ctx.stroke();

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

    ctx.beginPath();
    ctx.arc(x, y, 1.5, 0, Math.PI * 2);
    ctx.fillStyle = palette.midpoint;
    ctx.fill();
    ctx.restore();
  }

  // Draw waypoint city pin with badge label
  function drawCityPin(x, y, name, color, haloColor, align = 'right') {
    ctx.save();
    ctx.beginPath();
    ctx.arc(x, y, 8, 0, Math.PI * 2);
    ctx.fillStyle = haloColor;
    ctx.fill();

    ctx.beginPath();
    ctx.arc(x, y, 4.5, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = 1.2;
    ctx.stroke();

    if (name) {
      ctx.font = '600 11px system-ui, -apple-system, sans-serif';
      const textMetrics = ctx.measureText(name);
      const textW = textMetrics.width;
      const textH = 14;
      const padX = 7;
      const padY = 4;
      const badgeW = textW + padX * 2;
      const badgeH = textH + padY * 2;

      let badgeX;
      if (align === 'left') {
        badgeX = x - badgeW - 10;
      } else {
        badgeX = x + 10;
      }
      const badgeY = y - badgeH / 2;

      ctx.beginPath();
      if (typeof ctx.roundRect === 'function') {
        ctx.roundRect(badgeX, badgeY, badgeW, badgeH, 4);
      } else {
        ctx.rect(badgeX, badgeY, badgeW, badgeH);
      }
      ctx.fillStyle = palette.pinTextBg;
      ctx.fill();
      ctx.strokeStyle = palette.pinTextBorder;
      ctx.lineWidth = 0.8;
      ctx.stroke();

      ctx.fillStyle = palette.pinText;
      ctx.textBaseline = 'middle';
      ctx.textAlign = 'left';
      ctx.fillText(name, badgeX + padX, badgeY + badgeH / 2);
    }
    ctx.restore();
  }

  // Animation controller
  let animFrameId = null;
  const requestAnim = typeof requestAnimationFrame !== 'undefined'
    ? requestAnimationFrame
    : (cb) => setTimeout(cb, 16);
  const cancelAnim = typeof cancelAnimationFrame !== 'undefined'
    ? cancelAnimationFrame
    : (id) => clearTimeout(id);

  function rotateTo(targetLng, targetLat, animate = true) {
    if (animFrameId) {
      cancelAnim(animFrameId);
      animFrameId = null;
    }

    const targetYaw = targetLng === 0 ? 0 : -targetLng;
    const targetPitch = targetLat === 0 ? 0 : -targetLat;

    if (!animate) {
      orthoProj.rotate([targetYaw, targetPitch, 0]);
      render();
      if (typeof options.onRotate === 'function') {
        options.onRotate(orthoProj.rotate());
      }
      return;
    }

    const startRot = orthoProj.rotate().slice();
    const startYaw = startRot[0];
    const startPitch = startRot[1];

    let dYaw = ((targetYaw - startYaw + 540) % 360) - 180;
    const dPitch = targetPitch - startPitch;

    const startCenterLng = startYaw === 0 ? 0 : -startYaw;
    const startCenterLat = startPitch === 0 ? 0 : -startPitch;
    const vStart = geoToCartesian(startCenterLng, startCenterLat);
    const vEnd = geoToCartesian(targetLng, targetLat);

    const startTime = (typeof performance !== 'undefined' ? performance.now() : Date.now());
    const duration = options.animationDuration || 850;

    function frame(now) {
      const elapsed = now - startTime;
      const progress = Math.min(1, elapsed / duration);
      const ease = progress < 0.5
        ? 2 * progress * progress
        : -1 + (4 - 2 * progress) * progress;

      const vCurrent = slerp3D(vStart, vEnd, ease);
      const [currentLng, currentLat] = cartesianToGeo(vCurrent);

      const curYaw = currentLng === 0 ? 0 : -currentLng;
      const curPitch = currentLat === 0 ? 0 : -currentLat;

      orthoProj.rotate([curYaw, curPitch, 0]);
      render();

      if (typeof options.onRotate === 'function') {
        options.onRotate(orthoProj.rotate());
      }

      if (progress < 1) {
        animFrameId = requestAnim(frame);
      } else {
        orthoProj.rotate([targetYaw, targetPitch, 0]);
        animFrameId = null;
        render();
        if (typeof options.onRotate === 'function') {
          options.onRotate(orthoProj.rotate());
        }
      }
    }

    animFrameId = requestAnim(frame);
  }

  // Keep the north-up world outline in view and align its central meridian
  // with the route. Polar fallback midpoints use the endpoints' longitude.
  function centerRobinsonOnRoute() {
    if (!robinsonProj || !currentMidpoint) return;
    robinsonRouteDistance = currentGeodesic?.distanceKm || 0;
    let { lng, lat } = currentMidpoint;
    if (Math.abs(lat) > 80) {
      const origin = getCityCoords(currentOrigin);
      const dest = getCityCoords(currentDest);
      if (origin && dest) {
        const deltaLng = ((dest[0] - origin[0] + 540) % 360) - 180;
        lng = origin[0] + deltaLng / 2;
      }
    }
    robinsonCenterLng = ((lng + 540) % 360) - 180;
    robinsonCenterLat = robinsonRouteDistance > 0 && robinsonRouteDistance < 1500 ? currentMidpoint.lat : 0;
    robinsonScale = defaultRobinsonScale();
    robinsonProj.scale(robinsonScale)
      .rotate([-robinsonCenterLng, 0, 0]).center([0, robinsonCenterLat]);
  }

  // Recenter projection
  function recenter(animate = true) {
    if (!currentMidpoint) {
      if (projectionType === 'robinson') {
        if (robinsonProj) {
          robinsonCenterLng = 0;
          robinsonCenterLat = 0;
          robinsonRouteDistance = 0;
          robinsonScale = defaultRobinsonScale();
          robinsonProj.scale(robinsonScale).rotate([0, 0, 0]).center([0, 0]);
          render();
        }
      } else {
        rotateTo(0, 0, animate);
      }
      return;
    }

    if (projectionType === 'robinson') {
      if (robinsonProj) {
        robinsonScale = defaultRobinsonScale();
        centerRobinsonOnRoute();
        render();
      }
    } else {
      const center = orthographicRouteCenter();
      rotateTo(center.lng, center.lat, animate);
    }
  }

  // Switch projection type between 'orthographic' and 'robinson'
  function setProjectionType(type) {
    if (type !== 'orthographic' && type !== 'robinson') return;
    if (type === projectionType) return;
    projectionType = type;

    if (projectionType === 'robinson') {
      if (webglTerrain && webglTerrain.glCanvas) {
        webglTerrain.glCanvas.style.display = 'none';
      }
      if (robinsonProj && currentMidpoint) {
        centerRobinsonOnRoute();
      }
    } else {
      if (webglTerrain && webglTerrain.glCanvas && terrainEnabled) {
        webglTerrain.glCanvas.style.display = 'block';
      }
      if (currentMidpoint) {
        const center = orthographicRouteCenter();
        rotateTo(center.lng, center.lat, false);
      }
    }

    render();
    if (typeof options.onProjectionChange === 'function') {
      options.onProjectionChange(projectionType);
    }
  }

  // Set active geodesic flight route
  function setRoute(geodesicResult, originCity, destCity, animate = true) {
    currentGeodesic = geodesicResult || null;
    currentOrigin = originCity || null;
    currentDest = destCity || null;

    if (geodesicResult) {
      currentMidpoint = normalizeMidpoint(geodesicResult.sphericalMidpoint);
      if (geodesicResult.pathCoordinates && geodesicResult.pathCoordinates.length > 0) {
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

    if (projectionType === 'robinson') {
      if (currentMidpoint && robinsonProj) {
        centerRobinsonOnRoute();
      }
      render();
    } else {
      if (currentMidpoint && animate) {
        const center = orthographicRouteCenter();
        rotateTo(center.lng, center.lat, true);
      } else if (currentMidpoint && !animate) {
        const center = orthographicRouteCenter();
        rotateTo(center.lng, center.lat, false);
      } else {
        render();
      }
    }
  }

  // Adjust vertical projection center (e.g. upper region on mobile drawer)
  function setVerticalCenterRatio(ratio, animate = true) {
    const targetRatio = Math.max(0.1, Math.min(0.9, ratio));
    if (!animate || typeof requestAnimationFrame === 'undefined') {
      verticalCenterRatio = targetRatio;
      updateCanvasDimensions();
      render();
      return;
    }

    const startY = getActiveProjection().translate()[1];
    const targetY = height * targetRatio;
    const startTime = (typeof performance !== 'undefined' ? performance.now() : Date.now());
    const duration = 280;

    function anim(now) {
      const elapsed = now - startTime;
      const t = Math.min(1, elapsed / duration);
      const ease = t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;
      const currentY = startY + (targetY - startY) * ease;
      verticalCenterRatio = currentY / height;
      orthoProj.translate([width / 2, currentY]);
      if (robinsonProj) {
        robinsonProj.translate(robinsonTranslation());
      }
      render();
      if (t < 1) {
        requestAnimationFrame(anim);
      }
    }
    requestAnimationFrame(anim);
  }

  // Resize canvas and update projection viewport
  function resize(newWidth, newHeight) {
    const robinsonZoomRatio = robinsonScale / defaultRobinsonScale();
    if (typeof newWidth === 'number' && typeof newHeight === 'number') {
      width = newWidth;
      height = newHeight;
    } else if (containerElement && containerElement.clientWidth && containerElement.clientHeight) {
      width = containerElement.clientWidth;
      height = containerElement.clientHeight;
    }
    minScale = Math.min(width, height) * 0.2;
    maxScale = Math.min(width, height) * 3.5;
    robinsonScale = defaultRobinsonScale() * robinsonZoomRatio;
    minRobinsonScale = worldRobinsonScale() * 0.4;
    maxRobinsonScale = Math.min(width, height) * 4.0;
    if (robinsonProj) robinsonProj.scale(robinsonScale);

    updateCanvasDimensions();
    render();
  }

  // Interactivity: Drag to rotate (Orthographic) or pan (Robinson)
  let isDragging = false;
  let dragStartX = 0;
  let dragStartY = 0;
  let dragStartRot = [0, 0, 0];
  let dragStartRobinsonLng = 0;
  let dragStartRobinsonLat = 0;

  function onMouseDown(e) {
    if (e.button !== 0) return;
    if (animFrameId) {
      cancelAnim(animFrameId);
      animFrameId = null;
    }
    isDragging = true;
    dragStartX = e.clientX;
    dragStartY = e.clientY;
    dragStartRot = orthoProj.rotate().slice();
    dragStartRobinsonLng = robinsonCenterLng;
    dragStartRobinsonLat = robinsonCenterLat;
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

    if (projectionType === 'robinson' && robinsonProj) {
      const kLng = 180 / (Math.PI * robinsonScale);
      const kLat = 90 / (1.5934157939 * robinsonScale);
      robinsonCenterLng = ((dragStartRobinsonLng - dx * kLng + 540) % 360) - 180;
      robinsonCenterLat = Math.max(-80, Math.min(80, dragStartRobinsonLat + dy * kLat));
      robinsonProj.rotate([-robinsonCenterLng, 0, 0]).center([0, robinsonCenterLat]);
      render();
    } else {
      const k = 75 / orthoProj.scale();
      let newYaw = dragStartRot[0] + dx * k;
      let newPitch = dragStartRot[1] - dy * k;
      newPitch = Math.max(-89, Math.min(89, newPitch));

      if (Math.abs(newYaw) < 1e-9) newYaw = 0;
      if (Math.abs(newPitch) < 1e-9) newPitch = 0;

      orthoProj.rotate([newYaw, newPitch, 0]);
      render();

      if (typeof options.onRotate === 'function') {
        options.onRotate(orthoProj.rotate());
      }
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
      dragStartRot = orthoProj.rotate().slice();
      dragStartRobinsonLng = robinsonCenterLng;
      dragStartRobinsonLat = robinsonCenterLat;
    } else if (e.touches.length === 2) {
      isDragging = false;
      isPinching = true;
      const dx = e.touches[0].clientX - e.touches[1].clientX;
      const dy = e.touches[0].clientY - e.touches[1].clientY;
      touchStartDist = Math.hypot(dx, dy);
      touchStartScale = projectionType === 'robinson' ? robinsonScale : orthoProj.scale();
    }
  }

  function onTouchMove(e) {
    if (e.cancelable) e.preventDefault();

    if (e.touches.length === 1 && isDragging) {
      const dx = e.touches[0].clientX - touchStartX;
      const dy = e.touches[0].clientY - touchStartY;

      if (projectionType === 'robinson' && robinsonProj) {
        const kLng = 180 / (Math.PI * robinsonScale);
        const kLat = 90 / (1.5934157939 * robinsonScale);
        robinsonCenterLng = ((dragStartRobinsonLng - dx * kLng + 540) % 360) - 180;
        robinsonCenterLat = Math.max(-80, Math.min(80, dragStartRobinsonLat + dy * kLat));
        robinsonProj.rotate([-robinsonCenterLng, 0, 0]).center([0, robinsonCenterLat]);
        render();
      } else {
        const k = 75 / orthoProj.scale();
        let newYaw = dragStartRot[0] + dx * k;
        let newPitch = dragStartRot[1] - dy * k;
        newPitch = Math.max(-89, Math.min(89, newPitch));

        if (Math.abs(newYaw) < 1e-9) newYaw = 0;
        if (Math.abs(newPitch) < 1e-9) newPitch = 0;

        orthoProj.rotate([newYaw, newPitch, 0]);
        render();

        if (typeof options.onRotate === 'function') {
          options.onRotate(orthoProj.rotate());
        }
      }
    } else if (e.touches.length === 2 && isPinching && touchStartDist > 0) {
      const dx = e.touches[0].clientX - e.touches[1].clientX;
      const dy = e.touches[0].clientY - e.touches[1].clientY;
      const dist = Math.hypot(dx, dy);
      const factor = dist / touchStartDist;

      if (projectionType === 'robinson' && robinsonProj) {
        robinsonScale = Math.max(minRobinsonScale, Math.min(maxRobinsonScale, touchStartScale * factor));
        robinsonProj.scale(robinsonScale);
        render();
      } else {
        const newScale = Math.max(minScale, Math.min(maxScale, touchStartScale * factor));
        orthoProj.scale(newScale);
        render();
        if (typeof options.onZoom === 'function') {
          options.onZoom(newScale);
        }
      }
    }
  }

  function onTouchEnd(e) {
    if (e.touches.length === 0) {
      isDragging = false;
      isPinching = false;
      touchStartDist = 0;
    } else if (e.touches.length === 1) {
      isPinching = false;
      isDragging = true;
      touchStartX = e.touches[0].clientX;
      touchStartY = e.touches[0].clientY;
      dragStartRot = orthoProj.rotate().slice();
      dragStartRobinsonLng = robinsonCenterLng;
      dragStartRobinsonLat = robinsonCenterLat;
    }
  }

  // Interactivity: Wheel zoom
  function onWheel(e) {
    if (e.cancelable) e.preventDefault();
    const factor = e.deltaY < 0 ? 1.12 : 0.89;

    if (projectionType === 'robinson' && robinsonProj) {
      robinsonScale = Math.max(minRobinsonScale, Math.min(maxRobinsonScale, robinsonScale * factor));
      robinsonProj.scale(robinsonScale);
      render();
    } else {
      const nextScale = Math.max(minScale, Math.min(maxScale, orthoProj.scale() * factor));
      orthoProj.scale(nextScale);
      render();
      if (typeof options.onZoom === 'function') {
        options.onZoom(nextScale);
      }
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

    if (webglTerrain) {
      webglTerrain.destroy();
      webglTerrain = null;
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
    setProjectionType,
    getProjectionType: () => projectionType,
    setRobinsonViewportInsets,
    setVerticalCenterRatio,
    getVerticalCenterRatio: () => verticalCenterRatio,
    destroy,
    render,
    getRotation: () => getActiveProjection().rotate().slice(),
    getScale: () => getActiveProjection().scale(),
    getProjection: () => getActiveProjection(),
    getCanvas: () => canvas
  };
}
