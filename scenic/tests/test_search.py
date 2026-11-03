import numpy as np
import pytest

from scenic.route import RoadGraph
from scenic.search import (CostGraph, Context, Moments, ScenicRoute, _to5070, _top_distinct, credit_moments,
                           geojson, passed_moments, score_route, search)

CFG = {"budgets": [1.15, 1.30, 1.50], "lambdas": [0.5, 1.0, 3.0, 8.0], "alt_lambdas": [1.0], "alt_rounds": 1,
       "reuse_penalty": 1.0, "via_lambda": 1.0, "via_top": 4, "via_top_per_band": 2, "via_max_moments": 2,
       "via_min_extra_min": 1.0, "moment_radius_m": 100.0, "moment_spacing_km": 2.0, "feature_spacing_km": 10.0,
       "moments_per_hour": 6.0, "moment_minutes": 5.0}


def ladder():
    """Fast bottom road 0-1-2-3-4 (4 x 60 s, S 0.2) and a slower scenic top
    road 0-5-6-7-4 (300 s, ratio 1.25, S 0.9) with a moment on 6-7.
    Nodes ~2.5 km apart near (43, -77)."""
    lon = [-77.00, -76.97, -76.94, -76.91, -76.88, -76.97, -76.94, -76.91]
    lat = [43.00, 43.00, 43.00, 43.00, 43.00, 43.03, 43.03, 43.03]
    u = [0, 1, 2, 3, 0, 5, 6, 7]
    v = [1, 2, 3, 4, 5, 6, 7, 4]
    t = [60.0, 60.0, 60.0, 60.0, 30.0, 120.0, 120.0, 30.0]
    L = [2500.0] * 8
    g = RoadGraph(lon, lat, u, v, [False] * 8, t, L)
    S = np.array([0.2] * 4 + [0.9] * 4)
    mx, my = _to5070([(-76.94 - 76.91) / 2], [43.03])
    mom = Moments(id=np.array([0]), m=np.array([1.0]), x=mx, y=my, lon=np.array([-76.925]), lat=np.array([43.03]),
                  edge=np.array([6]), type=np.array(["water"], dtype=object),
                  label=np.array(["Toy Lake on top road"], dtype=object),
                  of_edge=np.array([-1] * 6 + [0, -1]), feature=np.array(["Toy Lake"], dtype=object))
    return Context(g=g, S=S, moments=mom, cfg=dict(CFG))


def latlon(g, n):
    return float(g.node_lat[n]), float(g.node_lon[n])


def test_search_picks_scenic_route_only_inside_its_budget():
    ctx = ladder()
    res = search(ctx, latlon(ctx.g, 0), latlon(ctx.g, 4))
    assert res.t0 == pytest.approx(240)
    assert list(res.fastest.nodes) == [0, 1, 2, 3, 4]
    assert list(res.picks[1.15].nodes) == [0, 1, 2, 3, 4]  # top road is +25 %
    top = res.picks[1.30]
    assert list(top.nodes) == [0, 5, 6, 7, 4]
    assert top.ratio == pytest.approx(1.25)
    assert top.moments == [0]
    assert top.scenic_min == pytest.approx(300 * (0.9 - 0.2) / 60)
    assert top.score == pytest.approx(top.scenic_min + 5 * 1.0)
    assert res.picks[1.50] is top or list(res.picks[1.50].nodes) == [0, 5, 6, 7, 4]


def test_route_exposes_consistent_node_and_edge_paths():
    ctx = ladder()
    g = ctx.g
    res = search(ctx, latlon(g, 0), latlon(g, 4))
    for sr in res.kept:
        n, e = sr.nodes, sr.edges
        assert len(e) == len(n) - 1
        for k in range(len(e)):
            assert {int(g.u[e[k]]), int(g.v[e[k]])} == {int(n[k]), int(n[k + 1])}
        assert sr.route.time_s == pytest.approx(g.time_s[e].sum())


def test_cap_bounds_every_kept_route():
    ctx = ladder()
    ctx.cfg["budgets"] = [1.1, 1.2]  # top road (+25 %) is over the cap
    res = search(ctx, latlon(ctx.g, 0), latlon(ctx.g, 4))
    assert all(sr.ratio <= 1.2 + 1e-9 for sr in res.kept)
    assert all(list(sr.nodes) == [0, 1, 2, 3, 4] for sr in res.picks.values())


