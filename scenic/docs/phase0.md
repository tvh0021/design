# Phase 0: Google Maps handoff test

Rochester to Syracuse on 5&20, past the north ends of the Finger Lakes.
`scripts/phase0_handoff.py` writes this file. Rerunning it overwrites the checklist, so copy your answers somewhere first.

- Origin: downtown Rochester (OSM place node), 43.15729,-77.61521.
- Destination: downtown Syracuse (OSM place node), 43.04812,-76.14742.
- Budget from the plan: 1.3x the fastest time is the target, 1.5x the cap.

## Results from the OSRM demo server

| Variant | Waypoints | OSRM min | Ratio to fastest | km | Share on 5&20 / NY-5 | Share on planned roads |
|---|---|---|---|---|---|---|
| Fastest (no waypoints) | 0 | 108 | 1.00 | 141 | 0% | n/a |
| A: Back roads, 9 waypoints | 9 | 152 | 1.40 (over target) | 161 | 50% | 74% |
| B: Back roads, 5 waypoints | 5 | 146 | 1.35 (over target) | 160 | 51% | 70% |
| C: Thruway to Geneva, then 5&20 | 4 | 137 | 1.26 | 162 | 36% | 85% |
| D: Limit test: A plus a 10th waypoint | 10 | 152 | 1.40 (over target) | 161 | 50% | 74% |

How the shares are measured: each stretch of the OSRM route is matched to OSM ways by node ID. "5&20 / NY-5" means the way's ref includes US 20 or NY 5. "Planned roads" adds the connectors the variant means to use: NY-96, NY-332 and NY-321 for A, B and D; I-490, I-90, NY-14 and NY-321 for C. Counting from OSRM's step names instead gives lower 5&20 / NY-5 shares (A 47%, B 47%, C 31%, D 47%), because OSRM leaves the NY-5 expressway at Camillus unnamed.

OSRM stands in for Google here. It does not prove what Google will draw.

## Links

