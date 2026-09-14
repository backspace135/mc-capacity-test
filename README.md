# Minecraft 服务器容量压测工具包

本工具包用于测量 Minecraft（Purpur）服务器的实际承载能力。测试方法不是追求"最高能跑多少 TPS"（该指标在空载下没有信息量），而是**固定 20 TPS，向服务器逐级施加可复现的合成负载（僵尸、盔甲架或掉落物），记录 P95 MSPT 首次达到 50ms（开始掉 tick）时的负载量**。输出为一条负载–MSPT 曲线和一个有单位、可复现、跨机器可比的容量值，例如"可承载约 5200 个僵尸"。

方法学原理与常见错误测法的分析见 `benchmark-methodology.md`。

## 文件清单

| 文件 | 作用 |
|---|---|
| `Linux/run_capacity_test.sh` | **Linux 入口**：自动部署测试服，执行测试，结果写入 `./results/` |
| `macOS/一键压测.command` | **macOS 入口**：双击运行完整测试，启动时选择运行方式 |
| `macOS/停服.command` | 双击停止测试服（容器或直跑进程） |
| `macOS/run_capacity_test.sh` | macOS 实际执行逻辑 |
| `Windows/一键压测.bat` | **Windows 入口**：双击运行完整测试 |
| `Windows/停服.bat` | 双击停止测试服 |
| `Windows/run_capacity_test.ps1` | Windows 实际执行逻辑 |
| `python/` | 全部 Python 测试脚本与共享模块 |
| `python/capacity_test.py` | 平台入口脚本调用的默认命令行程序；包含 `--ping` / `--stop` 服务器管理开关 |
| `python/capacity_gradient.py` | 负载梯度容量测试：逐级加载、回归求拐点 |
| `python/soak_test.py` | 固定负载长时稳定性测试（soak） |
| `python/sprint_experiment.py` | ABAB 交替 tick sprint 实验 |
| `python/sprint_stats.py` | sprint 结果统计：中位数、IQR、变异系数与 ABAB 判定 |
| `python/batch_spawn.py` | 批量实体生成策略评估 |
| `python/player_capacity.py` | 可选：真实玩家客户端容量测试 |
| `python/world_loads.py` | 可选：红石、漏斗与区块探索负载 |
| `python/rcon_client.py` | 共享 RCON 客户端 |
| `python/benchmark_metrics.py` | 共享 MSPT、CPU steal、回归与预热采样函数 |
| `benchmark-methodology.md` | 压测方法设计文档 |
| `README.md` | 本文档 |
| `LICENSE` | MIT 许可证 |

## 前置条件

本工具包需在**被测服务器上**直接运行，要求如下：

