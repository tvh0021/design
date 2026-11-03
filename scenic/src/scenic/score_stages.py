"""Stage runner for the scenery score (see scenic.score for the stage list)."""

from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import numpy as np

from . import data_dir


@dataclass
class Paths:
    graph: str
    raw: str
    rasters: str
    out: str
    config: str

    def p(self, kind: str, name: str) -> Path:
        return Path(getattr(self, kind)) / name

    @property
    def pbf(self) -> Path:
        return Path(self.raw) / "new-york-latest.osm.pbf"


class TrackedConfig(dict):
    """The score config, recording which top-level sections a stage reads, so
    the runner can check them against the sections the stage declares (and
    whose contents go into its fingerprint)."""

    accessed: set[str] = set()

    def __getitem__(self, key):
        TrackedConfig.accessed.add(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        TrackedConfig.accessed.add(key)
        return super().get(key, default)


def _cfg(paths: Paths) -> dict:
    from .score import load_config

    return TrackedConfig(load_config(Path(paths.config)))


def _grid(paths: Paths):
    from .rasters import Grid

    return Grid.from_json(paths.p("rasters", "grid.json").read_text())


def _log(msg: str) -> None:
    sys.stderr.write(f"  {msg}\n")
    sys.stderr.flush()


# --------------------------------------------------------------------------
# Stage bodies (run in a child process)


def st_grid(paths: Paths) -> dict:
    import pyarrow.parquet as pq

    from .rasters import make_grid
    from .score import project_lonlat

    cfg = _cfg(paths)["grid"]
    t = pq.read_table(paths.p("graph", "edges.parquet"), columns=["lon", "lat"])
    lon = t.column("lon").combine_chunks().values.to_numpy()
    lat = t.column("lat").combine_chunks().values.to_numpy()
    x, y = project_lonlat(lon, lat, cfg["crs"])
    g = make_grid(x.min(), y.min(), x.max(), y.max(), cfg["origin_x"], cfg["origin_y"], cfg["res"],
                  cfg["pad_m"], cfg["block"])
    out = paths.p("rasters", "grid.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(g.to_json())
    return {"shape_30m": list(g.shape), "cells_30m": g.width * g.height, "bounds_5070": list(g.bounds)}


def dem_tiles(paths: Paths) -> list[Path]:
    return sorted(Path(paths.raw, "dem").glob("USGS_1_*.tif"))


def st_dem(paths: Paths) -> dict:
    from .rasters import block_mean, build_dem, fill_nearest, write_tif

    g = _grid(paths)
    tiles = dem_tiles(paths)
    if not tiles:
        raise RuntimeError("no 3DEP tiles in data/raw/dem; run `scenic fetch-layers`")
    nodata = -32768
    dem = build_dem(g, tiles, nodata)
    valid = dem != nodata
    missing = float(1 - valid.mean())
    write_tif(paths.p("rasters", "dem30.tif"), dem, g, nodata=nodata)
    dem90 = fill_nearest(block_mean(dem, _cfg(paths)["grid"]["block"], valid=valid))
    write_tif(paths.p("rasters", "dem90.tif"), dem90, g.coarsen(_cfg(paths)["grid"]["block"]))
    return {"tiles": len(tiles), "nodata_share_30m": round(missing, 5),
            "elev_min_m": float(dem90.min()), "elev_max_m": float(dem90.max())}


def nlcd_tif(paths: Paths) -> Path:
    found = sorted(Path(paths.raw, "nlcd").glob("Annual_NLCD_LndCov_*_CU_*.tif"))
    if not found:
        raise RuntimeError("no NLCD land cover GeoTIFF in data/raw/nlcd; run `scenic fetch-layers`")
    return found[-1]


def st_nlcd(paths: Paths) -> dict:
    from .rasters import read_nlcd_window, write_tif

    g = _grid(paths)
    tif = nlcd_tif(paths)
    arr, info = read_nlcd_window(g, tif)
    write_tif(paths.p("rasters", "nlcd30.tif"), arr, g, nodata=250)
    counts = np.bincount(arr.ravel(), minlength=256)
    share = {int(c): round(float(counts[c] / arr.size), 4) for c in np.flatnonzero(counts)}
    return {"file": tif.name, "source": info, "class_share": share}


def st_water(paths: Paths) -> dict:
    from rasterio.features import rasterize
    from scipy import ndimage

    from .rasters import class_mask, disk_footprint, read_tif, water_size, write_tif
    from .water import project_wkb, read_water

    cfg = _cfg(paths)
    wc = cfg["water"]
    g = _grid(paths)
    t0 = time.monotonic()
    raw = read_water(paths.pbf, tuple(wc["exclude_water"]))
    t1 = time.monotonic()
    areas = project_wkb(raw["area"])
    area = rasterize(((a, 1) for a in areas if a is not None and not a.is_empty), out_shape=g.shape,
                     transform=g.transform, fill=0, all_touched=False, dtype="uint8").astype(bool)
    osm_area_cells = int(area.sum())
    del areas
    nlcd = read_tif(paths.p("rasters", "nlcd30.tif"))
    nlcd_water = class_mask(nlcd, cfg["groups"]["water"])
    del nlcd
    area |= nlcd_water
    del nlcd_water
    coast = np.zeros(g.shape, bool)
    if raw["coast"]:
        cl = project_wkb(raw["coast"])
        coast = rasterize(((c, 1) for c in cl), out_shape=g.shape, transform=g.transform, fill=0,
                          all_touched=True, dtype="uint8").astype(bool)
        r = int(round(wc["coastline_band_m"] / g.res))
        coast = ndimage.binary_dilation(coast, structure=disk_footprint(r))
        area |= coast
    lines = project_wkb(raw["line"])
    codes = raw["line_code"]
    shapes = []
    for code in (3, 2, 1):  # later shapes win: river over canal over stream
        sel = np.flatnonzero(codes == code)
        shapes.extend((lines[i], code) for i in sel)
    line = rasterize(shapes, out_shape=g.shape, transform=g.transform, fill=0, all_touched=True,
                     dtype="uint8")
    del shapes, lines
    line[area] = 0
    size = water_size(area, line, coast, g.res * g.res, wc)
    write_tif(paths.p("rasters", "water_area.tif"), area.astype(np.uint8), g)
    write_tif(paths.p("rasters", "water_line.tif"), line, g)
    write_tif(paths.p("rasters", "water_size.tif"), size, g)
    return {"osm_areas": len(raw["area"]), "osm_lines": len(raw["line"]), "coastline_ways": len(raw["coast"]),
            "bad_geometries": raw["bad"], "osm_read_s": round(t1 - t0, 1),
            "area_share": round(float(area.mean()), 4), "osm_area_cells": osm_area_cells,
            "coast_cells": int(coast.sum()), "line_cells": int((line > 0).sum())}


def st_water90(paths: Paths) -> dict:
    from .rasters import read_tif, water90_products, write_tif

    cfg = _cfg(paths)
    k = cfg["grid"]["block"]
    g90 = _grid(paths).coarsen(k)
    area = read_tif(paths.p("rasters", "water_area.tif")).astype(bool)
    dem30 = read_tif(paths.p("rasters", "dem30.tif"))
    prods = water90_products(area, dem30, k, g90.res, cfg)
    for name, arr in prods.items():
        write_tif(paths.p("rasters", f"{name}.tif"), arr, g90)
    big = prods["bigwater_idx90"]
    return {"reach90_p50_on_water": float(np.median(prods["reach90"][prods["wfrac90"] > 0.02][::5])),
            "bigwater_cells": int(np.count_nonzero((big >= 0) & (big == np.arange(big.size).reshape(big.shape)))),
            "share_within_wv_max": round(float((big >= 0).mean()), 4)}


def st_terrain90(paths: Paths) -> dict:
    from .rasters import far_min_max, read_tif, relief_tpi, write_tif

    cfg = _cfg(paths)
    g90 = _grid(paths).coarsen(cfg["grid"]["block"])
    dem90 = read_tif(paths.p("rasters", "dem90.tif"))
    relief, tpi = relief_tpi(dem90, cfg["hills"]["disk_radius_cells"])
    write_tif(paths.p("rasters", "relief90.tif"), relief, g90)
    write_tif(paths.p("rasters", "tpi90.tif"), tpi, g90)
    lo, hi = far_min_max(dem90, cfg["views"]["far_radius_cells"])
    write_tif(paths.p("rasters", "min3k90.tif"), lo, g90)
    write_tif(paths.p("rasters", "max3k90.tif"), hi, g90)
    return {"shape_90m": list(g90.shape), "relief_p50": float(np.median(relief[::7, ::7])),
            "relief_p99": float(np.percentile(relief[::7, ::7], 99))}


def st_land90(paths: Paths) -> dict:
    from .rasters import landcover_products, read_tif, write_tif

    cfg = _cfg(paths)
    g90 = _grid(paths).coarsen(cfg["grid"]["block"])
    prods = landcover_products(read_tif(paths.p("rasters", "nlcd30.tif")), cfg)
    for name, arr in prods.items():
        write_tif(paths.p("rasters", f"{name}.tif"), arr, g90)
    return {name: round(float(arr.mean()), 4) for name, arr in prods.items()}


def st_controls(paths: Paths) -> dict:
    from .water import read_signal_nodes

    pts = read_signal_nodes(paths.pbf)
    out = paths.p("out", "signal_nodes.npy")
    out.parent.mkdir(parents=True, exist_ok=True)
    np.save(out, pts)
    return {"signal_nodes": int(len(pts))}


def st_edges(paths: Paths) -> dict:
    from scipy.spatial import cKDTree

    from .graph import read_edges_fingerprint
    from .rasters import read_tif
    from .score import (curvature, excluded_mask, hills_edges, land_points, lookup, project_lonlat,
                        prospect_points, quiet, read_edges, road_keys, sample_points, save_samples, turning,
                        views_points, water_points, water_view_points, write_edge_signals)

    cfg = _cfg(paths)
    g30 = _grid(paths)
    g90 = g30.coarsen(cfg["grid"]["block"])
    R = lambda n: read_tif(paths.p("rasters", n))  # noqa: E731
    t0 = time.monotonic()
    E = read_edges(Path(paths.graph))
    s = sample_points(E.off, E.x, E.y, E.length_m, cfg["sampling"]["step_m"])
    timing = {"sample_s": round(time.monotonic() - t0, 1)}
    _log(f"{len(s.x):,} sample points on {E.n:,} edges")

    dem90 = R("dem90.tif")
    dem30 = R("dem30.tif")
    z = lookup(g30, dem30, s.x, s.y, fallback=-32768)
    bad = z <= -32768
    z[bad] = lookup(g90, dem90, s.x[bad], s.y[bad], fallback=0.0)

    t = time.monotonic()
    area = R("water_area.tif").astype(bool)
    line = R("water_line.tif")
    size = R("water_size.tif")
    reach90 = R("reach90.tif")
    water_z90 = R("water_z90.tif")
    W_pt, d_pt = water_points(s, g30, area, line, size, lambda x, y: lookup(g90, reach90, x, y,
                                                                              cfg["water"]["reach_min_m"]),
                              z, dem30, R("nlcd30.tif"), cfg,
                              zsurf_of=lambda x, y: lookup(g90, water_z90, x, y, np.nan))
    del area, line, size, reach90, dem30
    timing["water_s"] = round(time.monotonic() - t, 1)
    _log(f"W: {timing['water_s']} s")

    t = time.monotonic()
    wv_pt = water_view_points(s.x, s.y, z, g90, R("bigwater_idx90.tif"), water_z90, dem90,
                              R("wv_reach90.tif"), cfg)
    timing["water_view_s"] = round(time.monotonic() - t, 1)

    t = time.monotonic()
    relief_pt = lookup(g90, R("relief90.tif"), s.x, s.y)
    H, h_relief, h_grade = hills_edges(s, relief_pt, z, E.length_m, cfg)
    prospect = prospect_points(z, lookup(g90, R("min3k90.tif"), s.x, s.y, np.nan),
                               lookup(g90, R("max3k90.tif"), s.x, s.y, np.nan), cfg)
    prospect = np.nan_to_num(prospect)
    V_pt = views_points(lookup(g90, R("open90.tif"), s.x, s.y), lookup(g90, R("forest90.tif"), s.x, s.y),
                        prospect, wv_pt, cfg)
    L_pt = land_points(lookup(g90, R("lscore90.tif"), s.x, s.y), lookup(g90, R("mix90.tif"), s.x, s.y), cfg)
    dev_pt = lookup(g90, R("dev90.tif"), s.x, s.y)
    del dem90
    timing["rasters_s"] = round(time.monotonic() - t, 1)

    t = time.monotonic()
    turn = turning(E.off, E.x, E.y, cfg["curves"]["min_step_m"])
    C = curvature(E.off, E.x, E.y, E.length_m, cfg["curves"]["min_step_m"], cfg["curves"]["scale_deg_per_km"])
    timing["curvature_s"] = round(time.monotonic() - t, 1)

    # Signals: each signal node goes to the edge of its nearest sample point within 25 m.
    sig_ll = np.load(paths.p("out", "signal_nodes.npy"))
    sig = np.zeros(E.n)
    matched = 0
    if len(sig_ll):
        sx, sy = project_lonlat(sig_ll[:, 0], sig_ll[:, 1])
        d, i = cKDTree(np.column_stack([s.x, s.y])).query(
            np.column_stack([sx, sy]), distance_upper_bound=cfg["penalty"]["signal_match_m"], workers=-1)
        ok = np.isfinite(d)
        matched = int(ok.sum())
        sig = np.bincount(s.edge[i[ok]], minlength=E.n).astype(np.float64)

    n_nodes = int(max(E.u.max(), E.v.max())) + 1
    deg = np.bincount(E.u, minlength=n_nodes) + np.bincount(E.v, minlength=n_nodes)
    junction_halves = 0.5 * (deg[E.u] >= 3) + 0.5 * (deg[E.v] >= 3)
    wd = np.where(np.isfinite(d_pt), d_pt, 9999.0)
    cols = {
        "W": s.mean(W_pt), "H": H, "H_relief": h_relief, "H_grade": h_grade,
        "V": s.mean(V_pt), "V_prospect": s.mean(prospect), "V_water": s.mean(wv_pt),
        "L": s.mean(L_pt), "Q": quiet(E.highway, cfg), "C_edge": C, "turn_deg": turn, "dev": s.mean(dev_pt),
        "signals": sig, "junction_halves": junction_halves,
        "n_samples": s.n.astype(np.int32), "water_dist_m": s.mean(wd),
        "excluded": excluded_mask(E.surface, E.name, cfg),
        "key": road_keys(E.ref, E.name, E.way_id),
    }
    fp = read_edges_fingerprint(Path(paths.graph))
    write_edge_signals(cols, paths.p("out", "edge_signals.parquet"), fp)
    save_samples(paths.p("out", "samples.npz"), s, fp)
    return {"sample_points": int(len(s.x)), "signal_nodes_matched": matched,
            "points_with_water_hit": round(float(np.isfinite(d_pt).mean()), 4),
            "points_with_water_view": round(float((wv_pt > 0).mean()), 4), **timing}


def st_score(paths: Paths) -> dict:
    import pyarrow.parquet as pq

    from .graph import edges_fingerprint
    from .score import _dict_col, combine, read_edge_signals, scores_table, write_scores

    cfg = _cfg(paths)
    et = pq.read_table(paths.p("graph", "edges.parquet"),
                       columns=["u", "v", "way_id", "length_m", "highway", "maxspeed_tagged", "speed_mph"])
    sig_t = read_edge_signals(paths.p("out", "edge_signals.parquet"), edges_fingerprint(et))
    sig = {c: sig_t.column(c).to_numpy() for c in sig_t.column_names}
    extra = {c: et.column(c).to_numpy() for c in ("u", "v", "way_id", "length_m", "maxspeed_tagged", "speed_mph")}
    extra["highway"] = _dict_col(et, "highway")
    extra["key"] = sig["key"]
    active = ~sig["excluded"]
    res = combine(sig, extra, active, cfg)
    res.update({c: sig[c] for c in ("W", "H", "V", "L", "Q", "n_samples", "water_dist_m", "excluded")})
    table = scores_table(extra, res)
    write_scores(table, paths.p("graph", "edges.parquet"), paths.p("out", "scores.parquet"))
    L = extra["length_m"]
    return {"edges": int(et.num_rows), "excluded_edges": int(sig["excluded"].sum()),
            "excluded_km": round(float(L[sig["excluded"]].sum() / 1000), 1),
            "window_pairs": res["window_pairs"], "bfs_rounds": res["bfs_rounds"],
            "mean_window_m": round(float(np.nanmean(np.where(active, res["window_m"], np.nan))), 1)}


def st_outputs(paths: Paths) -> dict:
    from .report import heatmap, summary_md

    import pyarrow.parquet as pq

    from .graph import edges_fingerprint
    from .score import load_samples

    t = _load_scores(paths)
    et = pq.read_table(paths.p("graph", "edges.parquet"), columns=["u", "v", "way_id", "length_m"])
    L = et.column("length_m").to_numpy()
    smp = load_samples(paths.p("out", "samples.npz"), edges_fingerprint(et))
    cfg = _cfg(paths)
    heatmap(t, smp, L, paths.p("out", "heatmap.png"), cfg["output"]["heatmap_cell_m"],
            cfg["output"]["heatmap_width_px"])
    summary_md(t, L, paths.p("out", "summary.md"))
    return {"heatmap": str(paths.p("out", "heatmap.png")), "summary": str(paths.p("out", "summary.md"))}


def st_osm_features(paths: Paths) -> dict:
    import collections

    import pyarrow.parquet as pq

    from .moments import read_osm_features

    mc = _cfg(paths)["moments"]
    t = read_osm_features(paths.pbf, mc["osm_water_min_m2"], mc["pond_floor_m2"],
                          named_min_m2=mc["water_named_min_m2"])
    out = paths.p("out", "osm_features.parquet")
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(t, out, compression="zstd")
    return {"features": dict(collections.Counter(t.column("kind").to_pylist()))}


def st_moments(paths: Paths) -> dict:
    import pyarrow.parquet as pq

    from .moments import detect

    tbl, info = detect(Path(paths.graph), Path(paths.out), Path(paths.rasters), _cfg(paths), log=_log)
    pq.write_table(tbl, paths.p("out", "moments.parquet"), compression="zstd")
    return info


def st_validate(paths: Paths) -> dict:
    from .validate import run_validation

    res = run_validation(Path(paths.graph), Path(paths.out), Path(paths.raw), Path(paths.config).parent)
    paths.p("out", "validation.json").write_text(json.dumps(res, indent=2) + "\n")
    return {"byways_pass": res.get("byways", {}).get("pass"), "spot_scenic_pass": res["spot_checks"]["scenic"]["pass"],
            "spot_dull_pass": res["spot_checks"]["dull"]["pass"]}


def st_validate_routes(paths: Paths) -> dict:
    from .validate import run_route_validation

    res = run_route_validation(Path(paths.graph), Path(paths.out), Path(paths.config).parent, _cfg(paths)["route"],
                               log=_log)
    paths.p("out", "route_validation.json").write_text(json.dumps(res, indent=1) + "\n")
    return {"pass": res["pass"], "v1": f"{res['v1']['passed']}/{res['v1']['of']} (bands {res['v1']['bands_passed']})",
            "v2": f"{res['v2']['passed']}/{res['v2']['non_trivial']}",
            "v5": f"{res['v5']['moments_hit']}/{res['v5']['of']}, non {res['v5']['non_moments_hit']}/"
                  f"{res['v5']['non_of']}", "holdout2": res["holdout2"]["status"]}


def _load_scores(paths: Paths):
    from .score import load

    return load(paths.p("graph", "edges.parquet"), paths.p("out", "scores.parquet"))


# --------------------------------------------------------------------------
# Runner


@dataclass
class Stage:
    """One stage: its body, the files it reads and writes, the scenic modules
    whose code it runs and the config sections it reads. All of these go into
    its fingerprint (see `stage_fingerprint`)."""

    name: str
    fn: object
    inputs: list[Path]
    outputs: list[Path]
    modules: tuple[str, ...] = ()
    sections: tuple[str, ...] = ()


def stages(paths: Paths) -> list[Stage]:
    R = lambda n: paths.p("rasters", n)  # noqa: E731
    O = lambda n: paths.p("out", n)  # noqa: E731
    cfgd = Path(paths.config).parent
    edges = paths.p("graph", "edges.parquet")
    nlcd_in = sorted(Path(paths.raw, "nlcd").glob("Annual_NLCD_LndCov_*_CU_*.tif"))[-1:]
    byway_in = sorted(Path(paths.raw, "byways").glob("*.geojson"))
    terrain90 = [R(n) for n in ("relief90.tif", "tpi90.tif", "min3k90.tif", "max3k90.tif")]
    land90 = [R(n) for n in ("lscore90.tif", "mix90.tif", "open90.tif", "forest90.tif", "dev90.tif")]
    water = [R(n) for n in ("water_area.tif", "water_line.tif", "water_size.tif")]
    water90 = [R(n) for n in ("wfrac90.tif", "reach90.tif", "water_z90.tif", "bigwater_idx90.tif", "wv_reach90.tif")]
    return [
        Stage("grid", st_grid, [edges], [R("grid.json")], ("rasters", "score"), ("grid",)),
        Stage("dem", st_dem, [R("grid.json"), *dem_tiles(paths)], [R("dem30.tif"), R("dem90.tif")],
              ("rasters",), ("grid",)),
        Stage("nlcd", st_nlcd, [R("grid.json"), *nlcd_in], [R("nlcd30.tif")], ("rasters",)),
        Stage("water", st_water, [R("grid.json"), R("nlcd30.tif"), paths.pbf], water, ("rasters", "water"),
              ("water", "groups")),
        Stage("water90", st_water90, [R("grid.json"), R("water_area.tif"), R("dem30.tif")], water90, ("rasters",),
              ("grid", "water", "views")),
        Stage("terrain90", st_terrain90, [R("grid.json"), R("dem90.tif")], terrain90, ("rasters",),
              ("grid", "hills", "views")),
        Stage("land90", st_land90, [R("grid.json"), R("nlcd30.tif")], land90, ("rasters",),
              ("grid", "landcover", "groups", "views", "penalty")),
        Stage("controls", st_controls, [paths.pbf], [O("signal_nodes.npy")], ("water",)),
        Stage("edges", st_edges, [edges, R("grid.json"), R("dem30.tif"), R("dem90.tif"), R("nlcd30.tif"), *water,
                                  *water90, *terrain90, *land90, O("signal_nodes.npy")],
              [O("edge_signals.parquet"), O("samples.npz")], ("score", "rasters", "graph"),
              ("grid", "sampling", "water", "hills", "views", "landcover", "quiet", "curves", "penalty", "exclude")),
        Stage("score", st_score, [O("edge_signals.parquet"), edges], [O("scores.parquet")], ("score", "graph"),
              ("combine", "penalty", "smoothing", "curves")),
        Stage("outputs", st_outputs, [O("scores.parquet"), O("samples.npz"), edges], [O("heatmap.png"), O("summary.md")],
              ("report", "score", "graph"), ("output",)),
        Stage("osm_features", st_osm_features, [paths.pbf], [O("osm_features.parquet")], ("moments", "water"),
              ("moments",)),
        Stage("moments", st_moments,
              [edges, O("edge_signals.parquet"), O("samples.npz"), O("osm_features.parquet"), R("grid.json"),
               R("dem30.tif"), R("dem90.tif"), R("open90.tif"), R("min3k90.tif"), *water, R("bigwater_idx90.tif"),
               R("wfrac90.tif")],
              [O("moments.parquet")], ("moments", "score", "rasters", "graph", "water"),
              ("moments", "grid", "water")),
        Stage("validate", st_validate,
              [O("scores.parquet"), O("samples.npz"), O("edge_signals.parquet"), edges, *byway_in,
               cfgd / "spot_checks.toml", cfgd / "byways.toml",
               # RoadGraph.load reads these for the Rochester-Syracuse route
               paths.p("graph", "nodes.npz"), paths.p("graph", "edge_delays.parquet")],
              [O("validation.json")], ("validate", "score", "report", "route", "graph")),
        Stage("validate_routes", st_validate_routes,
              [O("scores.parquet"), O("moments.parquet"), O("samples.npz"), edges, paths.p("graph", "nodes.npz"),
               paths.p("graph", "edge_delays.parquet"), cfgd / "spot_checks.toml"],
              [O("route_validation.json")], ("validate", "search", "route", "moments", "score", "report", "graph"),
              ("route",)),
    ]


# --------------------------------------------------------------------------
# Freshness: a stage is up to date when its outputs exist, are the files it
# wrote last time, and its fingerprint is unchanged. The fingerprint hashes
# the stage's code (its body, the runner helpers and the scenic modules it
# uses), the config sections it reads and the contents of its input files.
# A stage whose code or config changes reruns; if that changes its outputs,
# every stage that reads them gets a new fingerprint and reruns too.

_SHARED = ("Paths", "TrackedConfig", "_cfg", "_grid", "dem_tiles", "nlcd_tif", "_load_scores")
PKG_DIR = Path(__file__).resolve().parent


def code_hash(fn, modules, pkg_dir: Path = PKG_DIR) -> str:
    """Hash of a stage body, the shared runner helpers and the source files of
    `modules` (scenic module names, e.g. "score")."""
    import inspect

    h = hashlib.sha256()
    h.update(inspect.getsource(fn).encode())
    me = sys.modules[__name__]
    for name in _SHARED:
        h.update(inspect.getsource(getattr(me, name)).encode())
    for m in sorted(modules):
        h.update(m.encode() + b"\0" + (Path(pkg_dir) / f"{m}.py").read_bytes())
    return h.hexdigest()


def config_hash(cfg: dict, sections) -> str:
    sub = {k: cfg.get(k) for k in sorted(sections)}
    return hashlib.sha256(json.dumps(sub, sort_keys=True, default=str).encode()).hexdigest()


class FileHashes:
    """SHA-256 of file contents, cached by (path, size, mtime_ns) in a JSON file."""

    def __init__(self, cache_path: Path | None = None):
        self.path = Path(cache_path) if cache_path else None
        self.cache = {}
        if self.path and self.path.exists():
            try:
                self.cache = json.loads(self.path.read_text())
            except ValueError:
                self.cache = {}

    def __call__(self, p: Path) -> str:
        p = Path(p)
        if not p.exists():
            return "missing"
        st = p.stat()
        key = str(p.resolve())
        ent = self.cache.get(key)
        if ent and ent[0] == st.st_size and ent[1] == st.st_mtime_ns:
            return ent[2]
        h = hashlib.sha256()
        with open(p, "rb") as f:
            while chunk := f.read(1 << 22):
                h.update(chunk)
        self.cache[key] = [st.st_size, st.st_mtime_ns, h.hexdigest()]
        return h.hexdigest()

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.cache))


