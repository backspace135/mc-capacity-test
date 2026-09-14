"""Measurement primitives for Minecraft capacity benchmarks.

All functions are importable and intentionally avoid CLI setup or side effects
(other than the requested RCON commands, sleeping, and Linux proc read).
"""
import re
import statistics
import time

from rcon_client import strip_colors


DUR_RE = re.compile(r"(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)")


def sample_mspt(rc, state):
    """Return ``dict(tps, p50, p95, mx, source)`` using spark or tick query."""
    if state.get("spark", True):
        out = strip_colors(rc.cmd("spark tps"))
        if "Tick durations" in out:
            tps = None
            m = re.search(r"TPS from[^:]*:\s*([^\n]*)", out)
            if m:
                nums = re.findall(r"[\d.]+", m.group(1).replace("*", ""))
                if nums:
                    tps = float(nums[0])
            m = DUR_RE.search(out.split("Tick durations", 1)[1])
            if m:
                mn, med, p95, mx = map(float, m.groups())
                return dict(tps=tps, p50=med, p95=p95, mx=mx, source="spark")
        state["spark"] = False
        print("  [!] spark 不可用,改用 /tick query(精度 0.1ms)")
    out = strip_colors(rc.cmd("tick query"))
    p50 = re.search(r"P50:\s*([\d.]+)\s*ms", out)
    p95 = re.search(r"P95:\s*([\d.]+)\s*ms", out)
    avg = re.search(r"([\d.]+)\s*ms", out)
    if not p95:
        raise RuntimeError(f"无法解析 tick query 输出: {out[:200]!r}")
    p50v = float(p50.group(1)) if p50 else (float(avg.group(1)) if avg else None)
    tps = None
    if p50v is not None:
        tps = 20.0 if p50v <= 50 else round(1000.0 / p50v, 1)
    return dict(tps=tps, p50=p50v, p95=float(p95.group(1)), mx=None, source="tick-query")


def read_cpu_steal():
    """Return ``(total_jiffies, steal_jiffies)``; non-Linux returns ``None``."""
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        vals = list(map(int, parts[1:]))
        return sum(vals), (vals[7] if len(vals) > 7 else 0)
    except OSError:
        return None


def count_entities(rc, etype):
    out = strip_colors(rc.cmd(f"execute if entity @e[type={etype}]"))
    m = re.search(r"count:\s*(\d+)", out, re.IGNORECASE)
    return int(m.group(1)) if m else None


def linfit(pts):
    """Fit ``p95 = a + b*count``; return ``(a, b, r2)`` or ``None``."""
    n = len(pts)
    if n < 2:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return a, b, r2


def warmup_stable(rc, state, max_s, interval=5, k=3, tol=0.10):
    """Warm up until *k* P95 samples have range/median below *tol* (minimum 15s)."""
    t0 = time.time()
    recent = []
    while time.time() - t0 < max_s:
        time.sleep(interval)
        recent = (recent + [sample_mspt(rc, state)["p95"]])[-k:]
        if time.time() - t0 >= 15 and len(recent) == k:
            med = statistics.median(recent)
            if med == 0 or (max(recent) - min(recent)) / med < tol:
                break
    return round(time.time() - t0)


__all__ = ["DUR_RE", "sample_mspt", "read_cpu_steal", "count_entities", "linfit", "warmup_stable"]
