# AGENTS.md — DVForge

DVForge compiles customized RustDesk clients locally. Pure-stdlib Python server (`app.py`, `127.0.0.1:8765`) + vanilla-JS GUI (`web/`) + build engine (`builder/`). No package manager, no pip install, no npm.

Deeper sources: `QWEN.md` is the machine-oriented map; `README.md` "For AI agents" section and `HANDOFF.md` hold architecture/history; `docs/` is the authoritative measured history. `.devin/skills/` are local-only (gitignored).

## Commands

```bash
python3 app.py                 # or ./run.sh (macOS/Linux), run.bat (Windows); --no-browser, RDLB_PORT
python3 -m unittest discover -s tests -q   # Python tests
node tests/test_wizard.cjs     # browser GUI test; needs playwright-core + a Chromium binary
python3 -m py_compile app.py builder/*.py  # compile check
git diff --check               # whitespace gate before committing
bash precheck.sh               # toolchain probe (precheck.bat on Windows)
bash clean.sh                  # artifact cleanup; keeps configs/ and workspace/branding/; --all deeper
```

First-time machine setup (idempotent, writes `.toolchains/env.json`): `Setup-DVForge-{macOS.sh,Ubuntu.sh,Windows.ps1,WSL2.ps1}`; uninstall counterparts `Uninstall-DVForge-*`.

Known state: on current `main`, `test_community.test_api_guard_rejects_cross_origin_and_nonloopback_host` fails — `_trusted_local_request` in `app.py` no longer rejects a foreign `Host` header (only Origin mismatches). Fix by re-adding the loopback-host check, don't weaken the test.

CI only builds/pushes the farm Docker image (`.github/workflows/docker-build-farm.yml`, context `farm/`); nothing validates Python changes — run the local checks yourself.

## Hard constraints (violating these breaks builds)

- **Stdlib only.** No third-party imports, no requirements file, in `app.py` and `builder/`.
- **`config_gen.py` must stay byte-for-byte identical to upstream `patches/load-config-original.py`.** Diff it before changing.
- **`custom_.txt` is base64, never raw JSON** — RustDesk's `read_custom_client()` decodes first. Historical footgun.
- **Compiled-in vs runtime split:** server/key/app-name/URLs/flags are sed-patched into RustDesk source (`config_gen.py`/`customize.py`); password, permissions, approve mode are runtime from base64 `custom_.txt`.
- **Android never file-reads `custom_.txt`** — config embedded into `MainService.kt` + `native_model.dart` as a Flutter asset.
- **Desktop targets are host-locked**; Android builds on any Linux/macOS host; Windows cannot build APKs but needs MSVC `link.exe` for every target (explicit admin install, excluded from bulk "install missing").
- **Pinned versions** (RustDesk 1.5.1 default / 1.5.0 / 1.4.9 / master; each ≥1.4.9 tag auto-discovered, per-tag vcpkg pin `9e593bb`/`120deac`; Rust 1.75, macOS 1.81; Flutter 3.24.5; LLVM 15.0.6; NDK r28c; JDK 17; flutter_rust_bridge_codegen 1.80.1). Changing a pin changes the capability matrix in `detect.py` and the Setup scripts.
- Server binds `127.0.0.1` with the local same-origin API guard (`_trusted_local_request`); farm is opt-in. No secrets in committed `configs/RustDesk.json` — customer profiles stay in private working copies.

## Architecture notes

- `app.py` loads `toolchains.apply_persisted_env(ROOT)` at startup **before** any toolchain detection — anything touching toolchains at import time must respect that order.
- `builder/` ownership: `detect.py` capability matrix · `prereqs.py` presence/hints · `toolchains.py` portable installs + `.toolchains/env.json` · `config_gen.py` config → `CUSTOM_*` + base64 txt · `customize.py` patches + Android embed + signature strip (`patches/allowCustom.py`) · `orchestrator.py` checkout/bridge codegen/compile/artifacts, dry-run, cancel · `environment.py`/`terminal_approval.py` Linux admin approval (passwords never through browser or build log) · `pub_config.py`/`artifacts.py` validation · `signing.py` self-signed material · `appimage_container.py` rootless Podman packaging.
- Subsystems: `farm/` (inbox/outbox shared-folder queue + `queue.py` HTTP mode with `DVFORGE_FARM_TOKEN`; own README), `printer-adapter/` (Rust crate replacing the signature-gated printer DLL; own README).
- Subprocess output is decoded utf-8 with `errors="replace"` everywhere; executables resolve via `shutil.which` + `~/.cargo/bin`. Keep it that way.
- Upstream RustDesk ≥ 1.5.2 braced the shell variables in `flutter/build_android_deps.sh` (`${VCPKG_ROOT}`, `${SCRIPTDIR}`) and nested the arm-neon block inside an `if`. `builder/orchestrator.py::_patch_android_deps_script` matches both braced and unbraced forms; when patching upstream scripts, keep replacement bodies valid bash (an `if` body can't be comment-only) and run `bash -n` on a patched copy.
- Don't delete `workspace/` as a routine retry — caches are reused and customizations must match the revision; reuse logic lives in `orchestrator.py`. Prefer dry-run (`POST /api/preflight`, build with `dry_run`) before real builds; real builds need network + installed toolchains and can't run in a sandbox.
- Test fixtures mock the API and never invoke real builds/installers.

## Contribution

Fork → feature branch → PR noting OS/targets tested. Nothing is committed/pushed automatically. Separate *measured* results from claims when touching `docs/`.
