# Loop mode (Mode 2, "Drive-around"): specification

Status: design, not implemented. Written 2026-11-11 against branch `scenic`.
Every formula and number below was run as a throwaway prototype on the real
New York graph (629,681 nodes, 825,892 edges, 2,522 moments) unless section 13
lists it as untested. The prototype scripts were deleted; this file is the
only record of them.

The request this answers: plan a scenic drive that starts and ends at the
same place, for a chosen length of time, with one decision point: go any
way, go in a compass direction ("north", "northeast"), or pass a chosen
place ("from downtown Rochester, pass by the lake").

Contents: 1 behaviour · 2 geometry · 3 config · 4 algorithm · 5 Python API ·
6 CLI · 7 HTTP API · 8 waypoints · 9 frontend · 10 demo · 11 tests ·
12 measurements · 13 untested parts · 14 later

## 1. Behaviour

Inputs: a start point H (also the finish), a time budget B, and a steer.

| Steer | Input | Meaning |
|---|---|---|
| `any` | none | The best loops, spread around the compass. |
| `compass` | `direction` in N, NE, E, SE, S, SW, W, NW | Loops whose bulk lies that way from H. |
| `anchor` | a map pin (`anchor`, may be on water), or a named scenic feature (`feature`) | Loops that pass that place. |

Budget: presets 60, 120, 180 min, or any whole number of minutes in
[30, 360]. A loop is shown only if its drive time is within
[0.75 B, 1.05 B].

Output: up to 3 loops, best first. Each has the same data as an A-to-B
route (time, distance, scenic minutes, moments, roads, scenery colouring,
Google Maps link) plus a direction word of its own ("Northwest loop"). There
is no "fastest" row and no "extra min" column: a loop has no baseline trip.

Three things the user is told when they apply:

- A compass request the land can't honour. North of downtown Rochester the
  roads end at the lake 13 km away, so 2 h and 3 h "north" loops come back
  leaning northwest or northeast (section 12.3). Each loop carries its own
  direction, and the response has a `note`.
- An anchor pin that was moved. A pin on open water is moved to the nearest
  stretch of that water's shore.
- A loop that leaves and returns on the same road for a stretch (a
  peninsula or a one-road town): the detail line gives the length.

## 2. Geometry and terms

**True bearings, not grid bearings.** `search._to5070` gives EPSG:5070
coordinates, whose grid north is 10° to 15° west of true north across New
York (true north has grid bearing −11.1° at Rochester, −10.3° at Buffalo,
−14.5° at Montauk). Bearings, cones and direction words must use a local
tangent plane at H:

```
R     = 6371008.8
east  = radians(lon - lon_H) * cos(radians(lat_H)) * R
north = radians(lat - lat_H) * R
bearing = degrees(atan2(east, north)) % 360          # 0 = north, 90 = east
angdiff(a, b) = abs((a - b + 180) % 360 - 180)        # 0..180
sdiff(a, t)   = (a - t + 180) % 360 - 180             # signed, negative = left of t
word(b)       = DIRECTIONS[int(((b + 22.5) % 360) // 45)]
DIRECTIONS    = ("N","NE","E","SE","S","SW","W","NW") # bearing = 45 * index
```

Use the same (east, north) plane for anchor positions, `along` projections
and anchor spacing. Use EPSG:5070 (`_to5070`, `_densify`) only where
distances between road shapes matter: the corridor penalty (4.4) and the
validity measure (4.7).

Terms:

- **H**: the start node, `g.snap(lat, lon)`.
- **B**: the budget in seconds.
- **Arc**: a directed edge, as in `g.arc_tail / arc_head / arc_edge`.
- **Via**: an arc the loop must drive, `(tail, head, arc)`; or a node the
  loop must touch (a point anchor), written as `tail = head = node`,
  `arc = -1`.
- **dH[n]**, **dR[n]**: fastest time H → n and n → H.
- **rt** of a via: `dH[tail] + t_arc + dR[head]`, the fastest out-and-back
  through it. For a node via `t_arc = 0`.

## 3. Config: `[loop]` in `config/score.toml`

Add this section after `[route]`. Loop scoring also reads these `[route]`
keys unchanged: `moment_radius_m`, `moment_spacing_km`, `feature_spacing_km`,
`moments_per_hour`, `moments_min_cap`, `moment_power`, `moment_minutes`.

```toml
[loop]
# time budget
budgets_min = [60, 120, 180]   # presets in the UI
default_min = 120
min_minutes = 30
max_minutes = 360
min_time_frac = 0.75           # keep a loop when min_time_frac*B <= time <= max_time_frac*B
max_time_frac = 1.05           # also the reach cut: dH + dR <= max_time_frac*B
n_loops = 3

# leg cost and anti-retrace (4.4)
lambdas = [3.0, 1.0, 0.0]      # cost = time * (1 + lambda * (1 - S)); first that fits is used
reuse_radius_m = 120.0         # edges with a midpoint this close to the driven path ...
reuse_penalty = 4.0            # ... cost (1 + reuse_penalty) times as much on later legs
uturn_ban_cost = 1e12

# score (4.8)
s_base = 0.50                  # scenic minutes count S above this; also s_fast for waypoints

# candidate via arcs (4.3)
seed_cell_m = 4000.0
seed_min_S = 0.6
seed_min_length_m = 150.0
seed_full_m = 20000.0          # sum(len * S) of qualifying edges in a cell that earns seed_value
seed_value = 0.25

# pairing, steer = any (4.5.1)
any_sector_deg = 30.0
any_per_sector = 2
first_rt_frac = [0.30, 0.90]
partner_tight = [0.65, 0.92]   # estimate window, as a fraction of B
partner_wide = [0.60, 0.95]
partner_tight_k = 2
partner_k = 4
partner_sep_deg = [25.0, 150.0]
partner_min_gain = 1.15        # estimate >= this * max(rt of either via)

# steer = compass (4.5.2)
cone_deg = 30.0
depth_quantile = 0.9
depth_frac = 0.5
apex_k = 8
anchor_gap_m = 3000.0
partner_half_plane_deg = 90.0
straddle_offset_deg = [15.0, 90.0]
straddle_left_k = 4
straddle_right_k = 3
straddle_max_skew_deg = 60.0
compass_tol_deg = 67.5
compass_tol_fallback_deg = 90.0
compass_full_weight_deg = 22.5
compass_weight_span_deg = 45.0
compass_min_weight = 0.5

# steer = anchor (4.5.3)
anchor_snap_m = 2000.0
shore_snap_km = 45.0
shore_targets = 3
feature_targets = 4
feature_target_gap_m = 5000.0
target_max_rt_frac = 0.90
anchor_reach_frac = 0.95
lens_rt_frac = [0.45, 0.95]
anchor_partner_k = 4
anchor_partner_sector_deg = 45.0
anchor_partner_window = [0.65, 0.92]

# validity (4.7)
sample_step_m = 75.0
doubled_radius_m = 150.0
doubled_min_sep_m = 1500.0
max_net_doubled_frac = 0.10
stem_start_m = 1500.0
stem_gap_m = 300.0
stem_frac_tier1 = 0.12
stem_frac_tier2 = 0.40

# selection (4.10)
max_overlap = 0.5
max_overlap_anchor = 0.6
max_overlap_relaxed = 0.7
min_bearing_sep_deg = [60.0, 30.0, 0.0]   # steer = any, tried in order

# work cap (4.6)
dijkstra_work_arcs = 5.0e7
dijkstra_count_range = [80, 600]
```

