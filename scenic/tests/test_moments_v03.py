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
        "bay_node_parent_m2": 1e8, "bay_node_m": 800.0, "river_name_m": 300.0, "river_wide_name_m": 1500.0,
        "lake_wins_min_m2": 1e6, "lake_wins_m": 400.0}

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


def name(features, hx, hy, wide=None, mc=None):
    return name_parent(features, hx, hy, wide, mc)[0]


def name_parent(features, hx, hy, wide=None, mc=None):
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
                       np.asarray(lon), np.asarray(lat), mc or NAME, wide=wide)
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


def test_nearer_river_beats_detached_canal_area():
    canal = ("water", "Power Canal", shapely.box(400, 0, 900, 1000), 5e5, "canal")
    river = ("river", "Main River", shapely.LineString([(0, -1000), (0, 1000)]), np.nan, "river")
    assert name([canal, river], [20.0], [200.0]) == ["Main River"]
    assert name([canal, river], [500.0], [200.0]) == ["Power Canal"]


def test_canal_name_propagates_only_through_connected_canal_endpoints():
    from scenic.moments import propagate_canal_names
    rows = dict(kind=["canal"] * 4 + ["river"], name=["Erie Canal", None, None, None, None],
                wkb=[_wkb(shapely.LineString(p)) for p in
                     [[(0, 0), (100, 0)], [(100, 0), (200, 0)], [(200, 0), (300, 0)],
                      [(100, 10), (200, 10)], [(300, 0), (400, 0)]]])
    propagate_canal_names(rows)
    assert rows["name"] == ["Erie Canal", "Erie Canal", "Erie Canal", None, None]


def test_motorway_near_big_water_still_needs_long_wet_bridge():
    base = dict(highway=["motorway"] * 2, size=[1.0] * 2, wfrac=[0.5] * 2,
                dev=[0.0] * 2, jdens=[0.0] * 2, named=[True] * 2, storm=[False] * 2)
    assert list(water_gate(**base, mc=GATE, bridge=[False, True])) == [3, 0]


def test_nearest_named_canal_can_label_a_crossing():
    canal = ("canal", "Erie Canal", shapely.LineString([(-1000, 0), (1000, 0)]), np.nan, "canal")
    assert name([canal], [0.0], [20.0]) == ["Erie Canal"]


def test_long_bridge_requires_substantial_actual_water_crossing():
    from scenic.moments import long_water_bridge
    mc = {"water_bridge_min_m": 500, "water_bridge_wet_m": 200}
    assert list(long_water_bridge([True, True, True, False], [1368, 1000, 300, 1368],
                                  [0.1724, 0.1, 1.0, 0.9], mc)) == [True, False, False, False]


def test_major_bridge_anchors_cluster_instead_of_shore_road():
    from scenic.moments import anchored_value, cluster, representative_rank
    m, bridge = np.array([1.0, 0.65, 0.9]), np.array([False, True, False])
    heads, owner = cluster(representative_rank(m, bridge), np.array([0, 900, 1800]), np.zeros(3), np.ones(3),
                           2000, 1000)
    assert list(heads) == [1]
    assert list(owner) == [1, 1, 1]
    # the deck is the position; the value is the best member edge's
    assert list(anchored_value(m, owner, heads, bridge)) == [1.0]


def test_only_bridge_anchored_clusters_take_the_best_member_value():
    from scenic.moments import anchored_value
    # two clusters: a shore road headed by its own best edge, and a bridge
    # head (0.6) with a 0.95 member
    m = np.array([0.8, 0.5, 0.6, 0.95])
    owner = np.array([0, 0, 2, 2])
    bridge = np.array([False, False, True, False])
    assert list(anchored_value(m, owner, np.array([0, 2]), bridge)) == [0.8, 0.95]
    # without the bridge flag the head keeps its own value
    assert list(anchored_value(m, owner, np.array([0, 2]), np.zeros(4, bool))) == [0.8, 0.6]


