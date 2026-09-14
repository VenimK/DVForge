#!/usr/bin/env bash
# Uninstall-DVForge-Fedora.sh
# Uninstalls and cleans up the DVForge build environment on Fedora.
#
# Usage:
#   ./Uninstall-DVForge-Fedora.sh               # Removes local .toolchains, .venv,
#                                               # sccache, and DVForge CA compatibility link
#                                               # (safe default)
#   ./Uninstall-DVForge-Fedora.sh --all         # Removes everything including Rust 1.75
#                                               # and DNF packages
#   ./Uninstall-DVForge-Fedora.sh --toolchains-only
#   ./Uninstall-DVForge-Fedora.sh --venv-only
#   ./Uninstall-DVForge-Fedora.sh --purge-dnf   # Removes installed system DNF build dependencies
#
# Notes:
#   The TLS CA compatibility cleanup only removes:
#
#       /etc/pki/tls/certs/ca-bundle.crt
#
#   when it is a symlink pointing to Fedora's managed CA bundle:
#
#       /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem
#
#   A regular file or a symlink pointing somewhere else is left untouched.

set -e

# --- default configuration ---
REMOVE_TOOLCHAINS=true
REMOVE_VENV=true
REMOVE_SCCACHE=true
REMOVE_RUST_PIN=false
REMOVE_CA_COMPAT_LINK=true
PURGE_DNF=false
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
                      the Fedora TLS CA compatibility link, and prompt to purge
                      installed DNF packages.
  --toolchains-only   Only delete the local .toolchains/ folder.
  --venv-only         Only delete the local .venv/ folder.
  --purge-dnf         Remove the system DNF packages installed by the setup script.
  --remove-rust       Uninstall the pinned Rust 1.75 toolchain via rustup.
  -y, --yes           Non-interactive mode (auto-confirm dangerous actions like DNF purge).
  -h, --help          Show this help message.

Default behavior (no flags):
  Deletes the project-local .toolchains/ and .venv/ directories, uninstalls
  sccache, and removes the DVForge Fedora TLS CA compatibility link if present.
  Does NOT touch system DNF packages or global Rust toolchains.
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
            REMOVE_CA_COMPAT_LINK=true
            PURGE_DNF=true
            shift
            ;;
        --toolchains-only)
            REMOVE_TOOLCHAINS=true
            REMOVE_VENV=false
            REMOVE_SCCACHE=false
            REMOVE_RUST_PIN=false
            REMOVE_CA_COMPAT_LINK=false
            PURGE_DNF=false
            shift
            ;;
        --venv-only)
            REMOVE_TOOLCHAINS=false
            REMOVE_VENV=true
            REMOVE_SCCACHE=false
            REMOVE_RUST_PIN=false
            REMOVE_CA_COMPAT_LINK=false
            PURGE_DNF=false
            shift
            ;;
        --purge-dnf|--purge-apt)
            PURGE_DNF=true
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

# Ensure cargo and rust binaries are visible
export PATH="${HOME}/.cargo/bin:${PATH}"

# --- Remove .toolchains directory ---
if [ "$REMOVE_TOOLCHAINS" = true ]; then
    log "Cleaning up local toolchains"
    if [ -d "${TOOLCHAINS_DIR}" ]; then
        rm -rf "${TOOLCHAINS_DIR}"
        ok "Removed ${TOOLCHAINS_DIR}"
    else
        skip "No .toolchains directory found"
    fi
fi

# --- Remove .venv directory, git exclude entry, and apt-key shim ---
if [ "$REMOVE_VENV" = true ]; then
    log "Cleaning up Python virtual environment"
    if [ -d "${VENV_DIR}" ]; then
        rm -rf "${VENV_DIR}"
        ok "Removed ${VENV_DIR}"
    else
        skip "No .venv directory found"
    fi

    # Clean up entry from .git/info/exclude if present
    EXCLUDE_FILE="${PROJECT_DIR}/.git/info/exclude"
    if [ -f "${EXCLUDE_FILE}" ]; then
        sed -i '/^\.venv\/$/d' "${EXCLUDE_FILE}" 2>/dev/null || true
        ok "Cleaned .venv from .git/info/exclude"
    fi

    # Remove the apt-key compatibility shim if it was created
    if [ -f "/usr/local/bin/apt-key" ]; then
        if grep -q "appimage-builder-shim" "/usr/local/bin/apt-key" 2>/dev/null; then
            sudo rm -f "/usr/local/bin/apt-key"
            sudo rm -f "/etc/apt/trusted.gpg.d/appimage-builder-shim.gpg" 2>/dev/null || true
            ok "Removed /usr/local/bin/apt-key compatibility shim"
        fi
    fi
