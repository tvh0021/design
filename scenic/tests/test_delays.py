"""Delay features (signals, stops, crossings, junctions, built-up), the
edge_delays.parquet file and its alignment with edges.parquet, and the fit."""

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from scenic import graph as G
from scenic.calibrate import FREE_FLOW, PARAM_NAMES, PRIORS, fit_params, osrm_motorway_share, sample_pairs
from scenic.graph import (
    WayCollector, build_edge_delays, build_graph, control_kind, delayed_time, edge_delay_features,
    make_controls, save_graph, simplify, write_edge_delays,
)
from scenic.route import RoadGraph

D = 0.001  # degrees; ~81 m east-west and ~111 m north-south at 43 N


def toy_ways():
    """Primary 1-2-3-4-5, secondary 5-6-7 continuing it, tertiary 20-21-3
    joining from the north at 3, and a separate motorway 30-31-32."""
    c = WayCollector()
    c.add(1, {"highway": "primary"}, [(n, -77 + n * D, 43.0) for n in (1, 2, 3, 4, 5)])
    c.add(4, {"highway": "secondary"}, [(n, -77 + n * D, 43.0) for n in (5, 6, 7)])
    c.add(2, {"highway": "tertiary"}, [(20, -77 + 3 * D, 43 + 2 * D), (21, -77 + 3 * D, 43 + D),
                                       (3, -77 + 3 * D, 43.0)])
    c.add(3, {"highway": "motorway"}, [(n, -77 + n * D, 43.1) for n in (30, 31, 32)])
    return c.finish()


def edge_index(g, a, b):
    ia, ib = np.searchsorted(g.node_osm_id, [a, b])
    hit = np.flatnonzero(((g.u == ia) & (g.v == ib)) | ((g.u == ib) & (g.v == ia)))
    assert len(hit) == 1
    return int(hit[0])


def ctrl(node_id, tags, ways):
    """One control row at a way node's location."""
    k = int(np.flatnonzero(ways.node_id == node_id)[0])
    return (node_id, ways.lon[k], ways.lat[k], *control_kind(tags))


@pytest.fixture
def toy():
    w = toy_ways()
    g = simplify(w)
    e = {name: edge_index(g, a, b) for name, (a, b) in
         {"p1": (1, 3), "p2": (3, 5), "sec": (5, 7), "ter": (20, 3), "mw": (30, 32)}.items()}
    return w, g, e


# ---- tags ------------------------------------------------------------------


@pytest.mark.parametrize("tags, expected", [
    ({"highway": "traffic_signals"}, (0, 1.0, True)),
    ({"highway": "traffic_signals", "traffic_signals:direction": "forward"}, (0, 0.5, True)),
    ({"highway": "stop"}, (1, 1.0, False)),
    ({"highway": "stop", "direction": "backward"}, (1, 0.5, False)),
    ({"highway": "stop", "stop": "all"}, (1, 1.0, True)),
    ({"highway": "crossing", "crossing": "traffic_signals"}, (2, 1.0, True)),
    ({"railway": "level_crossing"}, (2, 1.0, True)),
    ({"highway": "crossing", "crossing": "uncontrolled"}, None),
    ({"highway": "give_way"}, None),
])
def test_control_kind(tags, expected):
    assert control_kind(tags) == expected


def test_signalised_crossing_next_to_signal_is_merged():
    rows = [
        (1, -77.0, 43.0, *control_kind({"highway": "traffic_signals"})),
        (2, -77.0002, 43.0, *control_kind({"highway": "crossing", "crossing": "traffic_signals"})),  # 16 m
        (3, -77.01, 43.0, *control_kind({"highway": "crossing", "crossing": "traffic_signals"})),  # 800 m
    ]
    c = make_controls(rows)
    assert list(c.node_id) == [1, 3]
    assert list(c.kind) == [0, 2]


# ---- features --------------------------------------------------------------


def test_interior_and_junction_signals(toy):
    w, g, e = toy
    c = make_controls([ctrl(2, {"highway": "traffic_signals"}, w),   # interior of p1
                       ctrl(3, {"highway": "traffic_signals"}, w)])  # junction of p1, p2, ter
    f = edge_delay_features(w, g, c)
    assert f["signals"][e["p1"]] == 1.5
    assert f["signals"][e["p2"]] == 0.5
    assert f["signals"][e["ter"]] == 0.5
    # Driving 1 -> 3 -> 5 passes two signals; the junction one is paid once.
    assert f["signals"][e["p1"]] + f["signals"][e["p2"]] == 2.0


