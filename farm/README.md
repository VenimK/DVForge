# Two-machine farm (your PCs only)

Shared **inbox / outbox** on this NAS copy of DVForge. Each PC runs its own DVForge on localhost and a worker that only claims jobs it can actually build.

```
You drop a job  →  farm/inbox/*.json
Mac worker      →  macos-*-dmg
Windows worker  →  windows-*-exe / .msi
Finished files  →  farm/outbox/<job-id>/
```

No public website. No stranger builders. DVForge stays on `127.0.0.1`.

## 1. Same folder on both PCs

This tree is already on the Synology share. Mount it on Windows too, e.g.

`\\192.168.1.114\downloads\MusicLover\RustDesk\Buildwithconfig\test\dvforge`

Both workers must see the same `farm\` directory.

## 2. Mac mini

Terminal A:

```bash
cd /Volumes/downloads/MusicLover/RustDesk/Buildwithconfig/test/dvforge
python3 app.py --no-browser
```

Terminal B:

```bash
python3 farm/worker.py
```

## 3. Windows PC

Keep DVForge’s **toolchains on the PC** (`run.bat` as usual). Scripts can live in `C:\DVForge\farm`. The **inbox** must still be the NAS folder (same as the Mac), or jobs submitted from the Mac never show up.

```bat
cd C:\DVForge
run.bat
```

Other window — copy `windows-worker.bat` from the NAS `farm\` into `C:\DVForge\farm\` and double-click it, or:

```bat
set DVFORGE_URL=http://127.0.0.1:8765
set DVFORGE_FARM=\\192.168.1.114\downloads\MusicLover\RustDesk\Buildwithconfig\test\dvforge\farm
python C:\DVForge\farm\worker.py
```

The first log line must say `farm=\\192.168.1.114\...` not `farm=C:\DVForge\farm`.

## 3b. Linux PC

Same idea: DVForge on localhost, worker watching the **NAS** `farm` folder.

Mount the share (NFS or CIFS), e.g.:

```bash
sudo mkdir -p /mnt/downloads
sudo mount -t nfs 192.168.1.114:/volume1/downloads /mnt/downloads
# or: mount -t cifs //192.168.1.114/downloads /mnt/downloads -o username=...
```

Terminal A — start DVForge from your Linux clone (`~/DVForge` or the NAS tree):

```bash
cd ~/DVForge   # or /mnt/downloads/MusicLover/RustDesk/Buildwithconfig/test/dvforge
./run.sh
# or: python3 app.py --no-browser
```

Terminal B:

```bash
export DVFORGE_URL=http://127.0.0.1:8765
export DVFORGE_FARM=/mnt/downloads/MusicLover/RustDesk/Buildwithconfig/test/dvforge/farm
python3 /mnt/downloads/MusicLover/RustDesk/Buildwithconfig/test/dvforge/farm/worker.py
```

Or copy `farm/linux-worker.sh` and edit `DVFORGE_FARM` inside it, then `bash linux-worker.sh`.

The first log line must be:

`claiming linux-*, android-*`

Linux takes `.deb` / `.rpm` / `.AppImage` and Android APKs. It will **not** take Mac or Windows jobs.

## 3c. Easiest worker setup (no NAS mount)

Workers can talk to `https://api.nas86.eu` over HTTP instead of a shared folder. Open **https://api.nas86.eu/join**, pick the OS, paste the token, copy the command.

Pass the URL and token as flags (works in PowerShell, cmd, and bash). Do **not** use `set VAR=` / `%VAR%` in PowerShell — those stay literal.

Windows (PowerShell):

```powershell
cd C:\DVForge\farm
python worker.py --queue "https://api.nas86.eu" --token "pick-a-secret"
```

Linux (oxicloud):

```bash
# DVForge already running on 127.0.0.1:8765
cd /opt/rustdesk-builder/Buildwithconfig/test/DVForge/farm
python3 worker.py --queue "https://api.nas86.eu" --token "pick-a-secret"
```

Copy the latest `farm/worker.py` onto that machine if it is an older clone. Restart `queue.py` so `/claim` exists. Bump nginx `client_max_body_size` to `80m` (see `nginx-api.nas86.eu.conf`) so `.dmg` uploads succeed.

## 4. Submit jobs (from either PC)

```bash
# Mac job — only the Mac worker will take it
python3 farm/submit.py --targets macos-arm64-dmg

# Windows job — only the Windows worker will take it
python farm\submit.py --targets windows-x86_64-exe

# Plan only (no compile)
python3 farm/submit.py --targets macos-arm64-dmg --dry-run
```

