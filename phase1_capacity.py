#!/usr/bin/env python3
"""Standalone Phase 1 Minecraft capacity-gradient runner."""
import argparse
import csv
import os
import random
import re
import statistics
import sys
import time
from datetime import datetime

from rcon_client import Rcon, strip_colors

PEN = 24
PRESETS = {
    "armor_stand": dict(etype="minecraft:armor_stand", nbt="{NoGravity:1b,Invulnerable:1b}", desc="纯实体 tick(无 AI)"),
    "zombie": dict(etype="minecraft:zombie", nbt="{PersistenceRequired:1b,Invulnerable:1b}", desc="AI + 寻路 + 碰撞"),
    "item": dict(etype="minecraft:item", nbt='{Item:{id:"minecraft:stone",count:1},Age:-32768s,PickupDelay:32767s}', desc="掉落物(不合并不消失)"),
}
SETUP_CMDS = [
    "gamerule doMobSpawning false", "gamerule doDaylightCycle false", "gamerule doWeatherCycle false",
    "gamerule doFireTick false", "gamerule mobGriefing false", "gamerule doMobLoot false",
    "gamerule randomTickSpeed 0", "gamerule maxEntityCramming 0", "time set midnight", "weather clear",
    f"forceload add {-PEN - 24} {-PEN - 24} {PEN + 23} {PEN + 23}",
    f"fill {-PEN} 99 {-PEN} {PEN} 99 {PEN} minecraft:barrier",
    f"fill {-PEN - 1} 100 {-PEN - 1} {PEN + 1} 103 {-PEN - 1} minecraft:barrier",
    f"fill {-PEN - 1} 100 {PEN + 1} {PEN + 1} 103 {PEN + 1} minecraft:barrier",
    f"fill {-PEN - 1} 100 {-PEN - 1} {-PEN - 1} 103 {PEN + 1} minecraft:barrier",
    f"fill {PEN + 1} 100 {-PEN - 1} {PEN + 1} 103 {PEN + 1} minecraft:barrier",
    f"fill {-PEN - 1} 104 {-PEN - 1} {PEN + 1} 104 {PEN + 1} minecraft:barrier", "save-off",
]
DUR_RE = re.compile(r"(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)")

def sample_mspt(rc, state):
    if state.get("spark", True):
        out = strip_colors(rc.cmd("spark tps"))
        if "Tick durations" in out:
            tps = None
            m = re.search(r"TPS from[^:]*:\s*([^\n]*)", out)
            if m:
                nums = re.findall(r"[\d.]+", m.group(1).replace("*", ""))
                if nums: tps = float(nums[0])
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
    if not p95: raise RuntimeError(f"无法解析 tick query 输出: {out[:200]!r}")
    p50v = float(p50.group(1)) if p50 else (float(avg.group(1)) if avg else None)
    return dict(tps=(20.0 if p50v is not None and p50v <= 50 else round(1000.0 / p50v, 1) if p50v else None), p50=p50v, p95=float(p95.group(1)), mx=None, source="tick-query")

def read_cpu_steal():
    try:
        with open("/proc/stat") as f: vals = list(map(int, f.readline().split()[1:]))
        return sum(vals), vals[7] if len(vals) > 7 else 0
    except OSError: return None

def count_entities(rc, etype):
    m = re.search(r"count:\s*(\d+)", strip_colors(rc.cmd(f"execute if entity @e[type={etype}]")), re.I)
    return int(m.group(1)) if m else None

def linfit(pts):
    if len(pts) < 2: return None
    xs, ys = zip(*pts); mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x-mx)**2 for x in xs)
    if not sxx: return None
    b = sum((x-mx)*(y-my) for x,y in pts)/sxx; a = my-b*mx
    ssr = sum((y-(a+b*x))**2 for x,y in pts); sst = sum((y-my)**2 for y in ys)
    return a,b,(1-ssr/sst if sst else 1.0)

def warmup_stable(rc, state, max_s, interval=5, k=3, tol=.10):
    start, recent = time.time(), []
    while time.time()-start < max_s:
        time.sleep(interval); recent = (recent+[sample_mspt(rc,state)["p95"]])[-k:]
        if time.time()-start >= 15 and len(recent)==k:
            med=statistics.median(recent)
            if med == 0 or (max(recent)-min(recent))/med < tol: break
    return round(time.time()-start)

def spawn(rc, preset, n):
    p=PRESETS[preset]
    for i in range(n):
        x=round(random.uniform(-PEN+1,PEN-1),1); z=round(random.uniform(-PEN+1,PEN-1),1)
        out=rc.cmd(f"summon {p['etype']} {x} 101 {z} {p['nbt']}")
        if i==0 and any(x in out for x in ("Unable","Invalid","Unknown","Expected")): raise RuntimeError(f"summon 失败: {strip_colors(out)[:200]}")
        if (i+1)%200==0: print(f"    已生成 {i+1}/{n}")

