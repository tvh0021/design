# Route spur filter verification

Run on 2026-11-02 on branch `scenic`, with the existing local graph, scores, moments, and delay model. No server was started or stopped. No network requests were made. No commit, push, or branch switch was made.

## Implementation

- `src/scenic/search.py`: reject repeated nodes or edges and geometric returns in `_Kept.add`, before scoring, for sweep, alternative, and via candidates. Keep the fastest route unconditionally. Deduplicate rejected paths and expose the distinct under-cap rejection count as `spur_rejections` on `SearchResult` and in JSON output.
- `config/score.toml`: set `spur_radius_m = 150.0` and `spur_min_distance_m = 1500.0` in `[route]`. These catch the reported parallel-carriageway return while allowing a roughly 500 m cloverleaf ramp. Repeated topology is rejected at any length.
- `tests/test_search.py`: cover the distinct-node carriageway spur, a repeated-node via detour, legitimate fallback in each budget, a short cloverleaf, filtering of each candidate source, the fastest exemption, rejection deduplication, configurable thresholds, repeated edges, and segment distances just below and above 150 m.

The geometry check projects to EPSG:5070, splits segments to at most 75 m, indexes their midpoints with a KD-tree, and compares actual segment distances for nearby pairs. It checks distance along the projected polyline, including the constrained closest point when the ordinary closest points are less than 1.5 km apart along the route. It does not rely on sampled points being close enough.

Rejected candidates do not enter the scoring pool; the other candidates and the fastest fallback remain. The via seed ranking and its search bounds are unchanged. This chooses the best retained legitimate candidate in each budget, not a proof of global optimality over all possible graph paths.

## Tests and route validation

` .venv/bin/python -m pytest -q `: **256 passed in 4.42 s**. The search module has 22 passing test cases. The initial search module had 12.

`run_route_validation(data_dir()/"graph", data_dir()/"score", Path("config"), ctx.cfg)` was run before and after. The unsealed holdout-2 ODs were included.

| Check | Before | After |
|---|---|---|
| V1 corridors, scenic criterion | 7/8 | 7/8 |
| V1 corridors, budget bands | 8/8 | 8/8 |
| V1 overall | pass | pass |
| Holdout-2 V1, scenic criterion | 3/4 | 3/4 |
| Holdout-2 V1, budget bands | 4/4 | 4/4 |
| Holdout-2 V1 overall | pass | pass |
| V5 detected positives | 5/30 | 5/30 |
| V5 detected negatives | 2/15 | 2/15 |
| V5 overall | fail | fail |
| V2 overall | fail | fail |

V5 is unchanged because this fix changes route admission, not moment detection. Full validation results (`before-validation.json`, `after-validation.json`) and raw picks were kept locally in `scenic/.scratch/route-spurs/`, not committed.

## Search time and rejected candidates

One sequential run per OD per version, with one shared loaded Context per version. Wall times include the search call but exclude loading Context. The first OD in each run includes cold lazy initialization. Counts are distinct, under-cap candidate edge paths rejected by the new check. Duplicate attempts and over-cap routes are excluded. The baseline has no spur filter and thus rejects zero.

| OD | Before, s | After, s | Change | After rejected |
|---|---:|---:|---:|---:|
| Jamestown to Rochester | 4.708 | 4.403 | -6.5% | 84 |
| Rochester to Syracuse explicit | 2.793 | 2.193 | -21.5% | 81 |
| Rochester to Syracuse | 2.744 | 2.084 | -24.0% | 64 |
| Rochester to Geneva | 1.159 | 0.981 | -15.4% | 52 |
| Syracuse to Skaneateles | 0.728 | 0.653 | -10.4% | 44 |
| Albany to Lake Placid | 3.317 | 2.730 | -17.7% | 86 |
| Buffalo to Jamestown | 1.944 | 1.694 | -12.9% | 78 |
| Ithaca to Geneva | 1.098 | 1.030 | -6.2% | 42 |
| Port Jervis to Hancock | 1.496 | 1.346 | -10.0% | 42 |
| Kingston to Oneonta | 3.621 | 3.327 | -8.1% | 44 |
| Binghamton to Cooperstown | 1.615 | 1.446 | -10.5% | 72 |
| Glens Falls to North Creek | 0.773 | 0.719 | -7.1% | 50 |
| Watertown to Tupper Lake | 1.520 | 1.308 | -13.9% | 62 |
| Olean to Ellicottville | 0.510 | 0.499 | -2.1% | 36 |
| Total | 28.027 | 24.412 | -12.9% | 837 |

