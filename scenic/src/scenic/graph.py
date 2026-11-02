"""Build the statewide drivable road graph from an OSM PBF extract.

Pipeline:
  1. Read highway ways with pyosmium (node locations cached in C++).
  2. Keep drivable ways (see `is_drivable`), resolve oneway and speed.
  3. Simplify to junction nodes: a node used twice or more (by any kept way,
     or twice by the same way) or a way endpoint. Each edge keeps its full
     geometry and the attributes of its OSM way.
  4. Save edges as parquet (coordinates as list<double> columns) and nodes as npz.

Edges whose way is `oneway=-1` are stored reversed, so `oneway=True` always
means "only u -> v".
"""

from __future__ import annotations

import json
import re
import resource
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import data_dir

MPH_TO_MS = 0.44704
EARTH_RADIUS_M = 6_371_008.8

MAIN_CLASSES = (
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", "living_street",
)
LINK_CLASSES = ("motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link")
DRIVABLE = frozenset(MAIN_CLASSES + LINK_CLASSES)

# Default speeds in mph when maxspeed is missing or unparseable.
DEFAULT_SPEED_MPH = {
    "motorway": 65.0,
    "trunk": 55.0,
    "primary": 50.0,
    "secondary": 45.0,
    "tertiary": 40.0,
    "unclassified": 35.0,
    "residential": 25.0,
    "living_street": 15.0,
    **{c: 35.0 for c in LINK_CLASSES},
}

# Access values that make a way unusable for a private car.
NO_ACCESS = frozenset({"no", "private", "agricultural", "forestry"})
# Most specific first: the first one present decides.
ACCESS_KEYS = ("motorcar", "motor_vehicle", "vehicle", "access")

TAG_KEYS = (
    "highway", "oneway", "junction", "maxspeed", "name", "ref", "surface", "area",
    *ACCESS_KEYS,
)

_SPEED_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(mph|km/h|kmh|kph|knots)?$")

# Bare maxspeed values that, on a US road, are read as mph instead of km/h.
# These are posted US limits that no metric sign uses; US mappers often drop
# the " mph". Ambiguous values (40, 50, 60, ...) keep the km/h convention.
US_MPH_BARE = frozenset({25, 30, 35, 45, 55, 65})

# Rough outline of Canada around the New York extract, (lon, lat). It follows
# the midlines of Lake Erie, the Niagara River, Lake Ontario and the
# St. Lawrence, then the 45th parallel east past Vermont. Accurate to a few
# hundred metres; only used to tell US from Canadian bare maxspeed values.
CANADA_OUTLINE = (
    (-82.0, 42.20), (-80.1, 42.40), (-79.4, 42.67), (-79.0, 42.82), (-78.915, 42.885),
    (-78.908, 42.905), (-78.93, 42.95), (-79.01, 43.00), (-79.03, 43.04), (-79.07, 43.075),
    (-79.067, 43.09), (-79.06, 43.105), (-79.068, 43.118), (-79.05, 43.13), (-79.044, 43.153),
    (-79.045, 43.17), (-79.055, 43.25), (-79.065, 43.27), (-79.0, 43.50), (-78.0, 43.62),
    (-77.0, 43.62), (-76.55, 43.90), (-76.44, 44.07), (-76.35, 44.11), (-76.2, 44.18),
    (-76.05, 44.25), (-75.98, 44.33), (-75.85, 44.37), (-75.68, 44.58), (-75.5, 44.703),
    (-75.3, 44.83), (-75.2, 44.88), (-74.95, 44.975), (-74.74, 45.005), (-71.0, 45.01),
    (-71.0, 48.0), (-82.0, 48.0),
)


def in_canada(lon: float, lat: float) -> bool:
    """Point-in-polygon test against CANADA_OUTLINE (even-odd ray casting)."""
    inside = False
    pts = CANADA_OUTLINE
    x1, y1 = pts[-1]
    for x2, y2 in pts:
        if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
        x1, y1 = x2, y2
    return inside


def parse_maxspeed(value: str | None, lonlat: tuple[float, float] | None = None) -> float | None:
    """Parse an OSM maxspeed value to mph.

    "45 mph" -> 45; "80 km/h" / "80 kmh" -> 49.7. A bare number is km/h, as
    OSM defines it ("50" -> 31.1), with one exception: if `lonlat` (where the
    way is) lies in the US, not Canada, and the number is in US_MPH_BARE, it
    is read as mph ("55" -> 55). Without `lonlat` the convention always
    applies. Several values ("35 mph;45 mph") use the first. Non-numeric
    values ("none", "signals", "US:urban") and implausible numbers return None.
    """
    if value is None:
        return None
    s = str(value).strip().lower().split(";")[0].strip()
    m = _SPEED_RE.match(s)
    if not m:
        return None
    v = float(m.group(1))
    unit = m.group(2)
    if unit is None:
        us_mph = lonlat is not None and v in US_MPH_BARE and not in_canada(*lonlat)
        unit = "mph" if us_mph else "km/h"
    if unit in ("km/h", "kmh", "kph"):
        v /= 1.609344
    elif unit == "knots":
        v *= 1.150779
    if not (3.0 <= v <= 90.0):
        return None
    return v


