#!/usr/bin/env bash
# 一键容量压测(在服务器本机上直接运行)
# 自动:下载 Purpur → 写配置(虚空世界/RCON/激活范围) → 启动 Docker 容器 → 执行测试 → 结果存 ./results/
#
# 用法:
#   ./run_capacity_test.sh [capacity_test.py 的参数...]
# 例:
#   ./run_capacity_test.sh                                       # 完整测试(僵尸,自适应步长,约 10 分钟)
#   ./run_capacity_test.sh --preset armor_stand --step 2000
#   ./run_capacity_test.sh --warmup 120 --measure 300 --interval 10  # 长窗口精测
# 环境变量:
#   DIR=/opt/purpur-test  服务器数据目录
#   CPUSET=0-7            容器绑核(保证测试间可比)
#   NOHUP=1               后台运行(长测试防终端断开),自行 tail 日志
set -euo pipefail
cd "$(dirname "$0")"

DIR=${DIR:-/opt/purpur-test}
CPUSET=${CPUSET:-0-7}
IMAGE="eclipse-temurin:25-jre"
PURPUR_URL="https://api.purpurmc.org/v2/purpur/26.2/latest/download"

command -v docker >/dev/null || { echo "需要 docker"; exit 1; }
command -v python3 >/dev/null || { echo "需要 python3"; exit 1; }

echo "== [1/4] 准备 $DIR(jar/eula/配置)=="
mkdir -p "$DIR"
if [ ! -f "$DIR/purpur.jar" ]; then
  echo "下载 purpur.jar ..."
  curl -fL --retry 3 -o "$DIR/purpur.jar" "$PURPUR_URL"
fi
echo "eula=true" > "$DIR/eula.txt"
if [ ! -f "$DIR/server.properties" ]; then
  # RCON 只绑 127.0.0.1,密码随机生成,记录在 server.properties 里
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

echo "== [2/4] 确保容器 purpur-test 运行(绑核 $CPUSET)=="
docker inspect purpur-test >/dev/null 2>&1 \
  && docker start purpur-test >/dev/null \
  || docker run -d --name purpur-test --memory 6g --cpuset-cpus "$CPUSET" \
       -p 127.0.0.1:25575:25575 -p 25565:25565 -v "$DIR:/data" -w /data "$IMAGE" \
       java -Xms4G -Xmx4G -XX:+UseG1GC "-Xlog:gc*:file=/data/gc.log:time,uptime" \
       -jar purpur.jar nogui >/dev/null

echo "== [3/4] 等待 RCON 就绪 =="
for i in $(seq 1 60); do
  if python3 capacity_test.py --server-dir "$DIR" --ping 2>/dev/null; then
    break
  fi
  [ "$i" = 60 ] && { echo "RCON 90s 未就绪,查日志: docker logs purpur-test"; exit 1; }
  sleep 3
done

echo "== [4/4] 执行容量测试(结果在 ./results/)=="
mkdir -p results
if [ "${NOHUP:-0}" = "1" ]; then
  TS=$(date +%Y%m%d-%H%M%S)
  nohup python3 -u capacity_test.py --server-dir "$DIR" --outdir ./results "$@" > "results/run-$TS.log" 2>&1 &
  echo "已后台运行(PID $!)。看进度: tail -f results/run-$TS.log"
  exit 0
fi
python3 -u capacity_test.py --server-dir "$DIR" --outdir ./results "$@"

echo "完成。停服: docker stop purpur-test"
