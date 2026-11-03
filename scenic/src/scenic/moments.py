"""Scenic moments (docs/SCORE_V02.md section 4): short places worth driving
to, found once offline and collected by the route search.

Two score stages use this module:

  osm_features  viewpoints, waterfalls, cliffs, road bridges, places and
                named water from the PBF       score/osm_features.parquet
  moments       candidate edges, types, values, clustering
                                                score/moments.parquet

Types and values (SCORE_V02 section 4), on non-excluded edges of the main
classes (motorway to unclassified), from unsmoothed per-edge signals:

  water   W >= 0.5 or V_water >= 0.7; m = max(W, V_water) * (0.5 + 0.5 * size)
  vista   open >= 0.6, drop to the lowest ground within 3 km >= 150 m and a
          terrain sight line on dem90 of >= 2 km; m = clip(drop / 300, 0.5, 1)
  gorge   OSM waterfall or cliff within 150 m of a sample point, or a road
          bridge over water with the ground >= 30 m below the deck within
          200 m; m = 0.8
  osm     OSM tourism=viewpoint within 100 m of a sample point; m = 0.7

Candidates are clustered along the same road key within 2 km (greedy:
long water bridges first, then the best unclaimed edge takes every candidate
of its road within 2 km), then across roads within 1 km. A moment keeps its
best edge (its midpoint is the moment's position) and the list of member
edges. A long water bridge anchors its cluster: the deck is the position,
and the moment's value is the best member edge's.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

MOMENT_CLASSES = ("motorway", "trunk", "primary", "secondary", "tertiary", "unclassified")
TYPES = ("water", "vista", "gorge", "osm")
PLACE_RANK = {"city": 0, "town": 1, "village": 2, "hamlet": 3, "suburb": 4, "neighbourhood": 5, "locality": 6}

# --------------------------------------------------------------------------
# OSM features


def _is_bridge(tags) -> bool:
    b = (tags.get("bridge") or "").strip().lower()
    return b not in ("", "no")


STORM_WATER = ("basin", "wastewater", "reflecting_pool", "fountain", "pool")  # water=* values


BRIDGE_WORDS = ("bridge", "viaduct", "causeway")


def bridge_name(tags) -> str | None:
    """A bridge's own name: bridge:name, else a way name that names a
    structure (contains Bridge, Viaduct or Causeway). A plain road name
    ("FDR Drive") is not a bridge name."""
    bn = tags.get("bridge:name")
    if bn:
        return bn
    name = tags.get("name")
    if name and any(w in name.lower() for w in BRIDGE_WORDS):
        return name
    return None


def storm_tag(tags) -> str | None:
    """The tag that marks an OSM water area as stormwater or otherwise
    artificial (water=basin/wastewater/..., landuse=basin, any basin=*),
    None for ordinary water. Ponds are judged by size elsewhere."""
    w = (tags.get("water") or "").strip().lower()
    if w in STORM_WATER:
        return f"water={w}"
    if tags.get("landuse") == "basin":
        return "landuse=basin"
    b = (tags.get("basin") or "").strip().lower()
    if b:
        return f"basin={b}"
    return None


def is_stormwater(storm: str | None, name, area: float, named_min_m2: float) -> bool:
    """Whether an area with stormwater tag `storm` (storm_tag) is
    stormwater. A named body of at least named_min_m2 is a lake whatever
    its tags (Lewiston Reservoir, 7.6 km2, and Muskrat Lake, 39 ha, are
    landuse=basin), unless it is wastewater."""
    if not storm:
        return False
    return not (name and area >= named_min_m2 and storm != "water=wastewater")


def read_osm_features(pbf: Path, min_water_m2: float = 1e4, pond_floor_m2: float = 4e4, progress: bool = True,
                      named_min_m2: float = 1e5):
    """Point features and water areas from the PBF, as a pyarrow table with
    columns kind, name, lon, lat, way_id, wkb (EPSG:4326 WKB or null),
    place, area_m2 and tag.

    kinds: viewpoint (tourism=viewpoint, nodes and way/area centroids),
    waterfall (waterway=waterfall, nodes and way vertices), cliff
    (natural=cliff line, one row per way with its WKB; nodes as points),
    bridge (highway way with bridge=*: way_id, and as name its bridge:name
    or a way name containing Bridge/Viaduct/Causeway, see bridge_name), place (place=city ...
    locality nodes with a name; name of its kind in `wkb` is null),
    water (named natural=water / landuse=reservoir / waterway=riverbank
    area of at least `min_water_m2`, WKB polygon, tag = its water=* value),
    bay (named natural=bay or natural=strait area, WKB polygon; or a named
    natural=bay node, no WKB), stormwater (water=basin, wastewater ...,
    landuse=basin or basin=* area of any size, or an unnamed water=pond
    under `pond_floor_m2`; tag says which; a named one of at least
    `named_min_m2` is water unless it is wastewater), river (named waterway=river
    line, WKB), canal (named or unnamed waterway=canal line, WKB).
    Unnamed member ways inherit unambiguous waterway relation names."""
    import osmium
    import pyarrow as pa
    import shapely
    from osmium.filter import KeyFilter

    # Relation names bridge gaps in the tags of member waterway segments.
    relation_names = {}
    for rel in osmium.FileProcessor(str(pbf), entities=osmium.osm.RELATION):
        if rel.tags.get("type") == "waterway" and rel.tags.get("waterway") in ("river", "canal") and rel.tags.get("name"):
            for member in rel.members:
                if member.type == "w":
                    relation_names.setdefault(member.ref, set()).add(rel.tags.get("name"))
    wkbf = osmium.geom.WKBFactory()
    rows = {"kind": [], "name": [], "lon": [], "lat": [], "way_id": [], "wkb": [], "place": [], "area_m2": [],
            "tag": []}

    def add(kind, name, lon, lat, way_id=-1, wkb=None, place=None, area=np.nan, tag=None):
        rows["kind"].append(kind)
        rows["name"].append(name)
        rows["lon"].append(lon)
        rows["lat"].append(lat)
        rows["way_id"].append(way_id)
        rows["wkb"].append(wkb)
        rows["place"].append(place)
        rows["area_m2"].append(area)
        rows["tag"].append(tag)

    proc = (osmium.FileProcessor(str(pbf))
            .with_locations("flex_mem")
            .with_areas(KeyFilter("natural", "landuse", "waterway", "tourism", "water", "basin"))
            .with_filter(KeyFilter("tourism", "waterway", "natural", "highway", "place", "landuse", "water",
                                   "basin")))
    n = bad = 0
    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    for obj in proc:
        n += 1
        tags = obj.tags
        name = tags.get("name")
        t = obj.type_str()
        try:
            if t == "n":
                loc = obj.location
                if tags.get("tourism") == "viewpoint":
                    add("viewpoint", name, loc.lon, loc.lat)
                if tags.get("waterway") == "waterfall":
                    add("waterfall", name, loc.lon, loc.lat)
                if tags.get("natural") == "cliff":
                    add("cliff", name, loc.lon, loc.lat)
                if tags.get("natural") == "bay" and name:
                    add("bay", name, loc.lon, loc.lat)
                pl = tags.get("place")
                if pl in PLACE_RANK and name:
                    add("place", name, loc.lon, loc.lat, place=pl)
            elif t == "w":
                inherited = relation_names.get(obj.id, set())
                if not name and len(inherited) == 1:
                    name = next(iter(inherited))
                hw = tags.get("highway")
                if hw and _is_bridge(tags):
                    add("bridge", bridge_name(tags), np.nan, np.nan, way_id=obj.id)
                if tags.get("natural") == "cliff":
                    g = bytes.fromhex(wkbf.create_linestring(obj))
                    c = shapely.from_wkb(g).centroid
                    add("cliff", name, c.x, c.y, way_id=obj.id, wkb=g)
                if tags.get("waterway") == "waterfall":
                    for nd in obj.nodes:
                        add("waterfall", name, nd.location.lon, nd.location.lat, way_id=obj.id)
                if tags.get("waterway") in ("river", "canal") and (name or tags.get("waterway") == "canal"):
                    g = bytes.fromhex(wkbf.create_linestring(obj))
                    c = shapely.from_wkb(g).centroid
                    add(tags.get("waterway"), name, c.x, c.y, way_id=obj.id, wkb=g, tag=tags.get("waterway"))
            elif t == "a":
                if tags.get("tourism") == "viewpoint":
                    g = shapely.from_wkb(bytes.fromhex(wkbf.create_multipolygon(obj)))
                    c = g.centroid
                    add("viewpoint", name, c.x, c.y, way_id=obj.orig_id())
                nat = tags.get("natural")
                water = (nat == "water" or tags.get("landuse") in ("reservoir", "basin")
                         or tags.get("waterway") == "riverbank" or bool(tags.get("basin")))
                bay = nat in ("bay", "strait") and bool(name)
                if not (water or bay):
                    continue
                storm = storm_tag(tags) if water else None
                wv = (tags.get("water") or "").strip().lower() or None
                if not (bay or storm or name or wv == "pond"):
                    continue
                gb = bytes.fromhex(wkbf.create_multipolygon(obj))
                g = shapely.from_wkb(gb)
                area = abs(geod.geometry_area_perimeter(g)[0])
                c = g.representative_point()
                if bay:
                    add("bay", name, c.x, c.y, way_id=obj.orig_id(), wkb=gb, area=area, tag=nat)
                elif is_stormwater(storm, name, area, named_min_m2):
                    add("stormwater", name, c.x, c.y, way_id=obj.orig_id(), wkb=gb, area=area, tag=storm)
                elif wv == "pond" and not name and area < pond_floor_m2:
                    add("stormwater", name, c.x, c.y, way_id=obj.orig_id(), wkb=gb, area=area, tag="water=pond")
                elif name and area >= min_water_m2 and (tags.get("intermittent") or "").lower() != "yes":
                    add("water", name, c.x, c.y, way_id=obj.orig_id(), wkb=gb, area=area, tag=wv)
        except (RuntimeError, osmium.InvalidLocationError, ValueError):
            bad += 1
        if progress and n % 500_000 == 0:
            sys.stderr.write(f"\r  osm features: {n:,} objects, {len(rows['kind']):,} kept")
            sys.stderr.flush()
    if progress:
        sys.stderr.write(f"\r  osm features: {n:,} objects, {len(rows['kind']):,} kept, {bad} bad\n")
    propagate_canal_names(rows)
    return pa.table({
        "kind": pa.array(rows["kind"], pa.string()).dictionary_encode(),
        "name": pa.array(rows["name"], pa.string()),
        "lon": pa.array(rows["lon"], pa.float64()), "lat": pa.array(rows["lat"], pa.float64()),
        "way_id": pa.array(rows["way_id"], pa.int64()),
        "wkb": pa.array(rows["wkb"], pa.binary()),
        "place": pa.array(rows["place"], pa.string()),
        "area_m2": pa.array(rows["area_m2"], pa.float64()),
        "tag": pa.array(rows["tag"], pa.string()),
    })


def propagate_canal_names(rows):
    """Name unnamed canal segments only through touching canal endpoints.

    Iterate along the canal network; conflicting named channels leave a
    segment unnamed. Nearby parallel waterways and streams cannot inherit.
    """
    import shapely
    ix = [i for i, k in enumerate(rows["kind"]) if k == "canal"]
    if not ix:
        return
    from .water import project_wkb
    gs = project_wkb([rows["wkb"][i] for i in ix])
    ends = [shapely.points([g.coords[0], g.coords[-1]]) for g in gs]
    tree = shapely.STRtree([shapely.MultiPoint(e) for e in ends])
    neighbours = [tree.query(shapely.MultiPoint(e), predicate="dwithin", distance=1.0) for e in ends]
    while True:
        changes = {}
        for j, i in enumerate(ix):
            if rows["name"][i]:
                continue
            names = {rows["name"][ix[k]] for k in neighbours[j] if rows["name"][ix[k]]}
            if len(names) == 1:
                changes[i] = names.pop()
        if not changes:
            break
        for i, name in changes.items():
            rows["name"][i] = name


# --------------------------------------------------------------------------
# Edge-level values


def water_value(W: np.ndarray, V_water: np.ndarray, size: np.ndarray, cfg: dict) -> np.ndarray:
    """m for the water type, 0 where the edge is not a water candidate."""
    W = np.asarray(W, dtype=np.float64)
    Vw = np.asarray(V_water, dtype=np.float64)
    cand = (W >= cfg["water_W"]) | (Vw >= cfg["water_V"])
    m = np.maximum(W, Vw) * (0.5 + 0.5 * np.clip(size, 0, 1))
    return np.where(cand, m, 0.0)


def long_water_bridge(bridge, length, wet_share, mc):
    """Long bridge with a substantial measured crossing of areal water."""
    length = np.asarray(length)
    return (np.asarray(bridge, bool) & (length >= mc["water_bridge_min_m"])
            & (length * np.asarray(wet_share) >= mc["water_bridge_wet_m"]))


def representative_rank(m, major_bridge):
    """Anchor discrete major crossings before scenic shore stretches."""
    return np.asarray(m) + 2 * np.asarray(major_bridge, bool)


def anchored_value(m, owner, heads, anchored) -> np.ndarray:
    """The value of each moment: its head edge's m, or, for a cluster
    anchored on a bridge by representative_rank, the best m among its member
    edges (the bridge sets the position, not a lower value). `owner` maps
    each candidate to its head candidate; `heads` are head candidates."""
    m = np.asarray(m, dtype=np.float64)
    best = np.full(len(m), -np.inf)
    np.maximum.at(best, np.asarray(owner), m)
    heads = np.asarray(heads)
    return np.where(np.asarray(anchored, bool)[heads], best[heads], m[heads])


def vista_value(drop_m: np.ndarray, cfg: dict) -> np.ndarray:
    return np.clip(np.asarray(drop_m, dtype=np.float64) / cfg["vista_drop_scale_m"], cfg["vista_m_min"], 1.0)


def sight_rays(px, py, ze, grid90, dem90: np.ndarray, n_rays: int, max_m: float, tol: float,
               below_m: float) -> np.ndarray:
    """Terrain sight line (m) along each of `n_rays` azimuths from each eye
    point (px, py, ze) over dem90, to ground at least `below_m` below the
    eye; shape (n, n_rays). Ground at distance d is in sight when it is not
    more than `tol` below the steepest elevation angle of the ground before
    it (a viewshed along the ray)."""
    px = np.asarray(px, dtype=np.float64)
    py = np.asarray(py, dtype=np.float64)
    ze = np.asarray(ze, dtype=np.float64)
    n = len(px)
    out = np.zeros((n, n_rays))
    step = grid90.res
    ks = np.arange(1, int(max_m // step) + 1)
    for j, a in enumerate(np.arange(n_rays) * 2 * np.pi / n_rays):
        ux, uy = np.cos(a), np.sin(a)
        amax = np.full(n, -np.inf)
        best = np.zeros(n)
        for k in ks:
            d = k * step
            r, c, ins = grid90.rowcol(px + ux * d, py + uy * d)
            zg = np.where(ins, dem90[r, c], np.nan).astype(np.float64)
            ang = (zg - ze) / d
            seen = ins & (zg + tol >= ze + amax * d)
            ok = seen & (zg <= ze - below_m)
            best = np.where(ok, np.maximum(best, d), best)
            amax = np.where(ins, np.fmax(amax, ang), amax)
        out[:, j] = best
    return out


def far_sight(px, py, ze, grid90, dem90: np.ndarray, n_rays: int, max_m: float, tol: float,
              below_m: float) -> np.ndarray:
    """Longest terrain sight line (m) from each eye point over `n_rays`
    azimuths (the maximum of `sight_rays`)."""
    return sight_rays(px, py, ze, grid90, dem90, n_rays, max_m, tol, below_m).max(axis=1)


def window_min(arr: np.ndarray, r: np.ndarray, c: np.ndarray, radius_cells: float,
               ignore=None) -> np.ndarray:
    """Minimum of `arr` within a disk of `radius_cells` around each cell
    (r, c); cells equal to `ignore` do not count."""
    k = int(np.ceil(radius_cells))
    dr, dc = np.mgrid[-k:k + 1, -k:k + 1]
    disk = dr ** 2 + dc ** 2 <= radius_cells ** 2
    dr, dc = dr[disk], dc[disk]
    out = np.full(len(r), np.inf)
    H, Wd = arr.shape
    for i in range(0, len(r), 20000):
        rr = np.clip(r[i:i + 20000, None] + dr[None], 0, H - 1)
        cc = np.clip(c[i:i + 20000, None] + dc[None], 0, Wd - 1)
        v = arr[rr, cc].astype(np.float64)
        if ignore is not None:
            v[v == ignore] = np.inf
        out[i:i + 20000] = v.min(axis=1)
    return out


def near_points(px, py, fx, fy, radius: float) -> np.ndarray:
    """Index of the nearest feature (fx, fy) within `radius` of each point,
    -1 if none."""
    from scipy.spatial import cKDTree

    if len(fx) == 0 or len(px) == 0:
        return np.full(len(px), -1, np.int64)
    d, i = cKDTree(np.column_stack([fx, fy])).query(np.column_stack([px, py]), distance_upper_bound=radius,
                                                    workers=-1)
    return np.where(np.isfinite(d), i, -1)


def densify_lines(wkbs, step: float = 50.0):
    """Projected (EPSG:5070) points every `step` m along WKB lines, with the
    index of the line each point came from."""
    import shapely

    from .water import project_wkb

    geoms = project_wkb(list(wkbs))
    xs, ys, owner = [], [], []
    for i, g in enumerate(geoms):
        if g is None or g.is_empty:
            continue
        n = max(2, int(np.ceil(g.length / step)) + 1)
        p = shapely.line_interpolate_point(g, np.linspace(0, g.length, n))
        xs.append(shapely.get_x(p))
        ys.append(shapely.get_y(p))
        owner.append(np.full(n, i))
    if not xs:
        return np.zeros(0), np.zeros(0), np.zeros(0, np.int64)
    return np.concatenate(xs), np.concatenate(ys), np.concatenate(owner)


def junction_density(u, v, node_x, node_y, qx, qy, radius: float) -> np.ndarray:
    """Junctions (nodes where three or more edges meet) per km2 within
    `radius` of each query point."""
    from scipy.spatial import cKDTree

    n_nodes = len(node_x)
    deg = np.bincount(u, minlength=n_nodes) + np.bincount(v, minlength=n_nodes)
    j = np.flatnonzero((deg >= 3) & np.isfinite(node_x))
    if len(j) == 0 or len(qx) == 0:
        return np.zeros(len(qx))
    tree = cKDTree(np.column_stack([node_x[j], node_y[j]]))
    cnt = tree.query_ball_point(np.column_stack([qx, qy]), radius, return_length=True, workers=-1)
    return np.asarray(cnt, dtype=np.float64) / (np.pi * (radius / 1000) ** 2)


def edges_near(px, py, pe, geoms, radius: float, n_edges: int) -> np.ndarray:
    """Per edge, True when any of its sample points (px, py, edge pe) lies
    within `radius` of one of the (projected) shapely `geoms`."""
    import shapely

    out = np.zeros(n_edges, bool)
    geoms = [g_ for g_ in geoms if g_ is not None and not g_.is_empty]
    if not geoms or len(px) == 0:
        return out
    tree = shapely.STRtree(geoms)
    qi, _ = tree.query(shapely.points(px, py), predicate="dwithin", distance=radius)
    out[np.asarray(pe)[qi]] = True
    return out


WATER_DROP = ("kept", "small", "stormwater", "motorway", "town", "narrow")


def water_gate(highway, size, wfrac, dev, jdens, named, storm, mc: dict, bridge=None) -> np.ndarray:
    """Why each water candidate edge is dropped (index into WATER_DROP; 0 =
    kept), in this order:

      small       size < water_min_size, or < water_named_min_size when a
                  named lake, bay or river is near
      narrow      unnamed water filling less than water_unnamed_min_wfrac
                  of the 1 km disk (a creek or canal whose size comes from
                  the lake it drains into)
      stormwater  a stormwater area (basin, wastewater, small pond ...) is
                  near and size < water_storm_override_size
      motorway    a motorway edge
      town        built up: dev >= water_town_dev or junction density >=
                  water_town_junctions_km2

    "big" water lifts stormwater and town filters: named, size >= water_big_size and a
    local water fraction (1 km disk) >= water_broad_wfrac, i.e. a named lake
    or a wide river (Lake Ontario in a town, the Hudson under a motorway
    bridge). When `bridge` is supplied, motorway water also requires a
    long bridge with a substantial measured water crossing."""
    hw = np.asarray(highway).astype(str)
    size, wfrac = np.asarray(size, dtype=np.float64), np.asarray(wfrac, dtype=np.float64)
    named, storm = np.asarray(named, bool), np.asarray(storm, bool)
    big = named & (size >= mc["water_big_size"]) & (wfrac >= mc["water_broad_wfrac"])
    town = (np.asarray(dev) >= mc["water_town_dev"]) | (np.asarray(jdens) >= mc["water_town_junctions_km2"])
    floor = np.where(named, mc["water_named_min_size"], mc["water_min_size"])
    reason = np.zeros(len(hw), np.int8)
    reason[size < floor] = 1
    reason[(reason == 0) & ~named & (wfrac < mc.get("water_unnamed_min_wfrac", 0.0))] = 5
    reason[(reason == 0) & storm & (size < mc["water_storm_override_size"]) & ~big] = 2
    motorway_ok = big if bridge is None else big & np.asarray(bridge, bool)
    reason[(reason == 0) & (hw == "motorway") & ~motorway_ok] = 3
    reason[(reason == 0) & town & ~big] = 4
    return reason


def type_allowed(highway, moment_type: str, motorway_types) -> np.ndarray:
    """Per edge, whether it may hold a moment of `moment_type`: any
    non-motorway edge, and motorway edges only for the types listed in
    motorway_types."""
    hw = np.asarray(highway).astype(str)
    if moment_type in set(motorway_types):
        return np.ones(len(hw), bool)
    return hw != "motorway"


def thin_heads(m, x, y, radius: float) -> np.ndarray:
    """Greedy thinning, best m first: a point within `radius` of a better
    kept point is dropped. Returns the kept mask (input order)."""
    from scipy.spatial import cKDTree

    n = len(m)
    keep = np.zeros(n, bool)
    if n == 0:
        return keep
    xy = np.column_stack([x, y])
    tree = cKDTree(xy)
    gone = np.zeros(n, bool)
    for i in np.lexsort((np.arange(n), -np.asarray(m, dtype=np.float64))):
        if gone[i]:
            continue
        keep[i] = True
        gone[np.asarray(tree.query_ball_point(xy[i], radius), dtype=np.int64)] = True
    return keep

# --------------------------------------------------------------------------
# Clustering


def cluster(m: np.ndarray, x: np.ndarray, y: np.ndarray, key: np.ndarray, road_m: float,
            cross_m: float) -> tuple[np.ndarray, np.ndarray]:
    """Greedy clustering of candidate edges (SCORE_V02 section 4).

    Along roads: in order of m (best first), an unclaimed candidate claims
    every unclaimed candidate of the same road key within `road_m` of it.
    Across roads: in order of m, a surviving cluster head absorbs every
    other head (any key) within `cross_m`, with its members.

    Returns (head, owner): the candidate positions that head a moment, best
    first, and for every candidate the position of the head it belongs to."""
    from scipy.spatial import cKDTree

    n = len(m)
    owner = np.full(n, -1, np.int64)
    if n == 0:
        return np.zeros(0, np.int64), owner
    order = np.lexsort((np.arange(n), -np.asarray(m, dtype=np.float64)))
    xy = np.column_stack([x, y])
    tree = cKDTree(xy)
    heads = []
    for i in order:
        if owner[i] >= 0:
            continue
        nb = np.asarray(tree.query_ball_point(xy[i], road_m), dtype=np.int64)
        nb = nb[(key[nb] == key[i]) & (owner[nb] < 0)]
        owner[nb] = i
        owner[i] = i
        heads.append(i)
    heads = np.asarray(heads, dtype=np.int64)  # already best first
    htree = cKDTree(xy[heads])
    alive = np.ones(len(heads), bool)
    head_of = np.arange(len(heads))
    for j in range(len(heads)):
        if not alive[j]:
            continue
        nb = np.asarray(htree.query_ball_point(xy[heads[j]], cross_m), dtype=np.int64)
        nb = nb[(nb != j) & alive[nb]]
        alive[nb] = False
        head_of[nb] = j
    # resolve chains (an absorbed head never absorbs, so one hop suffices)
    final = heads[head_of]
    pos = np.full(n, -1, np.int64)
    pos[heads] = np.arange(len(heads))
    owner = final[pos[owner]]
    return heads[alive], owner

# --------------------------------------------------------------------------
# Detection


def edge_midpoints(sx, sy, sedge, n_edges: int) -> tuple[np.ndarray, np.ndarray]:
    """The middle sample point of every edge (samples are stored edge by edge)."""
    first = np.searchsorted(sedge, np.arange(n_edges))
    last = np.searchsorted(sedge, np.arange(n_edges), side="right") - 1
    mid = (first + last) // 2
    return sx[mid], sy[mid]


def _label(feature, road, place) -> str:
    parts = [feature] if feature else []
    if road and not (feature and road.strip().casefold() == feature.strip().casefold()):
        parts.append(("on " if parts else "") + road)
    if place:
        parts.append(f"near {place}")
    return " ".join(parts) or "unnamed"


def detect(graph_dir: Path, out_dir: Path, rasters_dir: Path, cfg: dict, log=print) -> tuple:
    """Moments for every edge of edges.parquet (see module docstring).
    Returns (pyarrow table for moments.parquet, info dict)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from .graph import edges_fingerprint
    from .rasters import Grid, read_tif
    from .score import _dict_col, load_samples, lookup, project_lonlat, read_edge_signals

    mc = cfg["moments"]
    g30 = Grid.from_json((rasters_dir / "grid.json").read_text())
    g90 = g30.coarsen(cfg["grid"]["block"])
    et = pq.read_table(graph_dir / "edges.parquet",
                       columns=["u", "v", "way_id", "length_m", "highway", "ref", "name"])
    fp = edges_fingerprint(et)
    n_e = et.num_rows
    sig = read_edge_signals(out_dir / "edge_signals.parquet", fp)
    W = sig.column("W").to_numpy().astype(np.float64)
    Vw = sig.column("V_water").to_numpy().astype(np.float64)
    excluded = sig.column("excluded").to_numpy()
    dev = sig.column("dev").to_numpy().astype(np.float64)
    key = sig.column("key").to_numpy()
    hw = _dict_col(et, "highway").astype(str)
    ref, name = _dict_col(et, "ref"), _dict_col(et, "name")
    way_id = et.column("way_id").to_numpy()
    ok_edge = np.isin(hw, list(mc["classes"])) & ~excluded

    smp = load_samples(out_dir / "samples.npz", fp)
    sx_all, sy_all, se_all = smp["x"].astype(np.float64), smp["y"].astype(np.float64), smp["edge"]
    mx, my = edge_midpoints(sx_all, sy_all, se_all, n_e)
    u, v = et.column("u").to_numpy().astype(np.int64), et.column("v").to_numpy().astype(np.int64)
    n_nodes = int(max(u.max(), v.max())) + 1 if n_e else 0
    node_x, node_y = np.full(n_nodes, np.nan), np.full(n_nodes, np.nan)
    e_first = np.searchsorted(se_all, np.arange(n_e))
    e_last = np.searchsorted(se_all, np.arange(n_e), side="right") - 1
    node_x[u], node_y[u] = sx_all[e_first], sy_all[e_first]
    node_x[v], node_y[v] = sx_all[e_last], sy_all[e_last]
    del e_first, e_last
    from .boundary import edges_in_state, filter_moments, load_boundary

    state = load_boundary()
    edge_inside = edges_in_state(u, v, node_x, node_y, state)
    keep = ok_edge[se_all]
    sx, sy, se = sx_all[keep], sy_all[keep], se_all[keep]
    del sx_all, sy_all, se_all
    info = {"candidate_edges": int(ok_edge.sum()), "candidate_points": int(len(sx))}

    dem30 = read_tif(rasters_dir / "dem30.tif")
    dem90 = read_tif(rasters_dir / "dem90.tif")
    z = lookup(g30, dem30, sx, sy, fallback=-32768)
    bad = z <= -32768
    z[bad] = lookup(g90, dem90, sx[bad], sy[bad], fallback=0.0)

    m_type = np.zeros((n_e, len(TYPES)))
    ft = pq.read_table(out_dir / "osm_features.parquet")
    kind = ft.column("kind").to_pylist()
    kind = np.asarray(kind, dtype=object)
    flon, flat = ft.column("lon").to_numpy(), ft.column("lat").to_numpy()
    fname = np.asarray(ft.column("name").to_pylist(), dtype=object)
    fwkb = ft.column("wkb").to_pylist()
    farea = ft.column("area_m2").to_numpy() if "area_m2" in ft.column_names else np.full(len(kind), np.nan)
    ftag = np.asarray(ft.column("tag").to_pylist() if "tag" in ft.column_names else [None] * len(kind),
                      dtype=object)

    def pts_of(kinds):
        sel = np.flatnonzero(np.isin(kind, kinds) & np.isfinite(flon))
        x, y = project_lonlat(flon[sel], flat[sel])
        return x, y, sel


    # water ------------------------------------------------------------------
    fway = ft.column("way_id").to_numpy()
    bridge_ways = np.unique(fway[kind == "bridge"])
    bridge_edge = np.isin(way_id, bridge_ways)
    water_area = read_tif(rasters_dir / "water_area.tif")
    rr, cc, inside = g30.rowcol(sx, sy)
    wet_samples = inside & (water_area[rr, cc] > 0)
    del water_area
    wet_share = np.bincount(se, weights=wet_samples, minlength=n_e) / np.maximum(1, np.bincount(se, minlength=n_e))
    length = et.column("length_m").to_numpy()
    long_bridge = long_water_bridge(bridge_edge, length, wet_share, mc)
    named_bridge_ways = fway[(kind == "bridge") & (fname != None)]  # noqa: E711
    named_bridge = long_bridge & np.isin(way_id, named_bridge_ways)
    wcand = ok_edge & ((W >= mc["water_W"]) | (Vw >= mc["water_V"]) | long_bridge)
    size30 = read_tif(rasters_dir / "water_size.tif")
    k = cfg["grid"]["block"]
    H, Wd = g90.shape
    size90 = size30[:H * k, :Wd * k].reshape(H, k, Wd, k).max(axis=(1, 3))
    del size30
    from scipy import ndimage

    rad = int(round(mc["size_radius_m"] / g90.res))
    size_near90 = ndimage.maximum_filter(size90, size=2 * rad + 1)
    pw = wcand[se]
    size_pt = lookup(g90, size_near90, sx[pw], sy[pw])
    del size_near90
    big = read_tif(rasters_dir / "bigwater_idx90.tif")
    r, c, ins = g90.rowcol(sx[pw], sy[pw])
    ti = np.where(ins, big[r, c], -1).astype(np.int64)
    sv = np.where(ti >= 0, size90.ravel()[np.maximum(ti, 0)], 0.0)
    del big
    size_e = np.zeros(n_e)
    np.maximum.at(size_e, se[pw], size_pt)
    size_view = np.zeros(n_e)
    np.maximum.at(size_view, se[pw], sv)
    size_e = np.where(Vw >= mc["water_V"], np.maximum(size_e, size_view), size_e)
    # Long wet bridges use local water coverage instead of diluted edge means.
    bridge_value = np.maximum(W, Vw) * (0.5 + 0.5 * size_e)
    m_water = np.where(wcand, np.maximum(water_value(W, Vw, size_e, mc),
                                        np.where(long_bridge, bridge_value, 0.0)), 0.0)
    info["water_edges"] = int(wcand.sum())

    # water gate (Moments v0.3): size or name, stormwater, motorways, towns
    from .water import project_wkb

    wf90 = read_tif(rasters_dir / "wfrac90.tif")
    rb = int(round(mc["water_broad_radius_m"] / g90.res))
    wf_near = ndimage.maximum_filter(wf90, size=2 * rb + 1) if rb > 0 else wf90
    del wf90
    wf_pt = lookup(g90, wf_near, sx[pw], sy[pw])
    wfrac_e = np.zeros(n_e)
    np.maximum.at(wfrac_e, se[pw], wf_pt)
    has_wkb = np.asarray([w_ is not None for w_ in fwkb])
    big_named = np.flatnonzero(np.isin(kind, ["water", "bay"]) & has_wkb & (np.nan_to_num(farea) >= mc["water_named_min_m2"]))
    rivers = np.flatnonzero(np.isin(kind, ["river", "canal"]) & (fname != None) & has_wkb)
    storm = np.flatnonzero((kind == "stormwater") & has_wkb)
    river_g = project_wkb([fwkb[i] for i in rivers])
    named_e = (edges_near(sx[pw], sy[pw], se[pw], project_wkb([fwkb[i] for i in big_named]), mc["size_radius_m"],
                          n_e)
               | edges_near(sx[pw], sy[pw], se[pw], river_g, mc["river_name_m"], n_e))
    # a wide river's named centre line can be far from the shore (the Hudson
    # is 1-2 km wide; its area polygons carry no name)
    pb = np.flatnonzero(wfrac_e[se[pw]] >= mc["water_broad_wfrac"])
    named_e |= edges_near(sx[pw][pb], sy[pw][pb], se[pw][pb], river_g, mc["river_wide_name_m"], n_e)
    named_e |= named_bridge
    del river_g
    storm_e = edges_near(sx[pw], sy[pw], se[pw], project_wkb([fwkb[i] for i in storm]), mc["water_storm_m"], n_e)
    we = np.flatnonzero(wcand)
    jd = junction_density(u, v, node_x, node_y, mx[we], my[we], mc["water_town_junction_radius_m"])
    why = water_gate(hw[we], size_e[we], wfrac_e[we], dev[we], jd, named_e[we], storm_e[we], mc, bridge=long_bridge[we])
    # The extra candidate rule requires broad named water as well as a wet deck.
    extra = long_bridge[we] & (W[we] < mc["water_W"]) & (Vw[we] < mc["water_V"])
    why[extra & ~(named_e[we] & (wfrac_e[we] >= mc["water_broad_wfrac"]))] = 5
    m_water[we[why > 0]] = 0.0
    m_type[:, 0] = m_water
    info["water_gate_edges"] = {WATER_DROP[k]: int((why == k).sum()) for k in range(len(WATER_DROP))}
    del node_x, node_y

    # vista ------------------------------------------------------------------
    open_pt = lookup(g90, read_tif(rasters_dir / "open90.tif"), sx, sy)
    min3k = lookup(g90, read_tif(rasters_dir / "min3k90.tif"), sx, sy, np.nan)
    drop = z - min3k
    vc = np.flatnonzero((open_pt >= mc["vista_open"]) & (drop >= mc["vista_drop_m"]))
    rays = sight_rays(sx[vc], sy[vc], z[vc] + cfg["water"]["eye_m"], g90, dem90, mc["vista_rays"],
                      mc["vista_far_m"], cfg["water"]["terrain_tol_m"], mc["vista_seen_below_m"])
    n_far = (rays >= mc["vista_sight_m"]).sum(axis=1)
    del rays
    vp = vc[n_far >= mc["vista_min_rays"]]
    vdrop = np.zeros(n_e)
    np.maximum.at(vdrop, se[vp], drop[vp])
    m_type[:, 1] = np.where(vdrop > 0, vista_value(vdrop, mc), 0.0)
    info.update(vista_points_open_drop=int(len(vc)), vista_points_any_ray=int((n_far >= 1).sum()),
                vista_points=int(len(vp)), vista_edges=int((vdrop > 0).sum()))
    del open_pt, min3k

    # gorge / falls and OSM viewpoints ----------------------------------------
    wfx, wfy, wsel = pts_of(["waterfall"])
    cl = np.flatnonzero((kind == "cliff"))
    cl_line = [i for i in cl if fwkb[i] is not None]
    cl_pt = np.asarray([i for i in cl if fwkb[i] is None], dtype=np.int64)
    lx, ly, lo = densify_lines([fwkb[i] for i in cl_line], mc["cliff_step_m"])
    px_, py_ = project_lonlat(flon[cl_pt], flat[cl_pt]) if len(cl_pt) else (np.zeros(0), np.zeros(0))
    gfx = np.concatenate([wfx, lx, px_])
    gfy = np.concatenate([wfy, ly, py_])
    gsrc = np.concatenate([wsel, np.asarray(cl_line, dtype=np.int64)[lo] if len(lo) else np.zeros(0, np.int64),
                           cl_pt]).astype(np.int64)
    hit = near_points(sx, sy, gfx, gfy, mc["gorge_feature_m"])
    gfeat = np.full(n_e, -1, np.int64)
    h = np.flatnonzero(hit >= 0)
    gfeat[se[h]] = gsrc[hit[h]]
    gorge = gfeat >= 0
    info["gorge_feature_edges"] = int(gorge.sum())

    br = ok_edge & np.isin(way_id, bridge_ways)
    bp = np.flatnonzero(br[se])
    area = read_tif(rasters_dir / "water_area.tif")
    line = read_tif(rasters_dir / "water_line.tif")
    r, c, ins = g30.rowcol(sx[bp], sy[bp])
    wet = ins & ((area[r, c] > 0) | (line[r, c] > 0))
    del area, line
    over = np.zeros(n_e, bool)
    over[se[bp[wet]]] = True
    # deck: the higher end of the edge (bridge ends sit on the abutments)
    first = np.searchsorted(se, np.arange(n_e))
    last = np.searchsorted(se, np.arange(n_e), side="right") - 1
    be = np.flatnonzero(over)
    deck = np.maximum(z[first[be]], z[last[be]])
    low = window_min(dem30, r[wet], c[wet], mc["gorge_bridge_radius_m"] / g30.res, ignore=-32768)
    low_e = np.full(n_e, np.inf)
    np.minimum.at(low_e, se[bp[wet]], low)
    gb = np.zeros(n_e, bool)
    gb[be] = deck - low_e[be] >= mc["gorge_bridge_drop_m"]
    info.update(bridge_edges=int(br.sum()), bridge_edges_over_water=int(over.sum()), gorge_bridge_edges=int(gb.sum()))
    # motorways: only the types in motorway_types (cliffs and waterfalls by a
    # motorway are mostly road cuts; water has its own gate)
    on = lambda t_: type_allowed(hw, t_, mc["motorway_types"])  # noqa: E731
    m_type[:, 1] = np.where(on("vista"), m_type[:, 1], 0.0)
    m_type[:, 2] = np.where((gorge & on("gorge")) | (gb & on("gorge_bridge")), mc["gorge_m"], 0.0)
    gfeat[~on("gorge")] = -1  # a motorway gorge_bridge by a road cut is not named after the cliff
    del dem30

    vfx, vfy, vsel = pts_of(["viewpoint"])
    hit = near_points(sx, sy, vfx, vfy, mc["osm_viewpoint_m"])
    vfeat = np.full(n_e, -1, np.int64)
    h = np.flatnonzero(hit >= 0)
    vfeat[se[h]] = vsel[hit[h]]
    m_type[:, 3] = np.where((vfeat >= 0) & on("osm"), mc["osm_m"], 0.0)
    info["osm_viewpoint_edges"] = int((vfeat >= 0).sum())
    vfeat[~on("osm")] = -1

    # cluster ----------------------------------------------------------------
    m_type[~ok_edge] = 0
    m_edge = m_type.max(axis=1)
    cand = np.flatnonzero(m_edge > 0)
    t_edge = m_type.argmax(axis=1)
    # A bridge is a discrete experience. Anchor a cluster on its deck rather
    # than an equally scenic shore road up to two kilometres away.
    anchor_rank = representative_rank(m_edge[cand], long_bridge[cand])
    heads, owner = cluster(anchor_rank, mx[cand], my[cand], key[cand], mc["cluster_road_m"],
                           mc["cluster_cross_m"])
    info["candidate_moment_edges"] = int(len(cand))
    # vistas: at most one per vista_spacing_m (the best by m); the others go
    # with their member edges
    is_v = t_edge[cand[heads]] == 1
    keep_h = np.ones(len(heads), bool)
    keep_h[is_v] = thin_heads(m_edge[cand[heads[is_v]]], mx[cand[heads[is_v]]], my[cand[heads[is_v]]],
                              mc["vista_spacing_m"])
    info["vistas_thinned"] = int((~keep_h).sum())
    hpos = np.full(len(cand), -1, np.int64)
    hpos[heads] = np.arange(len(heads))
    alive = keep_h[hpos[owner]]
    heads = heads[keep_h]
    m_moment = anchored_value(m_edge[cand], owner, heads, long_bridge[cand])

    # names --------------------------------------------------------------------
    hx, hy = mx[cand[heads]], my[cand[heads]]
    he = cand[heads]
    feature = np.full(len(heads), None, dtype=object)
    for j, e in enumerate(he):
        if t_edge[e] == 3 and vfeat[e] >= 0:
            feature[j] = fname[vfeat[e]] or "viewpoint"
        elif t_edge[e] == 2 and gfeat[e] >= 0:
            feature[j] = fname[gfeat[e]] or ("waterfall" if kind[gfeat[e]] == "waterfall" else "cliff")
    # broad water at the head: water candidates carry wfrac_e, other types
    # (gorge_bridge, vista) only the value at the head point
    wide = np.maximum(wfrac_e[he], lookup(g90, wf_near, hx, hy)) >= mc["water_broad_wfrac"]
    del wf_near
    feature, parent = _name_water(feature, hx, hy, kind, fname, fwkb, farea, ftag, flon, flat, mc, wide=wide)
    bsel_ = np.flatnonzero((kind == "bridge") & (fname != None))  # noqa: E711
    bridge_label = dict(zip(fway[bsel_][::-1].tolist(), fname[bsel_][::-1].tolist()))  # first row wins
    for j, e in enumerate(he):
        if feature[j] is None and named_bridge[e]:
            feature[j] = bridge_label[int(way_id[e])]
            parent[j] = feature[j]
    plx, ply, psel = pts_of(["place"])
    prank = np.asarray([PLACE_RANK.get(p, 9) for p in ft.column("place").to_pylist()])[psel]
    big_place = prank <= PLACE_RANK["hamlet"]
    pi = near_points(hx, hy, plx[big_place], ply[big_place], mc["place_radius_m"])
    place = np.where(pi >= 0, fname[psel[big_place]][np.maximum(pi, 0)], None)
    road = np.where(ref[he] != None, ref[he], name[he])  # noqa: E711

    lon, lat = _unproject(hx, hy)
    members = [[] for _ in heads]
    pos = {int(h_): j for j, h_ in enumerate(heads)}
    for i, o in enumerate(owner):
        if alive[i]:
            members[pos[int(o)]].append(int(cand[i]))
    tbl = pa.table({
        "id": pa.array(np.arange(len(heads), dtype=np.int32)),
        "type": pa.array([TYPES[t] for t in t_edge[he]]),
        "m": pa.array(m_moment.astype(np.float32)),
        "lon": pa.array(lon), "lat": pa.array(lat),
        "x": pa.array(hx), "y": pa.array(hy),
        "edge_idx": pa.array(he.astype(np.int32)),
        "key": pa.array(key[he].astype(np.int64)),
        "road": pa.array(list(road), pa.string()),
        "feature": pa.array(list(feature), pa.string()),
        "parent_feature": pa.array(list(parent), pa.string()),
        "place": pa.array(list(place), pa.string()),
        "label": pa.array([_label(f, r_, p) for f, r_, p in zip(feature, road, place)]),
        "n_edges": pa.array(np.asarray([len(x) for x in members], dtype=np.int32)),
        "members": pa.array(members, pa.list_(pa.int32())),
        "types": pa.array([sorted({TYPES[t_edge[e]] for e in mem}) for mem in members], pa.list_(pa.string())),
    })
    from .graph import FINGERPRINT_KEY

    tbl = tbl.replace_schema_metadata({FINGERPRINT_KEY: fp.encode()})
    counts = {t: int(sum(1 for x in tbl.column("type").to_pylist() if x == t)) for t in TYPES}
    info.update(moments=int(len(heads)), by_type=counts)
    return filter_moments(tbl, info, log, polygon=state, edge_inside=edge_inside)


