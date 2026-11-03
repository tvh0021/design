"""Scenery score v0 on tiny synthetic rasters and edges (no network, no statewide data)."""

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from scipy import ndimage

from scenic.rasters import (Grid, block_mean, disk_footprint, disk_max, disk_mean, disk_min, landcover_products,
                            make_grid, relief_tpi, size_score, water_size)
from scenic.score import (Samples, check_alignment, combine, curvature, excluded_mask, hills_edges, land_points,
                          load, load_config, percentile_rank, quiet, road_keys, sample_points, scores_table,
                          views_points, water_points, window_pairs, write_scores)
from scenic.validate import byway_edge_mask, match_road
from scenic.water import is_areal_water, is_signal_node, line_code

CFG = load_config()


def one_edge(xs, ys):
    return np.array([0, len(xs)]), np.asarray(xs, float), np.asarray(ys, float)


# ---------------------------------------------------------------- grid, filters


def test_make_grid_is_on_the_lattice_and_divisible_by_block():
    g = make_grid(1000.0, 2000.0, 5000.0, 4000.0, origin_x=-15.0, origin_y=3015.0, res=30.0, pad=100.0, block=3)
    assert (g.x0 + 15.0) % 30 == 0 and (3015.0 - g.y0) % 30 == 0
    assert g.width % 3 == 0 and g.height % 3 == 0
    x0, y0, x1, y1 = g.bounds
    assert x0 <= 900 and x1 >= 5100 and y0 <= 1900 and y1 >= 4100


def test_disk_filters_match_ndimage():
    a = np.random.default_rng(1).random((31, 29)).astype(np.float32) * 100
    fp = disk_footprint(4)
    assert np.array_equal(disk_max(a, 4), ndimage.maximum_filter(a, footprint=fp, mode="reflect"))
    assert np.array_equal(disk_min(a, 4), ndimage.minimum_filter(a, footprint=fp, mode="reflect"))
    ref = ndimage.convolve(a.astype(np.float64), fp / fp.sum(), mode="reflect")
    assert np.allclose(disk_mean(a, 4), ref, atol=1e-4)


def test_block_mean_ignores_invalid_cells():
    a = np.arange(36, dtype=np.float32).reshape(6, 6)
    valid = np.ones_like(a, bool)
    valid[0, 0] = False
    m = block_mean(a, 3, valid)
    assert m.shape == (2, 2)
    top_left = a[:3, :3].ravel()[1:].mean()
    assert m[0, 0] == pytest.approx(top_left)
    assert m[1, 1] == pytest.approx(a[3:, 3:].mean())


def test_relief_and_tpi_on_a_single_peak():
    dem = np.zeros((41, 41), np.float32)
    dem[20, 20] = 300.0
    relief, tpi = relief_tpi(dem, 11)
    assert relief[20, 20] == 300 and relief[20, 31] == 300 and relief[20, 32] == 0
    assert tpi[20, 20] == pytest.approx(300 - 300 / disk_footprint(11).sum(), rel=1e-4)


# ---------------------------------------------------------------- water size


def test_size_score_anchor_points():
    assert size_score([1e4, 1e6, 1e8, 1e10]).tolist() == pytest.approx([0.0, 0.5, 1.0, 1.0])
    assert size_score(10.0) == 0.0


def test_water_size_components_lines_and_coast():
    area = np.zeros((10, 10), bool)
    area[0:2, 0:2] = True          # 4 cells
    area[5:9, 5:9] = True          # 16 cells, diagonal neighbour joins below
    area[9, 9] = True
    line = np.zeros((10, 10), np.uint8)
    line[0, 5:8] = [1, 2, 3]
    line[1, 1] = 3                 # under areal water: ignored
    coast = np.zeros((10, 10), bool)
    cfg = CFG["water"]
    cell = 1e4                     # 1 ha cells
    out = water_size(area, line, coast, cell, cfg)
    assert out[0, 0] == pytest.approx(size_score(4e4))
    assert out[9, 9] == pytest.approx(size_score(17e4))  # 8-connectivity
    assert out[0, 5:8].tolist() == pytest.approx([0.45, 0.35, 0.15])
    assert out[1, 1] == pytest.approx(size_score(4e4))
    assert out[3, 3] == 0
    coast[0, 0] = True
    assert water_size(area, line, coast, cell, cfg)[0, 0] == 1.0


