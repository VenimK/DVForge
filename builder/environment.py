"""Selected-target dependency policy. Report-only by default; no shell recipes."""

import argparse
import json
import os
import platform
import shutil
import shlex
import subprocess
from ctypes.util import find_library

from . import detect, prereqs, toolchains

LINUX_PACKAGES = {
    "apt": {"git": ["git"], "clang": ["clang"], "llvm": ["llvm-dev", "libclang-dev"],
            "cmake": ["cmake"], "ninja": ["ninja-build"], "nasm": ["nasm"],
            "pkgconfig": ["pkg-config"], "rpmbuild": ["rpm"],
            "linux_libraries": ["libgtk-3-dev", "libxdo-dev", "libxtst-dev", "libva-dev",
                                "libvdpau-dev", "libpulse-dev", "libayatana-appindicator3-dev"]},
    "dnf": {"git": ["git"], "clang": ["clang"], "llvm": ["llvm-devel", "clang-devel"],
            "cmake": ["cmake"], "ninja": ["ninja-build"], "nasm": ["nasm"],
            "pkgconfig": ["pkgconf-pkg-config"], "rpmbuild": ["rpm-build"],
            "linux_libraries": ["gtk3-devel", "libXdo-devel", "libXtst-devel", "libva-devel",
                                "libvdpau-devel", "pulseaudio-libs-devel", "libayatana-appindicator-gtk3-devel"]},
    "pacman": {"git": ["git"], "clang": ["clang"], "llvm": ["llvm"],
               "cmake": ["cmake"], "ninja": ["ninja"], "nasm": ["nasm"],
               "pkgconfig": ["pkgconf"], "rpmbuild": ["rpm-tools"],
               "linux_libraries": ["gtk3", "xdotool", "libxtst", "libva", "libvdpau",
                                   "libpulse", "libayatana-appindicator"]},
}
BREW_PACKAGES = {"git": "git", "cmake": "cmake", "ninja": "ninja", "nasm": "nasm",
                 "pkgconfig": "pkg-config", "create_dmg": "create-dmg", "cocoapods": "cocoapods"}
WINGET_PACKAGES = {"git": "Git.Git", "cmake": "Kitware.CMake", "ninja": "Ninja-build.Ninja"}


def package_manager(release=None):
    if release is None:
        if hasattr(platform, "freedesktop_os_release"):
            release = platform.freedesktop_os_release()
        else:
            release = {}
            with open("/etc/os-release", encoding="utf-8") as stream:
                for line in stream:
                    if "=" in line and not line.lstrip().startswith("#"):
                        key, value = line.strip().split("=", 1)
                        tokens = shlex.split(value)
                        release[key] = tokens[0] if tokens else ""
    ids = {release.get("ID", ""), *release.get("ID_LIKE", "").split()}
    for manager, distros in (("apt", {"debian", "ubuntu"}),
                            ("dnf", {"fedora", "rhel", "centos"}),
                            ("pacman", {"arch", "manjaro"})):
        if ids & distros:
            return manager
    return None


def required(target_ids, host):
    targets = {t["id"]: t for t in detect.TARGETS}
    needed = set()
    problems = []
    for tid in target_ids:
        target = targets.get(tid)
        if not target:
            problems.append(f"Unknown target: {tid}")
            continue
        if host["os"] not in target["host_os"]:
            problems.append(f"{tid} requires a configured remote {target['platform']} worker")
            continue
        if target["platform"] in ("linux", "windows") and target["arch"] != host["arch"]:
            problems.append(f"{tid} requires a native {target['arch']} {target['platform']} worker; local cross-build is unsupported")
            continue
        needed.update(detect.required_tools(target, host["os"]))
        if target["platform"] == "linux":
            needed.add("linux_libraries")
            if target["ext"] in ("deb", "AppImage"):
                needed.add("dpkg_deb")
    return sorted(needed), problems


def check(tool):
    if tool == "linux_libraries":
        modules = ["gtk+-3.0", "x11", "xtst", "libva", "vdpau", "libpulse",
                   "ayatana-appindicator3-0.1"]
        try:
            proc = subprocess.run(["pkg-config", "--exists", *modules],
                                  capture_output=True, timeout=20)
            ok = proc.returncode == 0 and bool(find_library("xdo"))
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        return {"present": ok, "label": "Linux desktop libraries",
                "hint": ", ".join(modules) + ", libxdo"}
    if tool == "dpkg_deb":
        return {"present": bool(shutil.which("dpkg-deb")), "label": "dpkg-deb",
                "hint": "Install dpkg packaging tools"}
    st = dict(prereqs.CHECKS[tool]())
    st["label"] = prereqs.LABELS.get(tool, tool)
    if tool == "flutter" and st.get("present") and "3.24.5" not in st.get("version", ""):
        st.update(present=False, note="Pinned Flutter 3.24.5 required")
    if tool == "appimage_builder" and not shutil.which("apt-key"):
        st.update(present=False, hint="Legacy AppImage recipe requires apt-key; use a compatible Debian/Ubuntu build container, not a dummy apt-key shim")
    return st