No OD slowed down in this run. Rejecting detours also avoids their moment scoring. These timings are a smoke comparison, not a repeated benchmark or a claim of a stable speedup.

## Picks before and after

Each cell lists **minutes / ETA ratio / scenic minutes / origin / spur**. Scenic minutes exclude moment points. Spur means repeated topology or a geometric return under the configured rule. Fastest routes are included as a false-positive check. All 14 fastest routes and all 42 after-change picks pass.

### Jamestown to Rochester

| Budget | Before | After |
|---|---|---|
| fastest | 149.6 / 1.000 / 10.5 / fastest / no | 149.6 / 1.000 / 10.5 / fastest / no |
| 1.15 | 170.3 / 1.138 / 37.7 / via 3320 (lambda 1) / yes | 166.7 / 1.114 / 37.7 / lambda 1 / no |
| 1.3 | 174.0 / 1.163 / 44.8 / via 3325 (lambda 1) / yes | 174.8 / 1.169 / 47.7 / lambda 2 / no |
| 1.5 | 217.4 / 1.453 / 92.8 / lambda 12 / no | 217.4 / 1.453 / 92.8 / lambda 12 / no |

### Rochester to Syracuse explicit

| Budget | Before | After |
|---|---|---|
| fastest | 84.6 / 1.000 / 4.2 / fastest / no | 84.6 / 1.000 / 4.2 / fastest / no |
| 1.15 | 96.2 / 1.138 / 10.6 / via 2047+154+966 (lambda 1) / no | 96.2 / 1.138 / 10.6 / via 2047+154+966 (lambda 1) / no |
| 1.3 | 107.0 / 1.265 / 14.0 / via 1816+149+966 (lambda 1) / yes | 98.4 / 1.164 / 12.8 / via 149 (lambda 1) / no |
| 1.5 | 119.9 / 1.418 / 34.1 / lambda 20 / no | 119.9 / 1.418 / 34.1 / lambda 20 / no |

### Rochester to Syracuse

| Budget | Before | After |
|---|---|---|
| fastest | 87.0 / 1.000 / 4.7 / fastest / no | 87.0 / 1.000 / 4.7 / fastest / no |
| 1.15 | 98.7 / 1.134 / 10.9 / via 154+966 (lambda 1) / no | 98.7 / 1.134 / 10.9 / via 154+966 (lambda 1) / no |
| 1.3 | 106.4 / 1.222 / 13.9 / via 1816+149+966 (lambda 1) / yes | 100.8 / 1.159 / 13.1 / via 149 (lambda 1) / no |
| 1.5 | 122.6 / 1.409 / 34.4 / lambda 20 / no | 122.6 / 1.409 / 34.4 / lambda 20 / no |

### Rochester to Geneva

| Budget | Before | After |
|---|---|---|
| fastest | 49.9 / 1.000 / 2.0 / fastest / no | 49.9 / 1.000 / 2.0 / fastest / no |
| 1.15 | 56.8 / 1.139 / 3.7 / via 2495 (lambda 1) / no | 56.8 / 1.139 / 3.7 / via 2495 (lambda 1) / no |
| 1.3 | 62.3 / 1.249 / 5.1 / via 612+562 (lambda 1) / yes | 59.8 / 1.200 / 5.2 / via 562 (lambda 1) / no |
| 1.5 | 74.7 / 1.499 / 8.1 / via 1816+2517+280 (lambda 1) / yes | 59.8 / 1.200 / 5.2 / via 562 (lambda 1) / no |

### Syracuse to Skaneateles

| Budget | Before | After |
|---|---|---|
| fastest | 29.5 / 1.000 / 2.9 / fastest / no | 29.5 / 1.000 / 2.9 / fastest / no |
| 1.15 | 33.7 / 1.143 / 5.4 / via 2692 (lambda 1) / no | 33.7 / 1.143 / 5.4 / via 2692 (lambda 1) / no |
| 1.3 | 36.0 / 1.221 / 9.3 / lambda 3 / no | 36.0 / 1.221 / 9.3 / lambda 3 / no |
| 1.5 | 40.7 / 1.383 / 11.8 / via 1135 (lambda 1) / yes | 41.0 / 1.391 / 11.5 / via 1135+1633 (lambda 1) / no |

### Albany to Lake Placid

