# HANDOFF — RustDesk Local Builder

A pick-up-where-we-left-off doc. If you're a fresh session (human or AI), read
this top to bottom and you'll know what this is, what works, what doesn't yet,
and exactly what to do next.

Last updated: 2026-07-29.

## 0c. Third Windows test round (the linker wall)

All toolchains installed fine (Flutter/LLVM/NDK/vcpkg/Rust), but every `cargo`
compile failed with **`linker link.exe not found`** — Rust's Windows host is
MSVC, which needs the Visual C++ linker the machine didn't have. That one gap
blocked Android (host proc-macros/build-scripts compile for the host) *and* the
Windows `.msi`. Fixes:
- **Added a one-click "VS Build Tools (C++)" installer** — Microsoft's official
  Build Tools bootstrapper (`aka.ms/vs/17/release/vs_BuildTools.exe`), the
  command-line-only subset of Visual Studio (no IDE). Installs the MSVC toolset
  (link.exe) + Windows SDK + MSBuild via one UAC prompt. It's ~4–6 GB — the one
  unavoidably heavy, admin-requiring piece. Excluded from bulk "install missing";
  explicit click only, with a confirm.
- **Board now honestly requires the MSVC toolset on a Windows host for *all*
  targets** (incl. Android), since any cargo compile needs link.exe. So cells
  read "install first: msbuild" until Build Tools are in.
- **`msbuild` detection now uses `vswhere`** so it flips green post-install and
  confirms link.exe is available.
- **Unicode crash fixed**: `flutter build apk` output contained a non-cp1252
  byte and our reader crashed (`'charmap' codec can't decode 0x90`). Subprocess
  output is now decoded utf-8/`errors="replace"` everywhere.
- **Git Bash auto-found**: the NDK `.sh` scripts need bash; we now derive it from
  git's location (`<Git>\bin\bash.exe`) when it isn't on PATH.
- **Rust now adds itself to PATH** (dropped `--no-modify-path`), so cargo works
  in any new terminal, not just inside this app.

Possible future alternative to VS Build Tools (not yet built): a GNU host Rust
toolchain (`-gnu`) + portable mingw-w64, which avoids Visual Studio entirely and
is far smaller — but it's higher-risk and untested, so Build Tools is the
default. Ask if you want the no-VS path prototyped.

## 0b. Second Windows test round (fixes)

- **Rust wasn't installable on a bare machine** (no rustup → no install button,
  and every Android/Windows target showed "install first: rust"). Now Rust is
  one-click on any host: we download **rustup-init** and install 1.75 per-user
  into `~/.cargo` (`-y --profile minimal --no-modify-path`) — **no admin**. This
  unblocks Android entirely (Android needs rust/flutter/ndk/java, not LLVM).
- **LLVM install → WinError 740 (elevation)**: the official LLVM Windows
  installer is requireAdministrator, so silent `/S` can't do a per-user install.
  Now we run it **elevated via one UAC prompt** (temp `.bat` carries the `/D`
  path so spaces survive). LLVM is only needed for Windows *desktop* builds, and
  the log says so.
