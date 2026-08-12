<#
.SYNOPSIS
  Uninstalls the DVForge WSL2 / Android / Linux build environment created by
  Setup-DVForge-WSL2.ps1.

.DESCRIPTION
  Default (recommended): removes DVForge-related tools inside the Debian distro
  but keeps WSL and the Debian distro itself:
    - ~/DVForge (repo, .toolchains, workspace builds)
    - ~/Android/Sdk
    - /opt/flutter, /opt/vcpkg, /opt/appimage-builder-venv
    - /usr/local/bin/appimage-builder
    - Rust/cargo/rustup for the WSL user (~/.cargo, ~/.rustup)
    - sccache cache (~/.cache/sccache)
    - Gradle / pub caches used by Android builds
    - DVForge env block in ~/.bashrc
    - Root leftovers under /root from early root installs

  Optional switches (more destructive):
    -RemoveDebian     Unregister the entire Debian WSL distro
    -RemoveWslConfig  Delete %USERPROFILE%\.wslconfig (12GB/8CPU settings)
    -PurgeAptPackages Attempt to remove apt packages the installer added
                      (may affect other projects; off by default)

  Idempotent -- safe to re-run. Missing pieces are skipped.

.PARAMETER Distro
  WSL distro name (default: Debian).

.PARAMETER Force
  Skip interactive confirmation prompts.

.PARAMETER RemoveDebian
  Unregister the WSL distro after cleaning (destroys all data in that distro).

.PARAMETER RemoveWslConfig
  Delete the user .wslconfig file.

.PARAMETER PurgeAptPackages
  Also apt-get purge packages commonly installed by the setup script.

.EXAMPLE
  # Clean toolchains only; keep Debian
  .\Uninstall-DVForge-WSL2.ps1

.EXAMPLE
  # Full wipe including Debian distro
  .\Uninstall-DVForge-WSL2.ps1 -RemoveDebian -RemoveWslConfig -Force
#>

