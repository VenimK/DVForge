#!/usr/bin/env python3
"""
RustDesk Local Builder — backend server.

A zero-dependency (Python standard library only) local web app. Run:

    python3 app.py

then open http://127.0.0.1:8765 in your browser. It detects your hardware and
OS, shows which RustDesk targets this machine can build, lets you edit the
baked-in config, and runs the build locally with a live console — the same
customizations the GitHub Actions builder applies, minus GitHub.
"""

import base64
import json
import os
import platform
import queue
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from builder import detect, prereqs, config_gen, orchestrator, toolchains  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, "web")
CONFIG_PATH = os.path.join(ROOT, "configs", "RustDesk.json")
WORKSPACE = os.path.join(ROOT, "workspace")
BRANDING_DIR = os.path.join(WORKSPACE, "branding")

# apply any locally-installed toolchains (.toolchains/env.json) so detection
# below sees them — must run before the first prereqs scan.
toolchains.apply_persisted_env(ROOT)

HOST = "127.0.0.1"
PORT = int(os.environ.get("RDLB_PORT", "8765"))

MIME = {".html": "text/html", ".css": "text/css", ".js": "application/javascript",
        ".json": "application/json", ".svg": "image/svg+xml", ".ico": "image/x-icon"}


# ---------------------------------------------------------------------------
# a single active build + its log stream
# ---------------------------------------------------------------------------

class BuildSession:
    """Holds one running build, its log queue, and subscriber fan-out."""
    def __init__(self):
        self.build = None
        self.thread = None
        self.subscribers = []          # list of Queue
        self.lock = threading.Lock()
        self.history = []              # replay buffer for late subscribers
        self.running = False
        self.result = None

    def _emit(self, line):
        with self.lock:
            self.history.append(line)
            for q in list(self.subscribers):
                q.put(line)

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            for line in self.history:      # replay so a refresh doesn't lose logs
                q.put(line)
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def start(self, version, target_ids, config, dry_run=False):
        if self.running:
            return False, "a build is already running"
        with self.lock:
            self.history = []
            self.result = None
        self.build = orchestrator.Build(
            version, target_ids, config, WORKSPACE,
            log=self._emit, dry_run=dry_run,
        )
        self.running = True

        def _run():
            try:
                self.result = self.build.execute()
            finally:
                self.running = False
                self._emit("\x00DONE")     # sentinel to close SSE cleanly
        self.thread = threading.Thread(target=_run, daemon=True)
        self.thread.start()
        return True, "started"

    def cancel(self):
        if self.build and self.running:
            self.build.cancel()
            return True
        return False


class InstallSession:
    """Runs a toolchain install in the background with the same SSE fan-out."""
    def __init__(self):
        self.subscribers = []
        self.lock = threading.Lock()
        self.history = []
        self.running = False
        self.result = None
        self._cancel = False

    def _emit(self, line):
        with self.lock:
            self.history.append(line)
            for q in list(self.subscribers):
                q.put(line)

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            for line in self.history:
                q.put(line)
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def cancel(self):
        self._cancel = True
        return self.running

    def start(self, ids):
        if self.running:
            return False, "an install is already running"
        with self.lock:
            self.history = []
            self.result = None
        self._cancel = False
        self.running = True

        def _run():
            try:
                self.result = toolchains.install_many(
                    ids, ROOT, self._emit, cancelled=lambda: self._cancel)
                # refresh env for this process so a follow-up scan sees new tools
                toolchains.apply_persisted_env(ROOT)
            except Exception as e:  # noqa: BLE001
                self._emit(f"install error: {e}")
                self.result = {"installed": [], "errors": [["*", str(e)]]}
            finally:
                self.running = False
                self._emit("\x00DONE")
        threading.Thread(target=_run, daemon=True).start()
        return True, "started"


SESSION = BuildSession()
INSTALL = InstallSession()


