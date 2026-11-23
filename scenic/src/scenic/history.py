"""Explicitly recorded trip history, stored in a rollback-journal database."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Mapping, Sequence
import hashlib
import json
import math
import os
import sqlite3
import tomllib

from . import data_dir

if TYPE_CHECKING:
    from .loop import LoopRoute
    from .route import RoadGraph, Route
    from .search import Context, ScenicRoute

SCHEMA_VERSION = 1
MAX_HISTORY_PENALTY = 1e4

LatLon = tuple[float, float]
TripInput = "Route | ScenicRoute | LoopRoute | Path | str | Sequence[LatLon]"

_DIRECTIONS = frozenset(("N", "NE", "E", "SE", "S", "SW", "W", "NW"))
_MODES = frozenset(("loop", "route", "custom"))

_TRIP_COLUMNS = (
    "taken_at", "title", "mode",
    "origin_lat", "origin_lon", "origin_label",
    "destination_lat", "destination_lon", "destination_label",
    "direction", "anchor_json", "budget_minutes", "budget_ratio",
    "minutes", "km", "scenic_min", "score", "rating", "notes",
    "google_maps_url", "waypoints_json", "geometry_geojson",
    "graph_fingerprint", "moments_fingerprint", "source_kind",
    "metadata_json", "created_at",
)
_EDGE_COLUMNS = (
    "seq", "edge_idx", "way_id",
    "u_lon", "u_lat", "v_lon", "v_lat",
    "length_m", "road_name", "highway", "reversed",
    "geom_start", "geom_end",
)
_MOMENT_COLUMNS = (
    "moment_id", "seq", "type", "feature", "parent_feature",
    "label", "lat", "lon", "value_m",
)

_SCHEMA = r"""
CREATE TABLE trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL CHECK (mode IN ('loop', 'route', 'custom')),

    origin_lat REAL NOT NULL CHECK (origin_lat BETWEEN -90 AND 90),
    origin_lon REAL NOT NULL CHECK (origin_lon BETWEEN -180 AND 180),
    origin_label TEXT,
    destination_lat REAL NOT NULL CHECK (destination_lat BETWEEN -90 AND 90),
    destination_lon REAL NOT NULL CHECK (destination_lon BETWEEN -180 AND 180),
    destination_label TEXT,

    direction TEXT CHECK (
        direction IN ('N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW')
    ),
    anchor_json TEXT,
    budget_minutes REAL CHECK (budget_minutes > 0),
    budget_ratio REAL CHECK (budget_ratio >= 1),

    minutes REAL NOT NULL CHECK (minutes >= 0),
    km REAL NOT NULL CHECK (km >= 0),
    scenic_min REAL CHECK (scenic_min >= 0),
    score REAL CHECK (score >= 0),
    rating INTEGER CHECK (rating BETWEEN 1 AND 5),
    notes TEXT NOT NULL DEFAULT '',

    google_maps_url TEXT,
    waypoints_json TEXT NOT NULL DEFAULT '[]',
    geometry_geojson TEXT NOT NULL,
    graph_fingerprint TEXT,
    moments_fingerprint TEXT,
    source_kind TEXT NOT NULL CHECK (
        source_kind IN ('route_object', 'geojson', 'google_maps', 'waypoints')
    ),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE trip_edges (
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL CHECK (seq >= 0),
    edge_idx INTEGER CHECK (edge_idx >= 0),
    way_id INTEGER,

    u_lon REAL NOT NULL CHECK (u_lon BETWEEN -180 AND 180),
    u_lat REAL NOT NULL CHECK (u_lat BETWEEN -90 AND 90),
    v_lon REAL NOT NULL CHECK (v_lon BETWEEN -180 AND 180),
    v_lat REAL NOT NULL CHECK (v_lat BETWEEN -90 AND 90),
    length_m REAL NOT NULL CHECK (length_m > 0),
    road_name TEXT,
    highway TEXT,
    reversed INTEGER NOT NULL CHECK (reversed IN (0, 1)),

    geom_start INTEGER NOT NULL CHECK (geom_start >= 0),
    geom_end INTEGER NOT NULL CHECK (geom_end > geom_start),
    PRIMARY KEY (trip_id, seq)
);

CREATE TABLE trip_moments (
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    moment_id INTEGER NOT NULL CHECK (moment_id >= 0),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    type TEXT NOT NULL,
    feature TEXT,
    parent_feature TEXT,
    label TEXT,
    lat REAL NOT NULL CHECK (lat BETWEEN -90 AND 90),
    lon REAL NOT NULL CHECK (lon BETWEEN -180 AND 180),
    value_m REAL NOT NULL CHECK (value_m BETWEEN 0 AND 1),
    PRIMARY KEY (trip_id, moment_id),
    UNIQUE (trip_id, seq)
);

CREATE INDEX trips_taken_at
    ON trips(taken_at DESC, id DESC);
CREATE INDEX trips_graph
    ON trips(graph_fingerprint, id);
CREATE INDEX trip_edges_current
    ON trip_edges(edge_idx, trip_id);
CREATE INDEX trip_edges_way
    ON trip_edges(way_id, trip_id);
CREATE INDEX trip_moments_location
    ON trip_moments(type, lat, lon);

PRAGMA user_version = 1;
"""


def validate_history_penalty(value: float) -> float:
    """Validate a finite, nonnegative history penalty that avoids cost overflow."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("history penalty must be a finite nonnegative number")
    val = float(value)
    if not math.isfinite(val) or val < 0.0:
        raise ValueError("history penalty must be a finite nonnegative number")
    if val > MAX_HISTORY_PENALTY:
        raise ValueError(f"history penalty {val} exceeds maximum allowed {MAX_HISTORY_PENALTY:g}")
    return val


def is_history_active(
    history: HistoryBias | None,
    *,
    n_edges: int | None = None,
    n_moments: int | None = None,
) -> bool:
    """Return True iff `history` carries at least one nonzero edge or moment penalty."""
    if history is None:
        return False
    import numpy as np

    edge_penalty = validate_history_penalty(history.edge_penalty)
    moment_penalty = validate_history_penalty(history.moment_penalty)
    edge_strength = np.asarray(history.edge_strength, dtype=np.float64)
    moment_strength = np.asarray(history.moment_strength, dtype=np.float64)
    if n_edges is not None and len(edge_strength) != n_edges:
        raise ValueError(f"history edge_strength length {len(edge_strength)} != {n_edges}")
    if n_moments is not None and len(moment_strength) != n_moments:
        raise ValueError(f"history moment_strength length {len(moment_strength)} != {n_moments}")
    if len(edge_strength) and (not np.all(np.isfinite(edge_strength)) or np.any(edge_strength < 0.0)):
        raise ValueError("history edge_strength must contain finite nonnegative values")
    if len(moment_strength) and (not np.all(np.isfinite(moment_strength)) or np.any(moment_strength < 0.0)):
        raise ValueError("history moment_strength must contain finite nonnegative values")
    edge_active = edge_penalty > 0.0 and len(edge_strength) > 0 and bool(np.any(edge_strength > 0.0))
    moment_active = moment_penalty > 0.0 and len(moment_strength) > 0 and bool(np.any(moment_strength > 0.0))
    return edge_active or moment_active


