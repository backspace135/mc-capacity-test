# Minecraft 服务器容量压测工具包 · 使用教程

一句话:不测"最快能跑多少 TPS"（那个数字没意义），而是**固定 20 TPS，往服务器里一档档加负载（僵尸/盔甲架/掉落物），看加到多少时 P95 MSPT 触及 50ms（开始掉 tick）**——产出一条负载–MSPT 曲线和一个有单位、可复现、跨机器可比的容量值，比如"能扛 5200 个僵尸"。

方法学原理和常见错误测法的分析见 `benchmark-methodology.md`。

## 文件清单

| 文件 | 作用 |
|---|---|
| `Linux/run_capacity_test.sh` | **一键入口（Linux）**：自动部署测试服 → 执行测试 → 结果存 `./results/` |
| `macOS/一键压测.command` | **一键入口（macOS）**：双击即完整测试，开头选运行方式 |
| `macOS/停服.command` | 双击停掉测试服（容器或直跑进程） |
| `macOS/run_capacity_test.sh` | macOS 实际逻辑（.command 只是包装）；命令行用户可直接带参数调用 |
| `Windows/一键压测.bat` | **一键入口（Windows）**：双击即完整测试，进度全程显示在窗口里 |
| `Windows/停服.bat` | 双击停掉测试服（容器或直跑进程） |
| `Windows/run_capacity_test.ps1` | Windows 实际逻辑（bat 只是包装）；PowerShell 用户可直接带参数调用 |
| `capacity_test.py` | 测试逻辑（两平台共用），也可单独运行（`python3 capacity_test.py -h` 看全部参数） |
| `benchmark-methodology.md` | 压测方法设计文档 |
| `README.md` | 本教程 |
| `LICENSE` | MIT 许可证 |

## 前置条件

在**要被测的服务器上**直接运行本工具包，需要：

