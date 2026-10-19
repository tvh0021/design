/**
 * Geodesic Flight Visualizer - Application Coordinator
 *
 * Coordinates asynchronous initialization, data fetching, geodesic calculations,
 * 3D Orthographic / 2D Robinson map rendering, and responsive glassmorphic HUD controls.
 */

import { CityDirectory } from './directory.js';
import { computeGeodesic } from './geodesic.js';
import { createGlobeRenderer } from './globe.js';
import { FlightHUD } from './hud.js';

/**
 * Initializes the complete Geodesic Flight application.
 *
 * @param {object} [options]
 * @returns {Promise<object>} Application controller interface
 */
export async function initApp(options = {}) {
  const doc = options.document || (typeof document !== 'undefined' ? document : null);

  // UI Container references
  const globeContainer = options.globeContainer || (doc ? doc.getElementById('globe-container') : null);
  const hudContainer = options.hudContainer || (doc ? doc.getElementById('hud-container') : null);
  const loadingSplash = options.loadingSplash || (doc ? doc.getElementById('loading-splash') : null);

  if (!globeContainer || !hudContainer) {
    throw new Error('Application requires #globe-container and #hud-container elements');
  }

  const fetchFn = options.fetch || (typeof window !== 'undefined' && window.fetch ? window.fetch.bind(window) : globalThis.fetch);
  const citiesUrl = options.citiesUrl || 'data/cities10k.json';
  const worldUrl = options.worldUrl || 'data/world-110m.json';

  try {
    // 1. Load city directory and world topology concurrently
    const [citiesData, worldTopology] = await Promise.all([
      options.citiesData
        ? Promise.resolve(options.citiesData)
        : fetchFn(citiesUrl).then((r) => {
            if (!r.ok) throw new Error(`HTTP error ${r.status} fetching ${citiesUrl}`);
            return r.json();
          }),
      options.worldTopology
        ? Promise.resolve(options.worldTopology)
        : fetchFn(worldUrl).then((r) => {
            if (!r.ok) throw new Error(`HTTP error ${r.status} fetching ${worldUrl}`);
            return r.json();
          })
    ]);

    // 2. Initialize CityDirectory and load cities index
    const directory = options.directory || new CityDirectory();
    if (!options.citiesDataAlreadyLoaded && !options.directory) {
      directory.load(citiesData);
    }

    // Insets place the projection in the free area on desktop and mobile.
    const initialVerticalRatio = 0.50;

    // Check URL query parameters for custom initial route or projection
    const urlParams = typeof window !== 'undefined' && window.location?.search ? new URLSearchParams(window.location.search) : null;
    const initialProjParam = urlParams?.get('proj');
    const initialOriginParam = urlParams?.get('origin');
    const initialDestParam = urlParams?.get('dest');

    const resolvedProjection = options.initialProjection || (initialProjParam === 'robinson' ? 'robinson' : 'orthographic');

    let initialOrigin = options.initialOrigin;
    if (!initialOrigin && initialOriginParam) {
      const matches = directory.search(initialOriginParam);
      if (matches.length > 0) initialOrigin = matches[0];
    }
    if (!initialOrigin) {
      const singaporeMatches = directory.search('Singapore');
      initialOrigin = singaporeMatches.length > 0 ? singaporeMatches[0] : {
        id: 135,
        name: 'Singapore',
        country: 'Singapore',
        lat: 1.3,
        lng: 103.8
      };
    }

    let initialDest = options.initialDest;
    if (!initialDest && initialDestParam) {
      const matches = directory.search(initialDestParam);
      if (matches.length > 0) initialDest = matches[0];
    }
    if (!initialDest) {
      const newYorkMatches = directory.search('New York');
      initialDest = newYorkMatches.length > 0 ? newYorkMatches[0] : {
        id: 12,
        name: 'New York',
        country: 'United States',
        lat: 40.6943,
        lng: -73.9249
      };
    }

    // 3. Initialize Interactive Map Renderer
    const canvas = options.canvas || (doc ? doc.createElement('canvas') : undefined);
    const globe = createGlobeRenderer(globeContainer, worldTopology, {
      d3: options.d3,
      topojson: options.topojson,
      canvas,
      verticalCenterRatio: initialVerticalRatio,
      projectionType: resolvedProjection,
      ...(options.globeOptions || {})
    });

    let currentOrigin = initialOrigin;
    let currentDest = initialDest;
    let currentGeodesic = null;
    let isSwapping = false;

    /**
     * Updates geodesic computation, telemetry card, and globe camera/path.
     */
    function updateRoute(origin, dest, animate = true) {
      currentOrigin = origin;
      currentDest = dest;

      if (origin && dest) {
        currentGeodesic = computeGeodesic(origin, dest);
        hud.updateTelemetry(currentGeodesic, origin, dest);
        globe.setRoute(currentGeodesic, origin, dest, animate);
      } else {
        currentGeodesic = null;
        hud.updateTelemetry(null, origin, dest);
        globe.setRoute(null, origin, dest, false);
      }
      updateViewportInsets();
      return currentGeodesic;
    }

    // 5. Initialize FlightHUD with reactive event handlers
    const hud = new FlightHUD(hudContainer, {
      directory,
      initialOrigin,
      initialDest,
      document: doc,
      initialProjection: resolvedProjection,
      onCollapseChange: () => updateViewportInsets(),
      onOriginSelect: (city) => {
        if (isSwapping) return;
        updateRoute(city, currentDest, true);
      },
      onDestSelect: (city) => {
        if (isSwapping) return;
        updateRoute(currentOrigin, city, true);
      },
      onSwap: (origin, dest) => {
        isSwapping = true;
        updateRoute(origin, dest, true);
        queueMicrotask(() => {
          isSwapping = false;
        });
      },
      onRecenter: () => {
        globe.recenter();
      },
      onProjectionChange: (projectionType) => {
        globe.setProjectionType(projectionType);
      }
    });

    function updateViewportInsets() {
      if (typeof globe.setViewportInsets !== 'function') return;
      const isMobile = typeof window !== 'undefined' && window.innerWidth < 640;
      const panel = hud.hudElement;
      const hidden = hud.getIsCollapsed();
      globe.setViewportInsets({
        left: !isMobile && !hidden ? (panel.offsetLeft || 0) + (panel.offsetWidth || 0) + 24 : 0,
        top: 80,
        bottom: isMobile && !hidden ? (panel.offsetHeight || 0) + 12 : 0
      });
    }

    const panelObserver = typeof ResizeObserver !== 'undefined'
      ? new ResizeObserver(() => updateViewportInsets()) : null;
    if (panelObserver) panelObserver.observe(hud.hudElement);

    // Tap on globe canvas outside HUD collapses the sheet on mobile to reveal full globe
    const onGlobeTap = () => {
      const isMobile = typeof window !== 'undefined' && window.innerWidth < 640;
      if (isMobile && !hud.getIsCollapsed()) {
        hud.collapse();
      }
    };

    if (globeContainer && typeof globeContainer.addEventListener === 'function') {
      globeContainer.addEventListener('click', onGlobeTap);
    }

    // 6. Perform initial route computation and animation
    updateRoute(initialOrigin, initialDest, false);

    // 7. Window resize handler
    const onResize = () => {
      const targetRatio = 0.50;
      if (globe && typeof globe.setVerticalCenterRatio === 'function') {
        globe.setVerticalCenterRatio(targetRatio, false);
      }
      if (globe && typeof globe.resize === 'function') {
        globe.resize();
        updateViewportInsets();
      }
    };

    if (typeof window !== 'undefined') {
      window.addEventListener('resize', onResize);
    }

    // 8. Dismiss loading splash screen
    if (loadingSplash) {
      loadingSplash.classList.add('hidden');
      setTimeout(() => {
        loadingSplash.style.display = 'none';
      }, 500);
    }

    return {
      directory,
      globe,
      hud,
      getOrigin: () => currentOrigin,
      getDest: () => currentDest,
      getGeodesic: () => currentGeodesic,
      updateRoute,
      recenter: () => globe.recenter(),
      setProjection: (type) => hud.setProjection(type),
      getProjection: () => globe.getProjectionType(),
      resize: (w, h) => globe.resize(w, h),
      destroy: () => {
        if (typeof window !== 'undefined') {
          window.removeEventListener('resize', onResize);
        }
        if (globeContainer && typeof globeContainer.removeEventListener === 'function') {
          globeContainer.removeEventListener('click', onGlobeTap);
        }
        if (panelObserver) panelObserver.disconnect();
        globe.destroy();
        hud.destroy();
      }
    };
  } catch (err) {
    if (loadingSplash) {
      const splashText = loadingSplash.querySelector('.splash-text');
      if (splashText) splashText.textContent = `Initialization Error: ${err.message}`;
    }
    throw err;
  }
}

// Auto-bootstrap when loaded in browser window
if (typeof window !== 'undefined' && typeof document !== 'undefined') {
  const isTest = typeof process !== 'undefined' && process.env?.NODE_ENV === 'test';
  if (!isTest) {
    window.addEventListener('DOMContentLoaded', () => {
      initApp().catch((err) => {
        console.error('Failed to bootstrap Geodesic Flight visualizer:', err);
      });
    });
  }
}