def test_directional_stop_counts_half(toy):
    w, g, e = toy
    f = edge_delay_features(w, g, make_controls([ctrl(21, {"highway": "stop", "direction": "forward"}, w)]))
    assert f["stops"][e["ter"]] == 0.5
    assert f["stops"].sum() == 0.5


def test_two_way_stop_on_junction_only_for_minor_road(toy):
    w, g, e = toy
    f = edge_delay_features(w, g, make_controls([ctrl(3, {"highway": "stop"}, w)]))
    assert f["stops"][e["ter"]] == 0.5
    assert f["stops"][e["p1"]] == 0 and f["stops"][e["p2"]] == 0
    f = edge_delay_features(w, g, make_controls([ctrl(3, {"highway": "stop", "stop": "all"}, w)]))
    assert f["stops"][e["ter"]] == f["stops"][e["p1"]] == f["stops"][e["p2"]] == 0.5


def test_crossings_and_motorway(toy):
    w, g, e = toy
    c = make_controls([ctrl(4, {"highway": "crossing", "crossing": "traffic_signals"}, w),
                       ctrl(6, {"railway": "level_crossing"}, w),
                       ctrl(31, {"highway": "traffic_signals"}, w)])
    f = edge_delay_features(w, g, c)
    assert f["crossings"][e["p2"]] == 1 and f["crossings"][e["sec"]] == 1
    assert f["signals"][e["mw"]] == 0  # motorways carry no delays
    assert f["motorway"][e["mw"]] and not f["motorway"][e["p1"]]


def test_minor_ends(toy):
    w, g, e = toy
    f = edge_delay_features(w, g, make_controls([]))
    assert f["minor_ends"][e["ter"]] == 0.5  # tertiary meets primary at a 3-way junction
    assert f["minor_ends"][e["p1"]] == f["minor_ends"][e["p2"]] == 0  # primary is the top class
    assert f["minor_ends"][e["sec"]] == 0  # class change where the road just continues


def test_built_up_from_junction_density(toy, monkeypatch):
    w, g, e = toy
    monkeypatch.setattr(G, "BUILT_UP_MIN_JUNCTIONS", 4)
    f = edge_delay_features(w, g, make_controls([]))
    assert f["junctions_300m"][e["p1"]] == 4  # nodes 1, 3, 5, 20 within 300 m of node 2
    assert f["built_up"][e["p1"]]
    assert f["junctions_300m"][e["mw"]] == 2 and not f["built_up"][e["mw"]]


def test_delayed_time():
    feats = {"motorway": np.array([True, False, False]), "built_up": np.array([False, False, True]),
             "signals": np.array([0.0, 1.0, 2.0]), "crossings": np.array([0.0, 2.0, 0.0]),
             "stops": np.array([0.0, 0.5, 0.0]), "minor_ends": np.array([0.0, 0.5, 1.0])}
    p = {"motorway": 1.1, "rural": 1.2, "built_up": 1.5, "signal_s": 10.0, "stop_s": 4.0, "minor_end_s": 2.0}
    t = delayed_time(np.array([100.0, 100.0, 100.0]), feats, p)
    assert t == pytest.approx([110.0, 120 + 10 * (1 + G.CROSSING_AS_SIGNALS * 2) + 2 + 1, 150 + 20 + 2])


# ---- edge_delays.parquet ---------------------------------------------------


def test_load_uses_edge_delays(tmp_path, toy):
    w, g, e = toy
    save_graph(w, g, tmp_path)
    free = RoadGraph.load(tmp_path).time_s  # no edge_delays.parquet yet
    feats = edge_delay_features(w, g, make_controls([ctrl(3, {"highway": "traffic_signals"}, w)]))
    p = {**FREE_FLOW, "signal_s": 20.0}
    write_edge_delays(feats, free, tmp_path, p)
    rg = RoadGraph.load(tmp_path)
    assert rg.time_s == pytest.approx(free + 20.0 * feats["signals"])
    assert rg.time_s[e["p1"]] == pytest.approx(free[e["p1"]] + 10.0)
    assert RoadGraph.load(tmp_path, delays=False).time_s == pytest.approx(free)
    # Driving through the signalised junction costs one signal.
    r, _ = rg.fastest((43.0, -77 + D), (43.0, -77 + 5 * D))
    assert r.time_s == pytest.approx(free[e["p1"]] + free[e["p2"]] + 20.0)


def test_load_rejects_misaligned_edge_delays(tmp_path, toy):
    w, g, _ = toy
    save_graph(w, g, tmp_path)
    pq.write_table(pa.table({"time_s": pa.array([1.0, 2.0])}), tmp_path / "edge_delays.parquet")
    with pytest.raises(ValueError, match="rows"):
        RoadGraph.load(tmp_path)


