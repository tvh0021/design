"""Stage runner for the scenery score (see scenic.score for the stage list)."""

from __future__ import annotations

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


def _cfg(paths: Paths) -> dict:
    from .score import load_config

    return load_config(Path(paths.config))


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


def st_terrain90(paths: Paths) -> dict:
    from .rasters import read_tif, relief_tpi, write_tif

    cfg = _cfg(paths)
    g90 = _grid(paths).coarsen(cfg["grid"]["block"])
    dem90 = read_tif(paths.p("rasters", "dem90.tif"))
    relief, tpi = relief_tpi(dem90, cfg["hills"]["disk_radius_cells"])
    write_tif(paths.p("rasters", "relief90.tif"), relief, g90)
    write_tif(paths.p("rasters", "tpi90.tif"), tpi, g90)
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
    import pyarrow as pa
    import pyarrow.parquet as pq
    from scipy.spatial import cKDTree

    from .rasters import class_mask, read_tif
    from .score import (curvature, excluded_mask, hills_edges, land_points, lookup, project_lonlat, quiet,
                        read_edges, road_keys, sample_points, views_points, water_points)

    cfg = _cfg(paths)
    g30 = _grid(paths)
    g90 = g30.coarsen(cfg["grid"]["block"])
    t0 = time.monotonic()
    E = read_edges(Path(paths.graph))
    s = sample_points(E.off, E.x, E.y, E.length_m, cfg["sampling"]["step_m"])
    timing = {"sample_s": round(time.monotonic() - t0, 1)}
    _log(f"{len(s.x):,} sample points on {E.n:,} edges")

    t = time.monotonic()
    area = read_tif(paths.p("rasters", "water_area.tif")).astype(bool)
    area |= read_tif(paths.p("rasters", "water_line.tif")) > 0
    blocking = class_mask(read_tif(paths.p("rasters", "nlcd30.tif")), cfg["groups"]["blocking"])
    size = read_tif(paths.p("rasters", "water_size.tif"))
    W_pt, d_pt = water_points(s, g30, area, lambda idx: size.ravel()[idx], blocking, cfg)
    del area, blocking, size
    timing["water_s"] = round(time.monotonic() - t, 1)

    t = time.monotonic()
    dem90 = read_tif(paths.p("rasters", "dem90.tif"))
    dem30 = read_tif(paths.p("rasters", "dem30.tif"))
    z = lookup(g30, dem30, s.x, s.y, fallback=-32768)
    del dem30
    bad = z <= -32768
    z[bad] = lookup(g90, dem90, s.x[bad], s.y[bad], fallback=0.0)
    del dem90
    relief_pt = lookup(g90, read_tif(paths.p("rasters", "relief90.tif")), s.x, s.y)
    H, h_relief, h_grade = hills_edges(s, relief_pt, z, E.length_m, cfg)
    V_pt = views_points(lookup(g90, read_tif(paths.p("rasters", "open90.tif")), s.x, s.y),
                        lookup(g90, read_tif(paths.p("rasters", "tpi90.tif")), s.x, s.y), cfg)
    L_pt = land_points(lookup(g90, read_tif(paths.p("rasters", "lscore90.tif")), s.x, s.y),
                       lookup(g90, read_tif(paths.p("rasters", "mix90.tif")), s.x, s.y), cfg)
    dev_pt = lookup(g90, read_tif(paths.p("rasters", "dev90.tif")), s.x, s.y)
    timing["rasters_s"] = round(time.monotonic() - t, 1)

    t = time.monotonic()
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
        "V": s.mean(V_pt), "L": s.mean(L_pt), "Q": quiet(E.highway, cfg), "C": C, "dev": s.mean(dev_pt),
        "signals": sig, "junction_halves": junction_halves,
        "n_samples": s.n.astype(np.int32), "water_dist_m": s.mean(wd),
        "excluded": excluded_mask(E.surface, E.name, cfg),
        "key": road_keys(E.ref, E.name, E.way_id),
    }
    pq.write_table(pa.table({k: pa.array(v) for k, v in cols.items()}), paths.p("out", "edge_signals.parquet"),
                   compression="zstd")
    np.savez(paths.p("out", "samples.npz"), x=s.x.astype(np.float32), y=s.y.astype(np.float32), edge=s.edge)
    return {"sample_points": int(len(s.x)), "signal_nodes_matched": matched,
            "points_with_water_hit": round(float(np.isfinite(d_pt).mean()), 4), **timing}


def st_score(paths: Paths) -> dict:
    import pyarrow.parquet as pq

    from .score import _dict_col, combine, scores_table, write_scores

    cfg = _cfg(paths)
    sig_t = pq.read_table(paths.p("out", "edge_signals.parquet"))
    sig = {c: sig_t.column(c).to_numpy() for c in sig_t.column_names}
    et = pq.read_table(paths.p("graph", "edges.parquet"),
                       columns=["u", "v", "way_id", "length_m", "highway", "maxspeed_tagged", "speed_mph"])
    if et.num_rows != sig_t.num_rows:
        raise RuntimeError("edge_signals.parquet does not match edges.parquet; rerun the edges stage")
    extra = {c: et.column(c).to_numpy() for c in ("u", "v", "way_id", "length_m", "maxspeed_tagged", "speed_mph")}
    extra["highway"] = _dict_col(et, "highway")
    extra["key"] = sig["key"]
    active = ~sig["excluded"]
    res = combine(sig, extra, active, cfg)
    res.update({c: sig[c] for c in ("W", "H", "V", "L", "Q", "C", "n_samples", "water_dist_m", "excluded")})
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

    t = _load_scores(paths)
    L = pq.read_table(paths.p("graph", "edges.parquet"), columns=["length_m"]).column("length_m").to_numpy()
    smp = np.load(paths.p("out", "samples.npz"))
    cfg = _cfg(paths)
    heatmap(t, smp, L, paths.p("out", "heatmap.png"), cfg["output"]["heatmap_cell_m"],
            cfg["output"]["heatmap_width_px"])
    summary_md(t, L, paths.p("out", "summary.md"))
    return {"heatmap": str(paths.p("out", "heatmap.png")), "summary": str(paths.p("out", "summary.md"))}


