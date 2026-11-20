"""One-command data setup, status checks, and prebuilt bundle installation."""

from __future__ import annotations

import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from . import data_dir

REQUIRED_GRAPH = ("nodes.npz", "edges.parquet")
REQUIRED_SCORE = ("scores.parquet",)
OPTIONAL_RUNTIME = (("graph", "edge_delays.parquet"), ("score", "moments.parquet"))
PREBUILT_URL = ("https://github.com/tvh0021/design/releases/download/"
                "wanderers-data-v0.2/wanderers-ny-runtime-v0.2.tar.gz")
PREBUILT_SHA256 = "49766cdb2fc6828f86eafd846e764a7612030acd616264b715d6d21040a3ba02"
_ALLOWED_MEMBER = re.compile(r"^(?:[A-Za-z0-9_.-]+/)?(graph|score|boundary)/([A-Za-z0-9_.-]+)$")


@contextmanager
def _with_data_root(data_root: Path | None):
    if data_root is None:
        yield data_dir()
        return
    resolved = Path(data_root).expanduser().resolve()
    prev = os.environ.get("SCENIC_DATA")
    os.environ["SCENIC_DATA"] = str(resolved)
    try:
        yield resolved
    finally:
        if prev is None:
            os.environ.pop("SCENIC_DATA", None)
        else:
            os.environ["SCENIC_DATA"] = prev


def missing_graph_files(graph_dir: Path | None = None) -> tuple[Path, list[Path]]:
    gdir = Path(graph_dir or data_dir() / "graph")
    missing = [gdir / name for name in REQUIRED_GRAPH if not (gdir / name).is_file()]
    return gdir, missing


def missing_serve_files(
    graph_dir: Path | None = None, score_dir: Path | None = None
) -> tuple[Path, Path, list[Path]]:
    gdir, missing = missing_graph_files(graph_dir)
    sdir = Path(score_dir or data_dir() / "score")
    missing.extend(sdir / name for name in REQUIRED_SCORE if not (sdir / name).is_file())
    return gdir, sdir, missing


def format_missing_data_message(
    graph_dir: Path, score_dir: Path, missing: list[Path], cmd_name: str = "scenic serve"
) -> str:
    names = ", ".join(str(p) for p in missing)
    lines = [
        f"{cmd_name}: New York route data is not built yet.",
        f"  Missing: {names}",
        "",
        "Fastest: download the prebuilt New York data (~160 MB, under a minute):",
        "  scenic setup --prebuilt",
        "",
        "Or build all New York data yourself in one idempotent step (~5.0 GB download, ~7.2 GB disk, ~6-10 min, ~15 GB RAM):",
        "  scenic setup",
        "  scenic serve --setup --open   # build data if needed, then open the map",
        "",
        "Or unpack another prebuilt runtime archive:",
        "  scenic setup --bundle <path-or-https-url>",
        "",
        "Or point at an existing data folder:",
        f"  SCENIC_DATA=/path/to/data {cmd_name}",
    ]
    return "\n".join(lines)


def _fmt_size(num_bytes: int) -> str:
    if num_bytes >= 1e9:
        return f"{num_bytes / 1e9:.2f} GB"
    if num_bytes >= 1e6:
        return f"{num_bytes / 1e6:.1f} MB"
    return f"{num_bytes / 1e3:.0f} KB"


def _dir_bytes(paths: list[Path]) -> int:
    return sum(p.stat().st_size for p in paths if p.is_file())


def _graph_is_fresh(pbf: Path, gdir: Path) -> bool:
    from .graph import read_edge_delays, read_edges_fingerprint

    req = [gdir / "nodes.npz", gdir / "edges.parquet", gdir / "edge_delays.parquet"]
    if not all(p.is_file() for p in req):
        return False
    if pbf.is_file():
        pbf_mtime = pbf.stat().st_mtime
        if any(p.stat().st_mtime < pbf_mtime for p in req):
            return False
    try:
        fp = read_edges_fingerprint(gdir)
        read_edge_delays(gdir / "edge_delays.parquet", fp, columns=["time_s"])
    except Exception:
        return False
    return True


