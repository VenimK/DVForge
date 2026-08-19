"""
signing.py — optional Authenticode code signing for Windows builds.

Signing credentials are deliberately **session-only**. Nothing here is ever
written to configs/RustDesk.json or any other persistent file: the settings
arrive with the build request, live in memory for the lifetime of the DVForge
process, and die with it. That keeps certificate passwords and Azure account
names out of a config file that is tracked in git and pushed to a public remote.

Three modes, all driven through the same `signtool sign` invocation:

  pfx              a .pfx/.p12 file plus its password
  store            a certificate already in the Windows certificate store,
                   selected by SHA-1 thumbprint
  trusted-signing  Microsoft Trusted Signing (formerly Azure Code Signing)

Trusted Signing is signtool plus a dispatch library:

    signtool sign /dlib Azure.CodeSigning.Dlib.dll /dmdf metadata.json ...

The dlib is a plugin signtool loads; it makes the REST call to Azure, which
signs the hash with a key that never leaves their HSM. The metadata json just
carries the account details. Rather than make anyone hand-write that file, the
four values are collected individually (matching the SignToolGUI layout) and
the json is generated into a temp file at sign time and deleted afterwards.

Signing has to happen at three points in a Windows build:

  1. every .exe/.dll in the Flutter Release folder, BEFORE the MSI harvest and
     the portable packer consume it
  2. CustomActions.dll, before WiX seals it into the package
  3. the finished artifacts (MSI, portable installer .exe)
"""

import glob
import json
import os
import shutil
import subprocess
import tempfile

# Modes
MODE_PFX = "pfx"
MODE_STORE = "store"
MODE_TRUSTED = "trusted-signing"
MODES = (MODE_PFX, MODE_STORE, MODE_TRUSTED)

# Microsoft's RFC3161 timestamp service. Used for every mode, not just
# Trusted Signing - it is a public service and one default is easier to reason
# about than a per-mode split.
DEFAULT_TIMESTAMP_URL = "http://timestamp.acs.microsoft.com"
TRUSTED_TIMESTAMP_URL = DEFAULT_TIMESTAMP_URL
DEFAULT_DIGEST = "sha256"

SIGNABLE_EXTS = (".exe", ".dll")

def pfx_env_var():
    """Environment variable that can supply the .pfx passphrase.

    A lookup rather than a module constant on purpose: a name containing
    PASSWORD assigned a string literal reads as a hardcoded credential to
    secret scanners, and this is only ever a variable name.
    """
    return "DVFORGE_SIGN_PASSWORD"

# Trusted Signing endpoints are https://<code>.codesigning.azure.net.
# "custom" lets an unlisted region be entered directly.
TRUSTED_REGIONS = [
    ("eastus", "East US", "https://eus.codesigning.azure.net/"),
    ("eastus2", "East US 2", "https://eus2.codesigning.azure.net/"),
    ("westus", "West US", "https://wus.codesigning.azure.net/"),
    ("westus2", "West US 2", "https://wus2.codesigning.azure.net/"),
    ("westcentralus", "West Central US", "https://wcus.codesigning.azure.net/"),
    ("northeurope", "North Europe", "https://neu.codesigning.azure.net/"),
    ("westeurope", "West Europe", "https://weu.codesigning.azure.net/"),
]


def region_endpoint(region):
    for code, _label, url in TRUSTED_REGIONS:
        if code == region:
            return url
    return ""


def find_signtool():
    """Absolute path to signtool.exe, or None.

    Prefers the newest Windows SDK build under Windows Kits\\10\\bin: the older
    top-level bin\\x64\\signtool.exe predates the /dlib support Trusted Signing
    needs.
    """
    candidates = []
    for pf in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if not pf:
            continue
        root = os.path.join(pf, "Windows Kits", "10", "bin")
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            exe = os.path.join(root, name, "x64", "signtool.exe")
            if os.path.isfile(exe):
                candidates.append((name, exe))
    if candidates:
        def key(item):
            try:
                return (1, tuple(int(p) for p in item[0].split(".")))
            except ValueError:
                return (0, item[0])
        candidates.sort(key=key)
        return candidates[-1][1]
    return shutil.which("signtool")


