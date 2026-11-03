"""Routing on the statewide graph with scipy.sparse.csgraph.

`RoadGraph.route(..., edge_cost=...)` takes any positive per-edge cost array,
so a scenic cost (PLAN.md 2.3: t * (1 + lambda * (1 - S))) can replace travel
time later without touching this module. Reported minutes and km always come
from the edge's travel time and length, whatever cost drove the search.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

from . import data_dir
from .graph import EARTH_RADIUS_M, haversine_m

MIN_COST = 1e-6  # csgraph needs strictly positive weights on stored entries


def load_delay_time(path: Path, n_edges: int) -> np.ndarray:
    """time_s from edge_delays.parquet, which must have one row per edge of
    edges.parquet, in the same order."""
    import pyarrow.parquet as pq

    d = pq.read_table(path, columns=["time_s"])
    if d.num_rows != n_edges:
        raise ValueError(f"{path} has {d.num_rows} rows but edges.parquet has {n_edges}; "
                         "rerun `scenic calibrate --features`")
    return d.column("time_s").to_numpy()


@dataclass
class Route:
    nodes: np.ndarray  # node indices, source first
    edges: np.ndarray  # edge indices in travel order
    reversed: np.ndarray  # bool: edge driven v -> u
    time_s: float
    length_m: float
    cost: float
    geometry: list[tuple[float, float]] = field(default_factory=list)  # (lon, lat)

    @property
    def minutes(self) -> float:
        return self.time_s / 60

    @property
    def km(self) -> float:
        return self.length_m / 1000

    def geojson(self, properties: dict | None = None) -> dict:
        props = {"minutes": round(self.minutes, 2), "km": round(self.km, 3), "edges": int(len(self.edges))}
        props.update(properties or {})
        return {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": props,
                "geometry": {"type": "LineString", "coordinates": [list(p) for p in self.geometry]},
            }],
        }


class RoadGraph:
    """Directed road graph: every edge gives an arc u -> v, and two-way edges
    also v -> u. Parallel arcs between the same pair keep the cheapest."""

    def __init__(self, node_lon, node_lat, u, v, oneway, time_s, length_m,
                 lcc=None, geom_off=None, geom_lon=None, geom_lat=None, edges_table=None):
        self.node_lon = np.asarray(node_lon, dtype=np.float64)
        self.node_lat = np.asarray(node_lat, dtype=np.float64)
        self.u = np.asarray(u, dtype=np.int64)
        self.v = np.asarray(v, dtype=np.int64)
        self.oneway = np.asarray(oneway, dtype=bool)
        self.time_s = np.asarray(time_s, dtype=np.float64)
        self.length_m = np.asarray(length_m, dtype=np.float64)
        self.n_nodes = len(self.node_lon)
        self.n_edges = len(self.u)
        self.lcc = np.ones(self.n_nodes, bool) if lcc is None else np.asarray(lcc, dtype=bool)
        self.geom_off = geom_off
        self.geom_lon = geom_lon
        self.geom_lat = geom_lat
        self.edges_table = edges_table

        two = ~self.oneway
        idx = np.arange(self.n_edges)
        self.arc_tail = np.concatenate([self.u, self.v[two]])
        self.arc_head = np.concatenate([self.v, self.u[two]])
        self.arc_edge = np.concatenate([idx, idx[two]])
        self.arc_rev = np.concatenate([np.zeros(self.n_edges, bool), np.ones(two.sum(), bool)])
        loop = self.arc_tail == self.arc_head
        if loop.any():
            for name in ("arc_tail", "arc_head", "arc_edge", "arc_rev"):
                setattr(self, name, getattr(self, name)[~loop])

        self._lat0 = float(np.radians(self.node_lat.mean())) if self.n_nodes else 0.0
        snap_nodes = np.flatnonzero(self.lcc)
        self._snap_nodes = snap_nodes
        self._kdtree = cKDTree(self._project(self.node_lon[snap_nodes], self.node_lat[snap_nodes]))
        self._cache = None

    # ---- loading -----------------------------------------------------------

    @classmethod
    def load(cls, graph_dir: Path | None = None, delays: bool = True) -> "RoadGraph":
        """Load data/graph. Travel time comes from edge_delays.parquet (free
        flow plus signal, stop and junction delays) when that file exists and
        `delays` is true, else from the free-flow time_s in edges.parquet."""
        import pyarrow.parquet as pq

        graph_dir = Path(graph_dir or data_dir() / "graph")
        nodes = np.load(graph_dir / "nodes.npz")
        t = pq.read_table(graph_dir / "edges.parquet")
        time_s = t.column("time_s").to_numpy()
        delay_path = graph_dir / "edge_delays.parquet"
        if delays and delay_path.exists():
            time_s = load_delay_time(delay_path, t.num_rows)
        lon = t.column("lon").combine_chunks()
        lat = t.column("lat").combine_chunks()
        return cls(
            nodes["lon"], nodes["lat"],
            t.column("u").to_numpy(), t.column("v").to_numpy(), t.column("oneway").to_numpy(),
            time_s, t.column("length_m").to_numpy(),
            lcc=nodes["lcc"],
            geom_off=lon.offsets.to_numpy(), geom_lon=lon.values.to_numpy(), geom_lat=lat.values.to_numpy(),
            edges_table=t.drop_columns(["lon", "lat"]),
        )

    def set_time(self, time_s: np.ndarray) -> None:
        """Replace the per-edge travel time (and drop the cached matrix)."""
        time_s = np.asarray(time_s, dtype=np.float64)
        if time_s.shape != (self.n_edges,):
            raise ValueError(f"time_s must have shape ({self.n_edges},)")
        self.time_s = time_s
        self._cache = None

    # ---- snapping ----------------------------------------------------------

    def _project(self, lon, lat):
        lon = np.radians(np.asarray(lon, dtype=np.float64))
        lat = np.radians(np.asarray(lat, dtype=np.float64))
        return np.column_stack([EARTH_RADIUS_M * lon * np.cos(self._lat0), EARTH_RADIUS_M * lat])

    def snap(self, lat: float, lon: float) -> tuple[int, float]:
        """Nearest node in the largest strongly connected component, and its
        great-circle distance in metres."""
        _, i = self._kdtree.query(self._project([lon], [lat])[0])
        n = int(self._snap_nodes[i])
        return n, float(haversine_m(lon, lat, self.node_lon[n], self.node_lat[n]))

    # ---- search ------------------------------------------------------------

    def _matrix(self, edge_cost: np.ndarray):
        """CSR matrix over deduplicated arcs, plus the arc chosen per entry."""
        default = edge_cost is self.time_s  # only the travel-time matrix is cached
        if default and self._cache is not None:
            return self._cache
        arc_cost = np.maximum(np.asarray(edge_cost, dtype=np.float64)[self.arc_edge], MIN_COST)
        order = np.lexsort((arc_cost, self.arc_head, self.arc_tail))
        t, h = self.arc_tail[order], self.arc_head[order]
        first = np.ones(len(order), bool)
        first[1:] = (t[1:] != t[:-1]) | (h[1:] != h[:-1])
        sel = order[first]
        tail, head = self.arc_tail[sel], self.arc_head[sel]
        indptr = np.concatenate([[0], np.cumsum(np.bincount(tail, minlength=self.n_nodes))])
        m = csr_matrix((arc_cost[sel], head, indptr), shape=(self.n_nodes, self.n_nodes))
        out = (m, sel, indptr)
        if default:
            self._cache = out
        return out

    def route(self, src: int, dst: int, edge_cost: np.ndarray | None = None) -> Route | None:
        """Cheapest path from node `src` to node `dst` under `edge_cost`
        (default: travel time in seconds). None if unreachable."""
        cost = self.time_s if edge_cost is None else np.asarray(edge_cost)
        if cost.shape != (self.n_edges,):
            raise ValueError(f"edge_cost must have shape ({self.n_edges},)")
        if np.any(cost < 0) or not np.all(np.isfinite(cost)):
            raise ValueError("edge_cost must be finite and non-negative")
        m, sel, indptr = self._matrix(cost)
        dist, pred = dijkstra(m, directed=True, indices=src, return_predecessors=True)
        if not np.isfinite(dist[dst]):
            return None
        path = [dst]
        while path[-1] != src:
            path.append(int(pred[path[-1]]))
        path = np.asarray(path[::-1], dtype=np.int64)

        heads = m.indices
        arcs = np.empty(len(path) - 1, dtype=np.int64)
        for k in range(len(path) - 1):
            a, b = path[k], path[k + 1]
            lo, hi = indptr[a], indptr[a + 1]
            arcs[k] = sel[lo + np.searchsorted(heads[lo:hi], b)]
        edges = self.arc_edge[arcs]
        rev = self.arc_rev[arcs]
        return Route(
            nodes=path, edges=edges, reversed=rev,
            time_s=float(self.time_s[edges].sum()),
            length_m=float(self.length_m[edges].sum()),
            cost=float(dist[dst]),
            geometry=self._geometry(path, edges, rev),
        )

    def fastest(self, a_latlon: tuple[float, float], b_latlon: tuple[float, float],
                edge_cost: np.ndarray | None = None) -> tuple[Route | None, dict]:
        src, ds = self.snap(*a_latlon)
        dst, dd = self.snap(*b_latlon)
        return self.route(src, dst, edge_cost), {"from_node": src, "from_snap_m": ds,
                                                 "to_node": dst, "to_snap_m": dd}

    # ---- output ------------------------------------------------------------

    def _geometry(self, path, edges, rev) -> list[tuple[float, float]]:
        if self.geom_off is None:
            return [(float(self.node_lon[n]), float(self.node_lat[n])) for n in path]
        pts: list[tuple[float, float]] = []
        for e, r in zip(edges, rev):
            lo, hi = self.geom_off[e], self.geom_off[e + 1]
            xs, ys = self.geom_lon[lo:hi], self.geom_lat[lo:hi]
            if r:
                xs, ys = xs[::-1], ys[::-1]
            seg = list(zip(xs.tolist(), ys.tolist()))
            pts.extend(seg[1:] if pts else seg)
        return pts

    def road_summary(self, route: Route, top: int = 8) -> list[tuple[str, float]]:
        """Roads the route uses most, by km (label = ref, else name, else class)."""
        if self.edges_table is None:
            return []
        sub = self.edges_table.select(["ref", "name", "highway"]).take(route.edges).to_pylist()
        km: dict[str, float] = {}
        for row, e in zip(sub, route.edges):
            label = row["ref"] or row["name"] or row["highway"]
            km[label] = km.get(label, 0.0) + self.length_m[e] / 1000
        return sorted(km.items(), key=lambda kv: -kv[1])[:top]
