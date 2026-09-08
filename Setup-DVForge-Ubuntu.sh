#!/usr/bin/env bash
# Setup-DVForge-macOS.sh
# Sets up a native linux build environment for DVForge (linux/android).
#
# Idempotent — safe to re-run.
#
# Installs / verifies:
#   - installs build dependencies using apt
#   - optionally install appimage packaging dependencies using pip
#   - install toolchains into .toolchains folder and sets env.json file
#
#
# Usage:
#   ./Setup-DVForge-Ubuntu.sh --appimage          # Create Python venv and install packaging dependencies (appimage-builder).
#   ./Setup-DVForge-Ubuntu.sh --skip-toolchains   # Skip toolchain bootstrap (Rust, Java, Android SDK/NDK, Flutter, LLVM, vcpkg).

set -e

# --- default configuration ---
INSTALL_APPIMAGE_DEPS=false
INSTALL_TOOLCHAINS=true

# --- argument parsing ---
show_help() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  --appimage          Create Python venv and install packaging dependencies (appimage-builder).
  --skip-toolchains   Skip toolchain bootstrap (Rust, Java, Android SDK/NDK, Flutter, LLVM, vcpkg).
  -h, --help          Show this help message.

Default behavior (no flags):
  Installs system APT dependencies and bootstraps toolchains, but skips the Python venv/pip steps.
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --appimage)
            INSTALL_APPIMAGE_DEPS=true
            shift
            ;;
        --skip-toolchains)
            INSTALL_TOOLCHAINS=false
            shift
            ;;
        -h|--help)
            show_help
            ;;
        *)
            echo "Unknown option: $1"
            echo "Run with -h or --help for usage."
            exit 1
            ;;
    esac
done

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

# apt update + upgrade
log "Updating packages"
sudo apt-get update -y
sudo apt-get upgrade -y
ok "Packages updated"

# Install build dependencies
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
    libffi-dev potrace
)

# Optional fuse package name differs across Debian/Ubuntu releases (Ubuntu 24.04+ / 26.04 uses libfuse2t64)
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

# Sanity checks
if have rpmbuild; then ok "rpmbuild present"; else warn "rpmbuild missing after apt install"; fi
if have magick || have convert; then ok "ImageMagick present"; else warn "ImageMagick missing after apt install"; fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"

# Optional Virtual Environment & AppImage Packaging Setup
if [ "$INSTALL_APPIMAGE_DEPS" = true ]; then
    log "Setting up Python virtual environment & AppImage dependencies (--appimage enabled)"
    
    if [ ! -d "${VENV_DIR}" ]; then
        log "Creating virtual environment at ${VENV_DIR}"
        python3 -m venv "${VENV_DIR}"
    else
        log "Virtual environment already exists at ${VENV_DIR}."
    fi

    log "Installing Python build packages..."
    "${VENV_DIR}/bin/pip" install --upgrade pip setuptools wheel
    "${VENV_DIR}/bin/pip" install setuptools_scm
    "${VENV_DIR}/bin/pip" install "git+https://github.com/rustdesk-org/appimage-builder.git"

    # Prevent committing .venv if repo lacks .gitignore
    if [ -d "${PROJECT_DIR}/.git" ]; then
        EXCLUDE_FILE="${PROJECT_DIR}/.git/info/exclude"
        if ! grep -qs "^.venv/" "${EXCLUDE_FILE}" 2>/dev/null; then
            echo ".venv/" >> "${EXCLUDE_FILE}"
        fi
    fi
    ok "Virtual environment & AppImage tools configured"
else
    skip "Virtual environment and pip packages (--appimage omitted)"
fi

# Optional Toolchains Bootstrap
if [ "$INSTALL_TOOLCHAINS" = true ]; then
    log "Bootstrapping toolchains"

    # Determine python executable to run toolchains.py
    if [ "$INSTALL_APPIMAGE_DEPS" = true ] && [ -x "${VENV_DIR}/bin/python3" ]; then
        PY_EXEC="${VENV_DIR}/bin/python3"
    else
        PY_EXEC="python3"
    fi

    # Locate toolchains.py relative to script location
    TOOLCHAINS_PY=""
    for cand in "${PROJECT_DIR}/toolchains.py" "${PROJECT_DIR}/builder/toolchains.py"; do
        if [ -f "$cand" ]; then
            TOOLCHAINS_PY="$cand"
            break
        fi
    done

    # Ensure cargo and rust binaries are visible to Python and any subprocesses it spawns
    export PATH="${HOME}/.cargo/bin:${PATH}"

    if [ -n "${TOOLCHAINS_PY}" ]; then
        log "Running toolchains.py via ${PY_EXEC}"
        "$PY_EXEC" "${TOOLCHAINS_PY}" \
            rust java android_sdk android_ndk flutter llvm vcpkg sccache
        ok "Toolchains installed and env.json generated"
    else
        warn "Could not locate toolchains.py; skipping automated SDK downloads."
    fi

    # Fallback / Verification for sccache
    if ! command -v sccache >/dev/null 2>&1 && [ ! -f "${HOME}/.cargo/bin/sccache" ]; then
        log "Installing sccache 0.11.0 directly via cargo..."
        cargo install sccache --version 0.11.0 --locked
        ok "sccache installed"
    else
        ok "sccache is present"
    fi

    # Rust Toolchain Configuration
    if have rustup || [ -x "${HOME}/.cargo/bin/rustup" ]; then
        rustup toolchain install 1.75 --profile minimal || true
        rustup default 1.75 || true
        rustup component add rustfmt --toolchain 1.75 || true
        ok "Rust 1.75 toolchain and rustfmt configured"
    fi

    # Fix Android NDK execute permissions
    NDK_DIR="${PROJECT_DIR}/.toolchains/android_ndk"
    if [ -d "${NDK_DIR}" ]; then
        log "Fixing Android NDK binary permissions..."
        find "${NDK_DIR}" -type f -path "*/toolchains/llvm/prebuilt/*/bin/*" -exec chmod +x {} +
        find "${NDK_DIR}" -maxdepth 3 -type f \( -name "ndk-build" -o -name "*.sh" \) -exec chmod +x {} +
        ok "Android NDK permissions fixed"
    fi
else
    skip "Toolchains installation (--skip-toolchains passed)"
fi

log ""
log "Setup complete!"
if [ "$INSTALL_APPIMAGE_DEPS" = true ]; then
    log "To run the application with your venv:"
    log "  source .venv/bin/activate && ./run.sh"
else
    log "To run the application:"
    log "  ./run.sh"
fi