def _resolve_history_db(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    user_config: Path | None = None,
) -> tuple[Path, bool]:
    env = os.environ if environ is None else environ
    if path is not None:
        if not str(path).strip():
            raise ValueError("history database path must not be empty")
        return Path(path).expanduser().resolve(), True
    raw_env = env.get("SCENIC_HISTORY_DB")
    if raw_env is not None and raw_env != "":
        if not raw_env.strip():
            raise ValueError("SCENIC_HISTORY_DB must not be whitespace-only")
        return Path(raw_env).expanduser().resolve(), True
    if user_config is not None:
        config = Path(user_config).expanduser()
    else:
        xdg = env.get("XDG_CONFIG_HOME")
        if xdg is not None and xdg != "":
            if not xdg.strip():
                raise ValueError("XDG_CONFIG_HOME must not be whitespace-only")
            cfg_root = Path(xdg).expanduser()
        else:
            cfg_root = Path.home() / ".config"
        config = cfg_root / "scenic" / "config.toml"
    if config.exists():
        with config.open("rb") as f:
            settings = tomllib.load(f)
        history = settings.get("history", {})
        if not isinstance(history, dict):
            raise ValueError("history configuration must be a table")
        if "db" in history:
            value = history["db"]
            if not isinstance(value, str) or not value.strip():
                raise ValueError("history.db must be a nonempty path")
            result = Path(value).expanduser()
            return (result if result.is_absolute() else config.parent / result).resolve(), True
    if environ is None:
        base = data_dir()
    else:
        scenic_data = env.get("SCENIC_DATA")
        if scenic_data is not None and scenic_data != "":
            if not scenic_data.strip():
                raise ValueError("SCENIC_DATA must not be whitespace-only")
            base = Path(scenic_data).expanduser().resolve()
        else:
            from . import _repo_root

            repo = _repo_root()
            if repo is not None:
                base = repo / "data"
            else:
                xdg_data = env.get("XDG_DATA_HOME")
                root = Path(xdg_data).expanduser() if xdg_data else (Path.home() / ".local" / "share")
                base = (root / "scenic").resolve()
    return (base / "history.db").resolve(), False


def resolve_history_db(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    user_config: Path | None = None,
) -> Path:
    return _resolve_history_db(path, environ=environ, user_config=user_config)[0]


@contextmanager
def _connection(
    path: Path,
    *,
    create: bool = False,
    require_parent: bool = True,
    busy_timeout_ms: int = 5000,
) -> Iterator[sqlite3.Connection]:
    if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int) or busy_timeout_ms < 0:
        raise ValueError("busy_timeout_ms must be a nonnegative integer")
    path = Path(path).expanduser().resolve()
    if create and not path.parent.is_dir():
        if not require_parent:
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            raise FileNotFoundError(f"history database parent does not exist: {path.parent}")
    if not create and not path.exists():
        raise FileNotFoundError(f"history database not found: {path}; run scenic history init")
    uri = path.as_uri() + ("?mode=rwc" if create else "?mode=rw")
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=busy_timeout_ms / 1000.0)
    conn.row_factory = sqlite3.Row
    try:
        for statement in (
            f"busy_timeout={busy_timeout_ms}",
            "journal_mode=DELETE",
            "synchronous=FULL",
            "foreign_keys=ON",
            "locking_mode=NORMAL",
            "mmap_size=0",
        ):
            conn.execute("PRAGMA " + statement).fetchall()
        for name, expected in (
            ("journal_mode", "delete"),
            ("synchronous", 2),
            ("foreign_keys", 1),
            ("busy_timeout", busy_timeout_ms),
            ("locking_mode", "normal"),
            ("mmap_size", 0),
        ):
            val = conn.execute("PRAGMA " + name).fetchone()[0]
            if isinstance(val, str):
                val = val.lower()
            if val != expected:
                raise RuntimeError(f"could not configure SQLite {name}: got {val!r}")
        if not create:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version <= 0:
                raise ValueError(
                    f"history database is uninitialized (user_version={version}): {path}; run scenic history init"
                )
            if version > SCHEMA_VERSION:
                raise ValueError(f"unsupported history schema version {version}")
        yield conn
    finally:
        conn.close()


@contextmanager
def _transaction(conn: sqlite3.Connection, *, write: bool) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _timestamp(value: datetime | None = None) -> str:
    value = value if value is not None else datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_iso_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a nonempty ISO-8601 string")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return dt.astimezone(timezone.utc)


def history_config() -> dict:
    from .score import load_config

    return dict(load_config()["history"])


@dataclass(frozen=True)
class HistoryHit:
    trip_count: int
    last_taken_at: datetime
    decayed_visits: float


@dataclass(frozen=True)
class HistoryBias:
    edge_strength: object
    moment_strength: object
    edge_penalty: float
    moment_penalty: float
    diagnostics: Mapping[str, object]


@dataclass(frozen=True)
class TripMetadata:
    taken_at: datetime | None = None
    title: str = ""
    mode: str | None = None
    origin_label: str | None = None
    destination_label: str | None = None
    direction: str | None = None
    anchor: object = None
    budget_minutes: float | None = None
    budget_ratio: float | None = None
    rating: int | None = None
    notes: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class TripSummary:
    id: int
    taken_at: str
    title: str
    mode: str
    minutes: float
    km: float
    rating: int | None


@dataclass(frozen=True)
class TripRecord:
    trip: Mapping[str, object]
    geometry: Mapping[str, object]
    edges: list[Mapping[str, object]]
    moments: list[Mapping[str, object]]
    metadata: Mapping[str, object]


@dataclass(frozen=True)
class HistoryStats:
    trip_count: int
    minutes: float
    km: float
    by_mode: Mapping[str, int]
    first_taken_at: str | None
    latest_taken_at: str | None
    average_rating: float | None


def _coordinates(value) -> list[list[float]]:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise ValueError("a LineString needs at least two coordinates")
    result = []
    for pt in value:
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            raise ValueError("invalid coordinate")
        if isinstance(pt[0], bool) or isinstance(pt[1], bool):
            raise ValueError("invalid coordinate")
        lon, lat = float(pt[0]), float(pt[1])
        if not math.isfinite(lon) or not math.isfinite(lat) or not -180.0 <= lon <= 180.0 or not -90.0 <= lat <= 90.0:
            raise ValueError("invalid coordinate")
        result.append([lon, lat])
    return result


def _validate_trip_fields(trip: dict) -> None:
    if trip.get("mode") not in _MODES:
        raise ValueError(f"invalid trip mode: {trip.get('mode')!r}")
    direction = trip.get("direction")
    if direction is not None and direction not in _DIRECTIONS:
        raise ValueError(f"invalid trip direction: {direction!r}")
    rating = trip.get("rating")
    if rating is not None:
        if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
            raise ValueError("rating must be an integer between 1 and 5")
    for name in ("minutes", "km"):
        val = trip.get(name)
        if val is None or isinstance(val, bool) or not math.isfinite(float(val)) or float(val) < 0.0:
            raise ValueError(f"invalid {name}")
    for name in ("scenic_min", "score"):
        val = trip.get(name)
        if val is not None and (isinstance(val, bool) or not math.isfinite(float(val)) or float(val) < 0.0):
            raise ValueError(f"invalid {name}")
    bm = trip.get("budget_minutes")
    if bm is not None and (isinstance(bm, bool) or not math.isfinite(float(bm)) or float(bm) <= 0.0):
        raise ValueError("budget_minutes must be finite and > 0")
    br = trip.get("budget_ratio")
    if br is not None and (isinstance(br, bool) or not math.isfinite(float(br)) or float(br) < 1.0):
        raise ValueError("budget_ratio must be finite and >= 1")
    if not isinstance(trip.get("title", ""), str) or not isinstance(trip.get("notes", ""), str):
        raise ValueError("title and notes must be strings")


def _url_points(url: str) -> list[LatLon]:
    from urllib.parse import parse_qs, urlsplit

    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc.lower() != "www.google.com" or parsed.path != "/maps/dir/":
        raise ValueError("unsupported Google Maps directions URL")
    q = parse_qs(parsed.query, keep_blank_values=True)
    if set(q) - {"api", "origin", "destination", "travelmode", "waypoints"} or any(len(v) != 1 for v in q.values()):
        raise ValueError("unsupported Google Maps directions parameters")
    if q.get("api") != ["1"] or q.get("travelmode", ["driving"]) != ["driving"]:
        raise ValueError("unsupported Google Maps directions URL")

    def point(s: str) -> LatLon:
        parts = s.split(",")
        if len(parts) != 2:
            raise ValueError("Google Maps directions must use coordinates")
        try:
            lat, lon = float(parts[0]), float(parts[1])
            _coordinates([[lon, lat], [lon, lat]])
            return (lat, lon)
        except (TypeError, ValueError):
            raise ValueError("Google Maps directions must use coordinates") from None

    if not q.get("origin", [""])[0] or not q.get("destination", [""])[0]:
        raise ValueError("Google Maps directions require origin and destination")
    wp_raw = q.get("waypoints", [""])[0]
    wpts = [point(x) for x in wp_raw.split("|") if x] if wp_raw else []
    return [point(q["origin"][0])] + wpts + [point(q["destination"][0])]