# ---------------------------------------------------------------------------
# request handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "RDLocalBuilder/1.0"

    def log_message(self, *args):        # keep the console clean
        pass

    # ---- helpers ----
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode() or "{}")

    # ---- GET ----
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/host":
            return self._send_json(detect.host_info())
        if path == "/api/prereqs":
            return self._send_json(prereqs.summary())
        if path == "/api/matrix":
            host = detect.host_info()
            pr = {p["id"]: p for p in prereqs.summary()}
            return self._send_json({
                "host": host,
                "targets": detect.build_matrix(host, pr),
            })
        if path == "/api/config":
            try:
                return self._send_json(config_gen.load_config(CONFIG_PATH))
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)
        if path == "/api/config/status":
            # Tells the UI whether a real RustDesk.json exists, or we're running
            # on the example fallback, plus exactly where to place the file.
            return self._send_json(config_gen.config_status(CONFIG_PATH))        
        if path == "/api/build/stream":
            return self._stream(SESSION)
        if path == "/api/build/status":
            return self._send_json({
                "running": SESSION.running,
                "result": SESSION.result,
            })
        if path == "/api/toolchains":
            host = detect.host_info()
            inst = toolchains.installable(host["os"], detect.normalize_arch(host["arch"]))
            pr = {p["id"]: p for p in prereqs.summary()}
            local = toolchains.installed_info(ROOT)
            rows = []
            for tid, info in inst.items():
                sat = toolchains.SATISFIES.get(tid, tid)
                st = pr.get(sat, {})
                hint = toolchains.SIZE_HINTS.get(tid, {})
                rows.append({"id": tid, "label": info["label"],
                             "installable": info["ok"], "reason": info["reason"],
                             "satisfies": sat,
                             "present": st.get("present", False),
                             "version": st.get("version", ""),
                             "target_version": hint.get("version", ""),
                             "size_download": hint.get("download", ""),
                             "size_disk": hint.get("disk", ""),
                             "local": local.get(tid, {}).get("local", False),
                             "local_size": local.get(tid, {}).get("size", "")})
            return self._send_json({"tools": rows,
                                    "dir": toolchains.tools_dir(ROOT),
                                    "local_total": local.get("_total", {}).get("size", "")})
        if path == "/api/toolchains/stream":
            return self._stream(INSTALL)
        if path == "/api/toolchains/status":
            return self._send_json({"running": INSTALL.running,
                                    "result": INSTALL.result})
        if path.startswith("/api/branding/"):
            return self._serve_branding(path)
        return self._serve_static(path)

    # ---- POST ----
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self._read_json()
        except Exception as e:
            return self._send_json({"error": f"bad json: {e}"}, 400)

        if path == "/api/config":
            try:
                config_gen.save_config(CONFIG_PATH, data)
                return self._send_json({"ok": True})
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)

        if path == "/api/preview":
            # returns the CUSTOM_* env + decoded custom_.txt for the given config
            cfg = data or config_gen.load_config(CONFIG_PATH)
            env = config_gen.build_custom_env(cfg)
            return self._send_json({
                "env": {k: v for k, v in env.items() if k != "CUSTOM_TXT"},
                "custom_txt": env["CUSTOM_TXT"],
                "custom_b64": env["CUSTOM_B64"],
            })

        if path == "/api/build/preflight":
            targets = data.get("targets", [])
            pr = {p["id"]: p for p in prereqs.summary()}
            ok, problems = orchestrator.preflight(targets, pr)
            b = orchestrator.Build(data.get("version", "1.4.9"), targets,
                                   config_gen.load_config(CONFIG_PATH), WORKSPACE,
                                   dry_run=True)
            return self._send_json({"ok": ok, "problems": problems, "plan": b.plan()})

        if path == "/api/build/start":
            targets = data.get("targets", [])
            if not targets:
                return self._send_json({"error": "no targets selected"}, 400)
            version = data.get("version") or "latest"
            dry = bool(data.get("dry_run", False))
            cfg = config_gen.load_config(CONFIG_PATH)
            ok, msg = SESSION.start(version, targets, cfg, dry_run=dry)
            return self._send_json({"ok": ok, "message": msg}, 200 if ok else 409)

        if path == "/api/build/cancel":
            return self._send_json({"ok": SESSION.cancel()})
        
        if path == "/api/open-folder":
            # Reveal a build-output folder in the OS file manager. Accepts an
            # explicit {"path": ...} (must live under the workspace) or nothing,
            # in which case it opens the newest output/ folder (or output/ root).
            target = data.get("path", "") if isinstance(data, dict) else ""
            out_root = os.path.join(WORKSPACE, "output")
            if target:
                # Only allow paths inside the workspace — no arbitrary browsing.
                ap = os.path.abspath(target)
                if os.path.commonpath([ap, os.path.abspath(WORKSPACE)]) != \
                        os.path.abspath(WORKSPACE):
                    return self._send_json(
                        {"error": "path is outside the workspace"}, 400)
                target = ap
            else:
                # newest version dir under output/, else output/ itself
                target = out_root
                try:
                    subs = [os.path.join(out_root, d) for d in os.listdir(out_root)
                            if os.path.isdir(os.path.join(out_root, d))]
                    if subs:
                        target = max(subs, key=os.path.getmtime)
                except OSError:
                    pass
            if not os.path.isdir(target):
                os.makedirs(target, exist_ok=True)
            try:
                if sys.platform == "win32":
                    os.startfile(target)  # noqa: S606 (intended)
                elif sys.platform == "darwin":
                    import subprocess
                    subprocess.Popen(["open", target])
                else:
                    import subprocess
                    subprocess.Popen(["xdg-open", target])
                return self._send_json({"ok": True, "path": target})
            except Exception as e:
                return self._send_json({"error": str(e), "path": target}, 500)

        if path == "/api/toolchains/install":
            ids = data.get("ids", [])
            if not ids:
                return self._send_json({"error": "no tools selected"}, 400)
            ok, msg = INSTALL.start(ids)
            return self._send_json({"ok": ok, "message": msg}, 200 if ok else 409)

        if path == "/api/toolchains/cancel":
            return self._send_json({"ok": INSTALL.cancel()})

        if path == "/api/toolchains/remove":
            tid = data.get("id")
            if not tid:
                return self._send_json({"error": "no tool id"}, 400)
            try:
                res = toolchains.remove_tool(tid, ROOT)
                toolchains.apply_persisted_env(ROOT)
                return self._send_json({"ok": True, **res})
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)

        if path == "/api/upload":
            file_type = data.get("type", "")  # "icon" or "logo"
            file_data = data.get("data", "")  # base64-encoded file content
            filename = data.get("filename", f"{file_type}.png")
            if not file_type or not file_data:
                return self._send_json({"error": "missing type or data"}, 400)
            os.makedirs(BRANDING_DIR, exist_ok=True)
            # sanitize filename — only keep the extension
            ext = os.path.splitext(filename)[1].lower() or ".png"
            safe_name = f"{file_type}{ext}"
            dst = os.path.join(BRANDING_DIR, safe_name)
            try:
                with open(dst, "wb") as f:
                    f.write(base64.b64decode(file_data))
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)
            return self._send_json({"ok": True, "path": dst,
                                    "filename": safe_name})

        return self._send_json({"error": "not found"}, 404)

    # ---- SSE stream (shared by build + install) ----
    def _stream(self, session):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = session.subscribe()
        try:
            while True:
                try:
                    line = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                if line == "\x00DONE":
                    self.wfile.write(b"event: done\ndata: {}\n\n")
                    self.wfile.flush()
                    break
                payload = json.dumps({"line": line})
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # client closed the SSE stream (browser navigated / EventSource closed) —
            # normal, not an error. On Windows this surfaces as WinError 10053.
            pass
        finally:
            session.unsubscribe(q)

    # ---- branding files (uploaded icons/logos) ----
    def _serve_branding(self, path):
        # path is like /api/branding/icon.png
        name = os.path.basename(path)
        full = os.path.join(BRANDING_DIR, name)
        if not os.path.isfile(full):
            self.send_error(404)
            return
        ext = os.path.splitext(full)[1].lower()
        ctype = {".png": "image/png", ".svg": "image/svg+xml",
                 ".ico": "image/x-icon", ".jpg": "image/jpeg",
                 ".jpeg": "image/jpeg"}.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # ---- static files ----
    def _serve_static(self, path):
        if path == "/":
            path = "/index.html"
        # prevent path traversal
        safe = os.path.normpath(path).lstrip("/\\")
        full = os.path.join(WEB_DIR, safe)
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            self.send_error(404)
            return
        ext = os.path.splitext(full)[1]
        ctype = MIME.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    if sys.version_info < (3, 8):
        print("RustDesk Local Builder needs Python 3.8 or newer.")
        print(f"You're running {platform.python_version()} at {sys.executable}.")
        print("Install a recent Python 3 from https://www.python.org/downloads/ "
              "and run again.")
        sys.exit(1)
    os.makedirs(WORKSPACE, exist_ok=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print("=" * 60)
    print("  RustDesk Local Builder")
    print("=" * 60)
    print(f"  Serving at {url}")
    print(f"  Workspace: {WORKSPACE}")
    print("  Press Ctrl+C to stop.")
    print("=" * 60)
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
