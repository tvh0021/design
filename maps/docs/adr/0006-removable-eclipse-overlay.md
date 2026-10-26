# 6. Removable Eclipse Overlay (2 August 2027)

Date: 2026-10-25

## Status

Accepted. Adds an optional, self-contained map layer.

## Context

The map should show where the total solar eclipse of 2027 August 2 will be seen: the path of totality, with the duration of totality and the local time of mid-totality marked along the central line. (A first iteration also shaded the region of partial eclipse; it was too faint to read and was removed.) It is a dated, one-off feature, so it must be easy to switch off for users and easy to delete from the codebase later without leaving residue in the renderer or HUD.

## Decision

- **Data**: `maps/scripts/build_eclipse.py` computes the ground track from NASA GSFC's published Besselian elements (Fred Espenak) using the Meeus ellipsoidal reduction, with no third-party Python packages. The umbral limits and central line are traced analytically (tangent-grazing condition against the rotating ground). Markers are placed every 1000 km along the central line either side of greatest eclipse; each carries the central-line duration of totality (umbra diameter over ground-relative speed, matching NASA's table to 0.1 s) and the clock time of mid-totality, using the civil UTC offset in force on 2 August 2027 for Spain, Morocco, Algeria, Tunisia, Libya, Egypt, Saudi Arabia and Yemen, and nautical time (round(lon/15)) at sea. Output is `maps/data/eclipse-2027-08-02.json` (GeoJSON, ~24 KB) with features `total`, `central`, `greatest` and fourteen `marker`s.
- **Validation**: `python3 maps/scripts/build_eclipse.py --verify` compares the computed northern limit, central line and southern limit against NASA's path table at five instants. Worst deviation is 2.6 km. The 1000 km dots appear only from about 7 px per degree at the view centre and their duration/local-time badges from about 12 px per degree (the default globe sits near 4), so the default view shows only the band, the central line and the greatest-eclipse dot. Badges that would overlap one already drawn are skipped. Contact times (P1 07:30:07 UT, totality 08:25:03–11:48:18 UT, P4 12:43:11 UT) and greatest eclipse (25.5°N 33.2°E, 10:06:38 UT, 6m23s) match NASA's circumstances.
- **Rendering**: `src/eclipse.js` exports `createEclipseOverlay`, which draws between the political borders and the city dots so the route, pins and labels stay on top. It normalises ring winding with `d3.geoArea` so d3 never fills the complement of a polygon. The globe renderer exposes one hook, `setEclipseOverlay(overlay)`, and otherwise knows nothing about eclipses.
- **Control**: the HUD builds an "Eclipse 2027" pill beneath the projection toggle only when given an `eclipseToggle` option. Switching it on by click also opens a short description card (title, three lines, dismiss button) whose numbers come from the dataset via `eclipseToggle.getDescription()`. The overlay starts hidden; `?eclipse=1` starts it visible. `initApp({ eclipseUrl: null })` omits the feature entirely.
- **Removal path**: every line outside `src/eclipse.js` is tagged `[eclipse-overlay]`. To remove the feature: delete `src/eclipse.js`, `data/eclipse-2027-08-02.json`, `scripts/build_eclipse.py`, `tests/test_eclipse.js`, this ADR, then delete the tagged blocks found by `grep -rn "eclipse-overlay" maps/ package.json`.

## Consequences

- **Positive**: Accurate, source-backed eclipse geometry with no runtime dependency beyond the already-loaded d3; one toggle for users; one grep for maintainers.
- **Negative**: One more fetch on load (24 KB) and a second floating control at the top-right of the viewport.
- **Trade-off**: Local times at sea use nautical time rather than any ship's clock, and the land zones are coarse bounding boxes, adequate for a one-line label but not for a timetable.