| Budget | Before | After |
|---|---|---|
| fastest | 143.2 / 1.000 / 17.0 / fastest / no | 143.2 / 1.000 / 17.0 / fastest / no |
| 1.15 | 155.4 / 1.085 / 20.1 / via 7+10+2209 (lambda 1) / yes | 150.7 / 1.052 / 19.6 / via 7 (lambda 1) / no |
| 1.3 | 180.8 / 1.262 / 37.1 / lambda 5 / no | 180.8 / 1.262 / 37.1 / lambda 5 / no |
| 1.5 | 212.1 / 1.481 / 50.2 / via 2502+395+1320 (lambda 1) / yes | 192.7 / 1.345 / 42.0 / via 952 (lambda 1) / no |

### Buffalo to Jamestown

| Budget | Before | After |
|---|---|---|
| fastest | 84.9 / 1.000 / 7.7 / fastest / no | 84.9 / 1.000 / 7.7 / fastest / no |
| 1.15 | 95.1 / 1.121 / 11.4 / via 3109+3594+1339 (fastest legs) / yes | 92.2 / 1.087 / 11.4 / via 3109+3594 (fastest legs) / no |
| 1.3 | 98.1 / 1.156 / 14.8 / via 1060 (lambda 1) / yes | 103.2 / 1.216 / 27.9 / lambda 20 / no |
| 1.5 | 123.8 / 1.459 / 33.8 / via 687+144 (lambda 1) / yes | 103.2 / 1.216 / 27.9 / lambda 20 / no |

### Ithaca to Geneva

| Budget | Before | After |
|---|---|---|
| fastest | 62.7 / 1.000 / 5.0 / fastest / no | 62.7 / 1.000 / 5.0 / fastest / no |
| 1.15 | 71.9 / 1.146 / 21.8 / via 1842+1990 (lambda 1) / yes | 69.9 / 1.114 / 20.7 / via 1842 (lambda 1) / no |
| 1.3 | 72.5 / 1.156 / 24.0 / lambda 5 / no | 72.5 / 1.156 / 24.0 / lambda 5 / no |
| 1.5 | 83.4 / 1.329 / 31.8 / lambda 12 / no | 83.4 / 1.329 / 31.8 / lambda 12 / no |

### Port Jervis to Hancock

| Budget | Before | After |
|---|---|---|
| fastest | 85.3 / 1.000 / 9.1 / fastest / no | 85.3 / 1.000 / 9.1 / fastest / no |
| 1.15 | 97.9 / 1.148 / 21.7 / via 2562 (lambda 1) / no | 97.9 / 1.148 / 21.7 / via 2562 (lambda 1) / no |
| 1.3 | 101.2 / 1.187 / 25.9 / lambda 12 / no | 101.2 / 1.187 / 25.9 / lambda 12 / no |
| 1.5 | 123.5 / 1.449 / 28.0 / via 1281+1668 (lambda 1) / yes | 101.2 / 1.187 / 25.9 / lambda 12 / no |

### Kingston to Oneonta

| Budget | Before | After |
|---|---|---|
| fastest | 112.6 / 1.000 / 9.3 / fastest / no | 112.6 / 1.000 / 9.3 / fastest / no |
| 1.15 | 121.3 / 1.077 / 14.1 / via 1852+1946+1802 (lambda 1) / no | 121.3 / 1.077 / 14.1 / via 1852+1946+1802 (lambda 1) / no |
| 1.3 | 132.6 / 1.178 / 25.2 / lambda 12 / no | 132.6 / 1.178 / 25.2 / lambda 12 / no |
| 1.5 | 157.4 / 1.398 / 34.0 / lambda 20 / no | 157.4 / 1.398 / 34.0 / lambda 20 / no |

### Binghamton to Cooperstown

| Budget | Before | After |
|---|---|---|
| fastest | 88.0 / 1.000 / 5.3 / fastest / no | 88.0 / 1.000 / 5.3 / fastest / no |
| 1.15 | 97.5 / 1.108 / 6.7 / via 3405+2608+1333 (lambda 1) / yes | 93.0 / 1.057 / 6.7 / via 3276 (lambda 1) / no |
| 1.3 | 103.6 / 1.177 / 12.6 / lambda 5 / no | 103.6 / 1.177 / 12.6 / lambda 5 / no |
| 1.5 | 125.4 / 1.425 / 22.2 / lambda 12 / no | 125.4 / 1.425 / 22.2 / lambda 12 / no |

### Glens Falls to North Creek