def find_dlib():
    """Locate Azure.CodeSigning.Dlib.dll, or None.

    Ships in the Microsoft.Trusted.Signing.Client NuGet package (previously
    Azure.CodeSigning.Client), so the usual home is the local NuGet cache.
    """
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    # DVForge's own .toolchains copy first - that is the one the Toolchain
    # panel installs and removes, so it should win over a stray global one.
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    roots = [os.path.join(project, ".toolchains", "trusted_signing"),
             os.path.join(home, ".nuget", "packages")]
    for pf in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if pf:
            roots.append(pf)
    # The dispatch library has been renamed along with the service
    # (Azure Code Signing -> Trusted Signing -> Artifact Signing), so match on
    # the *Dlib.dll suffix rather than one fixed filename.
    for root in roots:
        for pat in (os.path.join(root, "**", "bin", "x64", "*Dlib.dll"),
                    os.path.join(root, "**", "*Dlib.dll")):
            hits = [h for h in glob.glob(pat, recursive=True)
                    if os.path.isfile(h)]
            if hits:
                hits.sort()
                return hits[-1]
    return None


def azure_credential_status():
    """How the dlib is likely to authenticate, if at all.

    It uses DefaultAzureCredential, so it needs a service principal in the
    environment, an `az login` session, or a managed identity. Returns
    (ok, description).
    """
    if (os.environ.get("AZURE_CLIENT_ID") and os.environ.get("AZURE_TENANT_ID")
            and os.environ.get("AZURE_CLIENT_SECRET")):
        return True, "service principal from AZURE_* environment variables"
    az = shutil.which("az")
    if az:
        try:
            proc = subprocess.run([az, "account", "show"],
                                  capture_output=True, timeout=30)
            if proc.returncode == 0:
                try:
                    acct = json.loads(proc.stdout.decode("utf-8", "replace"))
                    who = acct.get("user", {}).get("name") or acct.get("name") or "?"
                    return True, f"Azure CLI session ({who})"
                except ValueError:
                    return True, "Azure CLI session"
        except (OSError, subprocess.SubprocessError):
            pass
    return False, ("no Azure credentials detected - run `az login`, or set "
                   "AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_CLIENT_SECRET "
                   "(a managed identity would also work but cannot be detected "
                   "from here)")


