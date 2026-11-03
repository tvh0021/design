"""Local map server for Maps for Wanderers, the scenic route planner
(docs/WEB.md). The same page also runs as a static demo with preset routes
(`scenic.web.demo`, docs/PUBLISH.md).

`scenic serve` binds to 127.0.0.1 only, loads the `Context` once in a
background thread and serves a single-page Leaflet app from `static/`.

API (JSON in and out; POST bodies must be application/json):

    GET  /api/health     {"status": "loading" | "ready" | "error", ...}
    POST /api/geocode    {"q": "Watkins Glen"} -> {"results": [{name, lat, lon, type}]}
    POST /api/route      {"from": [lat, lon], "to": [lat, lon]} -> routes, picks, moments
    POST /api/waypoints  {"id": search id, "key": route key} -> Google Maps link

Searches and waypoint reductions share one lock: `Context` and the
waypoint index cache are not known to be thread-safe.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

import numpy as np

HOST = "127.0.0.1"  # never anything else: the server has no authentication
DEFAULT_PORT = 8765
STATIC_DIR = Path(__file__).resolve().parent / "static"

# New York State, (south, west, north, east). Points outside are refused
# before snapping; the snap limit catches points inside the box but off the
# graph (Pennsylvania, Vermont, Ontario, open water).
NY_BBOX = (40.4961, -79.7624, 45.0159, -71.8562)
MAX_SNAP_M = 2000.0
MAX_POINTS = 5000  # per route, after simplification
BASE_TOL_DEG = 5e-6  # ~0.5 m: drops collinear points without visible change
KEEP_RESULTS = 8  # searches kept for /api/waypoints

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "maps-for-wanderers/0.1 (personal research project)"
GEOCODE_INTERVAL_S = 1.0  # Nominatim usage policy: at most 1 request per second
MAX_QUERY = 200

WAYPOINT_OPTS = {"max_waypoints": 9, "follow_target": 0.9}


class ApiError(Exception):
    """An error for the page. `point` ("from" or "to") names the pin the
    message is about, so the page can tint it."""

    def __init__(self, status: int, message: str, point: str | None = None, code: str | None = None,
                 **numbers):
        super().__init__(message)
        self.status = status
        self.message = message
        self.point = point
        self.code = code  # machine-readable kind, e.g. "snap_far"
        self.numbers = numbers  # raw values the page formats in its own units (snap_m, limit_m)

    def payload(self) -> dict:
        out = {"error": self.message}
        if self.point:
            out["point"] = self.point
        if self.code:
            out["code"] = self.code
        out.update(self.numbers)
        return out


# --------------------------------------------------------------------------
# Geocoding (Nominatim, rate limited, cached on disk)


def default_cache_dir() -> Path:
    return Path.home() / ".cache" / "scenic-web"


def _nominatim_fetch(query: str) -> list:
    """One Nominatim search. Sends the query, the NY box and the project's
    User-Agent; nothing that identifies a person."""
    s, w, n, e = NY_BBOX
    params = {"q": query, "format": "jsonv2", "countrycodes": "us", "limit": "5",
              "viewbox": f"{w},{n},{e},{s}", "bounded": "1"}
    req = urllib.request.Request(f"{NOMINATIM_URL}?{urllib.parse.urlencode(params)}",
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


_NAME_TAILS = (", New York, United States", ", United States")


def clean_place_name(name: str) -> str:
    """Drop the trailing ", New York, United States" (or ", United States")
    that Nominatim puts on every name inside the search box."""
    for tail in _NAME_TAILS:
        if name.endswith(tail) and len(name) > len(tail):
            return name[: -len(tail)]
    return name


class Geocoder:
    """Place search through Nominatim, at most one request per
    `min_interval` seconds across all threads, with answers cached on disk
    by normalised query. `fetch`, `clock` and `sleep` are injectable for
    tests."""

    def __init__(self, cache_dir: Path | None = None, fetch: Callable[[str], list] = _nominatim_fetch,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 min_interval: float = GEOCODE_INTERVAL_S):
        self.cache_dir = Path(cache_dir or default_cache_dir()) / "geocode"
        self.fetch = fetch
        self.clock = clock
        self.sleep = sleep
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last: float | None = None

    @staticmethod
    def normalise(q: str) -> str:
        return re.sub(r"\s+", " ", q).strip().lower()

    def _path(self, key: str) -> Path:
        return self.cache_dir / (hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] + ".json")

    def _read_cache(self, path: Path) -> list[dict] | None:
        """Cached results, or None (a miss) if the file is missing, not
        JSON, or not shaped like {"results": [{name, lat, lon, type}]}."""
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            return None
        out = []
        for r in results:
            if not (isinstance(r, dict) and isinstance(r.get("name"), str) and isinstance(r.get("type", ""), str)):
                return None
            lat, lon = r.get("lat"), r.get("lon")
            if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
                       for x in (lat, lon)):
                return None
            out.append({"name": clean_place_name(r["name"]), "lat": float(lat), "lon": float(lon),
                        "type": r.get("type", "")})
        return out

    def _write_cache(self, path: Path, key: str, results: list[dict]) -> None:
        """Atomic write through a unique temp file in the cache directory.
        Called with the lock held."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.cache_dir, prefix=path.stem + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"q": key, "results": results}, f)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def search(self, q: str) -> list[dict]:
        key = self.normalise(q)
        if not key:
            raise ApiError(400, "Type a place name, or click the map.")
        if len(key) > MAX_QUERY:
            raise ApiError(400, f"That place name is over {MAX_QUERY} characters. Try a shorter one.")
        path = self._path(key)
        hit = self._read_cache(path)
        if hit is not None:
            return hit
        with self._lock:
            hit = self._read_cache(path)  # another thread may have fetched it while we waited
            if hit is not None:
                return hit
            if self._last is not None:
                wait = self.min_interval - (self.clock() - self._last)
                if wait > 0:
                    self.sleep(wait)
            try:
                raw = self.fetch(key)
            except Exception as exc:  # network, HTTP or JSON errors
                raise ApiError(502, f"Place search failed ({exc}). Try again in a few seconds, "
                                    "or click the map instead.") from exc
            finally:
                self._last = self.clock()
            results = []
            for r in raw if isinstance(raw, list) else []:
                try:
                    results.append({"name": clean_place_name(str(r.get("display_name", ""))),
                                    "lat": round(float(r["lat"]), 5), "lon": round(float(r["lon"]), 5),
                                    "type": str(r.get("type", ""))})
                except (AttributeError, KeyError, TypeError, ValueError):
                    continue
            self._write_cache(path, key, results)
        return results


