@echo off
chcp 65001 >nul
title MC 压测停服
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_capacity_test.ps1" -StopServer

echo.
pause