# ---------------------------------------------------------------- sampling


def test_sample_points_spacing_and_short_edges():
    off = np.array([0, 3, 5])
    x = np.array([0.0, 60.0, 120.0, 500.0, 510.0])
    y = np.zeros(5)
    s = sample_points(off, x, y, np.array([120.0, 10.0]), step=50.0)
    assert s.n.tolist() == [4, 2]
    assert s.x[:4].tolist() == pytest.approx([0, 40, 80, 120])
    assert s.x[4:].tolist() == pytest.approx([500, 510])
    assert s.edge.tolist() == [0, 0, 0, 0, 1, 1]
    assert s.mean(np.arange(6.0)).tolist() == pytest.approx([1.5, 4.5])


def test_sample_points_follow_a_bend():
    off, x, y = one_edge([0, 100, 100], [0, 0, 100])
    s = sample_points(off, x, y, np.array([200.0]), step=50.0)
    assert s.n[0] == 5
    assert np.allclose(np.column_stack([s.x, s.y]), [[0, 0], [50, 0], [100, 0], [100, 50], [100, 100]])


# ---------------------------------------------------------------- signals


def tiny_grid(n=40):
    return Grid(0.0, n * 30.0, 30.0, n, n)


def pts(xy):
    xy = np.asarray(xy, float)
    k = len(xy)
    return Samples(xy[:, 0], xy[:, 1], np.zeros(k, np.int32), np.array([k]), np.array([0, k]))


def test_water_signal_decay_size_and_visibility():
    g = tiny_grid()
    water = np.zeros(g.shape, bool)
    water[:, 30:] = True                       # water from x = 900 m
    size = np.where(water, 0.8, 0.0).astype(np.float32)
    blocking = np.zeros(g.shape, bool)
    cy = 600.0
    # Points at distance ~ 15, 85, 165, 300 m from the nearest water cell centre (x = 915).
    s = pts([[900, cy - 15], [830, cy - 15], [750, cy - 15], [615, cy - 15]])
    W, d = water_points(s, g, water, lambda i: size.ravel()[i], blocking, CFG)
    assert d[0] == pytest.approx(15, abs=1) and W[0] == pytest.approx(0.8)
    assert W[1] == pytest.approx(0.8 * np.clip((250 - d[1]) / 200, 0, 1))
    assert W[2] == pytest.approx(0.8 * (250 - d[2]) / 200)
    assert np.isinf(d[3]) and W[3] == 0
    # Forest between road and shore hides the water partly.
    blocking[:, 28:30] = True                  # x = 840..900
    W2, _ = water_points(s, g, water, lambda i: size.ravel()[i], blocking, CFG)
    assert W2[0] == pytest.approx(0.8)         # d < 45 m: always visible
    assert W2[2] == pytest.approx(0.5 * W[2])  # sight line cells at x 783, 816 open; 849, 882 forest
    assert W2[1] == 0                          # 847..898: all forest


def test_hills_relief_grade_and_short_edge_fallback():
    # Edge 0: 300 m long, 7 points, climbing 6 %: grade 0.06 -> H_grade 1.
    n0, n1 = 7, 3
    z0 = np.linspace(0, 18, n0)
    s = Samples(np.zeros(n0 + n1), np.zeros(n0 + n1), np.repeat([0, 1], [n0, n1]).astype(np.int32),
                np.array([n0, n1]), np.array([0, n0, n0 + n1]))
    relief = np.r_[np.full(n0, 150.0), np.full(n1, 600.0)]
    z = np.r_[z0, [5, 50, 5]]
    H, hr, hg = hills_edges(s, relief, z, np.array([300.0, 100.0]), CFG)
    # Running mean keeps the end points, so the total rise stays 18 m.
    assert hg[0] == pytest.approx(1.0)
    assert hr[0] == pytest.approx(0.5)
    assert H[0] == pytest.approx(0.7 * 0.5 + 0.3 * 1.0)
    assert H[1] == hr[1] == 1.0                # fewer than 4 points: H = H_relief


def test_flat_road_has_no_grade_score():
    s = Samples(np.zeros(5), np.zeros(5), np.zeros(5, np.int32), np.array([5]), np.array([0, 5]))
    z = np.array([100, 101, 100, 101, 100.0])  # 1 m DEM noise
    _, _, hg = hills_edges(s, np.zeros(5), z, np.array([200.0]), CFG)
    assert hg[0] == 0.0


def test_views_and_land_formulas():
    assert views_points(np.array([1.0, 1.0, 0.0, 0.5]), np.array([0.0, 60.0, 60.0, 30.0]), CFG).tolist() == \
        pytest.approx([0.35, 1.0, 0.0, 0.5 * (0.35 + 0.65 * 0.5)])
    assert land_points(np.array([0.9]), np.array([1.0]), CFG)[0] == pytest.approx(0.8 * 0.9 + 0.2)


def test_landcover_products_scores_mix_and_nodata():
    nlcd = np.full((9, 9), 41, np.uint8)       # forest
    nlcd[:, :3] = 81                           # pasture strip
    nlcd[6:, 6:] = 250                         # nodata corner
    p = landcover_products(nlcd, CFG)
    assert p["lscore90"].shape == (3, 3)
    # Centre 270 m window: 27 pasture + 45 forest valid cells, 9 nodata ignored.
    pf, po = 45 / 72, 27 / 72
    assert p["lscore90"][1, 1] == pytest.approx((27 * 0.9 + 45 * 0.7) / 72, rel=1e-5)
    assert p["mix90"][1, 1] == pytest.approx(4 * pf * po, rel=1e-5)
    assert p["open90"][1, 1] == pytest.approx(po, rel=1e-5)
    assert p["dev90"].max() == 0


def test_quiet_links_take_parent_value():
    q = quiet(np.array(["motorway", "trunk_link", "secondary", "unclassified", "residential", "service"], object), CFG)
    assert q.tolist() == pytest.approx([0.0, 0.10, 0.50, 0.90, 0.60, 0.0])


def test_curvature_straight_bend_and_jitter():
    off = np.array([0, 3, 6, 10])
    x = np.array([0, 50, 100, 0, 100, 100, 0, 1, 2, 100.0])
    y = np.array([0, 0, 0, 0, 0, 100, 0, 1, 0, 0.0])
    L = np.array([100.0, 200.0, 100.0])
    c = curvature(off, x, y, L, 5.0, 200.0)
    assert c[0] == 0.0
    assert c[1] == pytest.approx(min(1.0, 90 / 0.2 / 200))
    assert c[2] == 0.0                         # 1 m wiggle dropped: only 2 kept vertices
    off2, x2, y2 = one_edge([0, 500, 500, 1000], [0, 0, 20, 20])
    assert curvature(off2, x2, y2, np.array([1020.0]))[0] == pytest.approx(180 / 1.02 / 200)


def test_exclusions():
    surf = np.array([None, "gravel", "asphalt", None], object)
    name = np.array(["Main St", None, "Old Mountain Rd (Seasonal Limited Use)", None], object)
    assert excluded_mask(surf, name, CFG).tolist() == [False, True, True, False]


def test_tag_rules():
    assert is_areal_water({"natural": "water"})
    assert is_areal_water({"landuse": "reservoir"})
    assert is_areal_water({"waterway": "riverbank"})
    assert not is_areal_water({"natural": "water", "water": "wastewater"})
    assert not is_areal_water({"natural": "water", "intermittent": "yes"})
    assert [line_code({"waterway": w}) for w in ("river", "canal", "stream", "ditch")] == [1, 2, 3, 0]
    assert is_signal_node({"highway": "traffic_signals"})
    assert is_signal_node({"highway": "stop", "stop": "all"})
    assert is_signal_node({"highway": "stop"})
    assert not is_signal_node({"highway": "stop", "direction": "forward"})
    assert not is_signal_node({"highway": "crossing"})


# ---------------------------------------------------------------- keys, smoothing, rank


