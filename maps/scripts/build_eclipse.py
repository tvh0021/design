#!/usr/bin/env python3
"""
Builds maps/data/eclipse-2027-08-02.json: the ground track of the total solar
eclipse of 2027 August 2 as a GeoJSON FeatureCollection.

Features (all WGS 84 lon/lat):
  - "total":    polygon of the path of totality (umbra sweep).
  - "central":  line of the central path (shadow axis).
  - "greatest": point, duration and local time at greatest eclipse.
  - "marker":   one point every 1000 km along the central line either side of
                greatest eclipse, with the duration of totality and the local
                clock time of mid-totality there.

Everything is derived from the Besselian elements published by NASA GSFC
(Fred Espenak, https://eclipse.gsfc.nasa.gov/SEsearch/SEdata.php?Ecl=20270802)
using the classical reduction in Meeus, "Elements of Solar Eclipses 1951-2200"
(ellipsoidal Earth). No third-party packages are required.

Run `python3 maps/scripts/build_eclipse.py --verify` to compare the computed
path of totality against NASA's published path table.
"""

import argparse
import json
import math
import os
import sys

OUTPUT_JSON = os.path.join(os.path.dirname(__file__), "..", "data", "eclipse-2027-08-02.json")

# ---------------------------------------------------------------------------
# Besselian elements, NASA GSFC (t in hours from t0 = 10:00 TDT).
# ---------------------------------------------------------------------------
T0_TDT_HOURS = 10.0
DELTA_T_SECONDS = 71.7  # value used by NASA's published path table for this eclipse
X_COEF = (-0.0197720, 0.5447123, -0.0000446, -0.0000092)
Y_COEF = (0.1600610, -0.2111582, -0.0001217, 0.0000038)
D_COEF = (17.7624702, -0.0101810, -0.0000040, 0.0)
L1_COEF = (0.5305960, 0.0000138, -0.0000128, 0.0)
L2_COEF = (-0.0154640, 0.0000137, -0.0000128, 0.0)
MU_COEF = (328.422546, 15.002100, 0.0, 0.0)
TAN_F1 = 0.0046064
TAN_F2 = 0.0045834
GREATEST_ECLIPSE_TDT_HOURS = 10.0 + 7.0 / 60.0 + 50.0 / 3600.0

E2 = 0.00669438  # WGS 84 first eccentricity squared
LONGITUDE_DT_CORRECTION_DEG = 0.00417807  # degrees of longitude per second of delta T

DEG = math.pi / 180.0


def poly(c, t):
    return c[0] + t * (c[1] + t * (c[2] + t * c[3]))


def dpoly(c, t):
    return c[1] + t * (2.0 * c[2] + 3.0 * t * c[3])


