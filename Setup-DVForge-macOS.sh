#!/usr/bin/env bash
# Setup-DVForge-macOS.sh
# Sets up a native macOS build environment for DVForge (macOS .dmg).
#
# Mirrors Setup-DVForge-Windows.ps1 / Setup-DVForge-WSL2.ps1.
# Idempotent — safe to re-run.
#
# Installs / verifies:
#   - Xcode Command Line Tools (git, clang, iconutil)
#   - Homebrew (if missing)
#   - Python 3.8+, cmake, ninja, nasm, pkg-config, create-dmg, cocoapods
#   - DVForge at ~/DVForge (or --in-place / --install-root)
#   - Via builder/toolchains.py (same pins as the GUI):
#       Rust 1.81 (macOS CI pin), Flutter 3.24.5, LLVM/libclang 15.0.6,
#       vcpkg (pinned), sccache 0.11.0, ImageMagick, potrace
#   - Pins rustup default to 1.81-<host> and adds both Darwin targets
#     so universal DMGs can lipo
#   - Writes .toolchains/env.json + a DVForge block in ~/.zprofile
#
# Companion: Uninstall-DVForge-macOS.sh
#
# Usage:
#   bash Setup-DVForge-macOS.sh
#   bash Setup-DVForge-macOS.sh --in-place
#   bash Setup-DVForge-macOS.sh --install-root "$HOME/src/DVForge"
#   bash Setup-DVForge-macOS.sh --with-android   # also JDK 17 + NDK r28c
#   bash Setup-DVForge-macOS.sh --skip-optional  # no sccache / imagemagick / potrace

set -euo pipefail

INSTALL_ROOT="${HOME}/DVForge"
IN_PLACE=0
REPO_URL="https://github.com/VenimK/DVForge.git"
SKIP_OPTIONAL=0
WITH_ANDROID=0
WITH_LLVM=0
FORCE=0

usage() {
  cat <<'EOF'
Setup-DVForge-macOS.sh — native macOS build environment for DVForge (.dmg)

Options:
  --install-root DIR   Where DVForge should live (default: ~/DVForge)
  --in-place           Use this script's directory (do not copy/clone)
  --repo-url URL       Git clone URL when the folder is empty
  --skip-optional      Skip sccache, ImageMagick, potrace
  --with-android       Also install Temurin JDK 17 + NDK r28c
                       (APKs are still marked Linux-only on the board)
  --with-llvm          Accepted for compatibility; LLVM 15.0.6 is installed
                       by default into .toolchains/llvm
  --force              Re-copy / re-clone into install-root
  -h, --help           Show this help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --install-root) INSTALL_ROOT="${2:-}"; shift 2 ;;
    --in-place)     IN_PLACE=1; shift ;;
    --repo-url)     REPO_URL="${2:-}"; shift 2 ;;
    --skip-optional) SKIP_OPTIONAL=1; shift ;;
    --with-android) WITH_ANDROID=1; shift ;;
    --with-llvm)    WITH_LLVM=1; shift ;;
    --force)        FORCE=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# helpers (bash 3.2 compatible — stock /bin/bash on macOS)
# ---------------------------------------------------------------------------
if [ "$(uname -s)" != "Darwin" ]; then
  printf 'This installer is for macOS. On Windows use Setup-DVForge-Windows.ps1.\n' >&2
  exit 1
fi