`loop.py` keeps the same values as a `DEFAULTS` dict and merges the file over
it, so tests can pass a partial dict.

## 4. Algorithm

### 4.1 Validate and snap

- `minutes`: a finite number in [`min_minutes`, `max_minutes`]; `B = 60 * minutes`.
- `H, snap_m = g.snap(*start)`. More than `MAX_SNAP_M` (2000 m) is the
  existing `snap_far` error.
- `steer`: `any`, `compass` (needs `direction`), or `anchor` (needs exactly
  one of `anchor` and `feature`).

### 4.2 Reach

```
M  = g._matrix(g.time_s)               # cached CSR, time in seconds
MT = M.T.tocsr()                       # cache once per process (0.6 s the first time)
dH = dijkstra(M,  indices=H, limit=max_time_frac * B)
dR = dijkstra(MT, indices=H, limit=max_time_frac * B)
region = (dH + dR) <= max_time_frac * B          # nodes
arcs   = arcs with both ends in region
```

Measured: the cut takes 0.02 to 0.16 s. Region size from downtown Rochester:
69k arcs at 60 min, 116k at 120, 275k at 180, 556k at 360; Manhattan at 180
is 609k.

For each λ in `lambdas`, `G[λ] = CostGraph(g, arcs, g.time_s * (1 + λ * (1 - S)))`.
`G[0.0]` is the plain time graph. These objects live for one request only.

### 4.3 Candidate vias

Two sources, both restricted to edges with both ends in the region:

1. **Moments.** Both arcs of every moment's best edge (`moments.edge`).
   `value = m ** 2`, `key = moment id`.
2. **Seeds**, so scenic roads without a moment still shape loops. Bin edge
   midpoints (EPSG:5070) into `seed_cell_m` squares. In each cell take the
   edges with `S >= seed_min_S`, `length_m >= seed_min_length_m` that are no
   moment's edge. The cell's seed is the one with the largest
   `S * sqrt(length_m)`; both its arcs are candidates with
   `value = seed_value * min(1, sum(length_m * S) / seed_full_m)` over the
   cell's qualifying edges, and `key = -1 - edge`.

Per candidate compute `t_arc`, `t_out = dH[tail] + t_arc`,
`t_back = dR[head]`, `rt = t_out + t_back`, and the position of `head` in
the tangent plane: `east`, `north`, `bearing`, `dist`.

`best_dir(set)`: for each key keep the arc with the smaller `rt`.
`spaced(list, n, gap)`: walk the list in order, keep a candidate when it is
at least `gap` metres from every one kept, stop at `n`.
All "top by value" orders are value descending with a stable sort over the
candidate index, so results are deterministic.

### 4.4 Building one loop

A loop is built leg by leg through its vias (one via: a "lens"; two: a
"triangle"), then back to H. Three rules matter; the first two were found by
probing and are not optional.

**Corridor reuse penalty.** After each leg, every region edge whose midpoint
lies within `reuse_radius_m` of that leg's path joins the `used` set, along
with the edges driven. On later legs each used edge costs
`(1 + reuse_penalty)` times its normal cost (applied once, not compounded).
"Within the path" is tested against the straight chords between consecutive
path nodes, sampled every `reuse_radius_m`, with a KD-tree of region edge
midpoints (EPSG:5070, `query_ball_point`).
An edge-identity penalty is not enough: it misses the opposite carriageway
and the frontage road, and leaves a median 14 to 24 % of a loop doubled
(section 12.1).

**No U-turn at a via.** Without this rule 42 of 49 prototype loops reversed
on the spot at a via. Keep `ban`, the arc that would undo the last hop:

- The first leg (from H) has no ban.
- After a leg whose node path is `p`, `ban = (p[-1] → p[-2])`.
- *Through rule.* If the last arc of the leg lies on the via's own edge (the
  leg arrived by driving the via edge backwards), the via counts as driven:
  do not append the via arc, continue from `tail`, and keep the ban above.
- Otherwise append the via arc: continue from `head`, `ban = (head → tail)`.
- A node via appends nothing; the ban above applies.
- The banned arc gets cost `uturn_ban_cost` on the next leg. If the target's
  distance is then infinite or ≥ `uturn_ban_cost`, the leg fails and the loop
  is dropped.
- *Dead-end retry.* Only when the via just left is the user's anchor (a
  point anchor, or a feature target in anchor mode): on such a failure retry
  that leg without the ban and mark the loop `uturn = True`. The spur it
  creates is still judged by 4.7. Measured: the Irondequoit Bay pin snaps to
  a dead-end node; with the ban alone it gets 0 loops at 60, 120 and 180 min,
  with the retry 6 of 8 and 8 of 8 valid loops at 60 and 120 min, net doubled
  ≤ 0.02.

**λ ladder.** Try `lambdas` in order and keep the first loop whose time is
≤ `max_time_frac * B`. A loop shorter than `min_time_frac * B` is dropped,
not retried.

Legs, for cost graph `G = G[λ]`:

- Leg 1 (H → first via): the unpenalised path from `G.tree(H)`. One tree per
  λ, reused by every loop.
- Later legs: copy `G.fwd.data`, multiply the entries of the used edges,
  overwrite the banned arc's entry, and run scipy `dijkstra` from the current
  node on `csr_matrix((data, G.fwd.indices, G.fwd.indptr))`. The entry for
  arc (t, h) is at `searchsorted(G.key, t * G.n + h)`.
- The middle leg of a triangle depends only on (first via, λ): its penalty
  set is leg 1's corridor and its ban is leg 1's last hop. Compute that tree
  once per (first via, λ) and read every partner's path from it. This is the
  same computation as one Dijkstra per pair, not an approximation.
- The last leg (→ H) depends on both earlier legs: one Dijkstra per loop.

Order of work for one first via `a1` with partners `pending`:

```
for lam in lambdas:
    if not pending: break
    leg1 = path H -> a1 on G[lam], then the via arc or the through rule
    tree2 = dijkstra(penalised(G[lam], corridor(leg1), ban1), source=cur1)   # shared
    retry = []
    for a2 in pending:
        leg2 = path cur1 -> a2 from tree2, then via arc or through rule      # fail: drop a2
        leg3 = dijkstra(penalised(G[lam], corridor(leg1 + leg2), ban2), source=cur2) -> H
        T = loop time
        if T > max_time_frac * B: retry.append(a2)
        elif T >= min_time_frac * B: keep the loop
    pending = retry
```

The route is `g.make_route(node_path, arcs)`.

**Memory rule.** `CostGraph.tree` caches every root for the life of the
object, and each tree is two arrays over all 630k nodes (about 7.5 MB). Use
`G.tree` for root H only. Per-via trees (estimates, middle legs, last legs)
are local scipy calls whose arrays are dropped after use; estimate trees
pass `return_predecessors=False`. At most one middle tree is alive at a time.

