"""Scenery score outputs: heatmap, summary.md and score_report.json."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np

SUMMARY_COLUMNS = ("W", "H", "V", "L", "Q", "C", "penalty", "raw", "smooth", "S")
SIGNALS = ("W", "H", "V", "L", "Q", "C")

# Choices the spec leaves open or that had to change; copied into score_report.json.
DEVIATIONS = [
    "NLCD: the spec's S3 COG (Annual_NLCD_LndCov_2023_CU_C1V0.tif) is now a 42-byte placeholder. "
    "Used Annual NLCD Collection 1.1 (C1V1) 2023 land cover from the MRLC download server "
    "(data-bundles zip, 1.43 GB), whole CONUS file; same year and 30 m grid lattice.",
    "NLCD nodata (250, e.g. Canada) is left out of every land cover fraction and mean (lscore, mix, open, dev); "
    "a window with no valid cell scores 0.",
    "DEM: 3DEP tile n41w072 does not exist (open water). Cells with no DEM are filled with the nearest valid "
    "90 m value (dem90); a point with no dem30 value uses dem90.",
    "Disk filters (relief max/min, TPI mean) are computed exactly as the union of horizontal runs of the disk "
    "(1-D filters per row width), identical to scipy.ndimage with the disk footprint and mode='reflect' but O(N r).",
    "Junction and signal counts in the smoothing window are length-weighted: edge j contributes "
    "w_ij / length_j of its count (a junction node shared by two edges counts half on each).",
    "Signal nodes go to the edge of their single nearest sample point within 25 m.",
    "Byway source: NYSDOT 'Scenic Byways' layer served by gisportaldev.dot.ny.gov (ArcGIS REST, GeoJSON). "
    "It also contains the NYC and Long Island historic parkways; results are reported for all features and "
    "for the designated State/National Scenic Byway features alone. An edge counts as byway when at least half "
    "of its sample points lie within 30 m of a byway line; excluded edges are left out.",
    "Summary statistics are over non-excluded edges.",
    "NLCD C1V1 declares an Albers CRS on a WGS84 datum with the EPSG:5070 parameters; treated as EPSG:5070 "
    "as the spec says. Its origin (-2415585, 3314805) is on the spec's 30 m lattice, so no resampling.",
    "3DEP contains Hudson River bathymetry (down to -89 m near the Hudson Highlands); left as is, so relief "
    "next to the deep Hudson is slightly inflated. Lakes (Ontario, Erie, Finger Lakes) are flat surfaces.",
    "Smoothing BFS uses numpy sort/searchsorted joins instead of pandas merge (allowed by the spec); "
    "the adjacency and BFS skip excluded edges.",
    "config/byways.toml (ref fallback, unused this run) uses lon/lat boxes instead of county sets: no county "
    "polygons are on disk.",
    "Spot-check boxes in config/spot_checks.toml were set from the two named towns; an edge matches by ref "
    "(any of its ;-separated refs) with its middle vertex in the box.",
    "Stage children run with GDAL_CACHEMAX=256 MB; GDAL's default (5 % of RAM) added about 3.4 GB of RSS.",
    "v0.1 replaces the spec's W, V and C definitions (docs/SCORE_SPEC.md, 'v0.1 changes'): W reach scales with "
    "the water around the shore and visibility is a terrain-and-canopy sight line, with a fan of rays; V adds "
    "prospect, broad water in view and a forest baseline; C is scored over the 1 km smoothing window.",
    "Holdout spot checks (config/spot_checks.toml, [holdout]) were added before any v0.1 formula change and "
    "their v0 results kept in data/score_v0/holdout_v0.json; they were not consulted while tuning.",
]


def wquantile(x: np.ndarray, w: np.ndarray, q) -> np.ndarray:
    """Weighted quantile (inverse of the weighted CDF at midpoints)."""
    x = np.asarray(x, dtype=np.float64)
    w = np.asarray(w, dtype=np.float64)
    ok = np.isfinite(x) & (w > 0)
    x, w = x[ok], w[ok]
    if len(x) == 0:
        return np.full(np.shape(q), np.nan)
    o = np.argsort(x)
    x, w = x[o], w[o]
    cw = np.cumsum(w) - 0.5 * w
    return np.interp(np.asarray(q) * w.sum(), cw, x)


def wmean(x, w) -> float:
    x = np.asarray(x, dtype=np.float64)
    ok = np.isfinite(x)
    return float(np.sum(x[ok] * w[ok]) / np.sum(w[ok])) if ok.any() else float("nan")


def heatmap(scores, samples, length_m: np.ndarray, out: Path, cell_m: float = 500.0,
            width_px: int = 3000) -> Path:
    """Length-weighted mean S per `cell_m` cell from the sample points."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    S = scores.column("S").to_numpy()
    excl = scores.column("excluded").to_numpy()
    n = scores.column("n_samples").to_numpy().astype(np.float64)
    x, y, e = samples["x"].astype(np.float64), samples["y"].astype(np.float64), samples["edge"]
    keep = ~excl[e]
    x, y, e = x[keep], y[keep], e[keep]
    w = (np.asarray(length_m, dtype=np.float64) / n)[e]
    x0, y1 = np.floor(x.min() / cell_m) * cell_m, np.ceil(y.max() / cell_m) * cell_m
    nx = int(np.ceil((x.max() - x0) / cell_m)) + 1
    ny = int(np.ceil((y1 - y.min()) / cell_m)) + 1
    c = ((x - x0) // cell_m).astype(np.int64)
    r = ((y1 - y) // cell_m).astype(np.int64)
    idx = r * nx + c
    sw = np.bincount(idx, weights=w, minlength=nx * ny)
    sws = np.bincount(idx, weights=w * S[e], minlength=nx * ny)
    with np.errstate(invalid="ignore", divide="ignore"):
        img = np.where(sw > 0, sws / sw, np.nan).reshape(ny, nx)

    dpi = 300
    fig_w = width_px / dpi
    fig_h = fig_w * ny / nx * 1.02 + 0.5
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=dpi)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("#e9e9e9")
    im = ax.imshow(img, cmap="viridis", vmin=0, vmax=1, interpolation="nearest",
                   extent=(x0, x0 + nx * cell_m, y1 - ny * cell_m, y1))
    ax.set_xticks([])
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.set_title(f"Scenery score v0.1, New York State: length-weighted mean S per {cell_m:.0f} m cell "
                 f"(run {date.today().isoformat()})", fontsize=6)
    cb = fig.colorbar(im, ax=ax, fraction=0.025, pad=0.01)
    cb.ax.tick_params(labelsize=5)
    cb.set_label("S (percentile of road length)", fontsize=5)
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=dpi)
    plt.close(fig)
    return out


