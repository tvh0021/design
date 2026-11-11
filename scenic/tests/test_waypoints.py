import math
from urllib.parse import parse_qs, urlparse

import numpy as np
import pytest

from scenic.route import RoadGraph
from scenic.waypoints import (
    EDITABLE_STOPS, GAP_TOL_M, JUNCTION_CLEAR_M, MAX_GAP_M, MAX_URL, PARALLEL_M, TIME_MARGIN,
    UTURN_PENALTY_M, _slowed_matrix, editable_waypoint_cap, maps_url, reduce_to_waypoints, route_via,
    split_waypoints,
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


# -- robustness to a router that rates roads differently --


def long_detour_graph():
    """50 km of fast road A-M shared with the fastest route, then a 34 km
    detour M-C-D-B 2 km north of the fast road M-B."""
    pts = {"A": (-50, 0), "M": (0, 0), "B": (30, 0), "C": (0, 2), "D": (30, 2)}
    path = ["A", "M", "C", "D", "B"]
    edges = [("A", "M", FAST), ("M", "B", FAST)] + [(a, b, SLOW) for a, b in zip(path[1:-1], path[2:])]
    g, idx = build(pts, edges)
    return g, idx, [idx[n] for n in path]


def test_spacing_rule_spaces_waypoints_on_the_detour_only():
    g, idx, nodes = long_detour_graph()
    off = reduce_to_waypoints(g, nodes, max_gap_m=None)
    assert off.count == 1  # our own router needs one waypoint on the detour

    res = reduce_to_waypoints(g, nodes)
    assert res.max_gap_m == MAX_GAP_M and res.scenic_m == pytest.approx(34_000)
    assert res.count == 2 and not res.capped
    assert res.overlap == pytest.approx(1.0, abs=1e-6)
    assert all(leg.scenic_m <= MAX_GAP_M + GAP_TOL_M for leg in res.legs)
    assert res.risk_legs == [] and not any(leg.at_risk for leg in res.legs)
    # The 50 km on the fastest route's own road need no waypoint.
    assert res.legs[0].target_m > 50_000
    assert edge_id(g, idx, "A", "M") not in {w.edge for w in res.waypoints}
    assert res.summary()["max_scenic_gap_km"] <= MAX_GAP_M / 1000 + 0.05

    wide = reduce_to_waypoints(g, nodes, max_gap_m=20_000)
    assert wide.count == 1 and max(leg.scenic_m for leg in wide.legs) <= 20_000 + GAP_TOL_M


def test_spacing_rule_counts_only_roads_above_the_fastest_routes_score():
    g, idx, nodes = long_detour_graph()
    S = np.full(g.n_edges, 0.5)  # the fast road scores 0.5
    detour = [edge_id(g, idx, a, b) for a, b in (("M", "C"), ("C", "D"), ("D", "B"))]
    S[detour] = 1.0
    assert reduce_to_waypoints(g, nodes, scenic=S).count == 2
    S[edge_id(g, idx, "C", "D")] = 0.4  # the long middle is plain road
    res = reduce_to_waypoints(g, nodes, scenic=S)
    assert res.scenic_m == pytest.approx(4_000) and res.count == 1
    mask = S > 0.5
    assert reduce_to_waypoints(g, nodes, scenic=mask).count == 1


def wiggle_graph():
    """A 34 km scenic detour (A-C-...-D-B, 2 km north of the fast road A-B)
    whose middle bows 1 km north through H on faster road. A straight cut
    P1-P2 takes 545 s against the bow's 510 s: slower by our timings, faster
    once the bow is 10% slower."""
    pts = {"A": (0, 0), "B": (30, 0), "C": (0, 2), "P1": (10, 2), "H": (15, 3), "P2": (20, 2), "D": (30, 2)}
    path = ["A", "C", "P1", "H", "P2", "D", "B"]
    bow = {("P1", "H"): 20.0, ("H", "P2"): 20.0}
    edges = [("A", "B", FAST), ("P1", "P2", 10_000 / 545)]
    edges += [(a, b, bow.get((a, b), SLOW)) for a, b in zip(path[:-1], path[1:])]
    g, idx = build(pts, edges)
    return g, idx, [idx[n] for n in path], {edge_id(g, idx, "P1", "H"), edge_id(g, idx, "H", "P2")}


def test_time_margin_default_holds_a_road_only_a_little_faster_than_its_shortcut():
    assert 0.1 <= TIME_MARGIN <= 0.15
    g, idx, nodes, bow = wiggle_graph()
    res = reduce_to_waypoints(g, nodes, max_gap_m=None)
    assert res.time_margin == TIME_MARGIN
    assert res.count == 1 and res.waypoints[0].edge in bow  # on the bow, so no router can cut it
    assert res.overlap == pytest.approx(1.0, abs=1e-6)
    assert res.risk_legs == []

    plain = reduce_to_waypoints(g, nodes, max_gap_m=None, time_margin=0)
    assert plain.time_margin == 0
    assert plain.count == 1 and plain.waypoints[0].edge not in bow  # follows our router, but only just
    assert plain.overlap == pytest.approx(1.0, abs=1e-6)
    # The cut is 6.9% slower: a 5% margin does not need the bow held either.
    assert reduce_to_waypoints(g, nodes, max_gap_m=None, time_margin=0.05).waypoints[0].edge not in bow


def test_a_leg_that_fails_under_the_margin_is_flagged():
    g, idx, nodes, bow = wiggle_graph()
    res = reduce_to_waypoints(g, nodes, max_gap_m=None, max_waypoints=0)
    assert res.capped and res.count == 0
    leg = res.legs[0]
    assert leg.at_risk and leg.margin_skip_m == pytest.approx(34_200, rel=0.01)
    assert res.risk_legs == [0]
    assert any("10% slower" in n and "34.2 km" in n for n in res.notes)


def two_detour_graph():
    """Fast A-M-B; a 48 km detour A-C-D-M 2 km north and a 22 km detour M-E-F-B 1 km north."""
    pts = {"A": (0, 0), "M": (44, 0), "B": (64, 0), "C": (0, 2), "D": (44, 2), "E": (44, 1), "F": (64, 1)}
    path = ["A", "C", "D", "M", "E", "F", "B"]
    edges = [("A", "M", FAST), ("M", "B", FAST)] + [(a, b, SLOW) for a, b in zip(path[:-1], path[1:])]
    g, idx = build(pts, edges)
    return g, idx, [idx[n] for n in path]


def test_over_budget_keeps_every_detour_and_flags_the_longest_unguarded_stretch():
    g, idx, nodes = two_detour_graph()
    full = reduce_to_waypoints(g, nodes)
    assert full.count == 4 and not full.capped and full.risk_legs == []
    assert all(leg.scenic_m <= MAX_GAP_M + GAP_TOL_M for leg in full.legs)

    res = reduce_to_waypoints(g, nodes, max_waypoints=2)
    assert res.capped and res.needed == 4 and res.count == 2
    # A waypoint on each detour (both are needed to follow the route at all)
    # comes before spacing: a sure miss costs more than a likely one.
    assert res.overlap == pytest.approx(1.0, abs=1e-6)
    roads = {w.edge for w in res.waypoints}
    assert edge_id(g, idx, "C", "D") in roads and edge_id(g, idx, "E", "F") in roads
    # The legs left over the limit are flagged, most scenic road exposed first.
    risk = res.risk_legs
    assert risk and all(res.legs[i].scenic_m > MAX_GAP_M for i in risk)
    exposure = [res.legs[i].exposure_m for i in risk]
    assert exposure == sorted(exposure, reverse=True)
    assert res.legs[risk[0]].scenic_m == max(leg.scenic_m for leg in res.legs)
    flagged = [n for n in res.notes if n.startswith("leg ") and " at risk: " in n]
    assert len(flagged) == len(risk) and flagged[0].startswith(f"leg {risk[0] + 1} ")
    assert any("needed 4 waypoints; kept the 2" in n for n in res.notes)
    s = res.summary()
    assert s["risk_legs"] == len(risk) and s["max_scenic_gap_km"] > MAX_GAP_M / 1000

    one = reduce_to_waypoints(g, nodes, max_waypoints=1)
    assert one.count == 1 and one.waypoints[0].edge == edge_id(g, idx, "C", "D")  # the longer detour


def test_summary_keeps_old_keys_and_adds_risk():
    g, idx, nodes = long_detour_graph()
    s = reduce_to_waypoints(g, nodes).summary()
    old = {"waypoints", "needed", "capped", "overlap", "meets_target", "eta_min", "target_eta_min",
           "eta_ratio", "km", "target_km", "uturn_legs", "weak_waypoints"}
    assert old <= set(s)
    assert s["risk_legs"] == 0 and s["scenic_km"] == pytest.approx(34.0) and s["time_margin"] == TIME_MARGIN


def test_url_cap_holds_with_a_tight_spacing_limit():
    g, idx, nodes = long_detour_graph()
    res = reduce_to_waypoints(g, nodes, max_waypoints=500, max_gap_m=300)
    assert res.capped and res.needed > res.count
    assert len(res.url) < MAX_URL
    assert any(f"{MAX_URL}-character link" in n for n in res.notes)


@pytest.mark.parametrize("which, factor", [(0, 1.3), (1, 1.3), (0, 10.0)])
def test_slowed_matrix_matches_a_full_rebuild(which, factor):
    # Two parallel edges between the same junctions (0 fast, 1 slow), plus a
    # one-way edge: slowing the fast one by 10 makes the slow one the cheaper arc.
    pts = {"A": (0, 0), "B": (10, 0), "C": (5, 3)}
    g0, idx = build(pts, [("A", "B", FAST), ("A", "C", SLOW), ("C", "B", SLOW)])
    u = np.r_[g0.u, idx["A"]]
    v = np.r_[g0.v, idx["B"]]
    g = RoadGraph(g0.node_lon, g0.node_lat, u, v, [False, False, True, False],
                  np.r_[g0.time_s, 10_000 / SLOW], np.r_[g0.length_m, 10_000.0])
    edges = np.array([[0, 2], [3]][which])
    cost = np.array(g.time_s, dtype=np.float64)
    cost[edges] *= factor
    want, wsel, _ = g._matrix(cost)
    got, gsel, _ = _slowed_matrix(g, edges, factor)
    assert np.allclose(got.toarray(), want.toarray())
    assert np.array_equal(g.arc_edge[gsel], g.arc_edge[wsel])


def test_closed_loop_edge_path_reduces_to_waypoints_with_same_origin_and_destination():
    pts = {"H": (0, 0), "E": (12, 0), "N": (12, 10), "W": (0, 10)}
    ring = [("H", "E", SLOW), ("E", "N", SLOW), ("N", "W", SLOW), ("W", "H", SLOW)]
    # Add an interior chord so there are junctions and alternative paths.
    g, idx = build(pts, ring + [("H", "N", FAST)])
    edge_path = [edge_id(g, idx, a, b) for a, b, _ in ring]
    res = reduce_to_waypoints(g, edge_path=edge_path)
    assert res.count >= 1
    assert res.overlap == pytest.approx(1.0, abs=1e-6)
    assert res.meets_target
    q = parse_qs(urlparse(res.url).query)
    assert q["origin"] == q["destination"]
    assert "waypoints" in q and q["waypoints"][0]


def test_editable_waypoint_cap_is_seven_for_open_and_closed_routes():
    assert EDITABLE_STOPS == 9
    assert editable_waypoint_cap(False) == 7
    assert editable_waypoint_cap(True) == 7


def test_pruning_penalises_uturn_joins():
    # Three greedy waypoints on A -> B:
    # - Detour 1 (A-D1-D2-M, bypassed by A-M) loses ~3.8 km if w0 is dropped, with no U-turn.
    # - Detour 2 (M-U-V, bypassed by M-V) needs w1 on U-V, and V-B (~2.2 km slow) is bypassed by
    #   a fast highway U-B so it needs w2 on V-B. Dropping w2 makes leg w1 -> B reverse back along
    #   U-V to U (`exit_back`, a U-turn) with only ~2.2 km of plain miss. UTURN_PENALTY_M must
    #   outweigh Detour 1's ~3.8 km plain miss so max_waypoints=2 keeps {w1, w2} with 0 U-turns.
    assert UTURN_PENALTY_M >= 5_000.0
    pts = {
        "A": (0, 0), "D1": (0, 1.2), "D2": (1.4, 1.2), "M": (1.4, 0),
        "U": (4.0, 2.5), "V": (7.2, 2.5), "B": (8.0, 0.5),
    }
    edges = [
        ("A", "M", FAST),
        ("A", "D1", SLOW), ("D1", "D2", SLOW), ("D2", "M", SLOW),
        ("M", "V", FAST),
        ("M", "U", SLOW), ("U", "V", SLOW),
        ("U", "B", 45.0),
        ("V", "B", 4.0),
    ]
    g, idx = build(pts, edges)
    nodes = [idx[n] for n in ("A", "D1", "D2", "M", "U", "V", "B")]

    full = reduce_to_waypoints(g, nodes, max_gap_m=None)
    assert full.count == 3 and full.summary()["uturn_legs"] == 0

    capped = reduce_to_waypoints(g, nodes, max_waypoints=2, max_gap_m=None)
    assert capped.count == 2
    assert capped.summary()["uturn_legs"] == 0
    kept_edges = {w.edge for w in capped.waypoints}
    assert edge_id(g, idx, "V", "B") in kept_edges


def _chain_detour_graph(n_detours: int = 10):
    """Chain of n_detours independent scenic bows, each needing its own waypoint."""
    pts = {}
    edges = []
    open_names = ["J0"]
    pts["J0"] = (0.0, 0.0)
    for k in range(n_detours):
        x0, x1 = 8.0 * k, 8.0 * (k + 1)
        n1, n2, j1 = f"N{k}", f"P{k}", f"J{k + 1}"
        pts[n1] = (x0 + 2.0, 2.5)
        pts[n2] = (x1 - 2.0, 2.5)
        pts[j1] = (x1, 0.0)
        edges.append((f"J{k}", j1, FAST))
        edges.extend([(f"J{k}", n1, SLOW), (n1, n2, SLOW), (n2, j1, SLOW)])
        open_names.extend([n1, n2, j1])
    # Closing scenic south bow from J{n} back to J0 for closed-loop tests
    end = f"J{n_detours}"
    x_end = 8.0 * n_detours
    pts["S1"] = (x_end - 2.0, -3.0)
    pts["S2"] = (2.0, -3.0)
    edges.append((end, "J0", FAST))
    edges.extend([(end, "S1", SLOW), ("S1", "S2", SLOW), ("S2", "J0", SLOW)])
    g, idx = build(pts, edges)
    open_nodes = [idx[n] for n in open_names]
    loop_names = open_names + ["S1", "S2", "J0"]
    loop_edges = [edge_id(g, idx, a, b) for a, b in zip(loop_names[:-1], loop_names[1:])]
    return g, idx, open_nodes, loop_edges


def test_split_waypoints_returns_empty_parts_when_single_link_fits():
    g, _, nodes = two_detour_graph()
    single, full, parts = split_waypoints(g, nodes)
    assert parts == []
    assert single.count == full.count == 4
    assert not single.capped


def test_split_waypoints_splits_open_routes_and_closed_loops_under_editable_cap():
    g, _, open_nodes, loop_edges = _chain_detour_graph(10)

    # Open route needing >= 10 waypoints (editable single-link cap = 7)
    single, full, parts = split_waypoints(g, open_nodes)
    assert single.count <= editable_waypoint_cap(False) == 7
    assert single.capped
    assert len(parts) >= 2
    assert full.overlap == pytest.approx(1.0, abs=1e-6)
    assert full.summary()["uturn_legs"] == 0
    for k, p in enumerate(parts):
        assert len(p.waypoints) <= editable_waypoint_cap(False)
        q = parse_qs(urlparse(p.url).query)
        assert q["origin"] != q["destination"]
        if p.waypoints:
            assert len(q["waypoints"][0].split("|")) == len(p.waypoints) <= 7
        if k + 1 < len(parts):
            assert p.handoff_n is not None
            assert p.destination == parts[k + 1].origin
            assert full.waypoints[p.handoff_n - 1].latlon == p.destination
        else:
            assert p.handoff_n is None
            assert p.destination == full.destination

    # Closed loop needing >= 11 waypoints (editable single-link cap = 7, split part cap = 7)
    single_l, full_l, parts_l = split_waypoints(g, edge_path=loop_edges)
    assert single_l.origin == single_l.destination
    assert single_l.count <= editable_waypoint_cap(True) == 7
    assert len(parts_l) >= 2
    assert full_l.overlap == pytest.approx(1.0, abs=1e-6)
    assert full_l.summary()["uturn_legs"] == 0
    assert parts_l[0].origin == full_l.origin
    assert parts_l[-1].destination == full_l.destination
    for k, p in enumerate(parts_l):
        assert p.origin != p.destination
        assert len(p.waypoints) <= editable_waypoint_cap(False) == 7
        if k + 1 < len(parts_l):
            assert p.destination == parts_l[k + 1].origin

    # Closed loop needing exactly 8 waypoints must ALSO split so no link has 10 stops
    g8, _, _, loop8 = _chain_detour_graph(7)
    single8, full8, parts8 = split_waypoints(g8, edge_path=loop8, max_gap_m=None)
    assert full8.count == 8
    assert single8.count <= 7 and single8.capped
    assert len(parts8) == 2
    assert all(len(p.waypoints) <= 7 for p in parts8)


