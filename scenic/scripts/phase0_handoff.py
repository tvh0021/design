#!/usr/bin/env python3
"""Phase 0: prove the Google Maps handoff for Rochester -> Syracuse via 5 & 20.

Standard library only. Steps:

1. Ask Overpass for the route ways (US 20, NY 5, NY 96, NY 332, NY 321) near
   each stretch we want to force, plus every road within 60 m of them.
2. In each stretch, pick one waypoint on the route road that is at least
   300 m from any junction with another named road, has no parallel road
   within 50 m, and is not on a one-way (divided) carriageway.
3. Build desktop Google Maps links for four variants (A-D) and a no-waypoint
   baseline.
4. Ask the public OSRM demo server for the fastest route and for the route
   through each variant's waypoints; report minutes, km, ratio to fastest,
   and the share of distance on 5&20 / NY-5 (from OSRM step names and refs).
5. Write docs/phase0.md.

Responses are cached in ~/.cache/scenic-phase0 (override with
SCENIC_PHASE0_CACHE), so reruns make no network calls. Use --refresh-overpass
or --refresh-osrm to fetch again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOC_PATH = HERE.parent / "docs" / "phase0.md"
CACHE = Path(os.environ.get("SCENIC_PHASE0_CACHE", Path.home() / ".cache" / "scenic-phase0"))

USER_AGENT = "scenic-phase0/0.1 (personal scenic route planner; phase 0 Google Maps handoff test)"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/"
OSRM_MIN_INTERVAL_S = 1.1  # demo server asks for at most 1 request per second
OSRM_MAX_REQUESTS = 14     # hard cap per run (cache hits do not count)

# Waypoint rules.
JUNCTION_CLEARANCE_M = 300.0   # min straight-line distance to a junction with another named road
PARALLEL_RADIUS_M = 50.0       # no other road running alongside within this distance
PARALLEL_ANGLE_DEG = 35.0      # "alongside" = bearing within this many degrees
SAMPLE_STEP_M = 20.0           # candidate spacing along the road
CLEARANCE_CAP_M = 800.0        # beyond this, extra clearance stops mattering; closeness to aim wins

# Plan budget (docs/PLAN.md, section 2).
RATIO_TARGET = 1.3
RATIO_CAP = 1.5

ORIGIN_PLACE = "Rochester"
DEST_PLACE = "Syracuse"

DRIVABLE = {
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
    "residential", "living_street", "service", "road",
    "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link",
}
MAJOR = {
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link",
}

# Each stretch: one waypoint on `refs` between places a and b. `aim` is where
# along a->b (0..1) we would like it; tmin/tmax limit the search window;
# `clearance` overrides JUNCTION_CLEARANCE_M. The comments record what the
# first OSRM run showed.
STRETCHES = {
    "S1": dict(refs=("NY 96",), a="Pittsford", b="Victor", aim=0.15,
               label="NY-96 East Jefferson Rd, east of Pittsford"),
    # Without this point OSRM leaves NY-96 at Bushnell's Basin and takes I-490
    # (exit 27 to exit 29).
    "S2": dict(refs=("NY 96",), a="Pittsford", b="Victor", aim=0.42, tmin=0.3, tmax=0.55,
               label="NY-96, Bushnell's Basin to Eastview"),
    # NY-332 (Farmington to Canandaigua) is mapped as a divided road end to end,
    # so it fails the no-parallel-road rule. A NY-96 point past the last I-490
    # exit near Victor does the job: OSRM then takes NY-96 and NY-332 on its own.
    "S3": dict(refs=("NY 96",), a="Pittsford", b="Victor", aim=0.85,
               label="NY-96, south of Eastview toward Victor"),
    "S4": dict(refs=("US 20", "NY 5"), a="Canandaigua", b="Geneva", aim=0.35,
               label="5&20, Canandaigua to Geneva"),
    # On the Seneca Lake shore; further east OSRM cuts through Border City Road
    # and skips the Geneva lakefront.
    "S5": dict(refs=("US 20", "NY 5"), a="Geneva", b="Waterloo", aim=0.12, tmin=0.03, tmax=0.3,
               label="5&20, Geneva lakefront (Seneca Lake)"),
    "S6": dict(refs=("US 20", "NY 5"), a="Waterloo", b="Seneca Falls", aim=0.5,
               label="5&20, Waterloo to Seneca Falls"),
    "S7": dict(refs=("US 20", "NY 5"), a="Seneca Falls", b="Cayuga", aim=0.65,
               label="5&20, east of Seneca Falls toward the Cayuga Lake crossing"),
    "S8": dict(refs=("US 20",), a="Auburn", b="Skaneateles", aim=0.5,
               label="US-20, Auburn to Skaneateles"),
    # As close to Skaneateles as the rules allow (~2.4 km north). Alone it is
    # not enough: OSRM reaches it from Auburn by Franklin St, Old Seneca Tpk and
    # Jewett Rd and skips the village. Pair it with S8 on US-20.
    "S9": dict(refs=("NY 321",), a="Skaneateles", b="Camillus", aim=0.1, tmin=0.03, tmax=0.4,
               label="NY-321, just north of Skaneateles"),
    # Rural NY-5 east of the NY-321 junction (t window keeps it east of it).
    "S10": dict(refs=("NY 5",), a="Elbridge", b="Camillus", aim=0.72, tmin=0.62, tmax=0.92,
                label="NY-5 West Genesee Tpk, west of Camillus"),
    # Without a point here OSRM leaves NY-5 for NY-695 and I-690 for the last
    # ~7 km. West Genesee Street has a cross street every block: the best spot
    # is ~90 m from one, far below the 300 m rule. So it is in no variant; the
    # doc lists it as an optional manual add.
    "S11": dict(refs=("NY 5",), a="Fairmount", b="Syracuse", aim=0.45, tmin=0.2, tmax=0.75,
                clearance=80.0, optional=True, label="NY-5 West Genesee St, west side of Syracuse"),
}

ALL_REFS = {r for s in STRETCHES.values() for r in s["refs"]}
CORRIDOR = ("US 20", "NY 5")
BACK_ROADS = ("NY 96", "NY 332", "US 20", "NY 5", "NY 321")
VARIANTS = {
    "A": dict(title="Back roads, 9 waypoints",
              stops=["S1", "S2", "S3", "S4", "S5", "S7", "S8", "S9", "S10"],
              planned=BACK_ROADS),
    "B": dict(title="Back roads, 5 waypoints",
              stops=["S3", "S4", "S8", "S9", "S10"],
              planned=BACK_ROADS),
    "C": dict(title="Thruway to Geneva, then 5&20",
              stops=["S5", "S8", "S9", "S10"],
              planned=("I 490", "I 90", "NY 14", "US 20", "NY 5", "NY 321")),
    "D": dict(title="Limit test: A plus a 10th waypoint",
              stops=["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10"],
              planned=BACK_ROADS),
}
# Refs fetched statewide-ish for the "share on road" check (OSM node match).
SHARE_REFS = sorted(set(CORRIDOR) | {r for v in VARIANTS.values() for r in v["planned"]})


# ---------------------------------------------------------------- network ---

def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _request(url: str, data: bytes | None = None, timeout: float = 240) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def overpass(name: str, query: str, refresh: bool, check=None) -> dict:
    path = CACHE / f"overpass_{name}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    body = urllib.parse.urlencode({"data": query}).encode()
    for attempt in range(4):
        try:
            log(f"overpass: {name} (attempt {attempt + 1})")
            raw = _request(OVERPASS_URL, body)
            data = json.loads(raw)
            if data.get("remark"):
                raise RuntimeError(f"overpass remark: {data['remark']}")
            if check:
                check(data)
            path.write_bytes(raw)
            return data
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, RuntimeError) as e:
            log(f"overpass: {name} failed: {e}")
            if attempt == 3:
                raise
            time.sleep(30 * (attempt + 1))
    raise AssertionError("unreachable")


class Osrm:
    def __init__(self, refresh: bool):
        self.refresh = refresh
        self.last = 0.0
        self.sent = 0

    def route(self, points: list[tuple[float, float]]) -> dict:
        coords = ";".join(f"{lon:.5f},{lat:.5f}" for lat, lon in points)
        url = f"{OSRM_URL}{coords}?overview=false&steps=true&alternatives=false&annotations=nodes,distance"
        path = CACHE / f"osrm_{hashlib.sha1(url.encode()).hexdigest()[:16]}.json"
        if path.exists() and not self.refresh:
            return json.loads(path.read_text())
        if self.sent >= OSRM_MAX_REQUESTS:
            raise RuntimeError("OSRM request cap reached for this run")
        wait = OSRM_MIN_INTERVAL_S - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        self.sent += 1
        log(f"osrm: request {self.sent} ({len(points)} points)")
        data = json.loads(_request(url, timeout=60))
        if data.get("code") != "Ok":
            raise RuntimeError(f"OSRM error: {data.get('code')} {data.get('message')}")
        path.write_text(json.dumps(data))
        return data


# --------------------------------------------------------------- geometry ---

M_PER_DEG_LAT = 111_320.0
LAT0 = 43.0
M_PER_DEG_LON = M_PER_DEG_LAT * math.cos(math.radians(LAT0))


def xy(lat: float, lon: float) -> tuple[float, float]:
    return lon * M_PER_DEG_LON, lat * M_PER_DEG_LAT


def latlon(x: float, y: float) -> tuple[float, float]:
    return y / M_PER_DEG_LAT, x / M_PER_DEG_LON


def dist(p, q) -> float:
    return math.hypot(p[0] - q[0], p[1] - q[1])


def seg_dist(p, a, b) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def bearing(a, b) -> float:
    return math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 180.0  # undirected


def angle_diff(b1: float, b2: float) -> float:
    d = abs(b1 - b2) % 180.0
    return min(d, 180.0 - d)


class SegmentGrid:
    """Grid index of road segments for radius queries."""

    def __init__(self, cell: float = 100.0):
        self.cell = cell
        self.cells: dict[tuple[int, int], list] = {}

    def add(self, a, b, way: dict) -> None:
        c = self.cell
        x0, x1 = sorted((a[0], b[0]))
        y0, y1 = sorted((a[1], b[1]))
        for i in range(int(x0 // c), int(x1 // c) + 1):
            for j in range(int(y0 // c), int(y1 // c) + 1):
                self.cells.setdefault((i, j), []).append((a, b, way))

    def near(self, p, r: float):
        c = self.cell
        seen = set()
        for i in range(int((p[0] - r) // c), int((p[0] + r) // c) + 1):
            for j in range(int((p[1] - r) // c), int((p[1] + r) // c) + 1):
                for item in self.cells.get((i, j), ()):
                    key = id(item)
                    if key not in seen:
                        seen.add(key)
                        yield item


# ---------------------------------------------------------------- OSM data ---

def ref_tokens(tags: dict) -> set[str]:
    return {t.strip() for t in tags.get("ref", "").split(";") if t.strip()}


def is_oneway(tags: dict) -> bool:
    return tags.get("oneway") in ("yes", "1", "-1", "true") or tags.get("junction") == "roundabout"


def is_named_road(tags: dict) -> bool:
    hw = tags.get("highway")
    return hw in DRIVABLE and (hw in MAJOR or "name" in tags or "ref" in tags)


def fetch_places(refresh: bool) -> dict[str, tuple[float, float]]:
    q = """[out:json][timeout:60];