### 4.5 Which pairs to build

`est(a1, a2) = t_out[a1] + d1[a2.tail] + t_arc[a2] + t_back[a2]`, where `d1`
is the plain-time distance from `a1.head` (one Dijkstra on `G[0.0]` per first
via, `limit = max_time_frac * B`, no predecessors). Built loops take 1.09 to
1.17 times this estimate at the median (q10 1.00 to 1.07, q90 1.23 to 1.31),
which is why the windows sit below [0.75, 1.05].

`partners(a1, extra)`: candidates `a2` with a different key from `a1` that
pass `extra` and

- `partner_sep_deg[0] <= angdiff(bearing[a2], bearing[a1]) <= partner_sep_deg[1]`,
- `est >= partner_min_gain * max(rt[a1], rt[a2])` (the triangle is more than
  an out-and-back to either via),

taken by value, one per key (of a key's two arcs, the one with the smaller
`est`): first up to `partner_tight_k` with `est` in `partner_tight * B`, then
filled to `partner_k` from `partner_wide * B`. The two-window fill matters:
either window alone loses the best loop at some home (section 12.2).

#### 4.5.1 `any`

First vias: `best_dir` of candidates with `rt` in `first_rt_frac * B`; bin
by bearing into `any_sector_deg` sectors; top `any_per_sector` by value in
each (at most 24). Pairs: each first via with `partners(a1, none)`.

#### 4.5.2 `compass`, direction bearing θ

A fixed ring at one-third of the budget does not work: for north of
Rochester it finds 8 vias at 60 min and none at 90, 120 or 180, because the
roads stop at the shore. Use the reach of the roads themselves:

1. `cone` = candidates with `angdiff(bearing, θ) <= cone_deg` and
   `rt <= first_rt_frac[1] * B`. Empty: `no_loop_that_way`.
2. `along = east * sin θ + north * cos θ`. `depth` = the `depth_quantile`
   quantile of `along` over the cone.
3. **Apex vias**: `best_dir` of cone candidates with
   `along >= depth_frac * depth`, by value, `spaced(..., apex_k, anchor_gap_m)`.
4. **Apex pairs**: each apex with
   `partners(a1, angdiff(bearing[a2], θ) <= partner_half_plane_deg)`.
5. **Straddle pairs**, for directions where the cone is shallow (a loop that
   goes out on one side of θ and back on the other). Left vias: `best_dir` of
   candidates with `sdiff(bearing, θ)` in
   [−`straddle_offset_deg[1]`, −`straddle_offset_deg[0]`] and `rt` in
   `first_rt_frac * B`, by value, `spaced(..., straddle_left_k, anchor_gap_m)`.
   For each, right vias with `sdiff` in the mirrored range, `est` in
   `partner_wide * B`, `est >= partner_min_gain * max(rt)`, and
   `abs(sdiff_left + sdiff_right) <= straddle_max_skew_deg`; top
   `straddle_right_k` by value, one per key. The loop runs H → left → right → H.

A built loop is accepted when `off = angdiff(loop bearing, θ)` (4.9) is
≤ `compass_tol_deg`. If none is, accept ≤ `compass_tol_fallback_deg`. If
still none: `no_loop_that_way`. Rank by `score * w`:

```
w = 1 - (1 - compass_min_weight) * clip((off - compass_full_weight_deg) / compass_weight_span_deg, 0, 1)
```

so a loop within 22.5° keeps its score and one at 67.5° keeps half.

#### 4.5.3 `anchor`

Resolve the anchor first (`resolve_anchor`), in this order:

1. `feature` given: normalise (`casefold`, collapse spaces) and match the
   whole name against `moments.feature` and `moments.body`. Targets are all
   moments with that feature or body. No match: `unknown_feature`.
   Kind `feature`.
2. `anchor` point with `g.snap` distance ≤ `anchor_snap_m`: a node via at
   the snapped node. Kind `point`.
3. Otherwise the nearest moment of type `water` within `shore_snap_km` of
   the pin. If its `body` is named, targets are that body's moments;
   otherwise that one moment. Kind `shore`; `name` is the body; the pin moves
   to the nearest target that is used.
4. Otherwise `anchor_far`.

Why local names come first: with the app's own Nominatim settings, "Lake
Ontario" returns the Lake Ontario State Parkway (twice), a hamlet called
Ontario on the Lake and a boat-launch park, never the lake. And a pin on
open water cannot snap: 3 km offshore is 3,145 m from the nearest road, the
middle of the lake 37.6 km, with the nearest water moments 38.0 to 38.5 km
away, all of body "Lake Ontario".

Reach check: let `rt_min` be the smallest `rt` over the targets (for a node,
`dH + dR`). If `rt_min > anchor_reach_frac * B`, raise `anchor_out_of_reach`
with `round_trip_min = ceil(rt_min / 60)` and
`needed_min = 5 * ceil(rt_min / target_max_rt_frac / 300)`, capped at
`max_minutes`. The out-of-reach test needs `rt_min` beyond the budget's
region, so compute `dH`/`dR` for this check with
`limit = max_time_frac * 60 * max_minutes` (or a second, wider pass only
when no target is in the region).

Targets to build through:

- `feature`: `best_dir` of the feature's moment vias with
  `rt <= target_max_rt_frac * B`, by value then `rt`,
  `spaced(..., feature_targets, feature_target_gap_m)`.
- `shore`: the `shore_targets` in-reach moments of the body nearest the pin.
- `point`: the node via.

For each target F:

- a lens loop H → F → H when `rt[F]` is in `lens_rt_frac * B`;
- triangles with `anchor_partner_k` partners in each order (H → F → a → H and
  H → a → F → H). Partners are moment or seed vias with a different key and
  `est` in `anchor_partner_window * B`, by value, at most one per
  `anchor_partner_sector_deg` sector of bearing and one per key. The
  estimate for the first order uses a forward tree from `F.head`; for the
  second, a reverse tree to `F.tail`
  (`est = dH[a.tail] + t_arc[a] + d(a.head → F.tail) + t_arc[F] + dR[F.head]`).
  No separation or gain condition here: the anchor fixes the shape.

Keep only loops that really pass the anchor: a target moment's id is in the
loop's `passed` list, or the node is in its node path.

### 4.6 Work cap

```
max_dijkstras = clip(floor(dijkstra_work_arcs / len(arcs)),
                     dijkstra_count_range[0], dijkstra_count_range[1])
```

That is 600 at 60 min from Rochester, 431 at 120, 181 at 180, 89 at 360.
One Dijkstra costs about 85 ns per region arc (25 ms at 180 min, 47 ms at
360), so the cap bounds Dijkstra time near 4 s.

Count every Dijkstra after the reach cut. Process first vias one at a time
(estimate tree, then the ladder in 4.4) in this order, and stop starting new
first vias once the count reaches the cap:

- `any`: the best via of every sector, sectors in bearing order, then the
  second-best of every sector.
- `compass`: apex vias and left straddle vias interleaved, each list by value.
- `anchor`: targets in order; for each, the lens, then partners by value,
  alternating the two orders.

A first via already started is finished, so results never depend on timing.