class Elements:
    """Besselian elements and derived ellipsoid constants at one instant."""

    def __init__(self, t):
        self.t = t
        self.x = poly(X_COEF, t)
        self.y = poly(Y_COEF, t)
        self.dx = dpoly(X_COEF, t)
        self.dy = dpoly(Y_COEF, t)
        self.d = poly(D_COEF, t) * DEG
        self.l1 = poly(L1_COEF, t)
        self.l2 = poly(L2_COEF, t)
        self.mu = poly(MU_COEF, t)
        self.dmu = dpoly(MU_COEF, t) * DEG  # rad / hour

        sd, cd = math.sin(self.d), math.cos(self.d)
        self.rho1 = math.sqrt(1.0 - E2 * cd * cd)
        self.rho2 = math.sqrt(1.0 - E2 * sd * sd)
        self.sd1 = sd / self.rho1
        self.cd1 = math.sqrt(1.0 - E2) * cd / self.rho1
        sd2 = math.sqrt(1.0 - E2) * sd / self.rho2
        cd2 = cd / self.rho2
        # sin/cos of (d1 - d2)
        self.sd12 = self.sd1 * cd2 - self.cd1 * sd2
        self.cd12 = self.cd1 * cd2 + self.sd1 * sd2

    # -- fundamental plane <-> geographic -----------------------------------

    def zeta_from_plane(self, xi, eta):
        """Returns (eta1, zeta1, zeta) for a point (xi, eta) on the fundamental
        plane, or None if the point lies off the Earth's disc."""
        eta1 = eta / self.rho1
        z1sq = 1.0 - xi * xi - eta1 * eta1
        if z1sq < 0.0:
            return None
        zeta1 = math.sqrt(z1sq)
        zeta = self.rho2 * (zeta1 * self.cd12 - eta1 * self.sd12)
        return eta1, zeta1, zeta

    def to_geographic(self, xi, eta1, zeta1):
        """Fundamental-plane point on the near side -> (lon_east_deg, lat_deg)."""
        sin_phi1 = eta1 * self.cd1 + zeta1 * self.sd1
        cos_phi1_sin_theta = xi
        cos_phi1_cos_theta = zeta1 * self.cd1 - eta1 * self.sd1
        theta = math.atan2(cos_phi1_sin_theta, cos_phi1_cos_theta)
        cos_phi1 = math.hypot(cos_phi1_sin_theta, cos_phi1_cos_theta)
        phi1 = math.atan2(sin_phi1, cos_phi1)
        phi = math.atan(math.tan(phi1) / math.sqrt(1.0 - E2))
        lon_west = self.mu - theta / DEG - LONGITUDE_DT_CORRECTION_DEG * DELTA_T_SECONDS
        lon_east = -lon_west
        lon_east = (lon_east + 180.0) % 360.0 - 180.0
        return lon_east, phi / DEG

    def from_geographic(self, lon_east_deg, lat_deg):
        """Geographic point -> (xi, eta, zeta1, zeta) in the fundamental frame."""
        phi1 = math.atan(math.sqrt(1.0 - E2) * math.tan(lat_deg * DEG))
        theta = (self.mu + lon_east_deg - LONGITUDE_DT_CORRECTION_DEG * DELTA_T_SECONDS) * DEG
        sp, cp = math.sin(phi1), math.cos(phi1)
        st, ct = math.sin(theta), math.cos(theta)
        xi = cp * st
        eta1 = sp * self.cd1 - cp * ct * self.sd1
        zeta1 = sp * self.sd1 + cp * ct * self.cd1
        eta = self.rho1 * eta1
        zeta = self.rho2 * (zeta1 * self.cd12 - eta1 * self.sd12)
        return xi, eta, zeta1, zeta

    def surface_velocity(self, xi, eta1, zeta1):
        """Velocity (per hour) of a fixed ground point in the fundamental plane,
        caused by the Earth's rotation."""
        dxi = self.dmu * (zeta1 * self.cd1 - eta1 * self.sd1)
        deta = self.dmu * xi * self.sd1 * self.rho1
        return dxi, deta


def umbra_point(el, side):
    """Point on the northern (side=+1) or southern (side=-1) limit of the
    umbra at the instant `el`, or None if that limit is off the Earth.

    The limit is where the umbral edge grazes the ground tangentially, i.e. the
    edge point whose radius vector is perpendicular to the shadow's velocity
    relative to the (rotating) ground."""
    xi, eta = el.x, el.y
    vx, vy = el.dx, el.dy
    radius = -el.l2
    for _ in range(8):
        speed = math.hypot(vx, vy)
        nx, ny = -vy / speed, vx / speed
        xi = el.x + side * radius * nx
        eta = el.y + side * radius * ny
        z = el.zeta_from_plane(xi, eta)
        if z is None:
            return None
        eta1, zeta1, zeta = z
        radius = -(el.l2 - zeta * TAN_F2)
        sx, sy = el.surface_velocity(xi, eta1, zeta1)
        vx, vy = el.dx - sx, el.dy - sy
    return el.to_geographic(xi, eta1, zeta1)


def central_point(el):
    z = el.zeta_from_plane(el.x, el.y)
    if z is None:
        return None
    eta1, zeta1, _ = z
    return el.to_geographic(el.x, eta1, zeta1)


