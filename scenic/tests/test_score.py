"""Scenery score v0.2 on tiny synthetic rasters and edges (no network, no statewide data)."""

import copy
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from scipy import ndimage

from scenic.rasters import (Grid, block_mean, disk_footprint, disk_max, disk_mean, disk_min, landcover_products,
                            make_grid, reach_from_frac, relief_tpi, size_score, water90_products, water_size)
from scenic.score import (Samples, check_alignment, combine, curvature, excluded_mask, hills_edges, land_points,
                          load, load_config, near_falloff, window_top, percentile_rank, prospect_points, quiet, road_keys,
                          sample_points, scores_table, sight_line, turning, views_points, water_points,
                          water_view_points, window_pairs, write_scores)
from scenic.validate import byway_edge_mask, byway_neighbours, holdout2, match_road, v3_lists
from scenic.water import is_areal_water, is_signal_node, line_code

CFG = load_config()


def cfg_v01():
    """CFG with the v0.1 combination: subtractive penalty with a motorway term,
    no town water gate, mean smoothing, prospect in V, 0.35/0.30/0.20/0.15."""
    c = copy.deepcopy(CFG)
    c["combine"].update(w_water=0.35, w_hills=0.30, w_views=0.20, w_land=0.15)
    c["penalty"].update(mode="subtract", town_water_gate=False, motorway_weight=0.10)
    c["smoothing"]["pool_weight"] = 0.0
    c["views"]["w_prospect"] = 1.0
    c["water"]["transmit"] = {k: (0.4 if k in ("41", "42", "43") else t) for k, t in c["water"]["transmit"].items()}
    c["water"].pop("max_obstructed_cells", None)
    c["water"].pop("shore_road", None)
    return c


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


def lake_scene(n=40, nlcd_class=81):
    """Water from x = 900 m (column 30) eastwards, flat ground at 100 m,
    lake surface at 100 m, pasture between road and shore."""
    g = tiny_grid(n)
    area = np.zeros(g.shape, bool)
    area[:, 30:] = True
    size = np.where(area, 0.8, 0.0).astype(np.float32)
    line = np.zeros(g.shape, np.uint8)
    dem = np.full(g.shape, 100, np.int16)
    nlcd = np.full(g.shape, nlcd_class, np.uint8)
    nlcd[area] = 11
    return g, area, line, size, dem, nlcd


def run_w(s, g, area, line, size, dem, nlcd, reach=1000.0, z=100.0, cfg=CFG):
    return water_points(s, g, area, line, size, lambda x, y: np.full(len(x), reach), np.full(len(s.x), z),
                        dem, nlcd, cfg)


def test_near_falloff_scales_with_reach():
    assert near_falloff([0, 50, 150, 250], 250.0, 50, 0.2).tolist() == pytest.approx([1, 1, 0.5, 0])
    big = near_falloff([300, 900, 1500], 1500.0, 50, 0.2)   # full credit to 300 m
    assert big.tolist() == pytest.approx([1, 0.5, 0])


def test_reach_from_local_water_fraction():
    w = CFG["water"]
    r = reach_from_frac(np.array([0.0, 0.003, 0.06, 0.6]), 11, 90.0, w)
    assert r[0] == w["reach_min_m"]
    assert r[3] == w["reach_max_m"]                                 # shore of a broad lake
    assert r[2] == pytest.approx(1.2 * np.sqrt(0.06 * np.pi * 990 ** 2), rel=1e-5)  # ~100 m wide river: ~0.5 km
    assert 450 < r[2] < 550


def test_water_signal_decay_and_size():
    g, area, line, size, dem, nlcd = lake_scene()
    cy = 600.0
    # Distances to the nearest shore cell centre (x = 915): 30, 95, 315, 515 m.
    s = pts([[885, cy - 15], [820, cy - 15], [600, cy - 15], [400, cy - 15]])
    W, d = run_w(s, g, area, line, size, dem, nlcd, reach=1000.0)
    assert d[0] == pytest.approx(30, abs=1) and W[0] == pytest.approx(0.8)
    assert W[1] == pytest.approx(0.8)                                 # inside the 200 m full-credit zone
    assert W[2] == pytest.approx(0.8 * near_falloff(d[2], 1000.0, 50, 0.2), rel=1e-6)
    assert W[3] == pytest.approx(0.8 * (1000 - d[3]) / 800, rel=1e-6)
    # A pond (short reach) at the same distances counts only close by.
    Wp, dp = run_w(s, g, area, line, size, dem, nlcd, reach=150.0)
    assert Wp[0] == pytest.approx(0.8) and Wp[2] == 0 and np.isinf(dp[3])


