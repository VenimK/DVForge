#!/usr/bin/env python3
"""DVForge farm worker — YOUR machines only (Mac, Windows, Linux).

Each PC:
  1. Runs DVForge locally  (python3 app.py --no-browser / run.sh / run.bat)
  2. Runs this worker against that localhost API
  3. Shares this farm/ folder (this repo on the NAS)

Mac claims macos-* (add --android to also take APKs).
Windows claims windows-* only.
Linux claims linux-* and android-*.
Jobs the other OS must build stay in inbox/.

  python3 farm/worker.py --once          # process at most one job, then exit
  python3 farm/worker.py                 # loop
  set DVFORGE_URL=http://127.0.0.1:8765
  set DVFORGE_FARM=\\\\NAS\\downloads\\...\\dvforge\\farm
"""
from __future__ import print_function

import argparse
import json
import os
import platform
import shutil
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FARM = HERE
DEFAULT_URL = os.environ.get("DVFORGE_URL", "http://127.0.0.1:8765")

HOST = platform.system()  # Darwin | Windows | Linux
WORKER_NAME = os.environ.get("DVFORGE_WORKER") or platform.node() or HOST

# Target prefix this host will claim.
CLAIM = {
    "Darwin": ("macos-",),
    "Windows": ("windows-",),
    "Linux": ("linux-", "android-"),
}
# Mac can also do Android; opt-in with --android
CLAIM_ANDROID_ON_MAC = False


def log(msg):
    print(time.strftime("%H:%M:%S"), "[%s]" % WORKER_NAME, msg, flush=True)


QUEUE_BASE = None
QUEUE_TOKEN = ""


def _qheaders(extra=None):
    h = dict(extra or {})
    if QUEUE_TOKEN:
        h["Authorization"] = "Bearer " + QUEUE_TOKEN
    return h


def http_json(url, data=None, method=None, timeout=30, headers=None):
    body = None
    hdrs = dict(headers or {})
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8") or "{}"
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            raise RuntimeError("HTTP %s %s: %s" % (e.code, url, raw[:200].replace("\n", " ")))
        err = parsed.get("error") if isinstance(parsed, dict) else None
        if e.code >= 400:
            raise RuntimeError("HTTP %s %s: %s" % (e.code, url, err or raw[:200]))
        return parsed
    except urllib.error.URLError as e:
        raise RuntimeError("unreachable %s: %s" % (url, e.reason))


def claim_http():
    r = http_json(QUEUE_BASE + "/claim", data={
        "os": HOST,
        "worker": WORKER_NAME,
        "android": CLAIM_ANDROID_ON_MAC,
    }, headers=_qheaders(), timeout=60)
    job = (r or {}).get("job")
    if not job:
        return None
    job["_http"] = True
    return job


def prefixes():
    p = list(CLAIM.get(HOST, ()))
    if HOST == "Darwin" and CLAIM_ANDROID_ON_MAC:
        p.append("android-")
    return tuple(p)


def can_claim(job):
    targets = job.get("targets") or []
    if not targets:
        return False
    pref = prefixes()
    return all(any(t.startswith(x) for x in pref) for t in targets)


def dirs(farm):
    return {
        "inbox": os.path.join(farm, "inbox"),
        "running": os.path.join(farm, "running"),
        "outbox": os.path.join(farm, "outbox"),
        "failed": os.path.join(farm, "failed"),
    }


def claim_one(d):
    """Rename inbox/*.json -> running/. First success wins (Mac vs Windows)."""
    try:
        names = sorted(os.listdir(d["inbox"]))
    except OSError:
        return None
    for name in names:
        if not name.endswith(".json") or name.startswith("."):
            continue
        src = os.path.join(d["inbox"], name)
        try:
            with open(src, encoding="utf-8") as f:
                job = json.load(f)
        except Exception as e:
            log("skip unreadable %s (%s)" % (name, e))
            continue
        if not can_claim(job):
            continue
        want = (job.get("assign") or job.get("assign_worker") or "").strip()
        if want and want != WORKER_NAME:
            continue
        dest = os.path.join(d["running"], name)
        try:
            os.rename(src, dest)
        except OSError:
            continue  # other worker got it
        job["_path"] = dest
        job["_file"] = name
        return job
    return None


def wait_build(url, timeout_sec, on_tick=None):
    deadline = time.time() + timeout_sec
    t0 = time.time()
    while time.time() < deadline:
        st = http_json(url + "/api/build/status")
        if not st.get("running"):
            return st.get("result") or {}
        if on_tick:
            try:
                on_tick(time.time() - t0, st)
            except Exception:
                pass
        time.sleep(5)
    return {"ok": False, "error": "timeout waiting for build"}


