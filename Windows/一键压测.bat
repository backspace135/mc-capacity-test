@echo off
rem ASCII only in this file: cmd garbles multibyte UTF-8 batch lines while parsing.
rem All UI text (menu, progress, results) lives in run_capacity_test.ps1.
title MC Capacity Test
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_capacity_test.ps1" -Menu %*
echo.
pause
