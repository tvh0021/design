"""Command line: scenic fetch | build-graph | fastest | calibrate."""

from __future__ import annotations

import argparse
import json
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scenic", description="Scenic route planner (New York State)")
    sub = p.add_subparsers(dest="cmd", required=True)

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

    c = sub.add_parser("calibrate", help="write edge_delays.parquet (signal, stop and junction delays)")
    c.add_argument("--features", action="store_true", help="recount delay features from the PBF")
    c.add_argument("--fit", action="store_true", help="refit the delay parameters (Google + OSRM demo server)")
    c.add_argument("--pbf", type=Path, help="input .osm.pbf (default data/raw/new-york-latest.osm.pbf)")
    c.add_argument("--graph", type=Path, help="graph directory (default data/graph)")
    c.set_defaults(func=cmd_calibrate)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
