# Maps for Wanderers

Scenic drives across New York State: a route planner that finds the most
scenic route under a time budget. See `docs/PLAN.md` for the full plan.

The product is called Maps for Wanderers. The code keeps its working name: the
Python package is `scenic`, the command is `scenic`, and the folder and
branch are still `scenic`.

- `scenic serve` runs the interactive planner on your own machine
  (`docs/WEB.md`).
- `scenic demo build` writes a static demo site with five preset routes,
  for GitHub Pages (`docs/PUBLISH.md`).

So far it does phase 1 for OpenStreetMap only. It downloads the New York OSM
extract, builds a statewide graph of drivable roads, and finds the fastest
route between two points. It also gives every road edge a scenery score
(v0.2, `docs/SCORE_SPEC.md`; the v0.1 and v0.2 changes are listed at its end,
and `docs/SCORE_V02.md` gives the reasons for v0.2).

## Setup

```
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
```

Data goes in `data/` (or the folder in `$SCENIC_DATA`). It is not committed.

## Commands

```
scenic fetch
```

Downloads `new-york-latest.osm.pbf` (about 500 MB) from Geofabrik into
`data/raw/`. Skips the download if the file is already there and its md5
matches Geofabrik's. Checks the md5 after every download.

```
scenic build-graph
```

Builds the drivable road graph into `data/graph/`: `edges.parquet` (one row
per road segment between junctions, with geometry, OSM way id, name, ref,
class, surface, oneway, length, speed and travel time) and `nodes.npz`
(junctions). Takes under a minute and about 4 GB of memory.

```
scenic fastest --from 43.1566,-77.6088 --to 43.0481,-76.1474 --geojson route.geojson
```

Prints the fastest route's minutes, km and main roads. `--geojson` also saves
the route line.

```
scenic route --from 43.15729,-77.61521 --to 43.04812,-76.14742 \
    [--geojson routes.geojson] [--png routes.png] [--json routes.json]
```

Finds the fastest route and the most scenic route under +15 %, +30 % and
+50 % of its time (`docs/SCORE_V02.md` section 4). It needs `scenic score`
to have run, for `scores.parquet` and `moments.parquet`. For each of the four
routes it prints minutes, ratio to the fastest, km, scenic minutes, the
moments credited (type, value, name and place) and the main roads.
`--geojson` writes the four routes, each with its ordered `edge_path` and
`node_path` (indices into `edges.parquet` and `nodes.npz`), plus the credited
moments as points. `--png` draws a map and `--json` saves the table. A query
takes 1 to 4 s after about 3 s of loading. The settings are in the `[route]`
section of `config/score.toml`.

How it searches (`scenic.search`):

1. One forward Dijkstra from A and one reverse Dijkstra from B, both on
   travel time, give the fastest time t0. They also give the nodes some
   route under 1.5 t0 can reach; all later searches run on those nodes only.
2. A λ sweep, cost = t (1 + λ (1 − S)) for λ from 0.5 to 20. Each distinct
   route under the cap is kept.
3. Alternatives. The sweep runs again for λ 1, 3 and 8, with edges of routes
   already kept costing twice as much. This runs for two rounds.
4. Via-moment routes. The two trees from step 1 give t(A→M) + t(M→B) for
   every moment at once. Moments under the cap are ranked by m / extra
   minutes, keeping one per named lake or river. The top 12, and the top 4
   in each budget band, seed routes A → M → B. Each is built twice, with
   λ = 1 legs and with fastest legs. A second and a third moment are then
   added greedily at their best insertion point while the time stays under
   1.5 t0.
5. Every kept route is scored as `scenic_min + 5 · moment_pts`:
   - `scenic_min` is Σ t·max(0, S − S_fast) / 60, where S_fast is the
     fastest route's time-weighted mean S.
   - `moment_pts` is the sum of m² over the moments the route passes, within
     100 m or by driving one of the moment's edges. Moments are taken best
     first, at most one per 2 km of route, and the same named lake or river
     at most once per 10 km (a bay counts as its lake). The total is capped
     at 8 moments per hour of t0, and at least 3.

   The best route under each budget is picked. Budgets are our own ETA ratio
   to our own fastest route. When a larger budget's best route is a cheaper
   pick or shares more than 80% of its length with one, the larger budget
   takes the best different route scoring at least 85% of its best; with
   none, the pick is marked shared.

```
scenic calibrate [--features] [--fit]
```

