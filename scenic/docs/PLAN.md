# Scenic route planner: plan

Status: preliminary, 2026-11-02. Nothing built yet. Revised after review (see
"Review changes" at the end), then changed to need no Google API key.

## Goal

Two modes, both handing off to Google Maps on desktop:

1. **A to B.** Take the fastest route and bend it onto prettier roads.
   Target about 30% over the fastest ETA; never go past 50%.
2. **Drive-around.** Start and end at the same place. Given a time budget, suggest
   loops that pass the best scenery and stops within reach.

Scenery means water, hills and views first. Quiet roads (low traffic) are a
smaller bonus.

Scope of v1: New York State, desktop browser handoff only. Rochester to Syracuse
is the first test case, not the product.

## Decisions so far

| Decision | Choice | Why |
|---|---|---|
| Language | Python | Queries are not speed-bound (see "Speed" below). Rust only if profiling says so. |
| Region | All of New York State | Score every road once, offline; queries route on the statewide graph. |
| Handoff | Google Maps URL, desktop | Desktop links take more waypoints than mobile ones (about 9 vs 3; confirm in phase 0). |
| ETA budget | target +30%, hard cap +50% | User choice. |
| ETA accuracy | loose | Our own OSM-based ETA drives the search and sets the baseline. A free public OSRM or Valhalla server gives a second opinion. |
| Google API | none | Too much account setup. Google is used only through Maps links, which need no key and are free. |
| Score weights | hand-set from the user's priorities | Water, hills and views are what the user wants; fitting weights to the byway list would let the byway list decide. |
| Location | `scenic/` on the `scenic` branch | Same pattern as `maps/` and `usage-habit-report/`. |

## How it works

Google Maps cannot be told to prefer pretty roads. A Maps link only takes
waypoints. So we route on our own copy of the road
network (OpenStreetMap), score every road for scenery, find a scenic route, and
then reduce it to a few waypoints that make Google drive the same roads.

### 1. Statewide scenery score (offline, run once)

Every drivable road segment in New York gets a score. Inputs:

| Signal | Source | What it measures |
|---|---|---|
| Water (W) | OSM water polygons, rivers, Great Lakes shoreline; NLCD | Share of the segment within ~200 m of water, weighted up for bigger lakes (log of area). Only counts if the strip between road and shore is open (not forest or buildings in NLCD), so a lake hidden behind cottages scores less than open shoreline. |
| Hills (H) | USGS 3DEP elevation, 30 m | Relief within 1 km of the segment (max minus min elevation), plus how much the road itself climbs and falls. |
| Views (V) | Elevation + land cover | v0 proxy: the road sits higher than its surroundings (topographic position) and the land around it is open, not forest. v1: true line-of-sight sampling every ~500 m on the best candidate roads. |
| Land cover (L) | USGS NLCD | Forest, farmland, pasture, wetland score high; developed land scores low. A mix of field and forest edge scores highest. |
| Quiet (Q), bonus | NYSDOT traffic counts (AADT) on state roads; road class elsewhere | Fewer cars per day. |
| Curves (C), bonus | OSM geometry | Turn density per km. Small weight; it is driving pleasure, not view. |

Combined:

```
raw = 0.35·W + 0.30·H + 0.20·V + 0.15·L
S   = statewide percentile rank of ( raw · (1 + 0.10·Q + 0.05·C) − penalties )
```

S is a percentile (0 to 1) across all NY road length, so a given λ means the
same thing in the flat west as in the Adirondacks.

Penalties: dense intersections and traffic lights, town centers, urban speed
limits. Unpaved and seasonal roads are excluded by default. Motorways get a
mild penalty only: some are genuinely scenic (the Adirondack Northway, NY-17 in
the Southern Tier), so they should not be zeroed out.

Weights live in one config file. Scores are smoothed over about 1 km *along the
same road* (same OSM name/ref, continuing through junctions), so a village
crossroads does not bleed into the lake road beside it.

**Validation**, two checks:

- **Byways.** New York's designated scenic byways (state list plus national ones
  such as the Seaway Trail) are not an input. Their segments should rank high
  in S. This is a coarse check: byways are few and partly political.
- **Hand-labelled sample.** 50–100 segments across the state, rated from Street
  View on a 1–5 scale, kept out of all tuning. Score quality = rank correlation
  between S and the ratings.

A statewide heatmap of S is the visual sanity check.

### 2. A to B

1. **Baseline.** Our own fastest route from A to B gives t₀. No traffic, so
   it is a free-flow ETA, like Google's at 3 a.m.