def _waypoint_route(points: Sequence[LatLon], g: RoadGraph | None) -> Route:
    import numpy as np
    from .route import Route

    if g is None or len(points) < 2:
        raise ValueError("at least two waypoints and a graph are required")
    nodes = []
    for pt in points:
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            raise ValueError("invalid waypoint")
        lat, lon = float(pt[0]), float(pt[1])
        _coordinates([[lon, lat], [lon, lat]])
        node, distance = g.snap(lat, lon)
        if distance > 2000.0:
            raise ValueError("waypoint is more than 2000 m from the graph")
        nodes.append(node)
    legs = [g.route(a, b) for a, b in zip(nodes, nodes[1:])]
    if any(leg is None for leg in legs):
        raise ValueError("waypoint leg is unreachable")
    edges = np.concatenate([leg.edges for leg in legs])
    if not len(edges):
        raise ValueError("route has no edges")
    path = np.concatenate([legs[0].nodes] + [leg.nodes[1:] for leg in legs[1:]])
    rev = np.concatenate([leg.reversed for leg in legs])
    return Route(
        path,
        edges,
        rev,
        sum(x.time_s for x in legs),
        sum(x.length_m for x in legs),
        sum(x.cost for x in legs),
        g._geometry(path, edges, rev),
    )


def _legacy_route(feature: dict, g: RoadGraph | None) -> Route:
    import numpy as np
    from shapely.geometry import LineString
    from .route import Route
    from .search import _to5070

    if g is None:
        raise ValueError("legacy GeoJSON requires the original graph or a history-ready export")
    props = feature.get("properties", {})
    try:
        edges = [int(e) for e in props["edge_path"]]
        nodes = [int(n) for n in props["node_path"]]
        if not edges or len(nodes) != len(edges) + 1:
            raise ValueError()
        rev = []
        for i, e in enumerate(edges):
            if not 0 <= e < g.n_edges:
                raise ValueError()
            u, v = int(g.u[e]), int(g.v[e])
            if (nodes[i], nodes[i + 1]) == (u, v):
                rev.append(False)
            elif (nodes[i], nodes[i + 1]) == (v, u) and not g.oneway[e]:
                rev.append(True)
            else:
                raise ValueError()
        geometry = g._geometry(nodes, edges, rev)
        saved = _coordinates(feature["geometry"]["coordinates"])

        def line(c):
            x, y = _to5070(*np.asarray(c).T)
            return LineString(np.column_stack((x, y)))

        a, b = line(geometry), line(saved)
        if abs(a.length - b.length) > max(10.0, 0.001 * max(a.length, b.length)):
            raise ValueError()
        count = max(1, int(np.ceil(max(a.length, b.length) / 50.0)))
        if any(
            a.interpolate(i / count, normalized=True).distance(b.interpolate(i / count, normalized=True)) > 5.0
            for i in range(count + 1)
        ):
            raise ValueError()
        return Route(
            np.asarray(nodes),
            np.asarray(edges),
            np.asarray(rev),
            float(g.time_s[edges].sum()),
            float(g.length_m[edges].sum()),
            0.0,
            geometry,
        )
    except (KeyError, TypeError, ValueError, IndexError):
        raise ValueError(
            "legacy GeoJSON does not match the graph; supply the original graph or a history-ready export"
        ) from None


def _moment_dicts_from_ids(mom, moment_ids: Sequence[int]) -> list[dict]:
    by_id = {int(mid): i for i, mid in enumerate(mom.id)}
    rows = []
    for seq, mid in enumerate(moment_ids):
        i = by_id[int(mid)]
        rows.append({
            "moment_id": int(mid),
            "seq": seq,
            "type": str(mom.type[i]),
            "feature": None if mom.feature is None else mom.feature[i],
            "parent_feature": None if mom.group is None else mom.group[i],
            "label": None if mom.label is None else str(mom.label[i]) if mom.label[i] is not None else None,
            "lat": float(mom.lat[i]),
            "lon": float(mom.lon[i]),
            "value_m": float(mom.m[i]),
        })
    return rows


def _compute_credited_moment_dicts(route: Route, ctx: Context) -> list[dict]:
    from .search import credit_moments, moment_cap, passed_moments

    mom = ctx.moments
    if len(mom.id) == 0:
        return []
    ids, positions = passed_moments(route, ctx.g, mom, ctx.cfg["moment_radius_m"])
    if len(ids) == 0:
        return []
    by_id = {int(mid): i for i, mid in enumerate(mom.id)}
    indices = [by_id[int(mid)] for mid in ids]
    body = mom.body
    features = None if body is None else body[indices]
    credited = credit_moments(
        ids,
        positions,
        mom.m[indices],
        ctx.cfg["moment_spacing_km"] * 1000.0,
        moment_cap(ctx.cfg, route.time_s),
        features,
        ctx.cfg.get("feature_spacing_km", 0.0) * 1000.0,
    )
    return _moment_dicts_from_ids(mom, credited)


def _legacy_credited_moment_dicts(feature: dict, route: Route, ctx: Context) -> list[dict]:
    from .graph import haversine_m

    props = feature.get("properties", {})
    stored = props.get("moments")
    mom = ctx.moments
    if isinstance(stored, list):
        by_id = {int(mid): i for i, mid in enumerate(mom.id)}
        valid_ids: list[int] = []
        seen: set[int] = set()
        ok = True
        for item in stored:
            if not isinstance(item, dict) or "id" not in item:
                ok = False
                break
            mid = item.get("id")
            if isinstance(mid, bool) or not isinstance(mid, int) or mid not in by_id or mid in seen:
                ok = False
                break
            i = by_id[mid]
            if "type" in item and str(mom.type[i]) != str(item["type"]):
                ok = False
                break
            if "lat" in item and "lon" in item:
                try:
                    lat, lon = float(item["lat"]), float(item["lon"])
                    if not math.isfinite(lat) or not math.isfinite(lon):
                        ok = False
                        break
                    if float(haversine_m(lon, lat, float(mom.lon[i]), float(mom.lat[i]))) > 50.0:
                        ok = False
                        break
                except (TypeError, ValueError):
                    ok = False
                    break
            seen.add(mid)
            valid_ids.append(mid)
        if ok:
            return _moment_dicts_from_ids(mom, valid_ids)
    return _compute_credited_moment_dicts(route, ctx)


