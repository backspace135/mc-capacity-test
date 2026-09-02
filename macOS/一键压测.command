#!/bin/bash
# 双击入口(macOS):打开 Terminal 跑完整压测,开头可选运行方式。
# 实际逻辑都在同目录的 run_capacity_test.sh 里。
cd "$(dirname "$0")"
bash ./run_capacity_test.sh --menu "$@"
code=$?
echo
echo "按回车键关闭窗口..."
read -r _
exit $code
