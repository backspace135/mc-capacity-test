#!/usr/bin/env python3
"""Optional redstone, hopper, and chunk-exploration workloads."""
import argparse, csv, os, time
from dataclasses import dataclass
from datetime import datetime
from benchmark_metrics import read_cpu_steal, sample_mspt

@dataclass(frozen=True)
class Workload:
    kind: str
    count: int = 1
    origin: tuple[int, int, int] = (0, 100, 0)


def build_workload(kind, count=1, origin=(0, 100, 0)):
    if kind not in {"redstone", "hopper", "exploration"}:
        raise ValueError("kind must be redstone, hopper, or exploration")
    if count < 1: raise ValueError("count must be positive")
    return Workload(kind, count, tuple(origin))


def build_setup_commands(workload):
    x, y, z = workload.origin; n = workload.count
    if workload.kind == "redstone":
        return [f"fill {x} {y} {z} {x+n-1} {y} {z} minecraft:redstone_wire", f"setblock {x} {y+1} {z} minecraft:lever[powered=true]"]
    if workload.kind == "hopper":
        return [f"fill {x} {y} {z} {x+n-1} {y} {z} minecraft:hopper[facing=east]"]
    return []


def chunk_route(count, origin=(0, 100, 0)):
    x, y, z = origin
    return [(x + 16 * i, y, z + 16 * (i % 2)) for i in range(count)]


def run_workload(rc, workload, samples=12, interval=5, csv_path=None, allow_world_changes=False, cleanup=False, movement=None):
    if workload.kind != "exploration" and not allow_world_changes:
        raise PermissionError("world-changing workload requires allow_world_changes=True")
    commands = build_setup_commands(workload)
    if commands:
        for command in commands: rc.cmd(command)
    state = {"spark": True}; rows=[]; prev=read_cpu_steal()
    try:
        for i in range(samples):
            if workload.kind == "exploration" and movement:
                movement(chunk_route(workload.count)[i % workload.count])
            metric=sample_mspt(rc,state); cpu=read_cpu_steal(); steal=None
            if cpu and prev and cpu[0] > prev[0]: steal=round(100*(cpu[1]-prev[1])/(cpu[0]-prev[0]),2)
            prev=cpu; rows.append({"time":datetime.now().isoformat(timespec="seconds"),"workload":workload.kind,"count":workload.count,"sample":i,"tps":metric.get("tps"),"p50_ms":metric.get("p50"),"p95_ms":metric.get("p95"),"max_ms":metric.get("mx"),"steal_pct":steal})
            if i+1 < samples: time.sleep(interval)
    finally:
        if cleanup and commands: rc.cmd(f"fill {workload.origin[0]} {workload.origin[1]} {workload.origin[2]} {workload.origin[0]+workload.count-1} {workload.origin[1]+1} {workload.origin[2]} minecraft:air")
    if csv_path:
        with open(csv_path,"w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=rows[0].keys() if rows else ["time"]); w.writeheader(); w.writerows(rows)
    return rows


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("kind",choices=("redstone","hopper","exploration")); p.add_argument("--count",type=int,default=1); p.add_argument("--samples",type=int,default=12); p.add_argument("--interval",type=float,default=5); p.add_argument("--help-only",action="store_true"); p.parse_args(argv)
    return 0

if __name__ == "__main__": raise SystemExit(main())
