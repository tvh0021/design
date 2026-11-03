import math
from urllib.parse import parse_qs, urlparse

import numpy as np
import pytest

from scenic.route import RoadGraph
from scenic.waypoints import (
    JUNCTION_CLEAR_M, PARALLEL_M, maps_url, reduce_to_waypoints, route_via,
)

LAT0, LON0 = 43.0, -77.0
KM_LAT = 1 / 111.195  # degrees of latitude per km
KM_LON = KM_LAT / math.cos(math.radians(LAT0))


def pt(x_km, y_km):
    """(lon, lat) of a point x km east and y km north of the toy origin."""
    return LON0 + x_km * KM_LON, LAT0 + y_km * KM_LAT


def build(points, edges):
    """points: {name: (x_km, y_km)}; edges: [(a, b, speed_m_s)], two-way,
    straight, length from the coordinates."""
    names = list(points)
    idx = {n: i for i, n in enumerate(names)}
    lon, lat = zip(*(pt(*points[n]) for n in names))
    u = [idx[a] for a, _, _ in edges]
    v = [idx[b] for _, b, _ in edges]
    length = [1000 * math.dist(points[a], points[b]) for a, b, _ in edges]
    time = [L / s for L, (_, _, s) in zip(length, edges)]
    g = RoadGraph(lon, lat, u, v, [False] * len(edges), time, length)
    return g, idx


def edge_id(g, idx, a, b):
    a, b = idx[a], idx[b]
    for e in range(g.n_edges):
        if {int(g.u[e]), int(g.v[e])} == {a, b}:
            return e
    raise KeyError((a, b))


# A fast road A-B along the bottom and a slow detour A-C-...-D-B 2 km north.
FAST, SLOW = 30.0, 15.0


def detour_graph(extra_points=None, extra_edges=None, detour=("C", "D")):
    pts = {"A": (0, 0), "B": (10, 0), "C": (0, 2), "D": (10, 2)}
    pts.update(extra_points or {})
    path = ["A", *detour, "B"]
    edges = [("A", "B", FAST)] + [(a, b, SLOW) for a, b in zip(path[:-1], path[1:])]
    edges += extra_edges or []
    g, idx = build(pts, edges)
    return g, idx, [idx[n] for n in path]


def test_fastest_route_needs_no_waypoints():
    g, idx, _ = detour_graph()
    target = g.route(idx["A"], idx["B"])
    res = reduce_to_waypoints(g, target)
    assert res.count == 0 and res.needed == 0 and not res.capped
    assert res.overlap == pytest.approx(1.0)
    assert res.eta_s == pytest.approx(target.time_s)
    assert res.url.endswith("travelmode=driving")


def test_detour_off_fast_road_gets_one_waypoint_on_the_detour():
    g, idx, nodes = detour_graph()
    res = reduce_to_waypoints(g, nodes)
    assert res.count == 1
    w = res.waypoints[0]
    assert w.edge == edge_id(g, idx, "C", "D")
    # Reaching x km along C-D: via C 133 + 66.7x s, via B and D 466 + 66.7(10 - x) s.
    # They tie at x = 7.5 km; the U-turn margin keeps the waypoint short of that.
    x = (w.lon - LON0) / KM_LON
    assert 6.0 < x < 7.5
    assert res.overlap == pytest.approx(1.0, abs=1e-6)
    assert res.eta_s == pytest.approx(res.target_eta_s, rel=1e-6)
    assert not any(leg.uturn or leg.uturn_risk for leg in res.legs)
    assert res.meets_target


def test_edge_path_and_route_inputs_agree():
    g, idx, nodes = detour_graph()
    target = g.route(idx["A"], idx["B"], edge_cost=np.where(np.arange(g.n_edges) == 0, 1e6, 1.0))
    assert list(target.nodes) == nodes
    a = reduce_to_waypoints(g, target)
    b = reduce_to_waypoints(g, edge_path=target.edges)
    assert [w.latlon for w in a.waypoints] == [w.latlon for w in b.waypoints]


