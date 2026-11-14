# Web UI: Maps for Wanderers

`scenic serve` runs Maps for Wanderers, a map page for A-to-B route search and
drive-around loops, on your own machine. The same page also runs as a read-only
static demo with preset routes (`scenic demo build`, see `docs/PUBLISH.md`).

```
scenic serve [--port 8765] [--graph DIR] [--score DIR] [--verbose]
```

Open http://127.0.0.1:8765/. The server listens on 127.0.0.1 only and has no
login, so it is not reachable from other machines. It needs the same data as
`scenic route`: `data/graph/` and `data/score/` (scores and moments). The
route data loads in the background in about a second. The page shows a
loading banner until it is ready, and builds the reverse time matrix and
waypoint index (about 3.5 s) just after that.

## Using it

- **Start and end.** Click the map once for A and again for B, then drag the
  markers to adjust. You can also type a place name and press Search (or
  Enter), then pick a result. Typing `43.1566, -77.6088` sets a point
  directly. Clear starts over and Swap reverses the trip.
- **Home.** "Set as home" next to A, B or S saves that point in the browser
  (localStorage key `scenic.home`, `{lat, lon, label}`). "From home" puts it
  in A or in the loop start S.
- **Units.** The mi / km switch (top of the panel) sets every distance on
  the page. Miles are the default; the choice is kept in localStorage
  (`scenic.units`). Server errors send distances as numbers (`snap_m`,
  `limit_m`), and the page words them in the chosen unit, so an error
  already on screen switches units too.
- **Find routes.** Shows the fastest route as a dashed grey line and the
  most scenic route under +15 %, +30 % and +50 % of its time in blue
  (#0072B2), orange (#E69F00) and reddish purple (#CC79A7), the Okabe-Ito
  colours, picked by the route's `role` index. A search takes about 3 s.
  The table lists, per route: minutes, extra minutes over the fastest
  route, distance, and scenic minutes (drive time on roads scenier than the
  fastest route's average, weighted by how much scenier; the caption under
  the table says so). Under each row a thin strip shows the route's scenery
  from start to end in the five scenery colours, with its first three main
  roads. A muted details line gives the number of candidates and the search
  time, and the snap distance only when A or B is more than 500 m from a
  road. When two picks are the same route, it is drawn once and its row
  shows both labels (for example `+30% = +50%`).
- **Select a route** by clicking its row or its line. The selected route is
  drawn wide and on top; the others are thin. The checkboxes show or hide
  routes. The detail area says how scenic the route is as a percentile
  ("on average, scenier than 58% of NY road miles") and lists its scenic
  stops in drive order, as `min 47 · Lake Ontario on CR 101 near Sodus
  Point` (minutes from the start). Moments the route passes but that earn no
  credit are not listed. Consecutive stops at the same named lake or river
  (bays count as part of their lake) fold into one line, such as `min 243–288 · Lake Ontario shoreline, 5
  stops`, which opens to list them; each keeps its own dot on the map. The
  server sends these runs as `stop_groups`. The stops are white dots on the map; each popup
  gives the label, the kind of stop, the minute, and a link to the spot in
  Google Maps.
- **Color by scenery** is on by default. It draws the selected route in
  five colours by edge score S, from pale yellow to dark blue, inside a
  casing in the route's own colour. Each colour covers about a fifth of New
  York's road miles (breaks from the data, currently 0.17, 0.38, 0.58 and
  0.79), and the legend names them "bottom fifth" to "top fifth". The
  checkbox turns it off.
- **Google Maps.** The detail area has one button for the selected route,
  "Get a Google Maps link". It reduces the route to at most 9 waypoints
  that make the fastest route follow it (`scenic.waypoints`) and shows the
  result in place: the number of waypoints and how much of the route
  Google's route follows, a warning when that is under 90 % (check the
  route in Google before driving), the waypoint placer's notes folded under
  "Notes", and numbered pins on the map (an orange outline marks a weaker
  spot near a junction or another road). Only then does it show the "Open
  in Google Maps" link, which opens a new tab when clicked. No tab opens
  before the result is known.
- **Map key.** With results on screen, a key at the bottom left explains
  the scenic stop dot, the numbered waypoint and the weak waypoint outline.
- **Base layers.** Switch between OpenStreetMap streets, USGS Topo
  (terrain and contours, for hills) and USGS Imagery (top right), all from
  public tile servers that need no API key (`docs/PUBLISH.md` gives their
  terms). The USGS layers have tiles to zoom 16 and are enlarged beyond it.
  Satellite shows shorelines and lakes under the routes. The attribution
  always credits OpenStreetMap for the routes, whatever the base layer.
