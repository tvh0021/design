"""Static demo site for Maps for Wanderers (docs/PUBLISH.md).

`scenic demo build` runs the real search for each preset (and optional `[loop]`
origin) in config/demo_routes.toml, places Google Maps waypoints for every
route it picks, and writes a self-contained static site:

    site/index.html              the page from static/, in read-only demo mode
    site/static/...              app.js, app.css and the self-hosted Leaflet
    site/data/presets.json       the preset list and Drive-around loop index
    site/data/<id>.json          one A-to-B preset: /api/route + /api/waypoints
    site/data/loop-*.json        one Drive-around search: /api/loop + /api/waypoints

The page then makes no API calls: it reads data/*.json by relative path, so
the site works from any folder (for example /design/wanderers/ on GitHub
Pages). The only outside requests are the visitors' map tiles.
"""

from __future__ import annotations

import json
import re
import shutil
import time
import tomllib
from pathlib import Path
from typing import Callable

import numpy as np

from .server import CSP_BASE, STATIC_DIR, App

PROJECT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_PRESETS = PROJECT_DIR / "config" / "demo_routes.toml"
DEFAULT_OUT = PROJECT_DIR / "site"
SOURCE_URL = "https://github.com/tvh0021/design/tree/main/scenic"
DEMO_MAX_POINTS = 1500  # per route; ~20 m tolerance on a 200 km drive, fine up to zoom 13 or so
MARKER = ".maps-for-wanderers-demo"  # marks a folder this builder owns and may clear
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
LOOP_DIRS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
COPY = ["app.js", "app.css", "vendor"]  # from static/; index.html is rewritten


class DemoError(Exception):
    pass


class _NoGeocoder:
    """The demo builder never geocodes."""

    def search(self, q):  # pragma: no cover - never called
        raise DemoError("the demo builder does not geocode")


def _point(v, where: str) -> list[float]:
    try:
        lat, lon = (float(x) for x in v)
    except (TypeError, ValueError):
        raise DemoError(f"{where} must be [lat, lon], got {v!r}")
    return [lat, lon]


def _slug(name: str, seen: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:28].rstrip("-") or "feature"
    slug, k = base, 2
    while slug in seen:
        slug = f"{base[:24].rstrip('-')}-{k}"
        k += 1
    seen.add(slug)
    return slug


def load_presets(path: Path | None = None) -> list[dict]:
    with open(path or DEFAULT_PRESETS, "rb") as f:
        raw = tomllib.load(f).get("preset", [])
    out, seen = [], set()
    for i, p in enumerate(raw):
        pid = p.get("id")
        if not isinstance(pid, str) or not ID_RE.match(pid):
            raise DemoError(f"preset {i}: id must match {ID_RE.pattern}, got {pid!r}")
        if pid in seen:
            raise DemoError(f"preset id {pid!r} is used twice")
        seen.add(pid)
        if not isinstance(p.get("name"), str) or not p["name"].strip():
            raise DemoError(f"preset {pid}: needs a name")
        out.append({"id": pid, "name": p["name"].strip(),
                    "from": _point(p.get("from"), f"preset {pid}: from"),
                    "to": _point(p.get("to"), f"preset {pid}: to"),
                    "from_label": str(p.get("from_label", "")), "to_label": str(p.get("to_label", ""))})
    if not out:
        raise DemoError("no [[preset]] entries")
    return out


def load_loop_preset(path: Path | None = None) -> dict | None:
    with open(path or DEFAULT_PRESETS, "rb") as f:
        raw = tomllib.load(f).get("loop")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise DemoError("[loop] must be a table")
    lid = raw.get("id")
    if not isinstance(lid, str) or not ID_RE.match(lid):
        raise DemoError(f"loop: id must match {ID_RE.pattern}, got {lid!r}")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise DemoError("loop: needs a name")
    start = _point(raw.get("start"), "loop: start")
    budgets_raw = raw.get("budgets", [60, 120, 180])
    if not isinstance(budgets_raw, list) or not budgets_raw or not all(
        isinstance(b, int) and not isinstance(b, bool) and 30 <= b <= 360 for b in budgets_raw
    ):
        raise DemoError(f"loop: budgets must be integers in 30..360, got {budgets_raw!r}")
    budgets = list(dict.fromkeys(budgets_raw))
    default_min = raw.get("default_min", budgets[0])
    if default_min not in budgets:
        raise DemoError(f"loop: default_min {default_min!r} must be in budgets {budgets!r}")
    dirs_raw = raw.get("directions", list(LOOP_DIRS))
    if not isinstance(dirs_raw, list) or not all(isinstance(d, str) and d in LOOP_DIRS for d in dirs_raw):
        raise DemoError(f"loop: directions must be a subset of {LOOP_DIRS}, got {dirs_raw!r}")
    directions = list(dict.fromkeys(dirs_raw))
    max_features = raw.get("max_features", 8)
    if not isinstance(max_features, int) or isinstance(max_features, bool) or not (0 <= max_features <= 16):
        raise DemoError(f"loop: max_features must be 0..16, got {max_features!r}")
    return {
        "id": lid,
        "name": name.strip(),
        "start": start,
        "budgets": budgets,
        "default_min": default_min,
        "directions": directions,
        "max_features": max_features,
    }


