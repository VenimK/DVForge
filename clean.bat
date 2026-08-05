@echo off
REM RustDesk Local Builder — uninstaller / clean-slate reset (Windows)
REM
REM Usage:
REM   clean.bat           — remove build artifacts (safe reset)
REM   clean.bat --all     — also remove output artifacts and stray installers
REM   clean.bat --purge   — NUCLEAR: remove everything including .toolchains,
REM                         branding, configs, patches, web, screenshots
REM   clean.bat --purge-system
REM                        - also wipe system-wide build caches:
REM                         %%USERPROFILE%%\.cargo\registry, pub-cache, .gradle,
REM                         vcpkg buildtrees/install.  Does NOT uninstall
REM                         Rust/Flutter themselves.

setlocal enabledelayedexpansion
cd /d "%~dp0"

set ALL=false
set PURGE=false
set PURGE_SYSTEM=false
if /i "%1"=="--all" set ALL=true
if /i "%1"=="--purge" set PURGE=true
if /i "%1"=="--purge-system" (
  set PURGE=true
  set PURGE_SYSTEM=true
)

echo.
echo RustDesk Local Builder - Clean Reset
echo =====================================
echo.

REM --- workspace/rustdesk-src ---
if exist "workspace\rustdesk-src" (
  call :rmdir_with_log "workspace\rustdesk-src" "workspace\rustdesk-src"
) else (
  echo   . workspace\rustdesk-src not present
)

REM --- workspace\.checkout-tmp ---
if exist "workspace\.checkout-tmp" (
  call :rmdir_with_log "workspace\.checkout-tmp" "workspace\.checkout-tmp"
) else (
  echo   . workspace\.checkout-tmp not present
)

REM --- Python caches ---
for %%d in (__pycache__ builder\__pycache__) do (
  if exist "%%d" (
    rmdir /s /q "%%d" 2>nul
    echo   OK removed %%d
  ) else (
    echo   . %%d not present
  )
)

REM --- build logs ---
for %%f in (error*.txt buildingMSI.txt nugetmsi.txt *.log) do (
  if exist "%%f" (
    del /f /q "%%f" 2>nul
    echo   OK removed %%f
  )
)

REM --- --all or --purge: remove output ---
if "%ALL%"=="true" goto :remove_outputs
if "%PURGE%"=="true" goto :remove_outputs
goto :after_outputs

:remove_outputs
echo.
echo Removing output artifacts
echo --------------------------
if exist "workspace\output" (
  call :rmdir_with_log "workspace\output" "workspace\output"
) else (
  echo   . workspace\output not present
)
for %%e in (*.exe *.msi *.dmg *.deb *.rpm *.AppImage *.apk *.tar.gz *.zip) do (
  if exist "%%e" (
    echo   removing %%e ...
    del /f /q "%%e" 2>nul && echo   OK removed %%e
  )
)

:after_outputs

REM --- --purge: remove EVERYTHING ---
if not "%PURGE%"=="true" goto :done

echo.
echo PURGE: removing toolchains, branding, configs, patches, web
echo ============================================================
for %%d in (.toolchains workspace\branding configs patches web) do (
  call :rmdir_with_log "%%d" "%%d"
)

REM stray screenshots and text files
for %%f in (*.png *.txt) do (
  if exist "%%f" (
    if /i not "%%f"=="clean.bat" (
      echo   removing %%f ...
      del /f /q "%%f" 2>nul && echo   OK removed %%f
    )
  )
)

REM remove empty workspace dir
if exist "workspace" (
  dir /b /a "workspace\*" 2>nul | findstr "." >nul
  if errorlevel 1 (
    rmdir "workspace" 2>nul
    echo   OK removed empty workspace\
  )
)

:done
REM --- --purge-system: wipe system-wide build caches ---
if not "%PURGE_SYSTEM%"=="true" goto :final

echo.
echo PURGE-SYSTEM: wiping system-wide build caches
echo =================================================

REM Rust crate cache
if exist "%USERPROFILE%\.cargo\registry" (
  call :rmdir_with_log "%USERPROFILE%\.cargo\registry" ".cargo\registry"
) else (
  echo   . .cargo\registry not present
)
if exist "%USERPROFILE%\.cargo\git" (
  call :rmdir_with_log "%USERPROFILE%\.cargo\git" ".cargo\git"
) else (
  echo   . .cargo\git not present
)

REM Flutter/Dart package cache (Windows: %LOCALAPPDATA%\Pub\Cache)
if exist "%LOCALAPPDATA%\Pub\Cache" (
  call :rmdir_with_log "%LOCALAPPDATA%\Pub\Cache" "Pub\Cache"
) else if exist "%USERPROFILE%\.pub-cache" (
  call :rmdir_with_log "%USERPROFILE%\.pub-cache" ".pub-cache"
) else (
  echo   . pub-cache not present
)

REM Gradle cache
if exist "%USERPROFILE%\.gradle" (
  call :rmdir_with_log "%USERPROFILE%\.gradle" ".gradle"
) else (
  echo   . .gradle not present
)

REM vcpkg buildtrees / installed (if VCPKG_ROOT is set)
if defined VCPKG_ROOT (
  if exist "%VCPKG_ROOT%\buildtrees" (
    call :rmdir_with_log "%VCPKG_ROOT%\buildtrees" "VCPKG_ROOT\buildtrees"
  ) else (
    echo   . VCPKG_ROOT\buildtrees not present
  )
  if exist "%VCPKG_ROOT%\installed" (
    call :rmdir_with_log "%VCPKG_ROOT%\installed" "VCPKG_ROOT\installed"
  ) else (
    echo   . VCPKG_ROOT\installed not present
  )
  if exist "%VCPKG_ROOT%\packages" (
    call :rmdir_with_log "%VCPKG_ROOT%\packages" "VCPKG_ROOT\packages"
  ) else (
    echo   . VCPKG_ROOT\packages not present
  )
) else (
  echo   . VCPKG_ROOT not set
)

echo.
echo   Note: Rust toolchain and Flutter SDK were NOT removed.
echo   To fully uninstall them:
echo     rustup self uninstall
echo     rm -rf %%USERPROFILE%%\flutter  (or wherever Flutter is installed)

:final
echo.
if "%PURGE_SYSTEM%"=="true" (
  echo Done. Full purge + system caches wiped.
  echo   Toolchains, branding, configs, AND build caches are gone.
  echo   Next build will re-download everything from scratch.
) else if "%PURGE%"=="true" (
  echo Done. Full purge complete - back to bare source code.
  echo   You will need to re-download toolchains and reconfigure
  echo   branding/configs before the next build.
) else (
  echo Done. System is clean and ready for a fresh build.
  echo   Run run.bat to start a new build.
)
echo.
goto :end

REM === helper: remove a directory with a dot spinner ===
:rmdir_with_log
set _DIR=%~1
set _NAME=%~2
if not exist "%_DIR%" (
  echo   . %_NAME% not present
  goto :eof
)
echo   removing %_NAME% (this may take a moment) ...
attrib -r -s -h "%_DIR%\*.*" /s /d 2>nul
start /b "" rmdir /s /q "%_DIR%" 2>nul
for /l %%i in (1,1,600) do (
  if not exist "%_DIR%" goto :rmdir_done
  <nul set /p "=."
  ping -n 2 127.0.0.1 >nul
)
:rmdir_done
echo.
if exist "%_DIR%" (
  echo   ! FAILED to remove %_NAME%
) else (
  echo   OK removed %_NAME%
)
goto :eof

:end
endlocal
