# 03: Interactive Orthographic Globe Viewport

**What to build:**
Implement the `GlobeRenderer` module (`maps/src/globe.js`) and download/bundle TopoJSON land geometry (`maps/data/world-110m.json`). The module must mount an orthographic globe projection using D3.js (`d3-geo`, `topojson-client`), rendering graticules, landmass outlines, origin/destination pins, and the illuminated great-circle trajectory arc. Support mouse/touch drag-rotation, scroll-wheel zoom, recentering on the geodesic midpoint, and smooth camera slerp transition upon route update.

**Blocked by:** None (can start immediately)

**Status:** completed

## Acceptance Criteria
- [x] TopoJSON world atlas asset exists at `maps/data/world-110m.json` (~120 KB).
- [x] `createGlobeRenderer(container, topology)` renders Earth landmasses, oceans, and graticule lines in an orthographic projection.
- [x] Route display renders origin marker, destination marker, and curved geodesic line on the globe surface with back-hemisphere clipping.
- [x] User can click-and-drag to rotate the globe in 3D with mouse or touch.
- [x] User can zoom in and out smoothly.
- [x] Calling `setRoute(geodesicResult, origin, dest, true)` triggers a smooth camera rotation to center the orthographic view on `geodesicResult.sphericalMidpoint`.
- [x] `recenter()` resets camera view directly to the route midpoint.