`--once` on the worker processes a single matching job and exits (good for a first test).

## 5. Collect

`farm/outbox/<job-id>/` has the installer plus `status.json` (`ok`, worker name, seconds).  
Failures go to `farm/failed/`.

One job = one OS. Don’t put `macos-arm64-dmg` and `windows-x86_64-exe` in the same JSON.

## 6. Curl a JSON file onto the inbox

Workers still have no HTTP port. Run **queue.py** on the Mac (it can write the NAS `inbox/`):

```bash
export DVFORGE_FARM_TOKEN=pick-a-secret
python3 farm/queue.py --host 0.0.0.0 --port 8766
```

Someone on the LAN:

```bash
curl -sS -X POST \
  'http://MAC_LAN_IP:8766/job?targets=windows-x86_64-exe&version=1.4.9' \
  -H 'Authorization: Bearer pick-a-secret' \
  -H 'Content-Type: application/json' \
  --data-binary @RustDesk.json
```

macOS job: `?targets=macos-arm64-dmg`. List jobs: `curl -H 'Authorization: Bearer pick-a-secret' http://MAC_LAN_IP:8766/jobs`

Do not put port 8766 on the public internet without nginx HTTPS + a token. DVForge itself stays on `127.0.0.1:8765`.

## 7. nginx: `api.nas86.eu` (queue) vs `creator.nas86.eu` (DVForge UI)

Publish **only the queue**. Workers keep talking to local DVForge.

On the NAS (same box as nginx):

```bash
export DVFORGE_FARM=/volume1/downloads/MusicLover/RustDesk/Buildwithconfig/test/dvforge/farm
export DVFORGE_FARM_TOKEN=pick-a-secret
python3 $DVFORGE_FARM/queue.py --host 127.0.0.1 --port 8766
```

Install `farm/nginx-api.nas86.eu.conf` into nginx and add a DNS A record for `api.nas86.eu`.

**Anyone:** open `https://api.nas86.eu/` — pick Windows / Mac / Android, upload `RustDesk.json`, wait, download.

Save the config in DVForge first. That embeds icon, logo, and signing **files** (`iconbase64`, `logobase64`, `signMacP12Base64`, …) in the JSON so a remote worker can recreate `workspace/branding/` and `workspace/signing/`. Apple **Developer ID** / Keychain identities are not files — they must already exist on the Mac worker. Passwords still travel in the JSON.

Workers need the updated `builder/config_gen.py` (restart DVForge there) so `POST /api/config` unpacks those blobs.

Workers are rated from job results. A new machine starts at 50%. Higher-rated idle workers of the same OS get unassigned jobs first. A worker with **zero successes after 2+ jobs**, or **5 failures in a row**, is skipped until you reset it:

```bash
curl -sS -X POST https://api.nas86.eu/worker/reset \
  -H 'Authorization: Bearer pick-a-secret' \
  -H 'Content-Type: application/json' \
  -d '{"name":"DESKTOP-MVN28Q7"}'
```

Pin a machine (skips rating; useful after a fix):

```bash
curl -sS -X POST \
  'https://api.nas86.eu/job?targets=windows-x86_64-exe&assign=DESKTOP-MVN28Q7' \
  -H 'Authorization: Bearer pick-a-secret' \
  -H 'Content-Type: application/json' \
  --data-binary @RustDesk.json
```

The submit form has a Worker dropdown (default: first available). Reset buttons are on `https://api.nas86.eu/status`.

Live stats (no job ids, no configs):

```bash
curl -sS https://api.nas86.eu/health
curl -sS https://api.nas86.eu/stats
```

Browser: `https://api.nas86.eu/status` — which workers are online, queue depth, recent builds. Workers appear after they POST `/claim` (within ~5s). Restart `queue.py` after updating it.

Curl still works:

```bash
curl -sS -X POST \
  'https://api.nas86.eu/job?targets=windows-x86_64-exe&version=1.4.9' \
  -H 'Authorization: Bearer pick-a-secret' \
  -H 'Content-Type: application/json' \
  --data-binary @RustDesk.json
```

If `creator.nas86.eu` already proxies DVForge `:8765`, anyone who can open that site can also `POST /api/build/start`. Prefer blocking `/api/build/` on that vhost (snippet in the nginx file) and letting `api.nas86.eu` be the only public job entry.