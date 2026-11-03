# New York boundary filter

Implemented and checked on 2026-11-03. Roads are unchanged.

## Source and fetching

Source: [Census 2023 state cartographic boundaries](https://www.census.gov/geographies/mapping-files/2023/geo/carto-boundary-file.html), 1:500,000, STATEFP 36. The [Census TIGER data is public domain](https://www.census.gov/newsroom/archives/2014-pr/cb14-208.html).
Archive: https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_state_500k.zip

Run `.venv/bin/python -m scenic.boundary` from scenic/. This saves `data/boundary/new_york.geojson` (119,637 bytes), respecting SCENIC_DATA. The ZIP was downloaded once with no user identity. Its SHA-256 is recorded in the GeoJSON; subsequent fetches reuse valid local data.

The lightweight pyshp dependency reads the shapefile. Existing pyproj and Shapely project the geometry to EPSG:5070 and buffer it by 200 m. A single final hook in moments.detect removes whole clusters by representative point and renumbers retained IDs. Metadata and retained memberships survive. The stage fingerprints the boundary file and module. Missing or invalid data stops moment generation. Removed counts and labels appear in score_report.json.

Census cartographic boundaries clip coastal waters. The mask therefore also removes offshore bridge points within New York waters, including the Verrazzano-Narrows Bridge. The tolerance keeps points near the mapped boundary, not every bridge. This is a conservative cartographic mask, not a legal jurisdiction polygon.

## Road audit

928 edges have both endpoints more than 200 m outside the mask. No edges were removed. [The complete list](data/boundary/outside_edges.csv (git-ignored, local)) includes edge and OSM way IDs, names, regions and both endpoint coordinates. The audit script and JSON are beside it.

US region labels use midpoint polygon membership. Canadian regions are coordinate estimates. One edge has a New York midpoint despite both endpoints failing; ten edges remain unclassified.

| Region | Edges |
|---|---:|
| Ontario (coordinate estimate) | 540 |
| New Jersey | 98 |
| Massachusetts | 1 |
| Connecticut | 28 |
| Pennsylvania | 146 |
| New York | 1 |
| Vermont | 9 |
| Unclassified border/water | 10 |
| Quebec (coordinate estimate) | 95 |

## Removed moments

2,547 before; 47 removed; 2,500 retained. Wolfe Island edge 500346, way 37593085, is gone. Its point (-76.185220, 44.235064) independently fails the mask. Every retained representative passes. Moments.from_table accepts the contiguous IDs.

Removed labels follow in removal order. Repeats represent distinct clusters.

- Hudson River on I 95;US 1;US 9 near Yonkers
- Saint Lawrence River on Ogdensburg-Prescott International Bridge near Ogdensburg
- Lake Champlain on US 2 near Rouses Point
- Lake Erie on QEW near Buffalo
- The Narrows on I 278 near Todt Hill
- Niagara River – Chippawa Channel on Niagara Parkway near Sandy Beach
- Niagara River – Chippawa Channel on Niagara Parkway near Sheenwater
- Lake Champlain on VT 17 near Port Henry
- Lac Saint-François on Long Sault Parkway near Massena
- Lac Saint-François on Second Street West near Cornwall Island
- Lac Saint-François on Island Road near Cornwall Island
- Lake Ontario on 96 near Grindstone
- Niagara River on River Road near Niagara Falls
- Niagara River on Niagara River Parkway near Stella Niagara
- Niagara River – Chippawa Channel on Niagara Parkway near Oakfield
- Niagara River – Chippawa Channel on Niagara Parkway near Oakfield
- Saint Lawrence River on CR 22 near Cardinal
- Saint Lawrence River on CR 2 near Cardinal
- Saint Lawrence River on CR 1 near Pine Grove
- Saint Lawrence River on Lakeshore Drive near Waddington
- Saint Lawrence River on Lakeshore Drive near Mariatown
- Saint Lawrence River on CR 4 near Mariatown
- Saint Lawrence River on Lakeshore Drive near Mariatown
- Lake Ontario on 95 near Cape Vincent
- Lac Saint-François on Island Road near Cornwall Island
- Lake Ontario on 96 near Millen Bay
- Lac Saint-François on Long Sault Parkway near Massena Center
- Niagara River on Niagara River Parkway near Collingwood Estates
- Allegheny Reservoir on PA 346 near High Banks
- Woodman Bay on 96 near Millen Bay
- Niagara River on Niagara Parkway near Lewiston
- Lakeview Drive near Lindley
- Lake Tappan on CR 94 near Nauraushaun
- Greenwood Lake on CR 511 near Sterling Forest
- Overlook Cliff on Winterdale Road near Hancock
- cliff on Delaware Drive near Sparrow Bush
- Lake Ontario on 95 near Cape Vincent
- Kill Van Kull on East 2nd Street near Todt Hill
- CR 2 near Cardinal
- Susquehanna River on PA 92 near Cascade Valley
- Lac Saint-François on Chemin Snye Road near St. Regis
- Putnam Lake on Fairfield Drive near Putnam Lake
- Riverside Drive near Ellistown
- Kerr Road near South Ripley
- Barrett Road near New Milford
- Ponus Ridge Road near Scotts Corners
- Delaware River on Masthope Plank Road near Westcolang

## Validation

Baseline files are in data/boundary/before/. The command `.venv/bin/scenic score --force moments validate validate_routes` completed with exit code 0. Its log is data/boundary/score-run.log. Code fingerprinting also reran osm_features; earlier score stages stayed cached.

| Check | Before | After |
|---|---|---|
| V1 corridors | 6/8 | 6/8 |
| V1 bands | 7/8 | 7/8 |
| V2 nontrivial stretches | 0/2 | 0/2 |
| V3 scenic | 10/10 | 10/10 |
| V3 urban | 7/7 | 7/7 |
| V3 flat | 3/3 | 3/3 |
| V4 landscape median S | 0.716 | 0.716 |
| V4 byways beating neighbours | 21/25 | 21/25 |
| V4 median neighbour gap | 0.103 | 0.103 |
| V5 representative hits | 9/30 | 9/30 |
| V5 representative non-moment hits | 0/15 | 0/15 |
| V5 member-edge hits | 30/30 | 28/30 |
| V5 member-edge non-moment hits | 1/15 | 1/15 |

V2 and V5 remain failed quality checks, as before. Exit code 0 does not mean every quality threshold passed.

Lost V5 member-edge matches:

- Verrazzano-Narrows Bridge (I-278)
- Lake Champlain Bridge (NY-185)

Tests: `.venv/bin/pytest -q` completed with 322 passed in 5.43 s. Three new synthetic tests cover filtering and tolerance, contiguous IDs and metadata, missing-data failure, and negative tolerance. git diff --check passed. No commit, push, demo build, server stop, or web/site source edit was performed.

## Working tree

Status and diff statistics are limited to this task's files. moments.py and SCORE_SPEC.md already contained another agent's changes, included in the statistics. This task only added the final moments filtering hook and replaced the unresolved-boundary paragraph. New untracked files do not appear in ordinary git diff --stat.

```
 M scenic/README.md
 M scenic/docs/SCORE_SPEC.md
 M scenic/pyproject.toml
 M scenic/src/scenic/moments.py
 M scenic/src/scenic/score_stages.py
?? scenic/docs/BOUNDARY_FIX_REPORT.md
?? scenic/src/scenic/boundary.py
?? scenic/tests/test_boundary.py
```

```
 scenic/README.md                  |  17 +++++
 scenic/docs/SCORE_SPEC.md         |  51 +++++++++++++++
 scenic/pyproject.toml             |   1 +
 scenic/src/scenic/moments.py      | 133 ++++++++++++++++++++++++++++++--------
 scenic/src/scenic/score_stages.py |   6 +-
 5 files changed, 179 insertions(+), 29 deletions(-)
```

New implementation files: boundary.py, 130 lines; test_boundary.py, 39 lines.
