# Web UI

`scenic serve` runs a map page for A-to-B route search on your own machine.

```
scenic serve [--port 8765] [--graph DIR] [--score DIR] [--verbose]
```

Open http://127.0.0.1:8765/. The server listens on 127.0.0.1 only and has no
login, so it is not reachable from other machines. It needs the same data as
`scenic route`: `data/graph/` and `data/score/` (scores and moments). The
route data loads in the background in about a second. The page shows a
loading banner until it is ready, and builds the waypoint index (about 3 s)
just after that.

## Using it

- **Start and end.** Click the map once for A and again for B, then drag the
  markers to adjust. You can also type a place name and press Search (or
  Enter), then pick a result. Typing `43.1566, -77.6088` sets a point
  directly. Clear starts over and Swap reverses the trip.
- **Home.** "Set as home" next to A or B saves that point in the browser
  (localStorage key `scenic.home`, `{lat, lon, label}`; a future loop mode
  can start from it). "From home" puts it in A.
- **Units.** The mi / km switch (top of the panel) sets every distance on
  the page. Miles are the default; the choice is kept in localStorage
  (`scenic.units`). Server error messages stay in km.
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
  credit are not listed. The stops are white dots on the map; each popup
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
- **Base layers.** Switch between OpenStreetMap streets, Esri World Topo
  Map (terrain and contours, for hills) and Esri World Imagery (top right).
  Satellite shows shorelines and lakes under the routes.
- **Drive-around (loop)** is a placeholder; loops are not built yet.

The page URL keeps the trip, for example
`#from=43.15660,-77.60880&to=43.04810,-76.14740&route=%2B50%25&scenery=0&layer=topo`
(`scenery=0` when colouring is off; `layer` is `topo` or `satellite`).
Opening such a link runs the search as soon as the data is loaded.

A search answer that arrives after A or B has changed (a drag, Swap, Clear,
or a newer search) is dropped, and so is a waypoint answer for a search
that has since been replaced.

## Errors

Messages start with a capital and say what to do next. When the problem is
one point, the server names it (`"point": "from"` or `"to"`) and the page
puts a pulsing red ring on that pin. The page shows the server's message for:

- a point outside New York State's bounding box (422, "Drag A into New York"),
- a point more than 2 km from the nearest road in the graph, such as one out
  in a lake (422, "Drag A onto a road"),
- no route, for example when both points snap to the same junction (404),
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
the User-Agent `scenic-route-planner/0.1 (personal research)`. It sends no
name, email or other personal detail.

## API

All bodies are JSON; POST requests must send `Content-Type:
application/json`. Errors are `{"error": "...", "point": "from" | "to"}`,
with `point` only when the message is about one point. Responses over 1 KB are gzipped when the client accepts
it. Coordinates are `[lat, lon]` with 5 decimals.

`GET /api/health`

```
{"status": "loading" | "ready" | "error", "bbox": [s, w, n, e], "max_snap_m": 2000,
 "edges": 825892, "moments": 3820, "s_breaks": [0.17, 0.377, 0.585, 0.792],
 "budgets": ["+15%", "+30%", "+50%"], "load_s": 0.8, "error": "..."}
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
                 "min_along": 12.3}],
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
converted with the cumulative edge times). `line` is simplified per S run with a tolerance that starts at about
0.5 m and doubles until the route has at most 5,000 points. Run ends stay
exact. The server keeps the last 8 searches for `/api/waypoints`.

`POST /api/waypoints` with `{"id": "<search id>", "key": "r3"}`

```
{"key": "r3", "url": "https://www.google.com/maps/dir/?api=1&origin=...", "url_length": 185,
 "origin": [...], "destination": [...],
 "waypoints": [{"n": 1, "lat": 43.26804, "lon": -77.40274, "road": "CR 1", "tier": 1}, ...],
 "meets_target": true, "follow_target": 0.9,
 "summary": {"waypoints": 3, "overlap": 0.9923, "eta_min": 124.5, ...},
 "notes": [], "seconds": 0.61}
```

`reduce_to_waypoints` runs with `max_waypoints=9` and `follow_target=0.9`.
Results are cached per search and route.

## Notes

- Searches and waypoint reductions run one at a time (one lock), since the
  route context is not known to be thread-safe. A second search waits for the
  first.
- The server refuses requests whose `Host` header is not `127.0.0.1` or
  `localhost`, which keeps DNS-rebinding pages out. POSTs must be JSON, which
  keeps cross-site form posts out.
- Leaflet 1.9.4 loads from unpkg with subresource integrity hashes. The page
  sets a Content-Security-Policy that allows only that CDN and the tile
  servers (tile.openstreetmap.org; server.arcgisonline.com serves both Esri
  layers).
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