#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$Distro = 'Debian',
    [switch]$Force,
    [switch]$RemoveDebian,
    [switch]$RemoveWslConfig,
    [switch]$PurgeAptPackages
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Auto-elevate to Administrator if not already elevated
# ---------------------------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host 'Requesting Administrator privileges (UAC) ...' -ForegroundColor Yellow
    $scriptPath = $MyInvocation.MyCommand.Path
    if (-not $scriptPath) { $scriptPath = $PSCommandPath }
    $argList = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $scriptPath,
        '-Distro', $Distro
    )
    if ($Force)            { $argList += '-Force' }
    if ($RemoveDebian)     { $argList += '-RemoveDebian' }
    if ($RemoveWslConfig)  { $argList += '-RemoveWslConfig' }
    if ($PurgeAptPackages) { $argList += '-PurgeAptPackages' }
    try {
        Start-Process -FilePath 'powershell.exe' -ArgumentList $argList `
            -Verb RunAs -Wait
    } catch {
        Write-Host '  [ERROR] UAC elevation was declined. Please re-run as Administrator.' -ForegroundColor Red
        exit 1
    }
    exit 0
}

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Skip($msg) { Write-Host "  [SKIP] $msg" -ForegroundColor Yellow }
function Write-Warn($msg) { Write-Host "  [WARN] $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "  [ERROR] $msg" -ForegroundColor Red }

function Confirm-Action {
    param([string]$Message)
    if ($Force) { return $true }
    $r = Read-Host "$Message [y/N]"
    return ($r -match '^[Yy](es)?$')
}

function Test-WslDistro {
    param([string]$Name)
    try {
        $raw = wsl --list --quiet 2>&1
        $clean = ($raw -join "`n") -replace "`0", ''
        return ($clean -match [regex]::Escape($Name))
    } catch {
        return $false
    }
}

# ---------------------------------------------------------------------------
# Plan / confirm
# ---------------------------------------------------------------------------
Write-Step 'DVForge WSL2 environment uninstaller'
Write-Host "  Distro:            $Distro"
Write-Host "  Remove toolchains: yes (default)"
Write-Host "  Remove Debian:     $(if ($RemoveDebian) { 'YES' } else { 'no' })"
Write-Host "  Remove .wslconfig: $(if ($RemoveWslConfig) { 'YES' } else { 'no' })"
Write-Host "  Purge apt pkgs:    $(if ($PurgeAptPackages) { 'YES' } else { 'no' })"
Write-Host ''

if ($RemoveDebian) {
    Write-Warn "This will DESTROY the entire '$Distro' WSL distro (all files, users, packages)."
}

if (-not (Confirm-Action 'Proceed with uninstall?')) {
    Write-Host 'Cancelled.'
    exit 0
}

# ---------------------------------------------------------------------------
# Phase 1 -- clean inside the distro (if present and not removing whole distro)
# ---------------------------------------------------------------------------
$distroExists = Test-WslDistro -Name $Distro

if ($distroExists -and -not $RemoveDebian) {
    Write-Step "Phase 1: Remove DVForge toolchains inside $Distro"

    $purgeFlag = if ($PurgeAptPackages) { '1' } else { '0' }

    # Single-quoted here-string = pure bash; inject purge flag afterwards.
    $bashScript = @'
set -e

log()  { echo -e "\n=== $1 ==="; }
ok()   { echo "  [OK] $1"; }
skip() { echo "  [SKIP] $1"; }
warn() { echo "  [WARN] $1"; }

PURGE_APT="__PURGE_APT__"

rm_path() {
    local p="$1"
    if [ -e "$p" ] || [ -L "$p" ]; then
        rm -rf "$p"
        ok "removed $p"
    else
        skip "not found: $p"
    fi
}

# Prefer root for /opt and /usr/local (script is launched as root via wsl -u root)
if [ "$(id -u)" -ne 0 ]; then
    warn "expected root; some paths may fail without sudo"
fi

log "Removing shared /opt toolchains"
rm_path /opt/flutter
rm_path /opt/vcpkg
rm_path /opt/appimage-builder-venv
rm_path /usr/local/bin/appimage-builder

log "Removing per-user DVForge / Android / Rust installs"
HOMES=()
if [ -d /home ]; then
    for d in /home/*; do
        [ -d "$d" ] && HOMES+=("$d")
    done
fi
HOMES+=("/root")

for H in "${HOMES[@]}"; do
    [ -d "$H" ] || continue
    echo "  · home: $H"
    rm_path "$H/DVForge"
    rm_path "$H/Android"
    rm_path "$H/.cargo"
    rm_path "$H/.rustup"
    rm_path "$H/.cache/sccache"
    # Large caches from Android / Flutter builds
    rm_path "$H/.gradle"
    rm_path "$H/.pub-cache"

    BASHRC="$H/.bashrc"
    if [ -f "$BASHRC" ]; then
        MARKER="# >>> DVForge Android build env >>>"
        END="# <<< DVForge Android build env <<<"
        if grep -q "$MARKER" "$BASHRC" 2>/dev/null; then
            # Escape for sed delimiters
            sed -i "/# >>> DVForge Android build env >>>/,/# <<< DVForge Android build env <<</d" "$BASHRC"
            ok "removed DVForge env block from $BASHRC"
        else
            skip "no DVForge env block in $BASHRC"
        fi
    fi
done

if [ "$PURGE_APT" = "1" ]; then
    log "Purging apt packages installed for DVForge/RustDesk builds"
    export DEBIAN_FRONTEND=noninteractive
    PKGS=(
        nasm yasm autoconf automake libtool libtool-bin
        libpam0g-dev rpm imagemagick libarchive-tools
        libfuse2t64 libfuse2
        libgtk-3-dev libayatana-appindicator3-dev libxcb-randr0-dev libxdo-dev
        libasound2-dev libpulse-dev libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
        libva-dev patchelf
        openjdk-21-jdk openjdk-17-jdk
        python3-venv
    )
    TO_PURGE=()
    for p in "${PKGS[@]}"; do
        if dpkg -s "$p" >/dev/null 2>&1; then
            TO_PURGE+=("$p")
        fi
    done
    if [ ${#TO_PURGE[@]} -gt 0 ]; then
        apt-get purge -y "${TO_PURGE[@]}" || warn "some packages could not be purged"
        apt-get autoremove -y || true
        ok "apt purge finished"
    else
        skip "none of the known packages are installed"
    fi
else
    skip "apt purge disabled (pass -PurgeAptPackages to enable)"
fi

log "Distro cleanup complete"
echo "  WSL distro was kept. Use -RemoveDebian on the Windows script to unregister it."
'@

    $bashScript = $bashScript.Replace('__PURGE_APT__', $purgeFlag)

    $tempSh = Join-Path $env:TEMP 'dvforge-wsl-uninstall.sh'
    $lf = $bashScript -replace "`r`n", "`n"
    Set-Content -Path $tempSh -Value $lf -Encoding ASCII -NoNewline

    $wslPath = ($tempSh -replace '\\', '/' -replace '^([A-Z]):', '/mnt/$1').ToLower()

    Write-Host "  Running cleanup inside $Distro as root ..."
    wsl -d $Distro -u root -- bash -c "sed 's/\r$//' '$wslPath' | bash"
    $exitCode = $LASTEXITCODE
    Remove-Item -Path $tempSh -Force -ErrorAction SilentlyContinue

    if ($exitCode -ne 0) {
        Write-Err "Cleanup inside $Distro failed (exit $exitCode). Check output above."
    } else {
        Write-Ok "Toolchain cleanup finished inside $Distro"
    }
}
elseif ($distroExists -and $RemoveDebian) {
    Write-Step 'Phase 1: Skipping in-distro cleanup (entire distro will be removed)'
    Write-Skip 'files inside the distro will be deleted with wsl --unregister'
}
else {
    Write-Step 'Phase 1: Distro cleanup'
    Write-Skip "$Distro is not installed (or not listed by wsl --list)"
}

