# Minecraft 服务器压测方法 v2（科学化方案）

## 一、旧方法的问题（基于 2026-08-31 两台 KVM 测试机的数据）

### 1. 测错了指标：空世界 sprint TPS 没有信息量
虚空世界每 tick 实际工作量只有 0.05–0.07ms（19,866 TPS ≈ 0.05ms/tick）。
tick sprint 测出的是「空 tick 循环的固定开销」，而不是服务器承载能力。
真实问题是：**在 20 TPS、每 tick 50ms 预算内，服务器能承载多少游戏负载**。

### 2. 倒数指标放大噪声，"8 倍退化"其实是绝对量 0.37ms 的扰动
19,866 TPS → 2,405 TPS 看起来掉了 8 倍，换算成每 tick 时间只是
0.05ms → 0.42ms，**绝对增量 0.37ms**——一次 GC 停顿、一次宿主机调度
抢占就是这个量级。当基础工作量趋近于零时，TPS = 1/MSPT 会把微小的
绝对扰动放大成倍数级差异。这就是结果不可复现的主因之一。

### 3. 不可比的对照：500k tick sprint（3,407 TPS）vs 100k（14,144 TPS）
sprint 时长本身是混杂变量（GC 累积、自动保存触发、宿主机争抢窗口变大）。
不同 tick 数的 sprint 结果不能互相比较。

### 4. 单次测量、无重复、无统计量
每个结论都来自 1 次运行。没有中位数、没有离散度，无法区分
「性能变化」和「测量噪声」。

### 5. 未受控/未观测的环境变量
- 两台机器都是 KVM 虚机（其一为 Xeon Platinum 8 核 16 线程），
  **CPU steal time 从未采集**——共享宿主机争抢完全可能解释全部退化现象；
- JVM 参数、GC 类型未固定未记录，无 GC 日志；
- JIT 未预热：首次 sprint 混入了 JIT 编译开销（或者相反，后续 sprint
  受 deopt/GC 影响），无法归因；
- `/tick query` 精度只有 0.1ms，多数读数是 0.0ms（量化误差 100%）。

---

## 二、新方法设计

### 核心思想
把「服务器最快能跑多少 TPS」改为「**固定 20 TPS 下，负载加到多大时
P95 MSPT 触及 50ms**」。产出是一条 **负载–MSPT 容量曲线** 和拐点值，
这是一个有单位、可复现、可跨机器对比的容量指标（如「12,000 个僵尸」
或「150 个假人」）。

### 指标
| 指标 | 来源 | 作用 |
|---|---|---|
| MSPT P50/P95/P99/max | spark（`/spark tps`、`/spark tickmonitor`） | 主指标，精度远高于 `/tick query` |
| GC 停顿次数/时长 | `-Xlog:gc*:file=gc.log` + `/spark gc` | 归因 JVM 内部因素 |
| CPU steal% | 宿主机 `vmstat 1` / `/proc/stat` | 归因宿主机争抢，**steal > 2% 的 run 作废重测** |
| 容器 CPU/内存 | `docker stats` | 确认瓶颈类型 |

Paper 系通常内置 spark；实测所用 Purpur 26.2 build 无 spark 命令，
脚本自动退回 `/tick query`（0.1ms 精度，在 50ms 拐点附近足够用）。

### 阶段 0：环境固化（一次性）
1. 固定并记录：Purpur build 号、JVM 版本、Aikar flags、`-Xms=-Xmx`（如 4G）、
   `server.properties`、view-distance/simulation-distance；
2. Docker 绑核：`--cpuset-cpus` 固定分配（如 0-7），排除调度漂移；
3. 关闭测量窗口内的自动保存（`save-off`，测完 `save-on`），或统一保留——
   两台机器必须一致；
4. 开 GC 日志。

### 阶段 1：负载梯度容量测试（主体）
用**可精确复制的合成负载**，按梯度递增：

| 负载类型 | 施加方式 | 模拟的真实负载 |
|---|---|---|
| 纯实体 tick | `/summon armor_stand`（无 AI） | 实体基础开销 |
| AI + 寻路 | `/summon zombie`（圈在围栏内） | 生物农场/刷怪 |
| 掉落物合并 | 批量 `/summon item` | 农场产出 |
| 容器/红石 | 漏斗阵列（结构方块粘贴） | 存储/机器 |
| 玩家负载 | Carpet fake player 或 mineflayer 机器人 | 真实玩家（含区块加载、网络包） |

每一级负载的流程：
```
施加负载 → 预热 3 min（JIT/区块稳定，数据丢弃）
        → 测量 5 min（每 10s 采一次 spark tps + docker stats + steal%）
        → 记录 MSPT 分布 → 加一档负载，重复
```
直到 P95 MSPT ≥ 50ms（开始掉 tick），该负载量即容量拐点。
**整条曲线重复 3 次（每次冷启动），取每个负载点的中位数。**

### 阶段 2：稳定性 soak 测试
取拐点约 50% 的负载（P95 ≈ 25ms），连续运行 1–2 小时，
每分钟采样。观察 MSPT 是否随时间漂移，并与 GC 日志、steal%
时间线对齐——漂移与 GC 相关 → JVM 问题；与 steal 相关 → 宿主机问题。

### 阶段 3：解释旧测试的"退化之谜"（专项实验）
ABAB 交替设计，每组 5 次 100k-tick sprint，全程记录 steal% 与 GC：
- A 组：每次 sprint 前重启容器；
- B 组：连续 sprint 不重启。

判定：
- B 退化、A 不退化，且 steal 平稳 → JVM 内部累积（GC/堆碎片/JIT deopt）；
- A、B 同样退化，steal 波动 → 宿主机争抢（KVM 邻居噪声）；
- 结果以 **ms/tick 绝对值** 报告，不用 TPS 倍数。

### 统计规范
- 每个数据点 ≥5 次重复（sprint 类）或 ≥3 条完整曲线（容量类）；
- 报告 **中位数 + IQR**，不报单次值；
- 变异系数 > 10% → 判为环境不稳，排查 steal/GC 后重测；
- 跨机器/跨配置对比（机器 A vs 机器 B、GC 算法 A vs B）必须
  交替执行（ABAB），不做先后串行，抵消时间漂移。

### sprint 微基准（保留，但降级为辅助）
如仍要测 tick 引擎裸吞吐：固定 100k ticks；冷启动后先跑 2 次丢弃
（JIT 预热），再连续 5 次记录；报告 ms/tick 中位数。仅用于同机
回归对比，不作容量结论。

---

## 三、采样脚本骨架

```bash
# 服务器侧，每 10s 一轮，写 CSV
while true; do
  ts=$(date +%s)
  tps=$(python3 rcon.py "spark tps")            # MSPT P50/P95
  cpu=$(docker stats --no-stream --format '{{.CPUPerc}},{{.MemUsage}}' purpur-test)
  steal=$(awk '/^cpu /{print $9}' /proc/stat)    # 差分得 steal
  echo "$ts,$tps,$cpu,$steal" >> bench.csv
  sleep 10
done
```
