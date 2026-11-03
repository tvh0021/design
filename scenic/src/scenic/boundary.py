"""Moment mask: New York and its US neighbours, from the public-domain Census
2023 TIGER/Line state boundaries. Only the Canadian side is outside it.

Run python -m scenic.boundary to fetch it. Roads remain unchanged.
"""
from __future__ import annotations

import hashlib
import io
import json
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import shapely
from pyproj import CRS, Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform, unary_union

from . import data_dir

SOURCE = "https://www2.census.gov/geo/tiger/TIGER2023/STATE/tl_2023_us_state.zip"
TOLERANCE_M = 200.0
# New York and the US states a drive from New York may cross. Only the Canadian
# side is outside the mask: routes may run through a neighbouring state, and a
# stop there is fine, but a stop across the border is not.
STATES = {"36": "New York", "42": "Pennsylvania", "34": "New Jersey", "09": "Connecticut",
          "25": "Massachusetts", "50": "Vermont"}


def boundary_path():
    return data_dir() / "boundary" / "new_york.geojson"


def census_states(archive: Path):
    """Read the Census archive, transforming its declared CRS to WGS84."""
    import shapefile

    with zipfile.ZipFile(archive) as z:
        stem = next(n[:-4] for n in z.namelist() if n.endswith(".shp"))
        crs = CRS.from_wkt(z.read(stem + ".prj").decode())
        tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        with shapefile.Reader(shp=io.BytesIO(z.read(stem + ".shp")),
                              shx=io.BytesIO(z.read(stem + ".shx")),
                              dbf=io.BytesIO(z.read(stem + ".dbf"))) as reader:
            return [(r.record.as_dict(), transform(tr.transform, shape(r.shape.__geo_interface__)))
                    for r in reader.iterShapeRecords()]


def fetch_boundary():
    dest = boundary_path()
    if dest.exists():
        load_boundary(dest)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    archive = dest.parent / SOURCE.rsplit("/", 1)[-1]
    if not archive.exists():
        part = archive.with_suffix(".zip.part")
        try:
            # Default User-Agent only; no account, identity, or authentication.
            with urllib.request.urlopen(SOURCE, timeout=120) as response:
                part.write_bytes(response.read())
            part.replace(archive)
        except Exception as exc:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"Boundary download failed: {SOURCE}: {exc}") from exc
    matches = {props["STATEFP"]: g for props, g in census_states(archive) if props["STATEFP"] in STATES}
    if set(matches) != set(STATES) or not all(g.is_valid for g in matches.values()):
        raise ValueError("Census archive lacks a valid geometry for one of " + ", ".join(STATES.values()))
    geometry = unary_union(list(matches.values()))
    feature = {"type": "Feature", "properties": {
        "STATEFP": sorted(STATES), "NAME": "New York and neighbouring states", "source": SOURCE,
        "license": "Public domain, U.S. Census Bureau",
        "source_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }, "geometry": mapping(geometry)}
    part = dest.with_suffix(".geojson.part")
    part.write_text(json.dumps(feature, separators=(",", ":")) + "\n")
    load_boundary(part)
    part.replace(dest)
    return dest


def load_boundary(path: Path | None = None):
    path = path or boundary_path()
    if not path.exists():
        raise FileNotFoundError(f"Missing state boundary: {path}. Run python -m scenic.boundary")
    feature = json.loads(path.read_text())
    geometry = shape(feature["geometry"])
    if (feature["properties"].get("STATEFP") != sorted(STATES)
            or geometry.geom_type not in {"Polygon", "MultiPolygon"}
            or geometry.is_empty or not geometry.is_valid):
        raise ValueError(f"Invalid state boundary: {path}")
    tr = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    projected = transform(tr.transform, geometry)
    # Reject accidentally clipped relations or unrelated geometry.
    if not 3.0e11 < projected.area < 4.5e11:
        raise ValueError(f"Incomplete state boundary: {path}")
    return projected


def points_in_state(lon, lat, polygon, tolerance_m=TOLERANCE_M):
    """Inclusive point test against a metre-based EPSG:5070 polygon."""
    if tolerance_m < 0:
        raise ValueError("Boundary tolerance must be nonnegative")
    tr = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    x, y = tr.transform(np.asarray(lon), np.asarray(lat))
    buffered = polygon.buffer(tolerance_m)
    shapely.prepare(buffered)
    return shapely.covers(buffered, shapely.points(x, y))


def edges_in_state(u, v, node_x, node_y, polygon, tolerance_m=TOLERANCE_M):
    """Per-edge flag: does either end node (EPSG:5070 metres) lie inside the
    state, within the tolerance? Compute it before the node arrays are freed."""
    u, v, node_x, node_y = (np.asarray(a) for a in (u, v, node_x, node_y))
    buffered = polygon.buffer(tolerance_m)
    shapely.prepare(buffered)
    node_in = shapely.covers(buffered, shapely.points(node_x, node_y)) & np.isfinite(node_x)
    return node_in[u] | node_in[v]


def filter_moments(table, info, log=print, polygon=None, edge_inside=None):
    """Filter whole clusters and restore contiguous IDs.

    A cluster stays when its representative point is inside the state (within
    the tolerance), or, when `edge_inside` (a per-edge flag from
    `edges_in_state`) is given, when its own road edge (`edge_idx`) touches a
    node inside it. The second rule keeps bridges that leave the state (the
    George Washington, Ogdensburg-Prescott and Rouses Point bridges put their
    representative point on the far shore) while a stop on a road wholly in
    Ontario, Pennsylvania or New Jersey still goes, even if its cluster reaches
    across the line."""
    import pyarrow as pa
    polygon = load_boundary() if polygon is None else polygon
    keep = points_in_state(table["lon"].to_numpy(), table["lat"].to_numpy(), polygon)
    if edge_inside is not None:
        edge_inside = np.asarray(edge_inside, dtype=bool)
        keep = keep | edge_inside[table["edge_idx"].to_numpy().astype(np.int64)]
    removed = table.filter(~keep)
    table = table.filter(keep)
    field = table.schema.field("id")
    table = table.set_column(table.schema.get_field_index("id"), field,
                             pa.array(np.arange(table.num_rows), type=field.type))
    info["state_boundary"] = {
        "source": SOURCE, "tolerance_m": TOLERANCE_M,
        "removed": removed.num_rows,
        "removed_labels": removed["label"].to_pylist(),
    }
    info["moments"] = table.num_rows
    info["by_type"] = {t: table["type"].to_pylist().count(t) for t in info["by_type"]}
    log(f"state boundary: removed {removed.num_rows} moments")
    for label in removed["label"].to_pylist():
        log(f"  outside New York: {label}")
    return table, info


if __name__ == "__main__":
    print(fetch_boundary())