### 4.7 Validity

`search.has_spur` returns True for every closed loop (a repeated node) and
must not be called on loops. Use this instead (`loop_validity`):

1. Project `route.geometry` to EPSG:5070 and resample every `sample_step_m`
   (`_densify`); `s` is distance along, `L` the total.
2. KD-tree `query_pairs(doubled_radius_m)`. A pair (i, j) counts when its
   cyclic separation `min(|s_i - s_j|, L - |s_i - s_j|)` exceeds
   `doubled_min_sep_m`. A sample in any counted pair is *flagged*.
3. `doubled = flagged samples / all samples` (both passes of a doubled road
   count).
4. **Stem.** From the start: if the first flagged sample has
   `s <= stem_start_m`, extend through following flagged samples while the
   gap between neighbours is ≤ `stem_gap_m`; `out` is the `s` where that run
   ends. The same from the finish gives `back`.
   `stem_frac = (out + back) / L`, `stem_km = (out + back) / 2000`, and
   `net = (flagged length outside both stem runs) / L`.
5. Tier 1: `net <= max_net_doubled_frac` and `stem_frac <= stem_frac_tier1`.
   Tier 2: `net <= max_net_doubled_frac` and `stem_frac <= stem_frac_tier2`.
   Anything else is rejected.

Restricting step 2 to near-parallel headings was tried and changes nothing
(12 valid loops either way in Manhattan), so the simple all-pairs measure
stays.

### 4.8 Score

`score_route(sr, ctx, t0=B, s_fast=s_base)` from `search.py`, unchanged. It
gives `scenic_min = Σ t · max(0, S − s_base) / 60`, credits moments with the
existing spacing rules under the cap
`max(moments_min_cap, floor(moments_per_hour · B / 3600))`, and sets
`score = scenic_min + moment_minutes · Σ m^moment_power`. `sr.ratio` becomes
time / B, the share of the budget used.

Why a fixed `s_base = 0.50`: a baseline computed from the roads within reach
ranges from 0.07 (Manhattan, 60 min) to 0.67 (Lake Placid, 60 min), so the
same road would earn very different scenic minutes from different homes. It
buys nothing in ranking: loop scores under 0.50 and under the local baseline
correlate at 0.95 to 0.99 (Spearman), and the three picks are the same or
differ by one loop in every case tried. Moment minutes dominate anyway
(Rochester at 120 min: median 10.8 scenic min against 25.5 moment min). The
same 0.50 is the `s_fast` the waypoint reducer needs (section 8).

### 4.9 Direction of a loop

Time-weighted centroid of the loop: for each edge, the midpoint of its two
end nodes in the tangent plane, weighted by `g.time_s`. `bearing` is the
bearing of that centroid, `direction = word(bearing)`, `far_km` the largest
distance of any edge midpoint from H. If the centroid is under 500 m from H,
`direction` is None and the loop is labelled "Loop".

### 4.10 Selection

1. Drop duplicate edge sequences.
2. Pool: tier 1 loops by rank, then tier 2 loops by rank. Rank is `score`
   (`score * w` in compass mode).
3. Greedy: take a loop when `route_overlap` with every loop taken is ≤ the
   overlap limit (`max_overlap`; `max_overlap_anchor` in anchor mode) and, in
   `any` mode, its bearing is at least `min_bearing_sep_deg[0]` from every
   loop taken. Stop at `n_loops`.
4. Fewer than `n_loops`: in `any` mode repeat step 3 with the next bearing
   separations; then, in every mode, repeat with `max_overlap_relaxed`. Keep
   the largest set found; with equal sizes keep the stricter one.