- **Drive-around (`docs/LOOP_SPEC.md`).** Switch to the Drive-around tab to
  plan loops starting and ending at `S` (copied from `A` on first open if `A`
  is already set). Pick a budget (`1 h`, `2 h`, `3 h`, or a custom 30 to
  360 minutes) and choose "Which way":
  - **Any way** spreads up to three loops across compass directions.
  - **Direction** opens an 8-point compass rose (`N` through `NW`) and steers
    into a 135 degree cone around that bearing.
  - **Pass by** anchors the trip on a scenic feature chip within reach, a
    place or feature search, or a map pin `P` (clicking open water snaps to
    the nearest mapped lake or river shore).
  Loop rows omit the extra-minutes column, draw in the three budget colours
  (`role` 1 to 3), and note shared out-and-back stems of 1 km or more in the
  detail line. Switching between A to B and Drive-around keeps each tab's
  pins and last search in memory. The static demo hides Drive-around because
  it has no server.

The page URL keeps the trip, for example
`#from=43.15660,-77.60880&to=43.04810,-76.14740&route=%2B50%25&scenery=0&layer=topo`
in A-to-B mode or `#mode=loop&start=43.15660,-77.60880&min=120&steer=compass&dir=NE&route=Northeast%20loop`
(or `steer=anchor&feature=Lake%20Ontario` / `steer=anchor&anchor=lat,lon`) in
Drive-around mode (`scenery=0` when colouring is off; `layer` is `topo` or
`satellite`). Opening such a link runs the search as soon as the data is loaded.

A search answer that arrives after the active tab's pins or controls have
changed (a drag, Swap, Clear, budget or steer edit, or a newer search) is
dropped, and so is a waypoint answer for a search that has since been
replaced.

## Errors

Messages start with a capital and say what to do next. When the problem is
one point, the server names it (`"point": "from"`, `"to"`, `"start"`, or
`"anchor"`) and the page puts a pulsing red ring on that pin (`A`, `B`, `S`,
or `P`). The page shows the server's message for:

- a point outside New York State's bounding box (422, "Drag A into New York"),
- a point more than 2 km from the nearest road in the graph, such as one out
  in a lake for `A`, `B` or `S` (422, `code` `snap_far` with `snap_m` and
  `limit_m`, which the page words in miles or km),
- an anchor pin over 2 km from any road with no mapped water shore within
  45 km (422, `code` `anchor_far`), an unknown feature name (422, `code`
  `unknown_feature`), or an anchor too far for the chosen budget (422, `code`
  `anchor_out_of_reach` with `round_trip_min` and `needed_min`),
- no loop fitting the budget or compass cone (422, `code` `no_loop` or
  `no_loop_that_way`),
- no route between `A` and `B`, for example when both points snap to the same
  junction (404),
- a search sent while the data is still loading (503),
- a place search that Nominatim could not answer (502).

## Place search

