#!/usr/bin/env python3
"""
precheck.py — quick system toolchain probe.

Reports which build tools are already installed on the system (outside the
project's .toolchains/ folder) and which ones the builder would still need to
install or download.
"""

import json
import os
import sys

# Allow running from project root or builder/
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from builder import prereqs  # noqa: E402


def _is_in_toolchains(path):
    """Return True if the resolved path lives under the project's .toolchains."""
    if not path:
        return False
    try:
        abs_path = os.path.abspath(os.path.realpath(path))
        tc_root = os.path.abspath(os.path.realpath(os.path.join(ROOT_DIR, ".toolchains")))
        return abs_path.startswith(tc_root + os.sep)
    except OSError:
        return False


def main():
    results = prereqs.summary()
    print("=" * 72)
    print("DVForge — system toolchain precheck")
    print("=" * 72)

    installed_system = []
    installed_toolchains = []
    missing = []

    for item in results:
        label = item["label"]
        present = item.get("present", False)
        path = item.get("path", "")
        version = item.get("version", "")
        note = item.get("note", "")
        hint = item.get("hint", "")

        if not present:
            missing.append((label, note, hint))
            continue

        in_tc = _is_in_toolchains(path)
        ver = f" ({version})" if version else ""
        loc = f"  path: {path}" if path else ""

        if in_tc:
            installed_toolchains.append((label, ver, loc, note))
        else:
            installed_system.append((label, ver, loc, note))

    if installed_system:
        print("\n[System-installed tools found outside .toolchains/]")
        for label, ver, loc, note in installed_system:
            print(f"  ✓ {label}{ver}")
            if loc:
                print(loc)
            if note:
                print(f"    note: {note}")

    if installed_toolchains:
        print("\n[Tools already provided by the project .toolchains/ folder]")
        for label, ver, loc, note in installed_toolchains:
            print(f"  ○ {label}{ver} (inside .toolchains/)")
            if loc:
                print(loc)
            if note:
                print(f"    note: {note}")

    if missing:
        print("\n[Missing tools that may need to be installed]")
        for label, note, hint in missing:
            print(f"  ✗ {label}")
            if note:
                print(f"    note: {note}")
            if hint:
                print(f"    hint: {hint}")

    print("\n" + "=" * 72)
    print(f"Summary: {len(installed_system)} system, {len(installed_toolchains)} toolchains, {len(missing)} missing")
    print("=" * 72)

    # Windows path-length tip — deep Flutter/MSBuild paths fail under long
    # install locations (e.g. C:\Users\…\Downloads\…). Toolchains env.json is
    # portable (relative), but the build tree still benefits from a short root.
    if sys.platform == "win32":
        root_len = len(ROOT_DIR)
        long_paths = False
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\FileSystem",
            ) as key:
                val, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
                long_paths = int(val) == 1
        except Exception:
            pass
        print("\n[Windows path portability]")
        print(f"  install root: {ROOT_DIR}  ({root_len} chars)")
        print(f"  LongPathsEnabled: {'yes' if long_paths else 'no'}")
        if root_len > 40 or not long_paths:
            drive = os.path.splitdrive(ROOT_DIR)[0] or "C:"
            print("  tip: for reliable Windows builds on every machine, install at a")
            print(f"       short fixed path such as {drive}\\DVForge (same layout everywhere).")
            print("       .toolchains/env.json stores project-relative paths, so the")
            print("       folder can move; deep MSBuild paths still prefer a short root.")
            if not long_paths:
                print("  tip: enable Win32 long paths (admin) if you must use a deep path:")
                print("       New-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem'")
                print("         -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force")

    # Optional JSON output for scripts/CI
    if "--json" in sys.argv:
        print(json.dumps({
            "system": [
                {"label": r[0], "version": r[1].strip(" ()"), "path": r[2].replace("  path: ", "").strip(), "note": r[3]}
                for r in installed_system
            ],
            "toolchains": [
                {"label": r[0], "version": r[1].strip(" ()"), "path": r[2].replace("  path: ", "").strip(), "note": r[3]}
                for r in installed_toolchains
            ],
            "missing": [
                {"label": r[0], "note": r[1], "hint": r[2]}
                for r in missing
            ],
        }, indent=2))

    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
