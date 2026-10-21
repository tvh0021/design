# 4. Neighborhood Thinning and Always-On Labels

Date: 2026-10-21

## Status

Accepted. Amends ADR 0003 (touch-only labels).

## Context

Rank order plus a 22 px minimum spacing let satellites of a megacity fill the dot budget. A Washington to Boston view showed Bridgeport, Oyster Bay and Middletown beside New York, Providence beside Boston, Baltimore beside Washington, and no dots in the free space to the west. Names also showed only on hover, so a desktop user could not read the map at a glance.

## Decision

Four thinning rules were implemented in `selectPlaceDots` and compared on the Washington to Boston, Paris to Berlin and Tokyo to Osaka views in both projections.

- `fixed`: the original 22 px floor only. Cluttered.
- `radius`: a kept dot excludes candidates within `minSpacing + reach * prominence` px, prominence being `ln(N / (rank + 1)) / ln(N)`. With reach 110 it cleared the NYC satellites but also Philadelphia, which sits inside New York's ring, so prominent cities pushed out other prominent cities.
- `soft` (chosen): before ranking, every candidate loses `softWeight * (1 - d / softRadius)` for each higher-scored candidate or route pin within `softRadius` px (160 px, weight 4). A satellite is outscored by a free-standing city but still shows when nothing else is in view. Penalizers are the top `max(300, 15 * limit)` candidates by base score bucketed in a `softRadius` grid, so the cost is linear in the candidate count. The first version penalized only the top 200 candidates; low-ranked satellites outside that pool (Attleboro, rank 9469) then leapfrogged the penalized pool and showed next to Boston, so the penalty now applies to every candidate.
- Label fit: a candidate is skipped unless its name badge fits right or left of the dot without covering a kept dot, kept label, route pin or pin label. On its own this did not reduce clutter (the badges fit between the satellites), so it is not a neighborhood mode but a check applied whenever labels are on. The reserved rect is stored on the dot so the renderer draws the label where it was checked.
- Every kept dot is labeled with its name on every device; hovering or tapping adds the country. The options `placeLabels` and `placeLabelCount` replace `touchLabels` and `placeTouchLabelCount` (kept as aliases).
- The dot budget is one per 45,000 px² of canvas, clamped to 6 to 30, so a phone shows about 7 labeled dots and a desktop up to 30. Label text is 13 px on canvases at least 900 px wide and 11 px below that. `placeDotLimit` still fixes it.
- URL parameters `neighborhood`, `reach` and `spacing` override the mode, radius reach and minimum spacing for comparison; `debug=1` exposes the renderer as `window.__globe`.

## Consequences

- **Positive**: The Northeast view shows New York, Philadelphia, Boston, Washington, Pittsburgh, Toronto, Montreal, Ottawa, Detroit, Cleveland, Columbus, Richmond, Raleigh and Charlotte, each labeled, with Providence, Bridgeport and Baltimore suppressed.
- **Negative**: The radius and weight are in pixels, so the same route thins differently at different canvas sizes. The area-scaled budget hides most of this on phones.
- **Trade-off**: A satellite can still appear when the larger city is just off screen, since only on-screen candidates penalize.