# ---------------------------------------------------------------------------
# Phase 2 -- optional unregister Debian
# ---------------------------------------------------------------------------
if ($RemoveDebian) {
    Write-Step "Phase 2: Unregister WSL distro '$Distro'"
    if (-not (Test-WslDistro -Name $Distro)) {
        Write-Skip "$Distro is not installed"
    } elseif (-not (Confirm-Action "Really DELETE distro '$Distro' permanently?")) {
        Write-Skip 'Distro unregister cancelled'
    } else {
        Write-Host "  wsl --terminate $Distro ..."
        wsl --terminate $Distro 2>$null
        Write-Host "  wsl --unregister $Distro ..."
        wsl --unregister $Distro
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "$Distro unregistered"
        } else {
            Write-Err "Failed to unregister $Distro"
        }
    }
} else {
    Write-Step 'Phase 2: Distro unregister'
    Write-Skip 'kept distro (use -RemoveDebian to remove)'
}

# ---------------------------------------------------------------------------
# Phase 3 -- optional .wslconfig
# ---------------------------------------------------------------------------
Write-Step 'Phase 3: Windows .wslconfig'
$wslConfigPath = Join-Path $env:USERPROFILE '.wslconfig'
if ($RemoveWslConfig) {
    if (Test-Path $wslConfigPath) {
        if (Confirm-Action "Delete $wslConfigPath ?") {
            Remove-Item -Path $wslConfigPath -Force
            Write-Ok "deleted $wslConfigPath"
            Write-Host '  Restarting WSL to drop memory limits ...'
            wsl --shutdown 2>$null
            Start-Sleep -Seconds 2
            Write-Ok 'WSL shutdown complete'
        } else {
            Write-Skip '.wslconfig kept'
        }
    } else {
        Write-Skip '.wslconfig not present'
    }
} else {
    if (Test-Path $wslConfigPath) {
        Write-Skip ".wslconfig kept at $wslConfigPath (use -RemoveWslConfig to delete)"
    } else {
        Write-Skip '.wslconfig not present'
    }
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Step 'Uninstall finished'
Write-Host ''
Write-Host '  Removed when present:' -ForegroundColor White
Write-Host '    ~/DVForge, ~/Android, ~/.cargo, ~/.rustup, ~/.cache/sccache' -ForegroundColor White
Write-Host '    ~/.gradle, ~/.pub-cache' -ForegroundColor White
Write-Host '    /opt/flutter, /opt/vcpkg, /opt/appimage-builder-venv' -ForegroundColor White
Write-Host '    DVForge block in ~/.bashrc' -ForegroundColor White
if ($RemoveDebian)     { Write-Host "    WSL distro: $Distro" -ForegroundColor White }
if ($RemoveWslConfig)  { Write-Host '    %USERPROFILE%\.wslconfig' -ForegroundColor White }
if ($PurgeAptPackages) { Write-Host '    selected apt packages' -ForegroundColor White }
Write-Host ''
Write-Host '  To reinstall:' -ForegroundColor White
Write-Host '    powershell -NoProfile -ExecutionPolicy Bypass -File Setup-DVForge-WSL2.ps1' -ForegroundColor White
Write-Host ''
if (-not $RemoveDebian -and (Test-WslDistro -Name $Distro)) {
    Write-Host "  Distro still available:  wsl -d $Distro" -ForegroundColor Yellow
    Write-Host ''
}
