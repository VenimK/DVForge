<#
.SYNOPSIS
  Sets up WSL2 + Debian with a full Android + Linux packaging build environment for DVForge.

.DESCRIPTION
  Phase 1 (Windows): Installs/verifies WSL2 with Debian, configures
    ~/.wslconfig for 12 GB RAM / 8 processors, restarts WSL to apply.
  Phase 2 (WSL): Installs build toolchains matching DVForge prereqs/toolchains pins:
    Rust 1.75, Flutter 3.24.5, Android SDK + NDK r28c (28.2.13676358),
    Temurin JDK 17 (.toolchains), vcpkg (pinned), LLVM 15.0.6 (.toolchains),
    sccache 0.11.0, ImageMagick 7.x, rpmbuild, appimage-builder — then clones DVForge into ~/DVForge.

  Idempotent -- safe to re-run. Existing installs are detected and skipped.

.NOTES
  The script auto-elevates to Administrator if needed (UAC prompt).
  Run as a normal WSL user (not root) so tools land under that home directory.
  After the script finishes, open http://127.0.0.1:8765 in your Windows browser.
  Companion uninstaller: Uninstall-DVForge-WSL2.ps1
#>

#Requires -Version 5.1

$ErrorActionPreference = 'Stop'
$Distro = 'Debian'

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
    $argList = '-NoProfile -ExecutionPolicy Bypass -File "' + $scriptPath + '"'
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

# ---------------------------------------------------------------------------
# Phase 1 -- Windows side
# ---------------------------------------------------------------------------

Write-Step 'Phase 1: WSL2 setup on Windows'

# 1a. Check if WSL is available
$wslInstalled = $false
try {
    $wslOutput = wsl --status 2>&1
    if ($LASTEXITCODE -eq 0) {
        $wslInstalled = $true
    }
} catch {
    # WSL not installed
}

if (-not $wslInstalled) {
    Write-Host "  WSL not found -- installing WSL2 + $Distro ..."
    Write-Host '  (This requires Administrator privileges.)'
    wsl --install -d $Distro
    if ($LASTEXITCODE -ne 0) {
        Write-Err 'WSL install failed. Please run this script as Administrator.'
        exit 1
    }
    Write-Host ''
    Write-Host '  WSL installed. If a reboot is required, reboot now and re-run this script.'
    Write-Host "  Set up your $Distro username/password when prompted, then re-run."
    exit 0
} else {
    Write-Skip 'WSL is already installed'
}

# 1b. Check if our distro is installed
# wsl --list --quiet outputs UTF-16LE with embedded null bytes, so we must
# strip them before PowerShell can match against the plain string.
$distroInstalled = $false
try {
    $rawList = wsl --list --quiet 2>&1
    $cleanList = ($rawList -join "`n") -replace "`0", ''
    if ($cleanList -match $Distro) {
        $distroInstalled = $true
    }
} catch {}

if (-not $distroInstalled) {
    Write-Host "  Installing $Distro ..."
    wsl --install -d $Distro --no-launch
    # Debian ships as a minimal install -- ensure sudo and apt are available
    wsl -d $Distro -- bash -c 'apt-get update -y && apt-get install -y sudo wget unzip'
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Failed to install $Distro"
        exit 1
    }
    Write-Ok "$Distro installed"
} else {
    Write-Skip "$Distro is already installed"
}

# 1c. Ensure WSL2 (not WSL1)
$version = 2
try {
    $verOutput = wsl --status 2>&1
    if ($verOutput -match 'Default Version:\s*(\d)') {
        $version = [int]$Matches[1]
    }
} catch {}

if ($version -ne 2) {
    Write-Host "  Upgrading $Distro to WSL2 ..."
    wsl --set-version $Distro 2
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Failed to set WSL2 for $Distro"
        exit 1
    }
    wsl --set-default-version 2 2>$null
    Write-Ok "$Distro is now on WSL2"
} else {
    Write-Skip "$Distro is already on WSL2"
}

# 1d. Configure .wslconfig (RAM + processors)
$wslConfigPath = Join-Path $env:USERPROFILE '.wslconfig'
$wslConfigContent = @"
[wsl2]
memory=12GB
processors=8
"@

$needWrite = $true
if (Test-Path $wslConfigPath) {
    $existing = Get-Content $wslConfigPath -Raw
    if ($existing -match 'memory=12GB' -and $existing -match 'processors=8') {
        $needWrite = $false
    }
}