def test_point_on_water_scores_its_size():
    g, area, line, size, dem, nlcd = lake_scene()
    W, d = run_w(pts([[1000, 600]]), g, area, line, size, dem, nlcd)
    assert d[0] == 0 and W[0] == pytest.approx(0.8)


def test_water_visibility_trees_cottages_and_height():
    g, area, line, size, dem, nlcd = lake_scene()
    v01 = cfg_v01()
    s = pts([[600, 585]])                                            # 315 m from the shore
    W_open, _ = run_w(s, g, area, line, size, dem, nlcd, cfg=v01)
    # One 30 m cell of forest along the shore: 40 % of the view gets through.
    nlcd[:, 29] = 41
    W1, _ = run_w(s, g, area, line, size, dem, nlcd, cfg=v01)
    assert W1[0] == pytest.approx(0.4 * W_open[0])
    # Four cells of forest: almost nothing.
    nlcd[:, 26:30] = 41
    W4, _ = run_w(s, g, area, line, size, dem, nlcd, cfg=v01)
    assert W4[0] == pytest.approx(0.4 ** 4 * W_open[0])
    # v0.2: forest passes 50 % per cell and only the first three cells count.
    assert run_w(s, g, area, line, size, dem, nlcd)[0][0] == pytest.approx(0.5 ** 3 * W_open[0])
    nlcd[:, 10:30] = 41                                              # 600 m of forest: still the screen
    assert run_w(s, g, area, line, size, dem, nlcd)[0][0] == pytest.approx(0.5 ** 3 * W_open[0])
    nlcd[:, 10:30] = 81
    # A road 60 m above the lake on a slope looks over a row of 20 m trees at the shore.
    nlcd[:, 26:29] = 81
    dem2 = dem.copy()
    cols = np.arange(g.width)
    slope = np.clip((900 - (cols + 0.5) * 30) / 300 * 60, 0, None)   # 60 m at x = 600, 0 at the shore
    dem2[:, :] = (100 + slope).astype(np.int16)
    Wh, _ = run_w(s, g, area, line, size, dem2, nlcd, z=160.0)
    assert Wh[0] > 0.9 * W_open[0]


def test_water_seen_through_a_gap_off_to_the_side():
    g, area, line, size, dem, nlcd = lake_scene()
    v01 = cfg_v01()
    s = pts([[600, 585]])                                            # row 20, 315 m from the shore
    W_open, _ = run_w(s, g, area, line, size, dem, nlcd, cfg=v01)
    nlcd[:, 29] = 41                                                  # trees all along the shore
    W1, _ = run_w(s, g, area, line, size, dem, nlcd, cfg=v01)
    # A gap in the trees about 30 degrees off the straight line to the shore:
    # the driver sees the lake through it, a little farther away.
    nlcd[12:17, 29] = 81
    nlcd[24:29, 29] = 81
    Wg, _ = run_w(s, g, area, line, size, dem, nlcd, cfg=v01)
    assert W1[0] == pytest.approx(0.4 * W_open[0])
    assert 0.8 * W_open[0] < Wg[0] < W_open[0]


