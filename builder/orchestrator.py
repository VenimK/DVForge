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
import shutil
import subprocess
import sys
import threading
import time
import zipfile
import platform as _platform

from . import customize, detect, prereqs

RUSTDESK_REPO = "https://github.com/rustdesk/rustdesk.git"

# toolchain versions (match the workflows / SKILL.md)
RUST_VERSION = "1.75"        # Windows/Linux desktop
MAC_RUST_VERSION = "1.81"    # macOS desktop (official CI uses 1.81)
FLUTTER_VERSION = "3.24.5"


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
        """PATH the build should see: cargo's bin dir + whatever we inherited
        (which already includes any locally-installed .toolchains via env.json)."""
        parts = [p for p in (_cargo_bin(),) if os.path.isdir(p)]
        parts.append(os.environ.get("PATH", ""))
        return os.pathsep.join(parts)

    def run(self, cmd, cwd=None, env=None, shell=False, check=True):
        pretty = cmd if isinstance(cmd, str) else " ".join(cmd)
        self.log(f"$ {pretty}")
        if self.dry_run:
            return 0
        self._check_cancel()
        full_env = os.environ.copy()
        full_env["PATH"] = self._effective_path()
        if env:
            full_env.update(env)

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
    def checkout_source(self):
        self.log("\n=== 1. Check out RustDesk source ===")
        # Customizations mutate the source tree (sed/patch/rename), so every
        # build must start from a pristine checkout. If a previous tree is here,
        # remove it first — reusing it would double-apply patches and corrupt it.
        if os.path.exists(self.src_dir):
            self.log(f"  clearing previous source at {self.src_dir}")
            if not self.dry_run:
                # Docker builds can leave the dir with d-w------- (no read/execute),
                # so rmtree can't traverse it. Fix permissions top-down first.
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
                # a leftover read-only .git on Windows can resist removal
                if os.path.exists(self.src_dir):
                    _force_rmtree(self.src_dir)
                # last resort: shell rm -rf can clear files Python cannot
                if os.path.exists(self.src_dir):
                    self.log(f"  force-removing leftover {self.src_dir}")
                    if self.host["os"] == "Windows":
                        subprocess.run(
                            ["cmd", "/c", f'rmdir /s /q "{self.src_dir}"'],
                            check=False)
                    else:
                        subprocess.run(["rm", "-rf", self.src_dir],
                                       check=False)
                # NFS volumes (e.g. /Volumes on macOS) create .nfs* "silly
                # rename" files when a deleted file is still open.  These
                # vanish once the process exits, so retry once after a brief
                # wait.  If the dir *still* won't go, clone into a sibling
                # temp dir and rename — git clone just needs an empty target.
                if os.path.exists(self.src_dir):
                    import time
                    time.sleep(2)
                    subprocess.run(["rm", "-rf", self.src_dir], check=False)
        os.makedirs(self.workspace, exist_ok=True)
        if os.path.exists(self.src_dir):
            # Could not remove the old tree (NFS stale handles, locked files).
            # Clone into a fresh temp name, then swap.
            tmp_dir = self.src_dir + ".checkout-tmp"
            if os.path.exists(tmp_dir):
                subprocess.run(["rm", "-rf", tmp_dir], check=False)
            self.run(["git", "clone", "--depth", "1", "--branch", self.version,
                      "--recurse-submodules", RUSTDESK_REPO, tmp_dir])
            # Best-effort: try removing the old dir again, then rename.
            subprocess.run(["rm", "-rf", self.src_dir], check=False)
            if os.path.exists(self.src_dir):
                # Still stuck — just use the temp dir as src_dir
                self.src_dir = tmp_dir
            else:
                os.rename(tmp_dir, self.src_dir)
        else:
            self.run(["git", "clone", "--depth", "1", "--branch", self.version,
                      "--recurse-submodules", RUSTDESK_REPO, self.src_dir])

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
        # clang resource dir inside the toolchains LLVM (stddef.h, etc.)
        lib_clang = os.path.join(llvm_home, "lib", "clang")
        if os.path.isdir(lib_clang):
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
        self.log(f"  · ffigen CPATH prepared for toolchains LLVM ({len(uniq)} dirs)")

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
            # bindgen (libclang) does not always honour SDKROOT alone
            extra = f"--sysroot={sdk}"
            existing = os.environ.get("BINDGEN_EXTRA_CLANG_ARGS", "")
            if "--sysroot=" not in existing:
                os.environ["BINDGEN_EXTRA_CLANG_ARGS"] = (
                    f"{existing} {extra}".strip() if existing else extra
                )
            self.log(f"  · SDKROOT = {sdk}")
            self.log(f"  · BINDGEN_EXTRA_CLANG_ARGS = "
                     f"{os.environ['BINDGEN_EXTRA_CLANG_ARGS']}")
        else:
            self.log("  ! could not resolve macOS SDK path via xcrun "
                     "(bindgen may fail to find system headers)")

    def _ensure_sccache(self):
        """If sccache is installed, set RUSTC_WRAPPER so cargo uses it.

        sccache caches Rust compilation artifacts across builds, dramatically
        reducing rebuild times. It's optional — if not installed, builds
        proceed normally without caching."""
        sccache = shutil.which("sccache", path=self._effective_path())
        if sccache:
            os.environ["RUSTC_WRAPPER"] = "sccache"
            self.log(f"  · sccache enabled ({sccache})")
        else:
            # Don't override an explicit user choice, but clear stale wrapper
            if os.environ.get("RUSTC_WRAPPER") == "sccache":
                del os.environ["RUSTC_WRAPPER"]
            self.log("  · sccache not found — builds will run without cache. "
                     "Install it via the Toolchain tab or: cargo install sccache")

    def _log_sccache_stats(self):
        """Print sccache cache statistics after all builds complete."""
        sccache = shutil.which("sccache", path=self._effective_path())
        if not sccache:
            return
        if os.environ.get("RUSTC_WRAPPER") != "sccache":
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

        # 1) Prefer the portable toolchains LLVM — always, when present.
        llvm_home = self._toolchains_llvm_home()
        if llvm_home:
            libdir = self._libclang_dir(llvm_home)
            bindir = os.path.join(llvm_home, "bin")
            prev = os.environ.get("LIBCLANG_PATH", "")
            os.environ["LIBCLANG_PATH"] = libdir
            # Prepend toolchains clang so PATH resolves to 15.0.6, not system 19+.
            if os.path.isdir(bindir):
                path_parts = [
                    p for p in os.environ.get("PATH", "").split(os.pathsep)
                    if p and p != bindir
                ]
                os.environ["PATH"] = os.pathsep.join([bindir] + path_parts)
            self._llvm_home = llvm_home
            self._wire_llvm_ffigen_includes(llvm_home)
            if prev and os.path.abspath(prev) != os.path.abspath(libdir):
                self.log(f"  · overriding LIBCLANG_PATH ({prev}) with toolchains LLVM")
            self.log(f"  · toolchains LLVM: {llvm_home}")
            self.log(f"  · LIBCLANG_PATH = {libdir}")
            return

        # 2) Fall back to whatever is already on the environment.
        libclang = os.environ.get("LIBCLANG_PATH", "")
        if libclang and self._has_libclang(libclang):
            self._llvm_home = os.path.dirname(libclang.rstrip(os.sep))
            self.log(f"  · LIBCLANG_PATH = {libclang} "
                     f"(no .toolchains/llvm — using existing env)")
            return
        if libclang and os.path.isdir(libclang):
            self.log(f"  ! LIBCLANG_PATH={libclang} does not contain libclang")

        # 3) System clang only as a last resort (and warn if not 15.0.6).
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

    def generate_bridge(self):
        self.log("\n=== 2. Generate flutter_rust_bridge ===")
        # Re-assert toolchains LLVM right before codegen so a later step
        # cannot have clobbered LIBCLANG_PATH.
        self._ensure_llvm()
        # Mirrors the generate-bridge job. cargo installs the codegen binary into
        # ~/.cargo/bin, which run() puts on PATH so it resolves on Windows too.
        self.run(["cargo", "install", "flutter_rust_bridge_codegen",
                  "--version", "1.80.1", "--features", "uuid", "--locked"],
                 check=False)
        # ffigen (invoked by the codegen) needs .dart_tool/package_config.json,
        # which only exists after `flutter pub get`. Without it the codegen
        # emits dummy code with an unresolvable Dart_Handle type and the build
        # fails with E0412.
        flutter_dir = os.path.join(self.src_dir, "flutter")
        self.run(["flutter", "pub", "get"], cwd=flutter_dir, check=True)
        pkg_config = os.path.join(flutter_dir, ".dart_tool", "package_config.json")
        if not os.path.isfile(pkg_config):
            self.log("  ! flutter pub get did not create .dart_tool/package_config.json")
            self.log("  ! bridge codegen will produce dummy code — aborting")
            return
        codegen = shutil.which("flutter_rust_bridge_codegen", path=self._effective_path())
        if not codegen and not self.dry_run:
            self.log("  ! flutter_rust_bridge_codegen not found after cargo install; "
                     "skipping bridge regen (may already be generated in the source).")
            return
        cmd = [codegen or "flutter_rust_bridge_codegen",
               "--rust-input", "./src/flutter_ffi.rs",
               "--dart-output", "./flutter/lib/generated_bridge.dart",
               "--c-output", "./flutter/macos/Runner/bridge_generated.h"]
        # ffigen does NOT honour LIBCLANG_PATH — it searches --llvm-path.
        # Always point it at the toolchains (or resolved) LLVM home.
        llvm_home = getattr(self, "_llvm_home", None) or self._toolchains_llvm_home()
        if llvm_home and os.path.isdir(llvm_home):
            cmd += ["--llvm-path", llvm_home]
            self.log(f"  · passing --llvm-path {llvm_home} to codegen")
        else:
            self.log("  ! no toolchains LLVM home for --llvm-path; ffigen may "
                     "produce broken bindings (typedef bool poison / dummy code)")
        # Pass CPATH/C_INCLUDE_PATH only to the codegen subprocess so ffigen
        # can find stdbool.h etc.  These must NOT leak into the global env —
        # Clang-specific headers break GCC compilations of zstd-sys / ring.
        codegen_env = {}
        ffigen_cpath = getattr(self, "_ffigen_cpath", "")
        if ffigen_cpath:
            codegen_env["CPATH"] = ffigen_cpath
            codegen_env["C_INCLUDE_PATH"] = ffigen_cpath
        self.run(cmd, cwd=self.src_dir, check=False, env=codegen_env)

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

    def setup_vcpkg(self, triplet):
        """Check out the pinned vcpkg commit and install RustDesk's native deps
        (ffmpeg, hwcodec, etc.) for `triplet`. Needs VCPKG_ROOT set."""
        root = os.environ.get("VCPKG_ROOT")
        if not root:
            self.log("  ! VCPKG_ROOT not set — skipping vcpkg dep install. "
                     "Set it to your vcpkg checkout so ffmpeg/hwcodec resolve.")
            return
        self.log(f"  vcpkg deps ({triplet}) from {root}")
        self.run(["git", "-C", root, "fetch", "--depth", "1", "origin", self.VCPKG_COMMIT],
                 check=False)
        self.run(["git", "-C", root, "checkout", self.VCPKG_COMMIT], check=False)
        vcpkg_exe = os.path.join(root, "vcpkg.exe" if self.host["os"] == "Windows" else "vcpkg")
        # After switching commits the vcpkg binary is stale — re-bootstrap it.
        # On Windows use bootstrap-vcpkg.bat; on Linux/macOS use bootstrap-vcpkg.sh.
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
        self.run([vcpkg_exe, "install", "--triplet", triplet,
                  f"--x-install-root={os.path.join(root, 'installed')}"],
                 cwd=self.src_dir, check=True, env=env)

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

    def build_windows(self):
        self.log("\n=== Build Windows x86_64 ===")
        # Always use the MSVC toolchain — the official CI pins
        # x86_64-pc-windows-msvc. The GNU target requires gcc.exe (MinGW)
        # which most Windows dev setups don't have.
        if self.host["os"] == "Windows":
            # Pin Rust 1.75 — matches official CI. Rust 1.78+ has an i128
            # ABI change that breaks sciter and other deps.
            # https://blog.rust-lang.org/2024/03/30/i128-layout-update.html
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

        self.setup_vcpkg("x64-windows-static")
        self.customize_for("windows")

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
        if self.host["os"] == "Windows" and not self.dry_run:
            self.log("  · replacing Flutter engine with RustDesk custom build")
            self.run(["flutter", "precache", "--windows"], check=False)
            flutter_exe = shutil.which("flutter", path=self._effective_path())
            if flutter_exe:
                # Find the engine artifacts dir
                flutter_dir = os.path.dirname(os.path.dirname(flutter_exe))
                engine_dir = os.path.join(flutter_dir, "bin", "cache", "artifacts",
                                          "engine", "windows-x64-release")
                zip_path = os.path.join(self.src_dir, "windows-x64-release.zip")
                self.run(["curl", "-sL", "-o", zip_path,
                          "https://github.com/rustdesk/engine/releases/download/main/windows-x64-release.zip"],
                         check=False)
                if os.path.isfile(zip_path):
                    extract_dir = os.path.join(self.src_dir, "windows-x64-release")
                    with zipfile.ZipFile(zip_path, 'r') as z:
                        z.extractall(extract_dir)
                    # Move contents into the engine dir
                    if os.path.isdir(extract_dir) and os.path.isdir(engine_dir):
                        for item in os.listdir(extract_dir):
                            src_item = os.path.join(extract_dir, item)
                            dst_item = os.path.join(engine_dir, item)
                            if os.path.isfile(dst_item):
                                os.remove(dst_item)
                            if os.path.isfile(src_item):
                                shutil.copy2(src_item, dst_item)
                        self.log(f"  ✓ custom engine installed to {engine_dir}")
                    else:
                        self.log(f"  ! engine dir not found: {engine_dir}")
                else:
                    self.log("  ! failed to download custom Flutter engine")

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

        basename = self._output_basename()
        version = self.version

        # Build and collect MSI installer if requested
        if wants_msi:
            self._build_windows_msi()

        # Pack + collect the portable exe if requested
        if wants_exe:
            self._pack_windows_portable(release, version)
            portable_exe = os.path.join(self.src_dir, f"rustdesk-{version}-install.exe")
            if os.path.isfile(portable_exe):
                dest = os.path.join(self.out_dir, f"{basename}-{version}-install.exe")
                shutil.copy2(portable_exe, dest)
                self.artifacts.append(dest)
                self.log(f"  ✓ artifact: {dest}")
            else:
                self.log("  ! portable exe not found — portable pack may have failed")
            # Also copy the Release directory as a fallback (loose files)
            self._collect_dir(release, "windows", "Release")

    def _pack_windows_portable(self, release, version):
        """Run libs/portable/generate.py ourselves (mirrors build.py's own
        build_flutter_windows packing step), now that `release` already has
        custom_.txt written into it — so the packed exe bakes in branding,
        password, and permissions correctly."""
        self.log("\n=== Pack Windows portable (single .exe) ===")
        exe_name = os.path.join(release, "rustdesk.exe")
        if not os.path.isfile(exe_name):
            # rename may already have happened in some flows
            for cand in os.listdir(release) if os.path.isdir(release) else []:
                if cand.lower().endswith(".exe"):
                    exe_name = os.path.join(release, cand)
                    break
        portable_dir = os.path.join(self.src_dir, "libs", "portable")
        self.run([self._py(), "-m", "pip", "install", "-r", "requirements.txt"],
                 cwd=portable_dir, check=False)
        self.run([self._py(), "generate.py", "-f", release, "-o", ".",
                  "-e", exe_name], cwd=portable_dir)
        packer_exe = os.path.join(self.src_dir, "target", "release",
                                  "rustdesk-portable-packer.exe")
        dest = os.path.join(self.src_dir, f"rustdesk-{version}-install.exe")
        if os.path.isfile(packer_exe):
            shutil.move(packer_exe, dest)
            self.log(f"  ✓ packed portable exe -> {dest}")
        else:
            self.log(f"  ! packer did not produce {packer_exe}")

    def build_linux(self):
        self.log("\n=== Build Linux ===")
        self.setup_vcpkg("x64-linux")
        self.customize_for("linux")
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
            self.run(["sed", "-i", f"s/Version:    .*/Version:    {version}/g", spec],
                     cwd=self.src_dir, check=False)
            # For aarch64, patch the hardcoded x64 bundle path
            if arch_seg != "x64":
                self.run(["sed", "-i", f"s/linux\/x64/linux\/{arch_seg}/g", spec],
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
                dest = os.path.join(
                    self.src_dir, f"{basename}-{version}{suffix}.rpm")
                shutil.move(rpms[0], dest)
                built.append(dest)
                self.log(f"  ✓ created {os.path.basename(dest)}")
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
        # Detect JDK 17 and set JAVA_HOME so Gradle uses it.
        jdk17 = None
        for candidate in (
            "/usr/lib/jvm/java-17-openjdk-amd64",
            "/usr/lib/jvm/java-17-openjdk",
            "/usr/lib/jvm/temurin-17",
            "/Library/Java/JavaVirtualMachines/temurin-17.jdk/Contents/Home",
        ):
            if os.path.isdir(candidate):
                jdk17 = candidate
                break
        if not jdk17:
            # Try to find any java-17 via update-alternatives or common paths
            hits = (_glob.glob("/usr/lib/jvm/*17*")
                    + _glob.glob("/Library/Java/JavaVirtualMachines/*17*/Contents/Home"))
            if hits:
                jdk17 = hits[0]
        if jdk17:
            self.log(f"  · using JDK 17: {jdk17}")
        else:
            self.log("  ! JDK 17 not found — Android build may fail with "
                     "JVM-target mismatch. Install openjdk-17-jdk.")

        # Clean stale Gradle caches from prior JDK 21 attempts
        gradle_cache = os.path.expanduser("~/.gradle/caches")
        if os.path.isdir(gradle_cache):
            for stale in _glob.glob(os.path.join(gradle_cache, "7.*")) + \
                          _glob.glob(os.path.join(gradle_cache, "8.*")):
                shutil.rmtree(stale, ignore_errors=True)
            init_gradle = os.path.expanduser("~/.gradle/init.gradle")
            if os.path.isfile(init_gradle):
                os.remove(init_gradle)

        # Gradle fixes from official CI: kill dead jcenter(), bump heap,
        # use debug signing so APK builds without a release keystore.
        build_gradle = os.path.join(self.src_dir, "flutter", "android",
                                    "build.gradle")
        if os.path.isfile(build_gradle):
            self.run(["sed", "-i", "s/jcenter()/mavenCentral()/g", build_gradle],
                     check=False)
        gradle_properties = os.path.join(self.src_dir, "flutter", "android",
                                         "gradle.properties")
        if os.path.isfile(gradle_properties):
            self.run(["sed", "-i",
                      "s/org.gradle.jvmargs=-Xmx1024M/org.gradle.jvmargs=-Xmx2g/g",
                      gradle_properties], check=False)
        app_build_gradle = os.path.join(self.src_dir, "flutter", "android",
                                        "app", "build.gradle")
        if os.path.isfile(app_build_gradle):
            self.run(["sed", "-i",
                      "s/signingConfigs.release/signingConfigs.debug/g",
                      app_build_gradle], check=False)

        # Compute NDK sysroot for bindgen (hwcodec cross-compile fix).
        # cargo-ndk sets CC/CXX but bindgen uses libclang directly and
        # needs --sysroot to find Android headers instead of host headers.
        ndk_home = os.environ.get("ANDROID_NDK_HOME") or os.environ.get("ANDROID_NDK_ROOT", "")
        ndk_sysroot = ""
        if ndk_home:
            prebuilt = os.path.join(ndk_home, "toolchains", "llvm",
                                    "prebuilt", "linux-x86_64", "sysroot")
            if os.path.isdir(prebuilt):
                ndk_sysroot = prebuilt
            else:
                # macOS host
                prebuilt = os.path.join(ndk_home, "toolchains", "llvm",
                                        "prebuilt", "darwin-x86_64", "sysroot")
                if os.path.isdir(prebuilt):
                    ndk_sysroot = prebuilt

        # Ensure vcpkg is at the pinned commit — build_android_deps.sh
        # calls vcpkg install but doesn't checkout the right version.
        vcpkg_root = os.environ.get("VCPKG_ROOT", "")
        if vcpkg_root and os.path.isdir(vcpkg_root):
            self.log(f"  · vcpkg checkout {self.VCPKG_COMMIT[:8]}")
            self.run(["git", "-C", vcpkg_root, "fetch", "--depth", "1",
                      "origin", self.VCPKG_COMMIT], check=False)
            self.run(["git", "-C", vcpkg_root, "checkout", self.VCPKG_COMMIT],
                     check=False)

        archs = {
            "android-arm64": ("aarch64-linux-android", "android-arm64", "arm64-v8a", "ndk_arm64.sh", "aarch64-linux-android"),
            "android-armv7": ("armv7-linux-androideabi", "android-arm", "armeabi-v7a", "ndk_arm.sh", "arm-linux-androideabi"),
            "android-x86_64": ("x86_64-linux-android", "android-x64", "x86_64", "ndk_x64.sh", "x86_64-linux-android"),
        }
        wanted = [a for a in self.target_ids if a in archs]
        universal = "android-universal" in self.target_ids
        if universal and not wanted:
            wanted = list(archs.keys())  # universal needs all three arch libs

        for tid in wanted:
            target, ftarget, abi, ndk, jni_arch = archs[tid]
            self.log(f"\n-- Android {abi} --")

            # Install vcpkg deps (FFmpeg, etc.) for this ABI via RustDesk's
            # own script — matches official CI.  Without this, hwcodec can't
            # find libavcodec/libavutil headers.
            deps_script = os.path.join(self.src_dir, "flutter",
                                       "build_android_deps.sh")
            if os.path.isfile(deps_script):
                self.log("  · installing vcpkg Android deps")
                bash = self._bash()
                if bash or self.dry_run:
                    self.run([bash or "bash", deps_script, abi],
                             cwd=self.src_dir, check=False)
            else:
                self.log("  ! flutter/build_android_deps.sh not found — "
                         "hwcodec may fail without vcpkg FFmpeg headers")

            self.run(["rustup", "target", "add", target], check=False)
            self.run(["cargo", "install", "cargo-ndk", "--version", "3.1.2", "--locked"],
                     check=False)
            script = f"./flutter/{ndk}"
            bash = self._bash()
            # Build env with NDK sysroot for bindgen
            ndk_env = {}
            if ndk_sysroot:
                ndk_env["BINDGEN_EXTRA_CLANG_ARGS"] = f"--sysroot={ndk_sysroot}"
                ndk_env[f"BINDGEN_EXTRA_CLANG_ARGS_{target.replace('-', '_')}"] = f"--sysroot={ndk_sysroot}"
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

            # Gradle env with JDK 17 to avoid JVM-target mismatch
            gradle_env = {}
            if jdk17:
                gradle_env["JAVA_HOME"] = jdk17
                gradle_env["PATH"] = os.path.join(jdk17, "bin") + ":" + os.environ.get("PATH", "")
            if not universal:
                self.run(["flutter", "build", "apk", "--release",
                          "--target-platform", ftarget, "--split-per-abi"],
                         cwd=os.path.join(self.src_dir, "flutter"), check=False,
                         env=gradle_env if gradle_env else None)
        if universal:
            self.log("\n-- Android universal (all ABIs) --")
            gradle_env = {}
            if jdk17:
                gradle_env["JAVA_HOME"] = jdk17
                gradle_env["PATH"] = os.path.join(jdk17, "bin") + ":" + os.environ.get("PATH", "")
            self.run(["flutter", "build", "apk", "--release"],
                     cwd=os.path.join(self.src_dir, "flutter"), check=False,
                     env=gradle_env if gradle_env else None)
        apk_dir = os.path.join(self.src_dir, "flutter", "build", "app",
                               "outputs", "flutter-apk")
        self._collect(apk_dir, (".apk",), "android")

    def build_macos(self):
        self.log("\n=== Build macOS ===")
        self.customize_for("macos")
        # Install native vcpkg deps (opus, libyuv, libvpx, ffmpeg) for macOS
        # before cargo runs, otherwise magnum-opus/scrap/hwcodec fail on
        # missing headers and libraries.
        self.setup_vcpkg("arm64-osx")
        # Official CI pins Rust 1.81 for macOS (1.75 is for Windows/Linux).
        # M1 builds fail with 1.78+ i128 ABI changes, and 1.81 is the macOS pin.
        # Toolchain host triple must match this machine; cross targets are separate.
        host_triple = self._host_rust_triple()
        toolchain = f"{MAC_RUST_VERSION}-{host_triple}"
        self.run(["rustup", "toolchain", "install", toolchain], check=False)
        mac_targets = {self._mac_target(), host_triple}
        if any("universal" in t for t in self.target_ids):
            mac_targets.update({"aarch64-apple-darwin", "x86_64-apple-darwin"})
        for target in sorted(mac_targets):
            self.run(["rustup", "target", "add", target,
                      "--toolchain", toolchain], check=False)
        self.run(["rustup", "default", toolchain], check=False)
        self._patch_macos_podfile()
        self._patch_macos_build_py()
        self._patch_macos_generated_bridge()
        self.run([self._py(), "build.py", "--flutter", "--hwcodec"],
                 cwd=self.src_dir, check=False)
        env = self._env()
        app_name = self.config.get("appname", "RustDesk") or "RustDesk"
        app_dir = os.path.join(self.src_dir, "flutter", "build", "macos",
                               "Build", "Products", "Release")
        app_bundle = os.path.join(app_dir, f"{app_name}.app")
        if os.path.isdir(app_dir):
            # Write custom_.txt next to the .app (Category B — runtime pickup)
            customize.write_custom_txt(app_dir, env, log=self.log)
        if os.path.isdir(app_bundle):
            # Also write custom_.txt INSIDE the app bundle's Contents/Resources/
            # so it travels with the .app inside the DMG (matches VenimK workflow).
            resources_dir = os.path.join(app_bundle, "Contents", "Resources")
            os.makedirs(resources_dir, exist_ok=True)
            customize.write_custom_txt(resources_dir, env, log=self.log)
        self._create_macos_dmg()
        self._collect(self.src_dir, (".dmg",), "macos")

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
        """Patch SqfliteImport.h to use `@import FMDB;` instead of
        `#import <fmdb/FMDB.h>`.

        The angle-bracket header import fails during module building on
        Xcode 26 because the sqflite modulemap doesn't declare a dependency
        on the FMDB module. Using `@import FMDB;` (Clang module import)
        resolves correctly because FMDB.framework has a valid modulemap.
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

    def _create_macos_dmg(self):
        """Create a .dmg from the built RustDesk.app using create-dmg.

        build.py has the create-dmg step commented out, so the orchestrator
        handles DMG packaging after the Flutter build completes.
        """
        if self.dry_run:
            self.log("  (would create .dmg)")
            return
        app_name = self.config.get("appname", "RustDesk") or "RustDesk"
        app_basename = f"{app_name}.app"
        app = os.path.join(self.src_dir, "flutter", "build", "macos",
                           "Build", "Products", "Release", app_basename)
        if not os.path.isdir(app):
            self.log(f"  ! {app_basename} not found — skipping DMG creation")
            return
        create_dmg = shutil.which("create-dmg", path=self._effective_path())
        if not create_dmg:
            self.log("  ! create-dmg not found — skipping DMG creation")
            return
        version = self.config.get("version", "")
        basename = self._output_basename()
        dmg_name = f"{basename}-{version}.dmg" if version else f"{basename}.dmg"
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
    def _collect(self, root, exts, platform):
        os.makedirs(self.out_dir, exist_ok=True)
        if self.dry_run or not os.path.isdir(root):
            self.log(f"  (would collect {'/'.join(exts)} from {root})")
            return
        appname = self.config.get("appname", "RustDesk")
        basename = self._output_basename()
        # Only collect files whose base name starts with the app name or
        # "rustdesk" — this avoids picking up dependency .deb files that
        # appimage-builder downloads into appimage/ and other build dirs.
        prefixes = tuple(p.lower() for p in {appname, "rustdesk", "app-"})
        # Directories to skip entirely during collection.
        skip_dirs = {"appimage", "tmpdeb", ".git"}
        found = 0
        for dp, dirnames, files in os.walk(root):
            # prune skip dirs in-place so os.walk doesn't descend into them
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for f in files:
                if not any(f.endswith(e) for e in exts):
                    continue
                if not f.lower().startswith(prefixes):
                    continue
                src = os.path.join(dp, f)
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
                dest = os.path.join(self.out_dir, out_name)
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
