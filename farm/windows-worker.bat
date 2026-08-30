@echo off
REM Run on the Windows laptop. DVForge (run.bat) must already be open.
REM Scripts can live in C:\DVForge\farm — the INBOX must be the NAS folder
REM the Mac uses, or this PC will never see jobs submitted from the Mac.

set "DVFORGE_URL=http://127.0.0.1:8765"
set "DVFORGE_FARM=\\192.168.1.114\downloads\MusicLover\RustDesk\Buildwithconfig\test\dvforge\farm"

echo DVForge API: %DVFORGE_URL%
echo Shared farm: %DVFORGE_FARM%
echo Worker script: C:\DVForge\farm
echo If Explorer cannot open the NAS path, browse \\192.168.1.114\ and paste the real path.
echo.

cd /d "C:\DVForge\farm"
if not exist "worker.py" cd /d "%~dp0"

py -3 worker.py %*
if errorlevel 1 python worker.py %*
pause
