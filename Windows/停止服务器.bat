@echo off
rem ASCII only in this file: cmd garbles multibyte UTF-8 batch lines while parsing.
title MC Capacity Test - Stop
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_capacity_test.ps1" -StopServer
echo.
pause
