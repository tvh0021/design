# Phase 0 results: Google Maps handoff

Checked by hand in desktop Google Maps on 2026-11-02, around 7 pm (light
traffic). Links are in `phase0.md`.

| Route | Google | Google ratio | Ours | Ours ratio | OSRM | Ours / Google |
|---|---|---|---|---|---|---|
| Fastest (no waypoints) | 87 min, 87.2 mi | 1.00 | 84 min, 86.9 mi | 1.00 | 108 min | 0.97 |
| A: back roads, 9 waypoints | 150 min, 105 mi | 1.72 | 131 min | 1.55 | 152 min | 0.87 |
| B: back roads, 5 waypoints | 138 min, 105 mi | 1.59 | 125 min | 1.49 | 146 min | 0.91 |
| C: Thruway to Geneva, then 5&20 | 127 min, 103 mi | 1.46 | 112 min | 1.32 | 137 min | 0.88 |
| D: A plus one, 10 waypoints | 149 min, 106 mi | 1.71 | – | – | 152 min | – |

"Ours" is the statewide graph's fastest route between consecutive waypoints.

## Findings

- **Budget.** Only C fits the +50% cap, and nothing hits the +30% target. A
  +30% route on this trip has about 26 extra minutes: mostly Thruway with one
  5&20 stretch.
- **Our ETAs.** On the interstate route we are within 3% of Google. On back
  roads we are 9–13% too fast, likely because there is no delay for traffic
  lights, turns and town centers. That moved C from 1.46 (Google) to 1.32 (ours),
  so the error lands right on the budget line. Fix before the λ search: slow
  non-motorway roads, or add per-signal and per-town delays, fitted to these
  numbers and checked on more routes.
- **OSRM.** Matches Google on back-road routes (within 1–8%), but is 24% slow
  on the interstate route, so its ratios are understated. It is a fair check
  for absolute back-road times, not for ratios.
- **Waypoint limit.** Google appeared to accept all 10 waypoints in D (user
  did not count them). Keep the cap at 9 until confirmed.
- **Route following.** In B (5 waypoints) Google left 5&20 west of Geneva, went
  north through West Junius to Auburn, and skipped Geneva and Seneca Falls. The
  gap between the waypoint west of Geneva and the one east of Auburn is about
  60 km with a faster road beside it. The waypoint step must check every gap
  this way, not just count waypoints.