def write_progress(job, d, **extra):
    jid = job.get("id") or os.path.splitext(job.get("_file") or "job")[0]
    rec = {
        "id": jid,
        "worker": WORKER_NAME,
        "host": HOST,
        "targets": job.get("targets") or [],
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    rec.update(extra)
    path = os.path.join(d["running"], jid + ".progress.json")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f)
            f.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass
    if QUEUE_BASE and rec.get("id"):
        try:
            http_json(QUEUE_BASE + "/progress/" + rec["id"], data=rec,
                      headers=_qheaders(), timeout=30)
        except Exception:
            pass


def clear_progress(job, d):
    jid = job.get("id") or os.path.splitext(job.get("_file") or "job")[0]
    path = os.path.join(d["running"], jid + ".progress.json")
    try:
        os.remove(path)
    except OSError:
        pass


def wait_idle(url, timeout_sec=4 * 3600):
    """DVForge runs one build at a time. Wait out a UI/other job first."""
    deadline = time.time() + timeout_sec
    announced = False
    while time.time() < deadline:
        st = http_json(url + "/api/build/status")
        if not st.get("running"):
            return
        if not announced:
            log("DVForge is already building — waiting for it to finish")
            announced = True
        time.sleep(5)
    raise RuntimeError("timeout waiting for the current DVForge build to finish")


def start_build(url, job, timeout_sec=4 * 3600):
    payload = {
        "version": job.get("version") or "1.4.9",
        "targets": job.get("targets"),
        "dry_run": bool(job.get("dry_run")),
    }
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        start = http_json(url + "/api/build/start", data=payload)
        if start.get("ok"):
            return start
        msg = str(start.get("message") or start)
        if "already running" in msg.lower():
            log("build/start busy, retrying…")
            wait_idle(url, max(30, deadline - time.time()))
            continue
        raise RuntimeError("build/start: %s" % msg)
    raise RuntimeError("build/start: still busy after waiting")


