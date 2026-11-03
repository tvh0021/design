"""Download the raster and reference layers the scenery score needs.

- USGS 3DEP 1 arc-second DEM, 1x1 degree GeoTIFF tiles (public S3, no key).
- Annual NLCD Collection 1 land cover, CONUS, one year (MRLC, no key).
- NYSDOT scenic byway lines (ArcGIS REST, GeoJSON export, no key), used only
  to validate the score.

Everything is streamed to `data/raw/<layer>/` through a `.part` file. A
download resumes from the `.part` with an HTTP Range request, a finished file
is skipped when its size matches the server's Content-Length, and S3 files are
checked against their ETag when it is a plain md5. `layers.json` in
`data/raw/` records what was fetched (URL, size, checksum, time).
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import data_dir
from .fetch import CHUNK, USER_AGENT, _progress

DEM_URL = ("https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1/TIFF/current/"
           "{tile}/USGS_1_{tile}.tif")
# New York: tiles named by the NW corner, lat 41..46 N, lon 72..80 W.
DEM_LATS = range(41, 47)
DEM_LONS = range(72, 81)

# The spec's MRLC S3 COG (Annual_NLCD_LndCov_2023_CU_C1V0.tif) is now a
# 42-byte placeholder. Collection 1.1 (C1V1) is published as zipped GeoTIFFs
# on the MRLC download server; the zip holds the single-band CONUS raster.
NLCD_YEAR = 2023
NLCD_URL = "https://www.mrlc.gov/downloads/sciweb1/shared/mrlc/data-bundles/Annual_NLCD_LndCov_{year}_CU_C1V1.zip"

# Byways: NYSDOT layer first, then the USDOT national compilation (NY layer).
BYWAY_SOURCES = (
    ("nysdot", "https://gisportaldev.dot.ny.gov/server/rest/services/CulturalResources/"
               "Cultural_Resources/MapServer/13/query"),
    ("usdot", "https://geo.dot.gov/server/rest/services/US_Scenic_Byways/MapServer/67/query"),
)


def _request(url: str, headers: dict | None = None, method: str = "GET", timeout: float = 60):
    h = {"User-Agent": USER_AGENT, **(headers or {})}
    return urllib.request.urlopen(urllib.request.Request(url, headers=h, method=method), timeout=timeout)


def remote_info(url: str) -> dict | None:
    """HEAD: {'size', 'etag'}; None on HTTP 404/403 (missing tile)."""
    try:
        with _request(url, method="HEAD") as r:
            return {"size": int(r.headers.get("Content-Length") or 0) or None,
                    "etag": (r.headers.get("ETag") or "").strip('"')}
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return None
        raise


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _is_md5(etag: str) -> bool:
    return len(etag) == 32 and all(c in "0123456789abcdef" for c in etag.lower())


def download_resumable(url: str, dest: Path, info: dict | None = None, quiet: bool = False) -> dict:
    """Stream `url` to `dest`. Skips when `dest` already has the remote size;
    resumes a `.part` file with a Range request. Verifies size, and md5 when
    the ETag is one. Returns a manifest entry."""
    info = info or remote_info(url)
    if info is None:
        raise FileNotFoundError(url)
    size, etag = info["size"], info.get("etag", "")
    entry = {"url": url, "path": str(dest), "size": size, "etag": etag}
    if dest.exists() and size and dest.stat().st_size == size:
        return {**entry, "status": "present"}
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if size and have > size:
        part.unlink()
        have = 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    t0 = time.monotonic()
    last = 0.0
    with _request(url, headers=headers, timeout=120) as r:
        mode = "ab" if (have and r.status == 206) else "wb"
        done = have if mode == "ab" else 0
        with open(part, mode) as f:
            while chunk := r.read(CHUNK):
                f.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if not quiet and now - last > 0.5:
                    _progress(done, size, t0)
                    last = now
    if not quiet:
        _progress(done, size, t0)
        sys.stderr.write("\n")
    got = part.stat().st_size
    if size and got != size:
        raise RuntimeError(f"{url}: got {got} bytes, expected {size}; rerun to resume")
    if _is_md5(etag):
        md5 = _md5(part)
        if md5 != etag.lower():
            part.unlink()
            raise RuntimeError(f"{url}: md5 {md5} does not match ETag {etag}")
        entry["md5_verified"] = True
    part.replace(dest)
    return {**entry, "status": "downloaded", "seconds": round(time.monotonic() - t0, 1)}


def dem_tiles() -> list[str]:
    return [f"n{n}w{w:03d}" for n in DEM_LATS for w in DEM_LONS]


def fetch_dem(raw_dir: Path, workers: int = 4) -> list[dict]:
    """All 54 New York 3DEP tiles; 404s (open water) are recorded as missing."""
    out_dir = raw_dir / "dem"

    def one(tile: str) -> dict:
        url = DEM_URL.format(tile=tile)
        dest = out_dir / f"USGS_1_{tile}.tif"
        try:
            info = remote_info(url)
        except (urllib.error.URLError, TimeoutError) as e:
            if dest.exists():
                return {"tile": tile, "url": url, "path": str(dest), "status": "present-offline",
                        "size": dest.stat().st_size, "warning": str(e)}
            raise
        if info is None:
            return {"tile": tile, "url": url, "status": "missing"}
        e = download_resumable(url, dest, info, quiet=True)
        sys.stderr.write(f"  dem {tile}: {e['status']} ({(e['size'] or 0) / 1e6:.1f} MB)\n")
        return {"tile": tile, **e}

    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(one, dem_tiles()))


def nlcd_tif_name(year: int = NLCD_YEAR) -> str:
    return f"Annual_NLCD_LndCov_{year}_CU_C1V1.tif"


def fetch_nlcd(raw_dir: Path, year: int = NLCD_YEAR) -> dict:
    """Download the CONUS zip and extract its land cover GeoTIFF."""
    out_dir = raw_dir / "nlcd"
    url = NLCD_URL.format(year=year)
    zpath = out_dir / url.rsplit("/", 1)[-1]
    tif = out_dir / nlcd_tif_name(year)
    if tif.exists() and not zpath.exists():
        return {"url": url, "path": str(tif), "status": "present", "size": tif.stat().st_size, "year": year}
    entry = download_resumable(url, zpath)
    with zipfile.ZipFile(zpath) as z:
        members = [m for m in z.infolist() if m.filename.lower().endswith(".tif")]
        if len(members) != 1:
            raise RuntimeError(f"{zpath}: expected one .tif, found {[m.filename for m in members]}")
        m = members[0]
        if not (tif.exists() and tif.stat().st_size == m.file_size):
            sys.stderr.write(f"  extracting {m.filename} ({m.file_size / 1e6:.0f} MB)\n")
            part = tif.with_name(tif.name + ".part")
            with z.open(m) as src, open(part, "wb") as dst:
                while chunk := src.read(CHUNK):
                    dst.write(chunk)
            part.replace(tif)  # zipfile checks the CRC on read
        # Sidecars (.aux.xml / .xml metadata) are small; extract them too.
        for side in z.infolist():
            if side is not m and side.filename.endswith((".tfw", ".aux.xml", ".ovr")):
                side_dest = out_dir / Path(side.filename).name
                with z.open(side) as src:
                    side_dest.write_bytes(src.read())
    return {**entry, "tif": str(tif), "tif_size": tif.stat().st_size, "year": year,
            "member": m.filename}


def fetch_byways(raw_dir: Path) -> dict:
    """GeoJSON of scenic byway lines (WGS84). Falls back down BYWAY_SOURCES;
    if none answers, validation uses config/byways.toml."""
    out_dir = raw_dir / "byways"
    errors = {}
    for name, base in BYWAY_SOURCES:
        dest = out_dir / f"{name}_scenic_byways.geojson"
        if dest.exists() and dest.stat().st_size > 1000:
            return {"source": name, "url": base, "path": str(dest), "status": "present",
                    "size": dest.stat().st_size}
        q = urllib.parse.urlencode({"where": "1=1", "outFields": "*", "outSR": 4326, "f": "geojson"})
        try:
            with _request(f"{base}?{q}", timeout=120) as r:
                body = r.read()
            gj = json.loads(body)
            if not gj.get("features"):
                raise RuntimeError("no features")
        except Exception as e:  # noqa: BLE001 - any failure means try the next source
            errors[name] = repr(e)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return {"source": name, "url": base, "path": str(dest), "status": "downloaded",
                "size": len(body), "features": len(gj["features"]), "errors": errors}
    return {"source": "config/byways.toml", "status": "fallback", "errors": errors}


def fetch_layers(raw_dir: Path | None = None, dem: bool = True, nlcd: bool = True,
                 byways: bool = True) -> dict:
    raw_dir = Path(raw_dir or data_dir() / "raw")
    manifest_path = raw_dir / "layers.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    if dem:
        print("3DEP 1 arc-second tiles")
        tiles = fetch_dem(raw_dir)
        # Keep the md5 check result of earlier downloads for files now skipped.
        old = {t["tile"]: t for t in manifest.get("dem", {}).get("tiles", [])}
        for t in tiles:
            prev = old.get(t["tile"], {})
            if t["status"].startswith("present") and prev.get("md5_verified") and prev.get("size") == t.get("size"):
                t["md5_verified"] = True
        manifest["dem"] = {"product": "USGS 3DEP 1 arc-second", "tiles": tiles,
                           "bytes": sum(t.get("size") or 0 for t in tiles if t["status"] != "missing")}
        n_ok = sum(t["status"] != "missing" for t in tiles)
        print(f"  {n_ok} tiles, {manifest['dem']['bytes'] / 1e9:.2f} GB")
    if nlcd:
        print(f"Annual NLCD land cover {NLCD_YEAR}")
        manifest["nlcd"] = fetch_nlcd(raw_dir)
        print(f"  {manifest['nlcd']['tif']}")
    if byways:
        print("scenic byways")
        manifest["byways"] = fetch_byways(raw_dir)
        print(f"  {manifest['byways']['source']}: {manifest['byways']['status']}")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest
