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
    print("RustDesk Local Builder — system toolchain precheck")
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
