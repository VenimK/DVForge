#!/usr/bin/env bash
# Uninstall-DVForge-Ubuntu.sh
# Uninstalls and cleans up the DVForge build environment on Ubuntu.
#
# Usage:
#   ./Uninstall-DVForge-Ubuntu.sh               # Removes local .toolchains, .venv, and sccache (safe default)
#   ./Uninstall-DVForge-Ubuntu.sh --all         # Removes everything including Rust 1.75 and apt packages
#   ./Uninstall-DVForge-Ubuntu.sh --toolchains-only
#   ./Uninstall-DVForge-Ubuntu.sh --venv-only
#   ./Uninstall-DVForge-Ubuntu.sh --purge-apt   # Removes installed system apt build dependencies

set -e

# --- default configuration ---
REMOVE_TOOLCHAINS=true
REMOVE_VENV=true
REMOVE_SCCACHE=true
REMOVE_RUST_PIN=false
PURGE_APT=false
ASSUME_YES=false

# --- helpers ---
log()  { echo -e "\n=== $1 ==="; }
ok()   { echo "  [OK] $1"; }
skip() { echo "  [SKIP] $1"; }
warn() { echo "  [WARN] $1"; }
fail() { echo "  [ERROR] $1"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# --- argument parsing ---
show_help() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  --all               Remove all project artifacts, the pinned Rust 1.75 toolchain,
                      and prompt to purge installed APT packages.
  --toolchains-only   Only delete the local .toolchains/ folder.
  --venv-only         Only delete the local .venv/ folder.
  --purge-apt         Remove the system APT packages installed by the setup script.
  --remove-rust       Uninstall the pinned Rust 1.75 toolchain via rustup.
  -y, --yes           Non-interactive mode (auto-confirm dangerous actions like APT purge).
  -h, --help          Show this help message.

Default behavior (no flags):
  Deletes the project-local .toolchains/ and .venv/ directories and uninstalls sccache.
  Does NOT touch system APT packages or global Rust toolchains.
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --all)
            REMOVE_TOOLCHAINS=true
            REMOVE_VENV=true
            REMOVE_SCCACHE=true
            REMOVE_RUST_PIN=true
            PURGE_APT=true
            shift
            ;;
        --toolchains-only)
            REMOVE_TOOLCHAINS=true
            REMOVE_VENV=false
            REMOVE_SCCACHE=false
            REMOVE_RUST_PIN=false
            PURGE_APT=false
            shift
            ;;
        --venv-only)
            REMOVE_TOOLCHAINS=false
            REMOVE_VENV=true
            REMOVE_SCCACHE=false
            REMOVE_RUST_PIN=false
            PURGE_APT=false
            shift
            ;;
        --purge-apt)
            PURGE_APT=true
            shift
            ;;
        --remove-rust)
            REMOVE_RUST_PIN=true
            shift
            ;;
        -y|--yes)
            ASSUME_YES=true
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

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"
TOOLCHAINS_DIR="${PROJECT_DIR}/.toolchains"
export PATH="${HOME}/.cargo/bin:${PATH}"

# remove .toolchains directory
if [ "$REMOVE_TOOLCHAINS" = true ]; then
    log "Cleaning up local toolchains"
    if [ -d "${TOOLCHAINS_DIR}" ]; then
        rm -rf "${TOOLCHAINS_DIR}"
        ok "Removed ${TOOLCHAINS_DIR}"
    else
        skip "No .toolchains directory found"
    fi
fi

# remove .venv directory and git exclude entry
if [ "$REMOVE_VENV" = true ]; then
    log "Cleaning up Python virtual environment"
    if [ -d "${VENV_DIR}" ]; then
        rm -rf "${VENV_DIR}"
        ok "Removed ${VENV_DIR}"
    else
        skip "No .venv directory found"
    fi

    # clean up entry from .git/info/exclude if present
    EXCLUDE_FILE="${PROJECT_DIR}/.git/info/exclude"
    if [ -f "${EXCLUDE_FILE}" ]; then
        sed -i '/^\.venv\/$/d' "${EXCLUDE_FILE}" 2>/dev/null || true
        ok "Cleaned .venv from .git/info/exclude"
    fi
fi

# remove sccache binary installed via cargo
if [ "$REMOVE_SCCACHE" = true ]; then
    log "Checking sccache"
    if have cargo && [ -f "${HOME}/.cargo/bin/sccache" ]; then
        cargo uninstall sccache 2>/dev/null || rm -f "${HOME}/.cargo/bin/sccache"
        ok "Uninstalled sccache from ~/.cargo/bin"
    else
        skip "sccache not found in ~/.cargo/bin"
    fi
fi

# remove pinned Rust toolchain (optional)
if [ "$REMOVE_RUST_PIN" = true ]; then
    log "Checking Rust toolchain"
    if have rustup; then
        if rustup toolchain list | grep -q "1.75"; then
            rustup toolchain uninstall 1.75
            ok "Uninstalled Rust 1.75 toolchain"
        else
            skip "Rust 1.75 toolchain not present"
        fi
    else
        skip "rustup not installed"
    fi
fi

# purge APT packages (optional / cautious)
if [ "$PURGE_APT" = true ]; then
    log "System APT packages cleanup"
    warn "Purging build packages may affect other software development tools on this system."
    
    CONFIRM=false
    if [ "$ASSUME_YES" = true ]; then
        CONFIRM=true
    else
        read -rp "Are you sure you want to remove the build dependency packages via apt? [y/N] " response
        case "$response" in
            [yY][eE][sS]|[yY]) CONFIRM=true ;;
            *) CONFIRM=false ;;
        esac
    fi

    if [ "$CONFIRM" = true ]; then
        sudo -v
        DEPS=(
            build-essential git python3-pip python3-venv curl wget unzip zip tar
            pkg-config libssl-dev libsqlite3-dev libclang-dev
            cmake ninja-build file
            lib32z1 lib32ncurses6 lib32stdc++6
            rpm imagemagick libarchive-tools
            nasm yasm
            autoconf automake libtool libtool-bin
            libpam0g-dev
            libgtk-3-dev libayatana-appindicator3-dev libxcb-randr0-dev libxdo-dev
            libasound2-dev libpulse-dev libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
            libva-dev patchelf
            libffi-dev potrace
        )

        for fuse_pkg in libfuse2t64 libfuse2; do
            if dpkg -s "$fuse_pkg" >/dev/null 2>&1; then
                DEPS+=("$fuse_pkg")
            fi
        done

        INSTALLED_TO_REMOVE=()
        for d in "${DEPS[@]}"; do
            if dpkg -s "$d" >/dev/null 2>&1; then
                INSTALLED_TO_REMOVE+=("$d")
            fi
        done

        if [ ${#INSTALLED_TO_REMOVE[@]} -gt 0 ]; then
            sudo apt-get remove --auto-remove -y "${INSTALLED_TO_REMOVE[@]}"
            ok "Removed APT packages"
        else
            skip "None of the specified APT packages were found installed"
        fi
    else
        skip "Skipping APT purge"
    fi
fi

log ""
log "Uninstall complete!"