def _unproject(x, y, crs: str = "EPSG:5070"):
    from pyproj import Transformer

    tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lon, lat = tr.transform(np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64))
    return np.asarray(lon), np.asarray(lat)


def _name_water(feature, hx, hy, kind, fname, fwkb, farea, ftag, flon, flat, mc, wide=None):
    """Fill unnamed moments with the most specific named water near them.

    Lakes (named water areas of at least water_named_min_m2) and bays
    (natural=bay/strait areas, water=bay) within water_name_m are
    candidates. The nearest one is the fallback; a smaller one wins when it
    is at most name_slack_m farther and is a bay or touches the nearest one
    (within name_touch_m: a bay or arm of the lake, not a pond behind the
    shore). If the pick is still a large body (>= bay_node_parent_m2) a
    named natural=bay node within bay_node_m names it instead. Moments with
    no lake or bay take the nearest named river or canal within river_name_m,
    or within river_wide_name_m where `wide` (broad water at the moment).
    A river or canal closer than the selected polygon takes precedence,
    unless a lake, bay or reservoir of at least lake_wins_min_m2 (not tagged
    canal, river or stream) lies within lake_wins_m of the moment: then the
    polygon pick stands (a creek crossing on a lakeshore road is the lake).

    Returns (feature, parent): parent is the water body the name belongs
    to, for spacing and grouping: for a bay polygon the largest candidate
    it touches (within name_touch_m), for an arm that won over the nearest
    body that body, for a bay node the body it names a part of, and the
    feature itself otherwise."""
    import shapely

    from .water import project_wkb

    parent = feature.copy()
    todo = np.flatnonzero(feature == None)  # noqa: E711
    if len(todo) == 0:
        return feature, parent
    has = np.asarray([w_ is not None for w_ in fwkb])
    area = np.nan_to_num(np.asarray(farea, dtype=np.float64))
    bayish = (kind == "bay") | (np.asarray(ftag, dtype=object) == "bay")
    idx = np.flatnonzero(has & (((kind == "water") & (area >= mc["water_named_min_m2"])) | (kind == "bay")))
    pts = shapely.points(hx[todo], hy[todo])
    area_distance = np.full(len(feature), np.inf)
    lake_guard = np.zeros(len(feature), bool)
    tag_s = np.asarray([str(t_ or "") for t_ in ftag], dtype=object)
    lakeish = ~np.asarray([any(w in t_ for w in ("canal", "river", "stream")) for t_ in tag_s], bool)
    big_lake = lakeish & (area >= mc["lake_wins_min_m2"])
    if len(idx):
        geoms = project_wkb([fwkb[i] for i in idx])
        tree = shapely.STRtree(geoms)
        qi, gi = tree.query(pts, predicate="dwithin", distance=mc["water_name_m"])
        d = shapely.distance(pts[qi], geoms[gi])
        touch: dict = {}
        bx, by, bsel = np.zeros(0), np.zeros(0), np.zeros(0, np.int64)
        bn = np.flatnonzero((kind == "bay") & ~has & np.isfinite(flon))
        if len(bn):
            from .score import project_lonlat

            bx, by = project_lonlat(flon[bn], flat[bn])
            bsel = bn
        for q in np.unique(qi):
            rows = np.flatnonzero(qi == q)
            lake_guard[todo[q]] = bool(np.any(big_lake[idx[gi[rows]]] & (d[rows] <= mc["lake_wins_m"])))
            near = rows[np.argmin(d[rows])]
            g0 = gi[near]
            best = g0
            for r in rows:
                g1 = gi[r]
                if g1 == g0 or d[r] > d[near] + mc["name_slack_m"] or area[idx[g1]] >= area[idx[best]]:
                    continue
                ok = bayish[idx[g1]]
                if not ok:
                    k_ = (int(g1), int(g0))
                    if k_ not in touch:
                        touch[k_] = bool(shapely.dwithin(geoms[g1], geoms[g0], mc["name_touch_m"]))
                    ok = touch[k_]
                if ok:
                    best = g1
            name = fname[idx[best]]
            top = g0  # an arm that won over the nearest body belongs to it
            if bayish[idx[best]]:  # a bay belongs to the largest body it touches
                top = best
                for r in rows:
                    g1 = gi[r]
                    if area[idx[g1]] > area[idx[top]]:
                        k_ = (int(g1), int(best))
                        if k_ not in touch:
                            touch[k_] = bool(shapely.dwithin(geoms[g1], geoms[best], mc["name_touch_m"]))
                        if touch[k_]:
                            top = g1
            parent[todo[q]] = fname[idx[top]]
            if area[idx[best]] >= mc["bay_node_parent_m2"] and len(bsel):
                dd = np.hypot(bx - hx[todo[q]], by - hy[todo[q]])
                j = int(np.argmin(dd))
                if dd[j] <= mc["bay_node_m"]:
                    name = fname[bsel[j]]
            feature[todo[q]] = name
            area_distance[todo[q]] = float(shapely.distance(pts[q], geoms[best]))
    wide = np.zeros(len(feature), bool) if wide is None else np.asarray(wide, bool)
    ridx = np.flatnonzero(np.isin(kind, ["river", "canal"]) & (fname != None) & has)
    if len(ridx) and len(todo):
        geoms = project_wkb([fwkb[i] for i in ridx])
        tree = shapely.STRtree(geoms)
        radius = np.where(wide[todo], max(mc["river_wide_name_m"], mc["river_name_m"]), mc["river_name_m"])
        (qi, gi), d = tree.query_nearest(pts, max_distance=float(radius.max()), return_distance=True,
                                         all_matches=False)
        ok = (d <= radius[qi]) & (d < area_distance[todo[qi]]) & ~lake_guard[todo[qi]]
        feature[todo[qi[ok]]] = fname[ridx[gi[ok]]]
        parent[todo[qi[ok]]] = fname[ridx[gi[ok]]]
    return feature, parent


def load_moments(path: Path, fingerprint: str | None = None):
    """moments.parquet, checked against the edges fingerprint when given."""
    import pyarrow.parquet as pq

    from .graph import FINGERPRINT_KEY

    if fingerprint is not None:
        stored = (pq.read_schema(path).metadata or {}).get(FINGERPRINT_KEY)
        if stored is None or stored.decode() != fingerprint:
            raise ValueError(f"{path} was not computed for the current edges.parquet; rerun the moments stage")
    return pq.read_table(path)
