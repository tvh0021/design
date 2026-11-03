# Scenery score v0: implementation spec

Status: 2026-11-02. Follows PLAN.md section 1. Implements `python -m scenic.score`,
which reads `scenic/data/graph/edges.parquet` (826k edges, 212k km) and writes
`scenic/data/score/`. All constants below live in `scenic/config/score.toml`
and are loaded once; code never hard-codes them. Weights are the PLAN.md
values; nothing changed. v0.1 (same weights) replaces the W, V and C
definitions below; see "v0.1 changes" at the end.

## 1. Data sources

| Input | Product | How to get it | CRS |
|---|---|---|---|
| Elevation | USGS 3DEP 1 arc-second (~30 m) seamless DEM, float32 metres NAVD88 | 1x1 degree GeoTIFF tiles from the public S3 bucket, no key: `https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1/TIFF/current/{tile}/USGS_1_{tile}.tif`, tile = `n{N}w{W:03d}` named by the NW corner (n43w078 = lat 42–43, lon −78 to −77). New York needs N in 41..46 and W in 72..80; request all 54, skip HTTP 404 (open water). About 50 MB per tile, under 2.5 GB total. | EPSG:4269 geographic |
| Land cover | Annual NLCD Collection 1, land cover, year 2023, CONUS, 30 m, uint8 classes (`Annual_NLCD_LndCov_2023_CU_C1V0.tif`) | Cloud-optimised GeoTIFF on the MRLC S3 bucket (`s3-us-west-2.amazonaws.com/mrlc/`), no key. Read only the New York window with rasterio (`/vsicurl/`), or download the whole file (about 1.5 GB). If that filename has moved, take the newest Annual NLCD land cover year from mrlc.gov downloads and record it in `score_report.json`. | NAD83 Albers, equivalent to EPSG:5070; treat as 5070 |
| Water | `scenic/data/raw/new-york-latest.osm.pbf` | Already on disk. Read with pyosmium `FileProcessor(pbf).with_areas()` and a tag filter. | EPSG:4326 |
| Roads | `edges.parquet` | On disk. | EPSG:4326 |
| Traffic signals | same PBF | Nodes with `highway=traffic_signals` or `highway=stop` (stop only when `stop=all` or untagged direction). | EPSG:4326 |

Working CRS is EPSG:5070 (metres, equal area, NLCD native). The working grid is
the NLCD 30 m grid (origin x = −2493045, y = 3310005) clipped to the bounding
box of all edge vertices plus 2 km. Expect about 23k x 19k cells (roughly 440 M).
Derived rasters are built at 90 m (3x3 block mean of the 30 m grid, about 49 M
cells) unless stated. Rasters are written to `scenic/data/rasters/` as GeoTIFF
(deflate, tiled) and read back with `np.memmap`-style windowed reads or whole.

**DEM.** Mosaic the tiles and warp to the working grid at 30 m with
`rasterio.warp.reproject`, bilinear, output int16 metres (`dem30.tif`). Also
`dem90.tif` (block mean, float32).

**NLCD class groups** (used throughout):

| Group | Classes |
|---|---|
| water | 11 |
| developed | 21 open space, 22 low, 23 medium, 24 high |
| forest | 41, 42, 43, 90 woody wetland |
| open | 31 barren, 52 shrub, 71 grassland, 81 pasture, 82 crops, 95 herbaceous wetland, 21 |
| blocking (for water visibility) | 22, 23, 24, 41, 42, 43, 90 |

**Water from OSM.** Two masks on the 30 m grid.

- Areal water, `water_area.tif` (uint8): rasterize (all_touched=False) every
  OSM area (closed way or multipolygon) with `natural=water`, or
  `landuse=reservoir`, or `waterway=riverbank`, excluding
  `water=wastewater|basin|reflecting_pool` and `intermittent=yes`. OR this
  with NLCD class 11 (NLCD supplies the Great Lakes surface and anything OSM
  lacks). Then OR in cells within 60 m of `natural=coastline` lines (fallback
  where NLCD has nodata over Lake Ontario or Erie).