def _prepare_payload(
    payload: object,
    geometry: object,
    metadata: TripMetadata | None,
) -> tuple[dict, list[dict], list[dict]]:
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or not isinstance(geometry, dict)
        or geometry.get("type") != "LineString"
    ):
        raise ValueError("invalid history_v1 payload")
    coords = _coordinates(geometry.get("coordinates"))
    if not isinstance(payload.get("trip"), dict):
        raise ValueError("invalid history_v1 trip")
    trip = dict(payload["trip"])
    raw_edges, raw_moments = payload.get("trip_edges"), payload.get("trip_moments")
    if not isinstance(raw_edges, list) or not isinstance(raw_moments, list) or not raw_edges:
        raise ValueError("invalid history_v1 records")

    edges: list[dict] = []
    for seq, edge in enumerate(raw_edges):
        if not isinstance(edge, dict):
            raise ValueError("invalid history_v1 edge")
        gs, ge = edge.get("geom_start"), edge.get("geom_end")
        if (
            edge.get("seq") != seq
            or isinstance(gs, bool) or not isinstance(gs, int)
            or isinstance(ge, bool) or not isinstance(ge, int)
            or not 0 <= gs < ge < len(coords)
        ):
            raise ValueError("invalid history_v1 edge geometry bounds")
        if seq and gs != raw_edges[seq - 1]["geom_end"]:
            raise ValueError("disconnected history_v1 edges")
        eidx = edge.get("edge_idx")
        if eidx is not None and (isinstance(eidx, bool) or not isinstance(eidx, int) or eidx < 0):
            raise ValueError("invalid history_v1 edge_idx")
        _coordinates([[edge["u_lon"], edge["u_lat"]], [edge["v_lon"], edge["v_lat"]]])
        if (
            isinstance(edge.get("length_m"), bool)
            or not math.isfinite(float(edge["length_m"]))
            or float(edge["length_m"]) <= 0.0
            or isinstance(edge.get("reversed"), bool)
            or edge.get("reversed") not in (0, 1)
        ):
            raise ValueError("invalid history_v1 edge")
        start, end = coords[gs], coords[ge]
        a = [float(edge["u_lon"]), float(edge["u_lat"])]
        b = [float(edge["v_lon"]), float(edge["v_lat"])]
        if (start, end) != ((b, a) if edge["reversed"] else (a, b)):
            raise ValueError("history_v1 edge endpoints do not match geometry")
        edges.append({c: edge.get(c) for c in _EDGE_COLUMNS})
    if edges[0]["geom_start"] != 0 or edges[-1]["geom_end"] != len(coords) - 1:
        raise ValueError("history_v1 edges do not span geometry")

    moments: list[dict] = []
    seen_mids: set[int] = set()
    for seq, moment in enumerate(raw_moments):
        if not isinstance(moment, dict):
            raise ValueError("invalid history_v1 moment")
        mid = moment.get("moment_id")
        if isinstance(mid, bool) or not isinstance(mid, int) or mid < 0 or mid in seen_mids:
            raise ValueError("invalid history_v1 moment_id")
        seen_mids.add(mid)
        val_m = moment.get("value_m")
        if (
            moment.get("seq") != seq
            or not isinstance(moment.get("type"), str)
            or isinstance(val_m, bool)
            or not math.isfinite(float(val_m))
            or not 0.0 <= float(val_m) <= 1.0
        ):
            raise ValueError("invalid history_v1 moment")
        _coordinates([[moment["lon"], moment["lat"]], [moment["lon"], moment["lat"]]])
        moments.append({c: moment.get(c) for c in _MOMENT_COLUMNS})

    for name, expected in (("waypoints_json", list), ("metadata_json", dict)):
        raw_json = trip.get(name)
        if not isinstance(raw_json, str) or not isinstance(json.loads(raw_json), expected):
            raise ValueError("invalid history_v1 JSON")
    if trip.get("anchor_json") is not None:
        if not isinstance(trip["anchor_json"], str):
            raise ValueError("invalid history_v1 anchor_json")
        json.loads(trip["anchor_json"])

    if metadata is not None:
        if metadata.title:
            trip["title"] = metadata.title
        if metadata.mode is not None:
            trip["mode"] = metadata.mode
        if metadata.rating is not None:
            trip["rating"] = metadata.rating
        if metadata.notes:
            trip["notes"] = metadata.notes
        if metadata.origin_label is not None:
            trip["origin_label"] = metadata.origin_label
        if metadata.destination_label is not None:
            trip["destination_label"] = metadata.destination_label
        if metadata.direction is not None:
            trip["direction"] = metadata.direction
        if metadata.anchor is not None:
            trip["anchor_json"] = json.dumps(metadata.anchor, allow_nan=False)
        if metadata.budget_minutes is not None:
            trip["budget_minutes"] = float(metadata.budget_minutes)
        if metadata.budget_ratio is not None:
            trip["budget_ratio"] = float(metadata.budget_ratio)
        if metadata.taken_at is not None:
            trip["taken_at"] = _timestamp(metadata.taken_at)
        if metadata.metadata:
            merged_meta = json.loads(trip["metadata_json"])
            merged_meta.update(metadata.metadata)
            trip["metadata_json"] = json.dumps(merged_meta, allow_nan=False)

    trip["taken_at"] = _timestamp(_parse_iso_timestamp(trip.get("taken_at")))
    trip.setdefault("title", "")
    trip.setdefault("notes", "")
    trip.update(
        geometry_geojson=json.dumps({"type": "LineString", "coordinates": coords}, allow_nan=False),
        origin_lon=coords[0][0],
        origin_lat=coords[0][1],
        destination_lon=coords[-1][0],
        destination_lat=coords[-1][1],
        source_kind="geojson",
        created_at=_timestamp(),
    )
    _validate_trip_fields(trip)
    json.dumps(payload, allow_nan=False)
    return trip, edges, moments


def _prepare_trip_record(
    source,
    *,
    ctx: Context | None = None,
    graph: RoadGraph | None = None,
    pick: str | None = None,
    metadata: TripMetadata | None = None,
) -> tuple[dict, list[dict], list[dict]]:
    from .graph import edges_fingerprint
    from .route import Route

    g = ctx.g if ctx is not None else graph
    source_kind = "route_object"
    url: str | None = None
    waypoints: list[list[float]] = []
    legacy_feature: dict | None = None

    if isinstance(source, Path):
        document = json.loads(source.read_text())
        features = document.get("features", []) if document.get("type") == "FeatureCollection" else [document]
        lines = [f for f in features if isinstance(f, dict) and f.get("geometry", {}).get("type") == "LineString"]
        if pick is not None:
            lines = [
                f for f in lines
                if pick in (f.get("properties", {}).get("role"), f.get("properties", {}).get("label"))
            ]
        if len(lines) != 1:
            raise ValueError("select exactly one route LineString with --pick")
        feature = lines[0]
        payload = feature.get("properties", {}).get("history_v1")
        if payload is not None:
            return _prepare_payload(payload, feature["geometry"], metadata)
        legacy_feature = feature
        source = _legacy_route(feature, g)
        source_kind = "geojson"
    elif isinstance(source, str):
        url = source
        pts = _url_points(source)
        waypoints = [[lat, lon] for lat, lon in pts]
        source = _waypoint_route(pts, g)
        source_kind = "google_maps"
    elif isinstance(source, (list, tuple)):
        pts = [(float(pt[0]), float(pt[1])) for pt in source]
        waypoints = [[lat, lon] for lat, lon in pts]
        source = _waypoint_route(pts, g)
        source_kind = "waypoints"

    wrapper = source if hasattr(source, "route") else None
    route = source.route if wrapper is not None else source
    if not isinstance(route, Route):
        raise TypeError(f"unsupported trip source type: {type(source).__name__}")
    if g is None:
        raise ValueError("recording a route requires a graph or context")
    if len(route.nodes) != len(route.edges) + 1 or len(route.reversed) != len(route.edges) or not len(route.edges):
        raise ValueError("route edge and node arrays are inconsistent")

    meta = metadata or TripMetadata()
    coords: list[list[float]] = []
    edge_dicts: list[dict] = []
    for seq, (edge, rev) in enumerate(zip(route.edges, route.reversed)):
        e, rev = int(edge), bool(rev)
        if not 0 <= e < g.n_edges:
            raise ValueError("route edge is out of range")
        u, v = int(g.u[e]), int(g.v[e])
        if (int(route.nodes[seq]), int(route.nodes[seq + 1])) != ((v, u) if rev else (u, v)) or (rev and g.oneway[e]):
            raise ValueError("route contains an illegal traversal")
        seg = g._geometry([v, u] if rev else [u, v], [e], [rev])
        seg_pts = _coordinates(seg)
        if coords and coords[-1] != seg_pts[0]:
            raise ValueError("route geometry is disconnected")
        start = len(coords) - 1 if coords else 0
        coords.extend(seg_pts[1:] if coords else seg_pts)
        attrs = (
            {
                name: g.edges_table.column(name)[e].as_py()
                for name in ("way_id", "name", "ref", "highway")
                if name in g.edges_table.column_names
            }
            if g.edges_table is not None
            else {}
        )
        edge_len = float(g.length_m[e])
        if not math.isfinite(edge_len) or edge_len <= 0.0:
            raise ValueError("edge length must be finite and positive")
        edge_dicts.append({
            "seq": seq,
            "edge_idx": e,
            "way_id": attrs.get("way_id"),
            "u_lon": float(g.node_lon[u]),
            "u_lat": float(g.node_lat[u]),
            "v_lon": float(g.node_lon[v]),
            "v_lat": float(g.node_lat[v]),
            "length_m": edge_len,
            "road_name": attrs.get("name") or attrs.get("ref"),
            "highway": attrs.get("highway"),
            "reversed": int(rev),
            "geom_start": start,
            "geom_end": len(coords) - 1,
        })

    if not all(math.isfinite(v) and v >= 0.0 for v in (route.minutes, route.km)):
        raise ValueError("invalid route duration or length")

    fp = edges_fingerprint(g.edges_table) if g.edges_table is not None else None
    mfp = moments_fingerprint(ctx.moments) if ctx is not None else None

    moment_dicts: list[dict] = []
    if wrapper is not None and ctx is not None:
        moment_dicts = _moment_dicts_from_ids(ctx.moments, wrapper.moments)
    elif legacy_feature is not None and ctx is not None:
        moment_dicts = _legacy_credited_moment_dicts(legacy_feature, route, ctx)
    elif wrapper is None and ctx is not None:
        moment_dicts = _compute_credited_moment_dicts(route, ctx)

    default_mode = "custom"
    if wrapper is not None:
        default_mode = "loop" if type(wrapper).__name__ == "LoopRoute" else "route"
    elif legacy_feature is not None:
        role = str(legacy_feature.get("properties", {}).get("role") or "")
        if role.startswith("loop_"):
            default_mode = "loop"
        elif role == "fastest" or role.startswith("+"):
            default_mode = "route"
    mode = meta.mode or default_mode

    scenic_min = None if wrapper is None else float(wrapper.scenic_min)
    score = None if wrapper is None else float(wrapper.score)
    if wrapper is None and legacy_feature is not None and ctx is not None:
        lprops = legacy_feature.get("properties", {})
        if lprops.get("scenic_minutes") is not None:
            scenic_min = float(lprops["scenic_minutes"])
        if lprops.get("score") is not None:
            score = float(lprops["score"])

    extra_dict = dict(meta.metadata)
    if waypoints:
        extra_dict["routing"] = "local_fastest"

    trip_dict = {
        "taken_at": _timestamp(meta.taken_at),
        "title": meta.title,
        "mode": mode,
        "origin_lat": coords[0][1],
        "origin_lon": coords[0][0],
        "origin_label": meta.origin_label,
        "destination_lat": coords[-1][1],
        "destination_lon": coords[-1][0],
        "destination_label": meta.destination_label,
        "direction": meta.direction,
        "anchor_json": None if meta.anchor is None else json.dumps(meta.anchor, allow_nan=False),
        "budget_minutes": meta.budget_minutes,
        "budget_ratio": meta.budget_ratio,
        "minutes": float(route.minutes),
        "km": float(route.km),
        "scenic_min": scenic_min,
        "score": score,
        "rating": meta.rating,
        "notes": meta.notes,
        "google_maps_url": url,
        "waypoints_json": json.dumps(waypoints, allow_nan=False),
        "geometry_geojson": json.dumps({"type": "LineString", "coordinates": coords}, allow_nan=False),
        "graph_fingerprint": fp,
        "moments_fingerprint": mfp,
        "source_kind": source_kind,
        "metadata_json": json.dumps(extra_dict, allow_nan=False),
        "created_at": _timestamp(),
    }
    _validate_trip_fields(trip_dict)
    return trip_dict, edge_dicts, moment_dicts


