"""
customize.py — apply every source customization the GitHub Actions workflows do,
but in Python, against a checked-out RustDesk source tree.

This is a faithful port of the "Apply customizations" steps in:
  build-windows.yml · build-linux.yml · build-android.yml

Two categories (SKILL.md §3):
  (A) compiled into the binary  — server/key/api/appname/company patched in source
  (B) read at runtime           — the base64 custom_.txt written next to the binary,
                                   plus (Android only) embedded into native code.

Everything routes through `apply(...)`, which takes the source dir, the target
platform, the CUSTOM_* env dict, and a `log` callback for streaming progress.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile


# ---------------------------------------------------------------------------
# small helpers (sed / patch equivalents)
# ---------------------------------------------------------------------------

def _read(path):
    with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
        return f.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(text)


def sed(src_dir, rel, old, new, log=None, required=False, count=0):
    """Literal string replace in a file (like `sed -i 's|old|new|'`)."""
    path = os.path.join(src_dir, rel)
    if not os.path.exists(path):
        if required and log:
            log(f"    ! missing (skipped): {rel}")
        return False
    text = _read(path)
    if old not in text:
        return False
    text = text.replace(old, new, count if count else -1)
    _write(path, text)
    if log:
        log(f"    · {rel}: {_short(old)} -> {_short(new)}")
    return True


def sed_regex(src_dir, rel, pattern, repl, log=None, flags=0):
    path = os.path.join(src_dir, rel)
    if not os.path.exists(path):
        return False
    text = _read(path)
    new_text, n = re.subn(pattern, repl, text, flags=flags)
    if n:
        _write(path, new_text)
        if log:
            log(f"    · {rel}: {n}× /{_short(pattern)}/")
    return bool(n)


def find_files(src_dir, subdir, suffix):
    root = os.path.join(src_dir, subdir)
    hits = []
    for dp, _, files in os.walk(root):
        for fn in files:
            if fn.endswith(suffix):
                hits.append(os.path.join(dp, fn))
    return hits


def git_apply(src_dir, patch_path, log=None):
    try:
        subprocess.run(["git", "apply", patch_path], cwd=src_dir,
                       check=True, capture_output=True, text=True)
        if log:
            log(f"    · applied patch {os.path.basename(patch_path)}")
        return True
    except subprocess.CalledProcessError as e:
        if log:
            log(f"    ! patch skipped ({os.path.basename(patch_path)}): "
                f"{(e.stderr or '').strip()[:120]}")
        return False


def _short(s, n=42):
    s = str(s).replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


# ---------------------------------------------------------------------------
# shared category-A customizations (server/key/api/appname/company/urls/flags)
# ---------------------------------------------------------------------------

def _apply_server_key_api(src, env, log):
    log("  Server + key + API")
    sed(src, "libs/hbb_common/src/config.rs", "rs-ny.rustdesk.com", env["CUSTOM_SERVER"], log)
    sed(src, "libs/hbb_common/src/config.rs",
        "OeVuKk5nlHiXp+APNn0Y3pC1Iwpwn44JGqrQCsWqmBw=", env["CUSTOM_KEY"], log)
    sed(src, "src/common.rs", "https://admin.rustdesk.com", env["CUSTOM_API_SERVER"], log)


def _apply_allow_custom(src, patches_dir, log):
    """Strip the signature check on custom_.txt and rename custom.txt->custom_.txt."""
    log("  Allow custom_.txt (strip signature check)")
    # reuse the upstream allowCustom.py by running it in the source dir
    script = os.path.join(os.path.abspath(patches_dir), "allowCustom.py")
    common = os.path.join(src, "src", "common.rs")
    if os.path.exists(script) and os.path.exists(common):
        try:
            subprocess.run([sys.executable, script], cwd=src, check=True,
                           capture_output=True, text=True)
            log("    · allowCustom.py applied")
        except subprocess.CalledProcessError as e:
            log(f"    ! allowCustom.py failed: {(e.stderr or '').strip()[:120]}")
    else:
        # fallback: inline sed like the Android job does
        _strip_signature_inline(src, log)


def _strip_signature_inline(src, log):
    """Android-style inline strip: remove const KEY block and verify block."""
    path = os.path.join(src, "src", "common.rs")
    if not os.path.exists(path):
        return
    text = _read(path)
    # remove `const KEY: &str = ...;` through matching `};` (the get_rs_pk block)
    text = re.sub(r"const KEY:.*?\n(?:.*?\n)*?\s*};\n", "", text, count=1)
    # remove the `if let Ok(data) = sign::verify(&data, &pk)` block up to `};`
    text = re.sub(r"let Ok\(data\) = sign::verify\(&data, &pk\).*?\n(?:.*?\n)*?\s*};\n",
                  "", text, count=1)
    text = text.replace("custom.txt", "custom_.txt")
    _write(path, text)
    log("    · signature check stripped (inline)")


def _sanitize_bundle_id(app):
    """Sanitize an app name into a valid macOS bundle ID component.

    Reverse-DNS bundle IDs must be alphanumeric with dots and hyphens only.
    Spaces become hyphens, everything else is stripped, lowercased.
    """
    sanitized = app.lower().replace(" ", "-")
    return re.sub(r"[^a-z0-9.-]", "", sanitized)


def _apply_appname(src, env, platform, log):
    app = env["CUSTOM_APPNAME"]
    if app.lower() == "rustdesk":
        return
    log(f"  App name -> {app}")
    for rs in find_files(src, "src/lang", ".rs"):
        rel = os.path.relpath(rs, src)
        sed_regex(src, rel, r"RustDesk", app, log=log, flags=re.IGNORECASE)
    # Slogan_tip doesn't contain "RustDesk" — patch it in every lang file
    slogan = env.get("CUSTOM_SLOGAN", "") or f"Powered by {app}"
    for rs in find_files(src, "src/lang", ".rs"):
        rel = os.path.relpath(rs, src)
        sed_regex(src, rel,
                  r'("Slogan_tip",\s*")[^"]*(")',
                  rf'\g<1>{slogan}\g<2>', log)
    # "About" menu value (e.g. "Over" in Dutch) has no "RustDesk" to replace,
    # so append the app name: "Over" -> "Over {app}"
    for rs in find_files(src, "src/lang", ".rs"):
        rel = os.path.relpath(rs, src)
        sed_regex(src, rel,
                  r'("Over",\s*")([^"]+)(")',
                  rf'\g<1>\g<2> {app}\g<3>', log)
    if platform == "windows":
        sed(src, "Cargo.toml", 'description = "RustDesk Remote Desktop"',
            f'description = "{app}"', log)
        sed(src, "flutter/windows/runner/Runner.rc", '"RustDesk Remote Desktop"',
            f'"{app}"', log)
        sed(src, "flutter/windows/runner/Runner.rc",
            'VALUE "InternalName", "rustdesk"',
            f'VALUE "InternalName", "{app}"', log)
        # Patch CMakeLists.txt so the output .exe uses the custom filename
        filename = env.get("CUSTOM_FILENAME", "") or app
        if filename and filename != "RustDesk" and filename != "rustdesk":
            cmake = "flutter/windows/runner/CMakeLists.txt"
            sed(src, cmake, "rustdesk", filename, log)
    if platform == "android":
        sed(src, "Cargo.toml", 'description = "RustDesk Remote Desktop"', f'description = "{app}"', log)
        sed(src, "Cargo.toml", 'name = "RustDesk"', f'name = "{app}"', log)
        sed(src, "flutter/android/app/src/main/res/values/strings.xml", "RustDesk", app, log)
        sed(src, "flutter/lib/main.dart", "title: 'RustDesk'", f"title: '{app}'", log)
        amanifest = "flutter/android/app/src/main/AndroidManifest.xml"
        sed(src, amanifest, 'android:label="RustDesk"', f'android:label="{app}"', log)
        sed(src, amanifest, 'android:label="RustDesk Input"', f'android:label="{app} Input"', log)
        kt = "flutter/android/app/src/main/kotlin/com/carriez/flutter_hbb"
        sed(src, f"{kt}/BootReceiver.kt", "RustDesk is Open", f"{app} is Open", log)
        sed(src, f"{kt}/FloatingWindowService.kt", "Show Rustdesk", f"Show {app}", log)
        sed(src, f"{kt}/MainService.kt", '"RustDesk"', f'"{app}"', log)
        sed(src, f"{kt}/MainService.kt", '"RustDesk Service', f'"{app} Service', log)
        sed(src, "flutter/lib/main.dart", "RustDesk", app, log)
        sed(src, "libs/hbb_common/src/config.rs", '"RustDesk"', f'"{app}"', log)
    if platform == "macos":
        bundle_id = f"com.carriez.{_sanitize_bundle_id(app)}"
        log(f"  macOS bundle ID -> {bundle_id}")
        # AppInfo.xcconfig — product name and bundle identifier
        sed(src, "flutter/macos/Runner/Configs/AppInfo.xcconfig",
            "PRODUCT_NAME = RustDesk", f"PRODUCT_NAME = {app}", log)
        sed(src, "flutter/macos/Runner/Configs/AppInfo.xcconfig",
            "PRODUCT_BUNDLE_IDENTIFIER = com.carriez.flutterHbb",
            f"PRODUCT_BUNDLE_IDENTIFIER = {bundle_id}", log)
        # Info.plist — bundle identifier, URL scheme, display name
        sed(src, "flutter/macos/Runner/Info.plist",
            "com.carriez.rustdesk", bundle_id, log)
        sed(src, "flutter/macos/Runner/Info.plist",
            "<string>rustdesk</string>", f"<string>{_sanitize_bundle_id(app)}</string>", log)
        sed_regex(src, "flutter/macos/Runner/Info.plist",
                  r"(<key>CFBundleDisplayName</key>\s*<string>).*?(</string>)",
                  rf"\g<1>{app}\g<2>", log)
        sed_regex(src, "flutter/macos/Runner/Info.plist",
                  r"(<key>NSMicrophoneUsageDescription</key>\s*<string>).*?(</string>)",
                  rf"\g<1>{app} needs microphone access for audio sharing.\g<2>", log)
        # project.pbxproj — bundle identifier (3 occurrences) and product name
        sed(src, "flutter/macos/Runner.xcodeproj/project.pbxproj",
            "PRODUCT_BUNDLE_IDENTIFIER = com.carriez.rustdesk",
            f"PRODUCT_BUNDLE_IDENTIFIER = {bundle_id}", log)
        sed(src, "flutter/macos/Runner.xcodeproj/project.pbxproj",
            'PRODUCT_NAME = "RustDesk"', f'PRODUCT_NAME = "{app}"', log)
        sed(src, "flutter/macos/Runner.xcodeproj/project.pbxproj",
            "RustDesk.app", f"{app}.app", log)
        # Cargo.toml — description (parity with windows/android)
        sed(src, "Cargo.toml", 'description = "RustDesk Remote Desktop"',
            f'description = "{app}"', log)


def _apply_company(src, env, platform, log):
    comp = env["CUSTOM_COMPNAME"]
    if not comp or comp == "Purslane Ltd":
        return
    log(f"  Company name -> {comp}")
    files = ["Cargo.toml", "libs/portable/Cargo.toml", "src/main.rs",
             "flutter/lib/desktop/pages/desktop_setting_page.dart",
             "flutter/windows/runner/Runner.rc", "res/msi/preprocess.py"]
    for rel in files:
        sed(src, rel, "Purslane Tech Pte. Ltd.", comp)
        sed(src, rel, "Purslane Ltd", comp)
    sed(src, "res/msi/preprocess.py", "PURSLANE", comp)
    if platform == "macos":
        sed(src, "flutter/macos/Runner/Configs/AppInfo.xcconfig",
            "Purslane Tech Pte. Ltd.", comp, log)
        sed(src, "flutter/macos/Runner/Configs/AppInfo.xcconfig",
            "Purslane Ltd", comp, log)


def _apply_theme_color(src, env, log):
    """Patch the accent/primary colors in flutter/lib/common.dart.

    RustDesk uses a blue accent (#0071FF) throughout the UI.  This replaces
    the accent, accent50, accent80, button, and idColor constants with a
    user-supplied hex color, re-skinning the entire app.
    """
    color = env.get("CUSTOM_THEME_COLOR", "") or ""
    if not color:
        return
    # Normalise: strip leading #, uppercase, ensure 6 hex digits
    hex_str = color.lstrip("#").upper()
    if not re.match(r"^[0-9A-F]{6}$", hex_str):
        log(f"  ! invalid theme color '{color}' — skipping")
        return
    log(f"  Theme color -> #{hex_str}")
    common = "flutter/lib/common.dart"
    path = os.path.join(src, common)
    if not os.path.isfile(path):
        log(f"  ! {common} not found — skipping theme color")
        return
    text = _read(path)
    # Replace the full-opacity accent: 0xFF0071FF -> 0xFF{hex}
    text = text.replace("0xFF0071FF", f"0xFF{hex_str}")
    # accent50 uses 0x77 alpha: 0x770071FF -> 0x77{hex}
    text = text.replace("0x770071FF", f"0x77{hex_str}")
    # accent80 uses 0xAA alpha: 0xAA0071FF -> 0xAA{hex}
    text = text.replace("0xAA0071FF", f"0xAA{hex_str}")
    # button color: 0xFF2C8CFF -> 0xFF{hex}
    text = text.replace("0xFF2C8CFF", f"0xFF{hex_str}")
    text = text.replace("0xFF2c8cff", f"0xFF{hex_str}")
    # idColor: 0xFF00B6F0 -> 0xFF{hex}
    text = text.replace("0xFF00B6F0", f"0xFF{hex_str}")
    _write(path, text)
    log(f"    · patched accent colors in {common}")


def _apply_urls(src, env, log):
    url = env["CUSTOM_URL_LINK"]
    dl = env["CUSTOM_DOWNLOAD_LINK"]
    if url and url != "https://rustdesk.com":
        log(f"  URL link -> {url}")
        for rel in ["flutter/lib/common.dart",
                    "flutter/lib/desktop/pages/desktop_setting_page.dart",
                    "flutter/lib/mobile/pages/settings_page.dart"]:
            sed(src, rel, "https://rustdesk.com", url)
        # Replace the bare "rustdesk.com" label shown in the mobile About
        # section (the actual link above is already replaced).
        url_host = url.replace("https://", "").replace("http://", "").strip("/").split("/")[0]
        if url_host:
            sed(src, "flutter/lib/mobile/pages/settings_page.dart",
                "'rustdesk.com'", f"'{url_host}'")
    if dl and dl != "https://rustdesk.com/download":
        for rel in ["flutter/lib/desktop/pages/desktop_home_page.dart",
                    "flutter/lib/mobile/pages/connection_page.dart"]:
            sed(src, rel, "https://rustdesk.com/download", dl)


def _apply_flags(src, env, patches_dir, log):
    log("  Feature flags")
    if env["CUSTOM_DELAY_FIX"] == "true":
        if sed(src, "src/client.rs", "!key.is_empty()", "false"):
            log("    · delay fix")
    if env["CUSTOM_X_OFFLINE"] == "true":
        git_apply(src, os.path.join(patches_dir, "xoffline.diff"), log)
    if env["CUSTOM_HIDE_CM"] == "true":
        git_apply(src, os.path.join(patches_dir, "hidecm.diff"), log)
    if env["CUSTOM_REMOVE_NEW_VERSION_NOTIF"] == "true":
        if sed(src, "flutter/lib/desktop/pages/desktop_home_page.dart",
               "updateUrl.isNotEmpty", "false"):
            log("    · removeNewVersionNotif")


def _apply_gpu_texture_fix(src, log):
    # needed so tagged releases resolve flutter_gpu_texture_renderer
    for rel in ["flutter/pubspec.lock", "flutter/pubspec.yaml"]:
        sed(src, rel, "2ded7f146437a761ffe6981e2f742038f85ca68d",
            "08a471bb8ceccdd50483c81cdfa8b81b07b14b87")


# ---------------------------------------------------------------------------
# Android-only: embed the base64 config into native code (SKILL.md §4.2)
# ---------------------------------------------------------------------------

def _apply_android_embed(src, env, log):
    log("  Embed custom config into native code (Android)")
    b64 = env["CUSTOM_B64"]
    kt = os.path.join("flutter", "android", "app", "src", "main", "kotlin",
                      "com", "carriez", "flutter_hbb", "MainService.kt")
    dart = os.path.join("flutter", "lib", "models", "native_model.dart")
    ok1 = sed(src, kt, 'FFI.startServer(configPath, "")',
              f'FFI.startServer(configPath, "{b64}")', log)
    ok2 = sed(src, dart, "customClientConfig: '',",
              f"customClientConfig: '{b64}',", log)
    if not (ok1 and ok2):
        log("    ! WARNING: one of the Android embed points was not found — "
            "password may not preset (see SKILL.md §4.2)")
    # app id — Android requires at least one dot in the package name.
    # Auto-prefix with "com." if the user-supplied id has no dot.
    app_id = env.get("CUSTOM_ANDROID_APP_ID", "")
    if app_id:
        if "." not in app_id:
            app_id = f"com.{app_id}"
            log(f"    · android app id auto-prefixed → {app_id}")
        sed(src, "flutter/android/app/build.gradle", "com.carriez.flutter_hbb", app_id, log)
    # remove android scam warning
    sed_regex(src, "flutter/lib/mobile/pages/server_page.dart",
              r'bind\.mainGetLocalOption\(key:\s*"show-scam-warning"\)', '"N"', log)


def write_custom_txt(dest_dir, env, log=None, filename="custom_.txt"):
    """Write the base64 payload next to the binary (category B)."""
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(env["CUSTOM_B64"])       # base64, NOT raw JSON — SKILL.md §4.1
    if log:
        log(f"  wrote {filename} (base64) -> {path}")
    return path


# ---------------------------------------------------------------------------
# icon / logo branding (all platforms)
# ---------------------------------------------------------------------------

def _magick_resize(src_img, size, dst_img, log=None):
    """Resize an image to an EXACT square using ImageMagick.

    Uses `-resize {size}x{size}^ -gravity center -extent {size}x{size}` so the
    output is always exactly size×size regardless of the input aspect ratio.
    A plain `-resize {size}x{size}` preserves aspect ratio and produces
    non-square output (e.g. 16x9 from a landscape photo), which iconutil
    and .ico generators reject."""
    for cmd in ("magick", "convert"):
        try:
            subprocess.run(
                [cmd, src_img, "-auto-orient",
                 "-resize", f"{size}x{size}^",
                 "-gravity", "center",
                 "-extent", f"{size}x{size}",
                 dst_img],
                check=True, capture_output=True, timeout=30)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    if log:
        log(f"    ! ImageMagick not found — cannot resize to {size}x{size}")
    return False


def _ensure_png(icon_abs, log=None):
    """Convert an icon to a normalized square PNG with alpha.

    Applies Exif orientation, converts to PNG with an alpha channel, and
    center-crops to a square (1024x1024) so downstream resizes produce
    exact dimensions. iconutil rejects non-square or non-alpha PNGs, and
    a JPG with Exif rotation would come out sideways without -auto-orient.
    Uses a temp file so the user's original is never overwritten.
    Returns the normalized PNG path, or the original on conversion failure."""
    # Use a temp file so we never overwrite the user's source image.
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    png_path = tmp.name
    for cmd in ("magick", "convert"):
        try:
            subprocess.run(
                [cmd, icon_abs, "-auto-orient",
                 "-resize", "1024x1024^",
                 "-gravity", "center", "-extent", "1024x1024",
                 "-background", "none", "-alpha", "on",
                 png_path],
                check=True, capture_output=True, timeout=30)
            if log:
                log(f"    · converted {os.path.basename(icon_abs)} -> "
                    "1024x1024 PNG (auto-orient + alpha)")
            return png_path
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    if log:
        log(f"    ! could not convert {os.path.basename(icon_abs)} to PNG — "
            "ImageMagick not found; using original file as-is")
    try:
        os.unlink(png_path)
    except OSError:
        pass
    return icon_abs


def _make_ico(src_img, dst_ico, log=None):
    """Create a multi-resolution ICO (256,64,48,32,16) from a PNG via ImageMagick."""
    for cmd in ("magick", "convert"):
        try:
            subprocess.run([cmd, src_img, "-define",
                            "icon:auto-resize=256,64,48,32,16", dst_ico],
                           check=True, capture_output=True, timeout=30)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    if log:
        log("    ! ImageMagick not found — cannot create .ico")
    return False


def _make_icns(src_img, dst_icns, log=None):
    """Create an .icns from a PNG using iconutil (macOS only)."""
    iconset = tempfile.mkdtemp(suffix=".iconset")
    sizes = [(16, "icon_16x16.png"), (32, "icon_16x16@2x.png"),
             (32, "icon_32x32.png"), (64, "icon_32x32@2x.png"),
             (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
             (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"),
             (512, "icon_512x512.png"), (1024, "icon_512x512@2x.png")]
    for sz, name in sizes:
        _magick_resize(src_img, sz, os.path.join(iconset, name), log)
    try:
        proc = subprocess.run(["iconutil", "-c", "icns", iconset, "-o", dst_icns],
                              capture_output=True, timeout=30)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            if log:
                log(f"    ! iconutil failed — cannot create .icns: {err}")
            return False
        if log:
            log(f"    · created {os.path.basename(dst_icns)} via iconutil")
        return True
    except Exception as exc:
        if log:
            log(f"    ! iconutil failed — cannot create .icns: {exc}")
        return False
    finally:
        shutil.rmtree(iconset, ignore_errors=True)


def _patch_ui_rs_icon(src, icon_path, log):
    """Replace the base64-encoded icon in src/ui.rs with the user's icon.

    Detects the actual image format and uses the correct MIME type so a JPG
    icon isn't mislabelled as image/png (which causes some renderers to fail)."""
    with open(icon_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    # Detect MIME type from file content (magic bytes), not extension.
    with open(icon_path, "rb") as f:
        header = f.read(12)
    if header[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif header[:4] == b"\x89PNG":
        mime = "image/png"
    elif header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        mime = "image/webp"
    elif header[:6] in (b"GIF87a", b"GIF89a"):
        mime = "image/gif"
    else:
        mime = "image/png"  # fallback — the pattern in ui.rs uses png
    # The icon appears as "data:image/<type>;base64,XXXX..." in ui.rs.
    # Match the existing mime type in the file and replace the payload.
    ok = sed_regex(src, "src/ui.rs",
                   r'(data:image/)(?:png|jpeg|jpg|webp|gif)(;base64,)[^"]*',
                   rf'\g<1>{mime.split("/")[-1]}\g<2>{b64}', log)
    if not ok and log:
        log("    ! ui.rs icon base64 pattern not found — skipping")
    return ok


def _apply_icon(src, env, platform, log):
    """Replace app icons across all platforms, mirroring the VenimK workflows."""
    icon_file = env.get("CUSTOM_ICON_FILE", "")
    if not icon_file:
        return
    icon_abs = os.path.abspath(icon_file)
    if not os.path.exists(icon_abs):
        log(f"  ! icon file not found: {icon_file}")
        return
    log(f"  App icon -> {os.path.basename(icon_file)}")

    # Convert non-PNG icons (JPG, WEBP, etc.) to PNG with an alpha channel.
    # iconutil (macOS .icns), .ico generators, and the base64 embed in ui.rs
    # all expect PNG data. Without this, a JPG gets copied as "icon.png" with
    # JPG bytes inside, and iconutil rejects the converted PNGs for missing
    # alpha — silently breaking the macOS app icon.
    icon_abs = _ensure_png(icon_abs, log)

    res_dir = os.path.join(src, "res")
    flutter_assets = os.path.join(src, "flutter", "assets")

    # ── common: res/icon.png + resized PNGs + flutter/assets/icon.png ──
    dst_icon_png = os.path.join(res_dir, "icon.png")
    shutil.copy2(icon_abs, dst_icon_png)
    log("    · res/icon.png")

    for sz in (32, 64, 128):
        _magick_resize(icon_abs, sz, os.path.join(res_dir, f"{sz}x{sz}.png"), log)

    # 128x128@2x.png (256x256)
    _magick_resize(icon_abs, 256, os.path.join(res_dir, "128x128@2x.png"), log)

    # flutter/assets/icon.png
    shutil.copy2(icon_abs, os.path.join(flutter_assets, "icon.png"))
    log("    · flutter/assets/icon.png")

    # rustdesk/data/flutter_assets/assets/icon.png (if exists)
    fa2 = os.path.join(src, "rustdesk", "data", "flutter_assets", "assets")
    if os.path.isdir(fa2):
        shutil.copy2(icon_abs, os.path.join(fa2, "icon.png"))

    # ── patch src/ui.rs base64 icon ──
    _patch_ui_rs_icon(src, icon_abs, log)

    # ── platform-specific ──
    if platform == "windows":
        _apply_icon_windows(src, icon_abs, res_dir, log)
    elif platform == "macos":
        _apply_icon_macos(src, icon_abs, res_dir, log)
    elif platform == "android":
        _apply_icon_android(src, icon_abs, res_dir, log)
    elif platform == "linux":
        _apply_icon_linux(src, icon_abs, res_dir, log)


def _apply_icon_windows(src, icon, res_dir, log):
    """Windows: .ico, tray icon, Runner.rc resource, flutter_assets icon.ico."""
    # res/icon.ico
    ico_path = os.path.join(res_dir, "icon.ico")
    _make_ico(icon, ico_path, log)
    # res/tray-icon.ico
    shutil.copy2(ico_path, os.path.join(res_dir, "tray-icon.ico"))
    log("    · res/tray-icon.ico")
    # flutter/windows/runner/resources/app_icon.ico (compiled into exe)
    runner_ico = os.path.join(src, "flutter", "windows", "runner",
                              "resources", "app_icon.ico")
    if os.path.exists(runner_ico):
        shutil.copy2(ico_path, runner_ico)
        log("    · flutter/windows/runner/resources/app_icon.ico")
    # flutter/assets/icon.ico — loaded at runtime by win32_window.cpp
    # LoadCustomIcon() from data\flutter_assets\assets\icon.ico
    flutter_assets = os.path.join(src, "flutter", "assets")
    shutil.copy2(ico_path, os.path.join(flutter_assets, "icon.ico"))
    log("    · flutter/assets/icon.ico (runtime window icon)")
    # rustdesk/data/flutter_assets/assets/icon.ico (if exists)
    fa2 = os.path.join(src, "rustdesk", "data", "flutter_assets", "assets")
    if os.path.isdir(fa2):
        shutil.copy2(ico_path, os.path.join(fa2, "icon.ico"))


def _apply_icon_macos(src, icon, res_dir, log):
    """macOS: .icns, appiconset, tray icons."""
    # res/mac-icon.png (128x128)
    _magick_resize(icon, 128, os.path.join(res_dir, "mac-icon.png"), log)

    # flutter/macos/Runner/AppIcon.icns
    icns_path = os.path.join(src, "flutter", "macos", "Runner", "AppIcon.icns")
    _make_icns(icon, icns_path, log)

    # AppIcon.appiconset (create if it doesn't exist)
    appiconset = os.path.join(src, "flutter", "macos", "Runner",
                              "Assets.xcassets", "AppIcon.appiconset")
    os.makedirs(appiconset, exist_ok=True)
    for sz in (16, 32, 64, 128, 256, 512, 1024):
        _magick_resize(icon, sz,
                       os.path.join(appiconset, f"app_icon_{sz}.png"), log)
    # Contents.json
    contents = {"images": [
        {"size": "16x16", "idiom": "mac", "filename": "app_icon_16.png", "scale": "1x"},
        {"size": "16x16", "idiom": "mac", "filename": "app_icon_32.png", "scale": "2x"},
        {"size": "32x32", "idiom": "mac", "filename": "app_icon_32.png", "scale": "1x"},
        {"size": "32x32", "idiom": "mac", "filename": "app_icon_64.png", "scale": "2x"},
        {"size": "128x128", "idiom": "mac", "filename": "app_icon_128.png", "scale": "1x"},
        {"size": "128x128", "idiom": "mac", "filename": "app_icon_256.png", "scale": "2x"},
        {"size": "256x256", "idiom": "mac", "filename": "app_icon_256.png", "scale": "1x"},
        {"size": "256x256", "idiom": "mac", "filename": "app_icon_512.png", "scale": "2x"},
        {"size": "512x512", "idiom": "mac", "filename": "app_icon_512.png", "scale": "1x"},
        {"size": "512x512", "idiom": "mac", "filename": "app_icon_1024.png", "scale": "2x"},
    ], "info": {"version": 1, "author": "xcode"}}
    with open(os.path.join(appiconset, "Contents.json"), "w") as f:
        json.dump(contents, f, indent=2)
    log("    · AppIcon.appiconset + Contents.json")

    # tray icons (dark + light, 22x22)
    for variant in ("dark", "light"):
        tray = os.path.join(res_dir, f"mac-tray-{variant}-x2.png")
        try:
            if variant == "dark":
                subprocess.run(["magick", icon, "-resize", "22x22",
                                "-colorspace", "gray", "-alpha", "set",
                                "-background", "none", "-channel", "A",
                                "-evaluate", "set", "100%", tray],
                               check=True, capture_output=True, timeout=30)
            else:
                subprocess.run(["magick", icon, "-resize", "22x22",
                                "-negate", "-colorspace", "gray", "-alpha", "set",
                                "-background", "none", "-channel", "A",
                                "-evaluate", "set", "100%", tray],
                               check=True, capture_output=True, timeout=30)
            log(f"    · res/mac-tray-{variant}-x2.png")
        except Exception:
            pass


def _apply_icon_android(src, icon, res_dir, log):
    """Android: .ico, tray icon, mipmap icons."""
    # res/icon.ico + tray-icon.ico
    _make_ico(icon, os.path.join(res_dir, "icon.ico"), log)
    shutil.copy2(os.path.join(res_dir, "icon.ico"),
                 os.path.join(res_dir, "tray-icon.ico"))

    # mipmap icons
    res_root = os.path.join(src, "flutter", "android", "app", "src", "main", "res")
    mipmap_sizes = {"mipmap-mdpi": 48, "mipmap-hdpi": 72,
                    "mipmap-xhdpi": 96, "mipmap-xxhdpi": 144,
                    "mipmap-xxxhdpi": 192}
    for folder, sz in mipmap_sizes.items():
        d = os.path.join(res_root, folder)
        if os.path.isdir(d):
            for name in ("ic_launcher.png", "ic_launcher_round.png",
                         "ic_stat_logo.png"):
                _magick_resize(icon, sz, os.path.join(d, name), log)
    # Remove the adaptive-icon XML folder, otherwise launchers on Android 8+
    # still use the default RustDesk vector icon instead of our PNG mipmaps.
    anydpi = os.path.join(res_root, "mipmap-anydpi-v26")
    if os.path.isdir(anydpi):
        shutil.rmtree(anydpi)
        log("    · removed default mipmap-anydpi-v26 adaptive icons")
    log("    · android mipmap icons")


def _apply_icon_linux(src, icon, res_dir, log):
    """Linux: same as Android (ico + resized PNGs)."""
    _make_ico(icon, os.path.join(res_dir, "icon.ico"), log)
    shutil.copy2(os.path.join(res_dir, "icon.ico"),
                 os.path.join(res_dir, "tray-icon.ico"))


def _apply_logo(src, env, platform, log):
    """Replace the in-app logo (flutter/assets/icon.svg and logo files)."""
    logo_file = env.get("CUSTOM_LOGO_FILE", "")
    if not logo_file:
        return
    logo_abs = os.path.abspath(logo_file)
    if not os.path.exists(logo_abs):
        log(f"  ! logo file not found: {logo_file}")
        return
    log(f"  App logo -> {os.path.basename(logo_file)}")

    flutter_assets = os.path.join(src, "flutter", "assets")

    # If the logo is a raster image (PNG/JPG/WEBP/etc.), copy as icon.png
    # (the in-app logo displayed in about) and try to generate an SVG via
    # potrace (macOS/Linux only; on Windows potrace is often unavailable,
    # so the PNG fallback in loadIcon() is used).
    # Non-PNG rasters are converted to PNG first so the file content matches
    # the .png extension — a JPG copied as "icon.png" has wrong magic bytes.
    if logo_abs.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")):
        logo_png = _ensure_png(logo_abs, log)
        shutil.copy2(logo_png, os.path.join(flutter_assets, "icon.png"))
        log("    · flutter/assets/icon.png (logo)")
        if platform in ("macos", "linux"):
            try:
                pbm = tempfile.NamedTemporaryFile(suffix=".pbm", delete=False)
                pbm.close()
                subprocess.run(["magick", logo_png, "-flatten", pbm.name],
                               check=True, capture_output=True, timeout=30)
                svg_path = os.path.join(flutter_assets, "icon.svg")
                subprocess.run(["potrace", "--svg", "-o", svg_path, pbm.name],
                               check=True, capture_output=True, timeout=30)
                log("    · flutter/assets/icon.svg (via potrace)")
            except Exception:
                pass
            finally:
                try:
                    os.unlink(pbm.name)
                except OSError:
                    pass
    elif logo_abs.lower().endswith(".svg"):
        shutil.copy2(logo_abs, os.path.join(flutter_assets, "icon.svg"))
        log("    · flutter/assets/icon.svg")
    else:
        # Unknown type — copy as-is but use the original extension, not .svg
        dst = os.path.join(flutter_assets, os.path.basename(logo_abs))
        shutil.copy2(logo_abs, dst)
        log(f"    · flutter/assets/{os.path.basename(logo_abs)} (from logo)")

    # Also copy to rustdesk/data/flutter_assets/assets/ if it exists
    fa2 = os.path.join(src, "rustdesk", "data", "flutter_assets", "assets")
    if os.path.isdir(fa2):
        for fname in ("icon.svg", "icon.png"):
            src_f = os.path.join(flutter_assets, fname)
            if os.path.exists(src_f):
                shutil.copy2(src_f, os.path.join(fa2, fname))


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def _apply_windows_build_fix(src, log):
    """Fix build.py's hardcoded 'python3' calls on Windows.

    RustDesk's build.py invokes the portable packer and inline-sciter
    scripts via a literal 'python3' shell command. Windows installs
    normally only expose 'python'/'py', not 'python3', so the portable
    .exe packing step fails with a non-zero exit code. Replace each
    call site with sys.executable (already imported in build.py).
    """
    path = os.path.join(src, "build.py")
    if not os.path.isfile(path):
        return
    text = _read(path)
    if "python3 " not in text and "'python3" not in text:
        return
    replacements = [
        # f-string call sites: 'python3' -> '{sys.executable}' inside an
        # existing f-string, so the braces are interpolated correctly.
        ("f'python3 ./generate.py", "f'{sys.executable} ./generate.py"),
        # plain-string call site: needs an f-prefix added too.
        ("system2('python3 res/inline-sciter.py')",
         "system2(f'{sys.executable} res/inline-sciter.py')"),
    ]
    new_text = text
    changed = False
    for old, new in replacements:
        if old in new_text:
            new_text = new_text.replace(old, new)
            changed = True
    if changed:
        _write(path, new_text)
        log("  · patched build.py: python3 -> sys.executable (Windows fix)")


def apply(src_dir, platform, env, patches_dir, log=print):
    """
    Apply all customizations for `platform` in-place on `src_dir`.
    platform: 'windows' | 'linux' | 'macos' | 'android'
    """
    log(f"Applying customizations for {platform} …")
    # allowCustom.py runs with cwd=src_dir, and git apply needs a real path,
    # so patches_dir must be absolute.
    patches_dir = os.path.abspath(patches_dir)
    src_dir = os.path.abspath(src_dir)
    _apply_server_key_api(src_dir, env, log)
    _apply_allow_custom(src_dir, patches_dir, log)
    git_apply(src_dir, os.path.join(patches_dir, "removeSetupServerTip.diff"), log)
    if platform == "windows":
        _apply_windows_build_fix(src_dir, log)
    _apply_appname(src_dir, env, platform, log)
    _apply_company(src_dir, env, platform, log)
    _apply_flags(src_dir, env, patches_dir, log)
    _apply_gpu_texture_fix(src_dir, log)
    _apply_urls(src_dir, env, log)
    _apply_theme_color(src_dir, env, log)
    _apply_icon(src_dir, env, platform, log)
    _apply_logo(src_dir, env, platform, log)
    if platform == "android":
        _apply_android_embed(src_dir, env, log)
    log("Customizations complete.")