- Linear water, `water_line.tif` (uint8, values 1 river, 2 canal, 3 stream):
  rasterize `waterway=river|canal|stream` ways (all_touched=True), only into
  cells not already in the areal mask.

**Lake size.** `scipy.ndimage.label` the areal mask with 8-connectivity; area
of a component = cell count x 900 m². Coastline fallback cells are forced to
10^9 m². Size score per cell `s = clip((log10(area) − 4) / 4, 0, 1)`: 1 ha → 0,
1 km² → 0.5, 100 km² and up → 1. Linear water gets fixed s: river 0.45,
canal 0.35, stream 0.15. Store `water_size.tif` (float32, 0 where no water).

**Quiet.** v0 uses road class only. NYSDOT AADT counts are deferred to v1;
the column name `Q` and its range are fixed now so v1 can swap the source.

## 2. Sampling

Project all edge vertices to EPSG:5070 in one `pyproj.Transformer` call on the
flattened lon/lat arrays (keep the list offsets). For each edge, place sample
points at even arc-length spacing with step 50 m, including both endpoints:
`n_i = max(2, ceil(length_m / 50) + 1)`. Vectorise with `np.repeat`,
per-edge cumulative length and `np.searchsorted`; no Python loop over edges.
About 6 M points. Keep `pt_edge` (int32 edge index per point) and use
`np.add.reduceat` / `np.bincount` for per-edge means.

- Edges under 20 m: two endpoints only; all point signals are the mean of the two.
- Edges over 5 km (rare: long motorway stretches): no special case, the mean
  over all points is the edge value.
- Grade and curvature need at least 4 points; edges with fewer use fallbacks
  given below.

Raster lookups are `arr[row, col]` with rows and cols from the affine
transform, clipped to the grid; points outside the grid get the nodata
fallback (signal 0).

## 3. Signals

All signals are per point unless stated, then averaged per edge, each in [0, 1].

**W, water.** Build a `cKDTree` on the centres of all water cells (areal or
linear) that lie within 300 m of any road sample point (prefilter with a
rasterized, 10-cell dilated road mask; this keeps the tree small). Query each
sample point with `distance_upper_bound=250`. For a hit at distance d with
size score s from `water_size.tif`:

```
f(d)  = clip((250 − d) / 200, 0, 1)            # 1 within 50 m, 0 at 250 m
vis   = fraction of the 4 NLCD cells at 20, 40, 60, 80 % of the way from
        the point to the water cell that are NOT in the blocking group
        (d < 45 m: vis = 1)
W_pt  = s · f(d) · vis
```

No hit: W_pt = 0. Points on bridges or causeways have d = 0 and score s.

**H, hills.** `relief90.tif` = max − min of dem90 in a disk of radius 1 km
(`scipy.ndimage.maximum_filter` and `minimum_filter` with a disk footprint of
radius 11 cells). `H_relief = clip(relief / 300, 0, 1)`. Grade: sample dem30 at
the points, apply a 3-point running mean along each edge (edge-local, use the
endpoint itself at the ends), `grade = Σ|Δz| / length_m`,
`H_grade = clip((grade − 0.01) / 0.05, 0, 1)` (the 1 % offset removes DEM noise).
`H = 0.7·H_relief + 0.3·H_grade`; edges with fewer than 4 points use `H = H_relief`.

**V, views (v0 proxy).** `tpi90.tif` = dem90 − mean of dem90 in a disk of
radius 1 km (`scipy.ndimage.convolve` with a normalised disk kernel of radius
11 cells). `V_tpi = clip(tpi / 60, 0, 1)`. `open90.tif` = fraction of cells in the open
or water group within a 3x3 window of the 90 m grid (about ±135 m).
`V = open · (0.35 + 0.65·V_tpi)`: open flat road 0.35, open ridge road 1,
forested ridge 0.

**L, land cover.** Per-class score table applied to the 30 m raster, block-mean
to 90 m, then 3x3 uniform filter → `lscore90.tif`:

| 11 | 12 | 21 | 22 | 23 | 24 | 31 | 41–43 | 52 | 71 | 81 | 82 | 90 | 95 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.8 | 0.8 | 0.4 | 0.2 | 0.1 | 0.0 | 0.5 | 0.7 | 0.7 | 0.8 | 0.9 | 0.7 | 0.7 | 0.9 |

Field–forest edge bonus: with `p_f` = forest-group fraction and `p_o` =
open-group fraction (excluding 21) in the same 270 m window,
`mix = 4·p_f·p_o`. `L = 0.8·lscore + 0.2·mix`.

**Q, quiet (per edge).** motorway 0.00, trunk 0.10, primary 0.25,
secondary 0.50, tertiary 0.75, unclassified 0.90, residential 0.60,
living_street 0.50; each `*_link` takes its parent's value.

**C, curves (per edge).** In projected coordinates, drop vertices closer than
5 m to the previous kept vertex, sum absolute heading changes in degrees,
`C = clip(turn_deg / length_km / 200, 0, 1)`. Edges with fewer than 3 kept
vertices: C = 0.

## 4. Combination, penalties, rank, smoothing

```
raw   = 0.35·W + 0.30·H + 0.20·V + 0.15·L
pre   = raw · (1 + 0.10·Q + 0.05·C) − P
```

Penalties P (sum, capped at 0.5), per edge:

| Penalty | Rule |
|---|---|
| junctions | `0.15 · clip((junctions_per_km − 4) / 16, 0, 1)` where junctions_per_km counts graph nodes of degree ≥ 3 inside the smoothing window (section below) divided by window length in km |
| signals | `0.20 · clip(signals_per_km / 3, 0, 1)`; signal nodes matched to sample points within 25 m (KD-tree), counted in the window |
| town centre | `dev` = developed fraction (22–24) within 300 m (uniform filter on the 90 m grid, 7x7); `0.25 · clip((dev − 0.3) / 0.5, 0, 1)` |
| urban speed | 0.10 if `maxspeed_tagged` and `speed_mph ≤ 30` |
| motorway | 0.10 if highway is motorway or motorway_link |

Excluded edges (`excluded = True`, `pre = NaN`, final `S = 0`): `surface` in
{unpaved, gravel, fine_gravel, compacted, dirt, earth, ground, grass, mud,
sand, rock, pebblestone, woodchips}, or `name` matches `/seasonal/i`
(NYSDOT "seasonal limited use" roads). Excluded edges are also skipped in
the smoothing window and the rank denominator.

**Same-road smoothing, 1 km.** Road key = normalised `ref` (upper case,
spaces and hyphens removed, first value if `;`-separated) if present, else
normalised `name` (lower case, collapsed whitespace), else `way:{way_id}`.
Two edges are road-adjacent if they share a node and have the same key;
oneway is ignored. For each edge i the window is i plus every road-adjacent
edge j reachable through same-key edges whose along-road distance from the
nearer end of i to the nearer end of j is under 500 m. Compute this
vectorised as a min-plus BFS on triples `(src, dst, dist)`: start with the
adjacency triples, join each round with the adjacency table on `dst`
(pandas merge or `np.searchsorted` on a sorted CSR), take the min dist per
`(src, dst)`, drop dist ≥ 500 m, stop after 8 rounds or when no triple is
added. Weight `w_ij = min(length_j, 500 − dist_ij)`, `w_ii = length_i`.
`smooth_i = Σ w_ij·pre_j / Σ w_ij` over non-excluded j. The window length used
by the junction and signal penalties is `Σ w_ij`. Penalties are therefore
computed after the window is known; `pre` uses the window counts.

**Percentile rank by length.** Over non-excluded edges, sort by `smooth`;
`S_i = (cumulative length before i + 0.5·length_i) / total non-excluded length`.
Ties take the same mean rank. S is in (0, 1).

## 5. Outputs

All under `scenic/data/score/`.

**`scores.parquet`**, one row per `edges.parquet` row in the same order, zstd.