def test_shore_road_term_floors_w_near_big_water():
    g, area, line, size, dem, nlcd = lake_scene()
    dem[:, 29] = 140                                                  # a bank hides the lake entirely
    sz = float(size[20, 35])
    assert sz >= 0.4
    shore_x = 30 * 30.0                                               # first water column starts at x = 900
    d = np.array([60.0, 210.0, 400.0])                                # from the nearest water cell centre
    s = pts([[shore_x + 15 - dd, 585] for dd in d])
    W, _ = run_w(s, g, area, line, size, dem, nlcd)
    assert W[0] == pytest.approx(0.5 * sz)                            # within 120 m: full floor
    assert W[1] == pytest.approx(0.5 * sz * (300 - 210) / 180)        # fading to 0 at 300 m
    assert W[2] == pytest.approx(0, abs=1e-12)
    W01, _ = run_w(s, g, area, line, size, dem, nlcd, cfg=cfg_v01())
    assert W01[0] == pytest.approx(0, abs=1e-12)                      # v0.1: the bank hid it


def test_sight_line_counts_only_the_first_obstructed_cells():
    g = tiny_grid()
    dem = np.full(g.shape, 100.0, np.float32)
    cls = np.zeros(g.shape, np.uint8)
    canopy = np.zeros(256)
    log_t = np.zeros(256)
    canopy[41], log_t[41] = 20.0, np.log(0.5)
    cls[:, 5:30] = 41
    args = (np.array([45.0, 45.0]), np.array([600.0, 300.0]), np.array([101.2, 101.2]),
            np.array([1005.0, 1005.0]), np.array([600.0, 300.0]), np.array([100.0, 100.0]), g)
    full = sight_line(*args, dem, 30.0, 2.0, cls, canopy, log_t)
    capped = sight_line(*args, dem, 30.0, 2.0, cls, canopy, log_t, max_obstructed=3)
    assert full[0] < 0.5 ** 10 and capped.tolist() == pytest.approx([0.125, 0.125])   # per ray


def test_water_hidden_behind_a_ridge():
    g, area, line, size, dem, nlcd = lake_scene()
    dem[:, 25] = 140                                                  # 40 m bank between road and lake
    W, _ = run_w(pts([[600, 585]]), g, area, line, size, dem, nlcd)
    assert W[0] == pytest.approx(0, abs=1e-12)


def test_pond_does_not_hide_the_lake_behind_it():
    g, area, line, size, dem, nlcd = lake_scene()
    area[18, 22] = True                                               # a one-cell pond 60 m from the road
    size[18, 22] = 0.05
    reach = lambda x, y: np.where(x < 800, 150.0, 1000.0)            # noqa: E731
    s = pts([[600, 585]])
    W, _ = water_points(s, g, area, line, size, reach, np.full(1, 100.0), dem, nlcd, CFG)
    assert W[0] > 0.5                                                 # the lake 315 m away, not the pond


def test_sight_line_terrain_and_canopy():
    g = tiny_grid()
    dem = np.full(g.shape, 100.0, np.float32)
    cls = np.zeros(g.shape, np.uint8)
    canopy = np.zeros(256)
    log_t = np.zeros(256)
    canopy[41], log_t[41] = 20.0, np.log(0.5)
    args = (np.array([45.0]), np.array([600.0]), np.array([101.2]), np.array([1005.0]), np.array([600.0]),
            np.array([100.0]), g)
    assert sight_line(*args, dem, 30.0, 2.0, cls, canopy, log_t)[0] == 1.0
    cls[:, 10] = 41
    assert sight_line(*args, dem, 30.0, 2.0, cls, canopy, log_t)[0] == pytest.approx(0.5)
    dem[:, 20] = 110.0
    assert sight_line(*args, dem, 30.0, 2.0, cls, canopy, log_t)[0] == pytest.approx(0, abs=1e-12)


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


