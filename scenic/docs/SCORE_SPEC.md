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

## v0.2 changes

Design and reasons: `docs/SCORE_V02.md` §3, changes 1–4. The moments layer,
the route search and checks V1, V2 and V5 (§4) are in "v0.2c" below. Every new
key in `config/score.toml` can switch the old behaviour back on.

**Fixes made first (v0.1.1).**
- Stage caching uses fingerprints: each stage hashes its code, its config
  sections and its input files, and is rerun when any of them changes.
- `edge_signals.parquet` and `samples.npz` carry the edges fingerprint and
  are refused for any other `edges.parquet`.
- A sight line samples every step strictly before the shore.
- The water-surface target is now the 10th percentile of the DEM on shore
  land cells within 5×5 90 m blocks (`water.surface_*`), instead of the DEM
  over the water.
- A point whose nearest shore is worth nothing still gets the fan of rays.

**1. Pooled smoothing.** The window is unchanged. The smoothed value is now
`smooth = 0.5·mean + 0.5·top`, where `top` is the w-weighted mean of `pre`
over the best 300 m of window weight, with the last edge taken in part. A
window under 300 m uses its maximum (`smoothing.pool_weight`,
`pool_top_m`).

**2. Penalty.** Penalties now multiply instead of subtract:
`pre = raw·(1 + 0.10·Q + 0.05·C)·(1 − P)` (`penalty.mode = "multiply"`).
The town term is multiplied by `1 − max(W, V_water)` (`town_water_gate`).
The motorway term is gone (`motorway_weight = 0`). The cap stays at 0.5.

**3. W.**
- Only the first 3 obstructed 30 m cells from the road multiply
  (`water.max_obstructed_cells`).
- Forest (classes 41, 42, 43) now lets 0.5 of the view through instead of
  0.4.
- Shore-road floor (`water.shore_road`): the nearest areal water cell of
  size ≥ 0.4 gives `W_pt ≥ 0.5·size` within 120 m. The floor fades linearly
  to 0 at 300 m and applies whatever the sight lines show.

**4. V and weights.** V no longer uses prospect (`views.w_prospect = 0`):
`V = open·(0.35 + 0.65·V_water) + forest·0.25`. The weights are now
`raw = 0.40·W + 0.30·H + 0.15·V + 0.15·L`.

**Validation.**
- The v0.1 checks are still reported.
- New checks:
  - V3: scenic ≥ 0.65 (8/10). Urban strips ≤ 0.20 (6 of the 7 dull items
    that are neither motorways nor farmland). Flat rural roads in
    [0.20, 0.50]: NY-5 Batavia, NY-98 and NY-104 Ridge, all three.
  - V4a: landscape-byway median, reported only.
  - V4b: each designated byway's rural km (dev ≤ 0.3) against non-byway
    rural roads of the same classes within 15 km, reweighted to the byway's
    class mix. Reported only.
  - Lake-front diagnostics.
  - Holdout-2 (`[holdout2]`), sealed until `declared_final`.

**Results** (v0.1 → v0.1.1 → v0.2a (changes 1, 2, 4) → v0.2b (+ change 3)):

| check | v0.1 | v0.1.1 | v0.2a | v0.2b | SCORE_V02 §5 |
|---|---|---|---|---|---|
| scenic ≥ 0.70 / dull ≤ 0.35 | 8, 10 | 8, 10 | 9, 10 | 9, 10 | 9, 10 |
| holdout-1 scenic, dull (burnt) | 6, 4 | 5, 4 | 7, 4 | 8, 4 | 8, 4 |
| designated byway median | 0.553 | 0.550 | 0.596 | 0.625 | 0.618 |
| landscape-byway median (V4a) | 0.651 | 0.648 | 0.699 | 0.716 | ≥ 0.62 |
| V4b byways beating neighbours | 18/25 | 18/25 | 22/25 | 21/25 | ≥ 18 |
| V3 scenic, urban, flat | 10, 7, 3 | 10, 7, 3 | 10, 6, 3 | 10, 7, 3 | 8, 6, 3 |
| Seneca front, Geneva | 0.25 | 0.24 | 0.65 | 0.76 | ≥ 0.65 |
| Skaneateles front | 0.32 | 0.30 | 0.58 | 0.63 | ≥ 0.65 |
| Rochester→Syracuse fastest (I-90) | 0.254 | 0.253 | 0.302 | 0.308 | 0.317 |
| US-20 Canandaigua–Skaneateles | 0.326 | 0.325 | 0.382 | 0.404 | 0.415 |
| NY-28 Old Forge–Inlet | 0.58 | 0.58 | 0.68 | 0.73 | ≥ 0.70 |