| Column | Type | Meaning |
|---|---|---|
| edge_idx | int32 | row index in edges.parquet |
| u, v, way_id | as in edges | copied for the alignment check |
| W, H, V, L, Q, C | float32 | per-edge signals |
| penalty | float32 | P |
| raw, pre, smooth | float32 | before and after smoothing (NaN when excluded) |
| S | float32 | final percentile score, 0 when excluded |
| excluded | bool | unpaved or seasonal |
| n_samples | int16 | sample points on the edge |
| water_dist_m | float32 | mean nearest-water distance of the points (inf → 9999) |

On write, assert `len(scores) == len(edges)` and that `u`, `v`, `way_id` are
identical arrays; refuse to write otherwise. Loader: `scenic.score.load()`
re-asserts the same against the edges file it is given.

**`heatmap.png`**: rasterize S to a 500 m grid in EPSG:5070 (length-weighted
mean S per cell, `np.bincount` on sample points), `imshow` with a sequential
colormap (viridis), state outline optional, 3000 px wide, title with run
date. Excluded edges left out.

**`summary.md`**: for each of W, H, V, L, Q, C, penalty, raw, smooth, S:
length-weighted mean, p10, p50, p90, share of length at exactly 0, plus a
6x6 Pearson correlation table of the signals (length-weighted) and the
excluded length in km. **`score_report.json`**: inputs with versions and
file sizes, grid shape, timings per stage, peak RSS (reuse `peak_rss_mb`
from graph.py), edge count, validation results from section 6.

## 6. Validation

**Byways.** Source, in order of preference: (1) NYSDOT "Scenic Byways"
layer from the NYS GIS Clearinghouse (gis.ny.gov; shapefile or the ArcGIS
REST feature service, GeoJSON export, no key); (2) fallback, a hand list of
byway refs in `scenic/config/byways.toml` (Seaway Trail NY-18/NY-104/NY-3,
Adirondack Trail NY-30, Central Adirondack Trail NY-28, Olympic Trail NY-3,
High Peaks NY-73, Roosevelt–Marcy NY-28N, Upper Delaware NY-97, Catskill
Mountains NY-28 and NY-23A, Shawangunk US-44/NY-55, Lakes to Locks NY-22/
US-9/NY-4, Cayuga Lake NY-89/NY-90, Route 20 US-20 Duanesburg–LaFayette).
Map byway geometry to edges by snapping sample points within 30 m
(KD-tree); with the ref fallback, match edges by road key inside the
byway's county set. Byways are a check, never an input.

Pass: length-weighted median S of byway edges ≥ 0.70, at least 60 % of byway
length has S ≥ 0.50, and for each highway class with ≥ 20 km of byway the
byway median S exceeds the statewide median of that class. Report all three;
failure of one is a warning, not an error.

**Spot checks.** Each stretch is defined in `scenic/config/spot_checks.toml`
by ref or name plus a lon/lat bounding box the implementer sets from the two
named towns. Result = length-weighted mean S over matched edges.

Expected scenic (S ≥ 0.70, pass if at least 8 of 10):

1. NY-97 Hawk's Nest, Port Jervis to Barryville (Delaware River, cliffs)
2. NY-73 Keene Valley to Lake Placid (High Peaks, Cascade Lakes)
3. NY-30 Indian Lake to Long Lake (Adirondack Trail)
4. NY-23A Palenville to Haines Falls (Kaaterskill Clove)
5. NY-18 Youngstown to Olcott (Lake Ontario shore)
6. NY-89 Ithaca to Seneca Falls (west shore of Cayuga Lake)
7. NY-414 Watkins Glen to Lodi (east shore of Seneca Lake)
8. NY-9N Bolton Landing to Hague (Lake George)
9. NY-22 Westport to Essex (Lake Champlain)
10. US-20 Skaneateles to LaFayette (ridge views, Route 20 byway)

Expected dull (S ≤ 0.35, pass if at least 8 of 10):

1. I-490 Rochester, Inner Loop to I-590
2. NY-104 West Ridge Road, Greece, Lake Avenue to Long Pond Road
3. NY-5 Erie Boulevard East, Syracuse to DeWitt
4. NY-5 Central Avenue, Albany, Everett Road to Wolf Road
5. NY-24 Hempstead Turnpike, Levittown
6. I-495 Long Island Expressway, Queens
7. NY-78 Transit Road, Williamsville to Depew
8. US-9 Wappingers Falls to Fishkill (Poughkeepsie Galleria strip)
9. US-62 Niagara Falls Boulevard, Tonawanda to Amherst
10. NY-5 Batavia to Le Roy (flat, straight, no water)

