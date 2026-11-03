"""Fit the delay model (graph.DELAY_PARAMS) to reference travel times.

References:
  - Google Maps ETAs from phase 0 (docs/phase0-results.md): the fastest
    Rochester-Syracuse route and the back-road variants A, B, C.
  - The public OSRM demo server on village-to-village trips across upstate
    New York with little motorway driving (OSRM is 24% slow on interstates,
    so motorway speed is anchored to Google's fastest route instead).

OSRM's car profile drives at about 0.8x the posted limit and charges only
~2 s per traffic signal, so its absolute times and its signal cost are
properties of the profile, not of real traffic. OSRM trips therefore enter
the fit with a free scale factor `beta` (ours = beta * OSRM) and with signals
priced at OSRM's own 2 s: they constrain how class, built-up areas, stop
signs and junctions trade off against each other, while Google sets the
absolute level. Neither source separates signal delay from a general
slowdown (A, B, C and the fastest route have similar mixes), so the
per-event delays carry weak priors from traffic engineering figures.

The model is linear in its parameters for a fixed path, so the fit
alternates: route every trip with the current parameters, sum each path's
features, solve a bounded least-squares problem on log(ours / reference),
repeat until the parameters settle.

OSRM requests go out at most once per second and are cached under
~/.cache/scenic-calibrate/ (override with SCENIC_CALIBRATE_CACHE), so reruns
make no network calls. Requests identify the project, not a person.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import data_dir
from .graph import CROSSING_AS_SIGNALS, DELAY_COLUMNS, DELAY_PARAMS, haversine_m, write_edge_delays

CACHE = Path(os.environ.get("SCENIC_CALIBRATE_CACHE", Path.home() / ".cache" / "scenic-calibrate"))
USER_AGENT = "scenic-route-planner/0.1 (+https://github.com/tvh0021/design)"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/"
OSRM_MIN_INTERVAL_S = 1.1
OSRM_MAX_REQUESTS = 55  # hard cap per run; cache hits do not count

# Parameter order for the linear model, bounds and priors (mean, sd).
PARAM_NAMES = ("motorway", "rural", "built_up", "signal_s", "stop_s", "minor_end_s")
BOUNDS = {
    "motorway": (0.8, 1.3),
    "rural": (0.9, 1.5),
    "built_up": (0.9, 2.0),
    "signal_s": (0.0, 40.0),
    "stop_s": (0.0, 20.0),
    "minor_end_s": (0.0, 15.0),
}
FREE_FLOW = {"motorway": 1.0, "rural": 1.0, "built_up": 1.0, "signal_s": 0.0, "stop_s": 0.0, "minor_end_s": 0.0}
# Average delay for a through car: signal ~15 s (HCM control delay, light
# traffic), stop sign ~8 s (decelerate, stop, accelerate), junction ~3 s.
PRIORS = {"signal_s": (15.0, 10.0), "stop_s": (8.0, 5.0), "minor_end_s": (3.0, 3.0)}
PRIOR_WEIGHT = 0.1  # a 1-sd miss costs as much as a 10% miss on one trip
GOOGLE_WEIGHT = 10.0  # per Google trip; each OSRM trip has weight 1
OSRM_SIGNAL_PENALTY_S = 2.0  # traffic_signal_penalty in OSRM's car profile
BETA_BOUNDS = (0.6, 1.4)

# Phase 0 Google Maps ETAs, 2026-11-02 around 7 pm (light traffic).
ROC = (43.15729, -77.61521)
SYR = (43.04812, -76.14742)
GOOGLE_TRIPS = {
    "fastest": ([ROC, SYR], 87.0),
    "A": ([ROC, (43.07109, -77.49198), (43.03791, -77.45410), (42.99236, -77.42724), (42.86230, -77.18485),
           (42.87303, -76.97127), (42.93097, -76.78126), (42.93991, -76.49489), (42.96821, -76.42691),
           (43.03570, -76.34214), SYR], 150.0),
    "B": ([ROC, (42.99236, -77.42724), (42.86230, -77.18485), (42.93991, -76.49489), (42.96821, -76.42691),
           (43.03570, -76.34214), SYR], 138.0),
    "C": ([ROC, (42.87303, -76.97127), (42.93991, -76.49489), (42.96821, -76.42691), (43.03570, -76.34214),
           SYR], 127.0),
}

# OD sampling: villages 20-80 km apart (straight line), upstate New York.
OD_MIN_KM, OD_MAX_KM = 20.0, 80.0
MAX_FAST_SHARE = 0.10  # motorway share of our route and of OSRM's
N_TRIPS = 40


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- OSRM ------


class Osrm:
    def __init__(self, cache: Path = CACHE, max_requests: int = OSRM_MAX_REQUESTS):
        self.cache = cache
        self.max_requests = max_requests
        self.sent = 0
        self.hits = 0
        self._last = 0.0

    def route(self, a: tuple[float, float], b: tuple[float, float]) -> dict:
        """OSRM fastest route from a to b, (lat, lon) each."""
        coords = f"{a[1]:.5f},{a[0]:.5f};{b[1]:.5f},{b[0]:.5f}"
        url = f"{OSRM_URL}{coords}?overview=false&steps=true&alternatives=false"
        path = self.cache / f"osrm_{hashlib.sha1(url.encode()).hexdigest()[:16]}.json"
        if path.exists():
            self.hits += 1
            return json.loads(path.read_text())
        if self.sent >= self.max_requests:
            raise RuntimeError(f"OSRM request cap ({self.max_requests}) reached for this run")
        wait = OSRM_MIN_INTERVAL_S - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        self.sent += 1
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        if data.get("code") != "Ok":
            raise RuntimeError(f"OSRM error: {data.get('code')} {data.get('message')}")
        self.cache.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        with open(self.cache / "requests.log", "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {coords}\n")
        return data


def osrm_motorway_share(resp: dict) -> float:
    """Share of the route's distance on interstates and other motorway-like
    steps, judged by step ref and name."""
    route = resp["routes"][0]
    total = route["distance"] or 1.0
    fast = 0.0
    for leg in route["legs"]:
        for st in leg["steps"]:
            ref = (st.get("ref") or "").upper()
            name = (st.get("name") or "").lower()
            refs = [r.strip() for r in ref.replace(",", ";").split(";")]
            if any(r.startswith("I ") or r.startswith("I-") for r in refs) or any(
                    w in name for w in ("thruway", "expressway", "interstate", "parkway")):
                fast += st["distance"]
    return fast / total


# --------------------------------------------------------------- trips ------


@dataclass
class Trip:
    name: str
    points: list[tuple[float, float]]  # (lat, lon), legs between consecutive points
    ref_min: float  # reference ETA in minutes
    source: str  # "google" or "osrm"


def read_villages(pbf: Path) -> list[tuple[str, float, float]]:
    """(name, lat, lon) of place=village/town nodes in upstate New York."""
    import osmium
    from osmium.filter import KeyFilter

    from .graph import in_canada

    out = []
    for n in osmium.FileProcessor(str(pbf), osmium.osm.NODE).with_filter(KeyFilter("place")):
        if n.tags.get("place") not in ("village", "town"):
            continue
        lat, lon = n.location.lat, n.location.lon
        # Upstate only: drop New York City, Long Island and the neighbouring
        # states the extract spills into (rough boxes).
        if lat < 41.3 or lon > -73.35 or in_canada(lon, lat):
            continue
        if lat < 42.0 and lon < -75.3:  # Pennsylvania
            continue
        out.append((n.tags.get("name") or str(n.id), lat, lon))
    return out


def sample_pairs(villages, n: int, seed: int = 7) -> list[tuple[tuple, tuple]]:
    """Candidate village pairs 20-80 km apart, spread over a 1-degree grid:
    cycle through grid cells, one random pair per cell per round."""
    rng = np.random.default_rng(seed)
    lat = np.array([v[1] for v in villages])
    lon = np.array([v[2] for v in villages])
    cell = np.floor(lat).astype(int) * 1000 + np.floor(lon).astype(int)
    cells = sorted(set(cell.tolist()))
    pairs = []
    for _ in range(50):
        for c in rng.permutation(cells):
            members = np.flatnonzero(cell == c)
            i = int(rng.choice(members))
            d = haversine_m(lon[i], lat[i], lon, lat) / 1000
            ok = np.flatnonzero((d >= OD_MIN_KM) & (d <= OD_MAX_KM))
            if len(ok):
                pairs.append((villages[i], villages[int(rng.choice(ok))]))
            if len(pairs) >= n:
                return pairs
    return pairs


# ------------------------------------------------------------- routing ------


class Model:
    """The graph plus per-edge feature matrix X, so that edge time = X @ p."""

    def __init__(self, graph_dir: Path | None = None):
        import pyarrow.parquet as pq

        from .route import RoadGraph

        graph_dir = Path(graph_dir or data_dir() / "graph")
        self.graph_dir = graph_dir
        self.g = RoadGraph.load(graph_dir, delays=False)
        d = pq.read_table(graph_dir / "edge_delays.parquet")
        if d.num_rows != self.g.n_edges:
            raise ValueError("edge_delays.parquet is not aligned with edges.parquet")
        self.feats = {c: d.column(c).to_numpy() for c in DELAY_COLUMNS}
        self.free_s = d.column("free_time_s").to_numpy()
        mw = self.feats["motorway"].astype(bool)
        bu = self.feats["built_up"].astype(bool)
        f = self.free_s
        self.X = np.column_stack([
            f * mw, f * (~mw & ~bu), f * (~mw & bu),
            self.feats["signals"] + CROSSING_AS_SIGNALS * self.feats["crossings"],
            self.feats["stops"], self.feats["minor_ends"],
        ])
        hw = np.asarray(self.g.edges_table.column("highway").to_pylist(), dtype=object)
        self.fast = np.isin(hw, ["motorway", "motorway_link"])
        self.params = dict(DELAY_PARAMS)

    def set_params(self, params: dict) -> None:
        self.params = {**self.params, **params}
        p = np.array([self.params[k] for k in PARAM_NAMES])
        self.g.set_time(self.X @ p)

    def trip(self, points) -> tuple[np.ndarray, float, float]:
        """Route all legs. Returns (feature sums, minutes, fast-road share)."""
        xs = np.zeros(self.X.shape[1])
        length = fast = 0.0
        for a, b in zip(points[:-1], points[1:]):
            r, _ = self.g.fastest(a, b)
            if r is None:
                raise RuntimeError(f"no route {a} -> {b}")
            xs += self.X[r.edges].sum(axis=0)
            length += r.length_m
            fast += self.g.length_m[r.edges][self.fast[r.edges]].sum()
        p = np.array([self.params[k] for k in PARAM_NAMES])
        return xs, float(xs @ p / 60), fast / max(length, 1.0)


def build_osrm_trips(model: Model, osrm: Osrm, pbf: Path, n: int = N_TRIPS) -> list[Trip]:
    villages = read_villages(pbf)
    log(f"{len(villages)} upstate villages and towns")
    trips: list[Trip] = []
    for (na, la, lo), (nb, lb, lob) in sample_pairs(villages, 4 * n):
        if len(trips) >= n:
            break
        a, b = (la, lo), (lb, lob)
        _, _, share = model.trip([a, b])
        if share > MAX_FAST_SHARE:
            continue
        resp = osrm.route(a, b)
        if osrm_motorway_share(resp) > MAX_FAST_SHARE:
            continue
        trips.append(Trip(f"{na} - {nb}", [a, b], resp["routes"][0]["duration"] / 60, "osrm"))
    log(f"{len(trips)} OSRM trips ({osrm.sent} requests, {osrm.hits} cached)")
    return trips


def google_trips() -> list[Trip]:
    return [Trip(k, pts, m, "google") for k, (pts, m) in GOOGLE_TRIPS.items()]


# ----------------------------------------------------------------- fit ------


def fit_params(Xg: np.ndarray, ref_g: np.ndarray, Xo: np.ndarray, ref_o: np.ndarray,
               x0: dict | None = None) -> tuple[dict, float]:
    """Bounded least squares. Rows of Xg / Xo are path feature sums (seconds
    of free flow per group, signals, stops, minor ends); ref_* are reference
    times in seconds. Residuals:
      Google: sqrt(GOOGLE_WEIGHT) * log(Xg @ p / ref)
      OSRM:   log(Xo @ p_osrm / (beta * ref)), p_osrm = p with OSRM's signal cost
      priors: PRIOR_WEIGHT * (p - mean) / sd
    Returns (params, beta)."""
    from scipy.optimize import least_squares

    x0 = {**FREE_FLOW, **(x0 or {})}
    k_sig = PARAM_NAMES.index("signal_s")
    lo = np.array([BOUNDS[k][0] for k in PARAM_NAMES] + [BETA_BOUNDS[0]])
    hi = np.array([BOUNDS[k][1] for k in PARAM_NAMES] + [BETA_BOUNDS[1]])
    start = np.clip([x0[k] for k in PARAM_NAMES] + [1.0], lo + 1e-9, hi - 1e-9)
    prior_idx = [PARAM_NAMES.index(k) for k in PRIORS]
    prior_mu = np.array([m for m, _ in PRIORS.values()])
    prior_sd = np.array([s for _, s in PRIORS.values()])

    def resid(x):
        p, beta = x[:-1], x[-1]
        po = p.copy()
        po[k_sig] = OSRM_SIGNAL_PENALTY_S
        return np.concatenate([
            math.sqrt(GOOGLE_WEIGHT) * np.log(Xg @ p / ref_g),
            np.log(Xo @ po / (beta * ref_o)),
            PRIOR_WEIGHT * (p[prior_idx] - prior_mu) / prior_sd,
        ])

    res = least_squares(resid, start, bounds=(lo, hi))
    return dict(zip(PARAM_NAMES, (float(v) for v in res.x[:-1]))), float(res.x[-1])


def evaluate(model: Model, trips: list[Trip]) -> tuple[np.ndarray, np.ndarray]:
    """Route every trip under the model's current parameters. Returns the
    feature matrix (trips x params) and our minutes."""
    X = np.zeros((len(trips), len(PARAM_NAMES)))
    mins = np.zeros(len(trips))
    for i, t in enumerate(trips):
        X[i], mins[i], _ = model.trip(t.points)
    return X, mins


def calibrate(model: Model, google: list[Trip], osrm: list[Trip], iters: int = 8) -> tuple[dict, float]:
    """Alternate routing and fitting until the parameters settle."""
    ref_g = np.array([t.ref_min * 60 for t in google])
    ref_o = np.array([t.ref_min * 60 for t in osrm])
    params, beta = dict(model.params), 1.0
    for it in range(iters):
        Xg, _ = evaluate(model, google)
        Xo, _ = evaluate(model, osrm)
        new, beta = fit_params(Xg, ref_g, Xo, ref_o, params)
        change = max(abs(new[k] - params[k]) / (abs(params[k]) + 1.0) for k in PARAM_NAMES)
        params = new
        model.set_params(params)
        log(f"  iteration {it + 1}: " + ", ".join(f"{k}={params[k]:.3g}" for k in PARAM_NAMES)
            + f", beta={beta:.3f}")
        if change < 0.005:
            break
    return params, beta


def ratio_table(model: Model, trips: list[Trip]) -> list[tuple[str, float, float, float]]:
    _, mins = evaluate(model, trips)
    return [(t.name, t.ref_min, m, m / t.ref_min) for t, m in zip(trips, mins)]


def summarize(rows) -> str:
    r = np.array([x[3] for x in rows])
    return (f"median {np.median(r):.3f}, IQR {np.percentile(r, 25):.3f}-{np.percentile(r, 75):.3f}, "
            f"range {r.min():.3f}-{r.max():.3f}, RMS log error {math.sqrt(np.mean(np.log(r) ** 2)):.3f}")


def run_fit(graph_dir: Path | None = None, pbf: Path | None = None, apply: bool = True) -> dict:
    """Fit, print a report, and (if `apply`) rewrite edge_delays.parquet with
    the fitted parameters. Returns the report."""
    pbf = pbf or data_dir() / "raw" / "new-york-latest.osm.pbf"
    model = Model(graph_dir)
    osrm = Osrm()

    model.set_params(FREE_FLOW)
    osrm_trips = build_osrm_trips(model, osrm, pbf)
    g_trips = google_trips()
    before_g = ratio_table(model, g_trips)
    before_o = ratio_table(model, osrm_trips)

    # Holdout: Google's fastest route only (it pins the motorway factor);
    # A, B and C are then out of sample.
    log("holdout fit: OSRM trips + Google fastest")
    params_h, beta_h = calibrate(model, [t for t in g_trips if t.name == "fastest"], osrm_trips)
    hold_g = ratio_table(model, g_trips)

    log("fit: OSRM trips + all Google trips")
    model.set_params(FREE_FLOW)
    params, beta = calibrate(model, g_trips, osrm_trips)
    after_g = ratio_table(model, g_trips)
    after_o = ratio_table(model, osrm_trips)

    report = {
        "params": params,
        "osrm_beta": beta,
        "holdout_params": params_h,
        "holdout_osrm_beta": beta_h,
        "google": {r[0]: {"google_min": r[1], "before_min": round(b[2], 1), "after_min": round(r[2], 1),
                          "before_ratio": round(b[3], 3), "after_ratio": round(r[3], 3),
                          "holdout_min": round(h[2], 1), "holdout_ratio": round(h[3], 3)}
                   for r, b, h in zip(after_g, before_g, hold_g)},
        "osrm_before": summarize(before_o),
        "osrm_after": summarize(after_o),
        "osrm_trips": [{"name": a[0], "points": t.points, "osrm_min": round(a[1], 1),
                        "before_min": round(b[2], 1), "after_min": round(a[2], 1)}
                       for a, b, t in zip(after_o, before_o, osrm_trips)],
        "osrm_requests": {"sent": osrm.sent, "cached": osrm.hits},
    }
    print(format_report(report))
    if apply:
        write_edge_delays(model.feats, model.free_s, model.graph_dir, params)
        (model.graph_dir / "delay_fit.json").write_text(json.dumps(report, indent=2) + "\n")
        print(f"wrote {model.graph_dir / 'edge_delays.parquet'} and delay_fit.json")
    return report


def format_report(rep: dict) -> str:
    lines = ["fitted parameters:"]
    lines += [f"  {k:12s} {rep['params'][k]:8.3f}   (holdout fit {rep['holdout_params'][k]:8.3f})"
              for k in PARAM_NAMES]
    lines.append(f"  OSRM scale beta (ours / OSRM) {rep['osrm_beta']:.3f} (holdout {rep['holdout_osrm_beta']:.3f})")
    lines.append("vs Google (minutes, ours / Google):")
    lines.append(f"  {'route':8s} {'Google':>7s} {'before':>7s} {'ratio':>6s} {'after':>7s} {'ratio':>6s}"
                 f" {'holdout':>8s} {'ratio':>6s}")
    for k, r in rep["google"].items():
        lines.append(f"  {k:8s} {r['google_min']:7.0f} {r['before_min']:7.1f} {r['before_ratio']:6.3f} "
                     f"{r['after_min']:7.1f} {r['after_ratio']:6.3f} {r['holdout_min']:8.1f} {r['holdout_ratio']:6.3f}")
    lines.append(f"vs OSRM, {len(rep['osrm_trips'])} trips, ours / OSRM:")
    lines.append(f"  before: {rep['osrm_before']}")
    lines.append(f"  after:  {rep['osrm_after']}")
    lines.append(f"OSRM requests: {rep['osrm_requests']['sent']} sent, {rep['osrm_requests']['cached']} cached")
    return "\n".join(lines)
