# DVForge reliability work: implementation and verification record

Date: 2026-10-07. Base and freshly fetched upstream `main`: `d150d4d`.

**Status: a tested reliability/wizard foundation, not completion of the entire roadmap.**
The generic remote controller, scoped worker credentials and DRM target are not
implemented. Driver runtime behavior is not verified. Do not describe those as
fixed or close their issues based on this report. The client's Connecting problem
was intentionally not investigated.

## Evidence levels

- Real previous user runs: seven macOS/Android outputs reported successful;
  Linux DEB, Fedora RPM and SUSE RPM listed in terminal output. These runs predate
  this community changeset and are not hardware verification of this changeset.
- Earlier isolated real probe: macOS SDK header parsing through the actual ffigen
  wrapper passed after disabling its automatic SDK include selection.
- Current automated verification: Python regression/unit tests; three Node config
  tests; headless Chromium five-step wizard on desktop and mobile, with mocked API
  responses. No RustDesk compilation or toolchain installation was run.
- Real read-only validation of the existing local Flutter package config passed
  for all 215 package roots. This checks the validator against an actual project;
  it is not a fresh pub/network resolution test.
- Windows EXE/MSI, native Linux ARM64, Fedora, Arch, Intel macOS, driver installation,
  DRM/Wayland and actual worker transport still require platform acceptance tests.

## Existing repairs retained

The previous local changes were reviewed against upstream before editing. Upstream
had not advanced beyond the local base; none of these local fixes can be called
already merged upstream.

| Issue 23 point | Confirmed finding / root cause | Repair and verification boundary |
| --- | --- | --- |
| 1 | Flutter executable discovery/PATH failures | Prefer selected portable SDK; pinned version checks. Previous successful user builds; new detector behavior mocked. |
| 2 | Flutter startup lock reported | No forced lock deletion. A live competing Flutter process remains an environment condition, not a proved compiler defect. |
| 3 | Dry-run incorrectly required generated package config | Dry-run prints commands without requiring pub files. Automated no-write tests pass. |
| 4 | Old bridge codegen rejects unsupported RUST_LOG | Preserve compatible codegen logging. Previous real generation progressed past the panic. |
| 5-6 | Both directions of Dart/Rust vs libclang CPU mismatch appeared in old logs | Preserve host-aware SDK/LLVM selection. Intel hardware not retested. |
| 7-8 | SDK headers / ffigen typedef errors | SDK include handling and ffigen wrapper retained; real isolated probe passed. |
| 9-10 | Missing bridge files and cascading IntoIntoDart failures | Strict diagnostics, required outputs and validation fingerprint. New cache reuse also requires valid pub package roots. |
| 11-12 | Flutter API / package compatibility errors | Retain Flutter 3.24.5, not a blind SDK/dependency upgrade. Successful older user runs do not establish all 1.5.0 compatibility. |
| 13 | Old macOS deployment targets conflict with newer SDK | Preserve explicit deployment target and Podfile/Xcode repairs. No new DMG build this round. |
| 14 | sdkmanager Java classpath split at spaces | Retain list-argument Java invocation; regression test covers one classpath argument. |
| 15 | Native filesystem operation unsupported | Observed; exact failing syscall not proved. Local-workspace migration is a mitigation, not proof of a particular syscall cause. |
| 16-17 | Native tool flags / paths with spaces failed | Preserve quoting and real-path guard. Arbitrary native source paths containing spaces are still unsupported. |
| 18 | Gradle checksum initialization reported unsupported operation | Exact cause still unconfirmed without a detailed stack trace. Do not label this OOM. |
| 19 | FFmpeg overlay explicitly rejects source paths containing spaces | Reject unsupported real source paths before expensive work; do not pretend a symlink fixes it. |
| 20 | Failure could end without genuine final artifacts | Strict commands, required artifact sets, architecture checks and failures for missing artifacts. Mock/unit verified; new full builds pending. |

Other confirmed previous defects: global Gradle cache deletion during dry-run,
over-aggressive Cargo cache deletion on retry, vcpkg readiness based on one header,
universal APK ABI omissions, simultaneous split/universal output omissions, and
patch-already-applied confused with patch failure. Existing repairs and tests remain.

HTTP BrokenPipe after a disconnected browser is distinct from a compiler crash.
The earlier disconnect guards remain. Customer config import/app IDs, portable
paths and local relocation fixes remain private integration work, not generic
community defaults. Source configuration is never evidence that server fallback
or client connectivity works at runtime.

## Changes in this round

### Dependency policy