def test_via_moment_route_reaches_a_moment_the_sweep_misses():
    ctx = ladder()
    ctx.S = np.full(8, 0.5)  # nothing for the lambda sweep to prefer
    ctx.cfg["alt_rounds"] = 0  # and no reuse-penalty alternatives
    res = search(ctx, latlon(ctx.g, 0), latlon(ctx.g, 4))
    assert any(sr.vias == (0,) for sr in res.kept)
    assert res.picks[1.30].moments == [0]


def test_passed_moments_by_distance_and_by_edge():
    ctx = ladder()
    g = ctx.g
    top = g.route(0, 4, edge_cost=np.array([9.0] * 4 + [1.0] * 4))
    ids, pos = passed_moments(top, g, ctx.moments, 100.0)
    assert list(ids) == [0] and 5000 < pos[0] < 10000
    bottom = g.route(0, 4)
    assert len(passed_moments(bottom, g, ctx.moments, 100.0)[0]) == 0
    # far from the point but driving a member edge still counts
    far = Moments(**{**ctx.moments.__dict__, "x": ctx.moments.x + 1e5})
    assert list(passed_moments(top, g, far, 100.0)[0]) == [0]


def test_credit_moments_spacing_cap_and_feature_spacing():
    ids = np.array([10, 11, 12, 13])
    pos = np.array([0.0, 1000.0, 5000.0, 9000.0])
    m = np.array([0.5, 1.0, 0.8, 0.9])
    # 11 (m 1.0) blocks 10 (1 km away); then 13, 12
    assert list(credit_moments(ids, pos, m, 2000.0, cap=10)) == [11, 12, 13]
    assert list(credit_moments(ids, pos, m, 2000.0, cap=2)) == [11, 13]
    feat = ["Lake", None, "Lake", "Lake"]
    # same named lake needs 10 km: 13 then 12 blocked (4 km), 10 kept (9 km from 13)
    assert list(credit_moments(ids, pos, m, 2000.0, 10, feat, 10000.0)) == [11, 13]


def test_top_distinct_one_per_named_feature():
    assert _top_distinct([5, 6, 7, 8], ["A", "A", None, "B"], 3) == [5, 7, 8]
    assert _top_distinct([5, 6, 7], [None, None, None], 2) == [5, 6]


def test_score_is_relative_to_fastest_baseline():
    ctx = ladder()
    g = ctx.g
    r = g.route(0, 4)
    sr = score_route(ScenicRoute(route=r, origin="t"), ctx, t0=240.0, s_fast=0.2)
    assert sr.scenic_min == 0 and sr.ratio == pytest.approx(1.0) and sr.score == 0


def test_costgraph_dedupes_to_cheapest_arc():
    g = RoadGraph([0, 0.01], [0, 0], [0, 0], [1, 1], [False, False], [50.0, 20.0], [1, 2])
    G = CostGraph(g, np.arange(len(g.arc_edge)), g.time_s)
    p = G.path(0, 1)
    assert list(p) == [0, 1]
    assert g.arc_edge[G.arcs_between(p[:-1], p[1:])].tolist() == [1]
    with pytest.raises(ValueError):
        G.arcs_between(np.array([0]), np.array([0]))


def test_make_route_checks_arc_count():
    g = ladder().g
    with pytest.raises(ValueError):
        g.make_route([0, 1, 2], [0])


def test_geojson_has_paths_and_moment_points():
    ctx = ladder()
    res = search(ctx, latlon(ctx.g, 0), latlon(ctx.g, 4))
    fc = geojson(ctx, res)
    lines = [f for f in fc["features"] if f["geometry"]["type"] == "LineString"]
    assert [f["properties"]["role"] for f in lines] == ["fastest", "+15%", "+30%", "+50%"]
    assert lines[2]["properties"]["node_path"] == [0, 5, 6, 7, 4]
    assert any(f["properties"]["role"] == "moment" for f in fc["features"])


def test_top_distinct_takes_features_in_ranked_order():
    """feats must be aligned with the ranked order, not the original one."""
    feat = np.array(["Lake", "Lake", "Falls"], dtype=object)
    order = np.array([2, 0, 1])
    assert _top_distinct(order, feat[order], 2) == [2, 0]