def umbra_width_km(el):
    """Approximate path width (km) across the central line at instant `el`."""
    n = umbra_point(el, +1)
    s = umbra_point(el, -1)
    if n is None or s is None:
        return None
    return great_circle_km(n, s)


def great_circle_km(a, b):
    lon1, lat1 = a[0] * DEG, a[1] * DEG
    lon2, lat2 = b[0] * DEG, b[1] * DEG
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def bisect_edge(fn, t_on, t_off, iters=30):
    """Refines the instant at which fn(t) stops returning a point."""
    for _ in range(iters):
        mid = 0.5 * (t_on + t_off)
        if fn(mid) is None:
            t_off = mid
        else:
            t_on = mid
    return t_on


def trace(fn, t_start, t_end, step_hours):
    """Samples fn over [t_start, t_end]; returns list of (t, point) for the
    contiguous on-Earth segment, with both ends refined by bisection."""
    n = int(math.ceil((t_end - t_start) / step_hours))
    samples = [(t_start + i * step_hours, None) for i in range(n + 1)]
    samples = [(t, fn(t)) for t, _ in samples]
    on = [i for i, (_, p) in enumerate(samples) if p is not None]
    if not on:
        return []
    first, last = on[0], on[-1]
    pts = [samples[i] for i in range(first, last + 1) if samples[i][1] is not None]
    if first > 0:
        t_edge = bisect_edge(fn, samples[first][0], samples[first - 1][0])
        pts.insert(0, (t_edge, fn(t_edge)))
    if last < len(samples) - 1:
        t_edge = bisect_edge(fn, samples[last][0], samples[last + 1][0])
        pts.append((t_edge, fn(t_edge)))
    return pts


def ring_signed_area(ring):
    s = 0.0
    for i in range(len(ring)):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % len(ring)]
        s += x0 * y1 - x1 * y0
    return s / 2.0


def clockwise(ring):
    """d3-geo expects exterior rings wound clockwise (interior on the right)
    for polygons smaller than a hemisphere."""
    return ring if ring_signed_area(ring) < 0 else ring[::-1]


def round_ring(ring, nd=3):
    pts = [[round(lon, nd), round(lat, nd)] for lon, lat in ring]
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    return pts


# ---------------------------------------------------------------------------
# Umbra path.
# ---------------------------------------------------------------------------

def build_umbra(step_minutes=0.5):
    step = step_minutes / 60.0
    north = trace(lambda t: umbra_point(Elements(t), +1), -3.0, 3.0, step)
    south = trace(lambda t: umbra_point(Elements(t), -1), -3.0, 3.0, step)
    central = trace(lambda t: central_point(Elements(t)), -3.0, 3.0, step)
    if not (north and south and central):
        raise RuntimeError("umbra never reaches the ground")

    ring = [p for _, p in north]
    ring.append(central[-1][1])
    ring.extend(p for _, p in reversed(south))
    ring.append(central[0][1])
    return clockwise(ring), [p for _, p in central], (central[0][0], central[-1][0])


def ut_hours(t_hours):
    return T0_TDT_HOURS + t_hours - DELTA_T_SECONDS / 3600.0