def test_views_land_and_prospect_formulas():
    zero = np.zeros(5)
    open_ = np.array([1.0, 1.0, 0.0, 0.0, 0.5])
    forest = np.array([0.0, 0.0, 1.0, 0.0, 0.5])
    prospect = np.array([0.0, 1.0, 1.0, 0.0, 0.5])
    v01 = cfg_v01()
    V = views_points(open_, forest, prospect, zero, v01)
    assert V.tolist() == pytest.approx([0.35, 1.0, 0.25, 0.0, 0.5 * (0.35 + 0.65 * 0.5) + 0.5 * 0.25])
    # v0.1: water in view and prospect combine as independent chances of a view.
    assert views_points(np.ones(1), np.zeros(1), np.array([0.5]), np.array([0.5]), v01)[0] == \
        pytest.approx(0.35 + 0.65 * 0.75)
    # v0.2: prospect is left to H; V = open * (0.35 + 0.65 * V_water) + forest * 0.25.
    assert views_points(open_, forest, prospect, zero, CFG).tolist() == \
        pytest.approx([0.35, 0.35, 0.25, 0.0, 0.5 * 0.35 + 0.5 * 0.25])
    assert views_points(np.ones(1), np.zeros(1), np.array([1.0]), np.array([0.5]), CFG)[0] == \
        pytest.approx(0.35 + 0.65 * 0.5)
    p = prospect_points(np.array([100.0, 300.0, 100.0]), np.array([90.0, 100.0, 100.0]),
                        np.array([120.0, 300.0, 900.0]), CFG)
    assert p.tolist() == pytest.approx([0.0, (200 - 30) / 200, 1.0])
    assert land_points(np.array([0.9]), np.array([1.0]), CFG)[0] == pytest.approx(0.8 * 0.9 + 0.2)


def test_water_view_angle_distance_and_ridge():
    g90 = Grid(0.0, 90 * 60.0, 90.0, 60, 60)
    big = np.zeros(g90.shape, bool)
    big[:, 50:] = True                                                # broad lake from x = 4500 m
    from scipy import ndimage as ndi
    _, (ir, ic) = ndi.distance_transform_edt(~big, return_indices=True)
    idx = (ir * g90.width + ic).astype(np.int32)
    wz = np.full(g90.shape, 100.0, np.float32)
    dem = np.full(g90.shape, 100.0, np.float32)
    y = np.array([2700.0, 2700.0, 2700.0])
    x = np.array([4500 - 1000.0, 4500 - 1000.0, 4500 - 3500.0])
    z = np.array([200.0, 110.0, 200.0])                               # high and close, low and close, high and far
    R = np.full(g90.shape, 4000.0, np.float32)                        # view range 4 km: fades from 2 km
    wv = water_view_points(x, y, z, g90, idx, wz, dem, R, CFG)
    assert wv[0] == pytest.approx(1.0)
    theta = np.degrees(np.arctan2(11.2, 1045.0))
    assert wv[1] == pytest.approx(theta / 2.0, rel=1e-3)
    assert 0 < wv[2] < 0.5                                            # 3.5 km: fading out
    # Wider water reads from farther away.
    assert water_view_points(x[2:], y[2:], z[2:], g90, idx, wz, dem, R * 1.5, CFG)[0] > wv[2]
    ridge = dem.copy()
    ridge[:, 45] = 300.0                                              # a ridge in between
    assert water_view_points(x[:1], y[:1], z[:1], g90, idx, wz, ridge, R, CFG)[0] == 0.0
    # A hill blocks the straight line to the nearest water, but the driver
    # still sees the lake obliquely past it: a fan ray finds it.
    hill = dem.copy()
    hill[25:36, 42:48] = 300.0
    v = water_view_points(x[:1], y[:1], z[:1], g90, idx, wz, hill, R, CFG)[0]
    assert 0.5 < v <= 1.0


def test_water90_products_reach_and_broad_water():
    k = 3
    area = np.zeros((90, 90), bool)
    area[:, 60:] = True                                               # broad water, 900 m wide
    area[30:33, 10:13] = True                                         # 90 m pond
    dem = np.full(area.shape, 100, np.int16)
    dem[area] = -40                                                   # bathymetry under both
    p = water90_products(area, dem, k, 90.0, CFG)
    assert p["reach90"][15, 25] == pytest.approx(CFG["water"]["reach_max_m"])  # lake shore cell
    assert p["reach90"][10, 3] < 400                                  # the pond
    # The surface is the shore height, not the bed; the middle of the lake too.
    assert p["water_z90"][15, 25] == 100 and p["water_z90"][15, 29] == 100
    assert np.isnan(p["water_z90"][0, 0])
    # Nearest broad water from the west side is the lake, not the pond.
    i = int(p["bigwater_idx90"][10, 3])
    assert i >= 0 and i % 30 == 20
    # View range grows with the water area around it, within the clip.
    wv = p["wv_reach90"]
    assert CFG["views"]["wv_min_m"] <= wv.min() and wv.max() <= CFG["views"]["wv_max_m"]
    assert wv[15, 25] > wv[15, 3]


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
    assert p["forest90"][1, 1] == pytest.approx(pf, rel=1e-5)
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
    assert turning(off, x, y).tolist() == pytest.approx([0, 90, 0])