- **Linux**：`python3`、`curl`；默认使用 `docker`（当前用户需可直接调用，即 root 或 docker 组成员）。未安装 Docker 时脚本会自动切换为本机 Java 直跑（需 Java 25+，推荐 [Temurin](https://adoptium.net/)），也可用 `--no-docker` 明确指定；
- **macOS**：`python3`（执行 `xcode-select --install` 即可获得）、`curl`（系统自带）；默认使用 Docker Desktop / OrbStack / colima。未安装 Docker 时脚本会自动切换为本机 Java 直跑（需 Java 25+，[Temurin](https://adoptium.net/) 或 `brew install --cask temurin@25`），也可用 `--no-docker` 明确指定；
- **Windows**：Python 3；默认使用 Docker Desktop（Linux 容器模式）。未安装 Docker 时脚本会自动切换为本机 Java 直跑（需 Java 25+，[Temurin](https://adoptium.net/)），也可用 `-NoDocker` 明确指定；
- 三个平台均要求：空闲内存不少于 6GB，端口 25565 与 25575 未被占用。

其余步骤全部自动完成：Purpur jar 下载、虚空世界配置、RCON 配置（密码随机生成；Docker 模式仅绑定 127.0.0.1）、容器或进程启动。**无需预先安装任何 Minecraft 服务器。**

## 快速开始

### Windows

解压后双击 `Windows\一键压测.bat`，测试结束后双击 `Windows\停服.bat`。启动时选择运行方式（1 自动 / 2 强制 Docker / 3 不使用 Docker，直接回车即自动），随后窗口依次显示 `[1/4] 准备`（含 jar 下载进度）、`[2/4] 启动服务器`、`[3/4] 等待 RCON`、`[4/4] 逐级加载测试`，最后输出结论。如需调整参数，在工具包根目录使用 PowerShell：

```powershell
# 冒烟测试(约 2 分钟,确认全链路可用)
.\Windows\run_capacity_test.ps1 --step 300 --max-levels 2 --warmup 10 --measure 30 --interval 5

# 明确不使用 Docker(需本机 Java 25+,绑核转为 CPU 亲和性;
# 不加此参数时,未安装 Docker 也会自动切换为直跑模式)
.\Windows\run_capacity_test.ps1 -NoDocker

# 后台运行 / 停止服务器
.\Windows\run_capacity_test.ps1 -Background     # 查看进度: Get-Content -Wait results\run-*.log
.\Windows\run_capacity_test.ps1 -StopServer
```

如提示脚本被禁止执行，先运行 `Set-ExecutionPolicy -Scope Process Bypass`；bat 文件已内置 Bypass，不受此限制。

### Linux

```bash
# 从 GitHub Releases 下载 zip(每个 v* 标签由 CI 自动打包发布),上传到服务器并解压
unzip mc-capacity-test-*.zip && cd mc-capacity-test
chmod +x Linux/run_capacity_test.sh

# 1) 冒烟测试(约 2 分钟,确认全链路可用)
./Linux/run_capacity_test.sh --step 300 --max-levels 2 --warmup 10 --measure 30 --interval 5

# 2) 正式测试(默认参数:僵尸,初始步长 1000,按回归预测自适应跳级,
#    每级预热不超过 60s 加测量 60s,直到 P95 >= 50ms;通常 4 级、约 10 分钟收敛)
NOHUP=1 ./Linux/run_capacity_test.sh
# 按屏幕提示 tail -f results/run-*.log 查看进度

# 3) 长窗口精测(每级预热不超过 2 分钟加测量 5 分钟,用于复现旧版慢速方法)
NOHUP=1 ./Linux/run_capacity_test.sh --warmup 120 --measure 300 --interval 10

# 明确不使用 Docker(需本机 Java 25+,绑核通过 taskset 生效;
# 不加此参数时,未安装 Docker 也会自动切换为直跑模式)
./Linux/run_capacity_test.sh --no-docker

# 停止服务器(容器或直跑进程均可)
./Linux/run_capacity_test.sh --stop-server
```

### macOS

解压后双击 `macOS/一键压测.command`，测试结束后双击 `macOS/停服.command`。与 Windows 相同，启动时选择运行方式（1 自动 / 2 强制 Docker / 3 不使用 Docker，直接回车即自动）。如需调整参数，在工具包根目录使用终端：

```bash
chmod +x macOS/run_capacity_test.sh macOS/*.command   # zip 解压后若丢失执行权限

# 冒烟测试(约 2 分钟,确认全链路可用)
./macOS/run_capacity_test.sh --step 300 --max-levels 2 --warmup 10 --measure 30 --interval 5

# 正式测试(后台运行,避免终端断开中断测试)
NOHUP=1 ./macOS/run_capacity_test.sh          # 查看进度: tail -f results/run-*.log

# 明确不使用 Docker(需本机 Java 25+)
./macOS/run_capacity_test.sh --no-docker

# 停止服务器(容器或直跑进程均可)
./macOS/run_capacity_test.sh --stop-server
```

macOS 版本与其他两个平台的差别仅有两处：**直跑模式不绑核**（macOS 没有 `taskset`，跨机对比时需注意；Docker 模式绑核正常生效）；**数据目录固定为 `~/purpur-test`**（Docker Desktop 默认仅共享 `/Users` 等路径，挂载 `/opt` 会导致启动失败）。脚本会自动选择本机主版本号最高的 JDK，Homebrew 安装的 `openjdk@25` / `openjdk@26` 默认不链接到 PATH，无需手动设置 `JAVA_HOME`。

首次运行会下载 jar（约 64MB）和 Docker 镜像（直跑模式无镜像），耗时一到两分钟；之后复用缓存，数秒内就绪。

## 参数

环境变量（Linux/macOS，置于命令前）与开关（Linux、macOS 以 `--` 开头，Windows 以 `-` 开头）：

| Linux / macOS | Windows 参数 | 默认 | 说明 |
|---|---|---|---|
| `DIR` 变量 | `-Dir` | Linux `/opt/purpur-test`（直跑 `~/purpur-test`）/ macOS `~/purpur-test` / Windows `%USERPROFILE%\purpur-test` | 测试服数据目录 |
| `CPUSET` 变量 | `-CpuSet` | `0-7` | 绑定的 CPU 核，保证测试间可比，请保持一致。直跑模式：Linux 通过 `taskset`、Windows 转为 CPU 亲和性、**macOS 不支持**。macOS 的 Docker 模式下超出虚拟机核数时自动收窄 |
| `NOHUP=1` 变量 | `-Background` | 关 | 后台运行，长时测试建议开启 |
| `--docker` | `-Docker` | 关 | 强制 Docker 模式，Docker 不可用时报错退出，不降级，保证环境可比 |
| `--no-docker` | `-NoDocker` | 关 | 不使用 Docker，本机 Java 25+ 直跑；两个开关均不加即自动模式（有 Docker 用 Docker，否则直跑） |
| `--stop-server` | `-StopServer` | — | 仅停止服务器，不执行测试（容器或直跑进程均可） |

测试参数（直接跟在命令后，透传给 `capacity_test.py`）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--preset` | `zombie` | 负载类型：`zombie`（AI + 寻路 + 碰撞，最接近真实生物负载）/ `armor_stand`（纯实体 tick）/ `item`（掉落物） |
| `--step` | `1000` | 初始步长及最小前进量；之后按回归预测的拐点自适应跳级 |
| `--max-levels` | `40` | 最多加载级数 |
| `--warmup` | `60` | 每级最大预热秒数（P95 连续 3 个样本稳定即提前结束，至少 15s） |
| `--measure` | `60` | 每级测量秒数 |
| `--interval` | `5` | 采样间隔秒数 |
| `--threshold` | `50` | P95 MSPT 阈值（ms），50 即开始掉 tick |
| `--keep-entities` | 关 | 测试结束后不清理实体，便于连入服务器观察 |

示例：`./Linux/run_capacity_test.sh --preset armor_stand --step 2000` / `./macOS/run_capacity_test.sh --preset armor_stand --step 2000` / `.\Windows\run_capacity_test.ps1 --preset armor_stand --step 2000`

## 其他测试工具

平台入口脚本仅运行负载梯度容量测试。以下工具在测试服运行期间从工具包根目录单独调用：

```bash
# 稳定性 soak 测试:以最近一次容量测试有效负载的 80% 持续运行 1 小时,每分钟采样
python3 python/soak_test.py --capacity-percent 80 --duration 3600 --interval 60

# ABAB 交替 sprint 实验:固定 100k tick,默认每组 5 次
python3 python/sprint_experiment.py --help

# sprint 结果统计:汇总 CSV/JSON,输出中位数、IQR、变异系数与判定(离线,不连接服务器)
python3 python/sprint_stats.py results/sprint.csv

# 批量实体生成评估:比较逐条 summon、分批发送与 datapack function 三种策略
python3 python/batch_spawn.py --help

# 真实玩家容量测试:需提供外部客户端 JSONL 命令适配器,不提供模拟回退
python3 python/player_capacity.py --adapter-command 'node player-client.js --jsonl {player_id}' --help

# 红石 / 漏斗 / 区块探索负载:会修改世界的负载必须显式授权
python3 python/world_loads.py redstone --count 32 --help
python3 python/world_loads.py hopper --count 64 --help
python3 python/world_loads.py exploration --count 16 --help
```

soak 测试、sprint 实验与批量生成评估均需真实 RCON 连接和运行中的测试服；sprint 统计为离线计算。玩家模拟依赖外部客户端适配器；红石与漏斗负载默认拒绝修改世界，通过 API 调用时必须显式授权。

## 结果解读

测试结束后直接输出结论，例如：

```
   count      P50      P95    TPS
    1000     12.6     14.6   20.0
    2000     25.0     27.5   20.0
    3300     41.2     44.8   20.0
    3850     55.1     58.2   18.1
容量拐点(P95=50.0ms,回归,4 级 R²=0.9998): 约 3660 ± 30 个 zombie
  交叉校验(相邻两点插值): 3655(偏差 0.1%)
```

拐点由全部有效级的最小二乘回归求出（P95 与实体数实测高度线性），`±` 为最大残差换算的实体数；R² 低于 0.98 时回退为相邻两点插值并标注低可信度。

CSV 文件写入 `./results/`：

- `summary-*.csv`：每级一行（实体数、P50/P95 中位数、TPS、steal%、valid），用于绘制负载–MSPT 曲线；`valid=0` 的级因实体损耗失真，未参与拟合。
- `samples-*.csv`：每 5 秒一行的原始采样，用于排查抖动、对齐 GC 日志（`$DIR/gc.log`）。

判读要点：

- **P95 < 50ms 且 TPS = 20**：该负载量在承载范围内。
- **拐点值是最终结论**（例如"约 5320 个僵尸"），中间各级的 MSPT 数值仅为过程量。
- **steal% 列大于 2% 的级应作废重测**：这是云宿主机上其他租户争抢 CPU 所致，与被测服务器无关（脚本会当场警告）。

## 保证结果可靠的三项原则

1. **同一配置重复 3 次，取拐点中位数。** 单次结果不构成结论。
2. **对比实验（更换 GC、机器或配置）采用 ABAB 交替顺序**，不要 AAA 之后 BBB。环境随时间漂移会污染串行对比。
3. **报告绝对量（ms/tick、实体数），不报告 TPS 倍数。** 空载时 TPS 是倒数指标，会把 0.3ms 的噪声放大为"8 倍差异"。

## 常见问题

**RCON 未就绪超时** — 服务器首次启动生成世界较慢，或内存不足。查看日志：`docker logs purpur-test`（直跑模式查看 `$DIR/server.log`）。macOS/Windows 的 Docker 模式还需确认虚拟机内存不少于 8GB（Docker Desktop → Settings → Resources），否则 4G 堆的服务器会被 OOM 终止；macOS 脚本会在启动前检查并提示。

**RCON 密码位置** — 首次部署时随机生成，写在 `$DIR/server.properties` 的 `rcon.password=`（Docker 模式仅绑定 127.0.0.1，不对外暴露）。`capacity_test.py` 会自动读取，无需手动填写。

**提示 "spark 不可用,改用 /tick query"** — 属正常情况。当前 Purpur build 未内置 spark，回退方案精度为 0.1ms，在 50ms 拐点附近足够使用。

**僵尸数量增加但 MSPT 不上升** — 通常是 entity-activation-range 未生效（无玩家在线时 AI 被跳过）。脚本部署时会写入 `spigot.yml`（全部为 0），但若数据目录中已存在旧的 `spigot.yml`，脚本不会覆盖。请检查 `$DIR/spigot.yml` 中 `entity-activation-range` 是否全部为 0，修改后执行 `docker restart purpur-test`（直跑模式：先停服，Linux/macOS 使用 `--stop-server`，Windows 双击 `停服.bat`，再重新运行脚本）。

**macOS 双击 .command 提示"无法打开，因为无法验证开发者"** — 这是 Gatekeeper 隔离标记，从网络下载的 zip 解压后均会带有。右键点击文件 → 打开 → 再次点击"打开"即可；或在终端执行 `xattr -dr com.apple.quarantine macOS/`。若提示权限不足，则是 zip 丢失了执行位：`chmod +x macOS/run_capacity_test.sh macOS/*.command`。

**macOS 直跑模式结果明显高于 Linux** — 属正常现象，不应直接横向比较。除 CPU 本身差异外，macOS 直跑不绑核（无 `taskset`），调度器会把 java 分配到全部性能核；Apple Silicon 的大容量 L2 缓存与统一内存对实体 tick 这类指针追逐负载也有明显优势。跨机对比请两端均使用 Docker 模式并保持 `CPUSET` 一致。

**对比多台服务器** — 将本工具包复制到每台机器分别运行，保持参数一致，比较各自的拐点值即可。

**测试结束后停止服务器或彻底删除** —
```bash
# Linux
./Linux/run_capacity_test.sh --stop-server               # 停止服务器(容器或直跑进程均可,保留数据,下次快速启动)
docker rm -f purpur-test && sudo rm -rf /opt/purpur-test # 彻底删除(Docker 模式)
rm -rf ~/purpur-test                                     # 彻底删除(直跑模式)
```
```bash
# macOS
./macOS/run_capacity_test.sh --stop-server               # 停止服务器(等价于双击 停服.command)
docker rm -f purpur-test; rm -rf ~/purpur-test           # 彻底删除(两种模式共用同一目录)
```
```powershell
# Windows
.\Windows\run_capacity_test.ps1 -StopServer              # 停止服务器(容器或直跑进程均可;等价于双击 停服.bat)
docker rm -f purpur-test; Remove-Item -Recurse -Force "$env:USERPROFILE\purpur-test"  # 彻底删除
```

**直跑模式（`--no-docker` / `-NoDocker`）注意事项** — RCON（25575）监听所有网卡（Minecraft 无法单独为 RCON 绑定地址），密码随机生成；机器有公网 IP 时请在防火墙拦截 25575。Linux 直跑默认数据目录为 `~/purpur-test`（无需 root），macOS 两种模式均使用 `~/purpur-test`。Windows/macOS 首次启动 Java 时出现防火墙弹窗属正常现象，家用机可拒绝（测试全程走本机回环）。

**中途终止测试** — 前台按 Ctrl+C，Linux/macOS 后台执行 `pkill -f capacity_test.py`。之后可用冒烟参数再运行一次由脚本自动清场，或进入服务器执行 `kill @e[type=zombie]`。

## 许可证

本项目基于 [MIT 许可证](LICENSE)发布。您可自由使用、复制、修改、合并、发布、分发、再许可及销售本软件的副本，包括用于商业用途；唯一要求是在软件的所有副本或实质性部分中保留上述版权声明与许可声明。本软件按"原样"提供，不附带任何明示或默示的担保，作者与版权持有人不对因使用本软件而产生的任何索赔、损害或其他责任负责。

许可范围仅限本仓库自有代码。本工具在运行时会自动下载 [Purpur](https://purpurmc.org/) 服务端（Purpur 自身以 MIT 分发，但其启动过程会获取并修补 Mojang 官方服务端 jar），该部分受 [Minecraft 最终用户许可协议](https://www.minecraft.net/eula)约束，不在本许可证覆盖范围内，使用者需自行遵守。
