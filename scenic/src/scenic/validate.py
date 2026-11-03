"""Validation of the scenery score (docs/SCORE_SPEC.md section 6): scenic
byways, hand-picked scenic and dull spot checks, and the Rochester to
Syracuse test case. Byways and spot checks are checks only, never inputs.

v0.2 adds the non-routing checks of docs/SCORE_V02.md section 1: V3 (split
dull lists), V4 (byways, reported only), the lake-front diagnostics of its
section 2, and holdout-2, which stays sealed until `declared_final`. The
v0.1 checks are still reported for comparison. V1, V2 and V5 need the route
search and moments of section 4 and are not built."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import numpy as np

from .report import wmean, wquantile
from .score import normalise_name, normalise_ref

DIAG_COLUMNS = ("W", "H", "V", "L", "Q", "C", "penalty", "raw", "pre", "smooth")

ROCHESTER = (43.1566, -77.6088)
SYRACUSE = (43.0481, -76.1474)
CANANDAIGUA = (42.8875, -77.2817)
SKANEATELES = (42.9470, -76.4291)


def ref_set(ref) -> set[str]:
    if ref is None:
        return set()
    return {normalise_ref(r) for r in str(ref).split(";") if r.strip()}


def match_road(refs: np.ndarray, names: np.ndarray, ref: str | None = None, name: str | None = None) -> np.ndarray:
    """Edges whose ref list contains `ref` (normalised) or whose name equals `name`."""
    out = np.zeros(len(refs), bool)
    if ref:
        want = normalise_ref(ref)
        a = np.where(refs == None, "", refs).astype(str)  # noqa: E711
        uniq, inv = np.unique(a, return_inverse=True)
        out |= np.array([want in ref_set(u) for u in uniq])[inv]
    if name:
        want = normalise_name(name)
        a = np.where(names == None, "", names).astype(str)  # noqa: E711
        uniq, inv = np.unique(a, return_inverse=True)
        out |= np.array([normalise_name(u) == want for u in uniq])[inv]
    return out


def in_bbox(lon: np.ndarray, lat: np.ndarray, bbox) -> np.ndarray:
    x0, y0, x1, y1 = bbox
    return (lon >= x0) & (lon <= x1) & (lat >= y0) & (lat <= y1)


def byway_edge_mask(line_xy: list[np.ndarray], sx, sy, sedge, n_edges: int, tol: float = 30.0,
                    step: float = 10.0, min_frac: float = 0.5) -> np.ndarray:
    """Edges with at least `min_frac` of their sample points within `tol` of
    a byway line (lines densified to `step`, projected metres)."""
    from scipy.spatial import cKDTree

    pts = []
    for xy in line_xy:
        if len(xy) < 2:
            continue
        seg = np.hypot(*np.diff(xy, axis=0).T)
        k = np.maximum(1, np.ceil(seg / step).astype(int))
        t = np.concatenate([np.arange(kk) / kk for kk in k])
        i = np.repeat(np.arange(len(seg)), k)
        pts.append(xy[i] + t[:, None] * (xy[i + 1] - xy[i]))
        pts.append(xy[-1:])
    if not pts:
        return np.zeros(n_edges, bool)
    tree = cKDTree(np.concatenate(pts))
    d, _ = tree.query(np.column_stack([sx, sy]), distance_upper_bound=tol, workers=-1)
    near = np.bincount(sedge, weights=np.isfinite(d), minlength=n_edges)
    tot = np.bincount(sedge, minlength=n_edges)
    with np.errstate(invalid="ignore", divide="ignore"):
        return (tot > 0) & (near / np.maximum(tot, 1) >= min_frac)


def byway_metrics(mask, S, L, highway, excluded, min_class_km: float = 20.0) -> dict:
    sel = mask & ~excluded
    act = ~excluded
    if not sel.any():
        return {"km": 0.0, "pass": False}
    med = float(wquantile(S[sel], L[sel], 0.5))
    share = float(L[sel & (S >= 0.5)].sum() / L[sel].sum())
    hw = np.asarray(highway, dtype=object).astype(str)
    classes = {}
    for c in np.unique(hw[sel]):
        csel = sel & (hw == c)
        km = L[csel].sum() / 1000
        if km < min_class_km:
            continue
        bm = float(wquantile(S[csel], L[csel], 0.5))
        sm = float(wquantile(S[act & (hw == c)], L[act & (hw == c)], 0.5))
        classes[c] = {"byway_km": round(km, 1), "byway_median_S": round(bm, 3), "state_median_S": round(sm, 3),
                      "pass": bm > sm}
    checks = {"median_S_ge_0.70": med >= 0.70, "share_S_ge_0.5_ge_60pct": share >= 0.60,
              "classes_beat_state_median": all(v["pass"] for v in classes.values())}
    return {"km": round(float(L[sel].sum() / 1000), 1), "edges": int(sel.sum()), "median_S": round(med, 3),
            "mean_S": round(wmean(S[sel], L[sel]), 3), "share_S_ge_0.5": round(share, 3), "classes": classes,
            "checks": checks, "pass": all(checks.values())}


def load_byway_lines(path: Path):
    """(features, projected line arrays per feature) from a GeoJSON file."""
    from .score import project_lonlat

    gj = json.loads(path.read_text())
    feats, lines = [], []
    for f in gj["features"]:
        g = f.get("geometry") or {}
        parts = g.get("coordinates") or []
        if g.get("type") == "LineString":
            parts = [parts]
        elif g.get("type") != "MultiLineString":
            continue
        xy = []
        for p in parts:
            a = np.asarray(p, dtype=np.float64)[:, :2]
            x, y = project_lonlat(a[:, 0], a[:, 1])
            xy.append(np.column_stack([x, y]))
        feats.append(f.get("properties") or {})
        lines.append(xy)
    return feats, lines


def designated(props: dict) -> bool:
    """A NYSDOT feature designated as a State or National Scenic Byway (not a
    historic parkway or a route-code-only row)."""
    vals = " ".join(str(props.get(k) or "") for k in ("DSG1", "DSG2", "DSG3", "DESIGNATS"))
    return "scenic byway" in vals.lower() or "all-american" in vals.lower()


def _check_list(items_cfg, S, L, excluded, refs, names, mlon, mlat, diag, thr: float, want: str,
                need: int) -> dict:
    items = []
    for it in items_cfg:
        m = match_road(refs, names, it.get("ref"), it.get("name")) & in_bbox(mlon, mlat, it["bbox"]) & ~excluded
        km = float(L[m].sum() / 1000)
        s = wmean(S[m], L[m]) if m.any() else float("nan")
        ok = bool(m.any() and (s >= thr if want == "ge" else s <= thr))
        row = {"label": it["label"], "km": round(km, 2), "edges": int(m.sum()),
               "S": round(s, 3) if m.any() else None, "pass": ok}
        row["signals"] = {c: round(wmean(diag[c][m], L[m]), 3) for c in DIAG_COLUMNS} if m.any() else {}
        items.append(row)
    n_ok = sum(r["pass"] for r in items)
    return {"threshold": thr, "passed": n_ok, "of": len(items), "need": need, "pass": n_ok >= need,
            "items": items}


def spot_checks(cfg: dict, S, L, excluded, refs, names, mlon, mlat, diag) -> dict:
    """The scenic and dull lists (need cfg thresholds.need of 10 each) and,
    when present, the held-out lists (need 80 % of each)."""
    out = {}
    args = (S, L, excluded, refs, names, mlon, mlat, diag)
    for kind, want in (("scenic", "ge"), ("dull", "le")):
        out[kind] = _check_list(cfg[kind], *args, cfg["thresholds"][kind], want, cfg["thresholds"]["need"])
    flat = next((r for r in out["dull"]["items"] if r["label"].startswith("NY-5 Batavia")), None)
    if flat and flat["S"] is not None:
        out["flat_farmland_calibration"] = {"S": flat["S"], "below_0.40": flat["S"] < 0.40,
                                            "not_under_0.15": flat["S"] >= 0.15}
    if "holdout" in cfg:
        out["holdout"] = {}
        for kind, want in (("scenic", "ge"), ("dull", "le")):
            lst = cfg["holdout"].get(kind, [])
            out["holdout"][kind] = _check_list(lst, *args, cfg["thresholds"][kind], want,
                                               int(np.ceil(0.8 * len(lst))))
    return out


def _match(it, refs, names, mlon, mlat, excluded) -> np.ndarray:
    return match_road(refs, names, it.get("ref"), it.get("name")) & in_bbox(mlon, mlat, it["bbox"]) & ~excluded


def _band_list(items_cfg, S, L, excluded, refs, names, mlon, mlat, lo: float, hi: float, need: int) -> dict:
    """Each item passes when its length-weighted mean S lies in [lo, hi]."""
    items = []
    for it in items_cfg:
        m = _match(it, refs, names, mlon, mlat, excluded)
        s = wmean(S[m], L[m]) if m.any() else float("nan")
        items.append({"label": it["label"], "km": round(float(L[m].sum() / 1000), 2), "edges": int(m.sum()),
                      "S": round(s, 3) if m.any() else None, "pass": bool(m.any() and lo <= s <= hi)})
    n_ok = sum(r["pass"] for r in items)
    return {"band": [lo, hi], "passed": n_ok, "of": len(items), "need": need, "pass": n_ok >= need,
            "items": items}


def v3_lists(cfg: dict, S, L, excluded, refs, names, mlon, mlat) -> dict:
    """SCORE_V02 V3: scenic >= 0.65 (8 of 10); urban strips <= 0.20 (6 of the
    7 non-motorway, non-farmland dull items); flat rural roads (dull and
    holdout-1 items with kind = "flat") in [0.20, 0.50], all of them. The
    motorway items are reported against the urban bound but not counted."""
    v = cfg["v3"]
    a = (S, L, excluded, refs, names, mlon, mlat)
    dull = [dict(it) for it in cfg["dull"]]
    flat = [it for it in dull + list(cfg.get("holdout", {}).get("dull", [])) if it.get("kind") == "flat"]
    urban = [it for it in dull if it.get("kind", "urban") == "urban"]
    motorway = [it for it in dull if it.get("kind") == "motorway"]
    out = {"scenic": _band_list(cfg["scenic"], *a, v["scenic_min"], 1.0, v["scenic_need"]),
           "urban": _band_list(urban, *a, 0.0, v["urban_max"], v["urban_need"]),
           "flat": _band_list(flat, *a, *v["flat_band"], len(flat)),
           "motorway_reported": _band_list(motorway, *a, 0.0, v["urban_max"], 0)}
    out["motorway_reported"].pop("pass")
    out["pass"] = all(out[k]["pass"] for k in ("scenic", "urban", "flat"))
    return out


def holdout2(cfg: dict, S, L, excluded, refs, names, mlon, mlat, diag) -> dict:
    """Holdout-2 (SCORE_V02 section 1). Sealed until `declared_final` is set:
    nothing is computed on it before then."""
    h = cfg.get("holdout2")
    if not h:
        return {"status": "absent"}
    if not h.get("declared_final", False):
        return {"status": "sealed", "items": {k: len(h.get(k, [])) for k in ("scenic", "urban", "flat", "od")}}
    a = (S, L, excluded, refs, names, mlon, mlat)
    out = {"status": "scored",
           "scenic": _band_list(h["scenic"], *a, h["scenic_min"], 1.0, int(np.ceil(0.8 * len(h["scenic"])))),
           "urban": _band_list(h["urban"], *a, 0.0, h["urban_max"], len(h["urban"]) - 1),
           "flat": _band_list(h["flat"], *a, *h["flat_band"], len(h["flat"])),
           "od": {"status": "not run (no route search yet)", "pairs": [o["label"] for o in h.get("od", [])]}}
    for k in ("scenic", "urban", "flat"):
        for row, it in zip(out[k]["items"], h[k]):
            m = _match(it, refs, names, mlon, mlat, excluded)
            row["signals"] = {c: round(wmean(diag[c][m], L[m]), 3) for c in diag} if m.any() else {}
    out["pass"] = all(out[k]["pass"] for k in ("scenic", "urban", "flat"))
    return out


def fronts(items_cfg, S, L, excluded, refs, names, mlon, mlat, cols: dict) -> list[dict]:
    """Length-weighted signals and S on named lake fronts (SCORE_V02 section 2)."""
    out = []
    for it in items_cfg:
        m = _match(it, refs, names, mlon, mlat, excluded)
        row = {"label": it["label"], "km": round(float(L[m].sum() / 1000), 2), "edges": int(m.sum())}
        if m.any():
            row.update({c: round(wmean(x[m], L[m]), 3) for c, x in cols.items()})
            row["S"] = round(wmean(S[m], L[m]), 3)
            row["S_max_edge"] = round(float(S[m].max()), 3)
        out.append(row)
    return out


def byway_neighbours(groups: dict[str, np.ndarray], any_byway: np.ndarray, S, L, hw, dev, excluded,
                     ex, ey, radius_m: float = 15000.0, dev_max: float = 0.3) -> dict:
    """SCORE_V02 V4b. For each byway, the length-weighted median S of its rural
    edges (dev <= dev_max) against non-byway rural edges of the same highway
    classes within radius_m of it. Neighbours are reweighted to the byway's
    class mix (per class: byway km / neighbour km), so a primary-heavy byway
    is compared with primaries."""
    from scipy.spatial import cKDTree

    hw = np.asarray(hw, dtype=object).astype(str)
    rural = ~excluded & (dev <= dev_max)
    pool = rural & ~any_byway
    out = {}
    for name, mask in groups.items():
        sel = mask & rural
        if L[sel].sum() < 1000:
            out[name] = {"rural_km": round(float(L[sel].sum() / 1000), 1), "beats": None}
            continue
        tree = cKDTree(np.column_stack([ex[sel], ey[sel]]))
        cand = np.flatnonzero(pool)
        d, _ = tree.query(np.column_stack([ex[cand], ey[cand]]), distance_upper_bound=radius_m, workers=-1)
        near = cand[np.isfinite(d)]
        bw, nw, bS, nS = [], [], [], []
        for c in np.unique(hw[sel]):
            bc = sel & (hw == c)
            nc = near[hw[near] == c]
            if not len(nc):
                continue
            bw.append(L[bc]); bS.append(S[bc])
            nw.append(L[nc] * (L[bc].sum() / L[nc].sum())); nS.append(S[nc])
        if not bw:
            out[name] = {"rural_km": round(float(L[sel].sum() / 1000), 1), "beats": None}
            continue
        bm = float(wquantile(np.concatenate(bS), np.concatenate(bw), 0.5))
        nm = float(wquantile(np.concatenate(nS), np.concatenate(nw), 0.5))
        out[name] = {"rural_km": round(float(L[sel].sum() / 1000), 1), "median_S": round(bm, 3),
                     "neighbour_median_S": round(nm, 3), "gap": round(bm - nm, 3), "beats": bm > nm}
    return out


def byways_v02(feats, lines, cfg: dict, sx, sy, se, S, L, hw, dev, excluded, ex, ey) -> dict:
    """V4 (reported only): landscape-subset median and the per-byway
    neighbour comparison; heritage byways are listed, never thresholded."""
    names = [str(p.get("SB") or "").strip() for p in feats]
    groups: dict[str, np.ndarray] = {}
    any_mask = byway_edge_mask([xy for f in lines for xy in f], sx, sy, se, len(S))
    for nm in sorted({n for n, p in zip(names, feats) if designated(p)}):
        xy = [a for n, p, f in zip(names, feats, lines) if n == nm and designated(p) for a in f]
        groups[nm] = byway_edge_mask(xy, sx, sy, se, len(S))
    land = set(cfg["landscape"])
    missing = sorted(land - set(groups))
    lmask = np.zeros(len(S), bool)
    for nm in land & set(groups):
        lmask |= groups[nm]
    sel = lmask & ~excluded
    per = {}
    for nm, m in groups.items():
        s = m & ~excluded
        per[nm] = {"km": round(float(L[s].sum() / 1000), 1),
                   "median_S": round(float(wquantile(S[s], L[s], 0.5)), 3) if s.any() else None,
                   "group": "landscape" if nm in land else "heritage"}
    nb = byway_neighbours(groups, any_mask, S, L, hw, dev, excluded, ex, ey, cfg["neighbour_radius_m"],
                          cfg["rural_dev_max"])
    n_beat = sum(1 for v in nb.values() if v["beats"])
    med = float(wquantile(S[sel], L[sel], 0.5)) if sel.any() else float("nan")
    return {"landscape": {"byways": len(land & set(groups)), "missing": missing,
                          "km": round(float(L[sel].sum() / 1000), 1), "median_S": round(med, 3),
                          "target": cfg["landscape_median_min"], "pass": med >= cfg["landscape_median_min"]},
            "neighbours": {"beat": n_beat, "of": len(nb), "target": cfg["neighbours_need"],
                           "pass": n_beat >= cfg["neighbours_need"],
                           "median_gap": round(float(np.median([v["gap"] for v in nb.values() if v["beats"] is not None])), 3),
                           "per_byway": nb},
            "per_byway": per}


def roc_syr(graph_dir: Path, S: np.ndarray, L: np.ndarray, refs: np.ndarray) -> dict:
    """Mean S along the fastest Rochester -> Syracuse route, and along US-20
    (with NY-5 where they share the road) from Canandaigua to Skaneateles."""
    from .route import RoadGraph

    g = RoadGraph.load(graph_dir)
    out = {}
    r, _ = g.fastest(ROCHESTER, SYRACUSE)
    e = r.edges
    out["fastest"] = {"minutes": round(r.minutes, 1), "km": round(r.km, 1),
                      "mean_S_by_length": round(wmean(S[e], L[e]), 3),
                      "mean_S_by_time": round(wmean(S[e], g.time_s[e]), 3),
                      "share_km_S_ge_0.5": round(float(L[e][S[e] >= 0.5].sum() / L[e].sum()), 3),
                      "roads": [(lab, round(km, 1)) for lab, km in g.road_summary(r)]}
    on20 = match_road(refs, refs, ref="US 20")
    cost = g.time_s * np.where(on20, 1.0, 50.0)
    r2, snap = g.fastest(CANANDAIGUA, SKANEATELES, edge_cost=cost)
    e2 = r2.edges
    k = on20[e2]
    out["us20_canandaigua_skaneateles"] = {
        "minutes": round(r2.minutes, 1), "km": round(r2.km, 1), "km_on_us20": round(float(L[e2][k].sum() / 1000), 1),
        "mean_S_by_length": round(wmean(S[e2], L[e2]), 3),
        "mean_S_us20_edges": round(wmean(S[e2][k], L[e2][k]), 3),
        "share_km_S_ge_0.5": round(float(L[e2][S[e2] >= 0.5].sum() / L[e2].sum()), 3),
        "snap_m": [round(snap["from_snap_m"]), round(snap["to_snap_m"])],
        "roads": [(lab, round(km, 1)) for lab, km in g.road_summary(r2)]}
    return out


def run_validation(graph_dir: Path, out_dir: Path, raw_dir: Path, config_dir: Path) -> dict:
    import pyarrow.parquet as pq

    from .score import _dict_col, load

    t = load(graph_dir / "edges.parquet", out_dir / "scores.parquet")
    S = t.column("S").to_numpy().astype(np.float64)
    excluded = t.column("excluded").to_numpy()
    diag = {c: t.column(c).to_numpy().astype(np.float64) for c in DIAG_COLUMNS}
    et = pq.read_table(graph_dir / "edges.parquet", columns=["length_m", "highway", "ref", "name", "lon", "lat"])
    L = et.column("length_m").to_numpy()
    hw, refs, names = _dict_col(et, "highway"), _dict_col(et, "ref"), _dict_col(et, "name")
    lon = et.column("lon").combine_chunks()
    lat = et.column("lat").combine_chunks()
    off = lon.offsets.to_numpy()
    mid = (off[:-1] + off[1:] - 1) // 2
    mlon, mlat = lon.values.to_numpy()[mid], lat.values.to_numpy()[mid]
    from .graph import read_edges_fingerprint
    from .score import load_samples

    smp = load_samples(out_dir / "samples.npz", read_edges_fingerprint(graph_dir))
    res: dict = {}

    byway_files = [raw_dir / "byways" / f"{s}_scenic_byways.geojson" for s in ("nysdot", "usdot")]
    path = next((p for p in byway_files if p.exists()), None)
    if path is not None:
        feats, lines = load_byway_lines(path)
        sx, sy, se = smp["x"].astype(np.float64), smp["y"].astype(np.float64), smp["edge"]
        all_mask = byway_edge_mask([xy for f in lines for xy in f], sx, sy, se, len(S))
        des = [xy for p, f in zip(feats, lines) if designated(p) for xy in f]
        des_mask = byway_edge_mask(des, sx, sy, se, len(S))
        res["byways"] = {"source": path.name, "features": len(feats),
                         **byway_metrics(all_mask, S, L, hw, excluded),
                         "designated_only": {"features": sum(designated(p) for p in feats),
                                             **byway_metrics(des_mask, S, L, hw, excluded)}}
    else:
        cfg = tomllib.loads((config_dir / "byways.toml").read_text())
        mask = np.zeros(len(S), bool)
        for b in cfg["byway"]:
            for ref in b["refs"]:
                mask |= match_road(refs, names, ref=ref) & in_bbox(mlon, mlat, b["bbox"])
        res["byways"] = {"source": "config/byways.toml (ref fallback)", **byway_metrics(mask, S, L, hw, excluded)}

    sc = tomllib.loads((config_dir / "spot_checks.toml").read_text())
    res["spot_checks"] = spot_checks(sc, S, L, excluded, refs, names, mlon, mlat, diag)
    res["v3"] = v3_lists(sc, S, L, excluded, refs, names, mlon, mlat)
    res["holdout2"] = holdout2(sc, S, L, excluded, refs, names, mlon, mlat, diag)
    from .score import read_edge_signals

    sig = read_edge_signals(out_dir / "edge_signals.parquet", read_edges_fingerprint(graph_dir))
    vwater, dev = sig.column("V_water").to_numpy(), sig.column("dev").to_numpy()
    if path is not None and "v4" in sc:
        from .score import project_lonlat

        ex, ey = project_lonlat(mlon, mlat)
        res["v4"] = byways_v02(feats, lines, sc["v4"], sx, sy, se, S, L, hw, dev, excluded, ex, ey)
    cols = {"W": diag["W"], "V_water": vwater, "raw": diag["raw"], "penalty": diag["penalty"],
            "pre": diag["pre"], "smooth": diag["smooth"]}
    res["fronts"] = fronts(sc.get("fronts", []), S, L, excluded, refs, names, mlon, mlat, cols)
    try:
        res["rochester_syracuse"] = roc_syr(graph_dir, S, L, refs)
    except Exception as e:  # noqa: BLE001 - informational only
        res["rochester_syracuse"] = {"error": repr(e)}
    return res