def default_speed(highway: str) -> float:
    return DEFAULT_SPEED_MPH.get(highway, 25.0)


def oneway_direction(highway: str | None, oneway: str | None, junction: str | None = None) -> int | None:
    """1 = only along the way's node order, -1 = only against it, 0 = both,
    None = not routable (reversible / alternating lanes).

    Implied oneway: motorway and motorway_link, and roundabouts
    (junction=roundabout or circular), unless tagged oneway=no.
    """
    o = (oneway or "").strip().lower()
    if o in ("yes", "true", "1"):
        return 1
    if o in ("-1", "reverse"):
        return -1
    if o in ("no", "false", "0"):
        return 0
    if o in ("reversible", "alternating"):
        return None
    if highway in ("motorway", "motorway_link"):
        return 1
    if (junction or "").strip().lower() in ("roundabout", "circular"):
        return 1
    return 0


def is_drivable(tags: dict) -> bool:
    if tags.get("highway") not in DRIVABLE:
        return False
    if tags.get("area") == "yes":
        return False
    for key in ACCESS_KEYS:
        val = tags.get(key)
        if val is not None:
            return val.split(";")[0].strip().lower() not in NO_ACCESS
    return True


# --------------------------------------------------------------------------
# Raw way collection


@dataclass
class Ways:
    """Kept ways, flattened. Way i covers positions offsets[i]:offsets[i+1]."""

    way_id: np.ndarray  # int64 (n_ways)
    highway: list[str]
    name: list[str | None]
    ref: list[str | None]
    surface: list[str | None]
    direction: np.ndarray  # int8: 1, -1, 0
    speed_mph: np.ndarray  # float64
    maxspeed_tagged: np.ndarray  # bool
    offsets: np.ndarray  # int64 (n_ways + 1)
    node_id: np.ndarray  # int64 (n_points)
    lon: np.ndarray  # float64
    lat: np.ndarray  # float64


class WayCollector:
    """Accumulates drivable ways from tag dicts and node lists."""

    def __init__(self) -> None:
        self.way_id: list[int] = []
        self.highway: list[str] = []
        self.name: list[str | None] = []
        self.ref: list[str | None] = []
        self.surface: list[str | None] = []
        self.direction: list[int] = []
        self.speed: list[float] = []
        self.tagged: list[bool] = []
        self.lengths: list[int] = [0]
        self.node_id: list[int] = []
        self.lon: list[float] = []
        self.lat: list[float] = []
        self.skipped = {"not_drivable": 0, "reversible": 0, "too_short": 0, "bad_location": 0}

    def add(self, way_id: int, tags: dict, nodes: list[tuple[int, float, float]]) -> bool:
        if not is_drivable(tags):
            self.skipped["not_drivable"] += 1
            return False
        hw = tags["highway"]
        d = oneway_direction(hw, tags.get("oneway"), tags.get("junction"))
        if d is None:
            self.skipped["reversible"] += 1
            return False
        if len(nodes) < 2:
            self.skipped["too_short"] += 1
            return False
        ms = parse_maxspeed(tags.get("maxspeed"), lonlat=nodes[0][1:])
        self.way_id.append(way_id)
        self.highway.append(hw)
        self.name.append(tags.get("name"))
        self.ref.append(tags.get("ref"))
        self.surface.append(tags.get("surface"))
        self.direction.append(d)
        self.speed.append(ms if ms is not None else default_speed(hw))
        self.tagged.append(ms is not None)
        self.lengths.append(len(nodes))
        for nid, x, y in nodes:
            self.node_id.append(nid)
            self.lon.append(x)
            self.lat.append(y)
        return True

    def finish(self) -> Ways:
        return Ways(
            way_id=np.asarray(self.way_id, dtype=np.int64),
            highway=self.highway,
            name=self.name,
            ref=self.ref,
            surface=self.surface,
            direction=np.asarray(self.direction, dtype=np.int8),
            speed_mph=np.asarray(self.speed, dtype=np.float64),
            maxspeed_tagged=np.asarray(self.tagged, dtype=bool),
            offsets=np.cumsum(np.asarray(self.lengths, dtype=np.int64)),
            node_id=np.asarray(self.node_id, dtype=np.int64),
            lon=np.asarray(self.lon, dtype=np.float64),
            lat=np.asarray(self.lat, dtype=np.float64),
        )


