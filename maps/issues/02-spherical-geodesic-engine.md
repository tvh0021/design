# 02: Spherical Geodesic and Flight Telemetry Engine

**What to build:**
Implement the pure mathematical module `GeodesicEngine` (`maps/src/geodesic.js`) according to `maps/docs/DESIGN.md` and `maps/docs/SPEC.md`. It must calculate true spherical great-circle distances, normalized 3D Cartesian vector midpoint ($\mathbf{v}_{\text{mid}} = \frac{\mathbf{v}_1 + \mathbf{v}_2}{\|\mathbf{v}_1 + \mathbf{v}_2\|}$), antipodal detection ($\ge 179.5^\circ$) with trans-polar North Pole routing, forward/backward azimuths, nominal flight time at 900 km/h, and prevailing westerly jet-stream penalty calculations. Include comprehensive automated tests in `maps/tests/test_geodesic.js`.

**Blocked by:** None (can start immediately)

**Status:** closed

## Acceptance Criteria
- [x] `computeGeodesic(origin, dest)` returns accurate distance in km and miles (within 0.1% of Haversine reference).
- [x] Midpoint calculation uses 3D unit vectors and handles antimeridian crossings without inverted or wrapped longitudes.
- [x] For identical origin and destination, distance is 0 and path is valid.
- [x] For antipodal pairs ($\ge 179.5^\circ$), `isAntipodal` is true, midpoint defaults to North Pole $(90^\circ, 0^\circ)$, and waypoint coordinates route through North Pole.
- [x] Flight time includes westerly prevailing wind penalty factor (e.g. London -> New York takes ~10-15% longer than New York -> London).
- [x] All unit tests in `maps/tests/test_geodesic.js` pass.
