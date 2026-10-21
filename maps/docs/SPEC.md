# Geodesic Flight Visualizer Specification

## Problem Statement

When planning or analyzing long-distance global travel, users often rely on flat 2D maps (such as Mercator projections) that severely distort paths, making shortest routes appear counter-intuitive. Previous attempts to calculate and plot great-circle routes and project the Earth orthographically centered on the route's midpoint suffered from numerical instability: linear interpolation of coordinates led to inverted or wraparound arcs, and antipode calculations broke down. Additionally, previous scripts were static Python scripts generating offline images, inaccessible to visitors of a personal web portfolio.

## Solution

A responsive, high-performance, browser-native web application hosted on GitHub Pages that interactively renders an orthographic 3D Earth globe. Users can select an origin and destination from 10,000 global cities and 109 curated geographic and heritage locations through instant-search autocomplete dropdowns. The application immediately calculates the true spherical geodesic arc using 3D unit vectors, computes the spherical midpoint, smoothly slerp-rotates the globe so the midpoint faces the viewer, and displays travel telemetry (distance in km and miles, flight time, and prevailing jet-stream wind penalties). Users can also freely drag-rotate and zoom the globe with touch or mouse controls.

## User Stories

1. As a visitor, I want to open the web page with zero installation, so that I can immediately explore global flight paths on any desktop or mobile browser.
2. As a user, I want to type into the origin search box and see autocomplete suggestions from the 10,000-city dataset and curated locations, so that I can quickly select my departure point.
3. As a user, I want to type into the destination search box and see city and curated location matches, so that I can pick my arrival point without scrolling through thousands of entries.
4. As a user, I want each city suggestion in the dropdown to display its country name, so that I can disambiguate cities with identical names (e.g., Paris, France vs. Paris, Texas).
5. As a user, I want the globe to automatically rotate smoothly to center directly on the midpoint between the two chosen cities, so that the entire flight route is visible in the orthographic hemisphere.
6. As a user, I want to see the shortest-distance geodesic path drawn as an illuminated arc on the globe, so that I can visualize the true curvature and route over continents and oceans.
7. As a user, I want to see prominent pin markers and city labels at the origin and destination, so that I can clearly identify where the route starts and finishes.
8. As a user, I want to view the total geodesic distance in both kilometers and statute miles, so that I have the exact spatial measurement in my preferred unit system.
9. As a user, I want to view an estimated commercial flight duration, so that I have a realistic understanding of travel time.
10. As a user, I want to see how prevailing westerly jet streams affect flight duration, so that I understand why westbound flights take longer than eastbound flights along the same corridor.
11. As a user, I want a 1-click "Swap Cities" button, so that I can quickly reverse the route and observe the flight time change caused by wind direction.
12. As a user, I want to click and drag the globe with my mouse or finger to rotate it freely, so that I can inspect the terrain and path from any angle.
13. As a user, I want to scroll or pinch to zoom into the globe, so that I can inspect specific regional waypoint sections of the flight route.
14. As a user, I want a "Recenter Route" button, so that I can restore the camera view back to the midpoint after rotating or panning away.
15. As a user, I want to select antipodal or near-antipodal cities (such as Madrid and Wellington) and see a clear informational callout badge explaining that multiple shortest paths exist, so that I am informed about the spherical geometry edge case.
16. As an aviation enthusiast, I want antipodal routes to default to a trans-polar route over the North Pole, so that the path aligns with standard polar aviation navigation conventions.
17. As a mobile user, I want the UI controls to collapse into an ergonomic drawer or bottom card, so that the interactive globe remains unencumbered on small screens.
18. As a site owner, I want the application assets to be under 400 KB total, so that the page loads in under 1 second on GitHub Pages.
19. As a user, I want small dots on the most populous cities in the current view (20 on a desktop, 10 on a phone, re-ranked as I pan and zoom), each with its name shown beside it, so that I can orient myself at any zoom level. Selection is weighted toward the view center, so a sparse region (central Australia, Nevada) still shows dots near its middle rather than only large cities on the rim. Dots are spread out: a satellite of a larger city in view (Bridgeport next to New York, Providence next to Boston) yields its slot to a free-standing city, and a dot only appears where its label fits without covering another dot, label or route pin. Hovering or tapping a dot adds the country.

## Implementation Decisions

The default route is Los Angeles, United States → London, United Kingdom.