def test_road_keys_normalise_ref_then_name_then_way():
    ref = np.array(["NY 5;US 20", "NY-5", None, None, None, "  "], object)
    name = np.array([None, "x", "Lake  Road", "lake road", None, None], object)
    way = np.array([1, 2, 3, 4, 5, 5])
    k = road_keys(ref, name, way)
    assert k[0] == k[1]
    assert k[2] == k[3] and k[2] != k[0]
    assert k[4] == k[5] and k[4] not in (k[0], k[2])


def chain():
    """Lake Rd: 0-1-2-3-4-5 (edges 0..4, 200 m each); Cross St at node 2 (edge 5)."""
    u = np.array([0, 1, 2, 3, 4, 2])
    v = np.array([1, 2, 3, 4, 5, 6])
    key = np.array([7, 7, 7, 7, 7, 9])
    L = np.array([200.0] * 5 + [300.0])
    return u, v, key, L


def test_window_follows_same_road_through_junctions_only():
    u, v, key, L = chain()
    src, dst, dist, _ = window_pairs(u, v, key, L, np.ones(6, bool), 500.0, 8)
    pairs = {(int(a), int(b)): float(d) for a, b, d in zip(src, dst, dist)}
    assert pairs[(1, 2)] == 0.0                # adjacent through the junction with Cross St
    assert pairs[(0, 2)] == 200.0              # through edge 1
    assert pairs[(0, 3)] == 400.0
    assert (0, 4) not in pairs                 # 600 m >= 500 m
    assert not any(5 in p for p in pairs)      # cross street never joins Lake Rd's window


def test_window_skips_excluded_edges():
    u, v, key, L = chain()
    active = np.ones(6, bool)
    active[2] = False
    src, dst, _, _ = window_pairs(u, v, key, L, active, 500.0, 8)
    pairs = set(zip(src.tolist(), dst.tolist()))
    assert (1, 3) not in pairs and (0, 1) in pairs and (3, 4) in pairs


def test_smoothing_and_penalty_windows():
    u, v, key, L = chain()
    n = 6
    sig = {"W": np.array([0, 0, 1.0, 0, 0, 0]), "H": np.zeros(n), "V": np.zeros(n), "L": np.zeros(n),
           "Q": np.zeros(n), "C": np.zeros(n), "dev": np.zeros(n), "signals": np.zeros(n),
           "junction_halves": np.zeros(n)}
    extra = {"u": u, "v": v, "key": key, "length_m": L, "highway": np.array(["tertiary"] * n, object),
             "maxspeed_tagged": np.zeros(n, bool), "speed_mph": np.full(n, 45.0)}
    active = np.ones(n, bool)
    active[4] = False
    r = combine(sig, extra, active, CFG)
    assert np.isnan(r["pre"][4]) and np.isnan(r["smooth"][4]) and r["S"][4] == 0
    # Edge 2 window: itself (200), edges 1 and 3 (200 each, d=0); edge 0 at d=200: w=200.
    assert r["window_m"][2] == pytest.approx(800)
    assert r["smooth"][2] == pytest.approx(0.35 * 200 / 800)
    # Edge 0: itself 200, edge 1 (200), edge 2 (d=200, w=200), edge 3 (d=400, w=100).
    assert r["smooth"][0] == pytest.approx(0.35 * 200 / 700)
    assert r["smooth"][5] == 0.0               # the cross street sees none of Lake Rd's water
    # A signal on edge 3 counts in edge 2's window by length share.
    sig["signals"] = np.array([0, 0, 0, 1.0, 0, 0])
    r2 = combine(sig, extra, active, CFG)
    per_km = 1 / 0.8
    assert r2["signals_per_km"][2] == pytest.approx(per_km)
    assert r2["penalty"][2] == pytest.approx(0.20 * per_km / 3)