def test_placement_avoids_junction_and_parallel_road():
    # Junction Q 7.2 km along the detour (a dead-end side road goes south), and
    # an unconnected road 30 m north of the detour from 5.5 to 7.1 km. Without
    # them the waypoint would sit near 7.2 km (see the test above).
    off = 0.03
    g, idx, nodes = detour_graph(
        extra_points={"Q": (7.2, 2), "R": (7.2, 1.7), "P1": (5.5, 2 + off), "P2": (7.1, 2 + off)},
        extra_edges=[("Q", "R", SLOW), ("P1", "P2", SLOW)],
        detour=("C", "Q", "D"),
    )
    res = reduce_to_waypoints(g, nodes)
    assert res.count == 1
    w = res.waypoints[0]
    assert w.tier == 1
    assert w.edge == edge_id(g, idx, "C", "Q")
    assert w.clearance_m >= JUNCTION_CLEAR_M
    assert w.nearest_other_m > PARALLEL_M
    x = (w.lon - LON0) / KM_LON
    assert 5.0 < x < 5.5 - PARALLEL_M / 1000 + 0.01  # slid back past the parallel road
    assert res.overlap == pytest.approx(1.0, abs=1e-6)


def test_parallel_road_alone_is_avoided():
    g, idx, nodes = detour_graph(
        extra_points={"P1": (6.0, 2.02), "P2": (7.6, 2.02)},
        extra_edges=[("P1", "P2", SLOW)],
    )
    res = reduce_to_waypoints(g, nodes)
    w = res.waypoints[0]
    assert w.nearest_other_m > PARALLEL_M
    assert (w.lon - LON0) / KM_LON < 6.0


def test_two_detours_need_two_waypoints_and_the_cap_keeps_the_better_one():
    # Fast A-M-B; detour 1 A-C-D-M is 2 km north, detour 2 M-E-F-B is 1 km north.
    pts = {"A": (0, 0), "M": (10, 0), "B": (20, 0), "C": (0, 2), "D": (10, 2),
           "E": (10, 1), "F": (20, 1)}
    path = ["A", "C", "D", "M", "E", "F", "B"]
    edges = [("A", "M", FAST), ("M", "B", FAST)] + [(a, b, SLOW) for a, b in zip(path[:-1], path[1:])]
    g, idx = build(pts, edges)
    nodes = [idx[n] for n in path]
    full = reduce_to_waypoints(g, nodes)
    assert full.count == 2 and not full.capped
    assert full.overlap == pytest.approx(1.0, abs=1e-6)

    capped = reduce_to_waypoints(g, nodes, max_waypoints=1)
    assert capped.capped and capped.needed == 2 and capped.count == 1
    assert capped.overlap < 1.0
    # Keeping the waypoint on the longer detour (14 km vs 12 km) loses less.
    assert capped.waypoints[0].edge == edge_id(g, idx, "C", "D")
    assert capped.eta_s < full.eta_s


def test_oneway_detour_waypoint_is_on_the_detour():
    g, idx, nodes = detour_graph()
    ow = g.oneway.copy()
    ow[edge_id(g, idx, "C", "D")] = True  # C -> D only
    g2 = RoadGraph(g.node_lon, g.node_lat, g.u, g.v, ow, g.time_s, g.length_m)
    res = reduce_to_waypoints(g2, nodes)
    assert res.count == 1 and res.overlap == pytest.approx(1.0, abs=1e-6)


def test_route_via_drives_through_a_mid_edge_point():
    g, idx, nodes = detour_graph()
    lon, lat = pt(5, 2)  # middle of C-D
    path = route_via(g, [(LAT0, LON0), (lat, lon), pt(10, 0)[::-1]])
    assert list(path) == nodes


