@echo off
REM DVForge farm — stop whatever is bound to :8765 (DVForge app) and :8766 (queue.py)
REM
REM --with-app / --with-queue leave those processes running on purpose (so other
REM machines keep claiming after you Ctrl+C the worker). Use this when you
REM actually want them gone — e.g. before testing a fresh --notification-webhook
REM run, or to free the ports for a clean restart.
REM
REM Usage:
REM   stop-farm.bat            stop :8765 and :8766
REM   stop-farm.bat 8766       stop only the queue
setlocal enabledelayedexpansion
cd /d "%~dp0"

set PORTS=%*
if "%PORTS%"=="" set PORTS=8765 8766

for %%P in (%PORTS%) do call :stop_port %%P

REM Stale worker locks are harmless (acquire_lock() checks liveness), but
REM clean them up anyway so a leftover lock file never causes confusion.
if exist ".worker-*.lock" del /q ".worker-*.lock" >nul 2>&1

echo Done.
goto :eof

:stop_port
set PORT=%1
set FOUND=0
for /f "tokens=5" %%A in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
  set FOUND=1
  echo   stopping pid %%A on :%PORT%
  taskkill /PID %%A /F >nul 2>&1
)
if "%FOUND%"=="0" echo   :%PORT% — nothing listening
goto :eof