2. **Budget, as a ratio.** A candidate route with our ETA t passes if
   t / t₀ ≤ 1.3 (target) or ≤ 1.5 (cap). Because both numbers come from the
   same speed model, its errors mostly cancel. The public OSRM demo server
   (router.project-osrm.org, about 1 request per second, light use only) or
   FOSSGIS Valhalla gives a second ETA for the same waypoints; logging the gap
   tells us how loose ours is.
3. **Search.** Edge cost = t · (1 + λ·(1 − S)). λ = 0 is the fastest route;
   larger λ makes unscenic roads more expensive. Costs stay positive for every
   λ, so Dijkstra stays valid. Sweep λ over a grid on the full statewide graph
   (one scipy Dijkstra is about a second) and **keep every distinct route** the
   sweep finds, not just the last one.
   - A λ sweep only finds routes on the convex hull of the time/scenery
     trade-off, so it can jump from +12% straight to +45%. From the kept set,
     take the best route under each budget (+15%, +30%, +50%), then spend any
     unused budget by local improvement: swap one stretch of the route for a
     more scenic detour that still fits.
   - Score a route by **scenic minutes above baseline**: time spent on roads
     whose S beats the fastest route's average S. Filling the budget with roads
     barely prettier than the Thruway then earns nothing.
   - Alternatives: penalize edges already used and search again.
   - No separate penalty for doubling back. The budget already bounds detours,
     and some good routes do loop around a lake.
4. **Waypoints.** Greedy farthest reach: from the current waypoint, find the
   farthest point along our route that a fastest-route engine still follows,
   place the next waypoint there, repeat. This uses the fewest waypoints. Each
   waypoint must snap to the intended road: reject spots with a parallel road
   within ~50 m and move along the road until clear.
   - Our own λ = 0 router stands in for Google; OSRM, a different engine, is a
     second stand-in. Target ≥ 90% overlap within 9 waypoints for both.
   - Spot-check by hand: open a sample of the links in desktop Google Maps and
     see whether Google draws the same roads.
5. **Final check.** Report the OSRM ETA through the waypoints vs t₀. Over +50%:
   drop to the next route down the kept set.
6. **Output.** Route on a map, the three budget options with scenic minutes
   gained, and an "Open in Google Maps" link.

Built (v0.2c, `scenic route`, `scenic.search`). Steps 1–3 follow
`docs/SCORE_V02.md` §4: a λ sweep, edge-reuse alternatives and via-moment
routes, scored by scenic minutes above baseline plus moment credit. The
local-improvement pass in step 3 is not built; the via-moment routes fill
that gap. Steps 4–6 are separate: `scenic.waypoints` makes the Maps link from
a route's `node_path`/`edge_path`. See `docs/SCORE_SPEC.md` "v0.2c" for
results.

Rochester to Syracuse expectation: 390/90 is about 85–90 min, so +30% buys about
25 min, enough for long back-road stretches (Route 5/20 through the north end of
the Finger Lakes, Route 31 along the canal, or a mix with the Thruway). The
algorithm decides; this is what we check it against.

### 3. Drive-around (loop)

Start with anchor loops; add a full solver only if they fall short.

1. **Anchor loops (primary).** Sample 2–3 anchor points at a radius of about
   budget · typical speed / 2.5 around home, at different bearings. Route
   home → a₁ → a₂ → home with the scenic cost from section 2, penalizing reuse
   of roads already driven on this loop. Keep the best loops under budget.
   Sampling anchors from high-S areas (lakeshores, ridges) beats sampling them
   at random.
2. **Stops.** Viewpoints, waterfalls, lake access and parks near the chosen
   loop get attached as optional stops (10–30 min each, or 0 for drive-only),
   if they fit in the remaining budget.
3. **Return 3 loops** that differ in direction or theme (lakes, hills,
   waterfalls), each with ETA, stops and a Google Maps link. One OSRM check
   per loop.
4. **Deferred: OR-Tools orienteering.** Only if anchor loops miss obvious good
   stops. If built, its time matrix must use the scenic leg times, not the
   fastest ones, or tours will overrun the budget.

## Speed

Not a constraint. The statewide scoring is a one-off batch (minutes, vectorised
with numpy/geopandas/rasterio). A query is a λ sweep of about 10–20 Dijkstra
runs on the statewide graph with `scipy.sparse.csgraph` or igraph, so seconds,
plus a few OSRM calls. If that gets slow, cut the graph to the ellipse with A
and B as foci (|AP| + |PB| ≤ v_max · 1.5 · t₀) before searching. The one heavy
piece is true line-of-sight views (v1); if that is slow in numpy, that function
is the candidate for numba or Rust.