Number 10 on the dull list is the calibration case for flat farmland: it
should sit below 0.40 but not near 0; if it scores under 0.15 the L and V
scales are too harsh on open farmland.

## 7. Compute budget

Target: one run on a 16 GB laptop in under 30 minutes, peak RSS under 8 GB.

| Stage | Method | Expected time |
|---|---|---|
| Downloads | 3DEP tiles (2.5 GB) and NLCD window (under 200 MB with `/vsicurl/`), cached, skipped when present | 5–20 min once, network-bound |
| DEM mosaic and warp to 30 m | rasterio `reproject`, tile by tile into one int16 array | 3–5 min |
| OSM water extraction | pyosmium with areas, shapely via WKB, `rasterio.features.rasterize` | 3–5 min |
| 90 m derived rasters | `scipy.ndimage` filters on 49 M cells (max, min, mean with a disk footprint; uniform filters) | 3–6 min |
| Sampling 6 M points | numpy, one pass | under 1 min |
| Water KD-tree | `cKDTree` on a few million prefiltered cells, one query | 1–3 min |
| Smoothing BFS | pandas merges, about 8 rounds over ≤ 10 M triples | 1–3 min |
| Rank, outputs, heatmap | numpy, matplotlib `imshow` | under 1 min |

Memory: dem30 int16 880 MB, NLCD uint8 440 MB, water masks 440 MB each
(uint8), each 90 m float32 raster 200 MB, sample arrays about 6 M x ~10
columns float32 = 240 MB. Free the 30 m arrays after the 90 m products and
the point samples exist. Rules: no Python loop over edges or points; loops
over raster tiles (for the warp) and BFS rounds are fine. Each stage writes
its product to disk and is skipped on rerun when the product is newer than
its inputs, so a failed stage does not repeat the earlier ones.

## Implementer's choice

Each has a default; change only with a note in `score_report.json`.

- Elevation resolution: default 1 arc-second. The 1/3 arc-second product
  (9x the data) is not worth it for 1 km relief and TPI.
- NLCD year: default 2023; any Annual NLCD year 2021–2024 is acceptable.
- Disk footprint filters at 90 m: `scipy.ndimage` with a boolean footprint is
  the default; a separable square window of the same width is acceptable if
  the disk version takes over 10 minutes.
- Water KD-tree prefilter: default dilated road mask; building the tree on
  every water cell is acceptable if it fits in memory.
- Smoothing BFS implementation: default pandas merge; any equivalent that
  yields the same `(src, dst, min dist)` triples is fine.
- Seasonal detection: default name regex. Adding `seasonal` to `TAG_KEYS`
  in graph.py and rebuilding the graph is the better fix; do it in a
  separate change.
- Byway geometry source: default NYS GIS layer; fall back to the ref list
  without asking if the download is not reachable.

## v0.1 changes

v0 failed validation (designated byway median 0.51, scenic spot checks
6/10). v0.1 keeps the weights (0.35/0.30/0.20/0.15), H, L, Q, the penalties,
smoothing and rank, and changes how W, V and C are measured. Before any
change, a holdout set (10 scenic, 5 dull, `[holdout]` in
`config/spot_checks.toml`) was written and scored with v0
(`data/score_v0/holdout_v0.json`); it was not looked at again until the
v0.1 run was final. Each change has a reason in what a driver can see.

**W, water.**

- *Reach scales with the water.* A pond is seen from 150 m; a wide lake
  from 1.5 km. `wfrac` = water fraction in a 1 km disk on the 90 m grid;
  `reach = clip(1.2·sqrt(wfrac·π·(1 km)²), 150, 1500)` m. Full credit within
  `max(50 m, 0.2·reach)`, then linear to 0 at `reach`. OSM river, canal and
  stream lines with no area keep a 250 m reach (full to 50 m). A wide river
  mapped as an area gets the reach of its area, so it is treated like a
  lake of the same width.
