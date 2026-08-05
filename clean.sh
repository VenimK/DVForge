#!/usr/bin/env bash
# RustDesk Local Builder — uninstaller / clean-slate reset
#
# Removes all generated build artifacts so the next run starts from scratch:
#   - workspace/rustdesk-src   (cloned source tree, can be huge)
#   - workspace/output          (built installers / DMGs / APKs)
#   - workspace/.checkout-tmp   (leftover from NFS-stale-handle fallback)
#   - __pycache__               (Python bytecode cache)
#   - builder/__pycache__       (same)
#   - *.log, error*.txt         (build logs from failed runs)
#
# Preserves (in default and --all mode):
#   - configs/       (your RustDesk.json and branding config)
#   - workspace/branding/  (custom icons, logos, etc.)
#   - .toolchains/    (downloaded toolchain references)
#   - patches/        (custom patches)
#   - web/            (web UI assets)
#   - builder/ source code
#
# Usage:
#   ./clean.sh           — remove build artifacts (safe reset)
#   ./clean.sh --all     — also remove output artifacts and stray installers
#   ./clean.sh --purge   — NUCLEAR: remove everything generated, including
#                          .toolchains, workspace/branding, configs, patches,
#                          web, screenshots — back to just source code
#   ./clean.sh --purge-system
#                        — also wipe system-wide build caches:
#                          ~/.cargo/registry, ~/.pub-cache, ~/.gradle,
#                          vcpkg buildtrees/install.  Does NOT uninstall
#                          Rust/Flutter themselves (use rustup/rustup-init
#                          or rm -rf ~/.rustup for that).
#   ./clean.sh --help    — show this help
set -e
cd "$(dirname "$0")"

ALL=false
PURGE=false
PURGE_SYSTEM=false
case "${1:-}" in
  --all)           ALL=true ;;
  --purge)         PURGE=true ;;
  --purge-system)  PURGE=true; PURGE_SYSTEM=true ;;
  --help|-h)
    sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
    exit 0 ;;
esac

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
skip() { printf '  \033[33m·\033[0m %s (not present)\n' "$1"; }
info() { printf '  \033[36m%s\033[0m %s\n' "..." "$1"; }