def read_ways(pbf: Path, progress: bool = True) -> tuple[Ways, dict]:
    import osmium
    from osmium.filter import EntityFilter, TagFilter

    col = WayCollector()
    proc = (
        osmium.FileProcessor(str(pbf), osmium.osm.NODE | osmium.osm.WAY)
        .with_locations("flex_mem")
        .with_filter(EntityFilter(osmium.osm.WAY))
        .with_filter(TagFilter(*[("highway", h) for h in sorted(DRIVABLE)]))
    )
    n = 0
    for w in proc:
        n += 1
        tags = {k: w.tags.get(k) for k in TAG_KEYS}
        try:
            nodes = [(nd.ref, nd.lon, nd.lat) for nd in w.nodes]
        except osmium.InvalidLocationError:
            col.skipped["bad_location"] += 1
            continue
        col.add(w.id, tags, nodes)
        if progress and n % 200_000 == 0:
            sys.stderr.write(f"\r  read {n:,} highway ways, kept {len(col.way_id):,}")
            sys.stderr.flush()
    if progress:
        sys.stderr.write(f"\r  read {n:,} highway ways, kept {len(col.way_id):,}\n")
    return col.finish(), {"highway_ways_read": n, **col.skipped}


# --------------------------------------------------------------------------
# Simplification