- **Linux**：`python3`、`curl`；默认用 `docker`（当前用户可直接使用，root 或 docker 组），**没有 Docker 也能跑**——脚本会自动切到本机 Java 直跑（需 Java 25+，[Temurin](https://adoptium.net/)），或用 `--no-docker` 明确指定；
- **macOS**：`python3`（`xcode-select --install` 即可）、`curl`（系统自带）；默认用 Docker Desktop / OrbStack / colima，**没有 Docker 也能跑**——脚本会自动切到本机 Java 直跑（需 Java 25+，[Temurin](https://adoptium.net/) 或 `brew install --cask temurin@25`），或用 `--no-docker` 明确指定；
- **Windows**：Python 3；默认用 Docker Desktop（Linux 容器模式），**没有 Docker 也能跑**——脚本会自动切到本机 Java 直跑（需 Java 25+，[Temurin](https://adoptium.net/)），或用 `-NoDocker` 明确指定；
- 三者都要求：空闲内存 ≥ 6GB，端口 25565/25575 未被占用。

其余全自动：Purpur jar 下载、虚空世界配置、RCON（密码随机生成；Docker 模式只绑 127.0.0.1）、容器/进程启动都由脚本完成。**不需要预先装任何 Minecraft 服务器。**

## 快速开始

**Windows：解压后双击 `Windows\一键压测.bat`，完事双击 `Windows\停服.bat`。** 就这么多——开头选运行方式（1 自动 / 2 强制 Docker / 3 不用 Docker，直接回车 = 自动），然后窗口里依次显示 `[1/4] 准备`（含 jar 下载进度）→ `[2/4] 启动服务器` → `[3/4] 等 RCON（实时计秒）` → `[4/4] 逐级加载测试`，最后直接给结论。要调参数就走 PowerShell（在工具包根目录）：

```powershell
# 冒烟(2 分钟,确认全链路通)
.\Windows\run_capacity_test.ps1 --step 300 --max-levels 2 --warmup 10 --measure 30 --interval 5

# 明确不用 Docker(需要本机 Java 25+,绑核转为 CPU 亲和性;
# 不加此参数时,没装 Docker 也会自动切到直跑模式)
.\Windows\run_capacity_test.ps1 -NoDocker

# 后台运行 / 停服
.\Windows\run_capacity_test.ps1 -Background     # 看进度: Get-Content -Wait results\run-*.log
.\Windows\run_capacity_test.ps1 -StopServer
```

（如提示脚本被禁止，先执行 `Set-ExecutionPolicy -Scope Process Bypass`；bat 已内置 Bypass，无此问题。）

Linux：

```bash
# 从 GitHub Releases 下载 zip(每个 v* 标签由 CI 自动打包发布),传到服务器并解压
unzip mc-capacity-test-*.zip && cd mc-capacity-test
chmod +x Linux/run_capacity_test.sh

# 1) 先跑个 2 分钟冒烟,确认全链路通
./Linux/run_capacity_test.sh --step 300 --max-levels 2 --warmup 10 --measure 30 --interval 5

# 2) 正式测试(默认参数:僵尸,初始步长1000,回归预测自适应跳级,
#    每级预热≤60s+测量60s,直到 P95≥50ms;通常 4 级、约 10 分钟收敛)
NOHUP=1 ./Linux/run_capacity_test.sh
# 之后按屏幕提示 tail -f results/run-*.log 看进度

# 3) 长窗口精测(每级预热≤2分钟+测量5分钟,复现旧版慢速方法时用)
NOHUP=1 ./Linux/run_capacity_test.sh --warmup 120 --measure 300 --interval 10

# 明确不用 Docker(需要本机 Java 25+,绑核经 taskset 生效;
# 不加此参数时,没装 Docker 也会自动切到直跑模式)
./Linux/run_capacity_test.sh --no-docker

# 停服(容器或直跑进程均可)
./Linux/run_capacity_test.sh --stop-server
```

**macOS：解压后双击 `macOS/一键压测.command`，完事双击 `macOS/停服.command`。** 和 Windows 一样，开头选运行方式（1 自动 / 2 强制 Docker / 3 不用 Docker，直接回车 = 自动）。要调参数就走终端（在工具包根目录）：

```bash
chmod +x macOS/run_capacity_test.sh macOS/*.command   # 从 zip 解压后如果丢了执行权限

# 冒烟(2 分钟,确认全链路通)
./macOS/run_capacity_test.sh --step 300 --max-levels 2 --warmup 10 --measure 30 --interval 5

# 正式测试(后台跑,防终端断开)
NOHUP=1 ./macOS/run_capacity_test.sh          # 看进度: tail -f results/run-*.log

# 明确不用 Docker(需要本机 Java 25+)
./macOS/run_capacity_test.sh --no-docker

# 停服(容器或直跑进程均可)
./macOS/run_capacity_test.sh --stop-server
```

macOS 与另两版的差别只有两处：**直跑模式不绑核**（macOS 没有 `taskset`，跨机对比时注意；Docker 模式绑核照常生效），**数据目录固定在 `~/purpur-test`**（Docker Desktop 默认只共享 `/Users` 等路径，挂 `/opt` 会启动即失败）。脚本还会自动挑本机主版本号最高的 JDK——Homebrew 装的 `openjdk@25`/`openjdk@26` 默认不 link 到 PATH，不用手动改 `JAVA_HOME`。

第一次运行会下载 jar（~64MB）和 Docker 镜像（直跑模式无镜像），多花一两分钟；之后复用，几秒就绪。

## 参数

环境变量（Linux/macOS，放在命令前面）/ 开关（Linux、macOS `--` 开头，Windows `-` 开头）：

| Linux / macOS | Windows 参数 | 默认 | 说明 |
|---|---|---|---|
| `DIR` 变量 | `-Dir` | Linux `/opt/purpur-test`（直跑 `~/purpur-test`）/ macOS `~/purpur-test` / Windows `%USERPROFILE%\purpur-test` | 测试服数据目录 |
| `CPUSET` 变量 | `-CpuSet` | `0-7` | 绑定的 CPU 核（保证测试间可比，别改来改去）。直跑模式：Linux 经 `taskset`、Windows 转为 CPU 亲和性、**macOS 不支持**。macOS 的 Docker 模式下超出虚拟机核数会自动收窄 |
| `NOHUP=1` 变量 | `-Background` | 关 | 后台运行，长测试必开 |
| `--docker` | `-Docker` | 关 | 强制 Docker 模式，Docker 不可用时报错退出（不悄悄降级，保证环境可比） |
| `--no-docker` | `-NoDocker` | 关 | 不用 Docker，本机 Java 25+ 直跑；两个开关都不加 = 自动（有 Docker 用 Docker，没有自动切直跑） |
| `--stop-server` | `-StopServer` | — | 只停服，不测试（容器或直跑进程均可） |

测试参数（直接跟在命令后，透传给 `capacity_test.py`）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--preset` | `zombie` | 负载类型：`zombie`（AI+寻路+碰撞，最接近真实生物负载）/ `armor_stand`（纯实体 tick）/ `item`（掉落物） |
| `--step` | `1000` | 初始步长/最小前进量；之后按回归预测的拐点自适应跳级 |
| `--max-levels` | `40` | 最多加多少级 |
| `--warmup` | `60` | 每级最大预热秒数（P95 连续 3 样本稳定即提前结束，至少 15s） |
| `--measure` | `60` | 每级测量秒数 |
| `--interval` | `5` | 采样间隔秒 |
| `--threshold` | `50` | P95 MSPT 阈值（ms），50 = 开始掉 tick |
| `--keep-entities` | 关 | 测完不清理实体（想连服观察时用） |

例：`./Linux/run_capacity_test.sh --preset armor_stand --step 2000` / `./macOS/run_capacity_test.sh --preset armor_stand --step 2000` / `.\Windows\run_capacity_test.ps1 --preset armor_stand --step 2000`

## 怎么读结果

测完屏幕直接给结论，例如：

```
   count      P50      P95    TPS
    1000     12.6     14.6   20.0
    2000     25.0     27.5   20.0
    3300     41.2     44.8   20.0
    3850     55.1     58.2   18.1
容量拐点(P95=50.0ms,回归,4 级 R²=0.9998): 约 3660 ± 30 个 zombie
  交叉校验(相邻两点插值): 3655(偏差 0.1%)
```

拐点由全部有效级的最小二乘回归求出（P95–实体数实测高度线性），
`±` 为最大残差换算的实体数；R²<0.98 时回退相邻两点插值并标注低可信。

CSV 落在 `./results/`：

- `summary-*.csv`：每级一行（实体数、P50/P95 中位数、TPS、steal%、valid）——画负载–MSPT 曲线用它；`valid=0` 的级因实体损耗失真，未参与拟合。
- `samples-*.csv`：每 5 秒一行的原始采样——排查抖动、对齐 GC 日志（`$DIR/gc.log`）用它。

判读要点：

- **P95 < 50ms 且 TPS = 20** → 这个负载量扛得住。
- **拐点值才是结论**（"约 5320 个僵尸"），中间某级的 MSPT 数值只是过程量。
- **steal% 一列 > 2% 的级作废重测**——那是云宿主机邻居在抢 CPU，不是你服务器的问题（脚本会当场警告）。

## 让结果科学的三条纪律

1. **同一配置重复 3 次，取拐点中位数**。单次结果不构成结论。
2. **对比实验（换 GC、换机器、换配置）用 ABAB 交替顺序跑**，别 AAA 然后 BBB——环境随时间漂移会污染串行对比。
3. **报告绝对量（ms/tick、实体数），别报 TPS 倍数**。空载时 TPS 是倒数指标，会把 0.3ms 的噪声放大成"8 倍差异"。

## 常见问题

**RCON 未就绪超时** — 服务器首次启动生成世界较慢，或内存不足。看日志：`docker logs purpur-test`（直跑模式看 `$DIR/server.log`）。macOS/Windows 的 Docker 模式还要确认虚拟机内存 ≥ 8GB（Docker Desktop → Settings → Resources），否则 4G 堆的服务器会被 OOM 杀掉——macOS 脚本会在启动前检查并提示。

**RCON 密码在哪** — 首次部署时随机生成，写在 `$DIR/server.properties` 的 `rcon.password=`（Docker 模式只绑 127.0.0.1 不对外）。`capacity_test.py` 会自动读取，无需手填。

**提示 "spark 不可用,改用 /tick query"** — 正常。此 Purpur build 没带 spark，回退方案精度 0.1ms，在 50ms 拐点附近完全够用。

**僵尸加了很多但 MSPT 不涨** — 大概率 entity-activation-range 没生效（无玩家在线时 AI 被跳过）。脚本部署时会写好 `spigot.yml`（全 0），但如果数据目录里已有旧的 `spigot.yml`，脚本不会覆盖——手动检查 `$DIR/spigot.yml` 里 `entity-activation-range` 是否全为 0，改完 `docker restart purpur-test`（直跑模式：先停服——Linux/macOS `--stop-server`，Windows 双击 `停服.bat`——再重新运行脚本）。

**macOS 双击 .command 提示"无法打开，因为无法验证开发者"** — Gatekeeper 隔离标记，从网上下载的 zip 解压出来都会有。右键点文件 → 打开 → 再点"打开"即可；或在终端执行 `xattr -dr com.apple.quarantine macOS/`。若提示权限不足则是 zip 丢了执行位：`chmod +x macOS/run_capacity_test.sh macOS/*.command`。

**macOS 上直跑模式测出来比 Linux 高很多** — 正常，别直接横比。除了 CPU 本身的差异，macOS 直跑不绑核（无 `taskset`），调度器会把 java 排到全部性能核上；Apple Silicon 的大 L2 + 统一内存对实体 tick 这种指针追逐负载还格外占便宜。跨机对比请两边都用 Docker 模式且 `CPUSET` 一致。

**想对比多台服务器** — 把本工具包复制到每台机器各自运行，参数保持一致，比较各自的拐点值即可。

**测完想停服 / 彻底删除** —
```bash
# Linux
./Linux/run_capacity_test.sh --stop-server               # 停服(容器或直跑进程均可,保留数据,下次秒起)
docker rm -f purpur-test && sudo rm -rf /opt/purpur-test # 彻底删除(Docker 模式)
rm -rf ~/purpur-test                                     # 彻底删除(直跑模式)
```
```bash
# macOS
./macOS/run_capacity_test.sh --stop-server               # 停服(等价于双击 停服.command)
docker rm -f purpur-test; rm -rf ~/purpur-test           # 彻底删除(两种模式同一个目录)
```
```powershell
# Windows
.\Windows\run_capacity_test.ps1 -StopServer              # 停服(容器或直跑进程均可;等价于双击 停服.bat)
docker rm -f purpur-test; Remove-Item -Recurse -Force "$env:USERPROFILE\purpur-test"  # 彻底删除
```

**直跑模式（`--no-docker` / `-NoDocker`）的注意点** — RCON(25575)监听所有网卡（Minecraft 无法单独给 RCON 绑地址），密码随机生成；机器有公网 IP 的话请在防火墙拦掉 25575。Linux 直跑默认数据目录改为 `~/purpur-test`（免 root），macOS 两种模式都用 `~/purpur-test`。Windows/macOS 首次启动 Java 时防火墙弹窗属正常，家用机可拒绝（测试全程走本机回环）。

**中途想终止测试** — Ctrl+C（前台）或 `pkill -f capacity_test.py`（Linux/macOS 后台），之后可再跑一次冒烟参数让脚本自动清场，或进服执行 `kill @e[type=zombie]`。

## 许可证

[MIT](LICENSE)。随便用、改、商用、闭源分发，保留版权声明即可，作者不承担任何担保责任。

注意：本工具包会自动下载 [Purpur](https://purpurmc.org/) 服务端（其自身按 MIT 分发，但运行时会拉取并打补丁到 Mojang 的官方服务端 jar，后者受 [Minecraft EULA](https://www.minecraft.net/eula) 约束）。MIT 只覆盖本仓库自己的代码。
