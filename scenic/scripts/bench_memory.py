#!/usr/bin/env python3
"""Benchmark `scenic serve` RSS and wall time across warmup, routes, waypoints, and loops.

Starts `scenic serve` on 127.0.0.1:8793 (by default), samples child RSS at 5 ms
intervals, drives API requests with `curl`, and prints peak/steady RSS and wall
time per stage. Optionally saves or compares canonical response JSONs to verify
identical routing results across code changes.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

DEFAULT_PORT = 8793

REQUESTS = [
    ("route_roc_wg", "Rochester -> Watkins Glen", "/api/route", {
        "from": [43.1566, -77.6088],
        "to": [42.3806, -76.8733],
    }),
    ("route_alb_roc", "Albany -> Rochester", "/api/route", {
        "from": [42.6526, -73.7562],
        "to": [43.1566, -77.6088],
    }),
    ("route_man_buf", "Manhattan -> Buffalo", "/api/route", {
        "from": [40.7580, -73.9855],
        "to": [42.8864, -78.8784],
    }),
    ("wp_man_buf_r3", "Waypoints Manhattan -> Buffalo (r3)", "/api/waypoints", {
        "id": "$route_man_buf",
        "key": "r3",
    }),
    ("loop_roc_120_n", "Loop Rochester 120m N", "/api/loop", {
        "start": [43.1566, -77.6088],
        "minutes": 120,
        "steer": "compass",
        "direction": "N",
    }),
    ("loop_man_180_any", "Loop Manhattan 180m any", "/api/loop", {
        "start": [40.7580, -73.9855],
        "minutes": 180,
        "steer": "any",
    }),
]


def _make_rss_reader():
    """Return `fn(pid) -> (rss_bytes, cpu_ns)` using fast OS primitives or `ps`."""
    if sys.platform == "darwin":
        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib")

            class ProcTaskInfo(ctypes.Structure):
                _fields_ = [
                    ("pti_virtual_size", ctypes.c_uint64),
                    ("pti_resident_size", ctypes.c_uint64),
                    ("pti_total_user", ctypes.c_uint64),
                    ("pti_total_system", ctypes.c_uint64),
                    ("pti_threads_user", ctypes.c_uint64),
                    ("pti_threads_system", ctypes.c_uint64),
                    ("pti_policy", ctypes.c_int32),
                    ("pti_faults", ctypes.c_int32),
                    ("pti_pageins", ctypes.c_int32),
                    ("pti_cow_faults", ctypes.c_int32),
                    ("pti_messages_sent", ctypes.c_int32),
                    ("pti_messages_received", ctypes.c_int32),
                    ("pti_syscalls_mach", ctypes.c_int32),
                    ("pti_syscalls_unix", ctypes.c_int32),
                    ("pti_csw", ctypes.c_int32),
                    ("pti_threadnum", ctypes.c_int32),
                    ("pti_numrunning", ctypes.c_int32),
                    ("pti_priority", ctypes.c_int32),
                ]

            sz = ctypes.sizeof(ProcTaskInfo)

            def read_darwin(pid: int) -> tuple[int, int]:
                ti = ProcTaskInfo()
                n = libproc.proc_pidinfo(pid, 4, 0, ctypes.byref(ti), sz)
                if n != sz:
                    return 0, 0
                return int(ti.pti_resident_size), int(ti.pti_total_user + ti.pti_total_system)

            if read_darwin(os.getpid())[0] > 0:
                return read_darwin
        except OSError:
            pass

    if sys.platform.startswith("linux"):
        page = os.sysconf("SC_PAGE_SIZE")
        clk = os.sysconf("SC_CLK_TCK")

        def read_linux(pid: int) -> tuple[int, int]:
            try:
                stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                rss = int(stat[21]) * page
                cpu = int((int(stat[11]) + int(stat[12])) * 1e9 / clk)
                return rss, cpu
            except (OSError, IndexError, ValueError):
                return 0, 0

        return read_linux

    def read_ps(pid: int) -> tuple[int, int]:
        try:
            out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)], text=True).strip()
            return (int(out) * 1024 if out else 0), 0
        except (OSError, subprocess.SubprocessError, ValueError):
            return 0, 0

    return read_ps


class RssSampler:
    def __init__(self, pid: int, interval_s: float = 0.005):
        self.pid = pid
        self.interval_s = interval_s
        self._read = _make_rss_reader()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.current = 0
        self.cpu_ns = 0
        self.window_peak = 0
        self.overall_peak = 0
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def reset_window(self) -> int:
        rss, cpu = self._read(self.pid)
        with self._lock:
            if rss > 0:
                self.current = rss
                self.cpu_ns = cpu
                self.overall_peak = max(self.overall_peak, rss)
            self.window_peak = self.current
            return self.current

    def snapshot(self) -> tuple[int, int, int]:
        rss, cpu = self._read(self.pid)
        with self._lock:
            if rss > 0:
                self.current = rss
                self.cpu_ns = cpu
                self.window_peak = max(self.window_peak, rss)
                self.overall_peak = max(self.overall_peak, rss)
            return self.current, self.window_peak, self.cpu_ns

    def _run(self) -> None:
        while not self._stop.is_set():
            rss, cpu = self._read(self.pid)
            if rss > 0:
                with self._lock:
                    self.current = rss
                    self.cpu_ns = cpu
                    if rss > self.window_peak:
                        self.window_peak = rss
                    if rss > self.overall_peak:
                        self.overall_peak = rss
            self._stop.wait(self.interval_s)


def curl_json(method: str, url: str, body: dict | None = None, timeout: int = 180,
              quiet_err: bool = False, allow_http_error: bool = False) -> dict:
    cmd = ["curl", "-sS" if not allow_http_error else "-s", "--max-time", str(timeout), "-X", method]
    if not allow_http_error:
        cmd.insert(1, "-f")
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    cmd.append(url)
    out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL if quiet_err else None)
    return json.loads(out)


def canonicalize(obj: dict) -> dict:
    """Strip ephemeral timing/id fields so route choices and geometries compare cleanly."""
    d = json.loads(json.dumps(obj))
    for k in ("id", "search_s", "seconds", "load_s"):
        d.pop(k, None)
    return d


def fmt_gb(b: int) -> str:
    return f"{b / (1024 ** 3):.3f}"


def fmt_mb(b: int) -> str:
    return f"{b / (1024 ** 2):.1f}"


def wait_for_ready_and_warmup(base_url: str, sampler: RssSampler) -> tuple[dict, dict]:
    t0 = time.monotonic()
    sampler.reset_window()
    while True:
        try:
            h = curl_json("GET", f"{base_url}/api/health", timeout=5, quiet_err=True)
            if h.get("status") == "ready":
                break
            if h.get("status") == "error":
                raise RuntimeError(f"server failed to load: {h}")
        except (subprocess.SubprocessError, ValueError):
            pass
        if time.monotonic() - t0 > 120:
            raise TimeoutError("timed out waiting for /api/health ready")
        time.sleep(0.02)

    ready_s = time.monotonic() - t0
    ready_cur, ready_peak, _ = sampler.snapshot()
    ready_row = {
        "stage": "ready",
        "label": "Context loaded (ready)",
        "wall_s": ready_s,
        "peak_bytes": ready_peak,
        "steady_bytes": ready_cur,
    }

    # `App.load` sets `status = "ready"` right before `warm_waypoints()`, which holds `app.lock`.
    # Sending an off-road snap point inside NY_BBOX acquires `app.lock`, checks one KD-tree snap,
    # and returns 422 as soon as `warm_waypoints()` finishes, with negligible allocation.
    t_warm = time.monotonic()
    sampler.reset_window()
    curl_json(
        "POST",
        f"{base_url}/api/route",
        body={"from": [43.6, -77.6], "to": [43.6, -77.6]},
        timeout=120,
        allow_http_error=True,
    )
    warm_s = time.monotonic() - t_warm
    time.sleep(0.05)
    warm_cur, warm_peak, _ = sampler.snapshot()
    warm_row = {
        "stage": "warmup",
        "label": "App.warm_waypoints()",
        "wall_s": warm_s,
        "peak_bytes": warm_peak,
        "steady_bytes": warm_cur,
    }
    return ready_row, warm_row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--save-json", type=Path, help="directory to save canonical response JSONs")
    ap.add_argument("--compare-json", type=Path, help="directory of baseline JSONs to verify against")
    ap.add_argument("--json-out", type=Path, help="write benchmark summary JSON to this path")
    args = ap.parse_args()

    if args.port == 8765:
        raise SystemExit("Refusing to run benchmark on default port 8765; use 8793.")

    base_url = f"http://127.0.0.1:{args.port}"
    cmd = [sys.executable, "-m", "scenic.cli", "serve", "--port", str(args.port)]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    sampler = RssSampler(proc.pid, interval_s=0.005)
    sampler.start()

    rows = []
    diffs = []
    try:
        ready_row, warm_row = wait_for_ready_and_warmup(base_url, sampler)
        rows.extend([ready_row, warm_row])

        ids: dict[str, str] = {}
        if args.save_json:
            args.save_json.mkdir(parents=True, exist_ok=True)

        for slug, label, endpoint, raw_body in REQUESTS:
            body = dict(raw_body)
            if isinstance(body.get("id"), str) and body["id"].startswith("$"):
                ref = body["id"][1:]
                body["id"] = ids[ref]

            sampler.reset_window()
            t0 = time.monotonic()
            resp = curl_json("POST", f"{base_url}{endpoint}", body=body)
            wall_s = time.monotonic() - t0
            time.sleep(0.05)
            steady_b, peak_b, _ = sampler.snapshot()

            if "id" in resp:
                ids[slug] = resp["id"]

            rows.append({
                "stage": slug,
                "label": label,
                "wall_s": wall_s,
                "peak_bytes": peak_b,
                "steady_bytes": steady_b,
            })

            canon = canonicalize(resp)
            if args.save_json:
                (args.save_json / f"{slug}.json").write_text(json.dumps(canon, indent=2, sort_keys=True))
            if args.compare_json:
                ref_path = args.compare_json / f"{slug}.json"
                ref_obj = json.loads(ref_path.read_text())
                if canon != ref_obj:
                    diffs.append(slug)
    finally:
        sampler.stop()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    print(f"{'Stage':<36s} {'Peak GiB':>9s} {'Peak MB':>9s} {'Steady GiB':>10s} {'Wall (s)':>9s}")
    print("-" * 77)
    for r in rows:
        print(
            f"{r['label']:<36s} "
            f"{fmt_gb(r['peak_bytes']):>9s} "
            f"{fmt_mb(r['peak_bytes']):>9s} "
            f"{fmt_gb(r['steady_bytes']):>10s} "
            f"{r['wall_s']:>9.2f}"
        )
    print("-" * 77)
    print(f"{'Overall peak':<36s} {fmt_gb(sampler.overall_peak):>9s} {fmt_mb(sampler.overall_peak):>9s}")

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps({
            "overall_peak_bytes": sampler.overall_peak,
            "rows": rows,
            "diffs": diffs,
        }, indent=2))

    if args.compare_json:
        if diffs:
            print(f"JSON MISMATCH on: {', '.join(diffs)}", file=sys.stderr)
            return 1
        print("JSON comparison: IDENTICAL across all 6 requests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