# --------------------------------------------------------------------------
# Route payloads


def _edge_point_counts(g, edges: np.ndarray) -> np.ndarray:
    """Points each edge contributes to `RoadGraph._geometry` (its own
    geometry, or its two nodes when the graph has none)."""
    if g.geom_off is None:
        return np.full(len(edges), 2, np.int64)
    off = np.asarray(g.geom_off)
    return (off[edges + 1] - off[edges]).astype(np.int64)


def s_breaks(g, S: np.ndarray, n: int = 5) -> list[float]:
    """Inner class breaks for colouring by S: length-weighted quantiles of
    S over the whole graph, so each class holds about 1/n of the road km."""
    S = np.asarray(S, dtype=np.float64)
    if len(S) == 0:
        return [round(k / n, 3) for k in range(1, n)]
    w = np.asarray(g.length_m, dtype=np.float64)
    order = np.argsort(S)
    cw = np.cumsum(w[order])
    cw /= cw[-1] if cw[-1] > 0 else 1.0
    out = []
    for k in range(1, n):
        out.append(round(float(S[order][min(np.searchsorted(cw, k / n), len(S) - 1)]), 3))
    return out


def s_grid(g, S: np.ndarray, n: int = 1000) -> np.ndarray:
    """S at every 1/n of the road length (length-weighted quantiles), for
    turning a route's mean S into "scenier than p % of NY road miles"."""
    S = np.asarray(S, dtype=np.float64)
    if len(S) == 0:
        return np.linspace(0.0, 1.0, n + 1)
    w = np.asarray(g.length_m, dtype=np.float64)
    order = np.argsort(S)
    cw = np.cumsum(w[order])
    cw /= cw[-1] if cw[-1] > 0 else 1.0
    idx = np.minimum(np.searchsorted(cw, np.linspace(0.0, 1.0, n + 1)), len(S) - 1)
    return S[order][idx]