Place names are looked up by the server through Nominatim
(nominatim.openstreetmap.org), limited to the United States and to New
York's bounding box. Requests follow the Nominatim usage policy: search
runs only when you press Search (no autocomplete), at most one request per
second across the whole server, and every answer is cached on disk in
`~/.cache/scenic-web/geocode/`. Each entry is written under a lock through
a unique temp file, and an entry that is not JSON of the shape
`{"results": [{name, lat, lon, type}]}` counts as a miss and is fetched
again. Names lose a trailing ", New York, United States" (or ", United
States"). The request carries the query, the box and
the User-Agent `maps-for-wanderers/0.1 (personal research project)`. It
sends no name, email or other personal detail. The place list ends with the
credit "Place search: Nominatim, © OpenStreetMap contributors", as the
Nominatim usage policy asks.

## API

All bodies are JSON; POST requests must send `Content-Type:
application/json`. Errors are `{"error": "...", "point": "from" | "to" |
"start" | "anchor", "code": "...", ...}`, with `point` only when the message
is about one point and `code` for machine-readable error kinds (`snap_far`,
`bad_minutes`, `anchor_out_of_reach`, `anchor_far`, `unknown_feature`,
`no_loop`, `no_loop_that_way`). Responses over 1 KB are gzipped when the
client accepts it. Coordinates are `[lat, lon]` with 5 decimals.

`GET /api/health`

```
{"status": "loading" | "ready" | "error", "bbox": [s, w, n, e], "max_snap_m": 2000,
 "edges": 825892, "moments": 3820, "s_breaks": [0.17, 0.377, 0.585, 0.792],
 "budgets": ["+15%", "+30%", "+50%"], "load_s": 0.8,
 "loop": {"budgets_min": [60, 120, 180], "default_min": 120, "min_minutes": 30,
          "max_minutes": 360, "directions": ["N", "NE", "E", "SE", "S", "SW", "W", "NW"],
          "n_loops": 3},
 "error": "..."}
```

`POST /api/geocode` with `{"q": "Watkins Glen"}`

```
{"results": [{"name": "Watkins Glen, Town of Dix, Schuyler County", "lat": 42.38106, "lon": -76.87058,
              "type": "administrative"}]}
```

`POST /api/route` with `{"from": [lat, lon], "to": [lat, lon]}`

```
{"id": "7c0237ed2574", "from": [...], "to": [...], "snap_m": [6, 3], "t0_min": 86.1,
 "S_fast": 0.3, "kept": 109, "search_s": 3.28, "seconds": {...}, "s_breaks": [...],
 "routes": [
   {"key": "r0", "labels": ["fastest"], "role": 0, "origin": "fastest", "minutes": 86.1, "ratio": 1.0,
    "km": 140.6, "scenic_minutes": 4.3, "mean_S": 0.3, "mean_S_pct": 32, "moment_points": 4.8,
    "score": 28.4, "roads": [["I 90", 98.1], ...],
    "moments": [{"id": 232, "type": "water", "m": 1.0, "label": "...", "lat": ..., "lon": ...,
                 "min_along": 12.3, "feature": "Lake Ontario", "parent_feature": "Lake Ontario"}],
    "stop_groups": [{"first": 0, "last": 4, "feature": "Lake Ontario"}, ...],
    "moments_passed": 7,
    "line": [[lat, lon], ...],        // display geometry
    "runs": [[first, last, class]],   // point ranges of `line` by S class (0..4)
    "points_raw": 1674},
   ...]}
```

`routes` holds one entry per distinct route; `labels` lists every role it
fills, and `role` is the index of the first one (0 fastest, k the k-th
budget), which picks the colour. `mean_S_pct` is the share of New York road
length (0-100) with S below the route's time-weighted mean S. `moments` are
the credited moments in drive order; `min_along` is the drive time in
minutes from the start to each one (the moment's position along the route,
converted with the cumulative edge times). `feature` is the named lake,
river or falls (null when unnamed or generic). `parent_feature` is the
water body it belongs to: Lake George for a stop named after Huddle Bay,
otherwise the feature. `stop_groups` splits `moments` into runs of
consecutive stops with the same `parent_feature`, as index ranges that
cover every stop once; a stop with no feature is its own run. A group's
`feature` is that parent. `line` is simplified per S run with a tolerance that starts at about
0.5 m and doubles until the route has at most 5,000 points. Run ends stay
exact. The server keeps the last 8 route and loop searches for `/api/waypoints`.

`POST /api/loop` with `{"start": [lat, lon], "minutes": 120, "steer": "any" | "compass" | "anchor", ...}`

```
{"id": "a19f02cd8810", "mode": "loop", "start": [...], "snap_m": [12], "minutes": 120,
 "steer": {"mode": "compass", "direction": "NE", "bearing": 45},
 "s_base": 0.5, "kept": 36, "built": 72, "search_s": 1.4, "seconds": {...},
 "s_breaks": [...], "note": null,
 "routes": [
   {"key": "r0", "labels": ["Northeast loop"], "role": 1, ...,
    "direction": "NE", "bearing": 41.2, "off_deg": 3.8, "stem_km": 0.4, "tier": 1},
   ...]}
```

Returns up to 3 loops with `role` 1 to 3 (never 0) and `s_base` 0.50 as the
scenic-minute baseline. For `"steer": "compass"`, pass `"direction"` (`"N"`
through `"NW"`). For `"steer": "anchor"`, pass either `"anchor": [lat, lon]`
or `"feature": "Lake Ontario"`.

`POST /api/loop/features` with `{"start": [lat, lon], "minutes": 120, "q": "lake", "limit": 8}`

```
{"features": [{"name": "Lake Ontario", "count": 21, "best_m": 1.0, "types": ["water"],
               "round_trip_min": 19, "fits": true, "needed_min": 22,
               "point": [43.25702, -77.60331], "bearing": 2.1, "direction": "N"}, ...]}
```

Local lookup over `moments.parquet` with no external network call. Without `q`
it returns features that fit the budget; with `q` it returns substring matches
with fitting features first.

`POST /api/waypoints` with `{"id": "<search id>", "key": "r3"}`

```
{"key": "r3", "url": "https://www.google.com/maps/dir/?api=1&origin=...", "url_length": 185,
 "origin": [...], "destination": [...],
 "waypoints": [{"n": 1, "lat": 43.26804, "lon": -77.40274, "road": "CR 1", "tier": 1}, ...],
 "meets_target": true, "follow_target": 0.9,
 "summary": {"waypoints": 3, "overlap": 0.9923, "eta_min": 124.5, ...},
 "notes": [], "parts": [], "seconds": 0.61}
```

Google Maps counts stop boxes (origin, waypoints, and destination, even when
`origin` equals `destination` on a closed loop) and locks route editing at 10
stops (`"Maximum number of destinations reached"` or `"Route cannot be modified
further"`). `split_waypoints` caps single links at 7 intermediate waypoints (9
stops, `follow_target=0.9`). When a long route or loop needs more than 7
waypoints or would create a U-turn under the cap, `split_waypoints` splits the
full waypoint chain into 2 or more balanced links in `parts` (each with at most
7 intermediate waypoints, sharing a `"split": true` handoff waypoint) and keeps
the 7-waypoint single-link summary in `single`. On a closed loop `origin`
equals `destination` and the server requires at least one waypoint. Results are
cached per search and route.

## Design

The page carries the look of the author's website (tvh0021.github.io), so
the demo reads as part of it. A compact copy of the site's navigation runs
across the top (the italic "Trung Ha" mark, then Home, Research, CV,
Projects and Contact, with Projects marked as the current section), and the
panel ends with the site's footer line. The page has no email address.

- **Palette**, from the website's stylesheet (`style.css`, sampled from its
  star-trails photograph): `--sky-deep #172030` page and nav background,
  `--sky #222c3b` panel, `--line #3a4658` hairlines, `--trail #eef1f5`
  text, `--haze #a3b0c1` secondary text, `--halpha #e8687b` accent (link
  underlines, hover, focus ring, active tab, selected route and preset).
  Hairline borders, square corners, no drop shadows on the panel. Banners
  tint the dark background (grey for info, amber for warnings, halpha for
  errors) with a coloured left rule.
