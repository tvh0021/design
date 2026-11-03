"""Scenery score v0 (docs/SCORE_SPEC.md): one score S in [0, 1] per edge of
edges.parquet.

Stages (each writes its product to disk and is skipped on rerun while the
product is newer than its inputs; `--force` reruns them):

  grid       working 30 m grid in EPSG:5070            rasters/grid.json
  dem        3DEP mosaic warped to the grid             rasters/dem30.tif, dem90.tif
  nlcd       NLCD window                                rasters/nlcd30.tif
  water      OSM + NLCD water masks and size score      rasters/water_area.tif, water_line.tif, water_size.tif
  terrain90  relief and TPI at 90 m                     rasters/relief90.tif, tpi90.tif
  land90     land cover score, mix, open, developed     rasters/lscore90.tif, mix90.tif, open90.tif, dev90.tif
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

from . import data_dir
from .rasters import Grid

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
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


def water_points(s: Samples, grid: Grid, water_mask: np.ndarray, size_of_cells, blocking: np.ndarray,
                 cfg: dict) -> tuple[np.ndarray, np.ndarray]:
    """Per-point W and nearest-water distance (inf when none within range).

    water_mask: bool 30 m mask of water cells (areal or linear).
    size_of_cells(flat_idx) -> size score of those cells.
    blocking: bool 30 m mask of cells that block the view of water."""
    from scipy import ndimage
    from scipy.spatial import cKDTree

    w = cfg["water"]
    r, c, inside = grid.rowcol(s.x, s.y)
    near = np.zeros(grid.shape, dtype=np.uint8)
    near[r[inside], c[inside]] = 1
    size = 2 * int(w["road_dilate_cells"]) + 1
    near = ndimage.maximum_filter1d(near, size, axis=0)
    near = ndimage.maximum_filter1d(near, size, axis=1)
    cells = np.flatnonzero(water_mask.ravel() & near.ravel().astype(bool))
    del near
    d = np.full(len(s.x), np.inf)
    W = np.zeros(len(s.x))
    if len(cells) == 0:
        return W, d
    cr, cc = np.divmod(cells, grid.width)
    cx, cy = grid.centres(cr, cc)
    tree = cKDTree(np.column_stack([cx, cy]))
    dist, idx = tree.query(np.column_stack([s.x, s.y]), distance_upper_bound=w["max_dist_m"], workers=-1)
    hit = np.flatnonzero(np.isfinite(dist))
    d[hit] = dist[hit]
    ci = idx[hit]
    sz = np.asarray(size_of_cells(cells[ci]), dtype=np.float64)
    f = np.clip((w["max_dist_m"] - dist[hit]) / w["ramp_m"], 0, 1)
    open_n = np.zeros(len(hit))
    px, py = s.x[hit], s.y[hit]
    for fr in w["vis_fracs"]:
        qx = px + fr * (cx[ci] - px)
        qy = py + fr * (cy[ci] - py)
        qr, qc, qin = grid.rowcol(qx, qy)
        open_n += ~(blocking[qr, qc] & qin)
    vis = np.where(dist[hit] < w["vis_min_dist_m"], 1.0, open_n / len(w["vis_fracs"]))
    W[hit] = sz * f * vis
    return W, d


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


def views_points(open_pt: np.ndarray, tpi_pt: np.ndarray, cfg: dict) -> np.ndarray:
    v = cfg["views"]
    return open_pt * (v["base"] + v["tpi_weight"] * np.clip(tpi_pt / v["tpi_scale_m"], 0, 1))


def land_points(lscore_pt: np.ndarray, mix_pt: np.ndarray, cfg: dict) -> np.ndarray:
    lc = cfg["landcover"]
    return lc["w_score"] * lscore_pt + lc["w_mix"] * mix_pt


def quiet(highway: np.ndarray, cfg: dict) -> np.ndarray:
    q = cfg["quiet"]
    uniq, inv = np.unique(np.asarray(highway, dtype=object).astype(str), return_inverse=True)
    vals = np.array([q.get(h.removesuffix("_link"), 0.0) for h in uniq])
    return vals[inv]


def curvature(off: np.ndarray, x: np.ndarray, y: np.ndarray, length_m: np.ndarray,
              min_step: float = 5.0, scale: float = 200.0) -> np.ndarray:
    """C = clip(turn_deg / length_km / scale, 0, 1), turning summed over the
    vertices kept after dropping those closer than `min_step` to the previous
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
    with np.errstate(invalid="ignore", divide="ignore"):
        c = np.where(length_m > 0, turn / (length_m / 1000.0) / scale, 0.0)
    return np.where(n_kept >= 3, np.clip(c, 0, 1), 0.0)


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


def combine(sig: dict, edges_extra: dict, active: np.ndarray, cfg: dict, pairs=None) -> dict:
    """Penalties, pre, smoothing and rank. `sig` holds per-edge W, H, V, L,
    Q, C, dev, signals (count per edge), junction_halves; `edges_extra` holds
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
    hw = np.asarray(edges_extra["highway"], dtype=object).astype(str)
    p = (pen["junction_weight"] * np.clip((junc_km - pen["junction_lo_per_km"]) / pen["junction_span_per_km"], 0, 1)
         + pen["signal_weight"] * np.clip(sig_km / pen["signal_full_per_km"], 0, 1)
         + pen["town_weight"] * np.clip((sig["dev"] - pen["town_lo"]) / pen["town_span"], 0, 1)
         + pen["urban_speed_weight"] * (np.asarray(edges_extra["maxspeed_tagged"], bool)
                                        & (np.asarray(edges_extra["speed_mph"]) <= pen["urban_speed_mph"]))
         + pen["motorway_weight"] * np.isin(hw, ["motorway", "motorway_link"]))
    P = np.minimum(p, pen["cap"])
    pre = raw * (1 + cb["bonus_quiet"] * sig["Q"] + cb["bonus_curves"] * sig["C"]) - P
    pre = np.where(active, pre, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        smooth = window_sum(s, d, w, np.nan_to_num(pre), n) / wsum
    smooth = np.where(active, smooth, np.nan)
    S = percentile_rank(smooth, L, active)
    return {"raw": raw, "penalty": P, "pre": pre, "smooth": smooth, "S": S,
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