def test_lake_beats_the_creek_crossed_on_a_lakeshore_road():
    """Canadaway Creek on NY 5 at Dunkirk is Lake Erie: a lake of at least
    lake_wins_min_m2 within lake_wins_m keeps the label even though the
    crossed creek line is nearer."""
    creek = ("river", "Toy Creek", shapely.LineString([(0, -3000), (0, 0)]), np.nan, "river")
    mc = {**NAME, "water_name_m": 1500.0}
    # 10 m from the creek, 300 m from the lake shore
    assert name([LAKE, creek], [10.0], [-300.0], mc=mc) == ["Great Lake"]
    # 1.4 km from the lake: the crossed creek names it
    assert name([LAKE, creek], [10.0], [-1400.0], mc=mc) == ["Toy Creek"]
    # a lake under lake_wins_min_m2 does not beat a nearer creek
    small = ("water", "Toy Pond", shapely.box(-400, 0, 400, 600), 4.8e5, "lake")
    assert name([small, creek], [10.0], [-300.0], mc=mc) == ["Toy Creek"]


def test_canal_tagged_water_does_not_beat_a_nearer_river():
    """Lewiston: the Chippawa-Queenston Power Canal (water=canal) does not
    take the label from the Niagara River even when large and close."""
    canal = ("water", "Power Canal", shapely.box(100, 0, 900, 3000), 2.4e6, "canal")
    river = ("river", "Main River", shapely.LineString([(0, -1000), (0, 4000)]), np.nan, "river")
    assert name([canal, river], [20.0], [200.0]) == ["Main River"]
    lake = (*canal[:4], "reservoir")
    assert name([lake, river], [20.0], [200.0]) == ["Power Canal"]


def test_bridge_name_never_comes_from_a_plain_road_name():
    from scenic.moments import _label, bridge_name
    assert bridge_name({"name": "FDR Drive", "bridge": "yes"}) is None
    assert bridge_name({"name": "Williamsburg Bridge"}) == "Williamsburg Bridge"
    assert bridge_name({"name": "Robert Moses Causeway"}) == "Robert Moses Causeway"
    assert bridge_name({"name": "Skyway Viaduct"}) == "Skyway Viaduct"
    assert bridge_name({"name": "NY 17", "bridge:name": "Toy Memorial Span"}) == "Toy Memorial Span"
    assert _label("Williamsburg Bridge", "Williamsburg Bridge", "New York") == "Williamsburg Bridge near New York"
    assert _label("Toy Bridge", "NY 5", "Dunkirk") == "Toy Bridge on NY 5 near Dunkirk"


def test_bridge_row_name_from_osm(tmp_path):
    from scenic.moments import read_osm_features
    source = tmp_path / "bridges.osm"
    source.write_text('''<osm version="0.6">
      <node id="1" lat="40.7" lon="-73.97"/><node id="2" lat="40.71" lon="-73.97"/>
      <way id="10"><nd ref="1"/><nd ref="2"/><tag k="highway" v="motorway"/><tag k="bridge" v="yes"/>
      <tag k="name" v="FDR Drive"/></way>
      <way id="11"><nd ref="1"/><nd ref="2"/><tag k="highway" v="primary"/><tag k="bridge" v="yes"/>
      <tag k="name" v="Williamsburg Bridge"/></way>
      <way id="12"><nd ref="1"/><nd ref="2"/><tag k="highway" v="primary"/><tag k="bridge" v="yes"/>
      <tag k="name" v="Toy Road"/><tag k="bridge:name" v="Toy Span"/></way></osm>''')
    rows = read_osm_features(source, progress=False).to_pylist()
    names = {r["way_id"]: r["name"] for r in rows if r["kind"] == "bridge"}
    assert names == {10: None, 11: "Williamsburg Bridge", 12: "Toy Span"}


def test_waterway_relation_names_unnamed_canal_member(tmp_path):
    from scenic.moments import read_osm_features
    source = tmp_path / "canal.osm"
    source.write_text('''<osm version="0.6">
      <node id="1" lat="43" lon="-77"/><node id="2" lat="43" lon="-76.999"/>
      <way id="10"><nd ref="1"/><nd ref="2"/><tag k="waterway" v="canal"/></way>
      <relation id="20"><member type="way" ref="10" role="main_stream"/>
      <tag k="type" v="waterway"/><tag k="waterway" v="canal"/>
      <tag k="name" v="Erie Canal"/></relation></osm>''')
    rows = read_osm_features(source, progress=False).to_pylist()
    canal = [r for r in rows if r["kind"] == "canal"]
    assert len(canal) == 1 and canal[0]["name"] == "Erie Canal"