## Layout

```
scenic/
  README.md
  pyproject.toml          uv-managed
  config/weights.toml     scenery weights and penalties
  src/scenic/
    fetch.py              download OSM, elevation, land cover, AADT, byways
    graph.py              build statewide drivable graph
    score.py              per-segment scenery score
    route.py              λ sweep, kept route set, budgets, local improvement
    waypoints.py          greedy farthest-reach waypoints, snapping check
    loop.py               anchor loops, stops
    handoff.py            Google Maps URL builder, OSRM check
    cli.py                `scenic route A B`, `scenic loop HOME --hours 2`
  tests/
  data/                   gitignored raw and derived data
  docs/PLAN.md
```

Data size on disk: NY OSM extract a few hundred MB, 30 m elevation and land
cover under 1 GB each. All gitignored.

## Phases

0. **Prove the handoff first.** It is the riskiest piece. Hand-build a
   Rochester to Syracuse route via 5&20 with 5–9 waypoints, open the link in
   desktop Google Maps, and confirm Google follows it and accepts 9 waypoints.
   Note Google's ETA for the fastest route and the scenic one by eye. No API key
   needed.
1. **Data.** Download NY OSM, elevation, land cover, AADT, byway list. Build the
   statewide graph.
2. **Score v0.** Water (with visibility), hills, land cover, view proxy, quiet,
   curves. Statewide heatmap. Byway check. Start the hand-labelled sample.
3. **A to B.** λ sweep, kept route set, three budgets, Rochester to Syracuse end
   to end, routes drawn on a 2D map.
4. **Waypoints + handoff.** Farthest-reach reduction, OSRM check, Maps link,
   hand spot-checks in Google Maps.
5. **Statewide benchmark.** ~50 city pairs across NY (short, medium, long; flat
   west, Finger Lakes, Adirondacks, Catskills, Hudson Valley, Long Island).
   Metrics below.
6. **Loop mode.** ~20 home locations, 1 h / 2 h / 3 h budgets.
7. **Small web page.** Start, end or home, budget, map, Google Maps button.
8. **Later.** True views, fall foliage timing, sun glare, warn on arriving after
   dark, Apple Maps handoff, other states.

## Metrics (benchmark)

- ETA ratio vs fastest (target ~1.3, cap 1.5), by our estimate and by OSRM's.
- Scenic minutes above baseline, per extra minute driven.
- Share of distance on small roads (secondary, tertiary, unclassified).
- Overlap with designated scenic byways.
- Waypoints used (≤ 9) and overlap between the stand-in routes and ours; hand
  spot-check pass rate in Google Maps.
- Our ETA error vs OSRM, by road class.
- Score quality: rank correlation with the hand-labelled sample.

## Risks

- **Loose ETAs, no traffic.** OSM speeds on back roads are guesses, and there is
  no live traffic. The ratio budget cancels most of the error; the OSRM gap and
  the hand spot-checks show how far off we are. If it matters later, add the
  Google Routes API back as an optional check.
- **Score quality.** v0 will mislabel some roads (a rural strip of big-box stores
  on a secondary road). The heatmap, byway check and hand-labelled sample are
  there to catch this.
- **Public server limits.** The OSRM and FOSSGIS servers are free demos with
  fair-use rules. If the benchmark needs more, run OSRM locally in Docker on
  the NY extract.
- **Waypoint limit.** If Google still cuts corners with 9 waypoints on long
  trips, prefer fewer, longer scenic stretches over many short ones.

## Review changes

A review (2026-11-02) led to these changes from the first draft:

- Cost changed from t·(1 − λS) to t·(1 + λ(1 − S)), which never goes to zero.
- Keep every route from the λ sweep and pick per budget, instead of binary
  search, which can miss +30% entirely.
- Score is a statewide percentile; routes earn credit only for scenery above the
  fastest route's average.
- Budget compared as a ratio of our own ETAs, not ours against Google's.
- Waypoints placed by greedy farthest reach with a snapping check, then
  checked against a second engine (OSRM) and by hand in Google Maps.
- Dropped the doubling-back penalty and the mandatory corridor cut.
- Water counts only when visible; curves added as a small bonus; smoothing
  follows the same road.
- Loops built from anchors first; OR-Tools deferred.
- Hand-labelled Street View sample added as the held-out score test.
- Phase 0 now tests the Google handoff before any scoring code.

Not adopted: fitting score weights to byway membership. The weights follow the
user's stated priorities, and byways stay a check rather than a target.
