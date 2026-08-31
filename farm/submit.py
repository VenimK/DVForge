#!/usr/bin/env python3
"""Drop a job into farm/inbox/ for a DVForge worker.

Mac:
  python3 farm/submit.py --targets macos-arm64-dmg
Windows:
  python farm\\submit.py --targets windows-x86_64-exe

Both machines must see the same farm/ folder (this NAS copy of the repo).
One job = one OS. Don't mix macos-* and windows-* in the same file.
"""
from __future__ import print_function

import argparse
import json
import os
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
INBOX = os.path.join(HERE, "inbox")
DEFAULT_CONFIG = os.path.join(ROOT, "configs", "RustDesk.json")


def main():
    p = argparse.ArgumentParser(description="Submit a DVForge farm job")
    p.add_argument("--targets", required=True, nargs="+",
                   help="e.g. macos-arm64-dmg  or  windows-x86_64-exe windows-x86_64-msi")
    p.add_argument("--version", default="1.4.9")
    p.add_argument("--config", default=DEFAULT_CONFIG,
                   help="RustDesk.json to bake (default: configs/RustDesk.json)")
    p.add_argument("--dry-run", action="store_true",
                   help="Worker prints the plan only, does not compile")
    p.add_argument("--id", default="", help="Job id (default: timestamp + uuid)")
    p.add_argument("--assign", default="",
                   help="Only this worker hostname may claim the job")
    args = p.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)

    plats = set()
    for t in args.targets:
        if t.startswith("macos-"):
            plats.add("macos")
        elif t.startswith("windows-"):
            plats.add("windows")
        elif t.startswith("linux-"):
            plats.add("linux")
        elif t.startswith("android-"):
            plats.add("android")
        else:
            sys.exit("unknown target: " + t)
    if len(plats) > 1:
        sys.exit("one job must be one OS family (got %s). Submit two jobs."
                 % ", ".join(sorted(plats)))

    jid = args.id.strip() or (
        time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    job = {
        "id": jid,
        "version": args.version,
        "targets": list(args.targets),
        "dry_run": bool(args.dry_run),
        "config": cfg,
        "submitted": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if args.assign.strip():
        job["assign"] = args.assign.strip()
    os.makedirs(INBOX, exist_ok=True)
    dest = os.path.join(INBOX, jid + ".json")
    tmp = dest + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f, indent=2)
        f.write("\n")
    os.replace(tmp, dest)
    print("queued", dest)
    print("targets:", " ".join(args.targets),
          "| dry_run=" + str(job["dry_run"]))
    if job.get("assign"):
        print("assign:", job["assign"])
    print("workers pick this up from farm/inbox/ (Mac vs Windows by target)")


if __name__ == "__main__":
    main()