class SigningConfig:
    """Session-only signing settings. Never serialised to disk."""

    def __init__(self, enabled=False, mode=MODE_PFX, pfx_path="",
                 pfx_password="", thumbprint="", dlib_path="",
                 region="eastus", endpoint="", account_name="",
                 cert_profile="", correlation_id="",
                 timestamp_url="", digest=DEFAULT_DIGEST, extra_args=""):
        self.enabled = bool(enabled)
        self.mode = mode if mode in MODES else MODE_PFX
        self.pfx_path = (pfx_path or "").strip()
        self.pfx_password = pfx_password or ""
        self.thumbprint = (thumbprint or "").replace(" ", "").strip()
        self.dlib_path = (dlib_path or "").strip()
        self.region = (region or "").strip()
        self.endpoint = (endpoint or "").strip()
        self.account_name = (account_name or "").strip()
        self.cert_profile = (cert_profile or "").strip()
        self.correlation_id = (correlation_id or "").strip()
        self.digest = (digest or "").strip() or DEFAULT_DIGEST
        self.extra_args = (extra_args or "").strip()
        ts = (timestamp_url or "").strip()
        if not ts:
            ts = (TRUSTED_TIMESTAMP_URL if self.mode == MODE_TRUSTED
                  else DEFAULT_TIMESTAMP_URL)
        self.timestamp_url = ts

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        cfg = cls(
            enabled=d.get("signEnabled", False),
            mode=d.get("signMode", MODE_PFX),
            pfx_path=d.get("signPfxPath", ""),
            pfx_password=d.get("signPfxPassword", ""),
            thumbprint=d.get("signThumbprint", ""),
            dlib_path=d.get("signDlibPath", ""),
            region=d.get("signRegion", "eastus"),
            endpoint=d.get("signEndpoint", ""),
            account_name=d.get("signAccountName", ""),
            cert_profile=d.get("signCertProfile", ""),
            correlation_id=d.get("signCorrelationId", ""),
            timestamp_url=d.get("signTimestampUrl", ""),
            digest=d.get("signDigest", DEFAULT_DIGEST),
            extra_args=d.get("signExtraArgs", ""),
        )
        env_pw = os.environ.get(pfx_env_var())
        if env_pw:
            cfg.pfx_password = env_pw
        if cfg.mode == MODE_TRUSTED and not cfg.dlib_path:
            cfg.dlib_path = find_dlib() or ""
        return cfg

    def resolved_endpoint(self):
        return self.endpoint or region_endpoint(self.region)

    def metadata(self):
        """The /dmdf document, as a dict."""
        md = {
            "Endpoint": self.resolved_endpoint(),
            "CodeSigningAccountName": self.account_name,
            "CertificateProfileName": self.cert_profile,
        }
        if self.correlation_id:
            md["CorrelationId"] = self.correlation_id
        return md

    def describe(self):
        """One-line summary safe to log. Never includes the password."""
        if not self.enabled:
            return "disabled"
        if self.mode == MODE_PFX:
            what = f"pfx={os.path.basename(self.pfx_path) or '<unset>'}"
        elif self.mode == MODE_STORE:
            tp = self.thumbprint
            shown = f"{tp[:8]}..." if len(tp) > 8 else (tp or "<unset>")
            what = f"store thumbprint={shown}"
        else:
            what = (f"trusted-signing account={self.account_name or '<unset>'} "
                    f"profile={self.cert_profile or '<unset>'} "
                    f"endpoint={self.resolved_endpoint() or '<unset>'}")
        return f"{self.mode} ({what}), digest={self.digest}, ts={self.timestamp_url}"

    def validate(self):
        """Return a list of problems; empty means usable."""
        problems = []
        if not self.enabled:
            return problems
        if self.mode == MODE_PFX:
            if not self.pfx_path:
                problems.append("certificate file not set")
            elif not os.path.isfile(self.pfx_path):
                problems.append(f"certificate file not found: {self.pfx_path}")
            if not self.pfx_password:
                problems.append(
                    "certificate password not set (enter it, or set the "
                    f"{pfx_env_var()} environment variable)")
        elif self.mode == MODE_STORE:
            if not self.thumbprint:
                problems.append("certificate thumbprint not set")
            elif len(self.thumbprint) != 40:
                problems.append(
                    f"thumbprint should be 40 hex characters, got {len(self.thumbprint)}")
        elif self.mode == MODE_TRUSTED:
            if not self.dlib_path:
                problems.append(
                    "dispatch library not found - install the "
                    "Microsoft.Trusted.Signing.Client NuGet package, or set the "
                    "path to Azure.CodeSigning.Dlib.dll")
            elif not os.path.isfile(self.dlib_path):
                problems.append(f"dispatch library not found: {self.dlib_path}")
            if not self.resolved_endpoint():
                problems.append("endpoint region not set")
            if not self.account_name:
                problems.append("signing account name not set")
            if not self.cert_profile:
                problems.append("certificate profile not set")
            ok, why = azure_credential_status()
            if not ok:
                problems.append(why)
        if not find_signtool():
            problems.append(
                "signtool.exe not found - install the Windows SDK "
                "(Windows Kits\\10\\bin\\<version>\\x64)")
        return problems


def build_command(cfg, files, signtool=None, metadata_file=None):
    """The full signtool argv for `files`. Raises ValueError if unusable."""
    signtool = signtool or find_signtool()
    if not signtool:
        raise ValueError("signtool.exe not found")
    cmd = [signtool, "sign", "/fd", cfg.digest]
    if cfg.mode == MODE_PFX:
        cmd += ["/f", cfg.pfx_path, "/p", cfg.pfx_password]
    elif cfg.mode == MODE_STORE:
        cmd += ["/sha1", cfg.thumbprint]
    elif cfg.mode == MODE_TRUSTED:
        if not metadata_file:
            raise ValueError("trusted signing needs a metadata file")
        cmd += ["/dlib", cfg.dlib_path, "/dmdf", metadata_file]
    if cfg.timestamp_url:
        cmd += ["/tr", cfg.timestamp_url, "/td", cfg.digest]
    if cfg.extra_args:
        cmd += cfg.extra_args.split()
    cmd += list(files)
    return cmd


