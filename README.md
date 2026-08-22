[![PayPal](https://img.shields.io/badge/Donate-PayPal-blue.svg)](https://paypal.me/VenimK)

# DVForge

A local desktop app that does what your `rustdesk-builder-v2` GitHub Actions
workflows do — build a custom RustDesk client with your server, key, password
and permissions baked in — but **on your own machine, with no GitHub**.

It has a browser GUI (served by a tiny Python backend) that:

- reads your **hardware and OS**, and shows a **capability board** of exactly
  which targets this machine can build right now;
- lets you edit the **baked-in config** (server, key, password, permissions,
  connection direction, tweaks) with a live preview of the `custom_.txt` payload;
- runs the build locally and **streams the log** into a console, then lists the
  artifacts it produced.

No dependencies beyond **Python 3** — the whole app uses the standard library.

## Runs on

**Windows, macOS (Intel + Apple Silicon), and Linux** — the exact same folder.
`app.py` is pure standard-library Python, so the only thing you need already
installed is **Python 3.8+** (macOS and most Linux ship it; on Windows you
install it once). Everything else — Flutter, LLVM, the NDK, the JDK, vcpkg — the
app can download for you into a local `.toolchains/` folder.

The capability board then shows what *that* machine can build: a Windows PC
builds Windows, a Mac builds macOS (Apple Silicon, Intel, or universal), a
Linux box builds Linux + Android. Same app, different lit cells.

---

## Quick start

```bash
# Linux / macOS
./run.sh

# Windows
run.bat
```

or directly:

```bash
python3 app.py
```

It starts a local server on <http://127.0.0.1:8765> and opens your browser.
Use `--no-browser` to skip auto-opening, or set `RDLB_PORT` to change the port.

---

## Who can build what

Desktop clients must be built on their **own** OS (this matches the workflows —
no cross-OS desktop builds):

| Target                         | Build host        |
|--------------------------------|-------------------|
| Windows `.exe` / `.msi`        | Windows only      |
| macOS Apple Silicon `.dmg`     | macOS (native on ARM) |
| macOS Intel `.dmg`             | macOS (native on Intel; ARM Macs cross-compile x86_64) |
| macOS universal `.dmg`         | macOS (lipo of both slices) |
| Linux `.deb`/`.rpm`/`.AppImage`| Linux only        |
| **Android APKs** (all ABIs)    | **Linux host** |

So: a Windows PC builds Windows; a Linux box builds Linux + Android; a Mac
builds macOS. The capability board shows this automatically — lit (amber) cells
are ready, outlined (steel) cells need a toolchain, hatched cells need a
different host OS.

### macOS targets (Intel, Apple Silicon, universal)

Any Mac can select any of these three cells (Xcode + create-dmg required):

| Board cell | Artifact suffix | What it is |
|------------|-----------------|------------|
| macOS Apple Silicon — .dmg | `-aarch64` | Native arm64. Fast on M1/M2/M3. |
| macOS Intel — .dmg | `-x86_64` | Native on Intel Macs. **Apple Silicon cross-compiles** `x86_64-apple-darwin` (cargo `--target` + `FLUTTER_XCODE_ARCHS=x86_64`). |
| macOS universal (Intel + Apple Silicon) — .dmg | `-universal` | `lipo` of both Rust dylibs + a fat Flutter runner (`ARCHS=arm64 x86_64`). Slower; needs **both** vcpkg triplets. |

Examples: `MyApp-1.4.9-aarch64.dmg`, `MyApp-1.4.9-x86_64.dmg`, `MyApp-1.4.9-universal.dmg`.

Universal installs `arm64-osx` and `x64-osx` into **isolated** vcpkg roots so the second triplet does not delete the first (manifest-mode vcpkg otherwise prunes `installed/arm64-osx` and FFmpeg headers vanish). Cross-target cargo on a `/Volumes` NAS uses `~/Library/Caches/dvforge/cargo-target/` and turns sccache off — rustc rmeta encode is unreliable on network volumes.

**Connection direction** (Config → Tweaks): `both`, `incoming` (host-only), or `outgoing` (client-only). Written as `conn-type` in `custom_.txt`.

---

## Toolchains you need

The app **detects** these, and for most of them can **auto-download** a portable
copy for you — click **install** next to a missing tool (or **install missing**
to get them all). Downloads go into a local `.toolchains/` folder next to the
app; nothing is installed system-wide and no admin rights are needed. The app
records the needed environment variables in `.toolchains/env.json` and applies
them on every launch, so the tools "just work" for builds. Delete that folder to
start clean.

Auto-installable: **Flutter, LLVM 15, Android NDK r28c, JDK 17, vcpkg**, and
**Rust 1.75** (added via your existing `rustup`). The Visual Studio **C++
workload** and **Xcode** can't be sideloaded silently, so those stay guided
steps with a copy-paste hint.

Versions mirror the workflows:

| Tool            | Version | Needed for                     |
|-----------------|---------|--------------------------------|
| Git             | any     | everything                     |
| Python 3        | 3.8+    | everything (and `build.py`)    |
| Rust + Cargo    | 1.75    | everything                     |
| Flutter SDK     | 3.24.5  | all Flutter builds             |
| LLVM / libclang | 15      | Windows / native bindgen       |
| vcpkg           | pinned  | native deps (ffmpeg, hwcodec)  |
| MSBuild (VS)    | 2022    | Windows `.msi`                 |
| Java (JDK)      | 17      | Android                        |
| Android NDK     | r27c    | Android                        |
| Xcode CLT       | —       | macOS                          |

Point `VCPKG_ROOT` at your vcpkg checkout; the builder checks out the pinned
commit and installs the native deps for you.

### Windows toolchain note

A full Windows build additionally needs the RustDesk **custom Flutter engine**
(the workflow downloads `windows-x64-release.zip` from `rustdesk/engine`) and
LLVM 15. Install those once as the workflow does; the builder then runs
`build.py` and packages the portable `.exe` (and `.msi` if MSBuild is present).

---

## What it actually does to the source

Every customization is a faithful Python port of the workflow steps
(`builder/customize.py`). Two categories, exactly as in the project's SKILL.md:

- **Compiled in** — server, public key, API server, app/company name, URLs and
  feature flags are `sed`-patched into the RustDesk source before building.
- **Runtime** — the password, permissions and approve-mode are written to
  `custom_.txt` **as base64** (never raw JSON — that was the big historical
  bug). On **Android**, the base64 config is also embedded into `MainService.kt`
  and `native_model.dart`, because Android never file-reads `custom_.txt`.

The signature check on `custom_.txt` is stripped with the project's own
`allowCustom.py`, and `custom.txt` is renamed to `custom_.txt` throughout.

---

## Dry run

Toggle **Dry run** (or press *Preview plan*) to print every command the build
would execute, without running anything. Handy for reviewing a build or
inspecting it on a machine that doesn't have the toolchains yet.

---

## Layout

```
dvforge/
├── app.py                 # stdlib HTTP server: API + SSE log stream + static files
├── run.sh / run.bat       # launchers
├── builder/
│   ├── detect.py          # hardware/OS detection + build capability matrix
│   ├── prereqs.py         # toolchain detection + per-OS install hints
│   ├── config_gen.py      # config -> CUSTOM_* env + base64 custom_.txt (verified port)
│   ├── customize.py       # source customizations (sed/patch port)
│   └── orchestrator.py    # runs the build per platform, streams logs
├── web/                   # the GUI (index.html, style.css, app.js)
├── configs/RustDesk.json  # your baked-in config (edit in the GUI)
└── patches/               # allowCustom.py + the .diff patches from the repo
```

Artifacts land in `workspace/output/v<version>/`.

---

## Notes & limits

- The app assumes the base toolchains are installed; it guides you but never
  installs system-wide toolchains for you.
- CI uses a separate Flutter (3.22.3) just for the flutter_rust_bridge codegen;
  locally you have one Flutter. The bridge step is best-effort — if codegen
  hiccups, install the bridge Flutter as the workflow does.
- Android APKs are built with the debug signing slot unless you wire your own
  keystore; sideloaded remote-desktop APKs always trip Play Protect ("install
  anyway") — that's inherent, not a build bug.
- It only builds; it doesn't publish releases or drive the Cloudflare download
  page (that was GitHub's job).
