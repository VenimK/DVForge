"""
toolchains.py — optionally auto-download the build toolchains into a local
`.toolchains/` folder next to the app, with **no admin rights and no system
changes**. We extract portable archives, then record the env vars (PATH
additions, LIBCLANG_PATH, VCPKG_ROOT, ANDROID_NDK_HOME, JAVA_HOME) to
`.toolchains/env.json`. `app.py` loads that file on startup and applies it, so
detection immediately sees the freshly-installed tools.

What can be auto-installed (portable, deterministic):
    flutter · llvm · vcpkg · android_ndk · java · rust(add 1.75 via rustup)

What can't (needs a real system installer / elevation) stays a guided hint
or a package-manager install:
    msbuild / VS Build Tools · nuget · .NET SDK · Xcode

NOTE: the download URLs are the official ones but versions/paths do drift —
if a download 404s, update the registry below. Everything writes under
`.toolchains/`; delete that folder to start clean.
"""

import json
import os
import platform
import shutil
import ssl
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import zipfile


PINNED = {"flutter": "3.24.5", "llvm": "15.0.6", "ndk": "r28c",
          "rust": "1.75", "vcpkg": "120deac3062162151622ca4860575a33844ba10b"}

# rough download / on-disk footprint, shown in the UI so people know what a tool
# costs before installing and what deleting it will free.
SIZE_HINTS = {
    "flutter":     {"download": "~1.1 GB", "disk": "~2.8 GB", "version": "3.24.5"},
    "llvm":        {"download": "~150 MB", "disk": "~2.5 GB", "version": "15.0.6"},
    "android_ndk": {"download": "~700 MB", "disk": "~2.6 GB", "version": "r28c"},
    "java":        {"download": "~190 MB", "disk": "~330 MB", "version": "17 (Temurin)"},
    "vcpkg":       {"download": "~10 MB",  "disk": "~600 MB", "version": "pinned"},
    "rust":        {"download": "~250 MB", "disk": "~800 MB", "version": "1.75"},
    "vs_buildtools": {"download": "~4 MB", "disk": "~4-6 GB", "version": "2022 (C++)"},
    "nuget":       {"download": "~5 MB",   "disk": "~20 MB",  "version": "CLI"},
    "dotnet":      {"download": "~200 MB", "disk": "~500 MB", "version": "8.0 SDK"},
    "sccache":     {"download": "~30 MB",  "disk": "~100 MB", "version": "0.11.0"},
    "imagemagick": {"download": "~60 MB",  "disk": "~200 MB", "version": "7.x"},
    "potrace":     {"download": "~1 MB",   "disk": "~5 MB",   "version": "1.16"},
}

WIN = platform.system() == "Windows"
EXE = ".exe" if WIN else ""


def _system():
    return {"Darwin": "macOS"}.get(platform.system(), platform.system())


def _arch():
    m = platform.machine().lower()
    if m in ("x86_64", "amd64", "x64"):
        return "x86_64"
    if m in ("arm64", "aarch64"):
        return "arm64"
    return m


# ---------------------------------------------------------------------------
# tool registry: url per (os, arch), archive kind, and how to wire env after
# ---------------------------------------------------------------------------
FLUTTER_BASE = "https://storage.googleapis.com/flutter_infra_release/releases/stable"
LLVM_REL = "https://github.com/llvm/llvm-project/releases/download/llvmorg-15.0.6"
NDK_BASE = "https://dl.google.com/android/repository"
ADOPTIUM = "https://api.adoptium.net/v3/binary/latest/17/ga"

