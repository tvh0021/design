# Moment corrections report

No commit, push, demo build, site edit, server stop, or network request was performed. Concurrent web changes belong to the other agent.

## Results by item

1. Fixed. `_name_water` previously exhausted lake and canal polygons before considering river lines. The nearest named line now beats the selected water polygon when geometrically closer and within the existing naming radius. The old moment 1585 becomes Niagara River on I 190 near Lewiston Heights. Existing bay, arm, parent-body and detached-pond tests pass. Files: `src/scenic/moments.py`, `tests/test_moments_v03.py`, `docs/SCORE_SPEC.md`.

2. Fixed. The US span is OSM way 22777387, a trunk road, not a motorway. Its 1,368 m edge has W=0.448397 and V_water=0.653589, below both candidate thresholds. Only 5 of 29 raster samples are wet, and the nearest named river line is over 5 km away. A tagged bridge at least 500 m long with at least 200 m of sampled water crossing can qualify when water is broad and either nearby water or the bridge itself is named. It retains size, stormwater and town checks. The new point is 3.45 m from the Thousand Islands validation point and uses its OSM bridge:name. Motorway water additionally needs this measured long crossing, removing proximity-only water credit. Albany (42.6526,-73.7562) to Rochester (43.1566,-77.6088) fastest route is 212.66 minutes, 363.75 km, and passes zero moments, including zero water moments. Files: moments module, score config, moments tests, score spec.

3. Fixed. Named canal lines were not extracted at all. Waterway relation names now fill unnamed member ways when unambiguous. Canal names also propagate through touching canal endpoints within 1 m. River/stream geometry cannot receive canal endpoint names; streams are not added as named-water exceptions. Erie Canal labels increase from 1 to 50 moments. Gates still reject small, stormwater, built-up, motorway and narrow unnamed water. Files: moments module, moments tests, score spec.

4. Not fixed after two failed boundary approaches, as required by the escalation rule. Edge 500346 is OSM way 37593085, unclassified Road 96, ref 96, source Geobase_Import_2009. Its point (-76.185220,44.235064) is northwest of the nearest local NY boundary point (-76.180925,44.228670), 753 m away. This supports the Wolfe Island, Ontario identification. The graph includes the foreign road and its moment remains. The PBF has NY relation 61320, but osmium produces no assembled state area. Direct-member reconstruction finds 426 of 427 ways and polygonizes only two small island polygons. No incomplete mask was installed. A complete local state boundary or a separately validated boundary reconstruction is still needed. No boundary code or tests were left as a false fix. The limitation is documented in the score spec.

5. Partially fixed. Heads were already the highest-scoring edge, but a shore edge could absorb a bridge within the 2 km / 1 km cluster radii. Long water bridges now anchor clusters before shore edges; their value still comes from their actual edge, without the ordering bonus. V5 improves but remains below 27/30. No validation points, thresholds or radii changed. Other misses remain a limitation of one representative point for a long scenic cluster. V2 is an edge-score contrast test and does not consume moments. NY-414 has gap 0.184 below required 0.25; NY-22 has a faster, more scenic shoreline alternative, CR 22J (0.893 versus stretch 0.721). Neither is a moment-placement defect, so V2 was left unchanged. Files: moments module, moments tests, score spec.

## Validation

| Check | Before | After |
|---|---|---|
| V1 corridors | 7/8 | 6/8 |
| V1 budget bands | 8/8 | 7/8 |
| V2 non-trivial pairs | 0/2 | 0/2 |
| V3 scenic | 10/10 | 10/10 |
| V3 urban | 7/7 | 7/7 |
| V3 flat | 3/3 | 3/3 |
| V4 landscape median | 0.716 | 0.716 |
| V4 byways beating neighbours | 21/25 | 21/25 |
| V5 by point, positives | 7/30 | 9/30 |
| V5 by point, false positives | 0/15 | 0/15 |
| V5 member-edge positives | 29/30 | 30/30 |
| V5 member-edge false positives | 1/15 | 1/15 |

## Checks

V1 regresses from 7/8 to 6/8 corridors and from 8/8 to 7/8 budget-band coverage. Both still meet their 6/8 acceptance floor, but this regression remains a tradeoff to review before shipping.

`.venv/bin/scenic score --force osm_features moments validate validate_routes` completed successfully from `scenic/`. All earlier scoring stages were current; the four requested stages ran.

