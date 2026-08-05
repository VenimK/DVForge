"""
detect.py — figure out what machine we're on and what it can build.

Two jobs:
  1. host_info()    -> hardware + OS facts for the dashboard
  2. build_matrix() -> for every RustDesk target, can THIS host build it, and why/why not

The rules come straight from the GitHub Actions workflows in rustdesk-builder-v2:
  - Windows desktop  = Flutter Windows engine, MSVC toolchain  -> Windows host only
  - macOS desktop    = Xcode / clang / create-dmg              -> macOS host only
  - Linux desktop    = gcc/clang + flutter-elinux for arm64     -> Linux host only
  - Android (all archs) = Android NDK (cross-platform)          -> any host
"""

import os
import platform
import shutil
import subprocess
import multiprocessing


# ---------------------------------------------------------------------------
# hardware / OS detection
# ---------------------------------------------------------------------------

def _bytes_to_gb(n):
    try:
        return round(int(n) / (1024 ** 3), 1)
    except Exception:
        return None


def _total_ram_gb():
    """Total physical RAM in GB, cross-platform, stdlib only."""
    # POSIX (Linux, most Unix): sysconf
    try:
        if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            return _bytes_to_gb(pages * page_size)
    except Exception:
        pass
    # macOS: sysctl hw.memsize
    try:
        if platform.system() == "Darwin":
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip()
            return _bytes_to_gb(out)
    except Exception:
        pass
    # Windows: wmic / powershell
    if platform.system() == "Windows":
        try:
            out = subprocess.check_output(
                ["wmic", "ComputerSystem", "get", "TotalPhysicalMemory"],
                text=True, stderr=subprocess.DEVNULL,
            )
            for line in out.splitlines():
                line = line.strip()
                if line.isdigit():
                    return _bytes_to_gb(line)
        except Exception:
            pass
        try:
            ps = ("(Get-CimInstance Win32_ComputerSystem)."
                  "TotalPhysicalMemory")
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", ps],
                text=True, stderr=subprocess.DEVNULL,
            ).strip()
            return _bytes_to_gb(out)
        except Exception:
            pass
    return None


def _free_disk_gb(path="."):
    try:
        usage = shutil.disk_usage(os.path.abspath(path))
        return round(usage.free / (1024 ** 3), 1)
    except Exception:
        return None


def _cpu_name():
    system = platform.system()
    try:
        if system == "Linux":
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.lower().startswith("model name"):
                        return line.split(":", 1)[1].strip()
        elif system == "Darwin":
            return subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
            ).strip()
        elif system == "Windows":
            name = os.environ.get("PROCESSOR_IDENTIFIER")
            if name:
                return name
    except Exception:
        pass
    return platform.processor() or "Unknown CPU"