| Budget | Before | After |
|---|---|---|
| fastest | 48.4 / 1.000 / 7.1 / fastest / no | 48.4 / 1.000 / 7.1 / fastest / no |
| 1.15 | 52.2 / 1.078 / 8.8 / via 401 (lambda 1) / no | 52.2 / 1.078 / 8.8 / via 401 (lambda 1) / no |
| 1.3 | 60.2 / 1.243 / 13.8 / lambda 5 / no | 60.2 / 1.243 / 13.8 / lambda 5 / no |
| 1.5 | 71.0 / 1.466 / 18.3 / via 27+3475+1204 (lambda 1) / yes | 70.5 / 1.456 / 19.6 / lambda 8 / no |

### Watertown to Tupper Lake

| Budget | Before | After |
|---|---|---|
| fastest | 125.5 / 1.000 / 12.4 / fastest / no | 125.5 / 1.000 / 12.4 / fastest / no |
| 1.15 | 141.3 / 1.126 / 20.6 / via 2596 (lambda 1) / no | 141.3 / 1.126 / 20.6 / via 2596 (lambda 1) / no |
| 1.3 | 159.6 / 1.272 / 19.5 / via 2515+661 (lambda 1) / yes | 141.3 / 1.126 / 20.6 / via 2596 (lambda 1) / no |
| 1.5 | 185.8 / 1.481 / 46.2 / alt lambda 8 #1 / no | 185.8 / 1.481 / 46.2 / alt lambda 8 #1 / no |

### Olean to Ellicottville

| Budget | Before | After |
|---|---|---|
| fastest | 30.0 / 1.000 / 2.6 / fastest / no | 30.0 / 1.000 / 2.6 / fastest / no |
| 1.15 | 34.4 / 1.150 / 5.2 / lambda 5 / no | 34.4 / 1.150 / 5.2 / lambda 5 / no |
| 1.3 | 34.4 / 1.150 / 5.2 / lambda 5 / no | 34.4 / 1.150 / 5.2 / lambda 5 / no |
| 1.5 | 34.4 / 1.150 / 5.2 / lambda 5 / no | 34.4 / 1.150 / 5.2 / lambda 5 / no |

## Audit of rejected baseline picks

These were already defective under the requested rule. The table distinguishes topological repeats from returns using separate carriageways. The shortest route exemption did not hide any flagged fastest route in this OD set.

| OD | Budget | Repeated node occurrences | Repeated edge occurrences | Reason |
|---|---|---:|---:|---|
| Jamestown to Rochester | 1.15 | 0 | 0 | geometric return only |
| Jamestown to Rochester | 1.3 | 3 | 3 | repeated topology |
| Rochester to Syracuse explicit | 1.3 | 11 | 11 | repeated topology |
| Rochester to Syracuse | 1.3 | 11 | 11 | repeated topology |
| Rochester to Geneva | 1.3 | 9 | 9 | repeated topology |
| Rochester to Geneva | 1.5 | 10 | 10 | repeated topology |
| Syracuse to Skaneateles | 1.5 | 1 | 1 | repeated topology |
| Albany to Lake Placid | 1.15 | 17 | 16 | repeated topology |
| Albany to Lake Placid | 1.5 | 9 | 9 | repeated topology |
| Buffalo to Jamestown | 1.15 | 9 | 9 | repeated topology |
| Buffalo to Jamestown | 1.3 | 2 | 2 | repeated topology |
| Buffalo to Jamestown | 1.5 | 3 | 3 | repeated topology |
| Ithaca to Geneva | 1.15 | 1 | 1 | repeated topology |
| Port Jervis to Hancock | 1.5 | 39 | 39 | repeated topology |
| Binghamton to Cooperstown | 1.15 | 3 | 3 | repeated topology |
| Glens Falls to North Creek | 1.5 | 14 | 14 | repeated topology |
| Watertown to Tupper Lake | 1.3 | 3 | 3 | repeated topology |

## Findings left outside scope

- V5 was already failing, detecting only 5 of 30 expected moments. The full validation JSON records distances and labels. Moment quality and detection were left unchanged.
- V2 was already failing and still fails. This change does not alter edge scores or its fastest-versus-stretch comparison.
- Existing via picks on several corridors repeated nodes or edges, beyond the two reported Jamestown picks. Their replacements and repeat counts are recorded above.
- This geometric rule intentionally treats any long near-return as a spur. It cannot infer a driver's intent or distinguish a scenic circuit from an unwanted detour. Short distinct-node interchange loops pass; repeated-node loops are rejected as requested.