def test_curvature_is_scored_over_the_window():
    # Lake Rd chain of 200 m edges; edge 2 has one 90-degree bend, which on its
    # own would saturate C (450 deg/km); over its 1000 m window it is 90 deg/km.
    u, v, key, L = chain()
    n = 6
    sig = {k: np.zeros(n) for k in ("W", "H", "V", "L", "Q", "dev", "signals", "junction_halves", "V_water")}
    sig["turn_deg"] = np.array([0, 0, 90.0, 0, 0, 0])
    extra = {"u": u, "v": v, "key": key, "length_m": L, "highway": np.array(["tertiary"] * n, object),
             "maxspeed_tagged": np.zeros(n, bool), "speed_mph": np.full(n, 45.0)}
    r = combine(sig, extra, np.ones(n, bool), CFG)
    assert r["C"][2] == pytest.approx(90 / 1.0 / 200)
    assert r["C"][0] == pytest.approx(90 * 200 / 200 / 0.7 / 200)  # edge 0 window: 700 m, edge 2 weight 200
    assert r["C"][5] == 0


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
           "junction_halves": np.zeros(n), "V_water": np.zeros(n)}
    extra = {"u": u, "v": v, "key": key, "length_m": L, "highway": np.array(["tertiary"] * n, object),
             "maxspeed_tagged": np.zeros(n, bool), "speed_mph": np.full(n, 45.0)}
    active = np.ones(n, bool)
    active[4] = False
    r = combine(sig, extra, active, cfg_v01())
    assert np.isnan(r["pre"][4]) and np.isnan(r["smooth"][4]) and r["S"][4] == 0
    # Edge 2 window: itself (200), edges 1 and 3 (200 each, d=0); edge 0 at d=200: w=200.
    assert r["window_m"][2] == pytest.approx(800)
    assert r["smooth"][2] == pytest.approx(0.35 * 200 / 800)
    # Edge 0: itself 200, edge 1 (200), edge 2 (d=200, w=200), edge 3 (d=400, w=100).
    assert r["smooth"][0] == pytest.approx(0.35 * 200 / 700)
    assert r["smooth"][5] == 0.0               # the cross street sees none of Lake Rd's water
    # A signal on edge 3 counts in edge 2's window by length share.
    sig["signals"] = np.array([0, 0, 0, 1.0, 0, 0])
    r2 = combine(sig, extra, active, cfg_v01())
    per_km = 1 / 0.8
    assert r2["signals_per_km"][2] == pytest.approx(per_km)
    assert r2["penalty"][2] == pytest.approx(0.20 * per_km / 3)


def test_penalties_cap_and_motorway_urban():
    n = 2
    sig = {k: np.zeros(n) for k in ("W", "H", "V", "L", "Q", "C", "signals", "junction_halves", "V_water")}
    sig["dev"] = np.array([0.0, 1.0])
    sig["signals"] = np.array([0.0, 50.0])
    sig["junction_halves"] = np.array([0.0, 50.0])
    extra = {"u": np.array([0, 2]), "v": np.array([1, 3]), "key": np.array([0, 1]), "length_m": np.array([1000.0, 1000.0]),
             "highway": np.array(["motorway", "residential"], object), "maxspeed_tagged": np.array([False, True]),
             "speed_mph": np.array([65.0, 25.0])}
    r = combine(sig, extra, np.ones(n, bool), cfg_v01())
    assert r["penalty"][0] == pytest.approx(0.10)
    assert r["penalty"][1] == pytest.approx(0.5)  # 0.15 + 0.20 + 0.25 + 0.10, capped
    r = combine(sig, extra, np.ones(n, bool), CFG)
    assert r["penalty"][0] == 0.0                 # v0.2: no motorway term
    assert r["penalty"][1] == pytest.approx(0.5)


