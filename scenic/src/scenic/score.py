"""Scenery score v0.2 (docs/SCORE_SPEC.md, with its "v0.1 changes" and "v0.2
changes"): one score S in [0, 1] per edge of edges.parquet.

Stages (each writes its product to disk and is skipped on rerun while its
fingerprint, the hash of its code, config sections and input files, is
unchanged and its outputs are as it left them; `--force` reruns them):

  grid       working 30 m grid in EPSG:5070            rasters/grid.json
  dem        3DEP mosaic warped to the grid             rasters/dem30.tif, dem90.tif
  nlcd       NLCD window                                rasters/nlcd30.tif
  water      OSM + NLCD water masks and size score      rasters/water_area.tif, water_line.tif, water_size.tif
  water90    water reach, surface height, broad water   rasters/wfrac90.tif, reach90.tif, water_z90.tif, bigwater_idx90.tif
  terrain90  relief, TPI, 3 km min/max at 90 m          rasters/relief90.tif, tpi90.tif, min3k90.tif, max3k90.tif
  land90     land cover score, mix, open, forest, dev   rasters/lscore90.tif, mix90.tif, open90.tif, forest90.tif, dev90.tif
  controls   traffic signal nodes from the PBF          score/signal_nodes.npy
  edges      sample points and per-edge signals         score/edge_signals.parquet, score/samples.npz
  score      penalties, smoothing, percentile rank      score/scores.parquet
  outputs    heatmap and summary                        score/heatmap.png, score/summary.md
  validate   byways and spot checks                     score/validation.json

Each stage runs in its own child process so its runtime and peak RSS are
reported separately in score_report.json.
"""

from __future__ import annotations

import json
import re
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import config_dir, data_dir
from .rasters import Grid

CONFIG_DIR = config_dir()
SCORE_COLUMNS = ("W", "H", "V", "L", "Q", "C")


def load_config(path: Path | None = None) -> dict:
    with open(path or CONFIG_DIR / "score.toml", "rb") as f:
        return tomllib.load(f)


# --------------------------------------------------------------------------
# Edges


@dataclass
class Edges:
    u: np.ndarray
    v: np.ndarray
    way_id: np.ndarray
    length_m: np.ndarray
    highway: np.ndarray  # object (str)
    name: np.ndarray  # object (str or None)
    ref: np.ndarray
    surface: np.ndarray
    maxspeed_tagged: np.ndarray
    speed_mph: np.ndarray
    off: np.ndarray  # int64 (n + 1) vertex offsets
    x: np.ndarray  # projected vertex x (EPSG:5070)
    y: np.ndarray

    @property
    def n(self) -> int:
        return len(self.u)


def project_lonlat(lon, lat, crs: str = "EPSG:5070") -> tuple[np.ndarray, np.ndarray]:
    from pyproj import Transformer

    tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    x, y = tr.transform(np.asarray(lon, dtype=np.float64), np.asarray(lat, dtype=np.float64))
    return np.asarray(x), np.asarray(y)


def _dict_col(t, name) -> np.ndarray:
    """Dictionary column -> object array, built from the dictionary (fast)."""
    col = t.column(name).combine_chunks()
    if hasattr(col, "dictionary"):
        vals = np.asarray(col.dictionary.to_pylist() + [None], dtype=object)
        idx = col.indices.fill_null(len(vals) - 1).to_numpy()
        return vals[idx]
    return np.asarray(col.to_pylist(), dtype=object)


def read_edges(graph_dir: Path) -> Edges:
    import pyarrow.parquet as pq

    t = pq.read_table(Path(graph_dir) / "edges.parquet")
    lon = t.column("lon").combine_chunks()
    lat = t.column("lat").combine_chunks()
    x, y = project_lonlat(lon.values.to_numpy(), lat.values.to_numpy())
    return Edges(
        u=t.column("u").to_numpy(), v=t.column("v").to_numpy(), way_id=t.column("way_id").to_numpy(),
        length_m=t.column("length_m").to_numpy(),
        highway=_dict_col(t, "highway"), name=_dict_col(t, "name"), ref=_dict_col(t, "ref"),
        surface=_dict_col(t, "surface"),
        maxspeed_tagged=t.column("maxspeed_tagged").to_numpy(), speed_mph=t.column("speed_mph").to_numpy(),
        off=lon.offsets.to_numpy().astype(np.int64), x=x, y=y,
    )


# --------------------------------------------------------------------------
# Sampling


@dataclass
class Samples:
    x: np.ndarray  # float64
    y: np.ndarray
    edge: np.ndarray  # int32 edge index per point
    n: np.ndarray  # points per edge
    off: np.ndarray  # int64 (n_edges + 1)

    def mean(self, vals: np.ndarray) -> np.ndarray:
        """Per-edge mean of a per-point value."""
        return np.bincount(self.edge, weights=vals, minlength=len(self.n)) / self.n


def sample_points(off: np.ndarray, x: np.ndarray, y: np.ndarray, length_m: np.ndarray,
                  step: float = 50.0) -> Samples:
    """Evenly spaced points along each edge polyline, both ends included:
    n_i = max(2, ceil(length_m / step) + 1). Vectorised over all edges."""
    n_e = len(off) - 1
    seg = np.hypot(np.diff(x), np.diff(y))
    last = off[1:] - 1  # last vertex of each edge
    seg[last[:-1]] = 0.0  # no length between an edge's last vertex and the next edge's first
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    start = off[:-1]
    plen = cum[last] - cum[start]

    n = np.maximum(2, np.ceil(np.asarray(length_m) / step).astype(np.int64) + 1)
    p_off = np.concatenate([[0], np.cumsum(n)])
    edge = np.repeat(np.arange(n_e, dtype=np.int32), n)
    k = np.arange(p_off[-1]) - np.repeat(p_off[:-1], n)
    frac = k / np.repeat(n - 1, n)
    t = cum[start][edge] + frac * plen[edge]
    j = np.searchsorted(cum, t, side="right") - 1
    j = np.clip(j, start[edge], last[edge] - 1)
    s = cum[j + 1] - cum[j]
    with np.errstate(invalid="ignore", divide="ignore"):
        a = np.clip(np.where(s > 0, (t - cum[j]) / s, 0.0), 0, 1)
    px = x[j] + a * (x[j + 1] - x[j])
    py = y[j] + a * (y[j + 1] - y[j])
    return Samples(px, py, edge, n, p_off)