def _extract_linestrings(geom) -> list:
    from shapely.geometry import GeometryCollection, LineString, MultiLineString
    from shapely.ops import linemerge

    raw: list[LineString] = []

    def collect(g):
        if g.is_empty:
            return
        if isinstance(g, LineString):
            if len(g.coords) >= 2 and g.length > 0.0:
                raw.append(g)
        elif isinstance(g, (MultiLineString, GeometryCollection)):
            for part in g.geoms:
                collect(part)

    collect(geom)
    if len(raw) <= 1:
        return raw
    merged = linemerge(raw)
    out: list[LineString] = []
    if isinstance(merged, LineString):
        if merged.length > 0.0:
            out.append(merged)
    elif isinstance(merged, (MultiLineString, GeometryCollection)):
        for part in merged.geoms:
            if isinstance(part, LineString) and part.length > 0.0:
                out.append(part)
    return out


def _union_intervals(intervals: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    if not intervals:
        return []
    ordered = sorted((min(a, b), max(a, b)) for a, b in intervals if b > a)
    if not ordered:
        return []
    merged: list[tuple[float, float]] = [ordered[0]]
    for lo, hi in ordered[1:]:
        prev_lo, prev_hi = merged[-1]
        if lo <= prev_hi + 1e-9:
            merged[-1] = (prev_lo, max(prev_hi, hi))
        else:
            merged.append((lo, hi))
    return merged


def _interval_overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def _tangent(ln, d: float) -> tuple[float, float] | None:
    if ln.length <= 1e-9:
        return None
    step = min(5.0, max(0.5, ln.length * 0.25))
    p = ln.interpolate(max(0.0, d - step))
    q = ln.interpolate(min(ln.length, d + step))
    dx, dy = q.x - p.x, q.y - p.y
    norm = math.hypot(dx, dy)
    if norm <= 1e-12:
        return None
    return (dx / norm, dy / norm)


def _mean_perp_distance(piece, target) -> float:
    n_s = min(50, max(3, int(math.ceil(piece.length / 10.0)) + 1))
    total = 0.0
    for k in range(n_s):
        pt = piece.interpolate(k / (n_s - 1), normalized=True)
        total += float(pt.distance(target))
    return total / n_s


def _norm_feature(value) -> str:
    return " ".join(str(value or "").casefold().split())


class HistoryDB:
    def __init__(self, path: str | Path, *, require_parent: bool = True, busy_timeout_ms: int = 5000):
        self.require_parent = require_parent
        self.busy_timeout_ms = busy_timeout_ms
        self.path = Path(path).expanduser().resolve()

    def init_db(self) -> None:
        with _connection(
            self.path,
            create=True,
            require_parent=self.require_parent,
            busy_timeout_ms=self.busy_timeout_ms,
        ) as conn, _transaction(conn, write=True):
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version < 0 or version > SCHEMA_VERSION:
                raise ValueError(f"unsupported history schema version {version}")
            if version == 0:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1").fetchone():
                    raise ValueError("database is not an empty history database")
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        conn.execute(statement)

    def list_trips(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        since: datetime | None = None,
        mode: str | None = None,
    ) -> list[TripSummary]:
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must be nonnegative")
        if mode is not None and mode not in _MODES:
            raise ValueError("invalid trip mode")
        sql = "SELECT id,taken_at,title,mode,minutes,km,rating FROM trips WHERE 1=1"
        params: list[object] = []
        if since is not None:
            sql += " AND taken_at >= ?"
            params.append(_timestamp(since))
        if mode is not None:
            sql += " AND mode = ?"
            params.append(mode)
        sql += " ORDER BY taken_at DESC,id DESC LIMIT ? OFFSET ?"
        params.extend((limit, offset))
        with _connection(self.path, busy_timeout_ms=self.busy_timeout_ms) as conn, _transaction(conn, write=False):
            return [TripSummary(**dict(row)) for row in conn.execute(sql, params)]

    def get_trip(self, trip_id: int) -> TripRecord | None:
        with _connection(self.path, busy_timeout_ms=self.busy_timeout_ms) as conn, _transaction(conn, write=False):
            row = conn.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
            if row is None:
                return None
            trip = dict(row)
            edges = [dict(r) for r in conn.execute("SELECT * FROM trip_edges WHERE trip_id=? ORDER BY seq", (trip_id,))]
            moments = [
                dict(r) for r in conn.execute("SELECT * FROM trip_moments WHERE trip_id=? ORDER BY seq", (trip_id,))
            ]
            return TripRecord(
                trip,
                json.loads(trip["geometry_geojson"]),
                edges,
                moments,
                json.loads(trip["metadata_json"]),
            )

    def delete_trip(self, trip_id: int) -> bool:
        with _connection(self.path, busy_timeout_ms=self.busy_timeout_ms) as conn, _transaction(conn, write=True):
            return conn.execute("DELETE FROM trips WHERE id=?", (trip_id,)).rowcount > 0

    def stats(self) -> HistoryStats:
        with _connection(self.path, busy_timeout_ms=self.busy_timeout_ms) as conn, _transaction(conn, write=False):
            row = conn.execute(
                "SELECT count(*),coalesce(sum(minutes),0),coalesce(sum(km),0),"
                "min(taken_at),max(taken_at),avg(rating) FROM trips"
            ).fetchone()
            counts = {r[0]: r[1] for r in conn.execute("SELECT mode,count(*) FROM trips GROUP BY mode")}
            return HistoryStats(row[0], row[1], row[2], counts, row[3], row[4], row[5])

    def _history_rows(self, since: datetime | None, as_of: datetime | None) -> tuple[list[dict], datetime]:
        now_dt = as_of if as_of is not None else datetime.now(timezone.utc)
        upper = _timestamp(now_dt)
        query = "SELECT * FROM trips WHERE taken_at <= ?"
        args: list[object] = [upper]
        if since is not None:
            query += " AND taken_at >= ?"
            args.append(_timestamp(since))
        with _connection(self.path, busy_timeout_ms=self.busy_timeout_ms) as conn, _transaction(conn, write=False):
            trips = [dict(row) for row in conn.execute(query, args)]
            for trip in trips:
                trip["edges"] = [
                    dict(row) for row in conn.execute("SELECT * FROM trip_edges WHERE trip_id=? ORDER BY seq", (trip["id"],))
                ]
                trip["moments"] = [
                    dict(row) for row in conn.execute("SELECT * FROM trip_moments WHERE trip_id=? ORDER BY seq", (trip["id"],))
                ]
        return trips, _parse_iso_timestamp(upper)

    def _compute_driven_edges(
        self,
        ctx: Context,
        trips: list[dict],
        now: datetime,
        *,
        half_life_days: float,
        exempt_centers: Sequence[LatLon],
        exempt_radius_m: float,
        edge_match_m: float,
    ) -> tuple[dict[int, HistoryHit], int, int]:
        import numpy as np
        from shapely.geometry import LineString, Point
        from shapely.ops import substring, unary_union
        from .graph import edges_fingerprint
        from .search import _to5070

        if (
            not math.isfinite(half_life_days) or half_life_days <= 0.0
            or not math.isfinite(edge_match_m) or edge_match_m <= 0.0
            or not math.isfinite(exempt_radius_m) or exempt_radius_m < 0.0
        ):
            raise ValueError("invalid history matching settings")

        g = ctx.g
        fp = edges_fingerprint(g.edges_table) if g.edges_table is not None else None
        ways: dict[int, list[int]] = {}
        edge_way_ids: list[object] = [None] * g.n_edges
        if g.edges_table is not None and "way_id" in g.edges_table.column_names:
            edge_way_ids = g.edges_table.column("way_id").to_pylist()
            for i, way in enumerate(edge_way_ids):
                if way is not None:
                    ways.setdefault(way, []).append(i)

        def line(coords) -> LineString:
            arr = np.asarray(coords, dtype=np.float64)
            x, y = _to5070(arr[:, 0], arr[:, 1])
            return LineString(np.column_stack((x, y)))

        def point5070(lon: float, lat: float) -> Point:
            x, y = _to5070([lon], [lat])
            return Point(float(x[0]), float(y[0]))

        disks = None
        if exempt_radius_m > 0.0 and exempt_centers:
            disks = unary_union([point5070(lon, lat).buffer(exempt_radius_m) for lat, lon in exempt_centers])

        cached: dict[int, LineString] = {}

        def current(e: int) -> LineString:
            ln = cached.get(e)
            if ln is None:
                ln = line(g._geometry([int(g.u[e]), int(g.v[e])], [e], [False]))
                cached[e] = ln
            return ln

        cos_limit = math.cos(math.pi / 6.0) - 1e-9
        accum: dict[int, list[tuple[datetime, float]]] = {}
        matched_segments = 0
        unmatched_segments = 0

        for trip in trips:
            taken = _parse_iso_timestamp(trip["taken_at"])
            age_days = max(0.0, (now - taken).total_seconds() / 86400.0)
            weight = 2.0 ** (-age_days / half_life_days)
            coords = json.loads(trip["geometry_geojson"])["coordinates"]
            intervals_by_edge: dict[int, list[tuple[float, float]]] = {}

            for edge in trip["edges"]:
                historical = line(coords[edge["geom_start"]:edge["geom_end"] + 1])
                old_u = point5070(float(edge["u_lon"]), float(edge["u_lat"]))
                old_v = point5070(float(edge["v_lon"]), float(edge["v_lat"]))
                candidates = ways.get(edge["way_id"], []) if edge["way_id"] is not None else []
                idx = edge["edge_idx"]

                # Tier 0: Fast mapping when graph fingerprint matches
                if (
                    fp is not None
                    and trip["graph_fingerprint"] == fp
                    and idx is not None
                    and 0 <= idx < g.n_edges
                    and edge_way_ids[idx] == edge["way_id"]
                ):
                    c = current(idx)
                    if max(Point(c.coords[0]).distance(old_u), Point(c.coords[-1]).distance(old_v)) <= edge_match_m:
                        if c.length > 0.0:
                            intervals_by_edge.setdefault(idx, []).append((0.0, c.length))
                        matched_segments += 1
                        continue

                if not candidates or historical.length <= 0.0:
                    unmatched_segments += 1
                    continue

                # Tier 1 (Rebuild step A): Orientation-agnostic endpoint match
                len_h = historical.length
                ep_matches: list[tuple[float, int]] = []
                for e in candidates:
                    c = current(e)
                    len_c = c.length
                    if len_c <= 0.0:
                        continue
                    cu, cv = Point(c.coords[0]), Point(c.coords[-1])
                    d_same = max(cu.distance(old_u), cv.distance(old_v))
                    d_rev = max(cu.distance(old_v), cv.distance(old_u))
                    if min(d_same, d_rev) > edge_match_m:
                        continue
                    max_len = max(len_c, len_h)
                    min_len = min(len_c, len_h)
                    if min_len / max_len < 0.5 or abs(len_c - len_h) > max(2.0 * edge_match_m, 0.15 * max_len):
                        continue
                    p0 = historical.project(cu)
                    p1 = historical.project(cv)
                    if abs(p1 - p0) < 0.6 * len_h:
                        continue
                    mean_d = _mean_perp_distance(c, historical)
                    if mean_d <= edge_match_m:
                        ep_matches.append((mean_d, e))

                if ep_matches:
                    ep_matches.sort(key=lambda x: x[0])
                    if len(ep_matches) > 1 and ep_matches[1][0] <= ep_matches[0][0] + 5.0:
                        unmatched_segments += 1
                    else:
                        best_e = ep_matches[0][1]
                        intervals_by_edge.setdefault(best_e, []).append((0.0, current(best_e).length))
                        matched_segments += 1
                    continue

                # Tier 2 (Rebuild step B): Polyline overlap in EPSG:5070
                hist_buf = historical.buffer(edge_match_m, cap_style="flat")
                poly_matches: list[tuple[float, int, tuple[float, float], tuple[float, float]]] = []
                for e in candidates:
                    c = current(e)
                    if c.length <= 0.0:
                        continue
                    raw_overlap = c.intersection(hist_buf)
                    if raw_overlap.is_empty:
                        continue
                    pieces = _extract_linestrings(raw_overlap)
                    min_overlap = min(20.0, 0.8 * min(c.length, len_h))
                    for piece in pieces:
                        if piece.length < min_overlap:
                            continue
                        n_s = min(50, max(3, int(math.ceil(piece.length / 10.0)) + 1))
                        pts = [piece.interpolate(k / (n_s - 1), normalized=True) for k in range(n_s)]
                        mean_dist = sum(float(pt.distance(historical)) for pt in pts) / len(pts)
                        if mean_dist > edge_match_m + 1e-6:
                            continue
                        # Check bidirectional local tangent alignment (<= 30 deg)
                        good_tan = 0
                        mid_ok = False
                        for idx_f, frac in enumerate((0.25, 0.5, 0.75)):
                            pt = piece.interpolate(frac, normalized=True)
                            tc = _tangent(c, c.project(pt))
                            th = _tangent(historical, historical.project(pt))
                            if tc is not None and th is not None:
                                cos_val = abs(tc[0] * th[0] + tc[1] * th[1])
                                if cos_val >= cos_limit:
                                    good_tan += 1
                                    if idx_f == 1:
                                        mid_ok = True
                        if not mid_ok or good_tan < 2:
                            continue
                        c_projs = [float(c.project(pt)) for pt in pts]
                        h_projs = [float(historical.project(pt)) for pt in pts]
                        c_int = (min(c_projs), max(c_projs))
                        h_int = (min(h_projs), max(h_projs))
                        if c_int[1] - c_int[0] <= 1e-6 or h_int[1] - h_int[0] <= 1e-6:
                            continue
                        poly_matches.append((mean_dist, e, c_int, h_int))

                poly_matches.sort(key=lambda x: x[0])
                accepted_any = False
                for i, (d_i, e_i, c_int_i, h_int_i) in enumerate(poly_matches):
                    h_len_i = h_int_i[1] - h_int_i[0]
                    rejected = False
                    for j, (d_j, e_j, _c_int_j, h_int_j) in enumerate(poly_matches):
                        if i == j or e_i == e_j:
                            continue
                        ov = _interval_overlap(h_int_i, h_int_j)
                        h_len_j = h_int_j[1] - h_int_j[0]
                        if ov > min(10.0, 0.5 * min(h_len_i, h_len_j)) and d_j <= d_i + 5.0:
                            rejected = True
                            break
                    if not rejected:
                        intervals_by_edge.setdefault(e_i, []).append(c_int_i)
                        accepted_any = True
                if accepted_any:
                    matched_segments += 1
                else:
                    unmatched_segments += 1

            # Union intervals per trip and remove exempt disk portions before computing f_te
            for e, raw_intervals in intervals_by_edge.items():
                full = current(e)
                total_len = full.length
                if total_len <= 0.0:
                    continue
                merged = _union_intervals(raw_intervals)
                if disks is not None:
                    covered_len = 0.0
                    for lo, hi in merged:
                        sub = full if (lo <= 1e-6 and hi >= total_len - 1e-6) else substring(full, lo, hi)
                        covered_len += float(sub.difference(disks).length)
                else:
                    covered_len = sum(hi - lo for lo, hi in merged)
                fraction = min(1.0, covered_len / total_len)
                if fraction > 0.0:
                    accum.setdefault(e, []).append((taken, weight * fraction))

        hits = {
            e: HistoryHit(len(rows), max(x[0] for x in rows), sum(x[1] for x in rows))
            for e, rows in accum.items()
        }
        return hits, matched_segments, unmatched_segments

    def driven_edges(
        self,
        ctx: Context,
        *,
        since: datetime | None = None,
        as_of: datetime | None = None,
        half_life_days: float = 180.0,
        exempt_centers: Sequence[LatLon] = (),
        exempt_radius_m: float = 0.0,
        edge_match_m: float = 25.0,
    ) -> dict[int, HistoryHit]:
        trips, now = self._history_rows(since, as_of)
        hits, _, _ = self._compute_driven_edges(
            ctx,
            trips,
            now,
            half_life_days=half_life_days,
            exempt_centers=exempt_centers,
            exempt_radius_m=exempt_radius_m,
            edge_match_m=edge_match_m,
        )
        return hits

    def _compute_visited_moments(
        self,
        ctx: Context,
        trips: list[dict],
        now: datetime,
        *,
        half_life_days: float,
        exempt_centers: Sequence[LatLon],
        exempt_radius_m: float,
        moment_match_m: float,
    ) -> tuple[dict[int, HistoryHit], int, int]:
        import numpy as np
        from .graph import haversine_m

        if (
            not math.isfinite(half_life_days) or half_life_days <= 0.0
            or not math.isfinite(moment_match_m) or moment_match_m < 0.0
            or not math.isfinite(exempt_radius_m) or exempt_radius_m < 0.0
        ):
            raise ValueError("invalid history matching settings")

        mom = ctx.moments
        fp = moments_fingerprint(mom)
        by_id = {int(mid): i for i, mid in enumerate(mom.id)}

        # Precompute normalized names and type index for fast matching
        n_mom = len(mom.id)
        cur_feat = [_norm_feature(None if mom.feature is None else mom.feature[i]) for i in range(n_mom)]
        cur_parent = [_norm_feature(None if mom.group is None else mom.group[i]) for i in range(n_mom)]
        by_type: dict[str, np.ndarray] = {}
        if n_mom:
            type_lists: dict[str, list[int]] = {}
            for i in range(n_mom):
                type_lists.setdefault(str(mom.type[i]), []).append(i)
            by_type = {k: np.asarray(v, dtype=np.int64) for k, v in type_lists.items()}

        accum: dict[int, list[tuple[datetime, float]]] = {}
        matched_moments = 0
        unmatched_moments = 0

        for trip in trips:
            taken = _parse_iso_timestamp(trip["taken_at"])
            age_days = max(0.0, (now - taken).total_seconds() / 86400.0)
            weight = 2.0 ** (-age_days / half_life_days)
            hits_this_trip: set[int] = set()
            fp_match = trip["moments_fingerprint"] == fp

            for old in trip["moments"]:
                matched_idx: int | None = None
                if fp_match:
                    matched_idx = by_id.get(int(old["moment_id"]))
                else:
                    same_type = by_type.get(str(old["type"]))
                    if same_type is not None and len(same_type):
                        dists = np.asarray(
                            haversine_m(
                                float(old["lon"]),
                                float(old["lat"]),
                                mom.lon[same_type],
                                mom.lat[same_type],
                            ),
                            dtype=np.float64,
                        )
                        near_pos = np.flatnonzero(dists <= moment_match_m)
                        if len(near_pos):
                            prior = _norm_feature(old["feature"])
                            prior_parent = _norm_feature(old["parent_feature"])
                            candidates: list[tuple[float, int, int, bool]] = []
                            for pos in near_pos:
                                i = int(same_type[int(pos)])
                                feat = cur_feat[i]
                                parent = cur_parent[i]
                                unnamed = False
                                if feat and prior:
                                    if feat != prior:
                                        continue
                                elif feat or prior:
                                    # Specific feature unavailable on one side: allow parent_feature fallback
                                    if feat and not prior:
                                        if not prior_parent or (parent != prior_parent and feat != prior_parent):
                                            continue
                                    else:
                                        if not parent or (parent != prior_parent and parent != prior):
                                            continue
                                else:
                                    if parent and prior_parent:
                                        if parent != prior_parent:
                                            continue
                                    elif parent or prior_parent:
                                        continue
                                    else:
                                        unnamed = True
                                candidates.append((float(dists[int(pos)]), int(mom.id[i]), i, unnamed))
                            candidates.sort(key=lambda x: (x[0], x[1]))
                            if candidates:
                                ambiguous = False
                                if len(candidates) > 1:
                                    d0, d1 = candidates[0][0], candidates[1][0]
                                    if d1 - d0 <= 5.0:
                                        ambiguous = True
                                    elif candidates[0][3] and d0 > 0.0 and d1 <= d0 + 15.0:
                                        ambiguous = True
                                if not ambiguous:
                                    matched_idx = candidates[0][2]

                if matched_idx is None:
                    unmatched_moments += 1
                    continue

                matched_moments += 1
                if (
                    exempt_radius_m > 0.0
                    and exempt_centers
                    and any(
                        float(haversine_m(lon, lat, float(mom.lon[matched_idx]), float(mom.lat[matched_idx])))
                        <= exempt_radius_m
                        for lat, lon in exempt_centers
                    )
                ):
                    continue
                hits_this_trip.add(int(mom.id[matched_idx]))

            for mid in hits_this_trip:
                accum.setdefault(mid, []).append((taken, weight))

        hits = {
            mid: HistoryHit(len(rows), max(x[0] for x in rows), sum(x[1] for x in rows))
            for mid, rows in accum.items()
        }
        return hits, matched_moments, unmatched_moments

    def visited_moments(
        self,
        ctx: Context,
        *,
        since: datetime | None = None,
        as_of: datetime | None = None,
        half_life_days: float = 180.0,
        exempt_centers: Sequence[LatLon] = (),
        exempt_radius_m: float = 0.0,
        moment_match_m: float = 250.0,
    ) -> dict[int, HistoryHit]:
        trips, now = self._history_rows(since, as_of)
        hits, _, _ = self._compute_visited_moments(
            ctx,
            trips,
            now,
            half_life_days=half_life_days,
            exempt_centers=exempt_centers,
            exempt_radius_m=exempt_radius_m,
            moment_match_m=moment_match_m,
        )
        return hits

    def bias(
        self,
        ctx: Context,
        *,
        start: LatLon,
        end: LatLon,
        cfg: Mapping[str, float],
        as_of: datetime | None = None,
    ) -> HistoryBias:
        import numpy as np

        params = {
            k: float(cfg[k])
            for k in (
                "edge_penalty",
                "moment_penalty",
                "half_life_days",
                "home_radius_m",
                "edge_match_m",
                "moment_match_m",
            )
        }
        validate_history_penalty(params["edge_penalty"])
        validate_history_penalty(params["moment_penalty"])
        if (
            not math.isfinite(params["half_life_days"]) or params["half_life_days"] <= 0.0
            or not math.isfinite(params["home_radius_m"]) or params["home_radius_m"] < 0.0
            or not math.isfinite(params["edge_match_m"]) or params["edge_match_m"] <= 0.0
            or not math.isfinite(params["moment_match_m"]) or params["moment_match_m"] < 0.0
        ):
            raise ValueError("invalid history configuration")

        # Single short read transaction; SQLite is closed before matching and searching.
        trips, now = self._history_rows(None, as_of)

        g = ctx.g
        nodes = [g.snap(float(pt[0]), float(pt[1]))[0] for pt in (start, end)]
        centers = [(float(g.node_lat[n]), float(g.node_lon[n])) for n in dict.fromkeys(nodes)]

        edge_hits, matched_segments, unmatched_segments = self._compute_driven_edges(
            ctx,
            trips,
            now,
            half_life_days=params["half_life_days"],
            exempt_centers=centers,
            exempt_radius_m=params["home_radius_m"],
            edge_match_m=params["edge_match_m"],
        )
        moment_hits, matched_moments_count, unmatched_moments = self._compute_visited_moments(
            ctx,
            trips,
            now,
            half_life_days=params["half_life_days"],
            exempt_centers=centers,
            exempt_radius_m=params["home_radius_m"],
            moment_match_m=params["moment_match_m"],
        )

        edge_strength = np.zeros(g.n_edges, dtype=np.float64)
        moment_strength = np.zeros(len(ctx.moments.id), dtype=np.float64)
        for e, hit in edge_hits.items():
            edge_strength[e] = min(1.0, hit.decayed_visits)
        for i, mid in enumerate(ctx.moments.id):
            hit = moment_hits.get(int(mid))
            if hit is not None:
                moment_strength[i] = min(1.0, hit.decayed_visits)

        return HistoryBias(
            edge_strength=edge_strength,
            moment_strength=moment_strength,
            edge_penalty=params["edge_penalty"],
            moment_penalty=params["moment_penalty"],
            diagnostics={
                "trip_count": len(trips),
                "matched_edges": len(edge_hits),
                "matched_segments": matched_segments,
                "unmatched_segments": unmatched_segments,
                "matched_moments": len(moment_hits),
                "matched_moment_hits": matched_moments_count,
                "unmatched_moments": unmatched_moments,
                "exempt_radius_m": params["home_radius_m"],
                "edge_penalty": params["edge_penalty"],
                "moment_penalty": params["moment_penalty"],
                "half_life_days": params["half_life_days"],
            },
        )

    def export_trip_geojson(self, trip_id: int) -> dict:
        record = self.get_trip(trip_id)
        if record is None:
            raise ValueError("trip not found")
        trip = dict(record.trip)
        for name in ("id", "geometry_geojson", "created_at", "source_kind"):
            trip.pop(name, None)
        payload = {
            "version": 1,
            "trip": trip,
            "trip_edges": [{k: v for k, v in row.items() if k != "trip_id"} for row in record.edges],
            "trip_moments": [{k: v for k, v in row.items() if k != "trip_id"} for row in record.moments],
        }
        features = [{"type": "Feature", "properties": {"history_v1": payload}, "geometry": record.geometry}]
        for m in record.moments:
            features.append({
                "type": "Feature",
                "properties": {"role": "moment", **{k: v for k, v in m.items() if k != "trip_id"}},
                "geometry": {"type": "Point", "coordinates": [m["lon"], m["lat"]]},
            })
        return {"type": "FeatureCollection", "features": features}

    def record_trip(
        self,
        source,
        *,
        ctx: Context | None = None,
        graph: RoadGraph | None = None,
        pick: str | None = None,
        metadata: TripMetadata | None = None,
    ) -> int:
        # Prepare and validate completely before taking any write lock.
        trip_dict, edge_dicts, moment_dicts = _prepare_trip_record(
            source, ctx=ctx, graph=graph, pick=pick, metadata=metadata
        )
        self.init_db()
        with _connection(self.path, busy_timeout_ms=self.busy_timeout_ms) as conn, _transaction(conn, write=True):
            cur = conn.execute(
                "INSERT INTO trips (" + ",".join(_TRIP_COLUMNS) + ") VALUES ("
                + ",".join("?" for _ in _TRIP_COLUMNS) + ")",
                [trip_dict.get(c) for c in _TRIP_COLUMNS],
            )
            trip_id = int(cur.lastrowid)
            conn.executemany(
                "INSERT INTO trip_edges (trip_id," + ",".join(_EDGE_COLUMNS) + ") VALUES ("
                + ",".join("?" for _ in range(len(_EDGE_COLUMNS) + 1)) + ")",
                [(trip_id, *(row.get(c) for c in _EDGE_COLUMNS)) for row in edge_dicts],
            )
            if moment_dicts:
                conn.executemany(
                    "INSERT INTO trip_moments (trip_id," + ",".join(_MOMENT_COLUMNS) + ") VALUES ("
                    + ",".join("?" for _ in range(len(_MOMENT_COLUMNS) + 1)) + ")",
                    [(trip_id, *(row.get(c) for c in _MOMENT_COLUMNS)) for row in moment_dicts],
                )
            return trip_id


def moments_fingerprint(moments) -> str:
    """Hash the identity and values of moments independently of the road graph."""
    records = []
    for i in sorted(range(len(moments.id)), key=lambda k: int(moments.id[k])):
        records.append({
            "id": int(moments.id[i]),
            "type": str(moments.type[i]),
            "feature": None if moments.feature is None else moments.feature[i],
            "parent_feature": None if moments.group is None else moments.group[i],
            "lon": float(moments.lon[i]),
            "lat": float(moments.lat[i]),
            "m": float(moments.m[i]),
        })
    payload = json.dumps(records, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(("scenic-moments-v1\n" + payload).encode()).hexdigest()


def build_history_v1_payload(ctx: Context, source, *, metadata: TripMetadata | None = None) -> dict:
    """Build portable history_v1 metadata in memory without touching SQLite."""
    trip_dict, edge_dicts, moment_dicts = _prepare_trip_record(source, ctx=ctx, metadata=metadata)
    for name in ("id", "geometry_geojson", "created_at", "source_kind"):
        trip_dict.pop(name, None)
    payload = {
        "version": 1,
        "trip": trip_dict,
        "trip_edges": edge_dicts,
        "trip_moments": moment_dicts,
    }
    json.dumps(payload, allow_nan=False)
    return payload
