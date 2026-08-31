#!/usr/bin/env python3
"""Minecraft 服务器容量压测(跑在服务器宿主机上,通过 RCON 控制)。

方法:固定 20 TPS,递增合成负载(盔甲架/僵尸/掉落物),
每级「预热(稳定即提前结束) + 测量(采样 spark/tick query 的 MSPT P95)」,
直到 P95 MSPT 触及阈值(默认 50ms)。
步长自适应:P95-实体数关系实测高度线性(R²>0.99),
每级结束后用全部有效级做最小二乘拟合,直接把下一级打到预测拐点附近,
通常 4 级即可完成 bracketing;拐点由回归求出(而非仅相邻两点插值)。

产出: <outdir>/samples-<ts>.csv(每次采样)、<outdir>/summary-<ts>.csv(每级汇总)。
"""
import argparse
import csv
import os
import random
import re
import socket
import statistics
import struct
import sys
import time
from datetime import datetime


# ---------- RCON ----------

class Rcon:
    def __init__(self, host, port, password, timeout=15):
        self.addr = (host, port)
        self.password = password
        self.timeout = timeout
        self.sock = None
        self.req = 0

    def connect(self):
        self.sock = socket.create_connection(self.addr, self.timeout)
        self.sock.settimeout(self.timeout)
        rid, _ = self._send(3, self.password)
        if rid == -1:
            raise RuntimeError("RCON 认证失败(密码错误?)")

    def _recvn(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("RCON 连接被关闭")
            buf += chunk
        return buf

    def _send(self, ptype, payload):
        self.req += 1
        rid = self.req
        body = struct.pack("<ii", rid, ptype) + payload.encode("utf-8") + b"\x00\x00"
        self.sock.sendall(struct.pack("<i", len(body)) + body)
        rlen = struct.unpack("<i", self._recvn(4))[0]
        resp = self._recvn(rlen)
        prid, _ = struct.unpack("<ii", resp[:8])
        return prid, resp[8:-2].decode("utf-8", "replace")

    def cmd(self, c, retries=3):
        last = None
        for _ in range(retries):
            try:
                if self.sock is None:
                    self.connect()
                return self._send(2, c)[1]
            except (OSError, ConnectionError, RuntimeError) as e:
                last = e
                self.sock = None
                time.sleep(2)
        raise RuntimeError(f"RCON 命令失败: {c!r}: {last}")


def strip_colors(s):
    return re.sub("§.", "", s)


# ---------- 指标采样 ----------

DUR_RE = re.compile(r"(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)")


def sample_mspt(rc, state):
    """返回 dict(tps, p50, p95, mx, source)。优先 spark,退回 /tick query。"""
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
    # tick query 不报真实 TPS,由 P50 推算:每 tick 预算 50ms,超出后 TPS ≈ 1000/MSPT
    tps = None
    if p50v is not None:
        tps = 20.0 if p50v <= 50 else round(1000.0 / p50v, 1)
    return dict(
        tps=tps,
        p50=p50v,
        p95=float(p95.group(1)),
        mx=None,
        source="tick-query",
    )


def read_cpu_steal():
    """返回 (total_jiffies, steal_jiffies);非 Linux 返回 None。"""
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
    """最小二乘拟合 p95 = a + b*count。返回 (a, b, r2),点不足或退化返回 None。"""
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
    """预热:连续 k 个 P95 样本的极差/中位数 < tol 即提前结束(至少 15s)。
    返回实际预热秒数。"""
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


# ---------- 负载预设 ----------

PEN = 24  # 围栏半径(方块),平台 y=99,实体活动层 y=100+

PRESETS = {
    "armor_stand": dict(
        etype="minecraft:armor_stand",
        nbt="{NoGravity:1b,Invulnerable:1b}",
        desc="纯实体 tick(无 AI)",
    ),
    "zombie": dict(
        etype="minecraft:zombie",
        # Invulnerable:高密度下拥挤(cramming)/推入墙窒息会成批死亡,使负载失真;免伤不改变 AI/寻路开销
        nbt="{PersistenceRequired:1b,Invulnerable:1b}",
        desc="AI + 寻路 + 碰撞",
    ),
    "item": dict(
        etype="minecraft:item",
        nbt='{Item:{id:"minecraft:stone",count:1},Age:-32768s,PickupDelay:32767s}',
        desc="掉落物(不合并不消失)",
    ),
}

SETUP_CMDS = [
    "gamerule doMobSpawning false",
    "gamerule doDaylightCycle false",
    "gamerule doWeatherCycle false",
    "gamerule doFireTick false",
    "gamerule mobGriefing false",
    "gamerule doMobLoot false",
    "gamerule randomTickSpeed 0",
    "gamerule maxEntityCramming 0",
    "time set midnight",
    "weather clear",
    f"forceload add {-PEN - 24} {-PEN - 24} {PEN + 23} {PEN + 23}",
    f"fill {-PEN} 99 {-PEN} {PEN} 99 {PEN} minecraft:barrier",
    f"fill {-PEN - 1} 100 {-PEN - 1} {PEN + 1} 103 {-PEN - 1} minecraft:barrier",
    f"fill {-PEN - 1} 100 {PEN + 1} {PEN + 1} 103 {PEN + 1} minecraft:barrier",
    f"fill {-PEN - 1} 100 {-PEN - 1} {-PEN - 1} 103 {PEN + 1} minecraft:barrier",
    f"fill {PEN + 1} 100 {-PEN - 1} {PEN + 1} 103 {PEN + 1} minecraft:barrier",
    # 顶棚:防止高密度挤压把实体弹出围墙掉进虚空
    f"fill {-PEN - 1} 104 {-PEN - 1} {PEN + 1} 104 {PEN + 1} minecraft:barrier",
    "save-off",
]


def spawn(rc, preset, n):
    p = PRESETS[preset]
    for i in range(n):
        x = round(random.uniform(-PEN + 1, PEN - 1), 1)
        z = round(random.uniform(-PEN + 1, PEN - 1), 1)
        out = rc.cmd(f"summon {p['etype']} {x} 101 {z} {p['nbt']}")
        if i == 0 and ("Unable" in out or "Invalid" in out or "Unknown" in out or "Expected" in out):
            raise RuntimeError(f"summon 失败: {strip_colors(out)[:200]}")
        if (i + 1) % 200 == 0:
            print(f"    已生成 {i + 1}/{n}")


# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--preset", choices=PRESETS, default="zombie")
    ap.add_argument("--step", type=int, default=1000,
                    help="初始步长/最小前进量;之后由回归预测自适应跳级(默认 1000)")
    ap.add_argument("--max-levels", type=int, default=40)
    ap.add_argument("--warmup", type=int, default=60,
                    help="每级最大预热秒数,P95 稳定后提前结束(默认 60)")
    ap.add_argument("--measure", type=int, default=60, help="每级测量秒数(默认 60)")
    ap.add_argument("--interval", type=int, default=5, help="采样间隔秒(默认 5)")
    ap.add_argument("--threshold", type=float, default=50.0, help="P95 MSPT 阈值 ms(默认 50)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=25575)
    ap.add_argument("--password", default=None, help="默认读 $RCON_PASSWORD 或 server.properties")
    ap.add_argument("--server-dir", default="/opt/purpur-test")
    ap.add_argument("--outdir", default=None, help="默认 <server-dir>/results")
    ap.add_argument("--ping", action="store_true", help="只测试 RCON 连通性后退出")
    ap.add_argument("--stop", action="store_true", help="向服务器发送 stop(优雅关服)后退出")
    ap.add_argument("--keep-entities", action="store_true", help="结束后不清理实体")
    args = ap.parse_args()

    pw = args.password or os.environ.get("RCON_PASSWORD")
    if not pw:
        try:
            with open(os.path.join(args.server_dir, "server.properties")) as f:
                for line in f:
                    if line.startswith("rcon.password="):
                        pw = line.split("=", 1)[1].strip()
        except OSError:
            pass
    if not pw:
        sys.exit("找不到 RCON 密码:--password / $RCON_PASSWORD / server.properties")

    rc = Rcon(args.host, args.port, pw)
    rc.connect()
    if args.ping:
        print(strip_colors(rc.cmd("list")).strip())
        return
    if args.stop:
        try:
            rc.cmd("stop", retries=1)
        except Exception:
            pass  # 服务器关闭时连接可能先断,不算失败
        print("已发送 stop")
        return

    outdir = args.outdir or os.path.join(args.server_dir, "results")
    os.makedirs(outdir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    samples_path = os.path.join(outdir, f"samples-{ts}.csv")
    summary_path = os.path.join(outdir, f"summary-{ts}.csv")
    sf = open(samples_path, "w", newline="")
    sw = csv.writer(sf)
    sw.writerow(["time", "level", "count", "tps", "p50_ms", "p95_ms", "max_ms", "steal_pct", "source"])

    p = PRESETS[args.preset]
    print(f"== 容量压测: {args.preset}({p['desc']}), 初始步长 {args.step}(回归自适应跳级), "
          f"预热 ≤{args.warmup}s + 测量 {args.measure}s/级, 阈值 P95 ≥ {args.threshold}ms ==")

    print("-- 场地初始化 --")
    for c in SETUP_CMDS:
        rc.cmd(c)
    rc.cmd(f"kill @e[type={p['etype']}]")
    time.sleep(2)

    state = {"spark": True}
    baseline = sample_mspt(rc, state)
    print(f"-- 空载基线: P95={baseline['p95']}ms (来源 {baseline['source']}) --")

    results = []
    valid_pts = []  # (count, p95) 仅取通过存活校验的级,供回归用
    prev_cpu = read_cpu_steal()
    stopped = "达到最大级数"
    target = args.step
    try:
        for level in range(1, args.max_levels + 1):
            print(f"\n[级 {level}] 目标 {target} 个 {args.preset}")
            cur = count_entities(rc, p["etype"]) or 0
            spawn(rc, args.preset, max(0, target - cur))  # 按现存数补齐,覆盖上一级的意外损耗
            actual = count_entities(rc, p["etype"])
            if actual is not None and abs(actual - target) > max(10, target * 0.02):
                print(f"  [!] 实际实体数 {actual} 与目标 {target} 偏差过大(死亡/掉出围栏?)")
            print(f"  预热(≤{args.warmup}s,P95 稳定即止)...")
            used = warmup_stable(rc, state, args.warmup)
            print(f"  预热 {used}s,测量 {args.measure}s ...")
            p95s, p50s, tpss, steals = [], [], [], []
            t_end = time.time() + args.measure
            while time.time() < t_end:
                s = sample_mspt(rc, state)
                cpu = read_cpu_steal()
                steal_pct = ""
                if cpu and prev_cpu and cpu[0] > prev_cpu[0]:
                    steal_pct = round(100.0 * (cpu[1] - prev_cpu[1]) / (cpu[0] - prev_cpu[0]), 2)
                    steals.append(steal_pct)
                prev_cpu = cpu
                sw.writerow([datetime.now().isoformat(timespec="seconds"), level,
                             actual or target, s["tps"], s["p50"], s["p95"], s["mx"],
                             steal_pct, s["source"]])
                sf.flush()
                p95s.append(s["p95"])
                if s["p50"] is not None:
                    p50s.append(s["p50"])
                if s["tps"] is not None:
                    tpss.append(s["tps"])
                time.sleep(args.interval)

            end_cnt = count_entities(rc, p["etype"])
            valid = not (end_cnt is not None and end_cnt < target * 0.98)
            if not valid:
                print(f"  [!] 测量结束时仅存活 {end_cnt}/{target},本级负载失真,数据不可信")
            row = dict(
                level=level,
                count=actual or target,
                count_end=end_cnt,
                p50=round(statistics.median(p50s), 2) if p50s else None,
                p95=round(statistics.median(p95s), 2),
                p95_max=round(max(p95s), 2),
                tps=round(statistics.median(tpss), 1) if tpss else None,
                steal=round(statistics.median(steals), 2) if steals else None,
                valid=valid,
            )
            results.append(row)
            print(f"  => count={row['count']} P50={row['p50']}ms P95={row['p95']}ms "
                  f"TPS={row['tps']} steal={row['steal']}%")
            if row["steal"] is not None and row["steal"] > 2:
                print("  [!] steal > 2%,宿主机被邻居抢占,本级数据可信度低")
            if valid:
                valid_pts.append((row["count"], row["p95"]))
            if row["p95"] >= args.threshold:
                stopped = "P95 触及阈值"
                break
            if row["tps"] is not None and row["tps"] < 10:
                stopped = "TPS < 10,服务器已过载"
                break

            # 自适应选择下一级目标:回归预测拐点,先打到其 90% 验证线性,
            # 逼近后过冲 5% 完成 bracketing;单跳限幅防早期噪声导致巨跳
            fit = linfit(valid_pts)
            nxt = target + args.step
            if fit and fit[1] > 0:
                a, b, r2 = fit
                pred = (args.threshold - a) / b
                goal = pred * (0.9 if row["p95"] < 0.8 * args.threshold else 1.05)
                nxt = int(min(max(goal, target + max(100, args.step // 5)), target + 4 * args.step))
                print(f"  拟合 P95≈{a:.1f}+{b * 1000:.2f}·(n/1000)ms R²={r2:.4f},"
                      f"预测拐点≈{pred:.0f} → 下一级 {nxt}")
            target = nxt
    finally:
        print("\n-- 清理 --")
        try:
            if not args.keep_entities:
                rc.cmd(f"kill @e[type={p['etype']}]")
            rc.cmd("forceload remove all")
            rc.cmd("save-on")
        except Exception as e:
            print(f"  [!] 清理失败: {e}")
        sf.close()

    with open(summary_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["level", "count", "count_end", "p50_ms", "p95_ms", "p95_max_ms", "tps", "steal_pct", "valid"])
        for r in results:
            w.writerow([r["level"], r["count"], r["count_end"], r["p50"], r["p95"],
                        r["p95_max"], r["tps"], r["steal"], int(r["valid"])])

    print(f"\n== 结束({stopped}),共 {len(results)} 级 ==")
    print(f"{'count':>8} {'P50':>8} {'P95':>8} {'TPS':>6}")
    for r in results:
        flag = "" if r["valid"] else "  [失真,未参与拟合]"
        print(f"{r['count']:>8} {r['p50']!s:>8} {r['p95']!s:>8} {r['tps']!s:>6}{flag}")

    # 拐点:优先全数据回归(抗单级噪声),两点插值仅作交叉校验
    over = [r for r in results if r["valid"] and r["p95"] >= args.threshold]
    under = [r for r in results if r["valid"] and r["p95"] < args.threshold]
    cap_itp = None
    if over and under:
        lo, hi = under[-1], over[0]
        cap_itp = lo["count"] + (args.threshold - lo["p95"]) * (hi["count"] - lo["count"]) / (hi["p95"] - lo["p95"])
    fit = linfit(valid_pts) if len(valid_pts) >= 3 else None
    if fit and fit[1] > 0 and fit[2] >= 0.98:
        a, b, r2 = fit
        cap = (args.threshold - a) / b
        # 不确定度:各级 P95 残差最大值换算成实体数
        err = max(abs(y - (a + b * x)) for x, y in valid_pts) / b
        last = max(x for x, _ in valid_pts)
        if cap <= last * 1.25:
            how = "回归" if over else "回归外推(阈值未实测触及)"
            print(f"\n容量拐点(P95={args.threshold}ms,{how},"
                  f"{len(valid_pts)} 级 R²={r2:.4f}): 约 {cap:.0f} ± {err:.0f} 个 {args.preset}")
            if cap_itp is not None:
                print(f"  交叉校验(相邻两点插值): {cap_itp:.0f}"
                      f"(偏差 {abs(cap - cap_itp) / cap * 100:.1f}%)")
        else:
            print(f"\n未触及阈值且外推超 25%(预测 {cap:.0f}),"
                  f"仅能断言容量 > {last} 个 {args.preset},请提高 --max-levels 续测")
    elif cap_itp is not None:
        why = f"R²={fit[2]:.3f} 线性度不足" if fit else "有效级数 < 3"
        print(f"\n容量拐点(P95={args.threshold}ms 两点插值,{why},可信度低): "
              f"约 {cap_itp:.0f} 个 {args.preset}")
    elif under:
        print(f"\n未触及阈值,容量 > {under[-1]['count']} 个 {args.preset}(加大 --max-levels)")
    else:
        print("\n第一级就超阈值,减小 --step 重测")
    print(f"\n明细: {samples_path}\n汇总: {summary_path}")
    print("提示:同一配置重复 3 次取中位数;对比配置用 ABAB 交替顺序。")


if __name__ == "__main__":
    main()
