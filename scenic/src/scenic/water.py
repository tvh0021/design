"""Water geometry and traffic-control nodes from the OSM extract, for the
scenery score (docs/SCORE_SPEC.md sections 1 and 3)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

LINE_CODES = {"river": 1, "canal": 2, "stream": 3}


def is_areal_water(tags: dict, exclude_water=("wastewater", "basin", "reflecting_pool")) -> bool:
    """natural=water, landuse=reservoir or waterway=riverbank, minus
    wastewater/basin/reflecting pools and intermittent water."""
    if (tags.get("intermittent") or "").strip().lower() == "yes":
        return False
    if (tags.get("water") or "").strip().lower() in exclude_water:
        return False
    return (tags.get("natural") == "water" or tags.get("landuse") == "reservoir"
            or tags.get("waterway") == "riverbank")


def line_code(tags: dict) -> int:
    """1 river, 2 canal, 3 stream, 0 otherwise."""
    return LINE_CODES.get(tags.get("waterway") or "", 0)


def read_water(pbf: Path, exclude_water=("wastewater", "basin", "reflecting_pool"),
               progress: bool = True) -> dict:
    """WKB geometries (EPSG:4326): 'area' polygons, 'line' (wkb, code) and
    'coast' lines."""
    import osmium
    from osmium.filter import EntityFilter, KeyFilter

    keys = ("natural", "landuse", "waterway", "water", "intermittent")
    wkb = osmium.geom.WKBFactory()
    areas: list[bytes] = []
    lines: list[bytes] = []
    codes: list[int] = []
    coast: list[bytes] = []
    bad = 0
    proc = (osmium.FileProcessor(str(pbf))
            .with_locations("flex_mem")
            .with_areas(KeyFilter("natural", "landuse", "waterway"))
            .with_filter(EntityFilter(osmium.osm.WAY | osmium.osm.AREA))
            .with_filter(KeyFilter("natural", "landuse", "waterway")))
    n = 0
    for obj in proc:
        n += 1
        tags = {k: obj.tags.get(k) for k in keys}
        try:
            if obj.type_str() == "a":
                if is_areal_water(tags, exclude_water):
                    areas.append(bytes.fromhex(wkb.create_multipolygon(obj)))
            else:
                code = line_code(tags)
                if code:
                    lines.append(bytes.fromhex(wkb.create_linestring(obj)))
                    codes.append(code)
                elif tags.get("natural") == "coastline":
                    coast.append(bytes.fromhex(wkb.create_linestring(obj)))
        except (RuntimeError, osmium.InvalidLocationError):
            bad += 1
        if progress and n % 200_000 == 0:
            sys.stderr.write(f"\r  water: {n:,} objects, {len(areas):,} areas, {len(lines):,} lines")
            sys.stderr.flush()
    if progress:
        sys.stderr.write(f"\r  water: {n:,} objects, {len(areas):,} areas, {len(lines):,} lines, "
                         f"{len(coast):,} coastline ways, {bad} bad geometries\n")
    return {"area": areas, "line": lines, "line_code": np.asarray(codes, np.uint8), "coast": coast,
            "bad": bad}


def project_wkb(wkbs: list[bytes], crs: str = "EPSG:5070"):
    """Shapely geometries from WKB, reprojected from EPSG:4326 in one call."""
    import shapely
    from pyproj import Transformer

    geoms = shapely.from_wkb(wkbs)
    tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)

    def f(xy):
        x, y = tr.transform(xy[:, 0], xy[:, 1])
        return np.column_stack([x, y])

    return shapely.transform(geoms, f)


def is_signal_node(tags: dict) -> bool:
    """highway=traffic_signals, or highway=stop when stop=all or the sign has
    no direction tag."""
    hw = (tags.get("highway") or "").strip().lower()
    if hw == "traffic_signals":
        return True
    if hw == "stop":
        if (tags.get("stop") or "").strip().lower() == "all":
            return True
        return all(tags.get(k) in (None, "") for k in ("direction", "stop:direction"))
    return False


def read_signal_nodes(pbf: Path) -> np.ndarray:
    """(n, 2) lon/lat of signal and qualifying stop nodes."""
    import osmium
    from osmium.filter import KeyFilter

    out = []
    proc = osmium.FileProcessor(str(pbf), osmium.osm.NODE).with_filter(KeyFilter("highway"))
    for nd in proc:
        tags = {k: nd.tags.get(k) for k in ("highway", "stop", "direction", "stop:direction")}
        if is_signal_node(tags):
            out.append((nd.location.lon, nd.location.lat))
    return np.asarray(out, dtype=np.float64).reshape(-1, 2)
