# 3. Center-Weighted City Dots and Always-On Touch Labels

Date: 2026-10-19

## Status

Accepted

## Context

City dots were the first 20 population-ranked places inside the view. Zooming into a sparse region (central Australia, Nevada, the Sahara) showed only large cities at the edges and nothing near the center. Touch devices also have no hover, so labels appeared only after a tap.

## Decision

- The dataset has no population column, so the rank (index in the population-ordered list) is the population proxy.
- Each visible candidate is scored `ln(N / (rank + 1)) - lambda * (r / R) + sticky`, where N is the place count, r the pixel distance from the center of the free viewport area (canvas minus HUD insets), R the inscribed radius of that area (half its shorter side), and `sticky` is 0.3 for places shown in the previous frame. Candidates are taken in score order through the existing minSpacing and route-pin exclusion filter, up to 20.
- R is the inscribed radius, not the half-diagonal. The globe disc never reaches the rectangle's corners, so a half-diagonal R capped in-frame penalties near half of lambda and let rim megacities outscore everything near the center.
- The default lambda scales with the view's angular radius θ (degrees from the center to the edge of the free area): `lambda = 2·ln(N) · clamp((180 − θ) / 100, 0, 1)`. For a hemisphere or tighter (θ ≤ 80°) that is the full 2·ln(N): a rank-1 city at half the inscribed radius ties a rank-N town at the center, so beyond r ≈ R/2 big cities appear only when nothing closer competes. At a whole-world Robinson view (θ = 180°) it is 0, i.e. the global top 20 by rank; a fixed 2·ln(N) there piled every dot into the Americas at the map's center. θ is `asin(R / scale)` on the globe and, in Robinson, half the longitude span of the free area's width measured from the projection's pixels per degree at the equator (`invert()` wraps longitudes to ±180°, so it cannot measure a span across the antimeridian).
- ln(10) and ln(N) were tried first as fixed values and both left a view centered on Australia showing only Asian megacities on the rim (Sydney is rank 155, a log-rank gap of 5 to Tokyo). `selectPlaceDots` itself defaults `centerBias` to 0, so callers that pass nothing keep pure rank order; `options.placeDotCenterBias` on the renderer overrides the zoom-dependent default with a fixed value.
- The sticky bonus damps flicker between near-equal candidates while panning.
- On touch devices (`(hover: none)`, or `?touchlabels=1`), the top 4 dots by score keep name labels, skipping any badge that overlaps another badge or a route pin label. The tap hit radius is 24 px.

## Consequences

- **Positive**: Every zoom level shows dots near the center, and touch users see the most relevant names without tapping.
- **Negative**: Scoring projects every candidate that survives the culling step, about 2 to 3 ms per frame at world view.
- **Trade-off**: Rank is only a proxy, so a city's order relative to a nearby larger one is approximate for ranks the dataset orders loosely (curated locations are appended at the end).