def summary_stats(scores, length_m: np.ndarray) -> dict:
    excl = scores.column("excluded").to_numpy()
    w = np.where(excl, 0.0, length_m)
    out = {}
    for c in SUMMARY_COLUMNS:
        x = scores.column(c).to_numpy().astype(np.float64)
        p10, p50, p90 = wquantile(x, w, [0.1, 0.5, 0.9])
        ok = np.isfinite(x) & (w > 0)
        out[c] = {"mean": wmean(np.where(w > 0, x, np.nan), w), "p10": float(p10), "p50": float(p50),
                  "p90": float(p90), "zero_share": float(w[ok & (x == 0)].sum() / w[ok].sum())}
    X = np.column_stack([scores.column(c).to_numpy().astype(np.float64) for c in SIGNALS])
    ok = ~excl & np.all(np.isfinite(X), axis=1)
    cov = np.cov(X[ok].T, aweights=length_m[ok])
    sd = np.sqrt(np.diag(cov))
    with np.errstate(invalid="ignore", divide="ignore"):
        corr = cov / np.outer(sd, sd)
    return {"columns": out, "corr": corr.tolist(), "excluded_km": float(length_m[excl].sum() / 1000),
            "total_km": float(length_m.sum() / 1000)}


def summary_md(scores, length_m: np.ndarray, out: Path) -> Path:
    st = summary_stats(scores, length_m)
    lines = [f"# Scenery score v0.1 summary ({date.today().isoformat()})", "",
             f"{scores.num_rows:,} edges, {st['total_km']:,.0f} km; excluded (unpaved or seasonal) "
             f"{st['excluded_km']:,.0f} km. Statistics are length-weighted over non-excluded edges.", "",
             "| Column | Mean | p10 | p50 | p90 | Share of length at 0 |", "|---|---|---|---|---|---|"]
    for c, s in st["columns"].items():
        lines.append(f"| {c} | {s['mean']:.3f} | {s['p10']:.3f} | {s['p50']:.3f} | {s['p90']:.3f} | "
                     f"{100 * s['zero_share']:.1f}% |")
    lines += ["", "Pearson correlation of the signals (length-weighted):", "",
              "| | " + " | ".join(SIGNALS) + " |", "|---" * (len(SIGNALS) + 1) + "|"]
    for i, a in enumerate(SIGNALS):
        lines.append(f"| {a} | " + " | ".join(f"{st['corr'][i][j]:+.2f}" for j in range(len(SIGNALS))) + " |")
    out.write_text("\n".join(lines) + "\n")
    return out


