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
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

from .graph import EARTH_RADIUS_M

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
WAYPOINT_COST_M = 50.0   # pruning: a waypoint is worth keeping if it saves more than this off the target

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
    needed: int  # waypoints needed without the cap (greedy pass, then pruned)
    capped: bool
    follow_target: float
    legs: list[Leg] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

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
        }


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


def _arc_lookup(g, cost=None):
    m, sel, indptr = g._matrix(g.time_s if cost is None else cost)
    heads = m.indices

    def edge(a: int, b: int) -> int | None:
        lo, hi = indptr[a], indptr[a + 1]
        k = lo + np.searchsorted(heads[lo:hi], b)
        if k < hi and heads[k] == b:
            return int(g.arc_edge[sel[k]])
        return None
    return m, edge


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

    def __init__(self, g, target: _Target, leg_follow: float, leg_slack_m: float, cost=None):
        self.g = g
        self.t = target
        self.ix = _index(g)
        self.cost = g.time_s if cost is None else cost
        self.m, self.edge_of = _arc_lookup(g, cost)
        self.leg_follow = leg_follow
        self.leg_slack_m = leg_slack_m
        self.limit = 1.5 * float(self.cost[target.edges].sum()) + 600.0
        self.rows: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        self.dijkstras = 0
        t = target
        self.origin = _Loc((0, 0.0), [(int(t.nodes[0]), 0.0, [], False)], [(int(t.nodes[0]), 0.0, [], False)])
        self.dest = _Loc((t.n - 1, float(t.length[-1])), [(int(t.nodes[-1]), 0.0, [], False)],
                         [(int(t.nodes[-1]), 0.0, [], False)])

    def row(self, node: int):
        r = self.rows.get(node)
        if r is None:
            d, p = dijkstra(self.m, directed=True, indices=node, return_predecessors=True, limit=self.limit)
            r = self.rows[node] = (d, p)
            self.dijkstras += 1
        return r

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
        path.reverse()
        out = []
        for a, b in zip(path[:-1], path[1:]):
            e = self.edge_of(a, b)
            out.append(((a, b), float(self.g.length_m[e]), e))
        return out

    def leg(self, A: _Loc, B: _Loc) -> _LegEval | None:
        """Fastest way from A to B over the four exit/entry combinations.
        `risk` is set when a combination that turns round at A or arrives at
        B from the far side is within the U-turn margin of the straight one,
        so another router's timings could pick it."""
        combos = []
        for xn, xc, xp, xb in A.exits:
            dist, _ = self.row(xn)
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
        mid = self.tree_pieces(self.row(xn)[1], xn, yn)
        if mid is None:
            return None
        pieces = list(xp) + mid + list(yp)
        tp = self.t.pieces(A.pos, B.pos) if B.pos >= A.pos else []
        shared, tm, pm = _overlap(tp, pieces)
        return _LegEval(c, pieces, xb, yb, shared, tm, pm, risk)

    def doubles_back(self, combo) -> bool:
        """A turning combination that just drives back over the waypoint's own
        edge (turn round at the junction behind it, or overshoot to the
        junction ahead and come back) is the straight route plus a loop, not a
        different road. Google would not take it, so it is not a risk."""
        _, _, xn, xp, xb, yn, yp, yb = combo
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
            if self.follows(self.leg(A, self.dest)):
                return out
            lo, hi = p + 1, t.n  # node N[lo] is reached trivially; N[hi] is not
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if self.follows(self.leg(A, self.node_loc(mid))):
                    lo = mid
                else:
                    hi = mid
            # Points part-way along E[lo] can still be reached along the target.
            jmax = lo
            w, loc = self.place(A, p + 1, jmax)
            out.append(w)
            A, p = loc, w.index
            if len(out) > t.n:
                raise RuntimeError("waypoint search is not making progress")

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

    def place(self, A: _Loc, jmin: int, jmax: int) -> tuple[Waypoint, _Loc]:
        ok_cache: dict[tuple[int, float], bool] = {}

        def leg_ok(sp) -> bool:
            key = (sp["j"], sp["s"])
            if key not in ok_cache:
                ok_cache[key] = self.follows(self.leg(A, self.target_loc(sp["j"], sp["s"])))
            return ok_cache[key]

        spots = []
        for j in range(jmax, jmin - 1, -1):
            link = self.ix.is_link(int(self.t.edges[j]))
            for s in self.candidates(j):
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
        far = [sp for sp in spots if sp["j"] == jmax]
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

    def prune(self, wps: list[Waypoint], cap: int) -> tuple[list[Waypoint], list[Waypoint]]:
        """Drop greedy waypoints that turn out not to be needed, and keep at
        most `cap`. A subset is scored by the length the stitched route and
        the target do not share (symmetric difference), plus RISK_PENALTY_M
        for each leg with a U-turn risk and WAYPOINT_COST_M per waypoint.
        Returns (the best subset of any size, the best of at most `cap`)."""
        locs = [self.origin] + [self.target_loc(w.index, w.offset_m) for w in wps] + [self.dest]
        m = len(locs)
        cost = np.full((m, m), np.inf)
        for i in range(m - 1):
            for j in range(i + 1, m):
                ev = self.leg(locs[i], locs[j])
                if ev is not None:
                    cost[i, j] = ev.target_m + ev.path_m - 2 * ev.shared + RISK_PENALTY_M * ev.risk
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

    def evaluate(self, wps: list[Waypoint], notes: list[str]) -> tuple[list[Leg], list]:
        """Route through the rounded coordinates, snapped back to the nearest
        edge the way Google would see them."""
        t = self.t
        locs = [self.origin]
        for w in wps:
            snap = self.ix.snap(w.lat, w.lon)
            pos = (w.index, w.offset_m)
            if snap is None:
                notes.append(f"waypoint {w.latlon} does not snap to any road")
                locs.append(self.target_loc(*pos))
                continue
            e, a, off = snap
            if e != w.edge:
                notes.append(f"waypoint {_fmt(w.latlon)} snaps to edge {e} ({self.ix.road(e)}), "
                             f"not {w.edge} ({w.road}), {off:.0f} m off")
                locs.append(self.edge_loc(e, int(self.g.u[e]), int(self.g.v[e]), a, pos))
            else:
                s = float(t.length[w.index] - a) if t.rev[w.index] else a
                locs.append(self.target_loc(w.index, min(max(s, 0.0), float(t.length[w.index]))))
        locs.append(self.dest)
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
        return legs, pieces


