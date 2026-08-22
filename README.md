[![PayPal](https://img.shields.io/badge/Donate-PayPal-00457C?logo=paypal&logoColor=white)](https://paypal.me/VenimK)
[![Python](https://img.shields.io/badge/Python-3.8%2B-3776AB?logo=python&logoColor=white)](#quick-start)
[![Platforms](https://img.shields.io/badge/Windows%20%7C%20macOS%20%7C%20Linux-black)](#who-can-build-what)
[![RustDesk](https://img.shields.io/badge/RustDesk-1.4.9-orange)](#)

# DVForge

**Build your own branded RustDesk client on this computer — server, key, password, and permissions baked in. No GitHub. No cloud CI.**

Open a local page in the browser. Pick a target. Hit build. The installer lands in `workspace/output/`.

```
Windows PC  →  Windows .exe / .msi
Mac         →  macOS .dmg  +  Android APK
Linux / WSL →  Linux packages  +  Android APK
```

Same folder on every OS. Python 3.8+ is the only thing you must already have. Flutter, Rust, NDK, JDK, and the rest install into a private `.toolchains/` folder — nothing system-wide.

---

## Quick start

### 1. Get the app

```bash
git clone https://github.com/VenimK/DVForge.git
cd DVForge
```

### 2. First-time machine setup (pick one)

| You are on | Run this |
|------------|----------|
| **macOS** (DMG, optional Android) | `bash Setup-DVForge-macOS.sh` &nbsp;·&nbsp; add `--with-android` for APKs |
| **Windows** (`.exe` / `.msi`) | `powershell -NoProfile -ExecutionPolicy Bypass -File .\Setup-DVForge-Windows.ps1` |
| **Windows + WSL** (Linux + APKs) | `powershell -NoProfile -ExecutionPolicy Bypass -File .\Setup-DVForge-WSL2.ps1` |

Already have the tree open? On a Mac use `bash Setup-DVForge-macOS.sh --in-place`.

### 3. Launch

```bash
# Linux / macOS
./run.sh

# Windows
run.bat
```

Or `python3 app.py`. The UI opens at [http://127.0.0.1:8765](http://127.0.0.1:8765).

`--no-browser` skips the auto-open. `RDLB_PORT=9000` changes the port.

---

## How a build works

1. **Targets** — the board lights what *this* machine can produce. Amber = ready. Outline = click **install** on a missing tool. Hatch = wrong OS.
2. **Config** — your server, public key, API, app name, icon, password, permissions, connection direction. Live preview of the baked-in payload.
3. **Build** — log streams live. When it finishes, installers sit in `workspace/output/v1.4.9/`.

**Dry run / Preview plan** prints every command without compiling. Use it once to see the plan.

---

## Who can build what

Desktop clients are built on their own OS. Android is cross-compiled from Linux or macOS.

| You want | Build it on | What you get |
|----------|-------------|--------------|
| Windows app | Windows | portable `.exe`, optional `.msi` |
| macOS Apple Silicon | any Mac | `*-aarch64.dmg` |
| macOS Intel | any Mac | `*-x86_64.dmg` (cross-compiled from Apple Silicon) |
| macOS universal | any Mac | `*-universal.dmg` (both CPUs, slower) |
| Linux | Linux / WSL | `.deb` / `.rpm` / `.AppImage` |
| Android phone / tablet | Linux, WSL, or macOS | `.apk` |

Windows PCs cannot build APKs. The board hides that for you.

---

## Android — which APK?

Pick the cell that matches the **device CPU**, not the computer you built on.

| Device | Board cell | File name |
|--------|------------|-----------|
| Almost every phone (Galaxy A54 5G, Pixel, most 2019+) | **Android arm64-v8a** | `YourApp-arm64-v8a-release.apk` |
| Very old 32-bit phones | Android armeabi-v7a | `…-armeabi-v7a-release.apk` |
| Android x86 emulators / some tablets | Android x86_64 | `…-x86_64-release.apk` |
| One APK for every ABI | Android universal | `…-release.apk` (larger) |

Only the target you selected is collected. Sideloaded remote-desktop APKs always trip Play Protect (“install anyway”) — that is normal, not a broken build.

APKs use **debug signing** unless you add your own keystore.

### Android tools (Linux or Mac)

Click **install** on the board, or on a Mac:

```bash
bash Setup-DVForge-macOS.sh --with-android
```

That puts **JDK 17**, **NDK r28c**, and the **Android SDK (API 34)** into `.toolchains/`. Same Flutter you already use for DMGs.

---

## Config worth knowing

| Setting | Where | What it does |
|---------|-------|----------------|
| Server / key / API | Config tab | Compiled into the client |
| App name, icon, logo, accent | Config tab | Branding on every platform |
| Password & permissions | Config tab | Written as base64 `custom_.txt` (on Android also embedded in native code) |
| Connection direction | Config → Tweaks | `both` · `incoming` (host only) · `outgoing` (client only) |

---

## Toolchains

The left rail **detects** what you have. For most tools it can **download a portable copy** into `.toolchains/` (no admin). Click **install** next to a gap, or **install missing**.

| Tool | Version | For |
|------|---------|-----|
| Python | 3.8+ | the app itself |
| Git | any | source checkout |
| Rust | 1.75 (1.81 on macOS) | every native build |
| Flutter | 3.24.5 | every Flutter UI |
| LLVM / libclang | 15 | bindgen / ffigen |
| vcpkg | pinned | FFmpeg / hwcodec |
| JDK | 17 | Android |
| Android NDK | r28c | Android native lib (16 KB pages — needed on Android 15+) |
| Android SDK | API 34 | `flutter build apk` |
| Xcode + create-dmg | — | macOS `.dmg` |
| VS C++ Build Tools | 2022 | Windows `.msi` / linker |

Xcode and Visual Studio cannot be silently sideloaded; the UI gives the exact install hint.

Delete `.toolchains/` to start the tool downloads clean. Uninstall scripts:

```bash
bash Uninstall-DVForge-macOS.sh
# Windows: Uninstall-DVForge-Windows.ps1
# WSL:     Uninstall-DVForge-WSL2.ps1
```

---

## Output

```
workspace/output/v1.4.9/
  YourApp-1.4.9-aarch64.dmg
  YourApp-arm64-v8a-release.apk
  …
```

---

## Layout

```
dvforge/
├── app.py                      # local HTTP UI (Python stdlib only)
├── run.sh / run.bat
├── Setup-DVForge-macOS.sh
├── Setup-DVForge-Windows.ps1
├── Setup-DVForge-WSL2.ps1
├── builder/                    # detect · install tools · customize · build
├── web/                        # the browser GUI
├── configs/RustDesk.json       # your baked-in config (edit in the GUI)
├── patches/                    # allowCustom + feature diffs
└── workspace/output/           # finished installers
```

---

## Notes

- DVForge **only builds**. It does not publish releases or host downloads.
- One Flutter on the machine (3.24.5). Bridge codegen is local, not a second CI Flutter.
- Updates: **Check for updates** in the left rail pulls a newer DVForge commit when you want it.

---

Built for people who self-host RustDesk and want a client that already knows their server.

[paypal.me/VenimK](https://paypal.me/VenimK)
