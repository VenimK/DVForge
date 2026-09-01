#!/usr/bin/env python3
"""LAN job inbox — curl a JSON file in; workers still talk to localhost DVForge.

This is NOT DVForge. It only writes farm/inbox/*.json.
Do not put it on the public internet without a token and HTTPS.

  python3 farm/queue.py
  python3 farm/queue.py --host 0.0.0.0 --port 8766

Optional:  export DVFORGE_FARM_TOKEN=secret

Curl (config file as body, targets in the URL):

  curl -sS -X POST \\
    'http://MAC_OR_NAS:8766/job?targets=windows-x86_64-exe&version=1.4.9' \\
    -H 'Content-Type: application/json' \\
    --data-binary @RustDesk.json

Or a full job object:

  curl -sS -X POST http://MAC_OR_NAS:8766/job \\
    -H 'Content-Type: application/json' \\
    --data-binary @job.json
"""
from __future__ import print_function

import argparse
import json
import mimetypes
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(HERE, "public")
FARM = os.environ.get("DVFORGE_FARM", HERE)
TOKEN = os.environ.get("DVFORGE_FARM_TOKEN", "").strip()
INBOX = os.path.join(FARM, "inbox")
OUTBOX = os.path.join(FARM, "outbox")
FAILED = os.path.join(FARM, "failed")
RUNNING = os.path.join(FARM, "running")
SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")

# Same prefixes as worker.py — HTTP workers claim through /claim instead of NFS.
CLAIM = {
    "Darwin": ("macos-",),
    "Windows": ("windows-",),
    "Linux": ("linux-", "android-"),
}
OS_LABEL = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}
ONLINE_SEC = 45
# Ban after this many attempts with zero successes, or this many fails in a row.
BAN_AFTER_FAIL_ONLY = 2
BAN_AFTER_STREAK = 5
STARTED = time.time()
WORKERS = {}
RATINGS = {}
RATINGS_FILE = os.path.join(FARM, "ratings.json")
_LOCK = threading.Lock()


def _jid():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]


def _prefixes(os_name, android_on_mac=False):
    p = list(CLAIM.get(os_name) or ())
    if os_name == "Darwin" and android_on_mac:
        p.append("android-")
    return tuple(p)


def _can_claim(job, os_name, android_on_mac=False):
    targets = job.get("targets") or []
    if not targets:
        return False
    pref = _prefixes(os_name, android_on_mac)
    if not pref:
        return False
    return all(any(str(t).startswith(x) for x in pref) for t in targets)


def _job_assign(job):
    return (job.get("assign") or job.get("assign_worker") or "").strip()


