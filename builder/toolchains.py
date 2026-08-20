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

Paths recorded in `.toolchains/env.json` that live under the project root are
stored **relative** (e.g. `.toolchains/flutter/flutter/bin`) so the same
DVForge folder works after a move or when installed at different absolute
paths on different machines. External system paths stay absolute.
"""

import json
import os
import platform
import shutil
import ssl
import stat
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
    # Linux/macOS: system package manager. Windows: Inno installer into
    # .toolchains/imagemagick (choco often records the package without
    # leaving magick.exe on PATH — we never treat that as success).
    "imagemagick": {
        "label": "ImageMagick (icon/logo branding)",
        "kind": "package",
        "marker": "magick",
        "packages": {
            "macOS":   ("brew", ["install", "imagemagick"]),
            "Linux":   ("sudo", ["apt", "install", "-y", "imagemagick"]),
            "Windows": ("choco", ["install", "-y", "imagemagick"]),
        },
        "urls": {
            ("Windows", "x86_64"): (
                "https://github.com/ImageMagick/ImageMagick/releases/"
                "download/7.1.2-29/ImageMagick-7.1.2-29-Q16-x64-dll.exe",
                "inno",
            ),
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


def _is_windows_admin():
    """True when this process already has an elevated token (Windows only)."""
    if not WIN:
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _detect_vs_msvc(log=None):
    """Return a short description if MSVC toolset + MSBuild are installed.

    Uses vswhere (ships with any VS / Build Tools install). Returns None when
    the C++ workload is missing so the bootstrapper can still run.
    """
    if not WIN:
        return None
    pf = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    vswhere = os.path.join(pf, "Microsoft Visual Studio", "Installer",
                           "vswhere.exe")
    if not os.path.isfile(vswhere):
        return None
    try:
        # Prefer an install that has the VC tools component.
        out = subprocess.check_output(
            [vswhere, "-latest", "-products", "*",
             "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
             "-property", "installationPath"],
            encoding="utf-8", errors="replace", timeout=30,
        ).strip()
        if not out:
            # Any MSBuild is better than nothing — still report present.
            out = subprocess.check_output(
                [vswhere, "-latest", "-products", "*",
                 "-requires", "Microsoft.Component.MSBuild",
                 "-property", "installationPath"],
                encoding="utf-8", errors="replace", timeout=30,
            ).strip()
        if not out:
            return None
        root = out.splitlines()[0].strip()
        # Confirm link.exe exists under that install.
        link = None
        tools = os.path.join(root, "VC", "Tools", "MSVC")
        if os.path.isdir(tools):
            for ver in sorted(os.listdir(tools), reverse=True):
                cand = os.path.join(tools, ver, "bin", "Hostx64", "x64",
                                    "link.exe")
                if os.path.isfile(cand):
                    link = cand
                    break
        if link:
            return f"{root} (link.exe)"
        # MSBuild-only install (rare) — still useful for MSI.
        msbuild = os.path.join(root, "MSBuild", "Current", "Bin", "MSBuild.exe")
        if os.path.isfile(msbuild):
            return f"{root} (MSBuild, no link.exe yet)"
        return root
    except Exception as e:
        if log:
            log(f"  · vswhere probe failed: {e}")
        return None


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
    root = os.path.abspath(root)
    home = os.path.join(tools_dir(root), tid)
    home_norm = os.path.normcase(os.path.normpath(home))
    freed = 0
    if os.path.isdir(home):
        freed = dir_size(home)
        shutil.rmtree(home, ignore_errors=True)
        log(f"removed {tid} ({human_size(freed)})")
    else:
        log(f"{tid} was not installed locally")

    def _points_at_home(p):
        if not p:
            return False
        resolved = _resolve_portable(p, root)
        rn = os.path.normcase(os.path.normpath(resolved))
        return rn == home_norm or rn.startswith(home_norm + os.sep) or home_norm in rn

    # rewrite env.json without paths/vars pointing at the removed home
    d = _load_env(root)
    d["path"] = [p for p in d.get("path", []) if not _points_at_home(p)]
    d["vars"] = {k: v for k, v in d.get("vars", {}).items() if not _points_at_home(v)}
    _save_env(root, d)
    return {"removed": tid, "freed": freed, "freed_human": human_size(freed)}


def installable(host_os=None, host_arch=None):
    """Return {id: {label, ok, reason}} for every tool, on this host."""
    host_os = host_os or _system()
    host_arch = host_arch or _arch()
    out = {}
    for tid, spec in TOOLS.items():
        ok, reason = True, ""
        # Android builds are only supported on Linux — don't offer the NDK for
        # install on macOS/Windows (the build_android path is kept for Linux).
        if tid == "android_ndk" and host_os != "Linux":
            ok, reason = False, "Android builds are only supported on Linux"
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
                # Windows ImageMagick has a direct Inno installer — choco is
                # optional, not required.
                has_direct = (tid == "imagemagick"
                              and (host_os, host_arch) in spec.get("urls", {}))
                if not shutil.which(mgr) and not has_direct:
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
    req = urllib.request.Request(url, headers={"User-Agent": "dvforge"})
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
        _extract_zip(archive, dest, log)
    elif kind == "dmg":
        _extract_dmg(archive, dest, log)
    else:  # tar.* (xz/gz auto-detected by mode 'r:*')
        with tarfile.open(archive, "r:*") as t:
            t.extractall(dest)


def _extract_zip(archive, dest, log):
    """Extract a zip, restoring Unix symlinks (ZipFile.extractall writes them
    as tiny text files — that flattens FlutterMacOS.framework and then
    `#import <FlutterMacOS/FlutterMacOS.h>` fails)."""
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            mode = (info.external_attr >> 16) & 0xFFFF
            target = os.path.join(dest, info.filename)
            if stat.S_ISLNK(mode):
                link = z.read(info).decode("utf-8", errors="replace").rstrip("\0")
                parent = os.path.dirname(target)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                if os.path.lexists(target):
                    if os.path.isdir(target) and not os.path.islink(target):
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        os.remove(target)
                try:
                    os.symlink(link, target)
                except OSError as e:
                    log(f"  ! symlink {info.filename} -> {link}: {e}")
                    z.extract(info, dest)
            else:
                z.extract(info, dest)


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


def find_flutter_home(root):
    """Return the Flutter SDK root under .toolchains, or None.

    Official zip extracts as `.toolchains/flutter/flutter/` (the inner
    folder is the SDK). A flat layout (marker directly under
    `.toolchains/flutter/`) is accepted too.
    """
    marker = os.path.join("bin", "flutter.bat" if WIN else "flutter")
    for cand in (
        os.path.join(root, ".toolchains", "flutter", "flutter"),
        os.path.join(root, ".toolchains", "flutter"),
    ):
        if os.path.isfile(os.path.join(cand, marker)):
            return cand
    return None


_FLUTTER_EXEC_NAMES = {
    "flutter", "dart", "dartaotruntime", "gen_snapshot", "gradlew",
    "idevicesyslog", "idevicescreenshot", "iproxy",
}
_FLUTTER_SKIP_EXT = {
    ".ttf", ".otf", ".woff", ".woff2", ".jar", ".zip", ".md", ".txt",
    ".stamp", ".json", ".xml", ".plist", ".png", ".jpg", ".ico",
    ".html", ".css", ".map", ".dill", ".kernel", ".inc",
}


def _flutter_should_be_executable(path):
    """True for Flutter/Dart launchers, shell scripts, and native binaries."""
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    if ext in _FLUTTER_SKIP_EXT:
        return False
    if name in _FLUTTER_EXEC_NAMES or ext in (".sh", ".py"):
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(4)
        if head.startswith(b"#!"):
            return True
        # ELF / Mach-O 64 / Mach-O 32 / fat Mach-O
        if head.startswith(b"\x7fELF") or head in (
                b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
                b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce",
                b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):
            return True
    except OSError:
        return False
    return False


def repair_flutter_permissions(home, log=None, deep=False):
    """Restore +x on Flutter/Dart binaries and helper scripts.

    Zip extract onto FAT/exFAT/NAS volumes (or a copy from Windows) often
    drops execute bits. `shutil.which` then skips the pinned SDK and the
    build falls through to an older Homebrew/system Flutter — which is
    exactly what makes `extended_text 14.0.0` fail (it needs Dart >= 3.5).

    `deep=True` also walks `bin/cache` (engine snapshots). Use that at
    install / build time, not on every UI rescan.
    """
    if WIN or not home or not os.path.isdir(home):
        return 0
    roots = [
        os.path.join(home, "bin"),
        os.path.join(home, "bin", "internal"),
        os.path.join(home, "bin", "cache", "dart-sdk", "bin"),
        # Xcode Run Script phases exec these directly (macos_assemble.sh,
        # xcode_backend.sh). Missing +x → PhaseScriptExecution exit 1.
        os.path.join(home, "packages", "flutter_tools", "bin"),
    ]
    if deep:
        roots.append(os.path.join(home, "bin", "cache"))
    seen = set()
    repaired = 0
    for root in roots:
        if not os.path.isdir(root):
            continue
        walker = os.walk(root)
        # bin/ itself: only the top-level launchers, not the whole cache
        # via the first root. `bin/internal` and dart-sdk/bin are walked.
        if os.path.basename(root) == "bin" and os.path.dirname(root) == home:
            for fn in ("flutter", "dart"):
                p = os.path.join(root, fn)
                if os.path.isfile(p) and not os.path.islink(p):
                    seen.add(p)
                    try:
                        mode = os.stat(p).st_mode
                        if not (mode & 0o111):
                            os.chmod(p, mode | 0o755)
                            repaired += 1
                    except OSError:
                        pass
            continue
        for dp, _dirs, files in walker:
            for fn in files:
                p = os.path.join(dp, fn)
                if p in seen or os.path.islink(p):
                    continue
                seen.add(p)
                if not _flutter_should_be_executable(p):
                    continue
                try:
                    mode = os.stat(p).st_mode
                    if mode & 0o111:
                        continue
                    os.chmod(p, mode | 0o755)
                    repaired += 1
                except OSError:
                    pass
    if repaired and log:
        log(f"  · restored execute bits on {repaired} Flutter/Dart binaries")
    if not WIN:
        repaired += repair_macos_framework_symlinks(home, log)
    return repaired


def _symlink_stub_target(path):
    """If `path` is a flattened symlink (tiny text file), return its target."""
    if os.path.islink(path) or os.path.isdir(path) or not os.path.isfile(path):
        return None
    try:
        sz = os.path.getsize(path)
    except OSError:
        return None
    if sz == 0 or sz > 200:
        return None
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if b"\0" in data:
        return None
    try:
        text = data.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    if not text or "\n" in text or " " in text:
        return None
    return text


def _restore_symlink(path, target, log=None):
    if os.path.islink(path):
        return 0
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            return 0
        if os.path.lexists(path):
            os.remove(path)
        os.symlink(target, path)
        return 1
    except OSError as e:
        if log:
            log(f"  ! could not restore symlink {path} -> {target}: {e}")
        return 0


def repair_macos_framework_symlinks(home, log=None):
    """Recreate .framework symlinks flattened by zip extract on NAS/exFAT.

    FlutterMacOS.framework/Headers becomes a 24-byte text file
    (`Versions/Current/Headers`) instead of a symlink. Clang then cannot
    resolve `#import <FlutterMacOS/FlutterMacOS.h>` and the macOS build
    produces an empty .app / 13 KB dmg.
    """
    engine = os.path.join(home, "bin", "cache", "artifacts", "engine")
    if not os.path.isdir(engine):
        return 0
    repaired = 0
    for dp, _dirs, _files in os.walk(engine):
        if not dp.endswith(".framework"):
            continue
        versions_a = os.path.join(dp, "Versions", "A")
        current = os.path.join(dp, "Versions", "Current")
        if os.path.isdir(versions_a):
            stub = _symlink_stub_target(current)
            if stub == "A" or (os.path.isfile(current) and not os.path.islink(current)
                               and os.path.getsize(current) <= 8):
                repaired += _restore_symlink(current, "A", log)
        for name in os.listdir(dp):
            if name == "Versions":
                continue
            p = os.path.join(dp, name)
            stub = _symlink_stub_target(p)
            if stub:
                repaired += _restore_symlink(p, stub, log)
    if repaired and log:
        log(f"  · restored {repaired} macOS framework symlinks")
    return repaired


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
        # Absolute path — cargo must not depend on PATH lookup, and a bare
        # "sccache" token used to be rewritten to <root>/sccache.
        cargo_bin = os.path.join(os.path.expanduser("~"), ".cargo", "bin")
        exe = (shutil.which("sccache")
               or os.path.join(cargo_bin, "sccache" + EXE))
        return {"vars": {"RUSTC_WRAPPER": exe}, "path": []}
    if tid == "imagemagick":
        # magick.exe sits at the Inno /DIR root, or in bin/ for some layouts.
        path = []
        if os.path.isfile(os.path.join(home, "magick" + EXE)):
            path = [home]
        elif os.path.isdir(os.path.join(home, "bin")):
            path = [os.path.join(home, "bin")]
        elif home:
            path = [home]
        return {"vars": {}, "path": path}
    return {"vars": {}, "path": []}


def _install_imagemagick_inno(home_target, url, log, cancelled=lambda: False):
    """Silent-install official ImageMagick into .toolchains/imagemagick.

    Chocolatey's imagemagick package is an Inno Setup wrapper that often
    records 'installed' without leaving magick.exe (UAC skipped, PATH never
    refreshed, or the installer cleaned itself up). Dropping the official
    exe into a project-local folder lets env.json wire PATH reliably.
    Returns the magick.exe path, or None.
    """
    magick = os.path.join(home_target, "magick.exe")
    if os.path.isfile(magick):
        return magick
    os.makedirs(home_target, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        exe = os.path.join(tmp, "imagemagick-setup.exe")
        _download(url, exe, log)
        if cancelled():
            raise RuntimeError("cancelled")
        # Inno Setup: /DIR= must be last. User-writable /DIR usually works
        # without admin; fall back to one UAC prompt if it didn't land.
        silent = [
            "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES", "/NOICONS",
            "/MERGETASKS=!desktop_icon,!install_FFmpeg",
            f"/DIR={home_target}",
        ]
        log("  installing ImageMagick into .toolchains (PATH wired via env.json)")
        rc = subprocess.call([exe] + silent)
        if os.path.isfile(magick):
            log(f"  ✓ ImageMagick at {magick}")
            return magick
        log("  installer needs admin — a UAC prompt will appear")
        bat = os.path.join(tmp, "install_imagemagick.bat")
        with open(bat, "w", encoding="ascii") as f:
            f.write("@echo off\r\n")
            f.write(f'"{exe}" {" ".join(silent)}\r\n')
        ps = (f"$p = Start-Process -FilePath '{bat}' -Verb RunAs -Wait -PassThru; "
              f"exit $p.ExitCode")
        rc = subprocess.call(["powershell", "-NoProfile", "-ExecutionPolicy",
                              "Bypass", "-Command", ps])
        if os.path.isfile(magick):
            log(f"  ✓ ImageMagick at {magick}")
            return magick
        log(f"  ! ImageMagick installer finished (exit {rc}) but "
            f"magick.exe not under {home_target}")
        return None


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
        # ImageMagick: never trust a bare `convert` (Windows system32) or a
        # Chocolatey package that left no magick.exe. Probe real binaries.
        just_installed = False
        if tid == "imagemagick":
            from . import prereqs as _prereqs
            already = _prereqs.find_imagemagick()
            if not already and WIN:
                url_entry = spec.get("urls", {}).get((host_os, _arch()))
                if url_entry:
                    installed = _install_imagemagick_inno(
                        home_target, url_entry[0], log, cancelled)
                    if installed:
                        already = installed
                        just_installed = True
        if already and not just_installed:
            log(f"  ✓ {tid} already installed ({already})")
        elif not already:
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
        if tid == "imagemagick":
            from . import prereqs as _prereqs
            found = _prereqs.find_imagemagick()
        if found:
            log(f"  ✓ {tid} installed via {mgr}" if not already
                else f"  ✓ {tid} ready")
        else:
            log(f"  ! {tid} install finished but binary not found on PATH — "
                f"re-open the app or start a new terminal")
        # Wire Program Files\dotnet / ImageMagick dir into session PATH
        env_path = []
        if tid == "dotnet" and WIN:
            pf_dotnet = os.path.join(
                os.environ.get("ProgramFiles", r"C:\Program Files"), "dotnet")
            if os.path.isdir(pf_dotnet):
                env_path = [pf_dotnet]
        if tid == "imagemagick" and found:
            env_path = [os.path.dirname(os.path.abspath(found))]
            os.environ["PATH"] = env_path[0] + os.pathsep + os.environ.get("PATH", "")
        return {"tool": tid, "home": os.path.dirname(found) if (tid == "imagemagick" and found) else "",
                "env": {"vars": {}, "path": env_path}}

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
        # Install / ensure Rust 1.75 per-user (~/.cargo, ~/.rustup).
        # On Windows the official RustDesk CI uses the MSVC host triple
        # (x86_64-pc-windows-msvc). Machines that already have a GNU default
        # (or Chocolatey rustc on PATH) must still get the MSVC toolchain.
        cargo_bin = os.path.join(os.path.expanduser("~"), ".cargo", "bin")
        rustup = shutil.which("rustup") or os.path.join(
            cargo_bin, "rustup" + (".exe" if WIN else ""))
        pin = PINNED["rust"]
        win_msvc = f"{pin}-x86_64-pc-windows-msvc"

        def _run_rustup(args, check=True):
            env = os.environ.copy()
            # Ignore Chocolatey / other system Rust on PATH during install.
            env["RUSTUP_INIT_SKIP_PATH_CHECK"] = "yes"
            path_parts = [cargo_bin] + env.get("PATH", "").split(os.pathsep)
            env["PATH"] = os.pathsep.join(p for p in path_parts if p)
            exe = rustup if os.path.isfile(rustup) else shutil.which("rustup")
            if not exe:
                return 1
            return subprocess.call([exe] + args, env=env)

        if os.path.isfile(rustup) or shutil.which("rustup"):
            log(f"  rustup already present — ensuring {pin}"
                + (" MSVC" if WIN else ""))
            if WIN:
                # Prefer MSVC as the default host for future installs.
                subprocess.call(
                    [rustup if os.path.isfile(rustup) else "rustup",
                     "set", "default-host", "x86_64-pc-windows-msvc"],
                    env={**os.environ, "RUSTUP_INIT_SKIP_PATH_CHECK": "yes"},
                )
                _run_rustup(["toolchain", "install", win_msvc], check=False)
                _run_rustup(["target", "add", "x86_64-pc-windows-msvc",
                             "--toolchain", win_msvc], check=False)
                _run_rustup(["default", win_msvc], check=False)
                _run_rustup(["component", "add", "rustfmt",
                             "--toolchain", win_msvc], check=False)
            else:
                _run_rustup(["toolchain", "install", pin, "--profile", "minimal"],
                            check=False)
                _run_rustup(["default", pin], check=False)
            log(f"  ✓ Rust ready; cargo bin: {cargo_bin}")
            return {"tool": tid, "home": "", "env": {"vars": {}, "path": [cargo_bin]}}

        url, _label = spec["urls"][(host_os, host_arch)]
        with tempfile.TemporaryDirectory() as tmp:
            init = os.path.join(tmp, "rustup-init" + (".exe" if WIN else ""))
            _download(url, init, log)
            if not WIN:
                os.chmod(init, 0o755)
            log("  installing Rust 1.75 (rustup-init -y, minimal profile)")
            # Force MSVC host on Windows so we never land on gnu-by-default
            # (common when MinGW/Chocolatey rust is already on PATH).
            env = os.environ.copy()
            env["RUSTUP_INIT_SKIP_PATH_CHECK"] = "yes"
            cmd = [init, "-y", "--default-toolchain",
                   win_msvc if WIN else pin, "--profile", "minimal"]
            if WIN:
                cmd += ["--default-host", "x86_64-pc-windows-msvc"]
            rc = subprocess.call(cmd, env=env)
            if rc != 0:
                raise RuntimeError("rustup-init failed")
        rustup = os.path.join(cargo_bin, "rustup" + (".exe" if WIN else ""))
        if WIN and os.path.isfile(rustup):
            subprocess.call([rustup, "default", win_msvc])
        log(f"  ✓ Rust installed; cargo bin: {cargo_bin}")
        return {"tool": tid, "home": "", "env": {"vars": {}, "path": [cargo_bin]}}

    if spec["kind"] == "vs":
        # Build Tools for Visual Studio (C++). Provides MSVC link.exe + MSBuild.
        # Skip when already present (re-running the quiet bootstrapper while
        # elevated often returns exit 1 even though the toolset is fine).
        already = _detect_vs_msvc(log)
        if already:
            log(f"  ✓ VS C++ toolset already present ({already})")
            return {"tool": tid, "home": "", "env": {"vars": {}, "path": []}}

        url, _ = spec["urls"][(host_os, host_arch)]
        with tempfile.TemporaryDirectory() as tmp:
            boot = os.path.join(tmp, "vs_BuildTools.exe")
            log_dir = os.path.join(tmp, "vs-logs")
            os.makedirs(log_dir, exist_ok=True)
            _download(url, boot, log)
            if cancelled():
                raise RuntimeError("cancelled")
            log("  launching the Visual Studio Build Tools installer.")
            log("  → This installs the C++ MSVC toolset (link.exe) + Windows SDK")
            log("    + MSBuild — a few GB, several minutes.")
            # Channel product id for Build Tools 2022; quiet + wait.
            arg_list = [
                "--quiet", "--wait", "--norestart", "--nocache",
                "--installPath",
                os.path.join(os.environ.get("ProgramFiles(x86)",
                                            r"C:\Program Files (x86)"),
                             "Microsoft Visual Studio", "2022", "BuildTools"),
                "--add", "Microsoft.VisualStudio.Workload.VCTools",
                "--includeRecommended",
                "--add", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                "--add", "Microsoft.VisualStudio.Component.Windows11SDK.22621",
            ]
            # If we are already elevated (setup script / admin shell), run the
            # bootstrapper directly. Nested Start-Process -Verb RunAs often
            # fails with exit 1 when UAC is already elevated.
            elevated = _is_windows_admin()
            if elevated:
                log("  · already elevated — running bootstrapper without nested UAC")
                rc = subprocess.call([boot] + arg_list)
            else:
                log("  · requesting one UAC elevation for the installer")
                # Quote-safe PowerShell invoke
                boot_ps = boot.replace("'", "''")
                args_ps = " ".join("'{0}'".format(a.replace("'", "''"))
                                   for a in arg_list)
                ps = (
                    f"$p = Start-Process -FilePath '{boot_ps}' "
                    f"-ArgumentList @({args_ps}) -Verb RunAs -Wait -PassThru; "
                    f"exit $p.ExitCode"
                )
                rc = subprocess.call(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-Command", ps])
            # 0 = ok, 3010 = success reboot recommended
            if rc not in (0, 3010):
                # Installer is flaky when a partial install exists — re-probe.
                again = _detect_vs_msvc(log)
                if again:
                    log(f"  · installer exit {rc}, but MSVC toolset is usable "
                        f"({again}) — treating as success")
                else:
                    raise RuntimeError(
                        f"VS Build Tools installer exited with code {rc}. "
                        "If UAC was declined, re-run elevated. Otherwise install "
                        "'Desktop development with C++' from "
                        "https://aka.ms/vs/17/release/vs_BuildTools.exe")
            else:
                log("  ✓ VS Build Tools install finished.")
        # Final probe
        final = _detect_vs_msvc(log)
        if final:
            log(f"  ✓ MSVC ready ({final})")
        else:
            log("  ! installer finished but vswhere cannot see VC.Tools yet — "
                "reboot or open a new shell, then re-scan.")
        return {"tool": tid, "home": "", "env": {"vars": {}, "path": []}}

    if spec["kind"] == "git":
        if os.path.isdir(home_target):
            log("  already present, checking out pinned commit")
        else:
            log(f"  git clone {spec['repo']}")
            rc = subprocess.call(["git", "clone", spec["repo"], home_target])
            if rc != 0:
                raise RuntimeError("git clone failed")
        # Force checkout of the pinned commit so vcpkg matches the build's
        # expected manifest baseline (prereqs.VCPKG_COMMIT / PINNED["vcpkg"]).
        pinned = PINNED.get("vcpkg", "")
        if pinned:
            log(f"  git checkout {pinned[:8]}")
            subprocess.call(["git", "-C", home_target, "fetch", "--all"])
            subprocess.call(["git", "-C", home_target, "checkout", pinned])
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
    if tid == "flutter":
        repair_flutter_permissions(home, log, deep=True)
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
#
# Portability rules (so DVForge can live at different absolute paths on every
# machine, while toolchain wiring stays identical):
#
#   * Paths *under the project root* are stored RELATIVE in env.json
#     (forward slashes), e.g. ".toolchains/flutter/flutter/bin".
#   * Paths outside the project (system VS, /opt/vcpkg, ~/.cargo, …) stay
#     absolute — they are machine-local by nature.
#   * On load we resolve relatives against the current root and rewrite any
#     stale absolute ".toolchains/…" paths left by an older install/move.
#   * After healing we re-save env.json in portable form so the next machine
#     (or a copy of the folder) just works.

def env_path(root):
    return os.path.join(tools_dir(root), "env.json")


def _norm_seps(p):
    """Normalize mixed / and \\ separators to os.sep for comparisons."""
    if not p:
        return p
    return p.replace("/", os.sep).replace("\\", os.sep)


def _under_root(path, root):
    """True if path is the root or a descendant (case-insensitive on Windows)."""
    if not path:
        return False
    try:
        ap = os.path.normcase(os.path.abspath(_norm_seps(path)))
        ar = os.path.normcase(os.path.abspath(root))
        return ap == ar or ap.startswith(ar + os.sep)
    except (OSError, ValueError):
        return False


def _to_portable(path, root):
    """Store path relative to project root when possible (portable env.json).

    Uses forward slashes so the same env.json works if the tree is later
    opened on Linux/WSL. External (non-project) paths stay absolute.
    """
    if not path:
        return path
    # Non-path values (e.g. RUSTC_WRAPPER=sccache bare name) pass through.
    if path in ("sccache",) or (not os.path.isabs(path)
                                  and ".toolchains" not in _norm_seps(path)
                                  and not path.startswith(".")):
        # Keep simple tokens; still try relative for known relative forms.
        if os.sep not in path and "/" not in path and "\\" not in path:
            return path
    path = _norm_seps(path)
    root = os.path.abspath(root)
    if os.path.isabs(path) and _under_root(path, root):
        rel = os.path.relpath(os.path.abspath(path), root)
        return rel.replace("\\", "/")
    if not os.path.isabs(path):
        # Already relative — normalize separators only.
        p2 = path[2:] if path.startswith("./") or path.startswith(".\\") else path
        return p2.replace("\\", "/")
    return os.path.normpath(path)


def _extract_toolchains_suffix(path):
    """If path contains a .toolchains segment, return the suffix after it.

    Handles both separators and mixed-case drive letters on Windows.
    """
    if not path:
        return None
    norm = _norm_seps(path)
    # Find ".../.toolchains/..." or leading ".toolchains/..."
    marker = os.sep + ".toolchains" + os.sep
    lower = norm.lower() if WIN else norm
    m = marker.lower() if WIN else marker
    idx = lower.find(m)
    if idx >= 0:
        return norm[idx + len(marker):]
    # path is exactly ".../.toolchains" or ".toolchains"
    if lower.endswith(os.sep + ".toolchains") or lower == ".toolchains":
        return ""
    if lower.startswith(".toolchains" + os.sep):
        return norm[len(".toolchains" + os.sep):]
    return None


def _resolve_portable(path, root):
    """Turn a stored env.json path into an absolute path for this machine."""
    if not path:
        return path
    root = os.path.abspath(root)
    # Bare tool names (RUSTC_WRAPPER=sccache) — leave alone.
    if os.sep not in path and "/" not in path and "\\" not in path:
        if not path.startswith("."):
            return path

    path_norm = _norm_seps(path)

    # Relative → join root.
    if not os.path.isabs(path_norm):
        p2 = path_norm[2:] if path_norm.startswith("." + os.sep) else path_norm
        return os.path.normpath(os.path.join(root, p2))

    abs_p = os.path.normpath(path_norm)

    # Already under current root — good.
    if _under_root(abs_p, root):
        return abs_p

    # Stale absolute from another machine / moved folder: rewrite any path
    # that mentions .toolchains into <current-root>/.toolchains/<suffix>.
    suffix = _extract_toolchains_suffix(abs_p)
    if suffix is not None:
        return os.path.normpath(os.path.join(root, ".toolchains", suffix))

    # External absolute path (VS, /opt/vcpkg, user SDK, …) — keep as-is.
    return abs_p


def _save_env(root, data):
    """Write env.json with project-local paths stored relative to root."""
    root = os.path.abspath(root)
    portable = {
        "vars": {k: _to_portable(v, root) for k, v in data.get("vars", {}).items()},
        "path": [_to_portable(p, root) for p in data.get("path", []) if p],
    }
    # De-dupe PATH entries (after portable conversion)
    seen = set()
    uniq = []
    for p in portable["path"]:
        key = p.replace("\\", "/").lower() if WIN else p.replace("\\", "/")
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    portable["path"] = uniq
    os.makedirs(tools_dir(root), exist_ok=True)
    with open(env_path(root), "w", encoding="utf-8") as f:
        json.dump(portable, f, indent=2)
        f.write("\n")
    return portable


def merge_env(root, results, log):
    """Merge install results into env.json using portable (relative) paths."""
    root = os.path.abspath(root)
    data = _load_env(root)
    # Resolve existing entries first so we can compare apples-to-apples,
    # then re-serialize everything as portable at the end.
    resolved_vars = {k: _resolve_portable(v, root)
                     for k, v in data.get("vars", {}).items()}
    resolved_path = [_resolve_portable(p, root) for p in data.get("path", [])]

    for r in results:
        for k, v in r["env"]["vars"].items():
            resolved_vars[k] = v  # install always produces absolute homes
        for p in r["env"]["path"]:
            if not p:
                continue
            ap = os.path.normpath(p)
            # Avoid duplicate absolute / already-resolved entries.
            if ap not in resolved_path and os.path.normcase(ap) not in {
                    os.path.normcase(x) for x in resolved_path}:
                resolved_path.insert(0, ap)

    portable = _save_env(root, {"vars": resolved_vars, "path": resolved_path})
    log(f"  wrote {env_path(root)} (portable paths relative to project root)")
    return portable


def _load_env(root):
    try:
        with open(env_path(root), encoding="utf-8") as f:
            d = json.load(f)
            d.setdefault("vars", {})
            d.setdefault("path", [])
            return d
    except Exception:
        return {"vars": {}, "path": []}


def apply_persisted_env(root):
    """Call at startup: set vars + prepend PATH so detection sees local tools.

    Self-heals:
      - relative paths in env.json (resolve against app root)
      - stale absolute paths from a moved/copied install (rewrite .toolchains/)
      - LIBCLANG_PATH pointing at the wrong sibling dir
      - prefers portable LLVM under .toolchains/ when present
      - re-saves env.json in portable form so the tree stays movable
    """
    root = os.path.abspath(root)

    d = _load_env(root)
    vars_ = {k: _resolve_portable(v, root) for k, v in d.get("vars", {}).items()}
    paths = [_resolve_portable(p, root) for p in d.get("path", [])]

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
                if not healed_ndk or not os.path.isdir(
                        os.path.join(healed_ndk, "toolchains")):
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

    # Pinned Flutter must beat Homebrew/Chocolatey on PATH. A later brew
    # install of imagemagick/potrace prepends /opt/homebrew/bin, which
    # otherwise shadows .toolchains/flutter and picks an older Dart —
    # then `flutter pub get` dies on extended_text 14.0.0 (Dart >= 3.5).
    flutter_home = find_flutter_home(root)
    if flutter_home:
        repair_flutter_permissions(flutter_home)
        fbin = os.path.join(flutter_home, "bin")
        rest = []
        for p in paths:
            try:
                if os.path.normcase(os.path.abspath(p)) == os.path.normcase(
                        os.path.abspath(fbin)):
                    continue
            except (OSError, ValueError):
                pass
            rest.append(p)
        paths = [fbin] + rest

    # ImageMagick: choco/NSIS often land off PATH (or choco marks the
    # package installed without leaving magick.exe). Discover a real
    # magick and persist its directory so customize.py can find it.
    try:
        from . import prereqs as _prereqs
        magick = _prereqs.find_imagemagick()
        if magick:
            mdir = os.path.dirname(os.path.abspath(magick))
            already = any(
                os.path.normcase(os.path.abspath(p)) == os.path.normcase(mdir)
                for p in paths if p)
            if mdir and os.path.isdir(mdir) and not already:
                paths.insert(0, mdir)
    except Exception:
        pass

    # Always re-save in portable form so a moved/copied tree keeps working
    # on the next machine without anyone hand-editing env.json.
    try:
        _save_env(root, {"vars": vars_, "path": paths})
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
    # Return resolved (absolute) view for callers that need real paths.
    return {"vars": vars_, "path": paths}


if __name__ == "__main__":
    import sys
    r = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print(json.dumps(installable(), indent=2))
    if len(sys.argv) > 1:
        install_many(sys.argv[1:], r, print)