def haversine_m(lon1, lat1, lon2, lat2):
    lon1, lat1, lon2, lat2 = map(np.radians, (lon1, lat1, lon2, lat2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(np.minimum(a, 1.0)))


@dataclass
class Graph:
    """Junction-simplified graph. Edge geometry: points geom_idx[geom_off[e]:geom_off[e+1]]
    of the flat way arrays, already in u -> v order."""

    node_osm_id: np.ndarray
    node_lon: np.ndarray
    node_lat: np.ndarray
    u: np.ndarray
    v: np.ndarray
    edge_way: np.ndarray  # index into Ways
    oneway: np.ndarray  # bool: only u -> v
    length_m: np.ndarray
    geom_off: np.ndarray
    geom_idx: np.ndarray


def simplify(ways: Ways) -> Graph:
    """Split ways at junction nodes. A junction is a way endpoint or a node
    that occurs two or more times across all kept ways."""
    off = ways.offsets
    n_pts = len(ways.node_id)
    n_ways = len(off) - 1
    way_of_pos = np.repeat(np.arange(n_ways), np.diff(off))

    _, inv, cnt = np.unique(ways.node_id, return_inverse=True, return_counts=True)
    is_junc = cnt[inv] >= 2
    is_junc[off[:-1]] = True
    is_junc[off[1:] - 1] = True

    # Segment lengths between consecutive points of the same way.
    seg = np.zeros(n_pts, dtype=np.float64)
    if n_pts > 1:
        same = way_of_pos[1:] == way_of_pos[:-1]
        d = haversine_m(ways.lon[:-1], ways.lat[:-1], ways.lon[1:], ways.lat[1:])
        seg[:-1] = np.where(same, d, 0.0)
    cum = np.concatenate([[0.0], np.cumsum(seg)])  # cum[p] = length up to point p

    jpos = np.flatnonzero(is_junc)
    a, b = jpos[:-1], jpos[1:]
    keep = way_of_pos[a] == way_of_pos[b]
    start, end = a[keep], b[keep]
    edge_way = way_of_pos[start]
    length = cum[end] - cum[start]

    junc_ids = np.unique(ways.node_id[jpos])
    u = np.searchsorted(junc_ids, ways.node_id[start])
    v = np.searchsorted(junc_ids, ways.node_id[end])

    direction = ways.direction[edge_way]
    rev = direction == -1
    u, v = np.where(rev, v, u), np.where(rev, u, v)

    # Geometry point indices, reversed for oneway=-1 edges.
    npts = end - start + 1
    geom_off = np.concatenate([[0], np.cumsum(npts)])
    k = np.arange(geom_off[-1]) - np.repeat(geom_off[:-1], npts)
    geom_idx = np.where(np.repeat(rev, npts), np.repeat(end, npts) - k, np.repeat(start, npts) + k)

    # Node coordinates: take them from the first occurrence of each junction id.
    first = np.full(len(junc_ids), -1, dtype=np.int64)
    jid_idx = np.searchsorted(junc_ids, ways.node_id[jpos])
    first[jid_idx[::-1]] = jpos[::-1]

    return Graph(
        node_osm_id=junc_ids,
        node_lon=ways.lon[first],
        node_lat=ways.lat[first],
        u=u.astype(np.int32),
        v=v.astype(np.int32),
        edge_way=edge_way,
        oneway=direction != 0,
        length_m=length,
        geom_off=geom_off,
        geom_idx=geom_idx,
    )


def largest_scc(n_nodes: int, u: np.ndarray, v: np.ndarray, oneway: np.ndarray) -> np.ndarray:
    """Boolean mask of nodes in the largest strongly connected component."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    two = ~oneway
    tail = np.concatenate([u, v[two]])
    head = np.concatenate([v, u[two]])
    m = coo_matrix((np.ones(len(tail), dtype=np.int8), (tail, head)), shape=(n_nodes, n_nodes)).tocsr()
    _, labels = connected_components(m, directed=True, connection="strong")
    biggest = np.bincount(labels).argmax()
    return labels == biggest


# --------------------------------------------------------------------------
# Save


def edge_table(ways: Ways, g: Graph):
    import pyarrow as pa

    ew = g.edge_way
    speed = ways.speed_mph[ew]
    time_s = g.length_m / (speed * MPH_TO_MS)

    def pick(col):
        return pa.array([col[i] for i in ew], pa.string()).dictionary_encode()

    return pa.table({
        "u": pa.array(g.u),
        "v": pa.array(g.v),
        "way_id": pa.array(ways.way_id[ew]),
        "name": pick(ways.name),
        "ref": pick(ways.ref),
        "highway": pick(ways.highway),
        "surface": pick(ways.surface),
        "oneway": pa.array(g.oneway),
        "length_m": pa.array(g.length_m),
        "speed_mph": pa.array(speed.astype(np.float32)),
        "maxspeed_tagged": pa.array(ways.maxspeed_tagged[ew]),
        "time_s": pa.array(time_s),
        "lon": pa.LargeListArray.from_arrays(pa.array(g.geom_off.astype(np.int64)), pa.array(ways.lon[g.geom_idx])),
        "lat": pa.LargeListArray.from_arrays(pa.array(g.geom_off.astype(np.int64)), pa.array(ways.lat[g.geom_idx])),
    })


def save_graph(ways: Ways, g: Graph, out_dir: Path) -> dict:
    import pyarrow.parquet as pq

    out_dir.mkdir(parents=True, exist_ok=True)
    lcc = largest_scc(len(g.node_osm_id), g.u, g.v, g.oneway)
    table = edge_table(ways, g)
    pq.write_table(table, out_dir / "edges.parquet", compression="zstd")
    np.savez(out_dir / "nodes.npz", osm_id=g.node_osm_id, lon=g.node_lon, lat=g.node_lat, lcc=lcc)
    return {
        "nodes": int(len(g.node_osm_id)),
        "edges": int(len(g.u)),
        "oneway_edges": int(g.oneway.sum()),
        "total_km": float(g.length_m.sum() / 1000),
        "lcc_nodes": int(lcc.sum()),
        "ways": int(len(ways.way_id)),
        "maxspeed_tagged_km": float(g.length_m[ways.maxspeed_tagged[g.edge_way]].sum() / 1000),
    }


def peak_rss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1e6 if sys.platform == "darwin" else r / 1e3  # bytes on macOS, KiB on Linux


def build_graph(pbf: Path | None = None, out_dir: Path | None = None) -> dict:
    pbf = pbf or data_dir() / "raw" / "new-york-latest.osm.pbf"
    out_dir = out_dir or data_dir() / "graph"
    t0 = time.monotonic()
    print(f"reading {pbf}")
    ways, read_stats = read_ways(pbf)
    t1 = time.monotonic()
    print(f"  {len(ways.way_id):,} drivable ways, {len(ways.node_id):,} points ({t1 - t0:.1f} s)")
    g = simplify(ways)
    t2 = time.monotonic()
    print(f"  simplified to {len(g.node_osm_id):,} nodes, {len(g.u):,} edges ({t2 - t1:.1f} s)")
    stats = save_graph(ways, g, out_dir)
    t3 = time.monotonic()
    report = {
        "pbf": str(pbf),
        **stats,
        "read": read_stats,
        "seconds": {"read": round(t1 - t0, 1), "simplify": round(t2 - t1, 1),
                    "save": round(t3 - t2, 1), "total": round(t3 - t0, 1)},
        "peak_rss_mb": round(peak_rss_mb(), 1),
    }
    (out_dir / "build_report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return report