def lookup(grid: Grid, arr: np.ndarray, px, py, fallback=0.0) -> np.ndarray:
    r, c, inside = grid.rowcol(px, py)
    out = arr[r, c].astype(np.float64)
    out[~inside] = fallback
    return out


# --------------------------------------------------------------------------
# Signals


def sight_line(px, py, ze, tx, ty, zt, grid: Grid, dem: np.ndarray, step: float, tol: float,
               cls: np.ndarray | None = None, canopy: np.ndarray | None = None, log_t: np.ndarray | None = None,
               stop=None, dem_nodata: float = -32768, max_samples: int = 20_000_000,
               max_obstructed: int | None = None) -> np.ndarray:
    """Share of the view from (px, py, eye height ze) to (tx, ty, zt) that
    gets through, per ray, in [0, 1].

    The ray is sampled at every positive multiple of `step` strictly before
    `stop` (default: the target). Bare ground (`dem`) more than `tol`
    above the straight sight line blocks it (0). With `cls`, a cell whose
    obstacle top (ground + canopy[class]) rises above the sight line passes
    exp(log_t[class]) of the view, and those shares multiply along the ray;
    with `max_obstructed`, only the first that many such cells from the
    observer do (the roadside screen; cells further out do not multiply)."""
    px, py, ze, tx, ty, zt = (np.asarray(a, dtype=np.float64) for a in (px, py, ze, tx, ty, zt))
    n = len(px)
    vis = np.ones(n)
    if n == 0:
        return vis
    d = np.hypot(tx - px, ty - py)
    reach = d if stop is None else np.minimum(np.asarray(stop, dtype=np.float64), d)
    # Samples at j * step for j = 1 .. K, all strictly short of `reach`.
    K = np.maximum(np.ceil(reach / step - 1e-9) - 1, 0).astype(np.int64)
    cum = np.cumsum(K)
    a = 0
    while a < n:
        base = cum[a - 1] if a else 0
        b = max(a + 1, int(np.searchsorted(cum, base + max_samples, side="right")))
        k = K[a:b]
        tot = int(k.sum())
        if tot:
            idx = np.repeat(np.arange(b - a), k)
            j = np.arange(tot) - np.repeat(np.cumsum(k) - k, k) + 1
            frac = j * step / d[a:b][idx]
            qx = px[a:b][idx] + frac * (tx[a:b][idx] - px[a:b][idx])
            qy = py[a:b][idx] + frac * (ty[a:b][idx] - py[a:b][idx])
            r, c, inside = grid.rowcol(qx, qy)
            g = dem[r, c].astype(np.float64)
            g[~inside | (g <= dem_nodata) | ~np.isfinite(g)] = -np.inf
            los = ze[a:b][idx] + (zt[a:b][idx] - ze[a:b][idx]) * frac
            blocked = np.bincount(idx, weights=g > los + tol, minlength=b - a) > 0
            lt = np.zeros(tot)
            if cls is not None:
                cc = cls[r, c]
                h = canopy[cc]
                lt = np.where((h > 0) & inside & (g + h > los), log_t[cc], 0.0)
                if max_obstructed is not None:
                    # Samples of one ray are contiguous and ordered outward.
                    ob = lt < 0
                    cs = np.cumsum(ob)
                    first = np.cumsum(k) - k
                    rank = cs - np.repeat(np.concatenate([[0], cs])[first], k)
                    lt = np.where(ob & (rank > max_obstructed), 0.0, lt)
            vis[a:b] = np.where(blocked, 0.0, np.exp(np.bincount(idx, weights=lt, minlength=b - a)))
        a = b
    return vis


def first_hit(px, py, ux, uy, maxd, grid: Grid, mask: np.ndarray, step: float):
    """March each ray (px, py) + t * (ux, uy), t = step, 2 step, ... <= maxd,
    to the first cell where `mask` is true. Returns (t, row, col); t = inf
    when the ray finds none."""
    n = len(px)
    t_hit = np.full(n, np.inf)
    hr = np.zeros(n, np.int64)
    hc = np.zeros(n, np.int64)
    act = np.arange(n)
    maxd = np.broadcast_to(np.asarray(maxd, dtype=np.float64), (n,))
    k = 1
    while len(act):
        t = k * step
        act = act[t <= maxd[act]]
        if len(act) == 0:
            break
        r, c, ins = grid.rowcol(px[act] + ux[act] * t, py[act] + uy[act] * t)
        hit = ins & mask[r, c]
        h = act[hit]
        t_hit[h] = t
        hr[h] = r[hit]
        hc[h] = c[hit]
        act = act[~hit]
        k += 1
    return t_hit, hr, hc


def fan_directions(ux, uy, fan_deg) -> list[tuple[np.ndarray, np.ndarray]]:
    """The unit vectors (ux, uy) rotated by each angle in `fan_deg`."""
    out = []
    for a in fan_deg:
        ca, sa = np.cos(np.radians(a)), np.sin(np.radians(a))
        out.append((ux * ca - uy * sa, ux * sa + uy * ca))
    return out


def obstacle_tables(cfg: dict) -> tuple[np.ndarray, np.ndarray]:
    """(canopy height, log transmission) lookup tables over NLCD class codes."""
    w = cfg["water"]
    canopy = np.zeros(256)
    log_t = np.zeros(256)
    for k, h in w["canopy_m"].items():
        canopy[int(k)] = h
    for k, t in w["transmit"].items():
        log_t[int(k)] = np.log(max(t, 1e-9))
    return canopy, log_t


