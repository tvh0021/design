import http.client
import json
import math
import re
import threading

import numpy as np
import pytest

from scenic.route import RoadGraph
from scenic.search import Context, Moments, _to5070
from scenic.web import server as web
from scenic.web.server import App, ApiError, Geocoder, make_server, route_display, s_breaks

CFG = {"budgets": [1.15, 1.30, 1.50], "lambdas": [0.5, 1.0, 3.0, 8.0], "alt_lambdas": [1.0], "alt_rounds": 1,
       "reuse_penalty": 1.0, "via_lambda": 1.0, "via_top": 4, "via_top_per_band": 2, "via_max_moments": 2,
       "via_min_extra_min": 1.0, "moment_radius_m": 100.0, "moment_spacing_km": 2.0, "feature_spacing_km": 10.0,
       "moments_per_hour": 6.0, "moment_minutes": 5.0}


def ladder(two_moments=False):
    """Fast bottom road 0-1-2-3-4 (240 s, S 0.2) and a scenic top road
    0-5-6-7-4 (300 s, +25 %, S 0.9) with a moment on 6-7 (as in
    test_search.py). `two_moments` adds a second moment on 5-6 and lifts
    the cap so both are credited."""
    if two_moments:
        mlon, mlat = [-76.955, -76.925], [43.03, 43.03]
        mx, my = _to5070(mlon, mlat)
        lon = [-77.00, -76.97, -76.94, -76.91, -76.88, -76.97, -76.94, -76.91]
        lat = [43.00, 43.00, 43.00, 43.00, 43.00, 43.03, 43.03, 43.03]
        g = RoadGraph(lon, lat, [0, 1, 2, 3, 0, 5, 6, 7], [1, 2, 3, 4, 5, 6, 7, 4], [False] * 8,
                      [60.0, 60.0, 60.0, 60.0, 30.0, 120.0, 120.0, 30.0], [2500.0] * 8)
        mom = Moments(id=np.array([0, 1]), m=np.array([1.0, 1.0]), x=mx, y=my, lon=np.array(mlon),
                      lat=np.array(mlat), edge=np.array([5, 6]), type=np.array(["vista", "water"], dtype=object),
                      label=np.array(["West Hill", "Toy Lake on top road"], dtype=object),
                      of_edge=np.array([-1] * 5 + [0, 1, -1]), feature=np.array([None, "Toy Lake"], dtype=object))
        return Context(g=g, S=np.array([0.2] * 4 + [0.9] * 4), moments=mom,
                       cfg={**CFG, "moments_per_hour": 60.0})
    lon = [-77.00, -76.97, -76.94, -76.91, -76.88, -76.97, -76.94, -76.91]
    lat = [43.00, 43.00, 43.00, 43.00, 43.00, 43.03, 43.03, 43.03]
    u = [0, 1, 2, 3, 0, 5, 6, 7]
    v = [1, 2, 3, 4, 5, 6, 7, 4]
    t = [60.0, 60.0, 60.0, 60.0, 30.0, 120.0, 120.0, 30.0]
    g = RoadGraph(lon, lat, u, v, [False] * 8, t, [2500.0] * 8)
    S = np.array([0.2] * 4 + [0.9] * 4)
    mx, my = _to5070([-76.925], [43.03])
    mom = Moments(id=np.array([0]), m=np.array([1.0]), x=mx, y=my, lon=np.array([-76.925]), lat=np.array([43.03]),
                  edge=np.array([6]), type=np.array(["water"], dtype=object),
                  label=np.array(["Toy Lake on top road"], dtype=object),
                  of_edge=np.array([-1] * 6 + [0, -1]), feature=np.array(["Toy Lake"], dtype=object))
    return Context(g=g, S=S, moments=mom, cfg=dict(CFG))


A, B = [43.0, -77.0], [43.0, -76.88]


class FakeGeocoder:
    def __init__(self):
        self.queries = []

    def search(self, q):
        self.queries.append(q)
        return [{"name": "Toy Town, New York", "lat": 43.0, "lon": -77.0, "type": "town"}]


@pytest.fixture
def app():
    return App(ctx=ladder(), geocoder=FakeGeocoder())


# ---- /api/route ----------------------------------------------------------------


def test_route_returns_fastest_and_picks_with_identical_picks_merged(app):
    status, out = app.api("POST", "/api/route", {"from": A, "to": B})
    assert status == 200, out
    labels = [r["labels"] for r in out["routes"]]
    # +15 % cannot afford the +25 % top road, so it is the fastest route; +30 and +50 share the top road
    assert labels == [["fastest", "+15%"], ["+30%", "+50%"]]
    fast, top = out["routes"]
    assert fast["minutes"] == pytest.approx(4.0) and fast["ratio"] == 1.0
    assert top["ratio"] == pytest.approx(1.25)
    assert top["scenic_minutes"] == pytest.approx(300 * 0.7 / 60, abs=0.05)
    assert [m["label"] for m in top["moments"]] == ["Toy Lake on top road"]
    assert {"lat", "lon", "type", "m"} <= set(top["moments"][0])
    # geometry is [lat, lon] with 5 decimals, from A's node to B's node
    assert top["line"][0] == [43.0, -77.0] and top["line"][-1] == [43.0, -76.88]
    assert all(round(x, 5) == x for p in top["line"] for x in p)
    # runs cover the whole line
    assert top["runs"][0][0] == 0 and top["runs"][-1][1] == len(top["line"]) - 1
    assert out["t0_min"] == pytest.approx(4.0)
    assert len(out["s_breaks"]) == 4 and out["id"]


def test_moments_carry_minutes_from_the_start_in_drive_order():
    app = App(ctx=ladder(two_moments=True), geocoder=FakeGeocoder())
    st, out = app.api("POST", "/api/route", {"from": A, "to": B})
    assert st == 200, out
    top = next(r for r in out["routes"] if "+50%" in r["labels"])
    ms = top["moments"]
    assert [m["label"] for m in ms] == ["West Hill", "Toy Lake on top road"]
    mins = [m["min_along"] for m in ms]
    assert all(isinstance(x, float) for x in mins)
    assert 0 < mins[0] < mins[1] < top["minutes"]
    # West Hill sits mid-edge 5-6: 30 s on 0-5, then half of 120 s
    assert mins[0] == pytest.approx(1.5, abs=0.1)
    assert mins[1] == pytest.approx(3.5, abs=0.1)


def test_routes_carry_role_index_and_mean_s_percentile(app):
    _, out = app.api("POST", "/api/route", {"from": A, "to": B})
    fast, top = out["routes"]
    assert fast["role"] == 0 and top["role"] == 2  # fastest; first label +30% is the 2nd budget
    # half the toy graph's road length has S 0.2 and half 0.9
    assert 0 <= fast["mean_S_pct"] <= 5
    assert top["mean_S_pct"] >= 45


def test_s_percentile():
    grid = np.linspace(0, 1, 101)
    assert web.s_percentile(grid, -1) == 0
    assert web.s_percentile(grid, 0.625) == 62
    assert web.s_percentile(grid, 2) == 100
    assert web.s_percentile(None, 0.5) is None


def test_point_errors_name_the_pin_and_say_what_to_do(app):
    st, out = app.api("POST", "/api/route", {"from": A, "to": [44.5, -74.0]})
    assert st == 422 and out["point"] == "to"
    assert out["error"][0].isupper() and "Drag B onto a road" in out["error"]
    st, out = app.api("POST", "/api/route", {"from": [40.7, -80.6], "to": B})
    assert st == 422 and out["point"] == "from" and "Drag A" in out["error"]


def test_route_payload_is_compact_json(app):
    status, out = app.api("POST", "/api/route", {"from": A, "to": B})
    assert status == 200
    assert "edge_path" not in json.dumps(out)


@pytest.mark.parametrize("body, status, words", [
    ({"from": [40.7, -80.6], "to": B}, 422, "outside New York"),
    ({"from": A, "to": [46.0, -75.0]}, 422, "outside New York"),
    ({"from": "x", "to": B}, 400, "[lat, lon]"),
    ({"to": B}, 400, "[lat, lon]"),
    ({"from": A, "to": [44.5, -74.0]}, 422, "too far from the nearest road"),  # in NY, far from the toy graph
    ({"from": A, "to": A}, 404, "No route"),
])
def test_route_errors(app, body, status, words):
    st, out = app.api("POST", "/api/route", body)
    assert st == status
    assert words in out["error"]


