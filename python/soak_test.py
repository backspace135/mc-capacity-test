#!/usr/bin/env python3
"""Long-duration stability soak at a fixed entity count.

Samples MSPT/TPS and host CPU steal through the same RCON and metric
primitives as :mod:`capacity_test`, writing a durable CSV for methodology
analysis.  The server is never emulated; a real RCON connection is required
when the soak is run.
"""
import argparse
import csv
import glob
import os
import statistics
import time
from datetime import datetime

from capacity_gradient import PRESETS, spawn
from benchmark_metrics import count_entities, read_cpu_steal, sample_mspt
from rcon_client import Rcon


def latest_capacity_estimate(results_dir, explicit=None):
    """Return the latest numeric capacity estimate from a capacity-gradient summary.

    ``explicit`` may be a CSV path or a numeric value.  Summary rows are
    considered only when valid and the largest observed count is selected.
    """
    if explicit is not None:
        try:
            return float(explicit)
        except (TypeError, ValueError):
            paths = [explicit]
    else:
        paths = sorted(glob.glob(os.path.join(results_dir, "summary-*.csv")))
    for path in reversed(paths):
        try:
            with open(path, newline="") as f:
                rows = list(csv.DictReader(f))
        except OSError:
            continue
        vals = []
        for row in rows:
            try:
                if str(row.get("valid", "1")).lower() in ("0", "false", "no"):
                    continue
                vals.append(float(row["count"]))
            except (KeyError, TypeError, ValueError):
                continue
        if vals:
            return max(vals)
    return None


def soak_target(results_dir, count=None, capacity_percent=80.0, estimate=None):
    """Resolve a positive integer target from count or a capacity percentage."""
    if count is not None:
        target = float(count)
    else:
        cap = latest_capacity_estimate(results_dir, estimate)
        if cap is None:
            raise ValueError("no capacity estimate; provide --count or --capacity-estimate")
        target = cap * float(capacity_percent) / 100.0
    if target <= 0:
        raise ValueError("target entity count must be positive")
    return max(1, int(round(target)))


def run_soak(rc, preset, target, duration, interval, output, keep_entities=False):
    """Run a soak and return sample rows plus a drift summary.

    ``output`` is a CSV path or a writable file-like object.  Cleanup is
    attempted in ``finally`` so save-on/forceload state is restored on errors.
    """
    p = PRESETS[preset]
    own_file = isinstance(output, (str, bytes, os.PathLike))
    sf = open(output, "w", newline="") if own_file else output
    writer = csv.DictWriter(sf, fieldnames=["timestamp", "elapsed_s", "mspt_p50", "mspt_p95", "mspt_max", "tps", "steal_pct", "entity_count", "valid"])
    writer.writeheader()
    rows = []
    state = {"spark": True}
    previous_cpu = read_cpu_steal()
    started = time.time()
    try:
        current = count_entities(rc, p["etype"]) or 0
        if current < target:
            spawn(rc, preset, target - current)
        while time.time() - started < duration:
            now = time.time()
            metric = sample_mspt(rc, state)
            cpu = read_cpu_steal()
            steal = None
            if cpu and previous_cpu and cpu[0] > previous_cpu[0]:
                steal = round(100.0 * (cpu[1] - previous_cpu[1]) / (cpu[0] - previous_cpu[0]), 2)
            previous_cpu = cpu
            entities = count_entities(rc, p["etype"])
            valid = entities is not None and entities >= target * 0.98
            row = {"timestamp": datetime.now().isoformat(timespec="seconds"), "elapsed_s": round(now - started, 1),
                   "mspt_p50": metric.get("p50"), "mspt_p95": metric.get("p95"), "mspt_max": metric.get("mx"),
                   "tps": metric.get("tps"), "steal_pct": steal, "entity_count": entities, "valid": int(valid)}
            writer.writerow(row); sf.flush(); rows.append(row)
            time.sleep(max(0, interval))
    finally:
        try:
            if not keep_entities:
                rc.cmd(f"kill @e[type={p['etype']}]")
            rc.cmd("forceload remove all")
            rc.cmd("save-on")
        finally:
            if own_file:
                sf.close()
    p95 = [float(r["mspt_p95"]) for r in rows if r["valid"] and r["mspt_p95"] is not None]
    steals = [float(r["steal_pct"]) for r in rows if r["steal_pct"] is not None]
    summary = {"samples": len(rows), "p95_median": statistics.median(p95) if p95 else None,
               "p95_iqr": (statistics.quantiles(p95, n=4)[2] - statistics.quantiles(p95, n=4)[0]) if len(p95) >= 4 else None,
               "steal_median": statistics.median(steals) if steals else None,
               "steal_over_2": any(x > 2 for x in steals)}
    return rows, summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, help="fixed entity count (overrides percentage)")
    ap.add_argument("--capacity-percent", type=float, default=80.0, help="percentage of latest capacity estimate (default 80)")
    ap.add_argument("--capacity-estimate", help="numeric estimate or capacity-gradient summary CSV")
    ap.add_argument("--duration", type=float, default=3600, help="soak duration seconds (default 3600)")
    ap.add_argument("--interval", type=float, default=60, help="sample interval seconds (default 60)")
    ap.add_argument("--preset", choices=PRESETS, default="zombie")
    ap.add_argument("--host", default="127.0.0.1"); ap.add_argument("--port", type=int, default=25575)
    ap.add_argument("--password", default=None); ap.add_argument("--server-dir", default="/opt/purpur-test")
    ap.add_argument("--outdir", default=None); ap.add_argument("--output", default=None)
    ap.add_argument("--keep-entities", action="store_true")
    args = ap.parse_args(argv)
    password = args.password or os.environ.get("RCON_PASSWORD")
    if not password:
        try:
            with open(os.path.join(args.server_dir, "server.properties")) as f:
                password = next((x.split("=", 1)[1].strip() for x in f if x.startswith("rcon.password=")), None)
        except OSError: pass
    if not password: ap.error("RCON password required via --password, RCON_PASSWORD, or server.properties")
    outdir = args.outdir or os.path.join(args.server_dir, "results"); os.makedirs(outdir, exist_ok=True)
    target = soak_target(outdir, args.count, args.capacity_percent, args.capacity_estimate)
    output = args.output or os.path.join(outdir, "soak-%s.csv" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    rc = Rcon(args.host, args.port, password); rc.connect()
    _, summary = run_soak(rc, args.preset, target, args.duration, args.interval, output, args.keep_entities)
    print("soak target=%d samples=%d P95 median=%s IQR=%s" % (target, summary["samples"], summary["p95_median"], summary["p95_iqr"]))
    if summary["steal_over_2"]: print("WARNING: CPU steal exceeded 2%%")


if __name__ == "__main__":
    main()
