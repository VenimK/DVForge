@echo off
REM RustDesk Local Builder - launcher (Windows)
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel%==0 (
  python app.py %*
  goto :eof
)
where py >nul 2>nul
if %errorlevel%==0 (
  py app.py %*
  goto :eof
)
echo Python 3 not found. Install it from https://www.python.org/downloads/ and check "Add to PATH".
pause
