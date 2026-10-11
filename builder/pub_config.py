"""Validate pub output without accepting a previous dependency resolution."""

import json
import os
from urllib.parse import unquote, urlparse


def validate(path):
    try:
        with open(path, encoding="utf-8") as stream:
            config = json.load(stream)
        if config.get("configVersion") != 2:
            raise ValueError("expected configVersion 2")
        packages = config.get("packages")
        if not isinstance(packages, list) or not packages:
            raise ValueError("empty or invalid packages list")
        names = set()
        for package in packages:
            name, uri = package["name"], package["rootUri"]
            if not isinstance(name, str) or not name or name in names:
                raise ValueError("invalid or duplicate package name")
            names.add(name)
            parsed = urlparse(uri)
            if parsed.scheme not in ("", "file") or parsed.netloc not in ("", "localhost"):
                raise ValueError(f"non-local package root: {name}")
            root = unquote(parsed.path)
            if os.name == "nt" and parsed.scheme == "file" and root.startswith("/"):
                root = root[1:]
            if not os.path.isabs(root):
                root = os.path.join(os.path.dirname(path), root)
            if not os.path.isdir(root):
                raise ValueError(f"package root is missing: {name}")
        if not {"ffigen", "flutter"}.issubset(names):
            raise ValueError("required flutter/ffigen packages are missing")
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise RuntimeError(f"Invalid Flutter package_config.json: {exc}") from exc
    return config


def resolve(path, run, dry_run=False):
    if dry_run:
        run(["flutter", "pub", "get"], check=True)
        return
    backup = path + ".dvforge-previous"
    if os.path.isfile(path):
        os.replace(path, backup)
    try:
        run(["flutter", "pub", "get"], check=True)
        validate(path)
    except Exception:
        # Preserve the old resolution for recovery, but never accept it as new.
        raise
    else:
        if os.path.isfile(backup):
            os.remove(backup)
