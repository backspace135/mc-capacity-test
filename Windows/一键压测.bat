@echo off
chcp 65001 >nul
title MC 容量压测
cd /d "%~dp0"

echo ============================================
echo   Minecraft 服务器容量压测(一键版)
echo   完整测试约 10 分钟,期间请勿关窗口
echo   也可带参数: 一键压测.bat --preset armor_stand
echo ============================================
echo.
echo 选择运行方式:
echo   [1] 自动 - 有 Docker 用 Docker,没有就本机 Java 直跑(推荐)
echo   [2] 强制 Docker - Docker Desktop 不可用则报错退出
echo   [3] 不用 Docker - 本机 Java 25+ 直跑
echo.
choice /C 123 /N /D 1 /T 30 /M "输入 1/2/3(30 秒不选默认 1): "

set "MODE="
if errorlevel 3 (
  set "MODE=-NoDocker"
) else if errorlevel 2 (
  set "MODE=-Docker"
)
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_capacity_test.ps1" %MODE% %*

echo.
if errorlevel 1 (
  echo [压测未正常完成,请看上方提示]
) else (
  echo [压测完成,结论在上方,CSV 在 results\ 目录;测完记得双击「停服.bat」]
)
pause