def test_window_top_takes_the_best_metres_of_the_window():
    # Window of edge 0: pairs to edges 0..3 with weights 200, 200, 100, 50.
    s = np.array([0, 0, 0, 0, 1])
    d = np.array([0, 1, 2, 3, 1])
    w = np.array([200.0, 200.0, 100.0, 50.0, 120.0])
    vals = np.array([0.1, 0.5, 0.9, 0.3])
    top = window_top(s, d, w, vals, 2, 300.0)
    assert top[0] == pytest.approx((100 * 0.9 + 200 * 0.5) / 300)   # best 300 m: edge 2, then 200 m of edge 1
    assert top[1] == pytest.approx(0.5)                             # window under 300 m: its max


def test_v02_penalty_multiplies_and_spares_water_in_town():
    n = 3
    sig = {k: np.zeros(n) for k in ("H", "V", "L", "Q", "C", "signals", "junction_halves")}
    sig["W"] = np.array([0.8, 0.0, 0.8])
    sig["V_water"] = np.array([1.0, 0.0, 0.0])
    sig["dev"] = np.array([0.8, 0.8, 0.8])              # all in town
    extra = {"u": np.array([0, 2, 4]), "v": np.array([1, 3, 5]), "key": np.array([0, 1, 2]),
             "length_m": np.full(n, 1000.0), "highway": np.array(["primary"] * n, object),
             "maxspeed_tagged": np.zeros(n, bool), "speed_mph": np.full(n, 45.0)}
    r = combine(sig, extra, np.ones(n, bool), CFG)
    raw = 0.40 * sig["W"]
    assert r["penalty"][0] == 0.0                        # lake in full view: no town penalty
    assert r["penalty"][1] == pytest.approx(0.25)
    assert r["penalty"][2] == pytest.approx(0.25 * 0.2)  # gated by max(W, V_water) = 0.8
    assert r["pre"].tolist() == pytest.approx((raw * (1 - r["penalty"])).tolist())
    assert (r["pre"] >= 0).all()                         # a penalty never makes a view negative
    r01 = combine(sig, extra, np.ones(n, bool), cfg_v01())
    assert r01["pre"][1] == pytest.approx(-0.25)


def test_v02_pooled_smoothing_keeps_the_lake_edge():
    u, v, key, L = chain()
    n = 6
    sig = {k: np.zeros(n) for k in ("H", "V", "L", "Q", "C", "dev", "signals", "junction_halves", "V_water")}
    sig["W"] = np.array([0, 0, 1.0, 0, 0, 0])
    extra = {"u": u, "v": v, "key": key, "length_m": L, "highway": np.array(["tertiary"] * n, object),
             "maxspeed_tagged": np.zeros(n, bool), "speed_mph": np.full(n, 45.0)}
    r = combine(sig, extra, np.ones(n, bool), CFG)
    # Edge 2 window: 1000 m (edges 0-4), mean 0.40 * 200 / 1000; best 300 m: 200 m of lake, 100 m of 0.
    assert r["window_m"][2] == pytest.approx(1000)
    assert r["smooth"][2] == pytest.approx(0.5 * 0.40 * 200 / 1000 + 0.5 * 0.40 * 200 / 300)
    r01 = combine(sig, extra, np.ones(n, bool), cfg_v01())
    assert r["smooth"][2] > r01["smooth"][2] * 2


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


# ---------------------------------------------------------------- review fixes (v0.1.1)


def test_sight_line_checks_every_step_before_the_stop():
    g = tiny_grid()
    dem = np.full(g.shape, 100.0, np.float32)
    # An 89 m ray at 30 m steps samples 30 and 60 m (was: 30 m only).
    px, py = np.array([15.0]), np.array([615.0])
    args = (px, py, np.array([101.2]), px + 89.0, py, np.array([100.0]), g)
    assert sight_line(*args, dem, 30.0, 2.0)[0] == 1.0
    dem[:, 2] = 120.0                                                 # a bank 60 m out (x = 60..90)
    assert sight_line(*args, dem, 30.0, 2.0)[0] == 0.0
    # The stop itself is never sampled: a 90 m ray samples 30 and 60 m only.
    dem[:, 2] = 100.0
    dem[:, 3] = 120.0                                                 # x = 90..120
    args90 = (px, py, np.array([101.2]), px + 90.0, py, np.array([100.0]), g)
    assert sight_line(*args90, dem, 30.0, 2.0)[0] == 1.0