node["place"~"^(city|town|village|hamlet)$"](42.78,-77.70,43.20,-76.08);
out;"""
    data = overpass("places", q, refresh)
    rank = {"city": 0, "town": 1, "village": 2, "hamlet": 3}
    best: dict[str, tuple[int, float, float]] = {}
    for e in data["elements"]:
        name = e.get("tags", {}).get("name")
        r = rank.get(e.get("tags", {}).get("place"), 9)
        if name and (name not in best or r < best[name][0]):
            best[name] = (r, e["lat"], e["lon"])
    return {k: (v[1], v[2]) for k, v in best.items()}


def stretch_bbox(places, s: dict, pad: float = 0.01) -> tuple[float, float, float, float]:
    (la, oa), (lb, ob) = places[s["a"]], places[s["b"]]
    return (min(la, lb) - pad, min(oa, ob) - pad, max(la, lb) + pad, max(oa, ob) + pad)


def fetch_corridor(places, refresh: bool) -> dict:
    parts = []
    names = []
    for i, (key, s) in enumerate(STRETCHES.items()):
        s_, w, n, e = stretch_bbox(places, s)
        alt = "|".join(re.escape(r) for r in s["refs"])
        parts.append(
            f'way["highway"]["ref"~"(^|;) *({alt}) *(;|$)"]({s_:.4f},{w:.4f},{n:.4f},{e:.4f})->.r{i};'
        )
        names.append(f".r{i};")
    q = (
        "[out:json][timeout:180];\n"
        + "\n".join(parts)
        + "\n(" + " ".join(names) + ")->.route;\n"
        + "way(around.route:60)[\"highway\"]->.near;\n"
        + ".route out body geom;\n"
        + ".near out body geom;"
    )
    # Cache key follows the query, so editing STRETCHES fetches fresh data.
    def check(data: dict) -> None:
        # Seen on overpass-api.de: the around() set sometimes comes back empty
        # with no error or remark. Without nearby roads every point looks clear.
        n_route = sum(1 for e in data["elements"] if ref_tokens(e.get("tags", {})) & ALL_REFS)
        n_other = len(data["elements"]) - n_route
        if n_other < n_route:
            raise RuntimeError(f"only {n_other} nearby roads for {n_route} route ways; around() failed")

    return overpass(f"corridor_{hashlib.sha1(q.encode()).hexdigest()[:10]}", q, refresh, check)


# ---------------------------------------------------------- waypoint pick ---

def pick_waypoints(places, osm: dict) -> dict[str, dict]:
    ways = {}
    for e in osm["elements"]:
        if e.get("type") == "way" and "geometry" in e:
            ways[e["id"]] = e
    node_ways: dict[int, list[int]] = {}
    grid = SegmentGrid()
    for w in ways.values():
        tags = w.get("tags", {})
        for n in w["nodes"]:
            node_ways.setdefault(n, []).append(w["id"])
        if tags.get("highway") not in DRIVABLE:
            continue
        pts = [xy(g["lat"], g["lon"]) for g in w["geometry"]]
        for a, b in zip(pts, pts[1:]):
            grid.add(a, b, w)

    chosen = {}
    for key, s in STRETCHES.items():
        refs = set(s["refs"])
        A = xy(*places[s["a"]])
        B = xy(*places[s["b"]])
        AB = (B[0] - A[0], B[1] - A[1])
        L = math.hypot(*AB)
        ux, uy = AB[0] / L, AB[1] / L
        max_lateral = max(1500.0, 0.35 * L)

        route_ways = [w for w in ways.values()
                      if w.get("tags", {}).get("highway") in DRIVABLE and ref_tokens(w["tags"]) & refs]
        route_ids = {w["id"] for w in route_ways}

        # Junctions: route-way nodes shared with a named road that does not carry these refs.
        junctions = []
        for w in route_ways:
            for n, g in zip(w["nodes"], w["geometry"]):
                for other in node_ways.get(n, ()):
                    if other in route_ids:
                        continue
                    ot = ways[other].get("tags", {})
                    if is_named_road(ot):
                        junctions.append(xy(g["lat"], g["lon"]))
                        break
        jgrid: dict[tuple[int, int], list] = {}
        for j in junctions:
            jgrid.setdefault((int(j[0] // 500), int(j[1] // 500)), []).append(j)

        def junction_dist(p) -> float:
            best = float("inf")
            ci, cj = int(p[0] // 500), int(p[1] // 500)
            for di in range(-2, 3):
                for dj in range(-2, 3):
                    for j in jgrid.get((ci + di, cj + dj), ()):
                        best = min(best, dist(p, j))
            return min(best, 1000.0)  # search radius is ~1 km; report farther as 1000

        candidates = []
        rejects = {"oneway/motorway": 0, "junction": 0, "parallel/crossing": 0}
        for w in route_ways:
            tags = w["tags"]
            pts = [xy(g["lat"], g["lon"]) for g in w["geometry"]]
            for a, b in zip(pts, pts[1:]):
                seglen = dist(a, b)
                nsteps = max(1, int(seglen // SAMPLE_STEP_M))
                brg = bearing(a, b)
                for k in range(nsteps):
                    f = (k + 0.5) / nsteps
                    p = (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]))
                    rel = (p[0] - A[0], p[1] - A[1])
                    t = (rel[0] * ux + rel[1] * uy) / L
                    lateral = abs(rel[0] * uy - rel[1] * ux)
                    if not (s.get("tmin", 0.1) <= t <= s.get("tmax", 0.9)) or lateral > max_lateral:
                        continue
                    if is_oneway(tags) or tags.get("highway", "").startswith("motorway") or \
                            tags.get("highway", "").endswith("_link"):
                        rejects["oneway/motorway"] += 1
                        continue
                    jd = junction_dist(p)
                    if jd < s.get("clearance", JUNCTION_CLEARANCE_M):
                        rejects["junction"] += 1
                        continue
                    blocked = None
                    nearest_other = float("inf")
                    for sa, sb, ow in grid.near(p, PARALLEL_RADIUS_M):
                        if ow["id"] in route_ids:
                            continue
                        d = seg_dist(p, sa, sb)
                        if d > PARALLEL_RADIUS_M:
                            continue
                        ot = ow.get("tags", {})
                        nearest_other = min(nearest_other, d)
                        if is_named_road(ot):
                            blocked = f"named road {ot.get('name') or ot.get('ref')} at {d:.0f} m"
                            break
                        if angle_diff(brg, bearing(sa, sb)) < PARALLEL_ANGLE_DEG:
                            blocked = f"parallel {ot.get('highway')} at {d:.0f} m"
                            break
                    if blocked:
                        rejects["parallel/crossing"] += 1
                        continue
                    score = min(jd, CLEARANCE_CAP_M) - 0.5 * abs(t - s["aim"]) * L
                    candidates.append((score, p, jd, t, w))
        if not candidates:
            raise SystemExit(f"{key}: no valid waypoint ({rejects}); loosen rules or move the stretch")
        score, p, jd, t, w = max(candidates, key=lambda c: c[0])
        lat, lon = latlon(*p)
        chosen[key] = dict(
            lat=round(lat, 5), lon=round(lon, 5), clearance_m=round(jd),
            t=round(t, 2), way_id=w["id"], name=w["tags"].get("name", ""),
            ref=w["tags"].get("ref", ""), n_candidates=len(candidates), rejects=rejects,
        )
        log(f"{key}: {lat:.5f},{lon:.5f} on {w['tags'].get('name')} [{w['tags'].get('ref')}] "
            f"clearance {jd:.0f} m, t={t:.2f}, {len(candidates)} valid samples")
    return chosen


# -------------------------------------------------------------- links/ETA ---

def fmt(p) -> str:
    return f"{p[0]:.5f},{p[1]:.5f}"


def maps_url(origin, dest, waypoints) -> str:
    q = f"https://www.google.com/maps/dir/?api=1&origin={fmt(origin)}&destination={fmt(dest)}&travelmode=driving"
    if waypoints:
        q += "&waypoints=" + "%7C".join(fmt(w) for w in waypoints)
    return q


CORRIDOR_NAME_RE = re.compile(r"\b(US|NY|Route|Rte|State Route)[ -]?(20|5)\b(?![A-Za-z])", re.I)


def step_refs(step: dict) -> set[str]:
    return {t.strip() for t in (step.get("ref") or "").split(";") if t.strip()}


def on_refs(step: dict, refs) -> bool:
    toks = step_refs(step)
    if toks & set(refs):
        return True
    # Some steps carry the route only in the name ("State Route 5").
    return bool(set(refs) & set(CORRIDOR)) and bool(CORRIDOR_NAME_RE.search(step.get("name") or ""))


def fetch_route_edges(refresh: bool) -> dict[frozenset, set[str]]:
    """OSM node pair -> refs, for every way carrying one of SHARE_REFS."""
    alt = "|".join(re.escape(r) for r in SHARE_REFS)
    q = f"""[out:json][timeout:180];