Holdout-2 was scored once, on v0.2b. It passes V3: scenic 8/10 ≥ 0.65,
urban 4/5 ≤ 0.20 and flat 3/3. v0.1 would also have passed V3 on it, so it
does not separate the two versions. Its OD pairs were routed once, after
the route search was final (v0.2c below).

**Deviations from SCORE_V02.md.**
- §2's "Cayuga N end" edge (1 km, W 0.68) is the Seneca River east of
  Geneva. The Cayuga crossing is reported separately.
- §5's "landscape-byway median ≥ 0.62" matches §3's "byway med" column,
  which covers all designated byways. Both medians are reported.
- V4b reproduces 18/25, not "15/25 today": the comparison method is not
  given in the design.
- The Skaneateles front misses the ≥ 0.65 prediction. The v0.1.1 fixes
  lowered its W from 0.31 to 0.22; the §3 what-ifs used v0.1's W. Which of
  those fixes caused the drop was not isolated, and the formula was not
  tuned further.

## v0.2c: moments and route search

Design: `docs/SCORE_V02.md` §4. The edge score is unchanged. New code:
`scenic.moments` (stages osm_features and moments), `scenic.search` (the A-to-B
search, `scenic route`), and V1, V2 and V5 in `scenic.validate` (stage
validate_routes, `route_validation.json`). Settings: `[moments]` and `[route]`
in `config/score.toml`, and `[v1]`, `[v2]` and `[v5]` in
`config/spot_checks.toml`.

**Moments** (`moments.parquet`). Candidates are non-excluded edges of class
motorway to unclassified. Each test uses unsmoothed per-edge or per-point
values:
- water: W ≥ 0.5 or V_water ≥ 0.7. m = max(W, V_water)·(0.5 + 0.5·size).
  size is the largest `water_size` within 450 m of the edge's points. Where
  V_water ≥ 0.7, the size of the water the view lands on also counts.
- vista: per sample point, open ≥ 0.6 and drop ≥ 150 m. A terrain viewshed
  on dem90, over 16 azimuths out to 3 km, must see ground at least 75 m below
  the eye at least 2 km away. m = clip(drop/300, 0.5, 1), from the edge's
  largest drop.
- gorge: an OSM waterfall or cliff within 150 m of a sample point (cliff
  lines are densified to 50 m). Or an OSM bridge edge over areal water or a
  mapped river or stream, where the DEM within 200 m is at least 30 m below
  the deck end. m = 0.8.
- osm: `tourism=viewpoint` within 100 m of a sample point. m = 0.7.

An edge takes its best type. Clustering is greedy and does not chain:
- Best m first, an unclaimed edge claims the unclaimed edges of the same road
  key within 2 km.
- Then each surviving cluster head absorbs the other heads within 1 km, with
  their members.

Names come from the OSM feature (gorge, osm), else the nearest named lake
(≥ 0.2 km²) within 1.5 km, else a named river within 300 m. The place is the
nearest city, town, village or hamlet within 15 km.

Result: 3,820 moments. 2,276 water, 1,205 vista, 278 gorge, 61 osm.

**Route score and search.** As in §4, with three additions, all in `[route]`:
- The same named lake, river or falls earns credit again only 10 km further
  along the route (`feature_spacing_km`). Without this, a loop round
  Onondaga Lake earned six full moments.
- Via seeds skip moments whose named feature the fastest route already
  passes, and keep one seed per named feature.
- Each via route is built twice, with λ = 1 legs and with fastest legs.

The λ sweep uses λ ∈ {0.5, 1, 2, 3, 5, 8, 12, 20}. The alternatives are
λ ∈ {1, 3, 8} with reused edges costing ×2, for two rounds. The search runs
on the nodes with t(A→n) + t(n→B) ≤ 1.5·t0. A query takes 1–4 s after about
3 s of loading.