def s_percentile(grid: np.ndarray | None, x: float) -> int | None:
    """Share of road length (0-100) with S below `x`."""
    if grid is None or len(grid) < 2:
        return None
    k = np.searchsorted(grid, x, side="left")  # grid points strictly below x
    return int(round(100 * min(max(k - 1, 0), len(grid) - 1) / (len(grid) - 1)))


def moment_minutes(ctx, sr) -> dict[int, float]:
    """Minutes from the start of the route to each moment it passes.

    `passed_moments` places a moment found through its member edge at that
    edge's midpoint, which is fine for scoring but can be minutes off on a
    long edge. For display, project each moment's point onto the route line
    instead and read the time there, interpolated within the edge driven."""
    from scipy.spatial import cKDTree

    from ..search import _densify, _to5070, passed_moments

    g, mom = ctx.g, ctx.moments
    e = np.asarray(sr.edges, dtype=np.int64)
    if len(e) == 0:
        return {}
    ids, _ = passed_moments(sr.route, g, mom, ctx.cfg["moment_radius_m"])
    geo = np.asarray(sr.route.geometry, dtype=np.float64).reshape(-1, 2)
    if len(ids) == 0 or len(geo) < 2:
        return {}
    x, y = _to5070(geo[:, 0], geo[:, 1])
    cs = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(x), np.diff(y)))])  # distance at each point
    cum_s = np.concatenate([[0.0], np.cumsum(g.time_s[e])])
    ends = np.cumsum(_edge_point_counts(g, e) - 1)  # last geometry point of each edge
    if ends[-1] == len(geo) - 1:
        knots = cs[np.concatenate([[0], ends])]  # distance at each edge boundary
    else:  # geometry does not follow the edge layout: spread time by distance
        knots = cum_s / cum_s[-1] * cs[-1] if cum_s[-1] > 0 else np.linspace(0, cs[-1], len(cum_s))
    px, py, along = _densify(x, y, 10.0)
    tree = cKDTree(np.column_stack([px, py]))
    q = np.column_stack([mom.x[ids], mom.y[ids]])
    d, _ = tree.query(q)
    out = {}
    for m, p, dm in zip(ids, q, d):
        # a route that passes the point twice (out and back) has two equally
        # near samples; the stop is reached at the first
        near = tree.query_ball_point(p, dm + 10.0)
        out[int(m)] = float(np.interp(along[near].min(), knots, cum_s)) / 60
    return out


def _simplify(pts: np.ndarray, tol: float) -> np.ndarray:
    if len(pts) <= 2 or tol <= 0:
        return pts
    from shapely.geometry import LineString

    return np.asarray(LineString(pts).simplify(tol, preserve_topology=False).coords)


def route_display(g, S: np.ndarray, sr, breaks: list[float], max_points: int = MAX_POINTS) -> dict:
    """Display geometry of one route: [[lat, lon], ...] with 5 decimals and
    `runs` [[first point, last point, S class], ...] over consecutive edges in
    the same class. Each run is simplified on its own, so run ends stay
    exact; the tolerance doubles from ~0.5 m until the route has at most
    `max_points` points."""
    edges = np.asarray(sr.edges, dtype=np.int64)
    geom = np.asarray(sr.route.geometry, dtype=np.float64).reshape(-1, 2)  # (lon, lat)
    if len(edges) == 0 or len(geom) == 0:
        return {"line": [], "runs": [], "points_raw": int(len(geom))}
    cls = np.searchsorted(np.asarray(breaks, dtype=np.float64), np.asarray(S, dtype=np.float64)[edges], side="right")
    cnt = _edge_point_counts(g, edges)
    # edge k spans geometry points [start[k], end[k]]
    end = np.cumsum(cnt - 1)
    start = end - (cnt - 1)
    if end[-1] != len(geom) - 1:  # geometry does not follow the edge layout; one run per route
        start, end = np.array([0]), np.array([len(geom) - 1])
        cls = np.array([int(np.round(np.median(cls)))])
    cut = np.flatnonzero(np.diff(cls) != 0) + 1
    groups = np.split(np.arange(len(cls)), cut)
    spans = [(int(start[gr[0]]), int(end[gr[-1]]), int(cls[gr[0]])) for gr in groups]

    tol = BASE_TOL_DEG
    while True:
        line: list[np.ndarray] = []
        runs = []
        n = 0
        for a, b, c in spans:
            seg = _simplify(geom[a:b + 1], tol)
            if line:
                seg = seg[1:]
                first = n - 1
            else:
                first = 0
            line.append(seg)
            n += len(seg)
            runs.append([first, n - 1, c])
        if n <= max_points or tol > 0.05:
            break
        tol *= 2
    pts = np.concatenate(line)
    latlon = [[round(float(y), 5), round(float(x), 5)] for x, y in pts]
    # merge runs that ended up with the same class next to each other
    merged: list[list[int]] = []
    for r in runs:
        if merged and merged[-1][2] == r[2] and merged[-1][1] == r[0]:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    return {"line": latlon, "runs": merged, "points_raw": int(len(geom)), "tolerance_deg": tol}