`scenic/.venv/bin/python -m pytest scenic/tests -q`: 319 passed, 0 failed in 5.30 s. This includes concurrently added web tests. `git diff --check` passed.

## Every changed moment label

Moments: 2506 before, 2547 after. 198 matched old labels changed; 11 old moments have no surviving member edge; 48 new clusters share no member edge with any old moment.

IDs are regenerated. Match each old moment to the new moment with the greatest count of shared member edges. Splits and merges can therefore map several old labels to one new label. This list includes road/place changes from moved representatives, as well as changed feature names. Existing bay/arm naming regression tests pass; this is not a manual geographic audit of every statewide label.

| Old ID | New ID | Before | After |
|---|---|---|---|
| 2 | 44 | Mohawk River on NY 32 near Cohoes | Old Champlain Canal on NY 32 near Cohoes |
| 21 | 63 | Lake Luzerne on CR 4 near Lake Luzerne | Hudson River on CR 4 near Lake Luzerne |
| 23 | 67 | Harlem River on Robert F. Kennedy Bridge near New York | Harlem River on Willis Avenue Bridge near New York |
| 26 | 24 | West Channel on North Loop Road near New York | West Channel on NY 25 near New York |
| 28 | 11 | Hudson River on Riverside Drive near New York | Hudson River on HH;NY 9A near New York |
| 30 | 70 | Jacqueline Kennedy Onassis Reservoir on Riverside Drive near New York | Hudson River on Riverside Drive near New York |
| 31 | 12 | Bank Rock Bay on Amsterdam Avenue near New York | Hudson River on NY 9A near New York |
| 32 | 71 | Jerome Park Reservoir on 9th Avenue near Yonkers | Harlem River Ship Canal on 9th Avenue near Yonkers |
| 33 | 451 | Newtown Creek on FDR near New York | Newtown Creek on Avenue C near New York |
| 36 | 73 | Sheepshead Bay on Emmons Avenue near South Battery | Shell Bank Creek on Emmons Avenue near South Battery |
| 40 | 30 | Coney Island Creek on Shell Road near Todt Hill | Coney Island Creek on BP near Todt Hill |
| 45 | 82 | East Channel on Vernon Boulevard near New York | Newtown Creek on Vernon Boulevard near New York |
| 46 | 27 | Flushing Bay on Marina Road near Harbor Hills | Flushing Creek on I 678 near Harbor Hills |
| 47 | 27 | Flushing Bay on Whitestone Expressway Service Road near Harbor Hills | Flushing Creek on I 678 near Harbor Hills |
| 48 | 83 | Motts Creek on Rockaway Boulevard near Inwood | Hook Creek on Rockaway Boulevard near Inwood |
| 49 | 29 | East Channel on Astoria Park South near New York | Harlem River on I 278 near New York |
| 52 | 29 | Luyster Creek on 20th Avenue near New York | Harlem River on I 278 near New York |
| 59 | 93 | Lake Ontario on NY 18 near Roosevelt Beach | Twelvemile Creek on NY 18 near Roosevelt Beach |
| 65 | 99 | Lake Ontario on NY 18 near Olcott | Eighteenmile Creek on NY 18 near Olcott |
| 79 | 111 | Lower Lake on Front Street near Rensselaer Falls | Oswegatchie River on Front Street near Rensselaer Falls |
| 87 | 13 | Saint Lawrence River on River Road near Ogdensburg | Saint Lawrence River on NY 812 near Ogdensburg |
| 94 | 15 | Lake Erie on Busti Avenue near Buffalo | Lake Erie on QEW near Buffalo |
| 95 | 445 | Lake Erie on NY 5 near Lackawanna | Lake Erie on Fuhrmann Boulevard near Lackawanna |
| 98 | 126 | Hudson River on US 6;US 9;US 202 near Peekskill | MacGregory Brook on US 6;US 9;US 202 near Peekskill |
| 106 | 134 | Cross Lake on River Road near Jones Point | Seneca River on River Road near Jones Point |
| 146 | 173 | Lake Algonquin on CR 5 near Wells | Sacandaga River on CR 5 near Wells |
| 176 | 203 | CR 65 near Mindenville | Erie Canal on CR 65 near Mindenville |
| 184 | 211 | Lake Ontario on Portside Drive near Summerville | Genesee River on Portside Drive near Summerville |
| 192 | 219 | Cross Lake on Plainville Road near Jones Point | State Ditch on Plainville Road near Jones Point |
| 193 | 220 | Lake Ontario on CR 5 near Bethel Corners | Salmon River on CR 5 near Bethel Corners |
| 195 | 222 | Lake Ontario on NY 3 near Bethel Corners | Salmon River on NY 3 near Bethel Corners |
| 199 | 226 | Oswego River on Old Rte 57 near Fulton | Oswego Canal on Old Rte 57 near Fulton |
| 200 | 227 | Lake Neatahwanta on NY 3 near Fulton | Oswego Canal on NY 3 near Fulton |
| 212 | 239 | Seneca Lake on NY 414 near Watkins Glen | Barge Canal on NY 414 near Watkins Glen |
| 224 | 251 | Laurel Lake on Bray Avenue near Laurel | James Creek on Bray Avenue near Laurel |
| 248 | 3 | CR 32 near Ponquogue | Ponquogue Bridge on CR 32 near Ponquogue |
| 260 | 299 | Shinnecock East Bay on Canoe Place Road near Newtown | Shinnecock East Bay on CR 80 near Newtown |
| 284 | 313 | Old Neck Creek on Wavecrest Drive near Old Mastic | Home Creek on Wavecrest Drive near Old Mastic |
| 328 | 359 | Waterport Reservoir on CR 26 near Toms Landing | Oak Orchard River on CR 26 near Toms Landing |
| 332 | 23 | Hessian Lake on US 6;US 9W;US 202 near Fort Clinton | Hudson River on US 6;US 202 near Fort Clinton |
| 333 | 363 | Foundry Cove on Brewerton Road near Garrison | Hudson River on Brewerton Road near Garrison |
| 334 | 364 | Foundry Cove on River Road near Cold Spring | Hudson River on River Road near Cold Spring |
| 337 | 367 | Foundry Cove on NY 218 near Cold Spring | Hudson River on NY 218 near Cold Spring |
| 338 | 450 | South Cove on FDR near New York | South Cove on Whitehall Street near Colonels Row |
| 343 | 373 | Haverstraw Bay on Radnor Avenue near Harmon | Croton River on Radnor Avenue near Harmon |
| 344 | 374 | Hudson River on South Street near Peekskill | MacGregory Brook on South Street near Peekskill |
| 364 | 394 | Island Parkway North near Harbor Isle | Island Park Canal on Island Parkway North near Harbor Isle |
| 387 | 417 | Foundry Cove on CR 17 near Cold Spring | Hudson River on CR 17 near Cold Spring |
| 398 | 428 | Fallkill Creek on Kittredge Place near Poughkeepsie | Fallkill Creek on Mount Carmel Place near Poughkeepsie |
| 402 | 432 | City Ship Canal on Ohio Street near Buffalo | Buffalo River on Ohio Street near Buffalo |
| 403 | 496 | Niagara River on I 190 near Kenmore | Tonawanda Channel on NY 266 near Kenmore |
| 418 | 448 | Hudson Channel on Guy Lombardo Avenue near Freeport | Woodcleft Canal on Guy Lombardo Avenue near Freeport |
| 421 | 479 | Harlem River on HRD near Yonkers | Harlem River on Harlem River Drive near Yonkers |
| 422 | 67 | Harlem River on HRD near New York | Harlem River on Willis Avenue Bridge near New York |
| 423 | 453 | Cayuga Lake on NY 13;NY 34 near Cayuga Heights | Fall Creek on North Cayuga Street near Cayuga Heights |
| 424 | 454 | Hudson River on HH;NY 9A near Yonkers | Hudson River on Margaret Corbin Drive near Yonkers |
| 428 | 19 | The Narrows near Todt Hill | The Narrows on I 278 near Todt Hill |
| 431 | 563 | Shift Creek on M near Point Lookout | Shift Creek on Bay Parkway near Point Lookout |
| 434 | 8 | RM near Captree | Robert Moses Causeway on RM near Captree |
| 436 | 635 | The Narrows on BP near Todt Hill | The Narrows on Shore Road near South Battery |
| 438 | 507 | Irondequoit Bay on NY 104 near Newport | Irondequoit Bay near German Village |
| 440 | 10 | Cross Bay Bridge near Inwood | Cross Bay Bridge on Cross Bay Bridge near Inwood |
| 449 | 475 | Black Lake on US 20;NY 5 near Mud Lock | Seneca River on US 20;NY 5 near Mud Lock |
| 450 | 521 | Hutchinson River on I 95 near Pelham Manor | Hutchinson River on Boston Road near Pelham Manor |
| 454 | 481 | Meadow Lake on Meridian Road near Harbor Hills | Flushing Creek on Meridian Road near Harbor Hills |
| 455 | 779 | Bank Rock Bay on HH;NY 9A near New York | Hudson River near New York |
| 465 | 491 | Ellicott Creek on Taylor Drive near Tonawanda | Little River on Taylor Drive near Tonawanda |
| 466 | 41 | Lake Erie on Erie Street near Buffalo | City Ship Canal on NY 5 near Buffalo |
| 467 | 493 | Niagara River near Kenmore | Black Rock Channel near Kenmore |
| 469 | 566 | Hutchinson River on HR near Pelham Manor | Hutchinson River on Bay Plaza Drive near Pelham Manor |
| 470 | 41 | Lake Erie on Fuhrmann Boulevard near Buffalo | City Ship Canal on NY 5 near Buffalo |
| 479 | 23 | Hessian Lake on NY 9D near Fort Clinton | Hudson River on US 6;US 202 near Fort Clinton |
| 486 | 511 | Irondequoit Bay on NY 404 near LaSalle's Landing | Irondequoit Creek on NY 404 near LaSalle's Landing |
| 490 | 515 | Rockaway Inlet on Flatbush Avenue near Inwood | Gerritsen Creek on Flatbush Avenue near Inwood |
| 494 | 1394 | Sea Dog Creek on L near Point Lookout | Sea Dog Creek near Point Lookout |
| 502 | 528 | Onondaga Lake on I 90 near Long Branch | Onondaga Lake Outlet on John Glenn Boulevard near Long Branch |
| 504 | 529 | Mohawk River on Dyke Road near West Schuyler | Erie Canal on Dyke Road near West Schuyler |
| 510 | 537 | Lake Champlain on US 9 near South Junction | Salmon River on US 9 near South Junction |
| 514 | 541 | Peconic Lake on CR 105 near Riverhead | Peconic River on CR 105 near Riverhead |
| 521 | 548 | Papscanee Creek on Scott Avenue near Castleton-on-Hudson | Hudson River on Scott Avenue near Castleton-on-Hudson |
| 524 | 552 | Stewarts Bridge Reservoir on CR 8 near Conklingville | Sacandaga River on CR 8 near Conklingville |
| 537 | 28 | Hudson River on I 84;NY 52 near Brockway | Hudson River on I 84;NY 52 near Varick Homes |
| 541 | 570 | Tschache Pool on NY 89 near Mays Point | Erie Canal on NY 89 near Mays Point |
| 555 | 584 | West Creek on NY 261 near Payne Beach | Salmon Creek on NY 261 near Payne Beach |
| 557 | 586 | Lake Ontario on NY 18 near Kuckville | Johnson Creek on NY 18 near Kuckville |
| 559 | 588 | Lake Ontario on CR 37 near Point Breeze | Oak Orchard River on CR 37 near Point Breeze |
| 561 | 590 | Lake Erie on NY 266 near Buffalo | Black Rock Channel on NY 266 near Buffalo |
| 563 | 592 | Harris Lake on NY 28N near Newcomb | Hudson River on NY 28N near Newcomb |
| 569 | 598 | Pleasant Lake on Black Creek Road near Sand Ridge | Anthony Cut on Black Creek Road near Sand Ridge |
| 570 | 599 | Caughdenoy Road near Brewerton | Anthony Cut on Caughdenoy Road near Brewerton |
| 576 | 825 | Oneida Lake on I 81 near Brewerton | Oneida River on Kathan Road near Brewerton |
| 582 | 610 | Lake Erie on NY 5 near Dunkirk | Canadaway Creek on NY 5 near Dunkirk |
| 597 | 11 | Hudson River on Broadway near Yonkers | Hudson River on HH;NY 9A near New York |
| 600 | 627 | Lake Ontario on Lake Ontario State Parkway near Sandy Harbour Beach | Sandy Creek on Lake Ontario State Parkway near Sandy Harbour Beach |
| 607 | 634 | Flushing Bay on 127th Street near Saddle Rock | Westchester Creek on 127th Street near Saddle Rock |
| 618 | 645 | Haverstraw Bay on Bowline Plant Road near Haverstraw | Minisceongo Creek on Bowline Plant Road near Haverstraw |
| 619 | 646 | Parsonage Creek on Atlantic Avenue near Baldwin | Milburn Creek on Atlantic Avenue near Baldwin |
| 622 | 649 | Lake Champlain on Lakeshore Road near Chazy Landing | Little Chazy River on Lakeshore Road near Chazy Landing |
| 640 | 667 | Seneca River on NY 38 near Mosquito Point | Erie Canal on NY 38 near Mosquito Point |
| 641 | 668 | Oneida Lake on NY 13 near Verona Beach | Erie Canal on NY 13 near Verona Beach |
| 647 | 674 | Onondaga Lake on Hiawatha Boulevard West near Syracuse | Onondaga Creek on Hiawatha Boulevard West near Syracuse |
| 648 | 827 | Onondaga Lake on I 690 near Solvay | Onondaga Lake on Willis Avenue near Solvay |
| 660 | 687 | Rivière aux Saumons on Dundee Road near Fort Covington | Salmon River on Dundee Road near Fort Covington |
| 669 | 696 | Croton Bay on Old Albany Post Road near Crotonville | Croton River on Old Albany Post Road near Crotonville |
| 671 | 698 | Lake Erie on Lake Shore Road near Highland-on-the-Lake | Eighteen Mile Creek on Lake Shore Road near Highland-on-the-Lake |
| 672 | 699 | Dune Road near Hampton Beach | Quogue Canal on Dune Road near Hampton Beach |
| 691 | 720 | Mill River on Hewlett Point Avenue near Bay Park | Grand Canal on Hewlett Point Avenue near Bay Park |
| 718 | 38 | Lac Saint-François on Haverstock Road near Cornwall Island | Lac Saint-François on Seaway International Bridge near Cornwall Island |
| 733 | 760 | Great Sacandaga Lake on CR 15 near Maple Grove | East Stony Creek on CR 15 near Maple Grove |
| 750 | 777 | Wappinger Lake on Creek Road near Wappingers Falls | Wappinger Creek on Creek Road near Wappingers Falls |
| 758 | 33 | Catskill Creek on NY 23 near Catskill | Hudson River on NY 23 near Hamburg |
| 763 | 2127 | Kirby Pond on I 95 near Purdys Grove | Byram River on Mill Street near Port Chester |
| 764 | 788 | Champlins Creek on NY 27A near Islip | Bull Ditch on NY 27A near Bay Shore |
| 770 | 797 | Carlls River on NY 27A near Babylon | Sampawams Creek on NY 27A near Babylon |
| 786 | 815 | Lake Ontario on CR 48 near Lakeside | Johnson Creek on CR 48 near Lakeside |
| 790 | 819 | Lac Saint-François on Rue St-Régis near St. Regis | Saint Regis River on Rue St-Régis near St. Regis |
| 813 | 843 | Hudson River on CR 96 near Waterford | Old Champlain Canal on CR 96 near Waterford |
| 842 | 25 | Chautauqua Lake on I 86;NY 17 near Phillips Mills | Chautauqua Lake on I 86;NY 17 near Oriental Park |
| 887 | 35 | 3rd Avenue near Saddle Rock | Westchester Creek on I 678 near Saddle Rock |
| 913 | 942 | Hoosic River on NY 67 near Reynolds | Tomhannock Creek on NY 67 near Reynolds |
| 925 | 954 | Seneca Lake on NY 96A near East Geneva | Seneca River on NY 96A near East Geneva |
| 933 | 961 | Lake Algonquin on NY 30 near Wells | Sacandaga River on NY 30 near Wells |
| 969 | 1943 | Onondaga Lake on I 690 near Lakeside | Onondaga Lake on State Fair Boulevard near Lakeside |
| 971 | 998 | Hudson River on US 4 near Fort Miller | Champlain Canal on US 4 near Fort Miller |
| 1009 | 916 | Keuka Lake on NY 54A near Hammondsport | Glen Brook on NY 54A near Hammondsport |
| 1012 | 439 | H near Great River | Heckscher Canal on H near Great River |
| 1036 | 1061 | Mattituck Creek on Bay Avenue near Mattituck | James Creek on Bay Avenue near Mattituck |
| 1048 | 1073 | Lake Bonita on CR 24 near Randall Corner | Hudson River on CR 24 near Randall Corner |
| 1053 | 24 | West Channel on 1st Avenue near New York | West Channel on NY 25 near New York |
| 1068 | 1091 | Mohawk River on NY 5S near Herkimer | Erie Canal on NY 5S near Herkimer |
| 1125 | 40 | New Croton Reservoir on NY 129 near Croton Heights | New Croton Reservoir on Taconic State Parkway near Croton Heights |
| 1139 | 1160 | Kill Van Kull on South Avenue near Todt Hill | Bridge Creek on South Avenue near Todt Hill |
| 1147 | 1888 | Niagara River on I 190 near Ferry Village | Tonawanda Channel on Aqua Lane near Ferry Village |
| 1162 | 1182 | Forebay Canal on Power Authority Service Road near Lewiston Heights | Niagara River on Power Authority Service Road near Lewiston Heights |
| 1294 | 1312 | Chippawa-Queenston Power Canal on Niagara Parkway near Lewiston | Niagara River on Niagara Parkway near Lewiston |
| 1296 | 887 | Harlem River on East 132nd Street near New York | Harlem River on Sunken Meadow Loop near New York |
| 1297 | 77 | Buttermilk Channel on Van Brunt Street near South Battery | Gowanus Canal Flushing Tunnel on Columbia Street near South Battery |
| 1298 | 1314 | Mohawk River on Riverview Road near Dunsbach Ferry | Enlarged Erie Canal on Riverview Road near Dunsbach Ferry |
| 1303 | 1319 | Schoharie Reservoir on NY 990V near West Conesville | Manor Kill on NY 990V near West Conesville |
| 1304 | 454 | Harlem River on Amsterdam Avenue near Yonkers | Hudson River on Margaret Corbin Drive near Yonkers |
| 1310 | 1326 | Upper Ashokan Reservoir on NY 28A near Boiceville | Esopus Creek on NY 28A near Boiceville |
| 1323 | 1338 | Mohawk River on CR 188 near East Creek | Erie Canal on CR 188 near East Creek |
| 1332 | 1347 | Premium Millpond on US 1 near Larchmont | Premium River on US 1 near Larchmont |
| 1343 | 1358 | Feagles Lake on PA 652 near Narrowsburg | Delaware River on PA 652 near Narrowsburg |
| 1356 | 1371 | Lamoka Lake on Westlake Road near Bradford | Mud Creek on Westlake Road near Bradford |
| 1388 | 1406 | Tappan Zee on US 9 near Philipse Manor | Pocantico River on US 9 near Philipse Manor |
| 1549 | 39 | cliff on Walbridge Lane near Roslyn | William Cullen Bryant Viaduct on NY 25A near Roslyn |
| 1555 | 1570 | cliff on Tri Harbor Court near Flower Hill | cliff on Harbor Park Drive near Roslyn |
| 1585 | 1600 | Chippawa-Queenston Power Canal on I 190 near Lewiston Heights | Niagara River on I 190 near Lewiston Heights |
| 1601 | 1616 | Cayuga Lake on NY 34B near Myers | Salmon Creek on NY 34B near Myers |
| 1650 | 39 | cliff on Wittes Lane near Roslyn | William Cullen Bryant Viaduct on NY 25A near Roslyn |
| 1698 | 1712 | Haverstraw Bay on Croton Point Avenue near Harmon | Croton River on Croton Point Avenue near Harmon |
| 1750 | 30 | Coney Island Creek on Shore Parkway near Todt Hill | Coney Island Creek on BP near Todt Hill |
| 1756 | 307 | Aspatuck River on Beach Lane near Westhampton Beach | Quantuck Canal on Beach Lane near Westhampton Beach |
| 1815 | 1826 | Premium Millpond on Dillon Road near Larchmont | Premium River on Dillon Road near Larchmont |
| 1851 | 1862 | East Rockaway Inlet at Reynolds Channel on Lawrence Avenue near Lawrence | Crooked Creek on Lawrence Avenue near Lawrence |
| 1852 | 1863 | Lake Ontario on Porter Avenue near Youngstown | Niagara River on Porter Avenue near Youngstown |
| 1879 | 1891 | Hudson River on US 4 near Fort Miller | Champlain Canal on US 4 near Fort Miller |
| 1882 | 1894 | York Lake on NY 97 near Minisink Ford | Delaware River on NY 97 near Minisink Ford |
| 1915 | 1927 | Lake Champlain on NY 22 near Ticonderoga | La Chute River on NY 22 near Ticonderoga |
| 1949 | 2000 | Lake Erie on Main Street near Buffalo | Buffalo City Hall Observation Deck on NY 384 near Buffalo |
| 1982 | 6 | viewpoint on Furman Street near New York | FDR Drive on FDR near New York |
| 2045 | 2058 | Little Bay on NY 25 near Orient Point | Narrow River on NY 25 near Orient Point |
| 2077 | 2090 | Hinckley Reservoir on Harvey Bridge Road near Ohio | West Canada Creek on Harvey Bridge Road near Ohio |
| 2094 | 2108 | Lac Saint-François on NY 37 near Rooseveltown | Raquette River on NY 37 near Rooseveltown |
| 2122 | 2136 | Lake Roxanne on US 11 near Ellenburg Depot | North Branch Great Chazy River on US 11 near Ellenburg Depot |
| 2124 | 2138 | Delta Reservoir on CR 53 near Westernville | Mohawk River on CR 53 near Westernville |
| 2137 | 2151 | Upper Lake on NY 68 near Canton | Grasse River on NY 68 near Canton |
| 2154 | 35 | Westchester Creek on Ferry Point Park Service Road near Saddle Rock | Westchester Creek on I 678 near Saddle Rock |
| 2163 | 2176 | Crooked Creek on Woodmere Boulevard near Hewlett Neck | Auerbach Canal on Woodmere Boulevard near Hewlett Neck |
| 2216 | 2228 | Mud Lake on NY 370 near Plainville | Seneca River on NY 370 near Plainville |
| 2240 | 2252 | Union Falls Pond on CR 48 near Franklin Falls | Saranac River on CR 48 near Franklin Falls |
| 2256 | 2268 | Brant Lake on CR 33 near Brant Lake | Spuytenduivel Brook on CR 33 near Brant Lake |
| 2264 | 2276 | Senix Creek on Belleview Avenue near Bellview Beach | Terrell River on Belleview Avenue near Bellview Beach |
| 2279 | 2291 | Tschache Pool on CR 105 near Munsons Corner | Erie Canal on CR 105 near Munsons Corner |
| 2280 | 2292 | Pound Brook on CR 344 near Alloway | Erie Canal on CR 344 near Alloway |
| 2299 | 2311 | Motts Creek on Brookville Boulevard near North Woodmere | Hook Creek on Brookville Boulevard near North Woodmere |
| 2302 | 2314 | Hessian Lake on US 9W;US 202 near Fort Clinton | Hudson River on US 9W;US 202 near Fort Clinton |
| 2328 | 2355 | CR 24 near Hulberton | Erie Canal on CR 24 near Hulberton |
| 2341 | 2373 | Cannonsville Reservoir on NY 10 near Stilesville | West Branch Delaware River on NY 10 near Stilesville |
| 2357 | 2389 | Lake Tappan on CR 26 near Nauraushaun | Hackensack River on CR 26 near Nauraushaun |
| 2393 | 2427 | CR 7 near Port Gibson | Erie Canal on CR 7 near Port Gibson |
| 2394 | 2430 | NY 31 near Port Gibson | Erie Canal on NY 31 near Port Gibson |
| 2396 | 2432 | CR 206 near Wayneport | Erie Canal on CR 206 near Wayneport |
| 2397 | 2435 | CR 224 near Hydesville | Erie Canal on CR 224 near Hydesville |
| 2413 | 2452 | Cook Pond near Cook Corners | Oswegatchie River near Cook Corners |
| 2414 | 2453 | Chaumont Pond on River Road near Cook Corners | Oswegatchie River on River Road near Cook Corners |
| 2418 | 825 | Oneida Lake on CR 37 near Brewerton | Oneida River on Kathan Road near Brewerton |
| 2432 | 2470 | CR 175 near Spencerport | Erie Canal on CR 175 near Spencerport |
| 2460 | 2499 | Black Lake on NY 58 near Pope Mills | Fish Creek on NY 58 near Pope Mills |
| 2469 | 2508 | New Croton Reservoir on Croton Lake Road near Stanwood | Kisco River on Croton Lake Road near Stanwood |
| 2471 | 2428 | Irondequoit Creek on NY 96 near Bushnell's Basin | Erie Canal on NY 96 near Bushnell's Basin |
| 2473 | 2345 | CR 175 near Adams Basin | Erie Canal on CR 212 near Adams Basin |
| 2476 | 2513 | Smith Pond on Valley Farm Road near Pottersville | Schroon River on Valley Farm Road near Pottersville |
| 2480 | 2518 | River Street near Oriskany | Erie Canal on River Street near Oriskany |
| 2481 | 2519 | South James Street near Rome | Erie Canal on South James Street near Rome |
| 2497 | 2538 | Ganargua Creek on NY 31 near Macedon | Erie Canal on NY 31 near Macedon |
| 2502 | 2543 | Waterport Reservoir on CR 66 near Platten | Oak Orchard River on CR 66 near Platten |

