# Scenery score v0.2: design after two failed attempts

Status: 2026-11-02, design only, commit 77103d6. All numbers were computed
read-only from `data/score/` (v0.1), `data/score_v0/` and `data/graph/`. The
what-ifs re-ran `scenic.score.combine` on the stored per-edge signals (v0.1 S
reproduced to max |ΔS| = 0 first). Point-level signals (W sight lines) could
not be re-run; their effects are estimated from diagnostics and marked so.

## 0. What the numbers say

1. **The validation target is wrong twice.** A 0.70 median over all designated
   byways is unreachable with a statewide percentile: 44 % of Revolutionary
   Byway km and 28 % of Seaway Trail km are in towns (dev > 0.3) and their
   rural km sit at 0.50. Only 15 of 25 designated byways beat the non-byway
   roads of the same classes within 15 km (median gap +0.03); the Catskill byway
   is 0.88 but its neighbours are 0.90. The "dull ≤ 0.35" rule fails flat
   farmland by construction: S is a percentile and farmland (L ≈ 0.5) sits at
   0.3–0.45 (NY-5 Batavia 0.31, NY-98 0.35, NY-104 Ridge 0.41).
2. **"Developed" is counted three times.** corr(L, dev) = −0.94, corr(V, dev)
   = −0.82 (V's `open` factor is 1 − dev − forest), then the town penalty. A
   Geneva lakefront edge with W 0.87, V_water 1.0 has raw 0.48, penalty 0.19,
   pre 0.31; 1 km smoothing with Geneva streets (pre ≈ −0.2) gives smooth −0.01
   → S 0.27. Skaneateles front: W 0.59–0.82 → S 0.28–0.38. The lake crossings
   are in the signals and erased by the combination.
3. **V and H double count.** corr(V_prospect, H_relief) = +0.84: prospect is
   relief at 3 km. 30 % of state length has prospect ≥ 0.5, p90 = 0.82; Catskill
   and High Peaks byways average 0.95–1.00. It saturates on every plateau edge.
4. **Edge-level tweaks do not fix the failures.** No penalty, weight or V
   change moves the original scenic list off 8/10 or the byway median off
   0.55 ± 0.01 (table in §3). The one edge-level change that does is max-pooled
   smoothing: 10/10 scenic, byway median 0.595.
5. **The failing scenic roads are "moment" roads.** NY-436 is one bridge over
   the Letchworth gorge plus 12 km of plateau; NY-12 is bay crossings behind a
   cottage strip (strip 64 % developed, water 490 m away, road 8 m above it);
   NY-28 Old Forge–Inlet has a lake ≥ 1 km² within 400 m for 56 % of its points
   and within reach for 91 %, but the strip is 48 % forest and per-cell transmit
   (0.4 per 30 m) gives vis ≈ 0.4^n → W = 0.08.
6. **The router has no leverage in flat corridors, and no transform of S fixes
   it.** Rochester→Syracuse: cost t·(1+λ(1−S)) returns I-90 for every λ ≤ 12,
   then jumps to +46 % (NY-370/Lake Road, S 0.47). With (1−S)^2 or (1−S)^3 the
   first move is still +43 %. Where corridor contrast exists the cost works at
   λ = 1–3: Syracuse→Skaneateles +27 % (S 0.32 → 0.60, US-20), Port
   Jervis→Hancock +4 % (0.52 → 0.84, NY-97), Albany→Lake Placid +41 % (0.42 →
   0.70 at k = 2).

Selectability bound (why 6 is structural): a detour with time ratio ρ and mean
S1 against a fast route with S0 wins for some λ only if **S1 > 1 − (1−S0)/ρ**.
Rochester→Syracuse has S0 = 0.254, so a +30 % route needs S1 > 0.43 and a
+50 % route S1 > 0.50. US-20 Canandaigua–Skaneateles is 0.33 (0.42 under the
best edge-level variant). No λ grid can produce a +30 % route here.

## 1. Validation that measures what the router needs

Five checks replace the byway median. A run passes when V1–V3 pass; V4–V5
are reported.

**V1 Corridor test (primary).** Eight OD pairs with named scenic content:
Rochester→Syracuse (Skaneateles or Geneva lakefront, Sodus Bay/Lake Road),
Rochester→Geneva (NY-14, Canandaigua shore), Syracuse→Skaneateles (US-20),
Albany→Lake Placid (NY-22 shore, NY-9N), Buffalo→Jamestown (US-62/NY-5,
US-219), Ithaca→Geneva (NY-89 end to end), Port Jervis→Hancock (NY-97),
Kingston→Oneonta (NY-28). Run the full search (§4). Pass: for ≥ 6/8 pairs the
best route under +30 % differs from the fastest, has scenic minutes ≥ 8 and
contains a named road or moment the fastest route lacks; *and* the kept set
has a route in each band (+5–20, +20–30, +30–50 %) for ≥ 6/8. Today (λ sweep
only): Syracuse→Skaneateles and Port Jervis→Hancock pass; Rochester→Syracuse
and Rochester→Geneva fail; the others were not run.

**V2 Paired contrast.** For each scenic stretch (original list and holdout-2),
the fastest route between its end towns is the "fast alternative" if it shares
< 20 % of its length with the stretch; S0 is its mean S. Pass per pair:
S_stretch − S0 ≥ 0.25 and S_stretch > 1 − (1−S0)/1.3. Overall: ≥ 80 % of
non-trivial pairs.

**V3 Absolute lists (kept, loosened).** Scenic ≥ 0.65 for ≥ 8/10. Dull split:
urban strips ≤ 0.20 (7 items, need 6); flat rural 0.20 ≤ S ≤ 0.50 (NY-5
Batavia–Le Roy, NY-98, NY-104 Ridge; all three). The 0.35 farmland rule goes.

**V4 Byways, reported only.** (a) Landscape subset (Adirondack, Central and
Southern Adirondack, Roosevelt-Marcy, High Peaks, Olympic, Catskill, Mountain
Cloves, Durham Valley, Upper Delaware, Shawangunk, Cayuga, Seneca, Route 90,
Dude Ranch, Lakes to Locks): length-weighted median ≥ 0.65. (b) Every
designated byway's rural km (dev ≤ 0.3) median vs same-class non-byway roads
within 15 km: ≥ 18/25 beat theirs (today 15/25). Heritage byways (Seaway,
Revolutionary, Maple Traditions, Military, North Fork, Black River, Mohawk
Towpath, Route 20, Southtowns) are listed, never thresholded.

**V5 Moments.** Thirty hand-named moments (Geneva lakefront, Skaneateles
front, Portageville bridge, Hawk's Nest, Cascade Lakes, Rip Van Winkle bridge,
Thousand Islands bridge approach, …) each within 300 m of a detected moment;
fifteen hand-named non-moments (Syracuse canal bridges, I-90 Genesee crossing,
Hempstead Tpke pond) not. Pass ≥ 27/30 and ≤ 2/15.

**Holdout honesty.** Holdout-1 is burnt: its sub-signals were printed in v0.1
diagnostics and used here. Holdout-2 rules: the user picks 10 scenic stretches,
5 urban strips, 3 flat-rural roads and 4 OD pairs from roads named in no doc,
log or script in the repo; they go into `[holdout2]` before the first v0.2
line of code; nothing is computed on them until the v0.2 run is declared final;
results are written once to `validation.json`. Looking early retires the set.
Unscored candidate pool: NY-3 Saranac–Tupper, NY-86 Wilmington Notch, NY-8
Speculator–Wells, NY-30 Long Lake–Tupper, NY-28N, NY-23 Windham, NY-21 Naples,
NY-54A, US-9W Catskill–Saugerties; strips NY-104 Webster, NY-33 Buffalo; flat
NY-31 Brockport–Albion, NY-63 Batavia–Oakfield.

## 2. Rochester→Syracuse corridor

US-20/NY-5 at 1 km (`.scratch/us20_profile.py us20 1`): countryside 0.36–0.46
(L 0.45–0.65, W 0, prospect 0.1–0.25); town centres 0.04–0.19 (Canandaigua km
0–4, Geneva 23–27, Waterloo–Seneca Falls 37–38, Auburn 63–69, Skaneateles 78).
Lake crossings at the edge level:

| place | W | V_water | raw | penalty | pre | smooth | S |
|---|---|---|---|---|---|---|---|
| Geneva, Lake Front Dr (374 m) | 0.87 | 1.00 | 0.48 | 0.19 | 0.31 | −0.01 | 0.27 |
| Geneva front, 5 km mean | 0.35 | 0.66 | 0.21 | 0.25 | −0.03 | −0.06 | 0.24 |
| Cayuga N end, 5&20 (1 km edge) | 0.68 | 0.00 | 0.33 | 0.02 | 0.31 | 0.25 | 0.59 |
| Skaneateles, Genesee St (199 m) | 0.82 | 0.84 | 0.39 | 0.30 | 0.09 | 0.01 | 0.28 |
| Canandaigua Lakeshore Dr (2 km) | 0.61 | 0.72 | 0.29 | 0.19 | 0.13 | 0.12 | 0.35 |

The one crossing without a town (Cayuga) survives at 0.59; the others lose
0.19–0.30 to the penalty and the rest to smoothing. Statewide, 37 % of the
4,434 km with W ≥ 0.5 or V_water ≥ 0.7 is in towns and 30 % ends below S 0.5.

Via-point routes with today's S (fastest legs): Skaneateles front +24 % (mean S
0.30), Sodus Bay +30 % (0.31; +40 % and 0.44 with λ = 1 legs), Geneva +
Skaneateles on 5&20 +41 % (0.29). "+30 % buys 5&20" holds for one lake, not
three. None is reachable by a λ sweep (hull +0 % → +46 %). Verdict: keep
t·(1+λ(1−S)), λ ∈ {0.5, 1, 2, 3, 5}; convex transforms only push the first move
out (+90 % at k = 3). Leverage must come from §3 and §4.

