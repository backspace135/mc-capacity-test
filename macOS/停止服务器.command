#!/bin/bash
# 双击入口(macOS):停止测试服(容器或直跑进程均可,数据保留,下次快速启动)。
cd "$(dirname "$0")"
bash ./run_capacity_test.sh --stop-server
echo
echo "按回车键关闭窗口..."
read -r _