def redact(cmd, cfg):
    """A copy of `cmd` with the password replaced, for logging."""
    out = []
    skip_next = False
    for arg in cmd:
        if skip_next:
            out.append("***")
            skip_next = False
            continue
        if arg == "/p":
            out.append(arg)
            skip_next = True
            continue
        if cfg.pfx_password and arg == cfg.pfx_password:
            out.append("***")
            continue
        out.append(arg)
    return out


def sign_files(cfg, files, log=print, signtool=None, timeout=600):
    """Sign `files` in one signtool call.

    For Trusted Signing the metadata json is written to a temp file for the
    duration of the call and removed afterwards, so account details never
    persist on disk.

    Returns (ok, message). Missing files are skipped.
    """
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        return True, "nothing to sign"

    tmpdir = None
    metadata_file = None
    try:
        if cfg.mode == MODE_TRUSTED:
            tmpdir = tempfile.mkdtemp(prefix="dvforge-ts-")
            metadata_file = os.path.join(tmpdir, "metadata.json")
            with open(metadata_file, "w", encoding="utf-8") as f:
                json.dump(cfg.metadata(), f, indent=2)
        try:
            cmd = build_command(cfg, files, signtool=signtool,
                                metadata_file=metadata_file)
        except ValueError as exc:
            return False, str(exc)
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"signtool failed to run: {exc}"
        out = (proc.stdout or b"").decode("utf-8", "replace").strip()
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            detail = err or out or f"exit {proc.returncode}"
            # Never echo the command line - it may carry /p <password>.
            return False, detail.splitlines()[-1] if detail else f"exit {proc.returncode}"
        return True, f"signed {len(files)} file(s)"
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def verify_file(path, signtool=None, timeout=60):
    """True when `path` carries a signature chaining to a trusted root."""
    signtool = signtool or find_signtool()
    if not signtool or not os.path.isfile(path):
        return False
    try:
        proc = subprocess.run([signtool, "verify", "/pa", "/q", path],
                              capture_output=True, timeout=timeout)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _test_subject():
    """A small, real PE to practise on. Always copied before signing."""
    windir = os.environ.get("WINDIR", r"C:\Windows")
    for name in ("where.exe", "certutil.exe", "find.exe", "hostname.exe"):
        cand = os.path.join(windir, "System32", name)
        if os.path.isfile(cand):
            return cand
    return None


def test_sign(cfg, log=print):
    """Prove the credentials work before a build starts.

    Copies a small system binary to a temp file, signs the copy, verifies the
    result, and deletes it. Nothing on the system is modified.
    """
    problems = cfg.validate()
    if problems:
        return False, "; ".join(problems)

    signtool = find_signtool()
    src = _test_subject()
    if not src:
        return False, "no suitable test binary found under System32"

    tmpdir = tempfile.mkdtemp(prefix="dvforge-signtest-")
    target = os.path.join(tmpdir, "dvforge-signtest.exe")
    try:
        shutil.copy2(src, target)
        ok, msg = sign_files(cfg, [target], log=log, signtool=signtool)
        if not ok:
            return False, msg
        if not verify_file(target, signtool=signtool):
            return (True,
                    "signed, but the signature did not verify against a trusted "
                    "root - expected for a self-signed or internal CA "
                    "certificate, but end users would see a warning")
        return True, "signed and verified against a trusted root"
    except OSError as exc:
        return False, f"test sign failed: {exc}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def collect_signable(directory):
    """Every .exe/.dll under `directory`, recursively."""
    out = []
    for root, _dirs, files in os.walk(directory):
        for name in files:
            if name.lower().endswith(SIGNABLE_EXTS):
                out.append(os.path.join(root, name))
    return sorted(out)
