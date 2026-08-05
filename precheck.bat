@echo off
REM Run precheck.py on Windows from the project root.
cd /d "%~dp0"
python3 builder\precheck.py %*
if %errorlevel% neq 0 (
    echo.
    echo Some tools are missing. See hints above.
)
