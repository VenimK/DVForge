#!/usr/bin/env bash
# Uninstall-DVForge-macOS.sh
# Removes the DVForge macOS build environment created by Setup-DVForge-macOS.sh.
#
# Default (recommended): remove local toolchains + workspace junk, keep
# Homebrew, Xcode CLT, user Rust, and the project source:
#   - <install-root>/.toolchains
#   - <install-root>/workspace/rustdesk-src
#   - <install-root>/workspace/output
#   - DVForge env block in ~/.zprofile / ~/.zshrc / ~/.bashrc
#
# Optional:
#   --remove-project   Delete the entire install-root folder
#   --remove-rust      Delete ~/.cargo and ~/.rustup
#   --remove-sccache   Delete ~/.cache/sccache
#   --force            Skip confirmation
#
# Does NOT uninstall: Homebrew, Xcode / CLT, create-dmg, cmake, cocoapods,
# system Python.
#
# Idempotent — safe to re-run.

set -euo pipefail

INSTALL_ROOT=""
FORCE=0
REMOVE_PROJECT=0
REMOVE_RUST=0
REMOVE_SCCACHE=0

usage() {
  cat <<'EOF'
Uninstall-DVForge-macOS.sh — undo Setup-DVForge-macOS.sh

Options:
  --install-root DIR   DVForge folder (default: this script's tree, else ~/DVForge)
  --remove-project     Delete the entire install-root after cleaning toolchains
  --remove-rust        Delete ~/.cargo and ~/.rustup
  --remove-sccache     Delete ~/.cache/sccache
  --force              Skip confirmation
  -h, --help           Show this help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --install-root)   INSTALL_ROOT="${2:-}"; shift 2 ;;
    --remove-project) REMOVE_PROJECT=1; shift ;;
    --remove-rust)    REMOVE_RUST=1; shift ;;
    --remove-sccache) REMOVE_SCCACHE=1; shift ;;
    --force)          FORCE=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage; exit 1 ;;
  esac
done

if [ "$(uname -s)" != "Darwin" ]; then
  printf 'This uninstaller is for macOS.\n' >&2
  exit 1
fi

step() { printf '\n=== %s ===\n' "$1"; }
ok()   { printf '  [OK] %s\n' "$1"; }
skip() { printf '  [SKIP] %s\n' "$1"; }
warn() { printf '  [WARN] %s\n' "$1"; }

is_dvforge_root() {
  [ -n "${1:-}" ] && [ -f "$1/app.py" ] && [ -d "$1/builder" ]
}

rm_safe() {
  path="$1"
  label="${2:-$1}"
  if [ -e "$path" ] || [ -L "$path" ]; then
    rm -rf "$path"
    ok "removed $label"
  else
    skip "not found: $label"
  fi
}

SCRIPT_PATH="$0"
if [ -L "$SCRIPT_PATH" ]; then
  SCRIPT_PATH="$(readlink "$SCRIPT_PATH")"
fi
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"

if [ -z "$INSTALL_ROOT" ]; then
  if is_dvforge_root "$SCRIPT_DIR"; then
    INSTALL_ROOT="$SCRIPT_DIR"
  else
    INSTALL_ROOT="${HOME}/DVForge"
  fi
fi
INSTALL_ROOT="${INSTALL_ROOT/#\~/$HOME}"
case "$INSTALL_ROOT" in
  /*) ;;
  *) INSTALL_ROOT="$(pwd)/$INSTALL_ROOT" ;;
esac

step 'DVForge macOS environment uninstaller'
printf '  Install root:      %s\n' "$INSTALL_ROOT"
printf '  Remove toolchains: yes\n'
printf '  Remove workspace:  yes (src/output under project)\n'
printf '  Remove project:    %s\n' "$( [ "$REMOVE_PROJECT" -eq 1 ] && echo YES || echo no )"
printf '  Remove Rust user:  %s\n' "$( [ "$REMOVE_RUST" -eq 1 ] && echo YES || echo no )"
printf '  Remove sccache:    %s\n' "$( [ "$REMOVE_SCCACHE" -eq 1 ] && echo YES || echo no )"
printf '\n'
printf '  Kept by default: Homebrew, Xcode CLT, create-dmg, cmake, CocoaPods\n'
printf '\n'

if [ "$REMOVE_PROJECT" -eq 1 ]; then
  warn "This will DELETE the entire folder: $INSTALL_ROOT"
fi
if [ "$REMOVE_RUST" -eq 1 ]; then
  warn "This will DELETE ~/.cargo and ~/.rustup (all Rust toolchains)"
fi

if [ "$FORCE" -ne 1 ]; then
  printf 'Continue? [y/N] '
  read -r ans
  case "$ans" in
    y|Y|yes|YES) ;;
    *) printf 'Aborted.\n'; exit 0 ;;
  esac
fi

step 'Phase 1: Local toolchains + workspace'
rm_safe "${INSTALL_ROOT}/.toolchains" ".toolchains"
rm_safe "${INSTALL_ROOT}/workspace/rustdesk-src" "workspace/rustdesk-src"
rm_safe "${INSTALL_ROOT}/workspace/output" "workspace/output"
rm_safe "${INSTALL_ROOT}/workspace/.build-cache" "workspace/.build-cache"

step 'Phase 2: Shell profile block'
MARKER="# >>> DVForge macOS build env >>>"
ENDMARK="# <<< DVForge macOS build env <<<"
strip_block() {
  file="$1"
  if [ ! -f "$file" ]; then
    skip "no $file"
    return
  fi
  if ! grep -q "$MARKER" "$file" 2>/dev/null; then
    skip "no DVForge block in $file"
    return
  fi
  tmp="${file}.dvforge.tmp"
  awk -v m="$MARKER" -v e="$ENDMARK" '
    $0 == m {skip=1; next}
    $0 == e {skip=0; next}
    !skip {print}
  ' "$file" > "$tmp"
  mv "$tmp" "$file"
  ok "removed env block from $file"
}
strip_block "${HOME}/.zprofile"
strip_block "${HOME}/.zshrc"
strip_block "${HOME}/.bashrc"

step 'Phase 3: Optional user caches'
if [ "$REMOVE_SCCACHE" -eq 1 ]; then
  rm_safe "${HOME}/.cache/sccache" "~/.cache/sccache"
else
  skip 'sccache cache kept (pass --remove-sccache)'
fi
if [ "$REMOVE_RUST" -eq 1 ]; then
  rm_safe "${HOME}/.cargo" "~/.cargo"
  rm_safe "${HOME}/.rustup" "~/.rustup"
else
  skip 'user Rust kept (pass --remove-rust)'
fi

step 'Phase 4: Project folder'
if [ "$REMOVE_PROJECT" -eq 1 ]; then
  # If we live inside the tree, delete after this process exits
  if [ "$SCRIPT_DIR" = "$INSTALL_ROOT" ]; then
    warn "script lives in $INSTALL_ROOT — deleting after exit"
    # Move out so rm can succeed
    cd "$HOME"
    rm -rf "$INSTALL_ROOT"
    ok "removed $INSTALL_ROOT"
  else
    rm_safe "$INSTALL_ROOT" "$INSTALL_ROOT"
  fi
else
  skip "project source kept at $INSTALL_ROOT (pass --remove-project to delete)"
fi

step 'All done!'
printf '\n'
printf '  Homebrew packages (create-dmg, cmake, cocoapods, nasm, …) were left installed.\n'
printf '  To drop those too:  brew uninstall create-dmg cocoapods cmake ninja nasm pkg-config\n'
printf '\n'