Label each pick `"<Direction> loop"` with the word spelled out ("Northwest
loop"); a repeated word gets " 2", " 3" so labels are unique within a result.

`note` (one sentence, optional):

- compass, no pick has the requested direction word: "No loop this long
  stays to the north of here. The closest ones lean northwest and
  northeast." (words from the picks, in order, deduplicated);
- anchor of kind `shore`: "Your pin is on the water, so these loops pass the
  nearest stretch of Lake Ontario shore.";
- fewer than `n_loops` picks: "Only 1 loop fits." / "Only 2 loops fit.".

Join several with a space.

### 4.11 When nothing fits

All are `LoopError(code, message, **numbers)`; the server returns 422.

| Code | When | Message |
|---|---|---|
| `no_loop_that_way` | compass: empty cone, or no accepted loop | "No loop fits to the southwest in 60 min. Try a longer drive or another direction." |
| `no_loop` | any or anchor: no valid loop | "No loop fits in 60 min from here. Try a longer drive." |
| `unknown_feature` | no moment has that feature or body name | "No scenic feature named 'X' in the data. Pick one from the list or drop a pin." |
| `anchor_far` | pin over 2 km from a road and no water moment within 45 km | "That pin is too far from any road or mapped shore. Move it closer to a road." |
| `anchor_out_of_reach` | 4.5.3; numbers `round_trip_min`, `needed_min` | "That place is about 73 min there and back on the fastest roads. Choose 85 min or more." |

`no_loop_that_way` is a real outcome: southwest of Lake Placid at 60 min the
cone holds 6 vias, all on dead-end roads into the High Peaks, and all 12
pairs fail the U-turn ban.

## 5. Python API: `src/scenic/loop.py`

New module. It imports from `search.py` (`Context`, `CostGraph`,
`ScenicRoute`, `score_route`, `route_overlap`, `_to5070`, `_densify`,
`main_roads`, `describe`) and changes nothing there.

```python
DIRECTIONS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
DEFAULTS: dict            # section 3

class LoopError(Exception):
    code: str; message: str; numbers: dict

@dataclass(frozen=True)
class Steer:
    mode: str = "any"                           # "any" | "compass" | "anchor"
    direction: str | None = None                # compass
    point: tuple[float, float] | None = None    # anchor pin (lat, lon)
    feature: str | None = None                  # anchor by name

@dataclass
class Anchor:
    kind: str                                   # "feature" | "point" | "shore"
    name: str | None                            # feature or body name
    point: tuple[float, float]                  # (lat, lon) to show on the map
    requested: tuple[float, float] | None       # the pin as given
    moved_m: float                              # requested -> point; 0 for a feature
    node: int | None                            # point anchors
    moments: np.ndarray                         # target moment ids
    round_trip_s: float

@dataclass
class LoopRoute(ScenicRoute):                   # so describe(), route_display(), moment_minutes() work
    bearing: float | None = None
    direction: str | None = None
    off_deg: float | None = None                # compass only
    far_km: float = 0.0
    doubled: float = 0.0
    net: float = 0.0
    stem_frac: float = 0.0
    stem_km: float = 0.0
    tier: int = 1
    uturn: bool = False
    rank: float = 0.0
    label: str = ""

@dataclass
class LoopResult:
    start: tuple[float, float]
    node: int
    snap_m: float
    budget_s: float
    steer: Steer
    anchor: Anchor | None
    s_fast: float            # = s_base; this name lets App.waypoints run unchanged
    picks: list[LoopRoute]
    kept: int                # valid loops compared
    built: int               # loops built
    dijkstras: int
    region_arcs: int
    seconds: dict            # {"reach", "vias", "build", "select", "total"}
    note: str | None

def loop_config(cfg: dict | None = None) -> dict
def loops(ctx, start, minutes, steer: Steer = Steer(), cfg: dict | None = None, log=None) -> LoopResult
def resolve_anchor(ctx, reach, steer, cfg) -> Anchor
def features_in_reach(ctx, start, minutes, q: str | None = None, limit: int = 8, cfg=None) -> list[dict]
def loop_validity(route, cfg) -> dict            # doubled, net, stem_frac, stem_km, tier (0 = rejected)
def loop_bearing(g, route, node) -> tuple[float | None, float]   # bearing, far_km
def result_json(ctx, res) -> dict
def geojson(ctx, res) -> dict
def format_table(ctx, res) -> str
```

`loops` raises `LoopError` for every case in 4.11 and `ValueError` for bad
arguments (minutes out of range, unknown direction). It never returns an
empty `picks`.

`features_in_reach` computes its own reach with
`limit = max_time_frac * 60 * max_minutes`, groups in-reach moments by every
name they carry (`feature` and `body`), and returns per name:

```python
{"name", "count", "best_m", "types": [...], "round_trip_min", "fits": rt_min <= anchor_reach_frac * B,
 "needed_min", "point": [lat, lon], "bearing", "direction"}
```

`point` is the moment with the smallest `rt`. Without `q`: only `fits`
entries, ordered by `count * best_m ** 2` descending. With `q` (normalised
substring match): all matches, `fits` first, then the same order. Measured
from Rochester: 11 named features fit at 60 min (Lake Ontario 21 moments,
Irondequoit Bay 6, Erie Canal 15), 36 at 120 (Canandaigua Lake appears,
round trip 69 min), 90 at 180.

## 6. CLI

```
scenic loop --from LAT,LNG [--minutes N]
            [--direction N|NE|E|SE|S|SW|W|NW | --anchor LAT,LNG | --feature NAME]
            [--geojson FILE] [--json FILE] [--png FILE] [--graph DIR] [--score DIR]
```

`--minutes` defaults to `default_min`. The three steer options are mutually
exclusive; none means `any`. Follow `cmd_route`: print the load time and
snap distance, the resolved anchor when there is one, the `note`, then
`format_table`: one row per loop with label, minutes, km, scenic min,
moments credited, score, net doubled, stem km and main roads. A `LoopError`
prints its message and returns 1.

`--geojson`: a LineString per loop (label, direction, minutes, km, score,
scenic_minutes), a Point per credited moment, a Point for the start and one
for the resolved anchor. `--png`: `search.plot_routes` reads a
`SearchResult`; write a small `plot_loops` beside it in `loop.py` that draws
the picks the same way.

## 7. HTTP API (`web/server.py`)

Dispatch additions: `("POST", "/api/loop")`, `("POST", "/api/loop/features")`.
Both take the search lock, like `/api/route`.

### `POST /api/loop`

```json
{"start": [43.1566, -77.6088], "minutes": 120, "steer": "any"}
{"start": [43.1566, -77.6088], "minutes": 120, "steer": "compass", "direction": "N"}
{"start": [43.1566, -77.6088], "minutes": 60,  "steer": "anchor", "anchor": [43.29, -77.60]}
{"start": [43.1566, -77.6088], "minutes": 60,  "steer": "anchor", "feature": "Lake Ontario"}
```

Response:

```json
{
  "id": "…", "mode": "loop",
  "start": [43.1566, -77.6088], "snap_m": [12],
  "minutes": 120,
  "steer": {"mode": "anchor", "kind": "shore", "name": "Lake Ontario", "point": [43.26, -77.60],
            "requested": [43.29, -77.60], "moved_m": 3145, "round_trip_min": 21},
  "s_base": 0.5, "kept": 27, "built": 45, "search_s": 1.4, "seconds": {…}, "s_breaks": […],
  "note": "…",
  "routes": [ {…}, {…}, {…} ]
}
```

`steer` is `{"mode": "any"}`, `{"mode": "compass", "direction": "N", "bearing": 0}`,
or the anchor form above. `snap_m` is a one-element list so the page's
existing loop over it works.

Each route entry has every field of an A-to-B entry (`key`, `labels`, `role`,
`origin`, `minutes`, `ratio`, `km`, `scenic_minutes`, `mean_S`, `mean_S_pct`,
`moment_points`, `score`, `roads`, `moments`, `stop_groups`, `moments_passed`,
`line`, `runs`) plus `direction`, `bearing`, `off_deg`, `stem_km`, `tier`.
`key` is `r0`, `r1`, `r2`; `labels` is `[label]`; `role` is 1, 2, 3 (never 0,
so nothing is drawn dashed and the three budget colours are reused);
`ratio` is time over budget.

Build it with a `loop_payload(ctx, res, breaks, grid, max_points)` beside
`route_payload`. Move the per-route dict out of `route_payload` into a shared
helper so both use the same code. Store the result exactly like a search:

```python
self.results[rid] = {"res": res, "routes": {key: LoopRoute}, "waypoints": {}}
```

Errors (the JSON carries `error`, `point`, `code` and the numbers, as now):

| Status | Code | Case |
|---|---|---|
| 400 | | body not an object; `start` not `[lat, lon]`; unknown `steer`; `compass` without a valid `direction`; `anchor` with neither or both of `anchor` and `feature` |
| 400 | `bad_minutes` | `minutes` missing, not finite, or outside the range; numbers `min`, `max` |
| 422 | | `start` or `anchor` outside the New York bbox (`point` names it) |
| 422 | `snap_far` | start over 2000 m from a road; `point: "start"`, numbers as now |
| 422 | section 4.11 codes | `LoopError`; `point: "anchor"` for the three anchor codes |
| 503 | | still loading (existing `_ready`) |

`App._point` hard-codes wording for `from` and `to`. Give it a small table:
`from → ("start", "A")`, `to → ("end", "B")`, `start → ("start", "the start pin")`,
`anchor → ("anchor", "the anchor pin")`.

### `POST /api/loop/features`

`{"start": [lat, lon], "minutes": 120, "q": "lake", "limit": 8}` (`q` and
`limit` optional; `limit` at most 30) → `{"features": [ … section 5 … ]}`.
Purely local: no outside request.

### Existing endpoints

- `/api/waypoints` needs no change to its call: it reads `stored["res"].s_fast`
  and `stored["routes"][key].edges`, both present. Two edits: the expiry
  messages say "Search again." in place of "Press Find routes again.", and
  the guard in section 8.
- `/api/health`, when ready, gains
  `"loop": {"budgets_min": [60, 120, 180], "default_min": 120, "min_minutes": 30,
  "max_minutes": 360, "directions": ["N", …], "n_loops": 3}`.
- `App.load` (or `warm_waypoints`) also builds the transposed time matrix, so
  the first loop search does not pay the 0.6 s.
- Loop results share `KEEP_RESULTS` and the `results` dict with searches.

## 8. Waypoints and the Google Maps link

No change to the reduction algorithm. Measured on 9 real loops (60, 120 and
180 min), calling
`reduce_to_waypoints(g, edge_path=route.edges, scenic=ctx.S, s_fast=0.5, max_waypoints=9, follow_target=0.9)`:

- 7 to 9 waypoints, overlap with the planned loop 0.984 to 0.9996,
  `meets_target` true for all nine, 0.2 to 0.9 s each (3.8 s the first time,
  for the index).
- The link is `origin=H&destination=H&waypoints=…`, so Google draws the whole
  loop. The H → H leg never collapses to nothing because the reducer keeps
  adding waypoints until the legs follow the target.
- Most loops of 120 min and more come back `capped` (they wanted 10 to 18
  waypoints; scenic gaps up to 23.8 km against the 15 km rule; 0 to 6 risk
  legs). The page already shows these notes.
- Passing `scenic` and `s_fast` matters. Without them every edge counts as
  scenic: up to 21 waypoints wanted and overlap as low as 0.9708.

Changes:

1. Guard in `App.waypoints`: when `origin == destination` and there are no
   waypoints, raise `ApiError(500, "Could not place waypoints for this loop. Try another loop.")`.
   A link with the same start and end and no waypoints is a zero-length
   trip. This did not happen in any probe; the guard is for safety.
2. Never call `has_spur` on a loop (4.7).
3. Tests in section 11.

## 9. Frontend (`index.html`, `app.js`, `app.css`)

Same night-sky Spectral design and tokens (`--trail`, `--sky-deep`,
`--line`, `--haze`, `--dim`, `--halpha`). All text goes in through
`textContent`, as now. Everything below is live mode only and sits inside
the existing `if (DEMO) … else …` wiring.

### 9.1 Markup

- `#tab-loop`: remove `disabled`, `aria-disabled`, the `title` and the
  `.soon` span; the text is "Drive-around". Give the tabs `aria-controls`.
- The existing `<section class="points">` gets `id="panel-ab"`.
- New `<section class="points loop" id="panel-loop" hidden>`:

```
form#form-h.place         label "Start and finish" with pin "S"; buttons #home-h "From home",
                          #home-set-h "Set as home"; input #q-h; ul#res-h
div#budget.field          label "How long"; .seg buttons data-min=60|120|180 ("1 h", "2 h", "3 h");
                          input#min-custom type=number min=30 max=360 step=5, suffix "min"
div#steer.field           label "Which way"; .seg buttons #steer-any "Any way",
                          #steer-compass "A direction", #steer-anchor "Past a place"
  div#rose hidden         3 x 3 grid: NW N NE / W · E / SW S SE; eight buttons with aria-pressed;
                          the centre cell is a dot for the start
  div#anchor-box hidden   form#form-x.place (input #q-x "Lake, bay, falls, town …", ul#res-x),
                          ul#chips (feature chips), p#anchor-note
p#hint-loop.hint
div.actions               button#find-loop.primary "Find loops", button#clear-loop "Clear"
```

- Results table: give the "extra min" header cell `id="extra-head"` and the
  caption `id="caption"`.

### 9.2 State and helpers

- `state.mode` is `"ab"` or `"loop"`. Results are kept per mode:
  `state.byMode = {ab: {search, selected, visible, waypoints}, loop: {…}}`,
  and switching tabs swaps those four into `state` and calls
  `renderAll(false)`. A search is not lost by looking at the other tab.
- New points `state.h` (start) and `state.x` (anchor pin), and
  `state.loop = {minutes: 120, steer: "any", dir: null, feature: null}`.
- Generalise `setPoint`, `removePoint`, `pinIcon`, `setBadPin` and
  `wireSearch` from `a | b` to `a | b | h | x`, with markers in
  `state.markers[which]`. Add `.pin-h` and `.pin-x` styles (letters "S" and
  "P"). Only the active mode's markers are on the map.
- `api()` maps the server's `point` to a pin: add `start → "h"`,
  `anchor → "x"`. `errorText` words `snap_far` for the start pin, and builds
  `anchor_out_of_reach` from `round_trip_min` and `needed_min`.
- The first time the loop tab opens with no start set and A set, copy A to
  the start.
- Home: `#home-h` and `#home-set-h` reuse `fromHome`, `setHome`, `HOME_KEY`.

### 9.3 Controls

- **Budget.** A preset press sets `minutes` and clears the custom box. A
  custom value is clamped to the range from `/api/health` on change.
- **Steer.** One of the three segments is pressed. "A direction" shows the
  rose; pressing a rose button sets `dir`. "Past a place" shows the anchor
  box.
- **Anchor box.**
  - Chips: when the start or the minutes change while this box is open,
    call `/api/loop/features` and show up to 8 chips ("Lake Ontario",
    "Irondequoit Bay", …). Pressing one sets `feature`, clears the pin, and
    drops a display marker at the feature's `point`.
  - Search, on Enter only: call `/api/loop/features` with `q` and
    `/api/geocode` together. List feature matches first under "Scenic
    features" (those that don't fit are shown with "needs about 100 min" and
    can still be chosen), then "Places" from Nominatim. A place sets the pin.
    Nominatim is still called only on Enter, under the existing rate limit.
  - Map: with "Past a place" active and the start set, a map click sets or
    moves the anchor pin, and the pin can be dragged. Water is allowed.
  - After a search whose `steer.kind` is `shore`, replace the pin with
    `feature = steer.name`, move the marker to `steer.point`, and show the
    `note` in `#anchor-note`. The hash then holds the feature, so a reload
    repeats the same search.