def stage_fingerprint(code_h: str, cfg_h: str, inputs: list[Path], hashes: FileHashes) -> str:
    h = hashlib.sha256()
    h.update(code_h.encode() + cfg_h.encode())
    for p in inputs:
        h.update(str(Path(p).name).encode() + b"=" + hashes(p).encode() + b";")
    return h.hexdigest()


def stamp_path(stage: Stage) -> Path:
    return Path(stage.outputs[0]).parent / ".stages" / f"{stage.name}.json"


def read_stamp(stage: Stage) -> dict | None:
    p = stamp_path(stage)
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except ValueError:
        return None


def write_stamp(stage: Stage, fingerprint: str, hashes: FileHashes) -> None:
    p = stamp_path(stage)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"fingerprint": fingerprint,
                             "outputs": {Path(o).name: hashes(o) for o in stage.outputs}}, indent=1) + "\n")


def is_fresh(stage: Stage, fingerprint: str, hashes: FileHashes) -> bool:
    if not all(Path(p).exists() for p in stage.outputs):
        return False
    st = read_stamp(stage)
    if not st or st.get("fingerprint") != fingerprint:
        return False
    return all(st.get("outputs", {}).get(Path(o).name) == hashes(o) for o in stage.outputs)


def _child(name: str, paths_d: dict) -> tuple[dict, float, float, list[str]]:
    import os

    # GDAL's default block cache is 5 % of RAM (3+ GB here); the stages read
    # whole rasters once, so a small cache is enough.
    os.environ.setdefault("GDAL_CACHEMAX", "256")
    from .graph import peak_rss_mb

    fn = {s.name: s.fn for s in stages(Paths(**paths_d))}[name]
    TrackedConfig.accessed = set()
    t0 = time.monotonic()
    info = fn(Paths(**paths_d))
    return info, time.monotonic() - t0, peak_rss_mb(), sorted(TrackedConfig.accessed)