def test_maps_url_encodes_pipe():
    url = maps_url((43.157291, -77.615214), (43.04812, -76.14742), [(42.9, -77.1), (42.95, -76.5)])
    assert url == ("https://www.google.com/maps/dir/?api=1&origin=43.15729,-77.61521"
                   "&destination=43.04812,-76.14742&travelmode=driving"
                   "&waypoints=42.90000,-77.10000%7C42.95000,-76.50000")
    assert "|" not in url
    q = parse_qs(urlparse(url).query)
    assert q["waypoints"] == ["42.90000,-77.10000|42.95000,-76.50000"]
    assert maps_url((1, 2), (3, 4)).endswith("travelmode=driving")


def test_maps_url_length_limit():
    with pytest.raises(AssertionError):
        maps_url((43, -77), (43, -76), [(43.12345, -76.12345)] * 120)


def test_other_carriageway_is_not_a_parallel_road():
    import pyarrow as pa

    # The detour C-D is a divided road: C -> D eastbound, D2 -> C2 westbound 20 m north.
    pts = {"A": (0, 0), "B": (10, 0), "C": (0, 2), "D": (10, 2), "C2": (0, 2.02), "D2": (10, 2.02)}
    names = list(pts)
    idx = {n: i for i, n in enumerate(names)}
    lon, lat = zip(*(pt(*pts[n]) for n in names))
    E = [("A", "B", FAST, False, "Fast Road"), ("A", "C", SLOW, False, "West Road"),
         ("C", "D", SLOW, True, "Main Street"), ("D", "B", SLOW, False, "East Road"),
         ("D2", "C2", SLOW, True, "Main Street"), ("C", "C2", SLOW, False, "Main Street"),
         ("D", "D2", SLOW, False, "Main Street")]
    length = [1000 * math.dist(pts[a], pts[b]) for a, b, *_ in E]
    table = pa.table({"way_id": list(range(100, 100 + len(E))), "name": [e[4] for e in E],
                      "ref": [""] * len(E), "highway": ["secondary"] * len(E)})
    g = RoadGraph(lon, lat, [idx[e[0]] for e in E], [idx[e[1]] for e in E], [e[3] for e in E],
                  [L / e[2] for L, e in zip(length, E)], length, edges_table=table)
    res = reduce_to_waypoints(g, [idx[n] for n in ("A", "C", "D", "B")])
    assert res.count == 1
    w = res.waypoints[0]
    assert w.edge == 2 and w.road == "Main Street"
    assert w.nearest_other_m > PARALLEL_M  # the westbound carriageway does not block
    assert w.divided and w.carriageway_m == pytest.approx(20, abs=2)
    assert w.tier == 2  # closer than CARRIAGEWAY_M, so only a fallback spot
    assert res.overlap == pytest.approx(1.0, abs=1e-6)


def test_parallel_edge_with_same_ends_is_not_the_same_road():
    """Two different roads join the same junctions: a fast straight one and
    a slow one bowed 2 km north. The target takes the slow one, so overlap
    must not count the fast one as following it."""
    shapes = [[(0, 0), (10, 0)], [(0, 0), (5, 2), (10, 0)]]
    length = [10_000.0, 2 * 1000 * math.dist((0, 0), (5, 2))]
    time = [length[0] / FAST, length[1] / SLOW]
    off = np.cumsum([0] + [len(sh) for sh in shapes])
    glon, glat = zip(*(pt(*p) for sh in shapes for p in sh))
    lon, lat = zip(pt(0, 0), pt(10, 0))
    g = RoadGraph(lon, lat, [0, 0], [1, 1], [False, False], time, length,
                  geom_off=off, geom_lon=np.array(glon), geom_lat=np.array(glat))
    res = reduce_to_waypoints(g, edge_path=[1])
    assert res.count == 1
    assert res.overlap == pytest.approx(1.0)
    fast_only = reduce_to_waypoints(g, edge_path=[1], max_waypoints=0)
    assert fast_only.overlap < 0.1 and not fast_only.meets_target
