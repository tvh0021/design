# 04: Frontend Styling, Autocomplete HUD and Flight Telemetry Card

**What to build:**
Implement the user interface and styling (`maps/src/style.css` and `maps/src/hud.js`) strictly adhering to Anthropic's `frontend-design` guidelines. Implement a modern cosmic cartographic theme with high typographic discipline, responsive layout, custom searchable combobox dropdowns for Origin and Destination cities, a 1-click swap button, antipodal warning badge, and a floating telemetry HUD card displaying distance (km and miles), midpoint coordinates, nominal flight duration, and jet-stream wind delay breakdown.

**Blocked by:** 01: Top 10k City Data Pipeline and Search Directory, 02: Spherical Geodesic and Flight Telemetry Engine

**Status:** ready-for-agent

## Acceptance Criteria
- [ ] CSS uses disciplined custom properties (palette: `#070a13`, `#0d1424`, `#1e293b`, `#06b6d4`, `#f59e0b`).
- [ ] Autocomplete dropdown allows keyboard navigation (arrow keys, enter, esc) and instant filtering.
- [ ] 1-click swap button switches origin and destination with smooth visual feedback.
- [ ] Telemetry card renders distance in km and miles, midpoint lat/lon, flight duration, and westerly wind penalty.
- [ ] Antipodal indicator badge displays warning message when `geodesicResult.isAntipodal` is true.
- [ ] UI is fully responsive across desktop and mobile screens.