def check_status(data_root: Path | None = None, out: TextIO = sys.stdout) -> int:
    root = Path(data_root).expanduser().resolve() if data_root is not None else data_dir()
    pbf = root / "raw" / "new-york-latest.osm.pbf"
    gdir = root / "graph"
    sdir = root / "score"
    bfile = root / "boundary" / "new_york.geojson"
    layers_manifest = root / "raw" / "layers.json"

    _, _, missing = missing_serve_files(gdir, sdir)
    ready = not missing

    out.write(f"Data directory: {root}\n")
    graph_files = [gdir / "nodes.npz", gdir / "edges.parquet", gdir / "edge_delays.parquet"]
    score_files = [sdir / "scores.parquet", sdir / "moments.parquet"]
    steps = [
        ("OSM road extract", pbf.is_file(), [pbf]),
        ("Road graph + delays", all(p.is_file() for p in graph_files), graph_files),
        ("DEM, NLCD & byway layers", layers_manifest.is_file(), [layers_manifest]),
        ("State boundary mask", bfile.is_file(), [bfile]),
        ("Scenery scores + moments", all(p.is_file() for p in score_files), score_files),
    ]
    for label, ok, files in steps:
        tag = "ok     " if ok else "missing"
        sz = f" ({_fmt_size(_dir_bytes(files))})" if ok and _dir_bytes(files) else ""
        out.write(f"  [{tag}] {label}{sz}\n")

    if ready:
        opt_missing = [f"{sub}/{name}" for sub, name in OPTIONAL_RUNTIME if not (root / sub / name).is_file()]
        note = f" (optional files missing: {', '.join(opt_missing)})" if opt_missing else ""
        out.write(f"\nReady for 'scenic serve --open'{note}.\n")
        return 0

    out.write(
        "\nNot ready for 'scenic serve' yet.\n"
        "Run 'scenic setup --prebuilt' (~160 MB download, under a minute),\n"
        "or 'scenic setup' to build it yourself (~5.0 GB download, ~7.2 GB disk, ~6-10 min, ~15 GB peak RAM),\n"
        "or 'scenic setup --bundle <archive>' to unpack prebuilt graph and score files.\n"
    )
    return 1


def _extract_member_rel(raw_name: str) -> tuple[str, str] | None:
    norm = raw_name.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    if not norm or norm.endswith("/"):
        return None
    parts = norm.split("/")
    if any(p in ("", ".", "..") for p in parts) or raw_name.startswith("/"):
        raise ValueError(f"Unsafe archive path: {raw_name!r}")
    m = _ALLOWED_MEMBER.match(norm)
    if not m or m.group(2).startswith("._"):  # skip macOS AppleDouble files
        return None
    return m.group(1), m.group(2)


