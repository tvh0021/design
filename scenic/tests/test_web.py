import http.client
import json
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
    assert 1 <= len(wp["waypoints"]) <= 9
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
    from scenic.web.demo import load_presets

    ps = load_presets()
    assert len(ps) == 5 and len({p["id"] for p in ps}) == 5
    s, w, n, e = web.NY_BBOX
    for p in ps:
        for lat, lon in (p["from"], p["to"]):
            assert s <= lat <= n and w <= lon <= e


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
