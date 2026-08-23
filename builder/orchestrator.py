"""
orchestrator.py — actually run a build on this machine.

It mirrors the GitHub Actions workflows step-for-step, but as local subprocess
calls with live log streaming. A single Build drives one run: it checks out the
RustDesk source at the chosen tag, generates the flutter_rust_bridge code,
applies customizations (via customize.py), builds the selected targets, writes
the base64 custom_.txt, and collects artifacts into an output folder.

Nothing here needs the network *at import time* — the heavy toolchains
(Rust/Flutter/vcpkg/NDK) are invoked only when a build is actually started.

Set dry_run=True to print the exact command plan without executing anything —
useful to preview a build, or to inspect it on a machine without the toolchains.
"""

import glob as _glob
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
import platform as _platform

from . import customize, detect, prereqs, toolchains

RUSTDESK_REPO = "https://github.com/rustdesk/rustdesk.git"
WINDOWS_FLUTTER_ENGINE_URL = (
    "https://github.com/rustdesk/engine/releases/download/main/"
    "windows-x64-release.zip"
)
# Official CI (flutter-build.yml) ships these next to rustdesk.exe so
# Settings → Printer can find drivers/RustDeskPrinterDriver/*.inf
PRINTER_DRIVER_ZIP_URL = (
    "https://github.com/rustdesk/hbb_common/releases/download/driver/"
    "rustdesk_printer_driver_v4-1.4.zip"
)
PRINTER_ADAPTER_ZIP_URL = (
    "https://github.com/rustdesk/hbb_common/releases/download/driver/"
    "printer_driver_adapter.zip"
)

# toolchain versions (match the workflows / SKILL.md)
RUST_VERSION = "1.75"        # Windows/Linux desktop
MAC_RUST_VERSION = "1.81"    # macOS desktop (official CI uses 1.81)
FLUTTER_VERSION = "3.24.5"

# Compile caches and bulky downloads that must survive a source reset.
# Customizations mutate tracked files, so we still git-reset; we just do
# not delete these (they are gitignored / untracked).
SOURCE_CACHE_KEEP = (
    "target",
    "flutter/build",
    "flutter/.dart_tool",
    "flutter/.flutter-plugins",
    "flutter/.flutter-plugins-dependencies",
    "flutter/windows/flutter/ephemeral",
    "flutter/windows/flutter/generated_plugin_registrant.cc",
    "flutter/windows/flutter/generated_plugin_registrant.h",
    "flutter/windows/flutter/generated_plugins.cmake",
    "src/bridge_generated.rs",
    "src/bridge_generated.io.rs",
    "flutter/lib/generated_bridge.dart",
    "flutter/lib/generated_bridge.freezed.dart",
    # Xcode compile input (SWIFT_OBJC_BRIDGING_HEADER). gitignored, so
    # git clean -fdx deletes it unless listed here; rust/dart-only keep
    # then made generate_bridge() skip and macOS builds fail.
    "flutter/macos/Runner/bridge_generated.h",
    "flutter/ios/Runner/bridge_generated.h",
    "windows-x64-release.zip",
    "windows-x64-release",
)


def _cargo_bin():
    return os.path.join(os.path.expanduser("~"), ".cargo", "bin")


def _force_rmtree(path):
    """Remove a tree even if it has read-only files (Windows .git objects)."""
    import stat

    def _onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass
    shutil.rmtree(path, onerror=_onerror)