def run(graph_dir: Path | None = None, out_dir: Path | None = None, force=None,
        raw_dir: Path | None = None, rasters_dir: Path | None = None, config: Path | None = None) -> dict:
    """Run every stage that is stale (or forced), then write score_report.json.
    `force`: None = nothing forced, [] = all stages, else stage names."""
    from .score import CONFIG_DIR, load_config

    d = data_dir()
    paths = Paths(graph=str(graph_dir or d / "graph"), raw=str(raw_dir or d / "raw"),
                  rasters=str(rasters_dir or d / "rasters"), out=str(out_dir or d / "score"),
                  config=str(config or CONFIG_DIR / "score.toml"))
    Path(paths.out).mkdir(parents=True, exist_ok=True)
    report_path = paths.p("out", "score_report.json")
    prev = json.loads(report_path.read_text()) if report_path.exists() else {}
    stage_rep = dict(prev.get("stages", {}))
    force_all = force is not None and len(force) == 0
    forced = set(force or [])
    cfg = load_config(Path(paths.config))
    hashes = FileHashes(paths.p("rasters", ".file_hashes.json"))
    t_all = time.monotonic()
    for st in stages(paths):
        fp = stage_fingerprint(code_hash(st.fn, st.modules), config_hash(cfg, st.sections), st.inputs, hashes)
        stale = force_all or st.name in forced or not is_fresh(st, fp, hashes)
        if not stale:
            print(f"[{st.name}] up to date")
            stage_rep.setdefault(st.name, {})["ran"] = False
            continue
        print(f"[{st.name}] running")
        ctx = mp.get_context("spawn")
        with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as ex:
            info, secs, rss, used = ex.submit(_child, st.name, asdict(paths)).result()
        undeclared = sorted(set(used) - set(st.sections))
        if undeclared:
            raise RuntimeError(f"stage {st.name} read config sections {undeclared} it does not declare; "
                               "add them to its Stage(..., sections=...) so they count in its fingerprint")
        write_stamp(st, fp, hashes)
        hashes.save()
        stage_rep[st.name] = {"ran": True, "seconds": round(secs, 1), "peak_rss_mb": round(rss, 1),
                              "run_date": date.today().isoformat(), **info}
        print(f"[{st.name}] {secs:.1f} s, peak RSS {rss:.0f} MB")
    hashes.save()
    from .report import build_report

    report = build_report(paths, stage_rep, round(time.monotonic() - t_all, 1))
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {report_path}")
    return report