## Removed clusters with no surviving member edges

- Shift Creek on L near Point Lookout
- Goose Creek on W near Bellmore
- Rockaway Inlet on BP near South Battery
- Allegheny Reservoir on I 86;NY 17 near Cold Spring
- Hudson River on NJ 495 near New York
- Fresh Kills on NY 440 near Todt Hill
- Black Lake on I 90 near Montezuma
- Hudson River on I 87 near Fernwood
- Chautauqua Lake on I 86;NY 17 near Hadley Bay
- Mill Basin on Belt Parkway near Inwood
- Chemung River on I 86;NY 17 near Chemung

## Added clusters with no old member edges

- Great South Bay Bridge on RM near Captree
- Throgs Neck Bridge on I 295 near Saddle Rock
- Williamsburg Bridge on Williamsburg Bridge near New York
- Thousand Islands Bridge on I 81 near Collins Landing
- Gowanus Canal on I 278 near South Battery
- Strongs Creek on Shore Road near Lindenhurst
- Brightwater Canal on NY 27A near Brightwaters
- Champlain Canal on NY 196 near Hudson Falls
- Island Park Canal on Long Beach Boulevard near Island Park
- Champlain Canal on NY 149 near Kingsbury
- Champlain Canal on Baldwin Corners Road near Baldwin Corner
- Strongs Creek on Bayview Avenue West near Copiague Harbor
- Gowanus Canal on Columbia Street near South Battery
- Gowanus Canal on 5th Avenue near South Battery
- Canal Grande on Montauk Highway near Copiague Harbor
- Gowanus Canal near South Battery
- Patchogue River on Blue Point Avenue near Blue Point
- Quogue Canal on Quogue Street near Quogue
- Erie Canal on CR 130 near Pendleton
- Champlain Canal on US 4 near Fort Ann
- Erie Canal on CR 77 near Nottingham Estates
- Erie Canal on CR 79 near Eagle Harbor
- Erie Canal on NY 31E near Shelby Basin
- Erie Canal on CR 118 near Orangeport
- Erie Canal on CR 210 near Spencerport
- Erie Canal on CR 208 near South Greece
- Erie Canal on CR 35 near Knowlesville
- Erie Canal on Butts Road near Albion
- Erie Canal on Keitel Road near Richs Corners
- Erie Canal on CR 64 near Hinsburg
- Erie Canal on CR 54 near Holley
- Erie Canal on NY 387 near Brockville
- Erie Canal on Gallup Road near Adams Basin
- Erie Canal on NY 260 near Garland
- Erie Canal on CR 11 near Richs Corners
- Erie Canal on CR 88 near Gaines Basin
- Erie Canal on Carmen Road near Middleport
- Erie Canal on East Canal Road near Garland
- Erie Canal on NY 237 near Holley
- Erie Canal near Mud Mills
- Erie Canal on Canandaigua Road near North Macedon
- Erie Canal on Galloway Road near East Palmyra
- Erie Canal on NY 31 near Bushnell's Basin
- Erie Canal on West Canal Road near Brockport
- Erie Canal on NY 46 near New London
- Erie Canal on NY 19 Truck;NY 31 near Brockport
- Erie Canal on NY 31 near Holley
- Erie Canal on CR 77 near Carlisle Gardens

## Workspace status

Full status includes concurrent web-agent changes. Only the five files in the implementation diff below belong to this task.

```text
 M config/score.toml
 M docs/PUBLISH.md
 M docs/SCORE_SPEC.md
 M docs/WEB.md
 M src/scenic/moments.py
 M src/scenic/web/server.py
 M src/scenic/web/static/app.css
 M src/scenic/web/static/app.js
 M src/scenic/web/static/index.html
 M tests/test_moments_v03.py
 M tests/test_web.py
?? docs/MOMENTS_FIX_REPORT.md
?? src/scenic/web/static/vendor/spectral/
```

Implementation diff before this report was written:

```text
 scenic/config/score.toml         |   2 +
 scenic/docs/SCORE_SPEC.md        |  39 ++++++++++++
 scenic/src/scenic/moments.py     | 129 +++++++++++++++++++++++++++++++--------
 scenic/tests/test_moments_v03.py |  57 +++++++++++++++++
 4 files changed, 201 insertions(+), 26 deletions(-)
```