- **Map clicks in loop mode.** No start: set it. Start set and steer is
  anchor: the anchor pin. Otherwise the hint says "The start is set. Drag
  the marker to move it."
- **Find loops** is enabled when the server is ready, a start is set, not
  busy, and the steer is complete (compass has a direction; anchor has a pin
  or a feature). `#hint-loop` says what is missing.
- Any change to start, minutes, steer, direction or anchor bumps a
  generation counter, like `state.gen`; an answer for an older generation is
  dropped.
- **Clear** removes the start and anchor, the loop results and the error.

### 9.4 Search and results

`findLoops()` mirrors `findRoutes()`: busy text "Looking for loops… (a few
seconds)", `POST /api/loop`, then `state.search = data`, all routes visible,
the first route selected, `writeHash()`, `renderAll(true)`.

When `state.search.mode === "loop"`:

- `renderTable`: the first header reads "Loop"; `#extra-head` and the extra
  cell are left out (sub-row `colSpan` 4); `extraMin` is not called. The
  caption reads "Scenic min: drive time on roads scenier than a typical New
  York road, weighted by how much scenier they are."
- The details line reads "Details: N loops compared in X s." plus the snap
  note for the start, the response `note`, and for the selected loop with
  `stem_km >= 1`: "Leaves and returns on the same road for the first 3.7 mi."
- `renderMap`, `drawScenery`, `drawMoments`, `drawWaypoints`, `renderDetail`,
  `mapsBlock`, `getWaypoints` and `select` work unchanged on loop entries.

### 9.5 URL hash

```
#mode=loop&start=43.15660,-77.60880&min=120&steer=compass&dir=N&route=North%20loop
#mode=loop&start=43.15660,-77.60880&min=60&steer=anchor&feature=Lake%20Ontario
#mode=loop&start=43.15660,-77.60880&min=60&steer=anchor&anchor=43.20000,-77.53300
```

