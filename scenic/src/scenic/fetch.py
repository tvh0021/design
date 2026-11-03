"""Download raw data. For now: the Geofabrik New York State OSM extract."""

from __future__ import annotations

import hashlib
import sys
import time
import urllib.request
from pathlib import Path

from . import data_dir

NY_OSM_URL = "https://download.geofabrik.de/north-america/us/new-york-latest.osm.pbf"
USER_AGENT = "scenic-route-planner/0.1 (personal research)"
CHUNK = 1 << 20


def _open(url: str, timeout: float = 60):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)


def remote_md5(url: str) -> str:
    """Geofabrik publishes `<file>.md5` holding '<hex>  <name>'."""
    with _open(url + ".md5") as r:
        return r.read().decode().split()[0].strip().lower()


def file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _progress(done: int, total: int | None, t0: float) -> None:
    mb = done / 1e6
    rate = mb / max(time.monotonic() - t0, 1e-6)
    if total:
        msg = f"\r  {mb:8.1f} / {total / 1e6:.1f} MB  {100 * done / total:5.1f}%  {rate:6.1f} MB/s"
    else:
        msg = f"\r  {mb:8.1f} MB  {rate:6.1f} MB/s"
    sys.stderr.write(msg)
    sys.stderr.flush()


def download(url: str, dest: Path, expected_md5: str | None = None) -> Path:
    """Stream `url` to `dest` (via a .part file), checking the md5 on the fly."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h = hashlib.md5()
    t0 = time.monotonic()
    last = 0.0
    with _open(url) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0) or None
        done = 0
        while chunk := r.read(CHUNK):
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            now = time.monotonic()
            if now - last > 0.5:
                _progress(done, total, t0)
                last = now
        _progress(done, total, t0)
        sys.stderr.write("\n")
    got = h.hexdigest()
    if expected_md5 and got != expected_md5:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"md5 mismatch for {url}: got {got}, expected {expected_md5}")
    part.replace(dest)
    return dest


def fetch_osm(url: str = NY_OSM_URL, raw_dir: Path | None = None, force: bool = False) -> Path:
    """Fetch the OSM extract into data/raw/. Skips the download if the local
    file's md5 matches Geofabrik's published one."""
    raw_dir = raw_dir or data_dir() / "raw"
    dest = raw_dir / url.rsplit("/", 1)[-1]
    md5 = remote_md5(url)
    if dest.exists() and not force:
        if file_md5(dest) == md5:
            print(f"{dest} is up to date (md5 {md5})")
            return dest
        print(f"{dest} exists but md5 differs from Geofabrik's; downloading again")
    print(f"downloading {url}")
    download(url, dest, expected_md5=md5)
    (dest.with_name(dest.name + ".md5")).write_text(f"{md5}  {dest.name}\n")
    print(f"saved {dest} ({dest.stat().st_size / 1e6:.1f} MB, md5 {md5} verified)")
    return dest
