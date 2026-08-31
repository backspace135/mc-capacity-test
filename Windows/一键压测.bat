@echo off
chcp 65001 >nul
title MC 容量压测
cd /d "%~dp0"

echo ============================================
echo   Minecraft 服务器容量压测(一键版)
echo   双击 = 完整测试,约 10 分钟,期间请勿关窗口
echo   也可带参数: 一键压测.bat --preset armor_stand
echo ============================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_capacity_test.ps1" %*

echo.
if errorlevel 1 (
  echo [压测未正常完成,请看上方提示]
) else (
  echo [压测完成,结论在上方,CSV 在 results\ 目录;测完记得双击「停服.bat」]
)
pause
