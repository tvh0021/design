"""Turn a target route into Google Maps waypoints (PLAN.md 2.4).

Our own fastest router (`RoadGraph`, travel time) stands in for Google. The
reduction is greedy farthest reach: from the current anchor (the origin, then
each waypoint), find the farthest node of the target route that the fastest
route from the anchor still reaches along the target, and put the next
waypoint just before it. One Dijkstra per anchor gives the shortest-path tree
to every node, so each "does the fastest route follow?" check is a walk up
that tree, and a binary search over the target's nodes finds the farthest
point. Phase 0 is the reason to check every gap: with 5 waypoints Google left
5&20 west of Geneva for a faster parallel road (docs/phase0-results.md).

A waypoint sits mid-edge, where Google can only snap it to the intended road:
at least `JUNCTION_CLEAR_M` from a junction where possible, and with no other
drivable road within `PARALLEL_M` (the road's own continuation and its other
carriageway do not count). If the farthest spot fails, the search slides back
along the route until one passes.

A two-way road can be entered or left either way, so each leg is routed over
all four combinations (leave the previous waypoint forward or by turning
round, arrive from behind or from ahead). A spot where the fastest
combination turns round, or reaches the waypoint from the far side, is
rejected: Google would make the same U-turn detour.

Sliding back can leave the greedy pass with waypoints a later one makes
redundant, so a dynamic program over the greedy waypoints then keeps the
subset that best reproduces the target per waypoint used, at most
`max_waypoints` of them. When more are needed, it keeps the ones whose removal
loses the most overlap.

Overlap is by length over directed node pairs: shared length divided by the
longer of the target and the stitched route, so both skipped stretches and
added detours count against it.
"""

from __future__ import annotations

import math
import weakref
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

from .graph import EARTH_RADIUS_M
from .route import MIN_COST

LEG_FOLLOW = 0.98        # a leg follows if this share of it (by length) is on the target
LEG_SLACK_M = 100.0      # ... or it misses at most this much (keeps short legs from failing on one turn)
JUNCTION_CLEAR_M = 150.0  # straight-line distance from a waypoint to the nearest junction, where possible
PARALLEL_M = 50.0        # no other drivable road this close to a waypoint
CARRIAGEWAY_M = 25.0     # on a divided road, the other carriageway at least this far away where possible
SAMPLE_STEP_M = 20.0     # spacing of candidate spots along the route
MIN_END_M = 10.0         # a waypoint stays at least this far from either end of its edge
ADJACENT_M = 150.0       # target edges this close along the route are the same road, not a parallel one
INDEX_STEP_M = 40.0      # sample spacing of the edge-geometry search index
MAX_URL = 2048
COST_EPS = 1e-6
UTURN_MARGIN_S = 30.0    # a turning-round alternative must be at least this much slower ...
UTURN_MARGIN = 0.05      # ... and this share of the leg, so Google's different timings don't flip it
RISK_PENALTY_M = 500.0   # pruning: a leg with a U-turn risk counts as this much off the target
UTURN_PENALTY_M = 5_000.0  # pruning: a leg that actually turns round or arrives from the far side
WAYPOINT_COST_M = 50.0   # pruning: a waypoint is worth keeping if it saves more than this off the target
TIME_MARGIN = 0.1        # default: place waypoints as if the target's own roads were 10% slower
MAX_GAP_M = 15_000.0     # default: at most this much scenic road between two waypoints
EXPOSURE_WEIGHT = 0.5    # pruning: cost per metre of scenic road a leg leaves exposed, vs 1 per metre missed
ROW_SLACK_S = 300.0      # Dijkstra rows search this much past what a leg check can use
GAP_TOL_M = 2 * MIN_END_M  # a leg may run this far over max_gap_m (waypoints keep MIN_END_M off edge ends)
HORIZON_GAPS = 4         # pruning joins legs over at most this many max_gap_m of scenic road
# Google Maps' URL parser accepts up to 9 intermediate waypoints (11 stops),
# whereas its desktop Directions UI counts stop boxes (origin + waypoints +
# destination, even when origin == destination on a closed loop) and locks route
# editing at 10 stops ("Maximum number of destinations reached" / "Route cannot
# be modified further"). Keeping at most 9 stops (7 intermediate waypoints) per
# link leaves every generated link editable.
EDITABLE_STOPS = 9

_INDEX: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


# --------------------------------------------------------------------------
# Results


@dataclass
class Waypoint:
    lat: float  # rounded to 5 decimals, as it goes in the URL
    lon: float
    edge: int  # edge index in the graph
    index: int  # position of that edge in the target's edge path
    offset_m: float  # distance along the edge, in the target's direction of travel
    road: str
    clearance_m: float  # straight line to the nearest junction
    nearest_other_m: float  # nearest other drivable road (inf if none within the search radius)
    tier: int  # 1 all rules met, 2 near a junction or the other carriageway, 3 another road close by,
    #            4 no spot passes the leg check
    divided: bool  # on a one-way carriageway with its other carriageway within PARALLEL_M
    carriageway_m: float = math.inf  # distance to that other carriageway

    @property
    def latlon(self) -> tuple[float, float]:
        return (self.lat, self.lon)


@dataclass
class Leg:
    our_s: float  # fastest time through the waypoints at either end
    target_s: float  # the target route's time over the same stretch
    our_m: float
    target_m: float
    overlap: float
    uturn: bool  # the fastest way turns round at a waypoint or arrives from the far side
    uturn_risk: bool = False  # a turning-round way is within the U-turn margin of the straight one
    scenic_m: float = 0.0  # scenic road on the target between the two waypoints (see `max_gap_m`)
    margin_skip_m: float = 0.0  # scenic road the fastest way skips with the target's roads `time_margin` slower
    at_risk: bool = False  # scenic_m over max_gap_m, or margin_skip_m over LEG_SLACK_M
    # scenic road a router that rates roads differently could skip here: the
    # larger of margin_skip_m and the scenic road beyond max_gap_m
    exposure_m: float = 0.0


@dataclass
class WaypointPart:
    """One editable Google Maps link in a multi-link split of a long route."""

    part: int  # 1-based part number
    origin: tuple[float, float]
    destination: tuple[float, float]
    waypoints: list[Waypoint]
    first_n: int  # 1-based index of first intermediate waypoint in the full chain (0 if none)
    last_n: int   # 1-based index of last intermediate waypoint in the full chain (0 if none)
    handoff_n: int | None  # 1-based index in the full chain of the split waypoint where this part ends
    handoff_road: str | None
    eta_s: float
    length_m: float

    @property
    def url(self) -> str:
        return maps_url(self.origin, self.destination, [w.latlon for w in self.waypoints])


@dataclass
class WaypointResult:
    waypoints: list[Waypoint]
    origin: tuple[float, float]  # (lat, lon) of the target's first node
    destination: tuple[float, float]
    overlap: float  # stitched fastest legs vs the target, by length
    eta_s: float  # stitched fastest legs
    target_eta_s: float
    stitched_m: float
    target_m: float
    needed: int  # waypoints needed without the cap (greedy pass, then pruned; at least what the spacing rule needs)
    capped: bool
    follow_target: float
    legs: list[Leg] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    time_margin: float = 0.0
    max_gap_m: float = math.inf
    scenic_m: float = 0.0  # target road the spacing rule applies to

    @property
    def risk_legs(self) -> list[int]:
        """Indices of legs at risk, most scenic road exposed first."""
        return sorted((i for i, leg in enumerate(self.legs) if leg.at_risk),
                      key=lambda i: -self.legs[i].exposure_m)

    @property
    def count(self) -> int:
        return len(self.waypoints)

    @property
    def meets_target(self) -> bool:
        return self.overlap >= self.follow_target

    @property
    def url(self) -> str:
        return maps_url(self.origin, self.destination, [w.latlon for w in self.waypoints])

    def summary(self) -> dict:
        return {
            "waypoints": self.count, "needed": self.needed, "capped": self.capped,
            "overlap": round(self.overlap, 4), "meets_target": bool(self.meets_target),
            "eta_min": round(self.eta_s / 60, 1), "target_eta_min": round(self.target_eta_s / 60, 1),
            "eta_ratio": round(self.eta_s / self.target_eta_s, 4) if self.target_eta_s else None,
            "km": round(self.stitched_m / 1000, 2), "target_km": round(self.target_m / 1000, 2),
            "uturn_legs": int(sum(leg.uturn for leg in self.legs)),
            "weak_waypoints": sum(w.tier > 1 for w in self.waypoints),
            "risk_legs": len(self.risk_legs),
            "max_scenic_gap_km": round(max((leg.scenic_m for leg in self.legs), default=0.0) / 1000, 2),
            "scenic_km": round(self.scenic_m / 1000, 2),
            "time_margin": self.time_margin,
        }


def editable_waypoint_cap(closed: bool = False) -> int:
    """Most intermediate waypoints per link (7 = 9 stops minus origin and
    destination) that keep Google Maps editable on both A-to-B routes and closed
    loops."""
    _ = closed
    return EDITABLE_STOPS - 2