Writes `data/graph/edge_delays.parquet`: per-edge counts of traffic signals,
stop signs, crossings and junctions, a built-up flag, and the travel time
with delays. It has one row per `edges.parquet` row, in the same order, and
`fastest` uses its `time_s` when the file is there. `build-graph` writes it
too. `--features` recounts from the PBF (about 25 s). `--fit` refits the delay
parameters against nine Google Maps ETAs (`GOOGLE_TRIPS` in calibrate.py) and
the OSRM demo server, runs a leave-one-out check, and writes `delay_fit.json`. OSRM replies are cached in `~/.cache/scenic-calibrate/`.

```
scenic fetch-layers [--only dem nlcd byways]
```

Downloads the score's input layers into `data/raw/`: the 53 USGS 3DEP
1 arc-second DEM tiles that cover New York (2.6 GB, md5-checked against the
S3 ETag), the Annual NLCD 2023 land cover zip from MRLC (1.4 GB, extracted
to a 1.4 GB GeoTIFF) and the NYSDOT scenic byway lines (GeoJSON, used only
to validate the score). Downloads resume from a `.part` file, and files
already present are skipped. `data/raw/layers.json` records what was fetched.

```
scenic score [--force [STAGE ...]]
```

Scores every edge in `data/graph/edges.parquet`. Settings are in
`config/score.toml`. The stages are grid, dem, nlcd, water, water90,
terrain90, land90, controls, edges, score, outputs, osm_features, moments,
validate and validate_routes. Each one
runs in its own process and caches its result in `data/rasters/` or
`data/score/`. A stage is skipped while its fingerprint is unchanged and its
outputs are the files it last wrote. The fingerprint hashes the stage's code
(its body and the scenic modules it calls), the `config/score.toml` sections
it reads and the contents of its input files, so editing a formula or a
setting reruns the stages it touches, and a stage whose output changes
reruns every stage that reads it. Fingerprints live in `.stages/` next to
each stage's outputs. `--force` reruns all stages, or just the ones named. A full run takes
about 3.5 minutes; the water90 stage peaks at about 13.5 GB of memory and the
water and edges stages at about 9.6 GB. It writes these files to
`data/score/`:

- `scores.parquet`: one row per edge, in the same order as `edges.parquet`,
  with S, the six signals (W, H, V, L, Q, C), penalty, raw, smooth and
  excluded.
- `heatmap.png`: a statewide map of S.
- `summary.md`: distributions of the signals and how they correlate.
- `validation.json`: the byway, spot-check and Rochester to Syracuse checks,
  and the v0.2 checks (V3 split dull lists, V4 byways, lake fronts).
  The spot checks are listed in `config/spot_checks.toml`, which also holds
  a holdout set (10 scenic, 5 dull) that was fixed before v0.1, and
  holdout-2 (10 scenic, 5 urban, 3 flat, 4 OD pairs), fixed before v0.2.
  Holdout-2 is skipped until `declared_final = true` is set in
  `[holdout2]`; then it is scored once.
- `osm_features.parquet`: OSM viewpoints, waterfalls, cliffs, bridge way
  ids, named lakes (0.2 km² and up), named rivers and places. Used to find
  and name moments.
- `moments.parquet`: scenic moments (`docs/SCORE_V02.md` section 4, with
  the v0.3 filters in `docs/SCORE_SPEC.md`). Water moments at stormwater
  basins, small ponds, built-up streets and motorways are dropped unless the
  water is a large named lake or a wide river. Water takes the name of the
  most specific body (Irondequoit Bay, not Lake Ontario). A vista needs a far
  view in at least three directions and stands at least 5 km from the next. Each
  row has an id, a type (water, vista, gorge, osm), a value m, a point,
  its best edge and member edges, the road, the named feature, the water
  body it belongs to (`parent_feature`: Lake George for Huddle Bay), the
  nearest place and a label.
- `route_validation.json`: the route checks of `docs/SCORE_V02.md`
  section 1. V1 runs the full search on eight corridors, V2 pairs each scenic
  stretch with the fastest route between its ends, and V5 checks hand-named
  moments and non-moments. The holdout-2 OD pairs and stretches are routed
  only when `routes_sealed = false` is set in `[holdout2]`.
- `edge_signals.parquet`: per-edge signals and their parts (W, H_relief,
  H_grade, V_prospect, V_water, turn_deg, water_dist_m and so on).
- `score_report.json`: inputs, versions, run time and peak memory per
  stage, deviations from the spec, and the validation results.

Known limitations of the score pipeline:

- The water90 stage peaks at about 14.8 GB of memory (the 1 km and 3 km disk
  filters on the full grid). It fits a 64 GB machine; on a 16 GB one it
  would need to be tiled.
- `fetch-layers` skips a file that is already downloaded when its size
  matches the server's, without re-checking its md5.

## Road model

- Roads: motorway, trunk, primary, secondary, tertiary, unclassified,
  residential, living_street and their `_link` roads. No service roads or
  tracks. Roads closed to private cars (`access`, `motor_vehicle` = no or
  private) are left out.