def _overlap(target_pieces, path_pieces) -> tuple[float, float, float]:
    tm: dict = {}
    for p in target_pieces:
        tm[p[0]] = tm.get(p[0], 0.0) + p[1]
    pm: dict = {}
    for (a, b), m, e in path_pieces:
        k = (a, b, int(e))
        pm[k] = pm.get(k, 0.0) + m
    shared = sum(min(v, pm.get(k, 0.0)) for k, v in tm.items())
    return shared, sum(tm.values()), sum(pm.values())


# --------------------------------------------------------------------------
# Entry points


def reduce_to_waypoints(graph, path=None, *, edge_path=None, max_waypoints: int = 9,
                        follow_target: float = 0.9, leg_follow: float = LEG_FOLLOW,
                        leg_slack_m: float = LEG_SLACK_M, time_margin: float = 0.0) -> WaypointResult:
    """Fewest waypoints that make the fastest route follow `path`.

    `path` is a `Route` or a node path; or pass `edge_path` (edge indices in
    travel order). `leg_follow` is the share of each leg, by length, that the
    fastest route must drive on the target (or miss by at most `leg_slack_m`).
    `follow_target` is the overall overlap the result is judged against.

    `time_margin` makes the placement robust to another router's timings:
    waypoints are placed as if the target's own edges were that share slower
    (0.1 = 10%), so each leg must still follow the target when Google rates
    it a little slower relative to the roads beside it than we do. The
    reported overlap and ETA always use plain travel time.
    """
    if (path is None) == (edge_path is None):
        raise ValueError("pass exactly one of path and edge_path")
    target = _target_from(graph, path, edge_path)
    if target.n == 0:
        raise ValueError("empty target route")
    r = _Reducer(graph, target, leg_follow, leg_slack_m)
    search = r
    if time_margin > 0:
        cost = np.array(graph.time_s, dtype=np.float64)
        cost[np.unique(target.edges)] *= 1 + time_margin
        search = _Reducer(graph, target, leg_follow, leg_slack_m, cost=cost)
    notes: list[str] = []
    greedy = search.greedy()
    best, wps = search.prune(greedy, max_waypoints) if greedy else ([], [])
    needed = len(best)
    capped = needed > max_waypoints
    if capped:
        notes.append(f"needed {needed} waypoints; kept the {len(wps)} that lose the least overlap")
    legs, pieces = r.evaluate(wps, notes)
    shared, tm, pm = _overlap(target.pieces(r.origin.pos, r.dest.pos), pieces)
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
    )


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