def uturn_margin(leg_s: float) -> float:
    return max(UTURN_MARGIN_S, UTURN_MARGIN * leg_s)


def _fmt(p: tuple[float, float]) -> str:
    return f"{p[0]:.5f},{p[1]:.5f}"


def maps_url(origin: tuple[float, float], destination: tuple[float, float],
             waypoints: Sequence[tuple[float, float]] = ()) -> str:
    """Google Maps directions link (Maps URLs, api=1). Points are (lat, lon),
    written with 5 decimals; waypoints are separated by an encoded pipe."""
    url = (f"https://www.google.com/maps/dir/?api=1&origin={_fmt(origin)}"
           f"&destination={_fmt(destination)}&travelmode=driving")
    if len(waypoints):
        url += "&waypoints=" + "%7C".join(_fmt(w) for w in waypoints)
    assert len(url) < MAX_URL, f"Maps URL is {len(url)} characters (limit {MAX_URL})"
    return url


# --------------------------------------------------------------------------
# Spatial helpers, built once per graph


class _Index:
    def __init__(self, g):
        self.g = g
        self.lat0 = float(np.radians(g.node_lat.mean())) if g.n_nodes else 0.0
        self.coslat0 = math.cos(self.lat0)

        # Junctions: nodes with three or more distinct neighbours.
        a = np.minimum(g.u, g.v)
        b = np.maximum(g.u, g.v)
        keep = a != b
        pairs = np.unique(np.stack([a[keep], b[keep]], axis=1), axis=0) if keep.any() else np.zeros((0, 2), int)
        deg = np.bincount(pairs.ravel(), minlength=g.n_nodes)
        self.junction = deg >= 3
        jn = np.flatnonzero(self.junction)
        self.junction_tree = cKDTree(self.xy(g.node_lon[jn], g.node_lat[jn])) if len(jn) else None

        # Edge polylines (u -> v), flat.
        if g.geom_off is not None:
            self.off = np.asarray(g.geom_off, dtype=np.int64)
            self.plon = np.asarray(g.geom_lon, dtype=np.float64)
            self.plat = np.asarray(g.geom_lat, dtype=np.float64)
        else:
            self.off = np.arange(g.n_edges + 1, dtype=np.int64) * 2
            self.plon = np.column_stack([g.node_lon[g.u], g.node_lon[g.v]]).ravel()
            self.plat = np.column_stack([g.node_lat[g.u], g.node_lat[g.v]]).ravel()
        self.pxy = self.xy(self.plon, self.plat)
        n_pts = len(self.plon)
        edge_of_pt = np.repeat(np.arange(g.n_edges), np.diff(self.off))
        seg_ok = edge_of_pt[:-1] == edge_of_pt[1:]
        seg_len = np.where(seg_ok, np.hypot(*(self.pxy[1:] - self.pxy[:-1]).T), 0.0)
        cs = np.concatenate([[0.0], np.cumsum(seg_len)])
        self.cum = cs[:n_pts] - cs[self.off[edge_of_pt]]  # distance along its edge at each point
        self.proj_len = cs[self.off[1:] - 1] - cs[self.off[:-1]]

        # Search index: samples along every segment, at most INDEX_STEP_M apart.
        self.seg = np.flatnonzero(seg_ok)  # segment i runs from point i to point i + 1
        self.seg_edge = edge_of_pt[self.seg]
        k = np.maximum(1, np.ceil(seg_len[self.seg] / INDEX_STEP_M).astype(np.int64))
        sid = np.repeat(np.arange(len(self.seg)), k)
        first = np.concatenate([[0], np.cumsum(k)[:-1]])
        frac = (np.arange(len(sid)) - first[sid] + 0.5) / k[sid]
        p0 = self.pxy[self.seg[sid]]
        p1 = self.pxy[self.seg[sid] + 1]
        self.sample_seg = sid
        self.sample_tree = cKDTree(p0 + (p1 - p0) * frac[:, None])
        self.sample_half = float(INDEX_STEP_M / 2)

        # Arcs by (tail, head) pair, for patching a few matrix entries.
        arc_key = g.arc_tail * g.n_nodes + g.arc_head
        self.arc_order = np.argsort(arc_key, kind="stable")
        self.arc_key_sorted = arc_key[self.arc_order]

        t = g.edges_table
        names = set(t.column_names) if t is not None else set()
        self.way_id = (t.column("way_id").to_numpy() if "way_id" in names
                       else np.arange(g.n_edges, dtype=np.int64))
        self._cols = {c: t.column(c) for c in ("name", "ref", "highway") if c in names}

    def xy(self, lon, lat) -> np.ndarray:
        lon = np.radians(np.asarray(lon, dtype=np.float64))
        lat = np.radians(np.asarray(lat, dtype=np.float64))
        return np.column_stack([EARTH_RADIUS_M * lon * self.coslat0, EARTH_RADIUS_M * lat])

    def attr(self, col: str, e: int) -> str:
        c = self._cols.get(col)
        if c is None:
            return ""
        return c[int(e)].as_py() or ""

    def road(self, e: int) -> str:
        return self.attr("ref", e) or self.attr("name", e) or self.attr("highway", e) or f"edge {e}"

    def is_link(self, e: int) -> bool:
        return self.attr("highway", e).endswith("_link")

    # -- points on edges --

    def point_on_edge(self, e: int, a_m: float) -> tuple[np.ndarray, float, float, float]:
        """Point `a_m` (length_m units) from u along edge e: (xy, lon, lat, bearing)."""
        lo, hi = int(self.off[e]), int(self.off[e + 1])
        L = float(self.g.length_m[e])
        d = self.proj_len[e] * (a_m / L) if L > 0 else 0.0
        cum = self.cum[lo:hi]
        i = int(np.clip(np.searchsorted(cum, d, side="right") - 1, 0, max(hi - lo - 2, 0)))
        if hi - lo < 2:
            return self.pxy[lo], float(self.plon[lo]), float(self.plat[lo]), 0.0
        seg = cum[i + 1] - cum[i]
        f = 0.0 if seg <= 0 else float(np.clip((d - cum[i]) / seg, 0.0, 1.0))
        p0, p1 = self.pxy[lo + i], self.pxy[lo + i + 1]
        lon = self.plon[lo + i] + f * (self.plon[lo + i + 1] - self.plon[lo + i])
        lat = self.plat[lo + i] + f * (self.plat[lo + i + 1] - self.plat[lo + i])
        brg = math.degrees(math.atan2(p1[0] - p0[0], p1[1] - p0[1]))
        return p0 + f * (p1 - p0), float(lon), float(lat), brg

    def _segments_near(self, p: np.ndarray, r: float):
        """(segment ids, distance, bearing, along-edge distance of the foot) within r of p."""
        ids = self.sample_tree.query_ball_point(p, r + self.sample_half)
        if not ids:
            return np.zeros(0, np.int64), np.zeros(0), np.zeros(0), np.zeros(0)
        s = np.unique(self.sample_seg[np.asarray(ids)])
        i = self.seg[s]
        a, b = self.pxy[i], self.pxy[i + 1]
        ab = b - a
        L2 = np.maximum((ab ** 2).sum(1), 1e-12)
        t = np.clip(((p - a) * ab).sum(1) / L2, 0.0, 1.0)
        foot = a + ab * t[:, None]
        d = np.hypot(*(p - foot).T)
        brg = np.degrees(np.arctan2(ab[:, 0], ab[:, 1]))
        along = self.cum[i] + t * np.sqrt(L2)
        return s, d, brg, along

    def snap(self, lat: float, lon: float, r: float = 200.0) -> tuple[int, float, float] | None:
        """Nearest edge to a point: (edge, distance from u in length_m units, metres off)."""
        p = self.xy([lon], [lat])[0]
        s, d, _, along = self._segments_near(p, r)
        if not len(s):
            return None
        k = int(np.argmin(d))
        e = int(self.seg_edge[s[k]])
        scale = self.g.length_m[e] / self.proj_len[e] if self.proj_len[e] > 0 else 0.0
        return e, float(along[k] * scale), float(d[k])

    def clearance(self, p: np.ndarray) -> float:
        if self.junction_tree is None:
            return math.inf
        d, _ = self.junction_tree.query(p)
        return float(d)

    def others_near(self, p: np.ndarray, e: int, brg: float, skip: set[int]) -> tuple[float, bool]:
        """Nearest other drivable edge within PARALLEL_M of p, and the
        distance to edge e's other carriageway (inf if not that close). Edges in `skip`, edges of the
        same OSM way and the other carriageway don't count."""
        s, d, sb, _ = self._segments_near(p, PARALLEL_M)
        best, carriageway = math.inf, math.inf
        if not len(s):
            return best, carriageway
        g = self.g
        for k in np.argsort(d):
            if d[k] > PARALLEL_M:
                break
            o = int(self.seg_edge[s[k]])
            if o == e or o in skip or self.way_id[o] == self.way_id[e]:
                continue
            if g.oneway[e] and g.oneway[o]:
                diff = abs((sb[k] - brg + 180.0) % 360.0 - 180.0)
                same_road = any(self.attr(c, o) and self.attr(c, o) == self.attr(c, e) for c in ("ref", "name"))
                if diff > 135.0 and same_road:
                    carriageway = min(carriageway, float(d[k]))
                    continue
            best = min(best, float(d[k]))
        return best, carriageway