def report(target_ids, host=None):
    host = host or detect.host_info()
    needed, problems = required(target_ids, host)
    rows = []
    for tool in needed:
        row = {"id": tool, **check(tool)}
        row["status"] = "installed" if row["present"] else "missing"
        rows.append(row)
    problems += [f"Missing or invalid: {r['id']}" for r in rows if not r["present"]]
    return {"ok": not problems, "host": host, "requirements": rows, "problems": problems}


def native_command(tool, host, release=None):
    if host["os"] == "Linux":
        manager = package_manager(release)
        packages = LINUX_PACKAGES.get(manager, {}).get(tool)
        if tool == "dpkg_deb":
            packages = ["dpkg"] if manager == "apt" else None
        if not packages:
            return None
        args = {"apt": ["apt-get", "install", "-y"],
                "dnf": ["dnf", "install", "-y"],
                "pacman": ["pacman", "-S", "--needed", "--noconfirm"]}[manager]
        return ([] if os.geteuid() == 0 else ["sudo", "-n"]) + args + packages
    if host["os"] == "macOS" and tool in BREW_PACKAGES:
        return ["brew", "install", BREW_PACKAGES[tool]]
    if host["os"] == "Windows" and tool in WINGET_PACKAGES:
        return ["winget", "install", "--id", WINGET_PACKAGES[tool], "--exact",
                "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"]
    return None


def run_native_installer(command, log):
    """Use the desktop's Polkit agent when unattended sudo needs authentication."""
    result = subprocess.run(command, capture_output=True, text=True, timeout=1800)
    needs_auth = (command[:2] == ["sudo", "-n"] and result.returncode and
                  any(message in (result.stderr or "").lower() for message in
                      ("a password is required", "a terminal is required", "no tty present")))
    if not needs_auth:
        return result
    # Only elevate the fixed package-manager command, never the web server.
    managers = {"apt-get": "/usr/bin/apt-get", "dnf": "/usr/bin/dnf",
                "pacman": "/usr/bin/pacman"}
    executable = managers.get(command[2])
    if not executable or not os.path.isfile(executable):
        raise RuntimeError("Supported system package manager not found")
    from . import terminal_approval
    if not shutil.which("pkexec"):
        return terminal_approval.install([executable, *command[3:]], log)
    log("Administrator approval required: accept the authentication dialog on the Linux build PC.")
    result = subprocess.run([shutil.which("pkexec"), "--disable-internal-agent", executable,
                             *command[3:]], capture_output=True, text=True, timeout=1800)
    if result.returncode and "no authentication agent found" in (result.stderr or "").lower():
        return terminal_approval.install([executable, *command[3:]], log)
    if result.returncode in (126, 127):
        raise RuntimeError("Administrator approval cancelled or unavailable. Run DVForge in "
                           "the logged-in Linux desktop session with a Polkit authentication agent, "
                           "then retry Prepare environment. " + (result.stderr or ""))
    return result


def ensure(target_ids, root, log, auto_install=False, cancelled=lambda: False):
    before = report(target_ids)
    if before["ok"] or not auto_install:
        return before
    _, routing = required(target_ids, before["host"])
    if routing:
        return before
    reverse = {satisfied: installer for installer, satisfied in toolchains.SATISFIES.items()}
    if before["host"]["os"] == "Windows":
        reverse["clang"] = "vs_buildtools"
    supported = toolchains.installable(before["host"]["os"],
                                      "arm64" if before["host"]["arch"] == "aarch64" else before["host"]["arch"])
    # JDK must precede SDK; preserve the registry's existing dependency order.
    order = {tid: n for n, tid in enumerate(toolchains.TOOLS)}
    rows = sorted(before["requirements"], key=lambda r: order.get(reverse.get(r["id"]), -1))
    for row in rows:
        if cancelled():
            return {**before, "ok": False, "problems": ["Dependency installation cancelled"]}
        tool = row["id"]
        if check(tool)["present"]:
            log(f"Environment: {tool}: installed (skip)")
            continue
        log(f"Environment: {tool}: installing")
        try:
            command = native_command(tool, before["host"])
            installer = reverse.get(tool)
            if command:
                proc = run_native_installer(command, log)
                log(proc.stdout or "")
                if proc.returncode:
                    raise RuntimeError(f"Installer exit {proc.returncode}: {proc.stderr}")
            elif installer and supported.get(installer, {}).get("ok"):
                result = toolchains.install_many([installer], root, log, cancelled)
                if result["errors"]:
                    raise RuntimeError(str(result["errors"]))
                toolchains.apply_persisted_env(root)
            else:
                raise RuntimeError(f"No safe automatic installer for {tool}; {row.get('hint', '')}")
            if not check(tool)["present"]:
                raise RuntimeError(f"Post-install verification failed: {tool}. Restart may be required to refresh PATH.")
            log(f"Environment: {tool}: installed (verified)")
        except Exception as exc:
            log(f"Environment: {tool}: failed: {exc}")
            return {**before, "ok": False, "problems": [f"{tool}: {exc}"]}
    return report(target_ids, before["host"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("targets", nargs="+")
    parser.add_argument("--install", action="store_true")
    options = parser.parse_args()
    result = ensure(options.targets, os.path.dirname(os.path.dirname(__file__)),
                    print, auto_install=options.install)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