def stop_groups(moments: list[dict]) -> list[dict]:
    """Runs of consecutive stops (in drive order) at the same named feature,
    for the stop list: [{"first": i, "last": j, "feature": name or None}],
    covering every stop once. A stop with no feature is a run of its own.
    Bays group under their parent water body (`parent_feature`)."""
    groups: list[dict] = []
    for i, m in enumerate(moments):
        f = m.get("parent_feature") or m.get("feature")
        if f and groups and groups[-1]["feature"] == f and groups[-1]["last"] == i - 1:
            groups[-1]["last"] = i
        else:
            groups.append({"first": i, "last": i, "feature": f or None})
    return groups


def _label(budget: float) -> str:
    return f"+{round((budget - 1) * 100)}%"


def route_payload(ctx, res, breaks: list[float], grid: np.ndarray | None = None,
                  max_points: int = MAX_POINTS) -> tuple[dict, dict]:
    """The fastest route and the budget picks, one entry per distinct edge
    path (identical picks share an entry and list every label). `role` is
    the index of the entry's first label: 0 for the fastest route, k for
    the k-th budget. Also returns {key: ScenicRoute}."""
    from ..search import describe

    rows = [("fastest", res.fastest)] + [(_label(b), sr) for b, sr in sorted(res.picks.items())]
    by_key: dict[bytes, dict] = {}
    routes: list[dict] = []
    srs: dict = {}
    for role, (label, sr) in enumerate(rows):
        k = np.asarray(sr.edges, dtype=np.int64).tobytes()
        if k in by_key:
            by_key[k]["labels"].append(label)
            continue
        d = describe(ctx, sr)
        disp = route_display(ctx.g, ctx.S, sr, breaks, max_points=max_points)
        at = moment_minutes(ctx, sr) if d["moments"] else {}
        feat = getattr(ctx.moments, "feature", None)
        body = getattr(ctx.moments, "body", feat)
        moments = [{**m, "min_along": round(at[m["id"]], 1) if m["id"] in at else None,
                    "feature": (str(feat[m["id"]]) if feat is not None and feat[m["id"]] else None),
                    "parent_feature": (str(body[m["id"]]) if body is not None and body[m["id"]] else None)}
                   for m in d["moments"]]
        # drive order: projected minutes, stops without a time keep their place at the end
        moments.sort(key=lambda m: (m["min_along"] is None, m["min_along"] or 0.0))
        entry = {
            "key": f"r{len(routes)}", "labels": [label], "role": role, "origin": d["origin"],
            "minutes": d["minutes"], "ratio": d["ratio"], "km": d["km"],
            "scenic_minutes": d["scenic_minutes"], "mean_S": d["mean_S"],
            "mean_S_pct": s_percentile(grid, d["mean_S"]), "moment_points": d["moment_points"],
            "score": d["score"], "roads": [[lab, km] for lab, km in d["roads"]],
            "moments": moments, "stop_groups": stop_groups(moments), "moments_passed": d["moments_passed"],
            "line": disp["line"], "runs": disp["runs"], "points_raw": disp["points_raw"],
        }
        by_key[k] = entry
        routes.append(entry)
        srs[entry["key"]] = sr
    return {"routes": routes}, srs