TOOLS = {
    "flutter": {
        "label": "Flutter SDK 3.24.5",
        "kind": "archive",
        "urls": {
            ("Windows", "x86_64"): (f"{FLUTTER_BASE}/windows/flutter_windows_3.24.5-stable.zip", "zip"),
            ("Linux", "x86_64"):   (f"{FLUTTER_BASE}/linux/flutter_linux_3.24.5-stable.tar.xz", "tar"),
            ("macOS", "x86_64"):   (f"{FLUTTER_BASE}/macos/flutter_macos_3.24.5-stable.zip", "zip"),
            ("macOS", "arm64"):    (f"{FLUTTER_BASE}/macos/flutter_macos_arm64_3.24.5-stable.zip", "zip"),
        },
        "marker": os.path.join("bin", "flutter.bat" if WIN else "flutter"),
    },
    "llvm": {
        "label": "LLVM / clang 15.0.6",
        "kind": "archive",
        # Windows is special: the official LLVM-15.0.6-win64.exe is an NSIS
        # installer that (a) requires admin and (b) when LLVM is already
        # installed, tries to silently *uninstall* the previous copy first —
        # that silent uninstall fails and pops a blocking "Uninstall failed"
        # dialog mid-build. bindgen/ffigen only need libclang.dll, not the
        # full clang toolchain, so on Windows we fetch just libclang via pip
        # into .toolchains/llvm (no admin, no NSIS, no uninstall, ~23 MB).
        # Linux/macOS keep the portable LLVM 15.0.6 tarball (they ship one;
        # Windows 15.0.6 does not).
        "urls": {
            ("Windows", "x86_64"): ("libclang==15.0.6.1", "pip"),
            ("Linux", "x86_64"):   (f"{LLVM_REL}/clang+llvm-15.0.6-x86_64-linux-gnu-ubuntu-18.04.tar.xz", "tar"),
            ("Linux", "arm64"):    (f"{LLVM_REL}/clang+llvm-15.0.6-aarch64-linux-gnu.tar.xz", "tar"),
            ("macOS", "x86_64"):   (f"{LLVM_REL}/clang+llvm-15.0.6-x86_64-apple-darwin21.0.tar.xz", "tar"),
            ("macOS", "arm64"):    (f"{LLVM_REL}/clang+llvm-15.0.6-arm64-apple-darwin21.0.tar.xz", "tar"),
        },
        # bin/clang.exe on Linux/macOS; on Windows the pip path lays down
        # bin/libclang.dll and _locate/_env_for/check_llvm accept either.
        "marker": os.path.join("bin", "clang" + EXE),
    },
    "android_ndk": {
        "label": "Android NDK r28c",
        "kind": "archive",
        "urls": {
            ("Windows", "x86_64"): (f"{NDK_BASE}/android-ndk-r28c-windows.zip", "zip"),
            ("Linux", "x86_64"):   (f"{NDK_BASE}/android-ndk-r28c-linux.zip", "zip"),
            ("macOS", "x86_64"):   (f"{NDK_BASE}/android-ndk-r28c-darwin.dmg", "dmg"),
            ("macOS", "arm64"):    (f"{NDK_BASE}/android-ndk-r28c-darwin.dmg", "dmg"),
        },
        "marker": "source.properties",
    },
    "java": {
        "label": "JDK 17 (Temurin)",
        "kind": "archive",
        "urls": {
            ("Windows", "x86_64"): (f"{ADOPTIUM}/windows/x64/jdk/hotspot/normal/eclipse", "zip"),
            ("Linux", "x86_64"):   (f"{ADOPTIUM}/linux/x64/jdk/hotspot/normal/eclipse", "tar"),
            ("Linux", "arm64"):    (f"{ADOPTIUM}/linux/aarch64/jdk/hotspot/normal/eclipse", "tar"),
            ("macOS", "x86_64"):   (f"{ADOPTIUM}/mac/x64/jdk/hotspot/normal/eclipse", "tar"),
            ("macOS", "arm64"):    (f"{ADOPTIUM}/mac/aarch64/jdk/hotspot/normal/eclipse", "tar"),
        },
        "marker": os.path.join("bin", "java" + EXE),
    },
    "vcpkg": {
        "label": "vcpkg (native deps)",
        "kind": "git",
        "repo": "https://github.com/microsoft/vcpkg",
        "marker": "bootstrap-vcpkg." + ("bat" if WIN else "sh"),
    },
    "rust": {
        "label": "Rust 1.75 (via rustup)",
        "kind": "rust",
        "urls": {
            ("Windows", "x86_64"): ("https://win.rustup.rs/x86_64", "rustup-init.exe"),
            ("Linux", "x86_64"):   ("https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init", "rustup-init"),
            ("Linux", "arm64"):    ("https://static.rust-lang.org/rustup/dist/aarch64-unknown-linux-gnu/rustup-init", "rustup-init"),
            ("macOS", "x86_64"):   ("https://static.rust-lang.org/rustup/dist/x86_64-apple-darwin/rustup-init", "rustup-init"),
            ("macOS", "arm64"):    ("https://static.rust-lang.org/rustup/dist/aarch64-apple-darwin/rustup-init", "rustup-init"),
        },
    },
    # Build Tools for Visual Studio — the command-line-only subset of VS (no IDE).
    # Provides the MSVC linker (link.exe) that Rust needs on Windows, plus MSBuild
    # for the .msi. Windows only; large (~3-5 GB) and needs one UAC prompt.
    "vs_buildtools": {
        "label": "VS Build Tools (C++ / MSVC linker + MSBuild)",
        "kind": "vs",
        "urls": {
            ("Windows", "x86_64"): ("https://aka.ms/vs/17/release/vs_BuildTools.exe", "vs"),
        },
    },
    # NuGet CLI + nuget.org feed — restore WiX CustomActions packages for MSI.
    # Chocolatey nuget often ships with zero package sources; install step
    # always re-registers nuget.org.
    "nuget": {
        "label": "NuGet CLI (MSI / WiX packages)",
        "kind": "nuget",
        "marker": "nuget",
        "packages": {
            "Windows": ("choco", ["install", "-y", "nuget.commandline"]),
        },
    },
    # .NET 8 SDK — required to resolve WixToolset.Sdk for WiX 4 Package.wixproj.
    "dotnet": {
        "label": ".NET 8 SDK (WiX Toolset / MSI)",
        "kind": "package",
        "marker": "dotnet",
        "packages": {
            "Windows": ("winget", [
                "install", "--id", "Microsoft.DotNet.SDK.8", "-e",
                "--accept-source-agreements",
                "--accept-package-agreements",
                "--disable-interactivity",
            ]),
        },
    },
    # sccache — shared compilation cache for Rust/C/C++. Installed via cargo,
    # works on all platforms. Set RUSTC_WRAPPER=sccache to speed up rebuilds.
    "sccache": {
        "label": "sccache (Rust/C++ compilation cache)",
        "kind": "cargo",
        "version": "0.11.0",
    },
    # ImageMagick — needed for icon/logo resizing and ICO/ICNS generation.
    # Installed via the system package manager (brew/apt/choco).
    "imagemagick": {
        "label": "ImageMagick (icon/logo branding)",
        "kind": "package",
        "marker": "magick",
        "packages": {
            "macOS":   ("brew", ["install", "imagemagick"]),
            "Linux":   ("sudo", ["apt", "install", "-y", "imagemagick"]),
            "Windows": ("choco", ["install", "-y", "imagemagick"]),
        },
    },
    # potrace — converts PNG logos to SVG. Optional but useful.
    "potrace": {
        "label": "potrace (PNG→SVG logo conversion)",
        "kind": "package",
        "marker": "potrace",
        "packages": {
            "macOS":   ("brew", ["install", "potrace"]),
            "Linux":   ("sudo", ["apt", "install", "-y", "potrace"]),
            "Windows": ("choco", ["install", "-y", "potrace"]),
        },
    },
}

