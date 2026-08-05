"""
prereqs.py — detect the build toolchains on this machine and, when something
is missing, say exactly how to get it for the current OS.

The versions mirror rustdesk-builder-v2's workflows:
    Rust 1.75 · Flutter 3.24.5 · LLVM 15 · NDK r28c · vcpkg (pinned commit)

We only *detect and advise* here — nothing is installed automatically, because
these are large system-wide toolchains a person should install deliberately.
"""

import os
import platform
import shutil
import subprocess


PINNED = {
    "rust": "1.75",
    "flutter": "3.24.5",
    "llvm": "15.0.6",
    "ndk": "r28c",
}


def _system():
    s = platform.system()
    return {"Darwin": "macOS"}.get(s, s)


def _which(name):
    return shutil.which(name)


def _run_version(cmd):
    """Run a --version-style command, return first line of output or None."""
    try:
        out = subprocess.check_output(
            cmd, stderr=subprocess.STDOUT, timeout=20,
            encoding="utf-8", errors="replace",
        )
        return out.strip().splitlines()[0] if out.strip() else ""
    except Exception:
        return None


# ---------------------------------------------------------------------------
# individual checks — each returns a status dict
# ---------------------------------------------------------------------------

def _status(present, version="", path="", note="", hint=""):
    return {"present": bool(present), "version": version or "",
            "path": path or "", "note": note or "", "hint": hint or ""}


def check_git():
    p = _which("git")
    return _status(p, _run_version(["git", "--version"]) or "", p or "",
                   hint=_install_hint("git"))


def check_python():
    # we're literally running in python, so it's present; report the interpreter
    p = shutil.which("python3") or shutil.which("python") or ""
    return _status(True, "Python " + platform.python_version(), p)


def check_rust():
    rc = _which("rustc")
    cg = _which("cargo")
    if not (rc and cg):
        return _status(False, hint=_install_hint("rust"))
    ver = _run_version(["rustc", "--version"]) or ""
    note = ""
    if PINNED["rust"] not in ver:
        note = f"Workflows pin {PINNED['rust']}; rustup can add it: rustup toolchain install {PINNED['rust']}"
    return _status(True, ver, rc, note=note)


def check_rust_target():
    """On Windows, verify the default Rust target is x86_64-pc-windows-msvc.
    The builder always enforces MSVC (matching the official CI); the GNU
    target requires gcc.exe (MinGW) which most setups don't have."""
    if _system() != "Windows":
        return _status(True, note="N/A on non-Windows hosts.")
    try:
        out = subprocess.check_output(
            ["rustup", "show"], stderr=subprocess.STDOUT, timeout=20,
            encoding="utf-8", errors="replace",
        )
    except Exception:
        return _status(False, hint="Install rustup: https://rustup.rs")
    show = out or ""
    has_msvc = "windows-msvc" in show
    has_pinned = PINNED["rust"] in show
    if has_msvc and has_pinned:
        return _status(True, f"{PINNED['rust']} x86_64-pc-windows-msvc", "",
                       note="Pinned Rust + MSVC target — matches official CI.")
    if has_msvc and not has_pinned:
        return _status(
            True, "x86_64-pc-windows-msvc", "",
            note=f"MSVC target present but Rust {PINNED['rust']} not installed.",
            hint=f"The builder will install it automatically, or run:\n"
                 f"  rustup toolchain install {PINNED['rust']}-x86_64-pc-windows-msvc\n"
                 f"  rustup default {PINNED['rust']}-x86_64-pc-windows-msvc",
        )
    return _status(
        False, "", "",
        note="Default Rust target is not windows-msvc.",
        hint="Switch to MSVC (the builder will do this automatically):\n"
             f"  rustup toolchain install {PINNED['rust']}-x86_64-pc-windows-msvc\n"
             f"  rustup default {PINNED['rust']}-x86_64-pc-windows-msvc\n"
             "  (requires Visual Studio Build Tools with C++ workload)",
    )


