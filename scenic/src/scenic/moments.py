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

Candidates are clustered along the same road key within 2 km (greedy: the
best unclaimed edge takes every candidate of its road within 2 km), then
across roads within 1 km. A moment keeps its best edge (its midpoint is the
moment's position) and the list of member edges.
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


def read_osm_features(pbf: Path, min_water_m2: float = 2e5, progress: bool = True):
    """Point features and named water from the PBF, as a pyarrow table with
    columns kind, name, lon, lat, way_id, wkb (EPSG:4326 WKB or null).

    kinds: viewpoint (tourism=viewpoint, nodes and way/area centroids),
    waterfall (waterway=waterfall, nodes and way vertices), cliff
    (natural=cliff line, one row per way with its WKB; nodes as points),
    bridge (highway way with bridge=*: way_id only), place (place=city ...
    locality nodes with a name; name of its kind in `wkb` is null),
    water (named natural=water / landuse=reservoir / waterway=riverbank
    area of at least `min_water_m2`, WKB polygon), river (named
    waterway=river line, WKB)."""
    import osmium
    import pyarrow as pa
    import shapely
    from osmium.filter import KeyFilter

    wkbf = osmium.geom.WKBFactory()
    rows = {"kind": [], "name": [], "lon": [], "lat": [], "way_id": [], "wkb": [], "place": []}

    def add(kind, name, lon, lat, way_id=-1, wkb=None, place=None):
        rows["kind"].append(kind)
        rows["name"].append(name)
        rows["lon"].append(lon)
        rows["lat"].append(lat)
        rows["way_id"].append(way_id)
        rows["wkb"].append(wkb)
        rows["place"].append(place)

    proc = (osmium.FileProcessor(str(pbf))
            .with_locations("flex_mem")
            .with_areas(KeyFilter("natural", "landuse", "waterway", "tourism"))
            .with_filter(KeyFilter("tourism", "waterway", "natural", "highway", "place", "landuse")))
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
                pl = tags.get("place")
                if pl in PLACE_RANK and name:
                    add("place", name, loc.lon, loc.lat, place=pl)
            elif t == "w":
                hw = tags.get("highway")
                if hw and _is_bridge(tags):
                    add("bridge", name, np.nan, np.nan, way_id=obj.id)
                if tags.get("natural") == "cliff":
                    g = bytes.fromhex(wkbf.create_linestring(obj))
                    c = shapely.from_wkb(g).centroid
                    add("cliff", name, c.x, c.y, way_id=obj.id, wkb=g)
                if tags.get("waterway") == "waterfall":
                    for nd in obj.nodes:
                        add("waterfall", name, nd.location.lon, nd.location.lat, way_id=obj.id)
                if tags.get("waterway") == "river" and name:
                    g = bytes.fromhex(wkbf.create_linestring(obj))
                    c = shapely.from_wkb(g).centroid
                    add("river", name, c.x, c.y, way_id=obj.id, wkb=g)
            elif t == "a":
                if tags.get("tourism") == "viewpoint":
                    g = shapely.from_wkb(bytes.fromhex(wkbf.create_multipolygon(obj)))
                    c = g.centroid
                    add("viewpoint", name, c.x, c.y, way_id=obj.orig_id())
                water = (tags.get("natural") == "water" or tags.get("landuse") == "reservoir"
                         or tags.get("waterway") == "riverbank")
                if water and name:
                    gb = bytes.fromhex(wkbf.create_multipolygon(obj))
                    g = shapely.from_wkb(gb)
                    area = abs(geod.geometry_area_perimeter(g)[0])
                    if area >= min_water_m2:
                        c = g.representative_point()
                        add("water", name, c.x, c.y, way_id=obj.orig_id(), wkb=gb)
        except (RuntimeError, osmium.InvalidLocationError, ValueError):
            bad += 1
        if progress and n % 500_000 == 0:
            sys.stderr.write(f"\r  osm features: {n:,} objects, {len(rows['kind']):,} kept")
            sys.stderr.flush()
    if progress:
        sys.stderr.write(f"\r  osm features: {n:,} objects, {len(rows['kind']):,} kept, {bad} bad\n")
    return pa.table({
        "kind": pa.array(rows["kind"], pa.string()).dictionary_encode(),
        "name": pa.array(rows["name"], pa.string()),
        "lon": pa.array(rows["lon"], pa.float64()), "lat": pa.array(rows["lat"], pa.float64()),
        "way_id": pa.array(rows["way_id"], pa.int64()),
        "wkb": pa.array(rows["wkb"], pa.binary()),
        "place": pa.array(rows["place"], pa.string()),
    })


# --------------------------------------------------------------------------
# Edge-level values


def water_value(W: np.ndarray, V_water: np.ndarray, size: np.ndarray, cfg: dict) -> np.ndarray:
    """m for the water type, 0 where the edge is not a water candidate."""
    W = np.asarray(W, dtype=np.float64)
    Vw = np.asarray(V_water, dtype=np.float64)
    cand = (W >= cfg["water_W"]) | (Vw >= cfg["water_V"])
    m = np.maximum(W, Vw) * (0.5 + 0.5 * np.clip(size, 0, 1))
    return np.where(cand, m, 0.0)


def vista_value(drop_m: np.ndarray, cfg: dict) -> np.ndarray:
    return np.clip(np.asarray(drop_m, dtype=np.float64) / cfg["vista_drop_scale_m"], cfg["vista_m_min"], 1.0)


def far_sight(px, py, ze, grid90, dem90: np.ndarray, n_rays: int, max_m: float, tol: float,
              below_m: float) -> np.ndarray:
    """Longest terrain sight line (m) from each eye point (px, py, ze) over
    dem90, over `n_rays` azimuths, to ground at least `below_m` below the eye.
    Ground at distance d is in sight when it is not more than `tol` below
    the steepest elevation angle of the ground before it (a viewshed along
    the ray)."""
    px = np.asarray(px, dtype=np.float64)
    py = np.asarray(py, dtype=np.float64)
    ze = np.asarray(ze, dtype=np.float64)
    n = len(px)
    best = np.zeros(n)
    step = grid90.res
    ks = np.arange(1, int(max_m // step) + 1)
    for a in np.arange(n_rays) * 2 * np.pi / n_rays:
        ux, uy = np.cos(a), np.sin(a)
        amax = np.full(n, -np.inf)
        for k in ks:
            d = k * step
            r, c, ins = grid90.rowcol(px + ux * d, py + uy * d)
            zg = np.where(ins, dem90[r, c], np.nan).astype(np.float64)
            ang = (zg - ze) / d
            seen = ins & (zg + tol >= ze + amax * d)
            ok = seen & (zg <= ze - below_m)
            best = np.where(ok, np.maximum(best, d), best)
            amax = np.where(ins, np.fmax(amax, ang), amax)
    return best


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
    if road:
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
    key = sig.column("key").to_numpy()
    hw = _dict_col(et, "highway").astype(str)
    ref, name = _dict_col(et, "ref"), _dict_col(et, "name")
    way_id = et.column("way_id").to_numpy()
    ok_edge = np.isin(hw, list(mc["classes"])) & ~excluded

    smp = load_samples(out_dir / "samples.npz", fp)
    sx_all, sy_all, se_all = smp["x"].astype(np.float64), smp["y"].astype(np.float64), smp["edge"]
    mx, my = edge_midpoints(sx_all, sy_all, se_all, n_e)
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

    # water ------------------------------------------------------------------
    wcand = ok_edge & ((W >= mc["water_W"]) | (Vw >= mc["water_V"]))
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
    m_type[:, 0] = np.where(wcand, water_value(W, Vw, size_e, mc), 0.0)
    info["water_edges"] = int(wcand.sum())

    # vista ------------------------------------------------------------------
    open_pt = lookup(g90, read_tif(rasters_dir / "open90.tif"), sx, sy)
    min3k = lookup(g90, read_tif(rasters_dir / "min3k90.tif"), sx, sy, np.nan)
    drop = z - min3k
    vc = np.flatnonzero((open_pt >= mc["vista_open"]) & (drop >= mc["vista_drop_m"]))
    sight = far_sight(sx[vc], sy[vc], z[vc] + cfg["water"]["eye_m"], g90, dem90, mc["vista_rays"],
                      mc["vista_far_m"], cfg["water"]["terrain_tol_m"], mc["vista_seen_below_m"])
    vp = vc[sight >= mc["vista_sight_m"]]
    vdrop = np.zeros(n_e)
    np.maximum.at(vdrop, se[vp], drop[vp])
    m_type[:, 1] = np.where(vdrop > 0, vista_value(vdrop, mc), 0.0)
    info.update(vista_points_open_drop=int(len(vc)), vista_points=int(len(vp)),
                vista_edges=int((vdrop > 0).sum()))
    del open_pt, min3k

    # gorge / falls and OSM viewpoints ----------------------------------------
    ft = pq.read_table(out_dir / "osm_features.parquet")
    kind = ft.column("kind").to_pylist()
    kind = np.asarray(kind, dtype=object)
    flon, flat = ft.column("lon").to_numpy(), ft.column("lat").to_numpy()
    fname = np.asarray(ft.column("name").to_pylist(), dtype=object)
    fwkb = ft.column("wkb").to_pylist()

    def pts_of(kinds):
        sel = np.flatnonzero(np.isin(kind, kinds) & np.isfinite(flon))
        x, y = project_lonlat(flon[sel], flat[sel])
        return x, y, sel

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

    bridge_ways = np.unique(ft.column("way_id").to_numpy()[kind == "bridge"])
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
    m_type[:, 2] = np.where(gorge | gb, mc["gorge_m"], 0.0)
    del dem30

    vfx, vfy, vsel = pts_of(["viewpoint"])
    hit = near_points(sx, sy, vfx, vfy, mc["osm_viewpoint_m"])
    vfeat = np.full(n_e, -1, np.int64)
    h = np.flatnonzero(hit >= 0)
    vfeat[se[h]] = vsel[hit[h]]
    m_type[:, 3] = np.where(vfeat >= 0, mc["osm_m"], 0.0)
    info["osm_viewpoint_edges"] = int((vfeat >= 0).sum())

    # cluster ----------------------------------------------------------------
    m_type[~ok_edge] = 0
    m_edge = m_type.max(axis=1)
    cand = np.flatnonzero(m_edge > 0)
    t_edge = m_type.argmax(axis=1)
    heads, owner = cluster(m_edge[cand], mx[cand], my[cand], key[cand], mc["cluster_road_m"],
                           mc["cluster_cross_m"])
    info["candidate_moment_edges"] = int(len(cand))

    # names --------------------------------------------------------------------
    hx, hy = mx[cand[heads]], my[cand[heads]]
    he = cand[heads]
    feature = np.full(len(heads), None, dtype=object)
    for j, e in enumerate(he):
        if t_edge[e] == 3 and vfeat[e] >= 0:
            feature[j] = fname[vfeat[e]] or "viewpoint"
        elif t_edge[e] == 2 and gfeat[e] >= 0:
            feature[j] = fname[gfeat[e]] or ("waterfall" if kind[gfeat[e]] == "waterfall" else "cliff")
    feature = _name_water(feature, hx, hy, kind, fname, fwkb, mc)
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
        members[pos[int(o)]].append(int(cand[i]))
    tbl = pa.table({
        "id": pa.array(np.arange(len(heads), dtype=np.int32)),
        "type": pa.array([TYPES[t] for t in t_edge[he]]),
        "m": pa.array(m_edge[he].astype(np.float32)),
        "lon": pa.array(lon), "lat": pa.array(lat),
        "x": pa.array(hx), "y": pa.array(hy),
        "edge_idx": pa.array(he.astype(np.int32)),
        "key": pa.array(key[he].astype(np.int64)),
        "road": pa.array(list(road), pa.string()),
        "feature": pa.array(list(feature), pa.string()),
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
    return tbl, info


def _unproject(x, y, crs: str = "EPSG:5070"):
    from pyproj import Transformer

    tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lon, lat = tr.transform(np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64))
    return np.asarray(lon), np.asarray(lat)


def _name_water(feature, hx, hy, kind, fname, fwkb, mc):
    """Fill unnamed moments with the nearest named lake (within
    water_name_m) or river (within river_name_m)."""
    import shapely

    from .water import project_wkb

    todo = np.flatnonzero(feature == None)  # noqa: E711
    if len(todo) == 0:
        return feature
    pts = shapely.points(hx[todo], hy[todo])
    for k, radius in (("water", mc["water_name_m"]), ("river", mc["river_name_m"])):
        idx = np.flatnonzero(kind == k)
        if len(idx) == 0 or len(todo) == 0:
            continue
        geoms = project_wkb([fwkb[i] for i in idx])
        tree = shapely.STRtree(geoms)
        (qi, gi), d = tree.query_nearest(pts, max_distance=radius, return_distance=True, all_matches=False)
        feature[todo[qi]] = fname[idx[gi]]
        left = np.setdiff1d(np.arange(len(todo)), qi)
        todo, pts = todo[left], pts[left]
    return feature


def load_moments(path: Path, fingerprint: str | None = None):
    """moments.parquet, checked against the edges fingerprint when given."""
    import pyarrow.parquet as pq

    from .graph import FINGERPRINT_KEY

    if fingerprint is not None:
        stored = (pq.read_schema(path).metadata or {}).get(FINGERPRINT_KEY)
        if stored is None or stored.decode() != fingerprint:
            raise ValueError(f"{path} was not computed for the current edges.parquet; rerun the moments stage")
    return pq.read_table(path)
