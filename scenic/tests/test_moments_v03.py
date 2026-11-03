"""Moments v0.3: water gate, stormwater tags, vista rays and thinning,
junction density, and naming by the most specific water body."""

import numpy as np
import pytest
import shapely

from scenic.moments import (WATER_DROP, _name_water, _unproject, edges_near, is_stormwater, junction_density,
                            sight_rays, storm_tag, thin_heads, water_gate)
from scenic.rasters import Grid

GATE = {"water_min_size": 0.40, "water_named_min_size": 0.25, "water_storm_override_size": 0.5,
        "water_town_dev": 0.30, "water_town_junctions_km2": 30.0, "water_big_size": 0.6,
        "water_broad_wfrac": 0.20}

NAME = {"water_named_min_m2": 1e5, "water_name_m": 600.0, "name_slack_m": 300.0, "name_touch_m": 50.0,
        "bay_node_parent_m2": 1e8, "bay_node_m": 800.0, "river_name_m": 300.0, "river_wide_name_m": 1500.0}

# toy coordinates: EPSG:5070 metres near Rochester
X0, Y0 = 1_380_000.0, 2_380_000.0


def test_storm_tag():
    assert storm_tag({"natural": "water", "water": "basin"}) == "water=basin"
    assert storm_tag({"natural": "water", "water": "Wastewater"}) == "water=wastewater"
    assert storm_tag({"landuse": "basin"}) == "landuse=basin"
    assert storm_tag({"natural": "water", "basin": "retention"}) == "basin=retention"
    assert storm_tag({"natural": "water", "water": "lake"}) is None
    assert storm_tag({"natural": "water", "water": "pond"}) is None  # ponds are judged by size


def gate(**kw):
    base = dict(highway=["secondary"], size=[0.7], wfrac=[0.05], dev=[0.0], jdens=[0.0], named=[False],
                storm=[False])
    base.update({k: [v] for k, v in kw.items()})
    return WATER_DROP[int(water_gate(**base, mc=GATE)[0])]


def test_water_gate_reasons_in_order():
    assert gate() == "kept"
    assert gate(size=0.35) == "small"
    assert gate(size=0.35, named=True) == "kept"  # named water has a lower size floor
    assert gate(size=0.2, named=True) == "small"
    assert gate(storm=True, size=0.45) == "stormwater"
    assert gate(storm=True, size=0.55) == "kept"  # big enough to override a nearby basin
    assert gate(highway="motorway") == "motorway"
    assert gate(dev=0.4) == "town"
    assert gate(jdens=35.0) == "town"
    assert gate(size=0.1, dev=0.9, highway="motorway") == "small"  # first reason wins


def test_big_named_broad_water_lifts_town_motorway_and_storm():
    big = dict(named=True, size=0.9, wfrac=0.5)
    assert gate(dev=0.8, **big) == "kept"
    assert gate(highway="motorway", **big) == "kept"
    assert gate(storm=True, **big) == "kept"
    # named but under water_big_size: a nearby basin still drops it
    assert gate(storm=True, **{**big, "size": 0.45}) == "stormwater"
    # named and large but narrow (little water within 1 km): still a town edge
    assert gate(dev=0.8, named=True, size=0.9, wfrac=0.1) == "town"
    # broad but unnamed: still a town edge
    assert gate(dev=0.8, named=False, size=0.9, wfrac=0.5) == "town"


def test_thin_heads_keeps_best_per_radius():
    x = np.array([0.0, 3000.0, 6000.0, 20000.0, 21000.0])
    m = np.array([0.6, 0.9, 0.7, 0.5, 0.5])
    keep = thin_heads(m, x, np.zeros(5), 5000.0)
    # 1 (best) removes 0 and 2; 3 and 4 tie: the first wins
    assert list(keep) == [False, True, False, True, False]
    assert thin_heads(np.zeros(0), np.zeros(0), np.zeros(0), 100.0).shape == (0,)


def flat_grid(z, res=90.0):
    h, w = z.shape
    return Grid(x0=0.0, y0=h * res, res=res, width=w, height=h)