def check_flutter():
    p = _which("flutter")
    if not p:
        return _status(False, hint=_install_hint("flutter"))
    ver = _run_version(["flutter", "--version"]) or ""
    note = ""
    if PINNED["flutter"] not in ver:
        note = f"Workflows use Flutter {PINNED['flutter']}."
    return _status(True, ver, p, note=note)


def check_clang():
    for name in ("clang", "cc", "gcc"):
        p = _which(name)
        if p:
            return _status(True, _run_version([name, "--version"]) or name, p)
    return _status(False, hint=_install_hint("clang"))

def _dir_has_libclang(d):
    """True if directory d contains a libclang shared library."""
    if not d or not os.path.isdir(d):
        return False
    names = ("libclang.dll", "libclang.dylib", "libclang.so")
    if any(os.path.isfile(os.path.join(d, n)) for n in names):
        return True
    # versioned sonames, e.g. libclang.so.15 / libclang.so.15.0.6
    try:
        return any(n.startswith("libclang.so.")
                   for n in os.listdir(d)
                   if os.path.isfile(os.path.join(d, n)))
    except OSError:
        return False


def _find_toolchains_libclang():
    """Look under .toolchains/llvm for a libclang library; return its dir."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    base = os.path.join(root, ".toolchains", "llvm")
    if not os.path.isdir(base):
        return None
    # common spots first (bin/ from the pip path, lib/ from the tarballs),
    # then a shallow walk as a fallback.
    for sub in ("bin", "lib"):
        d = os.path.join(base, sub)
        if _dir_has_libclang(d):
            return d
    for dp, _dirs, _files in os.walk(base):
        if _dir_has_libclang(dp):
            return dp
    return None

def check_llvm():
    # RustDesk's bindgen/ffigen only needs *libclang*, not a full clang
    # toolchain. On Windows we install just libclang.dll (via pip) into
    # .toolchains/llvm, so there is deliberately no clang.exe to find — detect
    # the library itself, not only a clang binary.
    #
    # 1) explicit LIBCLANG_PATH wins (set by the auto-installer's env.json).
    lc = os.environ.get("LIBCLANG_PATH", "")
    if _dir_has_libclang(lc):
        return _status(True, f"libclang ({PINNED['llvm']})", lc)
    # 2) a libclang under our managed .toolchains/llvm tree.
    tc = _find_toolchains_libclang()
    if tc:
        return _status(True, f"libclang ({PINNED['llvm']})", tc)
    # 3) fall back to a real clang / llvm-config on PATH (Linux/macOS system
    #    installs, or a full LLVM tarball whose bin/ is on PATH).# RustDesk's bindgen wants libclang; look for llvm-config or clang
    p = _which("llvm-config") or _which("clang")
    if not p:
        return _status(False, hint=_install_hint("llvm"))
    ver = _run_version([os.path.basename(p), "--version"]) or ""
    note = ""
    if PINNED["llvm"] not in ver:
        note = (f"Workflows pin LLVM {PINNED['llvm']}; newer versions can "
                f"cause bindgen/libclang issues. Use the auto-installer "
                f"or install LLVM {PINNED['llvm']} and set LIBCLANG_PATH.")
    return _status(True, ver, p, note=note)


# Must match the baseline in vcpkg.json and the official CI's VCPKG_COMMIT_ID.
VCPKG_COMMIT = "120deac3062162151622ca4860575a33844ba10b"


def check_vcpkg():
    root = os.environ.get("VCPKG_ROOT")
    exe = None
    if root:
        cand = os.path.join(root, "vcpkg.exe" if _system() == "Windows" else "vcpkg")
        if os.path.exists(cand):
            exe = cand
    exe = exe or _which("vcpkg")
    if not exe:
        return _status(False, hint=_install_hint("vcpkg"))

    # vcpkg itself is a git repo; the build requires a specific commit.
    vcpkg_root = root or (os.path.dirname(exe) if exe else None)
    note = ""
    if vcpkg_root and os.path.isdir(os.path.join(vcpkg_root, ".git")):
        try:
            cur = subprocess.check_output(
                ["git", "-C", vcpkg_root, "rev-parse", "HEAD"],
                text=True, stderr=subprocess.PIPE, timeout=10
            ).strip()
            if cur != VCPKG_COMMIT:
                note = (f"vcpkg is on {cur[:8]}, but {VCPKG_COMMIT[:8]} is "
                        f"required. The build will switch the checkout.")
        except Exception:
            pass
    return _status(True, _run_version([exe, "version"]) or "vcpkg", exe,
                   note=note or "vcpkg will be checked out to the pinned commit at build time.")


def check_msbuild():
    if _system() != "Windows":
        return _status(False, note="Windows only.")
    p = _which("msbuild") or _which("MSBuild")
    if p:
        return _status(True, _run_version([p, "-version"]) or "MSBuild", p)
    # Use vswhere (ships with any VS/Build Tools install) to find MSBuild + the
    # MSVC toolset. This is what tells us link.exe is available for Rust.
    pf = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    vswhere = os.path.join(pf, "Microsoft Visual Studio", "Installer", "vswhere.exe")
    if os.path.exists(vswhere):
        try:
            out = subprocess.check_output(
                [vswhere, "-latest", "-products", "*",
                 "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                 "-property", "installationPath"],
                encoding="utf-8", errors="replace", timeout=20).strip()
            if out:
                # find MSBuild.exe under the install for a version string
                msb = ""
                for root, _dirs, files in os.walk(os.path.join(out, "MSBuild")):
                    if "MSBuild.exe" in files:
                        msb = os.path.join(root, "MSBuild.exe"); break
                return _status(True, "VC++ Build Tools + MSBuild", msb or out,
                               note="MSVC linker (link.exe) available for Rust.")
        except Exception:
            pass
    # fall back to a directory heuristic
    guess = os.path.join(pf, "Microsoft Visual Studio")
    present = os.path.isdir(guess)
    return _status(present, "Visual Studio detected" if present else "",
                   guess if present else "", hint=_install_hint("msbuild"))


def check_java():
    p = _which("java")
    if not p:
        return _status(False, hint=_install_hint("java"))
    return _status(True, _run_version(["java", "-version"]) or "java", p)


def check_android_ndk():
    # NDK is found via env or inside the Android SDK
    for var in ("ANDROID_NDK_HOME", "ANDROID_NDK_ROOT", "NDK_HOME"):
        v = os.environ.get(var)
        if v and os.path.isdir(v):
            return _status(True, os.path.basename(v.rstrip("/\\")), v)
    sdk = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
    if sdk:
        ndk_dir = os.path.join(sdk, "ndk")
        if os.path.isdir(ndk_dir):
            versions = sorted(os.listdir(ndk_dir))
            if versions:
                return _status(True, versions[-1], os.path.join(ndk_dir, versions[-1]))
    return _status(False, hint=_install_hint("android_ndk"))


def check_xcode():
    if _system() != "macOS":
        return _status(False, note="macOS only.")
    p = _which("xcodebuild")
    if p:
        return _status(True, _run_version(["xcodebuild", "-version"]) or "Xcode", p)
    return _status(False, hint=_install_hint("xcode"))


def check_rpmbuild():
    p = _which("rpmbuild")
    if p:
        return _status(True, _run_version(["rpmbuild", "--version"]) or "rpmbuild", p)
    return _status(False, hint=_install_hint("rpmbuild"))


def check_appimage_builder():
    p = _which("appimage-builder")
    if p:
        return _status(True, "appimage-builder", p)
    return _status(False, hint=_install_hint("appimage_builder"))


def check_sccache():
    p = _which("sccache")
    if not p:
        return _status(False, hint=_install_hint("sccache"))
    ver = _run_version(["sccache", "--version"]) or "sccache"
    return _status(True, ver, p, note="Optional: speeds up Rust/C++ rebuilds significantly.")


def nuget_has_org_source(nuget_exe=None):
    """True if nuget.exe has nuget.org (or api.nuget.org) as a package source."""
    exe = nuget_exe or _which("nuget")
    if not exe:
        return False
    try:
        out = subprocess.check_output(
            [exe, "sources", "list"],
            stderr=subprocess.STDOUT, timeout=30,
            encoding="utf-8", errors="replace",
        )
    except Exception:
        return False
    low = out.lower()
    return "nuget.org" in low or "api.nuget.org" in low


def ensure_nuget_org(nuget_exe=None, log=None):
    """Register nuget.org if missing. Safe to call repeatedly.

    Chocolatey's nuget.exe often ships with an empty source list, which makes
    every restore fail with 'Unable to find version …'.
    """
    log = log or (lambda _m: None)
    exe = nuget_exe or _which("nuget")
    if not exe:
        log("  ! nuget not found — cannot configure package sources")
        return False
    try:
        out = subprocess.check_output(
            [exe, "sources", "list"],
            stderr=subprocess.STDOUT, timeout=30,
            encoding="utf-8", errors="replace",
        )
    except Exception as e:
        log(f"  · could not list nuget sources: {e}")
        return False
    low = out.lower()
    if "nuget.org" in low or "api.nuget.org" in low:
        if "nuget.org" in low and "[disabled]" in low:
            log("  · enabling nuget.org package source")
            try:
                subprocess.check_call(
                    [exe, "sources", "Enable", "-Name", "nuget.org"],
                    timeout=30)
            except Exception as e:
                log(f"  · enable nuget.org failed: {e}")
                return False
        return True
    log("  · adding nuget.org package source")
    try:
        subprocess.check_call(
            [exe, "sources", "Add",
             "-Name", "nuget.org",
             "-Source", "https://api.nuget.org/v3/index.json"],
            timeout=30)
        return True
    except Exception as e:
        log(f"  · add nuget.org failed: {e}")
        return False


def check_nuget():
    """NuGet CLI — required to restore WiX packages for Windows MSI builds."""
    if _system() != "Windows":
        return _status(False, note="Windows only (MSI packaging).")
    p = _which("nuget")
    if not p:
        return _status(False, hint=_install_hint("nuget"))
    ver = _run_version(["nuget", "help"]) or "nuget"
    if not nuget_has_org_source(p):
        # Auto-fix empty source lists so a re-scan flips green without a
        # manual `nuget sources Add` (common after Chocolatey nuget install).
        if ensure_nuget_org(p):
            return _status(True, ver, p,
                           note="nuget.org source registered (was missing).")
        return _status(
            False, ver, p,
            note="nuget.org package source missing — WiX restore will fail.",
            hint="Run: nuget sources Add -Name nuget.org "
                 "-Source https://api.nuget.org/v3/index.json")
    return _status(True, ver, p, note="Needed for MSI / WiX package restore.")


def check_dotnet():
    """ .NET SDK — required to resolve WixToolset.Sdk for MSI (WiX 4). """
    if _system() != "Windows":
        return _status(False, note="Windows only (MSI / WiX 4 packaging).")
    p = _which("dotnet")
    if not p:
        # Common install location not yet on PATH for this process
        for cand in (
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                         "dotnet", "dotnet.exe"),
            r"C:\Program Files\dotnet\dotnet.exe",
        ):
            if os.path.isfile(cand):
                p = cand
                break
    if not p:
        return _status(False, hint=_install_hint("dotnet"))
    # Prefer listing SDKs — runtime-only installs have no SDKs and cannot
    # restore WixToolset.Sdk.
    try:
        out = subprocess.check_output(
            [p, "--list-sdks"],
            stderr=subprocess.STDOUT, timeout=20,
            encoding="utf-8", errors="replace",
        ).strip()
    except Exception:
        out = ""
    if not out:
        ver = _run_version([p, "--version"]) or "dotnet"
        return _status(
            False, ver, p,
            note="dotnet found but no SDK installed (runtime only?). "
                 "WiX 4 needs an SDK (e.g. .NET 8).",
            hint=_install_hint("dotnet"))
    # First line like: "8.0.423 [C:\Program Files\dotnet\sdk]"
    first = out.splitlines()[0].strip()
    return _status(True, first, p,
                   note="Needed for WiX Toolset SDK 4.x (Windows MSI).")


def check_imagemagick():
    """ImageMagick — needed for icon/logo resizing and ICO/ICNS generation."""
    for name in ("magick", "convert"):
        p = _which(name)
        if p:
            ver = _run_version([name, "--version"]) or name
            return _status(True, ver, p,
                           note="Required for custom icon/logo branding.")
    return _status(False, hint=_install_hint("imagemagick"))


def check_iconutil():
    """iconutil — macOS only, creates .icns from iconset."""
    if _system() != "macOS":
        return _status(False, note="macOS only.")
    p = _which("iconutil")
    if p:
        return _status(True, "iconutil", p,
                       note="Required for custom macOS .icns icon.")
    return _status(False, hint=_install_hint("iconutil"))


def check_potrace():
    """potrace — optional, converts PNG logos to SVG."""
    p = _which("potrace")
    if p:
        return _status(True, _run_version(["potrace", "--version"]) or "potrace",
                       p, note="Optional: converts PNG logos to SVG for in-app display.")
    return _status(False, hint=_install_hint("potrace"))


CHECKS = {
    "git": check_git,
    "python": check_python,
    "rust": check_rust,
    "rust_target": check_rust_target,
    "flutter": check_flutter,
    "clang": check_clang,
    "llvm": check_llvm,
    "vcpkg": check_vcpkg,
    "msbuild": check_msbuild,
    "nuget": check_nuget,
    "dotnet": check_dotnet,
    "java": check_java,
    "android_ndk": check_android_ndk,
    "xcode": check_xcode,
    "rpmbuild": check_rpmbuild,
    "appimage_builder": check_appimage_builder,
    "sccache": check_sccache,
    "imagemagick": check_imagemagick,
    "iconutil": check_iconutil,
    "potrace": check_potrace,
}

LABELS = {
    "git": "Git",
    "python": "Python 3",
    "rust": "Rust toolchain (rustc + cargo)",
    "rust_target": "Rust target (MSVC vs GNU)",
    "flutter": "Flutter SDK",
    "clang": "C/C++ compiler (clang/gcc)",
    "llvm": "LLVM / libclang",
    "vcpkg": "vcpkg (native deps: ffmpeg, hwcodec)",
    "msbuild": "MSBuild (Visual Studio)",
    "nuget": "NuGet CLI (MSI / WiX packages)",
    "dotnet": ".NET SDK (WiX Toolset / MSI)",
    "java": "Java (JDK 17)",
    "android_ndk": "Android NDK",
    "xcode": "Xcode command-line tools",
    "rpmbuild": "rpmbuild (RPM packaging)",
    "appimage_builder": "appimage-builder (AppImage packaging)",
    "sccache": "sccache (Rust/C++ compilation cache)",
    "imagemagick": "ImageMagick (icon/logo branding)",
    "iconutil": "iconutil (macOS .icns generation)",
    "potrace": "potrace (PNG→SVG logo conversion)",
}


def check_all():
    return {k: fn() for k, fn in CHECKS.items()}


def summary():
    all_status = check_all()
    out = []
    for k, st in all_status.items():
        out.append({"id": k, "label": LABELS.get(k, k), **st})
    return out


# ---------------------------------------------------------------------------
# per-OS install hints
# ---------------------------------------------------------------------------

def _install_hint(tool):
    os_name = _system()
    hints = {
        "git": {
            "Windows": "Install Git for Windows: https://git-scm.com/download/win",
            "Linux": "sudo apt install git   (or your distro's package manager)",
            "macOS": "xcode-select --install   (bundles git), or: brew install git",
        },
        "rust": {
            "Windows": "Install rustup: https://rustup.rs  then: rustup toolchain install 1.75",
            "Linux": "curl https://sh.rustup.rs -sSf | sh   then: rustup toolchain install 1.75",
            "macOS": "curl https://sh.rustup.rs -sSf | sh   then: rustup toolchain install 1.75",
        },
        "flutter": {
            "Windows": "Install Flutter 3.24.5: https://docs.flutter.dev/get-started/install/windows",
            "Linux": "Install Flutter 3.24.5: https://docs.flutter.dev/get-started/install/linux",
            "macOS": "Install Flutter 3.24.5: https://docs.flutter.dev/get-started/install/macos  (or: brew install --cask flutter)",
        },
        "clang": {
            "Windows": "Comes with Visual Studio C++ workload.",
            "Linux": "sudo apt install clang cmake ninja-build pkg-config libgtk-3-dev",
            "macOS": "xcode-select --install",
        },
        "llvm": {
            "Windows": "Install LLVM 15: https://github.com/llvm/llvm-project/releases  and set LIBCLANG_PATH.",
            "Linux": "sudo apt install llvm-dev libclang-dev clang",
            "macOS": "brew install llvm@15",
        },
        "vcpkg": {
            "Windows": "git clone https://github.com/microsoft/vcpkg  then set VCPKG_ROOT. The builder checks out the pinned commit.",
            "Linux": "git clone https://github.com/microsoft/vcpkg  then set VCPKG_ROOT.",
            "macOS": "git clone https://github.com/microsoft/vcpkg  then set VCPKG_ROOT.",
        },
        "msbuild": {
            "Windows": "Click install to get Build Tools for Visual Studio (C++) — the command-line MSVC toolset (link.exe) + MSBuild, no full IDE.",
            "Linux": "N/A — MSI is Windows-only.",
            "macOS": "N/A — MSI is Windows-only.",
        },
        "java": {
            "Windows": "Install JDK 17: https://adoptium.net",
            "Linux": "sudo apt install openjdk-17-jdk",
            "macOS": "brew install openjdk@17",
        },
        "android_ndk": {
            "Windows": "Install Android Studio, add NDK r28c via SDK Manager, set ANDROID_NDK_HOME.",
            "Linux": "Install Android command-line tools + NDK r28c, set ANDROID_NDK_HOME.",
            "macOS": "Install Android Studio / cmdline-tools + NDK r28c, set ANDROID_NDK_HOME.",
        },
        "xcode": {
            "macOS": "xcode-select --install   then: brew install create-dmg",
        },
        "rpmbuild": {
            "Linux": "sudo apt install rpm  (or: sudo dnf install rpm-build)",
        },
        "appimage_builder": {
            "Linux": "sudo apt install libarchive-tools libfuse2 && sudo pip3 install setuptools_scm<10 && sudo pip3 install git+https://github.com/rustdesk-org/appimage-builder.git",
        },
        "sccache": {
            "Windows": "cargo install sccache",
            "Linux": "cargo install sccache",
            "macOS": "cargo install sccache",
        },
        "imagemagick": {
            "Windows": "Download from https://imagemagick.org/script/download.php#windows  or: choco install imagemagick",
            "Linux": "sudo apt install imagemagick",
            "macOS": "brew install imagemagick",
        },
        "iconutil": {
            "macOS": "Comes with Xcode command-line tools: xcode-select --install",
        },
        "potrace": {
            "Windows": "Download from https://potrace.sourceforge.net/#downloading",
            "Linux": "sudo apt install potrace",
            "macOS": "brew install potrace",
        },
        "nuget": {
            "Windows": "choco install nuget.commandline   "
                       "(then ensure nuget.org: nuget sources Add -Name nuget.org "
                       "-Source https://api.nuget.org/v3/index.json)",
            "Linux": "N/A — MSI is Windows-only.",
            "macOS": "N/A — MSI is Windows-only.",
        },
        "dotnet": {
            "Windows": "winget install Microsoft.DotNet.SDK.8   "
                       "or: https://dotnet.microsoft.com/download/dotnet/8.0  "
                       "(.NET 8+ SDK required for WiX Toolset 4 / MSI)",
            "Linux": "N/A — MSI is Windows-only.",
            "macOS": "N/A — MSI is Windows-only.",
        },
    }
    return hints.get(tool, {}).get(os_name, "")


if __name__ == "__main__":
    import json
    print(json.dumps(summary(), indent=2))
