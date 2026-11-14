# Publishing: the static demo

The interactive planner (`scenic serve`, `docs/WEB.md`) runs only on your own
machine. For the public site there is a static demo instead: five preset
point-to-point drives plus 1-, 2-, and 3-hour Drive-around loops starting and
finishing in Auburn in the Finger Lakes, all computed ahead of time and shown
by the same page in a read-only demo mode. It has no server code and makes no
outside requests except the visitor's map tiles.

## Build

```
scenic demo build [--out DIR] [--presets FILE] [--graph DIR] [--score DIR] [--max-points N]
```

It reads the presets and optional `[loop]` block from `config/demo_routes.toml`.
For each `[[preset]]` it runs `/api/route`, and for `[loop]` it runs
`/api/loop` across every configured duration (`60`, `120`, and `180` minutes
from Auburn) for `Any way`, all eight compass directions, and up to eight
nearby scenic features (`Cayuga Lake`, `Owasco Lake`, `Skaneateles Lake`,
`Seneca Lake`, `Keuka Lake`, `Canandaigua Lake`, `Lake Ontario`, and others).
Every recommended route or loop also gets its Google Maps waypoints
(`/api/waypoints`), split into editable multi-part links whenever a route
needs more than 7 waypoints. Identical edge paths share cached waypoint
reductions during the build. The builder writes the site to `scenic/site/` by
default in roughly two minutes.

All routes and loops are computed before anything is written. The output folder
is then cleared and rewritten. The builder only clears a folder that holds its
own `.maps-for-wanderers-demo` marker (or is empty), and refuses any other.

Layout:

```
site/index.html                      static/index.html with data-mode="demo" and the CSP as a <meta> tag
site/static/app.js, app.css          the same frontend as scenic serve
site/static/vendor/leaflet-1.9.4/    self-hosted Leaflet (JS, CSS, images, LICENSE)
site/static/vendor/spectral/         self-hosted Spectral woff2 files (latin subset) and OFL.txt
site/data/presets.json               the preset list and the Drive-around loop index
site/data/<id>.json                  one A-to-B preset: /api/route payload + waypoints per route
site/data/loop-<min>-any.json        one Drive-around Any way search + waypoints per loop
site/data/loop-<min>-dir-<d>.json    one Drive-around compass direction search + waypoints
site/data/loop-<min>-feat-<slug>.json one Drive-around scenic feature search + waypoints
```

Each preset or loop file holds `search` (without the timing breakdown and
`points_raw`) and `waypoints` (mapping each route key to the `/api/waypoints`
answer, or to `{"error": ...}`). Route lines are simplified to at most 1,500
points (`--max-points`), with coordinates at 5 decimals. Visitors load only
`presets.json` and the individual drives or loops they open.

Every path is relative, so the site works from any folder, such as
`/design/wanderers/` on GitHub Pages. The headless check below served it
from that path.

## Demo mode in the page

`<html data-mode="demo">` switches `app.js` to demo mode:

- The `A to B` tab lists the five point-to-point presets ("Pick a drive"), and
  the `Drive-around` tab shows Auburn as the fixed start and finish (`S`) with
  `1 h`, `2 h`, and `3 h` budgets, `Any way`, `A direction` (8-way compass
  rose), and `Past a place` (clickable Finger Lakes feature chips).
- Choosing a preset or loop loads its `data/*.json` file and shows everything
  the live page shows after a search: the picks, scenery coloring and strips,
  scenic stops grouped by feature, numbered waypoints, and editable Google Maps
  links (single-link or Part 1 / Part 2 when split).
- There is no place search, no map-click point picking (markers cannot be
  dragged), no custom minute input, no Find/Swap/Clear/home buttons, and no
  health polling. `api()` throws in demo mode. The only fetches are
  `data/*.json`.
- The URL hash keeps the view in both tabs:
  `#preset=finger-lakes&route=%2B30%25&layer=topo` or
  `#mode=loop&min=180&steer=anchor&feature=Keuka%20Lake`.
- The units switch, route selection, scenery coloring, and base layers work
  as in the live page.

## GitHub Pages deployment

`.github/workflows/deploy.yml` copies `scenic/site/.` into `_site/wanderers/`
and links to it from `site/index.html`. Because CI does not carry the New York
road graph, rebuild and commit `scenic/site/` locally whenever preset routes,
loop recommendations, moments, or waypoint logic change:

1. Run `scenic demo build` inside `scenic/`.
2. Commit the updated `scenic/site/` directory.

## Third-party resources

These are what visitors' browsers load. They were checked on 2026-11-03 by
reading each provider's current policy page. The USGS services were also
checked by fetching their service metadata and sample tiles.

