"""Static demo site for Maps for Wanderers (docs/PUBLISH.md).

`scenic demo build` runs the real search for each preset in
config/demo_routes.toml, places Google Maps waypoints for every route it
picks, and writes a self-contained static site:

    site/index.html              the page from static/, in read-only demo mode
    site/static/...              app.js, app.css and the self-hosted Leaflet
    site/data/presets.json       the preset list
    site/data/<id>.json          one preset: the /api/route payload and the
                                 /api/waypoints answer for each of its routes

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

from .server import CSP_BASE, STATIC_DIR, App

PROJECT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_PRESETS = PROJECT_DIR / "config" / "demo_routes.toml"
DEFAULT_OUT = PROJECT_DIR / "site"
SOURCE_URL = "https://github.com/tvh0021/design/tree/main/scenic"
DEMO_MAX_POINTS = 1500  # per route; ~20 m tolerance on a 200 km drive, fine up to zoom 13 or so
MARKER = ".maps-for-wanderers-demo"  # marks a folder this builder owns and may clear
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
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


def build_preset(app: App, preset: dict) -> dict:
    """The page's data for one preset: the route payload and, per route
    key, the waypoint answer (or {"error": ...})."""
    st, search = app.api("POST", "/api/route", {"from": preset["from"], "to": preset["to"]})
    if st != 200:
        raise DemoError(f"preset {preset['id']}: search failed ({st}): {search.get('error')}")
    waypoints = {}
    for r in search["routes"]:
        st, wp = app.api("POST", "/api/waypoints", {"id": search["id"], "key": r["key"]})
        waypoints[r["key"]] = wp if st == 200 else {"error": wp.get("error", f"HTTP {st}")}
    # what the page does not use
    search.pop("seconds", None)
    for r in search["routes"]:
        r.pop("points_raw", None)
    search["id"] = preset["id"]
    return {"preset": preset, "search": search, "waypoints": waypoints}


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
    out = Path(out or DEFAULT_OUT)
    if ctx is None:
        from ..search import Context

        t0 = time.monotonic()
        ctx = Context.load(graph_dir, score_dir)
        log(f"loaded the route data in {time.monotonic() - t0:.1f} s")
    app = App(ctx=ctx, geocoder=_NoGeocoder(), max_points=max_points)
    built = []
    for p in presets:  # compute everything before touching `out`
        t0 = time.monotonic()
        built.append(build_preset(app, p))
        log(f"{p['id']}: {len(built[-1]['search']['routes'])} routes in {time.monotonic() - t0:.1f} s")

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
    meta = {"title": "Maps for Wanderers", "generated": time.strftime("%Y-%m-%d", time.gmtime()),
            "source": SOURCE_URL, "s_breaks": app.breaks,
            "budgets": [r for r in app.health().get("budgets", [])], "presets": index}
    total += _dump(meta, out / "data" / "presets.json")

    for name in COPY:
        src = STATIC_DIR / name
        if src.is_dir():
            shutil.copytree(src, out / "static" / name)
        else:
            (out / "static").mkdir(exist_ok=True)
            shutil.copy2(src, out / "static" / name)
    (out / "index.html").write_text(demo_html((STATIC_DIR / "index.html").read_text()))
    log(f"wrote {out} ({len(built)} presets, {total / 1024:.0f} KB of JSON)")
    return out