- *Shore cells, by band.* Areal water is represented by its shore cells.
  They are split into reach bands (≤ 400, ≤ 900, > 900 m), each with its own
  KD-tree, so a pond beside the road does not hide the lake behind it. Points
  on water (bridges, causeways) score the water's size.
- *Visibility is a sight line.* From eye height (1.2 m) to the water,
  sampled every 30 m on dem30: bare ground more than 2 m above the line
  blocks it (vis 0). Trees, wetland trees and buildings have a height per
  NLCD class (forest 20 m, woody wetland 12 m, developed 8–15 m, shrub
  3 m); each 30 m cell whose top rises above the line lets through a share
  of the view (forest 0.4, developed medium 0.4, high 0.2). A road on a
  slope therefore sees over the shore trees; a road behind them does not.
  The line aims past the shore by `min(clip(0.5·d, 30, 500), 0.25·reach)`
  so it looks at open water, not at the one cell behind the shore trees;
  obstacles are checked up to the shore.
- *A fan of sight lines.* Besides the line to the nearest shore cell, four
  more go out at ±30° and ±60°, each to the first water cell within the
  band's reach, scored with their own distance. W is the best of them: the
  driver sees the lake through whichever gap opens, often at an angle.
- `W_pt = max over bands and sight lines of size · falloff(d, reach) · vis`.

**V, views.**

```
V = open·(0.35 + 0.65·(1 − (1 − prospect)(1 − water_view))) + forest·0.25
```

- *Prospect* (replaces the 1 km TPI): how much landscape is laid out in
  front of an open viewpoint. `drop` = height above the lowest ground within
  3 km, `rise` = the highest ground within 3 km above the point;
  `prospect = clip((drop + 0.5·rise − 30) / 200, 0, 1)`. TPI only saw ridge
  tops; a valley road with hills rising on both sides and a hillside road
  looking over a valley both have a view.
- *Broad water in view.* Broad water is a 90 m cell at least half water whose
  1 km disk is at least 25 % water. The score is the angle the driver looks
  down at it, `clip(θ / 2°, 0, 1)` (a lake 150 m below at 2 km fills the
  view, one at road level is a line on the horizon), times a fade over the
  second half of the water's view range `R = clip(1.5·sqrt(water area within
  3 km), 2, 6) km`, times a terrain-only sight line on dem90. Sight lines go
  to the nearest broad-water cell and along the same ±30°, ±60° fan.
- *Forest baseline.* Inside forest the view is trees: 0.25 per unit forest
  fraction instead of 0, so a forest road is not scored like a strip mall.
  Forest is not scenic in itself here (L already scores it 0.7); this only
  stops V from treating a wooded road as having no view at all.

**C, curves.** Turning is summed per edge (degrees) and scored over the
same 1 km smoothing window as the penalties:
`C = clip(Σ_j w_ij·turn_j/length_j / (Σ_j w_ij / 1000) / 200, 0, 1)`. A curvy
road split into many short edges no longer gets C = 0 on its straight
pieces and 1 on its bends.

**New products.** `water90` stage: `wfrac90`, `reach90`, `water_z90` (mean
DEM over water cells), `bigwater_idx90` (nearest broad-water cell within
6 km, from the EDT), `wv_reach90`. `terrain90` also writes `min3k90` and
`max3k90`. `edge_signals.parquet` adds H_relief, H_grade, V_prospect,
V_water, turn_deg and water_dist_m.

**Results** (v0 → v0.1): designated byway median S 0.51 → 0.55 (share ≥ 0.5:
0.52 → 0.57); scenic spot checks 6/10 → 8/10, dull 10/10 → 10/10; holdout
scenic 3/10 → 6/10, holdout dull 3/5 → 4/5; Rochester to Syracuse fastest
0.251 → 0.254, US-20 0.331 → 0.326. The byway check still fails: many
designated byways are heritage routes through farmland and towns (Seaway
Trail 837 km at median 0.44, Revolutionary Byway 0.37, Maple Traditions
0.41).
