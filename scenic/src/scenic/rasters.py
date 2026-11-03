"""Raster building blocks for the scenery score (docs/SCORE_SPEC.md sections 1 and 3).

Pure array functions (block means, disk filters, lake sizes, land cover
products) are kept separate from the stage functions that read the
downloaded layers and write GeoTIFFs, so the formulas can be tested on tiny
synthetic arrays.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from scipy import ndimage

WORK_CRS = "EPSG:5070"


# --------------------------------------------------------------------------
# Grid


@dataclass(frozen=True)
class Grid:
    """North-up grid: cell (r, c) covers x0 + c*res .. x0 + (c+1)*res and
    y0 - (r+1)*res .. y0 - r*res."""

    x0: float
    y0: float
    res: float
    width: int
    height: int

    @property
    def shape(self) -> tuple[int, int]:
        return self.height, self.width

    @property
    def transform(self):
        from affine import Affine

        return Affine(self.res, 0.0, self.x0, 0.0, -self.res, self.y0)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return self.x0, self.y0 - self.height * self.res, self.x0 + self.width * self.res, self.y0

    def coarsen(self, k: int) -> "Grid":
        return Grid(self.x0, self.y0, self.res * k, self.width // k, self.height // k)

    def rowcol(self, x, y) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Row, col (clipped to the grid) and an inside flag for points."""
        c = np.floor((np.asarray(x) - self.x0) / self.res).astype(np.int64)
        r = np.floor((self.y0 - np.asarray(y)) / self.res).astype(np.int64)
        inside = (r >= 0) & (r < self.height) & (c >= 0) & (c < self.width)
        return np.clip(r, 0, self.height - 1), np.clip(c, 0, self.width - 1), inside

    def centres(self, rows, cols) -> tuple[np.ndarray, np.ndarray]:
        return self.x0 + (np.asarray(cols) + 0.5) * self.res, self.y0 - (np.asarray(rows) + 0.5) * self.res

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> "Grid":
        return cls(**json.loads(s))


def make_grid(xmin: float, ymin: float, xmax: float, ymax: float, origin_x: float, origin_y: float,
              res: float, pad: float, block: int) -> Grid:
    """Grid aligned to (origin_x, origin_y) covering the box plus `pad`, with
    width and height multiples of `block`."""
    c0 = np.floor((xmin - pad - origin_x) / res)
    c1 = np.ceil((xmax + pad - origin_x) / res)
    r0 = np.floor((origin_y - (ymax + pad)) / res)
    r1 = np.ceil((origin_y - (ymin - pad)) / res)
    w = int(c1 - c0)
    h = int(r1 - r0)
    w += (-w) % block
    h += (-h) % block
    return Grid(float(origin_x + c0 * res), float(origin_y - r0 * res), float(res), w, h)


# --------------------------------------------------------------------------
# Array helpers


