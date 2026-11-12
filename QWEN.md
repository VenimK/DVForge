# QWEN.md — DVForge

## Project overview

**DVForge** is a local builder for customized [RustDesk](https://github.com/rustdesk/rustdesk)
remote-desktop clients. It replaces the GitHub Actions workflow approach: configure a self-hosted
server, branding, baked-in password and permission policy, pick a target, and compile locally.
Output installers land in `workspace/output/<os>/<version>/<architecture>/`.

Architecture is three layers, all dependency-free:

1. **`web/`** — vanilla HTML/CSS/JS browser GUI (five wizard steps: Environment, Config,
   Targets, Review, Build).
2. **`app.py`** — a `ThreadingHTTPServer` on **Python standard library only**; serves the GUI,
   exposes a small JSON API + SSE log streams, binds `127.0.0.1:8765`.
3. **`builder/`** — the engine: detection, toolchain install, config generation, source
   customization, build orchestration, artifact validation.

Optional subsystems: `farm/` (multi-machine build queue, shared folder or HTTP),
`printer-adapter/` (Rust crate replacing RustDesk's signature-gated printer DLL),
`patches/` (feature diffs applied to RustDesk source).

## Running the app

```bash
python3 app.py          # any OS; auto-opens the browser at http://127.0.0.1:8765
./run.sh                # Linux / macOS launcher
run.bat                 # Windows launcher
```

Flags / env: `--no-browser` (suppress auto-open), `RDLB_PORT` (default `8765`).
`app.py` calls `toolchains.apply_persisted_env(ROOT)` **before** the first toolchain scan so
`.toolchains/env.json` is loaded before detection.

First-time machine setup (idempotent, writes `.toolchains/env.json`):
`Setup-DVForge-macOS.sh` / `Setup-DVForge-Windows.ps1` / `Setup-DVForge-WSL2.ps1` /
`Setup-DVForge-Ubuntu.sh`. Per-OS uninstall scripts: `Uninstall-DVForge-*`.
Artifact cleanup: `bash clean.sh` (keeps `configs/` and `workspace/branding/`; `--all` for a
deeper clean). Toolchain probe: `bash precheck.sh` (`precheck.bat` on Windows).

## Verification commands

```bash
python3 -m unittest discover -s tests -q     # live-verified: 43 tests, all passing
python3 -m py_compile app.py builder/*.py   # compile check
git diff --check                             # whitespace check before committing
node tests/test_wizard.cjs                  # needs playwright-core + a Chromium executable
```

`tests/` currently contains `test_community.py`, `test_native_approval.py`,
`test_appimage_container.py`, `test_wizard.cjs`. Browser fixtures mock the API and never invoke
a real build or installer. Real toolchain builds require network access and are not runnable in a
sandbox without toolchains.

## Development conventions

- **Zero pip dependencies.** `app.py` and `builder/` must stay stdlib-only. Do not add a
  requirements file or third-party import.
- **Byte-compatibility with upstream.** `config_gen.py` must stay byte-for-byte identical to
  `patches/load-config-original.py` (`load-config.py`). Verify with a diff before changing.
- **Test before real builds.** Use dry-run / Preview plan (`POST /api/preflight`,
  `POST /api/build/start` with `dry_run`) to see the full command sequence without compiling.
- **Do not delete the workspace as a routine retry.** Build caches are preserved; source
  customizations must match the selected revision. Reuse is managed by `builder/orchestrator.py`.
- **Subprocess output is decoded utf-8 with `errors="replace"`** everywhere; executables are
  resolved via `shutil.which` plus `~/.cargo/bin`. Keep it that way.
- **API safety:** `/api/*` requests pass a local-request trust check (`_trusted_local_request`)
  and the server binds to `127.0.0.1`. Keep the local-only default; the farm is opt-in.
- **No secrets in committed configs.** `configs/RustDesk.json` here is an upstream sample;
  customer profiles (server, key, password, branding) belong in the private working copy, not here.
- Contribution flow: fork → feature branch → PR with the OS/target tested noted. The broader
  `docs/CONTRIBUTION-READY.md` lists the current branch scope; nothing is committed, pushed or
  commented automatically.

## Load-bearing invariants (do not violate)

1. **`custom_.txt` is base64, never raw JSON.** RustDesk's `read_custom_client()` calls
   `decode64()` first. This is the historical footgun.
2. **Compiled-in vs runtime split.** Server IP, public key, API server, app/company name, URLs and
   feature flags are `sed`-patched into the RustDesk **source** at build time
   (`config_gen.py` / `customize.py`). Password, permissions and approve mode are **runtime**,
   read from base64 `custom_.txt` next to the binary.
3. **Android never file-reads `custom_.txt`** — the base64 config is embedded into
   `MainService.kt` and `native_model.dart` and bundled as a Flutter asset.
4. `patches/allowCustom.py` strips a 9-line signature-verification block from `src/common.rs` and
   renames `custom.txt` → `custom_.txt`; without it an unsigned build will not load its config.
5. **Desktop builds are host-locked** (Windows→Windows, macOS→macOS, Linux→Linux). **Android is
   cross-platform** (any Linux/macOS host). Windows PCs cannot build APKs.
6. **Windows needs the MSVC linker (`link.exe`) for every target, including Android** — the
   `msbuild` requirement is an explicit admin install, excluded from bulk "install missing".
7. **Pinned versions:** RustDesk `1.4.9` / `1.5.0` (each pins its own vcpkg commit: `120deac` /
   `9e593bb`), Rust `1.75` (macOS `1.81`), Flutter `3.24.5`, LLVM `15.0.6`, NDK `r28c`, JDK `17`,
   flutter_rust_bridge_codegen `1.80.1`. Changing a pin changes the capability matrix and the
   `Setup-*` scripts.

## Module map (`builder/`)

| Module | Responsibility |
|---|---|
| `detect.py` | OS/CPU → capability matrix; target IDs (`windows-x86_64-{exe,msi}`, `macos-{arm64,x86_64,universal}-dmg`, `linux-{x86_64-deb,x86_64-rpm,x86_64-appimage,aarch64-deb}`, `android-{arm64,armv7,x86_64,universal}`) |
| `prereqs.py` | toolchain presence + install hints |
| `toolchains.py` | portable download/install registry; writes/loads `.toolchains/env.json` |
| `config_gen.py` | `configs/RustDesk.json` → `CUSTOM_*` env + base64 `custom_.txt` |
| `customize.py` | applies source patches, Android native embed, signature strip |
| `orchestrator.py` | checkout, bridge codegen, compile per target, verified artifacts, dry-run, cancel, live log |
| `environment.py` / `terminal_approval.py` | target-scoped requirements, explicit install, local Linux admin approval (Polkit; terminal fallback) — passwords never go through the browser or build log |
| `pub_config.py` / `artifacts.py` | dependency-state validation and artifact/architecture validation |
| `signing.py` | self-signed Windows/macOS/Android signing material |
| `appimage_container.py` | rootless Podman packaging image for Linux x86_64 |
| `precheck.py` | standalone toolchain probe |

`web/app.js` (UI + API client), `web/wizard.js` (wizard navigation), `web/build-view.js`
(progress + log filters), `web/advanced-keys.js`, `web/index.html`, `web/style.css`.
`farm/worker.py` claims jobs matching its OS; `farm/queue.py` is the HTTP queue
(`DVFORGE_FARM_TOKEN`), `farm/submit.py` submits a job.

## State to keep in mind

The current branch is `main`. Reliability, wizard UX, dependency approval and AppImage-container
work landed via PR #24. Acceptance is still open for: Windows runtime/display-driver behavior,
native ARM, DRM/unattended Wayland, generic worker scheduling, and a real AppImage build. The
docs describe what was *measured* here, not what is guaranteed on every platform — keep measured
results separate from claims. `docs/` (COMMUNITY-REPORT, ISSUE-23-UPDATE, CONTRIBUTION-READY,
REMOTE-ARCHITECTURE) is the authoritative history; `HANDOFF.md` is engineering context.