step() { printf '\n=== %s ===\n' "$1"; }
ok()   { printf '  [OK] %s\n' "$1"; }
skip() { printf '  [SKIP] %s\n' "$1"; }
warn() { printf '  [WARN] %s\n' "$1"; }
err()  { printf '  [ERROR] %s\n' "$1" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

is_dvforge_root() {
  [ -n "${1:-}" ] && [ -f "$1/app.py" ] && [ -f "$1/builder/toolchains.py" ]
}

# Resolve the directory this script lives in (follow symlink one hop).
SCRIPT_PATH="$0"
if [ -L "$SCRIPT_PATH" ]; then
  SCRIPT_PATH="$(readlink "$SCRIPT_PATH")"
fi
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"

SOURCE_ROOT=""
if is_dvforge_root "$SCRIPT_DIR"; then
  SOURCE_ROOT="$SCRIPT_DIR"
fi

if [ "$IN_PLACE" -eq 1 ]; then
  if [ -z "$SOURCE_ROOT" ]; then
    err '--in-place requires this script to live inside a DVForge tree (app.py + builder/).'
    exit 1
  fi
  INSTALL_ROOT="$SOURCE_ROOT"
fi

# Expand ~ and make absolute
INSTALL_ROOT="${INSTALL_ROOT/#\~/$HOME}"
case "$INSTALL_ROOT" in
  /*) ;;
  *) INSTALL_ROOT="$(pwd)/$INSTALL_ROOT" ;;
esac

ARCH="$(uname -m)"
if [ "$ARCH" = "arm64" ]; then
  HOST_TRIPLE="aarch64-apple-darwin"
  BREW_PREFIX="/opt/homebrew"
else
  HOST_TRIPLE="x86_64-apple-darwin"
  BREW_PREFIX="/usr/local"
fi
RUST_TOOLCHAIN="1.81-${HOST_TRIPLE}"

# Prefer Homebrew on PATH even in a fresh Terminal.
if [ -x "${BREW_PREFIX}/bin/brew" ]; then
  eval "$("${BREW_PREFIX}/bin/brew" shellenv)"
elif have brew; then
  eval "$(brew shellenv)"
fi

step 'DVForge macOS build environment setup'
printf '  Install root:     %s\n' "$INSTALL_ROOT"
printf '  Source tree:      %s\n' "${SOURCE_ROOT:-'(git clone)'}"
printf '  Host triple:      %s\n' "$HOST_TRIPLE"
printf '  Rust pin:         %s (macOS CI; Windows/Linux use 1.75)\n' "$RUST_TOOLCHAIN"
printf '  Optional tools:   %s\n' "$( [ "$SKIP_OPTIONAL" -eq 1 ] && echo skip || echo 'sccache + ImageMagick + potrace' )"
printf '  Android extras:   %s\n' "$( [ "$WITH_ANDROID" -eq 1 ] && echo 'JDK 17 + NDK r28c' || echo no )"
printf '  Portable LLVM:    15.0.6 -> .toolchains/llvm (clang stays off PATH)\n'
printf '\n'

# ---------------------------------------------------------------------------
# Phase 1 — Xcode Command Line Tools
# ---------------------------------------------------------------------------
step 'Phase 1: Xcode Command Line Tools'

CLT_OK=0
if xcode-select -p >/dev/null 2>&1; then
  CLT_PATH="$(xcode-select -p)"
  if [ -x "${CLT_PATH}/usr/bin/clang" ] || [ -x /usr/bin/clang ]; then
    CLT_OK=1
  fi
fi

if [ "$CLT_OK" -eq 1 ]; then
  skip "Xcode tools present ($(xcode-select -p))"
else
  warn 'Xcode Command Line Tools are not installed.'
  printf '  Opening the official installer. When it finishes, re-run this script.\n'
  xcode-select --install >/dev/null 2>&1 || true
  err 'Waiting for Command Line Tools. Re-run after the installer completes.'
  exit 1
fi

if have xcodebuild; then
  ok "xcodebuild: $(xcodebuild -version 2>/dev/null | head -1)"
else
  skip 'xcodebuild not on PATH (CLT-only is enough for rustdesk/flutter macos)'
fi

if have git; then
  skip "Git present: $(command -v git)"
else
  err 'git missing after CLT install. Open a new Terminal and re-run.'
  exit 1
fi

# ---------------------------------------------------------------------------
# Phase 2 — Homebrew + build packages
# ---------------------------------------------------------------------------
step 'Phase 2: Homebrew + packaging tools'

if have brew; then
  skip "Homebrew present: $(command -v brew)"
else
  printf '  Installing Homebrew (may prompt for your Mac password) ...\n'
  NONINTERACTIVE=1 /bin/bash -c \
    "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  if [ -x "${BREW_PREFIX}/bin/brew" ]; then
    eval "$("${BREW_PREFIX}/bin/brew" shellenv)"
  fi
  if ! have brew; then
    err 'Homebrew install finished but brew is not on PATH. Open a new Terminal and re-run.'
    exit 1
  fi
  ok "Homebrew installed: $(command -v brew)"
fi

# Python 3.8+
PY=""
for cand in python3 python; do
  if have "$cand"; then
    if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
      PY="$cand"
      break
    fi
  fi
done
if [ -n "$PY" ]; then
  skip "Python present: $("$PY" -c 'import sys; print(sys.version.split()[0])') ($PY)"
else
  printf '  brew install python@3.12 ...\n'
  brew install python@3.12
  if have python3; then
    PY=python3
  else
    err 'Python 3.8+ is required. Install from https://www.python.org/downloads/macos/ and re-run.'
    exit 1
  fi
  ok "Using $PY ($("$PY" -c 'import sys; print(sys.version.split()[0])'))"
fi

BREW_PKGS="cmake ninja nasm pkg-config create-dmg cocoapods"
for pkg in $BREW_PKGS; do
  if brew list --formula "$pkg" >/dev/null 2>&1 || have "$pkg"; then
    skip "$pkg already installed"
  else
    printf '  brew install %s ...\n' "$pkg"
    brew install "$pkg"
    ok "$pkg installed"
  fi
done

if have create-dmg; then
  ok "create-dmg: $(command -v create-dmg)"
else
  warn 'create-dmg not on PATH — DMG packaging will be skipped until brew install create-dmg'
fi

if have pod; then
  ok "CocoaPods: $(command -v pod)"
else
  warn 'pod not on PATH — flutter build macos needs CocoaPods'
fi

# ---------------------------------------------------------------------------
# Phase 3 — Place DVForge
# ---------------------------------------------------------------------------
step 'Phase 3: DVForge project files'

copy_tree() {
  src="$1"
  dest="$2"
  mkdir -p "$dest"
  printf '  Copying %s -> %s ...\n' "$src" "$dest"
  if have rsync; then
    rsync -a \
      --exclude '.git' \
      --exclude '__pycache__' \
      --exclude 'workspace/rustdesk-src/target' \
      --exclude 'workspace/rustdesk-src/flutter/build' \
      --exclude 'workspace/output' \
      --exclude '.toolchains' \
      "$src/" "$dest/"
  else
    ditto "$src" "$dest"
  fi
  ok "DVForge files at $dest"
}

if is_dvforge_root "$INSTALL_ROOT"; then
  if [ -n "$SOURCE_ROOT" ] && [ "$SOURCE_ROOT" = "$INSTALL_ROOT" ]; then
    skip "Already running inside install root: $INSTALL_ROOT"
  elif [ "$FORCE" -eq 1 ] && [ -n "$SOURCE_ROOT" ]; then
    copy_tree "$SOURCE_ROOT" "$INSTALL_ROOT"
  else
    skip "DVForge already present at $INSTALL_ROOT (pass --force to re-copy)"
  fi
elif [ -n "$SOURCE_ROOT" ]; then
  if [ ! -e "$INSTALL_ROOT" ] || [ "$FORCE" -eq 1 ]; then
    copy_tree "$SOURCE_ROOT" "$INSTALL_ROOT"
  else
    err "$INSTALL_ROOT exists but is not a DVForge tree. Pass --force or another --install-root."
    exit 1
  fi
else
  if [ -e "$INSTALL_ROOT" ]; then
    if [ "$FORCE" -ne 1 ]; then
      err "$INSTALL_ROOT exists. Pass --force to remove and re-clone, or --install-root elsewhere."
      exit 1
    fi
    warn "Removing existing $INSTALL_ROOT for fresh clone ..."
    rm -rf "$INSTALL_ROOT"
  fi
  printf '  git clone %s %s ...\n' "$REPO_URL" "$INSTALL_ROOT"
  git clone "$REPO_URL" "$INSTALL_ROOT"
  ok "Cloned to $INSTALL_ROOT"
fi

if ! is_dvforge_root "$INSTALL_ROOT"; then
  err "Install root is not a valid DVForge tree: $INSTALL_ROOT"
  exit 1
fi

# ---------------------------------------------------------------------------
# Phase 4 — pinned toolchains via builder/toolchains.py
# ---------------------------------------------------------------------------
step 'Phase 4: Install pinned toolchains via builder/toolchains.py'

IDS="rust,flutter,llvm,vcpkg"
if [ "$SKIP_OPTIONAL" -eq 0 ]; then
  IDS="${IDS},sccache,imagemagick,potrace"
fi
if [ "$WITH_LLVM" -eq 1 ]; then
  skip 'LLVM 15.0.6 is already in the default toolchain set'
fi
if [ "$WITH_ANDROID" -eq 1 ]; then
  IDS="${IDS},java,android_ndk"
fi

printf '  tools: %s\n' "$IDS"
printf '  (Flutter + vcpkg first run can take several minutes.)\n\n'

export PATH="${HOME}/.cargo/bin:${PATH}"

(
  cd "$INSTALL_ROOT"
  IDS_CSV="$IDS" "$PY" - <<'PY'
import os, sys
root = os.getcwd()
ids_csv = os.environ.get("IDS_CSV", "")
sys.path.insert(0, root)
from builder import toolchains
ids = [x.strip() for x in ids_csv.split(",") if x.strip()]
print("Installing:", ids)
r = toolchains.install_many(ids, root, print)
toolchains.apply_persisted_env(root)
errs = r.get("errors") or []
if errs:
    print("TOOLCHAIN_ERRORS", errs)
    core = {"rust", "flutter", "llvm", "vcpkg"}
    bad = [e for e in errs if e[0] in core]
    if bad:
        raise SystemExit("core toolchain install failed: %s" % bad)
print("TOOLCHAINS_OK", r.get("installed"))
PY
)

ok 'Toolchains install finished'

# ---------------------------------------------------------------------------
# Phase 5 — Pin Rust 1.81 (macOS CI) + both Darwin targets
# ---------------------------------------------------------------------------
step 'Phase 5: Pin Rust 1.81 for macOS'

if [ -f "${HOME}/.cargo/env" ]; then
  # shellcheck disable=SC1090
  . "${HOME}/.cargo/env"
fi
export PATH="${HOME}/.cargo/bin:${PATH}"

if have rustup; then
  rustup toolchain install "$RUST_TOOLCHAIN"
  rustup target add aarch64-apple-darwin --toolchain "$RUST_TOOLCHAIN"
  rustup target add x86_64-apple-darwin --toolchain "$RUST_TOOLCHAIN"
  rustup default "$RUST_TOOLCHAIN"
  rustup component add rustfmt --toolchain "$RUST_TOOLCHAIN"
  ACTIVE="$(rustup show active-toolchain 2>/dev/null | head -1 || true)"
  ok "rustup default = ${ACTIVE:-$RUST_TOOLCHAIN}"
else
  warn 'rustup not on PATH — open a new Terminal, or install Rust via the GUI Toolchain tab'
fi

# ---------------------------------------------------------------------------
# Phase 6 — env.json + shell profile
# ---------------------------------------------------------------------------
step 'Phase 6: Environment (env.json + ~/.zprofile)'

VCPKG_ROOT="${INSTALL_ROOT}/.toolchains/vcpkg"
if [ ! -d "$VCPKG_ROOT" ]; then
  VCPKG_ROOT=""
fi
SCCACHE_BIN="${HOME}/.cargo/bin/sccache"
if [ ! -x "$SCCACHE_BIN" ]; then
  SCCACHE_BIN=""
fi
FLUTTER_BIN="${INSTALL_ROOT}/.toolchains/flutter/flutter/bin"
if [ ! -x "${FLUTTER_BIN}/flutter" ]; then
  FLUTTER_BIN=""
fi

# Merge VCPKG_ROOT / RUSTC_WRAPPER into whatever toolchains.py already wrote.
INSTALL_ROOT="$INSTALL_ROOT" VCPKG_ROOT="$VCPKG_ROOT" SCCACHE_BIN="$SCCACHE_BIN" \
"$PY" - <<'PY'
import json, os
root = os.environ["INSTALL_ROOT"]
p = os.path.join(root, ".toolchains", "env.json")
os.makedirs(os.path.dirname(p), exist_ok=True)
try:
    with open(p) as f:
        d = json.load(f)
except Exception:
    d = {}
d.setdefault("vars", {})
d.setdefault("path", [])
vcpkg = os.environ.get("VCPKG_ROOT") or ""
sccache = os.environ.get("SCCACHE_BIN") or ""
if vcpkg:
    d["vars"]["VCPKG_ROOT"] = vcpkg
    if vcpkg not in d["path"]:
        d["path"].append(vcpkg)
if sccache:
    d["vars"]["RUSTC_WRAPPER"] = sccache
with open(p, "w") as f:
    json.dump(d, f, indent=2)
    f.write("\n")
print("  wrote", p)
PY

MARKER="# >>> DVForge macOS build env >>>"
ENDMARK="# <<< DVForge macOS build env <<<"
ENV_BLOCK=$(cat <<ENVEOF
${MARKER}
# Added by Setup-DVForge-macOS.sh
export PATH="${HOME}/.cargo/bin:${FLUTTER_BIN:+${FLUTTER_BIN}:}${VCPKG_ROOT:+${VCPKG_ROOT}:}\$PATH"
${VCPKG_ROOT:+export VCPKG_ROOT="${VCPKG_ROOT}"}
${SCCACHE_BIN:+export RUSTC_WRAPPER="${SCCACHE_BIN}"}
${ENDMARK}
ENVEOF
)

write_profile() {
  file="$1"
  touch "$file"
  if grep -q "$MARKER" "$file" 2>/dev/null; then
    # bash 3.2: rewrite without GNU sed -i
    tmp="${file}.dvforge.tmp"
    awk -v m="$MARKER" -v e="$ENDMARK" '
      $0 == m {skip=1; next}
      $0 == e {skip=0; next}
      !skip {print}
    ' "$file" > "$tmp"
    mv "$tmp" "$file"
  fi
  printf '\n%s\n' "$ENV_BLOCK" >> "$file"
  ok "env block written to $file"
}

write_profile "${HOME}/.zprofile"
if [ -f "${HOME}/.zshrc" ]; then
  write_profile "${HOME}/.zshrc"
fi
if [ -f "${HOME}/.bashrc" ]; then
  write_profile "${HOME}/.bashrc"
fi

# ---------------------------------------------------------------------------
# Phase 7 — Sanity
# ---------------------------------------------------------------------------
step 'Phase 7: Sanity check'

export PATH="${HOME}/.cargo/bin:${FLUTTER_BIN:+${FLUTTER_BIN}:}${VCPKG_ROOT:+${VCPKG_ROOT}:}${PATH}"

check() {
  name="$1"
  cmd="$2"
  if have "$cmd"; then
    ok "$name: $(command -v "$cmd")"
  else
    warn "$name: not on PATH (may still work inside the app via env.json)"
  fi
}

check git git
check python3 "$PY"
check rustc rustc
check cargo cargo
check flutter flutter
check create-dmg create-dmg
check pod pod
check cmake cmake
if [ -n "$VCPKG_ROOT" ] && [ -x "${VCPKG_ROOT}/vcpkg" ]; then
  ok "vcpkg: ${VCPKG_ROOT}/vcpkg"
else
  warn 'vcpkg binary not found under .toolchains/vcpkg'
fi
if [ -f "${INSTALL_ROOT}/.toolchains/env.json" ]; then
  ok "env.json: ${INSTALL_ROOT}/.toolchains/env.json"
else
  warn 'env.json missing — launch app.py once or re-run setup'
fi
LLVM_DIR="${INSTALL_ROOT}/.toolchains/llvm"
if [ -f "${LLVM_DIR}/lib/libclang.dylib" ] || [ -f "${LLVM_DIR}/bin/libclang.dylib" ]; then
  ok "LLVM 15.0.6 libclang: ${LLVM_DIR}"
else
  # tarball nests clang+llvm-15.0.6-*-apple-darwin21.0/
  if find "$LLVM_DIR" -name 'libclang.dylib' -print -quit 2>/dev/null | grep -q .; then
    ok "LLVM 15.0.6 libclang under ${LLVM_DIR}"
  else
    warn "LLVM/libclang 15.0.6 not found under ${LLVM_DIR}"
  fi
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
step 'All done!'
printf '\n'
printf '  DVForge (macOS .dmg builds) is ready.\n'
printf '\n'
printf '  Next steps:\n'
printf '    1. cd "%s"\n' "$INSTALL_ROOT"
printf '    2. ./run.sh\n'
printf '       or:  python3 app.py\n'
printf '    3. Open http://127.0.0.1:8765\n'
printf '\n'
printf '  Capability board should light macOS — .dmg when Xcode CLT + Flutter are present.\n'
printf '  Windows .exe/.msi: use Setup-DVForge-Windows.ps1 on a Windows PC.\n'
printf '  Linux/Android packages: use Setup-DVForge-WSL2.ps1 (or a Linux box).\n'
printf '\n'
printf '  Uninstall:\n'
printf '    bash "%s/Uninstall-DVForge-macOS.sh"\n' "$INSTALL_ROOT"
printf '\n'
