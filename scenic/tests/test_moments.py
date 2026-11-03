import numpy as np
import pytest

from scenic.moments import cluster, far_sight, vista_value, water_value, window_min
from scenic.rasters import Grid

MC = {"water_W": 0.5, "water_V": 0.7, "vista_drop_scale_m": 300.0, "vista_m_min": 0.5}


def test_water_value_gates_and_scales_by_size():
    W = np.array([0.6, 0.4, 0.2, 0.9])
    V = np.array([0.0, 0.8, 0.5, 0.0])
    size = np.array([1.0, 0.0, 1.0, 0.5])
    m = water_value(W, V, size, MC)
    assert m == pytest.approx([0.6, 0.4, 0.0, 0.9 * 0.75])


def test_vista_value_clipped():
    assert vista_value(np.array([150.0, 240.0, 600.0]), MC) == pytest.approx([0.5, 0.8, 1.0])


def test_cluster_along_road_then_across_roads():
    # road key 1: points every 500 m on x; road key 2: one point 600 m off
    # the best point of road 1, and one far away
    x = np.array([0.0, 500.0, 1000.0, 3000.0, 0.0, 50000.0])
    y = np.array([0.0, 0.0, 0.0, 0.0, 600.0, 0.0])
    key = np.array([1, 1, 1, 1, 2, 2])
    m = np.array([0.9, 1.0, 0.5, 0.7, 0.8, 0.6])
    heads, owner = cluster(m, x, y, key, road_m=2000.0, cross_m=1000.0)
    # 1 claims 0, 2 (and 3 at 2.5 km stays apart); 4 is absorbed by 1 across roads
    assert list(heads) == [1, 3, 5]
    assert list(owner) == [1, 1, 1, 3, 1, 5]


def test_cluster_does_not_chain():
    # 0 - 1 - 2 at 1.5 km steps on one road: 0 (best) claims 1 only; 2 is its own
    x = np.array([0.0, 1500.0, 3000.0])
    heads, owner = cluster(np.array([1.0, 0.9, 0.8]), x, np.zeros(3), np.zeros(3, int), 2000.0, 1000.0)
    assert list(heads) == [0, 2] and list(owner) == [0, 0, 2]


def flat_grid(z, res=90.0):
    h, w = z.shape
    return Grid(x0=0.0, y0=h * res, res=res, width=w, height=h)


def test_far_sight_sees_low_ground_and_respects_a_ridge():
    z = np.full((41, 41), 100.0, dtype=np.float32)
    eye_r = eye_c = 20
    g = flat_grid(z)
    px, py = g.centres(np.array([eye_r]), np.array([eye_c]))
    # plain 100 m below the eye: seen to the edge of the 3 km reach
    d = far_sight(px, py, np.array([200.0]), g, z, 8, 1800.0, 5.0, 75.0)
    assert d[0] >= 1700
    # a 400 m wall 2 cells out on every side hides everything beyond
    z2 = z.copy()
    z2[eye_r - 2:eye_r + 3, eye_c - 2:eye_c + 3] = 600.0
    z2[eye_r - 1:eye_r + 2, eye_c - 1:eye_c + 2] = 100.0
    d2 = far_sight(px, py, np.array([200.0]), g, z2, 8, 1800.0, 5.0, 75.0)
    assert d2[0] < 200
    # ground not 75 m below the eye never counts
    d3 = far_sight(px, py, np.array([150.0]), g, z, 8, 1800.0, 5.0, 75.0)
    assert d3[0] == 0


def test_window_min_disk_and_ignore():
    a = np.full((9, 9), 10.0)
    a[4, 7] = 1.0  # 3 cells right of the centre
    a[0, 0] = -5.0  # outside a radius-3 disk around (4, 4)
    a[4, 3] = -9.0
    r, c = np.array([4]), np.array([4])
    assert window_min(a, r, c, 3.0, ignore=-9.0)[0] == 1.0
    assert window_min(a, r, c, 3.0)[0] == -9.0
