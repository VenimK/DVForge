#!/usr/bin/env python3
"""
Reads configs/RustDesk.json and outputs key=value lines for GITHUB_ENV.
Usage: python3 scripts/load-config.py configs/RustDesk.json >> $GITHUB_ENV
"""
import json, sys, base64, os

with open(sys.argv[1]) as f:
    d = json.load(f)

def b(val):
    return "true" if val in (True, "on") else "false"

# --- Core identity ---
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

print(f"CUSTOM_APPNAME={appname}")
print(f"CUSTOM_FILENAME={filename}")
print(f"CUSTOM_COMPNAME={compname}")
print(f"CUSTOM_SERVER={server}")
print(f"CUSTOM_KEY={key}")
print(f"CUSTOM_API_SERVER={api}")
print(f"CUSTOM_URL_LINK={url_link}")
print(f"CUSTOM_DOWNLOAD_LINK={download_link}")

# --- Flags ---
print(f"CUSTOM_DELAY_FIX={b(d.get('delayFix', False))}")
print(f"CUSTOM_HIDE_CM={b(d.get('hidecm', False))}")
print(f"CUSTOM_X_OFFLINE={b(d.get('xOffline', False))}")
print(f"CUSTOM_REMOVE_NEW_VERSION_NOTIF={b(d.get('removeNewVersionNotif', False))}")

# --- Permissions / custom.txt ---
custom = {}
direction = d.get("direction", "both")
if direction.lower() not in ("both",):
    custom["conn-type"] = direction.lower()

installation = d.get("installation", "installationY")
if installation == "installationN":
    custom["disable-installation"] = "Y"

settings = d.get("settings", "settingsY")
if settings == "settingsN":
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

# Parse manual settings (skip empty lines)
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

# Use heredoc-safe output for multiline-safe value
print(f"CUSTOM_TXT={custom_json}")
print(f"CUSTOM_B64={custom_b64}")