def test_water_surface_is_the_shore_not_the_bed_nor_clamped():
    from scenic.rasters import water_surface90
    k = 3
    area = np.zeros((60, 90), bool)
    area[:, 60:] = True
    dem = np.full(area.shape, 50, np.int16)
    dem[area] = -80                                                   # river bed
    z = water_surface90(area, dem, k, 5, 0.1)
    assert np.all(z[:, 20:] == 50) and np.all(np.isnan(z[:, :19]))
    # A flooded quarry below sea level keeps its height.
    dem2 = np.full(area.shape, -30, np.int16)
    dem2[area] = -55
    assert np.nanmax(water_surface90(area, dem2, k, 5, 0.1)) == -30
    # A river falling downstream: each reach takes its own shore height.
    dem3 = np.where(np.arange(60)[:, None] < 30, 200, 100).astype(np.int16) * np.ones((1, 90), np.int16)
    dem3[area] = 0
    z3 = water_surface90(area, dem3, k, 5, 0.1)
    assert z3[0, 25] == 200 and z3[19, 25] == 100


def test_water_points_use_the_surface_height():
    g, area, line, size, dem, nlcd = lake_scene()
    dem[area] = 40                                                    # bathymetry 60 m below the shore
    s = pts([[600, 585]])
    W_bed, _ = run_w(s, g, area, line, size, dem, nlcd)
    W_surf, _ = water_points(s, g, area, line, size, lambda x, y: np.full(len(x), 1000.0), np.full(1, 100.0),
                             dem, nlcd, CFG, zsurf_of=lambda x, y: np.full(len(x), 100.0))
    W_ref, _ = run_w(s, g, area, line, size, np.full_like(dem, 100), nlcd)
    assert W_surf[0] == pytest.approx(W_ref[0])
    assert W_bed[0] <= W_surf[0]


def test_worthless_nearest_shore_still_gets_the_fan():
    g, area, line, size, dem, nlcd = lake_scene()
    # A pond under 1 ha (size 0) right in front of the road, same reach band
    # as the lake; the lake is visible to either side of it.
    area[18:22, 22:24] = True
    size[18:22, 22:24] = 0.0
    nlcd[18:22, 22:24] = 11
    s = pts([[600, 585]])
    W, _ = run_w(s, g, area, line, size, dem, nlcd, reach=1000.0)
    assert W[0] > 0.5


def test_cached_signals_and_samples_must_match_the_edges(tmp_path):
    from scenic.graph import edges_fingerprint
    from scenic.score import load_samples, read_edge_signals, save_samples, write_edge_signals
    e1 = pa.table({"u": [0, 1], "v": [1, 2], "way_id": [5, 6], "length_m": [10.0, 20.0]})
    e2 = pa.table({"u": [1, 0], "v": [2, 1], "way_id": [6, 5], "length_m": [20.0, 10.0]})  # same rows, reordered
    fp1, fp2 = edges_fingerprint(e1), edges_fingerprint(e2)
    p = write_edge_signals({"W": np.array([0.1, 0.2])}, tmp_path / "sig.parquet", fp1)
    assert read_edge_signals(p, fp1).column("W").to_pylist() == pytest.approx([0.1, 0.2])
    with pytest.raises(ValueError):
        read_edge_signals(p, fp2)
    s = Samples(np.zeros(4), np.zeros(4), np.array([0, 0, 1, 1], np.int32), np.array([2, 2]), np.array([0, 2, 4]))
    save_samples(tmp_path / "s.npz", s, fp1)
    assert load_samples(tmp_path / "s.npz", fp1)["edge"].tolist() == [0, 0, 1, 1]
    with pytest.raises(ValueError):
        load_samples(tmp_path / "s.npz", fp2)


