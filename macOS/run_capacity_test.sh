#!/usr/bin/env bash
# 一键容量压测(macOS 版,在被测机器上直接运行)
# 自动:下载 Purpur → 写配置(虚空世界/RCON/激活范围) → 启动服务器 → 执行测试 → 结果存 ./results/
#
# 两种运行方式:
#   Docker 模式(默认): 需要 Docker Desktop/OrbStack/colima,支持绑核与内存限制,RCON 只绑 127.0.0.1
#   直跑模式(--no-docker): 不需要 Docker,用本机 Java 25+ 直接启动 Purpur
#
# 不熟悉命令行?直接双击同目录的「一键压测.command」即可;测完双击「停服.command」关服。
#
# 用法(在工具包根目录下):
#   ./macOS/run_capacity_test.sh [选项] [capacity_test.py 的参数...]
# 例:
#   ./macOS/run_capacity_test.sh                                       # 完整测试(僵尸,自适应步长,约 10 分钟)
#   ./macOS/run_capacity_test.sh --no-docker                           # 不用 Docker,本机 Java 直跑
#   ./macOS/run_capacity_test.sh --preset armor_stand --step 2000
#   ./macOS/run_capacity_test.sh --warmup 120 --measure 300 --interval 10  # 长窗口精测
#   ./macOS/run_capacity_test.sh --stop-server                         # 停服(容器或直跑进程)
# 选项(脚本自己消费,其余参数透传给 capacity_test.py):
#   --docker       强制 Docker 模式:docker 不可用时直接报错,不悄悄降级(保证环境可比)
#   --no-docker    本机 Java 直跑(不加任何开关时:有 docker 用 docker,没有自动切直跑)
#   --menu         交互式选择运行方式(.command 双击入口用)
#   --stop-server  只停服,不测试
# 环境变量:
#   DIR=~/purpur-test     服务器数据目录(默认放家目录:Docker Desktop 默认只共享 /Users 等路径)
#   CPUSET=0-7            绑核(仅 Docker 模式生效,超出 VM 核数会自动收窄;macOS 无 taskset,直跑不绑核)
#   NOHUP=1               后台运行(长测试防终端断开),自行 tail 日志
set -euo pipefail
cd "$(dirname "$0")/.."   # 工具包根目录:capacity_test.py 与 results/ 都在这里

CPUSET=${CPUSET:-0-7}
IMAGE="eclipse-temurin:25-jre"
PURPUR_URL="https://api.purpurmc.org/v2/purpur/26.2/latest/download"

# ---- 拆分脚本选项与透传参数 ----
FORCE_DOCKER=0; NO_DOCKER=0; STOP_SERVER=0; MENU=0
PASS=()
for a in "$@"; do
  case "$a" in
    --docker)      FORCE_DOCKER=1 ;;
    --no-docker)   NO_DOCKER=1 ;;
    --menu)        MENU=1 ;;
    --stop-server) STOP_SERVER=1 ;;
    *)             PASS+=("$a") ;;
  esac
done
if [ "$FORCE_DOCKER" = 1 ] && [ "$NO_DOCKER" = 1 ]; then
  echo "--docker 与 --no-docker 不能同时指定"; exit 1
fi

# macOS 自带 python3(装了 Command Line Tools 后可用);没有就提示装
if ! command -v python3 >/dev/null 2>&1; then
  echo "需要 python3。执行 xcode-select --install 安装命令行工具,或从 https://www.python.org/downloads/ 安装"
  exit 1
fi

docker_ok() {
  command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1
}

# java 主版本号:25.0.1 → 25,1.8.0_392 → 8,25-ea → 25
java_major() {
  local v
  v=$("$1" -version 2>&1 | awk -F'"' '/version/ {print $2; exit}') || return 1
  [ -n "$v" ] || return 1
  case "$v" in
    1.*) v=${v#1.}; printf '%s' "${v%%[!0-9]*}" ;;
    *)   printf '%s' "${v%%[!0-9]*}" ;;
  esac
}

# ---- --stop-server: 只停服 ----
if [ "$STOP_SERVER" = 1 ]; then
  DIR=${DIR:-}
  done_stop=0
  if docker_ok && docker inspect purpur-test >/dev/null 2>&1; then
    docker stop purpur-test >/dev/null
    echo "已停止容器 purpur-test(数据保留,下次秒起)"
    done_stop=1
  fi
  for d in ${DIR:+"$DIR"} "$HOME/purpur-test"; do
    PIDFILE="$d/server.pid"
    [ -f "$PIDFILE" ] || continue
    SRV_PID=$(head -1 "$PIDFILE")
    if kill -0 "$SRV_PID" 2>/dev/null; then
      # 先走 RCON 优雅关服,30s 不退再强杀
      python3 python/capacity_test.py --server-dir "$d" --stop >/dev/null 2>&1 || true
      for i in $(seq 1 30); do
        kill -0 "$SRV_PID" 2>/dev/null || break
        sleep 1
      done
      kill -0 "$SRV_PID" 2>/dev/null && kill -9 "$SRV_PID" 2>/dev/null || true
      echo "已停止直跑服务器(PID $SRV_PID)"
    fi
    rm -f "$PIDFILE"
    done_stop=1
  done
  [ "$done_stop" = 1 ] || echo "未发现在运行的测试服"
  exit 0
fi

# ---- 交互菜单(.command 双击入口) ----
if [ "$MENU" = 1 ] && [ "$FORCE_DOCKER" = 0 ] && [ "$NO_DOCKER" = 0 ]; then
  echo "============================================"
  echo "  Minecraft 服务器容量压测(一键版 · macOS)"
  echo "  完整测试约 10 分钟,期间请勿关窗口"
  echo "============================================"
  echo
  echo "选择运行方式:"
  echo "  [1] 自动 - 有 Docker 用 Docker,没有就本机 Java 直跑(推荐)"
  echo "  [2] 强制 Docker - Docker 不可用则报错退出"
  echo "  [3] 不用 Docker - 本机 Java 25+ 直跑"
  printf '输入 1/2/3 后回车(直接回车 = 1 自动): '
  read -r sel || sel=""
  case "$(printf '%s' "$sel" | tr -d '[:space:]')" in
    2) FORCE_DOCKER=1 ;;
    3) NO_DOCKER=1 ;;
  esac
  echo
fi

# ---- 选择运行模式 ----
USE_DOCKER=1
[ "$NO_DOCKER" = 1 ] && USE_DOCKER=0
if [ "$USE_DOCKER" = 1 ] && ! docker_ok; then
  if [ "$FORCE_DOCKER" = 1 ]; then
    echo "docker 不可用(未安装,或 Docker Desktop/OrbStack/colima 未启动),而 --docker 要求必须用 Docker,退出"
    exit 1
  fi
  echo "[!] docker 不可用(未安装,或 Docker Desktop/OrbStack/colima 未启动),改用本机 Java 直跑"
  USE_DOCKER=0
fi

# 数据目录一律放家目录:Docker Desktop 默认只共享 /Users /Volumes /private /tmp,
# 挂 /opt 会启动即失败;直跑模式也免 sudo
DIR=${DIR:-$HOME/purpur-test}

if [ "$USE_DOCKER" = 1 ]; then
  echo "运行模式: Docker 容器"
  # Docker Desktop 跑在虚拟机里,VM 的核数/内存与宿主不同,超了要当场收窄而不是启动失败
  NCPU=$(docker info --format '{{.NCPU}}' 2>/dev/null || true)
  MAXIDX=$(printf '%s' "$CPUSET" | tr ',-' '\n\n' | grep -E '^[0-9]+$' | sort -n | tail -1)
  if [ -n "$NCPU" ] && [ -n "$MAXIDX" ] && [ "$NCPU" -gt 0 ] && [ "$MAXIDX" -ge "$NCPU" ]; then
    echo "  [!] Docker 虚拟机只有 $NCPU 核,CPUSET=$CPUSET 超界,收窄为 0-$((NCPU - 1))"
    echo "      (跨机对比请在 Docker Desktop → Settings → Resources 里把 CPU 调成一致的核数)"
    CPUSET="0-$((NCPU - 1))"
  fi
  MEMTOTAL=$(docker info --format '{{.MemTotal}}' 2>/dev/null || true)
  if [ -n "$MEMTOTAL" ] && [ "$MEMTOTAL" -lt 6871947673 ] 2>/dev/null; then
    echo "  [!] Docker 虚拟机内存只有 $((MEMTOTAL / 1024 / 1024))MB,少于容器要的 6GB"
    echo "      请到 Docker Desktop → Settings → Resources 调到 8GB 以上,否则服务器会被 OOM 杀掉"
  fi
else
  echo "运行模式: 本机 Java 直跑"
  echo "  [!] macOS 无 taskset,直跑模式不绑核(不影响本次测试,跨机对比时注意)"
  # PATH 上的 java 常常不是最新的那个(Homebrew 的 openjdk 默认不 link,java_home 也看不到),
  # 所以把常见安装位置全扫一遍,挑主版本号最高的用
  CANDS=""
  add_cand() { [ -n "${1:-}" ] && [ -x "$1" ] && CANDS="$CANDS
$1"; return 0; }
  command -v java >/dev/null 2>&1 && add_cand "$(command -v java)"
  add_cand "${JAVA_HOME:-}/bin/java"
  if [ -x /usr/libexec/java_home ]; then
    JH=$(/usr/libexec/java_home 2>/dev/null || true)
    add_cand "$JH/bin/java"
  fi
  for j in /Library/Java/JavaVirtualMachines/*/Contents/Home/bin/java \
           "$HOME/Library/Java/JavaVirtualMachines"/*/Contents/Home/bin/java \
           /opt/homebrew/opt/openjdk*/bin/java \
           /opt/homebrew/opt/openjdk*/libexec/openjdk.jdk/Contents/Home/bin/java \
           /usr/local/opt/openjdk*/bin/java \
           /usr/local/opt/openjdk*/libexec/openjdk.jdk/Contents/Home/bin/java; do
    add_cand "$j"
  done
  JAVA_BIN=""; JMAJOR=0
  while IFS= read -r j; do
    [ -n "$j" ] && [ -x "$j" ] || continue
    m=$(java_major "$j" 2>/dev/null) || continue
    [ -n "$m" ] || continue
    if [ "$m" -gt "$JMAJOR" ]; then JMAJOR=$m; JAVA_BIN=$j; fi
  done <<< "$CANDS"
  if [ -z "$JAVA_BIN" ] || [ "$JMAJOR" -lt 25 ]; then
    CUR=$([ "$JMAJOR" -gt 0 ] && echo "Java $JMAJOR" || echo "未安装")
    echo "直跑模式需要 Java 25+(当前: $CUR)。请装 Temurin 25: https://adoptium.net/"
    echo "(或 brew install --cask temurin@25;国内可用清华镜像 https://mirrors.tuna.tsinghua.edu.cn/Adoptium/)"
    exit 1
  fi
  echo "  使用 Java $JMAJOR: $JAVA_BIN"
fi
PIDFILE="$DIR/server.pid"

echo "== [1/4] 准备 $DIR(jar/eula/配置)=="
mkdir -p "$DIR"
if [ ! -f "$DIR/purpur.jar" ]; then
  echo "下载 purpur.jar(约 64MB,仅首次)..."
  curl -fL --retry 3 -o "$DIR/purpur.jar" "$PURPUR_URL"
fi
echo "eula=true" > "$DIR/eula.txt"
if [ ! -f "$DIR/server.properties" ]; then
  # RCON 只绑 127.0.0.1(Docker 模式),密码随机生成,记录在 server.properties 里
  RCON_PW=$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')
  cat > "$DIR/server.properties" <<PROPS
level-type=minecraft:flat
generator-settings={"layers":[],"biome":"minecraft:the_void"}
enable-rcon=true
rcon.port=25575
rcon.password=$RCON_PW
online-mode=false
white-list=true
spawn-protection=0
view-distance=8
simulation-distance=8
max-players=5
motd=capacity-test
PROPS
fi
# 关键:entity-activation-range 全 0(禁用降频),否则无玩家在线时 AI 被跳过,僵尸负载失真
if [ ! -f "$DIR/spigot.yml" ]; then
  cat > "$DIR/spigot.yml" <<'YML'
world-settings:
  default:
    entity-activation-range:
      animals: 0
      monsters: 0
      raiders: 0
      misc: 0
      water: 0
      villagers: 0
      flying-monsters: 0
YML
fi

if [ "$USE_DOCKER" = 1 ]; then
  echo "== [2/4] 确保容器 purpur-test 运行(绑核 $CPUSET)=="
  if docker inspect purpur-test >/dev/null 2>&1; then
    echo "  容器已存在,启动..."
    docker start purpur-test >/dev/null
  else
    if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
      echo "  拉取镜像 $IMAGE(仅首次,视网速 1-2 分钟)..."
      docker pull "$IMAGE"
    fi
    echo "  创建并启动容器..."
    docker run -d --name purpur-test --memory 6g --cpuset-cpus "$CPUSET" \
      -p 127.0.0.1:25575:25575 -p 25565:25565 -v "$DIR:/data" -w /data "$IMAGE" \
      java -Xms4G -Xmx4G -XX:+UseG1GC "-Xlog:gc*:file=/data/gc.log:time,uptime" \
      -jar purpur.jar nogui >/dev/null
  fi
else
  echo "== [2/4] 确保本机 Java 服务器运行 =="
  ALIVE=0
  if [ -f "$PIDFILE" ] && kill -0 "$(head -1 "$PIDFILE")" 2>/dev/null; then
    ALIVE=1
  fi
  if [ "$ALIVE" = 1 ]; then
    echo "  服务器已在运行(PID $(head -1 "$PIDFILE")),直接复用"
  else
    # macOS 没有 setsid:用 (nohup ... &) 脱离作业控制 + 断开 stdin,防止挂住终端/ssh 会话
    # PID 由子 shell 先写 $$ 再 exec 得到($! 拿到的是外层 bash,exec 后才是 java 本尊)
    ( cd "$DIR" && nohup bash -c \
        'echo $$ > server.pid; exec "$0" -Xms4G -Xmx4G -XX:+UseG1GC -Xlog:gc*:file=gc.log:time,uptime -jar purpur.jar nogui' \
        "$JAVA_BIN" > server.log 2>&1 < /dev/null & )
    for i in $(seq 1 50); do [ -s "$PIDFILE" ] && break; sleep 0.1; done
    echo "  已启动 java(PID $(head -1 "$PIDFILE" 2>/dev/null || echo '?')),日志: $DIR/server.log"
    echo "  [!] 直跑模式 RCON(25575)监听所有网卡,密码随机;机器在公网上的话请在防火墙拦掉该端口"
  fi
fi

echo "== [3/4] 等待 RCON 就绪(首次启动要生成世界,稍慢)=="
for i in $(seq 1 60); do
  if python3 python/capacity_test.py --server-dir "$DIR" --ping 2>/dev/null; then
    break
  fi
  if [ "$i" = 60 ]; then
    HINT=$([ "$USE_DOCKER" = 1 ] && echo "docker logs purpur-test" || echo "$DIR/server.log")
    echo "RCON 180s 未就绪,查日志: $HINT"; exit 1
  fi
  printf '\r  等待中 %ds / 最多 180s ' $((i * 3))
  sleep 3
done
echo

echo "== [4/4] 执行容量测试(结果在 ./results/)=="
mkdir -p results
if [ "${NOHUP:-0}" = "1" ]; then
  TS=$(date +%Y%m%d-%H%M%S)
  nohup python3 -u python/capacity_test.py --server-dir "$DIR" --outdir ./results ${PASS[@]+"${PASS[@]}"} > "results/run-$TS.log" 2>&1 &
  echo "已后台运行(PID $!)。看进度: tail -f results/run-$TS.log"
  exit 0
fi
python3 -u python/capacity_test.py --server-dir "$DIR" --outdir ./results ${PASS[@]+"${PASS[@]}"}

if [ "$USE_DOCKER" = 1 ]; then
  echo "完成。停服: docker stop purpur-test(或 ./macOS/run_capacity_test.sh --stop-server)"
else
  echo "完成。停服: ./macOS/run_capacity_test.sh --stop-server"
fi