def _shell_rmtree(path):
    """Best-effort shell delete for stubborn trees. Never raises if the tool is missing.

    On Windows, Unix `rm -rf` is usually not on PATH — calling it raised WinError 2
    and aborted the whole build during checkout cleanup. Use rmdir /s /q instead.
    """
    if not path or not os.path.exists(path):
        return
    try:
        if _platform.system() == "Windows":
            # Use list form so paths with spaces stay one argument.
            subprocess.run(
                ["cmd", "/c", "rmdir", "/s", "/q", path],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.run(
                ["rm", "-rf", path],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except FileNotFoundError:
        # cmd/rm missing from PATH — ignore; caller may still clone to a temp dir.
        pass
    except OSError:
        pass


class BuildCancelled(Exception):
    pass


class Build:
    def __init__(self, version, target_ids, config, workspace,
                 log=None, dry_run=False):
        self.version = version.lstrip("v")
        self.target_ids = target_ids
        self.config = config
        self.workspace = os.path.abspath(workspace)
        self._log = log or (lambda m: print(m))
        self.dry_run = dry_run
        self.cancel_event = threading.Event()

        self.src_dir = os.path.join(self.workspace, "rustdesk-src")
        self.out_dir = os.path.join(self.workspace, "output", f"v{self.version}")
        self.patches_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "patches"))
        self.host = detect.host_info()
        self.artifacts = []
        self._llvm_home = None
        self._ffigen_cpath = ""
        self._flutter_bindir = None

    # -- logging / cancel ---------------------------------------------------
    def log(self, msg=""):
        self._log(msg)

    def cancel(self):
        self.cancel_event.set()

    def _check_cancel(self):
        if self.cancel_event.is_set():
            raise BuildCancelled()

    # -- subprocess with streaming -----------------------------------------
    def _effective_path(self):
        """PATH the build should see: chosen Flutter, cargo's bin dir, then
        whatever we inherited (env.json / system). Flutter goes first so a
        Homebrew or Chocolatey copy cannot shadow Dart 3.5.4.
        """
        parts = []
        flutter_bin = getattr(self, "_flutter_bindir", None)
        if not flutter_bin:
            flutter_home = toolchains.find_flutter_home(self._project_root())
            if flutter_home:
                cand = os.path.join(flutter_home, "bin")
                if os.path.isdir(cand):
                    flutter_bin = cand
        if flutter_bin:
            parts.append(flutter_bin)
        cargo = _cargo_bin()
        if os.path.isdir(cargo):
            parts.append(cargo)
        parts.append(os.environ.get("PATH", ""))
        return os.pathsep.join(parts)

    def _sed_i(self, expression, path, cwd=None, check=False):
        """In-place sed. BSD sed on macOS needs `sed -i '' expr file`."""
        if self._is_macos_host():
            cmd = ["sed", "-i", "", expression, path]
        else:
            cmd = ["sed", "-i", expression, path]
        return self.run(cmd, cwd=cwd, check=check)

    def run(self, cmd, cwd=None, env=None, shell=False, check=True):
        pretty = cmd if isinstance(cmd, str) else " ".join(cmd)
        self.log(f"$ {pretty}")
        if self.dry_run:
            return 0
        self._check_cancel()
        full_env = os.environ.copy()
        full_env["PATH"] = self._effective_path()
        if env:
            for k, v in env.items():
                if v is None:
                    full_env.pop(k, None)
                else:
                    full_env[k] = v

        # Resolve the executable so Windows finds .exe/.bat/.cmd (via PATHEXT)
        # and tools in ~/.cargo/bin — a bare name otherwise raises WinError 2.
        if not shell and isinstance(cmd, list) and cmd:
            resolved = shutil.which(cmd[0], path=full_env["PATH"])
            if resolved:
                cmd = [resolved] + cmd[1:]
            else:
                msg = (f"'{cmd[0]}' was not found on PATH. If you just installed it, "
                       f"click re-scan; otherwise install it from the Toolchain panel.")
                if check:
                    raise RuntimeError(msg)
                self.log("  ! " + msg)
                return 127

        try:
            os.makedirs(self.workspace, exist_ok=True)
            proc = subprocess.Popen(
                cmd, cwd=cwd or self.workspace, env=full_env, shell=shell,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", bufsize=1,
            )
        except FileNotFoundError:
            exe = cmd if isinstance(cmd, str) else cmd[0]
            raise RuntimeError(f"could not launch '{exe}' — is it installed and on PATH?")
        try:
            for line in proc.stdout:
                self.log(line.rstrip("\n"))
                if self.cancel_event.is_set():
                    proc.terminate()
                    raise BuildCancelled()
        finally:
            proc.stdout.close()
        rc = proc.wait()
        if check and rc != 0:
            raise RuntimeError(f"command failed (exit {rc}): {pretty}")
        return rc

    # -- high-level plan ----------------------------------------------------
    def platforms_needed(self):
        matrix = {t["id"]: t for t in detect.TARGETS}
        plats = []
        for tid in self.target_ids:
            p = matrix[tid]["platform"] if tid in matrix else None
            if p and p not in plats:
                plats.append(p)
        return plats

    def plan(self):
        """Return a human-readable list of the steps that will run."""
        steps = ["Check out RustDesk source at the chosen tag",
                 "Generate flutter_rust_bridge code"]
        for p in self.platforms_needed():
            steps.append(f"Apply customizations for {p}")
            steps.append(f"Build {p} target(s)")
        steps.append("Write base64 custom_.txt + collect artifacts")
        return steps

    # -- steps --------------------------------------------------------------
    def _git_capture(self, args, cwd=None, timeout=60):
        """Run git and return (rc, stdout). Never raises. Not logged as a step."""
        git = shutil.which("git", path=self._effective_path()) or "git"
        try:
            r = subprocess.run(
                [git] + list(args),
                cwd=cwd or self.src_dir,
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
                env={**os.environ, "PATH": self._effective_path()},
            )
            return r.returncode, (r.stdout or "").strip()
        except Exception as e:
            return 1, str(e)

    def _cache_park_dir(self):
        return os.path.join(self.workspace, ".build-cache")

    def _keep_path(self, rel, root=None):
        bits = rel.replace("\\", "/").split("/")
        return os.path.join(root or self.src_dir, *bits)

    def _park_build_caches(self):
        """Move compile caches out of src_dir so a wipe/clone cannot delete them."""
        if self.dry_run:
            return
        park = self._cache_park_dir()
        os.makedirs(park, exist_ok=True)
        for rel in SOURCE_CACHE_KEEP:
            src = self._keep_path(rel)
            dst = self._keep_path(rel, park)
            if not os.path.exists(src):
                continue
            if os.path.exists(dst):
                try:
                    if os.path.isdir(dst) and not os.path.islink(dst):
                        _force_rmtree(dst)
                    else:
                        os.remove(dst)
                except OSError:
                    try:
                        _shell_rmtree(dst)
                    except Exception:
                        pass
            parent = os.path.dirname(dst)
            if parent:
                os.makedirs(parent, exist_ok=True)
            self.log(f"  · parking {rel} -> {dst}")
            try:
                shutil.move(src, dst)
            except OSError as e:
                self.log(f"  ! could not park {rel}: {e}")

    def _restore_build_caches(self):
        """Move parked caches back after a fresh clone."""
        if self.dry_run:
            return
        park = self._cache_park_dir()
        if not os.path.isdir(park):
            return
        for rel in SOURCE_CACHE_KEEP:
            src = self._keep_path(rel, park)
            dst = self._keep_path(rel)
            if not os.path.exists(src):
                continue
            if os.path.exists(dst):
                continue
            parent = os.path.dirname(dst)
            if parent:
                os.makedirs(parent, exist_ok=True)
            self.log(f"  · restoring {rel}")
            try:
                shutil.move(src, dst)
            except OSError as e:
                self.log(f"  ! could not restore {rel}: {e}")

    def _source_tag(self):
        """Exact tag at HEAD, without a leading v. Empty if untagged."""
        rc, desc = self._git_capture(
            ["describe", "--tags", "--exact-match"], cwd=self.src_dir)
        if rc != 0 or not desc:
            return ""
        return desc.lstrip("v")

    def _try_reuse_source(self):
        """Reset existing rustdesk-src to self.version, keeping compile caches.

        Customizations mutate tracked files, so we still `git reset --hard`.
        Return True if the tree is ready, False if the caller should wipe+clone.
        """
        git_marker = os.path.join(self.src_dir, ".git")
        if not os.path.exists(git_marker):
            return False

        rc, url = self._git_capture(["remote", "get-url", "origin"], cwd=self.src_dir)
        norm = (url or "").replace(":", "/").lower()
        if rc != 0 or "rustdesk/rustdesk" not in norm:
            self.log("  ! existing tree is not rustdesk/rustdesk — will reclone")
            return False

        want = self.version.lstrip("v")
        have = self._source_tag()
        if have != want:
            self.log(f"  · switching checkout {have or 'unknown'} -> {want}")
            switched = False
            for ref in (want, f"v{want}"):
                rc = self.run(
                    ["git", "fetch", "--depth", "1", "origin",
                     f"refs/tags/{ref}:refs/tags/{ref}"],
                    cwd=self.src_dir, check=False)
                if rc == 0:
                    rc = self.run(
                        ["git", "checkout", "--force", ref],
                        cwd=self.src_dir, check=False)
                    if rc == 0 and self._source_tag() == want:
                        switched = True
                        break
            if not switched:
                rc = self.run(
                    ["git", "fetch", "--depth", "1", "origin", want],
                    cwd=self.src_dir, check=False)
                if rc == 0:
                    self.run(["git", "checkout", "--force", "FETCH_HEAD"],
                             cwd=self.src_dir, check=False)
                if self._source_tag() != want:
                    self.log("  ! could not switch to requested tag — will reclone")
                    return False
        else:
            self.log(f"  · reusing existing v{want} checkout "
                     "(reset tracked files, keep cargo/flutter caches)")

        rc = self.run(["git", "reset", "--hard", "HEAD"],
                      cwd=self.src_dir, check=False)
        if rc != 0:
            self.log("  ! git reset failed — will reclone")
            return False

        clean = ["git", "clean", "-fdx"]
        for rel in SOURCE_CACHE_KEEP:
            clean += ["-e", "/" + rel.replace("\\", "/")]
        rc = self.run(clean, cwd=self.src_dir, check=False)
        if rc != 0:
            self.log("  ! git clean failed — will reclone")
            return False

        self.run(["git", "submodule", "foreach", "--recursive",
                  "git", "reset", "--hard"], cwd=self.src_dir, check=False)
        self.run(["git", "submodule", "update", "--init", "--recursive"],
                 cwd=self.src_dir, check=False)

        if not os.path.isfile(os.path.join(self.src_dir, "Cargo.toml")):
            self.log("  ! Cargo.toml missing after reset — will reclone")
            return False
        self.log("  ✓ source reset; compile caches kept")
        return True

    def _wipe_source_tree(self):
        """Force-remove rustdesk-src. Used only when incremental reuse fails."""
        self.log(f"  clearing previous source at {self.src_dir}")
        try:
            import stat
            os.chmod(self.src_dir, stat.S_IRWXU)
            for root, dirs, files in os.walk(self.src_dir, topdown=False):
                for name in dirs + files:
                    p = os.path.join(root, name)
                    try:
                        os.chmod(p, stat.S_IRWXU)
                    except OSError:
                        pass
        except OSError:
            pass
        shutil.rmtree(self.src_dir, ignore_errors=True)
        if os.path.exists(self.src_dir):
            _force_rmtree(self.src_dir)
        if os.path.exists(self.src_dir):
            self.log(f"  force-removing leftover {self.src_dir}")
            _shell_rmtree(self.src_dir)
        if os.path.exists(self.src_dir):
            time.sleep(2)
            _shell_rmtree(self.src_dir)

    def _clone_source(self):
        os.makedirs(self.workspace, exist_ok=True)
        if os.path.exists(self.src_dir):
            tmp_dir = self.src_dir + ".checkout-tmp"
            if os.path.exists(tmp_dir):
                _shell_rmtree(tmp_dir)
                if os.path.exists(tmp_dir):
                    _force_rmtree(tmp_dir)
            self.run(["git", "clone", "--depth", "1", "--branch", self.version,
                      "--recurse-submodules", RUSTDESK_REPO, tmp_dir])
            _shell_rmtree(self.src_dir)
            if os.path.exists(self.src_dir):
                self.src_dir = tmp_dir
            else:
                os.rename(tmp_dir, self.src_dir)
        else:
            self.run(["git", "clone", "--depth", "1", "--branch", self.version,
                      "--recurse-submodules", RUSTDESK_REPO, self.src_dir])

    def checkout_source(self):
        self.log("\n=== 1. Check out RustDesk source ===")
        # Customizations mutate tracked files (sed/patch/rename), so every
        # build must start from a pristine *source* tree. Compile caches
        # (cargo target/, flutter/build, engine zip) are gitignored and
        # safe to keep — wiping them was the main rebuild tax.
        os.makedirs(self.workspace, exist_ok=True)
        if os.path.exists(self.src_dir):
            if self.dry_run:
                self.log(f"  (would reset {self.src_dir} to v{self.version}, "
                         "keeping cargo/flutter caches)")
                return
            if self._try_reuse_source():
                return
            self.log("  · reuse failed — parking caches and cloning fresh")
            self._park_build_caches()
            self._wipe_source_tree()
        self._clone_source()
        self._restore_build_caches()

    def _project_root(self):
        return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    def _has_libclang(self, d):
        """Check whether directory d contains a libclang shared library."""
        if not os.path.isdir(d):
            return False
        names = (
            "libclang.dylib", "libclang.so", "libclang.dll",
            "libclang.so.15", "libclang.so.16", "libclang.so.17",
            "libclang.so.13", "libclang.so.14",
        )
        if any(os.path.isfile(os.path.join(d, n)) for n in names):
            return True
        # versioned sonames e.g. libclang.so.15.0.6
        try:
            return any(
                n.startswith("libclang.so.") or n.startswith("libclang.")
                for n in os.listdir(d)
                if os.path.isfile(os.path.join(d, n))
            )
        except OSError:
            return False

    def _find_llvm_home(self, tc_llvm):
        """Find the actual LLVM home under .toolchains/llvm (one-level nesting)."""
        if os.path.isdir(os.path.join(tc_llvm, "bin")):
            return tc_llvm
        for name in os.listdir(tc_llvm) if os.path.isdir(tc_llvm) else []:
            candidate = os.path.join(tc_llvm, name)
            if os.path.isdir(candidate) and os.path.isdir(os.path.join(candidate, "bin")):
                return candidate
        return None

    def _toolchains_llvm_home(self):
        """Absolute path to the portable LLVM under .toolchains/, or None."""
        return self._find_llvm_home(
            os.path.join(self._project_root(), ".toolchains", "llvm"))

    def _libclang_dir(self, llvm_home):
        """Return the directory with libclang.dylib/so/dll."""
        names = ("libclang.dylib", "libclang.so", "libclang.dll",
                 "libclang.so.15", "libclang.so.16", "libclang.so.17")
        for sub in ("lib", "bin"):
            d = os.path.join(llvm_home, sub)
            if any(os.path.isfile(os.path.join(d, n)) for n in names):
                return d
            # any versioned libclang.so.*
            try:
                if any(n.startswith("libclang.so.") or n == "libclang.so"
                       for n in os.listdir(d)
                       if os.path.isfile(os.path.join(d, n))):
                    return d
            except OSError:
                pass
        # Default to lib on macOS/Linux, bin on Windows.
        import sys
        return (os.path.join(llvm_home, "bin") if sys.platform == "win32"
                else os.path.join(llvm_home, "lib"))

    def _wire_llvm_ffigen_includes(self, llvm_home):
        """Give ffigen/libclang host + Dart headers when using portable LLVM.

        The clang+llvm tarball ships libclang but not the host C library
        headers. Without CPATH, ffigen fails on stdbool.h and emits broken
        bindings such as `typedef bool = NativeFunction<...>` which shadows
        Dart's bool and tanks the Flutter build.
        """
        parts = []
        # macOS: Apple SDK headers first. Putting LLVM 15's clang resource
        # dir ahead of them makes #include <stdint.h> resolve to LLVM's
        # copy, which does not satisfy MacOSX.sdk's sys/resource.h
        # (unknown type name 'uint8_t').
        if self._is_macos_host():
            sdk = os.environ.get("SDKROOT", "")
            if sdk:
                for sub in ("usr/include", "usr/local/include"):
                    cand = os.path.join(sdk, sub)
                    if os.path.isdir(cand):
                        parts.append(cand)
        # clang resource dir inside the toolchains LLVM (stddef.h, etc.)
        # On macOS this is a last resort — Xcode libclang has its own.
        lib_clang = os.path.join(llvm_home, "lib", "clang")
        if os.path.isdir(lib_clang) and not self._is_macos_host():
            for ver in sorted(os.listdir(lib_clang), reverse=True):
                cand = os.path.join(lib_clang, ver, "include")
                if os.path.isdir(cand):
                    parts.append(cand)
                    break
        # host system headers
        for d in (
            "/usr/include",
            "/usr/include/x86_64-linux-gnu",
            "/usr/include/aarch64-linux-gnu",
            "/usr/include/arm-linux-gnueabihf",
        ):
            if os.path.isdir(d):
                parts.append(d)
        # gcc / system-clang builtin headers (stdbool.h on Debian/Ubuntu)
        for base in (
            "/usr/lib/gcc",
            "/usr/lib/llvm-19/lib/clang",
            "/usr/lib/llvm-18/lib/clang",
            "/usr/lib/llvm-17/lib/clang",
            "/usr/lib/llvm-15/lib/clang",
        ):
            if not os.path.isdir(base):
                continue
            found = None
            for root, _dirs, files in os.walk(base):
                if "stdbool.h" in files:
                    found = root
                    break
            if found:
                parts.append(found)
                break
        # Flutter/Dart SDK headers for dart_api.h (prefer toolchains Flutter)
        flutter_candidates = [
            os.path.join(self._project_root(), ".toolchains", "flutter",
                         "flutter", "bin", "cache", "dart-sdk", "include"),
        ]
        which_flutter = shutil.which("flutter", path=self._effective_path())
        if which_flutter:
            # .../bin/flutter → .../bin/cache/dart-sdk/include
            flutter_candidates.append(os.path.normpath(os.path.join(
                os.path.dirname(which_flutter), "cache", "dart-sdk", "include")))
        for dart_inc in flutter_candidates:
            if os.path.isdir(dart_inc):
                parts.append(dart_inc)
                third = os.path.join(dart_inc, "third_party")
                if os.path.isdir(third):
                    parts.append(third)
                break

        # de-dupe, keep order
        seen, uniq = set(), []
        for p in parts:
            ap = os.path.abspath(p)
            if ap not in seen and os.path.isdir(ap):
                seen.add(ap)
                uniq.append(ap)
        if not uniq:
            self._ffigen_cpath = ""
            return
        # Store for generate_bridge() to pass locally to the codegen command.
        # Do NOT set CPATH/C_INCLUDE_PATH in os.environ here — those are global
        # and would leak Clang-specific headers into GCC compilations (zstd-sys,
        # ring, etc.) causing "missing binary operator" errors in xmmintrin.h.
        self._ffigen_cpath = os.pathsep.join(uniq)
        self.log(f"  · ffigen CPATH prepared ({len(uniq)} dirs)")

    def _host_rust_triple(self):
        """Rustup host triple for this machine (the runnable toolchain)."""
        os_name = self.host.get("os_raw") or _platform.system()
        arch = self.host.get("arch") or "x86_64"
        # detect.normalize_arch → aarch64 | x86_64 | armv7 | …
        if arch in ("arm64", "aarch64"):
            arch = "aarch64"
        elif arch in ("x86_64", "amd64"):
            arch = "x86_64"
        if os_name == "Darwin":
            return f"{arch}-apple-darwin"
        if os_name == "Windows":
            # MSVC is the default Windows host for RustDesk builds
            return f"{arch}-pc-windows-msvc"
        return f"{arch}-unknown-linux-gnu"

    def _ensure_rust(self):
        """Install and default the Rust toolchain needed for the selected targets.

        macOS builds use 1.81 (official CI pin); Windows/Linux/Android use 1.75.
        The toolchain name must use the *host* triple (so rustc can run here),
        not a cross-compile target triple. Also ensures rustfmt is installed so
        flutter_rust_bridge_codegen can format generated code."""
        version = (MAC_RUST_VERSION
                   if any("macos" in t for t in self.target_ids)
                   else RUST_VERSION)
        host_triple = self._host_rust_triple()
        toolchain = f"{version}-{host_triple}"
        self.log(f"  · ensuring Rust {toolchain}")
        rustc = shutil.which("rustc", path=self._effective_path())
        rustc_ver = ""
        if rustc:
            try:
                rustc_ver = subprocess.check_output(
                    [rustc, "--version"], timeout=15,
                    encoding="utf-8", errors="replace",
                    env={**os.environ, "PATH": self._effective_path()},
                ).strip()
            except Exception:
                rustc_ver = ""
        if rustc_ver and version in rustc_ver:
            rustfmt = shutil.which("rustfmt", path=self._effective_path())
            if rustfmt:
                self.log(f"  · {rustc_ver} already active — skip rustup")
                return
        self.run(["rustup", "toolchain", "install", toolchain], check=False)
        # Host std is always needed; for macOS also ensure the selected Mac target.
        targets = {host_triple}
        if any("macos" in t for t in self.target_ids):
            targets.add(self._mac_target())
            # Universal DMG builds need both arches available for lipo.
            if any("universal" in t for t in self.target_ids):
                targets.update({"aarch64-apple-darwin", "x86_64-apple-darwin"})
        for target in sorted(targets):
            self.run(["rustup", "target", "add", target, "--toolchain", toolchain],
                     check=False)
        self.run(["rustup", "default", toolchain], check=False)
        # rustfmt is required by flutter_rust_bridge_codegen; without it the
        # codegen aborts and the build continues with stale/dummy bridge code.
        self.run(["rustup", "component", "add", "rustfmt", "--toolchain", toolchain],
                 check=False)
        # Also ensure the currently-active default has rustfmt (covers the case
        # where rustup default failed for a bad triple and we stayed on another
        # toolchain).
        self.run(["rustup", "component", "add", "rustfmt"], check=False)

    def _ensure_macos_sdk(self):
        """Point bindgen/libclang at the Apple SDK so system headers resolve.

        Custom LLVM tarballs ship libclang without the macOS SDK. Without
        SDKROOT / BINDGEN_EXTRA_CLANG_ARGS, bindgen fails with:
          fatal error: 'stdlib.h' file not found
          fatal error: 'inttypes.h' file not found
        """
        if self.host.get("os_raw") != "Darwin" and self.host.get("os") != "macOS":
            return
        sdk = os.environ.get("SDKROOT", "")
        if not sdk or not os.path.isdir(sdk):
            try:
                sdk = subprocess.check_output(
                    ["xcrun", "--show-sdk-path"], text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
            except Exception:
                sdk = ""
        if sdk and os.path.isdir(sdk):
            os.environ["SDKROOT"] = sdk
            # bindgen/ffigen (libclang) do not honour SDKROOT alone.
            # Newer MacOSX.sdk's sys/resource.h only #include <stdint.h>
            # when __DARWIN_C_LEVEL >= __DARWIN_C_FULL; LLVM 15's default
            # feature-test macros skip that and then fail with
            # "unknown type name 'uint8_t'". Force Darwin-full + sysroot.
            needed = [
                f"--sysroot={sdk}",
                "-D_DARWIN_C_SOURCE",
                "-D__DARWIN_C_LEVEL=__DARWIN_C_FULL",
            ]
            existing = os.environ.get("BINDGEN_EXTRA_CLANG_ARGS", "")
            parts = existing.split() if existing else []
            for flag in needed:
                if flag not in parts and not any(
                        p.startswith(flag.split("=")[0] + "=") for p in parts
                        if "=" in flag):
                    parts.append(flag)
            os.environ["BINDGEN_EXTRA_CLANG_ARGS"] = " ".join(parts)
            self.log(f"  · SDKROOT = {sdk}")
            self.log(f"  · BINDGEN_EXTRA_CLANG_ARGS = "
                     f"{os.environ['BINDGEN_EXTRA_CLANG_ARGS']}")
        else:
            self.log("  ! could not resolve macOS SDK path via xcrun "
                     "(bindgen may fail to find system headers)")

    def _is_macos_host(self):
        return (self.host.get("os_raw") == "Darwin"
                or self.host.get("os") == "macOS")

    def _macos_xcode_llvm_home(self):
        """Xcode's toolchain usr/ — ships a libclang that matches MacOSX.sdk."""
        stock = ("/Applications/Xcode.app/Contents/Developer/Toolchains/"
                 "XcodeDefault.xctoolchain/usr")
        if os.path.isfile(os.path.join(stock, "lib", "libclang.dylib")):
            return stock
        try:
            clang = subprocess.check_output(
                ["xcrun", "-f", "clang"], text=True,
                stderr=subprocess.DEVNULL, timeout=15,
            ).strip()
            usr = os.path.dirname(os.path.dirname(clang))
            if os.path.isfile(os.path.join(usr, "lib", "libclang.dylib")):
                return usr
        except Exception:
            pass
        return None

    def _macos_llvm_compiler_opts(self):
        """Flags ffigen/libclang need to parse current MacOSX.sdk headers."""
        sdk = os.environ.get("SDKROOT", "")
        if not sdk or not os.path.isdir(sdk):
            return ""
        # Use --sysroot= (not "-isysroot PATH"). clap treats a following
        # token that starts with -i as the short flag -i
        # ("unexpected argument '-i' found") and skips codegen entirely.
        return (f"--sysroot={sdk} -D_DARWIN_C_SOURCE "
                f"-D__DARWIN_C_LEVEL=__DARWIN_C_FULL")

    def _strip_llvm_bin_from_path(self):
        """Keep portable LLVM's clang off PATH on macOS.

        The 15.0.6 tarball's clang shadows Apple clang and then compiles
        MacOSX.sdk headers with the wrong feature-test macros (uint8_t
        unknown in sys/resource.h). LIBCLANG_PATH is enough for bindgen.
        """
        # Only the portable tarball — never Xcode's usr/bin.
        llvm_home = self._toolchains_llvm_home()
        if not llvm_home:
            return
        bindir = os.path.join(llvm_home, "bin")
        path = os.environ.get("PATH", "")
        keep = [p for p in path.split(os.pathsep)
                if p and os.path.normcase(os.path.abspath(p))
                != os.path.normcase(os.path.abspath(bindir))]
        if len(keep) != len([p for p in path.split(os.pathsep) if p]):
            os.environ["PATH"] = os.pathsep.join(keep)
            self.log(f"  · removed portable LLVM bin from PATH ({bindir})")

    @staticmethod
    def _is_sccache_wrapper(value):
        return bool(value) and "sccache" in os.path.basename(value).lower()

    def _clear_sccache_wrapper(self):
        if self._is_sccache_wrapper(os.environ.get("RUSTC_WRAPPER", "")):
            del os.environ["RUSTC_WRAPPER"]

    def _ensure_sccache(self):
        """If sccache is installed and its server works, wrap rustc with it.

        A dead or hung sccache server makes every `cargo` invocation fail with
        "Failed to send data to or receive data from server". That must never
        abort a build — fall back to uncached rustc instead.
        """
        sccache = shutil.which("sccache", path=self._effective_path())
        if not sccache:
            self._clear_sccache_wrapper()
            self.log("  · sccache not found — builds will run without cache. "
                     "Install it via the Toolchain tab or: cargo install sccache")
            return

        # Absolute path so cargo does not depend on PATH lookup. Bare
        # "sccache" is also easy to lose on Windows.
        rustc = shutil.which("rustc", path=self._effective_path())
        probe_env = {
            **os.environ,
            "PATH": self._effective_path(),
            "RUSTC_WRAPPER": sccache,
            # Don't let the server idle-exit during flutter pub get / vcpkg.
            "SCCACHE_IDLE_TIMEOUT": os.environ.get("SCCACHE_IDLE_TIMEOUT") or "0",
        }

        def _start_server():
            try:
                subprocess.run(
                    [sccache, "--start-server"],
                    capture_output=True, text=True, timeout=20,
                    env=probe_env,
                )
            except Exception:
                pass

        def _stop_server():
            try:
                subprocess.run(
                    [sccache, "--stop-server"],
                    capture_output=True, text=True, timeout=15,
                    env=probe_env,
                )
            except Exception:
                pass

        def _server_ok():
            if rustc:
                cmd = [sccache, rustc, "--print", "sysroot"]
            else:
                cmd = [sccache, "--show-stats"]
            try:
                r = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=30,
                    env=probe_env,
                )
                return r.returncode == 0
            except Exception:
                return False

        _start_server()
        if not _server_ok():
            self.log("  ! sccache server not responding — restarting it")
            _stop_server()
            _start_server()

        if _server_ok():
            os.environ["RUSTC_WRAPPER"] = sccache
            os.environ["SCCACHE_IDLE_TIMEOUT"] = probe_env["SCCACHE_IDLE_TIMEOUT"]
            self.log(f"  · sccache enabled ({sccache})")
            return

        # Don't let a broken cache take the whole Windows build down.
        self._clear_sccache_wrapper()
        self.log("  ! sccache server failed to start — continuing without "
                 "the compile cache (RUSTC_WRAPPER unset)")

    def _log_sccache_stats(self):
        """Print sccache cache statistics after all builds complete."""
        sccache = shutil.which("sccache", path=self._effective_path())
        if not sccache:
            return
        if not self._is_sccache_wrapper(os.environ.get("RUSTC_WRAPPER", "")):
            return
        self.log("\n=== sccache statistics ===")
        for args in (["--show-stats"], ["--show-adv-stats"]):
            try:
                out = subprocess.run(
                    [sccache] + args,
                    capture_output=True, text=True, timeout=15,
                    env={**os.environ, "PATH": self._effective_path()},
                )
                if out.stdout.strip():
                    for line in out.stdout.strip().splitlines():
                        self.log(f"  {line}")
                if out.stderr.strip():
                    self.log(f"  (stderr) {out.stderr.strip()}")
            except Exception as e:
                self.log(f"  ! could not run sccache {' '.join(args)}: {e}")

    def _ensure_llvm(self):
        """Always prefer LLVM from .toolchains/llvm for bindgen/ffigen.

        Must run BEFORE generate_bridge() — ffigen needs libclang from the
        portable LLVM 15.0.6 tree. Without it (or without host headers on
        CPATH), the codegen emits dummy/broken Dart bindings.

        Policy: if `.toolchains/llvm` is installed, it ALWAYS wins over any
        pre-existing LIBCLANG_PATH / system clang so builds stay reproducible.
        """
        # Always wire the Apple SDK first on macOS — even if LIBCLANG_PATH is
        # already set, bindgen still needs a sysroot for stdlib.h etc.
        self._ensure_macos_sdk()
        self._llvm_home = None

        # 1) macOS: prefer Xcode's libclang. It matches the installed
        # MacOSX.sdk. Portable LLVM 15.0.6 + Xcode 16/26 headers is what
        # produces ffigen's "unknown type name 'uint8_t'" in resource.h.
        if self._is_macos_host():
            xcode_llvm = self._macos_xcode_llvm_home()
            if xcode_llvm:
                libdir = self._libclang_dir(xcode_llvm)
                if self._has_libclang(libdir):
                    prev = os.environ.get("LIBCLANG_PATH", "")
                    os.environ["LIBCLANG_PATH"] = libdir
                    self._llvm_home = xcode_llvm
                    self._strip_llvm_bin_from_path()
                    self._wire_llvm_ffigen_includes(xcode_llvm)
                    if prev and os.path.abspath(prev) != os.path.abspath(libdir):
                        self.log(f"  · overriding LIBCLANG_PATH ({prev}) "
                                 "with Xcode libclang")
                    self.log(f"  · Xcode LLVM: {xcode_llvm}")
                    self.log(f"  · LIBCLANG_PATH = {libdir}")
                    return

        # 2) Prefer the portable toolchains LLVM when present (Win/Linux,
        #    or macOS without Xcode libclang).
        llvm_home = self._toolchains_llvm_home()
        if llvm_home:
            libdir = self._libclang_dir(llvm_home)
            bindir = os.path.join(llvm_home, "bin")
            prev = os.environ.get("LIBCLANG_PATH", "")
            os.environ["LIBCLANG_PATH"] = libdir
            # On macOS the tarball's clang shadows Apple clang and breaks
            # MacOSX.sdk parses. Windows/Linux still want it on PATH.
            if os.path.isdir(bindir) and not self._is_macos_host():
                path_parts = [
                    p for p in os.environ.get("PATH", "").split(os.pathsep)
                    if p and p != bindir
                ]
                os.environ["PATH"] = os.pathsep.join([bindir] + path_parts)
            elif self._is_macos_host():
                self._strip_llvm_bin_from_path()
            self._llvm_home = llvm_home
            self._wire_llvm_ffigen_includes(llvm_home)
            if prev and os.path.abspath(prev) != os.path.abspath(libdir):
                self.log(f"  · overriding LIBCLANG_PATH ({prev}) with toolchains LLVM")
            self.log(f"  · toolchains LLVM: {llvm_home}")
            self.log(f"  · LIBCLANG_PATH = {libdir}")
            return

        # 3) Fall back to whatever is already on the environment.
        libclang = os.environ.get("LIBCLANG_PATH", "")
        if libclang and self._has_libclang(libclang):
            self._llvm_home = os.path.dirname(libclang.rstrip(os.sep))
            self.log(f"  · LIBCLANG_PATH = {libclang} "
                     f"(no .toolchains/llvm — using existing env)")
            return
        if libclang and os.path.isdir(libclang):
            self.log(f"  ! LIBCLANG_PATH={libclang} does not contain libclang")

        # 4) System clang only as a last resort (and warn if not 15.0.6).
        clang = shutil.which("clang", path=self._effective_path())
        if clang:
            try:
                vout = subprocess.check_output(
                    [clang, "--version"], timeout=10,
                    encoding="utf-8", errors="replace").strip()
                self.log(f"  · system clang: {vout.splitlines()[0]}")
                if "15.0.6" not in vout:
                    self.log("  ! WARNING: LLVM 15.0.6 is pinned by the official "
                             "CI. Install it via the Toolchain tab into "
                             ".toolchains/llvm so builds always use that copy.")
            except Exception:
                pass
        else:
            self.log("  ! clang not found — install LLVM 15.0.6 via the "
                     "Toolchain tab (.toolchains/llvm).")

    def _parse_flutter_version(self, text):
        """Return (major, minor, patch) from `flutter --version` output, or None."""
        m = re.search(r"Flutter\s+(\d+)\.(\d+)\.(\d+)", text or "")
        if not m:
            return None
        return tuple(int(x) for x in m.groups())

    def _parse_dart_version(self, text):
        """Return (major, minor, patch) for the Dart SDK Flutter ships."""
        m = re.search(r"Dart\s+(\d+)\.(\d+)\.(\d+)", text or "")
        if not m:
            return None
        return tuple(int(x) for x in m.groups())

    def _flutter_version_output(self, exe):
        """Return (ok, stdout) for `flutter --version`."""
        try:
            r = subprocess.run(
                [exe, "--version"],
                timeout=90, capture_output=True,
                encoding="utf-8", errors="replace",
                env={**os.environ, "PATH": self._effective_path()},
            )
            out = (r.stdout or "") + ("\n" + r.stderr if r.stderr else "")
            return r.returncode == 0, out
        except Exception as e:
            return False, str(e)

    def _ensure_flutter(self):
        """Use Flutter 3.24.5+ (Dart >= 3.5.0) before any `flutter pub get`.

        flutter_hbb depends on extended_text 14.0.0, which requires
        SDK >=3.5.0 <4.0.0. An older Homebrew/system Flutter on PATH is
        the usual cause of "version solving failed". Prefer the portable
        .toolchains copy, but accept any runnable 3.24+ Flutter.
        """
        self.log("\n=== Flutter SDK ===")
        root = self._project_root()
        exe_name = "flutter.bat" if _platform.system() == "Windows" else "flutter"
        candidates = []
        home = toolchains.find_flutter_home(root)
        if home:
            toolchains.repair_flutter_permissions(home, self.log)
            pinned = os.path.join(home, "bin", exe_name)
            if os.path.isfile(pinned):
                candidates.append(pinned)
                self.log(f"  · toolchains Flutter: {home}")
        else:
            self.log("  · no .toolchains/flutter — using whatever flutter is on PATH")
        seen = {os.path.normcase(os.path.abspath(c)) for c in candidates}
        for extra in (
            shutil.which("flutter", path=self._effective_path()),
            shutil.which("flutter"),
        ):
            if not extra:
                continue
            key = os.path.normcase(os.path.abspath(extra))
            if key not in seen:
                seen.add(key)
                candidates.append(extra)

        chosen, out = None, ""
        for exe in candidates:
            ok, text = self._flutter_version_output(exe)
            if not ok:
                self.log(f"  ! not runnable: {exe}")
                if text:
                    self.log(f"    {text.strip().splitlines()[0]}")
                continue
            dart_ver = self._parse_dart_version(text)
            flutter_ver = self._parse_flutter_version(text)
            too_old = ((dart_ver is not None and dart_ver < (3, 5, 0))
                       or (dart_ver is None and flutter_ver is not None
                           and flutter_ver < (3, 24, 0)))
            if too_old:
                shown = ("Dart " + ".".join(str(x) for x in dart_ver)
                         if dart_ver else text.strip().splitlines()[0])
                self.log(f"  ! too old ({shown}): {exe}")
                continue
            chosen, out = exe, text
            break

        if not chosen:
            raise RuntimeError(
                "No Flutter with Dart >= 3.5.0 found. flutter_hbb depends on "
                f"extended_text 14.0.0, which needs that SDK. Install Flutter "
                f"{FLUTTER_VERSION} from the Toolchain tab and rebuild.")

        bindir = os.path.dirname(os.path.abspath(chosen))
        self._flutter_bindir = bindir
        rest = [p for p in os.environ.get("PATH", "").split(os.pathsep)
                if p and os.path.normcase(p) != os.path.normcase(bindir)]
        os.environ["PATH"] = os.pathsep.join([bindir] + rest)
        flutter_root = os.path.dirname(bindir)
        if os.path.isdir(flutter_root):
            os.environ["FLUTTER_ROOT"] = flutter_root

        first = next((ln.strip() for ln in out.splitlines() if ln.strip()), "")
        if first:
            self.log(f"  · {first}")
        dart_line = next((ln.strip() for ln in out.splitlines()
                          if "Dart" in ln), "")
        if dart_line and dart_line != first:
            self.log(f"  · {dart_line}")
        self.log(f"  · flutter = {chosen}")
        flutter_ver = self._parse_flutter_version(out)
        if flutter_ver and flutter_ver[:2] != (3, 24):
            self.log(f"  ! warning: official CI pins Flutter {FLUTTER_VERSION}; "
                     "other 3.x versions may hit widget/engine mismatches")

    def _bridge_header_paths(self):
        mac = os.path.join(self.src_dir, "flutter", "macos", "Runner",
                           "bridge_generated.h")
        ios = os.path.join(self.src_dir, "flutter", "ios", "Runner",
                           "bridge_generated.h")
        return mac, ios

    def _copy_mac_bridge_header_to_ios(self):
        mac_h, ios_h = self._bridge_header_paths()
        if os.path.isfile(mac_h):
            os.makedirs(os.path.dirname(ios_h), exist_ok=True)
            shutil.copy2(mac_h, ios_h)

    def _bridge_outputs_fresh(self):
        """True if rust/dart/C outputs exist and are not older than flutter_ffi.rs.

        The C header is a required Xcode input (SWIFT_OBJC_BRIDGING_HEADER).
        Checking only rust+dart let a kept compile cache skip codegen after
        git clean deleted the gitignored header.
        """
        rust_in = os.path.join(self.src_dir, "src", "flutter_ffi.rs")
        rust_out = os.path.join(self.src_dir, "src", "bridge_generated.rs")
        dart_out = os.path.join(self.src_dir, "flutter", "lib",
                                "generated_bridge.dart")
        mac_h, _ = self._bridge_header_paths()
        needed = (rust_in, rust_out, dart_out, mac_h)
        if not all(os.path.isfile(p) for p in needed):
            return False
        try:
            src_m = os.path.getmtime(rust_in)
            return all(os.path.getmtime(p) >= src_m
                       for p in (rust_out, dart_out, mac_h))
        except OSError:
            return False

    def generate_bridge(self):
        self.log("\n=== 2. Generate flutter_rust_bridge ===")
        # Re-assert toolchains LLVM right before codegen so a later step
        # cannot have clobbered LIBCLANG_PATH.
        self._ensure_llvm()
        if self._bridge_outputs_fresh():
            self.log("  · bridge files already up to date — skip codegen")
            if not self.dry_run:
                self._copy_mac_bridge_header_to_ios()
            return
        # Mirrors the generate-bridge job. cargo installs the codegen binary into
        # ~/.cargo/bin, which run() puts on PATH so it resolves on Windows too.
        self._ensure_cargo_install("flutter_rust_bridge_codegen", "1.80.1",
                                  extra=["--features", "uuid"])
        # Official CI also installs cargo-expand; codegen uses it to expand
        # macros in flutter_ffi.rs before emitting bridge_generated.rs.
        self._ensure_cargo_install("cargo-expand", "1.0.95")
        # ffigen (invoked by the codegen) needs .dart_tool/package_config.json,
        # which only exists after `flutter pub get`. Without it the codegen
        # emits dummy code with an unresolvable Dart_Handle type and the build
        # fails with E0412.
        flutter_dir = os.path.join(self.src_dir, "flutter")
        self.run(["flutter", "pub", "get"], cwd=flutter_dir, check=True)
        pkg_config = os.path.join(flutter_dir, ".dart_tool", "package_config.json")
        if not os.path.isfile(pkg_config):
            raise RuntimeError(
                "flutter pub get did not create .dart_tool/package_config.json; "
                "bridge codegen would emit dummy bindings")
        codegen = shutil.which("flutter_rust_bridge_codegen", path=self._effective_path())
        if not codegen and not self.dry_run:
            raise RuntimeError(
                "flutter_rust_bridge_codegen not found after cargo install")
        cmd = [codegen or "flutter_rust_bridge_codegen",
               "--rust-input", "./src/flutter_ffi.rs",
               "--dart-output", "./flutter/lib/generated_bridge.dart",
               "--c-output", "./flutter/macos/Runner/bridge_generated.h"]
        # ffigen does NOT honour LIBCLANG_PATH — it searches --llvm-path.
        llvm_home = getattr(self, "_llvm_home", None) or self._toolchains_llvm_home()
        if llvm_home and os.path.isdir(llvm_home):
            cmd += ["--llvm-path", llvm_home]
            self.log(f"  · passing --llvm-path {llvm_home} to codegen")
        else:
            self.log("  ! no LLVM home for --llvm-path; ffigen may "
                     "produce broken bindings (typedef bool poison / dummy code)")
        opts = self._macos_llvm_compiler_opts() if self._is_macos_host() else ""
        if opts:
            # Equals-form so clap cannot parse "-isysroot"/"--sysroot=..." as
            # a new flag (that was "unexpected argument '-i' found").
            cmd += [f"--llvm-compiler-opts={opts}"]
            self.log(f"  · --llvm-compiler-opts={opts}")
        # Pass CPATH/C_INCLUDE_PATH only to the codegen subprocess so ffigen
        # can find stdbool.h etc.  These must NOT leak into the global env —
        # Clang-specific headers break GCC compilations of zstd-sys / ring.
        # On macOS we rely on --sysroot instead: a CPATH that lists LLVM 15's
        # resource dir first is exactly what triggers uint8_t-in-resource.h.
        codegen_env = {}
        ffigen_cpath = getattr(self, "_ffigen_cpath", "")
        if ffigen_cpath and not self._is_macos_host():
            codegen_env["CPATH"] = ffigen_cpath
            codegen_env["C_INCLUDE_PATH"] = ffigen_cpath
        self.run(cmd, cwd=self.src_dir, check=True, env=codegen_env)
        rust_bridge = os.path.join(self.src_dir, "src", "bridge_generated.rs")
        dart_bridge = os.path.join(self.src_dir, "flutter", "lib",
                                   "generated_bridge.dart")
        if not self.dry_run:
            if not os.path.isfile(rust_bridge):
                raise RuntimeError(
                    "codegen did not write src/bridge_generated.rs — "
                    "cargo would fail with E0583 (module `bridge_generated`) "
                    "and EventToUI: IntoIntoDart")
            if not os.path.isfile(dart_bridge):
                raise RuntimeError(
                    "codegen did not write flutter/lib/generated_bridge.dart")
            mac_h, _ = self._bridge_header_paths()
            if not os.path.isfile(mac_h):
                raise RuntimeError(
                    "codegen did not write flutter/macos/Runner/bridge_generated.h — "
                    "Xcode would fail with 'Build input file cannot be found'")
            self._copy_mac_bridge_header_to_ios()
            self.log(f"  ✓ {os.path.relpath(rust_bridge, self.src_dir)}")

    def customize_for(self, platform):
        if self.dry_run:
            self.log(f"  (would apply {platform} customizations to {self.src_dir})")
            return
        env = self._env()
        customize.apply(self.src_dir, platform, env, self.patches_dir, log=self.log)

    def _env(self):
        from . import config_gen
        return config_gen.build_custom_env(self.config)

    def _py(self):
        """The Python that build.py should run under — guaranteed to exist."""
        return sys.executable or "python"

    def _bash(self):
        """bash for running RustDesk's NDK shell scripts (Git Bash on Windows)."""
        b = shutil.which("bash", path=self._effective_path())
        if b:
            return b
        # On Windows, Git ships bash but usually only git.exe is on PATH. Derive
        # bash from git's location: <Git>\cmd\git.exe -> <Git>\bin\bash.exe.
        git = shutil.which("git", path=self._effective_path())
        if git:
            gitroot = os.path.dirname(os.path.dirname(git))  # up from cmd/ or bin/
            for cand in (os.path.join(gitroot, "bin", "bash.exe"),
                         os.path.join(gitroot, "usr", "bin", "bash.exe")):
                if os.path.isfile(cand):
                    return cand
        # common install locations as a last resort
        for cand in (r"C:\Program Files\Git\bin\bash.exe",
                     r"C:\Program Files (x86)\Git\bin\bash.exe"):
            if os.path.isfile(cand):
                return cand
        return None

    # ---- native deps (vcpkg) ---------------------------------------------
    VCPKG_COMMIT = "120deac3062162151622ca4860575a33844ba10b"

    def _cargo_bin_version_text(self, name):
        """`name --version` stdout, or empty if the binary is missing."""
        exe = shutil.which(name, path=self._effective_path())
        if not exe:
            return ""
        try:
            r = subprocess.run(
                [exe, "--version"],
                capture_output=True, text=True, timeout=20,
                encoding="utf-8", errors="replace",
                env={**os.environ, "PATH": self._effective_path()},
            )
            return ((r.stdout or "") + " " + (r.stderr or "")).strip()
        except Exception:
            return ""

    def _ensure_cargo_install(self, name, version, extra=None):
        """`cargo install` only when the pinned version is not already on PATH."""
        shown = self._cargo_bin_version_text(name)
        if shown and version in shown:
            self.log(f"  · {name} {version} already present — skip install")
            return
        cmd = ["cargo", "install", name, "--version", version, "--locked"]
        if extra:
            cmd.extend(extra)
        self.run(cmd, check=False)

    def setup_vcpkg(self, triplet):
        """Check out the pinned vcpkg commit and install RustDesk's native deps
        (ffmpeg, hwcodec, etc.) for `triplet`. Needs VCPKG_ROOT set."""
        root = os.environ.get("VCPKG_ROOT")
        if not root:
            self.log("  ! VCPKG_ROOT not set — skipping vcpkg dep install. "
                     "Set it to your vcpkg checkout so ffmpeg/hwcodec resolve.")
            return
        self.log(f"  vcpkg deps ({triplet}) from {root}")
        vcpkg_exe = os.path.join(root, "vcpkg.exe" if self.host["os"] == "Windows" else "vcpkg")
        rc, head = self._git_capture(["rev-parse", "HEAD"], cwd=root)
        already = (rc == 0 and head and
                   (head.startswith(self.VCPKG_COMMIT) or
                    self.VCPKG_COMMIT.startswith(head)))
        if already and os.path.isfile(vcpkg_exe):
            self.log(f"  · vcpkg already at {self.VCPKG_COMMIT[:8]} — "
                     "skip fetch/bootstrap")
        else:
            self.run(["git", "-C", root, "fetch", "--depth", "1", "origin",
                      self.VCPKG_COMMIT], check=False)
            self.run(["git", "-C", root, "checkout", self.VCPKG_COMMIT], check=False)
            # After switching commits the vcpkg binary is stale — re-bootstrap it.
            bootstrap = os.path.join(root,
                                     "bootstrap-vcpkg.bat" if self.host["os"] == "Windows"
                                     else "bootstrap-vcpkg.sh")
            if os.path.isfile(bootstrap):
                self.log("  · re-bootstrapping vcpkg (stale after checkout)")
                self.run([bootstrap, "-disableMetrics"], cwd=root, check=False)
        # RustDesk's vcpkg.json declares ffmpeg as a "host" dependency.
        # vcpkg installs host deps for the host triplet (default: x64-windows),
        # but hwcodec's build.rs hardcodes x64-windows-static/include.
        # Setting VCPKG_DEFAULT_HOST_TRIPLET to the target triplet ensures
        # ffmpeg headers land in the static triplet directory.
        env = dict(os.environ)
        env["VCPKG_DEFAULT_HOST_TRIPLET"] = triplet
        # Manifest-mode `vcpkg install --triplet X` into a shared installed/
        # root *prunes every other triplet*. Universal macOS installs
        # arm64-osx then x64-osx and the second pass deletes arm64-osx —
        # then cargo --target aarch64-apple-darwin cannot find
        # libavcodec/avcodec.h. Isolate each triplet, then symlink it back
        # to installed/{triplet} where scrap/hwcodec look.
        install_root, pkg_dir = self._vcpkg_isolate_triplet(root, triplet)
        marker = os.path.join(pkg_dir, "include", "libavcodec", "avcodec.h")
        if os.path.isfile(marker):
            self.log(f"  · {triplet} ffmpeg headers already present — skip install")
        else:
            self.run([vcpkg_exe, "install", "--triplet", triplet,
                      f"--x-install-root={install_root}"],
                     cwd=self.src_dir, check=True, env=env)
        self._vcpkg_publish_triplet(root, triplet, pkg_dir)

    def _vcpkg_isolate_triplet(self, vcpkg_root, triplet):
        """Return (x-install-root, {root}/{triplet} package dir) for isolation."""
        iso_parent = os.path.join(vcpkg_root, "installed-triplets", triplet)
        pkg = os.path.join(iso_parent, triplet)
        combined_pkg = os.path.join(vcpkg_root, "installed", triplet)
        os.makedirs(iso_parent, exist_ok=True)
        # One-time migrate: previous combined tree → isolated (keep ffmpeg).
        if (os.path.isdir(combined_pkg) and not os.path.islink(combined_pkg)
                and not os.path.isdir(pkg)):
            try:
                shutil.move(combined_pkg, pkg)
                self.log(f"  · moved installed/{triplet} → "
                         f"installed-triplets/{triplet}/{triplet}")
            except OSError as e:
                self.log(f"  ! could not migrate installed/{triplet}: {e}")
        return iso_parent, pkg

    def _vcpkg_publish_triplet(self, vcpkg_root, triplet, pkg_dir):
        """Make VCPKG_ROOT/installed/{triplet} point at the isolated package."""
        combined = os.path.join(vcpkg_root, "installed")
        dest = os.path.join(combined, triplet)
        if not os.path.isdir(pkg_dir):
            self.log(f"  ! vcpkg did not create {pkg_dir}")
            return
        os.makedirs(combined, exist_ok=True)
        try:
            if os.path.islink(dest) or os.path.isfile(dest):
                os.remove(dest)
            elif os.path.isdir(dest) and not os.path.samefile(dest, pkg_dir):
                shutil.rmtree(dest, ignore_errors=True)
        except OSError:
            pass
        if os.path.exists(dest):
            return
        try:
            os.symlink(pkg_dir, dest)
            self.log(f"  · installed/{triplet} → {pkg_dir}")
        except OSError as e:
            self.log(f"  ! symlink installed/{triplet} failed ({e}); copying")
            try:
                shutil.copytree(pkg_dir, dest, symlinks=True)
            except OSError as e2:
                self.log(f"  ! copy installed/{triplet} failed: {e2}")

    # ---- per-platform builds ---------------------------------------------
    def _find_msbuild(self):
        """Locate MSBuild.exe — PATH first, then vswhere (same idea as prereqs)."""
        for name in ("msbuild", "MSBuild"):
            p = shutil.which(name, path=self._effective_path())
            if p:
                return p
        pf = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        vswhere = os.path.join(pf, "Microsoft Visual Studio", "Installer",
                               "vswhere.exe")
        if not os.path.isfile(vswhere):
            return None
        try:
            out = subprocess.check_output(
                [vswhere, "-latest", "-products", "*",
                 "-requires", "Microsoft.Component.MSBuild",
                 "-find", r"MSBuild\**\Bin\MSBuild.exe"],
                encoding="utf-8", errors="replace", timeout=20).strip()
            if out:
                return out.splitlines()[0].strip()
        except Exception:
            pass
        try:
            out = subprocess.check_output(
                [vswhere, "-latest", "-products", "*",
                 "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                 "-property", "installationPath"],
                encoding="utf-8", errors="replace", timeout=20).strip()
            if out:
                for root, _dirs, files in os.walk(os.path.join(out, "MSBuild")):
                    if "MSBuild.exe" in files:
                        return os.path.join(root, "MSBuild.exe")
        except Exception:
            pass
        return None

    def _prepare_msi_release_exe(self, release, msi_app):
        """Ensure Release/{app}.exe exists for preprocess.py.

        Flutter/CMake often still emit rustdesk.exe even when branding is set.
        preprocess.py runs `{app}.exe --version` / `--build-date` and packages
        under that name, so copy (don't rename) rustdesk.exe when needed so the
        portable packer can still find rustdesk.exe afterwards.
        """
        branded = os.path.join(release, f"{msi_app}.exe")
        if os.path.isfile(branded):
            return branded
        rustdesk = os.path.join(release, "rustdesk.exe")
        if os.path.isfile(rustdesk):
            shutil.copy2(rustdesk, branded)
            self.log(f"  · copied rustdesk.exe -> {msi_app}.exe for MSI")
            return branded
        # last resort: any .exe in Release
        if os.path.isdir(release):
            for name in os.listdir(release):
                if name.lower().endswith(".exe"):
                    src = os.path.join(release, name)
                    shutil.copy2(src, branded)
                    self.log(f"  · copied {name} -> {msi_app}.exe for MSI")
                    return branded
        raise RuntimeError(
            f"no .exe found in {release} to prepare {msi_app}.exe for MSI")

    def _build_windows_msi(self):
        """Build an MSI installer from the Flutter Release output.

        Mirrors the VenimK/tzdm / official workflow:
          1. ensure branded .exe exists in Release
          2. python res/msi/preprocess.py --app-name {app} --version {ver} --arp -d {release}
          3. nuget restore (CustomActions packages.config) + dotnet restore (WiX SDK)
          4. msbuild msi.sln -p:Configuration=Release -p:Platform=x64
        Failures raise — MSI was explicitly requested.
        """
        if self.dry_run:
            self.log("  (would build MSI)")
            return
        if self.host["os"] != "Windows":
            raise RuntimeError("MSI build requires Windows + nuget + msbuild")
        app_name = self.config.get("appname", "RustDesk") or "RustDesk"
        # MSI names can't have spaces — replace with underscores
        msi_app = app_name.replace(" ", "_")
        release = os.path.join(self.src_dir, "flutter", "build", "windows",
                               "x64", "runner", "Release")
        msi_dir = os.path.join(self.src_dir, "res", "msi")
        if not os.path.isdir(msi_dir):
            raise RuntimeError(f"res/msi not found at {msi_dir}")
        if not os.path.isdir(release):
            raise RuntimeError(f"Flutter Release dir not found: {release}")

        self.log("  · building MSI installer…")
        self._prepare_msi_release_exe(release, msi_app)

        # 1. Preprocess: inject app name + version into MSI templates.
        # Pass --version so we don't rely solely on running the exe (still need
        # --build-date from the binary, which is why the branded exe must exist).
        dist_arg = os.path.relpath(release, msi_dir)
        rc = self.run(
            [self._py(), "preprocess.py",
             "--app-name", msi_app,
             "--version", self.version,
             "--arp",
             "-d", dist_arg],
            cwd=msi_dir, check=False)
        if rc != 0:
            raise RuntimeError(
                f"MSI preprocess.py failed (exit {rc}). "
                f"Check that {msi_app}.exe exists under Release and responds "
                f"to --version / --build-date.")

        # 2. Ensure nuget.org is registered (Chocolatey nuget often ships with
        # zero sources — restore then fails with "Unable to find version …").
        nuget = shutil.which("nuget", path=self._effective_path()) or "nuget"
        prereqs.ensure_nuget_org(nuget, log=self.log)

        # 2a. Restore packages.config (CustomActions) into res/msi/packages
        ca = os.path.join(msi_dir, "CustomActions")
        if os.path.isfile(os.path.join(ca, "packages.config")):
            rc = self.run([nuget, "restore", "packages.config",
                           "-PackagesDirectory",
                           os.path.join(msi_dir, "packages")],
                          cwd=ca, check=False)
            if rc != 0:
                raise RuntimeError(
                    f"nuget restore of CustomActions packages failed (exit {rc}). "
                    f"Is nuget.org reachable?")

        # 2b. Restore the solution (WiX PackageReference + any remaining)
        rc = self.run([nuget, "restore", "msi.sln",
                       "-PackagesDirectory", "packages"],
                      cwd=msi_dir, check=False)
        if rc != 0:
            self.log(f"  · nuget restore msi.sln exit {rc} "
                     f"(may be OK if packages.config already restored)")

        # 2c. Restore WiX SDK-style Package project (needs .NET SDK / dotnet)
        pkg_proj = os.path.join(msi_dir, "Package", "Package.wixproj")
        # Prefer a fresh PATH so a just-installed .NET SDK is visible even if
        # this process started before the install.
        path_now = os.environ.get("PATH", "")
        for extra in (
            r"C:\Program Files\dotnet",
            os.path.expandvars(r"%ProgramFiles%\dotnet"),
        ):
            if extra and os.path.isdir(extra) and extra not in path_now:
                path_now = extra + os.pathsep + path_now
                os.environ["PATH"] = path_now
        dotnet = (shutil.which("dotnet", path=self._effective_path())
                  or shutil.which("dotnet"))
        if dotnet and os.path.isfile(pkg_proj):
            rc = self.run([dotnet, "restore", pkg_proj], cwd=msi_dir, check=False)
            if rc != 0:
                raise RuntimeError(
                    f"dotnet restore of Package.wixproj failed (exit {rc}). "
                    f"Need .NET 8+ SDK and nuget.org for WixToolset.Sdk 4.0.5.")
        elif not dotnet:
            raise RuntimeError(
                "dotnet not found — WiX Toolset SDK 4.x (Package.wixproj) "
                "requires the .NET SDK. Install .NET 8 SDK, re-open the app, "
                "and re-run the MSI build.")

        # 3. Build the MSI
        msbuild = self._find_msbuild()
        if not msbuild:
            raise RuntimeError(
                "MSBuild not found. Install VS Build Tools (Desktop C++ / MSBuild) "
                "or add MSBuild to PATH. Prereqs use vswhere; this step does too.")
        self.log(f"  · MSBuild: {msbuild}")
        rc = self.run([msbuild, "msi.sln",
                       "-p:Configuration=Release", "-p:Platform=x64",
                       "/p:TargetVersion=Windows10",
                       "/m"],
                      cwd=msi_dir, check=False)
        if rc != 0:
            raise RuntimeError(
                f"MSI msbuild failed (exit {rc}). Common causes: missing "
                f"WixToolset.Sdk 4.0.5 (install .NET SDK + `dotnet restore`), "
                f"or missing WixToolset.DUtil/WcaUtil under res/msi/packages.")

        # 4. Collect the MSI
        msi_src = os.path.join(msi_dir, "Package", "bin", "x64", "Release",
                               "en-us", "Package.msi")
        if not os.path.isfile(msi_src):
            # WiX sometimes drops the MSI one level up
            alt = os.path.join(msi_dir, "Package", "bin", "x64", "Release",
                               "Package.msi")
            if os.path.isfile(alt):
                msi_src = alt
        basename = self._output_basename()
        version = self.version
        if os.path.isfile(msi_src):
            os.makedirs(self.out_dir, exist_ok=True)
            msi_dest = os.path.join(self.out_dir, f"{basename}-{version}.msi")
            shutil.copy2(msi_src, msi_dest)
            self.artifacts.append(msi_dest)
            self.log(f"  ✓ artifact: {msi_dest}")
        else:
            raise RuntimeError(
                f"MSI not found after build (expected {msi_src}). "
                f"See msbuild output above.")

    def _windows_long_paths_enabled(self):
        """Best-effort read of HKLM\\...\\LongPathsEnabled (0 if unknown)."""
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\FileSystem",
            ) as key:
                val, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
                return int(val) == 1
        except Exception:
            return False

    def _ensure_windows_short_src(self):
        """Re-expose rustdesk-src via a short junction when paths risk MAX_PATH.

        Flutter's MSBuild step writes .tlog files under deep plugin dirs
        (e.g. flutter_gpu_texture_renderer_plugin...). With a workspace under
        C:\\Users\\...\\Downloads\\... those paths exceed the classic 260-char
        limit and fail with MSB3491 — even when LongPathsEnabled is on, some
        MSBuild tasks still choke.

        Junctions must be on the *same volume* as the real source (mklink /J
        cannot cross drives). We therefore pick ``<drive>:\\rdlb`` based on
        where the project actually lives, not hard-coded C:\\ — so installs on
        D:\\, E:\\, etc. get a stable short path of the same shape on every box:
        ``X:\\rdlb\\...``.
        """
        if self.host["os"] != "Windows" or self.dry_run:
            return
        # Observed failing relative path (+ a little headroom for hash suffixes)
        worst_rel = os.path.join(
            "flutter", "build", "windows", "x64", "plugins",
            "flutter_gpu_texture_renderer",
            "flutter_gpu_texture_renderer_plugin.dir", "Release",
            "flutter_.XXXXXXXX.tlog",
            "flutter_gpu_texture_renderer_plugin.lastbuildstate",
        )
        projected = len(os.path.join(self.src_dir, worst_rel))
        # When Win32 long paths are off, be more aggressive — MSBuild/linkers
        # fail well below the theoretical 260 once you add intermediate names.
        long_ok = self._windows_long_paths_enabled()
        threshold = 250 if long_ok else 220
        if projected < threshold:
            return

        real = os.path.abspath(self.src_dir)
        drive, _ = os.path.splitdrive(real)
        # Same-volume only. Prefer a fixed short name so every machine that
        # installs DVForge on that drive sees the same build path prefix.
        if not drive:
            drive = "C:"
        candidates = [
            os.path.join(drive + os.sep, "rdlb"),
            os.path.join(drive + os.sep, "rdlb-src"),
            os.path.join(drive + os.sep, "r"),
            # Last resort on system drive if project is elsewhere (rare; will
            # only work if somehow same volume — kept for back-compat).
            r"C:\rdlb",
            r"C:\rdlb-src",
        ]
        # De-dupe while preserving order
        seen = set()
        uniq = []
        for c in candidates:
            key = os.path.normcase(os.path.normpath(c))
            if key not in seen:
                seen.add(key)
                uniq.append(os.path.normpath(c))
        candidates = uniq

        short = None
        created_new_junction = False
        for cand in candidates:
            try:
                # Junctions cannot cross volumes — skip candidates on another drive.
                cand_drive = os.path.splitdrive(os.path.abspath(cand))[0]
                real_drive = os.path.splitdrive(real)[0]
                if cand_drive and real_drive and (
                        os.path.normcase(cand_drive) != os.path.normcase(real_drive)):
                    continue
                if os.path.isdir(cand):
                    try:
                        if os.path.samefile(cand, real):
                            short = cand
                            break
                    except OSError:
                        pass
                    # Existing dir/junction pointing elsewhere — try to drop
                    # it if it's a junction (rmdir removes the link only).
                    try:
                        os.rmdir(cand)
                    except OSError:
                        continue  # occupied; try next candidate
                # Create directory junction (no admin required, same volume).
                self.log(f"  · source path too long for MSBuild "
                         f"(~{projected} chars projected; limit 260"
                         f"{'' if long_ok else '; LongPathsEnabled=0'})")
                self.log(f"  · creating junction {cand} -> {real}")
                rc = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", cand, real],
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                )
                if rc.returncode != 0:
                    err = (rc.stderr or rc.stdout or "").strip()
                    self.log(f"  ! mklink {cand} failed: {err}")
                    continue
                short = cand
                created_new_junction = True
                break
            except Exception as e:
                self.log(f"  ! short-path candidate {cand} failed: {e}")
                continue

        if not short:
            self.log("  ! could not create a short path junction. Options:")
            self.log(f"      1) Install/move DVForge near the drive root "
                     f"(e.g. {drive}\\DVForge) so build paths stay short")
            self.log("      2) Enable Win32 long paths (admin, reboot may be needed):")
            self.log("         New-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet"
                     "\\Control\\FileSystem' -Name LongPathsEnabled -Value 1 "
                     "-PropertyType DWORD -Force")
            return

        if os.path.normcase(os.path.abspath(self.src_dir)) != os.path.normcase(short):
            self.src_dir = short
            self.log(f"  · build will use {short} to stay under MAX_PATH")

        # Only wipe Flutter's Windows intermediates when we just created a
        # new junction. A previous MAX_PATH failure can leave half-written
        # .tlog dirs; an already-good short path should keep the cache.
        if created_new_junction:
            flutter_win = os.path.join(self.src_dir, "flutter", "build", "windows")
            if os.path.isdir(flutter_win) and not self.dry_run:
                self.log("  · cleaning flutter/build/windows "
                         "(new short-path junction)")
                try:
                    _force_rmtree(flutter_win)
                except Exception as e:
                    self.log(f"  ! could not clean flutter build dir: {e}")

    def _ensure_windows_flutter_engine(self):
        """Install RustDesk's custom Flutter engine, skipping work when present.

        The zip is cached under workspace/.build-cache so later builds do not
        re-download ~60 MB. If flutter_windows.dll is already newer than the
        zip, the extract/copy step is skipped too.
        """
        if self.host["os"] != "Windows":
            return
        if self.dry_run:
            self.log("  (would install custom Flutter engine if missing)")
            return

        flutter_exe = shutil.which("flutter", path=self._effective_path())
        if not flutter_exe:
            self.log("  ! flutter not on PATH — cannot install custom engine")
            return
        flutter_dir = os.path.dirname(os.path.dirname(flutter_exe))
        engine_dir = os.path.join(flutter_dir, "bin", "cache", "artifacts",
                                  "engine", "windows-x64-release")
        dll = os.path.join(engine_dir, "flutter_windows.dll")
        cached_zip = os.path.join(self._cache_park_dir(), "windows-x64-release.zip")
        src_zip = os.path.join(self.src_dir, "windows-x64-release.zip")

        zip_path = None
        if os.path.isfile(cached_zip):
            zip_path = cached_zip
        elif os.path.isfile(src_zip):
            zip_path = src_zip
            try:
                os.makedirs(os.path.dirname(cached_zip), exist_ok=True)
                shutil.copy2(src_zip, cached_zip)
                zip_path = cached_zip
            except OSError:
                pass

        if os.path.isfile(dll) and zip_path and os.path.isfile(zip_path):
            try:
                if os.path.getmtime(dll) >= os.path.getmtime(zip_path) - 5:
                    self.log("  · custom Flutter engine already in place — skip")
                    return
            except OSError:
                pass
        elif os.path.isfile(dll) and not zip_path:
            # Engine present from a previous build; zip was cleaned. Trust it.
            self.log("  · custom Flutter engine already in place — skip")
            return

        if not os.path.isdir(engine_dir):
            self.log("  · flutter precache --windows (engine dir missing)")
            self.run(["flutter", "precache", "--windows"], check=False)
        else:
            self.log("  · flutter engine dir present — skip precache")

        if not zip_path:
            os.makedirs(os.path.dirname(cached_zip), exist_ok=True)
            self.log("  · downloading custom Flutter engine")
            self.run(["curl", "-sL", "-o", cached_zip, WINDOWS_FLUTTER_ENGINE_URL],
                     check=False)
            zip_path = cached_zip if os.path.isfile(cached_zip) else None
        else:
            self.log(f"  · using cached engine zip ({zip_path})")

        if not zip_path or not os.path.isfile(zip_path):
            self.log("  ! failed to download custom Flutter engine")
            return

        extract_dir = os.path.join(self.src_dir, "windows-x64-release")
        try:
            with zipfile.ZipFile(zip_path, "r") as z:
                z.extractall(extract_dir)
        except zipfile.BadZipFile as e:
            self.log(f"  ! engine zip is corrupt ({e}) — deleting cache")
            try:
                os.remove(zip_path)
            except OSError:
                pass
            return

        if not os.path.isdir(engine_dir):
            self.log(f"  ! engine dir not found: {engine_dir}")
            return
        if os.path.isdir(extract_dir):
            for item in os.listdir(extract_dir):
                src_item = os.path.join(extract_dir, item)
                dst_item = os.path.join(engine_dir, item)
                if os.path.isfile(src_item):
                    if os.path.isfile(dst_item):
                        os.remove(dst_item)
                    shutil.copy2(src_item, dst_item)
            self.log(f"  ✓ custom engine installed to {engine_dir}")

    def _ensure_windows_printer_driver(self, release):
        """Copy official printer driver + adapter DLL into the Flutter Release
        folder so Settings → Printer can install the virtual printer.

        Official CI downloads:
          rustdesk_printer_driver_v4-1.4.zip
            → Release/drivers/RustDeskPrinterDriver/*.inf
          printer_driver_adapter.zip
            → Release/printer_driver_adapter.dll
        Cached under workspace/.build-cache/printer-driver/.
        """
        if self.host["os"] != "Windows":
            return
        if self.dry_run:
            self.log("  (would install printer driver into Release if missing)")
            return
        if not os.path.isdir(release):
            self.log(f"  ! Release dir missing, skip printer driver: {release}")
            return

        dest_drv = os.path.join(release, "drivers", "RustDeskPrinterDriver")
        dest_inf = os.path.join(dest_drv, "RustDeskPrinterDriver.inf")
        dest_dll = os.path.join(release, "printer_driver_adapter.dll")
        if os.path.isfile(dest_inf) and os.path.isfile(dest_dll):
            self.log("  · printer driver already in Release — skip download")
            return

        cache = os.path.join(self._cache_park_dir(), "printer-driver")
        os.makedirs(cache, exist_ok=True)
        drv_zip = os.path.join(cache, "rustdesk_printer_driver_v4-1.4.zip")
        adp_zip = os.path.join(cache, "printer_driver_adapter.zip")

        def _fetch(url, path, label):
            if os.path.isfile(path) and os.path.getsize(path) > 1000:
                self.log(f"  · using cached {label}")
                return True
            self.log(f"  · downloading {label}")
            self.run(["curl", "-sL", "-o", path, url], check=False)
            if not os.path.isfile(path) or os.path.getsize(path) < 1000:
                self.log(f"  ! failed to download {label} from {url}")
                return False
            return True

        if not _fetch(PRINTER_DRIVER_ZIP_URL, drv_zip, "printer driver zip"):
            return
        if not _fetch(PRINTER_ADAPTER_ZIP_URL, adp_zip, "printer adapter zip"):
            return

        extract_root = os.path.join(cache, "extract")
        if os.path.isdir(extract_root):
            try:
                _force_rmtree(extract_root)
            except Exception:
                pass
        os.makedirs(extract_root, exist_ok=True)
        try:
            with zipfile.ZipFile(drv_zip, "r") as z:
                z.extractall(extract_root)
            with zipfile.ZipFile(adp_zip, "r") as z:
                z.extractall(extract_root)
        except zipfile.BadZipFile as e:
            self.log(f"  ! printer zip corrupt ({e}) — deleting cache")
            for p in (drv_zip, adp_zip):
                try:
                    os.remove(p)
                except OSError:
                    pass
            return

        inf_src = None
        dll_src = None
        for root, _dirs, files in os.walk(extract_root):
            for f in files:
                low = f.lower()
                if low == "rustdeskprinterdriver.inf":
                    inf_src = os.path.join(root, f)
                elif low == "printer_driver_adapter.dll":
                    dll_src = os.path.join(root, f)

        if not inf_src:
            self.log("  ! RustDeskPrinterDriver.inf not found in driver zip")
            return

        os.makedirs(dest_drv, exist_ok=True)
        src_dir = os.path.dirname(inf_src)
        for name in os.listdir(src_dir):
            src_p = os.path.join(src_dir, name)
            dst_p = os.path.join(dest_drv, name)
            if os.path.isfile(src_p):
                shutil.copy2(src_p, dst_p)
        if dll_src:
            shutil.copy2(dll_src, dest_dll)
        if os.path.isfile(dest_inf):
            self.log(f"  ✓ printer driver → {dest_drv}")
        else:
            self.log("  ! copied driver files but INF still missing")
        if os.path.isfile(dest_dll):
            self.log(f"  ✓ printer adapter → {dest_dll}")
        else:
            self.log("  ! printer_driver_adapter.dll not found in adapter zip")

    def _cached_windows_binary_name(self):
        """Exe/CMake target name baked into a previous flutter/build/windows."""
        cache = os.path.join(self.src_dir, "flutter", "build", "windows",
                             "x64", "CMakeCache.txt")
        if not os.path.isfile(cache):
            cache = os.path.join(self.src_dir, "flutter", "build", "windows",
                                 "CMakeCache.txt")
        if os.path.isfile(cache):
            try:
                text = open(cache, encoding="utf-8", errors="replace").read()
            except OSError:
                text = ""
            m = re.search(r"TARGET_FILE_DIR:([A-Za-z0-9_.-]+)", text)
            if m:
                return m.group(1)
        runner = os.path.join(self.src_dir, "flutter", "build", "windows",
                              "x64", "runner")
        if os.path.isdir(runner):
            skip = {"ALL_BUILD.vcxproj", "ZERO_CHECK.vcxproj",
                    "INSTALL.vcxproj", "RUN_TESTS.vcxproj"}
            for name in os.listdir(runner):
                if name.endswith(".vcxproj") and name not in skip:
                    return os.path.splitext(name)[0]
        return ""

    def _cached_linux_binary_name(self):
        """Name of the executable already sitting in the Flutter Linux bundle."""
        bundle = self._linux_bundle_dir()
        if not bundle or not os.path.isdir(bundle):
            return ""
        for name in os.listdir(bundle):
            if name.endswith(".so") or name.endswith(".dat"):
                continue
            path = os.path.join(bundle, name)
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return name
        return ""

    def _invalidate_stale_flutter_linux(self):
        """Drop flutter/build/linux when CMake BINARY_NAME changed."""
        if self.dry_run:
            return
        want = customize._linux_bin_name(self._output_basename())
        have = self._cached_linux_binary_name()
        if not have:
            return
        if have == want:
            self.log(f"  · flutter linux cache matches BINARY_NAME={want}")
            return
        flutter_linux = os.path.join(self.src_dir, "flutter", "build", "linux")
        self.log(f"  · linux binary renamed ({have} -> {want}) — "
                 "clearing flutter/build/linux (cargo cache kept)")
        try:
            _force_rmtree(flutter_linux)
        except Exception as e:
            self.log(f"  ! could not clear flutter linux cache: {e}")

    def _invalidate_stale_flutter_windows(self):
        """Drop flutter/build/windows when the CMake target name changed.

        Config fields like password/server still incrementally compile. Only
        an app/exe rename (BINARY_NAME) poisons CMakeCache.txt:
          $<TARGET_FILE_DIR:onmac>  with no target "onmac"
        Cargo's target/ cache is left alone.
        """
        if self.dry_run:
            return
        want = (self._output_basename() or "rustdesk").strip()
        if want.lower().endswith(".exe"):
            want = want[:-4]
        have = self._cached_windows_binary_name()
        if not have:
            return
        if have.lower() == want.lower():
            self.log(f"  · flutter windows cache matches BINARY_NAME={want}")
            return
        flutter_win = os.path.join(self.src_dir, "flutter", "build", "windows")
        self.log(f"  · exe name changed ({have} -> {want}) — "
                 "clearing flutter/build/windows (cargo cache kept)")
        try:
            _force_rmtree(flutter_win)
        except Exception as e:
            self.log(f"  ! could not clear flutter windows cache: {e}")
        # Ephemeral plugin files were generated against the old target too.
        ephemeral = os.path.join(self.src_dir, "flutter", "windows",
                                 "flutter", "ephemeral")
        if os.path.isdir(ephemeral):
            try:
                _force_rmtree(ephemeral)
            except Exception as e:
                self.log(f"  ! could not clear flutter ephemeral: {e}")

    def build_windows(self):
        self.log("\n=== Build Windows x86_64 ===")
        # Always use the MSVC toolchain — the official CI pins
        # x86_64-pc-windows-msvc. The GNU target requires gcc.exe (MinGW)
        # which most Windows dev setups don't have.
        if self.host["os"] == "Windows":
            # Pin Rust 1.75 — matches official CI. Rust 1.78+ has an i128
            # ABI change that breaks sciter and other deps.
            # https://blog.rust-lang.org/2024/03/30/i128-layout-update.html
            # _ensure_rust() already did this when the toolchain is missing.
            rustc = shutil.which("rustc", path=self._effective_path())
            rustc_ver = ""
            if rustc:
                try:
                    rustc_ver = subprocess.check_output(
                        [rustc, "--version"], timeout=15,
                        encoding="utf-8", errors="replace",
                        env={**os.environ, "PATH": self._effective_path()},
                    )
                except Exception:
                    rustc_ver = ""
            if RUST_VERSION not in (rustc_ver or ""):
                self.run(["rustup", "toolchain", "install",
                          f"{RUST_VERSION}-x86_64-pc-windows-msvc"], check=False)
                self.run(["rustup", "target", "add",
                          "x86_64-pc-windows-msvc",
                          "--toolchain", f"{RUST_VERSION}-x86_64-pc-windows-msvc"],
                         check=False)
                self.run(["rustup", "default",
                          f"{RUST_VERSION}-x86_64-pc-windows-msvc"], check=False)

            # LLVM was already set up before generate_bridge — just confirm.
            self._ensure_llvm()
            # Must run before Flutter/MSBuild — deep plugin .tlog paths exceed 260.
            self._ensure_windows_short_src()

        self.setup_vcpkg("x64-windows-static")
        self.customize_for("windows")
        # App/exe rename leaves CMakeCache.txt with $<TARGET_FILE_DIR:oldname>.
        # Wipe only the Flutter Windows CMake tree — cargo target/ stays.
        self._invalidate_stale_flutter_windows()

        # Patch Flutter dropdown (from official CI)
        dropdown_patch = os.path.join(self.patches_dir,
                                      "flutter_3.24.4_dropdown_menu_enableFilter.diff")
        if os.path.isfile(dropdown_patch):
            self.log("  · patching Flutter dropdown menu")
            # Find the Flutter SDK directory
            flutter_exe = shutil.which("flutter", path=self._effective_path())
            if flutter_exe:
                flutter_dir = os.path.dirname(os.path.dirname(flutter_exe))
                self.run(["git", "apply", dropdown_patch],
                         cwd=flutter_dir, check=False)

        # Replace Flutter engine with RustDesk custom build (from official CI)
        self._ensure_windows_flutter_engine()

        win_targets = [t for t in self.target_ids if t.startswith("windows-")]
        wants_exe = "windows-x86_64-exe" in win_targets
        wants_msi = "windows-x86_64-msi" in win_targets
        # Default: if neither is explicitly selected, build exe (back-compat)
        if not wants_exe and not wants_msi:
            wants_exe = True

        # build.py always runs the Flutter build. Pass --skip-portable-pack
        # so build.py does NOT pack the portable exe itself — packing must
        # happen AFTER we write custom_.txt into `release` below, otherwise
        # the packed exe is built from a Release folder that doesn't have
        # custom_.txt yet and ships with no baked-in branding/password/perms.
        build_args = [self._py(), "build.py", "--hwcodec", "--flutter", "--vram",
                      "--skip-portable-pack"]
        self.run(build_args, cwd=self.src_dir)

        release = os.path.join(self.src_dir, "flutter", "build", "windows",
                               "x64", "runner", "Release")
        # category B: base64 custom_.txt next to the binary — must land in
        # `release` BEFORE the portable packer runs (see comment above).
        if not self.dry_run:
            env = self._env()
            customize.write_custom_txt(release, env, log=self.log)
            self._ensure_windows_printer_driver(release)
            self._install_open_printer_adapter(release)

        basename = self._output_basename()
        version = self.version

        # Build and collect MSI installer if requested
        if wants_msi:
            self._build_windows_msi()

        # Pack + collect the portable exe if requested
        if wants_exe:
            self._pack_windows_portable(release, version)
            # Packer writes next to src (may be a short junction like C:\rdlb).
            # Also probe the real workspace tree in case paths diverged.
            candidates = [
                os.path.join(self.src_dir, f"{basename}-{version}-install.exe"),
                os.path.join(self.src_dir, f"rustdesk-{version}-install.exe"),
                os.path.join(self.workspace, "rustdesk-src",
                             f"{basename}-{version}-install.exe"),
                os.path.join(self.workspace, "rustdesk-src",
                             f"rustdesk-{version}-install.exe"),
            ]
            portable_exe = next((p for p in candidates if os.path.isfile(p)), None)
            if portable_exe:
                os.makedirs(self.out_dir, exist_ok=True)
                dest = os.path.join(
                    self.out_dir, f"{basename}-{version}-install.exe")
                shutil.copy2(portable_exe, dest)
                self.artifacts.append(dest)
                self.log(f"  ✓ artifact: {dest}")
            else:
                self.log("  ! portable exe not found — portable pack may have failed")
            # Also copy the Release directory as a fallback (loose files)
            self._collect_dir(release, "windows", "Release")

    def _install_open_printer_adapter(self, release):
        """Build printer-adapter/ and install it over RustDesk's adapter.

        RustDesk's own `printer_driver_adapter.dll` verifies the calling
        executable's Authenticode signature against a hardcoded vendor
        allow-list (it links `codesign-verify-rs` and imports `WinVerifyTrust`).
        A custom-branded build is never on that list, so its `init()` returns
        non-zero and the server logs:

            printer service init failed: Failed to init printer driver

        which disables remote printing entirely. `printer-adapter/` is a
        clean-room reimplementation of the same four-function ABI declared in
        `src/server/printer_service.rs`, with no signature check.

        Must run AFTER _ensure_windows_printer_driver() so it replaces the
        downloaded DLL, and BEFORE the MSI harvest and portable packer so the
        replacement ships inside both installers.

        Non-fatal: on failure the build keeps RustDesk's adapter and still
        produces a working client, minus remote printing.
        """
        crate = os.path.join(self._project_root(), "printer-adapter")
        manifest = os.path.join(crate, "Cargo.toml")
        if not os.path.isfile(manifest):
            self.log("  ! printer-adapter/ not found - keeping RustDesk's "
                     "signature-checked adapter (remote printing will not work)")
            return

        self.log("\n=== Open printer adapter ===")
        if self.dry_run:
            self.log(f"  [dry-run] would cargo build {manifest}")
            self.log(f"  [dry-run] would install the DLL into {release}")
            return

        rc = self.run(["cargo", "build", "--release", "--locked",
                       "--manifest-path", manifest],
                      cwd=crate, check=False)
        if rc != 0:
            self.log(f"  ! cargo build failed (exit {rc}) - keeping RustDesk's "
                     "adapter; remote printing will not work in this build")
            return

        built = os.path.join(crate, "target", "release",
                             "printer_driver_adapter.dll")
        if not os.path.isfile(built):
            self.log("  ! cargo reported success but the DLL is missing")
            return

        try:
            dst = os.path.join(release, "printer_driver_adapter.dll")
            shutil.copy2(built, dst)
            self.log(f"  ✓ printer_driver_adapter.dll ({os.path.getsize(dst)} "
                     "bytes) - replaces the signature-checked build")
        except OSError as exc:
            self.log(f"  ! could not install the adapter ({exc})")

    def _pack_windows_portable(self, release, version):
        """Run libs/portable/generate.py ourselves (mirrors build.py's own
        build_flutter_windows packing step), now that `release` already has
        custom_.txt written into it — so the packed exe bakes in branding,
        password, and permissions correctly."""
        self.log("\n=== Pack Windows portable (single .exe) ===")
        basename = self._output_basename()
        branded = os.path.join(release, f"{basename}.exe")
        rustdesk = os.path.join(release, "rustdesk.exe")
        if os.path.isfile(branded):
            exe_name = branded
        elif os.path.isfile(rustdesk):
            exe_name = rustdesk
        else:
            exe_name = rustdesk
            for cand in os.listdir(release) if os.path.isdir(release) else []:
                if cand.lower().endswith(".exe"):
                    exe_name = os.path.join(release, cand)
                    break
        self.log(f"  · portable startup exe: {exe_name}")
        portable_dir = os.path.join(self.src_dir, "libs", "portable")
        self.run([self._py(), "-m", "pip", "install", "-r", "requirements.txt"],
                 cwd=portable_dir, check=False)
        self.run([self._py(), "generate.py", "-f", release, "-o", ".",
                  "-e", exe_name], cwd=portable_dir)
        packer_exe = os.path.join(self.src_dir, "target", "release",
                                  "rustdesk-portable-packer.exe")
        dest = os.path.join(self.src_dir, f"{basename}-{version}-install.exe")
        if os.path.isfile(packer_exe):
            shutil.move(packer_exe, dest)
            self.log(f"  ✓ packed portable exe -> {dest}")
        else:
            self.log(f"  ! packer did not produce {packer_exe}")

    def build_linux(self):
        self.log("\n=== Build Linux ===")
        self.setup_vcpkg("x64-linux")
        self.customize_for("linux")
        # BINARY_NAME change poisons CMakeCache the same way Windows does.
        self._invalidate_stale_flutter_linux()
        # base64 custom_.txt staged for build.py + bundle (SKILL.md §4.4)
        if not self.dry_run:
            env = self._env()
            customize.write_custom_txt(self.src_dir, env, log=self.log)
        # build.py auto-detects distro and calls build_flutter_deb on
        # Debian/Ubuntu. That's fine for .deb targets, but for .rpm and
        # .AppImage we need to package after the flutter build completes.
        linux_targets = [t for t in self.target_ids if t.startswith("linux-")]
        wants_deb = any(t in ("linux-x86_64-deb", "linux-aarch64-deb")
                        for t in linux_targets)
        wants_rpm = "linux-x86_64-rpm" in linux_targets
        wants_appimage = "linux-x86_64-appimage" in linux_targets
        # If only .deb is requested, let build.py do its default thing.
        # Otherwise, skip build.py's packaging and do it ourselves.
        if wants_deb and not wants_rpm and not wants_appimage:
            self.run([self._py(), "build.py", "--flutter"],
                     cwd=self.src_dir, check=False)
        else:
            # Run cargo + flutter build without packaging, then package ourselves.
            self._build_linux_core()
            # Write custom_.txt into the flutter bundle BEFORE packaging,
            # since rpm/Arch specs copy from the bundle directory.
            for arch in ("x64", "arm64"):
                bundle = os.path.join(self.src_dir, "flutter", "build", "linux",
                                      arch, "release", "bundle")
                if os.path.isdir(bundle):
                    customize.write_custom_txt(bundle, env, log=self.log)
            # appimage-builder extracts from the .deb, so always build it
            # first when AppImage is requested.
            if wants_deb or wants_appimage:
                self._package_linux_deb()
            if wants_rpm:
                self._package_linux_rpm()
            if wants_appimage:
                self._package_linux_appimage()
        self._collect(self.src_dir, (".deb", ".rpm", ".AppImage", ".flatpak",
                                     ".pkg.tar.zst"), "linux")

    def _build_linux_core(self):
        """Run cargo build + flutter build linux without packaging."""
        features = "flutter"
        if "hwcodec" in self.config.get("features", []):
            features += ",hwcodec"
        self.run(["cargo", "build", "--locked", "--features", features,
                  "--lib", "--release"],
                 cwd=self.src_dir, check=False)
        flutter_dir = os.path.join(self.src_dir, "flutter")
        self.run(["flutter", "build", "linux", "--release"],
                 cwd=flutter_dir, check=False)

    def _linux_bundle_dir(self):
        """Find the flutter linux bundle directory."""
        for arch in ("x64", "arm64"):
            b = os.path.join(self.src_dir, "flutter", "build", "linux",
                             arch, "release", "bundle")
            if os.path.isdir(b):
                return b
        return None

    def _package_linux_deb(self):
        """Package the flutter bundle into a .deb using build.py's logic."""
        self.log("  · packaging .deb")
        # Delegate to build.py's build_flutter_deb by running build.py
        # with --skip-cargo (we already built the lib in _build_linux_core).
        self.run([self._py(), "build.py", "--flutter", "--skip-cargo"],
                 cwd=self.src_dir, check=False)

    def _output_basename(self):
        """The custom file name for output artifacts (e.g. 'myapp-1.4.9').

        Falls back to 'rustdesk' when no custom exename is set, matching
        the upstream build.py behaviour."""
        filename = self.config.get("exename", "") or self.config.get("appname", "") or "rustdesk"
        return filename

    def _package_linux_rpm(self):
        """Package the flutter bundle into .rpm files.

        Uses res/rpm-flutter.spec (Fedora) and res/rpm-flutter-suse.spec
        (openSUSE/SUSE), matching the official RustDesk CI.  These specs
        copy from the Flutter bundle directory, not target/release/rustdesk
        which only exists in sciter builds.
        """
        import glob as _glob
        self.log("  · packaging .rpm")
        version = self.version
        basename = self._output_basename()
        bundle = self._linux_bundle_dir()
        if not bundle:
            self.log("  ! no flutter linux bundle found — skipping .rpm")
            return
        rpm_tool = shutil.which("rpmbuild", path=self._effective_path())
        if not rpm_tool:
            self.log("  ! rpmbuild not found — skipping .rpm")
            return
        # Determine arch and the bundle path segment used in the spec.
        arch = "x86_64"
        arch_seg = "x64"
        if any(t.startswith("linux-aarch64") for t in self.target_ids):
            arch = "aarch64"
            arch_seg = "arm64"

        rpm_env = {"HBB": self.src_dir}
        built = []

        for spec_name, suffix in (
            ("rpm-flutter.spec", ""),
            ("rpm-flutter-suse.spec", "-suse"),
        ):
            spec = os.path.join(self.src_dir, "res", spec_name)
            if not os.path.isfile(spec):
                self.log(f"  ! res/{spec_name} not found — skipping {suffix or 'fedora'} .rpm")
                continue
            # Update version in the spec
            self._sed_i(f"s/Version:    .*/Version:    {version}/g", spec,
                        cwd=self.src_dir, check=False)
            # For aarch64, patch the hardcoded x64 bundle path
            if arch_seg != "x64":
                self._sed_i(f"s|linux/x64|linux/{arch_seg}|g", spec,
                            cwd=self.src_dir, check=False)
            # Build binary RPM only (-bb), matching CI
            self.run([rpm_tool, "-bb", spec],
                     cwd=self.src_dir, check=False,
                     env=rpm_env)
            # Collect the built RPM (rpmbuild always names it rustdesk-*.rpm)
            rpm_glob = os.path.expanduser(
                f"~/rpmbuild/RPMS/{arch}/rustdesk-*.rpm")
            rpms = _glob.glob(rpm_glob)
            if rpms:
                name = f"{basename}-{version}{suffix}.rpm"
                dest = os.path.join(self.src_dir, name)
                shutil.move(rpms[0], dest)
                built.append(dest)
                os.makedirs(self.out_dir, exist_ok=True)
                out = os.path.join(self.out_dir, name)
                shutil.copy2(dest, out)
                self.artifacts.append(out)
                self.log(f"  ✓ created {name}")
                self.log(f"  ✓ artifact: {out}")
            else:
                self.log(f"  ! no .rpm found in ~/rpmbuild/RPMS/{arch}/ for {spec_name}")

    def _package_linux_appimage(self):
        """Package the flutter bundle into an .AppImage using appimage-builder.

        Uses RustDesk's official appimage-builder recipe (appimage/*.yml),
        which extracts the .deb and bundles all shared library dependencies.
        This avoids the FUSE requirement of plain appimagetool, which fails
        in containers/ci.
        """
        self.log("  · packaging .AppImage")
        version = self.version
        basename = self._output_basename()
        # appimage-builder needs a .deb to extract — build it first if we
        # haven't already.  build.py always produces rustdesk-{ver}.deb.
        deb_path = os.path.join(self.src_dir, f"rustdesk-{version}.deb")
        if not os.path.isfile(deb_path):
            self.log("  · .deb not found, building it first for AppImage")
            self._package_linux_deb()
        deb_path = os.path.join(self.src_dir, f"rustdesk-{version}.deb")
        if not os.path.isfile(deb_path):
            self.log("  ! .deb build failed — cannot create AppImage without it")
            return
        # Determine arch from the target
        arch = "x86_64"
        if any(t == "linux-aarch64-deb" for t in self.target_ids):
            arch = "aarch64"
        recipe = os.path.join(self.src_dir, "appimage",
                              f"AppImageBuilder-{arch}.yml")
        if not os.path.isfile(recipe):
            self.log(f"  ! {recipe} not found — skipping .AppImage")
            return
        # Install appimage-builder if not present
        builder = shutil.which("appimage-builder", path=self._effective_path())
        if not builder:
            self.log("  · installing appimage-builder...")
            self.run(["pip3", "install", "setuptools_scm<10"], check=False)
            self.run(["pip3", "install",
                      "git+https://github.com/rustdesk-org/appimage-builder.git"],
                     check=False)
            builder = shutil.which("appimage-builder", path=self._effective_path())
        if not builder:
            self.log("  ! appimage-builder not found — skipping .AppImage")
            return
        # Copy the .deb into the appimage dir (the recipe expects rustdesk.deb)
        appimage_dir = os.path.join(self.src_dir, "appimage")
        shutil.copy2(deb_path, os.path.join(appimage_dir, "rustdesk.deb"))
        # Run appimage-builder
        self.run([builder, "--skip-tests", "--recipe", recipe],
                 cwd=appimage_dir, check=False)
        # Find and move the built AppImage
        import glob as _glob
        pattern = os.path.join(appimage_dir, f"*-{version}-{arch}.AppImage")
        imgs = _glob.glob(pattern)
        if not imgs:
            # fallback: any AppImage in the dir
            imgs = _glob.glob(os.path.join(appimage_dir, "*.AppImage"))
        if imgs:
            dest = os.path.join(self.src_dir, f"{basename}-{version}.AppImage")
            shutil.move(imgs[0], dest)
            self.log(f"  ✓ created {basename}-{version}.AppImage")
        else:
            self.log("  ! AppImage not found after build")

    def _android_gradle_env(self, jdk17=None):
        """JAVA_HOME + ANDROID_SDK_ROOT for flutter build apk / Gradle."""
        env = {}
        if jdk17:
            env["JAVA_HOME"] = jdk17
            env["PATH"] = (os.path.join(jdk17, "bin") + os.pathsep
                           + os.environ.get("PATH", ""))
        sdk = (os.environ.get("ANDROID_SDK_ROOT")
               or os.environ.get("ANDROID_HOME")
               or "")
        if not sdk or not os.path.isdir(sdk):
            cand = os.path.join(self._project_root(), ".toolchains", "android_sdk")
            sdk = cand if os.path.isdir(cand) else ""
        if sdk:
            env["ANDROID_SDK_ROOT"] = sdk
            env["ANDROID_HOME"] = sdk
        return env

    def _accept_android_sdk_licenses(self, jdk17=None):
        """Find the Android SDK and accept all licenses so Gradle doesn't fail."""
        # 1) Detect SDK path from env, local.properties, or common locations
        sdk = (os.environ.get("ANDROID_SDK_ROOT")
               or os.environ.get("ANDROID_HOME")
               or os.environ.get("ANDROID_SDK_HOME"))
        if not sdk:
            local_props = os.path.join(self.src_dir, "flutter", "android",
                                       "local.properties")
            if os.path.isfile(local_props):
                with open(local_props) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("sdk.dir="):
                            sdk = line[len("sdk.dir="):]
                            break
        if not sdk:
            for cand in (
                os.path.join(self._project_root(), ".toolchains", "android_sdk"),
                os.path.expanduser("~/Library/Android/sdk"),
                "/opt/homebrew/share/android-commandlinetools",
                "/opt/android-sdk",
                os.path.expanduser("~/Android/Sdk"),
                os.path.expanduser("~/AppData/Local/Android/Sdk"),
                "C:\\Android\\sdk",
                os.path.join(os.environ.get("LOCALAPPDATA", ""),
                             "Android", "Sdk"),
            ):
                if os.path.isdir(cand):
                    sdk = cand
                    break
        if not sdk or not os.path.isdir(sdk):
            self.log("  ! Android SDK not found — cannot accept licenses. "
                     "Set ANDROID_SDK_ROOT or install Android SDK.")
            return

        # 2) Find sdkmanager (sdkmanager.bat on Windows)
        sdkmanager = None
        is_win = _platform.system() == "Windows"
        exe = ".bat" if is_win else ""
        for pattern in (
            os.path.join(sdk, "cmdline-tools", "latest", "bin", "sdkmanager" + exe),
            os.path.join(sdk, "cmdline-tools", "bin", "sdkmanager" + exe),
            os.path.join(sdk, "tools", "bin", "sdkmanager" + exe),
            # also check without extension (some installs differ)
            os.path.join(sdk, "cmdline-tools", "latest", "bin", "sdkmanager"),
            os.path.join(sdk, "cmdline-tools", "bin", "sdkmanager"),
            os.path.join(sdk, "tools", "bin", "sdkmanager"),
        ):
            if os.path.isfile(pattern):
                sdkmanager = pattern
                break
        if not sdkmanager:
            # Try PATH
            from shutil import which as _which
            sdkmanager = _which("sdkmanager")
        # 3) Write known license files (works even without sdkmanager)
        licenses_dir = os.path.join(sdk, "licenses")
        os.makedirs(licenses_dir, exist_ok=True)
        # Write the known license hashes (Android SDK license acceptance)
        known_licenses = {
            "android-sdk-license": "\n8933bad161af4178b1185d1a37fbf41ea5269c55\n"
                                   "\nd56f5187479451eabf01fb78af6dfcb131a6481e\n"
                                   "24333f8a63b6825ea9c5514f83c2829b004d1fee\n",
            "android-sdk-preview-license": "\n84831b9409646a918e30573bab4c9c91346d8abd\n",
            "intel-android-extra-license": "\nd975f751698a77b662f1254ddbeed3901e976f5a\n",
            "mips-android-sysimage-license": "\ne9acab5b5fbb560a72cfaecce8946896ff6aab9d\n",
            "google-gdk-license": "\n33b6a2b64607f11b759f320ef9dff4ae5c47d97a\n",
        }
        for name, content in known_licenses.items():
            lic_file = os.path.join(licenses_dir, name)
            if not os.path.isfile(lic_file):
                with open(lic_file, "w") as f:
                    f.write(content)
        self.log(f"  ✓ wrote SDK license files to {licenses_dir}")

        # 4) If sdkmanager exists, also run --licenses to accept any new ones
        if not sdkmanager:
            self.log("  ! sdkmanager not found — license files written manually. "
                     "Install cmdline-tools for full SDK management.")
            return

        env = dict(os.environ)
        if jdk17:
            env["JAVA_HOME"] = jdk17
        self.log(f"  · running sdkmanager --licenses ({sdk})")
        try:
            proc = subprocess.run(
                [sdkmanager, "--licenses"],
                cwd=sdk, env=env, check=False,
                input="y\n" * 20,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", timeout=120,
            )
            for line in proc.stdout.splitlines():
                self.log(line)
        except Exception as exc:
            self.log(f"  ! sdkmanager --licenses failed: {exc}")
        self.log("  ✓ Android SDK licenses accepted")

    def _ndk_host_tag(self, ndk_home=""):
        """NDK llvm/prebuilt/<tag> for this host.

        NDK r28c on Apple Silicon may ship darwin-aarch64, darwin-x86_64, or
        both. Prefer the native tag when it exists; otherwise the other
        darwin/linux tag (Rosetta / qemu).
        """
        os_name = self.host.get("os")
        arch = self.host.get("arch") or ""
        arm = arch in ("aarch64", "arm64")
        if os_name == "macOS":
            preferred = (["darwin-aarch64", "darwin-x86_64"] if arm
                         else ["darwin-x86_64", "darwin-aarch64"])
        elif os_name == "Windows":
            preferred = ["windows-x86_64"]
        else:
            preferred = (["linux-aarch64", "linux-x86_64"] if arm
                         else ["linux-x86_64", "linux-aarch64"])
        prebuilt = os.path.join(ndk_home or "", "toolchains", "llvm", "prebuilt")
        tags = []
        if os.path.isdir(prebuilt):
            try:
                tags = [n for n in os.listdir(prebuilt)
                        if os.path.isdir(os.path.join(prebuilt, n))]
            except OSError:
                tags = []
        for p in preferred:
            if p in tags:
                return p
        if tags:
            return tags[0]
        return preferred[0]

    def _patch_android_deps_script(self, path, host_tag):
        """1.4.9 build_android_deps.sh hardcodes linux-x86_64 and GNU readlink -f."""
        if self.dry_run or not os.path.isfile(path) or not host_tag:
            return
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        orig = text
        text, n_tag = re.subn(
            r'HOST_TAG="[^"]*"', f'HOST_TAG="{host_tag}"', text, count=1)
        if self._is_macos_host() and "readlink -f" in text:
            portable = 'SCRIPTDIR="$(cd "$(dirname "$0")" && pwd)"'
            text2, n_rl = re.subn(
                r'SCRIPTDIR="\$\(readlink -f "\$0"\)"\s*\n'
                r'SCRIPTDIR="\$\(dirname "\$SCRIPTDIR"\)"',
                portable, text, count=1)
            if n_rl:
                text = text2
            else:
                text = text.replace(
                    'SCRIPTDIR="$(readlink -f "$0")"', portable, 1)
                text, _ = re.subn(
                    r'\nSCRIPTDIR="\$\(dirname "\$SCRIPTDIR"\)"',
                    "\n# dirname skipped; SCRIPTDIR is already the script dir",
                    text, count=1)
        if text != orig:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self.log(f"  · patched flutter/build_android_deps.sh "
                     f"(HOST_TAG={host_tag})")
        elif n_tag:
            self.log(f"  · build_android_deps.sh HOST_TAG={host_tag}")

    def _patch_android_ndk_script(self, path):
        """Android only needs librustdesk.so — skip host-style bins (naming/service)."""
        if self.dry_run or not os.path.isfile(path):
            return
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        if re.search(r"\s--lib(\s|$)", text):
            return
        new, n = re.subn(
            r"(build --locked --release --features \S+)",
            r"\1 --lib",
            text,
            count=1,
        )
        if not n:
            new, n = re.subn(
                r"(cargo ndk\b[^\n]*\bbuild\b)",
                r"\1 --lib",
                text,
                count=1,
            )
        if n and new != text:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new)
            self.log(f"  · patched {os.path.basename(path)} with --lib")

    def _ensure_android_16k_pages(self):
        """Android 15+ refuses APKs whose JNI .so LOAD segments are 4 KiB.

        NDK r28c defaults to 16 KiB; we still pass the flag so a leftover
        r27c (or older cargo-ndk link) cannot produce a 4 KiB .so that
        Android 15+ rejects with INSTALL_FAILED_INVALID_APK.
        """
        cfg_dir = os.path.join(self.src_dir, ".cargo")
        cfg = os.path.join(cfg_dir, "config.toml")
        if self.dry_run:
            self.log("  · (dry-run) would set Android 16 KiB page-size rustflags")
            return
        os.makedirs(cfg_dir, exist_ok=True)
        existing = ""
        if os.path.isfile(cfg):
            with open(cfg, encoding="utf-8", errors="replace") as f:
                existing = f.read()
        if "max-page-size=16384" in existing:
            self.log("  · Android 16 KiB page size already in .cargo/config.toml")
            return
        block = "\n".join([
            "",
            "# DVForge: 16 KiB ELF LOAD so the APK installs on Android 15+",
            '[target.aarch64-linux-android]',
            'rustflags = ["-C", "link-arg=-Wl,-z,max-page-size=16384"]',
            '[target.armv7-linux-androideabi]',
            'rustflags = ["-C", "link-arg=-Wl,-z,max-page-size=16384"]',
            '[target.x86_64-linux-android]',
            'rustflags = ["-C", "link-arg=-Wl,-z,max-page-size=16384"]',
            '[target.i686-linux-android]',
            'rustflags = ["-C", "link-arg=-Wl,-z,max-page-size=16384"]',
            "",
        ])
        with open(cfg, "a", encoding="utf-8") as f:
            f.write(block)
        self.log("  · .cargo/config.toml: 16 KiB ELF page size for Android")

    def _android_llvm_ar_env(self, ndk_bin, target):
        """Autotools (libsodium-sys) uses $AR, not cargo-ndk's AR_<triple>.

        On macOS that defaults to Apple `ar`, which writes a Darwin archive
        Android ld.lld cannot index → undefined crypto_sign_ed25519_open.
        """
        env = {}
        if not ndk_bin or not os.path.isdir(ndk_bin):
            return env
        ar = os.path.join(ndk_bin, "llvm-ar")
        ranlib = os.path.join(ndk_bin, "llvm-ranlib")
        nm = os.path.join(ndk_bin, "llvm-nm")
        underscored = (target or "").replace("-", "_")
        triple_u = underscored.upper()
        if os.path.isfile(ar):
            env["AR"] = ar
            env[f"AR_{underscored}"] = ar
            if triple_u:
                env[f"CARGO_TARGET_{triple_u}_AR"] = ar
        if os.path.isfile(ranlib):
            env["RANLIB"] = ranlib
            env[f"RANLIB_{underscored}"] = ranlib
        if os.path.isfile(nm):
            env["NM"] = nm
            env[f"NM_{underscored}"] = nm
        return env

    def _find_jdk17(self):
        """JDK 17 home (…/bin/java). Gradle + Kotlin fail on JDK 21+."""
        cands = []
        jh = os.environ.get("JAVA_HOME") or ""
        if jh:
            cands.append(jh)
        root = self._project_root()
        cands.extend([
            os.path.join(root, ".toolchains", "java", "Contents", "Home"),
            os.path.join(root, ".toolchains", "java"),
            "/usr/lib/jvm/java-17-openjdk-amd64",
            "/usr/lib/jvm/java-17-openjdk",
            "/usr/lib/jvm/temurin-17",
            "/Library/Java/JavaVirtualMachines/temurin-17.jdk/Contents/Home",
            "/opt/homebrew/opt/openjdk@17",
            "/usr/local/opt/openjdk@17",
        ])
        cands.extend(_glob.glob("/usr/lib/jvm/*17*"))
        cands.extend(_glob.glob(
            "/Library/Java/JavaVirtualMachines/*17*/Contents/Home"))
        cands.extend(_glob.glob(
            "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home"))
        seen = set()
        for cand in cands:
            if not cand:
                continue
            home = cand
            nested = os.path.join(home, "Contents", "Home")
            if os.path.isdir(os.path.join(nested, "bin")):
                home = nested
            java = os.path.join(home, "bin", "java")
            if home in seen or not os.path.isfile(java):
                continue
            seen.add(home)
            if "17" in home.replace("\\", "/"):
                return home
            try:
                out = subprocess.check_output(
                    [java, "-version"], stderr=subprocess.STDOUT,
                    encoding="utf-8", errors="replace", timeout=10)
            except Exception:
                continue
            if re.search(r'version "17[\. "]', out) or " 17." in out:
                return home
        return None

    def build_android(self):
        self.log("\n=== Build Android ===")
        self.customize_for("android")
        env = self._env()
        # bundled asset (SKILL.md §4.2)
        if not self.dry_run:
            assets = os.path.join(self.src_dir, "flutter", "assets")
            customize.write_custom_txt(assets, env, log=self.log)

        # Android builds require JDK 17 — JDK 21 causes a JVM-target
        # mismatch (Java compiles to 1.8, Kotlin picks up 21 from the JDK).
        jdk17 = self._find_jdk17()
        if jdk17:
            self.log(f"  · using JDK 17: {jdk17}")
        else:
            self.log("  ! JDK 17 not found — Android build may fail with "
                     "JVM-target mismatch. Install JDK 17 (board install, "
                     "brew install openjdk@17, or --with-android).")

        # Clean stale Gradle caches from prior JDK 21 attempts
        gradle_cache = os.path.expanduser("~/.gradle/caches")
        if os.path.isdir(gradle_cache):
            for stale in _glob.glob(os.path.join(gradle_cache, "7.*")) + \
                          _glob.glob(os.path.join(gradle_cache, "8.*")):
                shutil.rmtree(stale, ignore_errors=True)
            init_gradle = os.path.expanduser("~/.gradle/init.gradle")
            if os.path.isfile(init_gradle):
                os.remove(init_gradle)

        # Accept Android SDK licenses (Gradle fails if licenses aren't accepted)
        self._accept_android_sdk_licenses(jdk17)

        # Gradle fixes from official CI: kill dead jcenter(), bump heap,
        # use debug signing so APK builds without a release keystore.
        build_gradle = os.path.join(self.src_dir, "flutter", "android",
                                    "build.gradle")
        if os.path.isfile(build_gradle):
            self._sed_i("s/jcenter()/mavenCentral()/g", build_gradle, check=False)
        gradle_properties = os.path.join(self.src_dir, "flutter", "android",
                                         "gradle.properties")
        if os.path.isfile(gradle_properties):
            self._sed_i(
                "s/org.gradle.jvmargs=-Xmx1024M/org.gradle.jvmargs=-Xmx2g/g",
                gradle_properties, check=False)
        app_build_gradle = os.path.join(self.src_dir, "flutter", "android",
                                        "app", "build.gradle")
        if os.path.isfile(app_build_gradle):
            self._sed_i("s/signingConfigs.release/signingConfigs.debug/g",
                        app_build_gradle, check=False)

        # Compute NDK sysroot + toolchain bin for bindgen and autotools.
        # cargo-ndk sets per-target CC_aarch64_linux_android etc. (for cc-rs),
        # but autotools-based crates (libsodium-sys) need plain CC/CXX/AR/LD
        # to cross-compile instead of using the host compiler.
        ndk_home = os.environ.get("ANDROID_NDK_HOME") or os.environ.get("ANDROID_NDK_ROOT", "")
        ndk_sysroot = ""
        ndk_bin = ""
        host_tag = self._ndk_host_tag(ndk_home)
        if ndk_home and host_tag:
            prebuilt_base = os.path.join(ndk_home, "toolchains", "llvm", "prebuilt")
            ndk_bin = os.path.join(prebuilt_base, host_tag, "bin")
            ndk_sysroot = os.path.join(prebuilt_base, host_tag, "sysroot")
            if not os.path.isdir(ndk_sysroot):
                ndk_sysroot = ""
            self.log(f"  · NDK HOST_TAG={host_tag}")

        # Ensure vcpkg is at the pinned commit — build_android_deps.sh
        # calls vcpkg install but doesn't checkout the right version.
        vcpkg_root = os.environ.get("VCPKG_ROOT", "")
        if vcpkg_root and os.path.isdir(vcpkg_root):
            self.log(f"  · vcpkg checkout {self.VCPKG_COMMIT[:8]}")
            self.run(["git", "-C", vcpkg_root, "fetch", "--depth", "1",
                      "origin", self.VCPKG_COMMIT], check=False)
            self.run(["git", "-C", vcpkg_root, "checkout", self.VCPKG_COMMIT],
                     check=False)

        # (rust_target, flutter_target, abi, ndk_script, jni_arch, cc_prefix)
        archs = {
            "android-arm64": ("aarch64-linux-android", "android-arm64", "arm64-v8a", "ndk_arm64.sh", "aarch64-linux-android", "aarch64-linux-android"),
            "android-armv7": ("armv7-linux-androideabi", "android-arm", "armeabi-v7a", "ndk_arm.sh", "arm-linux-androideabi", "armv7a-linux-androideabi"),
            "android-x86_64": ("x86_64-linux-android", "android-x64", "x86_64", "ndk_x64.sh", "x86_64-linux-android", "x86_64-linux-android"),
        }
        wanted = [a for a in self.target_ids if a in archs]
        universal = "android-universal" in self.target_ids
        if universal and not wanted:
            wanted = list(archs.keys())  # universal needs all three arch libs

        self._ensure_android_16k_pages()

        deps_script = os.path.join(self.src_dir, "flutter",
                                   "build_android_deps.sh")
        if os.path.isfile(deps_script):
            self._patch_android_deps_script(deps_script, host_tag)

        apk_dir = os.path.join(self.src_dir, "flutter", "build", "app",
                               "outputs", "flutter-apk")
        # Flutter leaves previous ABI/universal APKs here. Wipe once before
        # this run so we cannot collect (and rename) an older ABI.
        self._clear_files(apk_dir, (".apk",))

        for tid in wanted:
            target, ftarget, abi, ndk, jni_arch, cc_prefix = archs[tid]
            self.log(f"\n-- Android {abi} --")

            # Install vcpkg deps (FFmpeg, etc.) for this ABI via RustDesk's
            # own script — matches official CI.  Without this, hwcodec can't
            # find libavcodec/libavutil headers.
            if os.path.isfile(deps_script):
                self.log("  · installing vcpkg Android deps")
                bash = self._bash()
                deps_env = {}
                if ndk_home:
                    deps_env["ANDROID_NDK_HOME"] = ndk_home
                    deps_env["ANDROID_NDK_ROOT"] = ndk_home
                    deps_env["ANDROID_NDK"] = ndk_home
                if bash or self.dry_run:
                    self.run([bash or "bash", deps_script, abi],
                             cwd=self.src_dir, check=False,
                             env=deps_env or None)
            else:
                self.log("  ! flutter/build_android_deps.sh not found — "
                         "hwcodec may fail without vcpkg FFmpeg headers")

            self.run(["rustup", "target", "add", target], check=False)
            self._ensure_cargo_install("cargo-ndk", "3.1.2")
            script = os.path.join(self.src_dir, "flutter", ndk)
            self._patch_android_ndk_script(script)
            bash = self._bash()
            # Build env: ensure cargo-ndk sees the correct NDK and bindgen
            # gets the Android sysroot.  Do NOT set plain CC/CXX/CFLAGS here —
            # they leak into host builds (libsodium-sys is also built as a
            # host build-dependency) and cause a cross-compiler mismatch.
            # cargo-ndk sets per-target vars (CC_aarch64_linux_android etc.)
            # automatically when ANDROID_NDK_HOME is correct.
            ndk_env = {
                # Android is always a cross target. sccache + NDK rustc
                # produced 1800+ cache write errors on macOS.
                "RUSTC_WRAPPER": None,
                "CARGO_INCREMENTAL": "0",
            }
            # Per-target rustflags (in addition to .cargo/config.toml) so
            # cargo-ndk 3.1.2 cannot drop the 16 KiB page-size link arg.
            triple_u = target.replace("-", "_").upper()
            page = "-C link-arg=-Wl,-z,max-page-size=16384"
            prev_rf = os.environ.get(f"CARGO_TARGET_{triple_u}_RUSTFLAGS", "")
            ndk_env[f"CARGO_TARGET_{triple_u}_RUSTFLAGS"] = (
                (prev_rf + " " + page).strip() if prev_rf else page)
            if ndk_home:
                ndk_env["ANDROID_NDK_HOME"] = ndk_home
                ndk_env["ANDROID_NDK_ROOT"] = ndk_home
                ndk_env["ANDROID_NDK"] = ndk_home
            ndk_env.update(self._android_llvm_ar_env(ndk_bin, target))
            if ndk_env.get("AR"):
                self.log(f"  · AR={ndk_env['AR']}")
            if ndk_sysroot:
                ndk_env["BINDGEN_EXTRA_CLANG_ARGS"] = f"--sysroot={ndk_sysroot}"
                ndk_env[f"BINDGEN_EXTRA_CLANG_ARGS_{target.replace('-', '_')}"] = f"--sysroot={ndk_sysroot}"
                # Per-target CFLAGS/LDFLAGS for autotools-based crates that
                # read CFLAGS_<target> (libsodium-sys via cc crate).
                target_underscored = target.replace('-', '_')
                ndk_env[f"CFLAGS_{target_underscored}"] = f"--sysroot={ndk_sysroot}"
                ndk_env[f"CXXFLAGS_{target_underscored}"] = f"--sysroot={ndk_sysroot}"
                ndk_env[f"LDFLAGS_{target_underscored}"] = (
                    f"--sysroot={ndk_sysroot} -Wl,-z,max-page-size=16384")
                self.log(f"  · NDK env: ANDROID_NDK_HOME={ndk_home}")
                self.log(f"  · sysroot for {target}: {ndk_sysroot}")
            # Stale Darwin-ar libsodium.a will keep failing until rebuilt.
            if self._is_macos_host():
                self.run(["cargo", "clean", "-p", "libsodium-sys",
                          "--target", target],
                         cwd=self.src_dir, check=False,
                         env=ndk_env or None)
            if bash or self.dry_run:
                self.run([bash or "bash", script], cwd=self.src_dir, check=False,
                         env=ndk_env if ndk_env else None)
            else:
                self.log("  ! bash not found — RustDesk's NDK build scripts are shell "
                         "scripts. On Windows install Git Bash (bundled with Git for "
                         "Windows) so these can run.")

            # Copy the built .so and libc++_shared.so into jniLibs (matches CI)
            jni = os.path.join(self.src_dir, "flutter", "android", "app", "src",
                               "main", "jniLibs", abi)
            os.makedirs(jni, exist_ok=True)
            so_src = os.path.join(self.src_dir, "target", target, "release",
                                  "liblibrustdesk.so")
            if os.path.isfile(so_src):
                shutil.copy2(so_src, os.path.join(jni, "librustdesk.so"))
                self.log(f"  ✓ copied librustdesk.so → jniLibs/{abi}/")
            if ndk_sysroot:
                cpp_shared = os.path.join(
                    ndk_sysroot, "usr", "lib", jni_arch, "libc++_shared.so")
                if os.path.isfile(cpp_shared):
                    shutil.copy2(cpp_shared, os.path.join(jni, "libc++_shared.so"))
                    self.log(f"  ✓ copied libc++_shared.so → jniLibs/{abi}/")

            gradle_env = self._android_gradle_env(jdk17)
            if not universal:
                self.run(["flutter", "build", "apk", "--release",
                          "--target-platform", ftarget, "--split-per-abi"],
                         cwd=os.path.join(self.src_dir, "flutter"), check=False,
                         env=gradle_env or None)
        if universal:
            self.log("\n-- Android universal (all ABIs) --")
            gradle_env = self._android_gradle_env(jdk17)
            self.run(["flutter", "build", "apk", "--release"],
                     cwd=os.path.join(self.src_dir, "flutter"), check=False,
                     env=gradle_env or None)
        self._collect(apk_dir, (".apk",), "android",
                      names=self._android_expected_apk_names())

    def build_macos(self):
        self.log("\n=== Build macOS ===")
        # rustdesk-local-builder's macos path is: customize → vcpkg → rust
        # → Podfile (explicit modules off + @import FMDB) → build.py.
        # Extra FRAMEWORK_SEARCH_PATHS / CPATH / Flutter SDK rewrites here
        # are what broke sqflite (`module 'FMDB' not found`). Only repair
        # NAS-flattened Flutter framework symlinks + macos_assemble.sh +x.
        self._repair_flutter_xcode_scripts()
        self.customize_for("macos")
        specs = self._macos_build_specs()
        if not specs:
            raise RuntimeError("no macOS targets selected")
        self.log("  · arches: " + ", ".join(
            f"{s['suffix']} (Flutter {s['flutter']})" for s in specs))

        triplets = []
        for s in specs:
            for t in s["vcpkg"]:
                if t not in triplets:
                    triplets.append(t)
        for triplet in triplets:
            self.setup_vcpkg(triplet)

        host_triple = self._host_rust_triple()
        toolchain = f"{MAC_RUST_VERSION}-{host_triple}"
        self.run(["rustup", "toolchain", "install", toolchain], check=False)
        rust_targets = {host_triple}
        for s in specs:
            rust_targets.update(s["rust"])
        for target in sorted(rust_targets):
            self.run(["rustup", "target", "add", target,
                      "--toolchain", toolchain], check=False)
        self.run(["rustup", "default", toolchain], check=False)
        self._patch_macos_podfile()
        self._patch_macos_build_py()
        self._patch_macos_build_py_arch_env()
        self._patch_macos_generated_bridge()

        for spec in specs:
            self._build_one_macos(spec)

        self._collect(self.src_dir, (".dmg",), "macos")

    def _flutter_sdk_roots(self):
        """Flutter SDK homes that Xcode / dart might actually exec."""
        roots, seen = [], set()
        for cand in (
            os.environ.get("FLUTTER_ROOT"),
            toolchains.find_flutter_home(self._project_root()),
        ):
            if not cand:
                continue
            key = os.path.normcase(os.path.abspath(cand))
            if key in seen or not os.path.isdir(cand):
                continue
            seen.add(key)
            roots.append(cand)
        which = shutil.which("flutter", path=self._effective_path())
        if which:
            home = os.path.dirname(os.path.dirname(os.path.abspath(which)))
            key = os.path.normcase(home)
            if key not in seen and os.path.isdir(home):
                roots.append(home)
        return roots

    def _repair_flutter_xcode_scripts(self):
        """Ensure macos_assemble.sh is executable.

        Xcode's Run Script phase invokes
        `$FLUTTER_ROOT/packages/flutter_tools/bin/macos_assemble.sh`
        as a program. On NAS/exFAT copies the +x bit is gone, and the
        only xcodebuild line you get is:
          Command PhaseScriptExecution failed with a nonzero exit code
        """
        if self.dry_run:
            return
        for home in self._flutter_sdk_roots():
            toolchains.repair_flutter_permissions(home, self.log)
            assemble = os.path.join(
                home, "packages", "flutter_tools", "bin", "macos_assemble.sh")
            if os.path.isfile(assemble) and os.access(assemble, os.X_OK):
                self.log(f"  · macos_assemble.sh is executable ({assemble})")
            elif os.path.isfile(assemble):
                self.log(f"  ! macos_assemble.sh still not executable: {assemble}")

    def _patch_flutter_macos_frames_bug(self):
        """Official rustdesk CI workaround for flutter/flutter#133533.

        Comment out `_setFramesEnabledState(false);` in the Flutter SDK
        scheduler so the macOS embedder does not disable frames.
        """
        if self.dry_run:
            return
        needle = "_setFramesEnabledState(false);"
        repl = "//_setFramesEnabledState(false);"
        for home in self._flutter_sdk_roots():
            dart = os.path.join(
                home, "packages", "flutter", "lib", "src",
                "scheduler", "binding.dart")
            if not os.path.isfile(dart):
                continue
            try:
                with open(dart, "r", encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                continue
            if needle not in text:
                continue
            with open(dart, "w", encoding="utf-8") as f:
                f.write(text.replace(needle, repl))
            self.log(f"  · patched Flutter #133533: {dart}")

    def _patch_macos_podfile(self):
        """Disable explicit modules and patch sqflite's FMDB import.

        Xcode 26 uses explicit modules / clang dependency scanning, which
        fails when the sqflite module imports <fmdb/FMDB.h> — the scanner
        can't resolve the cross-framework import even though FMDB.framework
        is built. We do two things:
        1. Disable CLANG/SWIFT_ENABLE_EXPLICIT_MODULES in Podfile + Runner.xcodeproj
        2. Patch SqfliteImport.h in the pub cache to use `@import FMDB;`
           (module import) instead of `#import <fmdb/FMDB.h>` (header import)
        """
        if self.dry_run:
            return
        # -- 1. Patch Podfile --
        podfile = os.path.join(self.src_dir, "flutter", "macos", "Podfile")
        if not os.path.isfile(podfile):
            return
        with open(podfile, "r") as f:
            content = f.read()
        if "CLANG_ENABLE_EXPLICIT_MODULES" not in content:
            injection = (
                "    target.build_configurations.each do |config|\n"
                "      config.build_settings['CLANG_ENABLE_EXPLICIT_MODULES'] = 'NO'\n"
                "      config.build_settings['SWIFT_ENABLE_EXPLICIT_MODULES'] = 'NO'\n"
                "    end\n"
            )
            marker = "    flutter_additional_macos_build_settings(target)\n"
            if marker in content:
                content = content.replace(marker, marker + injection)
                with open(podfile, "w") as f:
                    f.write(content)
                self.log("  · patched Podfile: explicit modules disabled")
        # -- 2. Patch Runner.xcodeproj --
        pbxproj = os.path.join(self.src_dir, "flutter", "macos",
                               "Runner.xcodeproj", "project.pbxproj")
        if os.path.isfile(pbxproj):
            with open(pbxproj, "r") as f:
                pbx = f.read()
            changed = False
            if "CLANG_ENABLE_EXPLICIT_MODULES" not in pbx:
                pbx = pbx.replace(
                    "CLANG_ENABLE_MODULES = YES;",
                    "CLANG_ENABLE_MODULES = YES;\n\t\t\t\tCLANG_ENABLE_EXPLICIT_MODULES = NO;")
                changed = True
            if "SWIFT_ENABLE_EXPLICIT_MODULES" not in pbx:
                pbx = pbx.replace(
                    "CLANG_ENABLE_EXPLICIT_MODULES = NO;",
                    "CLANG_ENABLE_EXPLICIT_MODULES = NO;\n\t\t\t\tSWIFT_ENABLE_EXPLICIT_MODULES = NO;")
                changed = True
            if changed:
                with open(pbxproj, "w") as f:
                    f.write(pbx)
                self.log("  · patched Runner.xcodeproj: explicit modules disabled")
        # -- 3. Patch SqfliteImport.h in pub cache --
        self._patch_sqflite_import()

    def _patch_sqflite_import(self):
        """Same as rustdesk-local-builder: `@import FMDB;` + explicit modules off.

        `#import <fmdb/FMDB.h>` fails Xcode 26's module scanner. `@import`
        works when we do *not* rewrite FRAMEWORK_SEARCH_PATHS in the Podfile
        (a string `<<` there corrupts CocoaPods' search paths and then
        `module 'FMDB' not found`).
        """
        import glob
        pub_cache = os.path.expanduser("~/.pub-cache")
        candidates = glob.glob(os.path.join(
            pub_cache, "hosted", "pub.dev", "sqflite-*",
            "macos", "Classes", "SqfliteImport.h"))
        for path in candidates:
            with open(path, "r") as f:
                content = f.read()
            if "#import <fmdb/FMDB.h>" in content:
                content = content.replace(
                    "#import <fmdb/FMDB.h>", "@import FMDB;")
                with open(path, "w") as f:
                    f.write(content)
                self.log(f"  · patched {path}: @import FMDB;")

    def _macos_build_specs(self):
        """One spec per selected macOS artifact (arm64, x86_64, and/or universal)."""
        catalog = {
            "macos-arm64-dmg": {
                "rust": ["aarch64-apple-darwin"],
                "flutter": "arm64",
                "only_active": True,
                "vcpkg": ["arm64-osx"],
                "suffix": "aarch64",
            },
            "macos-x86_64-dmg": {
                "rust": ["x86_64-apple-darwin"],
                "flutter": "x86_64",
                "only_active": True,
                "vcpkg": ["x64-osx"],
                "suffix": "x86_64",
            },
            "macos-universal-dmg": {
                "rust": ["aarch64-apple-darwin", "x86_64-apple-darwin"],
                "flutter": "arm64 x86_64",
                "only_active": False,
                "vcpkg": ["arm64-osx", "x64-osx"],
                "suffix": "universal",
            },
        }
        specs = [catalog[tid] for tid in self.target_ids if tid in catalog]
        if specs:
            return specs
        # Unknown macos-* id: host arch only (same as the old single-cell board).
        host = self._mac_target()
        if host.startswith("x86_64"):
            return [catalog["macos-x86_64-dmg"]]
        return [catalog["macos-arm64-dmg"]]

    def _build_one_macos(self, spec):
        """Cargo (+ lipo) for spec['rust'], then Flutter Xcode for spec['flutter']."""
        self.log(f"\n-- macOS {spec['suffix']} --")
        features = "hwcodec,flutter"
        host = self._host_rust_triple()
        cargo_dir = self._ensure_macos_cargo_dir()
        cargo_env = {
            "MACOSX_DEPLOYMENT_TARGET": "10.14",
            "CARGO_INCREMENTAL": "0",
            "CARGO_TARGET_DIR": cargo_dir,
        }
        for triple in spec["rust"]:
            cmd = ["cargo", "build", "--locked", "--features", features,
                   "--release", "--target", triple]
            self.log(f"  · cargo --target {triple}")
            env = dict(cargo_env)
            if triple != host:
                # sccache + rustc --target on a NAS volume often dies with
                # "failed to create encoded metadata from file (os error 2)"
                # (seen on minimal-lexical when crossing ARM → Intel).
                env["RUSTC_WRAPPER"] = None
                self.log("  · sccache off for cross-target cargo")
            rc = self.run(cmd, cwd=self.src_dir, check=False, env=env)
            if rc != 0:
                self.log("  ! cargo failed — retry once (no sccache, jobs=1, "
                         "fresh target dir)")
                env["RUSTC_WRAPPER"] = None
                env["CARGO_BUILD_JOBS"] = "1"
                tdir = os.path.join(cargo_dir, triple)
                if os.path.isdir(tdir) and not self.dry_run:
                    self.log(f"  · wiping {tdir}")
                    _force_rmtree(tdir)
                rc = self.run(cmd, cwd=self.src_dir, check=False, env=env)
            if rc != 0:
                raise RuntimeError(
                    f"command failed (exit {rc}): cargo build --locked "
                    f"--features {features} --release --target {triple}")
        self._stage_macos_binaries(spec["rust"], cargo_dir)

        flutter_macos_build = os.path.join(self.src_dir, "flutter", "build", "macos")
        if os.path.isdir(flutter_macos_build) and not self.dry_run:
            self.log("  · cleaning flutter/build/macos (arch switch)")
            _force_rmtree(flutter_macos_build)

        # Run flutter ourselves. build.py interpolates ARCHS into an
        # unquoted `os.system(...)` string, so
        # FLUTTER_XCODE_ARCHS=arm64 x86_64 splits into two shell words
        # and the universal build dies immediately (exit 255).
        flutter_dir = os.path.join(self.src_dir, "flutter")
        flutter_env = {
            "FLUTTER_XCODE_ARCHS": spec["flutter"],
            "FLUTTER_XCODE_ONLY_ACTIVE_ARCH":
                "YES" if spec["only_active"] else "NO",
            "MACOSX_DEPLOYMENT_TARGET": "10.14",
        }
        self.log(f"  · Flutter ARCHS={spec['flutter']!r} "
                 f"ONLY_ACTIVE_ARCH={flutter_env['FLUTTER_XCODE_ONLY_ACTIVE_ARCH']}")
        rc = self.run(
            ["flutter", "build", "macos", "--release"],
            cwd=flutter_dir, check=False, env=flutter_env)
        if self.dry_run:
            self.log("  (dry run — skip app completeness check / dmg)")
            return

        app_name = self.config.get("appname", "RustDesk") or "RustDesk"
        app_dir = os.path.join(self.src_dir, "flutter", "build", "macos",
                               "Build", "Products", "Release")
        app_bundle = self._find_macos_app(app_dir, app_name)
        if not self._macos_app_complete(app_bundle):
            self._log_macos_app_debug(app_dir, app_bundle)
            raise RuntimeError(
                f"macOS {spec['suffix']} Flutter build did not produce a complete "
                f"{app_name}.app (flutter exit {rc}). Not packaging a stub DMG.")
        env = self._env()
        customize.write_custom_txt(app_dir, env, log=self.log)
        resources_dir = os.path.join(app_bundle, "Contents", "Resources")
        os.makedirs(resources_dir, exist_ok=True)
        customize.write_custom_txt(resources_dir, env, log=self.log)
        # build.py used to copy target/release/service into the .app
        service = os.path.join(self.src_dir, "target", "release", "service")
        macos_bin = os.path.join(app_bundle, "Contents", "MacOS")
        if os.path.isfile(service) and os.path.isdir(macos_bin):
            shutil.copy2(service, macos_bin)
            self.log("  · copied service → Contents/MacOS/")
        exe = os.path.join(macos_bin, app_name)
        if os.path.isfile(exe):
            self.run(["lipo", "-info", exe], check=False)
        self._codesign_macos_app(app_bundle)
        self._create_macos_dmg(suffix=spec["suffix"])

    def _ensure_macos_cargo_dir(self):
        """Where cargo writes artifacts.

        rustc rmeta encode (`failed to create encoded metadata from file`)
        is unreliable on /Volumes NAS shares, especially for
        --target x86_64-apple-darwin. Keep the target dir on the boot disk.
        Xcode still links src/target/release/ — _stage_macos_binaries copies
        the dylibs there.
        """
        src = os.path.abspath(self.src_dir)
        if src.startswith("/Volumes/"):
            d = os.path.join(os.path.expanduser("~"), "Library", "Caches",
                             "dvforge", "cargo-target", self.version)
            os.makedirs(d, exist_ok=True)
            self.log(f"  · CARGO_TARGET_DIR = {d} (source is on /Volumes)")
            return d
        return os.path.join(self.src_dir, "target")

    def _stage_macos_binaries(self, rust_targets, cargo_dir=None):
        """Copy (or lipo) cargo output into target/release/ where Xcode links it."""
        if self.dry_run:
            self.log(f"  (would stage dylibs for {', '.join(rust_targets)})")
            return
        cargo_dir = cargo_dir or os.path.join(self.src_dir, "target")
        rel = os.path.join(self.src_dir, "target", "release")
        os.makedirs(rel, exist_ok=True)
        for name in ("liblibrustdesk.dylib", "service"):
            srcs = [os.path.join(cargo_dir, t, "release", name)
                    for t in rust_targets]
            missing = [s for s in srcs if not os.path.isfile(s)]
            if missing:
                raise RuntimeError(
                    "cargo did not produce " + ", ".join(missing))
            dest = os.path.join(rel, name)
            if len(srcs) == 1:
                shutil.copy2(srcs[0], dest)
            else:
                self.run(["lipo", "-create"] + srcs + ["-output", dest],
                         check=True)
            if name == "liblibrustdesk.dylib":
                shutil.copy2(dest, os.path.join(rel, "librustdesk.dylib"))
            self.log(f"  · staged {name} ← {', '.join(rust_targets)}")
            if len(srcs) > 1:
                self.run(["lipo", "-info", dest], check=False)

    def _patch_macos_build_py_arch_env(self):
        """Let DVFORGE_MAC_ARCH / DVFORGE_ONLY_ACTIVE override host-arch Flutter.

        Upstream build.py always uses platform.machine(), so an Apple Silicon
        Mac could never emit an Intel slice. After this patch, build.py reads
        those env vars (set per spec in _build_one_macos).
        """
        if self.dry_run:
            return
        build_py = os.path.join(self.src_dir, "build.py")
        if not os.path.isfile(build_py):
            return
        with open(build_py, "r", encoding="utf-8", errors="surrogateescape") as f:
            text = f.read()
        old = (
            "mac_arch = 'arm64' if platform.machine().lower() "
            "in ('arm64', 'aarch64') else 'x86_64'\n"
            "    system2(\n"
            "        f'FLUTTER_XCODE_ARCHS={mac_arch} "
            "FLUTTER_XCODE_ONLY_ACTIVE_ARCH=YES flutter build macos --release')"
        )
        new = (
            "mac_arch = os.environ.get('DVFORGE_MAC_ARCH') or "
            "('arm64' if platform.machine().lower() "
            "in ('arm64', 'aarch64') else 'x86_64')\n"
            "    only_active = os.environ.get('DVFORGE_ONLY_ACTIVE') or 'YES'\n"
            "    system2(\n"
            "        f'FLUTTER_XCODE_ARCHS={mac_arch} "
            "FLUTTER_XCODE_ONLY_ACTIVE_ARCH={only_active} "
            "flutter build macos --release')"
        )
        if old not in text:
            if "DVFORGE_MAC_ARCH" in text:
                self.log("  · build.py already reads DVFORGE_MAC_ARCH")
                return
            self.log("  ! could not patch build.py Flutter ARCHS "
                     "(upstream layout changed)")
            return
        text = text.replace(old, new, 1)
        with open(build_py, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(text)
        self.log("  · patched build.py: Flutter ARCHS from DVFORGE_MAC_ARCH")

    def _mac_target(self):
        """Rust target triple for the requested macOS build.

        host_info() normalizes arch to 'aarch64' (not 'arm64'), so check both.
        Target ids like macos-universal-dmg don't encode an arch — fall back to host.
        """
        for tid in self.target_ids:
            if "aarch64" in tid or "arm64" in tid:
                return "aarch64-apple-darwin"
            if "x86_64" in tid:
                return "x86_64-apple-darwin"
        # Default to the host architecture (detect uses 'aarch64', not 'arm64').
        arch = self.host.get("arch") or ""
        if arch in ("arm64", "aarch64"):
            return "aarch64-apple-darwin"
        return "x86_64-apple-darwin"

    def _patch_macos_build_py(self):
        """Replace hardcoded RustDesk.app in build.py with the custom app name.

        build.py line ~420 does:
            cp -rf ../target/release/service ./build/macos/Build/Products/Release/RustDesk.app/Contents/MacOS/
        When PRODUCT_NAME is customized, flutter build produces {App}.app, not
        RustDesk.app, so that cp fails silently. Patch build.py to use the
        configured app name.
        """
        if self.dry_run:
            return
        app_name = self.config.get("appname", "RustDesk") or "RustDesk"
        if app_name == "RustDesk":
            return
        build_py = os.path.join(self.src_dir, "build.py")
        if not os.path.isfile(build_py):
            return
        with open(build_py, "r", encoding="utf-8", errors="surrogateescape") as f:
            text = f.read()
        if "RustDesk.app" not in text:
            return
        text = text.replace("RustDesk.app", f"{app_name}.app")
        with open(build_py, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(text)
        self.log(f"  · patched build.py: RustDesk.app -> {app_name}.app")

    def _patch_macos_generated_bridge(self):
        """Fix ffigen-generated Dart bindings that fail on macOS builds.

        Two issues:
        1. wire_uint_8_list.ptr is typed as ffi.Pointer<ffi.Int> but the C
           header declares uint8_t *ptr. asTypedList() only exists on
           Pointer<Uint8>, not Pointer<Int>, causing a compile error.
        2. ffi_bindgen_function_refactor() (the sed fix for Bool->Uint8 in
           the DartPostCObjectFnType signature) is only called in
           build_flutter_deb and build_flutter_arch_manjaro, never for macOS.
        """
        if self.dry_run:
            return
        dart_file = os.path.join(self.src_dir, "flutter", "lib",
                                 "generated_bridge.dart")
        if not os.path.isfile(dart_file):
            return
        with open(dart_file, "r", encoding="utf-8") as f:
            text = f.read()
        changed = False
        # Fix 1: wire_uint_8_list ptr type Int -> Uint8
        if "external ffi.Pointer<ffi.Int> ptr;" in text:
            text = text.replace(
                "external ffi.Pointer<ffi.Int> ptr;",
                "external ffi.Pointer<ffi.Uint8> ptr;",
                1)
            changed = True
        # Fix 2: Bool Function(DartPort -> Uint8 Function(DartPort
        # (same sed as ffi_bindgen_function_refactor in build.py)
        if "ffi.NativeFunction<ffi.Bool Function(DartPort" in text:
            text = text.replace(
                "ffi.NativeFunction<ffi.Bool Function(DartPort",
                "ffi.NativeFunction<ffi.Uint8 Function(DartPort")
            changed = True
        if changed:
            with open(dart_file, "w", encoding="utf-8") as f:
                f.write(text)
            self.log("  · patched generated_bridge.dart: ptr type + Bool->Uint8")

    def _find_macos_app(self, app_dir, app_name):
        """Prefer {app_name}.app; otherwise any .app in Release."""
        named = os.path.join(app_dir, f"{app_name}.app")
        if self._macos_app_complete(named):
            return named
        if os.path.isdir(app_dir):
            try:
                for name in sorted(os.listdir(app_dir)):
                    if name.endswith(".app"):
                        cand = os.path.join(app_dir, name)
                        if self._macos_app_complete(cand):
                            self.log(f"  · using {name} (PRODUCT_NAME mismatch)")
                            return cand
            except OSError:
                pass
        return named

    def _log_macos_app_debug(self, app_dir, app_bundle):
        self.log(f"  ! expected app: {app_bundle}")
        macos = os.path.join(app_bundle or "", "Contents", "MacOS")
        if os.path.isdir(macos):
            try:
                for name in os.listdir(macos):
                    p = os.path.join(macos, name)
                    sz = os.path.getsize(p) if os.path.isfile(p) else 0
                    self.log(f"    Contents/MacOS/{name}  {sz} bytes")
            except OSError:
                pass
        elif os.path.isdir(app_dir):
            try:
                self.log("  ! Release contains: " + ", ".join(os.listdir(app_dir)[:20]))
            except OSError:
                pass

    def _macos_app_complete(self, app):
        """True when Contents/MacOS has a real runner (not an empty stub).

        The Flutter *runner* is a thin Swift wrapper (~200–400 KB). The
        megabytes live in App.framework. A 1 MB floor on the runner
        rejected a complete universal MacOSUNi.app (295 KB, arm64+x86_64).
        """
        macos = os.path.join(app or "", "Contents", "MacOS")
        if not os.path.isdir(macos):
            return False
        try:
            names = os.listdir(macos)
        except OSError:
            return False
        has_runner = False
        for name in names:
            p = os.path.join(macos, name)
            try:
                if os.path.isfile(p) and os.path.getsize(p) > 16_384:
                    has_runner = True
                    break
            except OSError:
                continue
        if not has_runner:
            return False
        frameworks = os.path.join(app, "Contents", "Frameworks")
        if os.path.isdir(frameworks):
            try:
                if any(n.endswith(".framework") or n.endswith(".dylib")
                       for n in os.listdir(frameworks)):
                    return True
            except OSError:
                pass
        return has_runner

    def _codesign_macos_app(self, app_bundle):
        """Ad-hoc sign the .app so macOS doesn't kill it on launch.

        Without any signature, Gatekeeper terminates the app immediately
        (the classic "opens and flashes" symptom). Ad-hoc signing (-s -)
        lets it run on the build machine and any machine that trusts the
        developer. For distribution, replace with a Developer ID signature.
        """
        if self.dry_run:
            self.log("  (would codesign)")
            return
        if not os.path.isdir(app_bundle):
            return
        self.log(f"  · ad-hoc codesigning {os.path.basename(app_bundle)}")
        self.run(["codesign", "--force", "--deep", "--sign", "-",
                  app_bundle], check=False)
        rc = self.run(["codesign", "--verify", "--verbose=1",
                       app_bundle], check=False)
        if rc == 0:
            self.log("  ✓ codesign verified")
        else:
            self.log("  ! codesign verification failed — app may be killed on launch")

    def _create_macos_dmg(self, suffix=None):
        """Create a .dmg from the built RustDesk.app using create-dmg.

        build.py has the create-dmg step commented out, so the orchestrator
        handles DMG packaging after the Flutter build completes.
        `suffix` is aarch64 / x86_64 / universal so Intel and ARM artifacts
        do not overwrite each other.
        """
        if self.dry_run:
            self.log("  (would create .dmg)")
            return
        app_name = self.config.get("appname", "RustDesk") or "RustDesk"
        app_basename = f"{app_name}.app"
        app = os.path.join(self.src_dir, "flutter", "build", "macos",
                           "Build", "Products", "Release", app_basename)
        if not self._macos_app_complete(app):
            self.log(f"  ! {app_basename} is missing or empty — skipping DMG "
                     "(a failed Flutter build used to yield a ~13 KB stub)")
            return
        create_dmg = shutil.which("create-dmg", path=self._effective_path())
        if not create_dmg:
            self.log("  ! create-dmg not found — skipping DMG creation")
            return
        version = self.config.get("version", "") or self.version
        basename = self._output_basename()
        parts = [basename]
        if version:
            parts.append(str(version).lstrip("v"))
        if suffix:
            parts.append(suffix)
        dmg_name = "-".join(parts) + ".dmg"
        dmg_path = os.path.join(self.src_dir, dmg_name)
        flutter_dir = os.path.join(self.src_dir, "flutter")
        tmp_dmg = os.path.join(flutter_dir, f"{basename}.dmg")
        self.run([
            create_dmg,
            "--volname", f"{app_name} Installer",
            "--window-pos", "200", "120",
            "--window-size", "800", "400",
            "--icon-size", "100",
            "--app-drop-link", "600", "185",
            "--icon", app_basename, "200", "190",
            "--hide-extension", app_basename,
            tmp_dmg, app,
        ], cwd=flutter_dir, check=False)
        if os.path.isfile(tmp_dmg):
            shutil.move(tmp_dmg, dmg_path)
            self.log(f"  ✓ created {dmg_name}")

    # ---- artifact collection ---------------------------------------------
    def _clear_files(self, directory, exts):
        """Delete files with the given extensions in directory (not recursive)."""
        if self.dry_run or not os.path.isdir(directory):
            return
        removed = 0
        try:
            for name in os.listdir(directory):
                if not any(name.endswith(e) for e in exts):
                    continue
                path = os.path.join(directory, name)
                if os.path.isfile(path):
                    os.remove(path)
                    removed += 1
        except OSError:
            return
        if removed:
            self.log(f"  · cleared {removed} stale {', '.join(exts)} from {directory}")

    def _android_expected_apk_names(self):
        """Flutter APK filenames this run should produce (not leftovers)."""
        names = set()
        if "android-arm64" in self.target_ids:
            names.add("app-arm64-v8a-release.apk")
        if "android-armv7" in self.target_ids:
            names.add("app-armeabi-v7a-release.apk")
        if "android-x86_64" in self.target_ids:
            names.add("app-x86_64-release.apk")
        if "android-universal" in self.target_ids:
            names.add("app-release.apk")
        return names

    def _collect(self, root, exts, platform, names=None):
        os.makedirs(self.out_dir, exist_ok=True)
        if self.dry_run or not os.path.isdir(root):
            self.log(f"  (would collect {'/'.join(exts)} from {root})")
            return
        appname = self.config.get("appname", "RustDesk")
        basename = self._output_basename()
        # Only collect files whose base name starts with the app name,
        # file name (exename), or "rustdesk". App name and file name can
        # differ (e.g. appname=fasterlinuxRPM, exename=fasterlinuxDEB) —
        # packaging uses exename, so both must be accepted. Still skip
        # dependency .deb files that appimage-builder downloads.
        prefixes = tuple(
            p.lower() for p in (appname, basename, "rustdesk", "app-") if p
        )
        # Directories to skip entirely during collection.
        skip_dirs = {"appimage", "tmpdeb", ".git"}
        found = 0
        for dp, dirnames, files in os.walk(root):
            # prune skip dirs in-place so os.walk doesn't descend into them
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for f in files:
                if not any(f.endswith(e) for e in exts):
                    continue
                if names is not None and f not in names:
                    continue
                if not f.lower().startswith(prefixes):
                    continue
                src = os.path.join(dp, f)
                try:
                    if f.lower().endswith(".dmg") and os.path.getsize(src) < 1_000_000:
                        self.log(f"  · skip stub dmg {src} ({os.path.getsize(src)} bytes)")
                        continue
                except OSError:
                    continue
                # Rename artifacts to use the custom basename:
                # - rustdesk-* -> {basename}-*  (Linux .deb/.rpm/.AppImage, macOS .dmg)
                # - app-*.apk -> {basename}-*.apk  (Android split APKs)
                # - app-release.apk -> {basename}-release.apk  (Android universal)
                out_name = f
                if basename.lower() != "rustdesk":
                    if f.lower().startswith("rustdesk"):
                        out_name = basename + f[len("rustdesk"):]
                    elif f.lower().startswith("app-") and f.lower().endswith(".apk"):
                        out_name = basename + f[3:]  # replace "app-" prefix
                os.makedirs(self.out_dir, exist_ok=True)
                dest = os.path.join(self.out_dir, out_name)
                if dest in self.artifacts:
                    found += 1
                    continue
                shutil.copy2(src, dest)
                self.artifacts.append(dest)
                self.log(f"  ✓ artifact: {dest}")
                found += 1
        if not found:
            self.log(f"  ! no {'/'.join(exts)} artifacts found under {root}")

    def _collect_dir(self, root, platform, outname):
        """Copy an entire build directory into the output, preserving structure."""
        os.makedirs(self.out_dir, exist_ok=True)
        if self.dry_run or not os.path.isdir(root):
            self.log(f"  (would collect {platform} directory from {root})")
            return
        dest = os.path.join(self.out_dir, outname)
        if os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.copytree(root, dest, ignore=shutil.ignore_patterns(
            "appimage", "tmpdeb", ".git"))
        # Rename rustdesk.exe to {filename}.exe inside the copied directory
        # (Windows build produces rustdesk.exe unless CMakeLists was patched)
        basename = self._output_basename()
        if basename.lower() != "rustdesk":
            old_exe = os.path.join(dest, "rustdesk.exe")
            new_exe = os.path.join(dest, f"{basename}.exe")
            if os.path.isfile(old_exe):
                os.rename(old_exe, new_exe)
                self.log(f"  · renamed rustdesk.exe -> {basename}.exe")
        self.artifacts.append(dest)
        self.log(f"  ✓ artifact: {dest}")

    # -- driver -------------------------------------------------------------
    def execute(self):
        start = time.time()
        try:
            self.log(f"Building RustDesk v{self.version} for: "
                     f"{', '.join(self.target_ids)}")
            self.log(f"Host: {self.host['os']} {self.host['arch']} · "
                     f"{self.host['cores_logical']} cores · {self.host['ram_gb']} GB RAM")
            if self.dry_run:
                self.log("** DRY RUN — commands are printed, nothing is executed **")

            self._ensure_flutter()
            self.checkout_source()
            self._ensure_rust()
            self._ensure_sccache()
            self._ensure_llvm()
            self.generate_bridge()

            plats = self.platforms_needed()
            dispatch = {
                "windows": self.build_windows,
                "linux": self.build_linux,
                "android": self.build_android,
                "macos": self.build_macos,
            }
            for p in plats:
                self._check_cancel()
                dispatch[p]()

            self._log_sccache_stats()

            elapsed = int(time.time() - start)
            self.log(f"\n=== DONE in {elapsed//60}m {elapsed%60}s ===")
            self.log(f"Artifacts ({len(self.artifacts)}):")
            for a in self.artifacts:
                self.log(f"  {a}")
            return {"ok": True, "artifacts": self.artifacts, "seconds": elapsed}
        except BuildCancelled:
            self.log("\n!! build cancelled by user")
            return {"ok": False, "cancelled": True, "artifacts": self.artifacts}
        except Exception as e:
            self.log(f"\n!! BUILD FAILED: {e}")
            return {"ok": False, "error": str(e), "artifacts": self.artifacts}


def preflight(target_ids, prereqs_status, host=None):
    """Return (ok, problems[]) — are the toolchains present for these targets?"""
    host = host or detect.host_info()
    problems = []
    matrix = {t["id"]: t for t in detect.TARGETS}
    for tid in target_ids:
        t = matrix.get(tid)
        if not t:
            problems.append(f"unknown target {tid}")
            continue
        if host["os"] not in t["host_os"]:
            problems.append(f"{t['label']}: needs a {' or '.join(t['host_os'])} host")
            continue
        for tool in detect.required_tools(t, host["os"]):
            st = prereqs_status.get(tool)
            if not st or not st.get("present"):
                problems.append(f"{t['label']}: missing {tool}")
    # de-dupe, preserve order
    seen, uniq = set(), []
    for p in problems:
        if p not in seen:
            seen.add(p); uniq.append(p)
    return (len(uniq) == 0, uniq)
