/**
 * Geodesic Flight Visualizer - Main Application Controller
 *
 * Coordinates:
 * - Asynchronous data loading (compact cities database and 110m TopoJSON world topology)
 * - CityDirectory search index
 * - Interactive Canvas GlobeRenderer with orthographic projection
 * - FlightHUD telemetry panel with combobox autocomplete and 1-click swap
 * - Reactive event binding for route changes, swap, recenter, and window resizing
 */

import { CityDirectory } from './directory.js';
import { computeGeodesic } from './geodesic.js';
import { createGlobeRenderer } from './globe.js';
import { FlightHUD } from './hud.js';

/**
 * Initializes the full Geodesic Map application.
 *
 * @param {object} [options]
 * @returns {Promise<object>} Application controller interface
 */
export async function initApp(options = {}) {
  const doc = options.document || (typeof document !== 'undefined' ? document : null);
  const globeContainer = options.globeContainer || (doc ? doc.getElementById('globe-container') : null);
  const hudContainer = options.hudContainer || (doc ? doc.getElementById('hud-container') : null);
  const loadingSplash = options.loadingSplash || (doc ? doc.getElementById('loading-splash') : null);

  const fetchFn = options.fetch || (typeof window !== 'undefined' && window.fetch ? window.fetch.bind(window) : globalThis.fetch);
  const citiesUrl = options.citiesUrl || 'data/cities10k.json';
  const worldUrl = options.worldUrl || 'data/world-110m.json';

  try {
    // 1. Fetch datasets asynchronously in parallel
    const [citiesData, worldTopology] = await Promise.all([
      options.citiesData
        ? Promise.resolve(options.citiesData)
        : fetchFn(citiesUrl).then((r) => {
            if (!r.ok) throw new Error(`HTTP ${r.status} fetching ${citiesUrl}`);
            return r.json();
          }),
      options.worldTopology
        ? Promise.resolve(options.worldTopology)
        : fetchFn(worldUrl).then((r) => {
            if (!r.ok) throw new Error(`HTTP ${r.status} fetching ${worldUrl}`);
            return r.json();
          })
    ]);

    // 2. Initialize CityDirectory and load cities index
    const directory = options.directory || new CityDirectory();
    if (!options.citiesDataAlreadyLoaded) {
      directory.load(citiesData);
    }

    // 3. Initialize GlobeRenderer
    const isMobilePortrait = typeof window !== 'undefined' && window.innerWidth < 640 && window.innerHeight > window.innerWidth;
    const initialVerticalRatio = isMobilePortrait ? 0.28 : 0.50;

    const canvas = options.canvas || (doc ? doc.createElement('canvas') : undefined);
    const globe = createGlobeRenderer(globeContainer, worldTopology, {
      d3: options.d3,
      topojson: options.topojson,
      canvas,
      verticalCenterRatio: initialVerticalRatio,
      ...(options.globeOptions || {})
    });

    // 4. Determine default initial iconic route: Paris, France -> Tokyo, Japan
    const parisMatches = directory.search('Paris');
    const tokyoMatches = directory.search('Tokyo');

    const initialOrigin = options.initialOrigin || (parisMatches.length > 0 ? parisMatches[0] : {
      id: 34,
      name: 'Paris',
      country: 'France',
      lat: 48.8566,
      lng: 2.3522
    });

    const initialDest = options.initialDest || (tokyoMatches.length > 0 ? tokyoMatches[0] : {
      id: 0,
      name: 'Tokyo',
      country: 'Japan',
      lat: 35.6839,
      lng: 139.7744
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
      return currentGeodesic;
    }

    // 5. Initialize FlightHUD with reactive event handlers
    const hud = new FlightHUD(hudContainer, {
      directory,
      initialOrigin,
      initialDest,
      document: doc,
      onCollapseChange: (isCollapsed) => {
        const isMobile = typeof window !== 'undefined' && window.innerWidth < 640 && window.innerHeight > window.innerWidth;
        if (isMobile) {
          globe.setVerticalCenterRatio(isCollapsed ? 0.50 : 0.28, true);
        } else {
          globe.setVerticalCenterRatio(0.50, true);
        }
      },
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
      }
    });

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

    // 6. Set initial iconic route (Paris -> Tokyo) and smooth camera focus
    updateRoute(initialOrigin, initialDest, true);

    // 7. Window resize handler
    const onResize = () => {
      const isMobile = typeof window !== 'undefined' && window.innerWidth < 640 && window.innerHeight > window.innerWidth;
      const targetRatio = (isMobile && !hud.getIsCollapsed()) ? 0.28 : 0.50;
      if (globe && typeof globe.setVerticalCenterRatio === 'function') {
        globe.setVerticalCenterRatio(targetRatio, false);
      }
      if (globe && typeof globe.resize === 'function') {
        globe.resize();
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
      resize: (w, h) => globe.resize(w, h),
      destroy: () => {
        if (typeof window !== 'undefined') {
          window.removeEventListener('resize', onResize);
        }
        if (globeContainer && typeof globeContainer.removeEventListener === 'function') {
          globeContainer.removeEventListener('click', onGlobeTap);
        }
        if (globe) globe.destroy();
        if (hud) hud.destroy();
      }
    };
  } catch (error) {
    console.error('Failed to initialize Geodesic Flight Visualizer:', error);
    if (loadingSplash) {
      const titleEl = loadingSplash.querySelector('.loading-title');
      if (titleEl) titleEl.textContent = 'INITIALIZATION FAILED';
      const subtitleEl = loadingSplash.querySelector('.loading-subtitle');
      if (subtitleEl) {
        subtitleEl.textContent = `Error loading application data: ${error.message}`;
        subtitleEl.style.color = '#f87171';
      }
    }
    throw error;
  }
}

// Auto-initialize when loaded as the main module in a browser environment
if (typeof window !== 'undefined' && typeof document !== 'undefined') {
  const launch = () => {
    if (document.getElementById('globe-container')) {
      initApp().then((app) => { window.__APP__ = app; }).catch((err) => {
        console.error('App launch failed:', err);
      });
    }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', launch);
  } else {
    launch();
  }
}
