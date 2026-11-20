"""Command line for Maps for Wanderers (the `scenic` command): scenic setup | serve |
route | loop | fastest | demo build | fetch | build-graph | calibrate | fetch-layers | score."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _latlon(s: str) -> tuple[float, float]:
    try:
        lat, lon = (float(x) for x in s.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected LAT,LNG, got {s!r}")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise argparse.ArgumentTypeError(f"out of range: {s!r}")
    return lat, lon


def cmd_setup(args) -> int:
    from .setup_data import run_setup

    return run_setup(
        data_root=args.data,
        force=args.force,
        bundle=args.bundle,
        check_only=args.check,
        prebuilt=args.prebuilt,
    )


def cmd_fetch(args) -> int:
    from .fetch import NY_OSM_URL, fetch_osm

    fetch_osm(args.url or NY_OSM_URL, force=args.force)
    return 0


def cmd_build_graph(args) -> int:
    from .graph import build_graph

    build_graph(args.pbf, args.out)
    return 0


def cmd_fastest(args) -> int:
    from .route import RoadGraph
    from .setup_data import format_missing_data_message, missing_graph_files

    gdir, missing = missing_graph_files(args.graph)
    if missing:
        print(format_missing_data_message(gdir, gdir.parent / "score", missing, cmd_name="scenic fastest"),
              file=sys.stderr)
        return 1

    t0 = time.monotonic()
    g = RoadGraph.load(args.graph)
    t1 = time.monotonic()
    route, snap = g.fastest(args.from_, args.to)
    t2 = time.monotonic()
    print(f"graph loaded in {t1 - t0:.1f} s ({g.n_nodes:,} nodes, {g.n_edges:,} edges); route in {t2 - t1:.2f} s")
    print(f"snapped start {snap['from_snap_m']:.0f} m, end {snap['to_snap_m']:.0f} m")
    if route is None:
        print("no route found")
        return 1
    print(f"fastest: {route.minutes:.1f} min, {route.km:.1f} km, {len(route.edges):,} edges")
    for label, km in g.road_summary(route):
        print(f"  {km:7.1f} km  {label}")
    if args.geojson:
        props = {"from": list(args.from_), "to": list(args.to)}
        Path(args.geojson).write_text(json.dumps(route.geojson(props)))
        print(f"wrote {args.geojson}")
    return 0


def cmd_route(args) -> int:
    from .search import Context, format_table, geojson, plot_routes, result_json, search
    from .setup_data import format_missing_data_message, missing_serve_files

    gdir, sdir, missing = missing_serve_files(args.graph, args.score)
    if missing:
        print(format_missing_data_message(gdir, sdir, missing, cmd_name="scenic route"), file=sys.stderr)
        return 1

    t0 = time.monotonic()
    ctx = Context.load(args.graph, args.score)
    t1 = time.monotonic()
    res = search(ctx, args.from_, args.to)
    if res is None:
        print("no route found")
        return 1
    print(f"loaded in {t1 - t0:.1f} s; snapped start {res.snap['from_snap_m']:.0f} m, end {res.snap['to_snap_m']:.0f} m")
    print(format_table(ctx, res))
    if args.geojson:
        Path(args.geojson).write_text(json.dumps(geojson(ctx, res)))
        print(f"wrote {args.geojson}")
    if args.json:
        Path(args.json).write_text(json.dumps(result_json(ctx, res), indent=1))
        print(f"wrote {args.json}")
    if args.png:
        print(f"wrote {plot_routes(ctx, res, args.png)}")
    return 0


def cmd_loop(args) -> int:
    from .loop import LoopError, Steer, format_table, geojson, loop_config, loops, plot_loops, result_json
    from .search import Context
    from .setup_data import format_missing_data_message, missing_serve_files

    gdir, sdir, missing = missing_serve_files(args.graph, args.score)
    if missing:
        print(format_missing_data_message(gdir, sdir, missing, cmd_name="scenic loop"), file=sys.stderr)
        return 1

    cfg = loop_config()
    minutes = args.minutes if args.minutes is not None else cfg["default_min"]
    if args.direction:
        steer = Steer(mode="compass", direction=args.direction)
    elif args.anchor:
        steer = Steer(mode="anchor", point=args.anchor)
    elif args.feature:
        steer = Steer(mode="anchor", feature=args.feature)
    else:
        steer = Steer(mode="any")

    t0 = time.monotonic()
    ctx = Context.load(args.graph, args.score)
    t1 = time.monotonic()
    try:
        res = loops(ctx, args.from_, minutes, steer=steer, cfg=cfg)
    except (LoopError, ValueError) as exc:
        msg = exc.message if isinstance(exc, LoopError) else str(exc)
        print(msg)
        return 1

    print(f"loaded in {t1 - t0:.1f} s; snapped start {res.snap_m:.0f} m")
    if res.anchor is not None:
        anc = res.anchor
        desc = anc.name or f"({anc.point[0]:.5f}, {anc.point[1]:.5f})"
        print(f"anchor ({anc.kind}): {desc}, moved {anc.moved_m:.0f} m")
    print(format_table(ctx, res))
    if args.geojson:
        Path(args.geojson).write_text(json.dumps(geojson(ctx, res)))
        print(f"wrote {args.geojson}")
    if args.json:
        Path(args.json).write_text(json.dumps(result_json(ctx, res), indent=1))
        print(f"wrote {args.json}")
    if args.png:
        print(f"wrote {plot_loops(ctx, res, args.png)}")
    return 0


def cmd_serve(args) -> int:
    from .setup_data import format_missing_data_message, missing_serve_files, run_setup

    gdir, sdir, missing = missing_serve_files(args.graph, args.score)
    if missing:
        if getattr(args, "setup", False) and args.graph is None and args.score is None:
            rc = run_setup()
            if rc != 0:
                return rc
        else:
            print(format_missing_data_message(gdir, sdir, missing, cmd_name="scenic serve"), file=sys.stderr)
            return 1

    from .web.server import serve

    return serve(
        port=args.port,
        graph_dir=args.graph,
        score_dir=args.score,
        verbose=args.verbose,
        open_browser=args.open,
    )


def cmd_demo_build(args) -> int:
    from .web.demo import DemoError, build_site

    try:
        build_site(out=args.out, presets_path=args.presets, graph_dir=args.graph, score_dir=args.score,
                   max_points=args.max_points)
    except DemoError as exc:
        print(f"scenic demo build: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_calibrate(args) -> int:
    from .calibrate import run_fit
    from . import data_dir
    from .graph import build_edge_delays, reapply_edge_delays

    graph_dir = args.graph or data_dir() / "graph"
    if args.features or not (graph_dir / "edge_delays.parquet").exists():
        build_edge_delays(args.pbf, graph_dir)
    if args.fit:
        run_fit(graph_dir, args.pbf)
    else:
        print(f"wrote {reapply_edge_delays(graph_dir)}")
    return 0


def cmd_fetch_layers(args) -> int:
    from .layers import fetch_layers

    only = set(args.only or ["dem", "nlcd", "byways"])
    fetch_layers(args.raw, dem="dem" in only, nlcd="nlcd" in only, byways="byways" in only)
    return 0


def cmd_score(args) -> int:
    from .score import run

    run(graph_dir=args.graph, out_dir=args.out, force=args.force)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scenic", description="Maps for Wanderers: scenic drives across New York State")
    sub = p.add_subparsers(dest="cmd", required=True)

    st = sub.add_parser("setup", help="download and build all New York data needed for scenic serve (idempotent)")
    st.add_argument("--data", type=Path, help="data directory (default $SCENIC_DATA or data/)")
    st.add_argument("--prebuilt", action="store_true",
                    help="download and verify the published New York runtime data (~160 MB) instead of building")
    st.add_argument("--bundle", metavar="PATH_OR_URL",
                    help="unpack a prebuilt graph+score archive (.tar.gz/.zip or https:// URL) instead of building")
    st.add_argument("--check", action="store_true", help="check whether data is ready without downloading or building")
    st.add_argument("--force", action="store_true", help="redownload the OSM extract and rebuild all stages")
    st.set_defaults(func=cmd_setup)

    sv = sub.add_parser("serve", help="Maps for Wanderers, the local map UI (http://127.0.0.1:PORT/, docs/WEB.md)")
    sv.add_argument("--port", type=int, default=8765, help="port on 127.0.0.1 (default 8765)")
    sv.add_argument("-o", "--open", action="store_true", help="open the map in your default browser")
    sv.add_argument("--setup", action="store_true", help="run 'scenic setup' automatically if data is missing")
    sv.add_argument("--graph", type=Path, help="graph directory (default data/graph)")
    sv.add_argument("--score", type=Path, help="score directory (default data/score)")
    sv.add_argument("--verbose", action="store_true", help="log every request")
    sv.set_defaults(func=cmd_serve)

    f = sub.add_parser("fetch", help="download the New York OSM extract")
    f.add_argument("--url", help="override the Geofabrik URL")
    f.add_argument("--force", action="store_true", help="download even if the md5 matches")
    f.set_defaults(func=cmd_fetch)

    b = sub.add_parser("build-graph", help="build the drivable road graph from the OSM extract")
    b.add_argument("--pbf", type=Path, help="input .osm.pbf (default data/raw/new-york-latest.osm.pbf)")
    b.add_argument("--out", type=Path, help="output directory (default data/graph)")
    b.set_defaults(func=cmd_build_graph)

    r = sub.add_parser("fastest", help="fastest route between two points")
    r.add_argument("--from", dest="from_", type=_latlon, required=True, metavar="LAT,LNG")
    r.add_argument("--to", type=_latlon, required=True, metavar="LAT,LNG")
    r.add_argument("--geojson", type=Path, help="write the route as GeoJSON")
    r.add_argument("--graph", type=Path, help="graph directory (default data/graph)")
    r.set_defaults(func=cmd_fastest)

    rt = sub.add_parser("route", help="fastest route and the most scenic route under +15, +30 and +50 %%")
    rt.add_argument("--from", dest="from_", type=_latlon, required=True, metavar="LAT,LNG")
    rt.add_argument("--to", type=_latlon, required=True, metavar="LAT,LNG")
    rt.add_argument("--geojson", type=Path, help="write the routes and their moments as GeoJSON")
    rt.add_argument("--png", type=Path, help="write a map of the routes")
    rt.add_argument("--json", type=Path, help="write the summary table as JSON")
    rt.add_argument("--graph", type=Path, help="graph directory (default data/graph)")
    rt.add_argument("--score", type=Path, help="score directory (default data/score)")
    rt.set_defaults(func=cmd_route)

    lp = sub.add_parser("loop", help="scenic drive-around loops starting and ending at the same point")
    lp.add_argument("--from", dest="from_", type=_latlon, required=True, metavar="LAT,LNG")
    lp.add_argument("--minutes", type=float, help="time budget in minutes (default 120, range 30..360)")
    steer_grp = lp.add_mutually_exclusive_group()
    steer_grp.add_argument(
        "--direction",
        type=lambda s: s.strip().upper(),
        choices=["N", "NE", "E", "SE", "S", "SW", "W", "NW"],
        help="compass direction from the start",
    )
    steer_grp.add_argument("--anchor", type=_latlon, metavar="LAT,LNG", help="anchor location to pass")
    steer_grp.add_argument("--feature", help="named scenic feature to pass (e.g. 'Lake Ontario')")
    lp.add_argument("--geojson", type=Path, help="write the loops and their moments as GeoJSON")
    lp.add_argument("--png", type=Path, help="write a map of the loops")
    lp.add_argument("--json", type=Path, help="write the summary table as JSON")
    lp.add_argument("--graph", type=Path, help="graph directory (default data/graph)")
    lp.add_argument("--score", type=Path, help="score directory (default data/score)")
    lp.set_defaults(func=cmd_loop)

    dm = sub.add_parser("demo", help="static demo site with preset routes (docs/PUBLISH.md)")
    dsub = dm.add_subparsers(dest="demo_cmd", required=True)
    db = dsub.add_parser("build", help="run the presets in config/demo_routes.toml and write a static site")
    db.add_argument("--out", type=Path, help="output folder (default scenic/site; cleared on rebuild)")
    db.add_argument("--presets", type=Path, help="preset file (default config/demo_routes.toml)")
    db.add_argument("--graph", type=Path, help="graph directory (default data/graph)")
    db.add_argument("--score", type=Path, help="score directory (default data/score)")
    db.add_argument("--max-points", type=int, default=1500, help="points per route line (default 1500)")
    db.set_defaults(func=cmd_demo_build)

    c = sub.add_parser("calibrate", help="write edge_delays.parquet (signal, stop and junction delays)")
    c.add_argument("--features", action="store_true", help="recount delay features from the PBF")
    c.add_argument("--fit", action="store_true", help="refit the delay parameters (Google + OSRM demo server)")
    c.add_argument("--pbf", type=Path, help="input .osm.pbf (default data/raw/new-york-latest.osm.pbf)")
    c.add_argument("--graph", type=Path, help="graph directory (default data/graph)")
    c.set_defaults(func=cmd_calibrate)

    fl = sub.add_parser("fetch-layers", help="download 3DEP elevation, NLCD land cover and byway lines")
    fl.add_argument("--only", nargs="+", choices=["dem", "nlcd", "byways"], help="fetch only these layers")
    fl.add_argument("--raw", type=Path, help="download directory (default data/raw)")
    fl.set_defaults(func=cmd_fetch_layers)

    s = sub.add_parser("score", help="compute the scenery score for every edge (data/score/)")
    s.add_argument("--graph", type=Path, help="graph directory (default data/graph)")
    s.add_argument("--out", type=Path, help="output directory (default data/score)")
    s.add_argument("--force", nargs="*", metavar="STAGE",
                   help="rerun these stages (no names: all) even if their products are fresh")
    s.set_defaults(func=cmd_score)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