- **Data colours stay as they were**: the Okabe-Ito route colours, the
  YlGnBu scenery classes, the stop-type tags and the A/B pins (the panel's
  A and B discs get a light ring, as on the map). Leaflet's zoom, layer,
  scale and attribution controls, popups and tooltips take the dark panel
  colours with a hairline; the base maps are unchanged.
- **Type**: Spectral, as on the website (body 300, headings 200 with tight
  letter-spacing, italic for the subtitle and secondary lines). It is
  self-hosted under `static/vendor/spectral/`: the latin-subset woff2 files
  Google Fonts serves for weights 200, 300, 400, 600 and italic 300 and 400
  (Spectral v15, fetched 2026-11-03), unchanged, with the SIL Open Font
  License (`OFL.txt`). Six files, 88,608 bytes in all; a page loads the
  ones it uses (five on the demo, about 73 KB). `app.css` declares them with
  `font-display: swap` and falls back to Georgia, serif; characters outside
  the latin subset (the arrows) come from the fallback.
- **CSP**: unchanged. Fonts fall under `default-src 'self'`, so self-hosted
  files load and no font host is allowed. The page links no Google Fonts.
  `scenic serve` sends the files as `font/woff2`.
- **Narrow screens** (under 900 px, where the website goes to one column):
  the panel sits above the map, takes a bit under half the height and
  scrolls on its own, so the map stays on screen.

`test_vendored_fonts_referenced_by_app_css_exist` checks that every
`url()` in `app.css` is a self-hosted file that exists.

## Notes

- Searches and waypoint reductions run one at a time (one lock), since the
  route context is not known to be thread-safe. A second search waits for the
  first.
- The server refuses requests whose `Host` header is not `127.0.0.1` or
  `localhost`, which keeps DNS-rebinding pages out. POSTs must be JSON, which
  keeps cross-site form posts out.
- Leaflet 1.9.4 is self-hosted under `static/vendor/leaflet-1.9.4/` (the
  unchanged npm files, BSD-2-Clause, with their subresource integrity hashes
  still checked). All page paths are relative. The Content-Security-Policy
  allows scripts and styles from the page's own origin only, and images from
  the two tile hosts (tile.openstreetmap.org, basemap.nationalmap.gov).
- The referrer policy is `strict-origin-when-cross-origin`: tile requests
  carry the page's origin as Referer, as OSM's tile policy asks, and nothing
  more. Google Maps links still open with `rel="noopener noreferrer"`.
- Text from OpenStreetMap or Nominatim (moment labels, road and place
  names) reaches the page only through `textContent` or DOM nodes; Leaflet
  would parse a string given to `bindTooltip`, `bindPopup`, `setContent` or
  `divIcon({html})` as HTML. `test_page_never_hands_leaflet_html_strings`
  guards the source. Manual check: run a toy server whose moment label,
  waypoint road and place name are `<img src=x onerror="window.__xss=1">`,
  hover and click the moment, place waypoints, search a place, then confirm
  the payload shows as text and `document.querySelectorAll('img[src="x"]')`
  is empty. (Done on 2026-11-02 in headless Chrome: payload shown as text
  in the tooltip, popup, waypoint tooltip, stop list and place list; no
  image created; console clean.)