if ($needWrite) {
    Write-Host "  Writing $wslConfigPath ..."
    Set-Content -Path $wslConfigPath -Value $wslConfigContent -Encoding UTF8
    Write-Ok '.wslconfig written (12 GB RAM, 8 processors)'
} else {
    Write-Skip '.wslconfig already configured'
}

# 1e. Restart WSL to apply config
Write-Host '  Restarting WSL to apply configuration ...'
wsl --shutdown
Start-Sleep -Seconds 3
Write-Ok 'WSL restarted'

# ---------------------------------------------------------------------------
# Phase 2 -- WSL side (everything inside the Linux distro)
# ---------------------------------------------------------------------------

Write-Step "Phase 2: Android build environment inside $Distro"

$bashScript = @'
set -e

# --- helpers ---
log()  { echo -e "\n=== $1 ==="; }
ok()   { echo "  [OK] $1"; }
skip() { echo "  [SKIP] $1"; }
warn() { echo "  [WARN] $1"; }
fail() { echo "  [ERROR] $1"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
dir_exists() { [ -d "$1" ]; }

# --- sudo keepalive (ask once) ---
sudo -v
trap 'sudo -k' EXIT

# 1. apt update + upgrade
log "Updating packages"
sudo apt-get update -y
sudo apt-get upgrade -y
ok "Packages updated"

# 2. Install build dependencies
# Pins / prereqs (builder/prereqs.py + builder/toolchains.py):
#   Java = Temurin JDK 17 via toolchains (not apt openjdk)
#   rpm/rpmbuild · imagemagick 7.x · appimage deps (libarchive, fuse)
# Prefer libfuse2t64, fall back to libfuse2.
log "Installing build dependencies"
DEPS=(
    build-essential git python3 python3-pip python3-venv curl wget unzip zip tar
    pkg-config libssl-dev libsqlite3-dev libclang-dev
    cmake ninja-build file
    lib32z1 lib32ncurses6 lib32stdc++6
    rpm imagemagick libarchive-tools
    # RustDesk linux vcpkg + desktop packaging deps
    nasm yasm
    autoconf automake libtool libtool-bin
    libpam0g-dev
    libgtk-3-dev libayatana-appindicator3-dev libxcb-randr0-dev libxdo-dev
    libasound2-dev libpulse-dev libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
    libva-dev patchelf
)
# Optional fuse package name differs across Debian releases
for fuse_pkg in libfuse2t64 libfuse2; do
    if apt-cache show "$fuse_pkg" >/dev/null 2>&1; then
        DEPS+=("$fuse_pkg")
        break
    fi
done
NEEDED=()
for d in "${DEPS[@]}"; do
    if ! dpkg -s "$d" >/dev/null 2>&1; then
        NEEDED+=("$d")
    fi
done
if [ ${#NEEDED[@]} -gt 0 ]; then
    sudo apt-get install -y "${NEEDED[@]}"
    ok "Build dependencies installed"
else
    skip "All build dependencies already installed"
fi
# Sanity: packaging tools from prereqs
if have rpmbuild; then ok "rpmbuild present"; else warn "rpmbuild missing after apt install"; fi
if have magick || have convert; then ok "ImageMagick present"; else warn "ImageMagick missing after apt install"; fi

# 3. Install Rust 1.75
log "Installing Rust 1.75"
if have rustc && rustc --version | grep -q "1.75"; then
    skip "Rust 1.75 already installed"
else
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain 1.75 --profile minimal
    source "$HOME/.cargo/env"
    rustup component add rustfmt
    ok "Rust 1.75 installed"
fi
[ -f "$HOME/.cargo/env" ] && source "$HOME/.cargo/env"

# 4. Install Flutter 3.24.5
log "Installing Flutter 3.24.5"
FLUTTER_DIR="/opt/flutter"
if dir_exists "$FLUTTER_DIR" && [ -x "$FLUTTER_DIR/bin/flutter" ]; then
    # Fix ownership if root-owned from a previous sudo install
    sudo chown -R "$(whoami):$(whoami)" "$FLUTTER_DIR" 2>/dev/null || true
    skip "Flutter already installed at $FLUTTER_DIR"
else
    FLUTTER_URL="https://storage.googleapis.com/flutter_infra_release/releases/stable/linux/flutter_linux_3.24.5-stable.tar.xz"
    sudo mkdir -p /opt
    sudo chown "$(whoami):$(whoami)" /opt
    wget -q "$FLUTTER_URL" -O /tmp/flutter.tar.xz
    tar xf /tmp/flutter.tar.xz -C /opt
    rm -f /tmp/flutter.tar.xz
    ok "Flutter 3.24.5 installed at $FLUTTER_DIR"
fi

# 5. Install Android SDK + NDK r28c
log "Installing Android SDK + NDK r28c"
ANDROID_SDK="$HOME/Android/Sdk"
NDK_DIR="$ANDROID_SDK/ndk/r28c"

if dir_exists "$NDK_DIR"; then
    skip "Android NDK r28c already installed"
else
    CMDLINE_DIR="$ANDROID_SDK/cmdline-tools"
    if [ ! -d "$CMDLINE_DIR/latest" ]; then
        mkdir -p "$CMDLINE_DIR"
        cd "$CMDLINE_DIR"
        wget -q "https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip" -O cmdline-tools.zip
        unzip -q cmdline-tools.zip
        mv cmdline-tools latest
        rm cmdline-tools.zip
        ok "Android command-line tools installed"
    else
        skip "Android command-line tools already present"
    fi

    export ANDROID_SDK_ROOT="$ANDROID_SDK"
    export ANDROID_HOME="$ANDROID_SDK"
    SDKMANAGER="$CMDLINE_DIR/latest/bin/sdkmanager"

    # sdkmanager uses versioned package IDs, not marketing names like "r28c".
    # NDK r28c == 28.2.13676358
    NDK_PKG="ndk;28.2.13676358"
    NDK_VER="28.2.13676358"

    # Pre-accept licenses so sdkmanager is non-interactive
    LICENSES_DIR="$ANDROID_SDK/licenses"
    mkdir -p "$LICENSES_DIR"
    echo -e "\n8933bad161af4178b1185d1a37fbf41ea5269c55\n\nd56f5187479451eabf01fb78af6dfcb131a6481e\n24333f8a63b6825ea9c5514f83c2829b004d1fee\n" > "$LICENSES_DIR/android-sdk-license"
    echo -e "\n84831b9409646a918e30573bab4c9c91346d8abd\n" > "$LICENSES_DIR/android-sdk-preview-license"
    echo -e "\nd975f751698a77b662f1254ddbeed3901e976f5a\n" > "$LICENSES_DIR/intel-android-extra-license"
    echo -e "\n33b6a2b64607f11b759f320ef9dff4ae5c47d97a\n" > "$LICENSES_DIR/google-gdk-license"
    yes | "$SDKMANAGER" --licenses >/dev/null 2>&1 || true

    # Keep stdout visible; only tolerate license-pipe SIGPIPE from `yes`
    set +e
    yes | "$SDKMANAGER" "platform-tools" "platforms;android-34" "build-tools;34.0.0"
    yes | "$SDKMANAGER" "$NDK_PKG"
    set -e

    # Alias path used by env vars / DVForge
    mkdir -p "$ANDROID_SDK/ndk"
    if [ -d "$ANDROID_SDK/ndk/$NDK_VER" ]; then
        ln -sfn "$NDK_VER" "$NDK_DIR"
    fi
    if [ ! -d "$ANDROID_SDK/platforms/android-34" ]; then
        fail "Android platform android-34 is missing after sdkmanager"
    fi
    if [ ! -d "$NDK_DIR" ]; then
        fail "NDK install completed but $NDK_DIR is missing (expected $NDK_PKG)"
    fi

    ok "Android SDK + NDK r28c installed ($NDK_VER)"
fi

# 6. Install vcpkg at pinned commit
log "Installing vcpkg (pinned commit 120deac)"
VCPKG_DIR="/opt/vcpkg"
VCPKG_COMMIT="120deac3062162151622ca4860575a33844ba10b"

# Ensure /opt exists and is writable by the current user
sudo mkdir -p /opt
sudo chown "$(whoami):$(whoami)" /opt

if dir_exists "$VCPKG_DIR" && [ -d "$VCPKG_DIR/.git" ]; then
    # Fix ownership if it was root-owned from a previous sudo clone
    sudo chown -R "$(whoami):$(whoami)" "$VCPKG_DIR"
    git config --global --add safe.directory "$VCPKG_DIR"
    CURRENT=$(git -C "$VCPKG_DIR" rev-parse HEAD 2>/dev/null || echo "")
    if [ "$CURRENT" = "$VCPKG_COMMIT" ]; then
        skip "vcpkg already at pinned commit"
    else
        git -C "$VCPKG_DIR" fetch --depth 1 origin "$VCPKG_COMMIT"
        git -C "$VCPKG_DIR" checkout "$VCPKG_COMMIT"
        ok "vcpkg updated to pinned commit"
    fi
else
    git clone https://github.com/microsoft/vcpkg.git "$VCPKG_DIR"
    git -C "$VCPKG_DIR" checkout "$VCPKG_COMMIT"
    bash "$VCPKG_DIR/bootstrap-vcpkg.sh" -disableMetrics
    ok "vcpkg installed at $VCPKG_DIR"
fi

# 7. Clone DVForge
log "Cloning DVForge"
DVFORGE_DIR="$HOME/DVForge"
if dir_exists "$DVFORGE_DIR" && [ -d "$DVFORGE_DIR/.git" ]; then
    skip "DVForge already cloned at $DVFORGE_DIR"
    echo "  (Run 'cd ~/DVForge && git pull' to update)"
else
    git clone https://github.com/VenimK/DVForge.git "$DVFORGE_DIR"
    ok "DVForge cloned to $DVFORGE_DIR"
fi

# 7b. Install DVForge-managed toolchains (pinned in builder/toolchains.py)
#   java       → Temurin JDK 17 into .toolchains/java  (Adoptium)
#   llvm 15.0.6  →  ~/DVForge/.toolchains/llvm  (portable tarball)
#   sccache 0.11.0 → ~/.cargo/bin via cargo install --version 0.11.0 --locked
#   imagemagick  → system apt (already installed above; installer detects it)
log "Installing DVForge toolchains (java/Temurin 17, llvm 15.0.6, sccache 0.11.0, imagemagick)"
[ -f "$HOME/.cargo/env" ] && source "$HOME/.cargo/env"
export PATH="/opt/flutter/bin:$HOME/.cargo/bin:$VCPKG_DIR:$PATH"
export VCPKG_ROOT="$VCPKG_DIR"
export ANDROID_SDK_ROOT="$ANDROID_SDK"
export ANDROID_HOME="$ANDROID_SDK"
(
  cd "$DVFORGE_DIR" || exit 1
  python3 - <<'PY'
import os
import builder.toolchains as tc
root = os.getcwd()  # ~/DVForge
r = tc.install_many(["java", "llvm", "sccache", "imagemagick"], root, print)
if r.get("errors"):
    raise SystemExit(f"toolchain install errors: {r['errors']}")
print("TOOLCHAINS_OK", r.get("installed"))
PY
) || fail "DVForge toolchains install failed (java/llvm/sccache/imagemagick)"
ok "DVForge toolchains installed (Temurin 17 / llvm / sccache / imagemagick)"

# 7c. appimage-builder (prereqs hint — not in TOOLS registry)
#   sudo apt install libarchive-tools libfuse2
#   pip install setuptools_scm<10 + git+https://github.com/rustdesk-org/appimage-builder.git
log "Installing appimage-builder (rustdesk-org fork)"
APPIMAGE_VENV="/opt/appimage-builder-venv"
if [ -x /usr/local/bin/appimage-builder ] || [ -x "$APPIMAGE_VENV/bin/appimage-builder" ]; then
    skip "appimage-builder already installed"
else
    sudo python3 -m venv "$APPIMAGE_VENV"
    sudo "$APPIMAGE_VENV/bin/pip" install -U pip wheel
    sudo "$APPIMAGE_VENV/bin/pip" install "setuptools_scm<10"
    sudo "$APPIMAGE_VENV/bin/pip" install "git+https://github.com/rustdesk-org/appimage-builder.git"
    sudo tee /usr/local/bin/appimage-builder >/dev/null <<'WRAP'
#!/bin/sh
exec /opt/appimage-builder-venv/bin/appimage-builder "$@"
WRAP
    sudo chmod +x /usr/local/bin/appimage-builder
    # Allow the current user to upgrade the venv later
    sudo chown -R "$(whoami):$(whoami)" "$APPIMAGE_VENV" 2>/dev/null || true
    ok "appimage-builder installed"
fi
have appimage-builder && ok "appimage-builder on PATH" || warn "appimage-builder not on PATH"

# Locate portable LLVM home (nested clang+llvm-15.0.6-... dir)
LLVM_HOME=""
if [ -d "$DVFORGE_DIR/.toolchains/llvm" ]; then
    if [ -x "$DVFORGE_DIR/.toolchains/llvm/bin/clang" ]; then
        LLVM_HOME="$DVFORGE_DIR/.toolchains/llvm"
    else
        for d in "$DVFORGE_DIR/.toolchains/llvm"/*; do
            if [ -x "$d/bin/clang" ]; then
                LLVM_HOME="$d"
                break
            fi
        done
    fi
fi
LIBCLANG_PATH=""
if [ -n "$LLVM_HOME" ]; then
    if [ -d "$LLVM_HOME/lib" ]; then
        LIBCLANG_PATH="$LLVM_HOME/lib"
    fi
fi
SCCACHE_BIN="$HOME/.cargo/bin/sccache"
# Absolute path — apply_persisted_env() abspath-resolves bare "sccache" to $ROOT/sccache
if [ ! -x "$SCCACHE_BIN" ]; then
    warn "sccache binary not found at $SCCACHE_BIN"
fi

# Locate Temurin JDK 17 home under .toolchains/java
JAVA_HOME_PATH=""
if [ -d "$DVFORGE_DIR/.toolchains/java" ]; then
    if [ -x "$DVFORGE_DIR/.toolchains/java/bin/java" ]; then
        JAVA_HOME_PATH="$DVFORGE_DIR/.toolchains/java"
    else
        for d in "$DVFORGE_DIR/.toolchains/java"/*; do
            if [ -x "$d/bin/java" ]; then
                JAVA_HOME_PATH="$d"
                break
            fi
            # macOS-style Contents/Home (unlikely on Linux, but cheap to check)
            if [ -x "$d/Contents/Home/bin/java" ]; then
                JAVA_HOME_PATH="$d/Contents/Home"
                break
            fi
        done
    fi
fi
if [ -z "$JAVA_HOME_PATH" ] || [ ! -x "$JAVA_HOME_PATH/bin/java" ]; then
    fail "Temurin JDK 17 not found under $DVFORGE_DIR/.toolchains/java"
fi
ok "Temurin JAVA_HOME=$JAVA_HOME_PATH ($("$JAVA_HOME_PATH/bin/java" -version 2>&1 | head -1))"

# 8. Write environment variables to ~/.bashrc
log "Configuring environment variables in ~/.bashrc"

BASHRC_MARKER="# >>> DVForge Android build env >>>"
BASHRC_END="# <<< DVForge Android build env <<<"

if grep -q "$BASHRC_MARKER" "$HOME/.bashrc" 2>/dev/null; then
    sed -i "/$BASHRC_MARKER/,/$BASHRC_END/d" "$HOME/.bashrc"
fi

LLVM_BIN_EXPORT=""
if [ -n "$LLVM_HOME" ]; then
    LLVM_BIN_EXPORT="$LLVM_HOME/bin:"
fi

cat >> "$HOME/.bashrc" << ENVEOF
$BASHRC_MARKER
export ANDROID_SDK_ROOT="$ANDROID_SDK"
export ANDROID_HOME="$ANDROID_SDK"
export ANDROID_NDK_HOME="$ANDROID_SDK/ndk/r28c"
export ANDROID_NDK_ROOT="$ANDROID_SDK/ndk/r28c"
export JAVA_HOME="$JAVA_HOME_PATH"
export VCPKG_ROOT="$VCPKG_DIR"
export LIBCLANG_PATH="$LIBCLANG_PATH"
export RUSTC_WRAPPER="$SCCACHE_BIN"
export PATH="${LLVM_BIN_EXPORT}\$JAVA_HOME/bin:/opt/flutter/bin:\$HOME/.cargo/bin:\$VCPKG_DIR:/usr/local/bin:\$PATH"
$BASHRC_END
ENVEOF

ok "Environment variables written to ~/.bashrc"

# 8b. Create/merge .toolchains/env.json so DVForge's apply_persisted_env() finds tools
log "Writing .toolchains/env.json for DVForge"
mkdir -p "$DVFORGE_DIR/.toolchains"
NDK_PATH="$ANDROID_SDK/ndk/r28c"
CARGO_BIN="$HOME/.cargo/bin"

# Merge with anything install_many already wrote (LIBCLANG_PATH, etc.).
# RUSTC_WRAPPER must be absolute — apply_persisted_env() abspath-resolves bare "sccache".
python3 - <<PY
import json, os
p = os.path.join("$DVFORGE_DIR", ".toolchains", "env.json")
try:
    with open(p) as f:
        d = json.load(f)
except Exception:
    d = {}
d.setdefault("vars", {})
d.setdefault("path", [])
vars_update = {
    "JAVA_HOME": "$JAVA_HOME_PATH",
    "VCPKG_ROOT": "$VCPKG_DIR",
    "ANDROID_SDK_ROOT": "$ANDROID_SDK",
    "ANDROID_HOME": "$ANDROID_SDK",
    "ANDROID_NDK_HOME": "$NDK_PATH",
    "ANDROID_NDK_ROOT": "$NDK_PATH",
    "RUSTC_WRAPPER": "$SCCACHE_BIN",
}
libclang = "$LIBCLANG_PATH"
if libclang:
    vars_update["LIBCLANG_PATH"] = libclang
d["vars"].update(vars_update)
path_candidates = []
llvm_home = "$LLVM_HOME"
if llvm_home:
    path_candidates.append(os.path.join(llvm_home, "bin"))
path_candidates += [
    "/opt/flutter/bin",
    "$VCPKG_DIR",
    "$JAVA_HOME_PATH/bin",
    "$CARGO_BIN",
    "/usr/local/bin",
]
for pth in path_candidates:
    if not pth or not os.path.isdir(pth):
        continue
    if pth in d["path"]:
        continue
    if "llvm" in pth:
        d["path"].insert(0, pth)
    else:
        d["path"].append(pth)
seen, paths = set(), []
for x in d["path"]:
    if x and x not in seen:
        seen.add(x)
        paths.append(x)
d["path"] = paths
with open(p, "w") as f:
    json.dump(d, f, indent=2)
    f.write("\n")
print(p)
PY

ok ".toolchains/env.json written"

# 9. Summary
log "Setup complete!"
echo ""
echo "  DVForge is at: ~/DVForge"
echo "  Flutter:          /opt/flutter"
echo "  Android SDK:      $ANDROID_SDK"
echo "  NDK r28c:         $ANDROID_SDK/ndk/r28c"
echo "  Temurin JDK 17:   $JAVA_HOME_PATH"
echo "  vcpkg:            $VCPKG_DIR"
echo "  Rust 1.75:        ~/.cargo"
echo "  LLVM 15.0.6:      ${LLVM_HOME:-.toolchains/llvm}"
echo "  sccache 0.11.0:   $SCCACHE_BIN"
echo "  ImageMagick:      $(command -v magick || command -v convert || 'apt')"
echo "  rpmbuild:         $(command -v rpmbuild || 'apt')"
echo "  appimage-builder: $(command -v appimage-builder || 'missing')"
echo ""
echo "  To start building:"
echo "    cd ~/DVForge"
echo "    python3 app.py --no-browser"
echo ""
echo "  Then open http://127.0.0.1:8765 in your Windows browser."
echo "  The capability board should show Android + Linux packaging tools as ready."
echo ""
'@

# Write the bash script to a temp file, then execute it inside WSL.
# This avoids shell quoting issues when passing a multi-line string with
# parentheses through PowerShell -> WSL -> bash -c.
Write-Host "  Running setup inside $Distro ..."
Write-Host "  (You may be prompted for your $Distro sudo password.)"
Write-Host ''

$tempSh = Join-Path $env:TEMP 'dvforge-wsl-setup.sh'
# Write with LF-only line endings (bash chokes on CRLF)
$lfContent = $bashScript -replace "`r`n", "`n"
Set-Content -Path $tempSh -Value $lfContent -Encoding ASCII -NoNewline

# Convert the Windows temp path to a WSL path (C:\Users\...\Temp -> /mnt/c/Users/.../Temp)
$wslPath = ($tempSh -replace '\\', '/' -replace '^([A-Z]):', '/mnt/$1').ToLower()

# Run with sed fallback to strip any stray \r, then pipe to bash
wsl -d $Distro -- bash -c "sed 's/\r$//' '$wslPath' | bash"
$wslExit = $LASTEXITCODE

Remove-Item -Path $tempSh -Force -ErrorAction SilentlyContinue

if ($wslExit -ne 0) {
    Write-Err 'WSL setup failed. Check the output above for details.'
    exit 1
}

Write-Step 'All done!'
Write-Host ''
Write-Host '  Next steps:' -ForegroundColor White
Write-Host "  1. Open a WSL terminal:  wsl -d $Distro" -ForegroundColor White
Write-Host '  2. Start DVForge:        cd ~/DVForge && python3 app.py --no-browser' -ForegroundColor White
Write-Host '  3. Open in browser:      http://127.0.0.1:8765' -ForegroundColor White
Write-Host ''
Write-Host '  The capability board should show Android targets as buildable.' -ForegroundColor White