def st_validate(paths: Paths) -> dict:
    from .validate import run_validation

    res = run_validation(Path(paths.graph), Path(paths.out), Path(paths.raw), Path(paths.config).parent)
    paths.p("out", "validation.json").write_text(json.dumps(res, indent=2) + "\n")
    return {"byways_pass": res.get("byways", {}).get("pass"), "spot_scenic_pass": res["spot_checks"]["scenic"]["pass"],
            "spot_dull_pass": res["spot_checks"]["dull"]["pass"]}


def _load_scores(paths: Paths):
    from .score import load

    return load(paths.p("graph", "edges.parquet"), paths.p("out", "scores.parquet"))


# --------------------------------------------------------------------------
# Runner


def stages(paths: Paths) -> list[tuple[str, object, list[Path], list[Path]]]:
    R = lambda n: paths.p("rasters", n)  # noqa: E731
    O = lambda n: paths.p("out", n)  # noqa: E731
    cfgp = Path(paths.config)
    edges = paths.p("graph", "edges.parquet")
    nlcd_in = sorted(Path(paths.raw, "nlcd").glob("Annual_NLCD_LndCov_*_CU_*.tif"))[-1:]
    byway_in = sorted(Path(paths.raw, "byways").glob("*.geojson"))
    rasters90 = [R(n) for n in ("relief90.tif", "tpi90.tif", "lscore90.tif", "mix90.tif", "open90.tif", "dev90.tif")]
    water = [R(n) for n in ("water_area.tif", "water_line.tif", "water_size.tif")]
    return [
        ("grid", st_grid, [edges, cfgp], [R("grid.json")]),
        ("dem", st_dem, [R("grid.json"), *dem_tiles(paths)], [R("dem30.tif"), R("dem90.tif")]),
        ("nlcd", st_nlcd, [R("grid.json"), *nlcd_in], [R("nlcd30.tif")]),
        ("water", st_water, [R("grid.json"), R("nlcd30.tif"), paths.pbf, cfgp], water),
        ("terrain90", st_terrain90, [R("dem90.tif"), cfgp], rasters90[:2]),
        ("land90", st_land90, [R("nlcd30.tif"), cfgp], rasters90[2:]),
        ("controls", st_controls, [paths.pbf], [O("signal_nodes.npy")]),
        ("edges", st_edges, [edges, R("dem30.tif"), R("nlcd30.tif"), *water, *rasters90, O("signal_nodes.npy"), cfgp],
         [O("edge_signals.parquet"), O("samples.npz")]),
        ("score", st_score, [O("edge_signals.parquet"), edges, cfgp], [O("scores.parquet")]),
        ("outputs", st_outputs, [O("scores.parquet"), O("samples.npz"), cfgp], [O("heatmap.png"), O("summary.md")]),
        ("validate", st_validate,
         [O("scores.parquet"), O("samples.npz"), *byway_in, cfgp.parent / "spot_checks.toml",
          cfgp.parent / "byways.toml"],
         [O("validation.json")]),
    ]


def fresh(inputs: list[Path], outputs: list[Path]) -> bool:
    if not all(p.exists() for p in outputs):
        return False
    newest_in = max((p.stat().st_mtime for p in inputs if p.exists()), default=0.0)
    return min(p.stat().st_mtime for p in outputs) > newest_in


def _child(name: str, paths_d: dict) -> tuple[dict, float, float]:
    import os

    # GDAL's default block cache is 5 % of RAM (3+ GB here); the stages read
    # whole rasters once, so a small cache is enough.
    os.environ.setdefault("GDAL_CACHEMAX", "256")
    from .graph import peak_rss_mb

    fn = dict((n, f) for n, f, _, _ in stages(Paths(**paths_d)))[name]
    t0 = time.monotonic()
    info = fn(Paths(**paths_d))
    return info, time.monotonic() - t0, peak_rss_mb()


def run(graph_dir: Path | None = None, out_dir: Path | None = None, force=None,
        raw_dir: Path | None = None, rasters_dir: Path | None = None, config: Path | None = None) -> dict:
    """Run every stage that is stale (or forced), then write score_report.json.
    `force`: None = nothing forced, [] = all stages, else stage names."""
    from .score import CONFIG_DIR

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
    t_all = time.monotonic()
    for name, _fn, ins, outs in stages(paths):
        stale = force_all or name in forced or not fresh(ins, outs)
        if not stale:
            print(f"[{name}] up to date")
            stage_rep.setdefault(name, {})["ran"] = False
            continue
        print(f"[{name}] running")
        ctx = mp.get_context("spawn")
        with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as ex:
            info, secs, rss = ex.submit(_child, name, asdict(paths)).result()
        stage_rep[name] = {"ran": True, "seconds": round(secs, 1), "peak_rss_mb": round(rss, 1),
                           "run_date": date.today().isoformat(), **info}
        print(f"[{name}] {secs:.1f} s, peak RSS {rss:.0f} MB")
    from .report import build_report

    report = build_report(paths, stage_rep, round(time.monotonic() - t_all, 1))
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {report_path}")
    return report