fi

# --- Remove Flutter/Dart TLS CA compatibility symlink ---
#
# This reverses the compatibility change made by Setup-DVForge-Fedora.sh.
#
# Only remove the link if it points to Fedora's managed CA bundle.
# Never remove a regular file or a symlink pointing somewhere else.

if [ "$REMOVE_CA_COMPAT_LINK" = true ]; then
    log "Checking TLS CA certificate compatibility link"

    CA_SOURCE="/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"
    CA_LINK="/etc/pki/tls/certs/ca-bundle.crt"

    if [ -L "${CA_LINK}" ]; then
        LINK_TARGET="$(readlink -f "${CA_LINK}" 2>/dev/null || true)"

        if [ "${LINK_TARGET}" = "${CA_SOURCE}" ]; then
            sudo rm -f "${CA_LINK}"
            ok "Removed Flutter/Dart TLS CA compatibility link"
        else
            skip "CA bundle symlink exists but does not point to Fedora's managed CA bundle"
        fi

    elif [ -e "${CA_LINK}" ]; then
        skip "${CA_LINK} is a regular file; leaving it unchanged"

    else
        skip "No TLS CA compatibility link found"
    fi
fi

# --- Remove sccache binary installed via cargo ---
if [ "$REMOVE_SCCACHE" = true ]; then
    log "Checking sccache"
    if have cargo && [ -f "${HOME}/.cargo/bin/sccache" ]; then
        cargo uninstall sccache 2>/dev/null || rm -f "${HOME}/.cargo/bin/sccache"
        ok "Uninstalled sccache from ~/.cargo/bin"
    else
        skip "sccache not found in ~/.cargo/bin"
    fi
fi

# --- Remove pinned Rust toolchain (optional) ---
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

# --- Purge DNF packages (optional / cautious) ---
if [ "$PURGE_DNF" = true ]; then
    log "System DNF packages cleanup"
    warn "Purging build packages may affect other software development tools on this system."

    CONFIRM=false
    if [ "$ASSUME_YES" = true ]; then
        CONFIRM=true
    else
        read -rp "Are you sure you want to remove the build dependency packages via dnf? [y/N] " response
        case "$response" in
            [yY][eE][sS]|[yY])
                CONFIRM=true
                ;;
            *)
                CONFIRM=false
                ;;
        esac
    fi

    if [ "$CONFIRM" = true ]; then
        sudo -v
        DEPS=(
            gcc gcc-c++ make git curl wget unzip zip tar
            pkgconf-pkg-config openssl-devel sqlite-devel clang-devel llvm-devel
            cmake ninja-build file
            rpm-build ImageMagick bsdtar
            glibc.i686 libstdc++.i686 zlib-ng.i686
            nasm yasm
            autoconf automake libtool
            pam-devel
            gtk3-devel libayatana-appindicator-gtk3-devel libxcb-devel libxdo-devel
            alsa-lib-devel pulseaudio-libs-devel gstreamer1-devel gstreamer1-plugins-base-devel
            libva-devel patchelf
            libffi-devel potrace
            fuse-libs
            ncurses-compat-libs.i686 ncurses-libs.i686
        )

        INSTALLED_TO_REMOVE=()
        for d in "${DEPS[@]}"; do
            if rpm -q "$d" >/dev/null 2>&1; then
                INSTALLED_TO_REMOVE+=("$d")
            fi
        done

        if [ ${#INSTALLED_TO_REMOVE[@]} -gt 0 ]; then
            sudo dnf remove -y "${INSTALLED_TO_REMOVE[@]}"
            ok "Removed DNF packages"
        else
            skip "None of the specified DNF packages were found installed"
        fi
    else
        skip "Skipping DNF purge"
    fi
fi

log ""
log "Uninstall complete!"