def write_pbf(path, ways_spec, node_tags):
    """Tiny PBF: ways_spec = [(way_id, tags, [(node_id, lon, lat), ...])]."""
    import osmium

    nodes = {}
    for _, _, pts in ways_spec:
        for nid, lon, lat in pts:
            nodes[nid] = (lon, lat)
    with osmium.SimpleWriter(str(path)) as wr:
        for nid in sorted(nodes):
            wr.add_node(osmium.osm.mutable.Node(id=nid, version=1, location=nodes[nid],
                                                 tags=node_tags.get(nid, {})))
        for wid, tags, pts in ways_spec:
            wr.add_way(osmium.osm.mutable.Way(id=wid, version=1, nodes=[p[0] for p in pts], tags=tags))


TOY_PBF_WAYS = [
    (1, {"highway": "primary"}, [(n, -77 + n * D, 43.0) for n in (1, 2, 3, 4, 5)]),
    (2, {"highway": "tertiary"}, [(20, -77 + 3 * D, 43 + 2 * D), (21, -77 + 3 * D, 43 + D), (3, -77 + 3 * D, 43.0)]),
    (4, {"highway": "secondary"}, [(n, -77 + n * D, 43.0) for n in (5, 6, 7)]),
]


def test_build_graph_writes_aligned_edge_delays(tmp_path):
    pbf = tmp_path / "toy.osm.pbf"
    write_pbf(pbf, TOY_PBF_WAYS, {3: {"highway": "traffic_signals"}, 21: {"highway": "stop"}})
    out = tmp_path / "graph"
    build_graph(pbf, out)
    edges = pq.read_table(out / "edges.parquet")
    delays = pq.read_table(out / "edge_delays.parquet")
    assert delays.num_rows == edges.num_rows == 4
    assert delays.column("free_time_s").to_numpy() == pytest.approx(edges.column("time_s").to_numpy())
    assert delays.column("signals").to_numpy().sum() == pytest.approx(1.5)  # 3 edges meet at node 3
    # Recounting from the PBF against the same graph gives the same file.
    before = delays.column("time_s").to_numpy()
    build_edge_delays(pbf, out)
    assert pq.read_table(out / "edge_delays.parquet").column("time_s").to_numpy() == pytest.approx(before)


def test_build_edge_delays_refuses_a_different_graph(tmp_path):
    pbf = tmp_path / "toy.osm.pbf"
    write_pbf(pbf, TOY_PBF_WAYS, {})
    out = tmp_path / "graph"
    build_graph(pbf, out)
    other = tmp_path / "other.osm.pbf"
    write_pbf(other, TOY_PBF_WAYS[:2], {})
    with pytest.raises(RuntimeError, match="rebuild the graph"):
        build_edge_delays(other, out)


# ---- calibration -----------------------------------------------------------


def test_fit_recovers_known_parameters():
    rng = np.random.default_rng(0)
    truth = {"motorway": 1.05, "rural": 1.1, "built_up": 1.3,
             **{k: m for k, (m, _) in PRIORS.items()}}  # delays at the prior means
    p = np.array([truth[k] for k in PARAM_NAMES])
    Xg = np.abs(rng.normal([3000, 2000, 800, 30, 3, 10], [1500, 1000, 400, 15, 2, 5], size=(4, 6)))
    Xo = np.abs(rng.normal([50, 3000, 600, 10, 5, 15], [50, 1500, 400, 8, 4, 8], size=(40, 6)))
    po = p.copy()
    po[PARAM_NAMES.index("signal_s")] = 2.0  # OSRM's own signal cost
    beta = 0.8
    fit, b = fit_params(Xg, Xg @ p, Xo, Xo @ po / beta)
    assert b == pytest.approx(beta, rel=1e-3)
    for k in PARAM_NAMES:
        assert fit[k] == pytest.approx(truth[k], rel=1e-2, abs=0.05), k


def test_osrm_motorway_share():
    resp = {"routes": [{"distance": 1000.0, "legs": [{"steps": [
        {"distance": 600.0, "ref": "I 90", "name": "New York State Thruway"},
        {"distance": 300.0, "ref": "US 20; NY 5", "name": ""},
        {"distance": 100.0, "name": "Main Street"},
    ]}]}]}
    assert osrm_motorway_share(resp) == pytest.approx(0.6)


def test_sample_pairs_distance_band():
    villages = [(f"v{i}", 42 + 0.1 * i, -76 + 0.05 * (i % 7)) for i in range(40)]
    pairs = sample_pairs(villages, 10)
    assert len(pairs) == 10
    for (_, la, lo), (_, lb, lob) in pairs:
        km = G.haversine_m(lo, la, lob, lb) / 1000
        assert 20 <= km <= 80