def _waypoints_for_search(app: App, search: dict, wp_cache: dict[tuple[bytes, float], dict] | None = None) -> dict:
    stored = app.results.get(search["id"])
    waypoints = {}
    for r in search["routes"]:
        cache_key = None
        if wp_cache is not None and stored is not None:
            sr = stored["routes"].get(r["key"])
            if sr is not None:
                cache_key = (np.asarray(sr.edges, dtype=np.int64).tobytes(), round(float(stored["res"].s_fast), 6))
                if cache_key in wp_cache:
                    cached = wp_cache[cache_key]
                    waypoints[r["key"]] = {**cached, "key": r["key"]} if "error" not in cached else dict(cached)
                    continue
        st, wp = app.api("POST", "/api/waypoints", {"id": search["id"], "key": r["key"]})
        ans = wp if st == 200 else {"error": wp.get("error", f"HTTP {st}")}
        if cache_key is not None:
            wp_cache[cache_key] = ans
        waypoints[r["key"]] = ans
    return waypoints


def _trim_search(search: dict, item_id: str) -> None:
    search.pop("seconds", None)
    for r in search["routes"]:
        r.pop("points_raw", None)
    search["id"] = item_id


def build_preset(app: App, preset: dict, wp_cache: dict[tuple[bytes, float], dict] | None = None) -> dict:
    """The page's data for one preset: the route payload and, per route
    key, the waypoint answer (or {"error": ...})."""
    st, search = app.api("POST", "/api/route", {"from": preset["from"], "to": preset["to"]})
    if st != 200:
        raise DemoError(f"preset {preset['id']}: search failed ({st}): {search.get('error')}")
    waypoints = _waypoints_for_search(app, search, wp_cache)
    _trim_search(search, preset["id"])
    return {"preset": preset, "search": search, "waypoints": waypoints}


def build_loop_demos(
    app: App,
    loop_spec: dict,
    wp_cache: dict[tuple[bytes, float], dict] | None = None,
    log: Callable[[str], None] = print,
) -> tuple[dict, list[tuple[str, dict]]]:
    """Precompute Drive-around recommendations (`any`, compass directions, and
    fitting scenic features) across every configured duration budget."""
    start = loop_spec["start"]
    max_feat = loop_spec["max_features"]
    files: list[tuple[str, dict]] = []
    by_min: dict[str, dict] = {}

    def _pack(fid: str, meta_item: dict, search: dict) -> str:
        waypoints = _waypoints_for_search(app, search, wp_cache)
        _trim_search(search, fid)
        rel = f"data/{fid}.json"
        files.append((f"{fid}.json", {"loop": meta_item, "search": search, "waypoints": waypoints}))
        return rel

    for m in loop_spec["budgets"]:
        t0 = time.monotonic()
        n_before = len(files)
        st, any_s = app.api("POST", "/api/loop", {"start": start, "minutes": m, "steer": "any"})
        if st != 200:
            raise DemoError(f"loop {loop_spec['id']} ({m} min any): search failed ({st}): {any_s.get('error')}")
        any_rel = _pack(f"loop-{m}-any", {"minutes": m, "steer": "any"}, any_s)

        compass_map: dict[str, str] = {}
        for d in loop_spec["directions"]:
            st_c, comp_s = app.api("POST", "/api/loop", {"start": start, "minutes": m, "steer": "compass", "direction": d})
            if st_c == 422:
                continue
            if st_c != 200:
                raise DemoError(f"loop {loop_spec['id']} ({m} min {d}): search failed ({st_c}): {comp_s.get('error')}")
            compass_map[d] = _pack(f"loop-{m}-dir-{d.lower()}", {"minutes": m, "steer": "compass", "direction": d}, comp_s)

        feat_entries: list[dict] = []
        if max_feat > 0:
            st_f, feat_resp = app.api(
                "POST",
                "/api/loop/features",
                {"start": start, "minutes": m, "limit": min(20, max(max_feat + 6, 12))},
            )
            if st_f != 200:
                raise DemoError(f"loop {loop_spec['id']} ({m} min features): failed ({st_f}): {feat_resp.get('error')}")
            seen_slugs: set[str] = set()
            for f in feat_resp.get("features", []):
                if not f.get("fits") or len(feat_entries) >= max_feat:
                    continue
                st_a, anc_s = app.api(
                    "POST",
                    "/api/loop",
                    {"start": start, "minutes": m, "steer": "anchor", "feature": f["name"]},
                )
                if st_a == 422:
                    continue
                if st_a != 200:
                    raise DemoError(
                        f"loop {loop_spec['id']} ({m} min {f['name']!r}): search failed ({st_a}): {anc_s.get('error')}"
                    )
                slug = _slug(f["name"], seen_slugs)
                rel = _pack(
                    f"loop-{m}-feat-{slug}",
                    {"minutes": m, "steer": "anchor", "feature": f["name"]},
                    anc_s,
                )
                feat_entries.append({
                    "id": slug,
                    "name": f["name"],
                    "point": f["point"],
                    "round_trip_min": f["round_trip_min"],
                    "file": rel,
                })

        by_min[str(m)] = {"any": any_rel, "compass": compass_map, "features": feat_entries}
        log(
            f"loop {loop_spec['id']} {m}m: {len(files) - n_before} searches "
            f"(1 any, {len(compass_map)} dirs, {len(feat_entries)} features) in {time.monotonic() - t0:.1f} s"
        )

    loop_index = {
        "id": loop_spec["id"],
        "name": loop_spec["name"],
        "start": start,
        "budgets": loop_spec["budgets"],
        "default_min": loop_spec["default_min"],
        "by_min": by_min,
    }
    return loop_index, files