def test_route_while_loading_and_after_a_failed_load():
    gate = threading.Event()
    app = App(loader=lambda: (gate.wait(5), ladder())[1], geocoder=FakeGeocoder())
    assert app.health()["status"] == "loading"
    th = app.start_loading()
    st, out = app.api("POST", "/api/route", {"from": A, "to": B})
    assert st == 503 and "loading" in out["error"]
    gate.set()
    th.join(5)
    h = app.health()
    assert h["status"] == "ready" and h["edges"] == 8 and h["budgets"] == ["+15%", "+30%", "+50%"]

    def boom():
        raise FileNotFoundError("scores.parquet")

    bad = App(loader=boom, geocoder=FakeGeocoder())
    bad.load()
    assert bad.health()["status"] == "error" and "scores.parquet" in bad.health()["error"]
    st, out = bad.api("POST", "/api/route", {"from": A, "to": B})
    assert st == 503


def test_searches_are_serialised(app):
    active, peak = [0], [0]
    from scenic.search import search as real

    def slow_search(ctx, a, b):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        threading.Event().wait(0.05)
        try:
            return real(ctx, a, b)
        finally:
            active[0] -= 1

    app._search = slow_search
    threads = [threading.Thread(target=app.api, args=("POST", "/api/route", {"from": A, "to": B})) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1


def test_unknown_endpoint_and_method(app):
    assert app.api("GET", "/api/nope")[0] == 404
    assert app.api("GET", "/api/route")[0] == 405


# ---- /api/waypoints --------------------------------------------------------------


def test_waypoints_for_a_route(app):
    _, out = app.api("POST", "/api/route", {"from": A, "to": B})
    top = next(r for r in out["routes"] if "+30%" in r["labels"])
    st, wp = app.api("POST", "/api/waypoints", {"id": out["id"], "key": top["key"]})
    assert st == 200, wp
    assert wp["url"].startswith("https://www.google.com/maps/dir/?api=1&origin=43.00000,-77.00000")
    assert wp["url_length"] == len(wp["url"]) < 2048
    assert wp["meets_target"] is True
    assert 1 <= len(wp["waypoints"]) <= 7
    assert wp["parts"] == [] and "single" not in wp
    assert [w["n"] for w in wp["waypoints"]] == list(range(1, len(wp["waypoints"]) + 1))
    w = wp["waypoints"][0]
    assert 43.0 < w["lat"] <= 43.03 + 1e-9  # on the top road
    # cached: a second call does not reduce again
    app._waypoints = lambda *a, **k: pytest.fail("reduced twice")
    assert app.api("POST", "/api/waypoints", {"id": out["id"], "key": top["key"]})[1] == wp


def test_waypoints_errors(app):
    _, out = app.api("POST", "/api/route", {"from": A, "to": B})
    assert app.api("POST", "/api/waypoints", {"id": "gone", "key": "r0"})[0] == 404
    assert app.api("POST", "/api/waypoints", {"id": out["id"], "key": "r9"})[0] == 404


def test_old_searches_expire(app):
    ids = [app.api("POST", "/api/route", {"from": A, "to": B})[1]["id"] for _ in range(web.KEEP_RESULTS + 1)]
    assert app.api("POST", "/api/waypoints", {"id": ids[0], "key": "r0"})[0] == 404
    assert ids[-1] in app.results and len(app.results) == web.KEEP_RESULTS


# ---- display geometry ------------------------------------------------------------


class _SR:
    def __init__(self, route):
        self.route = route
        self.edges = route.edges


def zigzag_graph(n_edges=40, pts_per_edge=200):
    """A chain of edges, each with its own dense wiggly geometry."""
    lon0, lat0 = -76.0, 43.0
    nlon = lon0 + 0.01 * np.arange(n_edges + 1)
    nlat = np.full(n_edges + 1, lat0)
    off, glon, glat = [0], [], []
    for e in range(n_edges):
        x = np.linspace(nlon[e], nlon[e + 1], pts_per_edge)
        y = lat0 + 0.0005 * np.sin(np.linspace(0, 6 * np.pi, pts_per_edge))
        y[0] = y[-1] = lat0
        glon.extend(x)
        glat.extend(y)
        off.append(len(glon))
    u = np.arange(n_edges)
    g = RoadGraph(nlon, nlat, u, u + 1, [False] * n_edges, [10.0] * n_edges, [800.0] * n_edges,
                  geom_off=np.asarray(off), geom_lon=np.asarray(glon), geom_lat=np.asarray(glat))
    return g


def test_display_geometry_is_simplified_and_runs_stay_exact():
    g = zigzag_graph()
    route = g.route(0, 40)
    assert len(route.geometry) == 40 * 199 + 1  # 7961 points
    S = np.where(np.arange(40) < 20, 0.1, 0.9)
    breaks = [0.2, 0.4, 0.6, 0.8]
    d = route_display(g, S, _SR(route), breaks, max_points=2000)
    assert len(d["line"]) <= 2000
    assert d["points_raw"] == 7961
    assert d["runs"] == [[0, d["runs"][0][1], 0], [d["runs"][0][1], len(d["line"]) - 1, 4]]
    # the class boundary is the shared node of edges 19 and 20, exactly
    assert d["line"][d["runs"][0][1]] == [43.0, round(-76.0 + 0.2, 5)]
    assert d["line"][0] == [43.0, -76.0] and d["line"][-1] == [43.0, round(-76.0 + 0.4, 5)]


def test_small_routes_are_not_simplified_beyond_half_a_metre():
    g = zigzag_graph(n_edges=2, pts_per_edge=50)
    route = g.route(0, 2)
    d = route_display(g, np.zeros(2), _SR(route), [0.2, 0.4, 0.6, 0.8])
    assert len(d["line"]) > 50  # wiggles (55 m amplitude) survive


def test_s_breaks_split_road_length_into_fifths():
    g = zigzag_graph(n_edges=10, pts_per_edge=2)
    S = np.linspace(0.05, 0.95, 10)
    br = s_breaks(g, S)
    assert len(br) == 4 and br == sorted(br)
    assert br[0] == pytest.approx(0.15, abs=0.11) and br[-1] == pytest.approx(0.75, abs=0.11)


# ---- geocoding ---------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.t = 100.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def nominatim_reply(name):
    return [{"display_name": name, "lat": "43.123456789", "lon": "-77.987654321", "type": "city"},
            {"display_name": "broken"}]


def test_geocode_rate_limit_and_disk_cache(tmp_path):
    clock = Clock()
    calls = []

    def fetch(q):
        calls.append((q, clock.t))
        clock.t += 0.2  # request takes 0.2 s
        return nominatim_reply(q.title())

    gc = Geocoder(tmp_path, fetch=fetch, clock=clock, sleep=clock.sleep)
    r1 = gc.search("Watkins  Glen ")
    assert r1 == [{"name": "Watkins Glen", "lat": 43.12346, "lon": -77.98765, "type": "city"}]
    assert gc.search("watkins glen") == r1  # same normalised query: cache, no request
    assert len(calls) == 1
    gc.search("Ithaca")
    assert len(calls) == 2
    assert calls[1][1] - calls[0][1] >= 1.0 - 1e-9  # one request per second
    # the second of spacing counts from the end of the previous request (stricter than from its start)
    assert clock.slept == [pytest.approx(1.0)]
    # a new geocoder (a server restart) reads the disk cache
    gc2 = Geocoder(tmp_path, fetch=lambda q: pytest.fail("not cached"), clock=clock, sleep=clock.sleep)
    assert gc2.search("WATKINS GLEN") == r1
    assert list((tmp_path / "geocode").glob("*.json"))


def test_geocode_rate_limit_holds_across_threads(tmp_path):
    import time

    times = []
    lock = threading.Lock()

    def fetch(q):
        with lock:
            times.append(time.monotonic())
        return []

    gc = Geocoder(tmp_path, fetch=fetch, min_interval=0.15)
    threads = [threading.Thread(target=gc.search, args=(f"place {i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    times.sort()
    assert len(times) == 4
    assert all(b - a >= 0.15 - 0.01 for a, b in zip(times, times[1:]))


def test_geocode_concurrent_identical_queries_share_one_fetch(tmp_path):
    import time

    calls = []

    def fetch(q):
        calls.append(q)
        time.sleep(0.05)
        return nominatim_reply("Seneca Falls, Seneca County, New York, United States")

    gc = Geocoder(tmp_path, fetch=fetch, min_interval=0.0)
    out, errors = [], []

    def run():
        try:
            out.append(gc.search("Seneca Falls"))
        except Exception as exc:  # the old code raised FileNotFoundError here
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(out) == 8 and all(r == out[0] for r in out)
    assert len(calls) == 1
    files = list((tmp_path / "geocode").iterdir())
    assert len(files) == 1 and files[0].suffix == ".json"  # no temp files left behind


@pytest.mark.parametrize("content", ["[]", '{"results": null}', '{"results": [1]}', "not json",
                                     '{"results": [{"name": "x", "lat": "43", "lon": -77, "type": ""}]}',
                                     '{"results": [{"lat": 43, "lon": -77}]}'])
def test_geocode_cache_of_the_wrong_shape_is_a_miss(tmp_path, content):
    calls = []

    def fetch(q):
        calls.append(q)
        return nominatim_reply("Ithaca")

    gc = Geocoder(tmp_path, fetch=fetch, sleep=lambda s: None)
    path = gc._path(gc.normalise("Ithaca"))
    path.parent.mkdir(parents=True)
    path.write_text(content)
    assert gc.search("Ithaca") == [{"name": "Ithaca", "lat": 43.12346, "lon": -77.98765, "type": "city"}]
    assert calls == ["ithaca"]
    assert gc.search("ithaca")[0]["name"] == "Ithaca" and len(calls) == 1  # rewritten cache is good


def test_geocode_drops_the_country_and_state_tail(tmp_path):
    names = {"rochester": "Rochester, Monroe County, New York, United States",
             "pa": "Erie, Erie County, Pennsylvania, United States",
             "plain": "Somewhere"}
    gc = Geocoder(tmp_path, fetch=lambda q: nominatim_reply(names[q]), sleep=lambda s: None)
    assert gc.search("rochester")[0]["name"] == "Rochester, Monroe County"
    assert gc.search("pa")[0]["name"] == "Erie, Erie County, Pennsylvania"
    assert gc.search("plain")[0]["name"] == "Somewhere"
    assert web.clean_place_name("New York, United States") == "New York"
    # old cache entries written before the trim are trimmed on read
    path = gc._path("old")
    path.write_text(json.dumps({"q": "old", "results": [
        {"name": "Sodus Point, Wayne County, New York, United States", "lat": 43.27, "lon": -76.99, "type": "village"}]}))
    assert gc.search("old")[0]["name"] == "Sodus Point, Wayne County"


def test_geocode_errors(tmp_path):
    def fail(q):
        raise OSError("network down")

    gc = Geocoder(tmp_path, fetch=fail, sleep=lambda s: None)
    with pytest.raises(ApiError) as e:
        gc.search("Albany")
    assert e.value.status == 502
    with pytest.raises(ApiError):
        gc.search("   ")
    with pytest.raises(ApiError):
        gc.search("x" * 300)
    assert not list((tmp_path / "geocode").glob("*.json"))  # failures are not cached


def test_nominatim_request_identifies_the_project_only(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"[]"

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.header_items())
        return Resp()

    monkeypatch.setattr(web.urllib.request, "urlopen", fake_urlopen)
    assert web._nominatim_fetch("seneca lake") == []
    assert seen["url"].startswith("https://nominatim.openstreetmap.org/search?")
    for part in ("countrycodes=us", "bounded=1", "viewbox=", "q=seneca+lake"):
        assert part in seen["url"]
    assert seen["headers"] == {"User-agent": "maps-for-wanderers/0.1 (personal research project)",
                               "Accept": "application/json"}
    assert "@" not in seen["url"] and "email" not in seen["url"]


def test_geocode_endpoint(app):
    st, out = app.api("POST", "/api/geocode", {"q": "toy town"})
    assert st == 200 and out["results"][0]["name"] == "Toy Town, New York"
    assert app.api("POST", "/api/geocode", {"q": 5})[0] == 400


# ---- HTTP server -------------------------------------------------------------------


@pytest.fixture
def live(app):
    srv = make_server(app, port=0)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def request(srv, method, path, body=None, headers=None):
    host, port = srv.server_address[:2]
    c = http.client.HTTPConnection(host, port, timeout=10)
    h = {"Host": f"127.0.0.1:{port}"}
    if body is not None:
        h["Content-Type"] = "application/json"
        body = json.dumps(body)
    h.update(headers or {})
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, dict(r.getheaders()), data


def test_server_binds_to_loopback_only(live):
    assert live.server_address[0] == "127.0.0.1"
    assert web.HOST == "127.0.0.1"
    import inspect

    assert "host" not in inspect.signature(make_server).parameters
    assert "host" not in inspect.signature(web.serve).parameters


def test_http_round_trip(live):
    st, _, data = request(live, "GET", "/api/health")
    assert st == 200 and json.loads(data)["status"] == "ready"
    st, hdr, data = request(live, "POST", "/api/route", {"from": A, "to": B})
    assert st == 200 and hdr["Content-Type"] == "application/json"
    assert len(json.loads(data)["routes"]) == 2
    st, hdr, data = request(live, "GET", "/")
    assert st == 200 and b"static/vendor/leaflet-1.9.4/leaflet.js" in data and b'integrity="sha256-' in data
    assert b"unpkg" not in data and b"Maps for Wanderers" in data
    assert "Content-Security-Policy" in hdr and "unpkg" not in hdr["Content-Security-Policy"]
    st, _, data = request(live, "GET", "/static/vendor/leaflet-1.9.4/images/layers.png")
    assert st == 200 and data[:4] == b"\x89PNG"
    st, _, data = request(live, "GET", "/static/app.js")
    assert st == 200 and b"/api/route" in data


def test_http_refuses_foreign_hosts_forms_and_traversal(live):
    assert request(live, "GET", "/api/health", headers={"Host": "evil.example:8765"})[0] == 403
    st, _, _ = request(live, "POST", "/api/route", headers={"Content-Type": "application/x-www-form-urlencoded"},
                       body=None)
    assert st == 415
    assert request(live, "GET", "/static/../server.py")[0] == 404
    assert request(live, "GET", "/static/%2e%2e/server.py")[0] == 404
    st, _, data = request(live, "POST", "/api/route", {"from": [0, 0], "to": B})
    assert st == 422 and b"outside New York" in data


def test_large_responses_are_gzipped(live):
    import gzip

    st, hdr, data = request(live, "POST", "/api/route", {"from": A, "to": B}, headers={"Accept-Encoding": "gzip"})
    assert st == 200
    if hdr.get("Content-Encoding") == "gzip":
        data = gzip.decompress(data)
    assert json.loads(data)["routes"]


def test_page_never_hands_leaflet_html_strings():
    """OSM and Nominatim text (moment labels, road and place names) must reach
    the page through textContent or DOM nodes. Leaflet parses a string given to
    bindTooltip, bindPopup, setContent or divIcon({html}) as HTML, so none of
    those may be called with a string or template literal, and nothing may
    write innerHTML. The headless check in docs/WEB.md covers the rendered page."""
    import re
    from pathlib import Path

    js = (Path(web.STATIC_DIR) / "app.js").read_text()
    assert not re.search(r"\b(innerHTML|outerHTML|insertAdjacentHTML|document\.write)\b", js)
    assert not re.search(r"\b(bindTooltip|bindPopup|setContent)\(\s*[`'\"]", js)
    assert not re.search(r"\b(bindTooltip|bindPopup|setContent)\(\s*[\w.]+\s*\+", js)
    assert not re.search(r"\bhtml:\s*[`'\"]", js)
    # every call site passes a node-building helper
    for m in re.finditer(r"\b(bindTooltip|bindPopup|setContent)\(\s*([\w.]+)", js):
        assert m.group(2) in {"tip", "textNode", "momentPopup"}, m.group(0)


def test_csp_allows_the_three_base_layers_and_nothing_else():
    js = (web.STATIC_DIR / "app.js").read_text()
    for host in ("tile.openstreetmap.org", "basemap.nationalmap.gov"):
        assert host in web.CSP
    assert "USGSTopo" in js and "USGSImageryOnly" in js
    assert "arcgisonline" not in js + web.CSP and "unpkg" not in web.CSP
    assert "script-src 'self';" in web.CSP and "style-src 'self';" in web.CSP
    # every https host the page loads images from is in img-src
    for host in set(re.findall(r"L\.tileLayer\(\s*\"https://([^/]+)/", js)):
        assert "https://" + host in web.CSP


def test_self_hosted_leaflet_matches_its_integrity_hashes():
    import base64
    import hashlib

    html = (web.STATIC_DIR / "index.html").read_text()
    refs = re.findall(r'(?:src|href)="(static/vendor/[^"]+)"\s+integrity="sha256-([^"]+)"', html)
    assert len(refs) == 2
    for path, want in refs:
        got = base64.b64encode(hashlib.sha256((web.STATIC_DIR.parent / path).read_bytes()).digest()).decode()
        assert got == want, path
    assets = re.findall(r'<(?:script|link)[^>]+(?:src|href)="([^"]+)"', html)
    assert assets and not [a for a in assets if re.match(r"(?:https?:)?//", a)]  # no CDN: every asset is ours


@pytest.mark.parametrize("forward", [True, False])
def test_moment_minutes_off_midpoint_on_a_long_edge(forward):
    """A stop 80% along a 10-minute edge is at minute 8 driving one way and
    minute 2 the other, not at the edge's midpoint."""
    from types import SimpleNamespace

    lon, lat = [-77.0, -76.9], [43.0, 43.0]
    g = RoadGraph(lon, lat, [0], [1], [False], [600.0], [8150.0])
    mlon, mlat = [-77.0 + 0.8 * 0.1], [43.0]
    mx, my = _to5070(mlon, mlat)
    mom = Moments(id=np.array([0]), m=np.array([1.0]), x=mx, y=my, lon=np.array(mlon), lat=np.array(mlat),
                  edge=np.array([0]), type=np.array(["water"], dtype=object),
                  label=np.array(["Toy Lake"], dtype=object), of_edge=np.array([0]),
                  feature=np.array([None], dtype=object))
    ctx = Context(g=g, S=np.array([0.5]), moments=mom, cfg=CFG)
    r = g.route(0, 1) if forward else g.route(1, 0)
    got = web.moment_minutes(ctx, SimpleNamespace(edges=r.edges, route=r))
    assert got[0] == pytest.approx(8.0 if forward else 2.0, abs=0.1)


def test_moment_minutes_out_and_back_uses_the_first_pass():
    from types import SimpleNamespace

    from scenic.route import Route

    lon, lat = [-77.0, -76.9], [43.0, 43.0]
    g = RoadGraph(lon, lat, [0], [1], [False], [600.0], [8150.0])
    mlon, mlat = [-77.0 + 0.04 * 0.1], [43.0]
    mx, my = _to5070(mlon, mlat)
    mom = Moments(id=np.array([0]), m=np.array([1.0]), x=mx, y=my, lon=np.array(mlon), lat=np.array(mlat),
                  edge=np.array([0]), type=np.array(["water"], dtype=object),
                  label=np.array(["Toy Lake"], dtype=object), of_edge=np.array([0]),
                  feature=np.array([None], dtype=object))
    ctx = Context(g=g, S=np.array([0.5]), moments=mom, cfg=CFG)
    out, back = g.route(0, 1), g.route(1, 0)
    r = Route(nodes=np.array([0, 1, 0]), edges=np.array([0, 0]), reversed=np.array([False, True]),
              time_s=1200.0, length_m=16300.0, cost=1200.0, geometry=out.geometry + back.geometry[1:])
    got = web.moment_minutes(ctx, SimpleNamespace(edges=r.edges, route=r))
    assert got[0] == pytest.approx(0.4, abs=0.1)


# ---- unit-neutral errors and stop groups -------------------------------------------


def test_snap_error_sends_numbers_not_a_unit(app):
    st, out = app.api("POST", "/api/route", {"from": A, "to": [44.5, -74.0]})
    assert st == 422 and out["code"] == "snap_far" and out["point"] == "to"
    assert isinstance(out["snap_m"], int) and out["snap_m"] > out["limit_m"] == 2000
    assert not re.search(r"\d\s*(km|mi|m)\b", out["error"])  # no distance in the text
    js = (web.STATIC_DIR / "app.js").read_text()
    # the page builds the words from the numbers, in its own units
    assert 'd.code === "snap_far"' in js and "dist(d.snap_m / 1000" in js and "dist(d.limit_m / 1000" in js


@pytest.mark.parametrize("features, groups", [
    ([], []),
    (["Lake Ontario"], [(0, 0, "Lake Ontario")]),
    (["Lake Ontario"] * 3 + [None, "Lake Ontario"],
     [(0, 2, "Lake Ontario"), (3, 3, None), (4, 4, "Lake Ontario")]),  # only consecutive stops merge
    ([None, None], [(0, 0, None), (1, 1, None)]),  # unnamed stops never merge
    (["Genesee River", "Lake Ontario", "Lake Ontario"], [(0, 0, "Genesee River"), (1, 2, "Lake Ontario")]),
])
def test_stop_groups(features, groups):
    got = web.stop_groups([{"feature": f} for f in features])
    assert [(g["first"], g["last"], g["feature"]) for g in got] == groups


def test_stop_groups_merge_bays_under_their_parent_body():
    stops = [{"feature": "Lake George", "parent_feature": "Lake George"},
             {"feature": "Boon Bay", "parent_feature": "Lake George"},
             {"feature": "Huddle Bay", "parent_feature": "Lake George"},
             {"feature": "Hudson River", "parent_feature": "Hudson River"}]
    got = web.stop_groups(stops)
    assert [(g["first"], g["last"], g["feature"]) for g in got] == [(0, 2, "Lake George"), (3, 3, "Hudson River")]


def test_route_payload_carries_features_and_stop_groups():
    app = App(ctx=ladder(two_moments=True), geocoder=FakeGeocoder())
    _, out = app.api("POST", "/api/route", {"from": A, "to": B})
    top = next(r for r in out["routes"] if "+50%" in r["labels"])
    assert [m["feature"] for m in top["moments"]] == [None, "Toy Lake"]
    assert [m["parent_feature"] for m in top["moments"]] == [None, "Toy Lake"]
    mins = [m["min_along"] for m in top["moments"]]
    assert None not in mins and mins == sorted(mins)
    assert top["stop_groups"] == [{"first": 0, "last": 0, "feature": None},
                                  {"first": 1, "last": 1, "feature": "Toy Lake"}]
    js = (web.STATIC_DIR / "app.js").read_text()
    assert "stopGroups(r)" in js and "r.stop_groups" in js


# ---- static demo -------------------------------------------------------------------

DEMO_TOML = """
[[preset]]
id = "toy"
name = "Toy ladder"
from = [43.0, -77.0]
from_label = "West end"
to = [43.0, -76.88]
to_label = "East end"

[[preset]]
id = "toy-back"
name = "Toy ladder, back"
from = [43.0, -76.88]
to = [43.0, -77.0]
"""


@pytest.fixture
def demo_site(tmp_path):
    from scenic.web.demo import build_site

    presets = tmp_path / "demo_routes.toml"
    presets.write_text(DEMO_TOML)
    out = tmp_path / "site"
    build_site(out=out, presets_path=presets, ctx=ladder(two_moments=True), log=lambda s: None)
    return out


def test_demo_writes_valid_json_for_every_preset(demo_site):
    meta = json.loads((demo_site / "data" / "presets.json").read_text())
    assert [p["id"] for p in meta["presets"]] == ["toy", "toy-back"]
    assert meta["title"] == "Maps for Wanderers" and len(meta["s_breaks"]) == 4
    for p in meta["presets"]:
        assert not p["file"].startswith("/") and (demo_site / p["file"]).is_file()
        d = json.loads((demo_site / p["file"]).read_text())  # strict JSON: no NaN
        assert d["preset"]["id"] == p["id"] and d["search"]["id"] == p["id"]
        routes = d["search"]["routes"]
        assert routes and set(d["waypoints"]) == {r["key"] for r in routes}
        for r in routes:
            assert "points_raw" not in r and "stop_groups" in r
            assert all(round(x, 5) == x for pt in r["line"] for x in pt)
            assert len(r["line"]) <= 1500
            assert d["waypoints"][r["key"]]["url"].startswith("https://www.google.com/maps/dir/?api=1&")
        assert "seconds" not in d["search"]


def test_demo_html_uses_relative_self_hosted_assets_only(demo_site):
    html = (demo_site / "index.html").read_text()
    assert '<html lang="en" data-mode="demo">' in html
    assert 'http-equiv="Content-Security-Policy"' in html and "frame-ancestors" not in html
    assert "Preset demo routes. The interactive planner runs locally" in html
    refs = re.findall(r'<(?:script|link)[^>]+(?:src|href)="([^"]+)"', html)
    assert refs and all(r.startswith("static/") or r.startswith("data:") for r in refs), refs
    for r in refs:
        if r.startswith("static/"):
            assert (demo_site / r).is_file(), r
    # Leaflet's CSS images resolve inside the site too
    css = (demo_site / "static/vendor/leaflet-1.9.4/leaflet.css").read_text()
    for img in set(re.findall(r"url\((images/[^)]+)\)", css)):
        assert (demo_site / "static/vendor/leaflet-1.9.4" / img).is_file(), img
    # no server code, no absolute-path assets, nothing from outside but the CSP's tile hosts
    assert not list(demo_site.rglob("*.py"))
    assert not re.search(r'(?:src|href)="/', html)


def test_demo_mode_makes_no_api_calls():
    """The page's only network calls are `api()` (live mode, /api/*) and
    `demoFetch()` (demo mode, relative data/ paths). In demo mode `api()`
    throws before fetching, and nothing that calls it is wired up."""
    js = (web.STATIC_DIR / "app.js").read_text()
    assert len(re.findall(r"\bfetch\(", js)) == 2
    assert re.search(r"async function api\(path, body\) \{\s*if \(DEMO\) throw", js)
    assert re.search(r"async function demoFetch\(file\) \{[^}]*\bfetch\(file", js)
    for m in re.finditer(r"demoFetch\(([^)]*)\)", js):
        assert m.group(1) in ("file", '"data/presets.json"', "p.file"), m.group(0)
    assert not re.search(r"XMLHttpRequest|sendBeacon|WebSocket|EventSource", js)
    # the live wiring (search, Find routes, health polling) sits in the non-demo branch only
    demo_branch = js[js.index("if (DEMO) {\n    startDemo();"):]
    live = demo_branch[demo_branch.index("} else {"):]
    for call in ("pollHealth();", 'wireSearch("a");', 'addEventListener("click", findRoutes)'):
        assert js.count(call) == 1 and call in live, call
    assert "if (!s || DEMO) return;" in js  # getWaypoints


def test_demo_refuses_to_clear_a_folder_it_did_not_make(tmp_path):
    from scenic.web.demo import DemoError, build_site

    presets = tmp_path / "p.toml"
    presets.write_text(DEMO_TOML)
    out = tmp_path / "mine"
    out.mkdir()
    (out / "notes.txt").write_text("keep me")
    with pytest.raises(DemoError):
        build_site(out=out, presets_path=presets, ctx=ladder(), log=lambda s: None)
    assert (out / "notes.txt").read_text() == "keep me"
    # a folder it made is rebuilt in place
    out2 = tmp_path / "site"
    build_site(out=out2, presets_path=presets, ctx=ladder(), log=lambda s: None)
    (out2 / "data" / "stale.json").write_text("{}")
    build_site(out=out2, presets_path=presets, ctx=ladder(), log=lambda s: None)
    assert not (out2 / "data" / "stale.json").exists()


def test_demo_presets_file_is_valid():
    from scenic.web.demo import load_loop_preset, load_presets

    ps = load_presets()
    assert len(ps) == 5 and len({p["id"] for p in ps}) == 5
    s, w, n, e = web.NY_BBOX
    for p in ps:
        for lat, lon in (p["from"], p["to"]):
            assert s <= lat <= n and w <= lon <= e

    lp = load_loop_preset()
    assert lp is not None and lp["id"] == "auburn" and "Auburn" in lp["name"]
    assert lp["budgets"] == [60, 120, 180] and lp["default_min"] == 120
    assert lp["directions"] == ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    slat, slon = lp["start"]
    assert s <= slat <= n and w <= slon <= e



def _js_function(js: str, head: str) -> str:
    """The body of the app.js function starting with `head` (to its closing
    brace at the same indent)."""
    i = js.index(head)
    indent = js[js.rindex("\n", 0, i) + 1:i]
    return js[i:js.index("\n" + indent + "}", i)]


def test_preset_is_committed_only_after_its_data_loads():
    js = (web.STATIC_DIR / "app.js").read_text()
    body = _js_function(js, "async function loadPreset(")
    fetched = body.index("await demoFetch(")
    assert body.index("state.preset = id") > fetched and body.index("renderPresets()") > fetched
    assert body.index("writeHash()") > body.index("state.preset = id")


def test_changed_points_rewrite_the_link():
    js = (web.STATIC_DIR / "app.js").read_text()
    body = _js_function(js, "function onPointsChanged(")
    assert "clearResults()" in body and "writeHash()" in body
    # Swap ends in onPointsChanged, so its old #from=…&to=…&route=… goes too
    swap = js[js.index('$("swap").addEventListener'):]
    swap = swap[:swap.index("});")]
    assert swap.rstrip().endswith("onPointsChanged();")


def test_app_js_has_no_unused_source_url():
    assert "SOURCE_URL" not in (web.STATIC_DIR / "app.js").read_text()


# ---- page design: self-hosted Spectral (docs/WEB.md, "Design") ----------------------

def test_vendored_fonts_referenced_by_app_css_exist():
    css = (web.STATIC_DIR / "app.css").read_text()
    urls = re.findall(r'url\(\s*"?([^")]+)"?\s*\)', css)
    fonts = [u for u in urls if u.endswith(".woff2")]
    assert len(fonts) == 6 and len(set(fonts)) == 6
    for u in urls:
        assert not re.match(r"(?:https?:|data:)?//", u), u  # self-hosted only (CSP default-src 'self')
        f = web.STATIC_DIR / u
        assert f.is_file(), u
        if u.endswith(".woff2"):
            assert f.read_bytes()[:4] == b"wOF2", u
    assert css.count("font-display: swap") == 6
    assert (web.STATIC_DIR / "vendor/spectral/OFL.txt").is_file()
    html = (web.STATIC_DIR / "index.html").read_text()
    assert "fonts.googleapis" not in html + css and "fonts.gstatic" not in html + css
    for pre in re.findall(r'<link rel="preload" href="([^"]+)"', html):
        assert (web.STATIC_DIR.parent / pre).is_file(), pre
    assert web._TYPES[".woff2"] == "font/woff2"


def test_page_carries_the_site_nav_and_no_email():
    html = (web.STATIC_DIR / "index.html").read_text()
    assert '<a class="mark" href="https://tvh0021.github.io/">Trung Ha</a>' in html
    assert re.search(r'href="https://tvh0021.github.io/design/" aria-current="true">Projects<', html)
    assert "mailto:" not in html and not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", html)


def test_demo_html_still_finds_its_markers():
    from scenic.web.demo import demo_html

    out = demo_html((web.STATIC_DIR / "index.html").read_text())
    assert out.count('<html lang="en" data-mode="demo">') == 1
    assert out.count('http-equiv="Content-Security-Policy"') == 1
    assert out.index('<meta charset="utf-8">') < out.index("Content-Security-Policy") < out.index("<title>")


def test_demo_site_carries_the_fonts(demo_site):
    css = (demo_site / "static/app.css").read_text()
    for u in re.findall(r'url\(\s*"?([^")]+)"?\s*\)', css):
        assert (demo_site / "static" / u).is_file(), u
    assert (demo_site / "static/vendor/spectral/OFL.txt").is_file()


# ---- Mode 2: /api/loop and /api/loop/features -------------------------------------


def _loop_grid_ctx(east_only: bool = False) -> Context:
    """9x9 (or 9x5 east-only) grid centered at (43.0, -77.0) with ~3.9 km edges
    (~215 s each) and named scenic features near H plus one far feature."""
    lat0, lon0 = 43.0, -77.0
    dlat, dlon, half = 0.035, 0.048, 4
    coords: dict[tuple[int, int], int] = {}
    lons: list[float] = []
    lats: list[float] = []
    j_range = range(0, half + 1) if east_only else range(-half, half + 1)
    for i in range(-half, half + 1):
        for j in j_range:
            coords[(i, j)] = len(lons)
            lats.append(lat0 + i * dlat)
            lons.append(lon0 + j * dlon)

    us, vs, lengths, times, scores = [], [], [], [], []
    edge_map: dict[tuple[tuple[int, int], tuple[int, int]], int] = {}

    def add_edge(a: tuple[int, int], b: tuple[int, int], s_val: float) -> None:
        if a not in coords or b not in coords:
            return
        u, v = coords[a], coords[b]
        dx = (lons[v] - lons[u]) * 111_320.0 * math.cos(math.radians(lat0))
        dy = (lats[v] - lats[u]) * 110_540.0
        dist = math.hypot(dx, dy)
        eid = len(us)
        us.append(u)
        vs.append(v)
        lengths.append(dist)
        times.append(dist / 18.0)
        scores.append(s_val)
        edge_map[(a, b)] = eid
        edge_map[(b, a)] = eid

    for i in range(-half, half + 1):
        for j in j_range:
            if (i + 1, j) in coords:
                ring = max(abs(i), abs(j))
                add_edge((i, j), (i + 1, j), 0.85 if ring >= 2 else 0.35)
            if (i, j + 1) in coords:
                ring = max(abs(i), abs(j))
                add_edge((i, j), (i, j + 1), 0.85 if ring >= 2 else 0.35)

    # Add a long eastern spur so "Far Peak" has ~40 min round trip (> 0.88 * 30 min).
    spur_prev = coords[(0, half)]
    for step in range(1, 5):
        nid = len(lons)
        lats.append(lat0)
        lons.append(lon0 + (half + step) * dlon)
        dist = dlon * 111_320.0 * math.cos(math.radians(lat0))
        us.append(spur_prev)
        vs.append(nid)
        lengths.append(dist)
        times.append(dist / 12.0)  # ~324 s each way -> ~55 min round trip from (0,0)
        scores.append(0.9)
        spur_prev = nid
    far_edge = len(us) - 1

    g = RoadGraph(lons, lats, us, vs, [False] * len(us), times, lengths)
    specs: list[tuple[int, str, str, float, str | None, str | None]] = []
    candidates = [
        ((2, 0), (2, 1), "water", "North Bay Overlook", 1.0, "North Bay", "North Lake"),
        ((3, 0), (3, 1), "water", "North Lake Shore", 0.95, "North Lake", "North Lake"),
        ((0, 2), (1, 2), "water", "East Creek Bend", 0.9, "East Creek", "East Creek"),
        ((-2, 0), (-2, 1), "vista", "South Ridge Crest", 0.88, "South Ridge", None),
        ((0, -2), (1, -2), "gorge", "West Gorge Rim", 0.92, "West Gorge", None),
    ]
    for a, b, mtype, label, mval, feat, grp in candidates:
        if (a, b) in edge_map:
            specs.append((edge_map[(a, b)], mtype, label, mval, feat, grp))
    specs.append((far_edge, "peak", "Far Peak Summit", 0.95, "Far Peak", None))

    edges_arr = np.array([s[0] for s in specs], dtype=np.int64)
    mlon = 0.5 * (g.node_lon[g.u[edges_arr]] + g.node_lon[g.v[edges_arr]])
    mlat = 0.5 * (g.node_lat[g.u[edges_arr]] + g.node_lat[g.v[edges_arr]])
    mx, my = _to5070(mlon, mlat)
    of_edge = np.full(g.n_edges, -1, dtype=np.int64)
    for mid, e in enumerate(edges_arr):
        of_edge[e] = mid
    mom = Moments(
        id=np.arange(len(specs), dtype=np.int64),
        m=np.array([s[3] for s in specs], dtype=np.float64),
        x=mx,
        y=my,
        lon=mlon,
        lat=mlat,
        edge=edges_arr,
        type=np.array([s[1] for s in specs], dtype=object),
        label=np.array([s[2] for s in specs], dtype=object),
        of_edge=of_edge,
        feature=np.array([s[4] for s in specs], dtype=object),
        group=np.array([s[5] for s in specs], dtype=object),
    )
    return Context(g=g, S=np.asarray(scores, dtype=np.float64), moments=mom, cfg=dict(CFG))


@pytest.fixture
def loop_app():
    return App(ctx=_loop_grid_ctx(), geocoder=FakeGeocoder())


def test_loop_payload_roles_labels_and_compact_json(loop_app):
    st, out = loop_app.api("POST", "/api/loop", {"start": [43.0, -77.0], "minutes": 60, "steer": "any"})
    assert st == 200, out
    assert out["mode"] == "loop" and out["minutes"] == 60
    assert out["start"] == [43.0, -77.0] and len(out["snap_m"]) == 1
    assert out["steer"] == {"mode": "any"}
    assert out["s_base"] == pytest.approx(0.5)
    assert out["kept"] >= len(out["routes"]) >= 1
    assert out["built"] >= out["kept"]
    assert len(out["s_breaks"]) == 4 and out["id"]

    roles = [r["role"] for r in out["routes"]]
    assert roles == list(range(1, len(out["routes"]) + 1)) and 0 not in roles
    labels = [r["labels"] for r in out["routes"]]
    assert all(len(lab) == 1 and isinstance(lab[0], str) for lab in labels)
    assert len({lab[0] for lab in labels}) == len(labels)

    req_keys = {
        "key", "labels", "role", "origin", "minutes", "ratio", "km", "scenic_minutes",
        "mean_S", "mean_S_pct", "moment_points", "score", "roads", "moments",
        "stop_groups", "moments_passed", "line", "runs", "points_raw",
        "direction", "bearing", "off_deg", "stem_km", "tier",
    }
    for r in out["routes"]:
        assert req_keys <= set(r)
        assert r["line"][0] == r["line"][-1] == [43.0, -77.0]

    raw = json.dumps(out, allow_nan=False)
    assert "edge_path" not in raw

    # Compass and shore-anchor shapes
    st_c, out_c = loop_app.api("POST", "/api/loop", {"start": [43.0, -77.0], "minutes": 60,
                                                     "steer": "compass", "direction": "N"})
    assert st_c == 200 and out_c["steer"] == {"mode": "compass", "direction": "N", "bearing": 0}
    assert all(r["off_deg"] is not None for r in out_c["routes"])

    # Pin > 2 km north of the grid (lat 43.18 is ~4.4 km north of i=4 at 43.14) resolves to North Lake shore
    st_s, out_s = loop_app.api("POST", "/api/loop", {"start": [43.0, -77.0], "minutes": 60,
                                                     "steer": "anchor", "anchor": [43.18, -76.97]})
    assert st_s == 200, out_s
    assert out_s["steer"]["mode"] == "anchor" and out_s["steer"]["kind"] == "shore"
    assert out_s["steer"]["name"] == "North Lake" and out_s["steer"]["moved_m"] > 2000


def test_loop_errors_cover_validation_and_domain_codes(loop_app):
    # 400 validation errors
    assert loop_app.api("POST", "/api/loop", [])[0] == 400
    assert loop_app.api("POST", "/api/loop", {"minutes": 60})[0] == 400
    assert loop_app.api("POST", "/api/loop", {"start": "bad", "minutes": 60})[0] == 400
    for bad_min in (None, True, "abc", 15, 500):
        st, err = loop_app.api("POST", "/api/loop", {"start": [43.0, -77.0], "minutes": bad_min})
        assert st == 400 and err["code"] == "bad_minutes" and err["min"] == 30 and err["max"] == 360
    assert loop_app.api("POST", "/api/loop", {"start": [43.0, -77.0], "minutes": 60, "steer": "up"})[0] == 400
    assert loop_app.api("POST", "/api/loop", {"start": [43.0, -77.0], "minutes": 60, "steer": "compass"})[0] == 400
    assert loop_app.api("POST", "/api/loop", {"start": [43.0, -77.0], "minutes": 60,
                                              "steer": "compass", "direction": "NNE"})[0] == 400
    assert loop_app.api("POST", "/api/loop", {"start": [43.0, -77.0], "minutes": 60, "steer": "anchor"})[0] == 400
    assert loop_app.api("POST", "/api/loop", {"start": [43.0, -77.0], "minutes": 60, "steer": "anchor",
                                              "anchor": [43.05, -77.0], "feature": "North Lake"})[0] == 400
    assert loop_app.api("POST", "/api/loop", {"start": [43.0, -77.0], "minutes": 60,
                                              "steer": "anchor", "feature": 123})[0] == 400

    # 422 outside New York bbox names the pin
    st, err = loop_app.api("POST", "/api/loop", {"start": [40.7, -80.6], "minutes": 60})
    assert st == 422 and err["point"] == "start" and "the start pin" in err["error"]
    st, err = loop_app.api("POST", "/api/loop", {"start": [43.0, -77.0], "minutes": 60,
                                                 "steer": "anchor", "anchor": [40.7, -80.6]})
    assert st == 422 and err["point"] == "anchor" and "the anchor pin" in err["error"]

    # 422 snap_far on start
    st, err = loop_app.api("POST", "/api/loop", {"start": [44.5, -74.0], "minutes": 60})
    assert st == 422 and err["code"] == "snap_far" and err["point"] == "start"
    assert err["snap_m"] > err["limit_m"] == 2000

    # 422 unknown_feature, anchor_far, anchor_out_of_reach (all point == "anchor")
    st, err = loop_app.api("POST", "/api/loop", {"start": [43.0, -77.0], "minutes": 60,
                                                 "steer": "anchor", "feature": "Atlantis"})
    assert st == 422 and err["code"] == "unknown_feature" and err["point"] == "anchor"

    st, err = loop_app.api("POST", "/api/loop", {"start": [43.0, -77.0], "minutes": 60,
                                                 "steer": "anchor", "anchor": [44.5, -74.0]})
    assert st == 422 and err["code"] == "anchor_far" and err["point"] == "anchor"

    st, err = loop_app.api("POST", "/api/loop", {"start": [43.0, -77.0], "minutes": 30,
                                                 "steer": "anchor", "feature": "Far Peak"})
    assert st == 422 and err["code"] == "anchor_out_of_reach" and err["point"] == "anchor"
    assert err["round_trip_min"] > 30 and err["needed_min"] > 30

    # 422 no_loop_that_way on an east-only graph when asking for W
    east_app = App(ctx=_loop_grid_ctx(east_only=True), geocoder=FakeGeocoder())
    st, err = east_app.api("POST", "/api/loop", {"start": [43.0, -77.0], "minutes": 60,
                                                 "steer": "compass", "direction": "W"})
    assert st == 422 and err["code"] == "no_loop_that_way" and "point" not in err

    # 422 no_loop on a tiny single-edge graph with no ring
    tiny_g = RoadGraph([-77.0, -76.99], [43.0, 43.0], [0], [1], [False], [60.0], [800.0])
    empty = np.zeros(0, dtype=np.float64)
    empty_i = np.zeros(0, dtype=np.int64)
    empty_o = np.zeros(0, dtype=object)
    tiny_mom = Moments(id=empty_i, m=empty, x=empty, y=empty, lon=empty, lat=empty,
                       edge=empty_i, type=empty_o, label=empty_o,
                       of_edge=np.array([-1], dtype=np.int64), feature=empty_o, group=empty_o)
    tiny_app = App(ctx=Context(g=tiny_g, S=np.array([0.5]), moments=tiny_mom, cfg=dict(CFG)),
                   geocoder=FakeGeocoder())
    st, err = tiny_app.api("POST", "/api/loop", {"start": [43.0, -77.0], "minutes": 60})
    assert st == 422 and err["code"] == "no_loop"

    # 503 while loading
    loading = App(loader=lambda: _loop_grid_ctx(), geocoder=FakeGeocoder())
    assert loading.api("POST", "/api/loop", {"start": [43.0, -77.0], "minutes": 60})[0] == 503


def test_loop_features_endpoint(loop_app):
    st, out = loop_app.api("POST", "/api/loop/features", {"start": [43.0, -77.0], "minutes": 30})
    assert st == 200, out
    names = [f["name"] for f in out["features"]]
    assert "North Lake" in names and "Far Peak" not in names
    assert all(f["fits"] is True for f in out["features"])

    # Query match surfaces out-of-reach features with fits=False
    st, q_out = loop_app.api("POST", "/api/loop/features", {"start": [43.0, -77.0], "minutes": 30, "q": "far"})
    assert st == 200
    assert len(q_out["features"]) == 1
    fp = q_out["features"][0]
    assert fp["name"] == "Far Peak" and fp["fits"] is False and fp["needed_min"] > 30

    # Limit cap and validation errors
    st, lim = loop_app.api("POST", "/api/loop/features", {"start": [43.0, -77.0], "minutes": 60, "limit": 1})
    assert st == 200 and len(lim["features"]) == 1
    assert loop_app.api("POST", "/api/loop/features", {"start": [43.0, -77.0], "minutes": 60, "q": 5})[0] == 400
    assert loop_app.api("POST", "/api/loop/features", {"start": [43.0, -77.0], "minutes": 60, "limit": 0})[0] == 400
    st, far = loop_app.api("POST", "/api/loop/features", {"start": [44.5, -74.0], "minutes": 60})
    assert st == 422 and far["code"] == "snap_far" and far["point"] == "start"


def test_waypoints_on_stored_loop_and_zero_waypoint_guard(loop_app):
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlparse

    _, out = loop_app.api("POST", "/api/loop", {"start": [43.0, -77.0], "minutes": 60, "steer": "any"})
    r0 = out["routes"][0]
    st, wp = loop_app.api("POST", "/api/waypoints", {"id": out["id"], "key": r0["key"]})
    assert st == 200, wp
    assert len(wp["waypoints"]) >= 1
    assert wp["origin"] == wp["destination"] == [43.0, -77.0]
    q = parse_qs(urlparse(wp["url"]).query)
    assert q["origin"] == q["destination"] and "waypoints" in q

    # Zero-waypoint safety guard when origin == destination
    guarded = App(
        ctx=_loop_grid_ctx(),
        geocoder=FakeGeocoder(),
        waypoints_fn=lambda *a, **k: SimpleNamespace(
            origin=(43.0, -77.0),
            destination=(43.0, -77.0),
            waypoints=(),
            url="https://www.google.com/maps/dir/?api=1&origin=43,77&destination=43,77&travelmode=driving",
            meets_target=False,
            follow_target=0.9,
            summary=lambda: {},
            notes=[],
        ),
    )
    _, out2 = guarded.api("POST", "/api/loop", {"start": [43.0, -77.0], "minutes": 60, "steer": "any"})
    st2, err2 = guarded.api("POST", "/api/waypoints", {"id": out2["id"], "key": "r0"})
    assert st2 == 500 and "Could not place waypoints for this loop" in err2["error"]


def test_health_loop_block_shared_lock_and_expiry(loop_app):
    h = loop_app.health()
    assert h["loop"] == {
        "budgets_min": [60, 120, 180],
        "default_min": 120,
        "min_minutes": 30,
        "max_minutes": 360,
        "directions": list(web.App.health.__globals__["__builtins__"] and ("N", "NE", "E", "SE", "S", "SW", "W", "NW")),
        "n_loops": 3,
    }

    # Route and loop requests serialize on the same search lock
    active, peak = [0], [0]
    from scenic.loop import loops as real_loops
    from scenic.search import search as real_search

    def slow_search(ctx, a, b):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        threading.Event().wait(0.04)
        try:
            return real_search(ctx, a, b)
        finally:
            active[0] -= 1

    def slow_loops(ctx, start, minutes, steer=None, cfg=None):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        threading.Event().wait(0.04)
        try:
            return real_loops(ctx, start, minutes, steer=steer, cfg=cfg)
        finally:
            active[0] -= 1

    loop_app._search = slow_search
    loop_app._loops = slow_loops
    t1 = threading.Thread(target=loop_app.api, args=("POST", "/api/route", {"from": [43.0, -77.0], "to": [43.0, -76.904]}))
    t2 = threading.Thread(target=loop_app.api, args=("POST", "/api/loop", {"start": [43.0, -77.0], "minutes": 60}))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert peak[0] == 1

    # Loop and route results share KEEP_RESULTS expiry
    loop_app._search = None
    loop_app._loops = None
    first_loop = loop_app.api("POST", "/api/loop", {"start": [43.0, -77.0], "minutes": 60})[1]["id"]
    for _ in range(web.KEEP_RESULTS):
        loop_app.api("POST", "/api/route", {"from": [43.0, -77.0], "to": [43.0, -76.904]})
    st_exp, err_exp = loop_app.api("POST", "/api/waypoints", {"id": first_loop, "key": "r0"})
    assert st_exp == 404 and "Search again." in err_exp["error"]


def test_loop_static_markup_demo_hiding_and_live_wiring():
    html = (web.STATIC_DIR / "index.html").read_text()
    css = (web.STATIC_DIR / "app.css").read_text()
    js = (web.STATIC_DIR / "app.js").read_text()

    tab_loop = re.search(r'<button[^>]*id="tab-loop"[^>]*>.*?</button>', html, re.S)
    assert tab_loop is not None
    tag = tab_loop.group(0)
    assert "disabled" not in tag and "soon" not in tag.lower() and "Drive-around" in tag
    assert 'aria-controls="panel-ab"' in html and 'aria-controls="panel-loop"' in html
    assert re.search(r'<section[^>]*id="panel-loop"[^>]*hidden', html)

    assert 'html[data-mode="demo"] #panel-loop' in css

    demo_branch = js[js.index("if (DEMO) {\n    startDemo();"):]
    live = demo_branch[demo_branch.index("} else {"):]
    for call in ('wireSearch("h");', "wireAnchorSearch();", 'addEventListener("click", findLoops)'):
        assert js.count(call) == 1 and call in live, call


def test_find_loops_js_sends_flat_request_matching_server_contract(loop_app):
    """Regression guard: app.js findLoops() must send steer as a flat string
    ('any' | 'compass' | 'anchor') with top-level direction/feature/anchor keys,
    matching App.loop()."""
    js = (web.STATIC_DIR / "app.js").read_text()
    body = _js_function(js, "async function findLoops(")
    assert "steer: state.loop.steer" in body
    assert "body.direction = state.loop.dir" in body
    assert "body.feature = state.loop.feature" in body
    assert "body.anchor = state.x" in body
    assert not re.search(r"body\.steer\s*=\s*\{", body)

    # Every payload shape constructed by findLoops succeeds against App.loop
    for req in (
        {"start": [43.0, -77.0], "minutes": 60, "steer": "any"},
        {"start": [43.0, -77.0], "minutes": 60, "steer": "compass", "direction": "N"},
        {"start": [43.0, -77.0], "minutes": 60, "steer": "anchor", "feature": "North Lake"},
        {"start": [43.0, -77.0], "minutes": 60, "steer": "anchor", "anchor": [43.07, -77.0]},
    ):
        st, out = loop_app.api("POST", "/api/loop", req)
        assert st == 200, (req, out)


def test_waypoints_splits_long_route_into_editable_parts_and_marks_handoff():
    """Routes/loops needing more waypoints than Google Maps keeps editable in one
    link (7 open, 8 closed) return multi-part links in `parts` with <= 7 waypoints
    each and `"split": True` on handoff waypoint(s)."""
    import math
    from urllib.parse import parse_qs, urlparse

    lat0, lon0 = 43.0, -77.5
    km_lat = 1 / 111.195
    km_lon = km_lat / math.cos(math.radians(lat0))

    pts = {"J0": (0.0, 0.0)}
    edges_spec = []
    for k in range(10):
        x0, x1 = 6.0 * k, 6.0 * (k + 1)
        n1, n2, j1 = f"N{k}", f"P{k}", f"J{k + 1}"
        pts[n1] = (x0 + 1.5, 1.2)
        pts[n2] = (x1 - 1.5, 1.2)
        pts[j1] = (x1, 0.0)
        edges_spec.append((f"J{k}", j1, 30.0, 0.05))
        edges_spec.extend([
            (f"J{k}", n1, 27.0, 0.90),
            (n1, n2, 27.0, 0.90),
            (n2, j1, 27.0, 0.90),
        ])

    names = list(pts)
    idx = {n: i for i, n in enumerate(names)}
    lon = [lon0 + pts[n][0] * km_lon for n in names]
    lat = [lat0 + pts[n][1] * km_lat for n in names]
    u = [idx[a] for a, _, _, _ in edges_spec]
    v = [idx[b] for _, b, _, _ in edges_spec]
    length = [1000 * math.dist(pts[a], pts[b]) for a, b, _, _ in edges_spec]
    time_s = [L / spd for L, (_, _, spd, _) in zip(length, edges_spec)]
    S = np.array([sc for *_, sc in edges_spec], dtype=np.float64)
    g = RoadGraph(lon, lat, u, v, [False] * len(edges_spec), time_s, length)
    mom = Moments(
        id=np.array([], dtype=np.int64), m=np.array([], dtype=np.float64),
        x=np.array([], dtype=np.float64), y=np.array([], dtype=np.float64),
        lon=np.array([], dtype=np.float64), lat=np.array([], dtype=np.float64),
        edge=np.array([], dtype=np.int64), type=np.array([], dtype=object),
        label=np.array([], dtype=object), of_edge=np.array([], dtype=np.int64),
        feature=np.array([], dtype=object),
    )
    chain_app = App(ctx=Context(g=g, S=S, moments=mom, cfg=CFG), geocoder=FakeGeocoder())
    a_pt = [lat[idx["J0"]], lon[idx["J0"]]]
    b_pt = [lat[idx["J10"]], lon[idx["J10"]]]

    st_r, out = chain_app.api("POST", "/api/route", {"from": a_pt, "to": b_pt})
    assert st_r == 200, out
    scenic_pick = out["routes"][-1]
    st_w, wp = chain_app.api("POST", "/api/waypoints", {"id": out["id"], "key": scenic_pick["key"]})
    assert st_w == 200, wp
    json.dumps(wp, allow_nan=False)

    assert len(wp["waypoints"]) >= 10
    assert len(wp["parts"]) >= 2
    assert "single" in wp and wp["single"]["url"] == wp["url"] and wp["single"]["waypoints"] <= 7
    assert wp["meets_target"] is True
    assert wp["summary"]["overlap"] >= 0.99

    split_ns = [w["n"] for w in wp["waypoints"] if w.get("split")]
    assert split_ns == [p["handoff_n"] for p in wp["parts"][:-1]]

    for k, p in enumerate(wp["parts"]):
        assert p["part"] == k + 1
        assert 1 <= p["waypoints"] <= 7
        assert p["url"].startswith("https://www.google.com/maps/dir/?api=1&")
        assert p["url_length"] == len(p["url"]) < 2048
        assert p["eta_min"] > 0 and p["km"] > 0
        q = parse_qs(urlparse(p["url"]).query)
        assert q["origin"] != q["destination"]
        assert len(q["waypoints"][0].split("|")) == p["waypoints"] <= 7
        if k + 1 < len(wp["parts"]):
            assert p["destination"] == wp["parts"][k + 1]["origin"]

    js = (web.STATIC_DIR / "app.js").read_text()
    mb = _js_function(js, "function mapsBlock(")
    dw = _js_function(js, "function drawWaypoints(")
    assert "x.parts" in mb and "Open Part " in mb and "dist(p.km)" in mb
    assert "p.split" in dw and "wp.split" not in dw


def test_demo_builds_loop_recommendations_with_directions_and_features(tmp_path):
    from scenic.web.demo import build_site

    cfg_toml = tmp_path / "demo_loop.toml"
    cfg_toml.write_text(
        """
[[preset]]
id = "grid-ab"
name = "Grid A to B"
from = [43.0, -77.0]
to = [43.0, -76.904]

[loop]
id = "grid-center"
name = "Grid Center"
start = [43.0, -77.0]
budgets = [45, 90]
default_min = 90
directions = ["N", "E", "W"]
max_features = 4
"""
    )
    out = tmp_path / "site"
    # East-only grid: W has no loops (422 skipped cleanly), Far Peak (~55m round trip) doesn't fit in 45m
    build_site(out=out, presets_path=cfg_toml, ctx=_loop_grid_ctx(east_only=True), log=lambda s: None)

    meta = json.loads((out / "data" / "presets.json").read_text())
    assert "loop" in meta
    lp = meta["loop"]
    assert lp["id"] == "grid-center" and lp["name"] == "Grid Center"
    assert lp["budgets"] == [45, 90] and lp["default_min"] == 90
    assert set(lp["by_min"]) == {"45", "90"}

    b45 = lp["by_min"]["45"]
    assert "W" not in b45["compass"] and "E" in b45["compass"]
    assert "Far Peak" not in {f["name"] for f in b45["features"]}
    assert {f["name"] for f in b45["features"]}  # fitting features included

    checked_files = set()
    for m_key, entry in lp["by_min"].items():
        rels = [entry["any"], *entry["compass"].values(), *(f["file"] for f in entry["features"])]
        for rel in rels:
            assert rel.startswith("data/loop-") and not rel.startswith("/")
            fpath = out / rel
            assert fpath.is_file(), rel
            d = json.loads(fpath.read_text())
            assert d["search"]["mode"] == "loop" and d["search"]["minutes"] == int(m_key)
            assert "seconds" not in d["search"]
            routes = d["search"]["routes"]
            assert routes and set(d["waypoints"]) == {r["key"] for r in routes}
            for r in routes:
                assert "points_raw" not in r
                assert r["line"][0] == r["line"][-1] == [43.0, -77.0]
                wp = d["waypoints"][r["key"]]
                assert wp["key"] == r["key"]
                assert wp["url"].startswith("https://www.google.com/maps/dir/?api=1&")
            checked_files.add(rel)
    assert len(checked_files) >= 6

    html = (out / "index.html").read_text()
    js = (out / "static" / "app.js").read_text()
    assert 'id="demo-loop-origin"' in html and 'id="mode-tabs"' in html
    assert "async function loadDemoLoop(" in js and "function syncDemoLoopControls(" in js


