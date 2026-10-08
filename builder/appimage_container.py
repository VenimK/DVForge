"""Rootless, isolated packaging for legacy RustDesk AppImage recipes."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

IMAGE = "localhost/dvforge-appimage:rustdesk-4ea36c95-v1"
MARKER = "DVFORGE_APPIMAGE_CONTAINER_V1"
DOCKERFILE = """FROM docker.io/library/ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive APPIMAGE_EXTRACT_AND_RUN=1
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv git ca-certificates gnupg libarchive-tools squashfs-tools patchelf desktop-file-utils zsync file binutils fakeroot wget curl && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/appimage && /opt/appimage/bin/pip install 'setuptools<81' 'setuptools_scm<10' wheel && /opt/appimage/bin/pip install 'git+https://github.com/rustdesk-org/appimage-builder.git@4ea36c95c6f3e29ff3c35807c1651ba353302f21'
ENV PATH=/opt/appimage/bin:$PATH
RUN command -v apt-key && appimage-builder --version
ENTRYPOINT ["appimage-builder"]
"""


def is_wrapper(path):
    try:
        return MARKER in Path(path).read_text()
    except (OSError, UnicodeError, TypeError):
        return False


def ready():
    if not shutil.which("podman"):
        return False
    try:
        return subprocess.run(["podman", "image", "exists", IMAGE],
                              capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def install(home, log, cancelled):
    from . import environment
    if not shutil.which("podman"):
        if environment.package_manager() != "apt":
            raise RuntimeError("Automatic AppImage container setup currently requires Debian/Ubuntu")
        command = ([] if os.geteuid() == 0 else ["sudo", "-n"]) + [
            "apt-get", "install", "-y", "podman", "uidmap", "slirp4netns", "fuse-overlayfs"]
        result = environment.run_native_installer(command, log)
        if result.returncode:
            raise RuntimeError("Podman installation failed: " + (result.stderr or ""))
    if os.geteuid() == 0:
        raise RuntimeError("Start DVForge as the desktop user, not root, for rootless AppImage packaging")
    if not ready():
        with tempfile.TemporaryDirectory(prefix="dvforge-appimage-") as directory:
            Path(directory, "Containerfile").write_text(DOCKERFILE)
            log("Preparing AppImage packaging image; existing compiler caches are not touched.")
            process = subprocess.Popen(["podman", "build", "--tag", IMAGE, directory],
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                for line in process.stdout:
                    if cancelled():
                        raise RuntimeError("AppImage toolchain preparation cancelled")
                    log(line.rstrip())
                if process.wait():
                    raise RuntimeError("AppImage packaging image preparation failed; see preceding output")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait()
    if not ready():
        raise RuntimeError("AppImage packaging image unavailable after installation")
    bin_dir = Path(home) / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    runner = bin_dir / "appimage_runner.py"
    shutil.copy2(__file__, runner)
    wrapper = bin_dir / "appimage-builder"
    wrapper.write_text("#!/usr/bin/env python3\n# " + MARKER + "\nimport runpy\nfrom pathlib import Path\nrunpy.run_path(str(Path(__file__).with_name('appimage_runner.py')), run_name='__main__')\n")
    wrapper.chmod(0o755)
    return {"tool": "appimage_builder", "home": str(home),
            "env": {"vars": {}, "path": [str(bin_dir)]}}


def run(arguments):
    # Mount only the RustDesk source required by the official recipe. Container
    # root maps to the invoking user; no daemon or host sudo is used for packaging.
    directory = Path.cwd().resolve()
    source = directory.parent if directory.name == "appimage" else directory
    arguments = list(arguments)
    if "--recipe" in arguments:
        index = arguments.index("--recipe") + 1
        if index >= len(arguments):
            raise RuntimeError("Missing AppImage recipe path")
        recipe = Path(arguments[index]).resolve()
        if not recipe.is_relative_to(source):
            raise RuntimeError("AppImage recipe must be inside the mounted RustDesk source")
        arguments[index] = str(recipe)
    if any(arg.startswith("--recipe") for arg in arguments) and not (source / "appimage").is_dir():
        raise RuntimeError("Run the AppImage recipe from the RustDesk source/appimage directory")
    command = ["podman", "run", "--rm", "--security-opt=no-new-privileges",
               "--volume", f"{source}:{source}:rw", "--workdir", str(directory),
               IMAGE, *arguments]
    return subprocess.call(command)


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