way["highway"]["ref"~"(^|;) *({alt}) *(;|$)"](42.78,-77.70,43.20,-76.08);
out body;"""
    data = overpass(f"routes_{hashlib.sha1(q.encode()).hexdigest()[:10]}", q, refresh)
    edges: dict[frozenset, set[str]] = {}
    for e in data["elements"]:
        refs = ref_tokens(e.get("tags", {}))
        for a, b in zip(e["nodes"], e["nodes"][1:]):
            edges.setdefault(frozenset((a, b)), set()).update(refs)
    return edges


def summarize(route: dict, planned, edges) -> dict:
    """Shares by OSM node match (OSRM annotations); step names as a cross-check.

    OSRM's steps leave some roads unnamed (the NY-5 Camillus expressway, for
    one), so the step-based share undercounts. The node match does not.
    """
    total = corr = plan = 0.0
    for leg in route["legs"]:
        ann = leg["annotation"]
        for (a, b), d in zip(zip(ann["nodes"], ann["nodes"][1:]), ann["distance"]):
            refs = edges.get(frozenset((a, b)), set())
            total += d
            if refs & set(CORRIDOR):
                corr += d
            if refs & set(planned):
                plan += d
    step_corr = 0.0
    off: dict[str, float] = {}
    for leg in route["legs"]:
        for st in leg["steps"]:
            if on_refs(st, CORRIDOR):
                step_corr += st["distance"]
            if not on_refs(st, planned) and (st.get("ref") or st.get("name")):
                label = (st.get("ref") or st.get("name")).strip()
                off[label] = off.get(label, 0.0) + st["distance"]
    top_off = [(k, v / 1000) for k, v in sorted(off.items(), key=lambda kv: -kv[1]) if v > 500][:5]
    total = total or route["distance"]
    return dict(minutes=route["duration"] / 60, km=route["distance"] / 1000,
                corridor_share=corr / total, planned_share=plan / total,
                step_corridor_share=step_corr / route["distance"], top_off=top_off)


# -------------------------------------------------------------------- doc ---

def write_doc(origin, dest, wps, base, base_url, results) -> None:
    lines: list[str] = []
    w = lines.append
    w("# Phase 0: Google Maps handoff test")
    w("")
    w("Rochester to Syracuse on 5&20, past the north ends of the Finger Lakes.")
    w("`scripts/phase0_handoff.py` writes this file. Rerunning it overwrites the checklist, "
      "so copy your answers somewhere first.")
    w("")
    w(f"- Origin: downtown Rochester (OSM place node), {fmt(origin)}.")
    w(f"- Destination: downtown Syracuse (OSM place node), {fmt(dest)}.")
    w(f"- Budget from the plan: {RATIO_TARGET}x the fastest time is the target, {RATIO_CAP}x the cap.")
    w("")
    w("## Results from the OSRM demo server")
    w("")
    w("| Variant | Waypoints | OSRM min | Ratio to fastest | km | Share on 5&20 / NY-5 | Share on planned roads |")
    w("|---|---|---|---|---|---|---|")
    w(f"| Fastest (no waypoints) | 0 | {base['minutes']:.0f} | 1.00 | {base['km']:.0f} | "
      f"{base['corridor_share']:.0%} | n/a |")
    for v, r in results.items():
        ratio = r["minutes"] / base["minutes"]
        flag = " **over cap**" if ratio > RATIO_CAP else (" (over target)" if ratio > RATIO_TARGET else "")
        w(f"| {v}: {VARIANTS[v]['title']} | {len(VARIANTS[v]['stops'])} | {r['minutes']:.0f} | "
          f"{ratio:.2f}{flag} | {r['km']:.0f} | {r['corridor_share']:.0%} | {r['planned_share']:.0%} |")
    w("")
    w("How the shares are measured: each stretch of the OSRM route is matched to OSM ways by node ID. "
      "\"5&20 / NY-5\" means the way's ref includes US 20 or NY 5. \"Planned roads\" adds the "
      "connectors the variant means to use: NY-96, NY-332 and NY-321 for A, B and D; I-490, I-90, "
      "NY-14 and NY-321 for C. "
      "Counting from OSRM's step names instead gives lower 5&20 / NY-5 shares ("
      + ", ".join(f"{v} {r['step_corridor_share']:.0%}" for v, r in results.items())
      + "), because OSRM leaves the NY-5 expressway at Camillus unnamed.")
    w("")
    w("OSRM stands in for Google here. It does not prove what Google will draw.")
    w("")
    w("## Links")
    w("")
    w(f"- Fastest, for Google's baseline ETA: [open in Google Maps]({base_url})")
    for v, r in results.items():
        w(f"- {v}, {VARIANTS[v]['title']}: [open in Google Maps]({r['url']}) ({len(r['url'])} characters)")
    w("")
    w("## Checklist")
    w("")
    w("Open each link in desktop Google Maps. Write down what you see.")
    w("")
    w("| Link | Google accepted all waypoints? | Route follows 5&20? | Google's ETA | Notes |")
    w("|---|---|---|---|---|")
    w("| Fastest | n/a | n/a | | |")
    for v in results:
        w(f"| {v} ({len(VARIANTS[v]['stops'])} waypoints) | | | | |")
    w("")
    w("For D, also note which stop Google drops or what error it shows, if any.")
    w("")
    w("## Waypoints")
    w("")
    w("| Key | Stretch | Point | Road in OSM | Nearest junction | In variants |")
    w("|---|---|---|---|---|---|")
    for k, st in STRETCHES.items():
        p = wps[k]
        road = f"{p['name']} ({p['ref']})" if p["name"] else p["ref"]
        inv = ", ".join(v for v, spec in VARIANTS.items() if k in spec["stops"]) or "none (optional)"
        near = f"{p['clearance_m']} m" if p["clearance_m"] < 1000 else "1 km or more"
        w(f"| {k} | {st['label']} | {p['lat']:.5f},{p['lon']:.5f} | {road} | {near} | {inv} |")
    w("")
    w("Rules for each point:")
    w("")
    w(f"- On the target road, at least {JUNCTION_CLEARANCE_M:.0f} m in a straight line from any "
      "junction with another named road.")
    w(f"- No road runs alongside within {PARALLEL_RADIUS_M:.0f} m, and no named road passes within "
      f"{PARALLEL_RADIUS_M:.0f} m (bridges, overpasses).")
    w("- Not on a one-way carriageway or a motorway, so Google cannot snap to the wrong direction.")
    w("- Road geometry comes from OpenStreetMap through the Overpass API.")
    w("")
    w("## Where OSRM leaves the planned roads")
    w("")
    w("Named roads off the plan, by OSRM step name, longest first. Unnamed ramps are left out.")
    w("")
    for v, r in results.items():
        off = "; ".join(f"{k} {km:.1f} km" for k, km in r["top_off"]) or "nothing over 0.5 km"
        w(f"- {v}: {off}.")
    w("")
    w("## Notes")
    w("")
    for note in NOTES:
        w(f"- {note}")
    w("")
    DOC_PATH.write_text("\n".join(lines))
    log(f"wrote {DOC_PATH}")


# Observations from building the waypoints. They come from the OSM data and
# the first OSRM run, not from this run's numbers.
NOTES = [
    "Every variant leaves downtown Rochester on I-490. A leaves it after about 5 km (then I-590 "
    "and Monroe Avenue to Pittsford). B stays on it to Victor. C stays on it to the Thruway, as "
    "intended. No spot on East Avenue (NY-96 in the city) meets the 300 m rule.",
    "NY-332 (Farmington to Canandaigua) is mapped as a divided road for its whole length, so no "
    "waypoint goes there. A NY-96 point near Victor (S3) is enough: OSRM then takes NY-96 and "
    "NY-332 into Canandaigua on its own.",
    "A keeps to NY-96 with two points (S1, S2). Without S2, OSRM jumps onto I-490 from Bushnell's "
    "Basin to Eastview. Even with it, OSRM uses 1.5 km of I-490 between S1 and S2; the stretch has "
    "only one spot that meets the rules. B has no NY-96 point before Victor, so it runs on I-490 "
    "from Rochester to Victor.",
    "The Geneva point (S5) sits on the Seneca Lake shore. A point further east lets OSRM cut "
    "through Border City Road and skip the lakefront.",
    "Skaneateles needs two points: US-20 between Auburn and Skaneateles (S8) and NY-321 north of "
    "the village (S9). With S9 alone, OSRM goes from Auburn by Franklin Street, Old Seneca "
    "Turnpike and Jewett Road and skips Skaneateles. NY-321 has no valid spot closer to the "
    "village.",
    "Into Syracuse, OSRM takes the NY-5 expressway, then NY-695 and I-690 for the last ~7 km "
    "instead of West Genesee Street. A point on West Genesee Street (S11) would stop that, but "
    "its best spot is ~90 m from a cross street. Add it by hand if Google also takes I-690.",
    "\"Genesee Street\" in the lists above is 5&20 through Auburn. OSRM's steps carry no ref "
    "there, and only part of it matches by node, so the 5&20 shares run a little low.",
    "D drives the same roads as A in OSRM; its extra point (S6) changes nothing there. It only "
    "tests whether desktop Google Maps takes a 10th waypoint (the documented limit is 9).",
]


# -------------------------------------------------------------------- main ---

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--refresh-overpass", action="store_true")
    ap.add_argument("--refresh-osrm", action="store_true")
    ap.add_argument("--no-osrm", action="store_true", help="pick waypoints and print links only")
    args = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)

    places = fetch_places(args.refresh_overpass)
    origin, dest = places[ORIGIN_PLACE], places[DEST_PLACE]
    osm = fetch_corridor(places, args.refresh_overpass)
    wps = pick_waypoints(places, osm)

    base_url = maps_url(origin, dest, [])
    urls = {}
    for v, spec in VARIANTS.items():
        pts = [(wps[k]["lat"], wps[k]["lon"]) for k in spec["stops"]]
        urls[v] = maps_url(origin, dest, pts)
        assert len(urls[v]) < 2048, f"{v}: URL too long ({len(urls[v])})"
        print(f"{v}: {urls[v]}")
    if args.no_osrm:
        return

    osrm = Osrm(args.refresh_osrm)
    edges = fetch_route_edges(args.refresh_overpass)
    base = summarize(osrm.route([origin, dest])["routes"][0], CORRIDOR, edges)
    results = {}
    for v, spec in VARIANTS.items():
        pts = [origin] + [(wps[k]["lat"], wps[k]["lon"]) for k in spec["stops"]] + [dest]
        r = summarize(osrm.route(pts)["routes"][0], spec["planned"], edges)
        r["url"] = urls[v]
        results[v] = r
        ratio = r["minutes"] / base["minutes"]
        status = "OVER CAP" if ratio > RATIO_CAP else ("over target" if ratio > RATIO_TARGET else "ok")
        print(f"{v}: {r['minutes']:.0f} min ({ratio:.2f}x, {status}), {r['km']:.0f} km, "
              f"5&20/NY-5 {r['corridor_share']:.0%}, planned {r['planned_share']:.0%}; off: {r['top_off']}")
    print(f"baseline: {base['minutes']:.0f} min, {base['km']:.0f} km")
    write_doc(origin, dest, wps, base, base_url, results)


if __name__ == "__main__":
    main()