def run_capacity(args, rc):
    """Run Phase 1 against an already-connected RCON client; return output paths/results."""
    outdir=args.outdir or os.path.join(args.server_dir,"results"); os.makedirs(outdir,exist_ok=True)
    ts=datetime.now().strftime("%Y%m%d-%H%M%S"); samples_path=os.path.join(outdir,f"samples-{ts}.csv"); summary_path=os.path.join(outdir,f"summary-{ts}.csv")
    p=PRESETS[args.preset]; results=[]; valid_pts=[]; state={"spark":True}; prev_cpu=read_cpu_steal(); target=args.step; stopped="达到最大级数"
    sf=open(samples_path,"w",newline=""); sw=csv.writer(sf); sw.writerow(["time","level","count","tps","p50_ms","p95_ms","max_ms","steal_pct","source"])
    try:
        for c in SETUP_CMDS: rc.cmd(c)
        rc.cmd(f"kill @e[type={p['etype']}]"); time.sleep(2); baseline=sample_mspt(rc,state)
        for level in range(1,args.max_levels+1):
            cur=count_entities(rc,p["etype"]) or 0; spawn(rc,args.preset,max(0,target-cur)); actual=count_entities(rc,p["etype"])
            warmup_stable(rc,state,args.warmup, args.interval); p95s=[]; p50s=[]; tpss=[]; steals=[]; end=time.time()+args.measure
            while time.time()<end:
                s=sample_mspt(rc,state); cpu=read_cpu_steal(); steal=""
                if cpu and prev_cpu and cpu[0]>prev_cpu[0]: steal=round(100*(cpu[1]-prev_cpu[1])/(cpu[0]-prev_cpu[0]),2); steals.append(steal)
                prev_cpu=cpu; sw.writerow([datetime.now().isoformat(timespec="seconds"),level,actual or target,s["tps"],s["p50"],s["p95"],s["mx"],steal,s["source"]]); sf.flush(); p95s.append(s["p95"])
                if s["p50"] is not None:p50s.append(s["p50"])
                if s["tps"] is not None:tpss.append(s["tps"])
                time.sleep(args.interval)
            end_cnt=count_entities(rc,p["etype"]); valid=not(end_cnt is not None and end_cnt<target*.98)
            row=dict(level=level,count=actual or target,count_end=end_cnt,p50=round(statistics.median(p50s),2) if p50s else None,p95=round(statistics.median(p95s),2),p95_max=round(max(p95s),2),tps=round(statistics.median(tpss),1) if tpss else None,steal=round(statistics.median(steals),2) if steals else None,valid=valid); results.append(row)
            if valid: valid_pts.append((row["count"],row["p95"]))
            if row["p95"]>=args.threshold: stopped="P95 触及阈值"; break
            if row["tps"] is not None and row["tps"]<10: stopped="TPS < 10,服务器已过载"; break
            fit=linfit(valid_pts); nxt=target+args.step
            if fit and fit[1]>0:
                a,b,_=fit; pred=(args.threshold-a)/b; goal=pred*(.9 if row["p95"]<.8*args.threshold else 1.05); nxt=int(min(max(goal,target+max(100,args.step//5)),target+4*args.step))
            target=nxt
    finally:
        try:
            if not getattr(args,"keep_entities",False): rc.cmd(f"kill @e[type={p['etype']}]")
            rc.cmd("forceload remove all"); rc.cmd("save-on")
        finally: sf.close()
    with open(summary_path,"w",newline="") as f:
        w=csv.writer(f); w.writerow(["level","count","count_end","p50_ms","p95_ms","p95_max_ms","tps","steal_pct","valid"])
        for r in results:w.writerow([r[k] for k in ("level","count","count_end","p50","p95","p95_max","tps","steal")]+[int(r["valid"])])
    return dict(samples_path=samples_path,summary_path=summary_path,results=results,baseline=baseline,stopped=stopped)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--preset",choices=PRESETS,default="zombie"); ap.add_argument("--step",type=int,default=1000); ap.add_argument("--max-levels",type=int,default=40); ap.add_argument("--warmup",type=int,default=60); ap.add_argument("--measure",type=int,default=60); ap.add_argument("--interval",type=int,default=5); ap.add_argument("--threshold",type=float,default=50.0); ap.add_argument("--host",default="127.0.0.1"); ap.add_argument("--port",type=int,default=25575); ap.add_argument("--password",default=None); ap.add_argument("--server-dir",default="/opt/purpur-test"); ap.add_argument("--outdir",default=None); ap.add_argument("--keep-entities",action="store_true")
    a=ap.parse_args(); pw=a.password or os.environ.get("RCON_PASSWORD")
    if not pw: raise SystemExit("找不到 RCON 密码")
    rc=Rcon(a.host,a.port,pw); rc.connect(); run_capacity(a,rc)
if __name__=="__main__": main()