# show a spinner while a long rm runs in the background
spin() {
  local pid=$1 delay=0.6
  local spinstr='|/-\'
  while kill -0 "$pid" 2>/dev/null; do
    local temp=${spinstr#?}
    printf "\033[2m [%c]\033[0m\r" "$spinstr"
    local spinstr=$temp${spinstr%"$temp"}
    sleep $delay
  done
  printf "\r"
}

# remove a directory and show live progress (spinner) + size removed
rm_with_log() {
  local target="$1"
  if [ -e "$target" ]; then
    local size
    size=$(du -sh "$target" 2>/dev/null | cut -f1)
    [ -z "$size" ] && size="unknown size"
    printf '  removing %s (%s, this may take a moment) ' "$target" "$size"
    chmod -R u+rwx "$target" 2>/dev/null || true
    rm -rf "$target" >/dev/null 2>&1 &
    local pid=$!
    spin "$pid"
    wait "$pid" 2>/dev/null || true
    if [ -e "$target" ]; then
      echo "\033[31mFAILED\033[0m"
      return 1
    else
      echo "\033[32m✓\033[0m"
      return 0
    fi
  fi
}

bold "RustDesk Local Builder — Clean Reset"
echo ""

# --- workspace/rustdesk-src (the big one) ---
SRC="workspace/rustdesk-src"
if [ -e "$SRC" ]; then
  rm_with_log "$SRC" || {
    echo "    ! Could not remove $SRC — a process may still hold files open."
    echo "      Trying NFS stale-handle retry in 2s..."
    sleep 2
    rm_with_log "$SRC" || {
      echo "    ! Still could not remove $SRC."
      echo "      Try: lsof +D \"$SRC\" to find what's locking it, then re-run."
    }
  }
else
  skip "$SRC"
fi

# --- workspace/.checkout-tmp (NFS fallback clone) ---
TMP="workspace/.checkout-tmp"
if [ -e "$TMP" ]; then
  rm_with_log "$TMP" || echo "    ! Could not remove $TMP"
else
  skip "$TMP"
fi

# --- Python caches ---
for pyc in __pycache__ builder/__pycache__; do
  if [ -d "$pyc" ]; then
    rm -rf "$pyc"
    ok "removed $pyc"
  else
    skip "$pyc"
  fi
done

# --- build logs ---
for f in error*.txt buildingMSI.txt nugetmsi.txt *.log; do
  if [ -f "$f" ]; then
    info "removing $f"
    rm -f "$f"
    ok "removed $f"
  fi
done

# --- --all: also remove output artifacts ---
if $ALL || $PURGE; then
  echo ""
  bold "Removing output artifacts"
  if [ -d "workspace/output" ]; then
    rm_with_log "workspace/output" || echo "    ! Could not remove workspace/output"
  else
    skip "workspace/output"
  fi
  # stray build artifacts in project root
  for f in *.exe *.msi *.dmg *.deb *.rpm *.AppImage *.apk *.tar.gz *.zip; do
    for g in $f; do
      [ -e "$g" ] && info "removing $g" && rm -f "$g" && ok "removed $g"
    done
  done
fi

# --- --purge: remove EVERYTHING generated ---
if $PURGE; then
  echo ""
  bold "PURGE: removing toolchains, branding, configs, patches, web"

  for d in .toolchains workspace/branding configs patches web; do
    rm_with_log "$d" || echo "    ! Could not remove $d"
  done

  # remove screenshots and other stray files in project root
  for f in *.png *.txt; do
    for g in $f; do
      # don't delete the script itself or clean.bat
      case "$g" in
        clean.sh|clean.bat) continue ;;
      esac
      [ -e "$g" ] && info "removing $g" && rm -f "$g" && ok "removed $g"
    done
  done

  # remove the entire workspace dir if empty now
  if [ -d "workspace" ] && [ -z "$(ls -A workspace 2>/dev/null)" ]; then
    info "removing empty workspace/"
    rmdir workspace
    ok "removed empty workspace/"
  fi
fi

# --- --purge-system: wipe system-wide build caches ---
if $PURGE_SYSTEM; then
  echo ""
  bold "PURGE-SYSTEM: wiping system-wide build caches"

  # Rust crate cache (downloaded crates, not the toolchain)
  rm_with_log "${HOME}/.cargo/registry" || echo "    ! Could not remove .cargo/registry"

  # Rust git checkouts cache
  rm_with_log "${HOME}/.cargo/git" || echo "    ! Could not remove .cargo/git"

  # Flutter/Dart package cache
  rm_with_log "${HOME}/.pub-cache" || echo "    ! Could not remove .pub-cache"

  # Gradle cache (Android builds)
  rm_with_log "${HOME}/.gradle" || echo "    ! Could not remove .gradle"

  # vcpkg buildtrees / installed packages (if VCPKG_ROOT is set)
  VCPKG_ROOT_VAL="${VCPKG_ROOT:-}"
  if [ -n "$VCPKG_ROOT_VAL" ] && [ -d "$VCPKG_ROOT_VAL" ]; then
    for vdir in buildtrees installed packages; do
      rm_with_log "$VCPKG_ROOT_VAL/$vdir" || echo "    ! Could not remove VCPKG_ROOT/$vdir"
    done
  else
    skip "VCPKG_ROOT (not set)"
  fi

  echo ""
  echo "  Note: Rust toolchain (~/.rustup) and Flutter SDK were NOT removed."
  echo "  To fully uninstall them:"
  echo "    rustup self uninstall"
  echo "    rm -rf ~/flutter  (or wherever Flutter is installed)"
fi

echo ""
if $PURGE_SYSTEM; then
  bold "Done. Full purge + system caches wiped."
  echo "  Toolchains, branding, configs, AND build caches are gone."
  echo "  Next build will re-download everything from scratch."
elif $PURGE; then
  bold "Done. Full purge complete — back to bare source code."
  echo "  You will need to re-download toolchains and reconfigure"
  echo "  branding/configs before the next build."
else
  bold "Done. System is clean and ready for a fresh build."
  echo "  Run ./run.sh to start a new build."
fi
