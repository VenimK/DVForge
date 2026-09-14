#!/usr/bin/env bash
# Setup-DVForge-Fedora.sh
# Sets up a native Linux build environment for DVForge on Fedora (Linux/Android).
#
# Idempotent — safe to re-run.
#
# Installs / verifies:
#   - Installs build dependencies using DNF
#   - Configures Fedora TLS CA bundle compatibility for older Flutter/Dart tooling
#   - Optionally installs AppImage packaging dependencies using pip
#   - Installs toolchains into .toolchains folder and sets env.json file
#
# Usage:
#   ./Setup-DVForge-Fedora.sh --appimage         # Create Python venv and install packaging dependencies (appimage-builder).
#   ./Setup-DVForge-Fedora.sh --skip-toolchains  # Skip toolchain bootstrap (Rust, Java, Android SDK/NDK, Flutter, LLVM, vcpkg).

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
  Installs system DNF dependencies and bootstraps toolchains, but skips the Python venv/pip steps.
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

# dnf update
log "Updating system packages"
sudo dnf check-update -y || true
sudo dnf upgrade -y
ok "Packages updated"

# Install build dependencies
log "Installing build dependencies"
DEPS=(
    gcc gcc-c++ make git python3 python3-pip python3-devel curl wget unzip zip tar
    pkgconf-pkg-config openssl-devel sqlite-devel clang-devel llvm-devel
    cmake ninja-build file
    rpm-build ImageMagick bsdtar
    libzstd-devel
    # Multi-arch / 32-bit compatibility libraries (for Android SDK tools & builds)
    glibc.i686 libstdc++.i686 zlib-ng.i686
    # RustDesk Linux vcpkg + desktop packaging deps
    nasm yasm
    autoconf automake libtool
    pam-devel
    gtk3-devel libayatana-appindicator-gtk3-devel libxcb-devel libxdo-devel
    alsa-lib-devel pulseaudio-libs-devel gstreamer1-devel gstreamer1-plugins-base-devel
    libva-devel patchelf
    libffi-devel potrace
    fuse-libs
)

# Optional 32-bit ncurses compat library
for ncurses_pkg in ncurses-compat-libs.i686 ncurses-libs.i686; do
    if dnf list --available "$ncurses_pkg" >/dev/null 2>&1 || rpm -q "$ncurses_pkg" >/dev/null 2>&1; then
        DEPS+=("$ncurses_pkg")
        break
    fi
done

NEEDED=()
for d in "${DEPS[@]}"; do
    if ! rpm -q "$d" >/dev/null 2>&1; then
        NEEDED+=("$d")
    fi
done

if [ ${#NEEDED[@]} -gt 0 ]; then
    sudo dnf install -y "${NEEDED[@]}"
    ok "Build dependencies installed"
else
    skip "All build dependencies already installed"
fi

# Fedora TLS CA certificate compatibility
#
# Older Flutter/Dart tooling may look for the CA bundle at the traditional
# OpenSSL path:
#
#   /etc/pki/tls/certs/ca-bundle.crt
#
# Fedora's current extracted CA bundle is located at:
#
#   /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem
#
# Provide the expected path without replacing Fedora's managed CA bundle.
# This is idempotent and only creates/fixes the symlink when necessary.

log "Checking TLS CA certificate compatibility"

CA_SOURCE="/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"
CA_LINK="/etc/pki/tls/certs/ca-bundle.crt"

if [ ! -f "${CA_SOURCE}" ]; then
    warn "Fedora CA bundle not found at ${CA_SOURCE}"
else
    CA_NEEDS_FIX=false

    if [ ! -e "${CA_LINK}" ] && [ ! -L "${CA_LINK}" ]; then
        # Expected CA bundle path does not exist.
        CA_NEEDS_FIX=true
    elif [ ! -L "${CA_LINK}" ]; then
        # A real file exists at the expected path. Do not replace it.
        warn "${CA_LINK} exists but is not a symlink; leaving it unchanged"
    elif [ "$(readlink -f "${CA_LINK}")" != "${CA_SOURCE}" ]; then
        # A symlink exists, but it points somewhere other than Fedora's
        # current CA bundle.
        CA_NEEDS_FIX=true
    fi

    if [ "${CA_NEEDS_FIX}" = true ]; then
        log "Creating Fedora CA bundle compatibility symlink"

        sudo mkdir -p "$(dirname "${CA_LINK}")"
        sudo ln -sfn "${CA_SOURCE}" "${CA_LINK}"

        ok "TLS CA bundle compatibility link created"
    else
        ok "TLS CA bundle compatibility link already configured"
    fi
fi

# Sanity checks
if have rpmbuild; then ok "rpmbuild present"; else warn "rpmbuild missing after dnf install"; fi
if have magick || have convert; then ok "ImageMagick present"; else warn "ImageMagick missing after dnf install"; fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"

# Optional Virtual Environment & AppImage Packaging Setup
if [ "$INSTALL_APPIMAGE_DEPS" = true ]; then
    log "Setting up Python virtual environment & AppImage dependencies (--appimage enabled)"

    # appimage-builder internally invokes dpkg/apt when packaging Debian binaries
    EXTRA_PKG=()
    for p in dpkg apt gnupg2; do
        if ! rpm -q "$p" >/dev/null 2>&1; then
            EXTRA_PKG+=("$p")
        fi
    done
    if [ ${#EXTRA_PKG[@]} -gt 0 ]; then
        sudo dnf install -y "${EXTRA_PKG[@]}" || true
    fi

    if [ ! -d "${VENV_DIR}" ]; then
        log "Creating virtual environment at ${VENV_DIR}"
        python3 -m venv "${VENV_DIR}"
    else
        log "Virtual environment already exists at ${VENV_DIR}."
    fi

    log "Installing Python build packages..."
    "${VENV_DIR}/bin/pip" install --upgrade pip setuptools wheel
    "${VENV_DIR}/bin/pip" install "setuptools_scm<10"
    "${VENV_DIR}/bin/pip" install "git+https://github.com/rustdesk-org/appimage-builder.git"

    # Create a compatibility shim if apt-key is not on $PATH
    if ! command -v apt-key >/dev/null 2>&1; then
        log "'apt-key' not found on \$PATH. Creating compatibility shim at /usr/local/bin/apt-key..."

        sudo mkdir -p /etc/apt/trusted.gpg.d
        sudo tee /usr/local/bin/apt-key >/dev/null <<'EOF'
#!/bin/sh
case "$1" in
  add)
    shift
    if [ "$1" = "-" ] || [ -z "$1" ]; then
      gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/appimage-builder-shim.gpg >/dev/null
    else
      gpg --dearmor < "$1" | sudo tee "/etc/apt/trusted.gpg.d/$(basename "$1").gpg" >/dev/null
    fi
    ;;
  list|fingerprint)
    gpg --no-default-keyring --keyring /etc/apt/trusted.gpg.d/*.gpg --list-keys 2>/dev/null
    ;;
  *)
    exit 0
    ;;
esac
exit 0
EOF

        sudo chmod +x /usr/local/bin/apt-key
        log "apt-key shim created successfully."
    else
        log "'apt-key' already exists at $(command -v apt-key)."
    fi

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
        find "${NDK_DIR}" \
            -type f \
            -path "*/toolchains/llvm/prebuilt/*/bin/*" \
            -exec chmod +x {} +
        find "${NDK_DIR}" \
            -maxdepth 3 \
            -type f \
            \( -name "ndk-build" -o -name "*.sh" \) \
            -exec chmod +x {} +
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