| Resource | Used for | Policy | Key terms | Decision |
|---|---|---|---|---|
| OpenStreetMap standard tiles, `https://tile.openstreetmap.org/{z}/{x}/{y}.png` | Streets layer | https://operations.osmfoundation.org/policies/tiles/ | No key; use that exact URL. "From web pages, ensure a valid HTTP Referer header is sent"; no restrictive Referrer-Policy; attribution "© OpenStreetMap contributors" visible, not hidden. No offline use, prefetching or automated scans. "We may block access, without notice, if your usage degrades the service"; no SLA. Sites that seek donations or are commercial are more likely to be cut off. | **Kept.** A small non-commercial site with attribution fits. The page now sends its origin as Referer (`strict-origin-when-cross-origin`, on the page and on each tile layer; it was `no-referrer` before). |
| Esri World Topo Map and World Imagery, keyless `server.arcgisonline.com` tiles | Terrain and satellite layers (before) | https://developers.arcgis.com/documentation/mapping-and-location-services/mapping/basemaps/ , https://developers.arcgis.com/documentation/terms-of-use/ , https://doc.arcgis.com/en/arcgis-online/reference/terms-of-use.htm | Esri's documented access to basemap tiles needs an ArcGIS Location Platform account and an access token, and its terms require Esri and data attribution. No page we could read permits keyless use of `server.arcgisonline.com` by a third-party public site. The item pages, which hold the item terms, render client-side and could not be read. | **Dropped**, replaced by USGS. |
| USGS The National Map, `https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}` and `.../USGSImageryOnly/MapServer/tile/{z}/{y}/{x}` | Terrain and satellite layers | https://www.usgs.gov/faqs/what-are-terms-uselicensing-map-services-and-data-national-map | "Map services and data downloaded from The National Map are free and in the public domain. There are no restrictions"; USGS asks for the credit "Map services and data available from U.S. Geological Survey, National Geospatial Program." No key. Web Mercator, 256 px tiles to zoom 16 (z17 returns 404), `Access-Control-Allow-Origin: *`, cached one day. US coverage only. | **Adopted.** Both layers set `maxNativeZoom: 16` (Leaflet enlarges beyond it) and show the USGS credit. |
| unpkg CDN (Leaflet 1.9.4) | Leaflet JS and CSS (before) | https://unpkg.com/ , https://unpkg.com/about.md | "A free service provided as-is"; "no uptime guarantee or SLA". Pin exact versions. | **Dropped**: Leaflet is self-hosted under `static/vendor/leaflet-1.9.4/`. The files are the unchanged npm release (BSD-2-Clause, LICENSE included), and their SHA-256 matches the SRI hashes the page pinned before. The CSP allows only `'self'` for scripts and styles. |
| Google Fonts (Spectral) | The website's typeface | https://openfontlicense.org (cited in `OFL.txt`) | Spectral is under the SIL Open Font License 1.1: free to use, bundle and redistribute with the license included. | **Self-hosted**, never linked: the latin woff2 files for six styles under `static/vendor/spectral/` with `OFL.txt` (88,608 bytes). The CSP stays `'self'`; visitors make no request to Google for fonts. |
| Nominatim, `nominatim.openstreetmap.org` | Place search, local `scenic serve` only; never called by the demo | https://operations.osmfoundation.org/policies/nominatim/ | At most 1 request/s; a User-Agent that identifies the application; visible attribution; results under ODbL. No auto-complete, no systematic queries. Cache results; apps "must make sure that they can switch the service at our request". | **Kept for local use**, as before: searches only on Enter, 1/s across the server, disk cache, User-Agent `maps-for-wanderers/0.1 (personal research project)`. The place list now credits Nominatim and OpenStreetMap. |
| Google Maps | "Open in Google Maps" link | — | No API is called. The link opens only when the visitor clicks it, with `rel="noopener noreferrer"`. | Unchanged. |

Considered and not used:

- **OpenTopoMap** (https://opentopomap.org/about). It is CC-BY-SA with no
  key, and requires the credit "Kartendaten: © OpenStreetMap-Mitwirkende,
  SRTM | Kartendarstellung: © OpenTopoMap (CC-BY-SA)". It runs on one
  volunteer server (moved to a smaller vServer in January 2026) with no
  availability guarantee. It is a fallback for topo if USGS goes away.
- **CARTO basemaps** (https://carto.com/legal/basemap-terms/). These now need
  an API key; raster tiles without one carry an "API key required"
  watermark.
- **OpenFreeMap** (https://openfreemap.org/tos/). It needs no key and has no
  limits, but serves vector tiles only, which would need MapLibre GL and a
  Leaflet plugin.

Attribution: each base layer shows its own credit. A fixed "Routes:
OpenStreetMap data" credit stays on every layer, because the roads, names
and scenic stops come from OSM (ODbL).

Observed quirk: at low zoom, USGS Topo's own labels can look odd (at zoom 8
it labelled Lake Ontario "Lake America"). That label comes from the tile
service, not from this project.

## Privacy

The demo is static files. Nothing about visitors is stored by this project.
The page keeps only the mi/km choice in the visitor's own browser
(localStorage, `scenic.units`). GitHub Pages serves the files under
GitHub's own terms and logs. The map tiles come straight from OpenStreetMap
and the USGS, which see the visitor's IP address, user agent and the site's
origin, as with any web map. The page footer says so in one sentence.

## Checking a build

The tests (`tests/test_web.py`, the "static demo" section) build the site
from a toy graph. They check that the JSON is strict and complete, that the
HTML loads only relative, self-hosted assets that exist in the site, and
that demo mode cannot reach `/api`.

For the real build, serve the folder from a subpath and open it in headless
Chrome:

```
mkdir -p /tmp/pages-root/design && ln -s "$PWD/site" /tmp/pages-root/design/wanderers
python3 -m http.server 8792 --bind 127.0.0.1 --directory /tmp/pages-root
# open http://127.0.0.1:8792/design/wanderers/#preset=finger-lakes ; the server log
# should list only index.html, static/*, data/*.json, with no /api requests
```
