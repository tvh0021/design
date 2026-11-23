"""Comprehensive tests for trip history storage, matching, routing, CLI, and privacy."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pytest

from scenic.cli import main as cli_main
from scenic.history import (
    HistoryBias,
    HistoryDB,
    SCHEMA_VERSION,
    TripMetadata,
    _connection,
    _resolve_history_db,
    _transaction,
    build_history_v1_payload,
    is_history_active,
    moments_fingerprint,
    resolve_history_db,
    validate_history_penalty,
)
from scenic.loop import (
    LoopError,
    Steer,
    _LoopBuilder,
    _build_candidates,
    compute_reach,
    geojson as loop_geojson,
    loop_config,
    loops,
    result_json as loop_result_json,
)
from scenic.route import RoadGraph, Route
from scenic.search import (
    Context,
    Moments,
    ScenicRoute,
    _to5070,
    choose_picks,
    geojson as search_geojson,
    result_json as search_result_json,
    score_route,
    search,
)
from scenic.waypoints import maps_url

SEARCH_CFG = {
    "budgets": [1.15, 1.30, 1.50],
    "lambdas": [0.5, 1.0, 3.0, 8.0],
    "alt_lambdas": [1.0],
    "alt_rounds": 1,
    "reuse_penalty": 1.0,
    "via_lambda": 1.0,
    "via_top": 4,
    "via_top_per_band": 2,
    "via_max_moments": 2,
    "via_min_extra_min": 1.0,
    "moment_radius_m": 100.0,
    "moment_spacing_km": 2.0,
    "feature_spacing_km": 10.0,
    "moments_per_hour": 6.0,
    "moments_min_cap": 3,
    "moment_power": 2.0,
    "moment_minutes": 5.0,
    "pick_max_overlap": 0.8,
    "pick_min_score_frac": 0.85,
}

HIST_CFG = {
    "edge_penalty": 3.0,
    "moment_penalty": 3.0,
    "half_life_days": 180.0,
    "home_radius_m": 2000.0,
    "edge_match_m": 25.0,
    "moment_match_m": 250.0,
}

T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _latlon(g: RoadGraph, n: int) -> tuple[float, float]:
    return float(g.node_lat[n]), float(g.node_lon[n])


def _make_moments(g: RoadGraph, specs: list[tuple[int, str, str, float, str | None, str | None]]) -> Moments:
    if not specs:
        return Moments.empty(g.n_edges)
    edges = np.array([s[0] for s in specs], dtype=np.int64)
    u, v = g.u[edges], g.v[edges]
    mlon = 0.5 * (g.node_lon[u] + g.node_lon[v])
    mlat = 0.5 * (g.node_lat[u] + g.node_lat[v])
    mx, my = _to5070(mlon, mlat)
    of_edge = np.full(g.n_edges, -1, dtype=np.int32)
    for mid, e in enumerate(edges):
        of_edge[e] = mid
    return Moments(
        id=np.arange(len(specs), dtype=np.int64),
        m=np.array([s[3] for s in specs], dtype=np.float64),
        x=mx,
        y=my,
        lon=mlon,
        lat=mlat,
        edge=edges,
        type=np.array([s[1] for s in specs], dtype=object),
        label=np.array([s[2] for s in specs], dtype=object),
        of_edge=of_edge,
        feature=np.array([s[4] for s in specs], dtype=object),
        group=np.array([s[5] for s in specs], dtype=object),
    )


def _twin_corridor_ctx() -> Context:
    """Origin 0 -> Destination 4 with:
    - Fast center road: 0-1-2-3-4 (4 x 60s = 240s, S=0.2)
    - North scenic branch: 0-5-6-7-4 (30+120+120+30 = 300s, S=0.9, moment 0 on 6-7)
    - South scenic branch: 0-8-9-10-4 (30+120+120+30 = 300s, S=0.9, moment 1 on 9-10)
    Nodes spaced ~3.5 km apart so middle edges sit outside the 2 km start/end disks.
    """
    lon = [-77.00, -76.96, -76.92, -76.88, -76.84, -76.96, -76.92, -76.88, -76.96, -76.92, -76.88]
    lat = [43.00, 43.00, 43.00, 43.00, 43.00, 43.04, 43.04, 43.04, 42.96, 42.96, 42.96]
    u = [0, 1, 2, 3, 0, 5, 6, 7, 0, 8, 9, 10]
    v = [1, 2, 3, 4, 5, 6, 7, 4, 8, 9, 10, 4]
    t = [60.0, 60.0, 60.0, 60.0, 30.0, 120.0, 120.0, 30.0, 30.0, 120.0, 120.0, 30.0]
    L = [3300.0] * 12
    ways = [100 + i for i in range(12)]
    names = [f"Road {i}" for i in range(12)]
    hw = ["primary"] * 4 + ["secondary"] * 8
    table = pa.table({"u": u, "v": v, "way_id": ways, "length_m": L, "name": names, "ref": [None] * 12, "highway": hw})
    g = RoadGraph(lon, lat, u, v, [False] * 12, t, L, edges_table=table)
    S = np.array([0.2] * 4 + [0.9] * 4 + [0.9] * 4, dtype=np.float64)
    mom = _make_moments(
        g,
        [
            (6, "water", "North Lake", 0.9, "North Lake", "North Lake"),
            (10, "water", "South Lake", 0.9, "South Lake", "South Lake"),
        ],
    )
    return Context(g=g, S=S, moments=mom, cfg=dict(SEARCH_CFG))


def _make_grid_ctx(half: int = 3) -> tuple[Context, tuple[float, float], dict[tuple[int, int], int]]:
    """Symmetric 2D road grid centered at (43.0, -77.0), ~3.9 km per step (~3.5 min)."""
    lat0, lon0 = 43.0, -77.0
    dlat, dlon = 0.035, 0.048
    coords: dict[tuple[int, int], int] = {}
    lons: list[float] = []
    lats: list[float] = []
    for i in range(-half, half + 1):
        for j in range(-half, half + 1):
            coords[(i, j)] = len(lons)
            lats.append(lat0 + i * dlat)
            lons.append(lon0 + j * dlon)
    u_list, v_list, t_list, l_list = [], [], [], []
    for (i, j), u in coords.items():
        for di, dj in ((1, 0), (0, 1)):
            nb = (i + di, j + dj)
            if nb in coords:
                u_list.append(u)
                v_list.append(coords[nb])
                t_list.append(210.0)
                l_list.append(3900.0)
    n_edges = len(u_list)
    table = pa.table({
        "u": u_list,
        "v": v_list,
        "way_id": [1000 + e for e in range(n_edges)],
        "length_m": l_list,
        "name": [f"Grid Rd {e}" for e in range(n_edges)],
        "ref": [None] * n_edges,
        "highway": ["secondary"] * n_edges,
    })
    g = RoadGraph(lons, lats, u_list, v_list, [False] * n_edges, t_list, l_list, edges_table=table)
    S = np.full(n_edges, 0.80, dtype=np.float64)
    # Place symmetric water moments in N, S, E, W quadrants
    specs = []
    for idx, (a, b, feat) in enumerate([
        ((2, 0), (2, 1), "North Pond"),
        ((-2, 0), (-2, 1), "South Pond"),
        ((0, 2), (1, 2), "East Pond"),
        ((0, -2), (1, -2), "West Pond"),
    ]):
        ua, ub = coords[a], coords[b]
        for e in range(n_edges):
            if {u_list[e], v_list[e]} == {ua, ub}:
                specs.append((e, "water", feat, 0.9, feat, feat))
                break
    mom = _make_moments(g, specs)
    return Context(g=g, S=S, moments=mom, cfg=dict(SEARCH_CFG)), (lat0, lon0), coords


def _strip_seconds(obj):
    if isinstance(obj, dict):
        return {k: _strip_seconds(v) for k, v in obj.items() if k != "seconds"}
    if isinstance(obj, list):
        return [_strip_seconds(v) for v in obj]
    return obj


# --------------------------------------------------------------------------
# 1. Path resolution & configuration


def test_resolution_precedence_xdg_relative_and_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "xdg" / "scenic"
    cfg_dir.mkdir(parents=True)
    cfg = cfg_dir / "config.toml"
    cfg.write_text('[history]\ndb = "sub/routes.db"\n')

    # Precedence 1: explicit CLI path (relative to CWD)
    p, explicit = _resolve_history_db("cli.db", environ={"SCENIC_HISTORY_DB": "env.db", "XDG_CONFIG_HOME": str(tmp_path / "xdg")})
    assert p == (tmp_path / "cli.db").resolve() and explicit is True

    # Precedence 2: SCENIC_HISTORY_DB (relative to CWD, ignores config)
    p, explicit = _resolve_history_db(None, environ={"SCENIC_HISTORY_DB": "env.db", "XDG_CONFIG_HOME": str(tmp_path / "xdg")})
    assert p == (tmp_path / "env.db").resolve() and explicit is True

    # Precedence 3: XDG_CONFIG_HOME/scenic/config.toml (relative to config file dir)
    p, explicit = _resolve_history_db(None, environ={"XDG_CONFIG_HOME": str(tmp_path / "xdg")})
    assert p == (cfg_dir / "sub" / "routes.db").resolve() and explicit is True

    # Precedence 4: SCENIC_DATA fallback when environ is supplied
    data_root = tmp_path / "custom_data"
    p, explicit = _resolve_history_db(
        None,
        environ={"SCENIC_DATA": str(data_root), "XDG_CONFIG_HOME": str(tmp_path / "empty_xdg")},
    )
    assert p == (data_root / "history.db").resolve() and explicit is False

    # Empty SCENIC_HISTORY_DB falls through; whitespace-only is rejected
    p, explicit = _resolve_history_db(None, environ={"SCENIC_HISTORY_DB": "", "XDG_CONFIG_HOME": str(tmp_path / "xdg")})
    assert p == (cfg_dir / "sub" / "routes.db").resolve() and explicit is True
    with pytest.raises(ValueError):
        resolve_history_db(None, environ={"SCENIC_HISTORY_DB": "   "})
    with pytest.raises(ValueError):
        resolve_history_db("", environ={})
    with pytest.raises(ValueError):
        resolve_history_db("   ", environ={})

    # Malformed selected config fails, but override skips reading it
    cfg.write_text("not valid = [")
    with pytest.raises(ValueError):
        resolve_history_db(None, environ={"XDG_CONFIG_HOME": str(tmp_path / "xdg")})
    assert resolve_history_db("override.db", environ={"XDG_CONFIG_HOME": str(tmp_path / "xdg")}) == (tmp_path / "override.db").resolve()
    assert resolve_history_db(None, environ={"SCENIC_HISTORY_DB": "env.db", "XDG_CONFIG_HOME": str(tmp_path / "xdg")}) == (tmp_path / "env.db").resolve()

    # Empty history.db value in config rejected
    cfg.write_text('[history]\ndb = "   "\n')
    with pytest.raises(ValueError):
        resolve_history_db(None, environ={"XDG_CONFIG_HOME": str(tmp_path / "xdg")})


def test_validate_history_penalty_and_is_history_active():
    assert validate_history_penalty(0.0) == 0.0
    assert validate_history_penalty(3.0) == 3.0
    for bad in (-1.0, float("nan"), float("inf"), float("-inf"), 1e9, True):
        with pytest.raises(ValueError):
            validate_history_penalty(bad)

    assert is_history_active(None) is False
    zero = HistoryBias(np.zeros(3), np.zeros(2), 3.0, 3.0, {})
    assert is_history_active(zero, n_edges=3, n_moments=2) is False
    zero_pen = HistoryBias(np.ones(3), np.ones(2), 0.0, 0.0, {})
    assert is_history_active(zero_pen, n_edges=3, n_moments=2) is False
    active_e = HistoryBias(np.array([0.0, 0.5, 0.0]), np.zeros(2), 3.0, 0.0, {})
    assert is_history_active(active_e, n_edges=3, n_moments=2) is True
    active_m = HistoryBias(np.zeros(3), np.array([0.0, 0.2]), 0.0, 3.0, {})
    assert is_history_active(active_m, n_edges=3, n_moments=2) is True
    with pytest.raises(ValueError):
        is_history_active(active_e, n_edges=4, n_moments=2)
    with pytest.raises(ValueError):
        is_history_active(active_m, n_edges=3, n_moments=1)


# --------------------------------------------------------------------------
# 2. SQLite policy, schema versions, transactions, rollback, locks


def test_sqlite_pragmas_versions_rollback_cascade_and_lock_timeout(tmp_path):
    path = tmp_path / "history.db"
    db = HistoryDB(path)
    db.init_db()
    db.init_db()  # idempotent

    # PRAGMA readbacks and rollback on child failure
    with _connection(path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA locking_mode").fetchone()[0].lower() == "normal"
        assert conn.execute("PRAGMA mmap_size").fetchone()[0] == 0
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        with pytest.raises(sqlite3.IntegrityError):
            with _transaction(conn, write=True):
                conn.execute("INSERT INTO trip_edges (trip_id,seq) VALUES (999,0)")
        assert not conn.in_transaction
    assert not list(tmp_path.glob("*-wal"))
    assert not list(tmp_path.glob("*-shm"))

    # Cascading delete removes edges and moments
    ctx = _twin_corridor_ctx()
    res = search(ctx, _latlon(ctx.g, 0), _latlon(ctx.g, 4))
    tid = db.record_trip(res.picks[1.30], ctx=ctx, metadata=TripMetadata(taken_at=T0))
    assert len(db.get_trip(tid).edges) > 0
    assert len(db.get_trip(tid).moments) > 0
    assert db.delete_trip(tid) is True
    assert db.delete_trip(tid) is False
    with _connection(path) as conn:
        assert conn.execute("SELECT count(*) FROM trip_edges WHERE trip_id=?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM trip_moments WHERE trip_id=?", (tid,)).fetchone()[0] == 0

    # Lock timeout with small busy_timeout_ms fails quickly
    blocker = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, isolation_level=None)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        with _connection(path, busy_timeout_ms=50) as conn:
            with pytest.raises(sqlite3.OperationalError):
                with _transaction(conn, write=True):
                    conn.execute("DELETE FROM trips")
    finally:
        if blocker.in_transaction:
            blocker.execute("ROLLBACK")
        blocker.close()

    # Uninitialized version-0 DB rejected on read/write open (create=False)
    empty_v0 = tmp_path / "empty_v0.db"
    sqlite3.connect(empty_v0).close()
    with pytest.raises(ValueError, match="uninitialized"):
        with _connection(empty_v0):
            pass

    # Unrelated version-0 DB (has table) rejected by init_db
    unrelated = tmp_path / "unrelated.db"
    c = sqlite3.connect(unrelated)
    c.execute("CREATE TABLE other (x INTEGER)")
    c.commit()
    c.close()
    with pytest.raises(ValueError, match="not an empty history database"):
        HistoryDB(unrelated).init_db()

    # Newer schema version rejected on both create=False and init_db
    future = tmp_path / "future.db"
    c = sqlite3.connect(future)
    c.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    c.commit()
    c.close()
    with pytest.raises(ValueError, match="unsupported history schema version"):
        with _connection(future):
            pass
    with pytest.raises(ValueError, match="unsupported history schema version"):
        HistoryDB(future).init_db()

    # Unavailable explicit parent fails; default data dir parent can be created
    with pytest.raises(FileNotFoundError):
        HistoryDB(tmp_path / "missing_mount" / "history.db", require_parent=True).init_db()
    default_db = HistoryDB(tmp_path / "auto_created" / "history.db", require_parent=False)
    default_db.init_db()
    assert default_db.path.exists()


# --------------------------------------------------------------------------
# 3. Recording, repeated traversals, validation, duplicate preservation


def test_recording_repeated_traversals_reversed_validation_and_duplicates(tmp_path):
    table = pa.table({"u": [0, 1], "v": [1, 2], "way_id": [10, 20], "length_m": [800.0, 900.0],
                      "name": [None, "Main St"], "ref": ["NY 1", None], "highway": ["secondary", "tertiary"]})
    g = RoadGraph([-74.0, -73.99, -73.98], [42.0, 42.0, 42.0], [0, 1], [1, 2], [False, False], [60.0, 70.0], [800.0, 900.0], edges_table=table)
    # Out-and-back repeated traversal: 0 -> 1 -> 0
    nodes = np.array([0, 1, 0])
    edges = np.array([0, 0])
    rev = np.array([False, True])
    route = Route(nodes, edges, rev, 120.0, 1600.0, 120.0, g._geometry(nodes, edges, rev))

    db = HistoryDB(tmp_path / "history.db")
    t1 = db.record_trip(route, graph=g, metadata=TripMetadata(taken_at=T0, title="Out and back", rating=5))
    t2 = db.record_trip(route, graph=g, metadata=TripMetadata(taken_at=T0 + timedelta(days=1), title="Repeat", rating=3))
    assert t1 != t2

    rec = db.get_trip(t1)
    assert rec.trip["scenic_min"] is None and rec.trip["score"] is None
    assert rec.moments == []
    assert len(rec.edges) == 2
    assert rec.edges[0]["reversed"] == 0 and rec.edges[0]["geom_start"] == 0 and rec.edges[0]["geom_end"] == 1
    assert rec.edges[1]["reversed"] == 1 and rec.edges[1]["geom_start"] == 1 and rec.edges[1]["geom_end"] == 2
    assert rec.edges[0]["road_name"] == "NY 1"  # falls back to ref when name is None

    st = db.stats()
    assert st.trip_count == 2
    assert st.by_mode == {"custom": 2}
    assert st.average_rating == pytest.approx(4.0)

    # list_trips filtering
    assert len(db.list_trips(since=T0 + timedelta(hours=12))) == 1
    assert len(db.list_trips(mode="loop")) == 0

    # Validation errors: naive timestamp, NaN metadata, invalid rating
    with pytest.raises(ValueError):
        db.record_trip(route, graph=g, metadata=TripMetadata(taken_at=datetime(2026, 1, 1)))
    with pytest.raises(ValueError):
        db.record_trip(route, graph=g, metadata=TripMetadata(metadata={"bad": float("nan")}))
    with pytest.raises(ValueError):
        db.record_trip(route, graph=g, metadata=TripMetadata(rating=6))


# --------------------------------------------------------------------------
# 4. Imports: legacy GeoJSON, history_v1 round-trip, URLs, waypoints


def test_legacy_geojson_preserves_credited_moments_and_validates(tmp_path):
    ctx = _twin_corridor_ctx()
    res = search(ctx, _latlon(ctx.g, 0), _latlon(ctx.g, 4))
    doc = search_geojson(ctx, res)  # legacy export: no history_v1
    for f in doc["features"]:
        assert "history_v1" not in f["properties"]
    path = tmp_path / "legacy.geojson"
    path.write_text(json.dumps(doc))

    db = HistoryDB(tmp_path / "history.db")
    # Ambiguous without --pick
    with pytest.raises(ValueError, match="--pick"):
        db.record_trip(path, ctx=ctx)

    # Pick +30% with ctx -> preserves credited moments!
    tid = db.record_trip(path, ctx=ctx, pick="+30%", metadata=TripMetadata(taken_at=T0))
    rec = db.get_trip(tid)
    assert rec.trip["source_kind"] == "geojson"
    assert rec.trip["mode"] == "route"
    assert [m["moment_id"] for m in rec.moments] == res.picks[1.30].moments
    assert len(rec.moments) == 1

    # Tampered node_path rejected
    bad_doc = json.loads(path.read_text())
    for f in bad_doc["features"]:
        if f["properties"].get("role") == "+30%":
            f["properties"]["node_path"] = list(reversed(f["properties"]["node_path"]))
    bad_path = tmp_path / "bad.geojson"
    bad_path.write_text(json.dumps(bad_doc))
    with pytest.raises(ValueError):
        db.record_trip(bad_path, ctx=ctx, pick="+30%")


def test_history_v1_round_trip_without_graph_and_preserves_metadata(tmp_path, monkeypatch):
    ctx = _twin_corridor_ctx()
    res = search(ctx, _latlon(ctx.g, 0), _latlon(ctx.g, 4))

    # Ensure build_history_v1_payload runs purely in memory without touching sqlite3.connect
    def fail_connect(*args, **kwargs):
        raise AssertionError("build_history_v1_payload must not open SQLite")

    monkeypatch.setattr(sqlite3, "connect", fail_connect)
    payload = build_history_v1_payload(ctx, res.picks[1.30], metadata=TripMetadata(taken_at=T0, title="In-memory"))
    assert payload["version"] == 1
    assert payload["trip"]["title"] == "In-memory"
    assert len(payload["trip_moments"]) == 1
    monkeypatch.undo()

    # Export with include_history=True and import without graph/ctx
    doc = search_geojson(ctx, res, include_history=True)
    export_file = tmp_path / "ready.geojson"
    export_file.write_text(json.dumps(doc))
    db1 = HistoryDB(tmp_path / "db1.db")
    tid1 = db1.record_trip(
        export_file,
        pick="+30%",
        metadata=TripMetadata(
            taken_at=T0,
            title="Sunday Lake Run",
            rating=5,
            notes="Great foliage",
            origin_label="Home",
            destination_label="Town",
        ),
    )

    # Export from db1 and re-import into db2 with default TripMetadata() -> must preserve exported metadata!
    exported = tmp_path / "trip_export.geojson"
    exported.write_text(json.dumps(db1.export_trip_geojson(tid1)))
    db2 = HistoryDB(tmp_path / "db2.db")
    tid2 = db2.record_trip(exported, metadata=TripMetadata())
    rec2 = db2.get_trip(tid2)
    assert rec2.trip["title"] == "Sunday Lake Run"
    assert rec2.trip["rating"] == 5
    assert rec2.trip["notes"] == "Great foliage"
    assert rec2.trip["origin_label"] == "Home"
    assert rec2.trip["destination_label"] == "Town"
    assert len(rec2.moments) == 1

    # Explicit override replaces only specified fields
    tid3 = db2.record_trip(exported, metadata=TripMetadata(rating=4))
    rec3 = db2.get_trip(tid3)
    assert rec3.trip["title"] == "Sunday Lake Run"
    assert rec3.trip["rating"] == 4


def test_google_maps_url_and_waypoints_import_and_rejection(tmp_path):
    ctx = _twin_corridor_ctx()
    g = ctx.g
    db = HistoryDB(tmp_path / "history.db")
    p0, p2, p4 = _latlon(g, 0), _latlon(g, 2), _latlon(g, 4)

    # Valid URL with intermediate waypoint
    url = maps_url(p0, p4, [p2])
    tid = db.record_trip(url, ctx=ctx, metadata=TripMetadata(taken_at=T0))
    rec = db.get_trip(tid)
    assert rec.trip["source_kind"] == "google_maps"
    assert rec.metadata["routing"] == "local_fastest"
    assert rec.trip["google_maps_url"] == url

    # Valid waypoint sequence
    tid_wp = db.record_trip([p0, p4], graph=g, metadata=TripMetadata(taken_at=T0))
    assert db.get_trip(tid_wp).trip["source_kind"] == "waypoints"

    # Reject short URLs, addresses, missing origin, unsupported params, walking mode
    for bad_url in (
        "https://maps.app.goo.gl/abc",
        "https://www.google.com/maps/dir/?api=1&origin=Rochester,NY&destination=43.0,-76.84",
        "https://www.google.com/maps/dir/?api=1&destination=43.0,-76.84",
        "https://www.google.com/maps/dir/?api=1&origin=43.0,-77.0&destination=43.0,-76.84&travelmode=walking",
        "https://www.google.com/maps/dir/?api=1&origin=43.0,-77.0&destination=43.0,-76.84&extra=1",
    ):
        with pytest.raises(ValueError):
            db.record_trip(bad_url, graph=g)

    # Disconnected waypoint leg aborts entire recording
    g_disc = RoadGraph(
        [-77.0, -76.99, -76.95, -76.94],
        [43.0, 43.0, 43.0, 43.0],
        [0, 2],
        [1, 3],
        [False, False],
        [60.0, 60.0],
        [800.0, 800.0],
    )
    with pytest.raises(ValueError, match="unreachable"):
        db.record_trip([(43.0, -77.0), (43.0, -76.99), (43.0, -76.94)], graph=g_disc)
    # Distant waypoint > 2000 m rejected
    with pytest.raises(ValueError, match="2000 m"):
        db.record_trip([(43.0, -77.0), (44.0, -77.0)], graph=g)


# --------------------------------------------------------------------------
# 5. Edge matching (fast path, rebuild step A, rebuild step B, exemptions)


def test_edge_matching_fast_rebuild_split_merge_ambiguous_and_exemptions(tmp_path):
    # Original graph: one 1000 m east-west road (way_id=500)
    t_orig = pa.table({"u": [0], "v": [1], "way_id": [500], "length_m": [1000.0]})
    g_orig = RoadGraph([-77.00, -76.9877], [43.00, 43.00], [0], [1], [False], [60.0], [1000.0], edges_table=t_orig)
    db = HistoryDB(tmp_path / "history.db")
    # Record reversed travel; also test future trip ignored by as_of
    db.record_trip(g_orig.route(1, 0), graph=g_orig, metadata=TripMetadata(taken_at=T0))
    db.record_trip(g_orig.route(0, 1), graph=g_orig, metadata=TripMetadata(taken_at=T0 + timedelta(days=10)))

    ctx_orig = SimpleNamespace(g=g_orig, moments=Moments.empty(g_orig.n_edges))

    # Fast path: exact match, reversed travel counts, decay at half-life = 0.5, future trip ignored
    hits_now = db.driven_edges(ctx_orig, as_of=T0)
    assert hits_now[0].trip_count == 1 and hits_now[0].decayed_visits == pytest.approx(1.0)
    hits_hl = db.driven_edges(ctx_orig, as_of=T0 + timedelta(days=180), half_life_days=180.0)
    # Includes both trips (T0 at 180d -> 0.5, T0+10d at 170d -> 2^(-170/180))
    assert hits_hl[0].trip_count == 2
    assert hits_hl[0].decayed_visits == pytest.approx(0.5 + 2.0 ** (-170.0 / 180.0), rel=1e-4)

    # Same fingerprint but shifted coordinates beyond edge_match_m -> rejected!
    g_shifted = RoadGraph([-77.00, -76.9877], [43.01, 43.01], [0], [1], [False], [60.0], [1000.0], edges_table=t_orig)
    assert db.driven_edges(SimpleNamespace(g=g_shifted, moments=Moments.empty(1)), as_of=T0) == {}

    # Rebuild step A: shuffled row index + reversed orientation + distant segment with same way_id
    t_shuf = pa.table({"u": [2, 1], "v": [3, 0], "way_id": [500, 500], "length_m": [1000.0, 1000.0]})
    g_shuf = RoadGraph(
        [-77.00, -76.9877, -76.90, -76.8877],
        [43.00, 43.00, 43.05, 43.05],
        [2, 1],
        [3, 0],
        [False, False],
        [60.0, 60.0],
        [1000.0, 1000.0],
        edges_table=t_shuf,
    )
    hits_shuf = db.driven_edges(SimpleNamespace(g=g_shuf, moments=Moments.empty(2)), as_of=T0)
    assert set(hits_shuf) == {1}  # only the matching segment, NOT the distant segment on way 500
    assert hits_shuf[1].decayed_visits == pytest.approx(1.0)

    # Rebuild step B (split): original 1000 m edge split into two 500 m edges on way 500
    t_split = pa.table({"u": [0, 1], "v": [1, 2], "way_id": [500, 500], "length_m": [500.0, 500.0]})
    g_split = RoadGraph(
        [-77.00, -76.99385, -76.9877],
        [43.00, 43.00, 43.00],
        [0, 1],
        [1, 2],
        [False, False],
        [30.0, 30.0],
        [500.0, 500.0],
        edges_table=t_split,
    )
    hits_split = db.driven_edges(SimpleNamespace(g=g_split, moments=Moments.empty(2)), as_of=T0)
    assert set(hits_split) == {0, 1}
    assert hits_split[0].decayed_visits == pytest.approx(1.0, abs=0.02)
    assert hits_split[1].decayed_visits == pytest.approx(1.0, abs=0.02)

    # Rebuild step B (merged): current edge is 2000 m covering original 1000 m -> ~0.5 fractional coverage
    t_merge = pa.table({"u": [0], "v": [1], "way_id": [500], "length_m": [2000.0]})
    g_merge = RoadGraph([-77.00, -76.9754], [43.00, 43.00], [0], [1], [False], [120.0], [2000.0], edges_table=t_merge)
    hits_merge = db.driven_edges(SimpleNamespace(g=g_merge, moments=Moments.empty(1)), as_of=T0)
    assert set(hits_merge) == {0}
    assert 0.45 < hits_merge[0].decayed_visits < 0.55

    # Perpendicular crossing on same way_id (90 deg tangent mismatch) -> rejected!
    t_cross = pa.table({"u": [0], "v": [1], "way_id": [500], "length_m": [1000.0]})
    g_cross = RoadGraph([-76.99385, -76.99385], [42.995, 43.005], [0], [1], [False], [60.0], [1000.0], edges_table=t_cross)
    assert db.driven_edges(SimpleNamespace(g=g_cross, moments=Moments.empty(1)), as_of=T0) == {}

    # Ambiguous parallel segments (2 m apart, both within 5 m) -> BOTH rejected!
    # 0.000018 deg lat ~ 2.0 m
    t_par = pa.table({"u": [0, 2], "v": [1, 3], "way_id": [500, 500], "length_m": [2000.0, 2000.0]})
    g_par_ambig = RoadGraph(
        [-77.00, -76.9754, -77.00, -76.9754],
        [43.00, 43.00, 43.000018, 43.000018],
        [0, 2],
        [1, 3],
        [False, False],
        [120.0, 120.0],
        [2000.0, 2000.0],
        edges_table=t_par,
    )
    assert db.driven_edges(SimpleNamespace(g=g_par_ambig, moments=Moments.empty(2)), as_of=T0) == {}

    # Non-ambiguous parallel segments (0 m vs 15 m -> 15 > 0 + 5) -> closer edge 0 wins!
    # 0.000135 deg lat ~ 15.0 m
    g_par_clear = RoadGraph(
        [-77.00, -76.9754, -77.00, -76.9754],
        [43.00, 43.00, 43.000135, 43.000135],
        [0, 2],
        [1, 3],
        [False, False],
        [120.0, 120.0],
        [2000.0, 2000.0],
        edges_table=t_par,
    )
    hits_clear = db.driven_edges(SimpleNamespace(g=g_par_clear, moments=Moments.empty(2)), as_of=T0)
    assert set(hits_clear) == {0}

    # Missing way_id -> unmatched, reported in diagnostics
    t_other = pa.table({"u": [0], "v": [1], "way_id": [999], "length_m": [1000.0]})
    g_other = RoadGraph([-77.00, -76.9877], [43.00, 43.00], [0], [1], [False], [60.0], [1000.0], edges_table=t_other)
    ctx_other = SimpleNamespace(g=g_other, moments=Moments.empty(1))
    bias_other = db.bias(ctx_other, start=(43.0, -77.0), end=(43.0, -76.9877), cfg={**HIST_CFG, "home_radius_m": 0.0}, as_of=T0)
    assert bias_other.diagnostics["matched_segments"] == 0
    assert bias_other.diagnostics["unmatched_segments"] == 1
    assert bias_other.diagnostics["trip_count"] == 1
    assert bias_other.diagnostics["half_life_days"] == 180.0

    # Partial and full start/end exemptions
    partial = db.driven_edges(ctx_orig, as_of=T0, exempt_centers=[(43.0, -77.0)], exempt_radius_m=300.0)
    assert 0.5 < partial[0].decayed_visits < 0.85
    full_exempt = db.driven_edges(ctx_orig, as_of=T0, exempt_centers=[(43.0, -77.0)], exempt_radius_m=2000.0)
    assert full_exempt == {}


# --------------------------------------------------------------------------
# 6. Moment matching & bias single-read diagnostics


def test_moment_matching_rebuilds_parent_fallback_unnamed_ambiguity_and_exempt_guard(tmp_path):
    table = pa.table({"u": [0, 1, 2], "v": [1, 2, 3], "way_id": [10, 20, 30], "length_m": [1000.0] * 3})
    g = RoadGraph([-77.00, -76.988, -76.976, -76.964], [43.0, 43.0, 43.0, 43.0],
                  [0, 1, 2], [1, 2, 3], [False] * 3, [60.0] * 3, [1000.0] * 3, edges_table=table)
    # Three moments: named bay on Lake George, distant Lake George viewpoint, unnamed vista
    mom_orig = _make_moments(
        g,
        [
            (0, "water", "Boon Bay", 0.9, "Boon Bay", "Lake George"),
            (2, "vista", "Ridge View", 0.8, None, None),
        ],
    )
    ctx_orig = Context(g=g, S=np.full(3, 0.8), moments=mom_orig, cfg=dict(SEARCH_CFG))
    route = g.route(0, 3)
    sr = ScenicRoute(route=route, origin="test", moments=[0, 1], passed=[0, 1], scenic_min=1.0, score=5.0)
    db = HistoryDB(tmp_path / "history.db")
    db.record_trip(sr, ctx=ctx_orig, metadata=TripMetadata(taken_at=T0))

    # 1) Matching fingerprint -> O(1) lookup, and exempt_radius_m=0 does NOT exempt moment at center
    m0_center = (float(mom_orig.lat[0]), float(mom_orig.lon[0]))
    hits_zero_r = db.visited_moments(ctx_orig, as_of=T0, exempt_centers=[m0_center], exempt_radius_m=0.0)
    assert set(hits_zero_r) == {0, 1}
    hits_pos_r = db.visited_moments(ctx_orig, as_of=T0, exempt_centers=[m0_center], exempt_radius_m=50.0)
    assert set(hits_pos_r) == {1}

    # 2) Changed moment IDs with unchanged road graph:
    #    - moment 0 -> shifted to id 10, specific feature missing -> matches via parent_feature="Lake George"
    #    - moment 11: different specific bay "Huddle Bay" on "Lake George" nearby -> rejected (specific feature differs)
    #    - moment 12: distant "Lake George" viewpoint (> 250 m away) -> NOT marked visited!
    #    - moment 13: unnamed vista shifted 30 m -> matches old unnamed vista
    mx, my = _to5070(
        [float(mom_orig.lon[0]), float(mom_orig.lon[0]) + 0.0001, float(mom_orig.lon[0]) + 0.01, float(mom_orig.lon[1]) + 0.0003],
        [float(mom_orig.lat[0]), float(mom_orig.lat[0]), float(mom_orig.lat[0]), float(mom_orig.lat[1])],
    )
    mom_rebuilt = Moments(
        id=np.array([10, 11, 12, 13], dtype=np.int64),
        m=np.array([0.9, 0.9, 0.9, 0.8]),
        x=mx,
        y=my,
        lon=np.array([float(mom_orig.lon[0]), float(mom_orig.lon[0]) + 0.0001, float(mom_orig.lon[0]) + 0.01, float(mom_orig.lon[1]) + 0.0003]),
        lat=np.array([float(mom_orig.lat[0]), float(mom_orig.lat[0]), float(mom_orig.lat[0]), float(mom_orig.lat[1])]),
        edge=np.array([0, 0, 1, 2], dtype=np.int64),
        type=np.array(["water", "water", "water", "vista"], dtype=object),
        label=np.array(["Lake George", "Huddle Bay", "Far Lake George", "Ridge View"], dtype=object),
        of_edge=np.array([10, 12, 13], dtype=np.int32),
        feature=np.array([None, "Huddle Bay", "Lake George", None], dtype=object),
        group=np.array(["Lake George", "Lake George", "Lake George", None], dtype=object),
    )
    assert moments_fingerprint(mom_rebuilt) != moments_fingerprint(mom_orig)
    ctx_rebuilt = SimpleNamespace(g=g, moments=mom_rebuilt)
    hits_reb = db.visited_moments(ctx_rebuilt, as_of=T0)
    assert set(hits_reb) == {10, 13}

    # 3) Ambiguous unnamed matches (two equidistant unnamed vistas 20 m east & west) -> rejected!
    mx2, my2 = _to5070([float(mom_orig.lon[1]) - 0.0002, float(mom_orig.lon[1]) + 0.0002], [43.0, 43.0])
    mom_ambig = Moments(
        id=np.array([0, 1], dtype=np.int64),
        m=np.array([0.8, 0.8]),
        x=mx2,
        y=my2,
        lon=np.array([float(mom_orig.lon[1]) - 0.0002, float(mom_orig.lon[1]) + 0.0002]),
        lat=np.array([43.0, 43.0]),
        edge=np.array([2, 2], dtype=np.int64),
        type=np.array(["vista", "vista"], dtype=object),
        label=np.array(["V1", "V2"], dtype=object),
        of_edge=np.array([-1, -1, 0], dtype=np.int32),
        feature=np.array([None, None], dtype=object),
        group=np.array([None, None], dtype=object),
    )
    assert db.visited_moments(SimpleNamespace(g=g, moments=mom_ambig), as_of=T0) == {}

    # 4) bias() executes a single _history_rows call and fills all diagnostics
    calls = 0
    orig_rows = db._history_rows

    def counted_rows(*args, **kwargs):
        nonlocal calls
        calls += 1
        return orig_rows(*args, **kwargs)

    db._history_rows = counted_rows
    b = db.bias(ctx_rebuilt, start=_latlon(g, 0), end=_latlon(g, 3), cfg={**HIST_CFG, "home_radius_m": 0.0}, as_of=T0)
    assert calls == 1
    for key in (
        "matched_segments", "unmatched_segments", "unmatched_moments",
        "trip_count", "exempt_radius_m", "edge_penalty", "moment_penalty", "half_life_days",
    ):
        assert key in b.diagnostics


# --------------------------------------------------------------------------
# 7. Search & Loop routing, scoring formulas, exemptions, fallback, zero-impact


def test_search_scoring_tiebreaker_avoidance_and_zero_impact(tmp_path):
    ctx = _twin_corridor_ctx()
    a, b = _latlon(ctx.g, 0), _latlon(ctx.g, 4)

    # Baseline (no history) vs all-zero HistoryBias must be identical
    base = search(ctx, a, b)
    zero_bias = HistoryBias(np.zeros(ctx.g.n_edges), np.zeros(len(ctx.moments.id)), 3.0, 3.0, {"trip_count": 0})
    with_zero = search(ctx, a, b, history=zero_bias)
    assert base.history is None and with_zero.history is None
    assert _strip_seconds(search_result_json(ctx, base)) == _strip_seconds(search_result_json(ctx, with_zero))
    assert search_geojson(ctx, base) == search_geojson(ctx, with_zero)
    assert "history" not in search_result_json(ctx, base)

    # Record whichever branch +30% picked originally (north: edges 4,5,6,7 or south: 8,9,10,11)
    first_pick = base.picks[1.30]
    first_nodes = list(first_pick.nodes)
    db = HistoryDB(tmp_path / "history.db")
    db.record_trip(first_pick, ctx=ctx, metadata=TripMetadata(taken_at=T0))

    bias = db.bias(ctx, start=a, end=b, cfg=HIST_CFG, as_of=T0)
    avoided = search(ctx, a, b, history=bias)
    assert avoided.t0 == pytest.approx(base.t0)
    assert avoided.s_fast == pytest.approx(base.s_fast)
    assert list(avoided.fastest.nodes) == [0, 1, 2, 3, 4]
    assert list(avoided.picks[1.30].nodes) != first_nodes
    assert avoided.picks[1.30].history["history_exposure"] == pytest.approx(0.0)
    assert avoided.history is not None
    assert "history" in search_result_json(ctx, avoided)
    gj = search_geojson(ctx, avoided)
    assert all("history_v1" in f["properties"] for f in gj["features"] if f["geometry"]["type"] == "LineString")

    # Verify exact score_route formula
    sr_check = ScenicRoute(route=first_pick.route, origin="check")
    score_route(sr_check, ctx, base.t0, base.s_fast, history=bias)
    assert sr_check.scenic_min == pytest.approx(first_pick.scenic_min)
    assert sr_check.mean_S == pytest.approx(first_pick.mean_S)
    assert sr_check.passed == first_pick.passed
    e = sr_check.route.edges
    t_e = ctx.g.time_s[e]
    H_e = bias.edge_strength[e]
    R = float(np.sum(t_e * H_e) / np.sum(t_e))
    H_m = bias.moment_strength[sr_check.moments]
    q_m = 1.0 / (1.0 + bias.moment_penalty * H_m)
    expected_mpts = float(np.sum((ctx.moments.m[sr_check.moments] ** 2.0) * q_m))
    expected_score = (sr_check.scenic_min + ctx.cfg["moment_minutes"] * expected_mpts) / (1.0 + bias.edge_penalty * R)
    assert sr_check.moment_pts == pytest.approx(expected_mpts)
    assert sr_check.score == pytest.approx(expected_score)
    assert sr_check.history["raw_score"] == pytest.approx(first_pick.score)
    assert sr_check.history["history_exposure"] == pytest.approx(R)
    assert sr_check.history["repeated_km"] > 0.0

    # Equal-score tie-breaker prefers smaller R only when use_history=True
    r1 = ScenicRoute(
        route=Route(np.array([0, 1]), np.array([0]), np.array([False]), 100.0, 1000.0, 100.0),
        origin="r1", ratio=1.1, score=5.0,
        history={"raw_score": 5.0, "raw_moment_points": 0.0, "history_exposure": 0.8, "repeated_km": 1.0},
    )
    r2 = ScenicRoute(
        route=Route(np.array([0, 2]), np.array([1]), np.array([False]), 110.0, 1000.0, 110.0),
        origin="r2", ratio=1.1, score=5.0,
        history={"raw_score": 5.0, "raw_moment_points": 0.0, "history_exposure": 0.1, "repeated_km": 0.1},
    )
    picks_no_h, _ = choose_picks([r1, r2], [1.15], np.array([1000.0, 1000.0]), use_history=False)
    picks_with_h, _ = choose_picks([r1, r2], [1.15], np.array([1000.0, 1000.0]), use_history=True)
    assert picks_no_h[1.15] is r1  # shorter duration wins without history
    assert picks_with_h[1.15] is r2  # smaller R wins with history

    # Fully visited graph (all edges strength=1) still returns feasible routes & lambda=0 distinct from pure-time T
    full_bias = HistoryBias(np.ones(ctx.g.n_edges), np.ones(len(ctx.moments.id)), 5.0, 5.0, {"trip_count": 3})
    ctx_lam0 = Context(g=ctx.g, S=ctx.S, moments=ctx.moments, cfg={**SEARCH_CFG, "via_lambda": 0.0, "lambdas": [0.0, 1.0]})
    res_full = search(ctx_lam0, a, b, history=full_bias)
    assert res_full is not None
    assert res_full.t0 == pytest.approx(base.t0)
    assert all(sr.ratio <= 1.50 + 1e-9 for sr in res_full.kept)
    assert list(res_full.fastest.nodes) == [0, 1, 2, 3, 4]


def test_loops_avoidance_zero_impact_pure_time_g0_and_single_fallback(tmp_path, monkeypatch):
    ctx, start, _ = _make_grid_ctx(half=3)
    lcfg = loop_config({"n_loops": 2, "lambdas": [3.0, 1.0, 0.0]})

    # Zero-impact: None vs all-zero HistoryBias
    base = loops(ctx, start, 45, Steer(mode="any"), cfg=lcfg)
    zero = HistoryBias(np.zeros(ctx.g.n_edges), np.zeros(len(ctx.moments.id)), 3.0, 3.0, {"trip_count": 0})
    with_zero = loops(ctx, start, 45, Steer(mode="any"), cfg=lcfg, history=zero)
    assert base.history is None and with_zero.history is None
    assert base.history_fallback is False and with_zero.history_fallback is False
    assert _strip_seconds(loop_result_json(ctx, base)) == _strip_seconds(loop_result_json(ctx, with_zero))
    assert loop_geojson(ctx, base) == loop_geojson(ctx, with_zero)
    assert "history" not in loop_result_json(ctx, base)
    assert "history_fallback" not in loop_result_json(ctx, base)

    # _LoopBuilder keeps G0 pure-time while graphs[0.0] carries history costs
    reach = compute_reach(ctx.g, start, 45 * 60.0, lcfg)
    full_bias = HistoryBias(np.ones(ctx.g.n_edges), np.ones(len(ctx.moments.id)), 4.0, 4.0, {"trip_count": 1})
    vias_h, reg_e, mid_t, nx5, ny5 = _build_candidates(ctx, reach, lcfg, history=full_bias)
    vias_0, _, _, _, _ = _build_candidates(ctx, reach, lcfg, history=None)
    assert all(vh.value < v0.value for vh, v0 in zip(vias_h, vias_0))
    builder = _LoopBuilder(ctx, reach, lcfg, reg_e, mid_t, nx5, ny5, history=full_bias)
    assert builder.G0 is not builder.graphs[0.0]
    assert np.allclose(builder.graphs[0.0].fwd.data, builder.G0.fwd.data * 5.0)

    # Record the top loop and verify avoidance shifts pick #1 away from it
    db = HistoryDB(tmp_path / "loops.db")
    db.record_trip(base.picks[0], ctx=ctx, metadata=TripMetadata(taken_at=T0))
    bias = db.bias(ctx, start=start, end=start, cfg=HIST_CFG, as_of=T0)
    avoided = loops(ctx, start, 45, Steer(mode="any"), cfg=lcfg, history=bias)
    assert avoided.history is not None
    assert avoided.history_fallback is False
    assert list(avoided.picks[0].edges) != list(base.picks[0].edges)
    rj = loop_result_json(ctx, avoided)
    assert "history" in rj and rj["history_fallback"] is False
    gj = loop_geojson(ctx, avoided)
    assert all("history_v1" in f["properties"] for f in gj["features"] if f["geometry"]["type"] == "LineString")

    # Compass and anchor rules still hold under history
    comp = loops(ctx, start, 45, Steer(mode="compass", direction="N"), cfg=lcfg, history=bias)
    assert len(comp.picks) >= 1 and comp.picks[0].off_deg is not None
    anc = loops(ctx, start, 45, Steer(mode="anchor", feature="North Pond"), cfg=lcfg, history=bias)
    assert len(anc.picks) >= 1 and 0 in anc.picks[0].passed

    # Fallback: if history pass fails to build any valid loop, retry ONCE with unpenalized routing
    import scenic.loop as loop_mod

    pass_calls: list[bool] = []
    real_pass = loop_mod._run_loop_pass

    def fail_first_pass(*args, **kwargs):
        has_routing_hist = kwargs.get("routing_history") is not None
        pass_calls.append(has_routing_hist)
        if has_routing_hist:
            raise LoopError("no_loop", "forced history routing failure")
        return real_pass(*args, **kwargs)

    monkeypatch.setattr(loop_mod, "_run_loop_pass", fail_first_pass)
    fb = loops(ctx, start, 45, Steer(mode="any"), cfg=lcfg, history=bias)
    assert pass_calls == [True, False]
    assert fb.history_fallback is True
    assert fb.picks[0].history is not None
    assert loop_result_json(ctx, fb)["history_fallback"] is True

    # Retry happens AT MOST once (if unpenalized also raises, propagates without looping)
    pass_calls.clear()

    def always_fail(*args, **kwargs):
        pass_calls.append(kwargs.get("routing_history") is not None)
        raise LoopError("no_loop", "no loop at all")

    monkeypatch.setattr(loop_mod, "_run_loop_pass", always_fail)
    with pytest.raises(LoopError):
        loops(ctx, start, 45, Steer(mode="any"), cfg=lcfg, history=bias)
    assert pass_calls == [True, False]


# --------------------------------------------------------------------------
# 8. CLI integration & zero-touch guarantee when avoidance is disabled


def test_cli_history_subcommands_and_route_loop_flags(tmp_path, monkeypatch, capsys):
    import scenic.history as hist_mod
    import scenic.search as search_mod
    import scenic.setup_data as setup_mod

    ctx = _twin_corridor_ctx()
    grid_ctx, grid_start, _ = _make_grid_ctx(half=3)
    db_path = tmp_path / "cli_history.db"

    # CLI history init / add / list / show / stats / export / delete (no network, no graph for init/list/show/stats/export/delete)
    assert cli_main(["history", "init", "--history-db", str(db_path)]) == 0
    assert db_path.exists()

    res = search(ctx, _latlon(ctx.g, 0), _latlon(ctx.g, 4))
    ready_gj = tmp_path / "ready.geojson"
    ready_gj.write_text(json.dumps(search_geojson(ctx, res, include_history=True)))
    assert cli_main([
        "history", "add", "--history-db", str(db_path), "--geojson", str(ready_gj),
        "--pick", "+30%", "--title", "CLI Trip", "--rating", "5", "--taken-at", "2026-06-01T12:00:00Z",
    ]) == 0

    capsys.readouterr()
    assert cli_main(["history", "list", "--history-db", str(db_path), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 1 and listed[0]["title"] == "CLI Trip"

    assert cli_main(["history", "show", "1", "--history-db", str(db_path), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["trip"]["rating"] == 5

    assert cli_main(["history", "stats", "--history-db", str(db_path), "--json"]) == 0
    stats_out = json.loads(capsys.readouterr().out)
    assert stats_out["trip_count"] == 1

    exp_path = tmp_path / "exported.geojson"
    assert cli_main(["history", "export", "1", "--geojson", str(exp_path), "--history-db", str(db_path)]) == 0
    assert "history_v1" in json.loads(exp_path.read_text())["features"][0]["properties"]

    # Missing DB under --avoid-history fails cleanly with actionable message
    missing_db = tmp_path / "does_not_exist.db"
    capsys.readouterr()
    rc = cli_main([
        "route", "--from", "43.0,-77.0", "--to", "43.0,-76.84",
        "--avoid-history", "--history-db", str(missing_db),
    ])
    err = capsys.readouterr().err
    assert rc == 1 and "scenic history init" in err

    # Invalid --history-penalty fails cleanly before loading data
    for bad_pen in ("-1", "nan", "inf", "1e9"):
        capsys.readouterr()
        rc = cli_main([
            "route", "--from", "43.0,-77.0", "--to", "43.0,-76.84",
            "--avoid-history", "--history-db", str(db_path), "--history-penalty", bad_pen,
        ])
        assert rc == 1 and capsys.readouterr().err

    # When --avoid-history is UNSET: must NOT touch _resolve_history_db, SQLite, or fingerprints!
    monkeypatch.setattr(setup_mod, "missing_serve_files", lambda g, s: (Path("g"), Path("s"), []))
    monkeypatch.setattr(search_mod.Context, "load", classmethod(lambda cls, g=None, s=None: ctx))

    def forbid(*args, **kwargs):
        raise AssertionError("history must not be touched when --avoid-history is unset")

    monkeypatch.setattr(hist_mod, "_resolve_history_db", forbid)
    monkeypatch.setattr(sqlite3, "connect", forbid)
    out_gj = tmp_path / "plain.geojson"
    out_js = tmp_path / "plain.json"
    rc = cli_main([
        "route", "--from", "43.0,-77.0", "--to", "43.0,-76.84",
        "--geojson", str(out_gj), "--json", str(out_js),
    ])
    assert rc == 0
    assert "history" not in json.loads(out_js.read_text())
    assert all("history_v1" not in f["properties"] for f in json.loads(out_gj.read_text())["features"])

    # --history-metadata emits history_v1 without opening SQLite or reading user config
    meta_gj = tmp_path / "meta.geojson"
    rc = cli_main([
        "route", "--from", "43.0,-77.0", "--to", "43.0,-76.84",
        "--geojson", str(meta_gj), "--history-metadata",
    ])
    assert rc == 0
    assert any("history_v1" in f["properties"] for f in json.loads(meta_gj.read_text())["features"])

    # Now restore history/sqlite and verify route --avoid-history and loop --avoid-history
    monkeypatch.undo()
    monkeypatch.setattr(setup_mod, "missing_serve_files", lambda g, s: (Path("g"), Path("s"), []))
    monkeypatch.setattr(search_mod.Context, "load", classmethod(lambda cls, g=None, s=None: ctx))
    av_gj = tmp_path / "avoid.geojson"
    av_js = tmp_path / "avoid.json"
    rc = cli_main([
        "route", "--from", "43.0,-77.0", "--to", "43.0,-76.84",
        "--avoid-history", "--history-db", str(db_path), "--history-penalty", "4.0",
        "--geojson", str(av_gj), "--json", str(av_js),
    ])
    assert rc == 0
    av_summary = json.loads(av_js.read_text())
    assert av_summary["history"]["edge_penalty"] == 4.0
    assert any("history_v1" in f["properties"] for f in json.loads(av_gj.read_text())["features"])

    # Loop --avoid-history
    monkeypatch.setattr(search_mod.Context, "load", classmethod(lambda cls, g=None, s=None: grid_ctx))
    lp_gj = tmp_path / "loop_avoid.geojson"
    lp_js = tmp_path / "loop_avoid.json"
    rc = cli_main([
        "loop", "--from", f"{grid_start[0]},{grid_start[1]}", "--minutes", "45",
        "--avoid-history", "--history-db", str(db_path),
        "--geojson", str(lp_gj), "--json", str(lp_js),
    ])
    assert rc == 0
    assert "history_v1" in json.loads(lp_gj.read_text())["features"][0]["properties"]

    # Delete trip
    assert cli_main(["history", "delete", "1", "--history-db", str(db_path)]) == 0
    assert cli_main(["history", "delete", "1", "--history-db", str(db_path)]) == 1


# --------------------------------------------------------------------------
# 9. Privacy checks (no personal paths or accidental DB/export artifacts)


def test_no_personal_paths_or_history_artifacts_in_repo():
    root = Path(__file__).resolve().parents[1]
    # No accidental SQLite DB files or journal/WAL/SHM artifacts tracked or sitting in src/tests/config
    for folder in (root / "src", root / "tests", root / "config"):
        for ext in ("*.db", "*.db-journal", "*.db-wal", "*.db-shm", "*.sqlite", "*.sqlite3"):
            assert not list(folder.rglob(ext)), f"unexpected database artifact in {folder}"

    # No hardcoded user home or removable mount paths in tracked source/test/config files
    forbidden_prefixes = ("/" + "home/", "/" + "media/", "/" + "Users/", "/" + "Volumes/")
    for path in [*root.glob("src/**/*.py"), *root.glob("tests/**/*.py"), *root.glob("config/**/*.toml")]:
        text = path.read_text()
        for prefix in forbidden_prefixes:
            assert prefix not in text, f"{path.relative_to(root)} contains hardcoded path prefix {prefix!r}"