`builder/environment.py` computes dependencies from selected known targets.
Reporting is the API/CLI default; installation requires an explicit option.
The wizard starts with automatic installation enabled, but installation only begins
when Prepare environment or Start build is clicked. Already valid tools are skipped.
Supported portable installers are reused, with their existing JDK-before-SDK order.
Every installation is verified again. SDK detection no longer changes its executable
permissions; explicit preparation/build retains the existing repair path.
The first critical failure stops preparation
and prevents calling the RustDesk build.

Linux system package recipes are selected using `ID`/`ID_LIKE`, not a display name:
apt on Debian derivatives, dnf on Fedora/RHEL derivatives, pacman on Arch derivatives.
Non-root installs use `sudo -n` so the web server cannot hang at a password prompt.
macOS native tools use Homebrew. Selected Windows tools use winget or existing
official/portable installers. No wholesale upgrade, `sudo pip`, cache repair or
global package reinstall is automatically attempted.

Not all dependencies have safe automatic installers. Full Xcode, some Windows tools,
unsupported distros and AppImage's legacy apt-key environment may require manual
preparation. Failed PATH rediscovery is a failure, not a claimed successful install.
The package recipes were code-reviewed/mocked, not executed on Fedora/Arch/Windows.
Checks are not yet a complete host-binary CPU attestation system; actual platform
build stages retain their architecture checks. Capability registration remains open.

Report only:

```sh
python3 -m builder.environment linux-x86_64-deb
```

Explicit install and verify:

```sh
python3 -m builder.environment --install linux-x86_64-deb
```

Run these inside the actual local DVForge checkout, not on the NAS source tree.

### Pub resolution / issue 18

The previous package-config check only established file existence. New resolution
moves an existing file to `.dvforge-previous`, invokes checked `flutter pub get`,
and requires JSON schema 2, non-empty unique packages, local existing package roots,
and the Flutter/ffigen packages. Failure never restores the old file as current
successful output. A backup remains for recovery; a successful resolution removes
that backup. No global pub cache is deleted. Dry-run does not move/read/require it.
Command failures carry a bounded tail of actual combined stdout/stderr into results.
The ineffective unverified `dart pub get` fallback was removed.

This fixes known orchestration/validation failure modes; it does not prove that the
reporter's Fedora or Intel macOS installation, proxy or dependency solver is repaired.

### Linux packaging and output

Cargo and Flutter Linux failures now stop packaging. The executable's ELF machine
must match the requested native architecture. DEB metadata and RPM/AppImage
architecture are checked before collection. RPM builds use an isolated workspace
packaging root/database, rather than choosing an old file from `~/rpmbuild`.
AppImage accepts exactly one expected fresh version/architecture file, not any old
AppImage. Selected packaging tools/specs/recipes are mandatory. Successful DEB/RPM
files are collected promptly so a later packaging failure does not erase success.

AppImage's external legacy apt-key dependency on Debian 13 is still unresolved.
No dummy apt-key shim, global pip install or warning suppression was added.

### Windows/display driver

The inspected RustDesk 1.4.9 `build.py` already builds and copies the virtual-display
helper DLL. DVForge now checks Cargo explicitly before delegating to that script,
checks the helper DLL and application PE architecture before packaging, and treats
missing portable EXEs as failure. Portable dependency installation is checked;
stale expected packer/installer files are removed before packaging. MSI's expected
previous output is removed before MSBuild and collected MSI success is tracked.

These are packaging safeguards, not proof that a display driver is installed or
called correctly. Driver installation can require Windows administrator rights and
valid kernel-driver signatures. No certificates, test-signing mode or driver trust
policy are changed. See the separate issue draft.

### Wizard

Five stages reuse the existing form, board, installer session and build session:
Environment, Config, Targets, Review, Build. Environment Next requires a usable
native desktop environment. Config Next saves/validates basic form input. Review
shows host, version, application, target routes and canonical output paths.
Missing dependencies can be installed at explicit build start; report-only failures
stop the backend before compilation. Unsupported ARM remote readiness is no longer
presented as local ready. No Windows-from-macOS cross compilation is claimed.

Logs show stage/group and elapsed time; results distinguish validated, failed,
cancelled, planned and not-started targets. This is not percentage-complete progress
or a full DAG scheduler. A shared-stage/platform failure can abort later targets;
unbuilt targets are not marked successful. Dry-run results say planned, not validated.

The local artifact download endpoint is confined to output installer files, resolves
symlinks and excludes `custom_.txt`/private configuration. Local API calls enforce
loopback Host and same-origin browser requests. These guards do not make the local
app a publicly deployable authenticated controller.

### Existing farm: limited hardening, not a new controller

Existing queue/worker helpers were inspected and reused. Remote HTTP worker URLs
require HTTPS and a token of at least 32 characters; URL credentials/query tokens
are rejected. Worker-started queues default to loopback. Non-loopback queue startup
requires a token. Header token comparison is constant-time. Artifact publication
includes SHA-256/size; the existing ARM receiver checks job/target, checksum and DEB
architecture, rejects empty success and copies into the new ARM output directory.