def _index(g) -> _Index:
    ix = _INDEX.get(g)
    if ix is None:
        ix = _INDEX[g] = _Index(g)
    return ix


# --------------------------------------------------------------------------
# Target route


class _Target:
    """The route to reproduce: nodes N[0..n], edges E[0..n-1], E[j] driven N[j] -> N[j+1]."""

    def __init__(self, g, nodes: np.ndarray, edges: np.ndarray):
        self.nodes = np.asarray(nodes, dtype=np.int64)
        self.edges = np.asarray(edges, dtype=np.int64)
        self.n = len(self.edges)
        self.length = g.length_m[self.edges].astype(np.float64)
        self.time = g.time_s[self.edges].astype(np.float64)
        self.rev = g.u[self.edges] != self.nodes[:-1]
        # pieces are keyed by directed edge, (tail, head, edge index), so two
        # parallel edges between the same junctions never count as one road
        self.keys = list(zip(self.nodes[:-1].tolist(), self.nodes[1:].tolist(), self.edges.tolist()))
        self.cum_len = np.concatenate([[0.0], np.cumsum(self.length)])
        self.cum_time = np.concatenate([[0.0], np.cumsum(self.time)])
        self.set_scenic(np.zeros(self.n, bool))

    def set_scenic(self, mask: np.ndarray) -> None:
        """Mark the target edges the spacing rule applies to."""
        self.scenic = np.asarray(mask, dtype=bool)
        self.scenic_edges = set(self.edges[self.scenic].tolist())
        self.cum_scen = np.concatenate([[0.0], np.cumsum(np.where(self.scenic, self.length, 0.0))])

    def scen_at(self, pos) -> float:
        """Scenic metres on the target from its start to position pos."""
        j, s = pos
        return float(self.cum_scen[j] + (s if self.scenic[j] else 0.0))

    def scen_limit(self, pos, gap: float) -> tuple[int, float] | None:
        """Farthest position with at most `gap` scenic metres after pos, or
        None if the rest of the route has no more than that."""
        lim = self.scen_at(pos) + gap
        k = int(np.searchsorted(self.cum_scen, lim, side="right")) - 1
        if k >= self.n:
            return None
        return k, float(lim - self.cum_scen[k])  # edge k is scenic: cum_scen rises across it

    def pieces(self, a: tuple[int, float], b: tuple[int, float]) -> list[tuple[tuple[int, int], float]]:
        """Target pieces from position a to b; a position is (edge index, metres along it)."""
        (ja, sa), (jb, sb) = a, b
        if ja == jb:
            return [(self.keys[ja], sb - sa)] if sb > sa else []
        out = [(self.keys[ja], self.length[ja] - sa)]
        out.extend((self.keys[j], self.length[j]) for j in range(ja + 1, jb))
        out.append((self.keys[jb], sb))
        return out

    def time_between(self, a, b) -> float:
        (ja, sa), (jb, sb) = a, b
        fa = sa / self.length[ja] if self.length[ja] > 0 else 0.0
        fb = sb / self.length[jb] if self.length[jb] > 0 else 0.0
        return float(self.cum_time[jb] + fb * self.time[jb] - self.cum_time[ja] - fa * self.time[ja])

    def pos_m(self, pos) -> float:
        return float(self.cum_len[pos[0]] + pos[1])


def _arc_lookup(g, cost=None, matrix=None):
    m, sel, indptr = matrix if matrix is not None else g._matrix(g.time_s if cost is None else cost)
    heads = m.indices

    def edge(a: int, b: int) -> int | None:
        lo, hi = indptr[a], indptr[a + 1]
        k = lo + np.searchsorted(heads[lo:hi], b)
        if k < hi and heads[k] == b:
            return int(g.arc_edge[sel[k]])
        return None
    return m, edge


def _entry_keys(m) -> np.ndarray:
    """tail * n + head for every stored entry; sorted, since rows are in
    order and heads are sorted within a row."""
    n = m.shape[0]
    return np.repeat(np.arange(n, dtype=np.int64), np.diff(m.indptr)) * n + m.indices


def _slowed_matrix(g, edges: np.ndarray, factor: float, base=None):
    """The travel-time matrix (or `base`, a subset of it) with `edges`
    `factor` times slower. Only the entries those edges touch are
    recomputed (the cheapest arc per pair, as in RoadGraph._matrix), which
    is much faster than a rebuild."""
    m, sel, indptr = base if base is not None else g._matrix(g.time_s)
    n = g.n_nodes
    ix = _index(g)
    edges = np.asarray(edges, dtype=np.int64)
    cost = np.array(g.time_s, dtype=np.float64)
    cost[edges] *= factor
    u, v = g.u[edges], g.v[edges]
    two = ~g.oneway[edges]
    touched = np.unique(np.concatenate([u * n + v, (v * n + u)[two]]))
    lo = np.searchsorted(ix.arc_key_sorted, touched, side="left")
    cnt = np.searchsorted(ix.arc_key_sorted, touched, side="right") - lo
    at = np.repeat(lo - np.cumsum(cnt) + cnt, cnt) + np.arange(cnt.sum())
    par = ix.arc_order[at]  # every arc between a touched pair (self-loops have none)
    pkey = ix.arc_key_sorted[at]
    c = np.maximum(cost[g.arc_edge[par]], MIN_COST)
    order = np.lexsort((c, pkey))
    k = pkey[order]
    first = np.ones(len(order), bool)
    first[1:] = k[1:] != k[:-1]
    best = par[order[first]]
    bkey = k[first]
    keys = _entry_keys(m)
    pos = np.minimum(np.searchsorted(keys, bkey), len(keys) - 1)
    ok = keys[pos] == bkey  # pairs outside a corridor `base` are not stored
    data, sel2 = m.data.copy(), sel.copy()
    data[pos[ok]] = c[order[first]][ok]
    sel2[pos[ok]] = best[ok]
    return csr_matrix((data, m.indices, m.indptr), shape=m.shape), sel2, indptr


def _corridor(g, target: "_Target", time_margin: float):
    """The travel-time matrix cut to the nodes any leg check can use.

    A leg from A to B only looks at ways that cost at most (1 + UTURN_MARGIN)
    times the target's own way plus UTURN_MARGIN_S; every node n on one has
    d(origin, n) + d(n, destination) within that of the whole target, under
    either cost (the margin only makes target edges slower). Two Dijkstras
    find those nodes, and every later Dijkstra runs on them alone.

    Returns (matrix, arc per entry, indptr) and each node's travel time to
    the destination (0 outside the corridor), the potential that lets
    `_Reducer.row` search toward the destination only."""
    m, sel, indptr = g._matrix(g.time_s)
    o, d = int(target.nodes[0]), int(target.nodes[-1])
    bound = ((1 + UTURN_MARGIN) * (1 + time_margin) * float(target.cum_time[-1])
             + UTURN_MARGIN_S + 2 * ROW_SLACK_S)
    d_o = dijkstra(m, directed=True, indices=o, limit=bound)
    d_d = dijkstra(m.T.tocsr(), directed=True, indices=d, limit=bound)
    region = d_o + d_d <= bound
    region[target.nodes] = True
    rows = np.repeat(np.arange(g.n_nodes, dtype=np.int64), np.diff(m.indptr))
    keep = region[rows] & region[m.indices]
    ip = np.concatenate([[0], np.cumsum(np.bincount(rows[keep], minlength=g.n_nodes))])
    pot = np.where(region & np.isfinite(d_d), d_d, 0.0)
    return (csr_matrix((m.data[keep], m.indices[keep], ip), shape=m.shape), sel[keep], ip), pot


def _target_from(g, path, edge_path) -> _Target:
    _, edge_of = _arc_lookup(g)
    if edge_path is not None:
        edges = np.asarray(edge_path, dtype=np.int64)
        if not len(edges):
            raise ValueError("empty edge path")
        if len(edges) == 1:
            start = int(g.u[edges[0]])
        else:
            e0, e1 = edges[0], edges[1]
            start = int(g.v[e0]) if g.v[e0] not in (g.u[e1], g.v[e1]) else int(g.u[e0])
        nodes = [start]
        for e in edges:
            nodes.append(int(g.v[e]) if g.u[e] == nodes[-1] else int(g.u[e]))
            if nodes[-2] not in (g.u[e], g.v[e]):
                raise ValueError(f"edge path is not continuous at edge {e}")
        return _Target(g, np.asarray(nodes), edges)
    if hasattr(path, "nodes") and hasattr(path, "edges"):
        return _Target(g, np.asarray(path.nodes), np.asarray(path.edges))
    nodes = np.asarray(path, dtype=np.int64)
    if len(nodes) < 2:
        raise ValueError("a target route needs at least two nodes")
    edges = []
    for a, b in zip(nodes[:-1].tolist(), nodes[1:].tolist()):
        e = edge_of(a, b)
        if e is None:
            raise ValueError(f"no drivable arc {a} -> {b} in the node path")
        edges.append(e)
    return _Target(g, nodes, np.asarray(edges))


