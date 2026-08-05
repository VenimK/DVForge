"""
config_gen.py — turn a RustDesk.json config dict into the CUSTOM_* values and
the base64 `custom_.txt` payload, exactly like scripts/load-config.py did for
GitHub Actions (which emitted them into $GITHUB_ENV).

Keeping this a faithful port matters: the base64 payload is what gets written to
custom_.txt and (on Android) embedded into the native code. See SKILL.md §4.1.
"""

import base64
import json
import os


def _b(val):
    return "true" if val in (True, "on") else "false"


def build_custom_env(cfg: dict) -> dict:
    """Return the full CUSTOM_* mapping (strings), mirroring load-config.py."""
    d = cfg

    appname = d.get("appname", d.get("exename", "RustDesk"))
    filename = d.get("exename", appname)
    compname = d.get("compname", "")
    server = d.get("serverIP", "rs-ny.rustdesk.com")
    key = d.get("key", "OeVuKk5nlHiXp+APNn0Y3pC1Iwpwn44JGqrQCsWqmBw=")
    api = d.get("apiServer", "")
    if not api:
        api = f"https://{server}/"
    url_link = d.get("urlLink", "") or "https://rustdesk.com"
    download_link = d.get("downloadLink", "") or "https://rustdesk.com/download"
    android_app_id = d.get("androidappid", "") or ""
    slogan = d.get("slogan", "") or ""
    icon_file = d.get("iconFile", "") or ""
    logo_file = d.get("logoFile", "") or ""
    theme_color = d.get("themeColor", "") or ""

    env = {
        "CUSTOM_APPNAME": appname,
        "CUSTOM_FILENAME": filename,
        "CUSTOM_COMPNAME": compname,
        "CUSTOM_SERVER": server,
        "CUSTOM_KEY": key,
        "CUSTOM_API_SERVER": api,
        "CUSTOM_URL_LINK": url_link,
        "CUSTOM_DOWNLOAD_LINK": download_link,
        "CUSTOM_ANDROID_APP_ID": android_app_id,
        "CUSTOM_SLOGAN": slogan,
        "CUSTOM_ICON_FILE": icon_file,
        "CUSTOM_LOGO_FILE": logo_file,
        "CUSTOM_THEME_COLOR": theme_color,
        "CUSTOM_DELAY_FIX": _b(d.get("delayFix", False)),
        "CUSTOM_HIDE_CM": _b(d.get("hidecm", False)),
        "CUSTOM_X_OFFLINE": _b(d.get("xOffline", False)),
        "CUSTOM_REMOVE_NEW_VERSION_NOTIF": _b(d.get("removeNewVersionNotif", False)),
    }

    # ---- permissions / custom.txt payload ----
    custom = {}

    direction = d.get("direction", "both")
    if direction.lower() not in ("both",):
        custom["conn-type"] = direction.lower()

    if d.get("installation", "installationY") == "installationN":
        custom["disable-installation"] = "Y"
    if d.get("settings", "settingsY") == "settingsN":
        custom["disable-settings"] = "Y"

    if appname.upper() != "RUSTDESK" and appname:
        custom["app-name"] = appname

    perm_pass = d.get("permanentPassword", "")
    if perm_pass:
        custom["password"] = perm_pass

    custom["enable-lan-discovery"] = "N" if d.get("denyLan", False) else "Y"
    custom["allow-auto-disconnect"] = "Y" if d.get("autoClose", False) else "N"

    hidecm = d.get("hidecm", False)
    ds = {}
    perm_fields = {
        "enable-keyboard": d.get("enableKeyboard", False),
        "enable-clipboard": d.get("enableClipboard", False),
        "enable-file-transfer": d.get("enableFileTransfer", False),
        "enable-audio": d.get("enableAudio", False),
        "enable-tunnel": d.get("enableTCP", False),
        "enable-remote-restart": d.get("enableRemoteRestart", False),
        "enable-record-session": d.get("enableRecording", False),
        "enable-block-input": d.get("enableBlockingInput", False),
        "allow-remote-config-modification": d.get("enableRemoteModi", False),
        "enable-remote-printer": d.get("enablePrinter", False),
        "enable-camera": d.get("enableCamera", False),
        "enable-terminal": d.get("enableTerminal", False),
    }
    for k, v in perm_fields.items():
        ds[k] = "Y" if v in (True, "on") else "N"

    ds["approve-mode"] = d.get("passApproveMode", "password-click")
    ds["verification-method"] = "use-permanent-password" if hidecm else "use-both-passwords"
    ds["allow-hide-cm"] = "Y" if hidecm else "N"
    ds["access-mode"] = d.get("permissionsType", "custom")
    ds["direct-server"] = "Y" if d.get("enableDirectIP", False) else "N"
    ds["allow-remove-wallpaper"] = "Y" if d.get("removeWallpaper", False) else "N"

    custom["default-settings"] = ds
    custom["override-settings"] = {}

    for line in (d.get("defaultManual", "") or "").splitlines():
        line = line.strip()
        if "=" in line:
            k, value = line.split("=", 1)
            custom["default-settings"][k.strip()] = value.strip()

    for line in (d.get("overrideManual", "") or "").splitlines():
        line = line.strip()
        if "=" in line:
            k, value = line.split("=", 1)
            custom["override-settings"][k.strip()] = value.strip()

    custom_json = json.dumps(custom)
    custom_b64 = base64.b64encode(custom_json.encode()).decode()

    env["CUSTOM_TXT"] = custom_json
    env["CUSTOM_B64"] = custom_b64
    return env

def config_status(path: str) -> dict:
    """Report whether the real config exists, or we're on the example fallback.
    Returns {"source": "config"|"example"|"missing", "path": <the path in use>,
             "expected": <where the real config should go>,
             "message": <human guidance, empty when source == "config">}.
    """
    expected = path
    if os.path.isfile(path):
        return {"source": "config", "path": path, "expected": expected,
                "message": ""}
    example = os.path.join(os.path.dirname(path), "RustDesk.example.json")
    if os.path.isfile(example):
        return {"source": "example", "path": example, "expected": expected,
                "message": (
                    "No RustDesk.json found — using RustDesk.example.json for "
                    f"now. To build with your own server/key/password, put your "
                    f"config file at:\n    {expected}\n"
                    "You can generate one at https://rdgen.crayoneater.org/ or "
                    "edit the Config tab and save.")}
    return {"source": "missing", "path": path, "expected": expected,
            "message": (
                "No config file found. Create one at:\n"
                f"    {expected}\n"
                "Generate it at https://rdgen.crayoneater.org/ (download as "
                "RustDesk.json), or fill in the Config tab and click Save.")}


def load_config(path: str) -> dict:
        # Prefer the user's real config; fall back to the shipped example so the
    # app still works out-of-the-box instead of throwing FileNotFoundError.
    # If neither exists, raise a message that says exactly where to put the file.
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    example = os.path.join(os.path.dirname(path), "RustDesk.example.json")
    if os.path.isfile(example):
        with open(example, encoding="utf-8") as f:
            return json.load(f)
    raise FileNotFoundError(
        "No RustDesk config found. Put your config at:\n"
        f"    {path}\n"
        "Generate one at https://rdgen.crayoneater.org/ (download as "
        "RustDesk.json), or use the Config tab in the app and click Save. "
        f"(A template also ships at {example}.)")


def save_config(path: str, cfg: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


if __name__ == "__main__":
    import sys
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else "configs/RustDesk.json")
    env = build_custom_env(cfg)
    for k, v in env.items():
        print(f"{k}={v}")
