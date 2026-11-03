"""Fit the delay model (graph.DELAY_PARAMS) to reference travel times.

References:
  - Google Maps ETAs checked by hand (GOOGLE_TRIPS): phase 0's fastest
    Rochester-Syracuse route and back-road variants A, B, C
    (docs/phase0-results.md), plus five shorter trips across upstate New
    York. These set the model.
  - The public OSRM demo server on village-to-village trips across upstate
    New York with little motorway driving (OSRM is 24% slow on interstates,
    so motorway speed is anchored to Google's fastest route instead). These
    enter with a small weight (OSRM_WEIGHT): OSRM's car profile has its own
    class speeds, which disagree with Google's on county roads.

OSRM's car profile drives at about 0.8x the posted limit and charges only
~2 s per traffic signal, so its absolute times and its signal cost are
properties of the profile, not of real traffic. OSRM trips therefore enter
the fit with a free scale factor `beta` (ours = beta * OSRM) and with signals
priced at OSRM's own 2 s, both when routing them and in their residual: they
only nudge how class, built-up areas, stop signs and junctions trade off
against each other, while Google sets the level and the class split. Google
does not separate signal delay from a general town slowdown well, so the
per-event delays carry weak priors from traffic engineering figures.

The model is linear in its parameters for a fixed path, so the fit
alternates: route every trip with the current parameters, sum each path's
features, solve a bounded least-squares problem on log(ours / reference),
repeat until the parameters settle. Path changes can make this cycle, so
the fit keeps the state with the lowest objective on its own routes.
Leave-one-out over the Google trips checks it out of sample.

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
from .graph import (
    CROSSING_AS_SIGNALS, DELAY_COLUMNS, DELAY_PARAMS, edges_fingerprint, haversine_m, read_edge_delays,
    write_edge_delays,
)

CACHE = Path(os.environ.get("SCENIC_CALIBRATE_CACHE", Path.home() / ".cache" / "scenic-calibrate"))
USER_AGENT = "scenic-route-planner/0.1 (+https://github.com/tvh0021/design)"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/"
OSRM_MIN_INTERVAL_S = 1.1
OSRM_MAX_REQUESTS = 55  # hard cap per run; cache hits do not count

# Parameter order for the linear model, bounds and priors (mean, sd).
PARAM_NAMES = ("motorway", "rural", "rural_minor", "built_up", "signal_s", "stop_s", "minor_end_s")
BOUNDS = {
    "motorway": (0.8, 1.3),
    "rural": (0.8, 1.5),
    "rural_minor": (0.7, 1.5),  # class default speeds (tertiary 40 mph) run below NY's 55
    "built_up": (0.8, 2.0),
    "signal_s": (0.0, 40.0),
    "stop_s": (0.0, 20.0),
    "minor_end_s": (0.0, 15.0),
}
FREE_FLOW = {"motorway": 1.0, "rural": 1.0, "rural_minor": 1.0, "built_up": 1.0,
             "signal_s": 0.0, "stop_s": 0.0, "minor_end_s": 0.0}
# Average delay for a through car: signal ~15 s (HCM control delay, light
# traffic), stop sign ~8 s (decelerate, stop, accelerate), junction ~3 s.
PRIORS = {"signal_s": (15.0, 10.0), "stop_s": (8.0, 5.0), "minor_end_s": (3.0, 3.0)}
PRIOR_WEIGHT = 0.1  # a 1-sd miss costs as much as a 10% miss on one trip
# Per-trip weights on squared log errors: Google 9 x 10 = 90 against
# OSRM 40 x 0.1 = 4, so Google decides wherever the two disagree.
GOOGLE_WEIGHT = 10.0
OSRM_WEIGHT = 0.1
OSRM_SIGNAL_PENALTY_S = 2.0  # traffic_signal_penalty in OSRM's car profile
# OSRM trips are routed with these overrides, matching their residual in fit_params.
OSRM_ROUTING = {"signal_s": OSRM_SIGNAL_PENALTY_S}
BETA_BOUNDS = (0.6, 1.4)

# Google Maps ground truth: name -> (stops (lat, lon), minutes, miles), as
# shown by desktop Google Maps on 2026-11-02 in the evening (light traffic).
# The first four are phase 0 (docs/phase0-results.md); the other five were
# checked the same evening. Every trip is in the fit; the report flags a trip
# whose route length differs from Google's by more than PATH_TOLERANCE, as
# that means a different path rather than a speed error.
ROC = (43.15729, -77.61521)
SYR = (43.04812, -76.14742)
GOOGLE_TRIPS = {
    "fastest": ([ROC, SYR], 87.0, 87.2),
    "A": ([ROC, (43.07109, -77.49198), (43.03791, -77.45410), (42.99236, -77.42724), (42.86230, -77.18485),
           (42.87303, -76.97127), (42.93097, -76.78126), (42.93991, -76.49489), (42.96821, -76.42691),
           (43.03570, -76.34214), SYR], 150.0, 105.0),
    "B": ([ROC, (42.99236, -77.42724), (42.86230, -77.18485), (42.93991, -76.49489), (42.96821, -76.42691),
           (43.03570, -76.34214), SYR], 138.0, 105.0),
    "C": ([ROC, (42.87303, -76.97127), (42.93991, -76.49489), (42.96821, -76.42691), (43.03570, -76.34214),
           SYR], 127.0, 103.0),
    "Potsdam-Massena": ([(44.66980, -74.98133), (44.93008, -74.89224)], 30.0, 20.4),
    "Ulster-Arlington": ([(41.96266, -73.98986), (41.69580, -73.89649)], 40.0, 25.5),
    "Ashford-Caneadea": ([(42.32034, -78.60808), (42.38645, -78.15362)], 36.0, 27.3),
    "Richmond-Howard": ([(42.79059, -77.53194), (42.36424, -77.50888)], 43.0, 34.3),
    "Lindley-Scio": ([(42.02841, -77.13969), (42.17146, -77.97861)], 72.0, 58.9),
}
PATH_TOLERANCE = 0.05
METERS_PER_MILE = 1609.344

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
    ref_mi: float | None = None  # reference route length (Google trips)


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
        from .route import RoadGraph

        graph_dir = Path(graph_dir or data_dir() / "graph")
        self.graph_dir = graph_dir
        self.g = RoadGraph.load(graph_dir, delays=False)
        d = read_edge_delays(graph_dir / "edge_delays.parquet", edges_fingerprint(self.g.edges_table))
        self.feats = {c: d.column(c).to_numpy() for c in DELAY_COLUMNS}
        self.free_s = d.column("free_time_s").to_numpy()
        mw = self.feats["motorway"].astype(bool)
        bu = self.feats["built_up"].astype(bool)
        minor = self.feats["minor_road"].astype(bool)
        f = self.free_s
        self.X = np.column_stack([
            f * mw, f * (~mw & ~bu & ~minor), f * (~mw & ~bu & minor), f * (~mw & bu),
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

    def trip(self, points) -> tuple[np.ndarray, float, float, float]:
        """Route all legs. Returns (feature sums, minutes, fast-road share, metres)."""
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
        return xs, float(xs @ p / 60), fast / max(length, 1.0), length


def build_osrm_trips(model: Model, osrm: Osrm, pbf: Path, n: int = N_TRIPS) -> list[Trip]:
    villages = read_villages(pbf)
    log(f"{len(villages)} upstate villages and towns")
    trips: list[Trip] = []
    for (na, la, lo), (nb, lb, lob) in sample_pairs(villages, 4 * n):
        if len(trips) >= n:
            break
        a, b = (la, lo), (lb, lob)
        _, _, share, _ = model.trip([a, b])
        if share > MAX_FAST_SHARE:
            continue
        resp = osrm.route(a, b)
        if osrm_motorway_share(resp) > MAX_FAST_SHARE:
            continue
        trips.append(Trip(f"{na} - {nb}", [a, b], resp["routes"][0]["duration"] / 60, "osrm"))
    log(f"{len(trips)} OSRM trips ({osrm.sent} requests, {osrm.hits} cached)")
    return trips


def google_trips() -> list[Trip]:
    return [Trip(k, pts, m, "google", mi) for k, (pts, m, mi) in GOOGLE_TRIPS.items()]


# ----------------------------------------------------------------- fit ------


_O_IDX = [PARAM_NAMES.index(k) for k in OSRM_ROUTING]
_O_VAL = np.array(list(OSRM_ROUTING.values()))
_PRIOR_IDX = [PARAM_NAMES.index(k) for k in PRIORS]
_PRIOR_MU = np.array([m for m, _ in PRIORS.values()])
_PRIOR_SD = np.array([s for _, s in PRIORS.values()])


def residuals(p: np.ndarray, beta: float, Xg: np.ndarray, ref_g: np.ndarray, Xo: np.ndarray,
              ref_o: np.ndarray) -> np.ndarray:
    """Weighted residuals of the fit. Rows of Xg / Xo are path feature sums
    (seconds of free flow per group, signals, stops, minor ends); ref_* are
    reference times in seconds.
      Google: sqrt(GOOGLE_WEIGHT) * log(Xg @ p / ref)
      OSRM:   sqrt(OSRM_WEIGHT) * log(Xo @ p_osrm / (beta * ref)), p_osrm = p
              with OSRM_ROUTING (OSRM's signal cost; route the OSRM trips with it too)
      priors: PRIOR_WEIGHT * (p - mean) / sd"""
    p = np.asarray(p, dtype=np.float64)
    po = p.copy()
    po[_O_IDX] = _O_VAL
    return np.concatenate([
        math.sqrt(GOOGLE_WEIGHT) * np.log(Xg @ p / ref_g),
        math.sqrt(OSRM_WEIGHT) * np.log(Xo @ po / (beta * ref_o)),
        PRIOR_WEIGHT * (p[_PRIOR_IDX] - _PRIOR_MU) / _PRIOR_SD,
    ])


def best_beta(p: np.ndarray, Xo: np.ndarray, ref_o: np.ndarray) -> float:
    """The OSRM scale that minimises the OSRM residuals for fixed p."""
    if not len(ref_o):
        return 1.0
    po = np.asarray(p, dtype=np.float64).copy()
    po[_O_IDX] = _O_VAL
    return float(np.clip(np.exp(np.mean(np.log(Xo @ po / ref_o))), *BETA_BOUNDS))


def objective(params: dict, Xg: np.ndarray, ref_g: np.ndarray, Xo: np.ndarray,
              ref_o: np.ndarray) -> tuple[float, float]:
    """Sum of squared residuals at `params` with its best beta. Returns (objective, beta)."""
    p = np.array([params[k] for k in PARAM_NAMES])
    beta = best_beta(p, Xo, ref_o)
    return float(np.sum(residuals(p, beta, Xg, ref_g, Xo, ref_o) ** 2)), beta


def fit_params(Xg: np.ndarray, ref_g: np.ndarray, Xo: np.ndarray, ref_o: np.ndarray,
               x0: dict | None = None) -> tuple[dict, float]:
    """Bounded least squares on `residuals` for fixed paths. Returns (params, beta)."""
    from scipy.optimize import least_squares

    x0 = {**FREE_FLOW, **(x0 or {})}
    lo = np.array([BOUNDS[k][0] for k in PARAM_NAMES] + [BETA_BOUNDS[0]])
    hi = np.array([BOUNDS[k][1] for k in PARAM_NAMES] + [BETA_BOUNDS[1]])
    start = np.clip([x0[k] for k in PARAM_NAMES] + [1.0], lo + 1e-9, hi - 1e-9)
    res = least_squares(lambda x: residuals(x[:-1], x[-1], Xg, ref_g, Xo, ref_o), start, bounds=(lo, hi))
    return dict(zip(PARAM_NAMES, (float(v) for v in res.x[:-1]))), float(res.x[-1])


def at_bounds(params: dict, tol: float = 1e-3) -> list[str]:
    """Names of parameters pinned at a bound."""
    return [k for k in PARAM_NAMES
            if min(abs(params[k] - BOUNDS[k][0]), abs(params[k] - BOUNDS[k][1])) <= tol * (abs(params[k]) + 1)]


def evaluate(model: Model, trips: list[Trip], overrides: dict | None = None
             ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Route every trip under the model's current parameters, with
    `overrides` applied while routing. Returns the feature matrix (trips x
    params), our minutes under the parameters used for routing, and route
    lengths in miles."""
    saved = dict(model.params)
    if overrides:
        model.set_params(overrides)
    try:
        X = np.zeros((len(trips), len(PARAM_NAMES)))
        mins = np.zeros(len(trips))
        miles = np.zeros(len(trips))
        for i, t in enumerate(trips):
            X[i], mins[i], _, length = model.trip(t.points)
            miles[i] = length / METERS_PER_MILE
    finally:
        if overrides:
            model.set_params(saved)
    return X, mins, miles


def calibrate(model: Model, google: list[Trip], osrm: list[Trip], iters: int = 10,
              tol: float = 0.005) -> tuple[dict, float]:
    """Alternate routing and fitting, starting from the model's parameters.
    Path changes between rounds can make the parameters cycle instead of
    settle, so every state is scored on the routes it picks itself and the
    one with the lowest objective is kept. It is left set on the model."""
    ref_g = np.array([t.ref_min * 60 for t in google])
    ref_o = np.array([t.ref_min * 60 for t in osrm])
    params = dict(model.params)
    best: tuple[float, dict, float] | None = None
    settled = False
    for it in range(iters + 1):
        Xg, _, _ = evaluate(model, google)
        Xo, _, _ = evaluate(model, osrm, OSRM_ROUTING)  # same signal cost as their residual
        cost, beta = objective(params, Xg, ref_g, Xo, ref_o)
        if best is None or cost < best[0]:
            best = (cost, params, beta)
        log(f"  round {it}: objective {cost:.4f}; " + ", ".join(f"{k}={params[k]:.3g}" for k in PARAM_NAMES)
            + f", beta={beta:.3f}")
        if settled or it == iters:
            break
        new, _ = fit_params(Xg, ref_g, Xo, ref_o, params)
        settled = max(abs(new[k] - params[k]) / (abs(params[k]) + 1.0) for k in PARAM_NAMES) < tol
        params = new
        model.set_params(params)
    cost, params, beta = best
    model.set_params(params)
    return params, beta


def ratio_table(model: Model, trips: list[Trip]) -> list[dict]:
    """Ours vs reference for each trip under the model's current parameters."""
    _, mins, miles = evaluate(model, trips)
    rows = []
    for t, m, mi in zip(trips, mins, miles):
        row = {"name": t.name, "ref_min": t.ref_min, "min": round(float(m), 1), "ratio": round(float(m / t.ref_min), 3)}
        if t.ref_mi:
            row |= {"ref_mi": t.ref_mi, "mi": round(float(mi), 1), "mi_ratio": round(float(mi / t.ref_mi), 3),
                    "path_differs": bool(abs(mi / t.ref_mi - 1) > PATH_TOLERANCE)}
        rows.append(row)
    return rows


def summarize(rows) -> str:
    r = np.array([x["ratio"] for x in rows])
    return (f"median {np.median(r):.3f}, IQR {np.percentile(r, 25):.3f}-{np.percentile(r, 75):.3f}, "
            f"range {r.min():.3f}-{r.max():.3f}, RMS log error {math.sqrt(np.mean(np.log(r) ** 2)):.3f}")


def leave_one_out(model: Model, google: list[Trip], osrm: list[Trip], restore: dict) -> list[dict]:
    """Refit without each Google trip in turn and score the trip left out.
    Every fold starts from FREE_FLOW, like the full fit, so the held-out
    trip cannot reach its own fold through the starting point. Leaves
    `restore` set on the model."""
    rows = []
    for i, t in enumerate(google):
        log(f"leave-one-out: without {t.name}")
        model.set_params(FREE_FLOW)
        params, beta = calibrate(model, google[:i] + google[i + 1:], osrm)
        rows.append({**ratio_table(model, [t])[0], "params": params, "osrm_beta": beta,
                     "at_bounds": at_bounds(params)})
    model.set_params(restore)
    return rows


def run_fit(graph_dir: Path | None = None, pbf: Path | None = None, apply: bool = True) -> dict:
    """Fit, validate by leave-one-out over the Google trips, print a report,
    and (if `apply`) rewrite edge_delays.parquet with the fitted parameters.
    Returns the report."""
    pbf = pbf or data_dir() / "raw" / "new-york-latest.osm.pbf"
    model = Model(graph_dir)
    osrm = Osrm()

    model.set_params(FREE_FLOW)
    osrm_trips = build_osrm_trips(model, osrm, pbf)
    g_trips = google_trips()
    before_g = ratio_table(model, g_trips)
    before_o = ratio_table(model, osrm_trips)

    log("fit: OSRM trips + all Google trips")
    params, beta = calibrate(model, g_trips, osrm_trips)
    after_g = ratio_table(model, g_trips)
    after_o = ratio_table(model, osrm_trips)
    loo = leave_one_out(model, g_trips, osrm_trips, params)
    loo_err = np.abs([r["ratio"] - 1 for r in loo])

    report = {
        "params": params,
        "osrm_beta": beta,
        "at_bounds": at_bounds(params),
        "weights": {"google": GOOGLE_WEIGHT, "osrm": OSRM_WEIGHT, "prior": PRIOR_WEIGHT},
        "google": {a["name"]: {"google_min": a["ref_min"], "google_mi": a["ref_mi"],
                               "before_min": b["min"], "before_ratio": b["ratio"],
                               "after_min": a["min"], "after_ratio": a["ratio"], "after_mi": a["mi"],
                               "path_differs": a["path_differs"],
                               "loo_min": h["min"], "loo_ratio": h["ratio"], "loo_mi": h["mi"],
                               "loo_path_differs": h["path_differs"], "loo_params": h["params"],
                               "loo_at_bounds": h["at_bounds"]}
                   for a, b, h in zip(after_g, before_g, loo)},
        "loo_summary": {"max_abs_error": round(float(loo_err.max()), 3),
                        "median_abs_error": round(float(np.median(loo_err)), 3)},
        "osrm_before": summarize(before_o),
        "osrm_after": summarize(after_o),
        "osrm_trips": [{"name": a["name"], "points": t.points, "osrm_min": round(a["ref_min"], 1),
                        "before_min": b["min"], "after_min": a["min"]}
                       for a, b, t in zip(after_o, before_o, osrm_trips)],
        "osrm_requests": {"sent": osrm.sent, "cached": osrm.hits},
    }
    print(format_report(report))
    if apply:
        write_edge_delays(model.feats, model.free_s, model.graph_dir, params,
                          edges_fingerprint(model.g.edges_table))
        (model.graph_dir / "delay_fit.json").write_text(json.dumps(report, indent=2) + "\n")
        print(f"wrote {model.graph_dir / 'edge_delays.parquet'} and delay_fit.json")
    return report


def format_report(rep: dict) -> str:
    lines = ["fitted parameters:"]
    lines += [f"  {k:12s} {rep['params'][k]:8.3f}" + ("   (at bound)" if k in rep["at_bounds"] else "")
              for k in PARAM_NAMES]
    lines.append(f"  OSRM scale beta (ours / OSRM) {rep['osrm_beta']:.3f}")
    lines.append("vs Google (minutes, ours / Google; * = route length differs from Google's by more than "
                 f"{PATH_TOLERANCE:.0%}, so a different path):")
    lines.append(f"  {'route':17s} {'Google':>6s} {'mi':>5s} {'before':>6s} {'ratio':>6s} {'after':>6s} {'ratio':>6s}"
                 f" {'our mi':>6s}  {'LOO':>6s} {'ratio':>6s}")
    for k, r in rep["google"].items():
        lines.append(f"  {k:17s} {r['google_min']:6.0f} {r['google_mi']:5.1f} {r['before_min']:6.1f} "
                     f"{r['before_ratio']:6.3f} {r['after_min']:6.1f} {r['after_ratio']:6.3f} "
                     f"{r['after_mi']:6.1f}{'*' if r['path_differs'] else ' '} {r['loo_min']:6.1f} "
                     f"{r['loo_ratio']:6.3f}{'*' if r['loo_path_differs'] else ''}"
                     + (f"  (LOO fit at bound: {', '.join(r['loo_at_bounds'])})" if r["loo_at_bounds"] else ""))
    s = rep["loo_summary"]
    lines.append(f"  leave-one-out: max |error| {s['max_abs_error']:.3f}, median {s['median_abs_error']:.3f}")
    lines.append(f"vs OSRM, {len(rep['osrm_trips'])} trips, ours / OSRM:")
    lines.append(f"  before: {rep['osrm_before']}")
    lines.append(f"  after:  {rep['osrm_after']}")
    lines.append(f"OSRM requests: {rep['osrm_requests']['sent']} sent, {rep['osrm_requests']['cached']} cached")
    return "\n".join(lines)