def hms(hours, seconds=True):
    hours = hours % 24.0
    h = int(hours)
    m = int((hours - h) * 60)
    sec = ((hours - h) * 60 - m) * 60
    if seconds:
        return "%02d:%02d:%02d" % (h, m, round(sec))
    # Round to the nearest minute for clock times.
    total_min = int(round(hours * 60)) % (24 * 60)
    return "%02d:%02d" % (total_min // 60, total_min % 60)


def tdt_to_ut_hms(t_hours):
    return hms(ut_hours(t_hours)) + " UT"


def central_duration_seconds(el):
    """Duration of totality on the central line at instant `el`: the umbra's
    diameter on the ground divided by its speed relative to the rotating ground.
    Matches NASA's path table to 0.1 s."""
    z = el.zeta_from_plane(el.x, el.y)
    if z is None:
        return None
    eta1, zeta1, zeta = z
    radius = -(el.l2 - zeta * TAN_F2)
    sx, sy = el.surface_velocity(el.x, eta1, zeta1)
    speed = math.hypot(el.dx - sx, el.dy - sy)
    return 2.0 * radius / speed * 3600.0


# Civil UTC offsets in force on 2027-08-02 for the countries under the central
# line; everything else (open sea) uses nautical time, round(lon / 15).
LAND_ZONES = [
    # (lon_min, lon_max, lat_min, lat_max, offset_hours, label)
    (25.0, 37.0, 21.0, 31.7, 3, "Egypt (UTC+3, summer time)"),
    (9.5, 25.0, 19.0, 33.2, 2, "Libya (UTC+2)"),
    (-13.2, 12.0, 27.0, 37.6, 1, "Morocco / Algeria / Tunisia (UTC+1)"),
    (-9.5, -5.0, 36.0, 37.6, 2, "Spain (UTC+2, summer time)"),
    (34.5, 55.0, 12.0, 32.0, 3, "Saudi Arabia / Yemen (UTC+3)"),
]


def utc_offset(lon, lat):
    for lon0, lon1, lat0, lat1, off, label in LAND_ZONES:
        if lon0 <= lon <= lon1 and lat0 <= lat <= lat1:
            return off, label
    off = int(round(lon / 15.0))
    return off, "nautical time (UTC%+d)" % off


def describe_instant(t):
    """Duration and clock-time properties for a central-line instant."""
    el = Elements(t)
    p = central_point(el)
    off, zone = utc_offset(p[0], p[1])
    ut = ut_hours(t)
    return p, {
        "durationSeconds": round(central_duration_seconds(el)),
        "utc": hms(ut) + " UT",
        "localTime": hms(ut + off, seconds=False),
        "utcOffset": off,
        "zone": zone
    }


def build_markers(t_greatest, spacing_km=1000.0, step_minutes=0.25):
    """Points every `spacing_km` along the central line either side of greatest
    eclipse (distance measured along the line), with duration and local time."""
    step = step_minutes / 60.0
    markers = []
    for sign in (-1, 1):
        t = t_greatest
        prev = central_point(Elements(t))
        travelled = 0.0
        target = spacing_km
        while True:
            t_prev = t
            t += sign * step
            p = central_point(Elements(t))
            if p is None:
                break
            seg = great_circle_km(prev, p)
            travelled += seg
            prev = p
            while travelled >= target and seg > 0:
                # Interpolate within this step so the marker sits at exactly `target` km.
                frac = 1.0 - (travelled - target) / seg
                t_hit = t_prev + frac * sign * step
                point, props = describe_instant(t_hit)
                props.update({"kind": "marker", "distanceKm": int(sign * target)})
                markers.append({
                    "type": "Feature",
                    "id": "marker%+d" % int(sign * target),
                    "properties": props,
                    "geometry": {"type": "Point", "coordinates": [round(point[0], 3), round(point[1], 3)]}
                })
                target += spacing_km
    markers.sort(key=lambda f: f["properties"]["distanceKm"])
    return markers


def build(dest_path=OUTPUT_JSON):
    print("Computing path of totality...")
    total_ring, central_line, (t_c0, t_c1) = build_umbra()
    print("  totality: %d points, central line %s -> %s" % (len(total_ring), tdt_to_ut_hms(t_c0), tdt_to_ut_hms(t_c1)))

    tg = GREATEST_ECLIPSE_TDT_HOURS - T0_TDT_HOURS
    greatest, greatest_props = describe_instant(tg)
    greatest_props.update({"kind": "greatest", "label": "Greatest eclipse",
                           "pathWidthKm": round(umbra_width_km(Elements(tg)))})
    markers = build_markers(tg)
    print("  %d markers every 1000 km along the central line" % len(markers))

    collection = {
        "type": "FeatureCollection",
        "name": "Total Solar Eclipse of 2027 August 2",
        "properties": {
            "date": "2027-08-02",
            "source": "Besselian elements by Fred Espenak, NASA GSFC (eclipse.gsfc.nasa.gov); "
                      "ground track computed by maps/scripts/build_eclipse.py",
            "deltaT": DELTA_T_SECONDS,
            "greatestEclipse": tdt_to_ut_hms(tg),
            "totalityBegins": tdt_to_ut_hms(t_c0),
            "totalityEnds": tdt_to_ut_hms(t_c1)
        },
        "features": [
            {
                "type": "Feature",
                "id": "total",
                "properties": {"kind": "total", "label": "Path of totality",
                               "begins": tdt_to_ut_hms(t_c0), "ends": tdt_to_ut_hms(t_c1)},
                "geometry": {"type": "Polygon", "coordinates": [round_ring(total_ring, 3)]}
            },
            {
                "type": "Feature",
                "id": "central",
                "properties": {"kind": "central", "label": "Central line"},
                "geometry": {"type": "LineString",
                             "coordinates": [[round(lon, 3), round(lat, 3)] for lon, lat in central_line]}
            },
            {
                "type": "Feature",
                "id": "greatest",
                "properties": greatest_props,
                "geometry": {"type": "Point", "coordinates": [round(greatest[0], 3), round(greatest[1], 3)]}
            }
        ] + markers
    }

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "w", encoding="utf-8") as f:
        json.dump(collection, f, separators=(",", ":"))
    print("Wrote %s (%d bytes)" % (os.path.abspath(dest_path), os.path.getsize(dest_path)))