# --------------------------------------------------------------------------
# Application state and handlers


class App:
    """State shared by the request threads: the Context (loaded once), the
    last few searches, waypoint results and the geocoder."""

    def __init__(self, ctx=None, loader: Callable[[], object] | None = None, geocoder: Geocoder | None = None,
                 search_fn=None, waypoints_fn=None, max_points: int = MAX_POINTS):
        self.ctx = ctx
        self.loader = loader
        self.geocoder = geocoder or Geocoder()
        self.status = "ready" if ctx is not None else ("loading" if loader else "error")
        self.error: str | None = None if (ctx is not None or loader) else "no context"
        self.load_s: float | None = None
        self.breaks: list[float] | None = s_breaks(ctx.g, ctx.S) if ctx is not None else None
        self.grid: np.ndarray | None = s_grid(ctx.g, ctx.S) if ctx is not None else None
        self.lock = threading.Lock()  # one search or waypoint reduction at a time
        self.results: OrderedDict[str, dict] = OrderedDict()
        self._rlock = threading.Lock()  # guards `results`
        self._search = search_fn
        self._waypoints = waypoints_fn
        self.max_points = max_points  # per route in the payload (the static demo uses fewer)

    # ---- loading -----------------------------------------------------------

    def load(self) -> None:
        t0 = time.monotonic()
        try:
            ctx = self.loader()
            breaks = s_breaks(ctx.g, ctx.S)
            grid = s_grid(ctx.g, ctx.S)
        except Exception as exc:  # reported through /api/health
            self.error = f"{type(exc).__name__}: {exc}"
            self.status = "error"
            return
        self.ctx, self.breaks, self.grid = ctx, breaks, grid
        self.load_s = round(time.monotonic() - t0, 1)
        self.status = "ready"
        self.warm_waypoints()

    def warm_waypoints(self) -> None:
        """Build the waypoint module's spatial index now (about 3 s on the
        statewide graph) instead of on the first "Open in Google Maps".
        `waypoints._index` is private; if it goes away the first link is
        just slower."""
        try:
            from ..waypoints import _index
        except ImportError:
            return
        with self.lock:
            try:
                _index(self.ctx.g)
            except Exception:  # never fatal: reduce_to_waypoints builds it on demand
                pass

    def start_loading(self) -> threading.Thread:
        th = threading.Thread(target=self.load, name="scenic-context-load", daemon=True)
        th.start()
        return th

    def _ready(self):
        if self.status == "loading":
            raise ApiError(503, "Still loading the road graph and scores. Try again in a moment.")
        if self.status != "ready":
            raise ApiError(503, f"The route data failed to load ({self.error}). If you run this server, "
                                "check the data folders and restart `scenic serve`.")
        return self.ctx

    # ---- handlers ------------------------------------------------------------

    def health(self) -> dict:
        out = {"status": self.status, "bbox": list(NY_BBOX), "max_snap_m": MAX_SNAP_M}
        if self.status == "ready":
            out.update({"edges": int(self.ctx.g.n_edges), "moments": int(len(self.ctx.moments.id)),
                        "s_breaks": self.breaks, "budgets": [_label(b) for b in sorted(self.ctx.cfg["budgets"])],
                        "load_s": self.load_s})
        if self.error:
            out["error"] = self.error
        return out

    def geocode(self, body: dict) -> dict:
        q = body.get("q") if isinstance(body, dict) else None
        if not isinstance(q, str):
            raise ApiError(400, "Expected {\"q\": \"place name\"}.")
        return {"results": self.geocoder.search(q)}

    @staticmethod
    def _point(body: dict, name: str) -> tuple[float, float]:
        p = body.get(name)
        try:
            lat, lon = (float(x) for x in p)
        except (TypeError, ValueError):
            raise ApiError(400, f"'{name}' must be [lat, lon].", name)
        if not (math.isfinite(lat) and math.isfinite(lon)):
            raise ApiError(400, f"'{name}' must be finite numbers.", name)
        s, w, n, e = NY_BBOX
        if not (s <= lat <= n and w <= lon <= e):
            which, pin = ("start", "A") if name == "from" else ("end", "B")
            raise ApiError(422, f"The {which} point ({lat:.4f}, {lon:.4f}) is outside New York State. "
                                f"Drag {pin} into New York.", name)
        return lat, lon

    def route(self, body: dict) -> dict:
        if not isinstance(body, dict):
            raise ApiError(400, "Expected a JSON object.")
        a, b = self._point(body, "from"), self._point(body, "to")
        ctx = self._ready()
        from ..search import search

        search_fn = self._search or search
        with self.lock:
            for name, which, pin, p in (("from", "start", "A", a), ("to", "end", "B", b)):
                _, d = ctx.g.snap(*p)
                if d > MAX_SNAP_M:
                    # no distance in the text: the page formats snap_m and limit_m in its own units
                    raise ApiError(422, f"The {which} point is too far from the nearest road in the graph. "
                                        f"Drag {pin} onto a road.", name, code="snap_far",
                                   snap_m=round(d), limit_m=round(MAX_SNAP_M))
            t0 = time.monotonic()
            res = search_fn(ctx, a, b)
            search_s = time.monotonic() - t0
        if res is None:
            raise ApiError(404, "No route found between these points: they may snap to the same junction "
                                "or be disconnected. Move A or B to a different road.")
        payload, sr_by_key = route_payload(ctx, res, self.breaks, self.grid, self.max_points)
        rid = uuid.uuid4().hex[:12]
        with self._rlock:
            self.results[rid] = {"res": res, "routes": sr_by_key, "waypoints": {}}
            while len(self.results) > KEEP_RESULTS:
                self.results.popitem(last=False)
        return {
            "id": rid, "from": [round(a[0], 5), round(a[1], 5)], "to": [round(b[0], 5), round(b[1], 5)],
            "snap_m": [round(res.snap["from_snap_m"]), round(res.snap["to_snap_m"])],
            "t0_min": round(res.t0 / 60, 1), "S_fast": round(res.s_fast, 3), "kept": len(res.kept),
            "search_s": round(search_s, 2), "seconds": res.seconds, "s_breaks": self.breaks, **payload,
        }

    def waypoints(self, body: dict) -> dict:
        if not isinstance(body, dict):
            raise ApiError(400, "Expected a JSON object.")
        rid, key = body.get("id"), body.get("key")
        ctx = self._ready()
        with self._rlock:
            stored = self.results.get(rid) if isinstance(rid, str) else None
        if stored is None:
            raise ApiError(404, "That search has expired. Press Find routes again.")
        sr = stored["routes"].get(key)
        if sr is None:
            raise ApiError(404, f"No route '{key}' in that search. Press Find routes again.")
        if key in stored["waypoints"]:
            return stored["waypoints"][key]
        from ..waypoints import reduce_to_waypoints

        fn = self._waypoints or reduce_to_waypoints
        with self.lock:
            t0 = time.monotonic()
            # only roads scenier than the fastest route count toward the waypoint spacing limit
            wr = fn(ctx.g, edge_path=np.asarray(sr.edges, dtype=np.int64), scenic=ctx.S,
                    s_fast=stored["res"].s_fast, **WAYPOINT_OPTS)
            secs = time.monotonic() - t0
        try:
            url = wr.url
        except AssertionError as exc:  # maps_url refuses links over the length limit
            raise ApiError(500, f"Could not build a Google Maps link ({exc}). Try another route.")
        out = {
            "key": key, "url": url, "url_length": len(url),
            "origin": [round(wr.origin[0], 5), round(wr.origin[1], 5)],
            "destination": [round(wr.destination[0], 5), round(wr.destination[1], 5)],
            "waypoints": [{"n": i + 1, "lat": round(w.lat, 5), "lon": round(w.lon, 5), "road": w.road,
                           "tier": int(w.tier)} for i, w in enumerate(wr.waypoints)],
            "meets_target": bool(wr.meets_target), "follow_target": wr.follow_target,
            "summary": wr.summary(), "notes": list(wr.notes), "seconds": round(secs, 2),
        }
        stored["waypoints"][key] = out
        return out

    # ---- dispatch ------------------------------------------------------------

    def api(self, method: str, path: str, body=None) -> tuple[int, dict]:
        routes = {("GET", "/api/health"): lambda: self.health(),
                  ("POST", "/api/geocode"): lambda: self.geocode(body),
                  ("POST", "/api/route"): lambda: self.route(body),
                  ("POST", "/api/waypoints"): lambda: self.waypoints(body)}
        fn = routes.get((method, path))
        if fn is None:
            if any(p == path for _, p in routes):
                return 405, {"error": f"{method} not allowed on {path}"}
            return 404, {"error": f"no endpoint {path}"}
        try:
            return 200, fn()
        except ApiError as exc:
            return exc.status, exc.payload()
        except Exception as exc:  # keep the server up and tell the page
            return 500, {"error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------
# HTTP

_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
          ".ico": "image/x-icon"}