def test_penalties_cap_and_motorway_urban():
    n = 2
    sig = {k: np.zeros(n) for k in ("W", "H", "V", "L", "Q", "C", "signals", "junction_halves")}
    sig["dev"] = np.array([0.0, 1.0])
    sig["signals"] = np.array([0.0, 50.0])
    sig["junction_halves"] = np.array([0.0, 50.0])
    extra = {"u": np.array([0, 2]), "v": np.array([1, 3]), "key": np.array([0, 1]), "length_m": np.array([1000.0, 1000.0]),
             "highway": np.array(["motorway", "residential"], object), "maxspeed_tagged": np.array([False, True]),
             "speed_mph": np.array([65.0, 25.0])}
    r = combine(sig, extra, np.ones(n, bool), CFG)
    assert r["penalty"][0] == pytest.approx(0.10)
    assert r["penalty"][1] == pytest.approx(0.5)  # 0.15 + 0.20 + 0.25 + 0.10, capped


def test_percentile_rank_by_length_with_ties():
    vals = np.array([0.1, 0.5, 0.5, 0.9, np.nan])
    L = np.array([100.0, 50.0, 50.0, 800.0, 1000.0])
    act = np.array([True, True, True, True, False])
    S = percentile_rank(vals, L, act)
    assert S[0] == pytest.approx(50 / 1000)
    assert S[1] == S[2] == pytest.approx((100 + 50) / 1000)
    assert S[3] == pytest.approx((200 + 400) / 1000)
    assert S[4] == 0


# ---------------------------------------------------------------- outputs


def test_scores_rows_align_with_edges(tmp_path):
    edges = pa.table({"u": pa.array([0, 1, 2], pa.int32()), "v": pa.array([1, 2, 3], pa.int32()),
                      "way_id": pa.array([10, 10, 11], pa.int64()), "length_m": [1.0, 2.0, 3.0]})
    ep = tmp_path / "edges.parquet"
    pq.write_table(edges, ep)
    n = 3
    res = {c: np.full(n, 0.5) for c in ("W", "H", "V", "L", "Q", "C", "penalty", "raw", "pre", "smooth", "S")}
    res.update(excluded=np.array([False, True, False]), n_samples=np.array([2, 2, 3]),
               water_dist_m=np.array([10.0, np.inf, 30.0]))
    cols = {"u": edges.column("u").to_numpy(), "v": edges.column("v").to_numpy(),
            "way_id": edges.column("way_id").to_numpy()}
    out = write_scores(scores_table(cols, res), ep, tmp_path / "scores.parquet")
    t = load(ep, out)
    assert t.column("edge_idx").to_pylist() == [0, 1, 2]
    assert t.column("S").to_pylist() == [0.5, 0.0, 0.5]
    assert np.isnan(t.column("raw").to_numpy()[1])
    assert t.column("water_dist_m").to_pylist()[1] == 9999.0
    bad = dict(cols, v=np.array([1, 2, 4], np.int32))
    with pytest.raises(ValueError):
        write_scores(scores_table(bad, res), ep, tmp_path / "bad.parquet")
    pq.write_table(edges.slice(0, 2), ep)
    with pytest.raises(ValueError):
        load(ep, out)


def test_check_alignment_rejects_reordered_rows():
    a = pa.table({"u": [0, 1], "v": [1, 2], "way_id": [5, 6]})
    b = pa.table({"u": [1, 0], "v": [2, 1], "way_id": [6, 5]})
    check_alignment(a, a)
    with pytest.raises(ValueError):
        check_alignment(a, b)


# ---------------------------------------------------------------- validation helpers


def test_match_road_by_any_ref_or_name():
    refs = np.array(["US 20;NY 5", "NY 5", "NY 50", None], object)
    names = np.array([None, None, None, "Lake  Road"], object)
    assert match_road(refs, names, ref="NY-5").tolist() == [True, True, False, False]
    assert match_road(refs, names, name="lake road").tolist() == [False, False, False, True]


def test_byway_mask_needs_most_points_near_the_line():
    line = [np.array([[0.0, 0.0], [1000.0, 0.0]])]
    # Edge 0 runs along the line 10 m off; edge 1 crosses it (one point near).
    sx = np.r_[np.linspace(0, 1000, 11), np.zeros(5) + 500]
    sy = np.r_[np.full(11, 10.0), np.linspace(0, 400, 5)]
    se = np.r_[np.zeros(11), np.ones(5)].astype(np.int32)
    assert byway_edge_mask(line, sx, sy, se, 2).tolist() == [True, False]