- Fastest, for Google's baseline ETA: [open in Google Maps](https://www.google.com/maps/dir/?api=1&origin=43.15729,-77.61521&destination=43.04812,-76.14742&travelmode=driving)
- A, Back roads, 9 waypoints: [open in Google Maps](https://www.google.com/maps/dir/?api=1&origin=43.15729,-77.61521&destination=43.04812,-76.14742&travelmode=driving&waypoints=43.07109,-77.49198%7C43.03791,-77.45410%7C42.99236,-77.42724%7C42.86230,-77.18485%7C42.87303,-76.97127%7C42.93097,-76.78126%7C42.93991,-76.49489%7C42.96821,-76.42691%7C43.03570,-76.34214) (311 characters)
- B, Back roads, 5 waypoints: [open in Google Maps](https://www.google.com/maps/dir/?api=1&origin=43.15729,-77.61521&destination=43.04812,-76.14742&travelmode=driving&waypoints=42.99236,-77.42724%7C42.86230,-77.18485%7C42.93991,-76.49489%7C42.96821,-76.42691%7C43.03570,-76.34214) (227 characters)
- C, Thruway to Geneva, then 5&20: [open in Google Maps](https://www.google.com/maps/dir/?api=1&origin=43.15729,-77.61521&destination=43.04812,-76.14742&travelmode=driving&waypoints=42.87303,-76.97127%7C42.93991,-76.49489%7C42.96821,-76.42691%7C43.03570,-76.34214) (206 characters)
- D, Limit test: A plus a 10th waypoint: [open in Google Maps](https://www.google.com/maps/dir/?api=1&origin=43.15729,-77.61521&destination=43.04812,-76.14742&travelmode=driving&waypoints=43.07109,-77.49198%7C43.03791,-77.45410%7C42.99236,-77.42724%7C42.86230,-77.18485%7C42.87303,-76.97127%7C42.90981,-76.81726%7C42.93097,-76.78126%7C42.93991,-76.49489%7C42.96821,-76.42691%7C43.03570,-76.34214) (332 characters)

## Checklist

Open each link in desktop Google Maps. Write down what you see.

| Link | Google accepted all waypoints? | Route follows 5&20? | Google's ETA | Notes |
|---|---|---|---|---|
| Fastest | n/a | n/a | | |
| A (9 waypoints) | | | | |
| B (5 waypoints) | | | | |
| C (4 waypoints) | | | | |
| D (10 waypoints) | | | | |

For D, also note which stop Google drops or what error it shows, if any.

## Waypoints

| Key | Stretch | Point | Road in OSM | Nearest junction | In variants |
|---|---|---|---|---|---|
| S1 | NY-96 East Jefferson Rd, east of Pittsford | 43.07109,-77.49198 | East Jefferson Road (NY 96) | 806 m | A, D |
| S2 | NY-96, Bushnell's Basin to Eastview | 43.03791,-77.45410 | Pittsford-Victor Road (NY 96) | 314 m | A, D |
| S3 | NY-96, south of Eastview toward Victor | 42.99236,-77.42724 | Victor-Pittsford Road (NY 96) | 399 m | A, B, D |
| S4 | 5&20, Canandaigua to Geneva | 42.86230,-77.18485 | Routes 5 and 20 (US 20;NY 5) | 615 m | A, B, D |
| S5 | 5&20, Geneva lakefront (Seneca Lake) | 42.87303,-76.97127 | Waterloo-Geneva Road (US 20;NY 5) | 696 m | A, C, D |
| S6 | 5&20, Waterloo to Seneca Falls | 42.90981,-76.81726 | Fall Street (US 20;NY 5;NY 414) | 571 m | D |
| S7 | 5&20, east of Seneca Falls toward the Cayuga Lake crossing | 42.93097,-76.78126 | Auburn Road (US 20;NY 5) | 800 m | A, D |
| S8 | US-20, Auburn to Skaneateles | 42.93991,-76.49489 | East Genesee Street (US 20) | 368 m | A, B, C, D |
| S9 | NY-321, just north of Skaneateles | 42.96821,-76.42691 | State Street Road (NY 321) | 461 m | A, B, C, D |
| S10 | NY-5 West Genesee Tpk, west of Camillus | 43.03570,-76.34214 | West Genesee Turnpike (NY 5) | 353 m | A, B, C, D |
| S11 | NY-5 West Genesee St, west side of Syracuse | 43.05078,-76.19231 | West Genesee Street (NY 5) | 91 m | none (optional) |

Rules for each point:

- On the target road, at least 300 m in a straight line from any junction with another named road.
- No road runs alongside within 50 m, and no named road passes within 50 m (bridges, overpasses).
- Not on a one-way carriageway or a motorway, so Google cannot snap to the wrong direction.
- Road geometry comes from OpenStreetMap through the Overpass API.

## Where OSRM leaves the planned roads

Named roads off the plan, by OSRM step name, longest first. Unnamed ramps are left out.

- A: I 490 6.8 km; Genesee Street 5.8 km; I 690 5.4 km; NY 31 4.6 km; I 590 2.0 km.
- B: I 490 22.8 km; Genesee Street 5.8 km; I 690 5.4 km; NY 695 1.7 km; West Washington Street 0.5 km.
- C: Genesee Street 5.8 km; I 690 5.4 km; NY 695 1.7 km; West Washington Street 0.5 km.
- D: I 490 6.8 km; Genesee Street 5.8 km; I 690 5.4 km; NY 31 4.6 km; I 590 2.0 km.

## Notes

- Every variant leaves downtown Rochester on I-490. A leaves it after about 5 km (then I-590 and Monroe Avenue to Pittsford). B stays on it to Victor. C stays on it to the Thruway, as intended. No spot on East Avenue (NY-96 in the city) meets the 300 m rule.
- NY-332 (Farmington to Canandaigua) is mapped as a divided road for its whole length, so no waypoint goes there. A NY-96 point near Victor (S3) is enough: OSRM then takes NY-96 and NY-332 into Canandaigua on its own.
- A keeps to NY-96 with two points (S1, S2). Without S2, OSRM jumps onto I-490 from Bushnell's Basin to Eastview. Even with it, OSRM uses 1.5 km of I-490 between S1 and S2; the stretch has only one spot that meets the rules. B has no NY-96 point before Victor, so it runs on I-490 from Rochester to Victor.
- The Geneva point (S5) sits on the Seneca Lake shore. A point further east lets OSRM cut through Border City Road and skip the lakefront.
- Skaneateles needs two points: US-20 between Auburn and Skaneateles (S8) and NY-321 north of the village (S9). With S9 alone, OSRM goes from Auburn by Franklin Street, Old Seneca Turnpike and Jewett Road and skips Skaneateles. NY-321 has no valid spot closer to the village.
- Into Syracuse, OSRM takes the NY-5 expressway, then NY-695 and I-690 for the last ~7 km instead of West Genesee Street. A point on West Genesee Street (S11) would stop that, but its best spot is ~90 m from a cross street. Add it by hand if Google also takes I-690.
- "Genesee Street" in the lists above is 5&20 through Auburn. OSRM's steps carry no ref there, and only part of it matches by node, so the 5&20 shares run a little low.
- D drives the same roads as A in OSRM; its extra point (S6) changes nothing there. It only tests whether desktop Google Maps takes a 10th waypoint (the documented limit is 9).