**Validation** (`route_validation.json`):

| check | result | need |
|---|---|---|
| V1 corridors: best under +30 % passes | 7/8 (Rochester→Geneva: 7.1 scenic min) | 6/8 |
| V1 bands +5–20, +20–30, +30–50 % all filled | 8/8 | 6/8 |
| V2 paired contrast | not testable: 0 of 10 pairs non-trivial | 80 % |
| V5 moments within 300 m of a moment point | 5/30 | 27/30 |
| V5 non-moments within 300 m | 2/15 | ≤ 2/15 |
| V5 by member edges (secondary) | 29/30 moments, 4/15 non-moments | |
| holdout-2 V1 (run once) | 4/4, bands 4/4 | 3/4 |
| holdout-2 V2 (run once) | 1 non-trivial pair, failed (NY-54A) | |

- **V2.** A stretch's ends are the two farthest-apart nodes of its matched
  edges. For nearly every stretch, the fastest route between those ends is
  the stretch itself (shared ≥ 84 %). So the "fast alternative" the check
  needs does not exist.
- **V5.** The primary measure was fixed before results were seen: distance
  to the moment's representative point. A 2 km cluster puts that point up
  to about 1 km from the place a person would name, so most named moments
  sit 300–1,900 m away even when one of their edges is a member.
- **V5 misses.** By member edges, the only miss is the Thousand Islands
  bridge (edge W 0.45, V_water 0.65). The I-90 and I-390 Genesee crossings,
  which §1 names as non-moments, pass §4's water rule.

**Rochester→Syracuse** (fastest 88.0 min, I-90):

| budget | min | ratio | km | scenic min | moments credited | main roads |
|---|---|---|---|---|---|---|
| fastest | 88.0 | 1.00 | 141 | 4.4 | 6 | I-90, I-490, I-690 |
| +15 % | 99.9 | 1.14 | 152 | 9.5 | 8 | I-90, NY-31, NY-34, River Rd (Cross Lake, Seneca River) |
| +30 % | 105.6 | 1.20 | 157 | 10.9 | 8 | I-90, NY-31, Plainville Rd (Cross Lake, Seneca River) |
| +50 % | 130.0 | 1.48 | 153 | 37.8 | 8 | NY-104, CR-101, Lake Road, NY-370 (Lake Ontario shore, Sodus Point) |

Forced via routes measured on our own graph:
- Skaneateles front: +26.5 % (§2 said +24 %), score 33.3.
- Sodus Bay: +30 % with fastest legs (as §2 said); +40 % with λ = 1 legs,
  mean S 0.54.

Neither Skaneateles nor Sodus Bay wins its budget. Under the §4 score the
Cross Lake / Seneca River detour wins the +15 % and +30 % budgets (47.9).
The Lake Ontario shore wins +50 % (77.8).

**Known issues.**
- 35 % of water moments are on built-up edges (dev > 0.3), because §4 has no
  town gate. Examples: the Hudson and Mohawk crossings at Albany, Troy and
  Cohoes, Kingston's Main Street, and Front Street in Olean. Such moments
  pull some +15 % picks into towns. Kingston→Oneonta +15 % has fewer scenic
  minutes than the fastest route but more moment points.
- The vista count (1,205) is well above §4's "a few hundred".


## Moments v0.3

Fixes the known issues above and four found by looking at the moments on a
map: water moments at stormwater basins and town ponds, bays named after
the lake they open into, too many vistas, and budget picks that repeat each
other. The edge score is unchanged. Settings: `[moments]` and `[route]` in
`config/score.toml`; every new key has a value that turns the rule off.

**OSM features.** `osm_features.parquet` gains `area_m2` and `tag`, and two
kinds:
- `bay`: named `natural=bay` or `natural=strait` areas, and named
  `natural=bay` nodes (no geometry).
- `stormwater`: `water=basin|wastewater|reflecting_pool|fountain|pool`,
  `landuse=basin` or any `basin=*`, at any size, plus unnamed `water=pond`
  under 4 ha (`pond_floor_m2`). A named body of at least 10 ha
  (`water_named_min_m2`) is `water` whatever these tags say, unless it is
  wastewater: Lewiston Reservoir (7.6 km²) and Muskrat Lake (39 ha) are
  tagged `landuse=basin`.