# ---------------------------------------------------------------- v0.2 validation


def _roads():
    """Four roads along a line; refs, names and midpoints for the list checks."""
    refs = np.array(["NY 1", "NY 2", "NY 3", "I 9"], object)
    names = np.array([None] * 4, object)
    lon = np.array([-75.0, -74.0, -73.0, -72.0])
    lat = np.full(4, 43.0)
    return refs, names, lon, lat


def test_v3_splits_urban_motorway_and_flat():
    refs, names, lon, lat = _roads()
    S = np.array([0.9, 0.15, 0.35, 0.3])
    L = np.full(4, 1000.0)
    box = lambda x: [x - 0.1, 42.9, x + 0.1, 43.1]                  # noqa: E731
    cfg = {"v3": {"scenic_min": 0.65, "scenic_need": 1, "urban_max": 0.20, "urban_need": 1,
                  "flat_band": [0.20, 0.50]},
           "scenic": [{"label": "a", "ref": "NY 1", "bbox": box(-75)}],
           "dull": [{"label": "b", "ref": "NY 2", "bbox": box(-74)},
                    {"label": "m", "ref": "I 9", "bbox": box(-72), "kind": "motorway"}],
           "holdout": {"dull": [{"label": "f", "ref": "NY 3", "bbox": box(-73), "kind": "flat"}]}}
    r = v3_lists(cfg, S, L, np.zeros(4, bool), refs, names, lon, lat)
    assert r["pass"] and r["urban"]["of"] == 1 and r["flat"]["of"] == 1
    assert r["flat"]["items"][0]["S"] == 0.35                       # farmland in the band, not "dull"
    assert r["motorway_reported"]["items"][0]["S"] == 0.3 and "pass" not in r["motorway_reported"]


def test_holdout2_is_sealed_until_declared_final():
    refs, names, lon, lat = _roads()
    S = np.array([0.9, 0.15, 0.35, 0.3])
    L = np.full(4, 1000.0)
    box = lambda x: [x - 0.1, 42.9, x + 0.1, 43.1]                  # noqa: E731
    h = {"declared_final": False, "urban_max": 0.2, "flat_band": [0.2, 0.5], "scenic_min": 0.65,
         "scenic": [{"label": "a", "ref": "NY 1", "bbox": box(-75)}],
         "urban": [{"label": "b", "ref": "NY 2", "bbox": box(-74)}],
         "flat": [{"label": "f", "ref": "NY 3", "bbox": box(-73)}], "od": []}
    diag = {"W": S}
    r = holdout2({"holdout2": h}, S, L, np.zeros(4, bool), refs, names, lon, lat, diag)
    assert r["status"] == "sealed" and "S" not in str(r)
    h["declared_final"] = True
    r = holdout2({"holdout2": h}, S, L, np.zeros(4, bool), refs, names, lon, lat, diag)
    assert r["status"] == "scored" and r["scenic"]["items"][0]["S"] == 0.9 and r["pass"]


def test_byway_neighbours_compare_same_class_rural_roads_nearby():
    # Byway: two rural primaries (S 0.8); neighbours: a primary at 0.5 within
    # 15 km, a residential at 0.9 (other class), a primary at 0.95 far away,
    # a town primary on the byway (dev 0.6) that is left out.
    S = np.array([0.8, 0.8, 0.5, 0.9, 0.95, 0.1])
    L = np.full(6, 1000.0)
    hw = np.array(["primary", "primary", "primary", "residential", "primary", "primary"], object)
    dev = np.array([0.1, 0.1, 0.1, 0.1, 0.1, 0.6])
    x = np.array([0.0, 1000.0, 5000.0, 5000.0, 40000.0, 500.0])
    by = np.array([True, True, False, False, False, True])
    r = byway_neighbours({"b": by}, by, S, L, hw, dev, np.zeros(6, bool), x, np.zeros(6), 15000.0, 0.3)
    assert r["b"]["median_S"] == pytest.approx(0.8) and r["b"]["neighbour_median_S"] == pytest.approx(0.5)
    assert r["b"]["beats"] and r["b"]["rural_km"] == 2.0
