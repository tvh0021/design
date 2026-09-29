# 05: Application Integration, Slerp Synchronization and Verification

**What to build:**
Assemble the complete application in `maps/index.html` and `maps/src/app.js`. Wire `CityDirectory`, `GeodesicEngine`, `GlobeRenderer`, and `FlightHUD` together into a seamless single-page application. Verify end-to-end functionality across sample routes (e.g., Paris to Fairbanks, Sydney to Rio de Janeiro, Tokyo to San Francisco, and Madrid to Wellington). Ensure the page is ready for deployment on GitHub Pages (`github.io`).

**Blocked by:** 01: Top 10k City Data Pipeline and Search Directory, 02: Spherical Geodesic and Flight Telemetry Engine, 03: Interactive Orthographic Globe Viewport, 04: Frontend Styling, Autocomplete HUD and Flight Telemetry Card

**Status:** ready-for-agent

## Acceptance Criteria
- [ ] `maps/index.html` loads all assets with relative paths and runs cleanly on static hosting.
- [ ] Initial state loads with a default iconic route (e.g. Paris -> Tokyo) rendered and centered on the midpoint.
- [ ] Changing origin or destination recomputes the geodesic, updates the HUD card, and slerp-rotates the globe to the new midpoint.
- [ ] Free rotation, zoom, and recenter button work reliably without visual glitch or projection distortion.
- [ ] Antipodal routes correctly display the trans-polar path and educational badge.
- [ ] Full end-to-end test and preview verification succeed.