Named water areas are kept from 1 ha (`osm_water_min_m2`, was 20 ha). The
stage now depends on `[moments]`.

**Water gate.** The v0.2c water test still finds the candidates. Then each
candidate edge is dropped for the first of these reasons that applies
(`water_gate`, counts in `score_report.json`):

| reason | rule | edges |
|---|---|---|
| small | size < 0.40 (`water_min_size`), or < 0.25 when named water is near (`water_named_min_size`) | 37 |
| narrow | unnamed, and the 1 km disk water fraction (max within 300 m) < 0.06 (`water_unnamed_min_wfrac`) | 468 |
| stormwater | a stormwater area within 100 m (`water_storm_m`) and size < 0.5 | 1 |
| motorway | a motorway edge | 613 |
| town | dev ≥ 0.30 or ≥ 30 junctions/km² within 400 m (`water_town_*`) | 6,048 |
| kept | | 14,193 |

"Named" means a named lake or bay of at least 10 ha (`water_named_min_m2`)
within 450 m, or a named river within 300 m. A named river within 1,500 m
counts where the water is broad (`river_wide_name_m`), because the Hudson's
centreline can lie 750 m from the shore road. Big water lifts the
stormwater, motorway and town rules: named, size ≥ 0.6, and a 1 km disk
water fraction ≥ 0.20 within 300 m (`water_big_size`, `water_broad_*`). So
the Lake Ontario shore in Rochester and the Hudson under the Tappan Zee
stay. The "narrow" rule catches creeks and canals whose size comes from the
lake they drain into (8-connected components), such as Cazenovia Creek at
Spring Brook, with size 1.0 and 5.7 % water nearby. Most stormwater
candidates are already gone as small or town; the reason is kept for the
rest.

**Motorways.** Only the types in `motorway_types` (water, vista,
gorge_bridge) may sit on a motorway edge. Cliffs beside I-90 and I-87 are
road cuts, and so are no longer gorge moments. Nor do they name one: a
motorway gorge_bridge beside a road-cut cliff takes the water's name
(Eighteen Mile Creek on I-90, not "cliff"). On the Lewiston-Queenston bridge
(I-190) the nearest named water area, the Chippawa-Queenston Power Canal,
still wins over the Niagara River, because lakes and canals are tried
before rivers.

**Vistas.** An eye point needs a far sight line (≥ 2 km to ground 75 m
below) along at least 3 of the 16 rays (`vista_min_rays`), not just one.
After clustering, vista heads within 5 km of a better head are dropped,
along with their members (`vista_spacing_m`).

| step | count |
|---|---|
| open, drop ≥ 150 m | 37,591 points |
| a far sight line on ≥ 1 ray (v0.2c rule) | 14,346 points |
| on ≥ 3 rays | 4,365 points, 891 edges |
| heads after clustering, then thinned | 548 → 273 vistas |

The 273 vistas sit in the Southern Tier and Catskills: 73 in 42N 76W,
45 in 42N 74W, 36 in 42N 77W, 34 in 42N 75W and 32 in 42N 78W. The other
1° cells have 14 or fewer. m quartiles are 0.57, 0.64 and 0.74.

**Names.** Lakes (named water ≥ 10 ha) and bays within 1.5 km are the
candidates. The nearest is the fallback. A smaller candidate wins if it is
at most 300 m farther away (`name_slack_m`) and is either a bay or touches
the nearest polygon within 50 m (`name_touch_m`, an arm of the lake rather
than a pond behind the shore). If the pick is still 100 km² or larger, a
named bay node within 800 m names the moment instead. Rivers come next, as
before. Irondequoit Bay and Sodus Bay now carry their own names (5 and 7
moments, was 0 and 3), as do Little Sodus Bay and Bear Creek Harbor. Lake
Ontario labels fall from 116 to 81. The 1,500 m river radius applies where
the water fraction at the moment's head is broad, for every type, so a
gorge_bridge or vista over a wide river is named too.