# --------------------------------------------------------------------------
# Locations and legs


@dataclass
class _Loc:
    pos: tuple[int, float]  # position on the target
    exits: list  # (node, cost_s, pieces, backward)
    entries: list


@dataclass
class _LegEval:
    cost: float
    pieces: list  # (key, metres, edge or -1)
    exit_back: bool
    entry_back: bool
    shared: float
    target_m: float
    path_m: float
    risk: bool = False
    skipped_scenic: float = 0.0  # target scenic metres this leg does not drive

    @property
    def miss(self) -> float:
        return max(self.target_m, self.path_m) - self.shared

    @property
    def overlap(self) -> float:
        d = max(self.target_m, self.path_m)
        return self.shared / d if d > 0 else 1.0


class _Reducer:
    """Leg checks under one per-edge cost: travel time, or travel time with
    the target's own edges made slower (see `time_margin`)."""

    def __init__(self, g, target: _Target, leg_follow: float, leg_slack_m: float, cost=None,
                 matrix=None, max_gap_m: float = math.inf, potential=None):
        self.g = g
        self.t = target
        self.ix = _index(g)
        self.cost = g.time_s if cost is None else cost
        self.mat = matrix if matrix is not None else g._matrix(self.cost)
        self.m, self.edge_of = _arc_lookup(g, matrix=self.mat)
        self.keys = _entry_keys(self.m)
        # Goal-directed search (A* by reweighting): with a potential pi that
        # never overestimates the time left to the destination, arcs cost
        # c + pi(head) - pi(tail) >= 0 and the same paths are shortest, but a
        # limited Dijkstra reaches only nodes on the way to the destination.
        self.pot = potential
        if potential is not None:
            heads = self.m.indices
            tails = np.repeat(np.arange(g.n_nodes, dtype=np.int64), np.diff(self.m.indptr))
            red = np.maximum(self.m.data + potential[heads] - potential[tails], MIN_COST)
            self.m_search = csr_matrix((red, heads, self.m.indptr), shape=self.m.shape)
        else:
            self.m_search = self.m
        self.leg_follow = leg_follow
        self.leg_slack_m = leg_slack_m
        self.max_gap_m = max_gap_m
        self.place_gap_m = max_gap_m  # spacing the greedy pass uses (see reduce_to_waypoints)
        self.horizon_m = math.inf  # pruning only joins legs over at most this much scenic road
        tc = self.cost[target.edges].astype(np.float64)
        self.cum_cost = np.concatenate([[0.0], np.cumsum(tc)])
        self.rows: dict[int, tuple[np.ndarray, np.ndarray, float]] = {}
        self.dijkstras = 0
        t = target
        self.origin = _Loc((0, 0.0), [(int(t.nodes[0]), 0.0, [], False)], [(int(t.nodes[0]), 0.0, [], False)])
        self.dest = _Loc((t.n - 1, float(t.length[-1])), [(int(t.nodes[-1]), 0.0, [], False)],
                         [(int(t.nodes[-1]), 0.0, [], False)])

    def horizon(self, A: _Loc) -> tuple[int, float]:
        """The farthest position a leg from A is checked to: `horizon_m` of
        scenic road on (pruning never joins legs past it), or the end."""
        h = self.t.scen_limit(A.pos, self.horizon_m) if math.isfinite(self.horizon_m) else None
        return self.dest.pos if h is None else h

    def reach(self, A: _Loc, far: tuple[int, float] | None = None) -> float:
        """How far Dijkstra rows from A's exits must search for legs from A
        to any position up to `far` (default: the horizon), as a bound on
        d(exit, n) + pi(n) over the nodes n such a leg can arrive by. A leg's
        straight way costs at most the target's own cost from A to its end
        B, and the check looks at ways up to the U-turn margin more; B is
        entered from target nodes N[k] up to the one past `far`."""
        far = max(far or self.horizon(A), A.pos)
        t = self.t
        j, s = A.pos
        L = float(t.length[j])
        c0 = self.cum_cost[j] + (s / L if L > 0 else 0.0) * (self.cum_cost[j + 1] - self.cum_cost[j])
        k = np.arange(j, min(far[0] + 2, t.n + 1))
        v = (1 + UTURN_MARGIN) * np.maximum(self.cum_cost[k] - c0, 0.0)
        if self.pot is not None:
            v = v + self.pot[t.nodes[k]]
        x = max((c for _, c, _, _ in A.exits), default=0.0)
        return float(v.max()) + (1 + UTURN_MARGIN) * x + UTURN_MARGIN_S + ROW_SLACK_S

    def row(self, node: int, reach: float | None = None):
        """(distances, predecessors) from node, exact for every node n with
        d(node, n) + pi(n) <= reach (pi = 0 without a potential). Default:
        a row already searched, else the whole target."""
        r = self.rows.get(node)
        if r is None or (reach is not None and r[2] < reach):
            if reach is None:
                reach = self.reach(self.origin, self.dest.pos)
            if self.pot is None:
                d, p = dijkstra(self.m, directed=True, indices=node, return_predecessors=True, limit=reach)
            else:  # reduced distance d' = d + pi(n) - pi(node)
                pn = float(self.pot[node])
                d, p = dijkstra(self.m_search, directed=True, indices=node, return_predecessors=True,
                                limit=max(reach - pn, ROW_SLACK_S))
                d = d - self.pot + pn
            r = self.rows[node] = (d, p, reach)
            self.dijkstras += 1
        return r[0], r[1]

    def node_loc(self, k: int) -> _Loc:
        """Target node N[k], k >= 1, as an arrival point."""
        nd = int(self.t.nodes[k])
        return _Loc((k - 1, float(self.t.length[k - 1])), [(nd, 0.0, [], False)], [(nd, 0.0, [], False)])

    def edge_loc(self, e: int, tail: int, head: int, s: float, pos: tuple[int, float]) -> _Loc:
        """A point s metres along edge e driven tail -> head."""
        g = self.g
        L = float(g.length_m[e])
        t = float(self.cost[e])
        f = s / L if L > 0 else 0.5
        fwd = not g.oneway[e] or g.u[e] == tail  # the arc tail -> head exists
        bwd = not g.oneway[e] or g.u[e] == head  # the arc head -> tail exists
        exits, entries = [], []
        if fwd:
            entries.append((tail, f * t, [((tail, head), s, e)], False))
            exits.append((head, (1 - f) * t, [((tail, head), L - s, e)], False))
        if bwd:
            entries.append((head, (1 - f) * t, [((head, tail), L - s, e)], True))
            exits.append((tail, f * t, [((head, tail), s, e)], True))
        return _Loc(pos, exits, entries)

    def target_loc(self, j: int, s: float) -> _Loc:
        t = self.t
        return self.edge_loc(int(t.edges[j]), int(t.nodes[j]), int(t.nodes[j + 1]), s, (j, s))

    def tree_pieces(self, pred: np.ndarray, x: int, y: int) -> list | None:
        path = [y]
        while path[-1] != x:
            p = int(pred[path[-1]])
            if p < 0:
                return None
            path.append(p)
        if len(path) < 2:
            return []
        path.reverse()
        a = np.asarray(path[:-1], dtype=np.int64)
        b = np.asarray(path[1:], dtype=np.int64)
        e = self.g.arc_edge[self.mat[1][np.searchsorted(self.keys, a * self.g.n_nodes + b)]]
        L = self.g.length_m[e]
        return list(zip(zip(a.tolist(), b.tolist()), L.tolist(), e.tolist()))

    def leg(self, A: _Loc, B: _Loc) -> _LegEval | None:
        """Fastest way from A to B over the four exit/entry combinations.
        `risk` is set when a combination that turns round at A or arrives at
        B from the far side is within the U-turn margin of the straight one,
        so another router's timings could pick it."""
        combos = []
        (ja, sa), (jb, sb) = A.pos, B.pos
        if ja == jb and sb > sa:  # both on one target edge: drive along it
            t = self.t
            e, L = int(t.edges[ja]), float(t.length[ja])
            c = float(self.cost[e]) * (sb - sa) / L if L > 0 else 0.0
            combos.append((c, False, None, [(t.keys[ja][:2], sb - sa, e)], False, None, [], False))
        reach = self.reach(A, max(B.pos, self.horizon(A)))
        for xn, xc, xp, xb in A.exits:
            dist, _ = self.row(xn, reach)
            for yn, yc, yp, yb in B.entries:
                d = dist[yn]
                if np.isfinite(d):
                    combos.append((xc + d + yc, xb or yb, xn, xp, xb, yn, yp, yb))
        if not combos:
            return None
        best = min(combos, key=lambda c: (c[0] - (COST_EPS if not c[1] else 0.0), c[1]))
        straight = [c for c in combos if not c[1]]
        risk = False
        if straight:
            bar = straight[0][0] + uturn_margin(straight[0][0])
            risk = any(c[1] and c[0] < bar and not self.doubles_back(c) for c in combos)
        else:
            risk = True
        c, _, xn, xp, xb, yn, yp, yb = best
        mid = [] if xn is None else self.tree_pieces(self.row(xn)[1], xn, yn)
        if mid is None:
            return None
        pieces = list(xp) + mid + list(yp)
        tp = self.t.pieces(A.pos, B.pos) if B.pos >= A.pos else []
        shared, tm, pm, skip = _overlap(tp, pieces, self.t.scenic_edges)
        return _LegEval(c, pieces, xb, yb, shared, tm, pm, risk, skip)

    def doubles_back(self, combo) -> bool:
        """A turning combination that just drives back over the waypoint's own
        edge (turn round at the junction behind it, or overshoot to the
        junction ahead and come back) is the straight route plus a loop, not a
        different road. Google would not take it, so it is not a risk."""
        _, _, xn, xp, xb, yn, yp, yb = combo
        if xn == yn:  # out to one junction and straight back (A and B on one edge)
            return True
        pred = self.row(xn)[1]
        if xb:  # left A backwards to its tail xn; xp's key is (head, tail)
            head = xp[0][0][0]
            y = yn
            while y != xn and int(pred[y]) != xn:
                y = int(pred[y])
                if y < 0:
                    return False
            if y != xn and y == head:
                return True
        if yb:  # reached B from its head yn; yp's key is (head, tail)
            tail = yp[0][0][1]
            if yn != xn and int(pred[yn]) == tail:
                return True
        return False

    def follows(self, ev: _LegEval | None) -> bool:
        if ev is None or ev.exit_back or ev.entry_back or ev.risk:
            return False
        return ev.miss <= max((1 - self.leg_follow) * ev.target_m, self.leg_slack_m)

    # -- greedy farthest reach --

    def greedy(self) -> list[Waypoint]:
        t = self.t
        A, p = self.origin, -1  # anchor and the target edge it sits on
        out: list[Waypoint] = []
        while True:
            # The spacing rule: the next waypoint comes before `max_gap_m` more scenic road.
            lim = t.scen_limit(A.pos, self.place_gap_m) if math.isfinite(self.place_gap_m) else None
            if lim is None and self.follows(self.leg(A, self.dest)):
                return out
            room = p >= 0 and any(x > A.pos[1] for x in self.candidates(p))  # spots left on A's own edge
            if lim is not None and lim[0] <= p:  # the limit is on the anchor's own edge
                if room and any(A.pos[1] < x <= lim[1] for x in self.candidates(p)):
                    w, loc = self.place(A, p, p, lim[1])
                elif p + 1 < t.n:  # no spot left before the limit: the start of the next edge
                    w, loc = self.place(A, p + 1, p + 1, 0.0)
                else:
                    return out
            elif p + 1 >= t.n:  # on the last edge, and the way to the end fails
                if not room:
                    return out
                w, loc = self.place(A, p, p)
            else:
                lo, hi = p + 1, t.n if lim is None else lim[0] + 1  # N[lo] is assumed reached; N[hi] is not
                while hi - lo > 1:
                    mid = (lo + hi) // 2
                    if self.follows(self.leg(A, self.node_loc(mid))):
                        lo = mid
                    else:
                        hi = mid
                # Points part-way along E[lo] can still be reached along the target.
                smax = lim[1] if lim is not None and lo == lim[0] else math.inf
                w, loc = self.place(A, p + 1, lo, smax)
                if w.tier == 4 and room:
                    # Nothing ahead passes. When the spacing rule put the
                    # anchor early on a long edge, the end of that edge may
                    # not be reached along the target: try the rest of it.
                    w2, loc2 = self.place(A, p, p)
                    if w2.tier < 4:
                        w, loc = w2, loc2
            if loc.pos <= A.pos:
                raise RuntimeError("waypoint search is not making progress")
            out.append(w)
            A, p = loc, w.index

    def candidates(self, j: int):
        """Candidate offsets on target edge j, farthest first."""
        L = float(self.t.length[j])
        if L < 2 * MIN_END_M:
            return [L / 2]
        n = int((L - 2 * MIN_END_M) // SAMPLE_STEP_M)
        return [L - MIN_END_M - k * SAMPLE_STEP_M for k in range(n + 1)]

    def spot(self, j: int, s: float) -> dict:
        t, ix = self.t, self.ix
        e = int(t.edges[j])
        a = float(t.length[j] - s) if t.rev[j] else s
        p, lon, lat, brg = ix.point_on_edge(e, a)
        return dict(j=j, s=s, e=e, p=p, lon=lon, lat=lat, brg=brg)

    def skip_edges(self, j: int, s: float) -> set[int]:
        t = self.t
        here = t.cum_len[j] + s
        lo = int(np.searchsorted(t.cum_len, here - ADJACENT_M, side="right")) - 1
        hi = int(np.searchsorted(t.cum_len, here + ADJACENT_M, side="left"))
        return set(t.edges[max(lo, 0):min(hi, t.n)].tolist())

    def assess(self, sp: dict) -> dict:
        ix = self.ix
        sp["clear"] = ix.clearance(sp["p"])
        sp["other"], sp["carriageway"] = ix.others_near(sp["p"], sp["e"], sp["brg"], self.skip_edges(sp["j"], sp["s"]))
        return sp

    def place(self, A: _Loc, jmin: int, jmax: int, smax: float = math.inf) -> tuple[Waypoint, _Loc]:
        """A waypoint on target edges jmax down to jmin, at most smax metres
        along edge jmax (the spacing limit), farthest good spot first. On
        A's own edge only spots past A count."""
        ok_cache: dict[tuple[int, float], bool] = {}

        def leg_ok(sp) -> bool:
            key = (sp["j"], sp["s"])
            if key not in ok_cache:
                ok_cache[key] = self.follows(self.leg(A, self.target_loc(sp["j"], sp["s"])))
            return ok_cache[key]

        spots = []
        for j in range(jmax, jmin - 1, -1):
            link = self.ix.is_link(int(self.t.edges[j]))
            cands = self.candidates(j)
            if j == A.pos[0] and A is not self.origin:
                cands = [s for s in cands if s > A.pos[1]]
            if j == jmax and smax < math.inf:
                cands = [s for s in cands if s <= smax] or ([cands[-1]] if j == jmin and cands else [])
            for s in cands:
                sp = self.spot(j, s)
                sp["link"] = link
                spots.append(sp)
                sp["clear"] = self.ix.clearance(sp["p"])
                if link or sp["clear"] < JUNCTION_CLEAR_M:
                    continue
                self.assess(sp)
                if sp["other"] > PARALLEL_M and sp["carriageway"] >= CARRIAGEWAY_M and leg_ok(sp):
                    return self.make(sp, 1)
        for sp in spots:
            if "other" not in sp:
                self.assess(sp)
        # Spots are farthest first. Fall back in steps, so a spot a little
        # short of the clearance far along beats a clear one back at the anchor.
        def bucket(x, steps):
            return next(i for i, b in enumerate(steps) if x >= b)

        clear_road = [sp for sp in spots if sp["other"] > PARALLEL_M]
        steps = (0.8 * JUNCTION_CLEAR_M, 0.6 * JUNCTION_CLEAR_M, 0.4 * JUNCTION_CLEAR_M, 0.2 * JUNCTION_CLEAR_M, 0.0)
        for sp in sorted(clear_road, key=lambda sp: (sp["link"], bucket(sp["clear"], steps))):
            if leg_ok(sp):
                return self.make(sp, 2)
        steps = (0.8 * PARALLEL_M, 0.6 * PARALLEL_M, 0.4 * PARALLEL_M, 0.2 * PARALLEL_M, 0.0)
        for sp in sorted(spots, key=lambda sp: (sp["link"], bucket(sp["other"], steps))):
            if leg_ok(sp):
                return self.make(sp, 3)
        # Nothing passes the leg check: take the least crowded spot on the farthest edge.
        jtop = max(sp["j"] for sp in spots)
        far = [sp for sp in spots if sp["j"] == jtop]
        return self.make(max(far, key=lambda sp: (sp["other"], sp["clear"])), 4)

    def make(self, sp: dict, tier: int) -> tuple[Waypoint, _Loc]:
        w = Waypoint(
            lat=round(sp["lat"], 5), lon=round(sp["lon"], 5), edge=sp["e"], index=sp["j"],
            offset_m=round(sp["s"], 1), road=self.ix.road(sp["e"]),
            clearance_m=round(sp["clear"], 1), nearest_other_m=sp["other"], tier=tier,
            divided=bool(sp["carriageway"] <= PARALLEL_M), carriageway_m=round(sp["carriageway"], 1),
        )
        return w, self.target_loc(sp["j"], sp["s"])

    # -- cap --

    def scenic_between(self, A: _Loc, B: _Loc) -> float:
        return max(0.0, self.t.scen_at(B.pos) - self.t.scen_at(A.pos))

    def excess(self, A: _Loc, B: _Loc) -> float:
        """Scenic road between A and B beyond the spacing limit."""
        return max(0.0, self.scenic_between(A, B) - self.max_gap_m) if math.isfinite(self.max_gap_m) else 0.0

    def spacing_cost(self, A: _Loc, B: _Loc) -> float:
        """The excess, plus a quarter of it again per gap of excess: a longer
        unguarded stretch is likelier to be cut. Linear in the excess alone,
        every split of an over-budget route into legs all over the limit
        would cost the same."""
        x = self.excess(A, B)
        return x * (1 + x / (4 * self.max_gap_m)) if x > 0 else 0.0

    def prune(self, wps: list[Waypoint], cap: int, plain: "_Reducer | None" = None, exposure: bool = True
              ) -> tuple[list[Waypoint], list[Waypoint]]:
        """Drop greedy waypoints that turn out not to be needed, and keep at
        most `cap`. A leg is scored, under `plain` travel time (default this
        reducer's cost), by the length the stitched route and the target do
        not share (symmetric difference) plus the scenic road it skips (so
        that counts twice), plus RISK_PENALTY_M for a U-turn risk; then
        EXPOSURE_WEIGHT per metre of scenic road it leaves exposed: the
        larger of what it skips under this reducer's cost (the time margin)
        and `spacing_cost`. A subset costs the sum over its legs plus
        WAYPOINT_COST_M per waypoint. So a certain miss counts several times
        a likely one, and under a cap the kept waypoints guard the most
        scenic road. With `exposure` off only the plain terms count (the
        rule before the spacing rule and the margin).
        Returns (the best subset of any size, the best of at most `cap`)."""
        plain = plain or self
        locs = [self.origin] + [self.target_loc(w.index, w.offset_m) for w in wps] + [self.dest]
        plocs = locs if plain is self else [plain.origin] + [plain.target_loc(w.index, w.offset_m) for w in wps] + [plain.dest]
        m = len(locs)
        cost = np.full((m, m), np.inf)
        for i in range(m - 1):
            for j in range(i + 1, m):
                if j > i + 1 and self.scenic_between(locs[i], locs[j]) > self.horizon_m:
                    break
                ev = plain.leg(plocs[i], plocs[j])
                if ev is None:
                    continue
                uturn = ev.exit_back or ev.entry_back
                cost[i, j] = (ev.target_m + ev.path_m - 2 * ev.shared + ev.skipped_scenic
                              + UTURN_PENALTY_M * uturn + RISK_PENALTY_M * (ev.risk and not uturn))
                if exposure:
                    skip = ev.skipped_scenic
                    if plain is not self:
                        em = self.leg(locs[i], locs[j])
                        if em is None:
                            skip = self.scenic_between(locs[i], locs[j])
                        else:
                            skip = em.skipped_scenic + RISK_PENALTY_M * ((em.exit_back or em.entry_back) and not uturn)
                    cost[i, j] += EXPOSURE_WEIGHT * max(skip, self.spacing_cost(locs[i], locs[j]))
        # best[c, j]: least cost to reach loc j through c waypoints (j itself counted if it is one)
        n = len(wps)
        best = np.full((n + 1, m), np.inf)
        back = np.full((n + 1, m), -1, dtype=np.int64)
        best[0, 0] = 0.0
        for j in range(1, m):
            used = 0 if j == m - 1 else 1
            for c in range(used, n + 1):
                prev = best[c - used, :j] + cost[:j, j]
                i = int(np.argmin(prev))
                if np.isfinite(prev[i]):
                    best[c, j], back[c, j] = prev[i], i

        def chosen(k: int) -> list[Waypoint]:
            total = best[: k + 1, m - 1] + WAYPOINT_COST_M * np.arange(k + 1)
            c = int(np.argmin(total))
            if not np.isfinite(total[c]):
                return list(wps[:k])
            out, j = [], m - 1
            while j > 0:
                i = int(back[c, j])
                if j != m - 1:
                    out.append(j - 1)
                    c -= 1
                j = i
            return [wps[i] for i in sorted(out)]

        return chosen(n), chosen(min(cap, n))

    # -- final check --

    def locs_for(self, wps: list[Waypoint], notes: list[str] | None = None) -> list[_Loc]:
        """Origin, the waypoints' rounded coordinates snapped back to the
        nearest edge the way Google would see them, and the destination."""
        t = self.t
        locs = [self.origin]
        for w in wps:
            snap = self.ix.snap(w.lat, w.lon)
            pos = (w.index, w.offset_m)
            if snap is None:
                if notes is not None:
                    notes.append(f"waypoint {w.latlon} does not snap to any road")
                locs.append(self.target_loc(*pos))
                continue
            e, a, off = snap
            if e != w.edge:
                if notes is not None:
                    notes.append(f"waypoint {_fmt(w.latlon)} snaps to edge {e} ({self.ix.road(e)}), "
                                 f"not {w.edge} ({w.road}), {off:.0f} m off")
                locs.append(self.edge_loc(e, int(self.g.u[e]), int(self.g.v[e]), a, pos))
            else:
                s = float(t.length[w.index] - a) if t.rev[w.index] else a
                locs.append(self.target_loc(w.index, min(max(s, 0.0), float(t.length[w.index]))))
        locs.append(self.dest)
        return locs

    def evaluate(self, wps: list[Waypoint], notes: list[str]) -> tuple[list[Leg], list, list]:
        """Route through the rounded coordinates, snapped back to the nearest
        edge the way Google would see them."""
        t = self.t
        locs = self.locs_for(wps, notes)
        legs, pieces = [], []
        for A, B in zip(locs[:-1], locs[1:]):
            ev = self.leg(A, B)
            if ev is None:
                notes.append(f"no route between waypoints at target positions {A.pos} and {B.pos}")
                legs.append(Leg(math.inf, t.time_between(A.pos, B.pos), math.inf,
                                t.pos_m(B.pos) - t.pos_m(A.pos), 0.0, False))
                continue
            pieces.extend(ev.pieces)
            legs.append(Leg(ev.cost, t.time_between(A.pos, B.pos), ev.path_m, ev.target_m,
                            ev.overlap, ev.exit_back or ev.entry_back, ev.risk))
        for leg, A, B in zip(legs, locs[:-1], locs[1:]):
            leg.scenic_m = self.scenic_between(A, B)
        return legs, pieces, locs


def _overlap(target_pieces, path_pieces, scenic_edges=frozenset()) -> tuple[float, float, float, float]:
    """(shared, target, path metres, target metres on `scenic_edges` the path skips)."""
    tm: dict = {}
    for p in target_pieces:
        tm[p[0]] = tm.get(p[0], 0.0) + p[1]
    pm: dict = {}
    for (a, b), m, e in path_pieces:
        k = (a, b, int(e))
        pm[k] = pm.get(k, 0.0) + m
    shared, skip = 0.0, 0.0
    for k, v in tm.items():
        s = min(v, pm.get(k, 0.0))
        shared += s
        if k[2] in scenic_edges:
            skip += v - s
    return shared, sum(tm.values()), sum(pm.values()), skip


# --------------------------------------------------------------------------
# Entry points


def _build_reducers(graph, target: _Target, leg_follow: float, leg_slack_m: float,
                    time_margin: float, gap: float, scenic, s_fast: float | None
                    ) -> tuple[_Reducer, _Reducer]:
    base, pot = _corridor(graph, target, time_margin)
    r = _Reducer(graph, target, leg_follow, leg_slack_m, matrix=base, max_gap_m=gap, potential=pot)
    target.set_scenic(_scenic_mask(graph, target, r, scenic, s_fast))
    search = r
    if time_margin > 0:
        edges = np.unique(target.edges)
        cost = np.array(graph.time_s, dtype=np.float64)
        cost[edges] *= 1 + time_margin
        search = _Reducer(graph, target, leg_follow, leg_slack_m, cost=cost,
                          matrix=_slowed_matrix(graph, edges, 1 + time_margin, base), max_gap_m=gap,
                          potential=pot)
    return r, search


def _finish_result(graph, target: _Target, r: _Reducer, search: _Reducer,
                   max_waypoints: int, follow_target: float, time_margin: float,
                   gap: float, init_notes: list[str] | None = None) -> WaypointResult:
    notes = list(init_notes or ())
    # When the spacing rule alone needs more than the budget, space the
    # greedy pass evenly over the budget instead: pruning picks from its
    # waypoints, and a grid at max_gap_m would leave legs of 2 or 3 gaps.
    scen = float(target.cum_scen[-1])
    search.place_gap_m = gap
    if math.isfinite(gap) and scen > gap * (max_waypoints + 1):
        search.place_gap_m = scen / (max_waypoints + 1)
    if math.isfinite(gap):
        r.horizon_m = search.horizon_m = max(HORIZON_GAPS * gap, 2 * scen / (max_waypoints + 1))
    else:
        r.horizon_m = search.horizon_m = math.inf
    greedy = search.greedy()
    best, wps = search.prune(greedy, max_waypoints, plain=r) if greedy else ([], [])
    needed = len(best)
    if math.isfinite(gap) and scen > gap:  # the spacing rule alone needs this many
        needed = max(needed, math.ceil(scen / (gap + GAP_TOL_M)) - 1)
    capped = needed > max_waypoints
    if capped:
        # Over budget. The robust pass's waypoints may not include the ones
        # our own router needs most, so add those (found as before the
        # robustness rules: plain time, no spacing) and choose from both.
        r.place_gap_m = math.inf
        plain_best, plain_cap = r.prune(r.greedy(), max_waypoints, exposure=False)
        if len(plain_best) > max_waypoints:
            notes.append(f"our own router alone needs {_n_waypoints(len(plain_best))}")
        pool = sorted({(w.index, w.offset_m): w for w in plain_best + plain_cap + greedy}.values(),
                      key=lambda w: (w.index, w.offset_m))
        _, wps = search.prune(pool, max_waypoints, plain=r)
        notes.append(f"needed {_n_waypoints(needed)}; kept the {len(wps)} that leave the least scenic road at risk")
    legs, pieces, _ = r.evaluate(wps, notes)
    shared, tm, pm, _ = _overlap(target.pieces(r.origin.pos, r.dest.pos), pieces)
    _flag_risks(r, search, wps, legs, time_margin, notes)
    for i, leg in enumerate(legs):
        if leg.uturn:
            notes.append(f"leg {i + 1} turns round at a waypoint or arrives from the far side")
        elif leg.uturn_risk:
            notes.append(f"leg {i + 1}: turning round at a waypoint is within the U-turn margin")
    for w in wps:
        if w.tier > 1:
            why = {2: "closer than the junction clearance or to the other carriageway", 3: "another road within the parallel radius",
                   4: "no spot passes the leg check"}[w.tier]
            notes.append(f"waypoint {_fmt(w.latlon)} on {w.road}: {why}")
        if w.divided:
            notes.append(f"waypoint {_fmt(w.latlon)} on {w.road} is on one carriageway of a divided road "
                         f"(other carriageway {w.carriageway_m:.0f} m away)")
    t0 = target.nodes[0]
    t1 = target.nodes[-1]
    return WaypointResult(
        waypoints=wps,
        origin=(round(float(graph.node_lat[t0]), 5), round(float(graph.node_lon[t0]), 5)),
        destination=(round(float(graph.node_lat[t1]), 5), round(float(graph.node_lon[t1]), 5)),
        overlap=float(shared / max(tm, pm)) if max(tm, pm) > 0 else 1.0,
        eta_s=float(sum(leg.our_s for leg in legs)),
        target_eta_s=float(target.cum_time[-1]),
        stitched_m=float(pm), target_m=float(tm), needed=needed, capped=capped,
        follow_target=follow_target, legs=legs, notes=notes,
        time_margin=float(time_margin), max_gap_m=gap, scenic_m=float(target.cum_scen[-1]),
    )


def reduce_to_waypoints(graph, path=None, *, edge_path=None, max_waypoints: int = 9,
                        follow_target: float = 0.9, leg_follow: float = LEG_FOLLOW,
                        leg_slack_m: float = LEG_SLACK_M, time_margin: float = TIME_MARGIN,
                        max_gap_m: float | None = MAX_GAP_M, scenic=None,
                        s_fast: float | None = None) -> WaypointResult:
    """Fewest waypoints that make the fastest route follow `path`.

    `path` is a `Route` or a node path; or pass `edge_path` (edge indices in
    travel order). `leg_follow` is the share of each leg, by length, that the
    fastest route must drive on the target (or miss by at most `leg_slack_m`).
    `follow_target` is the overall overlap the result is judged against.

    Google rates roads a little differently than we do, so two rules make
    the plan robust to that (phase 0: with 60 km between waypoints Google
    took a faster road beside 5&20 that our router did not):

    - `time_margin`: waypoints are placed as if the target's own edges were
      that share slower (0.1 = 10%), so each leg must still follow the
      target when another router rates it a little slower relative to the
      roads beside it. The reported overlap and ETA use plain travel time.
    - `max_gap_m`: at most this much scenic road between two waypoints
      (None or inf turns it off). Scenic road is the target's edges that the
      plain fastest route from origin to destination does not use; with
      `scenic` (per-edge S, or a per-edge bool mask) only those also above
      `s_fast` (default: the fastest route's time-weighted mean S). Roads
      shared with the fastest route can go any distance without a waypoint.

    With more than `max_waypoints` needed, the kept ones are those that
    leave the least scenic road exposed (see `_Reducer.prune`); the legs
    still at risk are flagged (`Leg.at_risk`, `WaypointResult.risk_legs`)
    and named in the notes, most exposed first.
    """
    if (path is None) == (edge_path is None):
        raise ValueError("pass exactly one of path and edge_path")
    target = _target_from(graph, path, edge_path)
    if target.n == 0:
        raise ValueError("empty target route")
    gap = math.inf if max_gap_m is None else float(max_gap_m)
    init_notes: list[str] = []
    url_cap = _url_cap()
    if max_waypoints > url_cap:
        init_notes.append(f"at most {url_cap} waypoints fit in a {MAX_URL}-character link")
        max_waypoints = url_cap
    time_margin = max(float(time_margin), 0.0)
    r, search = _build_reducers(graph, target, leg_follow, leg_slack_m, time_margin, gap, scenic, s_fast)
    return _finish_result(graph, target, r, search, max_waypoints, follow_target, time_margin, gap, init_notes)


def _partition_parts(full: WaypointResult, n_parts: int, per_part: int) -> list[WaypointPart]:
    wps = full.waypoints
    m = len(wps)
    cum_s = [0.0]
    for leg in full.legs:
        cum_s.append(cum_s[-1] + (leg.our_s if math.isfinite(leg.our_s) else leg.target_s))
    total_s = cum_s[-1] or 1.0
    endpoints = {full.origin, full.destination}
    splits: list[int] = []
    prev = -1
    for k in range(1, n_parts):
        rem = n_parts - k
        lo = max(prev + 1, m - rem * (per_part + 1))
        hi = min(prev + per_part + 1, m - rem)
        goal_s = total_s * k / n_parts
        best_i = min(
            range(lo, hi + 1),
            key=lambda i: (
                wps[i].latlon in endpoints,
                wps[i].tier >= 4,
                abs(cum_s[i + 1] - goal_s)
                + (600.0 if wps[i].tier > 1 else 0.0)
                + (600.0 if wps[i].divided else 0.0),
            ),
        )
        splits.append(best_i)
        prev = best_i
    bounds = [-1] + splits + [m]
    parts: list[WaypointPart] = []
    for idx in range(n_parts):
        a, b = bounds[idx], bounds[idx + 1]
        sub_wps = wps[a + 1 : b]
        sub_legs = full.legs[a + 1 : b + 1]
        orig = full.origin if a == -1 else wps[a].latlon
        dest = full.destination if b == m else wps[b].latlon
        parts.append(
            WaypointPart(
                part=idx + 1,
                origin=orig,
                destination=dest,
                waypoints=sub_wps,
                first_n=(a + 2) if sub_wps else 0,
                last_n=b if sub_wps else 0,
                handoff_n=(b + 1) if b < m else None,
                handoff_road=wps[b].road if b < m else None,
                eta_s=float(sum(leg.our_s if math.isfinite(leg.our_s) else leg.target_s for leg in sub_legs)),
                length_m=float(sum(leg.our_m if math.isfinite(leg.our_m) else leg.target_m for leg in sub_legs)),
            )
        )
    return parts


def split_waypoints(graph, path=None, *, edge_path=None, single_cap: int | None = None,
                    per_part: int | None = None, max_parts: int = 3,
                    follow_target: float = 0.9, leg_follow: float = LEG_FOLLOW,
                    leg_slack_m: float = LEG_SLACK_M, time_margin: float = TIME_MARGIN,
                    max_gap_m: float | None = MAX_GAP_M, scenic=None,
                    s_fast: float | None = None) -> tuple[WaypointResult, WaypointResult, list[WaypointPart]]:
    """Reduce `path` for Google Maps both as a single editable link (`single`)
    and, when a single link is capped or misses the route, as 2..`max_parts`
    editable links (`full`, `parts`) whose boundary waypoints double as the
    destination of part k and the origin of part k + 1.

    Every returned link stays within `editable_waypoint_cap`, so Google Maps
    opens it without the 'Route cannot be modified further' lock. Returns
    `(single, single, [])` when one editable link already suffices.
    """
    if (path is None) == (edge_path is None):
        raise ValueError("pass exactly one of path and edge_path")
    target = _target_from(graph, path, edge_path)
    if target.n == 0:
        raise ValueError("empty target route")
    closed = int(target.nodes[0]) == int(target.nodes[-1])
    if single_cap is None:
        single_cap = editable_waypoint_cap(closed)
    if per_part is None:
        per_part = editable_waypoint_cap(closed=False)
    if max_parts < 1:
        raise ValueError("max_parts must be at least 1")
    gap = math.inf if max_gap_m is None else float(max_gap_m)
    time_margin = max(float(time_margin), 0.0)
    url_cap = _url_cap()
    single_cap = min(int(single_cap), url_cap)
    per_part = min(int(per_part), url_cap)

    r, search = _build_reducers(graph, target, leg_follow, leg_slack_m, time_margin, gap, scenic, s_fast)
    single = _finish_result(graph, target, r, search, single_cap, follow_target, time_margin, gap)
    single_ok = (
        single.meets_target
        and not any(leg.uturn for leg in single.legs)
        and (
            not single.capped
            or (
                single.overlap >= max(follow_target, 0.995)
                and all(leg.margin_skip_m <= r.leg_slack_m for leg in single.legs)
                and (not math.isfinite(gap) or max((leg.scenic_m for leg in single.legs), default=0.0) <= 1.5 * gap)
            )
        )
    )
    if max_parts == 1 or single_ok:
        return single, single, []

    chain_cap = min(url_cap, max_parts * (per_part + 1) - 1)
    if chain_cap <= single_cap:
        return single, single, []
    full = _finish_result(graph, target, r, search, chain_cap, follow_target, time_margin, gap)
    if full.count <= single_cap:
        return single, single, []

    n_parts = min(max_parts, max(2, math.ceil((full.count + 1) / (per_part + 1))))
    if n_parts > 2:
        # Prefer 2 links when 2 * (per_part + 1) - 1 waypoints already hold >= 99% with no U-turn
        two_cap = min(url_cap, 2 * (per_part + 1) - 1)
        two = _finish_result(graph, target, r, search, two_cap, follow_target, time_margin, gap)
        if two.count > single_cap and two.overlap >= max(follow_target, 0.99) and not any(leg.uturn for leg in two.legs):
            full, n_parts = two, 2
    part_cap = min(url_cap, n_parts * (per_part + 1) - 1)
    if full.count > part_cap:
        full = _finish_result(graph, target, r, search, part_cap, follow_target, time_margin, gap)
    if full.count < n_parts - 1:
        return single, single, []
    return single, full, _partition_parts(full, n_parts, per_part)


def _n_waypoints(k: int) -> str:
    return f"{k} waypoint" + ("" if k == 1 else "s")


def _url_cap() -> int:
    """Most waypoints that keep a Maps link under MAX_URL characters."""
    p = (-90.0, -180.0)  # the longest coordinate text
    base = len(maps_url(p, p, [p]))
    return 1 + (MAX_URL - 1 - base) // (len("%7C") + len(_fmt(p)))


def _scenic_mask(g, target: _Target, r: "_Reducer", scenic, s_fast) -> np.ndarray:
    """Target edges the spacing rule applies to (see `reduce_to_waypoints`)."""
    o, d = int(target.nodes[0]), int(target.nodes[-1])
    fast = r.tree_pieces(r.row(o, r.reach(r.origin, r.dest.pos))[1], o, d) if o != d else []
    fast_edges = np.asarray([e for _, _, e in fast or []], dtype=np.int64)
    mask = ~np.isin(target.edges, fast_edges)
    if scenic is not None:
        sc = np.asarray(scenic)
        if sc.dtype == bool:
            mask &= sc[target.edges]
        else:
            sc = sc.astype(np.float64)
            if s_fast is None and len(fast_edges):
                tt = g.time_s[fast_edges]
                s_fast = float(np.sum(tt * sc[fast_edges]) / tt.sum()) if tt.sum() > 0 else 0.0
            mask &= sc[target.edges] > (s_fast if s_fast is not None else -np.inf)
    return mask


def _flag_risks(r: "_Reducer", search: "_Reducer", wps, legs: list[Leg], time_margin: float,
                notes: list[str]) -> None:
    """Fill each leg's scenic road, margin check and exposure; note the legs
    at risk, most scenic road exposed first."""
    locs = search.locs_for(wps)
    gap = r.max_gap_m
    flagged = []
    for i, (leg, A, B) in enumerate(zip(legs, locs[:-1], locs[1:])):
        skip = 0.0
        if search is not r:
            ev = search.leg(A, B)
            skip = r.scenic_between(A, B) if ev is None else ev.skipped_scenic
        leg.margin_skip_m = skip
        over = leg.scenic_m - gap if math.isfinite(gap) else 0.0
        over = over if over > GAP_TOL_M else 0.0
        leg.exposure_m = max(skip, over)
        leg.at_risk = over > 0 or skip > r.leg_slack_m
        if leg.at_risk:
            flagged.append(i)
    names = ["start"] + [f"waypoint {k + 1}" for k in range(len(wps))] + ["end"]
    for i in sorted(flagged, key=lambda i: -legs[i].exposure_m):
        leg = legs[i]
        road = _main_scenic_road(r, locs[i].pos, locs[i + 1].pos)
        where = f"leg {i + 1} ({names[i]} to {names[i + 1]}{', ' + road if road else ''})"
        if leg.margin_skip_m > r.leg_slack_m:
            notes.append(f"{where} at risk: with the route's own roads {time_margin:.0%} slower the fastest "
                         f"way skips {leg.margin_skip_m / 1000:.1f} km of scenic road")
        if math.isfinite(gap) and leg.scenic_m > gap + GAP_TOL_M:
            notes.append(f"{where} at risk: {leg.scenic_m / 1000:.1f} km of scenic road with no waypoint "
                         f"(limit {gap / 1000:.3g} km); a router that rates roads differently could cut it short")


def _main_scenic_road(r: "_Reducer", a, b) -> str:
    """The road with the most scenic length between target positions a and b."""
    t = r.t
    by: dict[str, float] = {}
    for j in range(a[0], min(b[0], t.n - 1) + 1):
        if t.scenic[j]:
            name = r.ix.road(int(t.edges[j]))
            by[name] = by.get(name, 0.0) + float(t.length[j])
    return max(by, key=by.get) if by else ""


def route_via(graph, points: Sequence[tuple[float, float]], edge_cost=None) -> np.ndarray:
    """Node path of the cheapest route through (lat, lon) points in order
    (default cost: travel time). Inner points are snapped to their nearest
    edge and the route drives along that edge through them, so a mid-road
    waypoint is not pulled to a junction on a side road. The direction along
    each such edge is chosen for the whole trip at once (Viterbi over the two
    directions per point), so the route does not turn round at a point
    unless that really is cheapest. First and last points snap to their
    nearest node."""
    if len(points) < 2:
        raise ValueError("need at least two points")
    ix = _index(graph)
    cost = graph.time_s if edge_cost is None else np.asarray(edge_cost, dtype=np.float64)
    m, _ = _arc_lookup(graph, None if edge_cost is None else cost)
    src, _ = graph.snap(*points[0])
    dst, _ = graph.snap(*points[-1])
    stages = [[(src, src, 0.0)]]
    for lat, lon in points[1:-1]:
        snap = ix.snap(lat, lon)
        if snap is None:
            raise ValueError(f"({lat}, {lon}) is not within 200 m of a road")
        e = snap[0]
        u, v = int(graph.u[e]), int(graph.v[e])
        states = [(u, v, float(cost[e]))]
        if not graph.oneway[e]:
            states.append((v, u, float(cost[e])))
        stages.append(states)
    stages.append([(dst, dst, 0.0)])

    rows: dict[int, tuple[np.ndarray, np.ndarray]] = {}

    def row(x: int):
        if x not in rows:
            rows[x] = dijkstra(m, directed=True, indices=x, return_predecessors=True)
        return rows[x]

    best = [[0.0]]
    back: list[list[int]] = [[-1]]
    for i in range(1, len(stages)):
        bi, ki = [], []
        for entry, _, c in stages[i]:
            opts = [best[i - 1][r] + row(stages[i - 1][r][1])[0][entry] + c for r in range(len(stages[i - 1]))]
            r = int(np.argmin(opts))
            bi.append(opts[r])
            ki.append(r)
        best.append(bi)
        back.append(ki)
    if not np.isfinite(best[-1][0]):
        raise ValueError("no route through the points")
    chosen = [0]
    for i in range(len(stages) - 1, 0, -1):
        chosen.append(back[i][chosen[-1]])
    chosen.reverse()

    nodes = [src]
    for i in range(1, len(stages)):
        x = stages[i - 1][chosen[i - 1]][1]
        entry, exit_, _ = stages[i][chosen[i]]
        pred = row(x)[1]
        seg = [entry]
        while seg[-1] != x:
            seg.append(int(pred[seg[-1]]))
        nodes.extend(seg[::-1][1:])
        if exit_ != entry:
            nodes.append(exit_)
    return np.asarray(nodes, dtype=np.int64)