# which detection id each tool satisfies (prereqs.py ids)
SATISFIES = {"flutter": "flutter", "llvm": "llvm", "android_ndk": "android_ndk",
             "java": "java", "vcpkg": "vcpkg", "rust": "rust",
             "vs_buildtools": "msbuild", "nuget": "nuget", "dotnet": "dotnet",
             "sccache": "sccache",
             "imagemagick": "imagemagick", "potrace": "potrace"}


def tools_dir(root):
    return os.path.join(root, ".toolchains")


def dir_size(path):
    """Total bytes under a directory (best-effort)."""
    total = 0
    for dp, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit != "GB" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def installed_info(root):
    """Which tools are present in the local .toolchains folder, and their size."""
    base = tools_dir(root)
    out = {}
    total = 0
    for tid in TOOLS:
        home = os.path.join(base, tid)
        if os.path.isdir(home):
            sz = dir_size(home)
            total += sz
            out[tid] = {"local": True, "bytes": sz, "size": human_size(sz)}
        else:
            out[tid] = {"local": False, "bytes": 0, "size": ""}
    out["_total"] = {"bytes": total, "size": human_size(total)}
    return out


def remove_tool(tid, root, log=lambda m: None):
    """Delete a locally-installed toolchain and drop its env entries."""
    home = os.path.join(tools_dir(root), tid)
    freed = 0
    if os.path.isdir(home):
        freed = dir_size(home)
        shutil.rmtree(home, ignore_errors=True)
        log(f"removed {tid} ({human_size(freed)})")
    else:
        log(f"{tid} was not installed locally")
    # rewrite env.json without paths/vars pointing at the removed home
    d = _load_env(root)
    d["path"] = [p for p in d.get("path", []) if home not in p]
    d["vars"] = {k: v for k, v in d.get("vars", {}).items() if home not in str(v)}
    os.makedirs(tools_dir(root), exist_ok=True)
    with open(env_path(root), "w") as f:
        json.dump(d, f, indent=2)
    return {"removed": tid, "freed": freed, "freed_human": human_size(freed)}


def installable(host_os=None, host_arch=None):
    """Return {id: {label, ok, reason}} for every tool, on this host."""
    host_os = host_os or _system()
    host_arch = host_arch or _arch()
    out = {}
    for tid, spec in TOOLS.items():
        ok, reason = True, ""
        # Android builds are not supported on macOS — don't offer the NDK for
        # install there (the build_android path is kept for Linux/Windows).
        if tid == "android_ndk" and host_os == "macOS":
            ok, reason = False, "Android builds are not supported on macOS"
        elif spec["kind"] == "archive":
            if (host_os, host_arch) not in spec["urls"]:
                ok, reason = False, f"no portable {spec['label']} for {host_os}/{host_arch}"
        elif spec["kind"] == "git":
            if not shutil.which("git"):
                ok, reason = False, "git is required to fetch vcpkg"
        elif spec["kind"] == "rust":
            if (host_os, host_arch) not in spec["urls"]:
                ok, reason = False, f"no rustup-init for {host_os}/{host_arch}"
        elif spec["kind"] == "vs":
            if host_os != "Windows":
                ok, reason = False, "Visual Studio Build Tools are Windows-only"
        elif spec["kind"] == "nuget":
            if host_os != "Windows":
                ok, reason = False, "NuGet CLI is only needed for Windows MSI builds"
            else:
                pkgs = spec.get("packages", {})
                if host_os in pkgs:
                    mgr = pkgs[host_os][0]
                    # Installable if the package manager is present OR nuget is
                    # already on PATH (we only need to fix the nuget.org source).
                    if not shutil.which(mgr) and not shutil.which(
                            spec.get("marker", "nuget")):
                        ok, reason = (
                            False,
                            f"{mgr} not found — install NuGet manually: "
                            "https://www.nuget.org/downloads")
        elif spec["kind"] == "cargo":
            if not shutil.which("cargo"):
                ok, reason = False, "Rust/cargo is required to install this"
        elif spec["kind"] == "package":
            pkgs = spec.get("packages", {})
            if (host_os, ) not in {(k[0],) for k in pkgs} and host_os not in pkgs:
                ok, reason = False, f"no package install for {host_os}"
            elif host_os in pkgs:
                mgr = pkgs[host_os][0]
                if not shutil.which(mgr):
                    ok, reason = False, f"{mgr} not found — install {spec['label']} manually"
        out[tid] = {"label": spec["label"], "ok": ok, "reason": reason}
    return out


# ---------------------------------------------------------------------------
# download + extract
# ---------------------------------------------------------------------------