- Oneway: `oneway=yes/-1` as tagged. Motorways, motorway links and
  roundabouts are oneway unless tagged otherwise.
- Speed: the `maxspeed` tag. "N mph" is mph and "N km/h" is km/h. A bare
  number is km/h (the OSM convention), except that on a US road (outside a
  rough Canada outline) bare 25, 30, 35, 45, 55 and 65 are read as mph, since
  US mappers often leave off the unit. With no usable tag, a
  default per class: motorway 65, trunk 55, primary 50, secondary 45,
  tertiary 40, unclassified 35, residential 25, living street 15, links 35.
- Delays (`graph.DELAY_PARAMS`), added per edge:
  `time = free_flow * f + 23.5 s * (signals + crossings / 2) + 8.55 s * stops + 2.96 s * minor_ends`.
  `f` is 0.983 on motorways and 1.072 in built-up areas. Outside built-up
  areas it is 1.087 on trunk, primary and secondary roads and 0.815 on
  tertiary and smaller roads (`minor_road`). Most small roads have no
  `maxspeed` tag, and their class defaults (tertiary 40 mph, unclassified 35)
  are slower than Google drives rural county roads, so their factor is below 1.
  Signals and stops come from OSM nodes (`highway=traffic_signals`,
  `highway=stop`). A one-direction sign counts half. A two-way stop on a
  junction only counts for the smaller road. Crossings are signalised
  pedestrian crossings more than 40 m from a signal, plus rail level
  crossings. A control at a junction counts half on each edge there, so
  driving through pays it once. `minor_ends` counts junctions where the edge
  meets a bigger road. Built-up means 12 or more junctions within 300 m.
  Motorways get no delays.
- Fit (`scenic calibrate --fit`): against nine Google Maps ETAs, all
  checked by hand on the evening of 2026-11-02 and kept in
  `calibrate.GOOGLE_TRIPS` with Google's miles. They are phase 0's fastest
  Rochester to Syracuse route and back-road routes A, B and C, plus five
  20 to 60 mile trips across upstate. Ours / Google:

  | Trip | Google | Earlier fit | Now | Leave-one-out |
  |---|---|---|---|---|
  | Fastest | 87 min, 87.2 mi | 1.012 | 1.000 | 1.005 |
  | A | 150 min, 105 mi | 1.002 | 1.013 | 1.031 |
  | B | 138 min, 105 mi | 1.027 | 1.003 | 1.004 |
  | C | 127 min, 103 mi | 0.960 | 0.993 | 0.990 |
  | Potsdam–Massena | 30 min, 20.4 mi | 0.919 | 0.985 | 0.927 |
  | Ulster–Arlington | 40 min, 25.5 mi | 0.894 | 0.983 | 0.975 |
  | Ashford–Caneadea | 36 min, 27.3 mi | 1.103 | 0.986 | 0.973 |
  | Richmond–Howard | 43 min, 34.3 mi | 1.094 | 1.005 | 1.008 |
  | Lindley–Scio | 72 min, 58.9 mi | 1.018 | 1.030 | 1.045 |

  Leave-one-out refits without each trip and predicts it: median error
  2.5%, worst 7.3% (Potsdam–Massena). Before the split into `rural` and
  `rural_minor`, one rural factor could not make the town trips slower and
  the county-road trips faster at once: the best fit left Potsdam–Massena at
  0.94 and Ashford–Caneadea at 1.04, and the worst leave-one-out error
  was 8.6% or more, however Google and OSRM were weighted.
  OSRM is not a direct check: its car profile drives at about 0.8x the
  posted limit, charges about 2 s per signal and has its own slow defaults
  for small roads. So OSRM trips (40 village-to-village trips with little
  motorway) enter with a free scale factor, are routed and scored with
  OSRM's own 2 s per signal, and weigh 0.1 each against 10 for each Google
  trip. Where the two disagree, the fit follows Google. The per-signal,
  per-stop and per-junction delays are held near traffic-engineering values
  (15, 8, 3 s) by weak priors, because Google cannot tell signal delay from
  a general town slowdown. Each round of the fit routes every trip with the
  current parameters, and a path change can send the next round somewhere
  worse, so the fit keeps the round with the lowest error on its own routes.
  No parameter ends at a bound, in the full fit or in any leave-one-out fit.
  `delay_fit.json` has the full report.
