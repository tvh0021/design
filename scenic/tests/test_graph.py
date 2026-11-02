import numpy as np
import pytest

from scenic.graph import (
    WayCollector, default_speed, in_canada, is_drivable, oneway_direction, parse_maxspeed,
    simplify,
)


# ---- maxspeed -------------------------------------------------------------

ROCHESTER = (-77.61, 43.16)  # (lon, lat) in the US
TORONTO = (-79.38, 43.65)    # across Lake Ontario, in Canada
FORT_ERIE = (-78.93, 42.91)  # across the Niagara River from Buffalo
BUFFALO = (-78.88, 42.89)


@pytest.mark.parametrize("value, mph", [
    ("45 mph", 45.0),
    ("45mph", 45.0),
    (" 55 MPH ", 55.0),
    ("35 mph;45 mph", 35.0),  # several values: first wins
])
def test_parse_maxspeed_mph(value, mph):
    assert parse_maxspeed(value) == pytest.approx(mph)
    assert parse_maxspeed(value, lonlat=TORONTO) == pytest.approx(mph)  # explicit unit wins


@pytest.mark.parametrize("value", ["80 km/h", "80 kmh", "80km/h", "80 kph"])
def test_parse_maxspeed_kmh(value):
    assert parse_maxspeed(value) == pytest.approx(80 / 1.609344)
    assert parse_maxspeed(value, lonlat=ROCHESTER) == pytest.approx(80 / 1.609344)


@pytest.mark.parametrize("value", ["50", "80", "55", "30"])
def test_parse_maxspeed_bare_is_kmh(value):
    """OSM convention: a bare number is km/h (no location given, or in Canada)."""
    kmh = float(value) / 1.609344
    assert parse_maxspeed(value) == pytest.approx(kmh)
    assert parse_maxspeed(value, lonlat=TORONTO) == pytest.approx(kmh)
    assert parse_maxspeed(value, lonlat=FORT_ERIE) == pytest.approx(kmh)


@pytest.mark.parametrize("value", ["25", "30", "35", "45", "55", "65"])
def test_parse_maxspeed_bare_us_mph_set(value):
    """On a US road, bare values that are common US mph limits are mph."""
    assert parse_maxspeed(value, lonlat=ROCHESTER) == pytest.approx(float(value))
    assert parse_maxspeed(value, lonlat=BUFFALO) == pytest.approx(float(value))


@pytest.mark.parametrize("value", ["20", "40", "50", "60"])
def test_parse_maxspeed_bare_us_ambiguous_stays_kmh(value):
    assert parse_maxspeed(value, lonlat=ROCHESTER) == pytest.approx(float(value) / 1.609344)


@pytest.mark.parametrize("lon, lat, canada", [
    (-77.61, 43.16, False),  # Rochester
    (-76.15, 43.05, False),  # Syracuse
    (-73.99, 40.75, False),  # Manhattan
    (-75.49, 44.69, False),  # Ogdensburg
    (-79.06, 43.10, False),  # Niagara Falls, NY
    (-79.38, 43.65, True),   # Toronto
    (-75.52, 44.71, True),   # Prescott, ON
    (-79.09, 43.09, True),   # Niagara Falls, ON
    (-73.57, 45.50, True),   # Montreal
])
def test_in_canada(lon, lat, canada):
    assert in_canada(lon, lat) is canada


@pytest.mark.parametrize("value", [None, "", "none", "signals", "US:urban", "0", "500 mph"])
def test_parse_maxspeed_missing_or_bad(value):
    assert parse_maxspeed(value) is None


def test_collector_reads_bare_maxspeed_by_location():
    c = WayCollector()
    c.add(1, {"highway": "trunk", "maxspeed": "55"}, [(1, -77.61, 43.16), (2, -77.60, 43.16)])
    c.add(2, {"highway": "secondary", "maxspeed": "80"}, [(3, -79.38, 43.65), (4, -79.37, 43.65)])
    c.add(3, {"highway": "secondary", "maxspeed": "55"}, [(5, -79.38, 43.65), (6, -79.37, 43.65)])
    w = c.finish()
    assert w.speed_mph == pytest.approx([55.0, 80 / 1.609344, 55 / 1.609344])
    assert w.maxspeed_tagged.all()