def _size(p: Path) -> int | None:
    return p.stat().st_size if p.exists() else None


def build_report(paths, stage_rep: dict, total_s: float) -> dict:
    import importlib.metadata as md

    from .rasters import Grid

    raw = Path(paths.raw)
    layers = json.loads((raw / "layers.json").read_text()) if (raw / "layers.json").exists() else {}
    dem = layers.get("dem", {})
    grid_p = Path(paths.rasters) / "grid.json"
    grid = Grid.from_json(grid_p.read_text()) if grid_p.exists() else None
    val_p = Path(paths.out) / "validation.json"
    versions = {}
    for pkg in ("numpy", "scipy", "pyarrow", "rasterio", "pyproj", "shapely", "osmium", "pandas", "matplotlib"):
        try:
            versions[pkg] = md.version(pkg)
        except md.PackageNotFoundError:
            pass
    try:
        import rasterio

        versions["gdal"] = rasterio.__gdal_version__
    except ImportError:
        pass
    pbf = raw / "new-york-latest.osm.pbf"
    md5 = raw / "new-york-latest.osm.pbf.md5"
    n_edges = None
    try:
        import pyarrow.parquet as pq

        n_edges = pq.read_metadata(Path(paths.graph) / "edges.parquet").num_rows
    except Exception:  # noqa: BLE001
        pass
    from .graph import peak_rss_mb

    return {
        "run_date": date.today().isoformat(),
        "inputs": {
            "osm_pbf": {"path": str(pbf), "size": _size(pbf),
                        "md5": md5.read_text().split()[0] if md5.exists() else None},
            "edges": {"path": str(Path(paths.graph) / "edges.parquet"),
                      "size": _size(Path(paths.graph) / "edges.parquet"), "rows": n_edges},
            "dem": {"product": dem.get("product"), "url_template": "https://prd-tnm.s3.amazonaws.com/StagedProducts/"
                    "Elevation/1/TIFF/current/{tile}/USGS_1_{tile}.tif",
                    "tiles": sum(t["status"] != "missing" for t in dem.get("tiles", [])),
                    "missing_tiles": [t["tile"] for t in dem.get("tiles", []) if t["status"] == "missing"],
                    "bytes": dem.get("bytes"),
                    "md5_verified": sum(bool(t.get("md5_verified")) for t in dem.get("tiles", []))},
            "nlcd": {k: layers.get("nlcd", {}).get(k) for k in ("url", "size", "etag", "tif", "tif_size", "year")},
            "byways": layers.get("byways"),
            "config": str(paths.config),
        },
        "versions": versions,
        "grid": None if grid is None else {"crs": "EPSG:5070", "shape_30m": list(grid.shape),
                                           "shape_90m": list(grid.coarsen(3).shape), "bounds": list(grid.bounds)},
        "edges": n_edges,
        "stages": stage_rep,
        "total_seconds_this_run": total_s,
        "peak_rss_mb_max_stage": max((s.get("peak_rss_mb", 0) for s in stage_rep.values()), default=None),
        "runner_peak_rss_mb": round(peak_rss_mb(), 1),
        "deviations": DEVIATIONS,
        "validation": json.loads(val_p.read_text()) if val_p.exists() else None,
    }