The label keeps the bay's name, but `parent_feature` records the water body
it belongs to: the lake whose shore a bay node names, the nearest body when
a smaller arm of it won, and for a bay polygon the largest candidate it
touches (Lake Ontario for Irondequoit Bay). Otherwise it is the feature.
Same-feature spacing (`feature_spacing_km`), the one-seed-per-feature rule
for vias and the stop groups in the web UI use `parent_feature`, so the
bays along Lake George's west shore count as Lake George.

**Route credit.** Per route, `moment_pts = Σ m²` (`moment_power = 2`) over at
most max(3, ⌊8·t0/h⌋) moments (`moments_per_hour = 8`, `moments_min_cap =
3`). Via-moment ranking uses m² too. Over the 15 test ODs, the fastest route now uses
0–67 % of the cap (median 18 %). In v0.2c it used 0–100 % (median 63 %).
A weak moment (m = 0.5) earns a quarter of a full one, so passing better
moments beats passing more of them. Some pick hit the cap in 13 of the 15
ODs in v0.2c. With the v0.3 moments that falls to 7 at 6/h, and to 5 at 8/h
with the floor. The floor of 3 covers trips under 23 minutes, where the cap
had been 1–2 moments.

**Distinct picks.** Picks are made smallest budget first. Suppose a larger
budget's best route shares more than 80 % of its length (edges) with an
earlier pick (`pick_max_overlap`). That budget then takes its best route
that overlaps no earlier pick by more than 80 %, scores at least 85 % of
its best (`pick_min_score_frac`) and has a ratio above the largest earlier
budget its best duplicates, so it uses the extra time. If no route
qualifies, it repeats the earlier pick it overlaps most. When a smaller
budget fell back to the fastest route, overlapping that pick is fine; only
a larger budget whose best is the fastest route itself repeats it. `SearchResult.shared` maps that budget to the
earlier one, and `result_json` writes it as `shared`. An alternative can
score below the smaller budget's pick: it is the best clearly different
route, not a better one.

**Results** (v0.2c → v0.3; spot checks in `validation.json` unchanged):

| | v0.2c | v0.3 |
|---|---|---|
| moments | 3,820 | 2,506 |
| water, vista, gorge, osm | 2,276, 1,205, 278, 61 | 1,868, 273, 299, 66 |
| water moments on dev > 0.3 edges | 35.2 % | 21.3 % (mostly big named water) |
| all moments on dev > 0.3 edges | 23.1 % | 20.1 % |
| V5 by point, non-moments | 5/30, 2/15 | 7/30, 0/15 |
| V5 by member edges, non-moments | 29/30, 4/15 | 29/30, 1/15 |
| V1, bands | 7/8, 8/8 | 7/8, 8/8 |
| holdout-2 V1, bands | 3/4, 4/4 | 3/4, 4/4 |
| picks repeating or > 80 % overlapping an earlier pick (15 ODs) | 11, in 10 ODs | 3, all marked shared (Rochester→Syracuse V1 +30 %; Olean→Ellicottville +30 % and +50 %) |

The remaining member-edge non-moment hit is I-490 over the Genesee: the
Upper Falls waterfall (an OSM feature, gorge type) sits 219 m from it. The
Thousand Islands bridge is still missed; its edge never reaches the water
threshold (W 0.45, V_water 0.65). Gorge moments rose from 278 to 299, and
osm from 61 to 66. These are edges that had held a vista or water moment,
the better type, and now fall back to gorge or osm.

### Moment geometry corrections

Named canal lines now join named river lines in the water gate and labels.
A waterway relation supplies its name to unnamed river or canal member ways
when membership gives one unambiguous name. An unnamed canal segment also
inherits a name through canal endpoints no more
than 1 m apart, iteratively along connected segments. A segment adjacent
to conflicting names stays unnamed. Streams and detached parallel canals
cannot inherit a name. The size, stormwater and town filters still apply.

