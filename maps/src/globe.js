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
 * Picks the places to show as dots in the current view. places must be sorted
 * by descending population, so the index is the population rank. project(place,
 * index) returns screen [x, y] or null when the place is off-map. Candidates
 * inside rect are scored
 *   ln(N / (rank + 1)) - centerBias * (r / R) + (stickyBonus if previously shown)
 * where r is the pixel distance from center and R the half-diagonal of rect, so
 * a sparse region still gets dots near its middle. Candidates are then taken in
 * score order, skipping any closer than minSpacing px to a kept dot or to a
 * point in exclude (route pins), until limit dots are kept. With centerBias 0 and
 * no previous dots the score is monotonic in rank, i.e. plain top-N by population.
 *
 * @returns {Array<{place: object, index: number, x: number, y: number, score: number}>}
 */
export function selectPlaceDots(places, project, rect, {
  limit = 20, minSpacing = 16, exclude = [], center = null, centerBias = 0, previous = [], stickyBonus = 0.3
} = {}) {
  const dots = [];
  if (!Array.isArray(places) || typeof project !== 'function' || !rect || limit <= 0) return dots;
  const cx = center ? center[0] : (rect.x0 + rect.x1) / 2;
  const cy = center ? center[1] : (rect.y0 + rect.y1) / 2;
  // Inscribed radius, not the half-diagonal: the globe disc never reaches the
  // corners, so the half-diagonal would cap in-frame penalties near half of
  // centerBias and let rim megacities outscore everything near the center.
  const halfDiag = Math.min(rect.x1 - rect.x0, rect.y1 - rect.y0) / 2 || 1;
  const prev = new Set();
  for (const d of previous || []) prev.add(d.place);
  const n = places.length;
  const cands = [];
  for (let i = 0; i < n; i++) {
    const pt = project(places[i], i);
    if (!pt) continue;
    const x = pt[0];
    const y = pt[1];
    if (!(x >= rect.x0 && x <= rect.x1 && y >= rect.y0 && y <= rect.y1)) continue;
    let score = Math.log(n / (i + 1));
    if (centerBias) score -= centerBias * (Math.hypot(x - cx, y - cy) / halfDiag);
    if (prev.has(places[i])) score += stickyBonus;
    cands.push({ place: places[i], index: i, x, y, score });
  }
  cands.sort((a, b) => b.score - a.score || a.index - b.index);
  const minSq = minSpacing * minSpacing;
  for (let c = 0; c < cands.length && dots.length < limit; c++) {
    const { x, y } = cands[c];
    let crowded = false;
    for (let j = 0; j < exclude.length && !crowded; j++) {
      const dx = exclude[j][0] - x;
      const dy = exclude[j][1] - y;
      crowded = dx * dx + dy * dy < minSq;
    }
    for (let j = 0; j < dots.length && !crowded; j++) {
      const dx = dots[j].x - x;
      const dy = dots[j].y - y;
      crowded = dx * dx + dy * dy < minSq;
    }
    if (!crowded) dots.push(cands[c]);
  }
  return dots;
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
 * Robinson LUT values are 16-bit integers split over two 8-bit texture
 * channels. The sampler returns each byte as byte/255, so the shader must
 * rebuild (hi * 256 + lo) / 65535 as r * 65280/65535 + g * 255/65535.
 */
export const LUT16_HI_SCALE = 65280 / 65535;
export const LUT16_LO_SCALE = 255 / 65535;
const LUT16_HI_GLSL = LUT16_HI_SCALE.toFixed(10);
const LUT16_LO_GLSL = LUT16_LO_SCALE.toFixed(10);

/** Encodes a 0..1 value as [hi, lo] bytes. */
export function encodeLut16(v01) {
  const q = Math.max(0, Math.min(65535, Math.round(v01 * 65535)));
  return [Math.floor(q / 256), q % 256];
}

/** Decodes sampler-normalized channels exactly as the Robinson shader does. */
export function decodeLut16(hiNorm, loNorm) {
  return hiNorm * Number(LUT16_HI_GLSL) + loNorm * Number(LUT16_LO_GLSL);
}

const ROBINSON_FS_BODY = `
    varying vec2 v_uv;

    uniform sampler2D u_texture;
    uniform sampler2D u_lut;
    uniform vec2 u_resolution;
    uniform vec2 u_origin;     // screen position (device px) of (center lng, lat 0)
    uniform float u_scale;     // projection scale in device px
    uniform float u_centerLng; // radians
    uniform float u_yMax;      // projected |y| at the poles for scale 1
    uniform float u_xMax;      // projected x at lng 180, lat 0 for scale 1
    uniform float u_lutSize;
    uniform float u_loaded;

    #define PI 3.141592653589793

    vec2 decodeLut(float i) {
      vec4 t = texture2D(u_lut, vec2((i + 0.5) / u_lutSize, 0.5));
      // Each value is a 16-bit integer split hi/lo over two 8-bit channels.
      float lat = (t.r * ${LUT16_HI_GLSL} + t.g * ${LUT16_LO_GLSL}) * 2.0 - 1.0; // normalized lat / 90
      float hw = t.b * ${LUT16_HI_GLSL} + t.a * ${LUT16_LO_GLSL};                // half width / xMax
      return vec2(lat, hw);
    }

    void main() {
      if (u_loaded < 0.5) discard;
      vec2 screenCoord = vec2(v_uv.x * u_resolution.x, (1.0 - v_uv.y) * u_resolution.y);
      float rx = (screenCoord.x - u_origin.x) / u_scale;
      float ry = (u_origin.y - screenCoord.y) / u_scale;

      float ny = ry / u_yMax;               // -1 (south pole) .. 1 (north pole)
      float edgeY = (u_yMax - abs(ry)) * u_scale;
      if (edgeY < -1.0) discard;
      float f = clamp(ny * 0.5 + 0.5, 0.0, 1.0) * (u_lutSize - 1.0);
      float i0 = floor(f);
      vec2 a = decodeLut(i0);
      vec2 b = decodeLut(min(i0 + 1.0, u_lutSize - 1.0));
      vec2 lh = mix(a, b, f - i0);
      float phi = lh.x * PI * 0.5;
      float hw = lh.y * u_xMax;

      float edgeX = (hw - abs(rx)) * u_scale;
      if (edgeX < -1.0) discard;
      float lambda = clamp(rx / hw, -1.0, 1.0) * PI + u_centerLng;

      float texU = fract(lambda / (2.0 * PI) + 0.5);
      float texV = clamp(0.5 - phi / PI, 0.001, 0.999);
      vec3 color = texture2D(u_texture, vec2(texU, texV)).rgb * 0.92;

      float alpha = clamp(min(edgeX, edgeY) + 0.5, 0.0, 1.0);
      gl_FragColor = vec4(color, alpha * 0.9);
    }
  `;

/** Returns the Robinson fragment shader source (for tests). */
export function getRobinsonFragmentSource() {
  return ROBINSON_FS_BODY;
}

/**
 * WebGL Physical Terrain Layer
 * Raymarches NOAA ETOPO1 relief on a true sphere beneath vector borders.
 */
function initWebGLTerrain(container, parentCanvas, width, height, dpr, textureUrl, onLoaded, robinsonLut, hiResUrl) {
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

  const precisionHeader = `
    #ifdef GL_FRAGMENT_PRECISION_HIGH
    precision highp float;
    #else
    precision mediump float;
    #endif
  `;

  const fsSource = precisionHeader + `
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

  // Robinson (pseudo-cylindrical) inverse mapping. Robinson has no closed-form
  // inverse, so a lookup table built from d3's own projection (see
  // buildRobinsonLut) maps normalized projected y -> (latitude, half-width at
  // that latitude). The flat map is then just a texture lookup per pixel.
  const fsRobinsonSource = precisionHeader + ROBINSON_FS_BODY;

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

  function createProgram(fsSrc) {
    const vs = createShader(gl.VERTEX_SHADER, vsSource);
    const fs = createShader(gl.FRAGMENT_SHADER, fsSrc);
    if (!vs || !fs) return null;
    const prog = gl.createProgram();
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return null;
    return prog;
  }

  const program = createProgram(fsSource);
  if (!program) return null;
  const robinsonProgram = robinsonLut ? createProgram(fsRobinsonSource) : null;

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

  let rob = null;
  let lutTexture = null;
  if (robinsonProgram) {
    const u = (name) => gl.getUniformLocation(robinsonProgram, name);
    rob = {
      posLoc: gl.getAttribLocation(robinsonProgram, 'a_position'),
      uTexture: u('u_texture'), uLut: u('u_lut'), uRes: u('u_resolution'), uOrigin: u('u_origin'),
      uScale: u('u_scale'), uCenterLng: u('u_centerLng'), uYMax: u('u_yMax'), uXMax: u('u_xMax'),
      uLutSize: u('u_lutSize'), uLoaded: u('u_loaded')
    };
    lutTexture = gl.createTexture();
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, lutTexture);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, robinsonLut.size, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, robinsonLut.data);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.activeTexture(gl.TEXTURE0);
  }

  const texture = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, texture);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([13, 20, 36, 255]));

  let isLoaded = false;
  let hiResActive = false;

  function uploadTexture(img) {
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    // Mipmaps keep a high-resolution texture free of shimmer when the globe is
    // zoomed out and many texels collapse into one screen pixel. WebGL1 requires
    // power-of-two dimensions for mipmapping, so fall back to plain LINEAR otherwise.
    const isPot = (n) => n > 0 && (n & (n - 1)) === 0;
    if (isPot(img.width) && isPot(img.height)) {
      gl.generateMipmap(gl.TEXTURE_2D);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
      const aniso = gl.getExtension('EXT_texture_filter_anisotropic')
        || gl.getExtension('WEBKIT_EXT_texture_filter_anisotropic');
      if (aniso) {
        const maxAniso = gl.getParameter(aniso.MAX_TEXTURE_MAX_ANISOTROPY_EXT) || 1;
        gl.texParameterf(gl.TEXTURE_2D, aniso.TEXTURE_MAX_ANISOTROPY_EXT, Math.min(4, maxAniso));
      }
    } else {
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    }
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  }

  // Decode off the main thread where supported so the swap does not stall a frame.
  function loadImage(url, onReady, onFail) {
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload = () => {
      const ready = typeof img.decode === 'function' ? img.decode().catch(() => {}) : Promise.resolve();
      ready.then(() => onReady(img));
    };
    img.onerror = () => { if (onFail) onFail(); };
    img.src = url;
  }

  // The high-res texture is a progressive upgrade: only fetched once the base
  // texture is showing, and only on GPUs that can hold it. Devices that report
  // low memory (Chrome's navigator.deviceMemory) keep the base texture.
  function canUseHiRes() {
    if (!hiResUrl) return false;
    const maxTex = gl.getParameter(gl.MAX_TEXTURE_SIZE) || 0;
    if (maxTex < 8192) return false;
    const mem = typeof navigator !== 'undefined' ? navigator.deviceMemory : undefined;
    if (typeof mem === 'number' && mem < 4) return false;
    return true;
  }

  loadImage(textureUrl, (img) => {
    if (hiResActive) return;
    uploadTexture(img);
    isLoaded = true;
    if (typeof onLoaded === 'function') onLoaded();
    if (canUseHiRes()) {
      loadImage(hiResUrl, (hi) => {
        uploadTexture(hi);
        hiResActive = true;
        if (typeof onLoaded === 'function') onLoaded();
      });
    }
  }, () => {
    isLoaded = false;
  });

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
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.enableVertexAttribArray(posLoc);
    gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 0, 0);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.uniform2f(uRes, w * currentDpr, h * currentDpr);
    gl.uniform2f(uCenter, cx * currentDpr, cy * currentDpr);
    gl.uniform1f(uRadius, r * currentDpr);
    gl.uniform1f(uLoaded, isLoaded ? 1.0 : 0.0);

    const rotMat = getEulerRotationMatrix(-rot[0], -rot[1], rot[2] || 0);
    gl.uniformMatrix3fv(uRotMat, false, rotMat);

    gl.drawArrays(gl.TRIANGLES, 0, 6);
  }

  /**
   * Flat Robinson terrain. originX/originY: screen px of (centerLng, 0);
   * scale: projection scale in CSS px; centerLng in degrees.
   */
  function renderRobinson(w, h, currentDpr, originX, originY, scale, centerLngDeg) {
    if (!isLoaded || !rob) return false;
    gl.viewport(0, 0, glCanvas.width, glCanvas.height);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);

    gl.useProgram(robinsonProgram);
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.enableVertexAttribArray(rob.posLoc);
    gl.vertexAttribPointer(rob.posLoc, 2, gl.FLOAT, false, 0, 0);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, lutTexture);
    gl.uniform1i(rob.uTexture, 0);
    gl.uniform1i(rob.uLut, 1);
    gl.uniform2f(rob.uRes, w * currentDpr, h * currentDpr);
    gl.uniform2f(rob.uOrigin, originX * currentDpr, originY * currentDpr);
    gl.uniform1f(rob.uScale, scale * currentDpr);
    gl.uniform1f(rob.uCenterLng, centerLngDeg * Math.PI / 180);
    gl.uniform1f(rob.uYMax, robinsonLut.yMax);
    gl.uniform1f(rob.uXMax, robinsonLut.xMax);
    gl.uniform1f(rob.uLutSize, robinsonLut.size);
    gl.uniform1f(rob.uLoaded, 1.0);
    gl.drawArrays(gl.TRIANGLES, 0, 6);
    gl.activeTexture(gl.TEXTURE0);
    return true;
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
    renderRobinson,
    hasRobinson: () => !!rob,
    destroy,
    isLoaded: () => isLoaded,
    isHiRes: () => hiResActive
  };
}

/**
 * Builds the Robinson inverse lookup table from d3's projection so the WebGL
 * overlay lines up exactly with the vector layers drawn by d3.geoPath.
 * Entry i covers normalized projected y from -1 (south pole) to +1 (north pole)
 * and stores latitude/90 and the half-width (x at lng 180) relative to xMax,
 * each as a 16-bit value split over two bytes.
 */
function buildRobinsonLut(d3, size = 512) {
  if (!d3 || typeof d3.geoRobinson !== 'function') return null;
  const unit = d3.geoRobinson().scale(1).translate([0, 0]).rotate([0, 0, 0]).center([0, 0]);
  if (typeof unit.invert !== 'function') return null;
  const xMax = unit([180, 0])[0];
  const yMax = -unit([0, 90])[1];
  if (!(xMax > 0) || !(yMax > 0)) return null;
  const data = new Uint8Array(size * 4);
  const enc16 = encodeLut16;
  for (let i = 0; i < size; i++) {
    const ny = (i / (size - 1)) * 2 - 1;
    const sy = -ny * yMax * 0.99999; // screen y (down) for this projected y
    const inv = unit.invert([0, sy]);
    const lat = inv && Number.isFinite(inv[1]) ? Math.max(-90, Math.min(90, inv[1])) : (ny < 0 ? -90 : 90);
    const hw = unit([180, lat])[0];
    const [l0, l1] = enc16(lat / 90 * 0.5 + 0.5);
    const [h0, h1] = enc16(Math.max(0, Math.min(1, hw / xMax)));
    data[i * 4] = l0; data[i * 4 + 1] = l1; data[i * 4 + 2] = h0; data[i * 4 + 3] = h1;
  }
  return { size, data, xMax, yMax };
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
    // Over the terrain texture a faint white line vanishes against snow/ice and
    // bright relief, so borders get a dark halo under a brighter line.
    terrainBorderHalo: 'rgba(5, 10, 24, 0.55)',
    terrainBorder: 'rgba(255, 244, 214, 0.85)',
    placeDot: '#9ca3af',
    placeDotOutline: 'rgba(8, 12, 28, 0.85)',
    placeDotHover: '#e5e7eb',
    terrainCoast: 'rgba(5, 10, 24, 0.35)',
    graticule: 'rgba(255, 255, 255, 0.06)',
    haloOuter: 'rgba(6, 182, 212, 0.22)',
    haloMid: 'rgba(6, 182, 212, 0.08)',
    haloBorder: 'rgba(6, 182, 212, 0.45)',
    // Periwinkle route: reads clearly over ocean blues and vegetated land without going neon.
    geodesic: '#8f7bff',
    geodesicGlow: 'rgba(143, 123, 255, 0.35)',
    geodesicInnerGlow: 'rgba(143, 123, 255, 0.70)',
    origin: '#f59e0b',
    originAura: 'rgba(245, 158, 11, 0.35)',
    // Rose destination: emerald vanished against vegetated land.
    dest: '#fb4a5c',
    destAura: 'rgba(251, 74, 92, 0.38)',
    midpoint: '#38bdf8',
    midpointAura: 'rgba(56, 189, 248, 0.35)',
    plane: '#ffffff',
    planeOutline: 'rgba(8, 12, 28, 0.85)',
    planeAura: 'rgba(143, 123, 255, 0.28)',
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

  // Extract Landmass & Sovereign Country Borders from TopoJSON (or GeoJSON)
  function extractFeatures(topology) {
    let land = null;
    let borders = null;
    if (topology && topojson && typeof topojson.feature === 'function') {
      if (topology.objects) {
        if (topology.objects.land) {
          land = topojson.feature(topology, topology.objects.land);
        }
        if (topology.objects.countries) {
          if (!land) {
            land = topojson.feature(topology, topology.objects.countries);
          }
          try {
            if (typeof topojson.mesh === 'function') {
              borders = topojson.mesh(
                topology,
                topology.objects.countries,
                (a, b) => a !== b
              );
            }
          } catch (e) {
            borders = null;
          }
        }
      } else if (topology.type === 'FeatureCollection' || topology.type === 'Feature') {
        land = topology;
      }
    }
    return { land, borders };
  }

  // Two levels of detail: the base (light, e.g. 110m) topology is always used while
  // the globe is being dragged or animated so interaction stays smooth; an optional
  // detail (e.g. simplified 50m) topology is drawn for idle frames only.
  const baseFeatures = extractFeatures(worldTopology);
  let detailFeatures = null;
  let landFeature = baseFeatures.land;
  let countryBordersFeature = baseFeatures.borders;

  function setDetailTopology(topology) {
    const extracted = topology ? extractFeatures(topology) : { land: null, borders: null };
    detailFeatures = extracted.land || extracted.borders ? extracted : null;
    render();
  }

  function selectDetailLevel() {
    const interacting = isDragging || isPinching || animFrameId !== null;
    const useDetail = !!detailFeatures && !interacting;
    landFeature = useDetail ? (detailFeatures.land || baseFeatures.land) : baseFeatures.land;
    countryBordersFeature = useDetail
      ? (detailFeatures.borders || baseFeatures.borders)
      : baseFeatures.borders;
    return useDetail;
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

  let viewportInsets = { left: 0, right: 0, top: 0, bottom: 0 };
  let robinsonRouteDistance = 0;
  // D3's Robinson unit sphere spans 2π horizontally and 3.18683 vertically.
  function worldRobinsonScale() {
    const availableWidth = Math.max(1, width - viewportInsets.left - viewportInsets.right - 48);
    const availableHeight = Math.max(1, height - viewportInsets.top - viewportInsets.bottom - 48);
    return Math.max(1, Math.min(availableWidth / (2 * Math.PI), availableHeight / 3.1868315878));
  }
  function defaultRobinsonScale() {
    const availableWidth = Math.max(1, width - viewportInsets.left - viewportInsets.right - 48);
    const availableHeight = Math.max(1, height - viewportInsets.top - viewportInsets.bottom - 48);
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
  // Progressive 8k upgrade; pass terrainHiResUrl: null to disable.
  const terrainHiResUrl = options.terrainHiResUrl === undefined ? 'data/etopo-8k.jpg' : options.terrainHiResUrl;
  let webglTerrain = null;

  if (terrainEnabled) {
    const initDpr = typeof window !== 'undefined' ? (window.devicePixelRatio || 1) : 1;
    let robinsonLut = null;
    try { robinsonLut = buildRobinsonLut(d3); } catch (e) { robinsonLut = null; }
    webglTerrain = initWebGLTerrain(containerElement, canvas, width, height, initDpr, terrainUrl, () => {
      render();
    }, robinsonLut, terrainHiResUrl);
  }

  // Interaction/animation state (read by render() to pick the detail level)
  let isDragging = false;
  let isPinching = false;
  let animFrameId = null;

  // Application state
  let currentGeodesic = null;
  let currentOrigin = null;
  let currentDest = null;
  let currentRouteGeoJson = null;
  let currentMidpoint = null;

  // Population-ranked city dots ("places"). placeVecs caches unit vectors so
  // the orthographic view can cull the far side before projecting.
  const placeDotLimit = options.placeDotLimit || 20;
  const placeDotSpacing = options.placeDotSpacing || 22;
  let placeList = [];
  let placeVecs = null;
  // null = zoom-dependent default, see centerBiasFor().
  const placeDotCenterBias = options.placeDotCenterBias ?? null;
  const placeTouchLabelCount = options.placeTouchLabelCount ?? 4;
  const placeTapRadius = 24;
  // Touch devices have no hover, so the most relevant dots keep their labels.
  const touchLabels = options.touchLabels
    ?? (typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      ? !!window.matchMedia('(hover: none)').matches : false);
  let placeDots = [];
  let hoveredPlace = null;

  function orthographicTranslation() {
    const { left, right, top, bottom } = viewportInsets;
    return [(width + left - right) / 2, (height + top - bottom) * verticalCenterRatio];
  }

  function robinsonTranslation() {
    const { left, right, top, bottom } = viewportInsets;
    return [(width + left - right) / 2, (height + top - bottom) / 2];
  }

  function setViewportInsets(insets = {}) {
    const zoomRatio = robinsonScale / defaultRobinsonScale();
    viewportInsets = {
      left: Math.max(0, insets.left || 0), right: Math.max(0, insets.right || 0),
      top: Math.max(0, insets.top || 0), bottom: Math.max(0, insets.bottom || 0)
    };
    robinsonScale = defaultRobinsonScale() * zoomRatio;
    minRobinsonScale = worldRobinsonScale() * 0.4;
    if (robinsonProj) robinsonProj.scale(robinsonScale).translate(robinsonTranslation());
    orthoProj.translate(orthographicTranslation());
    render();
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

    orthoProj.translate(orthographicTranslation());
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
    selectDetailLevel();
    const dpr = typeof window !== 'undefined' ? (window.devicePixelRatio || 1) : 1;
    const activeProj = getActiveProjection();
    const path = d3.geoPath(activeProj, ctx);

    const isRobinson = projectionType === 'robinson';
    const [cx, cy] = activeProj.translate();
    const r = activeProj.scale();
    const rot = activeProj.rotate();

    // Render WebGL terrain: raymarched sphere (orthographic) or flat inverse-mapped map (Robinson)
    let hasTerrain = false;
    if (webglTerrain && webglTerrain.isLoaded()) {
      if (isRobinson) {
        const origin = robinsonProj([robinsonCenterLng, 0]);
        if (origin && Number.isFinite(origin[0]) && Number.isFinite(origin[1])) {
          hasTerrain = webglTerrain.renderRobinson(width, height, dpr, origin[0], origin[1], r, robinsonCenterLng);
        }
      } else {
        webglTerrain.render(width, height, dpr, cx, cy, r, rot);
        hasTerrain = true;
      }
    }

    ctx.clearRect(0, 0, width, height);

    if (isRobinson) {
      // 1. Draw the curved world outline, leaving space outside the map.
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
      ctx.strokeStyle = hasTerrain ? palette.terrainCoast : palette.landStroke;
      ctx.lineWidth = 0.85;
      ctx.stroke();
      ctx.restore();
    }

    // 5. Sovereign Country Borders
    if (countryBordersFeature) {
      ctx.save();
      ctx.beginPath();
      path(countryBordersFeature);
      ctx.lineJoin = 'round';
      if (hasTerrain) {
        ctx.strokeStyle = palette.terrainBorderHalo;
        ctx.lineWidth = 2.4;
        ctx.stroke();
        ctx.strokeStyle = palette.terrainBorder;
        ctx.lineWidth = 0.9;
        ctx.stroke();
      } else {
        ctx.strokeStyle = palette.countryBorder;
        ctx.lineWidth = 0.75;
        ctx.stroke();
      }
      ctx.restore();
    }

    // 5b. Population-ranked city dots for the current view
    placeDots = computePlaceDots(activeProj);
    if (hoveredPlace && !placeDots.some((d) => d.place === hoveredPlace)) hoveredPlace = null;
    drawPlaceDots();

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

    // 7. Aircraft marker at the spherical midpoint, nose pointing along the route
    if (currentMidpoint) {
      const midCoord = [currentMidpoint.lng, currentMidpoint.lat];
      if (isCoordVisible(midCoord, activeProj, projectionType, width, height)) {
        const pt = activeProj(midCoord);
        if (pt && isFinite(pt[0]) && isFinite(pt[1])) {
          const heading = routeHeadingAt(0.5, activeProj);
          if (heading === null) {
            drawMidpointMarker(pt[0], pt[1]);
          } else {
            drawPlaneMarker(pt[0], pt[1], heading);
          }
        }
      }
    }

    // 8. City pins and labels (origin and destination)
    const pinRects = [];
    if (currentOrigin) {
      const coord = getCityCoords(currentOrigin);
      if (coord && isCoordVisible(coord, activeProj, projectionType, width, height)) {
        const pt = activeProj(coord);
        if (pt && isFinite(pt[0]) && isFinite(pt[1])) {
          const name = getCityName(currentOrigin);
          pinRects.push(drawCityPin(pt[0], pt[1], name, palette.origin, palette.originAura, 'left'));
        }
      }
    }

    if (currentDest) {
      const coord = getCityCoords(currentDest);
      if (coord && isCoordVisible(coord, activeProj, projectionType, width, height)) {
        const pt = activeProj(coord);
        if (pt && isFinite(pt[0]) && isFinite(pt[1])) {
          const name = getCityName(currentDest);
          pinRects.push(drawCityPin(pt[0], pt[1], name, palette.dest, palette.destAura, 'right'));
        }
      }
    }

    // 9. Name label for the hovered / tapped city dot
    const dotLabel = (d) => (d.place.country ? `${d.place.name}, ${d.place.country}` : d.place.name);
    const dotAlign = (d) => (d.x > width * 0.6 ? 'left' : 'right');
    const taken = pinRects.filter(Boolean);
    const hoveredDot = hoveredPlace && placeDots.find((d) => d.place === hoveredPlace);
    if (hoveredDot) {
      taken.push(drawLabelBadge(hoveredDot.x, hoveredDot.y, dotLabel(hoveredDot), dotAlign(hoveredDot), 12));
    }
    for (const d of placeDots) d.labeled = false;
    if (touchLabels) {
      // placeDots is in score order, so the first fits are the most relevant.
      let shown = 0;
      for (const d of placeDots) {
        if (shown >= placeTouchLabelCount) break;
        if (d === hoveredDot) { d.labeled = true; shown++; continue; }
        const text = dotLabel(d);
        const r = labelBadgeRect(d.x, d.y, text, dotAlign(d), 12);
        if (taken.some((t) => rectsOverlap(t, r))) continue;
        drawLabelBadge(d.x, d.y, text, dotAlign(d), 12);
        taken.push(r);
        d.labeled = true;
        shown++;
      }
    }
  }

  function setPlaces(list) {
    placeList = Array.isArray(list)
      ? list.filter((p) => p && Number.isFinite(p.lat) && Number.isFinite(p.lng) && p.name)
      : [];
    const toRad = Math.PI / 180;
    placeVecs = new Float64Array(placeList.length * 3);
    for (let i = 0; i < placeList.length; i++) {
      const phi = placeList[i].lat * toRad;
      const lam = placeList[i].lng * toRad;
      placeVecs[i * 3] = Math.cos(phi) * Math.cos(lam);
      placeVecs[i * 3 + 1] = Math.cos(phi) * Math.sin(lam);
      placeVecs[i * 3 + 2] = Math.sin(phi);
    }
    hoveredPlace = null;
    render();
  }

  // Visible screen area: the canvas minus the HUD insets.
  function placeRect() {
    const pad = 6;
    return {
      x0: viewportInsets.left + pad,
      x1: width - viewportInsets.right - pad,
      y0: viewportInsets.top + pad,
      y1: height - viewportInsets.bottom - pad
    };
  }

  function computePlaceDots(proj) {
    if (!placeList.length || typeof proj !== 'function') return [];
    const rect = placeRect();
    if (rect.x1 <= rect.x0 || rect.y1 <= rect.y0) return [];
    const toRad = Math.PI / 180;
    let cull;
    // Angular radius of the view in degrees: 180 when the whole world is in
    // frame (Robinson), <= 90 for a hemisphere or a zoomed region.
    let viewHalfAngle = 180;

    if (projectionType === 'robinson') {
      // Projected y depends only on latitude, so the center column bounds the band.
      const [tx] = proj.translate();
      const latAt = (y, fallback) => {
        const g = typeof proj.invert === 'function' ? proj.invert([tx, y]) : null;
        return g && Number.isFinite(g[1]) && Math.abs(g[1]) <= 90 ? g[1] : fallback;
      };
      const latMax = latAt(rect.y0, 90) + 1;
      const latMin = latAt(rect.y1, -90) - 1;
      cull = (p) => p.lat > latMax || p.lat < latMin;
      // Half the longitude span across the rect at the equator, from the
      // projection's px-per-degree there. (invert() wraps longitudes to +-180,
      // so it cannot measure a span that crosses the antimeridian.)
      const lng0 = -proj.rotate()[0];
      const p0 = proj([lng0, 0]);
      const p1 = proj([lng0 + 1, 0]);
      const pxPerDeg = p0 && p1 ? Math.abs(p1[0] - p0[0]) : 0;
      viewHalfAngle = pxPerDeg > 0 ? Math.min(180, (rect.x1 - rect.x0) / pxPerDeg / 2) : 180;
    } else {
      // Skip places farther from the view center than the farthest visible corner.
      const rot = proj.rotate();
      const [tx, ty] = proj.translate();
      const k = proj.scale();
      const phi0 = -rot[1] * toRad;
      const lam0 = -rot[0] * toRad;
      const c = [Math.cos(phi0) * Math.cos(lam0), Math.cos(phi0) * Math.sin(lam0), Math.sin(phi0)];
      const d = Math.max(
        Math.hypot(rect.x0 - tx, rect.y0 - ty), Math.hypot(rect.x1 - tx, rect.y0 - ty),
        Math.hypot(rect.x0 - tx, rect.y1 - ty), Math.hypot(rect.x1 - tx, rect.y1 - ty)
      );
      // Floor at ~78 deg from center: cities squeezed onto the limb read as clutter.
      const cosLimit = Math.max(0.2, d >= k ? 0 : Math.cos(Math.asin(d / k)) - 1e-4);
      const rIn = Math.min(rect.x1 - rect.x0, rect.y1 - rect.y0) / 2;
      viewHalfAngle = rIn >= k ? 90 : Math.asin(rIn / k) / toRad;
      cull = (p, i) => (
        c[0] * placeVecs[i * 3] + c[1] * placeVecs[i * 3 + 1] + c[2] * placeVecs[i * 3 + 2]
      ) < cosLimit;
    }

    const exclude = [];
    for (const city of [currentOrigin, currentDest]) {
      const coord = city && getCityCoords(city);
      if (coord && isCoordVisible(coord, proj, projectionType, width, height)) {
        const pt = proj(coord);
        if (pt && isFinite(pt[0]) && isFinite(pt[1])) exclude.push(pt);
      }
    }

    return selectPlaceDots(placeList, (p, i) => {
      if (cull(p, i)) return null;
      const pt = proj([p.lng, p.lat]);
      return pt && isFinite(pt[0]) && isFinite(pt[1]) ? pt : null;
    }, rect, {
      limit: placeDotLimit,
      minSpacing: placeDotSpacing,
      exclude,
      center: [(rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2],
      centerBias: placeDotCenterBias ?? centerBiasFor(viewHalfAngle),
      previous: placeDots
    });
  }

  // Center bias for a view of the given angular radius. Full strength 2·ln(N)
  // from a hemisphere (<= 80 deg) inward, fading to 0 at a whole-world view so
  // the world map still shows the global top 20 instead of piling dots at its
  // center.
  function centerBiasFor(halfAngleDeg) {
    const full = 2 * Math.log(Math.max(2, placeList.length));
    const t = Math.min(1, Math.max(0, (180 - halfAngleDeg) / 100));
    return full * t;
  }

  function drawPlaceDots() {
    if (!placeDots.length) return;
    ctx.save();
    for (const dot of placeDots) {
      const hovered = dot.place === hoveredPlace;
      ctx.beginPath();
      ctx.arc(dot.x, dot.y, hovered ? 8.4 : 5.6, 0, Math.PI * 2);
      ctx.fillStyle = hovered ? palette.placeDotHover : palette.placeDot;
      ctx.fill();
      ctx.strokeStyle = palette.placeDotOutline;
      ctx.lineWidth = 1.2;
      ctx.stroke();
    }
    ctx.restore();
  }

  // Nearest visible dot within radius px of a canvas point, or null.
  function placeDotAt(x, y, radius) {
    let best = null;
    let bestSq = radius * radius;
    for (const dot of placeDots) {
      const dSq = (dot.x - x) * (dot.x - x) + (dot.y - y) * (dot.y - y);
      if (dSq <= bestSq) {
        best = dot;
        bestSq = dSq;
      }
    }
    return best;
  }

  function setHoveredPlace(place) {
    if (place === hoveredPlace) return;
    hoveredPlace = place;
    render();
  }

  // Interpolated route coordinate at fraction t in [0, 1] along the sampled geodesic.
  function routePointAt(t) {
    const coords = currentRouteGeoJson && currentRouteGeoJson.coordinates;
    if (!coords || coords.length < 2) return null;
    const f = Math.max(0, Math.min(1, t)) * (coords.length - 1);
    const i = Math.min(coords.length - 2, Math.floor(f));
    const u = f - i;
    const a = coords[i];
    const b = coords[i + 1];
    let dLng = b[0] - a[0];
    if (dLng > 180) dLng -= 360;
    if (dLng < -180) dLng += 360;
    return [a[0] + dLng * u, a[1] + (b[1] - a[1]) * u];
  }

  // Screen-space heading (radians, canvas orientation) of travel at fraction t.
  // Returns null when either neighbour sample is off-globe or unprojectable.
  function routeHeadingAt(t, proj) {
    const eps = 0.01;
    const before = routePointAt(t - eps);
    const after = routePointAt(t + eps);
    if (!before || !after) return null;
    if (!isCoordVisible(before, proj, projectionType, width, height)) return null;
    if (!isCoordVisible(after, proj, projectionType, width, height)) return null;
    const p0 = proj(before);
    const p1 = proj(after);
    if (!p0 || !p1 || !isFinite(p0[0]) || !isFinite(p0[1]) || !isFinite(p1[0]) || !isFinite(p1[1])) return null;
    const dx = p1[0] - p0[0];
    const dy = p1[1] - p0[1];
    if (Math.hypot(dx, dy) < 1e-6) return null;
    return Math.atan2(dy, dx);
  }

  // Top-down airliner silhouette, nose along +x before rotation by heading.
  function drawPlaneMarker(x, y, heading) {
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(heading);
    const s = 1.15;
    ctx.scale(s, s);

    ctx.beginPath();
    ctx.arc(0, 0, 11, 0, Math.PI * 2);
    ctx.fillStyle = palette.planeAura;
    ctx.fill();

    ctx.beginPath();
    // Nose
    ctx.moveTo(10, 0);
    ctx.lineTo(7.5, -1.4);
    // Right wing (screen-down side)
    ctx.lineTo(2.2, -1.4);
    ctx.lineTo(-2.5, -8.5);
    ctx.lineTo(-5, -8.5);
    ctx.lineTo(-3, -1.4);
    // Fuselage to tail
    ctx.lineTo(-7.2, -1.1);
    ctx.lineTo(-9.5, -4.2);
    ctx.lineTo(-11, -4.2);
    ctx.lineTo(-10.2, -0.9);
    ctx.lineTo(-10.2, 0.9);
    ctx.lineTo(-11, 4.2);
    ctx.lineTo(-9.5, 4.2);
    ctx.lineTo(-7.2, 1.1);
    // Left wing
    ctx.lineTo(-3, 1.4);
    ctx.lineTo(-5, 8.5);
    ctx.lineTo(-2.5, 8.5);
    ctx.lineTo(2.2, 1.4);
    ctx.lineTo(7.5, 1.4);
    ctx.closePath();
    ctx.lineJoin = 'round';
    ctx.strokeStyle = palette.planeOutline;
    ctx.lineWidth = 2.2;
    ctx.stroke();
    ctx.fillStyle = palette.plane;
    ctx.fill();
    ctx.restore();
  }

  // Draw spherical midpoint marker (fallback when the heading cannot be resolved)
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

    ctx.restore();
    return name ? drawLabelBadge(x, y, name, align, 10) : null;
  }

  function rectsOverlap(a, b) {
    return a.x0 < b.x1 && a.x1 > b.x0 && a.y0 < b.y1 && a.y1 > b.y0;
  }

  // Badge geometry shared by drawing and overlap checks.
  function labelBadgeRect(x, y, name, align = 'right', offset = 10) {
    ctx.save();
    ctx.font = '600 11px system-ui, -apple-system, sans-serif';
    const textW = ctx.measureText(name).width;
    ctx.restore();
    const padX = 7;
    const badgeW = textW + padX * 2;
    const badgeH = 22;
    const badgeX = align === 'left' ? x - badgeW - offset : x + offset;
    const badgeY = y - badgeH / 2;
    return { x0: badgeX, y0: badgeY, x1: badgeX + badgeW, y1: badgeY + badgeH, padX };
  }

  function drawLabelBadge(x, y, name, align = 'right', offset = 10) {
    const rect = labelBadgeRect(x, y, name, align, offset);
    const { x0: badgeX, y0: badgeY, padX } = rect;
    const badgeW = rect.x1 - rect.x0;
    const badgeH = rect.y1 - rect.y0;
    ctx.save();
    ctx.font = '600 11px system-ui, -apple-system, sans-serif';

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
    ctx.restore();
    return rect;
  }

  // Animation controller
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
        webglTerrain.glCanvas.style.display = webglTerrain.hasRobinson() ? 'block' : 'none';
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

    const startRatio = verticalCenterRatio;
    const startTime = (typeof performance !== 'undefined' ? performance.now() : Date.now());
    const duration = 280;

    function anim(now) {
      const elapsed = now - startTime;
      const t = Math.min(1, elapsed / duration);
      const ease = t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;
      verticalCenterRatio = startRatio + (targetRatio - startRatio) * ease;
      orthoProj.translate(orthographicTranslation());
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
    if (detailFeatures) render();
  }

  // Interactivity: Touch drag & Pinch zoom
  let touchStartX = 0;
  let touchStartY = 0;
  let touchStartDist = 0;
  let touchStartScale = 0;

  function onTouchStart(e) {
    onTapStart(e);
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
    if (e.type !== 'touchcancel') onTapEnd(e);
    if (e.touches.length === 0) {
      isDragging = false;
      isPinching = false;
      touchStartDist = 0;
      if (detailFeatures) render();
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

  // City dot hover (desktop) and tap (touch) labels
  function canvasPoint(clientX, clientY) {
    const r = typeof canvas.getBoundingClientRect === 'function' ? canvas.getBoundingClientRect() : null;
    return r ? [clientX - r.left, clientY - r.top] : [clientX, clientY];
  }

  function onHoverMove(e) {
    if (isDragging || !placeDots.length) {
      if (!isDragging && hoveredPlace) setHoveredPlace(null);
      return;
    }
    const [x, y] = canvasPoint(e.clientX, e.clientY);
    const dot = placeDotAt(x, y, 10);
    canvas.style.cursor = dot ? 'pointer' : 'grab';
    setHoveredPlace(dot ? dot.place : null);
  }

  function onHoverLeave() {
    if (!isDragging) canvas.style.cursor = 'grab';
    setHoveredPlace(null);
  }

  let tapStart = null;
  function onTapStart(e) {
    tapStart = e.touches && e.touches.length === 1
      ? { x: e.touches[0].clientX, y: e.touches[0].clientY } : null;
  }

  function onTapEnd(e) {
    const t = e.changedTouches && e.changedTouches[0];
    if (!tapStart || !t || (e.touches && e.touches.length > 0)) return;
    const moved = Math.hypot(t.clientX - tapStart.x, t.clientY - tapStart.y);
    tapStart = null;
    if (moved > 8) return;
    const [x, y] = canvasPoint(t.clientX, t.clientY);
    const dot = placeDotAt(x, y, placeTapRadius);
    setHoveredPlace(dot ? dot.place : null);
  }

  // Attach event listeners
  canvas.addEventListener('mousemove', onHoverMove);
  canvas.addEventListener('mouseleave', onHoverLeave);
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

  if (Array.isArray(options.places)) setPlaces(options.places);

  // Initial draw
  render();

  // Teardown routine
  function destroy() {
    if (animFrameId) {
      cancelAnim(animFrameId);
      animFrameId = null;
    }

    canvas.removeEventListener('mousemove', onHoverMove);
    canvas.removeEventListener('mouseleave', onHoverLeave);
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
    setViewportInsets,
    setRobinsonViewportInsets: setViewportInsets,
    setVerticalCenterRatio,
    getVerticalCenterRatio: () => verticalCenterRatio,
    destroy,
    render,
    setDetailTopology,
    setPlaces,
    getPlaceDots: () => placeDots.map((d) => ({ name: d.place.name, country: d.place.country, x: d.x, y: d.y, labeled: !!d.labeled })),
    getHoveredPlace: () => (hoveredPlace ? { name: hoveredPlace.name, country: hoveredPlace.country } : null),
    getRotation: () => getActiveProjection().rotate().slice(),
    getScale: () => getActiveProjection().scale(),
    getProjection: () => getActiveProjection(),
    getCanvas: () => canvas
  };
}