def test_default_speeds():
    assert default_speed("motorway") == 65
    assert default_speed("residential") == 25
    assert default_speed("primary_link") == 35


# ---- oneway ---------------------------------------------------------------

@pytest.mark.parametrize("highway, oneway, junction, expected", [
    ("residential", None, None, 0),
    ("residential", "yes", None, 1),
    ("residential", "true", None, 1),
    ("residential", "1", None, 1),
    ("residential", "-1", None, -1),
    ("residential", "no", None, 0),
    ("motorway", None, None, 1),          # implied
    ("motorway_link", None, None, 1),     # implied
    ("motorway", "no", None, 0),          # explicit tag wins
    ("primary", None, "roundabout", 1),   # implied
    ("tertiary", None, "circular", 1),
    ("primary", "reversible", None, None),
])
def test_oneway_direction(highway, oneway, junction, expected):
    assert oneway_direction(highway, oneway, junction) == expected


# ---- access / class filter ------------------------------------------------

@pytest.mark.parametrize("tags, ok", [
    ({"highway": "residential"}, True),
    ({"highway": "service"}, False),
    ({"highway": "track"}, False),
    ({"highway": "footway"}, False),
    ({"highway": "trunk_link"}, True),
    ({"highway": "residential", "access": "private"}, False),
    ({"highway": "residential", "access": "no"}, False),
    ({"highway": "residential", "motor_vehicle": "no"}, False),
    ({"highway": "residential", "access": "no", "motor_vehicle": "yes"}, True),
    ({"highway": "residential", "access": "destination"}, True),
])
def test_is_drivable(tags, ok):
    assert is_drivable(tags) is ok


# ---- simplification -------------------------------------------------------

D = 0.001  # degrees of longitude on the equator, about 111.2 m


def pts(ids):
    """Nodes on the equator; node id n sits at lon = n * D."""
    return [(n, n * D, 0.0) for n in ids]


def toy_ways():
    c = WayCollector()
    c.add(100, {"highway": "residential"}, pts([1, 2, 3, 4, 5]))
    c.add(200, {"highway": "residential", "oneway": "-1"}, pts([3, 6, 7]))
    c.add(300, {"highway": "service"}, pts([5, 9]))  # excluded, so 5 stays an endpoint only
    return c.finish()


def test_simplify_junctions_and_edges():
    w = toy_ways()
    g = simplify(w)
    ids = g.node_osm_id
    assert list(ids) == [1, 3, 5, 7]  # 2, 4, 6 are interior; 9 is on an excluded way
    edges = sorted((int(ids[a]), int(ids[b]), bool(o)) for a, b, o in zip(g.u, g.v, g.oneway))
    # way 200 is oneway=-1, so it is stored reversed: 7 -> 3, oneway
    assert edges == [(1, 3, False), (3, 5, False), (7, 3, True)]


def test_simplify_keeps_geometry_and_length():
    w = toy_ways()
    g = simplify(w)
    ids = g.node_osm_id
    for e in range(len(g.u)):
        geom = w.node_id[g.geom_idx[g.geom_off[e]:g.geom_off[e + 1]]]
        assert geom[0] == ids[g.u[e]] and geom[-1] == ids[g.v[e]]
        assert len(geom) == 3  # each toy edge spans one interior node
        span = abs(int(ids[g.u[e]]) - int(ids[g.v[e]]))  # nodes sit at lon = id * D
        assert g.length_m[e] == pytest.approx(span * 111.195, rel=1e-3)
    rev = [e for e in range(len(g.u)) if g.oneway[e]][0]
    assert list(w.node_id[g.geom_idx[g.geom_off[rev]:g.geom_off[rev + 1]]]) == [7, 6, 3]


def test_simplify_closed_way_and_self_crossing():
    c = WayCollector()
    c.add(1, {"highway": "tertiary", "junction": "roundabout"}, pts([20, 21, 22, 20]))
    c.add(2, {"highway": "tertiary"}, pts([22, 23]))
    g = simplify(c.finish())
    ids = g.node_osm_id
    assert list(ids) == [20, 22, 23]
    edges = sorted((int(ids[a]), int(ids[b]), bool(o)) for a, b, o in zip(g.u, g.v, g.oneway))
    assert edges == [(20, 22, True), (22, 20, True), (22, 23, False)]
