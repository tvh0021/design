"""A-to-B scenic search (docs/PLAN.md section 2, docs/SCORE_V02.md section 4).

    ctx = Context.load()
    res = search(ctx, (43.15729, -77.61521), (43.04812, -76.14742))
    res.picks[1.30].route.edges   # ordered edge indices into edges.parquet
    res.picks[1.30].route.nodes   # ordered node indices into nodes.npz

Steps:

1. Fastest route (time only) gives t0 and S_fast, its time-weighted mean S.
   One forward Dijkstra from A and one reverse from B on time give
   t(A->n) + t(n->B) for every node; the search runs on the nodes where
   that is at most cap * t0 (no route under the cap can leave them).
2. lambda sweep: cost = t * (1 + lambda * (1 - S)); every distinct route
   under the cap is kept.
3. Alternatives: the sweep again for a few lambdas with the edges of every
   route kept so far costing (1 + reuse_penalty) times more.
4. Via moments: the same two time trees give t(A->M) + t(M->B) for every
   moment (driving its best edge). Moments under the cap that the fastest
   route does not pass are ranked by m / extra minutes; the best via_top
   overall and the best via_top_per_band in each budget band seed routes
   A -> M -> B, built with lambda = via_lambda legs and with fastest legs.
   A second and third moment are added greedily (re-ranked by m / extra
   minutes against the current route, best insertion point) while the
   time stays under the cap.
5. Every kept route is scored (`score_route`):
     scenic_min = sum_e t_e * max(0, S_e - S_fast) / 60
     moment_pts = sum of m over distinct moments the route passes (within
                  moment_radius_m, or driving one of the moment's edges),
                  at most one per moment_spacing_km of route and
                  moments_per_hour per hour of t0, best first
     score      = scenic_min + moment_minutes * moment_pts
   and the best route under each budget is picked.

The budget is our own ETA ratio t / t0, both from the same speed model.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

from .route import MIN_COST, RoadGraph, Route


GENERIC_FEATURES = {"cliff", "waterfall", "viewpoint"}  # OSM kinds used as labels for unnamed features

# --------------------------------------------------------------------------
# Graph pieces


class CostGraph:
    """The arcs of `g` listed in `arcs`, deduplicated to the cheapest arc per
    node pair under `edge_cost`, as forward and reverse CSR matrices.
    Dijkstra trees are cached per (root, direction)."""

    def __init__(self, g: RoadGraph, arcs: np.ndarray, edge_cost: np.ndarray):
        n = g.n_nodes
        c = np.maximum(np.asarray(edge_cost, dtype=np.float64)[g.arc_edge[arcs]], MIN_COST)
        t, h = g.arc_tail[arcs], g.arc_head[arcs]
        order = np.lexsort((c, h, t))
        t, h, c, a = t[order], h[order], c[order], arcs[order]
        first = np.ones(len(t), bool)
        first[1:] = (t[1:] != t[:-1]) | (h[1:] != h[:-1])
        t, h, c, a = t[first], h[first], c[first], a[first]
        self.n = n
        self.arc = a
        self.key = t * n + h  # sorted
        indptr = np.concatenate([[0], np.cumsum(np.bincount(t, minlength=n))])
        self.fwd = csr_matrix((c, h, indptr), shape=(n, n))
        o2 = np.lexsort((t, h))
        indptr2 = np.concatenate([[0], np.cumsum(np.bincount(h, minlength=n))])
        self.rev = csr_matrix((c[o2], t[o2], indptr2), shape=(n, n))
        self._trees: dict = {}

    def tree(self, root: int, reverse: bool = False) -> tuple[np.ndarray, np.ndarray]:
        """(dist, pred) from `root` (reverse: to `root`; pred is then the next
        node towards it)."""
        k = (int(root), bool(reverse))
        if k not in self._trees:
            self._trees[k] = dijkstra(self.rev if reverse else self.fwd, directed=True, indices=int(root),
                                      return_predecessors=True)
        return self._trees[k]

    def arcs_between(self, a: np.ndarray, b: np.ndarray) -> np.ndarray:
        q = np.asarray(a, dtype=np.int64) * self.n + np.asarray(b, dtype=np.int64)
        i = np.searchsorted(self.key, q)
        if len(i) and (np.any(i >= len(self.key)) or np.any(self.key[np.minimum(i, len(self.key) - 1)] != q)):
            raise ValueError("no arc between consecutive path nodes")
        return self.arc[i]

    def path(self, src: int, dst: int) -> np.ndarray | None:
        """Node path src -> dst along the forward tree from src."""
        dist, pred = self.tree(src)
        if not np.isfinite(dist[dst]):
            return None
        p = [int(dst)]
        while p[-1] != src:
            p.append(int(pred[p[-1]]))
        return np.asarray(p[::-1], dtype=np.int64)


def edge_arcs(g: RoadGraph) -> tuple[np.ndarray, np.ndarray]:
    """For every edge, its forward arc (u -> v) and reverse arc (v -> u, -1
    on oneway edges and self loops)."""
    fwd = np.full(g.n_edges, -1, np.int64)
    rev = np.full(g.n_edges, -1, np.int64)
    idx = np.arange(len(g.arc_edge))
    fwd[g.arc_edge[~g.arc_rev]] = idx[~g.arc_rev]
    rev[g.arc_edge[g.arc_rev]] = idx[g.arc_rev]
    return fwd, rev


# --------------------------------------------------------------------------
# Context (loaded once per process)


@dataclass
class Moments:
    id: np.ndarray
    m: np.ndarray
    x: np.ndarray  # EPSG:5070
    y: np.ndarray
    lon: np.ndarray
    lat: np.ndarray
    edge: np.ndarray  # best edge
    type: np.ndarray
    label: np.ndarray
    of_edge: np.ndarray  # moment id per edge, -1 if none
    feature: np.ndarray | None = None  # named lake, river, falls ...; None if unnamed or generic

    @classmethod
    def from_table(cls, t, n_edges: int) -> "Moments":
        of_edge = np.full(n_edges, -1, np.int64)
        ids = t.column("id").to_numpy().astype(np.int64)
        for i, mem in zip(ids, t.column("members").to_pylist()):
            of_edge[np.asarray(mem, dtype=np.int64)] = i
        order = np.argsort(ids)
        if not np.array_equal(ids[order], np.arange(len(ids))):
            raise ValueError("moment ids must be 0..n-1")
        col = lambda c: np.asarray(t.column(c).to_pylist(), dtype=object)[order]  # noqa: E731
        num = lambda c: t.column(c).to_numpy()[order]  # noqa: E731
        feat = col("feature")
        feat = np.asarray([f if f and f not in GENERIC_FEATURES else None for f in feat], dtype=object)
        return cls(id=ids[order], m=num("m").astype(np.float64), x=num("x"), y=num("y"), lon=num("lon"),
                   lat=num("lat"), edge=num("edge_idx").astype(np.int64), type=col("type"), label=col("label"),
                   of_edge=of_edge, feature=feat)

    @classmethod
    def empty(cls, n_edges: int) -> "Moments":
        z = np.zeros(0)
        return cls(np.zeros(0, np.int64), z, z, z, z, z, np.zeros(0, np.int64), np.zeros(0, object),
                   np.zeros(0, object), np.full(n_edges, -1, np.int64), np.zeros(0, object))


@dataclass
class Context:
    g: RoadGraph
    S: np.ndarray
    moments: Moments
    cfg: dict  # the [route] section of config/score.toml

    @classmethod
    def load(cls, graph_dir: Path | None = None, score_dir: Path | None = None, cfg: dict | None = None,
             g: RoadGraph | None = None) -> "Context":
        from . import data_dir
        from .graph import edges_fingerprint, read_edges_fingerprint
        from .moments import load_moments
        from .score import load, load_config

        graph_dir = Path(graph_dir or data_dir() / "graph")
        score_dir = Path(score_dir or data_dir() / "score")
        g = g or RoadGraph.load(graph_dir)
        t = load(graph_dir / "edges.parquet", score_dir / "scores.parquet")
        S = t.column("S").to_numpy().astype(np.float64)
        mp = score_dir / "moments.parquet"
        if mp.exists():
            fp = edges_fingerprint(g.edges_table) if g.edges_table is not None else read_edges_fingerprint(graph_dir)
            mom = Moments.from_table(load_moments(mp, fp), g.n_edges)
        else:
            mom = Moments.empty(g.n_edges)
        cfg = cfg if cfg is not None else load_config()["route"]
        return cls(g=g, S=S, moments=mom, cfg=cfg)


# --------------------------------------------------------------------------
# Routes and their scores


@dataclass
class ScenicRoute:
    route: Route
    origin: str  # "fastest", "lambda 2", "alt lambda 3 #1", "via 12+40 (lambda 1)"
    vias: tuple = ()
    ratio: float = float("nan")
    scenic_min: float = 0.0
    mean_S: float = 0.0  # time-weighted
    moments: list = field(default_factory=list)  # credited moment ids, in route order
    passed: list = field(default_factory=list)  # every moment passed, in route order
    moment_pts: float = 0.0
    score: float = 0.0

    @property
    def edges(self) -> np.ndarray:
        return self.route.edges

    @property
    def nodes(self) -> np.ndarray:
        return self.route.nodes

    @property
    def minutes(self) -> float:
        return self.route.minutes

    @property
    def km(self) -> float:
        return self.route.km


_TR = {}


def _to5070(lon, lat):
    from pyproj import Transformer

    if "t" not in _TR:
        _TR["t"] = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    x, y = _TR["t"].transform(np.asarray(lon, dtype=np.float64), np.asarray(lat, dtype=np.float64))
    return np.asarray(x), np.asarray(y)


def _densify(x, y, step: float):
    """Points every <= step along the polyline, with distance along it."""
    seg = np.hypot(np.diff(x), np.diff(y))
    k = np.maximum(1, np.ceil(seg / step).astype(np.int64))
    i = np.repeat(np.arange(len(seg)), k)
    f = np.concatenate([np.arange(kk) / kk for kk in k]) if len(k) else np.zeros(0)
    px = np.concatenate([x[i] + f * (x[i + 1] - x[i]), x[-1:]])
    py = np.concatenate([y[i] + f * (y[i + 1] - y[i]), y[-1:]])
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    s = np.concatenate([cum[i] + f * seg[i], cum[-1:]])
    return px, py, s


def has_spur(route: Route, radius_m: float = 150.0, min_distance_m: float = 1500.0) -> bool:
    """Repeated topology or a long return to within radius_m of the path.

    Index short segment midpoints, then measure actual segment distances.
    Sampling only points would miss parallel carriageways near the radius.
    Distances along the path use the same projected geometry as proximity.
    """
    from scipy.spatial import cKDTree
    import shapely

    if len(np.unique(route.nodes)) < len(route.nodes) or len(np.unique(route.edges)) < len(route.edges):
        return True
    geo = np.asarray(route.geometry, dtype=np.float64)
    if len(geo) < 2:
        return False
    x, y = _to5070(geo[:, 0], geo[:, 1])
    px, py, along = _densify(x, y, radius_m / 2)
    if along[-1] <= min_distance_m:
        return False
    points = np.column_stack([px, py])
    segments = np.stack([points[:-1], points[1:]], axis=1)
    # Each segment is <= radius/2 long. Midpoints within 1.5 * radius
    # include every pair of segments whose actual separation is <= radius.
    pairs = cKDTree(segments.mean(axis=1)).query_pairs(radius_m * 1.5, output_type="ndarray")
    if not len(pairs):
        return False
    i, j = pairs.T
    possible = along[j + 1] - along[i] > min_distance_m
    i, j = i[possible], j[possible]
    if not len(i):
        return False
    lines = shapely.linestrings(segments)
    near = shapely.distance(lines[i], lines[j]) <= radius_m
    i, j = i[near], j[near]
    if not len(i):
        return False
    # Minimize spatial distance over the segment pair subject to the
    # required separation along the route. Interior optima are the usual
    # closest points; on the constraint boundary both positions move the
    # same distance along their respective segments.
    closest = shapely.get_coordinates(shapely.shortest_line(lines[i], lines[j])).reshape(-1, 2, 2)
    si = along[i] + np.linalg.norm(closest[:, 0] - points[i], axis=1)
    sj = along[j] + np.linalg.norm(closest[:, 1] - points[j], axis=1)
    if np.any(sj - si > min_distance_m):
        return True
    li, lj = along[i + 1] - along[i], along[j + 1] - along[j]
    gap = min_distance_m - (along[j] - along[i])
    lo, hi = np.maximum(0, -gap), np.minimum(li, lj - gap)
    valid = (hi > lo) & (li > 0) & (lj > 0)
    if not valid.any():
        return False
    i, j, li, lj, gap, lo, hi = [v[valid] for v in (i, j, li, lj, gap, lo, hi)]
    di = (points[i + 1] - points[i]) / li[:, None]
    dj = (points[j + 1] - points[j]) / lj[:, None]
    delta = points[j] + gap[:, None] * dj - points[i]
    direction = dj - di
    denom = np.sum(direction * direction, axis=1)
    u = np.clip(-np.sum(delta * direction, axis=1) / np.maximum(denom, 1e-30), lo, hi)
    # Strict spatial inequality leaves room to move beyond the along-path
    # boundary, which itself is not an out-and-back.
    return bool(np.any(np.linalg.norm(delta + u[:, None] * direction, axis=1) < radius_m))


def passed_moments(route: Route, g: RoadGraph, mom: Moments, radius: float) -> tuple[np.ndarray, np.ndarray]:
    """Moments the route passes (ids) and their position along it (m): the
    route drives one of the moment's edges or comes within `radius` of its
    point."""
    from scipy.spatial import cKDTree

    pos: dict[int, float] = {}
    e = route.edges
    if len(e) == 0 or len(mom.id) == 0:
        return np.zeros(0, np.int64), np.zeros(0)
    start = np.concatenate([[0.0], np.cumsum(g.length_m[e])[:-1]])
    hit = mom.of_edge[e]
    for k in np.flatnonzero(hit >= 0):
        mid = int(hit[k])
        pos[mid] = min(pos.get(mid, np.inf), start[k] + 0.5 * g.length_m[e[k]])
    geo = np.asarray(route.geometry, dtype=np.float64)
    if len(geo) >= 2:
        x, y = _to5070(geo[:, 0], geo[:, 1])
        sel = np.flatnonzero((mom.x >= x.min() - radius) & (mom.x <= x.max() + radius)
                             & (mom.y >= y.min() - radius) & (mom.y <= y.max() + radius))
        if len(sel):
            px, py, s = _densify(x, y, radius / 4)
            d, i = cKDTree(np.column_stack([px, py])).query(np.column_stack([mom.x[sel], mom.y[sel]]),
                                                            distance_upper_bound=radius)
            for j in np.flatnonzero(np.isfinite(d)):
                mid = int(mom.id[sel[j]])
                pos[mid] = min(pos.get(mid, np.inf), s[i[j]])
    ids = np.asarray(sorted(pos, key=lambda k: pos[k]), dtype=np.int64)
    return ids, np.asarray([pos[k] for k in ids])


def credit_moments(ids: np.ndarray, pos: np.ndarray, m: np.ndarray, spacing_m: float, cap: int,
                   feature=None, feature_spacing_m: float = 0.0) -> np.ndarray:
    """Moments that earn credit: best m first, each at least `spacing_m`
    along the route from those already taken (and `feature_spacing_m` from
    any taken moment of the same named feature), at most `cap`. Returned in
    route order."""
    feature = [None] * len(ids) if feature is None else list(feature)
    order = sorted(range(len(ids)), key=lambda k: (-m[k], pos[k]))
    take: list[int] = []
    for k in order:
        if len(take) >= cap:
            break
        if all(abs(pos[k] - pos[j]) >= (feature_spacing_m if feature[k] is not None and feature[k] == feature[j]
                                        else spacing_m) for j in take):
            take.append(k)
    take.sort(key=lambda k: pos[k])
    return np.asarray([ids[k] for k in take], dtype=np.int64)


def score_route(sr: ScenicRoute, ctx: Context, t0: float, s_fast: float) -> ScenicRoute:
    cfg = ctx.cfg
    e = sr.route.edges
    t = ctx.g.time_s[e]
    sr.ratio = sr.route.time_s / t0
    sr.scenic_min = float(np.sum(t * np.maximum(0.0, ctx.S[e] - s_fast)) / 60)
    sr.mean_S = float(np.sum(t * ctx.S[e]) / max(t.sum(), 1e-9))
    ids, pos = passed_moments(sr.route, ctx.g, ctx.moments, cfg["moment_radius_m"])
    cap = max(1, int(np.floor(cfg["moments_per_hour"] * t0 / 3600)))
    feat = ctx.moments.feature[ids] if ctx.moments.feature is not None else None
    cred = credit_moments(ids, pos, ctx.moments.m[ids], cfg["moment_spacing_km"] * 1000, cap,
                          feat, cfg.get("feature_spacing_km", 0.0) * 1000)
    sr.passed = [int(i) for i in ids]
    sr.moments = [int(i) for i in cred]
    sr.moment_pts = float(ctx.moments.m[cred].sum()) if len(cred) else 0.0
    sr.score = sr.scenic_min + cfg["moment_minutes"] * sr.moment_pts
    return sr


# --------------------------------------------------------------------------
# Search


@dataclass
class SearchResult:
    a: tuple
    b: tuple
    snap: dict
    t0: float
    s_fast: float
    fastest: ScenicRoute
    kept: list
    picks: dict  # budget -> ScenicRoute
    seconds: dict
    region_nodes: int
    via_candidates: int = 0
    spur_rejections: int = 0


class _Kept:
    def __init__(self, cap_s: float, cfg: dict):
        self.cap_s = cap_s
        self.cfg = cfg
        self.spur_rejections = 0
        self.routes: list[ScenicRoute] = []
        self._seen: set = set()

    def add(self, route: Route | None, origin: str, vias=()) -> bool:
        if route is None or route.time_s > self.cap_s + 1e-6:
            return False
        k = hash(route.edges.tobytes())
        if k in self._seen:
            return False
        self._seen.add(k)
        if origin != "fastest" and has_spur(route, self.cfg.get("spur_radius_m", 150.0),
                                           self.cfg.get("spur_min_distance_m", 1500.0)):
            self.spur_rejections += 1
            return False
        self.routes.append(ScenicRoute(route=route, origin=origin, vias=tuple(vias)))
        return True


def _anchor_io(a, src, dst):
    """(out node, in node) of an anchor: 'A', 'B' or a via arc (tail, head, ...)."""
    if a == "A":
        return src, None
    if a == "B":
        return None, dst
    return a[1], a[0]


def _build_via(g: RoadGraph, G: CostGraph, src: int, dst: int, vias: list) -> Route | None:
    """Route src -> via arc 1 -> ... -> dst with legs on G's shortest paths.
    A via is (tail, head, arc, moment id)."""
    nodes = [np.asarray([src], dtype=np.int64)]
    arcs = []
    cur = src
    for tail, head, arc, _ in vias:
        p = G.path(cur, tail)
        if p is None:
            return None
        if len(p) > 1:
            nodes.append(p[1:])
            arcs.append(G.arcs_between(p[:-1], p[1:]))
        nodes.append(np.asarray([head], dtype=np.int64))
        arcs.append(np.asarray([arc], dtype=np.int64))
        cur = head
    p = G.path(cur, dst)
    if p is None:
        return None
    if len(p) > 1:
        nodes.append(p[1:])
        arcs.append(G.arcs_between(p[:-1], p[1:]))
    path = np.concatenate(nodes)
    arc = np.concatenate(arcs) if arcs else np.zeros(0, np.int64)
    return g.make_route(path, arc)


def _top_distinct(order, feats, n: int) -> list:
    """The first n of `order`, at most one per named feature (feats aligned
    with order; None = unnamed, always distinct)."""
    out, seen = [], set()
    for i, f in zip(order, feats):
        if len(out) >= n:
            break
        if f is not None:
            if f in seen:
                continue
            seen.add(f)
        out.append(int(i))
    return out


def _via_time(T: CostGraph, src: int, dst: int, vias: list, t_arc) -> float:
    tot = 0.0
    cur = src
    for tail, head, arc, _ in vias:
        tot += T.tree(cur)[0][tail] + t_arc[arc]
        cur = head
    return float(tot + T.tree(cur)[0][dst])


def search(ctx: Context, a_latlon, b_latlon, log=None) -> SearchResult | None:
    g, S, mom, cfg = ctx.g, ctx.S, ctx.moments, ctx.cfg
    secs: dict[str, float] = {}
    t_start = time.monotonic()
    budgets = sorted(cfg["budgets"])
    cap = budgets[-1]
    src, ds = g.snap(*a_latlon)
    dst, dd = g.snap(*b_latlon)
    snap = {"from_node": src, "from_snap_m": ds, "to_node": dst, "to_snap_m": dd}
    if src == dst:
        return None

    # 1. fastest and the region under the cap
    m_full, _, _ = g._matrix(g.time_s)
    dA = dijkstra(m_full, directed=True, indices=src)
    if not np.isfinite(dA[dst]):
        return None
    dB = dijkstra(m_full.T.tocsr(), directed=True, indices=dst)
    t0 = float(dA[dst])
    region = (dA + dB) <= cap * t0 * (1 + 1e-9) + 1e-6
    arcs = np.flatnonzero(region[g.arc_tail] & region[g.arc_head])
    secs["region"] = time.monotonic() - t_start

    t1 = time.monotonic()
    T = CostGraph(g, arcs, g.time_s)
    p = T.path(src, dst)
    fast = g.make_route(p, T.arcs_between(p[:-1], p[1:]), cost=t0)
    tt = g.time_s[fast.edges]
    s_fast = float(np.sum(tt * S[fast.edges]) / tt.sum())
    kept = _Kept(cap * fast.time_s, cfg)
    kept.add(fast, "fastest")

    # 2. lambda sweep
    graphs: dict[float, CostGraph] = {0.0: T}
    for lam in cfg["lambdas"]:
        G = CostGraph(g, arcs, g.time_s * (1 + lam * (1 - S)))
        graphs[float(lam)] = G
        p = G.path(src, dst)
        if p is not None:
            kept.add(g.make_route(p, G.arcs_between(p[:-1], p[1:])), f"lambda {lam:g}")
    secs["sweep"] = time.monotonic() - t1

    # 3. alternatives: penalise edges already used
    t1 = time.monotonic()
    for r in range(int(cfg["alt_rounds"])):
        used = np.zeros(g.n_edges, bool)
        for sr in kept.routes:
            used[sr.route.edges] = True
        for lam in cfg["alt_lambdas"]:
            cost = g.time_s * (1 + lam * (1 - S)) * (1 + cfg["reuse_penalty"] * used)
            G = CostGraph(g, arcs, cost)
            p = G.path(src, dst)
            if p is not None:
                kept.add(g.make_route(p, G.arcs_between(p[:-1], p[1:])), f"alt lambda {lam:g} #{r + 1}")
    secs["alternatives"] = time.monotonic() - t1

    # 4. via moments
    t1 = time.monotonic()
    fa, ra = edge_arcs(g)
    t_arc = g.time_s[g.arc_edge]
    on_fast = set(passed_moments(fast, g, mom, cfg["moment_radius_m"])[0].tolist())
    feat = mom.feature if mom.feature is not None else np.full(len(mom.id), None, dtype=object)
    fast_feats = {feat[i] for i in on_fast} - {None}
    dA_r, dB_r = T.tree(src)[0], T.tree(dst, reverse=True)[0]
    # every moment, both directions of its best edge: (tail, head, arc, id)
    cand = []
    for k in range(len(mom.id)):
        if int(mom.id[k]) in on_fast or feat[k] in fast_feats:
            continue
        e = mom.edge[k]
        for a in (fa[e], ra[e]):
            if a >= 0 and region[g.arc_tail[a]] and region[g.arc_head[a]]:
                cand.append((int(g.arc_tail[a]), int(g.arc_head[a]), int(a), int(mom.id[k])))
    via_arcs = np.asarray(cand, dtype=np.int64).reshape(-1, 4)
    seeds: list[tuple] = []
    if len(via_arcs):
        vt = dA_r[via_arcs[:, 0]] + t_arc[via_arcs[:, 2]] + dB_r[via_arcs[:, 1]]
        ok = np.isfinite(vt) & (vt <= cap * t0)
        via_arcs, vt = via_arcs[ok], vt[ok]
        # best direction per moment
        order = np.lexsort((vt, via_arcs[:, 3]))
        first = np.ones(len(order), bool)
        first[1:] = via_arcs[order[1:], 3] != via_arcs[order[:-1], 3]
        best = order[first]
        via_arcs, vt = via_arcs[best], vt[best]
        extra = np.maximum((vt - t0) / 60, cfg["via_min_extra_min"])
        rank = mom.m[via_arcs[:, 3]] / extra
        mid = via_arcs[:, 3]
        top = np.argsort(-rank, kind="stable")
        pick = _top_distinct(top, feat[mid[top]], int(cfg["via_top"]))
        lo = 1.0
        for hi in budgets:
            band = np.flatnonzero((vt / t0 > lo) & (vt / t0 <= hi))
            band = band[np.argsort(-rank[band], kind="stable")]
            pick += _top_distinct(band, feat[mid[band]], int(cfg["via_top_per_band"]))
            lo = hi
        seen = set()
        for i in pick:
            if int(i) not in seen:
                seen.add(int(i))
                seeds.append(tuple(int(v) for v in via_arcs[i]))
    secs["via_rank"] = time.monotonic() - t1

    t1 = time.monotonic()
    lam_v = float(cfg["via_lambda"])
    GV = graphs.get(lam_v) or CostGraph(g, arcs, g.time_s * (1 + lam_v * (1 - S)))
    m_by_id = mom.m
    for seed in seeds:
        seq = [seed]
        for step in range(int(cfg["via_max_moments"])):
            ids = "+".join(str(v[3]) for v in seq)
            rv = _build_via(g, GV, src, dst, seq)
            kept.add(rv, f"via {ids} (lambda {lam_v:g})", [v[3] for v in seq])
            rt = _build_via(g, T, src, dst, seq)
            kept.add(rt, f"via {ids} (fastest legs)", [v[3] for v in seq])
            if step + 1 >= int(cfg["via_max_moments"]) or len(via_arcs) == 0:
                break
            # next moment: best insertion into the current sequence
            cur_T = _via_time(T, src, dst, seq, t_arc)
            have = {v[3] for v in seq}
            base = rv if rv is not None else rt
            if base is not None:
                have |= set(passed_moments(base, g, mom, cfg["moment_radius_m"])[0].tolist())
            all_arcs = np.asarray(cand, dtype=np.int64).reshape(-1, 4)
            have_f = {feat[i] for i in have} - {None}
            all_arcs = all_arcs[~np.isin(all_arcs[:, 3], list(have))
                                & ~np.isin(feat[all_arcs[:, 3]], list(have_f))]
            if len(all_arcs) == 0:
                break
            anchors = [(None, src)] + [(v[0], v[1]) for v in seq] + [(dst, None)]
            best_delta = np.full(len(all_arcs), np.inf)
            best_gap = np.zeros(len(all_arcs), np.int64)
            for gi in range(len(anchors) - 1):
                p_out = anchors[gi][1]
                q_in = anchors[gi + 1][0]
                dP = T.tree(p_out)[0]
                dQ = T.tree(q_in, reverse=True)[0]
                leg = dP[q_in]
                d = dP[all_arcs[:, 0]] + t_arc[all_arcs[:, 2]] + dQ[all_arcs[:, 1]] - leg
                better = d < best_delta
                best_delta[better] = d[better]
                best_gap[better] = gi
            feas = np.isfinite(best_delta) & (cur_T + best_delta <= cap * t0)
            if not feas.any():
                break
            f = np.flatnonzero(feas)
            rk = m_by_id[all_arcs[f, 3]] / np.maximum(best_delta[f] / 60, cfg["via_min_extra_min"])
            j = f[int(np.argmax(rk))]
            seq = seq[:best_gap[j]] + [tuple(int(v) for v in all_arcs[j])] + seq[best_gap[j]:]
    secs["via_build"] = time.monotonic() - t1

    # 5. score and pick
    t1 = time.monotonic()
    for sr in kept.routes:
        score_route(sr, ctx, t0, s_fast)
    fastest = kept.routes[0]
    picks = {}
    for b in budgets:
        under = [sr for sr in kept.routes if sr.ratio <= b + 1e-9]
        picks[b] = max(under, key=lambda sr: (round(sr.score, 9), -sr.route.time_s))
    secs["score"] = time.monotonic() - t1
    secs["total"] = time.monotonic() - t_start
    return SearchResult(a=tuple(a_latlon), b=tuple(b_latlon), snap=snap, t0=t0, s_fast=s_fast, fastest=fastest,
                        kept=kept.routes, picks=picks, seconds={k: round(v, 2) for k, v in secs.items()},
                        region_nodes=int(region.sum()), via_candidates=int(len(via_arcs)),
                        spur_rejections=kept.spur_rejections)


# --------------------------------------------------------------------------
# Reporting


def main_roads(g: RoadGraph, route: Route, top: int = 4, min_km: float = 1.0) -> list[tuple[str, float]]:
    return [(lab, km) for lab, km in g.road_summary(route, top=top) if km >= min_km]


def describe(ctx: Context, sr: ScenicRoute) -> dict:
    mom = ctx.moments
    return {
        "origin": sr.origin,
        "minutes": round(sr.minutes, 1), "ratio": round(sr.ratio, 3), "km": round(sr.km, 1),
        "scenic_minutes": round(sr.scenic_min, 1), "moment_points": round(sr.moment_pts, 2),
        "score": round(sr.score, 1), "mean_S": round(sr.mean_S, 3),
        "moments": [{"id": int(i), "type": str(mom.type[i]), "m": round(float(mom.m[i]), 2),
                     "label": str(mom.label[i]), "lat": round(float(mom.lat[i]), 5),
                     "lon": round(float(mom.lon[i]), 5)} for i in sr.moments],
        "moments_passed": len(sr.passed),
        "roads": [(lab, round(km, 1)) for lab, km in main_roads(ctx.g, sr.route, top=6)],
    }


def result_json(ctx: Context, res: SearchResult) -> dict:
    return {
        "from": list(res.a), "to": list(res.b), "snap_m": [round(res.snap["from_snap_m"]), round(res.snap["to_snap_m"])],
        "t0_min": round(res.t0 / 60, 1), "S_fast": round(res.s_fast, 3), "kept": len(res.kept),
        "region_nodes": res.region_nodes, "via_candidates": res.via_candidates, "seconds": res.seconds,
        "spur_rejections": res.spur_rejections,
        "fastest": describe(ctx, res.fastest),
        "picks": {f"+{round((b - 1) * 100)}%": describe(ctx, sr) for b, sr in res.picks.items()},
    }


def geojson(ctx: Context, res: SearchResult) -> dict:
    """Fastest route and the budget picks as LineStrings (with the ordered
    edge and node index paths), and the credited moments as Points."""
    feats = []
    rows = [("fastest", res.fastest)] + [(f"+{round((b - 1) * 100)}%", sr) for b, sr in res.picks.items()]
    shown = set()
    for role, sr in rows:
        props = {"role": role, **describe(ctx, sr), "edge_path": [int(e) for e in sr.edges],
                 "node_path": [int(n) for n in sr.nodes]}
        props["roads"] = [list(r) for r in props["roads"]]
        feats.append({"type": "Feature", "properties": props,
                      "geometry": {"type": "LineString", "coordinates": [list(p) for p in sr.route.geometry]}})
        shown |= set(sr.moments)
    mom = ctx.moments
    for i in sorted(shown):
        feats.append({"type": "Feature",
                      "properties": {"role": "moment", "id": int(i), "type": str(mom.type[i]),
                                     "m": round(float(mom.m[i]), 2), "label": str(mom.label[i])},
                      "geometry": {"type": "Point", "coordinates": [float(mom.lon[i]), float(mom.lat[i])]}})
    return {"type": "FeatureCollection", "features": feats}


def format_table(ctx: Context, res: SearchResult) -> str:
    lines = [f"fastest {res.t0 / 60:.1f} min, S_fast {res.s_fast:.3f}; {len(res.kept)} routes kept "
             f"({res.region_nodes:,} nodes under the cap, {res.via_candidates} via moments); "
             f"search {res.seconds['total']:.1f} s"]
    rows = [("fastest", res.fastest)] + [(f"+{round((b - 1) * 100)}%", sr) for b, sr in res.picks.items()]
    lines.append(f"{'':8s} {'min':>6s} {'ratio':>6s} {'km':>6s} {'scenic':>7s} {'mom':>4s} {'score':>6s}  roads")
    for role, sr in rows:
        roads = ", ".join(f"{lab} {km:.0f} km" for lab, km in main_roads(ctx.g, sr.route))
        lines.append(f"{role:8s} {sr.minutes:6.1f} {sr.ratio:6.2f} {sr.km:6.1f} {sr.scenic_min:7.1f} "
                     f"{len(sr.moments):4d} {sr.score:6.1f}  {roads}")
        for i in sr.moments:
            lines.append(f"{'':10s}- {ctx.moments.type[i]} {ctx.moments.m[i]:.2f}: {ctx.moments.label[i]}")
        lines.append(f"{'':10s}({sr.origin})")
    return "\n".join(lines)


# Map colours: the fastest route is the neutral baseline; the three picks take
# the first three categorical slots (they validate all-pairs for CVD).
_INK, _INK2, _MUTED, _SURFACE = "#0b0b0b", "#52514e", "#b9b8b2", "#fcfcfb"
_PICK_COLOURS = ("#2a78d6", "#eb6834", "#1baf7a")


def plot_routes(ctx: Context, res: SearchResult, path: Path, title: str | None = None) -> Path:
    """PNG map: context roads (motorway to secondary) in grey, the fastest
    route in dark grey, the budget picks in colour, and the moments the
    picks are credited with as numbered markers listed on the right."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patheffects as pe
    from matplotlib.collections import LineCollection

    g = ctx.g
    rows = [("fastest", res.fastest)] + [(f"+{round((b - 1) * 100)}%", sr) for b, sr in res.picks.items()]
    pts = np.concatenate([np.asarray(sr.route.geometry) for _, sr in rows])
    lo0, la0 = pts.min(0)
    lo1, la1 = pts.max(0)
    pad_lo, pad_la = 0.06 * (lo1 - lo0) + 0.02, 0.06 * (la1 - la0) + 0.02
    lo0, lo1, la0, la1 = lo0 - pad_lo, lo1 + pad_lo, la0 - pad_la, la1 + pad_la
    aspect = 1 / np.cos(np.radians(0.5 * (la0 + la1)))
    map_w = 8.4  # inches
    map_h = float(np.clip(map_w * aspect * (la1 - la0) / (lo1 - lo0), 3.5, 10.0))
    fig_w, fig_h = map_w + 4.6, max(map_h, 5.5) + 1.0
    fig = plt.figure(figsize=(fig_w, fig_h), dpi=150, facecolor=_SURFACE)
    ax = fig.add_axes([0.25 / fig_w, (fig_h - 0.6 - map_h) / fig_h, map_w / fig_w, map_h / fig_h],
                      facecolor=_SURFACE)
    side = fig.add_axes([(map_w + 0.45) / fig_w, 0.4 / fig_h, 4.0 / fig_w, (fig_h - 1.0) / fig_h],
                        facecolor=_SURFACE)
    side.axis("off")

    # context roads in view
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

    # nested widths so shared stretches show every route: the fastest is a
    # wide grey band underneath, then +50 %, +30 %, +15 % narrower on top
    handles = [None] * len(rows)
    widths = [7.0, 1.6, 3.0, 4.6]  # fastest, +15, +30, +50
    for k in [0] + list(range(len(rows) - 1, 0, -1)):
        role, sr = rows[k]
        xy = np.asarray(sr.route.geometry)
        colour = _MUTED if k == 0 else _PICK_COLOURS[(k - 1) % 3]
        (h,) = ax.plot(xy[:, 0], xy[:, 1], color=colour, lw=widths[min(k, 3)], zorder=3 + (len(rows) - k) if k else 2,
                       solid_capstyle="round", solid_joinstyle="round",
                       label=f"{role:8s}{sr.minutes:4.0f} min  {sr.ratio:.2f}x  {sr.km:4.0f} km  "
                             f"{sr.scenic_min:3.0f} scenic min")
        handles[k] = h
    a, b = res.fastest.route.geometry[0], res.fastest.route.geometry[-1]
    for (x, y), lab in ((a, "A"), (b, "B")):
        ax.plot(x, y, "o", ms=9, color=_INK, mec=_SURFACE, mew=2, zorder=10)
        ax.annotate(lab, (x, y), xytext=(7, 7), textcoords="offset points", fontsize=11, weight="bold",
                    color=_INK, zorder=11)

    # credited moments of the picks, numbered
    mom = ctx.moments
    shown: list[int] = []
    for _, sr in rows[1:]:
        for i in sr.moments:
            if i not in shown:
                shown.append(i)
    for n, i in enumerate(shown, 1):
        ax.plot(mom.lon[i], mom.lat[i], "o", ms=7, color=_INK, mec=_SURFACE, mew=1.5, zorder=12)
        off = ((6, 4), (6, -11), (-14, 4), (-14, -11))[n % 4]
        ax.annotate(str(n), (mom.lon[i], mom.lat[i]), xytext=off, textcoords="offset points", fontsize=8,
                    color=_INK, zorder=13, path_effects=[pe.withStroke(linewidth=2.5, foreground=_SURFACE)])

    ax.set_xlim(lo0, lo1)
    ax.set_ylim(la0, la1)
    ax.set_aspect(aspect)
    ax.set_xticks([])
    ax.set_yticks([])
    for s in ax.spines.values():
        s.set_color(_MUTED)
    fig.text(0.25 / fig_w, 1 - 0.35 / fig_h,
             title or f"{res.t0 / 60:.0f}-minute fastest route and the best route under each budget",
             fontsize=13, color=_INK, weight="bold", va="center")
    leg = side.legend(handles=handles, loc="upper left", bbox_to_anchor=(0, 1), fontsize=8, frameon=False,
                      labelcolor=_INK, prop={"family": "monospace", "size": 7.5}, handlelength=2.2)
    leg._legend_box.align = "left"

    lines = ["Moments credited to the picks", ""]
    for n, i in enumerate(shown, 1):
        who = ",".join(role for role, sr in rows[1:] if i in sr.moments)
        lab = str(mom.label[i])
        lab = lab if len(lab) <= 46 else lab[:45] + "..."
        lines.append(f"{n:2d}  {lab}")
        lines.append(f"    {mom.type[i]} {mom.m[i]:.2f}  ({who})")
    side.text(0, 0.80, "\n".join(lines[:60]), va="top", ha="left", fontsize=7.5, family="monospace", color=_INK)
    fig.text(0.25 / fig_w, 0.15 / fig_h, "Thin grey: motorway to secondary roads. Wide grey band: fastest route. "
             "Dots: credited moments (water, vista, gorge, OSM viewpoint).", fontsize=8, color=_INK2)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=_SURFACE)
    plt.close(fig)
    return path
