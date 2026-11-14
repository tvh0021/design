"""Mode 2 drive-around loop search (docs/LOOP_SPEC.md).

    ctx = Context.load()
    res = loops(ctx, (43.1566, -77.6088), 120, Steer(mode="compass", direction="N"))
    res.picks[0].route.edges
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

from .graph import EARTH_RADIUS_M, haversine_m
from .route import RoadGraph, Route
from .search import (
    Context,
    CostGraph,
    ScenicRoute,
    _densify,
    _INK,
    _INK2,
    _MUTED,
    _PICK_COLOURS,
    _SURFACE,
    _to5070,
    describe,
    edge_arcs,
    main_roads,
    route_overlap,
    score_route,
)

DIRECTIONS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
DIRECTION_WORDS = {
    "N": "North",
    "NE": "Northeast",
    "E": "East",
    "SE": "Southeast",
    "S": "South",
    "SW": "Southwest",
    "W": "West",
    "NW": "Northwest",
}
MAX_SNAP_M = 2000.0

DEFAULTS: dict = {
    "budgets_min": [60, 120, 180],
    "default_min": 120,
    "min_minutes": 30,
    "max_minutes": 360,
    "min_time_frac": 0.75,
    "max_time_frac": 1.05,
    "n_loops": 3,
    "lambdas": [3.0, 1.0, 0.0],
    "reuse_radius_m": 120.0,
    "reuse_penalty": 4.0,
    "uturn_ban_cost": 1e12,
    "s_base": 0.50,
    "seed_cell_m": 4000.0,
    "seed_min_S": 0.6,
    "seed_min_length_m": 150.0,
    "seed_full_m": 20000.0,
    "seed_value": 0.25,
    "any_sector_deg": 30.0,
    "any_per_sector": 2,
    "first_rt_frac": [0.30, 0.90],
    "partner_tight": [0.65, 0.92],
    "partner_wide": [0.60, 0.95],
    "partner_tight_k": 2,
    "partner_k": 4,
    "partner_sep_deg": [25.0, 150.0],
    "partner_min_gain": 1.15,
    "cone_deg": 30.0,
    "depth_quantile": 0.9,
    "depth_frac": 0.5,
    "apex_k": 8,
    "anchor_gap_m": 3000.0,
    "partner_half_plane_deg": 90.0,
    "straddle_offset_deg": [15.0, 90.0],
    "straddle_left_k": 4,
    "straddle_right_k": 3,
    "straddle_max_skew_deg": 60.0,
    "compass_tol_deg": 67.5,
    "compass_tol_fallback_deg": 90.0,
    "compass_full_weight_deg": 22.5,
    "compass_weight_span_deg": 45.0,
    "compass_min_weight": 0.5,
    "anchor_snap_m": 2000.0,
    "shore_snap_km": 45.0,
    "shore_targets": 3,
    "feature_targets": 4,
    "feature_target_gap_m": 5000.0,
    "target_max_rt_frac": 0.90,
    "anchor_reach_frac": 0.95,
    "lens_rt_frac": [0.45, 0.95],
    "anchor_partner_k": 4,
    "anchor_partner_sector_deg": 45.0,
    "anchor_partner_window": [0.65, 0.92],
    "sample_step_m": 75.0,
    "doubled_radius_m": 150.0,
    "doubled_min_sep_m": 1500.0,
    "max_net_doubled_frac": 0.10,
    "stem_start_m": 1500.0,
    "stem_gap_m": 300.0,
    "stem_frac_tier1": 0.12,
    "stem_frac_tier2": 0.40,
    "max_overlap": 0.5,
    "max_overlap_anchor": 0.6,
    "max_overlap_relaxed": 0.7,
    "min_bearing_sep_deg": [60.0, 30.0, 0.0],
    "dijkstra_work_arcs": 5.0e7,
    "dijkstra_count_range": [80, 600],
}


class LoopError(Exception):
    def __init__(self, code: str, message: str, **numbers):
        super().__init__(message)
        self.code = code
        self.message = message
        self.numbers = numbers


@dataclass(frozen=True)
class Steer:
    mode: str = "any"  # "any" | "compass" | "anchor"
    direction: str | None = None
    point: tuple[float, float] | None = None
    feature: str | None = None


@dataclass
class Anchor:
    kind: str  # "feature" | "point" | "shore"
    name: str | None
    point: tuple[float, float]
    requested: tuple[float, float] | None
    moved_m: float
    node: int | None
    moments: np.ndarray
    round_trip_s: float


@dataclass
class LoopRoute(ScenicRoute):
    bearing: float | None = None
    direction: str | None = None
    off_deg: float | None = None
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
    s_fast: float
    picks: list[LoopRoute]
    kept: int
    built: int
    dijkstras: int
    region_arcs: int
    seconds: dict
    note: str | None


def loop_config(cfg: dict | None = None) -> dict:
    from .score import load_config

    out = dict(DEFAULTS)
    try:
        file_cfg = load_config().get("loop", {})
        out.update(file_cfg)
    except Exception:
        pass
    if cfg:
        out.update(cfg)
    return out


# --------------------------------------------------------------------------
# True-bearing tangent plane geometry (section 2)


def _tangent_xy(lon, lat, lon0: float, lat0: float) -> tuple[np.ndarray, np.ndarray]:
    lon_a = np.asarray(lon, dtype=np.float64)
    lat_a = np.asarray(lat, dtype=np.float64)
    east = np.radians(lon_a - lon0) * math.cos(math.radians(lat0)) * EARTH_RADIUS_M
    north = np.radians(lat_a - lat0) * EARTH_RADIUS_M
    return east, north


def _bearing_from_en(east, north):
    return np.degrees(np.arctan2(east, north)) % 360.0


def angdiff(a, b):
    return np.abs((np.asarray(a) - np.asarray(b) + 180.0) % 360.0 - 180.0)


def sdiff(a, t):
    return (np.asarray(a) - np.asarray(t) + 180.0) % 360.0 - 180.0


def direction_word(bearing: float) -> str:
    idx = int(((float(bearing) + 22.5) % 360.0) // 45.0)
    return DIRECTIONS[idx]


def direction_bearing(direction: str) -> float:
    d = str(direction).strip().upper()
    if d not in DIRECTIONS:
        raise ValueError(f"unknown direction {direction!r}; expected one of {', '.join(DIRECTIONS)}")
    return 45.0 * DIRECTIONS.index(d)


def compass_weight(off_deg: float, cfg: dict) -> float:
    full_deg = float(cfg["compass_full_weight_deg"])
    span_deg = float(cfg["compass_weight_span_deg"])
    min_w = float(cfg["compass_min_weight"])
    t = float(np.clip((float(off_deg) - full_deg) / max(span_deg, 1e-9), 0.0, 1.0))
    return 1.0 - (1.0 - min_w) * t


def _norm_name(s: str | None) -> str:
    if not s:
        return ""
    return " ".join(str(s).split()).casefold()


def _rev_time_matrix(g: RoadGraph) -> csr_matrix:
    return g.rev_time_matrix()


# --------------------------------------------------------------------------
# Reach and anchor resolution (sections 4.2, 4.5.3, 5)


@dataclass
class Reach:
    node: int
    snap_m: float
    lat0: float
    lon0: float
    budget_s: float
    dH: np.ndarray
    dR: np.ndarray
    region: np.ndarray
    arcs: np.ndarray
    fa: np.ndarray
    ra: np.ndarray
    _wide: tuple[np.ndarray, np.ndarray] | None = None

    def wide_dists(self, g: RoadGraph, cfg: dict) -> tuple[np.ndarray, np.ndarray]:
        if self._wide is None:
            lim = float(cfg["max_time_frac"]) * 60.0 * float(cfg["max_minutes"])
            m, _, _ = g._matrix(g.time_s)
            mt = _rev_time_matrix(g)
            dH_w = dijkstra(m, directed=True, indices=self.node, limit=lim)
            dR_w = dijkstra(mt, directed=True, indices=self.node, limit=lim)
            self._wide = (dH_w, dR_w)
        return self._wide


def compute_reach(g: RoadGraph, start: tuple[float, float], budget_s: float, cfg: dict) -> Reach:
    node, snap_m = g.snap(float(start[0]), float(start[1]))
    lim = float(cfg["max_time_frac"]) * budget_s
    m, _, _ = g._matrix(g.time_s)
    mt = _rev_time_matrix(g)
    dH = dijkstra(m, directed=True, indices=node, limit=lim)
    dR = dijkstra(mt, directed=True, indices=node, limit=lim)
    region = (dH + dR) <= lim * (1.0 + 1e-9) + 1e-6
    arcs = np.flatnonzero(region[g.arc_tail] & region[g.arc_head])
    fa, ra = edge_arcs(g)
    return Reach(
        node=node,
        snap_m=snap_m,
        lat0=float(g.node_lat[node]),
        lon0=float(g.node_lon[node]),
        budget_s=budget_s,
        dH=dH,
        dR=dR,
        region=region,
        arcs=arcs,
        fa=fa,
        ra=ra,
    )


def _moment_rts(g: RoadGraph, mom, m_ids: np.ndarray, dH: np.ndarray, dR: np.ndarray,
                fa: np.ndarray, ra: np.ndarray) -> np.ndarray:
    out = np.full(len(m_ids), np.inf, dtype=np.float64)
    t_arc = g.time_s[g.arc_edge]
    for idx, mid in enumerate(m_ids):
        e = int(mom.edge[int(mid)])
        best = np.inf
        for a in (int(fa[e]), int(ra[e])):
            if a >= 0:
                rt = float(dH[g.arc_tail[a]] + t_arc[a] + dR[g.arc_head[a]])
                if rt < best:
                    best = rt
        out[idx] = best
    return out


def _needed_minutes(rt_s: float, cfg: dict) -> int:
    raw = 5 * int(math.ceil(rt_s / float(cfg["target_max_rt_frac"]) / 300.0))
    return max(int(cfg["min_minutes"]), min(int(cfg["max_minutes"]), raw))


def resolve_anchor(ctx: Context, reach: Reach, steer: Steer, cfg: dict | None = None) -> Anchor:
    cfg = loop_config(cfg)
    g, mom = ctx.g, ctx.moments
    B = reach.budget_s
    has_feat = bool(steer.feature and str(steer.feature).strip())
    has_pt = steer.point is not None
    if has_feat == has_pt:
        raise ValueError("anchor steer requires exactly one of feature or point")

    feat_arr = mom.feature if mom.feature is not None else np.full(len(mom.id), None, dtype=object)
    body_arr = mom.body if mom.body is not None else feat_arr

    if has_feat:
        q_norm = _norm_name(steer.feature)
        matched = []
        canon_name = None
        for i in range(len(mom.id)):
            f_val = feat_arr[i]
            b_val = body_arr[i]
            if _norm_name(f_val) == q_norm:
                matched.append(int(mom.id[i]))
                if canon_name is None:
                    canon_name = str(f_val)
            elif _norm_name(b_val) == q_norm:
                matched.append(int(mom.id[i]))
                if canon_name is None:
                    canon_name = str(b_val)
        if not matched:
            raise LoopError(
                "unknown_feature",
                f"No scenic feature named '{steer.feature}' in the data. Pick one from the list or drop a pin.",
            )
        m_ids = np.asarray(matched, dtype=np.int64)
        rts = _moment_rts(g, mom, m_ids, reach.dH, reach.dR, reach.fa, reach.ra)
        if not np.any(np.isfinite(rts)):
            dH_w, dR_w = reach.wide_dists(g, cfg)
            rts = _moment_rts(g, mom, m_ids, dH_w, dR_w, reach.fa, reach.ra)
        if not np.any(np.isfinite(rts)):
            m_full, _, _ = g._matrix(g.time_s)
            mt_full = _rev_time_matrix(g)
            dH_u = dijkstra(m_full, directed=True, indices=reach.node)
            dR_u = dijkstra(mt_full, directed=True, indices=reach.node)
            rts = _moment_rts(g, mom, m_ids, dH_u, dR_u, reach.fa, reach.ra)
        best_idx = int(np.argmin(rts))
        rt_min = float(rts[best_idx])
        if not np.isfinite(rt_min) or rt_min > float(cfg["anchor_reach_frac"]) * B:
            rt_m = int(math.ceil(rt_min / 60.0)) if np.isfinite(rt_min) else int(cfg["max_minutes"]) + 1
            need_m = _needed_minutes(rt_min, cfg) if np.isfinite(rt_min) else int(cfg["max_minutes"])
            raise LoopError(
                "anchor_out_of_reach",
                f"That place is about {rt_m} min there and back on the fastest roads. Choose {need_m} min or more.",
                round_trip_min=rt_m,
                needed_min=need_m,
            )
        best_mid = int(m_ids[best_idx])
        return Anchor(
            kind="feature",
            name=canon_name,
            point=(float(mom.lat[best_mid]), float(mom.lon[best_mid])),
            requested=None,
            moved_m=0.0,
            node=None,
            moments=m_ids,
            round_trip_s=rt_min,
        )

    req_lat, req_lon = float(steer.point[0]), float(steer.point[1])
    snapped, snap_m = g.snap(req_lat, req_lon)
    if snap_m <= float(cfg["anchor_snap_m"]):
        rt_min = float(reach.dH[snapped] + reach.dR[snapped])
        if not np.isfinite(rt_min):
            dH_w, dR_w = reach.wide_dists(g, cfg)
            rt_min = float(dH_w[snapped] + dR_w[snapped])
        if not np.isfinite(rt_min):
            m_full, _, _ = g._matrix(g.time_s)
            mt_full = _rev_time_matrix(g)
            rt_min = float(
                dijkstra(m_full, directed=True, indices=reach.node)[snapped]
                + dijkstra(mt_full, directed=True, indices=reach.node)[snapped]
            )
        if not np.isfinite(rt_min) or rt_min > float(cfg["anchor_reach_frac"]) * B:
            rt_m = int(math.ceil(rt_min / 60.0)) if np.isfinite(rt_min) else int(cfg["max_minutes"]) + 1
            need_m = _needed_minutes(rt_min, cfg) if np.isfinite(rt_min) else int(cfg["max_minutes"])
            raise LoopError(
                "anchor_out_of_reach",
                f"That place is about {rt_m} min there and back on the fastest roads. Choose {need_m} min or more.",
                round_trip_min=rt_m,
                needed_min=need_m,
            )
        pt = (float(g.node_lat[snapped]), float(g.node_lon[snapped]))
        return Anchor(
            kind="point",
            name=None,
            point=pt,
            requested=(req_lat, req_lon),
            moved_m=float(snap_m),
            node=int(snapped),
            moments=np.zeros(0, dtype=np.int64),
            round_trip_s=rt_min,
        )

    # Shore fallback: nearest water moment within shore_snap_km
    water_mask = np.asarray([str(t) == "water" for t in mom.type], dtype=bool)
    water_ids = mom.id[water_mask]
    if len(water_ids):
        dists_m = np.asarray(
            haversine_m(req_lon, req_lat, mom.lon[water_ids], mom.lat[water_ids]),
            dtype=np.float64,
        )
        j = int(np.argmin(dists_m))
        if dists_m[j] <= float(cfg["shore_snap_km"]) * 1000.0:
            near_id = int(water_ids[j])
            b_name = body_arr[near_id]
            if b_name:
                b_norm = _norm_name(b_name)
                m_ids = np.asarray(
                    [int(mom.id[i]) for i in range(len(mom.id)) if _norm_name(body_arr[i]) == b_norm],
                    dtype=np.int64,
                )
                shore_name = str(b_name)
            else:
                m_ids = np.asarray([near_id], dtype=np.int64)
                shore_name = str(mom.label[near_id]) if mom.label[near_id] else None
            rts = _moment_rts(g, mom, m_ids, reach.dH, reach.dR, reach.fa, reach.ra)
            if not np.any(np.isfinite(rts)):
                dH_w, dR_w = reach.wide_dists(g, cfg)
                rts = _moment_rts(g, mom, m_ids, dH_w, dR_w, reach.fa, reach.ra)
            if not np.any(np.isfinite(rts)):
                m_full, _, _ = g._matrix(g.time_s)
                mt_full = _rev_time_matrix(g)
                dH_u = dijkstra(m_full, directed=True, indices=reach.node)
                dR_u = dijkstra(mt_full, directed=True, indices=reach.node)
                rts = _moment_rts(g, mom, m_ids, dH_u, dR_u, reach.fa, reach.ra)
            rt_min = float(np.min(rts))
            if not np.isfinite(rt_min) or rt_min > float(cfg["anchor_reach_frac"]) * B:
                rt_m = int(math.ceil(rt_min / 60.0)) if np.isfinite(rt_min) else int(cfg["max_minutes"]) + 1
                need_m = _needed_minutes(rt_min, cfg) if np.isfinite(rt_min) else int(cfg["max_minutes"])
                raise LoopError(
                    "anchor_out_of_reach",
                    f"That place is about {rt_m} min there and back on the fastest roads. Choose {need_m} min or more.",
                    round_trip_min=rt_m,
                    needed_min=need_m,
                )
            in_reach = m_ids[rts <= float(cfg["target_max_rt_frac"]) * B]
            if len(in_reach) == 0:
                in_reach = m_ids[rts <= float(cfg["anchor_reach_frac"]) * B]
            pin_d = np.asarray(
                haversine_m(req_lon, req_lat, mom.lon[in_reach], mom.lat[in_reach]),
                dtype=np.float64,
            )
            pick_mid = int(in_reach[int(np.argmin(pin_d))])
            pt = (float(mom.lat[pick_mid]), float(mom.lon[pick_mid]))
            moved = float(haversine_m(req_lon, req_lat, pt[1], pt[0]))
            return Anchor(
                kind="shore",
                name=shore_name,
                point=pt,
                requested=(req_lat, req_lon),
                moved_m=moved,
                node=None,
                moments=m_ids,
                round_trip_s=rt_min,
            )

    raise LoopError(
        "anchor_far",
        "That pin is too far from any road or mapped shore. Move it closer to a road.",
    )


def features_in_reach(
    ctx: Context,
    start: tuple[float, float],
    minutes: float,
    q: str | None = None,
    limit: int = 8,
    cfg: dict | None = None,
) -> list[dict]:
    cfg = loop_config(cfg)
    min_m, max_m = int(cfg["min_minutes"]), int(cfg["max_minutes"])
    if not (isinstance(minutes, (int, float)) and math.isfinite(minutes) and min_m <= minutes <= max_m):
        raise ValueError(f"minutes must be in [{min_m}, {max_m}]")
    limit = max(1, min(int(limit), 30))
    B = 60.0 * float(minutes)
    g, mom = ctx.g, ctx.moments
    H, snap_m = g.snap(float(start[0]), float(start[1]))
    if snap_m > MAX_SNAP_M:
        raise LoopError(
            "snap_far",
            "The start point is too far from the nearest road in the graph.",
            snap_m=round(snap_m),
            limit_m=round(MAX_SNAP_M),
        )
    if len(mom.id) == 0:
        return []

    wide_lim = float(cfg["max_time_frac"]) * 60.0 * float(max_m)
    m_mat, _, _ = g._matrix(g.time_s)
    mt_mat = _rev_time_matrix(g)
    dH = dijkstra(m_mat, directed=True, indices=H, limit=wide_lim)
    dR = dijkstra(mt_mat, directed=True, indices=H, limit=wide_lim)
    fa, ra = edge_arcs(g)
    rts = _moment_rts(g, mom, mom.id, dH, dR, fa, ra)

    feat_arr = mom.feature if mom.feature is not None else np.full(len(mom.id), None, dtype=object)
    body_arr = mom.body if mom.body is not None else feat_arr
    lat0, lon0 = float(g.node_lat[H]), float(g.node_lon[H])
    reach_cap = float(cfg["anchor_reach_frac"]) * B
    q_norm = _norm_name(q) if q else ""

    groups: dict[str, dict] = {}
    for i in range(len(mom.id)):
        rt = float(rts[i])
        if not np.isfinite(rt) or rt > wide_lim:
            continue
        names_seen = set()
        for raw in (feat_arr[i], body_arr[i]):
            if not raw:
                continue
            nk = _norm_name(raw)
            if not nk or nk in names_seen:
                continue
            names_seen.add(nk)
            if q_norm and q_norm not in nk:
                continue
            rec = groups.get(nk)
            if rec is None:
                rec = {
                    "name": str(raw),
                    "in_B_count": 0,
                    "wide_count": 0,
                    "in_B_best_m": 0.0,
                    "wide_best_m": 0.0,
                    "types": set(),
                    "rt_min": np.inf,
                    "best_mid": int(mom.id[i]),
                }
                groups[nk] = rec
            rec["wide_count"] += 1
            mv = float(mom.m[i])
            if mv > rec["wide_best_m"]:
                rec["wide_best_m"] = mv
            if rt <= reach_cap:
                rec["in_B_count"] += 1
                if mv > rec["in_B_best_m"]:
                    rec["in_B_best_m"] = mv
            rec["types"].add(str(mom.type[i]))
            if rt < rec["rt_min"]:
                rec["rt_min"] = rt
                rec["best_mid"] = int(mom.id[i])

    entries = []
    for rec in groups.values():
        rt_min = rec["rt_min"]
        fits = bool(rt_min <= reach_cap)
        if not q_norm and not fits:
            continue
        cnt = rec["in_B_count"] if fits else rec["wide_count"]
        best_m = rec["in_B_best_m"] if fits else rec["wide_best_m"]
        mid = rec["best_mid"]
        mlat, mlon = float(mom.lat[mid]), float(mom.lon[mid])
        east, north = _tangent_xy(mlon, mlat, lon0, lat0)
        dist = float(math.hypot(float(east), float(north)))
        brg = float(_bearing_from_en(east, north)) if dist >= 1.0 else 0.0
        dword = direction_word(brg) if dist >= 1.0 else "N"
        entries.append({
            "name": rec["name"],
            "count": int(cnt),
            "best_m": round(float(best_m), 3),
            "types": sorted(rec["types"]),
            "round_trip_min": int(math.ceil(rt_min / 60.0)),
            "fits": fits,
            "needed_min": _needed_minutes(rt_min, cfg),
            "point": [round(mlat, 5), round(mlon, 5)],
            "bearing": round(brg, 1),
            "direction": dword,
            "_rank": float(cnt) * (float(best_m) ** 2),
        })

    entries.sort(key=lambda e: (not e["fits"], -e["_rank"], e["round_trip_min"], e["name"]))
    out = []
    for e in entries[:limit]:
        e = dict(e)
        e.pop("_rank", None)
        out.append(e)
    return out


# --------------------------------------------------------------------------
# Candidate vias, corridor leg builder, validity, bearing (sections 4.3-4.9)


@dataclass
class _Via:
    tail: int
    head: int
    arc: int  # -1 for a node via
    key: int
    value: float
    t_arc: float
    t_out: float
    t_back: float
    rt: float
    east: float
    north: float
    bearing: float
    dist: float


def _edge_midpoints_lonlat(g: RoadGraph, edges: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if len(edges) == 0:
        return np.zeros(0, dtype=np.float64), np.zeros(0, dtype=np.float64)
    if g.geom_off is None:
        u, v = g.u[edges], g.v[edges]
        return 0.5 * (g.node_lon[u] + g.node_lon[v]), 0.5 * (g.node_lat[u] + g.node_lat[v])
    lo = g.geom_off[edges]
    hi = g.geom_off[edges + 1]
    cnt = hi - lo
    mid = lo + cnt // 2
    lon = g.geom_lon[mid].astype(np.float64, copy=True)
    lat = g.geom_lat[mid].astype(np.float64, copy=True)
    two = cnt == 2
    if np.any(two):
        lon[two] = 0.5 * (g.geom_lon[lo[two]] + g.geom_lon[hi[two] - 1])
        lat[two] = 0.5 * (g.geom_lat[lo[two]] + g.geom_lat[hi[two] - 1])
    return lon, lat


def _build_candidates(ctx: Context, reach: Reach, cfg: dict) -> tuple[list[_Via], np.ndarray, cKDTree, np.ndarray, np.ndarray]:
    """Return (candidates, reg_edges, reg_mid_tree, node_x5070, node_y5070)."""
    g, S, mom = ctx.g, ctx.S, ctx.moments
    region = reach.region
    fa, ra = reach.fa, reach.ra
    dH, dR = reach.dH, reach.dR
    t_arc_all = g.time_s[g.arc_edge]

    reg_edges = np.flatnonzero(region[g.u] & region[g.v])
    mlon, mlat = _edge_midpoints_lonlat(g, reg_edges)
    mx, my = _to5070(mlon, mlat) if len(reg_edges) else (np.zeros(0), np.zeros(0))
    mid_tree = cKDTree(np.column_stack([mx, my])) if len(reg_edges) else cKDTree(np.zeros((0, 2)))

    reg_nodes = np.flatnonzero(region)
    nx_all = np.zeros(g.n_nodes, dtype=np.float64)
    ny_all = np.zeros(g.n_nodes, dtype=np.float64)
    if len(reg_nodes):
        rx, ry = _to5070(g.node_lon[reg_nodes], g.node_lat[reg_nodes])
        nx_all[reg_nodes] = rx
        ny_all[reg_nodes] = ry

    raw_rows: list[tuple[int, int, int, int, float]] = []  # (tail, head, arc, key, value)

    # 1. Moment vias
    mom_edge_mask = np.zeros(g.n_edges, dtype=bool)
    if len(mom.id):
        mom_edge_mask[mom.edge] = True
        mom_edge_mask |= mom.of_edge >= 0
        for k in range(len(mom.id)):
            e = int(mom.edge[k])
            val = float(mom.m[k]) ** 2
            mid = int(mom.id[k])
            for a in (int(fa[e]), int(ra[e])):
                if a >= 0 and region[g.arc_tail[a]] and region[g.arc_head[a]]:
                    raw_rows.append((int(g.arc_tail[a]), int(g.arc_head[a]), a, mid, val))

    # 2. Seed vias
    if len(reg_edges):
        s_reg = S[reg_edges]
        l_reg = g.length_m[reg_edges]
        qual = (
            (s_reg >= float(cfg["seed_min_S"]))
            & (l_reg >= float(cfg["seed_min_length_m"]))
            & (~mom_edge_mask[reg_edges])
        )
        q_idx = np.flatnonzero(qual)
        if len(q_idx):
            cell_m = float(cfg["seed_cell_m"])
            cx = np.floor(mx[q_idx] / cell_m).astype(np.int64)
            cy = np.floor(my[q_idx] / cell_m).astype(np.int64)
            cells: dict[tuple[int, int], list[int]] = {}
            for pos_i, cell_key in enumerate(zip(cx.tolist(), cy.tolist())):
                cells.setdefault(cell_key, []).append(int(q_idx[pos_i]))
            seed_val_max = float(cfg["seed_value"])
            seed_full = float(cfg["seed_full_m"])
            for members in cells.values():
                m_arr = np.asarray(members, dtype=np.int64)
                scores = s_reg[m_arr] * np.sqrt(l_reg[m_arr])
                best_local = int(m_arr[int(np.argmax(scores))])
                e = int(reg_edges[best_local])
                tot_ls = float(np.sum(l_reg[m_arr] * s_reg[m_arr]))
                val = seed_val_max * min(1.0, tot_ls / max(seed_full, 1e-9))
                key = -1 - e
                for a in (int(fa[e]), int(ra[e])):
                    if a >= 0 and region[g.arc_tail[a]] and region[g.arc_head[a]]:
                        raw_rows.append((int(g.arc_tail[a]), int(g.arc_head[a]), a, key, val))

    if not raw_rows:
        return [], reg_edges, mid_tree, nx_all, ny_all

    heads = np.asarray([r[1] for r in raw_rows], dtype=np.int64)
    east_arr, north_arr = _tangent_xy(g.node_lon[heads], g.node_lat[heads], reach.lon0, reach.lat0)
    brg_arr = _bearing_from_en(east_arr, north_arr)
    dist_arr = np.hypot(east_arr, north_arr)

    vias: list[_Via] = []
    for i, (tail, head, arc, key, val) in enumerate(raw_rows):
        ta = float(t_arc_all[arc])
        t_out = float(dH[tail] + ta)
        t_back = float(dR[head])
        rt = t_out + t_back
        if not np.isfinite(rt):
            continue
        vias.append(
            _Via(
                tail=tail,
                head=head,
                arc=arc,
                key=key,
                value=val,
                t_arc=ta,
                t_out=t_out,
                t_back=t_back,
                rt=rt,
                east=float(east_arr[i]),
                north=float(north_arr[i]),
                bearing=float(brg_arr[i]),
                dist=float(dist_arr[i]),
            )
        )
    return vias, reg_edges, mid_tree, nx_all, ny_all


def _make_node_via(g: RoadGraph, reach: Reach, node: int) -> _Via:
    east, north = _tangent_xy(g.node_lon[node], g.node_lat[node], reach.lon0, reach.lat0)
    east_f, north_f = float(east), float(north)
    dist_f = math.hypot(east_f, north_f)
    brg_f = float(_bearing_from_en(east_f, north_f)) if dist_f >= 1.0 else 0.0
    t_out = float(reach.dH[node])
    t_back = float(reach.dR[node])
    return _Via(
        tail=int(node),
        head=int(node),
        arc=-1,
        key=-1_000_000_000 - int(node),
        value=1.0,
        t_arc=0.0,
        t_out=t_out,
        t_back=t_back,
        rt=t_out + t_back,
        east=east_f,
        north=north_f,
        bearing=brg_f,
        dist=dist_f,
    )


def _best_dir(vias: list[_Via], indices: list[int]) -> list[int]:
    best: dict[int, int] = {}
    for idx in indices:
        k = vias[idx].key
        prev = best.get(k)
        if prev is None or vias[idx].rt < vias[prev].rt:
            best[k] = idx
    return sorted(best.values(), key=lambda i: (-vias[i].value, i))


def _spaced(vias: list[_Via], indices: list[int], n: int, gap_m: float) -> list[int]:
    kept: list[int] = []
    for idx in indices:
        if len(kept) >= n:
            break
        v = vias[idx]
        if all(math.hypot(v.east - vias[j].east, v.north - vias[j].north) >= gap_m for j in kept):
            kept.append(idx)
    return kept


class _LoopBuilder:
    """Builds penalized, U-turn-safe loop legs on per-lambda CostGraphs."""

    def __init__(
        self,
        ctx: Context,
        reach: Reach,
        cfg: dict,
        reg_edges: np.ndarray,
        mid_tree: cKDTree,
        nx5070: np.ndarray,
        ny5070: np.ndarray,
    ):
        self.ctx = ctx
        self.g = ctx.g
        self.reach = reach
        self.cfg = cfg
        self.reg_edges = reg_edges
        self.mid_tree = mid_tree
        self.nx5070 = nx5070
        self.ny5070 = ny5070
        self.dijkstras = 0
        self.built = 0

        self.lambdas = [float(lam) for lam in cfg["lambdas"]]
        self.graphs: dict[float, CostGraph] = {}
        for lam in self.lambdas:
            if lam not in self.graphs:
                cost = self.g.time_s if lam == 0.0 else self.g.time_s * (1.0 + lam * (1.0 - ctx.S))
                self.graphs[lam] = CostGraph(self.g, reach.arcs, cost)
        if 0.0 not in self.graphs:
            self.graphs[0.0] = CostGraph(self.g, reach.arcs, self.g.time_s)
        self.G0 = self.graphs[0.0]
        self.arc_edges: dict[float, np.ndarray] = {
            lam: self.g.arc_edge[G.arc] for lam, G in self.graphs.items()
        }
        # Pre-warm root H trees on lambda graphs (and count them)
        for lam in self.lambdas:
            self.graphs[lam].tree(reach.node)
            self.dijkstras += 1

    def time_dist_from(self, node: int, limit: float) -> np.ndarray:
        self.dijkstras += 1
        return dijkstra(self.G0.fwd, directed=True, indices=int(node), limit=limit, return_predecessors=False)

    def time_dist_to(self, node: int, limit: float) -> np.ndarray:
        self.dijkstras += 1
        return dijkstra(self.G0.rev, directed=True, indices=int(node), limit=limit, return_predecessors=False)

    def corridor_mask(self, path_nodes: np.ndarray, path_arcs: np.ndarray) -> np.ndarray:
        used = np.zeros(self.g.n_edges, dtype=bool)
        if len(path_arcs):
            used[self.g.arc_edge[path_arcs]] = True
        r = float(self.cfg["reuse_radius_m"])
        if r > 0 and len(self.reg_edges) and len(path_nodes):
            x = self.nx5070[path_nodes]
            y = self.ny5070[path_nodes]
            if len(path_nodes) >= 2:
                px, py, _ = _densify(x, y, r)
            else:
                px, py = x, y
            hits = self.mid_tree.query_ball_point(np.column_stack([px, py]), r=r)
            flat = [j for sub in hits for j in sub]
            if flat:
                used[self.reg_edges[np.asarray(flat, dtype=np.int64)]] = True
        return used

    def _penalized_tree(
        self, lam: float, source: int, used: np.ndarray, ban: tuple[int, int] | None
    ) -> tuple[np.ndarray, np.ndarray]:
        G = self.graphs[lam]
        data = G.fwd.data.copy()
        ae = self.arc_edges[lam]
        pen = float(self.cfg["reuse_penalty"])
        if pen > 0:
            mask = used[ae]
            if np.any(mask):
                data[mask] *= 1.0 + pen
        if ban is not None:
            k_ban = int(ban[0]) * G.n + int(ban[1])
            idx = int(np.searchsorted(G.key, k_ban))
            if idx < len(G.key) and int(G.key[idx]) == k_ban:
                data[idx] = float(self.cfg["uturn_ban_cost"])
        mat = csr_matrix((data, G.fwd.indices, G.fwd.indptr), shape=(G.n, G.n))
        self.dijkstras += 1
        return dijkstra(mat, directed=True, indices=int(source), return_predecessors=True)

    @staticmethod
    def _extract_path(dist: np.ndarray, pred: np.ndarray, src: int, dst: int, ban_cost: float) -> np.ndarray | None:
        d = float(dist[dst])
        if not np.isfinite(d) or d >= 0.5 * ban_cost:
            return None
        p = [int(dst)]
        cur = int(dst)
        limit = len(pred) + 1
        while cur != src:
            cur = int(pred[cur])
            if cur < 0 or len(p) > limit:
                return None
            p.append(cur)
        return np.asarray(p[::-1], dtype=np.int64)

    def _advance_via(
        self,
        G: CostGraph,
        p: np.ndarray,
        via: _Via,
        prev_ban: tuple[int, int] | None,
    ) -> tuple[np.ndarray, np.ndarray, int, tuple[int, int] | None] | None:
        """Given path `p` from cur to via.tail, apply via arc / through rule.
        Returns (added_nodes, added_arcs, next_cur, next_ban) or None if U-turn."""
        if len(p) > 1:
            leg_nodes = p[1:]
            leg_arcs = G.arcs_between(p[:-1], p[1:])
            step_ban: tuple[int, int] | None = (int(p[-1]), int(p[-2]))
        else:
            leg_nodes = np.zeros(0, dtype=np.int64)
            leg_arcs = np.zeros(0, dtype=np.int64)
            step_ban = prev_ban

        if via.arc < 0:
            return leg_nodes, leg_arcs, int(via.head), step_ban

        v_edge = int(self.g.arc_edge[via.arc])
        if len(leg_arcs) and int(self.g.arc_edge[leg_arcs[-1]]) == v_edge:
            # Through rule: arrived by driving the via edge backwards
            return leg_nodes, leg_arcs, int(via.tail), step_ban

        if len(leg_arcs) == 0 and prev_ban is not None and (int(via.tail), int(via.head)) == prev_ban:
            return None

        added_nodes = np.concatenate([leg_nodes, np.asarray([via.head], dtype=np.int64)])
        added_arcs = np.concatenate([leg_arcs, np.asarray([via.arc], dtype=np.int64)])
        return added_nodes, added_arcs, int(via.head), (int(via.head), int(via.tail))

    def build_lens(self, via: _Via, origin_tag: str, dead_end_retry: bool = False) -> tuple[Route, bool] | None:
        H = self.reach.node
        B = self.reach.budget_s
        min_t = float(self.cfg["min_time_frac"]) * B
        max_t = float(self.cfg["max_time_frac"]) * B
        ban_cost = float(self.cfg["uturn_ban_cost"])

        for lam in self.lambdas:
            G = self.graphs[lam]
            dist1, pred1 = G.tree(H)
            p1 = self._extract_path(dist1, pred1, H, via.tail, ban_cost)
            if p1 is None:
                return None
            adv1 = self._advance_via(G, p1, via, None)
            if adv1 is None:
                return None
            n1, a1_arcs, cur1, ban1 = adv1
            nodes1 = np.concatenate([np.asarray([H], dtype=np.int64), n1])
            used1 = self.corridor_mask(nodes1, a1_arcs)

            uturn = False
            dist2, pred2 = self._penalized_tree(lam, cur1, used1, ban1)
            p2 = self._extract_path(dist2, pred2, cur1, H, ban_cost)
            if p2 is None and dead_end_retry and ban1 is not None:
                dist2, pred2 = self._penalized_tree(lam, cur1, used1, None)
                p2 = self._extract_path(dist2, pred2, cur1, H, ban_cost)
                uturn = True
            if p2 is None:
                return None
            a2_arcs = G.arcs_between(p2[:-1], p2[1:]) if len(p2) > 1 else np.zeros(0, dtype=np.int64)
            full_nodes = np.concatenate([nodes1, p2[1:]]) if len(p2) > 1 else nodes1
            full_arcs = np.concatenate([a1_arcs, a2_arcs])
            if len(full_arcs) == 0:
                return None
            route = self.g.make_route(full_nodes, full_arcs)
            self.built += 1
            if route.time_s <= max_t + 1e-6:
                if route.time_s >= min_t - 1e-6:
                    return route, uturn
                return None
        return None

    def build_triangles(
        self,
        a1: _Via,
        partners: list[_Via],
        retry_after_a1: bool = False,
        retry_after_a2: bool = False,
    ) -> list[tuple[Route, bool, _Via]]:
        """Build H -> a1 -> a2 -> H across the lambda ladder, sharing the middle tree."""
        if not partners:
            return []
        H = self.reach.node
        B = self.reach.budget_s
        min_t = float(self.cfg["min_time_frac"]) * B
        max_t = float(self.cfg["max_time_frac"]) * B
        ban_cost = float(self.cfg["uturn_ban_cost"])

        pending = list(partners)
        results: list[tuple[Route, bool, _Via]] = []

        for lam in self.lambdas:
            if not pending:
                break
            G = self.graphs[lam]
            dist1, pred1 = G.tree(H)
            p1 = self._extract_path(dist1, pred1, H, a1.tail, ban_cost)
            if p1 is None:
                break
            adv1 = self._advance_via(G, p1, a1, None)
            if adv1 is None:
                break
            n1, arcs1, cur1, ban1 = adv1
            nodes1 = np.concatenate([np.asarray([H], dtype=np.int64), n1])
            used1 = self.corridor_mask(nodes1, arcs1)

            dist2, pred2 = self._penalized_tree(lam, cur1, used1, ban1)
            noban_tree2: tuple[np.ndarray, np.ndarray] | None = None

            retry: list[_Via] = []
            for a2 in pending:
                uturn = False
                p2 = self._extract_path(dist2, pred2, cur1, a2.tail, ban_cost)
                if p2 is None and retry_after_a1 and ban1 is not None:
                    if noban_tree2 is None:
                        noban_tree2 = self._penalized_tree(lam, cur1, used1, None)
                    p2 = self._extract_path(noban_tree2[0], noban_tree2[1], cur1, a2.tail, ban_cost)
                    uturn = True
                if p2 is None:
                    continue
                adv2 = self._advance_via(G, p2, a2, ban1 if not uturn else (int(p2[-1]), int(p2[-2])) if len(p2) > 1 else None)
                if adv2 is None:
                    continue
                n2, arcs2, cur2, ban2 = adv2
                nodes12 = np.concatenate([nodes1, n2])
                arcs12 = np.concatenate([arcs1, arcs2])
                used12 = used1 | self.corridor_mask(
                    np.concatenate([np.asarray([cur1], dtype=np.int64), n2]),
                    arcs2,
                )

                dist3, pred3 = self._penalized_tree(lam, cur2, used12, ban2)
                p3 = self._extract_path(dist3, pred3, cur2, H, ban_cost)
                if p3 is None and retry_after_a2 and ban2 is not None:
                    dist3, pred3 = self._penalized_tree(lam, cur2, used12, None)
                    p3 = self._extract_path(dist3, pred3, cur2, H, ban_cost)
                    uturn = True
                if p3 is None:
                    continue

                arcs3 = G.arcs_between(p3[:-1], p3[1:]) if len(p3) > 1 else np.zeros(0, dtype=np.int64)
                full_nodes = np.concatenate([nodes12, p3[1:]]) if len(p3) > 1 else nodes12
                full_arcs = np.concatenate([arcs12, arcs3])
                if len(full_arcs) == 0:
                    continue
                route = self.g.make_route(full_nodes, full_arcs)
                self.built += 1
                if route.time_s > max_t + 1e-6:
                    retry.append(a2)
                elif route.time_s >= min_t - 1e-6:
                    results.append((route, uturn, a2))
            pending = retry

        return results


def loop_validity(route: Route, cfg: dict | None = None) -> dict:
    """Doubled-road share, stem length, net doubled share, and tier (1, 2, or 0=rejected)."""
    cfg = loop_config(cfg)
    geo = np.asarray(route.geometry, dtype=np.float64)
    if len(geo) < 2:
        return {"doubled": 1.0, "net": 1.0, "stem_frac": 0.0, "stem_km": 0.0, "tier": 0}
    x, y = _to5070(geo[:, 0], geo[:, 1])
    px, py, s = _densify(x, y, float(cfg["sample_step_m"]))
    L = float(s[-1]) if len(s) else 0.0
    if L <= 0.0 or len(s) < 2:
        return {"doubled": 1.0, "net": 1.0, "stem_frac": 0.0, "stem_km": 0.0, "tier": 0}

    pairs = cKDTree(np.column_stack([px, py])).query_pairs(
        float(cfg["doubled_radius_m"]), output_type="ndarray"
    )
    flagged = np.zeros(len(s), dtype=bool)
    if len(pairs):
        i, j = pairs.T
        ds = np.abs(s[i] - s[j])
        cyc = np.minimum(ds, L - ds)
        ok = cyc > float(cfg["doubled_min_sep_m"])
        if np.any(ok):
            flagged[i[ok]] = True
            flagged[j[ok]] = True

    doubled = float(flagged.mean())

    stem_start = float(cfg["stem_start_m"])
    stem_gap = float(cfg["stem_gap_m"])

    def _stem_run(s_arr: np.ndarray, flg: np.ndarray) -> float:
        idx = np.flatnonzero(flg)
        if len(idx) == 0 or float(s_arr[idx[0]]) > stem_start:
            return 0.0
        last = int(idx[0])
        for k in range(1, len(idx)):
            cur = int(idx[k])
            if float(s_arr[cur] - s_arr[last]) <= stem_gap:
                last = cur
            else:
                break
        return float(s_arr[last])

    out_m = _stem_run(s, flagged)
    s_rev = L - s[::-1]
    back_m = _stem_run(s_rev, flagged[::-1])

    stem_frac = (out_m + back_m) / L
    stem_km = min(L, out_m + back_m) / 2000.0

    # Sample interval weights summing to L
    seg = np.diff(s)
    w = np.zeros(len(s), dtype=np.float64)
    w[0] = 0.5 * seg[0]
    w[-1] = 0.5 * seg[-1]
    if len(s) > 2:
        w[1:-1] = 0.5 * (seg[:-1] + seg[1:])

    outside = flagged & (s > out_m + 1e-9) & (s < L - back_m - 1e-9)
    net = float(np.sum(w[outside]) / L)

    max_net = float(cfg["max_net_doubled_frac"])
    if net <= max_net + 1e-9 and stem_frac <= float(cfg["stem_frac_tier1"]) + 1e-9:
        tier = 1
    elif net <= max_net + 1e-9 and stem_frac <= float(cfg["stem_frac_tier2"]) + 1e-9:
        tier = 2
    else:
        tier = 0

    return {
        "doubled": doubled,
        "net": net,
        "stem_frac": float(stem_frac),
        "stem_km": float(stem_km),
        "tier": tier,
    }


def loop_bearing(g: RoadGraph, route: Route, node: int) -> tuple[float | None, float]:
    """Time-weighted centroid bearing in the tangent plane at `node`, and max edge distance (km)."""
    e = np.asarray(route.edges, dtype=np.int64)
    if len(e) == 0:
        return None, 0.0
    lat0, lon0 = float(g.node_lat[node]), float(g.node_lon[node])
    u, v = g.u[e], g.v[e]
    eu, nu = _tangent_xy(g.node_lon[u], g.node_lat[u], lon0, lat0)
    ev, nv = _tangent_xy(g.node_lon[v], g.node_lat[v], lon0, lat0)
    em = 0.5 * (eu + ev)
    nm = 0.5 * (nu + nv)
    far_km = float(np.max(np.hypot(em, nm)) / 1000.0)
    t = g.time_s[e]
    tot_t = float(np.sum(t))
    if tot_t <= 0.0:
        return None, far_km
    ce = float(np.sum(t * em) / tot_t)
    cn = float(np.sum(t * nm) / tot_t)
    if math.hypot(ce, cn) < 500.0:
        return None, far_km
    return float(_bearing_from_en(ce, cn)), far_km


# --------------------------------------------------------------------------
# Pairing, work cap, selection, and main `loops()` entry point


def _pick_partners(
    vias: list[_Via],
    a1: _Via,
    d1: np.ndarray,
    B: float,
    cfg: dict,
    extra_pred=None,
) -> list[_Via]:
    sep_lo, sep_hi = float(cfg["partner_sep_deg"][0]), float(cfg["partner_sep_deg"][1])
    gain = float(cfg["partner_min_gain"])
    t_lo, t_hi = float(cfg["partner_tight"][0]) * B, float(cfg["partner_tight"][1]) * B
    w_lo, w_hi = float(cfg["partner_wide"][0]) * B, float(cfg["partner_wide"][1]) * B
    u_lo, u_hi = min(t_lo, w_lo), max(t_hi, w_hi)
    tight_k = int(cfg["partner_tight_k"])
    total_k = int(cfg["partner_k"])

    by_key: dict[int, tuple[float, int]] = {}
    for idx, a2 in enumerate(vias):
        if a2.key == a1.key:
            continue
        if extra_pred is not None and not extra_pred(a2):
            continue
        sep = float(angdiff(a2.bearing, a1.bearing))
        if not (sep_lo <= sep <= sep_hi):
            continue
        d_mid = float(d1[a2.tail])
        if not np.isfinite(d_mid):
            continue
        est = a1.t_out + d_mid + a2.t_arc + a2.t_back
        if not (u_lo <= est <= u_hi):
            continue
        if est < gain * max(a1.rt, a2.rt):
            continue
        prev = by_key.get(a2.key)
        if prev is None or est < prev[0]:
            by_key[a2.key] = (est, idx)

    ordered = sorted(by_key.values(), key=lambda pair: (-vias[pair[1]].value, pair[1]))
    chosen_idx: list[int] = []
    chosen_set: set[int] = set()
    for est, idx in ordered:
        if len(chosen_idx) >= tight_k:
            break
        if t_lo <= est <= t_hi:
            chosen_idx.append(idx)
            chosen_set.add(idx)
    for est, idx in ordered:
        if len(chosen_idx) >= total_k:
            break
        if idx not in chosen_set and (w_lo <= est <= w_hi):
            chosen_idx.append(idx)
            chosen_set.add(idx)
    return [vias[i] for i in chosen_idx]


def _pick_straddle_rights(
    vias: list[_Via],
    a_left: _Via,
    d1: np.ndarray,
    theta: float,
    B: float,
    cfg: dict,
) -> list[_Via]:
    off_lo, off_hi = float(cfg["straddle_offset_deg"][0]), float(cfg["straddle_offset_deg"][1])
    max_skew = float(cfg["straddle_max_skew_deg"])
    gain = float(cfg["partner_min_gain"])
    w_lo, w_hi = float(cfg["partner_wide"][0]) * B, float(cfg["partner_wide"][1]) * B
    right_k = int(cfg["straddle_right_k"])
    sd_left = float(sdiff(a_left.bearing, theta))

    by_key: dict[int, tuple[float, int]] = {}
    for idx, ar in enumerate(vias):
        if ar.key == a_left.key:
            continue
        sd_r = float(sdiff(ar.bearing, theta))
        if not (off_lo <= sd_r <= off_hi):
            continue
        if abs(sd_left + sd_r) > max_skew:
            continue
        d_mid = float(d1[ar.tail])
        if not np.isfinite(d_mid):
            continue
        est = a_left.t_out + d_mid + ar.t_arc + ar.t_back
        if not (w_lo <= est <= w_hi):
            continue
        if est < gain * max(a_left.rt, ar.rt):
            continue
        prev = by_key.get(ar.key)
        if prev is None or est < prev[0]:
            by_key[ar.key] = (est, idx)

    ordered = sorted(by_key.values(), key=lambda pair: (-vias[pair[1]].value, pair[1]))
    return [vias[idx] for _, idx in ordered[:right_k]]


def _pick_anchor_partners(
    vias: list[_Via],
    target: _Via,
    dists: np.ndarray,
    forward: bool,
    B: float,
    cfg: dict,
) -> list[_Via]:
    w_lo, w_hi = float(cfg["anchor_partner_window"][0]) * B, float(cfg["anchor_partner_window"][1]) * B
    sec_deg = float(cfg["anchor_partner_sector_deg"])
    want_k = int(cfg["anchor_partner_k"])

    by_key: dict[int, tuple[float, int]] = {}
    for idx, cand in enumerate(vias):
        if cand.key == target.key:
            continue
        if forward:
            d_mid = float(dists[cand.tail])
            if not np.isfinite(d_mid):
                continue
            est = target.t_out + d_mid + cand.t_arc + cand.t_back
        else:
            d_mid = float(dists[cand.head])
            if not np.isfinite(d_mid):
                continue
            est = cand.t_out + d_mid + target.t_arc + target.t_back
        if not (w_lo <= est <= w_hi):
            continue
        prev = by_key.get(cand.key)
        if prev is None or est < prev[0]:
            by_key[cand.key] = (est, idx)

    ordered = sorted(by_key.values(), key=lambda pair: (-vias[pair[1]].value, pair[1]))
    used_sectors: set[int] = set()
    chosen: list[_Via] = []
    for _, idx in ordered:
        if len(chosen) >= want_k:
            break
        sec = int(vias[idx].bearing // max(sec_deg, 1e-6))
        if sec in used_sectors:
            continue
        used_sectors.add(sec)
        chosen.append(vias[idx])
    return chosen


def _interleave(a: list, b: list) -> list:
    out = []
    na, nb = len(a), len(b)
    for k in range(max(na, nb)):
        if k < na:
            out.append(a[k])
        if k < nb:
            out.append(b[k])
    return out


def _select_loops(
    pool: list[LoopRoute],
    length_m: np.ndarray,
    steer: Steer,
    cfg: dict,
) -> list[LoopRoute]:
    n_want = int(cfg["n_loops"])
    if not pool or n_want <= 0:
        return []
    ov_base = float(cfg["max_overlap_anchor"] if steer.mode == "anchor" else cfg["max_overlap"])
    ov_rel = float(cfg["max_overlap_relaxed"])
    seps = [float(x) for x in cfg["min_bearing_sep_deg"]] if steer.mode == "any" else [0.0]

    trials: list[tuple[float, float]] = [(ov_base, s) for s in seps]
    trials.append((ov_rel, seps[-1]))

    best_picks: list[LoopRoute] = []
    for ov_lim, sep_deg in trials:
        picks: list[LoopRoute] = []
        for lr in pool:
            if len(picks) >= n_want:
                break
            if any(route_overlap(lr.route, p.route, length_m) > ov_lim + 1e-9 for p in picks):
                continue
            if sep_deg > 0.0:
                if lr.bearing is None:
                    if picks:
                        continue
                elif any(
                    p.bearing is not None and float(angdiff(lr.bearing, p.bearing)) < sep_deg - 1e-9
                    for p in picks
                ):
                    continue
            picks.append(lr)
        if len(picks) > len(best_picks):
            best_picks = picks
        if len(best_picks) >= n_want:
            break
    return best_picks


def _label_picks(picks: list[LoopRoute]) -> None:
    counts: dict[str, int] = {}
    for lr in picks:
        base = f"{DIRECTION_WORDS[lr.direction]} loop" if lr.direction in DIRECTION_WORDS else "Loop"
        c = counts.get(base, 0) + 1
        counts[base] = c
        lr.label = base if c == 1 else f"{base} {c}"


def _join_words(words: list[str]) -> str:
    if len(words) == 1:
        return words[0]
    if len(words) == 2:
        return f"{words[0]} and {words[1]}"
    return f"{', '.join(words[:-1])} and {words[-1]}"


def _build_note(picks: list[LoopRoute], steer: Steer, anchor: Anchor | None, cfg: dict) -> str | None:
    notes: list[str] = []
    if steer.mode == "compass" and steer.direction:
        req = steer.direction.strip().upper()
        if not any(p.direction == req for p in picks):
            req_word = DIRECTION_WORDS[req].lower()
            lean_words: list[str] = []
            for p in picks:
                if p.direction in DIRECTION_WORDS:
                    w = DIRECTION_WORDS[p.direction].lower()
                    if w not in lean_words:
                        lean_words.append(w)
            if lean_words:
                notes.append(
                    f"No loop this long stays to the {req_word} of here. "
                    f"The closest ones lean {_join_words(lean_words)}."
                )
            else:
                notes.append(f"No loop this long stays to the {req_word} of here.")
    if anchor is not None and anchor.kind == "shore":
        shore_desc = f"{anchor.name} shore" if anchor.name else "mapped shore"
        notes.append(f"Your pin is on the water, so these loops pass the nearest stretch of {shore_desc}.")
    n_want = int(cfg["n_loops"])
    if len(picks) < n_want:
        notes.append("Only 1 loop fits." if len(picks) == 1 else f"Only {len(picks)} loops fit.")
    return " ".join(notes) if notes else None


def loops(
    ctx: Context,
    start: tuple[float, float],
    minutes: float,
    steer: Steer = Steer(),
    cfg: dict | None = None,
    log=None,
) -> LoopResult:
    cfg = loop_config(cfg)
    min_m, max_m = int(cfg["min_minutes"]), int(cfg["max_minutes"])
    if not (isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and math.isfinite(minutes)):
        raise ValueError(f"minutes must be a finite number in [{min_m}, {max_m}]")
    if not (min_m <= float(minutes) <= max_m):
        raise ValueError(f"minutes must be in [{min_m}, {max_m}], got {minutes}")

    mode = str(steer.mode or "any").strip().lower()
    if mode not in ("any", "compass", "anchor"):
        raise ValueError(f"unknown steer mode {steer.mode!r}")

    req_dir: str | None = None
    theta = 0.0
    if mode == "compass":
        if not steer.direction:
            raise ValueError("compass steer requires direction")
        req_dir = str(steer.direction).strip().upper()
        theta = direction_bearing(req_dir)
    elif mode == "anchor":
        has_feat = bool(steer.feature and str(steer.feature).strip())
        has_pt = steer.point is not None
        if has_feat == has_pt:
            raise ValueError("anchor steer requires exactly one of point or feature")

    norm_steer = Steer(
        mode=mode,
        direction=req_dir,
        point=(float(steer.point[0]), float(steer.point[1])) if steer.point is not None else None,
        feature=str(steer.feature).strip() if steer.feature else None,
    )

    t_total0 = time.monotonic()
    secs: dict[str, float] = {}
    B = 60.0 * float(minutes)
    min_disp = int(round(float(minutes)))

    # 4.1 & 4.2 Reach cut
    t0 = time.monotonic()
    reach = compute_reach(ctx.g, (float(start[0]), float(start[1])), B, cfg)
    if reach.snap_m > MAX_SNAP_M:
        raise LoopError(
            "snap_far",
            "The start point is too far from the nearest road in the graph.",
            snap_m=round(reach.snap_m),
            limit_m=round(MAX_SNAP_M),
        )
    anchor = resolve_anchor(ctx, reach, norm_steer, cfg) if mode == "anchor" else None
    secs["reach"] = round(time.monotonic() - t0, 3)

    # 4.3 Candidate vias
    t0 = time.monotonic()
    vias, reg_edges, mid_tree, nx5070, ny5070 = _build_candidates(ctx, reach, cfg)
    secs["vias"] = round(time.monotonic() - t0, 3)

    # 4.4-4.6 Build loops under work cap
    t0 = time.monotonic()
    n_arcs = max(len(reach.arcs), 1)
    c_lo, c_hi = int(cfg["dijkstra_count_range"][0]), int(cfg["dijkstra_count_range"][1])
    max_dijkstras = int(np.clip(math.floor(float(cfg["dijkstra_work_arcs"]) / n_arcs), c_lo, c_hi))

    builder = _LoopBuilder(ctx, reach, cfg, reg_edges, mid_tree, nx5070, ny5070)
    raw_built: list[tuple[Route, bool, str, tuple]] = []  # (route, uturn, origin, vias_tuple)
    lim_s = float(cfg["max_time_frac"]) * B

    if mode == "any":
        rt_lo, rt_hi = float(cfg["first_rt_frac"][0]) * B, float(cfg["first_rt_frac"][1]) * B
        elig = _best_dir(vias, [i for i, v in enumerate(vias) if rt_lo <= v.rt <= rt_hi])
        sec_deg = float(cfg["any_sector_deg"])
        per_sec = int(cfg["any_per_sector"])
        n_sec = max(1, int(round(360.0 / sec_deg)))
        sectors: dict[int, list[int]] = {s: [] for s in range(n_sec)}
        for i in elig:
            s = int(vias[i].bearing // sec_deg) % n_sec
            if len(sectors[s]) < per_sec:
                sectors[s].append(i)
        ordered_first: list[int] = []
        for rank_idx in range(per_sec):
            for s in range(n_sec):
                if rank_idx < len(sectors[s]):
                    ordered_first.append(sectors[s][rank_idx])

        for idx1 in ordered_first:
            if builder.dijkstras >= max_dijkstras:
                break
            a1 = vias[idx1]
            d1 = builder.time_dist_from(a1.head, lim_s)
            partners = _pick_partners(vias, a1, d1, B, cfg)
            for route, uturn, a2 in builder.build_triangles(a1, partners):
                raw_built.append((route, uturn, f"any {a1.key}->{a2.key}", (a1.key, a2.key)))

    elif mode == "compass":
        cone_deg = float(cfg["cone_deg"])
        rt_hi = float(cfg["first_rt_frac"][1]) * B
        rt_lo = float(cfg["first_rt_frac"][0]) * B
        cone_idx = [
            i for i, v in enumerate(vias)
            if float(angdiff(v.bearing, theta)) <= cone_deg and v.rt <= rt_hi
        ]
        if not cone_idx:
            dir_word = DIRECTION_WORDS[req_dir].lower()
            raise LoopError(
                "no_loop_that_way",
                f"No loop fits to the {dir_word} in {min_disp} min. Try a longer drive or another direction.",
            )
        rad = math.radians(theta)
        sin_t, cos_t = math.sin(rad), math.cos(rad)
        along = np.asarray([vias[i].east * sin_t + vias[i].north * cos_t for i in cone_idx], dtype=np.float64)
        depth = float(np.quantile(along, float(cfg["depth_quantile"])))
        deep_cone = [
            cone_idx[k] for k in range(len(cone_idx))
            if along[k] >= float(cfg["depth_frac"]) * depth
        ]
        apex_idx = _spaced(
            vias,
            _best_dir(vias, deep_cone),
            int(cfg["apex_k"]),
            float(cfg["anchor_gap_m"]),
        )

        off_lo, off_hi = float(cfg["straddle_offset_deg"][0]), float(cfg["straddle_offset_deg"][1])
        left_cand = [
            i for i, v in enumerate(vias)
            if -off_hi <= float(sdiff(v.bearing, theta)) <= -off_lo and (rt_lo <= v.rt <= rt_hi)
        ]
        left_idx = _spaced(
            vias,
            _best_dir(vias, left_cand),
            int(cfg["straddle_left_k"]),
            float(cfg["anchor_gap_m"]),
        )

        tasks = _interleave([("apex", i) for i in apex_idx], [("straddle", i) for i in left_idx])
        d1_cache: dict[int, np.ndarray] = {}
        half_plane = float(cfg["partner_half_plane_deg"])

        for kind, idx1 in tasks:
            if builder.dijkstras >= max_dijkstras:
                break
            a1 = vias[idx1]
            if a1.head not in d1_cache:
                d1_cache[a1.head] = builder.time_dist_from(a1.head, lim_s)
            d1 = d1_cache[a1.head]
            if kind == "apex":
                partners = _pick_partners(
                    vias,
                    a1,
                    d1,
                    B,
                    cfg,
                    extra_pred=lambda a2: float(angdiff(a2.bearing, theta)) <= half_plane,
                )
            else:
                partners = _pick_straddle_rights(vias, a1, d1, theta, B, cfg)
            for route, uturn, a2 in builder.build_triangles(a1, partners):
                raw_built.append((route, uturn, f"{kind} {a1.key}->{a2.key}", (a1.key, a2.key)))

    else:  # anchor
        assert anchor is not None
        targets: list[_Via] = []
        if anchor.kind == "point":
            assert anchor.node is not None
            targets = [_make_node_via(ctx.g, reach, anchor.node)]
        else:
            mset = set(int(m) for m in anchor.moments)
            max_rt = float(cfg["target_max_rt_frac"]) * B
            cand_f = [i for i, v in enumerate(vias) if v.key in mset and v.rt <= max_rt]
            if not cand_f:
                cand_f = [
                    i for i, v in enumerate(vias)
                    if v.key in mset and v.rt <= float(cfg["anchor_reach_frac"]) * B
                ]
            best_f = _best_dir(vias, cand_f)
            if anchor.kind == "feature":
                best_f.sort(key=lambda i: (-vias[i].value, vias[i].rt, i))
                picked_f = _spaced(
                    vias,
                    best_f,
                    int(cfg["feature_targets"]),
                    float(cfg["feature_target_gap_m"]),
                )
                targets = [vias[i] for i in picked_f]
            else:  # shore
                req_lat, req_lon = anchor.requested or anchor.point
                best_f.sort(
                    key=lambda i: (
                        float(
                            haversine_m(
                                req_lon,
                                req_lat,
                                ctx.moments.lon[vias[i].key],
                                ctx.moments.lat[vias[i].key],
                            )
                        ),
                        vias[i].rt,
                        i,
                    )
                )
                targets = [vias[i] for i in best_f[: int(cfg["shore_targets"])]]

        lens_lo, lens_hi = float(cfg["lens_rt_frac"][0]) * B, float(cfg["lens_rt_frac"][1]) * B
        for F in targets:
            if builder.dijkstras >= max_dijkstras:
                break
            if lens_lo <= F.rt <= lens_hi:
                lens_res = builder.build_lens(F, f"lens {F.key}", dead_end_retry=True)
                if lens_res is not None:
                    raw_built.append((lens_res[0], lens_res[1], f"lens {F.key}", (F.key,)))

            if builder.dijkstras >= max_dijkstras:
                break
            d_fwd = builder.time_dist_from(F.head, lim_s)
            p_fwd = _pick_anchor_partners(vias, F, d_fwd, True, B, cfg)

            if p_fwd and builder.dijkstras < max_dijkstras:
                for route, uturn, a2 in builder.build_triangles(F, p_fwd, retry_after_a1=True):
                    raw_built.append((route, uturn, f"anchor {F.key}->{a2.key}", (F.key, a2.key)))

            if builder.dijkstras >= max_dijkstras:
                break
            d_rev = builder.time_dist_to(F.tail, lim_s)
            p_rev = _pick_anchor_partners(vias, F, d_rev, False, B, cfg)

            for a1 in p_rev:
                if builder.dijkstras >= max_dijkstras:
                    break
                for route, uturn, _ in builder.build_triangles(a1, [F], retry_after_a2=True):
                    raw_built.append((route, uturn, f"anchor {a1.key}->{F.key}", (a1.key, F.key)))

    secs["build"] = round(time.monotonic() - t0, 3)

    # 4.7-4.10 Deduplicate, validate, score, and select
    t0 = time.monotonic()
    s_base = float(cfg["s_base"])
    seen_edges: set[bytes] = set()
    valid_loops: list[LoopRoute] = []
    anchor_mset = set(int(m) for m in anchor.moments) if anchor is not None and len(anchor.moments) else set()

    for route, uturn, origin, vtuple in raw_built:
        ek = np.asarray(route.edges, dtype=np.int64).tobytes()
        if ek in seen_edges:
            continue
        seen_edges.add(ek)
        val = loop_validity(route, cfg)
        if val["tier"] == 0:
            continue
        lr = LoopRoute(
            route=route,
            origin=origin,
            vias=vtuple,
            doubled=val["doubled"],
            net=val["net"],
            stem_frac=val["stem_frac"],
            stem_km=val["stem_km"],
            tier=val["tier"],
            uturn=uturn,
        )
        score_route(lr, ctx, t0=B, s_fast=s_base)
        if mode == "anchor" and anchor is not None:
            if anchor.kind == "point":
                if int(anchor.node) not in set(int(n) for n in route.nodes):
                    continue
            else:
                if not (set(lr.passed) & anchor_mset):
                    continue
        brg, far_km = loop_bearing(ctx.g, route, reach.node)
        lr.bearing = brg
        lr.direction = direction_word(brg) if brg is not None else None
        lr.far_km = far_km
        lr.rank = lr.score
        valid_loops.append(lr)

    kept_count = len(valid_loops)

    if mode == "compass":
        assert req_dir is not None
        tol1 = float(cfg["compass_tol_deg"])
        tol2 = float(cfg["compass_tol_fallback_deg"])
        for lr in valid_loops:
            lr.off_deg = float(angdiff(lr.bearing, theta)) if lr.bearing is not None else 180.0
        acc = [lr for lr in valid_loops if lr.off_deg is not None and lr.off_deg <= tol1 + 1e-9]
        if not acc:
            acc = [lr for lr in valid_loops if lr.off_deg is not None and lr.off_deg <= tol2 + 1e-9]
        if not acc:
            dir_word = DIRECTION_WORDS[req_dir].lower()
            raise LoopError(
                "no_loop_that_way",
                f"No loop fits to the {dir_word} in {min_disp} min. Try a longer drive or another direction.",
            )
        for lr in acc:
            assert lr.off_deg is not None
            lr.rank = lr.score * compass_weight(lr.off_deg, cfg)
        valid_loops = acc
        kept_count = len(valid_loops)
    elif not valid_loops:
        raise LoopError(
            "no_loop",
            f"No loop fits in {min_disp} min from here. Try a longer drive.",
        )

    tier1 = sorted(
        (lr for lr in valid_loops if lr.tier == 1),
        key=lambda lr: (-round(lr.rank, 9), -round(lr.score, 9), lr.route.time_s),
    )
    tier2 = sorted(
        (lr for lr in valid_loops if lr.tier == 2),
        key=lambda lr: (-round(lr.rank, 9), -round(lr.score, 9), lr.route.time_s),
    )
    picks = _select_loops(tier1 + tier2, ctx.g.length_m, norm_steer, cfg)
    if not picks:
        if mode == "compass" and req_dir:
            dir_word = DIRECTION_WORDS[req_dir].lower()
            raise LoopError(
                "no_loop_that_way",
                f"No loop fits to the {dir_word} in {min_disp} min. Try a longer drive or another direction.",
            )
        raise LoopError(
            "no_loop",
            f"No loop fits in {min_disp} min from here. Try a longer drive.",
        )

    _label_picks(picks)

    # For shore anchors, move display point to the nearest used target moment
    if anchor is not None and anchor.kind == "shore" and anchor.requested is not None:
        used_moms = sorted({m for lr in picks for m in lr.passed if m in anchor_mset})
        if used_moms:
            req_lat, req_lon = anchor.requested
            m_arr = np.asarray(used_moms, dtype=np.int64)
            dists_m = np.asarray(
                haversine_m(req_lon, req_lat, ctx.moments.lon[m_arr], ctx.moments.lat[m_arr]),
                dtype=np.float64,
            )
            near_mid = int(m_arr[int(np.argmin(dists_m))])
            anchor.point = (float(ctx.moments.lat[near_mid]), float(ctx.moments.lon[near_mid]))
            anchor.moved_m = float(dists_m[int(np.argmin(dists_m))])

    note = _build_note(picks, norm_steer, anchor, cfg)
    secs["select"] = round(time.monotonic() - t0, 3)
    secs["total"] = round(time.monotonic() - t_total0, 3)

    return LoopResult(
        start=(float(start[0]), float(start[1])),
        node=reach.node,
        snap_m=reach.snap_m,
        budget_s=B,
        steer=norm_steer,
        anchor=anchor,
        s_fast=s_base,
        picks=picks,
        kept=kept_count,
        built=builder.built,
        dijkstras=builder.dijkstras,
        region_arcs=int(len(reach.arcs)),
        seconds=secs,
        note=note,
    )


# --------------------------------------------------------------------------
# Reporting (sections 5 and 6)


def steer_json(steer: Steer, anchor: Anchor | None) -> dict:
    if steer.mode == "compass" and steer.direction:
        return {
            "mode": "compass",
            "direction": steer.direction,
            "bearing": round(direction_bearing(steer.direction), 1),
        }
    if steer.mode == "anchor" and anchor is not None:
        d: dict = {
            "mode": "anchor",
            "kind": anchor.kind,
            "name": anchor.name,
            "point": [round(anchor.point[0], 5), round(anchor.point[1], 5)],
            "requested": (
                [round(anchor.requested[0], 5), round(anchor.requested[1], 5)]
                if anchor.requested is not None
                else None
            ),
            "moved_m": round(anchor.moved_m),
            "round_trip_min": int(math.ceil(anchor.round_trip_s / 60.0)),
        }
        return d
    return {"mode": "any"}


def _describe_loop(ctx: Context, lr: LoopRoute) -> dict:
    d = describe(ctx, lr)
    d.update({
        "label": lr.label,
        "direction": lr.direction,
        "bearing": round(lr.bearing, 1) if lr.bearing is not None else None,
        "off_deg": round(lr.off_deg, 1) if lr.off_deg is not None else None,
        "far_km": round(lr.far_km, 1),
        "doubled": round(lr.doubled, 3),
        "net": round(lr.net, 3),
        "stem_km": round(lr.stem_km, 1),
        "tier": int(lr.tier),
        "uturn": bool(lr.uturn),
    })
    return d


def result_json(ctx: Context, res: LoopResult) -> dict:
    return {
        "mode": "loop",
        "start": [round(res.start[0], 5), round(res.start[1], 5)],
        "snap_m": [round(res.snap_m)],
        "minutes": round(res.budget_s / 60.0, 1),
        "steer": steer_json(res.steer, res.anchor),
        "s_base": round(res.s_fast, 3),
        "kept": res.kept,
        "built": res.built,
        "dijkstras": res.dijkstras,
        "region_arcs": res.region_arcs,
        "seconds": res.seconds,
        "note": res.note,
        "picks": [_describe_loop(ctx, lr) for lr in res.picks],
    }


def geojson(ctx: Context, res: LoopResult) -> dict:
    feats = []
    shown: set[int] = set()
    for rank_i, lr in enumerate(res.picks, 1):
        props = {
            "role": f"loop_{rank_i}",
            **_describe_loop(ctx, lr),
            "edge_path": [int(e) for e in lr.edges],
            "node_path": [int(n) for n in lr.nodes],
        }
        props["roads"] = [list(r) for r in props["roads"]]
        feats.append({
            "type": "Feature",
            "properties": props,
            "geometry": {"type": "LineString", "coordinates": [list(p) for p in lr.route.geometry]},
        })
        shown |= set(lr.moments)

    mom = ctx.moments
    for i in sorted(shown):
        feats.append({
            "type": "Feature",
            "properties": {
                "role": "moment",
                "id": int(i),
                "type": str(mom.type[i]),
                "m": round(float(mom.m[i]), 2),
                "label": str(mom.label[i]),
            },
            "geometry": {"type": "Point", "coordinates": [float(mom.lon[i]), float(mom.lat[i])]},
        })

    feats.append({
        "type": "Feature",
        "properties": {"role": "start", "snap_m": round(res.snap_m)},
        "geometry": {"type": "Point", "coordinates": [float(res.start[1]), float(res.start[0])]},
    })
    if res.anchor is not None:
        feats.append({
            "type": "Feature",
            "properties": {
                "role": "anchor",
                "kind": res.anchor.kind,
                "name": res.anchor.name,
                "moved_m": round(res.anchor.moved_m),
            },
            "geometry": {
                "type": "Point",
                "coordinates": [float(res.anchor.point[1]), float(res.anchor.point[0])],
            },
        })
    return {"type": "FeatureCollection", "features": feats}


def format_table(ctx: Context, res: LoopResult) -> str:
    lines = [
        f"budget {res.budget_s / 60:.0f} min, s_base {res.s_fast:.2f}; "
        f"{res.kept} valid of {res.built} built ({res.region_arcs:,} region arcs, "
        f"{res.dijkstras} Dijkstras); search {res.seconds['total']:.1f} s"
    ]
    if res.note:
        lines.append(f"note: {res.note}")
    lines.append(
        f"{'loop':18s} {'min':>6s} {'km':>6s} {'scenic':>7s} {'mom':>4s} "
        f"{'score':>6s} {'net':>5s} {'stem':>6s}  roads"
    )
    for lr in res.picks:
        roads = ", ".join(f"{lab} {km:.0f} km" for lab, km in main_roads(ctx.g, lr.route))
        lines.append(
            f"{lr.label:18s} {lr.minutes:6.1f} {lr.km:6.1f} {lr.scenic_min:7.1f} "
            f"{len(lr.moments):4d} {lr.score:6.1f} {lr.net:5.2f} {lr.stem_km:5.1f}k  {roads}"
        )
        for i in lr.moments:
            lines.append(f"{'':20s}- {ctx.moments.type[i]} {ctx.moments.m[i]:.2f}: {ctx.moments.label[i]}")
        lines.append(f"{'':20s}({lr.origin})")
    return "\n".join(lines)


def plot_loops(ctx: Context, res: LoopResult, path: Path, title: str | None = None) -> Path:
    """PNG map of the picked loops, start marker, optional anchor, and credited moments."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.patheffects as pe
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection

    g = ctx.g
    pts_list = [np.asarray(lr.route.geometry) for lr in res.picks]
    pts_list.append(np.asarray([[res.start[1], res.start[0]]]))
    if res.anchor is not None:
        pts_list.append(np.asarray([[res.anchor.point[1], res.anchor.point[0]]]))
    pts = np.concatenate(pts_list)
    lo0, la0 = pts.min(0)
    lo1, la1 = pts.max(0)
    pad_lo, pad_la = 0.06 * (lo1 - lo0) + 0.02, 0.06 * (la1 - la0) + 0.02
    lo0, lo1, la0, la1 = lo0 - pad_lo, lo1 + pad_lo, la0 - pad_la, la1 + pad_la
    aspect = 1 / np.cos(np.radians(0.5 * (la0 + la1)))
    map_w = 8.4
    map_h = float(np.clip(map_w * aspect * (la1 - la0) / max(lo1 - lo0, 1e-6), 3.5, 10.0))
    fig_w, fig_h = map_w + 4.6, max(map_h, 5.5) + 1.0
    fig = plt.figure(figsize=(fig_w, fig_h), dpi=150, facecolor=_SURFACE)
    ax = fig.add_axes(
        [0.25 / fig_w, (fig_h - 0.6 - map_h) / fig_h, map_w / fig_w, map_h / fig_h],
        facecolor=_SURFACE,
    )
    side = fig.add_axes(
        [(map_w + 0.45) / fig_w, 0.4 / fig_h, 4.0 / fig_w, (fig_h - 1.0) / fig_h],
        facecolor=_SURFACE,
    )
    side.axis("off")

    if g.edges_table is not None and g.geom_off is not None:
        hw = np.asarray(g.edges_table.column("highway").to_pylist(), dtype=object)
        keep = np.isin(hw, ["motorway", "trunk", "primary", "secondary"])
        ulo, ula = g.node_lon[g.u], g.node_lat[g.u]
        keep &= (ulo > lo0) & (ulo < lo1) & (ula > la0) & (ula < la1)
        segs = []
        for e in np.flatnonzero(keep):
            a, b = g.geom_off[e], g.geom_off[e + 1]
            segs.append(np.column_stack([g.geom_lon[a:b], g.geom_lat[a:b]]))
        ax.add_collection(LineCollection(segs, colors=_MUTED, linewidths=0.4, zorder=1))

    handles = [None] * len(res.picks)
    widths = [4.6, 3.0, 1.8]
    for k in range(len(res.picks) - 1, -1, -1):
        lr = res.picks[k]
        xy = np.asarray(lr.route.geometry)
        colour = _PICK_COLOURS[k % len(_PICK_COLOURS)]
        (h,) = ax.plot(
            xy[:, 0],
            xy[:, 1],
            color=colour,
            lw=widths[min(k, len(widths) - 1)],
            zorder=3 + (len(res.picks) - k),
            solid_capstyle="round",
            solid_joinstyle="round",
            label=f"{lr.label:16s}{lr.minutes:4.0f} min  {lr.km:4.0f} km  {lr.scenic_min:3.0f} scenic min",
        )
        handles[k] = h

    sx, sy = g.node_lon[res.node], g.node_lat[res.node]
    ax.plot(sx, sy, "o", ms=9, color=_INK, mec=_SURFACE, mew=2, zorder=10)
    ax.annotate("S", (sx, sy), xytext=(7, 7), textcoords="offset points", fontsize=11, weight="bold", color=_INK, zorder=11)

    if res.anchor is not None:
        ax_lon, ax_lat = res.anchor.point[1], res.anchor.point[0]
        ax.plot(ax_lon, ax_lat, "D", ms=8, color="#b8421f", mec=_SURFACE, mew=1.5, zorder=10)
        ax.annotate("P", (ax_lon, ax_lat), xytext=(7, -11), textcoords="offset points", fontsize=10, weight="bold", color="#b8421f", zorder=11)

    mom = ctx.moments
    shown: list[int] = []
    for lr in res.picks:
        for i in lr.moments:
            if i not in shown:
                shown.append(i)
    for n, i in enumerate(shown, 1):
        ax.plot(mom.lon[i], mom.lat[i], "o", ms=7, color=_INK, mec=_SURFACE, mew=1.5, zorder=12)
        off = ((6, 4), (6, -11), (-14, 4), (-14, -11))[n % 4]
        ax.annotate(
            str(n),
            (mom.lon[i], mom.lat[i]),
            xytext=off,
            textcoords="offset points",
            fontsize=8,
            color=_INK,
            zorder=13,
            path_effects=[pe.withStroke(linewidth=2.5, foreground=_SURFACE)],
        )

    ax.set_xlim(lo0, lo1)
    ax.set_ylim(la0, la1)
    ax.set_aspect(aspect)
    ax.set_xticks([])
    ax.set_yticks([])
    for s in ax.spines.values():
        s.set_color(_MUTED)
    fig.text(
        0.25 / fig_w,
        1 - 0.35 / fig_h,
        title or f"{res.budget_s / 60:.0f}-minute scenic loops ({res.steer.mode})",
        fontsize=13,
        color=_INK,
        weight="bold",
        va="center",
    )
    leg = side.legend(
        handles=handles,
        loc="upper left",
        bbox_to_anchor=(0, 1),
        fontsize=8,
        frameon=False,
        labelcolor=_INK,
        prop={"family": "monospace", "size": 7.5},
        handlelength=2.2,
    )
    leg._legend_box.align = "left"

    lines = ["Moments credited to the loops", ""]
    for n, i in enumerate(shown, 1):
        who = ",".join(lr.label for lr in res.picks if i in lr.moments)
        lab = str(mom.label[i])
        lab = lab if len(lab) <= 46 else lab[:45] + "..."
        lines.append(f"{n:2d}  {lab}")
        lines.append(f"    {mom.type[i]} {mom.m[i]:.2f}  ({who})")
    side.text(0, 0.80, "\n".join(lines[:60]), va="top", ha="left", fontsize=7.5, family="monospace", color=_INK)
    fig.text(
        0.25 / fig_w,
        0.15 / fig_h,
        "Thin grey: motorway to secondary roads. S: start/finish. Dots: credited moments.",
        fontsize=8,
        color=_INK2,
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=_SURFACE)
    plt.close(fig)
    return path