Water labels compare distances to the selected lake or bay polygon and
the nearest named river or canal line. The line wins only when it is
closer, within the existing river naming radius, and no large lake is
next to the moment: a named lake, bay or reservoir polygon of at least
`lake_wins_min_m2` (1 km²), not tagged `water=canal` (nor river or
stream), within `lake_wins_m` (400 m) of the moment keeps the polygon
pick over any nearer line. A lakeshore road that crosses a creek mouth is
the lake (Canadaway Creek on NY 5 at Dunkirk is Lake Erie; Sandy Creek on
the Lake Ontario State Parkway is Lake Ontario, the lake there lying 304 m
off), while Lewiston stays the Niagara River: the
Chippawa-Queenston Power Canal polygon is tagged canal, 0.45 km², and 841
m and 1358 m from those two moments. Lake arms and bay names retain the
existing specificity and parent-body rules. A road inside a named lake
polygon therefore keeps its lake name.

One name now covers the whole Erie Canal (49 moments). The route
search allows one via moment per name, so all Erie Canal moments compete
for a single via slot. This is why V1 Rochester to Syracuse lost its
1.2–1.3 budget band: the alternatives that filled it had passed canal
moments that are now all named Erie Canal. The search is unchanged.

A bridge edge at least `water_bridge_min_m` long, with at least
`water_bridge_wet_m` of sampled length over areal water, can become a
water candidate even if its edge-average W and V_water miss their usual
thresholds. This additional candidate needs nearby named water or a named bridge, and the
existing broad-water fraction. Its value uses max(W, V_water) times the usual water-size factor. This addresses long bridge decks whose averaged
signals dilute the view over the middle of the span. Motorway water
candidates must also meet the long wet bridge rule and the existing big
named broad-water gate. Proximity to a large lake alone is insufficient.

Clustering ranks long water-bridge candidates ahead of shore-road candidates, then
by moment value within each group. The representative point remains on
the chosen edge. This puts a bridge moment on its deck instead of a shore
road up to 2 km away. A cluster anchored on a long water bridge takes the
highest value among its member edges as its value `m`, not the deck
edge's own (the deck's averaged signals are diluted; the shore edges it
absorbed are the evidence). Without this, 16 clusters lost value to their
anchor (Bear Mountain 1.0 to 0.90, Mid-Hudson 1.0 to 0.93, Gowanus 1.0 to
0.61, New Croton on the Taconic 0.89 to 0.67, City Ship Canal 1.0 to
0.66); with it, 17 bridge-anchored values rise, those five back to 1.0,
1.0, 1.0, 0.89 and 1.0. Other
clusters keep their head edge's value, which is already the best member.
The ranking bonus does not enter route rewards. Cluster radii and the V5
300 m test remain unchanged. Applying the bonus only along the same road
(the across-roads pass ranked by plain `m`) was tried and rejected: shore
heads on other roads then absorb the decks, so Bear Mountain moves to
Hessian Lake (V5 9/30 to 8/30) and the Mid-Hudson Bridge to Fallkill Creek.

A bridge name is a fallback label when nearby named water geometry is
absent. It comes only from the way's `bridge:name`, or from a `name` that
contains Bridge, Viaduct or Causeway; a plain road name ("FDR Drive") is
not a bridge name, and a bridge whose only name is its road's does not
count as a named bridge for the water gate either. A label omits
"on <road>" when the road and the feature have the same name
("Williamsburg Bridge near New York").

Moment stops must lie on the US side of the border. The mask is the union
of New York, Pennsylvania, New Jersey, Connecticut, Massachusetts and
Vermont from the public-domain U.S. Census Bureau 2023 TIGER/Line state
file (the legal boundary, which extends into Lake Ontario, Lake Champlain
and New York Harbor; the cartographic file is clipped at the shoreline and
would drop the Verrazzano-Narrows and Lake Champlain bridges). It is saved
as WGS84 GeoJSON in `data/boundary/new_york.geojson`; fetch it with
`.venv/bin/python -m scenic.boundary`. The local PBF's clipped
administrative relation is not used. A cluster stays when its
representative point is within 200 m of the mask (EPSG:5070 metres), or
when its own road edge touches a node inside it; the second rule keeps
international bridges whose cluster point lands on the Canadian shore,
while a road wholly on Wolfe Island goes. A drive may run through a
neighbouring state and stop there. Filtering runs after clustering and
naming, removes whole clusters, and records removed labels and counts in
`score_report.json`. Missing or invalid boundary data stops moment
generation. The stage fingerprints the file. The road graph is unchanged.