## 3. v0.2 formula changes, ranked by impact

Edge-level what-ifs ("hold" is the burnt holdout-1, a trend only; "first move"
is the smallest λ that leaves I-90 for Rochester→Syracuse and its time ratio):

| variant | scenic | dull | hold | byway med | Seneca front | Skan. | I-90 | US-20 | first move |
|---|---|---|---|---|---|---|---|---|---|
| v0.1 | 8/10 | 10/10 | 6/10, 4/5 | 0.553 | 0.24 | 0.31 | 0.254 | 0.326 | λ20 +46 % |
| town penalty off | 8 | 10 | 6, 4 | 0.549 | 0.33 | 0.33 | 0.179 | 0.323 | λ8 +46 % |
| motorway penalty off | 8 | 10 | 6, 4 | 0.546 | 0.23 | 0.30 | 0.317 | 0.317 | none |
| multiplicative penalty | 8 | 10 | 7, 4 | 0.545 | 0.34 | 0.48 | 0.258 | 0.322 | none |
| + water-gated town | 8 | 10 | 6, 4 | 0.545 | 0.39 | 0.51 | 0.256 | 0.326 | none |
| V without prospect | 8 | 10 | 7, 3 | 0.562 | 0.24 | 0.32 | 0.257 | 0.332 | λ8 +46 % |
| W 0.45 / H 0.25 | 9 | 10 | 7, 4 | 0.563 | 0.25 | 0.33 | 0.259 | 0.337 | λ12 +48 % |
| max-pool smoothing (0.5) | 10 | 10 | 7, 4 | 0.595 | 0.33 | 0.36 | 0.257 | 0.361 | λ8 +45 % |
| A (changes 2 + 4), no pool | 8 | 10 | 7, 4 | 0.561 | 0.47 | 0.59 | 0.289 | 0.342 | λ20 +48 % |
| **A + pool** | 9 | 10 | 8, 4 | 0.618 | 0.72 | 0.68 | 0.317 | 0.415 | λ5 +5 % |