- **SSE `ConnectionAbortedError` (WinError 10053) traceback spam** when the
  browser closes the stream → now swallowed (it's normal).

## 0. Latest changes (real Windows test round)

Tested on a real Windows 11 box (Rust 1.97, VS present). Fixes applied since:
- **LLVM install 404** → Windows now uses the official NSIS `LLVM-15.0.6-win64.exe`
  with a silent per-user install (`/S /D=`), not the non-existent `.tar.xz`.
- **`flutter_rust_bridge_codegen` WinError 2** → `run()` now resolves every
  executable via `shutil.which` (respects Windows PATHEXT for `.exe/.bat/.cmd`)
  and puts `~/.cargo/bin` on PATH, so cargo-installed tools and `flutter.bat`
  are found. Missing tools now raise a clear message instead of WinError 2.
- **`rustdesk-src already exists`** → checkout is now always clean: any previous
  source tree is removed first (customizations mutate the tree, so reuse would
  corrupt it). Handles read-only `.git` on Windows.
- **Android `.sh` scripts on Windows** → routed through **bash** (Git Bash);
  clear message if bash is missing.
- **`python3` not on Windows** → build.py runs under the current interpreter
  (`sys.executable`).
- Toolchain panel now shows **download + on-disk sizes and versions**, a
  **local footprint total**, and a **remove (✕)** button per locally-installed
  tool. "Needs a tool" board cells are now an obvious **amber ⚠ "install first"**
  state instead of subtle steel.

Still unverified end-to-end: a *complete* real compile (the sandbox has no
network/toolchains). Download URLs are official but unhit from here — if one
404s, the console prints the exact URL and it's a one-line fix in `toolchains.py`.

---

## 1. What we're making

The user owns `penangit/rustdesk-builder-v2` — a set of **GitHub Actions**
workflows that compile a *customised* RustDesk remote-desktop client (their own
server address, public key, baked-in password, permissions, app name, etc.).

We're rebuilding that as a **local desktop app** so it runs on the user's own
machine with **no GitHub**. It has:

- a tiny **Python standard-library** web server (`app.py`) — no pip installs;
- a **browser GUI** (`web/`) with a hardware-capability aesthetic;
- three jobs:
  1. **detect** the machine's hardware + OS and show which RustDesk targets it
     can build (capability board);
  2. **edit config** (server/key/password/permissions/tweaks) with a live
     `custom_.txt` preview;
  3. **build locally** and stream the log, then list artifacts.

The user is on **Windows** (Visual Studio detected; Rust 1.97 present; Flutter,
LLVM, vcpkg, NDK, JDK17 missing). So their box targets **Windows + Android**.

---

## 2. The domain knowledge that matters (don't lose this)

From the project's `SKILL.md`. These are the load-bearing facts:

- Customisations land **two ways**:
  - **Compiled in** — server, public key, API server, app/company name, URLs,
    feature flags are `sed`-patched into the RustDesk *source* before building.
  - **Runtime** — password, permissions, approve-mode are read at runtime from
    `custom_.txt` sitting next to the binary.
- **`custom_.txt` MUST be BASE64, not raw JSON.** `read_custom_client()` starts
  with `decode64()`. This was the historic footgun. `config_gen.py` emits
  `CUSTOM_B64` and it is byte-for-byte identical to the original
  `load-config.py` (verified with diff).
- **Android never file-reads `custom_.txt`.** The base64 config must be embedded
  into native code: `MainService.kt` (`FFI.startServer(configPath, "")`) and
  `native_model.dart` (`customClientConfig: ''`), and also bundled as
  `flutter/assets/custom_.txt`. `customize.py` does this.
- `allowCustom.py` strips a 9-line signature-check block from `src/common.rs`
  and renames `custom.txt` → `custom_.txt`.
- Desktop builds are **host-locked**: Windows builds Windows, macOS builds
  macOS, Linux builds Linux. **Android builds on any host** (NDK is
  cross-platform). No cross-OS desktop builds — mirrors the workflows.
- Pinned toolchain versions: **Rust 1.75 · Flutter 3.24.5 · LLVM 15.0.6 ·
  NDK r28c · vcpkg (commit `120deac3062162151622ca4860575a33844ba10b`) ·
  flutter_rust_bridge_codegen 1.80.1 · JDK 17**. RustDesk built at tag v1.4.9.
- Real config values live in `configs/RustDesk.json` (server ir.remote-neo.com,
  compname deadboy, etc.).

---

## 3. What's DONE and verified

All Python modules compile and import; logic tested in-sandbox via dry-runs and
a mock source tree (no network here, so no *real* compile was possible).

| Piece | File | State |
|-------|------|-------|
| Hardware/OS detection + capability matrix | `builder/detect.py` | ✅ tested |
| Toolchain detection + per-OS install hints | `builder/prereqs.py` | ✅ tested |
| Config → `CUSTOM_*` + base64 `custom_.txt` | `builder/config_gen.py` | ✅ byte-identical to original |
| Source customisations (all sed/patch steps, Android embed, signature strip) | `builder/customize.py` | ✅ tested on mock tree |
| Build orchestration (checkout, bridge, per-OS build, vcpkg setup, artifact collect, dry-run, cancel, live log) | `builder/orchestrator.py` | ✅ dry-run tested |
| HTTP server + SSE log stream + static serving | `app.py` | ✅ endpoints tested |
| GUI (spec readout, capability board, config form + live preview, build console) | `web/index.html`, `web/style.css`, `web/app.js` | ✅ served; rendered content verified |
| Launchers | `run.sh`, `run.bat` | ✅ |
| Docs | `README.md`, this `HANDOFF.md` | ✅ |

Endpoints live: `GET /api/host /api/prereqs /api/matrix /api/config
/api/build/stream /api/build/status`; `POST /api/config /api/preview
/api/build/preflight /api/build/start /api/build/cancel`.

---

## 4. What's REMAINING

1. **Auto-download toolchains** (in progress — this is the current task). Let the
   app fetch the missing SDKs into a local `.toolchains/` folder (no admin, no
   system pollution), wire the env vars, and re-detect. Target tools on Windows:
   Flutter, LLVM 15, vcpkg, Android NDK r28c, JDK 17. Rust is present (just needs
   `rustup toolchain install 1.75`). The MSVC **C++ workload** can't be silently
   sideloaded — it stays a guided step (offer to drive `vs_installer` if found).
2. **A real end-to-end build** has never been run (sandbox has no network and no
   heavy toolchains). First real test must happen on the user's Windows box.
3. **Windows Flutter engine**: a full desktop build also needs RustDesk's custom
   Flutter engine (`rustdesk/engine` → `windows-x64-release.zip`) placed in the
   Flutter cache. Currently documented, not automated.
4. **Android keystore** wiring (optional) — APKs use debug signing otherwise.

---

## 5. How to continue — concrete steps

### To add auto-download (current task)
- New module `builder/toolchains.py` with a registry: for each tool id
  (`flutter`, `llvm`, `vcpkg`, `android_ndk`, `java`, `rust`) define the download
  URL per OS/arch, the archive kind (zip / tar.xz / tar.gz / git), the extract
  target under `.toolchains/`, and the env vars to set afterwards
  (`PATH` additions, `LIBCLANG_PATH`, `VCPKG_ROOT`, `ANDROID_NDK_HOME`,
  `JAVA_HOME`).
- Persist chosen env to `.toolchains/env.json`; **load and apply it at the top of
  `app.py`** before detection runs, so installed tools show up immediately.
- Stream install progress over SSE using the same pattern as `BuildSession`
  (add an `InstallSession`). Endpoints:
  `GET /api/toolchains`, `POST /api/toolchains/install`,
  `GET /api/toolchains/stream`, `POST /api/toolchains/cancel`.
- GUI: an "install" affordance next to each missing-but-installable tool + an
  "install missing" button; show progress in a console; on done, re-fetch
  `/api/prereqs` and `/api/matrix`.
- Known-good official URLs (verify once online):
  - Flutter Win: `https://storage.googleapis.com/flutter_infra_release/releases/stable/windows/flutter_windows_3.24.5-stable.zip`
  - LLVM 15.0.6 Win portable: LLVM GitHub release asset `clang+llvm-15.0.6-x86_64-pc-windows-msvc.tar.xz`
  - NDK r28c Win: `https://dl.google.com/android/repository/android-ndk-r28c-windows.zip`
  - JDK 17 Win: `https://api.adoptium.net/v3/binary/latest/17/ga/windows/x64/jdk/hotspot/normal/eclipse`
  - vcpkg: `git clone https://github.com/microsoft/vcpkg` (bootstrap after).

### To do the first real build (on the user's Windows box)
1. `python app.py` (or `run.bat`), open the GUI.
2. Install missing toolchains (auto or manual), then hit "re-scan".
3. Add the Windows Flutter engine per README "Windows toolchain note".
4. Pick target(s) (Windows portable/exe, or Android), **Preview plan** first,
   then run for real. Artifacts land in `workspace/output/v<version>/`.

---

## 6. Where things are

```
rustdesk-local-builder/
├── app.py                    # server + SSE + static
├── run.sh / run.bat          # launchers
├── HANDOFF.md                # this file
├── README.md                 # user-facing docs
├── builder/
│   ├── detect.py  prereqs.py  config_gen.py  customize.py  orchestrator.py
│   └── toolchains.py         # <-- being added now
├── web/  index.html  style.css  app.js
├── configs/RustDesk.json     # the baked-in config
├── patches/                  # allowCustom.py + the repo's .diff patches
└── workspace/                # build scratch + output/ (created at runtime)
```

Reference inputs (from the user, in the original session's uploads): the
`rustdesk-builder-v2` repo, the parent `rdgen` Django project, RustDesk source,
and the project `SKILL.md`. The workflow logic in `customize.py` /
`orchestrator.py` was ported from those workflows.

---

## 7. Sandbox gotchas for the next AI session

- **Network is disabled** in this build sandbox — you cannot run real downloads
  or compiles here. Test download/extract logic with `file://` fixtures and local
  `git clone` from a local path.
- Filesystem may reset between *separate* sessions; the deliverable is the zip in
  `/mnt/user-data/outputs/`. Always re-package there and call `present_files`.
- No browser in-sandbox; `wkhtmltoimage` exists but uses an old WebKit (no CSS
  grid) so screenshots under-represent the real UI. Modern browsers render it
  correctly.
- Resume without re-asking the user; read this file + the transcript first.