`steer=any` is left out. `scenery=0` and `layer=` work as now. `writeHash`
writes this form in loop mode once a start is set; the A-to-B form is
unchanged. `maybeRunFromHash`: with `mode=loop` and a valid `start`, switch
tab, restore the controls, run `findLoops()`, then select the route whose
label matches `route`. With `mode=loop` and no start, just open the tab.

### 9.6 CSS

`.seg` copies the `.units` segmented look. `#rose` is a 3 × 3 grid of
square buttons with the pressed state of `.units button[aria-pressed="true"]`.
`.chips` is a wrapping row of small pill buttons. `.field` has the spacing
of `.place`. Add `html[data-mode="demo"] #panel-loop { display: none; }`.

## 10. Demo mode

Keep the public demo A-to-B only in this change. `.tabs` is already hidden
in demo mode; hide `#panel-loop` too and keep all loop wiring behind
`!DEMO`. No loop presets, and no change to the preset files or to
`scenic demo build`.

Reasons: the demo has no server and cannot run a steer; the preset schema,
its tests and `docs/PUBLISH.md` are written around A-to-B payloads; and the
public site only changes when the demo is rebuilt and published, which is
its own step. Loop presets fit later without rework: a preset with
`mode = "loop"`, `start`, `minutes` and a steer, built through `/api/loop`
and `/api/waypoints` in the same process, and the page already renders a
payload with `mode: "loop"`.

## 11. Tests for the parent to write

`tests/test_loop.py`, on toy graphs:

1. On a grid, `loops(..., Steer())` returns loops that start and end at H
   with time in the window.
2. True bearings: a point due north of H (same longitude) has bearing
   0 ± 0.1°, and `word()` flips at 22.5°. This catches the EPSG:5070 trap.
3. Corridor penalty: with a parallel road 30 m away and an alternative
   further off, the return leg takes the alternative; with `reuse_penalty = 0`
   it takes the parallel road.
4. No U-turn: no returned loop contains an arc followed by its reverse.
   Through rule: a via reached from its head side is not driven twice.
5. A moment via on a dead end drops its pairs without an exception. A point
   anchor on a dead end gets loops with `uturn = True`.
6. `loop_validity`: an out-and-back is rejected (`doubled` near 1); a ring
   has `doubled = 0`; a lollipop has a stem, with `stem_km` right, and is
   tier 2; samples near the start and finish of a ring are not flagged
   (cyclic separation).
7. Compass: with roads only east of H, `W` raises `no_loop_that_way` and `E`
   returns loops with `off_deg <= 67.5`; the weight is 1 at 22.5° and 0.5 at
   67.5°.
8. `resolve_anchor`: name match on feature and on body, ignoring case; a pin
   within 2000 m gives a node; a far pin near a water moment gives `shore`
   with the body name; far from everything gives `anchor_far`; too far for
   the budget gives `anchor_out_of_reach` with both numbers.
9. Every anchor-mode loop passes its anchor.
10. Score equals `score_route(sr, ctx, B, s_base)`; the moment cap follows
    the budget.
11. Selection: overlap limit, bearing separation and its relaxation, tier 1
    before tier 2, unique labels, and the same picks on a second run.
12. Work cap: with a tiny `dijkstra_work_arcs` the count stays at or under
    the cap plus one first via's work, and two runs agree.
13. The shared middle tree gives the same loops as one Dijkstra per pair.
14. After a call, the cost graphs hold trees for root H only.
15. `features_in_reach`: counts, `fits`, ordering, and `q` matching.

`tests/test_web.py`:

16. `/api/loop` payload: keys, `mode`, roles 1 to 3 and no role 0, unique
    labels, strict compact JSON.
17. Each error in the section 7 table, with `point` and `code`.
18. `/api/loop/features`.
19. `/api/waypoints` on a stored loop: at least one waypoint, origin equals
    destination, and the zero-waypoint guard.
20. `/api/health` has the `loop` block; loop and route requests share the
    lock; loop results expire with searches.
21. Static checks: `#tab-loop` is enabled with no "coming soon"; loop wiring
    is inside the `!DEMO` branch; the demo HTML hides `#panel-loop`;
    `test_demo_html_still_finds_its_markers` and
    `test_demo_mode_makes_no_api_calls` still pass.

`tests/test_waypoints.py`:

22. A closed `edge_path` reduces to at least one waypoint and a link whose
    origin equals its destination.

Also update `docs/WEB.md` (endpoints, the loop panel, the hash) and the
status line in `docs/PLAN.md`.

## 12. Measurements

Home is downtown Rochester (43.1566, −77.6088) unless named. Scores are
`scenic_min + 5 × moment points` with `s_base = 0.50`. Times are the
prototype's, which ran two penalised Dijkstras per loop and did not share
middle trees, and they exclude the reach cut and building the cost graphs.

### 12.1 Retracing (share of a loop's length that is doubled)

| Rule | 60 min median | 120 min median | 180 min median |
|---|---|---|---|
| no penalty | 0.29 | 0.66 | 0.23 |
| edge-identity penalty, ×3 | 0.17 | 0.24 | 0.14 |
| corridor penalty, 120 m, ×5 | 0.032 | 0.000 | 0.003 |

With the corridor penalty 88 %, 97 % and 100 % of loops are at or under
0.08. A penalty of ×10 or a 200 m radius gains nothing more. After the
U-turn rules no loop reverses at a via.

Tier 1 counts among loops in the time window (before today's pairing
change): Rochester 43 of 46 at 60 min and 30 of 30 at 120; Lake Placid 15 of
15 at 60; Sodus Point 14 of 16 at 60 (1.1 km stem); Buffalo 14 tier 1 and 6
tier 2 at 120; Manhattan 10 of 40 at 60, where the grid really does run
parallel. Montauk at 120 min gives one loop (116 min, net 0.098, 0.8 km
stem); three of its four pairs fail the U-turn ban on the peninsula.

Loop time over budget: q10 0.80, median 0.91 to 0.98, q90 1.00 to 1.05.

### 12.2 `any` (true bearings, final pairing rule)

| Home, budget | Picks: direction, minutes, score | In window | Time |
|---|---|---|---|
| Rochester 60 | N 62 min 34.9 · SE 53 min 21.4 · NE 54 min 20.5 | 45 of 70 | 1.3 s |
| Rochester 120 | NW 122 min 57.1 · NE 102 min 53.3 · S 119 min 42.5 | 36 of 72 | 1.7 s |
| Rochester 180 | E 183 min 80.3 · W 157 min 63.1 · SW 146 min 50.9 | 34 of 52 | 3.1 s |
| Buffalo 120 | N 123 min 65.4 · SE 118 min 64.3 · SW 123 min 62.9 (6.0 km stem) | 52 of 76 | 2.7 s |
| Ithaca 120 | W 121 min 77.5 · N 116 min 64.4 · NE 111 min 60.8 | 54 of 80 | 1.6 s |
| Manhattan 60 | E 57 min 45.9 · S 53 min 35.9 · N 58 min 22.1 | 57 of 68 | 2.0 s |
| Manhattan 180 | E 186 min 150.7 · N 174 min 98.1 · SE 136 min 94.3 | 26 of 60 | 5.2 s |
| Lake Placid 60 | E 62 min 38.3 · N 62 min 33.0 (two loops only) | 26 of 60 | 0.3 s |
| Lake Placid 180 | E 174 min 103.0 · W 187 min 94.3 · N 189 min 91.0 | 54 of 76 | 2.1 s |