def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _download(url, dest, log):
    log(f"  ↓ {url}")
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "rustdesk-local-builder"})
    with urllib.request.urlopen(req, context=ctx, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length", 0))
        got = 0
        last = 0.0
        while True:
            chunk = r.read(262144)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            now = time.time()
            if now - last > 0.5:
                last = now
                if total:
                    pct = got * 100 // total
                    log(f"    {pct:3d}%  {_human(got)} / {_human(total)}")
                else:
                    log(f"    {_human(got)}")
    log(f"  ✓ downloaded {_human(os.path.getsize(dest))}")


def _extract(archive, kind, dest, log):
    log(f"  extracting → {dest}")
    os.makedirs(dest, exist_ok=True)
    if kind == "zip":
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
    elif kind == "dmg":
        _extract_dmg(archive, dest, log)
    else:  # tar.* (xz/gz auto-detected by mode 'r:*')
        with tarfile.open(archive, "r:*") as t:
            t.extractall(dest)


def _extract_dmg(dmg, dest, log):
    """macOS only: mount the .dmg, copy its contents out, then detach."""
    mnt = tempfile.mkdtemp(prefix="rdlb-dmg-")
    log("  mounting dmg (hdiutil)")
    subprocess.check_call(["hdiutil", "attach", "-nobrowse", "-quiet",
                           "-mountpoint", mnt, dmg])
    try:
        for name in os.listdir(mnt):
            src = os.path.join(mnt, name)
            dst = os.path.join(dest, name)
            if os.path.isdir(src):
                shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
    finally:
        subprocess.call(["hdiutil", "detach", "-quiet", mnt])
        shutil.rmtree(mnt, ignore_errors=True)


def _locate(base, marker, log):
    """Find the tool home under `base` — the dir that contains `marker`."""
    # common case: base itself, or a single top-level child
    if os.path.exists(os.path.join(base, marker)):
        return base
    for child in sorted(os.listdir(base)):
        p = os.path.join(base, child)
        if os.path.isdir(p) and os.path.exists(os.path.join(p, marker)):
            return p
    # deeper walk (NDK zip nests android-ndk-r28c/; some tars nest bin/)
    for dirpath, _dirs, _files in os.walk(base):
        if os.path.exists(os.path.join(dirpath, marker)):
            return dirpath
    log(f"  ! could not find marker '{marker}' under {base}")
    return base


def _libclang_dir(home):
    """Return the directory that contains libclang.{dylib,so,dll}."""
    names = ("libclang.dylib", "libclang.so", "libclang.dll")
    for sub in ("lib", "bin"):
        d = os.path.join(home, sub)
        if any(os.path.isfile(os.path.join(d, n)) for n in names):
            return d
    # Fall back to lib on Unix, bin on Windows.
    return os.path.join(home, "lib") if not WIN else os.path.join(home, "bin")


def _env_for(tid, home):
    """Env vars + PATH additions a tool needs, given its install home."""
    bindir = os.path.join(home, "bin")
    if tid == "flutter":
        return {"vars": {}, "path": [bindir]}
    if tid == "llvm":
        # LIBCLANG_PATH alone is what bindgen/ffigen need. Putting the
        # tarball's bin/ on PATH on macOS shadows Apple clang and breaks
        # compiles (missing SDK headers). Keep bin/ on PATH only for Windows,
        # where the system rarely has a usable clang/llvm-config.
        env = {"vars": {"LIBCLANG_PATH": _libclang_dir(home)}, "path": []}
        if WIN:
            env["path"] = [bindir]
        return env
    if tid == "java":
        # macOS Temurin nests Contents/Home
        mac_home = os.path.join(home, "Contents", "Home")
        jhome = mac_home if os.path.isdir(mac_home) else home
        return {"vars": {"JAVA_HOME": jhome}, "path": [os.path.join(jhome, "bin")]}
    if tid == "android_ndk":
        return {"vars": {"ANDROID_NDK_HOME": home, "ANDROID_NDK_ROOT": home}, "path": []}
    if tid == "vcpkg":
        return {"vars": {"VCPKG_ROOT": home}, "path": [home]}
    if tid == "sccache":
        return {"vars": {"RUSTC_WRAPPER": "sccache"}, "path": []}
    return {"vars": {}, "path": []}


# ---------------------------------------------------------------------------
# per-tool install
# ---------------------------------------------------------------------------

def install_one(tid, root, log, cancelled=lambda: False):
    spec = TOOLS[tid]
    host_os, host_arch = _system(), _arch()
    base = tools_dir(root)
    os.makedirs(base, exist_ok=True)
    home_target = os.path.join(base, tid)

    log(f"\n=== Installing {spec['label']} ===")

    if spec["kind"] == "cargo":
        # Tools installed via `cargo install` (e.g. sccache).
        # They land in ~/.cargo/bin, which is already on PATH via the rust env.
        # Pin versions compatible with our oldest Rust (1.75).
        pinned_ver = spec.get("version")
        if shutil.which(tid + EXE):
            log(f"  ✓ {tid} already installed")
        else:
            if pinned_ver:
                log(f"  cargo install {tid} --version {pinned_ver} --locked")
                rc = subprocess.call(["cargo", "install", tid,
                                      "--version", pinned_ver, "--locked"])
            else:
                log(f"  cargo install {tid} --locked")
                rc = subprocess.call(["cargo", "install", tid, "--locked"])
            if rc != 0:
                raise RuntimeError(f"cargo install {tid} failed")
        cargo_bin = os.path.join(os.path.expanduser("~"), ".cargo", "bin")
        env = _env_for(tid, cargo_bin)
        log(f"  ✓ {tid} ready at {cargo_bin}")
        return {"tool": tid, "home": cargo_bin, "env": env}

    if spec["kind"] == "package":
        # System package manager install (brew/apt/choco/winget).
        # The tool lands in the system PATH, not .toolchains — no env wiring needed.
        host_os = _system()
        mgr, args = spec["packages"][host_os]
        marker = spec.get("marker", tid)
        already = shutil.which(marker)
        # .NET may live under Program Files before PATH is refreshed
        if not already and tid == "dotnet" and WIN:
            for cand in (
                os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                             "dotnet", "dotnet.exe"),
                r"C:\Program Files\dotnet\dotnet.exe",
            ):
                if os.path.isfile(cand):
                    already = cand
                    break
        if already:
            log(f"  ✓ {tid} already installed ({already})")
        else:
            log(f"  running: {mgr} {' '.join(args)}")
            rc = subprocess.call([mgr] + args)
            # winget returns 0 on success; -1978335189 (0x8A15000B) means
            # "already installed" on some versions — treat as success.
            if rc != 0 and not (mgr == "winget" and rc in (-1978335189, -1978335212)):
                raise RuntimeError(f"{mgr} install failed for {tid} (exit {rc})")
        # Refresh PATH for this process so verification sees new installs
        if WIN:
            machine = os.environ.get("Path", os.environ.get("PATH", ""))
            # Pull Machine+User PATH via a lightweight PowerShell call is heavy;
            # just prepend the well-known dotnet location.
            pf_dotnet = os.path.join(
                os.environ.get("ProgramFiles", r"C:\Program Files"), "dotnet")
            if os.path.isdir(pf_dotnet) and pf_dotnet not in os.environ.get("PATH", ""):
                os.environ["PATH"] = pf_dotnet + os.pathsep + os.environ.get("PATH", "")
        found = shutil.which(marker)
        if not found and tid == "dotnet" and WIN:
            cand = os.path.join(
                os.environ.get("ProgramFiles", r"C:\Program Files"),
                "dotnet", "dotnet.exe")
            if os.path.isfile(cand):
                found = cand
        if found:
            log(f"  ✓ {tid} installed via {mgr}" if not already
                else f"  ✓ {tid} ready")
        else:
            log(f"  ! {tid} install finished but binary not found on PATH — "
                f"re-open the app or start a new terminal")
        # Wire Program Files\dotnet into session PATH when we installed it
        env_path = []
        if tid == "dotnet" and WIN:
            pf_dotnet = os.path.join(
                os.environ.get("ProgramFiles", r"C:\Program Files"), "dotnet")
            if os.path.isdir(pf_dotnet):
                env_path = [pf_dotnet]
        return {"tool": tid, "home": "", "env": {"vars": {}, "path": env_path}}

    if spec["kind"] == "nuget":
        # Install NuGet CLI (if needed) and always ensure nuget.org is registered.
        from . import prereqs as _prereqs  # local import avoids circulars at load
        host_os = _system()
        marker = spec.get("marker", "nuget")
        if not shutil.which(marker):
            if host_os not in spec.get("packages", {}):
                raise RuntimeError("NuGet install is Windows-only")
            mgr, args = spec["packages"][host_os]
            if not shutil.which(mgr):
                raise RuntimeError(
                    f"{mgr} not found. Install NuGet from "
                    "https://www.nuget.org/downloads or: "
                    "choco install nuget.commandline")
            log(f"  running: {mgr} {' '.join(args)}")
            rc = subprocess.call([mgr] + args)
            if rc != 0:
                raise RuntimeError(f"{mgr} install failed for nuget (exit {rc})")
        else:
            log(f"  ✓ nuget already on PATH ({shutil.which(marker)})")
        nuget_exe = shutil.which(marker) or "nuget"
        log("  ensuring nuget.org package source…")
        if not _prereqs.ensure_nuget_org(nuget_exe, log=log):
            raise RuntimeError(
                "Could not register nuget.org. Run manually: "
                "nuget sources Add -Name nuget.org "
                "-Source https://api.nuget.org/v3/index.json")
        log("  ✓ nuget ready (CLI + nuget.org)")
        return {"tool": tid, "home": "", "env": {"vars": {}, "path": []}}

    if spec["kind"] == "rust":
        # Download rustup-init and install 1.75 per-user (~/.cargo, ~/.rustup).
        # No admin needed. --no-modify-path: we manage PATH via env.json instead.
        url, _label = spec["urls"][(host_os, host_arch)]
        with tempfile.TemporaryDirectory() as tmp:
            init = os.path.join(tmp, "rustup-init" + (".exe" if WIN else ""))
            _download(url, init, log)
            if not WIN:
                os.chmod(init, 0o755)
            log("  installing Rust 1.75 (rustup-init -y, minimal profile)")
            # (no --no-modify-path: let rustup add ~/.cargo/bin to PATH for the
            #  user, so cargo works in any new terminal too. We also wire it into
            #  our own env.json for this app.)
            rc = subprocess.call([init, "-y", "--default-toolchain", PINNED["rust"],
                                  "--profile", "minimal"])
            if rc != 0:
                raise RuntimeError("rustup-init failed")
        cargo_bin = os.path.join(os.path.expanduser("~"), ".cargo", "bin")
        log(f"  ✓ Rust installed; cargo bin: {cargo_bin}")
        # home stays "" (rust lives in ~/.cargo, not .toolchains) but we DO wire PATH
        return {"tool": tid, "home": "", "env": {"vars": {}, "path": [cargo_bin]}}

    if spec["kind"] == "vs":
        # Build Tools for Visual Studio (C++). Bootstrapper self-elevates; we run
        # it elevated and wait. Installs the MSVC toolset (link.exe) + Windows SDK
        # + MSBuild to Program Files. cargo/rustc then find link.exe via vswhere,
        # no PATH wiring needed. Large download handled by the installer itself.
        url, _ = spec["urls"][(host_os, host_arch)]
        with tempfile.TemporaryDirectory() as tmp:
            boot = os.path.join(tmp, "vs_BuildTools.exe")
            _download(url, boot, log)
            if cancelled():
                raise RuntimeError("cancelled")
            log("  launching the Visual Studio Build Tools installer.")
            log("  → Accept the UAC prompt. This installs the C++ MSVC toolset")
            log("    (link.exe) + Windows SDK + MSBuild — a few GB, several minutes.")
            args = ("--quiet --wait --norestart --nocache "
                    "--add Microsoft.VisualStudio.Workload.VCTools "
                    "--includeRecommended")
            ps = (f"$p = Start-Process -FilePath '{boot}' -ArgumentList "
                  f"'{args}' -Verb RunAs -Wait -PassThru; exit $p.ExitCode")
            rc = subprocess.call(["powershell", "-NoProfile", "-ExecutionPolicy",
                                  "Bypass", "-Command", ps])
            # VS bootstrapper returns 3010 for "success, reboot recommended"
            if rc not in (0, 3010):
                raise RuntimeError(
                    f"VS Build Tools installer exited with code {rc}. If UAC was "
                    "declined, try again; otherwise install 'Desktop development "
                    "with C++' from https://aka.ms/vs/17/release/vs_BuildTools.exe")
        log("  ✓ VS Build Tools install finished. Click re-scan; cargo will now "
            "find link.exe.")
        # lives in Program Files, not .toolchains — no env wiring here.
        return {"tool": tid, "home": "", "env": {"vars": {}, "path": []}}

    if spec["kind"] == "git":
        if os.path.isdir(home_target):
            log("  already present, updating")
        else:
            log(f"  git clone {spec['repo']}")
            rc = subprocess.call(["git", "clone", spec["repo"], home_target])
            if rc != 0:
                raise RuntimeError("git clone failed")
        # bootstrap so the vcpkg binary exists
        boot = os.path.join(home_target, "bootstrap-vcpkg." + ("bat" if WIN else "sh"))
        log("  bootstrapping vcpkg")
        subprocess.call([boot] if WIN else ["bash", boot])
        env = _env_for(tid, home_target)
        return {"tool": tid, "home": home_target, "env": env}

    # archive
    url, arch_kind = spec["urls"][(host_os, host_arch)]
    if cancelled():
        raise RuntimeError("cancelled")

    if arch_kind == "pip":
        # Windows libclang via pip — no admin, no NSIS installer, no "Uninstall
        # failed" dialog. `url` here is a pip requirement like "libclang==15.0.6.1".
        # We install it into .toolchains/llvm with --target, then hoist the
        # bundled libclang.dll up to bin/ so _locate / _env_for / check_llvm
        # (which look under bin/ and lib/) find it with no further changes.
        req = url
        if os.path.isdir(home_target):
            shutil.rmtree(home_target, ignore_errors=True)
        os.makedirs(home_target, exist_ok=True)
        # Prefer the current interpreter's pip so we don't depend on a `pip`
        # binary being on PATH (py -m pip is the reliable Windows form).
        import sys as _sys
        pip_cmd = [_sys.executable, "-m", "pip", "install", "--no-input",
                   "--target", home_target, req]
        log(f"  installing {req} via pip (libclang only — no admin needed)")
        log("  $ " + " ".join(pip_cmd))
        rc = subprocess.call(pip_cmd)
        if rc != 0:
            raise RuntimeError(
                f"pip install {req} failed (exit {rc}). Ensure Python's pip is "
                "available; or install LLVM 15 manually and set LIBCLANG_PATH.")
        if cancelled():
            raise RuntimeError("cancelled")
        # Locate the DLL the wheel dropped (clang/native/libclang.dll) and
        # copy it into bin/ so the rest of the pipeline finds it uniformly.
        bindir = os.path.join(home_target, "bin")
        os.makedirs(bindir, exist_ok=True)
        found_dll = None
        for dp, _dirs, files in os.walk(home_target):
            for fn in files:
                if fn.lower() == "libclang.dll":
                    found_dll = os.path.join(dp, fn)
                    break
            if found_dll:
                break
        if not found_dll:
            raise RuntimeError(
                "pip installed libclang but libclang.dll was not found under "
                f"{home_target} — the wheel layout may have changed.")
        dst_dll = os.path.join(bindir, "libclang.dll")
        if os.path.abspath(found_dll) != os.path.abspath(dst_dll):
            shutil.copy2(found_dll, dst_dll)
        env = _env_for(tid, home_target)
        log(f"  ✓ libclang installed — LIBCLANG_PATH = {env['vars'].get('LIBCLANG_PATH')}")
        return {"tool": tid, "home": home_target, "env": env}
    
    if arch_kind == "nsis":
        # The official LLVM Windows installer is requireAdministrator, so a plain
        # silent /S install fails with WinError 740 (needs elevation). We run it
        # elevated through one UAC prompt. A temp .bat carries the /D path so
        # spaces in it survive (NSIS /D must be the unquoted rest of the line).
        with tempfile.TemporaryDirectory() as tmp:
            exe = os.path.join(tmp, tid + "-setup.exe")
            _download(url, exe, log)
            if cancelled():
                raise RuntimeError("cancelled")
            if os.path.isdir(home_target):
                shutil.rmtree(home_target, ignore_errors=True)
            os.makedirs(home_target, exist_ok=True)
            bat = os.path.join(tmp, "install_llvm.bat")
            with open(bat, "w") as f:
                f.write("@echo off\r\n")
                f.write(f'"{exe}" /S /D={home_target}\r\n')
            log("  LLVM's Windows installer needs admin — a UAC prompt will appear.")
            log("  (LLVM is only needed for Windows desktop builds, not Android.)")
            ps = (f"$p = Start-Process -FilePath '{bat}' -Verb RunAs -Wait -PassThru; "
                  f"exit $p.ExitCode")
            rc = subprocess.call(["powershell", "-NoProfile", "-ExecutionPolicy",
                                  "Bypass", "-Command", ps])
            if rc != 0:
                raise RuntimeError(
                    "elevated LLVM install did not complete (UAC declined or failed). "
                    "You can install LLVM 15 manually and set LIBCLANG_PATH, or skip "
                    "it — it's only needed for Windows desktop builds.")
        home = _locate(home_target, spec["marker"], log)
        env = _env_for(tid, home)
        if os.path.exists(os.path.join(home, spec["marker"])):
            log(f"  ✓ installed at {home}")
        else:
            log(f"  ! installer finished but clang not found under {home_target}")
        return {"tool": tid, "home": home, "env": env}

    ext = {"zip": ".zip", "dmg": ".dmg"}.get(arch_kind, ".tar")
    with tempfile.TemporaryDirectory() as tmp:
        fname = os.path.join(tmp, tid + ext)
        _download(url, fname, log)
        if cancelled():
            raise RuntimeError("cancelled")
        # clean prior install
        if os.path.isdir(home_target):
            shutil.rmtree(home_target, ignore_errors=True)
        _extract(fname, arch_kind, home_target, log)

    # macOS NDK r28c ships as a .app bundle inside the DMG. The DMG root
    # has a source.properties but the actual NDK (toolchains/, build/, etc.)
    # lives at AndroidNDK*.app/Contents/NDK/. _locate finds the root-level
    # source.properties first and returns the wrong directory.
    if tid == "android_ndk" and _system() == "macOS":
        home = None
        for child in sorted(os.listdir(home_target)):
            if child.endswith(".app"):
                app_ndk = os.path.join(home_target, child, "Contents", "NDK")
                if os.path.isdir(os.path.join(app_ndk, "toolchains")):
                    home = app_ndk
                    break
        if home is None:
            home = _locate(home_target, spec["marker"], log)
        # Create symlinks from home_target → real NDK root so that stale
        # references to .toolchains/android_ndk/toolchains/... still resolve.
        if home != home_target and os.path.isdir(home):
            for item in os.listdir(home):
                link_path = os.path.join(home_target, item)
                real_path = os.path.join(home, item)
                if not os.path.exists(link_path):
                    try:
                        os.symlink(real_path, link_path)
                    except OSError:
                        pass
    else:
        home = _locate(home_target, spec["marker"], log)
    env = _env_for(tid, home)
    # sanity
    if not os.path.exists(os.path.join(home, spec["marker"])):
        log(f"  ! warning: marker missing after extract ({spec['marker']})")
    else:
        log(f"  ✓ installed at {home}")
    return {"tool": tid, "home": home, "env": env}


