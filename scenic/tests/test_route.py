import json

import numpy as np
import pytest

from scenic.graph import WayCollector, save_graph, simplify
from scenic.route import RoadGraph


def toy(oneway=None, lcc=None):
    """Square 0-1-3 / 0-2-3. Nodes on a ~1 km grid near (43, -77)."""
    lon = [-77.00, -76.99, -77.00, -76.99]
    lat = [43.00, 43.00, 43.01, 43.01]
    u = [0, 1, 0, 2]
    v = [1, 3, 2, 3]
    time_s = [10.0, 10.0, 5.0, 30.0]
    length_m = [800.0, 1100.0, 1100.0, 800.0]
    ow = oneway if oneway is not None else [False] * 4
    return RoadGraph(lon, lat, u, v, ow, time_s, length_m, lcc=lcc)


def test_fastest_by_time():
    r = toy().route(0, 3)
    assert list(r.nodes) == [0, 1, 3]
    assert r.time_s == 20 and r.length_m == 1900
    assert r.minutes == pytest.approx(20 / 60)


def test_custom_edge_cost_changes_route_but_not_reported_time():
    g = toy()
    cost = np.array([100.0, 100.0, 5.0, 30.0])
    r = g.route(0, 3, edge_cost=cost)
    assert list(r.nodes) == [0, 2, 3]
    assert r.cost == 35 and r.time_s == 35 and r.length_m == 1900


def test_reverse_traversal_of_two_way_edges():
    r = toy().route(3, 0)
    assert list(r.nodes) == [3, 1, 0]
    assert list(r.reversed) == [True, True]


def test_oneway_respected():
    # 1 -> 3 is oneway, so 3 -> 0 must go round via 2 (35 s, not 20 s).
    g = toy(oneway=[False, True, False, False])
    assert g.route(0, 3).time_s == 20
    assert list(g.route(3, 0).nodes) == [3, 2, 0]


def test_parallel_edges_keep_cheapest():
    # edge 0: two-way, 50 s; edge 1: 0 -> 1 only, 20 s; edge 2: 1 -> 0 only, 40 s
    g = RoadGraph([0, 0.01], [0, 0], [0, 0, 1], [1, 1, 0], [False, True, True],
                  [50.0, 20.0, 40.0], [1, 2, 3])
    r = g.route(0, 1)
    assert list(r.edges) == [1] and r.time_s == 20
    r = g.route(1, 0)
    assert list(r.edges) == [2] and r.time_s == 40


def test_unreachable_returns_none():
    g = toy(oneway=[True, True, True, True])
    assert g.route(3, 0) is None


def test_snap_only_to_largest_component():
    lcc = np.array([True, True, False, True])
    g = toy(lcc=lcc)
    n, d = g.snap(43.01, -77.00)  # exactly on node 2, which is outside the LCC
    assert n != 2
    n, d = g.snap(43.0, -76.99)
    assert n == 1 and d < 1


def test_bad_cost_rejected():
    with pytest.raises(ValueError):
        toy().route(0, 3, edge_cost=np.array([1.0, -1.0, 1.0, 1.0]))
    with pytest.raises(ValueError):
        toy().route(0, 3, edge_cost=np.ones(3))


def test_save_load_roundtrip(tmp_path):
    D = 0.001
    c = WayCollector()
    c.add(1, {"highway": "primary", "maxspeed": "45 mph", "ref": "NY 5"},
          [(n, -77 + n * D, 43.0) for n in (1, 2, 3)])
    c.add(2, {"highway": "residential", "name": "Elm St"},
          [(n, -77 + n * D, 43.0 + (n - 3) * D) for n in (3, 4, 5)])
    w = c.finish()
    stats = save_graph(w, simplify(w), tmp_path)
    assert stats["nodes"] == 3 and stats["edges"] == 2
    g = RoadGraph.load(tmp_path)
    r, snap = g.fastest((43.0, -77 + D), (43.0 + 2 * D, -77 + 5 * D))
    assert snap["from_snap_m"] < 1 and snap["to_snap_m"] < 1
    assert stats["lcc_nodes"] == 3
    assert len(r.edges) == 2
    assert r.time_s == pytest.approx(g.time_s.sum())
    assert g.edges_table.column("speed_mph").to_pylist() == [45, 25]  # tagged, default
    expected = g.length_m / (np.array([45.0, 25.0]) * 0.44704)
    assert g.time_s == pytest.approx(expected)
    gj = r.geojson()
    coords = gj["features"][0]["geometry"]["coordinates"]
    assert len(coords) == 5  # 3 + 3 points, shared junction counted once
    json.dumps(gj)
    assert dict(g.road_summary(r)).keys() == {"NY 5", "Elm St"}


def test_road_summary_names_unnamed_ramps_in_words():
    import types

    import pyarrow as pa

    table = pa.table({"ref": [None, None, "NY 5"], "name": [None, None, None],
                      "highway": ["motorway_link", "unclassified", "primary"]})
    fake = types.SimpleNamespace(edges_table=table, length_m=np.array([2000.0, 1000.0, 3000.0]))
    got = dict(RoadGraph.road_summary(fake, types.SimpleNamespace(edges=[0, 1, 2])))
    assert got == {"NY 5": 3.0, "ramp": 2.0, "unnamed road": 1.0}