The Rochester loops run the Lake Ontario State Parkway, Long Pond and
Braddock Bay to the northwest; the 120 min south loop reaches Canandaigua
Lake. An earlier 360 min run from Rochester took 7.9 s for 168 Dijkstras.

Why two estimate windows. Top three scores, Rochester 60 / 120 and Buffalo
120:

| Partner rule | Rochester 60 | Rochester 120 | Buffalo 120 |
|---|---|---|---|
| 3 from [0.60, 0.95] | 34.9, 21.4, 20.5 | 57.1, 42.3, 37.9 | 65.4, 62.9, 35.6 |
| 3 from [0.65, 0.92] | 29.5, 21.4, 20.5 | 46.9, 42.5, 41.9 | 64.3, 62.9, 62.1 |
| 2 tight, filled to 4 wide | 34.9, 21.4, 20.5 | 57.1, 53.3, 42.5 | 65.4, 64.3, 62.9 |

### 12.3 `compass` (true bearings, final rule)

Each pick: its own direction, offset from the request, score.

| Request | 60 min | 120 min | 180 min |
|---|---|---|---|
| Rochester N | N 22° 34.9 · N 4° 25.9 · NW 53° 22.2 | NE 31° 53.3 · NW 48° 54.4 · NW 56° 50.5 | NW 47° 73.9 · NW 58° 83.6 · NE 65° 73.2 |
| Rochester NE | NE 5° 29.2 · NE 21° 23.6 · N 37° 24.7 | NE 2° 58.7 · NE 4° 55.0 · NE 10° 54.8 | E 55° 79.8 · E 50° 54.3 (two loops) |

At 120 min: Rochester S gives S 46.8, S 43.4, S 38.2 (Canandaigua, Conesus
and Honeoye lakes); E gives E 43.3, E 37.3, SE 37.4; W gives NW 46.9, W 38.0,
NW 50.4. Ithaca S gives three S loops (50.6, 49.7, 48.9) and W three W loops
(74.1, 65.2, 56.3; Cayuga and Seneca lakes). Buffalo N gives N 69.3, N 54.6,
NE 58.4 and E gives E 53.1, NE 43.7, E 42.4. Manhattan N gives three NE
loops up the Hudson (49.4, 34.0, 26.2). Compass searches took 0.3 to 2.0 s.

What this shows:

- North of Rochester, 60 min gives true north loops along the shore. At 120
  and 180 min the lake blocks the way and the loops lean northwest or
  northeast, with offsets of 31° to 65°. The same holds for northeast at 180
  min, where everything that fits runs east along the shore.
- Northeast at 180 min yields one loop under the 0.5 overlap limit and two
  under 0.7: every candidate shares the shore road. This is why selection
  relaxes the overlap limit.
- Lake Placid N at 60 min gives one loop (N, 1° off, 33.0); SW gives none
  (4.11).
- A ring of vias at B/3 one way, the first design, found 8 vias for
  Rochester N at 60 min and none at 90, 120 or 180.

### 12.4 `anchor` (final window; scores of the three picks)

| Anchor | 60 min | 120 min | 180 min |
|---|---|---|---|
| Feature "Lake Ontario" | 33.4, 31.2, 24.7 (0.7 s) | 56.0, 55.7, 49.3 (0.8 s) | 81.8, 80.4, 73.4 (2.1 s) |
| Feature "Canandaigua Lake" | not in reach | 46.8, 35.6, 34.3 (1.2 s) | 67.2, 64.0, 63.8 (1.9 s) |
| Pin, Pittsford village | 15.2, 13.8, 9.3 | 48.2, 26.2, 24.9 | 78.2, 57.3, 41.8 |
| Pin, Irondequoit Bay centre (dead-end node) | 19.5, 17.6, 17.6 | 49.8, 40.3, 38.4 | |
| Pin, Durand Eastman Park | 24.5, 24.1, 17.9 | 54.2, 50.4, 37.7 | |
| Pin, Hamlin Beach State Park | out of reach (73 min round trip) | 52.3, 40.4, 39.1 | |
| Pin 3 km offshore → Lake Ontario shore | 31.5, 30.5, 25.7 | 60.8, 52.9, 50.2 | |

The offshore row is from an earlier run with the partner window
[0.60, 0.95]. Under that window the Pittsford pin got one loop at 120 min
(its triangles built at 78 to 83 or 126 to 135 min); [0.65, 0.92] gives it
three at every budget.

Snap distances that set the rules: 1 km offshore 1,383 m; 3 km offshore
3,145 m; 5 km offshore 4,952 m; Irondequoit Bay centre 371 m; the middle of
Canandaigua Lake 648 m; the middle of Seneca Lake 1,108 m. So a pin in the
middle of a Finger Lake becomes a node on the nearest shore road, and a pin
well out in Lake Ontario becomes a shore anchor.

## 13. Not tested: check these while implementing

1. **Sharing the middle-leg tree** per (first via, λ). It is the same
   computation by construction, but the prototype ran one Dijkstra per pair.
   Test 13 covers it.
2. **The work cap** and its ordering (4.6). The cap formula comes from the
   measured cost per Dijkstra; no run was capped. Time the 360 min case.
3. **Anchor mode in the true-bearing frame.** The anchor runs used grid
   bearings for the 45° partner sectors. Only the sector boundaries move; the
   scores in 12.4 should hold to within a pick.
4. **Dead-end retry for feature targets.** Measured for a point anchor only.
5. **Taking the smaller-`est` arc of a key** when choosing partners. The
   prototype took whichever arc came first.
6. **The `needed_min` formula** and the wider reach pass for
   `anchor_out_of_reach`.
7. **`features_in_reach` ordering** was looked at for Rochester only.
8. **The server path.** `reduce_to_waypoints` was called directly on loops,
   not through `App.waypoints`.
9. **The "any" mode fallback to fewer than three loops** in sparse regions
   is reported honestly (Lake Placid at 60 min gives two) but lens loops were
   not tried as a filler for `any` or `compass`.

Acceptance checks on the real graph, worded so small differences don't
break them: Rochester `any` at 120 min returns 3 loops with net doubled
≤ 0.10 and a top score of at least 50; Rochester `N` at 60 min returns a
loop labelled North; Rochester feature "Lake Ontario" at 60 min returns 3
loops that each credit a Lake Ontario moment; a pin at (43.29, −77.60)
resolves to kind `shore`, name "Lake Ontario"; Lake Placid `SW` at 60 min
raises `no_loop_that_way`.

## 14. Later

- Two Google Maps links (out half, back half) for loops that come back
  `capped`, to lift the 9-waypoint limit.
- Loop presets in the public demo (section 10).
- A clockwise or counter-clockwise choice; more than one anchor.