def install_many(ids, root, log, cancelled=lambda: False):
    results, errors = [], []
    for tid in ids:
        if cancelled():
            log("\n[cancelled]")
            break
        if tid not in TOOLS:
            errors.append((tid, "unknown tool"))
            continue
        try:
            results.append(install_one(tid, root, log, cancelled))
        except Exception as e:  # noqa: BLE001
            log(f"  ✗ {tid} failed: {e}")
            errors.append((tid, str(e)))
    if results:
        merge_env(root, results, log)
    log("\n" + ("done." if not errors else f"done with {len(errors)} error(s)."))
    return {"installed": [r["tool"] for r in results], "errors": errors}


# ---------------------------------------------------------------------------
# persisted env — written after install, applied at app startup
# ---------------------------------------------------------------------------

def env_path(root):
    return os.path.join(tools_dir(root), "env.json")


def merge_env(root, results, log):
    data = _load_env(root)
    for r in results:
        for k, v in r["env"]["vars"].items():
            data["vars"][k] = v
        for p in r["env"]["path"]:
            if p and p not in data["path"]:
                data["path"].insert(0, p)
    os.makedirs(tools_dir(root), exist_ok=True)
    with open(env_path(root), "w") as f:
        json.dump(data, f, indent=2)
    log(f"  wrote {env_path(root)}")


