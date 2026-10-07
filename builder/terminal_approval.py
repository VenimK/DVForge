"""Interactive sudo fallback; credentials stay in a local terminal."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


def terminal_command(command):
    for name, options in (
        ("gnome-terminal", ["--wait", "--"]),
        ("konsole", ["--nofork", "-e"]),
        ("xfce4-terminal", ["--disable-server", "--execute"]),
        ("xterm", ["-e"]),
        ("x-terminal-emulator", ["-e"]),
    ):
        executable = shutil.which(name)
        if executable:
            return [executable, *options, *command]
    raise RuntimeError("No local terminal application found. Install a terminal emulator "
                       "and run DVForge from the Linux desktop session.")


def install(command, log, timeout=1800):
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("No Linux desktop display available to open the password terminal. "
                           "Start DVForge as ecz in a terminal on the Linux desktop, not via SSH "
                           "or as root; then retry Prepare environment.")
    with tempfile.TemporaryDirectory(prefix="dvforge-approval-") as directory:
        status = Path(directory) / "status.json"
        started = status.with_suffix(".started")
        helper = [sys.executable, str(Path(__file__).resolve()), "--run",
                  str(status), json.dumps(command)]
        launcher = terminal_command(helper)
        log("No Polkit agent: opening a terminal on the Linux build PC. "
            "Enter your sudo password there; DVForge waits for the installer result.")
        # A terminal may detach, so its exit code is not the installer's result.
        process = subprocess.Popen(launcher, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        startup_deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if status.is_file():
                result = json.loads(status.read_text())
                return subprocess.CompletedProcess(command, result["returncode"], "",
                                                   result.get("error", ""))
            if started.is_file():
                try:
                    os.kill(int(started.read_text()), 0)
                except ProcessLookupError:
                    raise RuntimeError("Authentication terminal closed before installation completed. "
                                       "Check package-manager activity before retrying.")
            elif time.monotonic() >= startup_deadline:
                raise RuntimeError("Authentication terminal did not start. Open DVForge from "
                                   "a terminal on the Linux desktop and retry.")
            if process.poll() not in (None, 0):
                raise RuntimeError("Could not open the authentication terminal. "
                                   "Start DVForge from the Linux desktop session and retry.")
            time.sleep(0.25)
        raise RuntimeError("Timed out waiting for the password terminal. "
                           "Check or close the installer terminal before retrying.")


def run(status, command):
    result = {"returncode": 1, "error": "Installer did not complete"}
    try:
        status.with_suffix(".started").write_text(str(os.getpid()))
        if not command or command[0] not in ("/usr/bin/apt-get", "/usr/bin/dnf", "/usr/bin/pacman"):
            raise ValueError("Unsupported package manager")
        print("DVForge: install build dependencies", flush=True)
        code = subprocess.run(["sudo", "--", *command]).returncode
        result = {"returncode": code,
                  "error": "" if code == 0 else f"Terminal installer exited with status {code}; see its output"}
    except (Exception, KeyboardInterrupt) as exc:
        result = {"returncode": 1, "error": str(exc) or "Installation cancelled"}
    finally:
        temporary = status.with_suffix(".tmp")
        temporary.write_text(json.dumps(result))
        temporary.replace(status)


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--run":
        raise SystemExit("Internal DVForge terminal installer")
    run(Path(sys.argv[2]), json.loads(sys.argv[3]))