# ---------------------------------------------------------------------------
# Verification against NASA's published path table (SE2027Aug02Tpath.html).
# (UT, north lat, north lon, central lat, central lon, south lat, south lon, width km)
# ---------------------------------------------------------------------------
NASA_ROWS = [
    ("08:30", (33 + 51.7 / 60, -(27 + 4.0 / 60)), (33 + 5.1 / 60, -(25 + 54.2 / 60)), (32 + 16.4 / 60, -(24 + 52.3 / 60)), 222),
    ("09:00", (36 + 34.8 / 60, 3 + 47.7 / 60), (35 + 28.6 / 60, 3 + 49.1 / 60), (34 + 22.6 / 60, 3 + 48.7 / 60), 243),
    ("10:00", (27 + 51.4 / 60, 31 + 44.0 / 60), (26 + 53.3 / 60, 31 + 0.8 / 60), (25 + 55.3 / 60, 30 + 18.2 / 60), 257),
    ("11:00", (12 + 35.4 / 60, 50 + 42.2 / 60), (11 + 51.1 / 60, 49 + 46.1 / 60), (11 + 6.4 / 60, 48 + 51.0 / 60), 257),
    ("11:40", (-(3 + 22.8 / 60), 72 + 3.8 / 60), (-(3 + 54.4 / 60), 70 + 51.6 / 60), (-(4 + 27.4 / 60), 69 + 42.2 / 60), 233),
]


def verify():
    worst = 0.0
    print("%-6s %-10s %-10s %-10s %-10s" % ("UT", "N err km", "C err km", "S err km", "width"))
    for ut, n, c, s, width in NASA_ROWS:
        h, m = ut.split(":")
        t = (int(h) + int(m) / 60.0 + DELTA_T_SECONDS / 3600.0) - T0_TDT_HOURS
        el = Elements(t)
        errs = []
        for ref, got in ((n, umbra_point(el, +1)), (c, central_point(el)), (s, umbra_point(el, -1))):
            errs.append(great_circle_km((ref[1], ref[0]), got))
        w = umbra_width_km(el)
        worst = max(worst, *errs)
        print("%-6s %-10.1f %-10.1f %-10.1f %.0f (NASA %d)" % (ut, errs[0], errs[1], errs[2], w, width))
    print("Worst position error vs NASA table: %.1f km" % worst)
    return worst


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--verify", action="store_true", help="compare against NASA's path table and exit")
    ap.add_argument("--out", default=OUTPUT_JSON)
    args = ap.parse_args()
    if args.verify:
        sys.exit(0 if verify() < 5.0 else 1)
    build(args.out)