def normalize_arch(machine=None):
    """Collapse the many spellings of an architecture into a canonical token."""
    m = (machine or platform.machine() or "").lower()
    if m in ("x86_64", "amd64", "x64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "aarch64"
    if m in ("armv7l", "armv7", "arm"):
        return "armv7"
    if m in ("i386", "i686", "x86"):
        return "i686"
    return m or "unknown"


def host_info():
    system = platform.system()          # 'Windows' | 'Linux' | 'Darwin'
    os_name = {"Darwin": "macOS"}.get(system, system)
    arch = normalize_arch()

    info = {
        "os": os_name,
        "os_raw": system,
        "os_release": platform.release(),
        "os_version": platform.version(),
        "arch": arch,
        "arch_raw": platform.machine(),
        "cpu": _cpu_name(),
        "cores_logical": os.cpu_count() or multiprocessing.cpu_count(),
        "ram_gb": _total_ram_gb(),
        "free_disk_gb": _free_disk_gb(),
        "hostname": platform.node(),
        "python": platform.python_version(),
    }
    # a couple of Linux niceties
    if system == "Linux":
        info["distro"] = _linux_distro()
    return info


def _linux_distro():
    try:
        data = {}
        with open("/etc/os-release") as f:
            for line in f:
                if "=" in line:
                    k, v = line.rstrip().split("=", 1)
                    data[k] = v.strip('"')
        return data.get("PRETTY_NAME") or data.get("NAME") or "Linux"
    except Exception:
        return "Linux"


# ---------------------------------------------------------------------------
# build capability matrix
# ---------------------------------------------------------------------------
#
# Each target = one artifact the builder can produce. `host_os` lists which
# host operating systems can build it natively (no emulation). We do NOT claim
# cross-OS desktop builds (e.g. Windows .exe from Linux) because the workflows
# don't do that and it isn't reliable without a full MSVC / Xcode environment.

TARGETS = [
    # ---- Windows (Flutter, 64-bit) -> Windows host only ----
    {"id": "windows-x86_64-exe", "platform": "windows", "arch": "x86_64",
     "label": "Windows 64-bit — portable .exe", "ext": "exe",
     "host_os": ["Windows"],
     "note": "Self-extracting portable executable (Flutter engine is 64-bit only)."},
    {"id": "windows-x86_64-msi", "platform": "windows", "arch": "x86_64",
     "label": "Windows 64-bit — installer .msi", "ext": "msi",
     "host_os": ["Windows"],
     "note": "MSI installer. Needs MSBuild, NuGet (nuget.org), and .NET 8+ SDK (WiX 4)."},

    # ---- Linux (Flutter) -> Linux host only ----
    {"id": "linux-x86_64-deb", "platform": "linux", "arch": "x86_64",
     "label": "Linux x86_64 — .deb", "ext": "deb", "host_os": ["Linux"],
     "note": "Debian/Ubuntu package."},
    {"id": "linux-x86_64-rpm", "platform": "linux", "arch": "x86_64",
     "label": "Linux x86_64 — .rpm (Fedora)", "ext": "rpm", "host_os": ["Linux"],
     "note": "Fedora/RHEL package."},
    {"id": "linux-x86_64-appimage", "platform": "linux", "arch": "x86_64",
     "label": "Linux x86_64 — .AppImage", "ext": "AppImage", "host_os": ["Linux"],
     "note": "Portable, distro-independent."},
    {"id": "linux-aarch64-deb", "platform": "linux", "arch": "aarch64",
     "label": "Linux arm64 — .deb", "ext": "deb", "host_os": ["Linux"],
     "note": "arm64 build. Native on an arm64 Linux host; needs flutter-elinux."},

    # ---- Android (NDK, cross-platform: builds on any host) ----
    {"id": "android-arm64", "platform": "android", "arch": "aarch64",
     "label": "Android arm64-v8a APK", "ext": "apk", "host_os": ["Linux", "macOS", "Windows"],
     "note": "Most modern phones. Built via Android NDK on any host."},
    {"id": "android-armv7", "platform": "android", "arch": "armv7",
     "label": "Android armeabi-v7a APK", "ext": "apk", "host_os": ["Linux", "macOS", "Windows"],
     "note": "Older 32-bit phones."},
    {"id": "android-x86_64", "platform": "android", "arch": "x86_64",
     "label": "Android x86_64 APK", "ext": "apk", "host_os": ["Linux", "macOS", "Windows"],
     "note": "Emulators / x86 tablets."},
    {"id": "android-universal", "platform": "android", "arch": "universal",
     "label": "Android universal APK (all ABIs)", "ext": "apk",
     "host_os": ["Linux", "macOS", "Windows"],
     "note": "One APK for every device — recommended. Reuses the per-arch native libs."},

    # ---- macOS (Xcode) -> macOS host only ----
    {"id": "macos-universal-dmg", "platform": "macos", "arch": "universal",
     "label": "macOS — .dmg", "ext": "dmg", "host_os": ["macOS"],
     "note": "Needs Xcode command-line tools + create-dmg."},
]


def build_matrix(host=None, prereqs=None):
    """
    Returns a list of targets, each annotated with:
      buildable      : host OS supports this target at all
      ready          : buildable AND required toolchains are present
      blocked_reason : short human string when not ready
      missing_tools  : list of tool ids still needed (from prereqs)
    `prereqs` is the dict returned by prereqs.check_all() (optional).
    """
    host = host or host_info()
    host_os = host["os_raw"]                       # 'Windows'|'Linux'|'Darwin'
    host_os_name = host["os"]                      # 'Windows'|'Linux'|'macOS'
    host_arch = host["arch"]

    rows = []
    for t in TARGETS:
        buildable = host_os_name in t["host_os"]
        row = dict(t)
        row["buildable"] = buildable
        row["missing_tools"] = []
        row["blocked_reason"] = ""

        if not buildable:
            hosts = " or ".join(t["host_os"])
            row["blocked_reason"] = f"Needs a {hosts} host — you're on {host_os_name}."
            row["ready"] = False
            rows.append(row)
            continue

        # arm64 desktop cross note: buildable but slower / needs elinux unless host is arm64
        if t["arch"] == "aarch64" and t["platform"] == "linux" and host_arch != "aarch64":
            row["blocked_reason"] = "Cross-compiles from x86_64 via flutter-elinux (slower)."

        # toolchain readiness
        needed = required_tools(t, host_os_name)
        missing = []
        if prereqs:
            for tool in needed:
                st = prereqs.get(tool)
                if not st or not st.get("present"):
                    missing.append(tool)
        row["missing_tools"] = missing
        row["required_tools"] = needed
        row["ready"] = buildable and not missing
        if missing and not row["blocked_reason"]:
            row["blocked_reason"] = "Missing: " + ", ".join(missing)
        rows.append(row)
    return rows


def required_tools(target, host_os_name):
    """Which prereq ids a given target needs."""
    common = ["git", "python", "rust"]
    p = target["platform"]
    if p == "windows":
        tools = common + ["flutter", "llvm", "vcpkg"]
        if target["ext"] == "msi":
            # MSI packaging: VS MSBuild builds msi.sln; nuget restores
            # CustomActions packages; dotnet resolves WixToolset.Sdk 4.x.
            tools += ["msbuild", "nuget", "dotnet"]
        return tools
    if p == "linux":
        tools = common + ["flutter", "clang", "vcpkg"]
        if target["ext"] == "rpm":
            tools += ["rpmbuild"]
        if target["ext"] == "AppImage":
            tools += ["appimage_builder"]
        return tools
    if p == "macos":
        return common + ["flutter", "xcode"]
    if p == "android":
        tools = common + ["flutter", "android_ndk", "java"]
        # On a Windows host the Rust host toolchain is MSVC, so building anything
        # (even Android — its build scripts/proc-macros compile for the host)
        # needs link.exe from the VC++ Build Tools. Reflect that honestly.
        if host_os_name == "Windows":
            tools += ["msbuild"]
        return tools
    return common


if __name__ == "__main__":
    import json
    h = host_info()
    print(json.dumps(h, indent=2))
    print("--- matrix ---")
    for r in build_matrix(h):
        flag = "READY" if r["ready"] else ("OK " if r["buildable"] else "NO ")
        print(f"[{flag}] {r['label']:<40} {r['blocked_reason']}")
