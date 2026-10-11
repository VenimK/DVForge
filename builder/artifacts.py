"""Output layout and independent artifact metadata; no build side effects."""

import hashlib
import os
import re
import struct
import subprocess


def output_dir(workspace, platform, version, arch):
    if platform not in {"windows", "linux", "macos", "android"}:
        raise ValueError("Unknown artifact platform")
    version = version.lstrip("v")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version):
        raise ValueError("Invalid version component")
    if arch not in {"x86_64", "aarch64", "armv7", "universal"}:
        raise ValueError("Invalid architecture component")
    return os.path.join(os.path.abspath(workspace), "output", platform, version, arch)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def elf_arch(path):
    with open(path, "rb") as stream:
        header = stream.read(20)
    if len(header) < 20 or header[:4] != b"\x7fELF" or header[5] not in (1, 2):
        raise RuntimeError(f"Not a valid ELF binary: {path}")
    machine = struct.unpack("<H" if header[5] == 1 else ">H", header[18:20])[0]
    arch = {62: "x86_64", 183: "aarch64", 40: "armv7"}.get(machine)
    if not arch:
        raise RuntimeError(f"Unsupported ELF machine {machine}: {path}")
    return arch


def pe_arch(path):
    with open(path, "rb") as stream:
        header = stream.read(64)
        if len(header) < 64 or header[:2] != b"MZ":
            raise RuntimeError(f"Not a PE executable: {path}")
        stream.seek(struct.unpack("<I", header[60:64])[0])
        signature = stream.read(6)
    if len(signature) != 6 or signature[:4] != b"PE\x00\x00":
        raise RuntimeError(f"Invalid PE signature: {path}")
    machine = struct.unpack("<H", signature[4:])[0]
    arch = {0x8664: "x86_64", 0xAA64: "aarch64", 0x14C: "i686"}.get(machine)
    if not arch:
        raise RuntimeError(f"Unsupported PE machine {machine}: {path}")
    return arch


def verify_package(path, arch):
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        raise RuntimeError(f"Missing or empty artifact: {path}")
    if path.endswith(".deb"):
        actual = subprocess.check_output(
            ["dpkg-deb", "--field", path, "Architecture"], text=True).strip()
        expected = {"x86_64": "amd64", "aarch64": "arm64"}.get(arch, arch)
    elif path.endswith(".rpm"):
        actual = subprocess.check_output(
            ["rpm", "-qp", "--qf", "%{ARCH}", path], text=True).strip()
        expected = arch
    elif path.endswith(".AppImage"):
        actual, expected = elf_arch(path), arch
    else:
        # APK ABI sets and DMG contents are validated in their platform stage.
        return
    if actual != expected:
        raise RuntimeError(f"Artifact architecture {actual}; expected {expected}: {path}")


def metadata(path):
    if os.path.isdir(path):
        return {"path": path, "kind": "directory"}
    return {"path": path, "kind": "file", "bytes": os.path.getsize(path),
            "sha256": sha256(path)}


def download_path(workspace, requested):
    root = os.path.realpath(os.path.join(workspace, "output"))
    path = os.path.realpath(requested)
    if os.path.commonpath([root, path]) != root or not os.path.isfile(path):
        raise ValueError("Artifact is outside output or missing")
    if not path.lower().endswith((".exe", ".msi", ".apk", ".dmg", ".deb", ".rpm", ".appimage", ".zip")):
        raise ValueError("Not a downloadable build artifact")
    return path
