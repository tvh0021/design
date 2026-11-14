# Maps for Wanderers

Maps for Wanderers plans scenic drives across New York State. Pick a start and
an end, or a start point and a time budget, and it searches a statewide
OpenStreetMap road graph scored from elevation, land cover, water views, and
mapped landmarks, then builds an editable Google Maps link for the drive you
pick.

The Python package, CLI command, folder, and branch keep the working name
`scenic`. `docs/PLAN.md` covers the project plan, and `docs/SCORE_SPEC.md` and
`docs/SCORE_V02.md` specify how the pipeline scores road edges and scenic
stops.

## Use it in your browser

### 1. Install `scenic`

With Python 3.11 or newer, install directly from GitHub without cloning using
`uv` or `pipx`:

```sh
uv tool install "git+https://github.com/tvh0021/design.git#subdirectory=scenic"
```

Or with `pipx`:

```sh
pipx install "git+https://github.com/tvh0021/design.git#subdirectory=scenic"
```

Inside a local clone of this repository, create a virtual environment in
`scenic/` instead:

```sh
cd scenic
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
source .venv/bin/activate   # puts `scenic` on your PATH
```

### 2. Prepare the New York route data

Download the prebuilt New York data (about 160 MB; the checksum is verified
before anything is unpacked):

```sh
scenic setup --prebuilt
```