def _load_env(root):
    try:
        with open(env_path(root)) as f:
            d = json.load(f)
            d.setdefault("vars", {})
            d.setdefault("path", [])
            return d
    except Exception:
        return {"vars": {}, "path": []}


def apply_persisted_env(root):
    """Call at startup: set vars + prepend PATH so detection sees local tools.

    Self-heals:
      - relative paths in env.json (resolve against app root) so subprocesses
        with a different cwd still find .toolchains tools
      - stale LIBCLANG_PATH pointing at LLVM bin/ instead of lib/
      - prefers absolute paths under .toolchains/
    """
    root = os.path.abspath(root)

    def _abspath(p):
        if not p:
            return p
        if os.path.isabs(p):
            return os.path.normpath(p)
        # strip leading ./
        p2 = p[2:] if p.startswith("./") or p.startswith(".\\") else p
        return os.path.normpath(os.path.join(root, p2))

    d = _load_env(root)
    vars_ = {k: _abspath(v) for k, v in d.get("vars", {}).items()}
    paths = [_abspath(p) for p in d.get("path", [])]

    libclang = vars_.get("LIBCLANG_PATH")
    if libclang:
        names = ("libclang.dylib", "libclang.so", "libclang.dll",
                 "libclang.so.15", "libclang.so.16", "libclang.so.17")
        def _has(dpath):
            if not dpath or not os.path.isdir(dpath):
                return False
            if any(os.path.isfile(os.path.join(dpath, n)) for n in names):
                return True
            try:
                return any(n.startswith("libclang.so.")
                           for n in os.listdir(dpath)
                           if os.path.isfile(os.path.join(dpath, n)))
            except OSError:
                return False
        if not _has(libclang):
            # try sibling lib/ (or bin/ on Windows) next to a mistaken path
            parent = os.path.dirname(libclang.rstrip(os.sep))
            for cand in (os.path.join(parent, "lib"),
                         os.path.join(parent, "bin"),
                         parent):
                if _has(cand):
                    vars_["LIBCLANG_PATH"] = cand
                    break
        # Prefer toolchains LLVM lib/ when it exists, even if env.json points
        # somewhere else (keeps builds pinned to the portable install).
        tc_llvm = os.path.join(root, ".toolchains", "llvm")
        if os.path.isdir(tc_llvm):
            # one-level nesting from the tarball
            homes = [tc_llvm]
            try:
                homes += [
                    os.path.join(tc_llvm, n)
                    for n in os.listdir(tc_llvm)
                    if os.path.isdir(os.path.join(tc_llvm, n, "bin"))
                ]
            except OSError:
                pass
            for home in homes:
                for sub in ("lib", "bin"):
                    cand = os.path.join(home, sub)
                    if _has(cand):
                        vars_["LIBCLANG_PATH"] = cand
                        break
                else:
                    continue
                break

    # Self-heal ANDROID_NDK_HOME: the NDK root may be nested differently
    # depending on platform — macOS DMG extracts a .app bundle with the real
    # NDK at Contents/NDK/, while Windows/Linux zips nest under
    # android-ndk-r28c/. If the current path lacks toolchains/, search.
    ndk_home = vars_.get("ANDROID_NDK_HOME", "")
    if ndk_home and os.path.isdir(ndk_home):
        if not os.path.isdir(os.path.join(ndk_home, "toolchains")):
            healed_ndk = None
            # macOS: look inside .app bundles
            for child in sorted(os.listdir(ndk_home)):
                if child.endswith(".app"):
                    inner = os.path.join(ndk_home, child, "Contents", "NDK")
                    if os.path.isdir(os.path.join(inner, "toolchains")):
                        healed_ndk = inner
                        break
            # All platforms: fall back to _locate (handles nested zip dirs)
            if healed_ndk is None:
                healed_ndk = _locate(ndk_home, "source.properties",
                                     lambda m: None)
                if not os.path.isdir(os.path.join(healed_ndk, "toolchains")):
                    healed_ndk = None
            if healed_ndk:
                vars_["ANDROID_NDK_HOME"] = healed_ndk
                vars_["ANDROID_NDK_ROOT"] = healed_ndk
                # Create symlinks from the stale path → real NDK root
                # so existing build references still resolve.
                if healed_ndk != ndk_home and os.path.isdir(healed_ndk):
                    for item in os.listdir(healed_ndk):
                        link_path = os.path.join(ndk_home, item)
                        real_path = os.path.join(healed_ndk, item)
                        if not os.path.exists(link_path):
                            try:
                                os.symlink(real_path, link_path)
                            except OSError:
                                pass

    # Persist self-healed absolute paths so next launch is clean.
    healed = {"vars": vars_, "path": paths}
    if healed != {"vars": d.get("vars", {}), "path": d.get("path", [])}:
        try:
            os.makedirs(tools_dir(root), exist_ok=True)
            with open(env_path(root), "w") as f:
                json.dump(healed, f, indent=2)
                f.write("\n")
        except Exception:
            pass

    for k, v in vars_.items():
        if v:
            os.environ[k] = v
    if paths:
        sep = os.pathsep
        existing = os.environ.get("PATH", "")
        prepend = sep.join(p for p in paths if os.path.isdir(p))
        if prepend:
            os.environ["PATH"] = prepend + sep + existing
    return healed


if __name__ == "__main__":
    import sys
    r = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print(json.dumps(installable(), indent=2))
    if len(sys.argv) > 1:
        install_many(sys.argv[1:], r, print)