# Leaflet is self-hosted under static/vendor/, so scripts and styles are 'self' only.
# img-src: the base layers' tile servers (docs/PUBLISH.md): OSM streets, and USGS
# The National Map topo and imagery (both on basemap.nationalmap.gov).
TILE_HOSTS = ("https://tile.openstreetmap.org", "https://basemap.nationalmap.gov")
CSP_BASE = ("default-src 'self'; script-src 'self'; style-src 'self'; "
            f"img-src 'self' data: {' '.join(TILE_HOSTS)}; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'")
CSP = CSP_BASE + "; frame-ancestors 'none'"  # frame-ancestors only works as a header, not in <meta>

MAX_BODY = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    app: App  # set by make_server
    server_version = "maps-for-wanderers/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter than the default; no client address
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _host_ok(self) -> bool:
        """Refuse requests whose Host is not this machine's loopback name
        (blocks DNS-rebinding pages from reaching the API)."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost")

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        gz = len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or "")
        if gz:
            body = gzip.compress(body, compresslevel=5)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # OSM's tile policy asks for a Referer; cross-site requests get the origin only
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        if gz:
            self.send_header("Content-Encoding", "gzip")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: dict) -> None:
        self._send(status, json.dumps(obj, separators=(",", ":")).encode("utf-8"), "application/json")

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("/", "/index.html") else path[len("/static/"):] if path.startswith("/static/") else None
        if name is None:
            return self._json(404, {"error": "not found"})
        f = (STATIC_DIR / name).resolve()
        if STATIC_DIR not in f.parents or not f.is_file():
            return self._json(404, {"error": "not found"})
        self._send(200, f.read_bytes(), _TYPES.get(f.suffix, "application/octet-stream"),
                   {"Content-Security-Policy": CSP} if f.suffix == ".html" else None)

    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "bad Host header"})
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/"):
            status, obj = self.server.app.api("GET", path)
            return self._json(status, obj)
        return self._static(path)

    do_HEAD = do_GET

    def do_POST(self):
        if not self._host_ok():
            return self._json(403, {"error": "bad Host header"})
        path = urllib.parse.urlsplit(self.path).path
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            return self._json(413, {"error": "request body too large"})
        raw = self.rfile.read(n) if n else b""
        if ctype != "application/json":  # also keeps cross-site form posts out
            return self._json(415, {"error": "send application/json"})
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            return self._json(400, {"error": "body is not valid JSON"})
        status, obj = self.server.app.api("POST", path, body)
        return self._json(status, obj)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, app: App, port: int, verbose: bool = False):
        self.app = app
        self.verbose = verbose
        super().__init__((HOST, port), Handler)


def make_server(app: App, port: int = DEFAULT_PORT, verbose: bool = False) -> Server:
    """An HTTP server for `app` on 127.0.0.1:`port` (0 picks a free port).
    There is deliberately no host argument."""
    return Server(app, port, verbose)


def serve(port: int = DEFAULT_PORT, graph_dir: Path | None = None, score_dir: Path | None = None,
          verbose: bool = False) -> int:
    from ..search import Context

    app = App(loader=lambda: Context.load(graph_dir, score_dir))
    httpd = make_server(app, port, verbose)
    app.start_loading()
    host, p = httpd.server_address[:2]
    print(f"Maps for Wanderers on http://{host}:{p}/  (loading route data in the background; Ctrl-C stops)",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