The data is derived from OpenStreetMap (© OpenStreetMap contributors, ODbL)
and is published as a
[GitHub release](https://github.com/tvh0021/design/releases/tag/wanderers-data-v0.2).

If you have another prebuilt runtime archive containing `nodes.npz`, `edges.parquet`,
`edge_delays.parquet`, `scores.parquet`, and `moments.parquet`, unpack and
verify it in a few seconds:

```sh
scenic setup --bundle /path/to/scenic-ny-runtime.tar.gz
```

`--bundle` accepts a local `.tar.gz`, `.tar`, or `.zip` file or an `https://`
URL, about 163 MB compressed.

To download the public source datasets and build everything locally in one
idempotent command instead:

```sh
scenic setup
```

A cold build downloads about 5.0 GB from Geofabrik, USGS 3DEP, MRLC Annual NLCD
2023, NYSDOT scenic byways, and Census TIGER state boundaries. It uses about
7.2 GB of disk, runs for 6 to 10 minutes, and peaks near 15 GB of RAM during
raster scoring. Re-running `scenic setup` skips finished downloads and
up-to-date stages. Run `scenic setup --check` at any time to check which files
are present.

Where `scenic` stores data:

- `$SCENIC_DATA`, when set in your environment.
- `./data` inside a git checkout of `scenic/`.
- `~/.local/share/scenic`, or `$XDG_DATA_HOME/scenic`, when installed as a
  standalone tool via `uv tool` or `pipx`.

### 3. Open the map

```sh
scenic serve --open
```

`scenic serve` binds only to `127.0.0.1` on port `8765` by default, loads the
825,892-edge road graph and scenery scores in about 1 second, and opens
`http://127.0.0.1:8765/` in your default browser. Options:

- `--open` or `-o` opens the map in your browser as soon as the server listens.
- `--port PORT` selects another local port when `8765` is busy.
- `--setup` runs `scenic setup` first if route data is missing.
- `--graph DIR` and `--score DIR` point at custom `graph/` and `score/`
  directories.

To browse preset routes without running a Python server, `scenic demo build`
writes a static site with five A-to-B drives and Finger Lakes drive-around
loops, documented in `docs/PUBLISH.md`.

## Using the browser planner

The left panel holds two planning tabs, `mi` and `km` unit buttons, route
comparisons, scenic stops, and Google Maps links. `docs/WEB.md` documents the
HTTP API and security model.

### A to B tab

1. **Set Start `A` and End `B`.** Click the map once for `A` and again for `B`,
   drag either marker to adjust, paste `lat, lon` coordinates, or type a New
   York town or landmark and press **Search**. Before your first search, the
   starter buttons **Rochester to Watkins Glen**, **Kingston to Oneonta**, and
   **Lake George to Lake Placid** run a sample drive in one click.
2. **Compare time budgets.** Click **Find routes**, or press `Cmd+Enter` on
   macOS or `Ctrl+Enter` on Windows and Linux. In about 3 seconds, the map and
   table compare the **fastest** route in dashed grey against the most scenic
   route within `+15%`, `+30%`, and `+50%` of the fastest travel time in blue,
   orange, and reddish purple. When two budgets pick the same path, they merge
   into one row such as `+30% = +50%`. **Swap** reverses `A` and `B`, and
   **Clear** resets the tab.

### Drive-around tab

Switch to **Drive-around** to find up to three circular drives that start and
finish at pin `S` without retracing their own roads, documented in
`docs/LOOP_SPEC.md`. When `A` is already set, opening **Drive-around** copies
`A` into `S` the first time.

1. **Pick a duration.** Choose `1 h`, `2 h`, `3 h`, or enter a custom budget
   from `30` to `360` minutes.
2. **Choose Which way:**
   - **Any way** spreads up to three loops across compass directions from `S`.
   - **A direction** opens an 8-point compass rose from `N` through `NW` and
     steers loops into that sector.
   - **Past a place** shows clickable chips for nearby lakes, gorges, falls,
     and viewpoints reachable within your time budget. You can also search a
     place or scenic feature by name, or click the map to place anchor pin `P`.
     Clicking open water snaps `P` to the nearest mapped lake or river shore.
3. **Run Find loops.** The starter buttons **Auburn**, **Ithaca**, and
   **Lake Placid** run a 2-hour sample loop in one click. Switching between
   **A to B** and **Drive-around** preserves each tab's pins and results.

### Reading results and opening Google Maps

- **Table and scenery strips.** Each row lists drive minutes, extra minutes over
  the fastest route on **A to B**, distance, and **scenic minutes**, above a
  start-to-end color strip and the route's main roads. Click a row or map line
  to select it, or focus a row and press `Enter`, `Space`, `Up`, or `Down`. The
  checkbox on each row hides or shows that route on the map.
- **Color by scenery.** Checked by default, this setting colors the selected
  route in five YlGnBu bands representing fifths of New York road miles ranked
  by scenery score `S`, from pale yellow in the bottom fifth to dark blue in
  the top fifth.
- **Scenic stops.** White dots on the map and the numbered list in the detail
  pane list credited water views, vistas, gorges, and landmarks in drive order,
  with minutes from the start and a Google Maps satellite link. Consecutive
  stops along one lake or river fold into one expandable line.
- **Google Maps link.** Click **Get a Google Maps link** on the selected route
  to place mid-road waypoints that keep Google's router on the scenic path and
  report the route overlap percentage. Google Maps locks route editing above 9
  total stops, which means 7 intermediate waypoints, so longer drives and loops
  split into editable **Part 1**, **Part 2**, ... links joined at a numbered
  handoff waypoint.
- **Home, links, keyboard, and mobile.** **Set as home** stores `A`, `B`, or `S`
  in browser `localStorage` under `scenic.home`, kept on your own machine, and
  **From home** fills `A` or `S`. The URL hash records your pins, mode, and
  selected route for bookmarking. Search dropdowns accept `Up`, `Down`,
  `Enter`, and `Escape`. Narrow screens stack the panel above the map and show
  a **Larger map** toggle in the header.

## Command-line reference

All commands read from `$SCENIC_DATA` when set, `./data` inside a repo
checkout, or `~/.local/share/scenic` in a standalone install.

```
scenic setup [--data DIR] [--bundle PATH_OR_URL] [--check] [--force]
```

Runs the full New York data pipeline across `fetch`, `build-graph`,
`fetch-layers`, `boundary`, and `score`, checks readiness with `--check`, or
unpacks and verifies a prebuilt runtime archive with `--bundle`.

```
scenic serve [--port 8765] [-o | --open] [--setup] [--graph DIR] [--score DIR] [--verbose]
```

Runs the local browser planner on `http://127.0.0.1:8765/`, documented in
`docs/WEB.md`. When required graph or score files are missing, `scenic serve`
prints setup instructions and exits unless `--setup` is passed.

```
scenic demo build [--out DIR] [--presets FILE] [--graph DIR] [--score DIR]
```

Runs the preset drives and loops in `config/demo_routes.toml` and writes a
static demo site, documented in `docs/PUBLISH.md`.

```
scenic fetch [--url URL] [--force]
```

Downloads `new-york-latest.osm.pbf` (about 500 MB) from Geofabrik into
`data/raw/`. Skips the download if the file is already there and its md5
matches Geofabrik's. Checks the md5 after every download.

```
scenic build-graph [--pbf FILE] [--out DIR]
```

Builds the drivable road graph into `data/graph/`: `edges.parquet` (one row
per road segment between junctions, with geometry, OSM way id, name, ref,
class, surface, oneway, length, speed and travel time), `edge_delays.parquet`,
and `nodes.npz` (junctions). Takes under a minute and about 4 GB of memory.

```
scenic fastest --from 43.1566,-77.6088 --to 43.0481,-76.1474 [--geojson route.geojson]
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
scenic loop --from 43.1566,-77.6088 [--minutes 120] \
    [--direction N|NE|E|SE|S|SW|W|NW | --anchor LAT,LNG | --feature "Lake Ontario"] \
    [--geojson loops.geojson] [--png loops.png] [--json loops.json]
```

Finds up to three scenic drive-around loops starting and ending at `--from`
within `--minutes` (30 to 360, default 120; `docs/LOOP_SPEC.md`). Without a
steer flag it spreads loops across compass directions (`any`); `--direction`
steers into an 8-point compass cone, `--feature` anchors the trip on a named
lake, river or scenic feature, and `--anchor` passes a coordinate pin (a pin on
open water snaps to the nearest mapped shore). Settings live under `[loop]` in
`config/score.toml`, and `scenic serve` exposes the same controls on its
Drive-around tab (`docs/WEB.md`).

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

Before scoring moments, fetch the state boundary once:

```
.venv/bin/python -m scenic.boundary
```

This downloads the public-domain [Census 2023 TIGER/Line state boundaries](https://www.census.gov/geographies/mapping-files/time-series/geo/tiger-line-file.html)
(the legal boundary, which runs out into Lake Ontario, Lake Champlain and
the harbour, unlike the cartographic file clipped at the shoreline), unions
New York with Pennsylvania, New Jersey, Connecticut, Massachusetts and
Vermont, and saves WGS84 geometry to `data/boundary/new_york.geojson` (also
respects `SCENIC_DATA`). The source ZIP and its SHA-256 recorded in the
GeoJSON preserve provenance. Existing valid data is reused. The download
sends no user identity. Moment generation fails if this file is missing or
invalid. It drops moment clusters whose stop is outside the mask: the
representative point must lie within 200 m of it, or the stop's own road
edge must touch a node inside it (so the Peace, Ogdensburg-Prescott and
Thousand Islands bridges keep their stop even when the cluster point lands
on the far shore). Only the Canadian side is outside the mask: a drive may
run through a neighbouring state and stop there. The road graph is not
filtered. The moment stage fingerprints the boundary file and reports
removed counts and labels in `score_report.json`.

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
redundant, penalises joins that would force a U-turn along a waypoint's own
edge, and keeps at most `max_waypoints` (default 9). When more are needed, it
keeps the subset that loses the least overlap without introducing U-turns.
Google Maps counts stop boxes (origin, waypoints, and destination, even on a
closed loop where start equals finish) and locks route editing at 10 stops
(more than 7 intermediate waypoints). In the web app, `split_waypoints` caps
single links at 7 intermediate waypoints and splits longer routes and loops
into balanced `Part 1`, `Part 2`, ... links of at most 7 intermediate waypoints
each, joined at a shared mid-road handoff waypoint. The coordinates are rounded
to 5 decimals and snapped back to the nearest road, and the report comes from
that: overall overlap with the target, ETA, and per-leg times next to the
target's.

Google weighs roads a little differently from our router, so by default
each leg is checked with the target's own roads 10% slower
(`time_margin=0.1`). This adds waypoints, including spurious ones in city
grids where parallel streets tie. Waypoints are also at most 15 km of scenic
road apart (`max_gap_m=15_000`). Scenic road is any part of the target the
plain fastest route doesn't use; pass `scenic=S, s_fast=...` to count only
roads scenier than the fastest route, as the web app does. When a single link's
waypoint cap can't meet both rules, the reducer keeps the waypoints that leave
the least scenic road at risk. The notes then list each leg still at risk,
worst first, with its main road and how much scenic road it leaves uncovered or
a slightly different router could skip. `summary()` reports `risk_legs` and
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