def block_mean(a: np.ndarray, k: int, valid: np.ndarray | None = None) -> np.ndarray:
    """k x k block mean (float32). With `valid`, the mean of valid cells only
    (NaN where a block has none). Shape must be divisible by k."""
    h, w = a.shape
    if h % k or w % k:
        raise ValueError(f"shape {a.shape} not divisible by {k}")
    if valid is None:
        s = a.reshape(h // k, k, w // k, k).sum(axis=(1, 3), dtype=np.float64)
        return (s / (k * k)).astype(np.float32)
    vals = np.where(valid, a, 0).reshape(h // k, k, w // k, k).sum(axis=(1, 3), dtype=np.float64)
    n = valid.reshape(h // k, k, w // k, k).sum(axis=(1, 3))
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(n > 0, vals / n, np.nan).astype(np.float32)


def block_count(mask: np.ndarray, k: int) -> np.ndarray:
    """Number of true cells per k x k block (uint8 for k <= 15)."""
    h, w = mask.shape
    return mask.reshape(h // k, k, w // k, k).sum(axis=(1, 3), dtype=np.uint16).astype(
        np.uint8 if k * k < 256 else np.uint16)


def disk_halfwidths(r: int) -> np.ndarray:
    """Half width of the disk footprint (dx^2 + dy^2 <= r^2) for dy = -r..r."""
    dy = np.arange(-r, r + 1)
    return np.floor(np.sqrt(r * r - dy * dy) + 1e-9).astype(int)


def disk_footprint(r: int) -> np.ndarray:
    y, x = np.mgrid[-r:r + 1, -r:r + 1]
    return x * x + y * y <= r * r


def _disk_reduce(a: np.ndarray, r: int, row_filter, combine, init) -> np.ndarray:
    """Disk-footprint filter as a union of horizontal runs: filter each row
    with a 1-D window of the run's width, then combine the rows shifted by
    dy. Boundary handling matches scipy.ndimage mode='reflect'."""
    hw = disk_halfwidths(r)
    rowf = {w: row_filter(a, 2 * w + 1) for w in np.unique(hw)}
    out = np.full(a.shape, init, dtype=np.result_type(a.dtype, np.float32) if init == 0 else a.dtype)
    h = a.shape[0]
    for k, dy in enumerate(range(-r, r + 1)):
        src = rowf[hw[k]]
        padded = np.pad(src, ((r, r), (0, 0)), mode="symmetric")  # == ndimage 'reflect'
        combine(out, padded[r + dy:r + dy + h])
    return out


def disk_max(a: np.ndarray, r: int) -> np.ndarray:
    """Same as ndimage.maximum_filter(a, footprint=disk_footprint(r)), but O(N r)."""
    return _disk_reduce(a, r, lambda x, s: ndimage.maximum_filter1d(x, s, axis=1, mode="reflect"),
                        lambda o, v: np.maximum(o, v, out=o), -np.inf)


def disk_min(a: np.ndarray, r: int) -> np.ndarray:
    return _disk_reduce(a, r, lambda x, s: ndimage.minimum_filter1d(x, s, axis=1, mode="reflect"),
                        lambda o, v: np.minimum(o, v, out=o), np.inf)


def disk_mean(a: np.ndarray, r: int) -> np.ndarray:
    """Same as ndimage.convolve(a, disk / disk.sum(), mode='reflect') (float64 sums)."""
    a64 = a.astype(np.float64)
    out = _disk_reduce(a64, r, lambda x, s: ndimage.uniform_filter1d(x, s, axis=1, mode="reflect") * s,
                       lambda o, v: np.add(o, v, out=o), 0)
    return (out / disk_footprint(r).sum()).astype(np.float32)


def fill_nearest(a: np.ndarray) -> np.ndarray:
    """Replace NaN cells with the value of the nearest non-NaN cell."""
    bad = np.isnan(a)
    if not bad.any():
        return a
    idx = ndimage.distance_transform_edt(bad, return_distances=False, return_indices=True)
    return a[tuple(idx)]


def lookup_table(table: dict, default: float = np.nan, size: int = 256) -> np.ndarray:
    lut = np.full(size, default, dtype=np.float32)
    for k, v in table.items():
        lut[int(k)] = v
    return lut


def class_mask(nlcd: np.ndarray, classes) -> np.ndarray:
    lut = np.zeros(256, bool)
    lut[list(classes)] = True
    return lut[nlcd]


# --------------------------------------------------------------------------
# Water


def size_score(area_m2, lo: float = 4.0, span: float = 4.0) -> np.ndarray:
    """clip((log10(area) - lo) / span, 0, 1): 1 ha -> 0, 1 km2 -> 0.5, 100 km2 -> 1."""
    with np.errstate(divide="ignore"):
        return np.clip((np.log10(np.asarray(area_m2, dtype=np.float64)) - lo) / span, 0, 1)


def water_size(area: np.ndarray, line: np.ndarray, coast: np.ndarray, cell_area: float,
               cfg: dict) -> np.ndarray:
    """Size score per cell (float32, 0 where no water).

    area: bool areal-water mask, line: uint8 (1 river, 2 canal, 3 stream),
    coast: bool coastline-band mask (forced to cfg['coastline_area_m2']).
    Components are 8-connected."""
    labels, n = ndimage.label(area, structure=np.ones((3, 3), bool))
    counts = np.bincount(labels.ravel(), minlength=n + 1).astype(np.float64)
    lab_score = size_score(counts * cell_area, cfg["size_log10_lo"], cfg["size_log10_span"]).astype(np.float32)
    lab_score[0] = 0
    out = lab_score[labels]
    del labels
    out[coast] = size_score(cfg["coastline_area_m2"], cfg["size_log10_lo"], cfg["size_log10_span"])
    ls = cfg["line_size"]
    line_lut = np.array([0, ls["river"], ls["canal"], ls["stream"]], dtype=np.float32)
    lin = (line > 0) & ~area
    out[lin] = line_lut[line[lin]]
    return out


# --------------------------------------------------------------------------
# Land cover products on the 90 m grid


def landcover_products(nlcd30: np.ndarray, cfg: dict) -> dict[str, np.ndarray]:
    """lscore90, mix90, open90, forest90 and dev90 from the 30 m class raster.

    Cells outside the class table (NLCD nodata 250, e.g. Canada) do not
    count: every fraction and mean is over valid cells only, and a window
    with no valid cell gets 0."""
    k = cfg["grid"]["block"]
    g = cfg["groups"]
    table = {int(c): float(v) for c, v in cfg["landcover"]["class_score"].items()}
    # Per-class cell counts in each 90 m block; everything below derives from them.
    counts = {c: block_count(nlcd30 == c, k).astype(np.float32) for c in table}
    n90 = sum(counts.values())

    def frac_sum(classes):
        return sum(counts[c] for c in classes if c in counts)

    score_sum = sum(v * counts[c] for c, v in table.items())

    def window_mean(num, size):
        num_w = ndimage.uniform_filter(num, size, mode="nearest")
        den_w = ndimage.uniform_filter(n90, size, mode="nearest")
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(den_w > 1e-6, num_w / den_w, 0).astype(np.float32)

    wl = cfg["landcover"]["window"]
    lscore = window_mean(score_sum, wl)
    p_f = window_mean(frac_sum(g["forest"]), wl)
    p_o = window_mean(frac_sum([c for c in g["open"] if c != 21]), wl)
    mix = (4 * p_f * p_o).astype(np.float32)
    open90 = window_mean(frac_sum(list(g["open"]) + list(g["water"])), cfg["views"]["open_window"])
    forest90 = window_mean(frac_sum(g["forest"]), cfg["views"]["open_window"])
    dev90 = window_mean(frac_sum(g["town"]), cfg["penalty"]["town_window"])
    return {"lscore90": lscore, "mix90": mix, "open90": open90, "forest90": forest90, "dev90": dev90}


def relief_tpi(dem90: np.ndarray, r: int) -> tuple[np.ndarray, np.ndarray]:
    relief = (disk_max(dem90, r) - disk_min(dem90, r)).astype(np.float32)
    tpi = (dem90 - disk_mean(dem90, r)).astype(np.float32)
    return relief, tpi


def far_min_max(dem90: np.ndarray, r: int) -> tuple[np.ndarray, np.ndarray]:
    """Lowest and highest ground within `r` cells (disk): what lies below
    and above a viewpoint at the scale of a long view (v0.1 prospect)."""
    return disk_min(dem90, r).astype(np.float32), disk_max(dem90, r).astype(np.float32)


def reach_from_frac(wfrac: np.ndarray, r_cells: int, res: float, w: dict) -> np.ndarray:
    """How far (m) a water cell counts for W, from the water fraction of the
    disk of `r_cells` around it: reach = k * sqrt(local water area), clipped.
    A shore of a broad lake (half the 1 km disk is water) reaches ~1.5 km, a
    100 m wide river ~0.5 km, a pond the minimum."""
    area = np.asarray(wfrac, dtype=np.float64) * np.pi * (r_cells * res) ** 2
    return np.clip(w["reach_k"] * np.sqrt(area), w["reach_min_m"], w["reach_max_m"]).astype(np.float32)


def water90_products(area30: np.ndarray, dem30: np.ndarray, k: int, res90: float, cfg: dict,
                     dem_nodata: int = -32768) -> dict[str, np.ndarray]:
    """90 m water products for v0.1:

    - wfrac90: water fraction of the 1 km disk around each 90 m cell.
    - reach90: W reach (m) of water in that cell (reach_from_frac).
    - water_z90: mean DEM over the water cells of the block (NaN: no water),
      i.e. the water surface without the shore land a plain block mean mixes in.
    - bigwater_idx90: flat index of the nearest broad-water 90 m cell (cell at
      least bigwater_cell_frac water, disk at least bigwater_disk_frac water),
      -1 when none within views.wv_max_m.
    - wv_reach90: how far broad water reads as a view (m), from the water
      area within views.wv_radius_cells: wv_k * sqrt(area), clipped to
      [wv_min_m, wv_max_m]."""
    w, v = cfg["water"], cfg["views"]
    frac = block_count(area30, k).astype(np.float32) / (k * k)
    wfrac = disk_mean(frac, w["reach_radius_cells"])
    reach = reach_from_frac(wfrac, w["reach_radius_cells"], res90, w)
    valid = area30 & (dem30 > dem_nodata)
    wz = block_mean(dem30, k, valid=valid)
    big = (frac >= v["bigwater_cell_frac"]) & (wfrac >= v["bigwater_disk_frac"]) & np.isfinite(wz)
    idx = np.full(frac.shape, -1, dtype=np.int32)
    if big.any():
        dist, (ir, ic) = ndimage.distance_transform_edt(~big, return_indices=True)
        ok = dist * res90 <= v["wv_max_m"]
        idx[ok] = (ir[ok].astype(np.int64) * frac.shape[1] + ic[ok]).astype(np.int32)
    rv = v["wv_radius_cells"]
    far_area = disk_mean(frac, rv).astype(np.float64) * np.pi * (rv * res90) ** 2
    wv_reach = np.clip(v["wv_k"] * np.sqrt(far_area), v["wv_min_m"], v["wv_max_m"]).astype(np.float32)
    return {"wfrac90": wfrac, "reach90": reach, "water_z90": wz.astype(np.float32), "bigwater_idx90": idx,
            "wv_reach90": wv_reach}


# --------------------------------------------------------------------------
# GeoTIFF IO


def write_tif(path: Path, arr: np.ndarray, grid: Grid, nodata=None) -> Path:
    import rasterio

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    profile = dict(driver="GTiff", width=grid.width, height=grid.height, count=1, dtype=arr.dtype.name,
                   crs=WORK_CRS, transform=grid.transform, compress="deflate", tiled=True,
                   blockxsize=512, blockysize=512, BIGTIFF="IF_SAFER", num_threads="ALL_CPUS")
    if nodata is not None:
        profile["nodata"] = nodata
    if arr.dtype.kind == "f":
        profile["predictor"] = 3
    elif arr.dtype.itemsize > 1:
        profile["predictor"] = 2
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(arr, 1)
    tmp.replace(path)
    return path


def read_tif(path: Path) -> np.ndarray:
    import rasterio

    with rasterio.open(path) as src:
        return src.read(1)


# --------------------------------------------------------------------------
# Stages that read the downloaded layers


def build_dem(grid: Grid, tiles: list[Path], nodata: int = -32768) -> np.ndarray:
    """Warp the 3DEP tiles (EPSG:4269) onto the 30 m grid, bilinear, int16 m.
    Each tile is warped into its own window of the grid; tile overlaps keep
    the first value written."""
    import rasterio
    from rasterio.warp import Resampling, reproject, transform_bounds

    dem = np.full(grid.shape, nodata, dtype=np.int16)
    for path in tiles:
        with rasterio.open(path) as src:
            b = transform_bounds(src.crs, WORK_CRS, *src.bounds, densify_pts=41)
            r0 = max(int(np.floor((grid.y0 - b[3]) / grid.res)) - 2, 0)
            c0 = max(int(np.floor((b[0] - grid.x0) / grid.res)) - 2, 0)
            r1 = min(int(np.ceil((grid.y0 - b[1]) / grid.res)) + 2, grid.height)
            c1 = min(int(np.ceil((b[2] - grid.x0) / grid.res)) + 2, grid.width)
            if r1 <= r0 or c1 <= c0:
                continue
            tmp = np.full((r1 - r0, c1 - c0), np.nan, dtype=np.float32)
            from affine import Affine

            t = grid.transform * Affine.translation(c0, r0)
            reproject(rasterio.band(src, 1), tmp, dst_transform=t, dst_crs=WORK_CRS,
                      resampling=Resampling.bilinear, src_nodata=src.nodata, dst_nodata=np.nan,
                      num_threads=4)
            view = dem[r0:r1, c0:c1]
            fill = (view == nodata) & np.isfinite(tmp)
            view[fill] = np.round(tmp[fill]).astype(np.int16)
    return dem


def read_nlcd_window(grid: Grid, tif: Path, nodata: int = 250) -> tuple[np.ndarray, dict]:
    """Read the grid's window from the CONUS NLCD raster (no resampling; the
    grid is on the NLCD 30 m lattice)."""
    import rasterio
    from rasterio.windows import from_bounds

    with rasterio.open(tif) as src:
        t = src.transform
        if abs(t.a - grid.res) > 1e-6 or abs(t.e + grid.res) > 1e-6:
            raise RuntimeError(f"NLCD resolution {t.a} x {t.e} is not {grid.res} m")
        fx = (grid.x0 - t.c) / grid.res
        fy = (t.f - grid.y0) / grid.res
        if abs(fx - round(fx)) > 1e-6 or abs(fy - round(fy)) > 1e-6:
            raise RuntimeError(f"NLCD origin ({t.c}, {t.f}) is not on the working grid lattice")
        win = from_bounds(*grid.bounds, transform=t).round_offsets().round_lengths()
        arr = src.read(1, window=win, boundless=True, fill_value=nodata)
        info = {"crs": src.crs.to_string() if src.crs else None, "transform": list(t)[:6],
                "nodata": src.nodata, "shape": [src.height, src.width]}
    if arr.shape != grid.shape:
        raise RuntimeError(f"NLCD window {arr.shape} != grid {grid.shape}")
    return arr.astype(np.uint8), info
