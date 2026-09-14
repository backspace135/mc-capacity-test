"""Deterministic statistics and reporting for Phase 3 ABAB sprint runs."""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from typing import Any, Iterable, Mapping, Sequence


def median_iqr(values: Iterable[float]) -> dict[str, float | int]:
    """Return count, median, quartiles, and IQR using inclusive interpolation."""
    xs = sorted(float(x) for x in values)
    if not xs:
        return {"count": 0}
    q1, med, q3 = statistics.quantiles(xs, n=4, method="inclusive") if len(xs) > 1 else (xs[0], xs[0], xs[0])
    return {"count": len(xs), "median": med, "q1": q1, "q3": q3, "iqr": q3 - q1}


def coefficient_of_variation(values: Iterable[float]) -> float | None:
    xs = [float(x) for x in values]
    if not xs:
        return None
    mean = statistics.fmean(xs)
    if mean == 0:
        return 0.0 if all(x == 0 for x in xs) else math.inf
    return statistics.pstdev(xs) / abs(mean)


def summarize(values: Iterable[float]) -> dict[str, Any]:
    xs = [float(x) for x in values]
    out = median_iqr(xs)
    out["cv"] = coefficient_of_variation(xs)
    out["unstable"] = out["cv"] is not None and out["cv"] > 0.10
    return out


def group_statistics(records: Iterable[Mapping[str, Any]], value_key: str = "ms_per_tick") -> dict[str, dict[str, Any]]:
    groups: dict[str, list[float]] = {"A": [], "B": []}
    for row in records:
        group = str(row.get("group", ""))
        value = row.get(value_key)
        if group in groups and value not in (None, "") and not row.get("warmup", False) and not row.get("error"):
            groups[group].append(float(value))
    return {group: summarize(values) for group, values in groups.items()}


def classify_abab(records: Iterable[Mapping[str, Any]], threshold: float = 0.10) -> str:
    """Classify degradation pattern per methodology; returns explicit insufficiency."""
    records = list(records)
    stats = group_statistics(records)
    a, b = stats["A"], stats["B"]
    if a.get("count", 0) < 2 or b.get("count", 0) < 2:
        return "insufficient-data"
    def drift(group: str) -> float:
        vals = [float(r["ms_per_tick"]) for r in records if str(r.get("group")) == group and r.get("ms_per_tick") not in (None, "") and not r.get("warmup") and not r.get("error")]
        return (vals[-1] - vals[0]) / abs(vals[0]) if vals and vals[0] else 0.0
    ad, bd = drift("A"), drift("B")
    if bd > threshold and abs(ad) <= threshold:
        return "jvm-internal-accumulation"
    if ad > threshold and bd > threshold:
        return "host-contention-or-shared-drift"
    av, bv = float(a["median"]), float(b["median"])
    if abs(av - bv) <= threshold * max(abs(av), abs(bv), 1e-12):
        return "no-material-difference"
    return "mixed-or-inconclusive"


def _load(path: str) -> list[dict[str, Any]]:
    with open(path, newline="", encoding="utf-8") as fh:
        if path.lower().endswith(".json"):
            data = json.load(fh)
            return data if isinstance(data, list) else data.get("results", [])
        return list(csv.DictReader(fh))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report Phase 3 ABAB sprint statistics")
    parser.add_argument("input", help="CSV or JSON results file")
    parser.add_argument("--value", default="ms_per_tick", help="numeric field (default: ms_per_tick)")
    args = parser.parse_args(argv)
    records = _load(args.input)
    report = {"groups": group_statistics(records, args.value), "classification": classify_abab(records)}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