Still failing under A + pool: US-20 Skaneateles–LaFayette (< 0.70), NY-12,
NY-436, NY-104 Ridge (passes V3's rural band). NY-12 and NY-436 are moment
roads for §4, not for more edge tuning.

**1. Pooled smoothing.** Keep the 1 km same-road window and weights w_ij.
`smooth_i = 0.5·mean_i + 0.5·top_i`, where `top_i` is the length-weighted
mean of pre over the best 300 m of the window (sort window edges by pre
descending, accumulate w_ij until 300 m; window shorter than 300 m: top =
max). A driver remembers the best view of the last kilometre, not the average;
the window was meant to stop a crossroads bleeding into a lake road, not to
average the lake away. The what-if used a plain max (10/10 scenic, dull
unchanged); the 300 m form stops a 20 m bridge edge lifting a kilometre.

**2. Penalties multiplicative and water-gated; no motorway term.**
```
P   = 0.15·clip((junc_km − 4)/16, 0, 1) + 0.20·clip(sig_km/3, 0, 1)
      + 0.25·clip((dev − 0.3)/0.5, 0, 1)·(1 − max(W, V_water)) + 0.10·urban_speed
P   = min(P, 0.5)
pre = raw·(1 + 0.10·Q + 0.05·C)·(1 − P)
```
A subtractive penalty makes a strip −0.4 (p10 of smooth is −0.26) and
smoothing then drags any lakefront within 500 m to zero; multiplicative keeps
dull roads dull (raw ≈ 0.05 stays bottom-decile; all 10 dull items pass) and
cannot erase a view. The town penalty is for streets where buildings are all
there is to see; with water in view the town is the lake's foreground. The
motorway term duplicates Q = 0 and the junction term; without it the dull list
still passes and NY-17 Hancock–Deposit (raw 0.38) passes.

**3. W for forested and set-back shores (point-level; estimated).**
- Obstruction counts at most the first three obstructed 30 m cells from the
  road (the roadside screen); further cells do not multiply. Forest transmit
  0.4 → 0.5, so a fully wooded strip floors at 0.125 instead of 0.4^n → 0.
- Shore-road term: an areal water cell with size ≥ 0.4 within 120 m of the
  point gives `W_pt ≥ 0.5·size` regardless of the sight line (the lake shows
  through every gap, driveway and bay), fading linearly to 0 at 300 m.
- Keep reach, bands, fan and aim-past-shore as they are.
Expected: NY-28 Old Forge–Inlet W 0.08 → ≈ 0.25–0.35, enough with 1–2 for
S ≥ 0.70. NY-12 (water 490 m behind a cottage strip) and NY-22 Westport–Essex
(lake median 3.8 km) are not reachable by W and should not be chased with it.

**4. V without prospect; weights 0.40 / 0.30 / 0.15 / 0.15.**
```
V   = open·(0.35 + 0.65·V_water) + forest·0.25
raw = 0.40·W + 0.30·H + 0.15·V + 0.15·L
```
Prospect is 3 km relief (corr +0.84 with H_relief) and saturates; H already
carries hills. V_water is the only part of V that is not land cover or relief.
Real vistas become a moment type (§4). The 0.05 goes to W, the user's first
priority.

**5. Do not change.** H, L's class table and mix term, Q, C, the statewide
percentile rank (λ must mean the same everywhere; leverage comes from §4), the
1 km window definition, sampling, exclusions, water extraction, reach bands,
V_water, the dull list. No convex cost, no region-relative rank, no AADT yet.

## 4. Route-level scenic moments (yes, build it)

Averaging erases what the user wants to drive to. Add a moment layer on top of
S and let the router collect moments under the time budget.

**Detection (offline, in `score`, writes `data/score/moments.parquet`).**
Candidates: non-excluded edges, highway in {motorway, trunk, primary,
secondary, tertiary, unclassified}, unsmoothed per-edge values:
- `water`: W ≥ 0.5 or V_water ≥ 0.7; `m = max(W, V_water)·(0.5 + 0.5·size)`.
- `vista`: open ≥ 0.6, drop (height above the lowest ground within 3 km)
  ≥ 150 m, terrain sight line on dem90 ≥ 2 km in the fan; `m = clip(drop/300, 0.5, 1)`.
- `gorge/falls`: OSM `waterway=waterfall` or `natural=cliff` within 150 m of a
  sample point, or a `bridge=yes` edge over areal water with the DEM dropping
  ≥ 30 m within 200 m; `m = 0.8`.
- `osm`: `tourism=viewpoint` within 100 m of a sample point; `m = 0.7`.
Cluster along the same road key within 2 km (keep max m, its midpoint), then
across roads within 1 km (one lake seen from two roads is one moment). Store
id, type, m, lon/lat, edge_idx, road key. Expect ≈ 2–3 k water moments (from
4,434 km of candidates) and a few hundred vistas. V5 checks them.

**Route score.**
```
scenic_min = Σ_e t_e·max(0, S_e − S_fast) / 60              (PLAN §2.3)
moment_pts = Σ_k m_k over distinct moments within 100 m of the route,
             at most 1 per 2 km of route and 6 per hour of t0
score      = scenic_min + 5·moment_pts         (a full moment = 5 scenic minutes)
```
**Search (online).** (1) λ sweep as now, keep distinct routes. (2) One Dijkstra
from A and one reverse from B on fastest time give t(A→M)+t(M→B) for every
moment at once; keep those ≤ 1.5·t0, rank by m / extra minutes. (3) For the
top 12 build A→M→B with λ = 1 legs; greedily add a second and third moment
(re-rank against the current route) while t ≤ 1.5·t0. (4) Score every kept
route; present the best under +15, +30, +50 %. (5) Moments become the first
waypoints in the Google Maps reduction. With today's S this already yields
Skaneateles front at +24 % and Sodus Bay at +30 %; with §3 the lakefronts score
≈ 0.7 so scenic_min agrees with moment_pts instead of fighting it.

## 5. Order of work and expected result

1. v0.2a: changes 1, 2, 4 (one `combine` change, one config change). Expect
   original scenic 9/10, dull 10/10, landscape-byway median ≥ 0.62, Seneca and
   Skaneateles fronts ≥ 0.65, Rochester→Syracuse first move at λ ≈ 5, +5 %.
2. v0.2b: change 3 (W, point level). Expect NY-28 Old Forge–Inlet ≥ 0.70 and
   no dull regression (dull roads have no water).
3. v0.2c: moments, search, V1/V2/V5 in `validate.py`. Write holdout-2 before
   step 1; score it once after step 3.