- Known limitations:
  - A signal or stop sign tagged for one direction (forward or backward)
    counts half in both directions, instead of in full in its own direction
    and not at all in the other.
  - A two-way stop at a junction charges half a stop to every edge of the
    smaller road that touches it. It should charge a full stop when you
    drive up to the junction on that road, and nothing when you drive away
    from it.
  - Potsdam–Massena misses the ±7% leave-one-out target (0.927). Its slow
    stretches are villages on trunk road with no `maxspeed` tag (Norwood,
    the edge of Massena), driven at the 55 mph default and below the
    built-up threshold, so only its signals slow it down. It is the only
    trip of its kind, so leaving it out loses the evidence for it.
  - Three trips take a route whose length differs from Google's by more
    than 5%, so they compare our fastest path with a different Google path:
    B (116.6 mi against 105: the Thruway between Geneva and Auburn),
    Ulster–Arlington (27.3 against 25.5: the Kingston–Rhinecliff Bridge) and
    Lindley–Scio (53.2 against 58.9: NY 417 and county roads). The times
    still bound the error, since each side picked its fastest path, but they
    do not say which road is wrong.
  - `rural_minor` also speeds up small roads that do carry a `maxspeed` tag
    (22% of rural tertiary, 9% of unclassified), so a tertiary road tagged
    55 mph is driven at about 67 mph.

## Tests

```
.venv/bin/python -m pytest
```

The tests use small hand-made graphs and synthetic rasters, and need no
downloaded data or network.

## Waypoints

`scenic.waypoints` turns a target route into a Google Maps link (PLAN.md 2.4).
There is no CLI command for it yet.

```python
from scenic.route import RoadGraph
from scenic.waypoints import reduce_to_waypoints, route_via

g = RoadGraph.load()
target = route_via(g, [(43.15729, -77.61521), (42.87303, -76.97127), (43.04812, -76.14742)])
res = reduce_to_waypoints(g, target)  # a node path, a Route, or edge_path=[...]
res.summary()  # waypoints, overlap, ETA vs the target's, U-turn and weak-spot counts
res.url        # https://www.google.com/maps/dir/?api=1&...&waypoints=a%7Cb%7Cc
```

Our fastest router stands in for Google. From each anchor (the origin, then
each waypoint) one Dijkstra gives the fastest path to every node. A binary
search over the target's nodes finds the farthest one that this path reaches
along the target: at least 98% of the leg by length, or no more than 100 m
off it. The next waypoint goes just before that point, and the search
repeats until the destination is reached.

A waypoint must sit mid-edge, at least 150 m in a straight line from a
junction, with no other drivable road within 50 m. Its own road's next
segments and its other carriageway don't count, but on a divided road the
other carriageway must be at least 25 m away. If the farthest spot fails,
the search slides back along the route. If no spot passes, it takes the best
fallback: first a spot near a junction, then one near another road. Each
waypoint records which rules it meets (`tier`).

On a two-way road, a waypoint can be reached and left in either direction.
Each leg is routed over all four combinations. A spot is rejected if turning
round, or arriving from the far side, is faster than driving straight
through or within max(30 s, 5%) of it. Merely driving back over the
waypoint's own edge doesn't count as turning round.

A dynamic program then drops greedy waypoints that a later one made
redundant, and keeps at most `max_waypoints` (default 9). When more are
needed, it keeps the subset that loses the least overlap. The coordinates
are rounded to 5 decimals and snapped back to the nearest road, and the
report comes from that: overall overlap with the target, ETA, and per-leg
times next to the target's.

Google weighs roads a little differently from our router, so by default
each leg is checked with the target's own roads 10% slower
(`time_margin=0.1`). This adds waypoints, including spurious ones in city
grids where parallel streets tie. Waypoints are also at most 15 km of scenic
road apart (`max_gap_m=15_000`). Scenic road is any part of the target the
plain fastest route doesn't use; pass `scenic=S, s_fast=...` to count only
roads scenier than the fastest route, as the web app does. When 9 waypoints
can't meet both rules, the reducer keeps the ones that leave the least scenic
road at risk. The notes then list each leg still at risk, worst first, with
its main road and how much scenic road it leaves uncovered or a slightly
different router could skip. `summary()` reports `risk_legs` and
`max_scenic_gap_km`.

`route_via(g, points)` builds a target through (lat, lon) points. Each inner
point snaps to its nearest edge and the route drives along that edge. The
direction along each edge is chosen for the whole trip, so the route only
turns round when that really is cheapest.

Known limitations:

- Each leg is checked against our router's single fastest path. A
  near-tie with another road, which a different engine could pick, is only
  caught through `time_margin`. In phase 0 variant B, our router stays on
  5&20 from west of Geneva to Auburn until 5&20 is made about 15% slower;
  Google left it.
- Service roads and parking aisles are not in the graph, so the 50 m rule
  can't see them, and Google may snap to them.
- The edge search index takes about 4 s and 1.3 GB to build on the
  statewide graph, once per process.