def publish(job, result, d):
    jid = job.get("id") or os.path.splitext(job["_file"])[0]
    out = os.path.join(d["outbox"], jid)
    os.makedirs(out, exist_ok=True)
    copied = []
    for art in result.get("artifacts") or []:
        if not art or not os.path.isfile(art):
            continue
        dest = os.path.join(out, os.path.basename(art))
        shutil.copy2(art, dest)
        copied.append(dest)
    status = {
        "id": jid,
        "ok": bool(result.get("ok")),
        "worker": WORKER_NAME,
        "host": HOST,
        "targets": job.get("targets"),
        "dry_run": bool(job.get("dry_run")),
        "seconds": result.get("seconds"),
        "artifacts": copied,
        "error": result.get("error") or result.get("message") or "",
        "finished": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(os.path.join(out, "status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2)
        f.write("\n")
    if QUEUE_BASE:
        for path in copied:
            name = os.path.basename(path)
            try:
                with open(path, "rb") as f:
                    raw = f.read()
                url = QUEUE_BASE + "/artifact/" + jid + "?name=" + name
                req = urllib.request.Request(
                    url, data=raw, method="POST",
                    headers=_qheaders({"Content-Type": "application/octet-stream"}))
                urllib.request.urlopen(req, timeout=600).read()
                log("uploaded %s (%s bytes)" % (name, len(raw)))
            except Exception as e:
                log("upload failed %s: %s" % (name, e))
        pub = dict(status)
        pub["artifacts"] = [os.path.basename(p) for p in copied]
        try:
            http_json(QUEUE_BASE + "/result/" + jid, data=pub,
                      headers=_qheaders(), timeout=60)
        except Exception as e:
            log("result post failed: %s" % e)
    return status


def fail_job(job, d, err):
    jid = job.get("id") or os.path.splitext(job.get("_file") or "job")[0]
    os.makedirs(d["failed"], exist_ok=True)
    rec = {
        "id": jid,
        "ok": False,
        "worker": WORKER_NAME,
        "error": str(err),
        "targets": job.get("targets"),
        "finished": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path = os.path.join(d["failed"], jid + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=2)
        f.write("\n")
    src = job.get("_path")
    if src and os.path.isfile(src):
        try:
            os.replace(src, os.path.join(d["failed"], job["_file"]))
        except OSError:
            pass
    clear_progress(job, d)
    log("FAILED %s: %s" % (jid, err))
    if QUEUE_BASE:
        try:
            http_json(QUEUE_BASE + "/result/" + jid, data=rec,
                      headers=_qheaders(), timeout=30)
        except Exception:
            pass


def run_job(job, url, d):
    jid = job.get("id") or job.get("_file")
    log("claimed %s targets=%s dry_run=%s" % (
        jid, job.get("targets"), job.get("dry_run")))
    ping = http_json(url + "/api/host")
    log("DVForge host %s %s" % (ping.get("os"), ping.get("hostname")))

    prev = None
    try:
        prev = http_json(url + "/api/config")
    except Exception:
        prev = None

    cfg = job.get("config")
    if not isinstance(cfg, dict):
        raise RuntimeError("job has no config object")

    # One compile at a time. Don't overwrite config while another build runs.
    timeout = 600 if job.get("dry_run") else 4 * 3600
    write_progress(job, d, phase="waiting", elapsed_sec=0,
                   note="Waiting for DVForge to become idle")
    wait_idle(url, timeout)

    saved = http_json(url + "/api/config", data=cfg)
    if not saved.get("ok"):
        raise RuntimeError("could not save config: %s" % saved)

    start_build(url, job, timeout)
    t0 = time.time()
    write_progress(job, d, phase="building", elapsed_sec=0, log_tail=[])

    def _tick(elapsed, st):
        write_progress(
            job, d,
            phase="building",
            elapsed_sec=int(elapsed),
            log_tail=st.get("log_tail") or [],
        )

    result = wait_build(url, timeout, on_tick=_tick)
    clear_progress(job, d)
    if not result.get("ok"):
        raise RuntimeError("build failed: %s" % result)

    status = publish(job, result, d)
    src = job.get("_path")
    if src and os.path.isfile(src):
        try:
            os.remove(src)
        except OSError:
            pass
    log("done %s ok=%s files=%s" % (
        jid, status["ok"], len(status["artifacts"])))

    if prev and isinstance(prev, dict) and "appname" in prev:
        try:
            http_json(url + "/api/config", data=prev)
        except Exception:
            pass
    return status


def loop(farm, url, once):
    d = dirs(farm)
    for k in d:
        os.makedirs(d[k], exist_ok=True)
    log("farm=%s" % (QUEUE_BASE or farm))
    log("api=%s  claiming %s" % (url, ", ".join(prefixes()) + "*"))
    idle = 0
    while True:
        try:
            job = claim_http() if QUEUE_BASE else claim_one(d)
        except Exception as e:
            idle += 1
            if idle == 1 or idle % 12 == 0:
                log("queue not ready (%s) — is queue.py running behind nginx?" % e)
            if once:
                raise
            time.sleep(5)
            continue
        if job:
            idle = 0
            try:
                run_job(job, url, d)
            except Exception as e:
                fail_job(job, d, e)
            if once:
                return
            continue
        idle += 1
        if idle == 1 or idle % 12 == 0:
            log("waiting for inbox jobs…")
        if once:
            log("no matching job")
            return
        time.sleep(5)


def main():
    global CLAIM_ANDROID_ON_MAC, QUEUE_BASE, QUEUE_TOKEN
    p = argparse.ArgumentParser()
    p.add_argument("--farm", default=os.environ.get("DVFORGE_FARM", DEFAULT_FARM))
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--queue", default=os.environ.get("DVFORGE_QUEUE", ""),
                   help="https://api.nas86.eu — no NAS mount needed")
    p.add_argument("--token", default=os.environ.get("DVFORGE_FARM_TOKEN", ""),
                   help="same token as the website / queue")
    p.add_argument("--once", action="store_true")
    p.add_argument("--android", action="store_true",
                   help="On macOS, also claim android-* jobs")
    args = p.parse_args()
    CLAIM_ANDROID_ON_MAC = bool(args.android)
    QUEUE_TOKEN = (args.token or "").strip()
    QUEUE_BASE = (args.queue or "").strip().rstrip("/") or None
    if QUEUE_BASE and ("%" in QUEUE_BASE or QUEUE_BASE.startswith("$")):
        sys.exit(
            "queue URL was not expanded: %s\n"
            "PowerShell does not honor set / %%VAR%%. Run:\n"
            "  python worker.py --queue https://api.nas86.eu --token YOUR_TOKEN"
            % QUEUE_BASE)
    farm = os.path.abspath(args.farm)
    try:
        http_json(args.url + "/api/host")
    except Exception as e:
        sys.exit("DVForge not reachable at %s\n"
                 "Start it first: python3 app.py --no-browser   (or run.sh / run.bat)\n%s"
                 % (args.url, e))
    if QUEUE_BASE:
        log("queue=%s (HTTP, no shared folder required)" % QUEUE_BASE)
    loop(farm, args.url.rstrip("/"), args.once)


if __name__ == "__main__":
    main()