def near_falloff(d, reach, full_min: float, full_frac: float) -> np.ndarray:
    """1 within max(full_min, full_frac * reach), then linear to 0 at reach."""
    reach = np.asarray(reach, dtype=np.float64)
    full = np.minimum(np.maximum(full_min, full_frac * reach), reach - 1.0)
    return np.clip((reach - np.asarray(d, dtype=np.float64)) / (reach - full), 0, 1)


def view_score(px, py, ze, ux, uy, d, wr, wc, val, reach, beyond: bool, grid: Grid, dem, nlcd, canopy, log_t,
               w: dict, zsurf_of=None) -> np.ndarray:
    """val * vis for rays from (px, py) along (ux, uy) to water cell (wr, wc)
    at distance d. Closer than vis_min_dist_m: vis = 1. For areal water the
    sight line aims past the shore (the open water beyond the shore strip,
    not the water right behind it); obstacles are checked up to the shore.
    The sight line ends on the water surface, `zsurf_of(x, y)` (NaN: unknown);
    without it, or where it is unknown, on the DEM at the water cell (which
    may be bathymetry)."""
    out = np.asarray(val, dtype=np.float64).copy()
    far = np.flatnonzero(d >= w["vis_min_dist_m"])
    if len(far) == 0:
        return out
    zt = dem[wr[far], wc[far]].astype(np.float64)
    zt = np.where(zt <= -32768, ze[far] - w["eye_m"], zt)
    if zsurf_of is not None:
        zs = np.asarray(zsurf_of(*grid.centres(wr[far], wc[far])), dtype=np.float64)
        zt = np.where(np.isfinite(zs), zs, zt)
    ext = np.zeros(len(far))
    if beyond:
        ext = np.minimum(np.clip(w["beyond_frac"] * d[far], grid.res, w["beyond_max_m"]),
                         w["beyond_reach_frac"] * reach[far])
    tx = px[far] + ux[far] * (d[far] + ext)
    ty = py[far] + uy[far] * (d[far] + ext)
    out[far] *= sight_line(px[far], py[far], ze[far], tx, ty, zt, grid, dem, w["ray_step_m"], w["terrain_tol_m"],
                           nlcd, canopy, log_t, stop=d[far], max_obstructed=w.get("max_obstructed_cells"))
    return out


def water_points(s: Samples, grid: Grid, area: np.ndarray, line: np.ndarray, size: np.ndarray, reach_of,
                 z_pt: np.ndarray, dem: np.ndarray, nlcd: np.ndarray, cfg: dict,
                 zsurf_of=None) -> tuple[np.ndarray, np.ndarray]:
    """Per-point W and nearest-water distance (inf when none in reach), v0.1.

    area: bool 30 m areal-water mask; line: uint8 linear water (river, canal,
    stream) outside it; size: size score per cell; reach_of(x, y) -> reach (m)
    of areal water at those cell centres; z_pt: ground height at the points;
    dem, nlcd: 30 m rasters for the sight lines; zsurf_of(x, y) -> water
    surface height (m) at water cell centres (view_score).

    Areal water is represented by its shore cells (a cell with a 4-neighbour
    that is not water); points on water (bridges, causeways) get W = size.
    Shore cells are split into bands by reach and each band has its own
    KD-tree, so a pond next to the road cannot hide the lake behind it. From
    each point, sight lines go to the nearest shore cell of the band and
    along a fan (water.fan_deg) either side of it, to the first water cell
    within the band's reach. W_pt = max over bands and sight lines of
    size * falloff(d, reach) * vis.

    v0.2 shore-road term (water.shore_road): within full_m of an areal
    water cell of size >= min_size, W_pt >= frac * size whatever the sight
    lines say (the lake shows through every gap), fading linearly to 0 at
    zero_m. The nearest such cell counts."""
    from scipy import ndimage
    from scipy.spatial import cKDTree

    w = cfg["water"]
    n = len(s.x)
    W = np.zeros(n)
    dmin = np.full(n, np.inf)
    r, c, inside = grid.rowcol(s.x, s.y)
    onw = inside & area[r, c]
    W[onw] = size[r[onw], c[onw]]
    dmin[onw] = 0.0

    bands = sorted(float(b) for b in w["reach_bands_m"])
    rad = int(np.ceil(max(bands[-1], w["line_reach_m"]) / grid.res)) + 1
    near = np.zeros(grid.shape, dtype=np.uint8)
    near[r[inside], c[inside]] = 1
    near = ndimage.maximum_filter1d(near, 2 * rad + 1, axis=0)
    near = ndimage.maximum_filter1d(near, 2 * rad + 1, axis=1).astype(bool)
    shore = area.copy()
    inner = np.zeros_like(area)
    inner[1:-1, 1:-1] = area[:-2, 1:-1] & area[2:, 1:-1] & area[1:-1, :-2] & area[1:-1, 2:]
    shore &= ~inner
    del inner
    shore_cells = np.flatnonzero((shore & near).ravel())
    del shore
    line_cells = np.flatnonzero(((line > 0) & near).ravel())
    del near

    canopy, log_t = obstacle_tables(cfg)
    groups = []
    if len(line_cells):
        groups.append((line_cells, np.full(len(line_cells), w["line_reach_m"]), w["line_reach_m"],
                       w["line_full_m"], 0.0, False))
    if len(shore_cells):
        sr, sc = np.divmod(shore_cells, grid.width)
        reach = np.asarray(reach_of(*grid.centres(sr, sc)), dtype=np.float64)
        lo = 0.0
        for hi in bands:
            sel = (reach > lo) & (reach <= hi) if hi < bands[-1] else reach > lo
            if sel.any():
                groups.append((shore_cells[sel], reach[sel], hi, w["full_min_m"], w["full_frac"], True))
            lo = hi
    ze = z_pt + w["eye_m"]
    q = np.flatnonzero(~onw)
    qxy = np.column_stack([s.x[q], s.y[q]])
    linemask = line > 0
    for cells, reach, upper, full_min, full_frac, is_area in groups:
        cr, cc = np.divmod(cells, grid.width)
        cx, cy = grid.centres(cr, cc)
        dist, idx = cKDTree(np.column_stack([cx, cy])).query(qxy, distance_upper_bound=upper, workers=-1)
        hit = np.flatnonzero(np.isfinite(dist))
        ci = idx[hit]
        dh = dist[hit]
        pi = q[hit]
        dmin[pi] = np.minimum(dmin[pi], dh)
        val = size[cr[ci], cc[ci]].astype(np.float64) * near_falloff(dh, reach[ci], full_min, full_frac)
        if len(pi) == 0:
            continue
        px, py = s.x[pi], s.y[pi]
        ux = (cx[ci] - px) / np.maximum(dh, 1e-9)
        uy = (cy[ci] - py) / np.maximum(dh, 1e-9)
        # The nearest shore may be worth nothing (a pond under 1 ha, or a
        # shore out of its own reach); the point still gets the fan, so a pond
        # beside the road cannot hide the lake beyond it.
        best = np.zeros(len(pi))
        go = np.flatnonzero(val > 0)
        if len(go):
            best[go] = view_score(px[go], py[go], ze[pi[go]], ux[go], uy[go], dh[go], cr[ci[go]], cc[ci[go]],
                                  val[go], reach[ci[go]], is_area, grid, dem, nlcd, canopy, log_t, w, zsurf_of)
        # A fan of sight lines either side of the nearest shore: the driver
        # sees water through whichever gap there is, not only straight across.
        mask = area if is_area else linemask
        for fx, fy in fan_directions(ux, uy, w["fan_deg"]):
            th, hr, hc = first_hit(px, py, fx, fy, upper, grid, mask, w["ray_step_m"])
            ok = np.flatnonzero(np.isfinite(th))
            if len(ok) == 0:
                continue
            hx, hy = grid.centres(hr[ok], hc[ok])
            rch = (np.asarray(reach_of(hx, hy), dtype=np.float64) if is_area
                   else np.full(len(ok), w["line_reach_m"]))
            v = size[hr[ok], hc[ok]].astype(np.float64) * near_falloff(th[ok], rch, full_min, full_frac)
            g2 = np.flatnonzero(v > best[ok])
            if len(g2) == 0:
                continue
            o2 = ok[g2]
            cand = view_score(px[o2], py[o2], ze[pi[o2]], fx[o2], fy[o2], th[o2], hr[o2], hc[o2], v[g2], rch[g2],
                              is_area, grid, dem, nlcd, canopy, log_t, w, zsurf_of)
            best[o2] = np.maximum(best[o2], cand)
        W[pi] = np.maximum(W[pi], best)  # one query per point: pi is unique
    sr_cfg = w.get("shore_road")
    if sr_cfg and len(shore_cells):
        sr, sc = np.divmod(shore_cells, grid.width)
        big = size[sr, sc] >= sr_cfg["min_size"]
        if big.any():
            bx, by = grid.centres(sr[big], sc[big])
            dist, idx = cKDTree(np.column_stack([bx, by])).query(qxy, distance_upper_bound=sr_cfg["zero_m"],
                                                                 workers=-1)
            hit = np.flatnonzero(np.isfinite(dist))
            fade = np.clip((sr_cfg["zero_m"] - dist[hit]) / (sr_cfg["zero_m"] - sr_cfg["full_m"]), 0, 1)
            floor = sr_cfg["frac"] * size[sr[big][idx[hit]], sc[big][idx[hit]]] * fade
            W[q[hit]] = np.maximum(W[q[hit]], floor)
    return W, dmin