Checksums detect corruption, not a malicious authenticated/shared-folder writer.
Queue tokens are still shared, not scoped to each worker. File queues rely on share
ACLs. HTTPS termination is external. The GUI dependency policy rejects remote-only
targets because online capabilities are not implemented yet. This deliberately
prevents another four-hour optimistic ARM queue wait, but is a migration break for
legacy GUI farm users. See REMOTE-ARCHITECTURE.md for the required next work.

### Deprecations

DVForge's tar extraction now explicitly uses Python's data filter; an interpreter
without that security filter fails with an actionable message. External Flutter,
Dart, Gradle, RustDesk and native-library warnings were not suppressed/upgraded.
No obsolete RustDesk override keys were guessed or removed: their exact versioned
runtime semantics still require a separate audit. Windows WMIC fallback remains;
replacing it is not necessary for the current compiler fixes.

## Output migration

New layout: `workspace/output/<os>/<version>/<architecture>/<artifact>`.
OS: windows, linux, macos, android. Architectures: x86_64, aarch64, armv7, universal.
Version does not carry a `v` prefix. Normal and quick client filenames remain distinct.

Legacy `output/v1.4.9` remains untouched and is historical output, not fresh evidence.
No automatic move/reclassification of old artifacts is performed. Filename alone
cannot prove architecture. Update consuming scripts only after validating old package
metadata. Clean/uninstall tools already target the output root and still work.
UI/API consumers should use returned absolute artifact paths, not reconstruct them.
Old workers must be updated together with the controller to publish checksums.
Do not change old investigation logs merely because their historical paths differ.

## Files

New: `builder/artifacts.py`, `builder/pub_config.py`, `builder/environment.py`,
`web/wizard.js`, `tests/test_community.py`, `tests/test_wizard.cjs`, this report,
`docs/GITHUB-DRAFTS.md`, `docs/REMOTE-ARCHITECTURE.md`.

Modified this round: `app.py`, `builder/detect.py`, `builder/orchestrator.py`,
`builder/prereqs.py`, `builder/toolchains.py`, `farm/queue.py`, `farm/worker.py`,
`web/app.js`, `web/index.html`, `web/style.css`, `tests/test_build_safety.py`,
`README.md`, `HANDOFF.md`, local `ECZ-START.md`.
Prior modifications remain; see git status rather than treating every dirty file
as a new change. No commits, issue comments or PRs have been published.

## Validation and remaining acceptance gates

Commands: `python3 -m unittest discover -s tests -q`; Node
`--test tests/test_config_ui.cjs`; Node `tests/test_wizard.cjs` with playwright-core
and a Chromium executable. Browser fixtures never invoke a real build or installer.
Latest passing totals: 66 Python tests, 3 Node configuration tests, desktop/mobile
wizard traversal and the missing-environment navigation gate. Python compile checks
and git whitespace checks also passed. These are not actual compiled clients.

Coverage: installed skip; missing report; installer error; installer exit-zero but
invalid tool; quoted/spaced temporary paths; command diagnostics; stale/corrupt pub;
dry-run; multiple Android/macOS variants; missing artifacts; wrong DEB/ELF/PE arch;
output path traversal/symlink escape; backend fail-before-build; TLS/token policy;
unavailable ARM route; wizard desktop/mobile navigation and no horizontal overflow.

Pending: clean real builds with this revision, actual package-manager installs,
real remote job lifecycle/offline/cancel/logs, cryptographic per-worker scopes,
DRM capture/packaging, driver/headless Windows behavior, complete deprecation/override
audit, full invalid-form wizard gating, exact Gradle filesystem diagnosis, runtime
Connecting. This changeset is not a cross-platform release certification.

## Suggested reviewable commits / PRs

1. `fix: validate fresh pub resolution and bridge outputs` (issue 18 + codegen fixes).
2. `fix: enforce native build and artifact validation` (Android/macOS preserved,
   Linux strict packaging, Windows DLL/EXE safeguards, issue 21 safeguards).
3. `feat: add target-scoped dependency preparation` (environment module/API).
4. `feat: add five-step wizard and versioned artifact paths` (UI, download, migration).
5. `security: harden legacy farm transport and artifact integrity` (separate PR;
   does not claim generic workers or per-worker scoped tokens).
6. `docs: publish verification record and issue follow-ups` (sanitized docs/tests).

Exclude private config JSONs, backups, credentials/keys/certificates, branding,
customer server patches, ECZ import/transfer/ARM-container scripts, workspace,
toolchains, installed packages, screenshots of private config, generated clients,
customer-specific profile policy and the Connecting investigation. Keep DRM and
the controller/capability subsystem separate from reliability fixes.