def load_ratings():
    global RATINGS
    try:
        with open(RATINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        RATINGS = data if isinstance(data, dict) else {}
    except Exception:
        RATINGS = {}


def save_ratings():
    os.makedirs(os.path.dirname(RATINGS_FILE) or ".", exist_ok=True)
    tmp = RATINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(RATINGS, f, indent=2)
        f.write("\n")
    os.replace(tmp, RATINGS_FILE)


def rating_public(name):
    rec = RATINGS.get(name) or {}
    s = int(rec.get("successes") or 0)
    f = int(rec.get("failures") or 0)
    streak = int(rec.get("consecutive_failures") or 0)
    # Laplace: new workers start at 50%, not 0 or 100.
    score = (s + 1) / float(s + f + 2)
    eligible = True
    blocked = ""
    if s + f >= BAN_AFTER_FAIL_ONLY and s == 0:
        eligible = False
        blocked = "failed all %s jobs" % (s + f)
    elif streak >= BAN_AFTER_STREAK:
        eligible = False
        blocked = "%s failures in a row" % streak
    return {
        "score": round(score, 4),
        "score_pct": int(round(score * 100)),
        "successes": s,
        "failures": f,
        "consecutive_failures": streak,
        "eligible": eligible,
        "blocked_reason": blocked,
        "last_error": rec.get("last_error") or "",
    }


def record_result(worker_name, ok, error="", os_name=""):
    name = (worker_name or "").strip()
    if not name:
        return
    with _LOCK:
        rec = dict(RATINGS.get(name) or {})
        rec["successes"] = int(rec.get("successes") or 0)
        rec["failures"] = int(rec.get("failures") or 0)
        rec["consecutive_failures"] = int(rec.get("consecutive_failures") or 0)
        if os_name:
            rec["os"] = os_name
        if ok:
            rec["successes"] += 1
            rec["consecutive_failures"] = 0
            rec["last_error"] = ""
        else:
            rec["failures"] += 1
            rec["consecutive_failures"] += 1
            rec["last_error"] = (error or "")[:200]
        rec["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        RATINGS[name] = rec
        save_ratings()


def reset_rating(worker_name):
    name = (worker_name or "").strip()
    if not name:
        raise ValueError("worker name required")
    with _LOCK:
        rec = dict(RATINGS.get(name) or {})
        rec["successes"] = 0
        rec["failures"] = 0
        rec["consecutive_failures"] = 0
        rec["last_error"] = ""
        rec["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        rec["reset"] = True
        RATINGS[name] = rec
        save_ratings()
    return rating_public(name)


def _better_idle_online(os_name, worker_name):
    """Name of a higher-rated idle worker of the same OS, else None."""
    mine = rating_public(worker_name)["score"]
    now = time.time()
    with _LOCK:
        others = list(WORKERS.values())
    for rec in others:
        name = (rec.get("name") or "").strip()
        if not name or name == worker_name:
            continue
        if (rec.get("os") or "") != os_name:
            continue
        last = float(rec.get("last_seen") or 0)
        if last <= 0 or (now - last) > ONLINE_SEC:
            continue
        if rec.get("busy"):
            continue
        info = rating_public(name)
        if not info["eligible"]:
            continue
        if info["score"] > mine:
            return name
    return None


def claim_job(os_name, worker_name, android_on_mac=False):
    """Rename inbox → running for the best job this worker may take.

    Unassigned jobs: blocked workers are skipped; if a higher-rated idle
    worker of the same OS is online, leave the job for them.
    Assigned jobs (`assign`) go only to that worker, even if blocked
    (so you can test a machine after a fix without resetting first).
    """
    try:
        names = sorted(os.listdir(INBOX))
    except OSError:
        return None
    me_ok = rating_public(worker_name)["eligible"]
    better = _better_idle_online(os_name, worker_name) if me_ok else None
    for name in names:
        if not name.endswith(".json") or name.startswith("."):
            continue
        src = os.path.join(INBOX, name)
        try:
            with open(src, encoding="utf-8") as f:
                job = json.load(f)
        except Exception:
            continue
        if not _can_claim(job, os_name, android_on_mac):
            continue
        assign = _job_assign(job)
        if assign:
            if assign != worker_name:
                continue
        else:
            if not me_ok:
                continue
            if better:
                continue
        os.makedirs(RUNNING, exist_ok=True)
        dest = os.path.join(RUNNING, name)
        try:
            os.rename(src, dest)
        except OSError:
            continue
        job["_file"] = name
        job["_claimed_by"] = worker_name
        return job
    return None


def _pack_job_config(job):
    """If the JSON has icon/logo/p12 paths this host can read, embed the bytes."""
    cfg = job.get("config")
    if not isinstance(cfg, dict):
        return
    root = os.path.dirname(HERE)
    try:
        if root not in sys.path:
            sys.path.insert(0, root)
        from builder import config_gen
        packed = config_gen.pack_portable(cfg, root)
        job["config"] = cfg
        if packed:
            job["portable"] = packed
    except Exception:
        pass


def write_job(job):
    os.makedirs(INBOX, exist_ok=True)
    jid = (job.get("id") or "").strip() or _jid()
    job["id"] = jid
    job.setdefault("submitted", time.strftime("%Y-%m-%dT%H:%M:%S"))
    _pack_job_config(job)
    dest = os.path.join(INBOX, jid + ".json")
    tmp = dest + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f, indent=2)
        f.write("\n")
    os.replace(tmp, dest)
    return jid, dest


def parse_body(raw, qs):
    data = json.loads(raw.decode("utf-8") or "{}")
    if not isinstance(data, dict):
        raise ValueError("JSON object required")

    qs_assign = (
        (qs.get("assign") or [""])[0]
        or (qs.get("worker") or [""])[0]
        or ""
    ).strip()

    # Full job: {targets, config, version?}
    if isinstance(data.get("config"), dict) and data.get("targets"):
        job = {
            "id": data.get("id") or "",
            "version": data.get("version") or qs.get("version", ["1.4.9"])[0],
            "targets": list(data["targets"]),
            "dry_run": bool(data.get("dry_run", False)),
            "config": data["config"],
        }
        assign = (data.get("assign") or data.get("assign_worker") or qs_assign or "").strip()
        if assign:
            job["assign"] = assign
        return job

    # Bare RustDesk.json — targets must be in the query string
    targets = qs.get("targets") or []
    if len(targets) == 1 and "," in targets[0]:
        targets = [t.strip() for t in targets[0].split(",") if t.strip()]
    if not targets:
        raise ValueError(
            "pass targets: POST /job?targets=windows-x86_64-exe  "
            "or JSON {\"targets\":[...],\"config\":{...}}")
    dry = qs.get("dry_run", ["0"])[0] in ("1", "true", "yes")
    job = {
        "id": "",
        "version": qs.get("version", ["1.4.9"])[0],
        "targets": targets,
        "dry_run": dry,
        "config": data,
    }
    if qs_assign:
        job["assign"] = qs_assign
    return job


def _job_json_names(folder):
    try:
        return sorted(
            n[:-5] for n in os.listdir(folder)
            if n.endswith(".json") and not n.startswith(".")
            and not n.endswith(".progress.json"))
    except OSError:
        return []


def list_jobs():
    def names(folder):
        return _job_json_names(folder)

    out = []
    try:
        for name in sorted(os.listdir(OUTBOX)):
            st = os.path.join(OUTBOX, name, "status.json")
            if os.path.isfile(st):
                with open(st, encoding="utf-8") as f:
                    out.append(json.load(f))
    except OSError:
        pass
    return {
        "inbox": names(INBOX),
        "running": names(RUNNING),
        "failed": names(FAILED),
        "outbox": out,
    }


HINTS = {
    "macos-arm64-dmg": "Usually 6–15 min if this Mac built recently, 20–40 min on a cold start.",
    "macos-x86_64-dmg": "Usually 15–30 min (cross-compile from Apple Silicon is slower).",
    "macos-universal-dmg": "Usually 25–50 min — both CPU slices.",
    "windows-x86_64-exe": "Usually 15–35 min on a warmed Windows PC, 40+ min first time.",
    "windows-x86_64-msi": "Usually 20–45 min (exe plus WiX/MSI).",
    "android-arm64": "Usually 15–30 min on Mac/Linux with NDK already installed.",
    "android-universal": "Usually 40–70 min — every ABI.",
    "linux-x86_64-deb": "Usually 10–25 min on a warmed Linux box.",
    "linux-x86_64-rpm": "Usually 10–25 min (needs rpmbuild).",
    "linux-x86_64-appimage": "Usually 12–30 min.",
}

# Median warm-cache seconds — used only for the progress bar estimate.
TYPICAL_SEC = {
    "macos-arm64-dmg": 12 * 60,
    "macos-x86_64-dmg": 22 * 60,
    "macos-universal-dmg": 35 * 60,
    "windows-x86_64-exe": 22 * 60,
    "windows-x86_64-msi": 30 * 60,
    "android-arm64": 20 * 60,
    "android-universal": 50 * 60,
    "linux-x86_64-deb": 15 * 60,
    "linux-x86_64-rpm": 15 * 60,
    "linux-x86_64-appimage": 18 * 60,
}


def _peek_job(path):
    """Public-safe slice of a job file (no password / keys)."""
    try:
        with open(path, encoding="utf-8") as f:
            job = json.load(f)
    except Exception:
        return {}
    cfg = job.get("config") if isinstance(job.get("config"), dict) else {}
    targets = job.get("targets") or []
    t0 = targets[0] if targets else ""
    hint = HINTS.get(t0, "Several minutes to an hour, depending on the machine.")
    who = "a builder"
    if any(str(t).startswith("macos-") for t in targets):
        who = "a Mac"
    elif any(str(t).startswith("windows-") for t in targets):
        who = "a Windows PC"
    elif any(str(t).startswith("linux-") for t in targets):
        who = "a Linux PC"
    elif any(str(t).startswith("android-") for t in targets):
        who = "a Linux (or Mac) builder"
    assign = _job_assign(job)
    if assign:
        who = assign
    return {
        "targets": targets,
        "version": job.get("version") or "",
        "appname": cfg.get("appname") or cfg.get("exename") or "",
        "hint": hint,
        "typical_sec": TYPICAL_SEC.get(t0, 20 * 60),
        "waiting_for": who,
        "assign": assign,
        "submitted": job.get("submitted") or "",
    }


def _read_progress(jid):
    path = os.path.join(RUNNING, jid + ".progress.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def one_job(jid):
    """Status for a single id. The id is unguessable; used as a capability URL."""
    if not SAFE_ID.match(jid or ""):
        return None
    st_path = os.path.join(OUTBOX, jid, "status.json")
    if os.path.isfile(st_path):
        with open(st_path, encoding="utf-8") as f:
            status = json.load(f)
        files = []
        try:
            for n in sorted(os.listdir(os.path.join(OUTBOX, jid))):
                if n.startswith(".") or n == "status.json":
                    continue
                files.append(n)
        except OSError:
            pass
        return {"id": jid, "state": "done", "status": status, "files": files,
                "targets": status.get("targets") or []}
    run_path = os.path.join(RUNNING, jid + ".json")
    if os.path.isfile(run_path):
        info = {"id": jid, "state": "running"}
        info.update(_peek_job(run_path))
        prog = dict(_read_progress(jid) or {})
        try:
            claimed = int(time.time() - os.path.getmtime(run_path))
        except OSError:
            claimed = 0
        if claimed < 0:
            claimed = 0
        prog["elapsed_sec"] = max(int(prog.get("elapsed_sec") or 0), claimed)
        if not prog.get("worker"):
            prog["worker"] = info.get("waiting_for") or "builder"
        if not prog.get("phase"):
            prog["phase"] = "building"
        info["progress"] = prog
        return info
    if os.path.isfile(os.path.join(INBOX, jid + ".json")):
        info = {"id": jid, "state": "queued"}
        info.update(_peek_job(os.path.join(INBOX, jid + ".json")))
        ahead = 0
        try:
            for n in sorted(os.listdir(INBOX)):
                if not n.endswith(".json") or n.startswith("."):
                    continue
                if n == jid + ".json":
                    break
                ahead += 1
        except OSError:
            pass
        info["queue_ahead"] = ahead
        return info
    fail = os.path.join(FAILED, jid + ".json")
    if os.path.isfile(fail):
        err = ""
        try:
            with open(fail, encoding="utf-8") as f:
                rec = json.load(f)
            err = rec.get("error") or ""
        except Exception:
            err = "failed"
        return {"id": jid, "state": "failed", "error": err}
    return {"id": jid, "state": "unknown"}


def note_worker(os_name, worker_name, android=False, busy=None):
    """Remember a /claim or /progress ping. In-memory; resets if queue.py restarts."""
    name = (worker_name or "").strip()
    if not name:
        return
    with _LOCK:
        rec = dict(WORKERS.get(name) or {})
        rec["name"] = name
        if os_name:
            rec["os"] = os_name
        rec["android"] = bool(android) or bool(rec.get("android"))
        rec["last_seen"] = time.time()
        if busy is not None:
            rec["busy"] = bool(busy)
        claiming = list(CLAIM.get(rec.get("os") or "", ()) or ())
        if rec.get("android") and "android-" not in claiming:
            claiming.append("android-")
        rec["claiming"] = claiming
        WORKERS[name] = rec


def _kinds(targets):
    out = []
    for t in targets or []:
        t = str(t)
        kind = t.split("-", 1)[0] if t else ""
        if kind in ("macos", "windows", "linux", "android") and kind not in out:
            out.append(kind)
    return out


def _safe_job(info, extra=None):
    rec = {
        "appname": info.get("appname") or "",
        "targets": info.get("targets") or [],
        "version": info.get("version") or "",
        "waiting_for": info.get("waiting_for") or "",
        "typical_sec": info.get("typical_sec") or 0,
        "assign": info.get("assign") or "",
    }
    if extra:
        rec.update(extra)
    return rec


def _merge_progress_workers():
    try:
        names = os.listdir(RUNNING)
    except OSError:
        return
    for name in names:
        if not name.endswith(".progress.json"):
            continue
        path = os.path.join(RUNNING, name)
        try:
            mtime = os.path.getmtime(path)
            with open(path, encoding="utf-8") as f:
                prog = json.load(f)
        except Exception:
            continue
        worker = (prog.get("worker") or "").strip()
        if not worker:
            continue
        with _LOCK:
            rec = dict(WORKERS.get(worker) or {})
            rec["name"] = worker
            rec["os"] = prog.get("host") or rec.get("os") or ""
            rec["last_seen"] = max(float(rec.get("last_seen") or 0), mtime)
            rec["busy"] = True
            claiming = list(CLAIM.get(rec.get("os") or "", ()) or ())
            rec["claiming"] = claiming
            rec["android"] = bool(rec.get("android"))
            WORKERS[worker] = rec


def farm_stats():
    """Public snapshot — no job ids, no config, no secrets."""
    _merge_progress_workers()
    now = time.time()
    waiting_files = _job_json_names(INBOX)
    running_files = _job_json_names(RUNNING)
    failed_n = len(_job_json_names(FAILED))
    try:
        out_dirs = [
            n for n in os.listdir(OUTBOX)
            if not n.startswith(".") and os.path.isdir(os.path.join(OUTBOX, n))
        ]
        out_dirs.sort(key=lambda n: os.path.getmtime(os.path.join(OUTBOX, n)), reverse=True)
    except OSError:
        out_dirs = []

    waiting_by = {"macos": 0, "windows": 0, "linux": 0, "android": 0}
    waiting = []
    for name in waiting_files:
        peek = _peek_job(os.path.join(INBOX, name + ".json"))
        for k in _kinds(peek.get("targets")):
            waiting_by[k] = waiting_by.get(k, 0) + 1
        waiting.append(_safe_job(peek, {"submitted": peek.get("submitted") or ""}))

    running = []
    busy_names = set()
    for name in running_files:
        peek = _peek_job(os.path.join(RUNNING, name + ".json"))
        prog = dict(_read_progress(name) or {})
        try:
            claimed = int(now - os.path.getmtime(os.path.join(RUNNING, name + ".json")))
        except OSError:
            claimed = 0
        elapsed = max(int(prog.get("elapsed_sec") or 0), max(claimed, 0))
        worker = prog.get("worker") or peek.get("waiting_for") or ""
        if worker:
            busy_names.add(worker)
        running.append(_safe_job(peek, {
            "worker": worker,
            "phase": prog.get("phase") or "building",
            "elapsed_sec": elapsed,
        }))

    recent = []
    done_ok = 0
    done_fail = 0
    for jid in out_dirs[:20]:
        st_path = os.path.join(OUTBOX, jid, "status.json")
        try:
            with open(st_path, encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            continue
        ok = bool(st.get("ok"))
        if ok:
            done_ok += 1
        else:
            done_fail += 1
        recent.append({
            "ok": ok,
            "targets": st.get("targets") or [],
            "worker": st.get("worker") or "",
            "seconds": st.get("seconds") or 0,
            "finished": st.get("finished") or "",
            "error": (st.get("error") or "")[:160] if not ok else "",
        })

    workers = []
    coverage = {"macos": False, "windows": False, "linux": False, "android": False}
    with _LOCK:
        live = dict(WORKERS)
        rated = list(RATINGS.keys())
    names = set(live.keys()) | set(rated)
    snapshot = []
    for name in names:
        rec = dict(live.get(name) or {})
        rec["name"] = name
        if not rec.get("os") and RATINGS.get(name, {}).get("os"):
            rec["os"] = RATINGS[name]["os"]
        snapshot.append(rec)
    for rec in snapshot:
        last = float(rec.get("last_seen") or 0)
        age = int(now - last) if last else None
        online = age is not None and age <= ONLINE_SEC
        plat = OS_LABEL.get(rec.get("os") or "", "")
        busy = bool(rec.get("busy")) or rec.get("name") in busy_names
        rate = rating_public(rec.get("name") or "")
        item = {
            "name": rec.get("name") or "",
            "os": rec.get("os") or "",
            "platform": plat,
            "claiming": rec.get("claiming") or list(CLAIM.get(rec.get("os") or "", ()) or ()),
            "android": bool(rec.get("android")),
            "last_seen_sec": age,
            "online": online,
            "busy": busy if online else False,
        }
        item.update(rate)
        workers.append(item)
        if online and rate["eligible"] and plat:
            coverage[plat] = True
            if plat == "linux" or item["android"]:
                coverage["android"] = True
    workers.sort(key=lambda w: (
        not w["online"],
        not w["eligible"],
        -float(w.get("score") or 0),
        w.get("name") or "",
    ))

    return {
        "ok": True,
        "now": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "uptime_sec": int(now - STARTED),
        "queue": {
            "waiting": len(waiting_files),
            "running": len(running_files),
            "failed": failed_n,
            "done": len(out_dirs),
        },
        "waiting_by": waiting_by,
        "coverage": coverage,
        "workers_online": sum(1 for w in workers if w["online"]),
        "workers": workers,
        "waiting": waiting,
        "running": running,
        "recent": recent,
        "recent_ok": done_ok,
        "recent_fail": done_fail,
    }


def health_stats():
    s = farm_stats()
    return {
        "ok": True,
        "uptime_sec": s["uptime_sec"],
        "waiting": s["queue"]["waiting"],
        "running": s["queue"]["running"],
        "failed": s["queue"]["failed"],
        "done": s["queue"]["done"],
        "workers_online": s["workers_online"],
        "coverage": s["coverage"],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _auth(self):
        if not TOKEN:
            return True
        got = self.headers.get("Authorization") or ""
        if got == "Bearer " + TOKEN:
            return True
        if (self.headers.get("X-Farm-Token") or "") == TOKEN:
            return True
        qs = parse_qs(urlparse(self.path).query)
        if (qs.get("token") or [""])[0] == TOKEN:
            return True
        self._send(401, {"error": "need Authorization: Bearer <token>"})
        return False

    def _cors(self):
        origin = os.environ.get("DVFORGE_QUEUE_CORS", "*")
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, X-Farm-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send(self, code, obj):
        body = json.dumps(obj, indent=2).encode("utf-8") + b"\n"
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _send_file(self, path, ctype=None):
        with open(path, "rb") as f:
            body = f.read()
        if not ctype:
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition",
                         "attachment; filename=\"%s\"" % os.path.basename(path))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/health", "/healthz"):
            return self._send(200, health_stats())
        if path == "/stats":
            return self._send(200, farm_stats())
        if path in ("/status", "/status.html", "/stats.html"):
            page = os.path.join(PUBLIC, "stats.html")
            if os.path.isfile(page):
                with open(page, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            return self._send(200, farm_stats())
        if path in ("/join", "/join.html"):
            join = os.path.join(PUBLIC, "join.html")
            if os.path.isfile(join):
                with open(join, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            return self._send(404, {"error": "no join page"})
        if path in ("/", "/index.html"):
            index = os.path.join(PUBLIC, "index.html")
            if os.path.isfile(index):
                with open(index, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            return self._send(200, list_jobs())
        if path.startswith("/job/"):
            jid = unquote(path.split("/", 2)[-1])
            info = one_job(jid)
            if info is None:
                return self._send(400, {"error": "bad job id"})
            return self._send(200, info)
        if path.startswith("/dl/"):
            parts = [unquote(p) for p in path.split("/") if p]
            # ['dl', id, filename]
            if len(parts) != 3 or not SAFE_ID.match(parts[1]):
                return self._send(400, {"error": "bad download path"})
            jid, name = parts[1], os.path.basename(parts[2])
            if name in ("", ".", "..", "status.json") or "/" in parts[2] or "\\" in parts[2]:
                return self._send(400, {"error": "bad filename"})
            fp = os.path.join(OUTBOX, jid, name)
            real_out = os.path.realpath(OUTBOX)
            real_fp = os.path.realpath(fp)
            if not real_fp.startswith(real_out + os.sep) or not os.path.isfile(fp):
                return self._send(404, {"error": "file not ready"})
            return self._send_file(fp)
        if path == "/jobs":
            if not self._auth():
                return
            return self._send(200, list_jobs())
        self._send(404, {"error": "not found"})

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_POST(self):
        if not self._auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        if path == "/claim":
            try:
                data = json.loads(self._body().decode("utf-8") or "{}")
            except Exception:
                data = {}
            os_name = data.get("os") or ""
            worker = data.get("worker") or "worker"
            android = bool(data.get("android"))
            job = claim_job(os_name, worker, android)
            note_worker(os_name, worker, android, busy=bool(job))
            if not job:
                return self._send(200, {"ok": True, "job": None})
            return self._send(200, {"ok": True, "job": job})

        if path.startswith("/progress/"):
            jid = unquote(path.split("/", 2)[-1])
            if not SAFE_ID.match(jid or ""):
                return self._send(400, {"error": "bad id"})
            try:
                prog = json.loads(self._body().decode("utf-8") or "{}")
            except Exception as e:
                return self._send(400, {"error": str(e)})
            os.makedirs(RUNNING, exist_ok=True)
            dest = os.path.join(RUNNING, jid + ".progress.json")
            tmp = dest + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(prog, f)
                f.write("\n")
            os.replace(tmp, dest)
            note_worker(prog.get("host") or "", prog.get("worker") or "", busy=True)
            return self._send(200, {"ok": True})

        if path.startswith("/result/"):
            jid = unquote(path.split("/", 2)[-1])
            if not SAFE_ID.match(jid or ""):
                return self._send(400, {"error": "bad id"})
            try:
                status = json.loads(self._body().decode("utf-8") or "{}")
            except Exception as e:
                return self._send(400, {"error": str(e)})
            out = os.path.join(OUTBOX, jid)
            os.makedirs(out, exist_ok=True)
            with open(os.path.join(out, "status.json"), "w", encoding="utf-8") as f:
                json.dump(status, f, indent=2)
                f.write("\n")
            for extra in (
                os.path.join(RUNNING, jid + ".json"),
                os.path.join(RUNNING, jid + ".progress.json"),
            ):
                try:
                    os.remove(extra)
                except OSError:
                    pass
            if not status.get("ok"):
                os.makedirs(FAILED, exist_ok=True)
                with open(os.path.join(FAILED, jid + ".json"), "w", encoding="utf-8") as f:
                    json.dump(status, f, indent=2)
                    f.write("\n")
            note_worker(status.get("host") or "", status.get("worker") or "", busy=False)
            if not status.get("dry_run"):
                record_result(
                    status.get("worker") or "",
                    bool(status.get("ok")),
                    status.get("error") or "",
                    status.get("host") or "",
                )
            return self._send(200, {"ok": True})

        if path.startswith("/artifact/"):
            jid = unquote(path.split("/", 2)[-1])
            name = os.path.basename((qs.get("name") or [""])[0])
            if not SAFE_ID.match(jid or "") or not name or name in (".", "..", "status.json"):
                return self._send(400, {"error": "bad artifact path"})
            out = os.path.join(OUTBOX, jid)
            os.makedirs(out, exist_ok=True)
            dest = os.path.join(out, name)
            raw = self._body()
            with open(dest, "wb") as f:
                f.write(raw)
            return self._send(200, {"ok": True, "bytes": len(raw), "name": name})

        if path in ("/worker/reset", "/rating/reset"):
            try:
                data = json.loads(self._body().decode("utf-8") or "{}")
            except Exception:
                data = {}
            name = (data.get("name") or data.get("worker") or (qs.get("name") or [""])[0] or "").strip()
            try:
                info = reset_rating(name)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"ok": True, "name": name, "rating": info})

        if path != "/job":
            return self._send(404, {"error": "POST /job | /claim | /progress/id | /result/id | /artifact/id | /worker/reset"})
        raw = self._body()
        try:
            job = parse_body(raw, qs)
            jid, dest = write_job(job)
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": str(e)})
        out = {
            "ok": True,
            "id": jid,
            "targets": job["targets"],
            "dry_run": job["dry_run"],
            "inbox": dest,
        }
        if job.get("assign"):
            out["assign"] = job["assign"]
        return self._send(200, out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=os.environ.get("DVFORGE_QUEUE_HOST", "127.0.0.1"),
                   help="127.0.0.1 behind nginx; 0.0.0.0 only on a trusted LAN")
    p.add_argument("--port", type=int, default=int(os.environ.get("DVFORGE_QUEUE_PORT", "8766")))
    p.add_argument("--farm", default=FARM)
    args = p.parse_args()
    global INBOX, OUTBOX, FAILED, RUNNING, RATINGS_FILE
    farm = os.path.abspath(args.farm)
    INBOX = os.path.join(farm, "inbox")
    OUTBOX = os.path.join(farm, "outbox")
    FAILED = os.path.join(farm, "failed")
    RUNNING = os.path.join(farm, "running")
    RATINGS_FILE = os.path.join(farm, "ratings.json")
    load_ratings()
    for folder in (INBOX, OUTBOX, FAILED, RUNNING):
        os.makedirs(folder, exist_ok=True)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print("farm queue  http://%s:%s/job" % (args.host, args.port), flush=True)
    print("stats       http://%s:%s/stats   (JSON)  /status (page)" % (args.host, args.port), flush=True)
    print("inbox       %s" % INBOX, flush=True)
    print("ratings     %s (%s workers)" % (RATINGS_FILE, len(RATINGS)), flush=True)
    print("token       %s" % ("yes" if TOKEN else "none (LAN only!)"), flush=True)
    print("curl example:", flush=True)
    print("  curl -sS -X POST 'http://THIS_PC:%s/job?targets=windows-x86_64-exe' \\" % args.port)
    print("    -H 'Content-Type: application/json' --data-binary @RustDesk.json", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstop")


if __name__ == "__main__":
    main()