def hills_edges(s: Samples, relief_pt: np.ndarray, z_pt: np.ndarray, length_m: np.ndarray,
                cfg: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(H, H_relief, H_grade) per edge."""
    h = cfg["hills"]
    h_relief = s.mean(np.clip(relief_pt / h["relief_scale_m"], 0, 1))
    z = z_pt.astype(np.float64)
    zm = z.copy()
    k = np.arange(len(z)) - np.repeat(s.off[:-1], s.n)
    interior = (k > 0) & (k < np.repeat(s.n - 1, s.n))
    ii = np.flatnonzero(interior)
    zm[ii] = (z[ii - 1] + z[ii] + z[ii + 1]) / 3.0
    same = s.edge[1:] == s.edge[:-1]
    dz = np.where(same, np.abs(np.diff(zm)), 0.0)
    rise = np.bincount(s.edge[:-1], weights=dz, minlength=len(s.n))
    with np.errstate(invalid="ignore", divide="ignore"):
        grade = np.where(length_m > 0, rise / length_m, 0.0)
    h_grade = np.clip((grade - h["grade_offset"]) / h["grade_scale"], 0, 1)
    enough = s.n >= cfg["sampling"]["min_points_shape"]
    H = np.where(enough, h["w_relief"] * h_relief + h["w_grade"] * h_grade, h_relief)
    return H, h_relief, h_grade


def prospect_points(z_pt: np.ndarray, min_far: np.ndarray, max_far: np.ndarray, cfg: dict) -> np.ndarray:
    """How much landscape is laid out in front of an open viewpoint: the
    drop to the lowest ground within 3 km plus half the rise to the highest,
    less an offset for ordinary rolling ground, scaled to [0, 1]."""
    v = cfg["views"]
    drop = np.maximum(z_pt - min_far, 0)
    rise = np.maximum(max_far - z_pt, 0)
    return np.clip((drop + v["rise_weight"] * rise - v["prospect_offset_m"]) / v["prospect_scale_m"], 0, 1)


def water_view_points(px, py, z_pt, grid90: Grid, big_idx: np.ndarray, water_z: np.ndarray, dem90: np.ndarray,
                      wv_reach: np.ndarray, cfg: dict) -> np.ndarray:
    """Broad water in view, in [0, 1]: broad water (lake or river over ~1 km
    wide) seen from the road, scored by the angle the driver looks down at it
    (a lake 150 m below at 2 km fills the view, one at road level 2 km off
    is a line on the horizon), fading over the second half of the water's
    view range wv_reach (wider water reads from farther), and 0 when bare
    terrain is in the way. Sight lines go to the nearest broad-water cell
    and along a fan (water.fan_deg) either side; the best one counts."""
    v = cfg["views"]
    px = np.asarray(px, dtype=np.float64)
    py = np.asarray(py, dtype=np.float64)
    out = np.zeros(len(px))
    r, c, inside = grid90.rowcol(px, py)
    ti = big_idx[r, c].astype(np.int64)
    ok = np.flatnonzero(inside & (ti >= 0))
    if len(ok) == 0:
        return out
    tr, tc = np.divmod(ti[ok], grid90.width)
    tx, ty = grid90.centres(tr, tc)
    ox, oy = px[ok], py[ok]
    ze = np.asarray(z_pt, dtype=np.float64)[ok] + cfg["water"]["eye_m"]
    d0 = np.hypot(tx - ox, ty - oy)
    ux = np.where(d0 > 0, (tx - ox) / np.maximum(d0, 1e-9), 1.0)
    uy = np.where(d0 > 0, (ty - oy) / np.maximum(d0, 1e-9), 0.0)

    def score(sel, d, hr, hc):
        R = wv_reach[hr, hc].astype(np.float64)
        zw = water_z[hr, hc].astype(np.float64)
        theta = np.degrees(np.arctan2(ze[sel] - zw, np.maximum(d, grid90.res / 2)))
        a = np.clip(theta / v["wv_angle_full_deg"], 0, 1) * np.clip((R - d) / (0.5 * R), 0, 1)
        a[~np.isfinite(a)] = 0
        go = np.flatnonzero(a > 0)
        if len(go):
            hx, hy = grid90.centres(hr[go], hc[go])
            a[go] *= sight_line(ox[sel][go], oy[sel][go], ze[sel][go], hx, hy, zw[go], grid90, dem90,
                                v["wv_ray_step_m"], cfg["water"]["terrain_tol_m"])
        return a

    best = score(np.arange(len(ok)), d0, tr, tc)
    big = big_idx == np.arange(big_idx.size, dtype=np.int64).reshape(big_idx.shape)
    for fx, fy in fan_directions(ux, uy, cfg["water"]["fan_deg"]):
        th, hr, hc = first_hit(ox, oy, fx, fy, v["wv_max_m"], grid90, big, v["wv_ray_step_m"])
        sel = np.flatnonzero(np.isfinite(th))
        if len(sel):
            best[sel] = np.maximum(best[sel], score(sel, th[sel], hr[sel], hc[sel]))
    out[ok] = best
    return out


def views_points(open_pt: np.ndarray, forest_pt: np.ndarray, prospect: np.ndarray, wv: np.ndarray,
                 cfg: dict) -> np.ndarray:
    """V = open * (base + (1 - base) * view) + forest * forest_base, where
    view = 1 - (1 - w_prospect * prospect) * (1 - water_view). Open flat ground with
    nothing in view scores `base`, an open slope over a valley or lake 1,
    a road inside forest `forest_base`, a built-up street 0."""
    v = cfg["views"]
    # v0.2: prospect (3 km relief) is weighted by w_prospect, 0 by default: it
    # duplicates H and saturates on plateaus (docs/SCORE_V02.md, change 4).
    wp = v.get("w_prospect", 1.0)
    view = 1 - (1 - wp * np.clip(prospect, 0, 1)) * (1 - np.clip(wv, 0, 1))
    return open_pt * (v["base"] + (1 - v["base"]) * view) + forest_pt * v["forest_base"]


def land_points(lscore_pt: np.ndarray, mix_pt: np.ndarray, cfg: dict) -> np.ndarray:
    lc = cfg["landcover"]
    return lc["w_score"] * lscore_pt + lc["w_mix"] * mix_pt


def quiet(highway: np.ndarray, cfg: dict) -> np.ndarray:
    q = cfg["quiet"]
    uniq, inv = np.unique(np.asarray(highway, dtype=object).astype(str), return_inverse=True)
    vals = np.array([q.get(h.removesuffix("_link"), 0.0) for h in uniq])
    return vals[inv]


def turning(off: np.ndarray, x: np.ndarray, y: np.ndarray, min_step: float = 5.0) -> np.ndarray:
    """Absolute heading change (degrees) summed over the vertices of each
    edge kept after dropping those closer than `min_step` to the previous
    kept vertex; 0 for edges with fewer than 3 kept vertices.

    The drop rule is sequential along each edge, so this loops over vertex
    position (vectorised across all edges that are that long), never over edges."""
    n_e = len(off) - 1
    nv = np.diff(off)
    order = np.argsort(-nv, kind="stable")
    nv_sorted = nv[order]
    kept = np.zeros(len(x), bool)
    kept[off[:-1]] = True
    lx = x[off[:-1]].copy()
    ly = y[off[:-1]].copy()
    for j in range(1, int(nv.max()) if n_e else 0):
        cnt = int(np.searchsorted(-nv_sorted, -j, side="left"))  # edges with nv > j
        act = order[:cnt]
        idx = off[act] + j
        ok = np.hypot(x[idx] - lx[act], y[idx] - ly[act]) >= min_step
        a = act[ok]
        kept[idx[ok]] = True
        lx[a] = x[idx[ok]]
        ly[a] = y[idx[ok]]
    edge_of_v = np.repeat(np.arange(n_e), nv)
    kv = np.flatnonzero(kept)
    ke = edge_of_v[kv]
    n_kept = np.bincount(ke, minlength=n_e)
    same = ke[1:] == ke[:-1]
    head = np.degrees(np.arctan2(np.diff(y[kv]), np.diff(x[kv])))
    # Turn between segment m (kv[m] -> kv[m+1]) and m+1, both inside one edge.
    both = same[:-1] & same[1:]
    dh = (np.diff(head) + 180.0) % 360.0 - 180.0
    turn = np.bincount(ke[1:-1][both], weights=np.abs(dh[both]), minlength=n_e)
    return np.where(n_kept >= 3, turn, 0.0)


def curvature(off: np.ndarray, x: np.ndarray, y: np.ndarray, length_m: np.ndarray,
              min_step: float = 5.0, scale: float = 200.0) -> np.ndarray:
    """Per-edge C = clip(turn_deg / length_km / scale, 0, 1) (v0). v0.1 scores
    the window value instead (combine), so one bend on a 30 m edge no longer
    saturates it; this stays as a per-edge diagnostic."""
    turn = turning(off, x, y, min_step)
    with np.errstate(invalid="ignore", divide="ignore"):
        c = np.where(length_m > 0, turn / (length_m / 1000.0) / scale, 0.0)
    return np.clip(c, 0, 1)


def excluded_mask(surface: np.ndarray, name: np.ndarray, cfg: dict) -> np.ndarray:
    surfaces = set(cfg["exclude"]["surfaces"])
    rx = re.compile(cfg["exclude"]["name_regex"], re.IGNORECASE)

    def per_unique(arr, fn):
        a = np.asarray(arr, dtype=object)
        keyed = np.where(a == None, "", a).astype(str)  # noqa: E711
        uniq, inv = np.unique(keyed, return_inverse=True)
        return np.array([fn(v) for v in uniq], dtype=bool)[inv]

    return (per_unique(surface, lambda v: v.strip().lower() in surfaces)
            | per_unique(name, lambda v: bool(rx.search(v))))


# --------------------------------------------------------------------------
# Road keys, smoothing windows


def normalise_ref(ref: str) -> str:
    return ref.split(";")[0].strip().upper().replace(" ", "").replace("-", "")


def normalise_name(name: str) -> str:
    return " ".join(name.lower().split())


def road_keys(ref: np.ndarray, name: np.ndarray, way_id: np.ndarray) -> np.ndarray:
    """int64 code per edge: same code = same road key (normalised ref, else
    normalised name, else the OSM way)."""
    ref = np.asarray(ref, dtype=object)
    name = np.asarray(name, dtype=object)

    def codes(arr, norm):
        keyed = np.where(arr == None, "", arr).astype(str)  # noqa: E711
        uniq, inv = np.unique(keyed, return_inverse=True)
        normed = np.array([norm(v) for v in uniq], dtype=object)
        nu, ninv = np.unique(normed.astype(str), return_inverse=True)
        code = ninv[inv]
        valid = (normed != "")[inv]
        return code, valid, len(nu)

    rc, rv, nr = codes(ref, normalise_ref)
    nc, nv, nn = codes(name, normalise_name)
    _, wc = np.unique(way_id, return_inverse=True)
    return np.where(rv, rc, np.where(nv, nr + nc, nr + nn + wc)).astype(np.int64)


def adjacency(u: np.ndarray, v: np.ndarray, key: np.ndarray, active: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Ordered pairs (i, j), i != j, of active edges that share a node and a key."""
    e = np.flatnonzero(active)
    ee = np.concatenate([e, e])
    node = np.concatenate([u[e], v[e]]).astype(np.int64)
    k = key[ee]
    order = np.lexsort((ee, node, k))
    ee, node, k = ee[order], node[order], k[order]
    newgrp = np.ones(len(ee), bool)
    newgrp[1:] = (node[1:] != node[:-1]) | (k[1:] != k[:-1])
    gid = np.cumsum(newgrp) - 1
    gstart = np.flatnonzero(newgrp)
    gsize = np.diff(np.concatenate([gstart, [len(ee)]]))
    sz = gsize[gid]
    rep = np.repeat(np.arange(len(ee)), sz)
    within = np.arange(len(rep)) - np.repeat(np.concatenate([[0], np.cumsum(sz)[:-1]]), sz)
    partner = gstart[gid[rep]] + within
    src, dst = ee[rep], ee[partner]
    keep = src != dst
    src, dst = src[keep], dst[keep]
    pair = np.unique(src.astype(np.int64) * (len(u) + 1) + dst)
    return (pair // (len(u) + 1)), (pair % (len(u) + 1))


def window_pairs(u, v, key, length_m, active, radius: float = 500.0, max_rounds: int = 8):
    """Min-plus BFS over same-key adjacency. Returns (src, dst, dist) for
    every i != j with along-road distance from the nearer end of i to the
    nearer end of j under `radius` (adjacent edges: 0; each edge passed
    through adds its length). Rounds stop at `max_rounds` or when nothing
    improves."""
    n = len(u)
    a_src, a_dst = adjacency(u, v, key, active)
    order = np.argsort(a_src, kind="stable")
    a_src, a_dst = a_src[order], a_dst[order]
    indptr = np.concatenate([[0], np.cumsum(np.bincount(a_src, minlength=n))])
    L = np.asarray(length_m, dtype=np.float64)
    M = np.int64(n + 1)

    best_key = a_src * M + a_dst
    best_d = np.zeros(len(best_key))
    f_src, f_dst, f_d = a_src, a_dst, best_d.copy()
    rounds = 0
    for _ in range(max_rounds):
        if len(f_src) == 0:
            break
        rounds += 1
        deg = indptr[f_dst + 1] - indptr[f_dst]
        rep = np.repeat(np.arange(len(f_src)), deg)
        pos = np.arange(len(rep)) - np.repeat(np.concatenate([[0], np.cumsum(deg)[:-1]]), deg)
        k_dst = a_dst[indptr[f_dst][rep] + pos]
        c_src = f_src[rep]
        c_d = f_d[rep] + L[f_dst[rep]]
        ok = (c_src != k_dst) & (c_d < radius)
        c_key = c_src[ok] * M + k_dst[ok]
        c_d = c_d[ok]
        allk = np.concatenate([best_key, c_key])
        alld = np.concatenate([best_d, c_d])
        isnew = np.concatenate([np.zeros(len(best_key), bool), np.ones(len(c_key), bool)])
        o = np.lexsort((isnew, alld, allk))  # old entries win ties
        allk, alld, isnew = allk[o], alld[o], isnew[o]
        first = np.ones(len(allk), bool)
        first[1:] = allk[1:] != allk[:-1]
        best_key, best_d, improved = allk[first], alld[first], isnew[first]
        f_src = best_key[improved] // M
        f_dst = best_key[improved] % M
        f_d = best_d[improved]
    return best_key // M, best_key % M, best_d, rounds


def window_weights(src, dst, dist, length_m, active, radius: float = 500.0):
    """Pairs including i itself: (src, dst, w) with w_ij = min(len_j,
    radius - d_ij) and w_ii = len_i. Lengths are floored at 0.1 m."""
    L = np.maximum(np.asarray(length_m, dtype=np.float64), 0.1)
    e = np.flatnonzero(active)
    s = np.concatenate([e, src])
    d = np.concatenate([e, dst])
    w = np.concatenate([L[e], np.minimum(L[dst], radius - dist)])
    return s, d, w


def window_sum(s, d, w, values, n) -> np.ndarray:
    return np.bincount(s, weights=w * np.asarray(values, dtype=np.float64)[d], minlength=n)


def percentile_rank(values: np.ndarray, length: np.ndarray, active: np.ndarray) -> np.ndarray:
    """S_i = (length of active edges ranked below i + 0.5 * length_i) / total
    active length; ties share the mean of their group. 0 for inactive edges."""
    S = np.zeros(len(values))
    e = np.flatnonzero(active)
    if len(e) == 0:
        return S
    x = np.asarray(values, dtype=np.float64)[e]
    L = np.asarray(length, dtype=np.float64)[e]
    order = np.argsort(x, kind="stable")
    xs, Ls = x[order], L[order]
    total = Ls.sum()
    newgrp = np.ones(len(xs), bool)
    newgrp[1:] = xs[1:] != xs[:-1]
    gid = np.cumsum(newgrp) - 1
    glen = np.bincount(gid, weights=Ls)
    gbefore = np.concatenate([[0.0], np.cumsum(glen)[:-1]])
    r = (gbefore + 0.5 * glen)[gid] / total
    S[e[order]] = r
    return S


def window_top(s, d, w, values, n, top_m: float) -> np.ndarray:
    """Per window (pairs s -> d with weights w): the w-weighted mean of
    `values` over the best `top_m` metres of window weight (pairs sorted by
    value, descending; the last one taken in part). A window lighter than
    `top_m` gives its maximum."""
    x = np.asarray(values, dtype=np.float64)[d]
    order = np.lexsort((-x, s))
    so, xo, wo = s[order], x[order], w[order]
    start = np.ones(len(so), bool)
    start[1:] = so[1:] != so[:-1]
    cw = np.cumsum(wo)
    first = np.flatnonzero(start)
    base = np.repeat(cw[first] - wo[first], np.diff(np.append(first, len(so))))
    before = cw - wo - base                      # window weight above this pair
    take = np.clip(top_m - before, 0.0, wo)
    num = np.bincount(so, weights=take * xo, minlength=n)
    den = np.bincount(so, weights=take, minlength=n)
    wsum = np.bincount(so, weights=wo, minlength=n)
    mx = np.full(n, np.nan)
    mx[so[first]] = xo[first]
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(wsum >= top_m, num / den, mx)


def combine(sig: dict, edges_extra: dict, active: np.ndarray, cfg: dict, pairs=None) -> dict:
    """Penalties, pre, smoothing and rank. `sig` holds per-edge W, H, V, L,
    Q, turn_deg (or a ready C), dev, signals (count per edge), junction_halves; `edges_extra` holds
    u, v, length_m, highway, maxspeed_tagged, speed_mph, key."""
    cb, pen, sm = cfg["combine"], cfg["penalty"], cfg["smoothing"]
    n = len(active)
    L = np.asarray(edges_extra["length_m"], dtype=np.float64)
    raw = cb["w_water"] * sig["W"] + cb["w_hills"] * sig["H"] + cb["w_views"] * sig["V"] + cb["w_land"] * sig["L"]

    if pairs is None:
        pairs = window_pairs(edges_extra["u"], edges_extra["v"], edges_extra["key"], L, active,
                             sm["radius_m"], sm["max_rounds"])
    src, dst, dist, rounds = pairs
    s, d, w = window_weights(src, dst, dist, L, active, sm["radius_m"])
    Lw = np.maximum(L, 0.1)
    wsum = np.bincount(s, weights=w, minlength=n)
    with np.errstate(invalid="ignore", divide="ignore"):
        junc_km = window_sum(s, d, w, sig["junction_halves"] / Lw, n) / (wsum / 1000.0)
        sig_km = window_sum(s, d, w, sig["signals"] / Lw, n) / (wsum / 1000.0)
    junc_km = np.nan_to_num(junc_km)
    sig_km = np.nan_to_num(sig_km)
    # v0.1: curvature over the window (degrees per km of road around the edge).
    if "turn_deg" in sig:
        with np.errstate(invalid="ignore", divide="ignore"):
            turn_km = window_sum(s, d, w, sig["turn_deg"] / Lw, n) / (wsum / 1000.0)
        C = np.clip(np.nan_to_num(turn_km) / cfg["curves"]["scale_deg_per_km"], 0, 1)
    else:
        C = np.asarray(sig["C"], dtype=np.float64)
    hw = np.asarray(edges_extra["highway"], dtype=object).astype(str)
    town = np.clip((sig["dev"] - pen["town_lo"]) / pen["town_span"], 0, 1)
    if pen.get("town_water_gate", False):
        # v0.2: with water in view the town is the lake's foreground.
        town = town * (1 - np.clip(np.maximum(sig["W"], sig["V_water"]), 0, 1))
    p = (pen["junction_weight"] * np.clip((junc_km - pen["junction_lo_per_km"]) / pen["junction_span_per_km"], 0, 1)
         + pen["signal_weight"] * np.clip(sig_km / pen["signal_full_per_km"], 0, 1)
         + pen["town_weight"] * town
         + pen["urban_speed_weight"] * (np.asarray(edges_extra["maxspeed_tagged"], bool)
                                        & (np.asarray(edges_extra["speed_mph"]) <= pen["urban_speed_mph"]))
         + pen["motorway_weight"] * np.isin(hw, ["motorway", "motorway_link"]))
    P = np.minimum(p, pen["cap"])
    bonus = 1 + cb["bonus_quiet"] * sig["Q"] + cb["bonus_curves"] * C
    if pen.get("mode", "subtract") == "multiply":
        pre = raw * bonus * (1 - P)              # v0.2: a penalty cannot erase a view
    else:
        pre = raw * bonus - P
    pre = np.where(active, pre, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = window_sum(s, d, w, np.nan_to_num(pre), n) / wsum
    pw = sm.get("pool_weight", 0.0)
    if pw > 0:
        # v0.2: a driver remembers the best of the last kilometre, not its average.
        top = window_top(s, d, w, np.nan_to_num(pre), n, sm["pool_top_m"])
        smooth = (1 - pw) * mean + pw * top
    else:
        smooth = mean
    smooth = np.where(active, smooth, np.nan)
    S = percentile_rank(smooth, L, active)
    return {"raw": raw, "penalty": P, "pre": pre, "smooth": smooth, "S": S, "C": C,
            "junctions_per_km": junc_km, "signals_per_km": sig_km, "window_m": wsum,
            "window_pairs": int(len(src)), "bfs_rounds": int(rounds)}


# --------------------------------------------------------------------------
# Output table and loader


def scores_table(edges_cols: dict, res: dict):
    import pyarrow as pa

    n = len(edges_cols["u"])
    f32 = lambda a: pa.array(np.asarray(a, dtype=np.float32))  # noqa: E731
    wd = np.asarray(res["water_dist_m"], dtype=np.float64)
    cols = {
        "edge_idx": pa.array(np.arange(n, dtype=np.int32)),
        "u": pa.array(edges_cols["u"]), "v": pa.array(edges_cols["v"]), "way_id": pa.array(edges_cols["way_id"]),
        **{c: f32(res[c]) for c in SCORE_COLUMNS},
        "penalty": f32(res["penalty"]),
        "raw": f32(np.where(res["excluded"], np.nan, res["raw"])),
        "pre": f32(res["pre"]), "smooth": f32(res["smooth"]),
        "S": f32(np.where(res["excluded"], 0.0, res["S"])),
        "excluded": pa.array(np.asarray(res["excluded"], dtype=bool)),
        "n_samples": pa.array(np.minimum(res["n_samples"], 32767).astype(np.int16)),
        "water_dist_m": f32(np.where(np.isfinite(wd), wd, 9999.0)),
    }
    return pa.table(cols)


def check_alignment(scores, edges) -> None:
    """Raise unless `scores` has one row per edges row with identical u, v, way_id."""
    if scores.num_rows != edges.num_rows:
        raise ValueError(f"scores has {scores.num_rows} rows, edges {edges.num_rows}")
    for c in ("u", "v", "way_id"):
        if not np.array_equal(scores.column(c).to_numpy(), edges.column(c).to_numpy()):
            raise ValueError(f"scores column {c} differs from edges.parquet")


def write_scores(table, edges_path: Path, out_path: Path) -> Path:
    import pyarrow.parquet as pq

    edges = pq.read_table(edges_path, columns=["u", "v", "way_id"])
    check_alignment(table, edges)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".part")
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(out_path)
    return out_path


def write_edge_signals(cols: dict, path: Path, fingerprint: str) -> Path:
    """edge_signals.parquet with the fingerprint of the edges it was computed
    for (graph.edges_fingerprint) in its metadata."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from .graph import FINGERPRINT_KEY

    t = pa.table({k: pa.array(v) for k, v in cols.items()})
    t = t.replace_schema_metadata({FINGERPRINT_KEY: fingerprint.encode()})
    pq.write_table(t, path, compression="zstd")
    return path


def read_edge_signals(path: Path, fingerprint: str):
    """edge_signals.parquet, after checking it was computed for the edges with
    this fingerprint (same rows in the same order). Raises ValueError if not."""
    import pyarrow.parquet as pq

    from .graph import FINGERPRINT_KEY

    stored = (pq.read_schema(path).metadata or {}).get(FINGERPRINT_KEY)
    if stored is None or stored.decode() != fingerprint:
        raise ValueError(f"{path} was not computed for the current edges.parquet; rerun the edges stage")
    return pq.read_table(path)


def save_samples(path: Path, s: "Samples", fingerprint: str) -> None:
    np.savez(path, x=s.x.astype(np.float32), y=s.y.astype(np.float32), edge=s.edge,
             edges_fingerprint=np.array(fingerprint))


def load_samples(path: Path, fingerprint: str) -> dict:
    """samples.npz (x, y, edge), after the same check as read_edge_signals."""
    z = np.load(path)
    if "edges_fingerprint" not in z.files or str(z["edges_fingerprint"]) != fingerprint:
        raise ValueError(f"{path} was not computed for the current edges.parquet; rerun the edges stage")
    return {k: z[k] for k in ("x", "y", "edge")}


def load(edges_path: Path | None = None, scores_path: Path | None = None):
    """scores.parquet as a pyarrow table, after checking it lines up row for
    row with `edges_path` (default data/graph/edges.parquet)."""
    import pyarrow.parquet as pq

    edges_path = Path(edges_path or data_dir() / "graph" / "edges.parquet")
    scores_path = Path(scores_path or data_dir() / "score" / "scores.parquet")
    t = pq.read_table(scores_path)
    check_alignment(t, pq.read_table(edges_path, columns=["u", "v", "way_id"]))
    return t


# --------------------------------------------------------------------------
# Stages

from .score_stages import run  # noqa: E402,F401  (CLI entry point)