### Architectural Decisions
- **100% Client-Side Static App**: Vanilla ES modules + D3.js v7 (`d3-geo`, `topojson-client`). No Node runtime or backend server required. Hosted directly via GitHub Pages or embeddable in an `<iframe>`.
- **Compact Search Dataset**: Keep the top 10,000 cities from `worldcities.csv` in population order, then append 109 curated geographic and heritage locations. The North and South Poles are excluded because routes ending at a pole render poorly in the Robinson projection. Each entry uses `[name, country, lat, lng]`.

### Mathematical & Algorithmic Contracts
- **Spherical Coordinates**: Standard WGS84 spherical conversion:
  $$\mathbf{v} = (\cos \phi \cos \lambda, \cos \phi \sin \lambda, \sin \phi)$$
  where $\phi$ is latitude and $\lambda$ is longitude in radians.
- **Great Circle Distance**: Great-circle angular distance $\sigma$ computed via the Haversine formula or spherical arccos with clamping to $[-1, 1]$:
  $$\sigma = \arccos\left(\sin \phi_1 \sin \phi_2 + \cos \phi_1 \cos \phi_2 \cos(\lambda_2 - \lambda_1)\right)$$
  $$D = R_{\text{earth}} \cdot \sigma \quad (R_{\text{earth}} = 6371.0088\text{ km})$$
- **Spherical Midpoint**:
  $$\mathbf{v}_{\text{mid}} = \frac{\mathbf{v}_1 + \mathbf{v}_2}{\|\mathbf{v}_1 + \mathbf{v}_2\|}$$
  $$\phi_{\text{mid}} = \arcsin(z_{\text{mid}}), \quad \lambda_{\text{mid}} = \text{atan2}(y_{\text{mid}}, x_{\text{mid}})$$
  When $\|\mathbf{v}_1 + \mathbf{v}_2\| < 10^{-4}$ (antipodal), fallback midpoint is set to North Pole $(\phi = 90^\circ, \lambda = 0^\circ)$.
- **Directional Jet Stream Penalty**:
  $$\Delta \lambda_{\text{net}} = (\lambda_2 - \lambda_1) \pmod{360}$$
  Determine zonal direction: heading with a westerly component against prevailing jet streams adds a penalty factor proportional to cosine of average latitude:
  $$\text{Penalty Factor} = 1.0 + 0.12 \times \max(0, -\text{zonal\_component})$$

### Visual & Aesthetic Identity (Anthropic Frontend Design Standards)
- **Palette**: Deep cosmic indigo background (`#070a13`), globe ocean depth (`#0d1424`), continental landmass (`#1e293b`), graticule gridlines (`rgba(255,255,255,0.06)`), glowing cyan geodesic trajectory (`#06b6d4`), amber waypoint markers (`#f59e0b`).
- **Typography**: Space Grotesk / Inter for modern telemetry display and legible geographic labels.
- **No AI Slop**: Clean layout, no gratuitous purple gradients, no floating card clutter, and disciplined information hierarchy.

## Testing Decisions

- **Test Surface**: The public interface of `GeodesicEngine` (`computeGeodesic`) and `CityDirectory` (`search`).
- **Test Cases**:
  - Exact antipodal pairs (e.g. [0, 0] and [0, 180]): verify `isAntipodal: true` and trans-polar path generation without NaN.
  - Same origin and destination: verify distance `0` and empty or degenerate path.
  - Antimeridian crossing (e.g. Tokyo to San Francisco, Fiji to Hawaii): verify path does not wrap the long way around $360^\circ$.
  - Equator-straddling routes: verify midpoint latitude and longitude.
  - Jet stream penalty: verify Westbound flight time > Eastbound flight time for London $\leftrightarrow$ New York.
  - City search: case-insensitivity and ranking by city prefixes in population order, curated place prefixes, then substring matches.
  - Search dropdown: 2-character minimum, 100 ms debounce, at most 10 results, and hints for one character or no matches. Editing or clearing search text retains the selected route until a new selection; blur restores its display label.
  - Responsive framing: both projections use the viewport area left free by the desktop panel or mobile sheet.

## Out of Scope

- Real-time live air traffic (ADS-B) feeds.
- Full 3D terrain elevation or bathymetry heightmaps (orthographic vector coastlines and country borders only).
- Dynamic commercial airline scheduling or ticket pricing.

## Further Notes

- The resulting application directory in `maps/` should include `index.html`, `app.js`, `geodesic.js`, `directory.js`, `style.css`, and the bundled dataset `cities10k.json` plus `world-110m.json` (base borders, always used while dragging) and `world-50m.json` (simplified 50m borders, drawn on idle frames only), the 4096x2048 `etopo.jpg` base terrain texture, and the 8192x4096 `etopo-8k.jpg` texture that replaces it after first paint on GPUs whose MAX_TEXTURE_SIZE is at least 8192.