def demo_html(index_html: str) -> str:
    """static/index.html in demo mode: `data-mode="demo"` on <html> and the
    CSP as a <meta> tag (a static host sends no CSP header)."""
    if '<html lang="en">' not in index_html or '<meta charset="utf-8">' not in index_html:
        raise DemoError("static/index.html no longer has the markers the demo builder edits")
    csp = f'<meta http-equiv="Content-Security-Policy" content="{CSP_BASE}">'
    return (index_html.replace('<html lang="en">', '<html lang="en" data-mode="demo">', 1)
            .replace('<meta charset="utf-8">', f'<meta charset="utf-8">\n{csp}', 1))


def _prepare(out: Path) -> None:
    """Clear `out` if this builder made it; refuse any other non-empty folder."""
    if out.exists():
        if any(out.iterdir()) and not (out / MARKER).exists():
            raise DemoError(f"{out} is not empty and was not made by `scenic demo build`; pick another --out")
        shutil.rmtree(out)
    (out / "data").mkdir(parents=True)
    (out / MARKER).write_text("Made by `scenic demo build`; the next build clears this folder.\n")


def _dump(obj, path: Path) -> int:
    data = json.dumps(obj, separators=(",", ":"), allow_nan=False, ensure_ascii=False).encode("utf-8")
    path.write_bytes(data)
    return len(data)


def build_site(out: Path | None = None, presets_path: Path | None = None, ctx=None,
               graph_dir: Path | None = None, score_dir: Path | None = None,
               max_points: int = DEMO_MAX_POINTS, log: Callable[[str], None] = print) -> Path:
    presets = load_presets(presets_path)
    loop_spec = load_loop_preset(presets_path)
    out = Path(out or DEFAULT_OUT)
    if ctx is None:
        from ..search import Context

        t0 = time.monotonic()
        ctx = Context.load(graph_dir, score_dir)
        log(f"loaded the route data in {time.monotonic() - t0:.1f} s")
    app = App(ctx=ctx, geocoder=_NoGeocoder(), max_points=max_points)
    wp_cache: dict[tuple[bytes, float], dict] = {}
    built = []
    for p in presets:  # compute everything before touching `out`
        t0 = time.monotonic()
        built.append(build_preset(app, p, wp_cache=wp_cache))
        log(f"{p['id']}: {len(built[-1]['search']['routes'])} routes in {time.monotonic() - t0:.1f} s")
    loop_index, loop_files = (build_loop_demos(app, loop_spec, wp_cache=wp_cache, log=log)
                              if loop_spec is not None else (None, []))

    _prepare(out)
    index = []
    total = 0
    for d in built:
        p, s = d["preset"], d["search"]
        n = _dump(d, out / "data" / f"{p['id']}.json")
        total += n
        fastest = s["routes"][0]
        index.append({**p, "file": f"data/{p['id']}.json", "t0_min": s["t0_min"], "km": fastest["km"],
                      "routes": len(s["routes"]), "bytes": n})
    for fname, payload in loop_files:
        total += _dump(payload, out / "data" / fname)
    meta = {"title": "Maps for Wanderers", "generated": time.strftime("%Y-%m-%d", time.gmtime()),
            "source": SOURCE_URL, "s_breaks": app.breaks,
            "budgets": [r for r in app.health().get("budgets", [])], "presets": index}
    if loop_index is not None:
        meta["loop"] = loop_index
    total += _dump(meta, out / "data" / "presets.json")

    for name in COPY:
        src = STATIC_DIR / name
        if src.is_dir():
            shutil.copytree(src, out / "static" / name)
        else:
            (out / "static").mkdir(exist_ok=True)
            shutil.copy2(src, out / "static" / name)
    (out / "index.html").write_text(demo_html((STATIC_DIR / "index.html").read_text()))
    extra = f" + {len(loop_files)} loop searches" if loop_files else ""
    log(f"wrote {out} ({len(built)} presets{extra}, {total / 1024:.0f} KB of JSON)")
    return out