def _write_atomic(stream, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".", suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as dst:
            shutil.copyfileobj(stream, dst, length=1 << 20)
        size = tmp.stat().st_size
        tmp.replace(dest)
        return size
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def install_bundle(source: str | Path, data_root: Path | None = None, out: TextIO = sys.stdout,
                   sha256: str | None = None) -> Path:
    """Unpack prebuilt graph/score files from a local archive or HTTPS URL.

    Files are unpacked into a staging folder and verified there; the live data
    folder changes only after the bundle loads cleanly."""
    from .fetch import download
    from .search import Context

    with _with_data_root(data_root) as root:
        root.mkdir(parents=True, exist_ok=True)
        src_str = str(source)
        for old in root.glob(".bundle-*"):  # left behind by a killed install
            shutil.rmtree(old, ignore_errors=True)
        stage = Path(tempfile.mkdtemp(dir=root, prefix=".bundle-"))
        try:
            if src_str.startswith("http://"):
                raise ValueError("Bundle URLs must use https://")
            if src_str.startswith("https://"):
                out.write(f"Downloading bundle from {src_str}\n")
                out.flush()
                suffix = ".zip" if src_str.lower().endswith(".zip") else ".tar.gz"
                archive = download(src_str, stage / f"bundle{suffix}")
            else:
                archive = Path(source).expanduser().resolve()
                if not archive.is_file():
                    raise FileNotFoundError(f"Bundle not found: {archive}")
            if sha256:
                import hashlib
                h = hashlib.sha256()
                with open(archive, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                if h.hexdigest() != sha256.lower():
                    raise ValueError(f"Bundle checksum mismatch (expected sha256 {sha256})")

            staged: dict[tuple[str, str], None] = {}  # ordered, no duplicates
            total_bytes = 0
            if zipfile.is_zipfile(archive):
                with zipfile.ZipFile(archive) as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        # Reject Unix symlinks stored in zip external_attr
                        if (info.external_attr >> 16) & 0o170000 == 0o120000:
                            raise ValueError(f"Refusing symlink in archive: {info.filename!r}")
                        rel = _extract_member_rel(info.filename)
                        if rel is None:
                            continue
                        with zf.open(info, "r") as src_fp:
                            total_bytes += _write_atomic(src_fp, stage / rel[0] / rel[1])
                        staged[rel] = None
            elif tarfile.is_tarfile(archive):
                with tarfile.open(archive, "r:*") as tf:
                    for member in tf.getmembers():
                        if member.isdir():
                            continue
                        if not member.isfile():
                            raise ValueError(f"Refusing non-regular file in archive: {member.name!r}")
                        rel = _extract_member_rel(member.name)
                        if rel is None:
                            continue
                        src_fp = tf.extractfile(member)
                        if src_fp is None:
                            continue
                        with src_fp:
                            total_bytes += _write_atomic(src_fp, stage / rel[0] / rel[1])
                        staged[rel] = None
            else:
                raise ValueError(f"Unsupported archive format (expected .tar.gz, .tar, or .zip): {archive}")

            _, _, missing = missing_serve_files(stage / "graph", stage / "score")
            if missing:
                names = ", ".join(p.name for p in missing)
                raise ValueError(f"Archive did not contain required runtime files ({names})")
            ctx = Context.load(stage / "graph", stage / "score")

            # Verified: drop optional runtime files the bundle lacks (they would
            # not match its edges), then move the staged files into place.
            for sub, name in OPTIONAL_RUNTIME:
                if (sub, name) not in staged:
                    (root / sub / name).unlink(missing_ok=True)
            for sub, name in staged:
                (root / sub).mkdir(parents=True, exist_ok=True)
                os.replace(stage / sub / name, root / sub / name)
            out.write(
                f"Unpacked {len(staged)} files ({_fmt_size(total_bytes)}) into {root}\n"
                f"Verified {ctx.g.n_edges:,} road edges and {len(ctx.moments.id):,} scenic moments.\n"
                "Ready. Start the map in your browser with: scenic serve --open\n"
            )
            return root
        finally:
            shutil.rmtree(stage, ignore_errors=True)


def run_setup(
    data_root: Path | None = None,
    force: bool = False,
    bundle: str | None = None,
    check_only: bool = False,
    prebuilt: bool = False,
    out: TextIO = sys.stdout,
) -> int:
    if check_only:
        return check_status(data_root=data_root, out=out)
    sha = None
    if prebuilt:
        bundle, sha = PREBUILT_URL, PREBUILT_SHA256
    if bundle:
        try:
            install_bundle(bundle, data_root=data_root, out=out, sha256=sha)
        except Exception as exc:
            print(f"scenic setup: {exc}", file=sys.stderr)
            return 1
        return 0

    from .boundary import boundary_path, fetch_boundary, load_boundary
    from .fetch import NY_OSM_URL, fetch_osm
    from .graph import build_graph
    from .layers import fetch_layers
    from .score import run as run_score

    with _with_data_root(data_root) as root:
        t_start = time.monotonic()
        raw_dir = root / "raw"
        graph_dir = root / "graph"
        rasters_dir = root / "rasters"
        score_dir = root / "score"
        pbf = raw_dir / NY_OSM_URL.rsplit("/", 1)[-1]

        out.write(
            f"Setting up Maps for Wanderers data in {root}\n"
            "Estimates for a cold run: ~5.0 GB download, ~7.2 GB disk, ~6-10 min, ~15 GB peak RAM.\n"
            "Finished downloads and up-to-date stages are skipped automatically.\n\n"
        )
        out.flush()

        # 1/5 OSM PBF
        t0 = time.monotonic()
        out.write("[1/5] OSM road extract (~500 MB) ...\n")
        out.flush()
        if pbf.is_file() and pbf.with_name(pbf.name + ".md5").is_file() and not force:
            out.write(f"  reusing {pbf} ({_fmt_size(pbf.stat().st_size)})\n")
        else:
            fetch_osm(NY_OSM_URL, raw_dir=raw_dir, force=force)
        out.write(f"  done in {time.monotonic() - t0:.1f} s\n")

        # 2/5 Road graph + signal delays
        t0 = time.monotonic()
        out.write("[2/5] Drivable road graph and intersection delays (~45 s, ~4 GB RAM) ...\n")
        out.flush()
        if not force and _graph_is_fresh(pbf, graph_dir):
            out.write(f"  up to date in {graph_dir}\n")
        else:
            build_graph(pbf=pbf, out_dir=graph_dir)
        out.write(f"  done in {time.monotonic() - t0:.1f} s\n")

        # 3/5 Elevation, NLCD land cover, byways
        t0 = time.monotonic()
        out.write("[3/5] USGS 3DEP elevation, NLCD land cover, and scenic byways (~4.0 GB download) ...\n")
        out.flush()
        fetch_layers(raw_dir=raw_dir)
        out.write(f"  done in {time.monotonic() - t0:.1f} s\n")

        # 4/5 Census state boundary mask
        t0 = time.monotonic()
        out.write("[4/5] Census TIGER state boundary mask (~10 MB download) ...\n")
        out.flush()
        bp = boundary_path()
        if bp.is_file() and not force:
            load_boundary(bp)
            out.write(f"  up to date at {bp}\n")
        else:
            if force and bp.is_file():
                bp.unlink()
            fetch_boundary()
            out.write(f"  saved {bp}\n")
        out.write(f"  done in {time.monotonic() - t0:.1f} s\n")

        # 5/5 Scenery score + moments
        t0 = time.monotonic()
        out.write("[5/5] Statewide scenery score and scenic moments (~4-6 min, ~15 GB peak RAM) ...\n")
        out.flush()
        run_score(
            graph_dir=graph_dir,
            out_dir=score_dir,
            raw_dir=raw_dir,
            rasters_dir=rasters_dir,
            force=[] if force else None,
        )
        out.write(f"  done in {time.monotonic() - t0:.1f} s\n\n")

        out.write(
            f"Setup finished in {(time.monotonic() - t_start) / 60:.1f} min.\n"
            "Start the map in your browser with:\n"
            "  scenic serve --open\n"
        )
        out.flush()
    return 0