def test_sight_rays_counts_open_directions():
    # plateau at 200 m dropping to 50 m east of column 23
    z = np.full((41, 41), 200.0, dtype=np.float32)
    z[:, 24:] = 50.0
    g = flat_grid(z)
    eye = np.array([201.2])
    # at the rim: east, north-east and south-east see the low ground (rays
    # are counter-clockwise from east)
    px, py = g.centres(np.array([20]), np.array([23]))
    d = sight_rays(px, py, eye, g, z, 8, 1500.0, 2.0, 75.0)
    assert d.shape == (1, 8)
    assert list(np.flatnonzero(d[0] >= 1000)) == [0, 1, 7]
    # 400 m back from the rim the plateau edge hides the low ground
    px, py = g.centres(np.array([20]), np.array([19]))
    assert sight_rays(px, py, eye, g, z, 8, 1500.0, 2.0, 75.0).max() == 0
    # an isolated summit sees down on every side
    z2 = np.full((41, 41), 50.0, dtype=np.float32)
    z2[20, 20] = 200.0  # a one-cell summit: the ground falls away right at the eye
    px, py = g.centres(np.array([20]), np.array([20]))
    d2 = sight_rays(px, py, eye, g, z2, 8, 1500.0, 2.0, 75.0)
    assert (d2 >= 1000).sum() == 8


def test_junction_density():
    # node 0 meets three edges (a junction); nodes 1-3 are dead ends
    nx_ = np.array([0.0, 100.0, -100.0, 0.0, 5000.0])
    ny_ = np.array([0.0, 0.0, 0.0, 100.0, 0.0])
    u, v = np.array([0, 0, 0]), np.array([1, 2, 3])
    jd = junction_density(u, v, nx_, ny_, np.array([0.0, 2000.0]), np.array([0.0, 0.0]), 400.0)
    assert jd[0] == pytest.approx(1 / (np.pi * 0.4 ** 2))
    assert jd[1] == 0.0


def test_edges_near():
    geoms = [shapely.box(0, 0, 100, 100), None]
    px = np.array([150.0, 400.0, 400.0])
    pe = np.array([0, 1, 2])
    near = edges_near(px, np.array([50.0, 50.0, 50.0]), pe, geoms, 100.0, 4)
    assert list(near) == [True, False, False, False]


def _wkb(poly_xy):
    """WKB in lon/lat of a polygon or line given in toy EPSG:5070 metres."""
    g = shapely.transform(poly_xy, lambda xy: np.column_stack(_unproject(xy[:, 0] + X0, xy[:, 1] + Y0)))
    return shapely.to_wkb(g)


def name(features, hx, hy, wide=None):
    return name_parent(features, hx, hy, wide)[0]


def name_parent(features, hx, hy, wide=None):
    kind = np.asarray([f[0] for f in features], dtype=object)
    fname = np.asarray([f[1] for f in features], dtype=object)
    fwkb = [_wkb(f[2]) if f[2] is not None else None for f in features]
    farea = np.asarray([f[3] for f in features], dtype=np.float64)
    ftag = np.asarray([f[4] for f in features], dtype=object)
    lon, lat = [], []
    for f in features:
        xy = f[5] if len(f) > 5 else None
        if xy is None:
            lon.append(np.nan), lat.append(np.nan)
        else:
            lo, la = _unproject([xy[0] + X0], [xy[1] + Y0])
            lon.append(lo[0]), lat.append(la[0])
    feature = np.full(len(hx), None, dtype=object)
    f, p = _name_water(feature, np.asarray(hx) + X0, np.asarray(hy) + Y0, kind, fname, fwkb, farea, ftag,
                       np.asarray(lon), np.asarray(lat), NAME, wide=wide)
    return list(f), list(p)


# a "Great Lake" north of y = 0 and a bay south of it, sharing the shore
LAKE = ("water", "Great Lake", shapely.box(-20000, 0, 20000, 20000), 8e8, "lake")
BAY = ("bay", "Toy Bay", shapely.box(-1000, -3000, 1000, 0), 6e6, "bay")


def test_bay_beats_the_larger_lake_it_opens_into():
    # moment on the bay's east shore, 200 m from the bay, 400 m from the lake
    assert name([LAKE, BAY], [1200.0], [-400.0]) == ["Toy Bay"]
    # far along the lake shore the lake keeps its name
    assert name([LAKE, BAY], [8000.0], [-200.0]) == ["Great Lake"]


def test_smaller_lake_touching_the_nearest_wins_but_a_detached_pond_does_not():
    arm = ("water", "Toy Arm", shapely.box(-500, -2000, 500, 10), 2e6, "lake")  # overlaps the lake shore
    pond = ("water", "Toy Pond", shapely.box(-500, -2000, 500, -300), 2e6, "pond")  # 300 m off the shore
    assert name([LAKE, arm], [700.0], [-100.0]) == ["Toy Arm"]
    assert name_parent([LAKE, arm], [700.0], [-100.0])[1] == ["Great Lake"]
    # 100 m from the lake, 283 m from the pond (within the slack) but the pond
    # lies 300 m behind the shore
    assert name([LAKE, pond], [700.0], [-100.0]) == ["Great Lake"]


def test_bay_node_names_a_large_parent():
    node = ("bay", "Node Bay", None, np.nan, "bay", (6000.0, 300.0))
    assert name([LAKE, node], [6000.0], [-200.0]) == ["Node Bay"]
    assert name([LAKE, node], [12000.0], [-200.0]) == ["Great Lake"]  # 6 km from the node


def test_bays_keep_their_name_but_belong_to_the_parent_body():
    """Spacing and stop groups use parent_feature, so the bays along one
    lake shore count as one water body (Lake George on NY 9N)."""
    node = ("bay", "Node Bay", None, np.nan, "bay", (6000.0, 300.0))
    river = ("river", "Toy River", shapely.LineString([(-5000, -9000), (5000, -9000)]), np.nan, "river")
    feats, parents = name_parent([LAKE, BAY, node, river], [6000.0, 1200.0, 12000.0, 0.0],
                                 [-200.0, -400.0, -200.0, -9100.0])
    assert feats == ["Node Bay", "Toy Bay", "Great Lake", "Toy River"]
    assert parents == ["Great Lake", "Great Lake", "Great Lake", "Toy River"]


def test_named_feature_is_its_own_parent():
    feature = np.array(["Toy Falls"], dtype=object)
    f, p = _name_water(feature, np.array([X0]), np.array([Y0]), np.array(["water"], dtype=object),
                       np.array(["Great Lake"], dtype=object), [None], np.array([8e8]), np.array([None]),
                       np.array([np.nan]), np.array([np.nan]), NAME)
    assert list(f) == ["Toy Falls"] and list(p) == ["Toy Falls"]


def test_large_named_basin_is_water_not_stormwater():
    floor = 1e5  # water_named_min_m2
    assert not is_stormwater("landuse=basin", "Lewiston Reservoir", 7.61e6, floor)
    assert not is_stormwater("landuse=basin", "Muskrat Lake", 3.87e5, floor)
    assert is_stormwater("landuse=basin", "Toy Retention Pond", 3e4, floor)  # named but small
    assert is_stormwater("landuse=basin", None, 7e6, floor)  # big but unnamed
    assert is_stormwater("water=wastewater", "Toy Treatment Lagoon", 5e5, floor)
    assert not is_stormwater(None, "Toy Lake", 5e5, floor)


def test_wide_water_takes_a_river_name_from_farther_away():
    river = ("river", "Toy River", shapely.LineString([(-5000, 0), (5000, 0)]), np.nan, "river")
    assert name([river], [0.0, 0.0], [1000.0, 200.0]) == [None, "Toy River"]
    assert name([river], [0.0], [1000.0], wide=np.array([True])) == ["Toy River"]
    assert name([river], [0.0], [2000.0], wide=np.array([True])) == [None]


def test_motorway_holds_only_the_listed_moment_types():
    from scenic.moments import type_allowed

    hw = ["motorway", "primary"]
    types = ["water", "vista", "gorge_bridge"]
    assert list(type_allowed(hw, "vista", types)) == [True, True]
    assert list(type_allowed(hw, "gorge_bridge", types)) == [True, True]
    assert list(type_allowed(hw, "gorge", types)) == [False, True]  # road cuts beside a motorway
    assert list(type_allowed(hw, "osm", types)) == [False, True]


def test_unnamed_narrow_water_is_dropped_named_is_not():
    mc = {**GATE, "water_unnamed_min_wfrac": 0.06}
    base = dict(highway=["secondary"] * 3, size=[1.0] * 3, wfrac=[0.03, 0.03, 0.2], dev=[0.0] * 3,
                jdens=[0.0] * 3, named=[False, True, False], storm=[False] * 3)
    assert [WATER_DROP[i] for i in water_gate(**base, mc=mc)] == ["narrow", "kept", "kept"]
