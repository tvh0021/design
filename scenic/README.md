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
  No turn costs or traffic lights, so times are free-flow and optimistic.

## Tests

```
.venv/bin/python -m pytest
```

The tests use small hand-made graphs and need no downloaded data.
