"""Unit tests for Mode 2 drive-around loop routes (src/scenic/loop.py)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from scenic.loop import (
    DEFAULTS,
    DIRECTIONS,
    LoopError,
    LoopRoute,
    Steer,
    _LoopBuilder,
    _bearing_from_en,
    _build_candidates,
    _pick_partners,
    _select_loops,
    _tangent_xy,
    compass_weight,
    compute_reach,
    direction_word,
    features_in_reach,
    loop_bearing,
    loop_config,
    loop_validity,
    loops,
    resolve_anchor,
)
from scenic.route import RoadGraph, Route
from scenic.search import Context, Moments, ScenicRoute, _to5070, score_route

BASE_CFG = {
    "budgets": [1.15, 1.30, 1.50],
    "lambdas": [0.5, 1.0, 3.0, 8.0],
    "alt_lambdas": [1.0],
    "alt_lambdas_rounds": 1,
    "alt_rounds": 1,
    "reuse_penalty": 1.0,
    "via_lambda": 1.0,
    "via_top": 4,
    "via_top_per_band": 2,
    "via_max_moments": 2,
    "via_min_extra_min": 1.0,
    "moment_radius_m": 100.0,
    "moment_spacing_km": 2.0,
    "feature_spacing_km": 10.0,
    "moments_per_hour": 6.0,
    "moment_minutes": 5.0,
}


def _make_moments(g: RoadGraph, specs: list[tuple[int, str, str, float, str | None, str | None]]) -> Moments:
    """specs: [(edge, type, label, m_val, feature, body), ...]."""
    n_edges = g.n_edges
    if not specs:
        empty_f = np.zeros(0, dtype=np.float64)
        empty_i = np.zeros(0, dtype=np.int64)
        empty_o = np.zeros(0, dtype=object)
        return Moments(
            id=empty_i,
            m=empty_f,
            x=empty_f,
            y=empty_f,
            lon=empty_f,
            lat=empty_f,
            edge=empty_i,
            type=empty_o,
            label=empty_o,
            of_edge=np.full(n_edges, -1, dtype=np.int64),
            feature=empty_o,
            group=empty_o,
        )
    edges = np.array([s[0] for s in specs], dtype=np.int64)
    u = g.u[edges]
    v = g.v[edges]
    mlon = 0.5 * (g.node_lon[u] + g.node_lon[v])
    mlat = 0.5 * (g.node_lat[u] + g.node_lat[v])
    mx, my = _to5070(mlon, mlat)
    of_edge = np.full(n_edges, -1, dtype=np.int64)
    for mid, e in enumerate(edges):
        of_edge[e] = mid
    return Moments(
        id=np.arange(len(specs), dtype=np.int64),
        m=np.array([s[3] for s in specs], dtype=np.float64),
        x=mx,
        y=my,
        lon=mlon,
        lat=mlat,
        edge=edges,
        type=np.array([s[1] for s in specs], dtype=object),
        label=np.array([s[2] for s in specs], dtype=object),
        of_edge=of_edge,
        feature=np.array([s[4] for s in specs], dtype=object),
        group=np.array([s[5] for s in specs], dtype=object),
    )


def make_grid_ctx(
    half: int = 4,
    dlat: float = 0.035,
    dlon: float = 0.048,
    east_only: bool = False,
    with_dead_end: bool = False,
) -> tuple[Context, tuple[float, float], dict[tuple[int, int], int]]:
    """2D road grid centered at (43.0, -77.0). Each step is ~3.9 km (~3.5 min)."""
    lat0, lon0 = 43.0, -77.0
    coords: dict[tuple[int, int], int] = {}
    lons: list[float] = []
    lats: list[float] = []
    j_range = range(0, half + 1) if east_only else range(-half, half + 1)
    for i in range(-half, half + 1):
        for j in j_range:
            coords[(i, j)] = len(lons)
            lats.append(lat0 + i * dlat)
            lons.append(lon0 + j * dlon)

    u_list: list[int] = []
    v_list: list[int] = []
    t_list: list[float] = []
    l_list: list[float] = []
    s_list: list[float] = []
    edge_by_pair: dict[tuple[tuple[int, int], tuple[int, int]], int] = {}

    for (i, j), u_idx in list(coords.items()):
        for di, dj, length_m, time_s in [(1, 0, 3890.0, 210.0), (0, 1, 3910.0, 210.0)]:
            nb = (i + di, j + dj)
            if nb not in coords:
                continue
            v_idx = coords[nb]
            e_idx = len(u_list)
            edge_by_pair[((i, j), nb)] = e_idx
            u_list.append(u_idx)
            v_list.append(v_idx)
            t_list.append(time_s)
            l_list.append(length_m)
            ring = max(abs(i), abs(j), abs(i + di), abs(j + dj))
            s_list.append(min(0.95, 0.35 + 0.14 * ring))

    dead_moment_edge = None
    dead_anchor_node = None
    if with_dead_end:
        # Dead-end spur off (2, 2) with a moment on it, and a second dead-end node off (-2, 2)
        base1 = coords[(2, 2)]
        dn1 = len(lons)
        lats.append(lats[base1] + 0.012)
        lons.append(lons[base1] + 0.012)
        dead_moment_edge = len(u_list)
        u_list.append(base1)
        v_list.append(dn1)
        t_list.append(90.0)
        l_list.append(1200.0)
        s_list.append(0.95)

        base2 = coords[(-2, 2)]
        dead_anchor_node = len(lons)
        lats.append(lats[base2] - 0.012)
        lons.append(lons[base2] + 0.012)
        u_list.append(base2)
        v_list.append(dead_anchor_node)
        t_list.append(90.0)
        l_list.append(1200.0)
        s_list.append(0.85)

    g = RoadGraph(lons, lats, u_list, v_list, [False] * len(u_list), t_list, l_list)
    S = np.asarray(s_list, dtype=np.float64)

    mspecs: list[tuple[int, str, str, float, str | None, str | None]] = []
    candidates = [
        (((half, -1), (half, 0)), "water", "West Cove", 1.2, "West Cove", "Ontario Waters"),
        (((half, 0), (half, 1)), "water", "North Shore", 1.3, "Ontario Waters", "Ontario Waters"),
        (((half, 1), (half, 2)), "water", "East Bay", 1.1, "East Bay", "Ontario Waters"),
        (((1, half), (2, half)), "vista", "East Overlook", 1.2, "East Ridge", "East Ridge"),
        (((-2, half), (-1, half)), "gorge", "Southeast Gorge", 1.15, "High Gorge", "High Gorge"),
        (((-half, 0), (-half, 1)), "water", "South Lake", 1.25, "South Lake", "South Lake"),
        (((-1, -half), (0, -half)), "vista", "West Bluff", 1.2, "West Bluff", "West Bluff"),
        (((2, -half), (3, -half)), "water", "Northwest Pond", 1.1, "Long Pond", "Long Pond"),
    ]
    for pair, mtype, label, mval, feat, body in candidates:
        if pair in edge_by_pair:
            mspecs.append((edge_by_pair[pair], mtype, label, mval, feat, body))
    if dead_moment_edge is not None:
        mspecs.append((dead_moment_edge, "vista", "Dead End Spur", 1.5, "Dead Spur", "Dead Spur"))

    mom = _make_moments(g, mspecs)
    ctx = Context(g=g, S=S, moments=mom, cfg={**BASE_CFG, "loop": dict(DEFAULTS)})
    if dead_anchor_node is not None:
        ctx.cfg["_dead_anchor_node"] = dead_anchor_node
    return ctx, (lat0, lon0), coords


def test_grid_loops_start_end_at_h_within_time_window():
    ctx, start, coords = make_grid_ctx()
    h_node = coords[(0, 0)]
    budget_s = 3600.0
    res = loops(ctx, start=start, minutes=60.0, steer=Steer())
    assert 1 <= len(res.picks) <= 3
    cfg = loop_config()
    lo = cfg["min_time_frac"] * budget_s
    hi = cfg["max_time_frac"] * budget_s
    for p in res.picks:
        assert int(p.route.nodes[0]) == h_node
        assert int(p.route.nodes[-1]) == h_node
        assert lo <= p.route.time_s <= hi
        assert p.tier in (1, 2)
        assert p.net <= cfg["max_net_doubled_frac"]


def test_true_bearings_avoid_epsg5070_skew_and_word_sectors():
    # At -77.61 lon (Rochester), EPSG:5070 grid north is skewed ~11 deg from true north.
    # A point with identical longitude due north must have true bearing 0.0 deg.
    lat0, lon0 = 43.1566, -77.6088
    east, north = _tangent_xy(lon0, lat0 + 0.25, lon0, lat0)
    brg = float(_bearing_from_en(east, north))
    assert abs(brg) < 0.1 or abs(brg - 360.0) < 0.1
    east_s, north_s = _tangent_xy(lon0, lat0 - 0.25, lon0, lat0)
    assert float(_bearing_from_en(east_s, north_s)) == pytest.approx(180.0, abs=0.1)

    # Sector boundaries flip at 22.5 deg increments
    assert direction_word(0.0) == "N"
    assert direction_word(22.49) == "N"
    assert direction_word(22.51) == "NE"
    assert direction_word(337.49) == "NW"
    assert direction_word(337.51) == "N"
    assert len(DIRECTIONS) == 8


def test_corridor_penalty_avoids_parallel_road_within_120m():
    # Build an eastbound road H(0)->1->2->3 (~9 km) with a parallel road 30 m north (0->4->5->3)
    # and a southern return loop 2 km south (3->6->7->0).
    lat0, lon0 = 43.0, -77.0
    d30m = 30.0 / 110900.0  # ~30 m in latitude
    d2km = -2000.0 / 110900.0  # 2 km south
    lons = [lon0, lon0 + 0.04, lon0 + 0.08, lon0 + 0.12, lon0 + 0.04, lon0 + 0.08, lon0 + 0.08, lon0 + 0.04]
    lats = [lat0, lat0, lat0, lat0, lat0 + d30m, lat0 + d30m, lat0 + d2km, lat0 + d2km]
    edges_uv = [
        (0, 1, 3250.0, 180.0, 0.8),
        (1, 2, 3250.0, 180.0, 0.8),
        (2, 3, 3250.0, 180.0, 0.8),
        # Parallel road 30 m away: slightly faster than southern return if unpenalized
        (0, 4, 3255.0, 182.0, 0.85),
        (4, 5, 3250.0, 182.0, 0.85),
        (5, 3, 3255.0, 182.0, 0.85),
        # Southern return 2 km away: slightly longer unpenalized (620 s vs 546 s), much cheaper when 30m road is 6x
        (3, 6, 4000.0, 210.0, 0.75),
        (6, 7, 3250.0, 200.0, 0.75),
        (7, 0, 4000.0, 210.0, 0.75),
    ]
    g = RoadGraph(
        lons,
        lats,
        [e[0] for e in edges_uv],
        [e[1] for e in edges_uv],
        [False] * len(edges_uv),
        [e[3] for e in edges_uv],
        [e[2] for e in edges_uv],
    )
    S = np.array([e[4] for e in edges_uv], dtype=np.float64)
    mom = _make_moments(g, [(1, "water", "Mid Shore", 1.2, "Mid Shore", "Mid Shore")])

    # With default corridor penalty (5.0 within 120 m), return leg from 3->0 avoids 3->5->4->0 and takes 3->6->7->0
    ctx_pen = Context(g=g, S=S, moments=mom, cfg=dict(BASE_CFG))
    cfg_pen = loop_config({"min_minutes": 10})
    reach = compute_reach(g, (lat0, lon0), 1200.0, cfg_pen)
    vias, reg_edges, mid_tree, nx, ny = _build_candidates(ctx_pen, reach, cfg_pen)
    b_pen = _LoopBuilder(ctx_pen, reach, cfg_pen, reg_edges, mid_tree, nx, ny)
    f_via = next(v for v in vias if v.key == 0 and v.head == 2)
    lens_pen = b_pen.build_lens(f_via, "test")
    assert lens_pen is not None
    assert {6, 7, 8} <= set(int(e) for e in lens_pen[0].edges)

    # With reuse_penalty = 0, return leg takes the faster 30 m parallel corridor (edges 3, 4, 5)
    cfg_nopen = {**cfg_pen, "reuse_penalty": 0.0}
    b_nopen = _LoopBuilder(ctx_pen, reach, cfg_nopen, reg_edges, mid_tree, nx, ny)
    lens_nopen = b_nopen.build_lens(f_via, "test")
    assert lens_nopen is not None
    assert {3, 4, 5} <= set(int(e) for e in lens_nopen[0].edges)


def test_no_uturn_and_through_rule():
    ctx, start, _ = make_grid_ctx()
    res = loops(ctx, start=start, minutes=60.0, steer=Steer())
    assert res.picks
    for p in res.picks:
        nodes = p.route.nodes
        edges = p.route.edges
        # No immediate U-turn: never traverse an edge and immediately reverse on the same edge or node
        for k in range(len(edges) - 1):
            assert not (edges[k] == edges[k + 1] and nodes[k] == nodes[k + 2])
        for k in range(len(nodes) - 2):
            assert nodes[k] != nodes[k + 2]

    # Directly verify _advance_via through-rule when path reaches via.tail by traversing head->tail
    cfg = loop_config()
    reach = compute_reach(ctx.g, start, 3600.0, cfg)
    vias, reg_edges, mid_tree, nx, ny = _build_candidates(ctx, reach, cfg)
    builder = _LoopBuilder(ctx, reach, cfg, reg_edges, mid_tree, nx, ny)
    G = builder.G0
    via = vias[0]
    step = builder._advance_via(G, np.array([via.head, via.tail], dtype=np.int64), via, None)
    assert step is not None
    added_nodes, added_arcs, next_cur, next_ban = step
    # Because p[-2] == via.head, the via edge was already traversed head->tail; it must NOT append head again!
    assert list(added_nodes) == [via.tail]
    assert len(added_arcs) == 1
    assert next_cur == via.tail
    assert next_ban == (via.tail, via.head)


def test_dead_end_moment_drops_cleanly_and_dead_end_point_anchor_sets_uturn():
    ctx, start, _ = make_grid_ctx(with_dead_end=True)
    # Any-mode search includes the dead-end moment in candidates and drops its pairs without error
    res_any = loops(ctx, start=start, minutes=60.0, steer=Steer())
    assert res_any.picks
    assert all(not p.uturn for p in res_any.picks)

    # Point anchor on the dead-end node succeeds via dead-end retry and marks uturn=True
    dn = ctx.cfg["_dead_anchor_node"]
    pt = (float(ctx.g.node_lat[dn]), float(ctx.g.node_lon[dn]))
    res_dead = loops(ctx, start=start, minutes=60.0, steer=Steer(mode="anchor", point=pt))
    assert res_dead.picks
    assert all(dn in set(int(n) for n in p.route.nodes) for p in res_dead.picks)
    assert all(p.uturn is True for p in res_dead.picks)


def test_loop_validity_out_and_back_ring_lollipop_and_cyclic_endpoints():
    ctx, _, coords = make_grid_ctx()
    g = ctx.g

    # 1. Pure out-and-back: H(0,0) -> (0,3) -> H(0,0)
    out = g.route(coords[(0, 0)], coords[(0, 3)])
    back = g.route(coords[(0, 3)], coords[(0, 0)])
    ob_route = Route(
        nodes=np.concatenate([out.nodes, back.nodes[1:]]),
        edges=np.concatenate([out.edges, back.edges]),
        reversed=np.concatenate([out.reversed, back.reversed]),
        time_s=out.time_s + back.time_s,
        length_m=out.length_m + back.length_m,
        cost=out.cost + back.cost,
        geometry=out.geometry + back.geometry[1:],
    )
    v_ob = loop_validity(ob_route)
    assert v_ob["tier"] == 0
    assert v_ob["doubled"] > 0.85

    # 2. Clean rectangle ring: (0,0)->(0,2)->(2,2)->(2,0)->(0,0)
    legs = [
        g.route(coords[(0, 0)], coords[(0, 2)]),
        g.route(coords[(0, 2)], coords[(2, 2)]),
        g.route(coords[(2, 2)], coords[(2, 0)]),
        g.route(coords[(2, 0)], coords[(0, 0)]),
    ]
    ring_geom = list(legs[0].geometry)
    for lg in legs[1:]:
        ring_geom.extend(lg.geometry[1:])
    ring_route = Route(
        nodes=np.concatenate([legs[0].nodes] + [lg.nodes[1:] for lg in legs[1:]]),
        edges=np.concatenate([lg.edges for lg in legs]),
        reversed=np.concatenate([lg.reversed for lg in legs]),
        time_s=sum(lg.time_s for lg in legs),
        length_m=sum(lg.length_m for lg in legs),
        cost=sum(lg.cost for lg in legs),
        geometry=ring_geom,
    )
    v_ring = loop_validity(ring_route)
    assert v_ring["tier"] == 1
    assert v_ring["doubled"] == pytest.approx(0.0, abs=1e-6)
    assert v_ring["stem_km"] == pytest.approx(0.0, abs=0.15)

    # 3. Lollipop: 1-edge stem (~3.91 km) from (0,-1)->(0,0), then rectangle (0,0)->(0,3)->(3,3)->(3,0)->(0,0), then (0,0)->(0,-1)
    # Ring perimeter = 12 edges * ~3.9 km = ~46.8 km; stem = 3.91 km (doubled ~ 2*3.91 / 54.6 ~ 0.143 -> tier 2!)
    stem_out = g.route(coords[(0, -1)], coords[(0, 0)])
    big_legs = [
        stem_out,
        g.route(coords[(0, 0)], coords[(0, 3)]),
        g.route(coords[(0, 3)], coords[(3, 3)]),
        g.route(coords[(3, 3)], coords[(3, 0)]),
        g.route(coords[(3, 0)], coords[(0, 0)]),
        g.route(coords[(0, 0)], coords[(0, -1)]),
    ]
    lol_geom = list(big_legs[0].geometry)
    for lg in big_legs[1:]:
        lol_geom.extend(lg.geometry[1:])
    lol_route = Route(
        nodes=np.concatenate([big_legs[0].nodes] + [lg.nodes[1:] for lg in big_legs[1:]]),
        edges=np.concatenate([lg.edges for lg in big_legs]),
        reversed=np.concatenate([lg.reversed for lg in big_legs]),
        time_s=sum(lg.time_s for lg in big_legs),
        length_m=sum(lg.length_m for lg in big_legs),
        cost=sum(lg.cost for lg in big_legs),
        geometry=lol_geom,
    )
    v_lol = loop_validity(lol_route)
    assert v_lol["tier"] == 2
    assert v_lol["stem_km"] == pytest.approx(stem_out.length_m / 1000.0, abs=0.35)
    assert v_lol["net"] < 0.02


def test_compass_east_only_graph_and_weights():
    ctx, start, _ = make_grid_ctx(east_only=True)
    cfg = loop_config()
    assert compass_weight(0.0, cfg) == pytest.approx(1.0)
    assert compass_weight(22.5, cfg) == pytest.approx(1.0)
    assert compass_weight(67.5, cfg) == pytest.approx(0.5)
    assert compass_weight(45.0, cfg) == pytest.approx(0.75)

    with pytest.raises(LoopError) as exc:
        loops(ctx, start=start, minutes=60.0, steer=Steer(mode="compass", direction="W"))
    assert exc.value.code == "no_loop_that_way"

    res_e = loops(ctx, start=start, minutes=60.0, steer=Steer(mode="compass", direction="E"))
    assert res_e.picks
    for p in res_e.picks:
        assert p.off_deg is not None and p.off_deg <= 67.5


def test_resolve_anchor_feature_body_point_shore_and_errors():
    ctx, start, coords = make_grid_ctx()
    cfg = loop_config()
    reach = compute_reach(ctx.g, start, 3600.0, cfg)

    # 1. Exact feature match (case-insensitive)
    a_feat = resolve_anchor(ctx, reach, Steer(mode="anchor", feature="west cove"), cfg)
    assert a_feat.kind == "feature" and a_feat.name == "West Cove"
    assert len(a_feat.moments) == 1

    # 2. Parent body match ("Ontario Waters" aggregates West Cove + North Shore + East Bay)
    a_body = resolve_anchor(ctx, reach, Steer(mode="anchor", feature="ONTARIO WATERS"), cfg)
    assert a_body.kind == "feature" and a_body.name == "Ontario Waters"
    assert len(a_body.moments) >= 3

    # 3. Unknown feature
    with pytest.raises(LoopError) as exc_u:
        resolve_anchor(ctx, reach, Steer(mode="anchor", feature="Atlantis"), cfg)
    assert exc_u.value.code == "unknown_feature"

    # 4. Pin within 2000 m of road -> point anchor
    n22 = coords[(2, 2)]
    p22 = (float(ctx.g.node_lat[n22]) + 0.002, float(ctx.g.node_lon[n22]))
    a_pt = resolve_anchor(ctx, reach, Steer(mode="anchor", point=p22), cfg)
    assert a_pt.kind == "point" and a_pt.node == n22

    # 5. Offshore water pin: > 2000 m from roads, within 20 km of "Ontario Waters" water moments
    north_lat = float(ctx.g.node_lat[coords[(4, 0)]]) + 0.04  # ~4.4 km north of northernmost shore road
    a_shore = resolve_anchor(ctx, reach, Steer(mode="anchor", point=(north_lat, -77.0)), cfg)
    assert a_shore.kind == "shore"
    assert a_shore.name == "Ontario Waters"
    assert a_shore.moved_m > 2000.0

    # 6. Far from roads and > 20 km from any water moment -> anchor_far
    with pytest.raises(LoopError) as exc_far:
        resolve_anchor(ctx, reach, Steer(mode="anchor", point=(43.6, -77.0)), cfg)
    assert exc_far.value.code == "anchor_far"

    # 7. Too far for tiny budget -> anchor_out_of_reach with round_trip_min and needed_min
    tiny_reach = compute_reach(ctx.g, start, 600.0, cfg)  # 10 min budget; corner (4, 4) is ~28 min one-way (56 min RT)
    n44 = coords[(4, 4)]
    with pytest.raises(LoopError) as exc_oor:
        resolve_anchor(
            ctx,
            tiny_reach,
            Steer(mode="anchor", point=(float(ctx.g.node_lat[n44]), float(ctx.g.node_lon[n44]))),
            cfg,
        )
    assert exc_oor.value.code == "anchor_out_of_reach"
    assert exc_oor.value.numbers["round_trip_min"] >= 50
    assert exc_oor.value.numbers["needed_min"] > exc_oor.value.numbers["round_trip_min"]


def test_anchor_loops_always_pass_their_anchor():
    ctx, start, coords = make_grid_ctx()
    # Feature anchor
    res_f = loops(ctx, start=start, minutes=60.0, steer=Steer(mode="anchor", feature="Ontario Waters"))
    assert res_f.picks and res_f.anchor is not None
    mset = set(int(m) for m in res_f.anchor.moments)
    for p in res_f.picks:
        assert set(p.passed) & mset

    # Point anchor
    n_target = coords[(2, 3)]
    pt = (float(ctx.g.node_lat[n_target]), float(ctx.g.node_lon[n_target]))
    res_p = loops(ctx, start=start, minutes=60.0, steer=Steer(mode="anchor", point=pt))
    assert res_p.picks and res_p.anchor is not None
    for p in res_p.picks:
        assert n_target in set(int(n) for n in p.route.nodes)


def test_loop_score_matches_score_route_and_budget_moment_cap():
    ctx, start, _ = make_grid_ctx()
    budget_s = 3600.0
    res = loops(ctx, start=start, minutes=60.0, steer=Steer())
    cfg = loop_config()
    for p in res.picks:
        sr = ScenicRoute(route=p.route, origin="check")
        score_route(sr, ctx, t0=budget_s, s_fast=cfg["s_base"])
        assert p.score == pytest.approx(sr.score)
        assert p.scenic_min == pytest.approx(sr.scenic_min)
        assert p.moment_pts == pytest.approx(sr.moment_pts)

    # Moment cap scales with budget B (moments_per_hour * B / 3600)
    low_cap_ctx = Context(
        g=ctx.g,
        S=ctx.S,
        moments=ctx.moments,
        cfg={**ctx.cfg, "moments_per_hour": 1.0, "moments_min_cap": 1},
    )
    r0 = res.picks[0].route
    sr_1h = ScenicRoute(route=r0, origin="1h")
    sr_3h = ScenicRoute(route=r0, origin="3h")
    score_route(sr_1h, low_cap_ctx, t0=3600.0, s_fast=0.5)
    score_route(sr_3h, low_cap_ctx, t0=10800.0, s_fast=0.5)
    assert len(sr_1h.moments) <= 1
    assert len(sr_3h.moments) >= len(sr_1h.moments)


def test_selection_overlap_bearing_tier_labels_and_determinism():
    ctx, start, coords = make_grid_ctx()
    g = ctx.g

    def make_lr(path_coords: list[tuple[int, int]], score: float, tier: int, brg: float, d_word: str) -> LoopRoute:
        legs = [g.route(coords[a], coords[b]) for a, b in zip(path_coords, path_coords[1:])]
        geom = list(legs[0].geometry)
        for lg in legs[1:]:
            geom.extend(lg.geometry[1:])
        rt = Route(
            nodes=np.concatenate([legs[0].nodes] + [lg.nodes[1:] for lg in legs[1:]]),
            edges=np.concatenate([lg.edges for lg in legs]),
            reversed=np.concatenate([lg.reversed for lg in legs]),
            time_s=sum(lg.time_s for lg in legs),
            length_m=sum(lg.length_m for lg in legs),
            cost=sum(lg.cost for lg in legs),
            geometry=geom,
        )
        lr = LoopRoute(route=rt, origin="syn", tier=tier, bearing=brg, direction=d_word)
        lr.score = score
        lr.rank = score
        return lr

    # Two nearly identical north loops (overlap > 0.5), one north tier-2 loop with higher score,
    # one east tier-1 loop, and one south tier-1 loop. Pool passed to _select_loops is tier1 first, then tier2.
    c_n1 = make_lr([(0, 0), (3, 0), (3, 2), (0, 2), (0, 0)], 40.0, 1, 15.0, "N")
    c_n1_dup = make_lr([(0, 0), (3, 0), (3, 2), (1, 2), (1, 1), (0, 1), (0, 0)], 39.0, 1, 18.0, "N")
    c_t2_high = make_lr([(0, 0), (0, -2), (-2, -2), (-2, 0), (0, 0)], 99.0, 2, 225.0, "SW")
    c_e1 = make_lr([(0, 0), (0, 3), (-2, 3), (-2, 0), (0, 0)], 35.0, 1, 105.0, "E")
    c_w1 = make_lr([(0, 0), (-3, 0), (-3, -2), (0, -2), (0, 0)], 30.0, 1, 210.0, "SW")

    cfg = loop_config()
    pool = sorted([c_n1, c_n1_dup, c_t2_high, c_e1, c_w1], key=lambda x: (x.tier, -x.rank))
    picks = _select_loops(pool, g.length_m, Steer(mode="any"), cfg)
    assert picks == [c_n1, c_e1, c_w1]

    # Bearing relaxation: when all candidates share a narrow bearing cone (< 55 deg) with distinct edges,
    # later pass relaxes bearing separation and still returns multiple non-overlapping loops
    c_ne2 = make_lr([(0, 0), (2, 0), (2, 3), (1, 3), (1, 1), (0, 1), (0, 0)], 25.0, 1, 50.0, "NE")
    narrow = _select_loops([c_n1, c_ne2], g.length_m, Steer(mode="any"), cfg)
    assert len(narrow) == 2

    # End-to-end determinism and unique labels
    r1 = loops(ctx, start=start, minutes=60.0, steer=Steer(mode="compass", direction="N"))
    r2 = loops(ctx, start=start, minutes=60.0, steer=Steer(mode="compass", direction="N"))
    labels = [p.label for p in r1.picks]
    assert len(labels) == len(set(labels))
    assert [list(p.edges) for p in r1.picks] == [list(p.edges) for p in r2.picks]


def test_work_cap_bounds_dijkstras_and_stays_deterministic():
    ctx, start, _ = make_grid_ctx()
    cap_cfg = {"dijkstra_work_arcs": 100, "dijkstra_count_range": [6, 12]}
    r1 = loops(ctx, start=start, minutes=60.0, steer=Steer(), cfg=cap_cfg)
    r2 = loops(ctx, start=start, minutes=60.0, steer=Steer(), cfg=cap_cfg)
    assert r1.dijkstras <= 12 + 1 + 3 * (1 + 4)
    assert r1.dijkstras == r2.dijkstras
    assert [list(p.edges) for p in r1.picks] == [list(p.edges) for p in r2.picks]


def test_shared_middle_tree_matches_per_pair_build_and_caches_only_root_h():
    ctx, start, _ = make_grid_ctx()
    cfg = loop_config()
    reach = compute_reach(ctx.g, start, 3600.0, cfg)
    vias, reg_edges, mid_tree, nx, ny = _build_candidates(ctx, reach, cfg)
    b_shared = _LoopBuilder(ctx, reach, cfg, reg_edges, mid_tree, nx, ny)
    b_single = _LoopBuilder(ctx, reach, cfg, reg_edges, mid_tree, nx, ny)

    a1 = vias[0]
    d1 = b_shared.time_dist_from(a1.head, cfg["max_time_frac"] * 3600.0)
    partners = _pick_partners(vias, a1, d1, 3600.0, cfg)
    assert len(partners) >= 2

    got_shared = b_shared.build_triangles(a1, partners)
    got_single = []
    for a2 in partners:
        got_single.extend(b_single.build_triangles(a1, [a2]))

    assert len(got_shared) == len(got_single)
    for (r_sh, u_sh, v_sh), (r_sg, u_sg, v_sg) in zip(got_shared, got_single):
        assert v_sh.key == v_sg.key and u_sh == u_sg
        assert np.array_equal(r_sh.edges, r_sg.edges)
    assert b_shared.dijkstras < b_single.dijkstras + 1

    # CostGraphs hold cached shortest-path trees ONLY for root H (reach.node)
    for lam, G in b_shared.graphs.items():
        roots = {root for root, _ in G._trees}
        assert roots <= {reach.node}, f"lambda {lam} cached extra roots: {roots}"


def test_features_in_reach_counts_fits_ordering_and_query():
    ctx, start, _ = make_grid_ctx()
    # Without q: returns only reachable features, ordered by moment count / value
    items = features_in_reach(ctx, start=start, minutes=60.0, limit=10)
    assert items
    assert all(it["fits"] is True for it in items)
    by_name = {it["name"]: it for it in items}
    assert "Ontario Waters" in by_name
    assert by_name["Ontario Waters"]["count"] == 3  # aggregated parent body!
    assert items[0]["name"] == "Ontario Waters"  # highest moment count comes first

    # With q and a smaller budget where Ontario Waters (RT ~28 min, needs >= 40 min) does not fit:
    q_items = features_in_reach(ctx, start=start, minutes=30.0, q="ontario", limit=10)
    assert len(q_items) == 1
    assert q_items[0]["name"] == "Ontario Waters"
    assert q_items[0]["fits"] is False
    assert q_items[0]["needed_min"] > 30
