# scenic

A scenic route planner for New York State. See `docs/PLAN.md` for the full plan.

So far it does phase 1 for OpenStreetMap only. It downloads the New York OSM
extract, builds a statewide graph of drivable roads, and finds the fastest
route between two points. Scenery scoring comes later.

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
scenic calibrate [--features] [--fit]
```

Writes `data/graph/edge_delays.parquet`: per-edge counts of traffic signals,
stop signs, crossings and junctions, a built-up flag, and the travel time
with delays. It has one row per `edges.parquet` row, in the same order, and
`fastest` uses its `time_s` when the file is there. `build-graph` writes it
too. `--features` recounts from the PBF (about 25 s). `--fit` refits the delay
parameters against Google's phase 0 ETAs and the OSRM demo server, and writes
`delay_fit.json`. OSRM replies are cached in `~/.cache/scenic-calibrate/`.

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
  `time = free_flow * f + 16.4 s * (signals + crossings / 2) + 8.1 s * stops + 2.95 s * minor_ends`.
  `f` is 1.006 on motorways, 1.011 elsewhere and 1.002 in built-up areas, so
  nearly all the extra time comes from the signals, stops and junctions.
  Signals and stops come from OSM nodes (`highway=traffic_signals`,
  `highway=stop`). A one-direction sign counts half. A two-way stop on a
  junction only counts for the smaller road. Crossings are signalised
  pedestrian crossings more than 40 m from a signal, plus rail level
  crossings. A control at a junction counts half on each edge there, so
  driving through pays it once. `minor_ends` counts junctions where the edge
  meets a bigger road. Built-up means 12 or more junctions within 300 m.
  Motorways get no delays.
- Fit: against Google's phase 0 ETAs, ours / Google is 1.011 for the fastest
  route, 1.002 for A, 1.026 for B and 0.961 for C (was 0.97, 0.87, 0.91,
  0.88). Fitted on the fastest route alone, A, B and C come out at 1.02, 1.05
  and 0.97. OSRM is not a direct check: its car profile drives at about 0.8x
  the posted limit and charges about 2 s per signal. So OSRM trips (40
  village-to-village trips with little motorway) only enter with a free scale
  factor and OSRM's own signal cost. The per-signal, per-stop and per-junction
  delays are held near traffic-engineering values (15, 8, 3 s) by weak
  priors, because neither reference can tell signal delay from slower
  driving in general.

## Tests

```
.venv/bin/python -m pytest
```

The tests use small hand-made graphs and need no downloaded data.
