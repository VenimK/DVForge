"""Self-signed Windows Authenticode PFX for local distribution tests."""

import os
import re
import shutil
import subprocess
import tempfile


def pfx_stem(appname):
    """Safe filename stem from the app name (SIGNING → SIGNING)."""
    raw = (appname or "app").strip() or "app"
    s = re.sub(r"[^A-Za-z0-9._+-]+", "-", raw).strip(".-")
    return s or "app"


def _cn(appname):
    s = re.sub(r'[="\\]', "", (appname or "App").strip()) or "App"
    return s[:64]


def create_self_signed_pfx(dest_dir, appname, password, years=5):
    """Create a code-signing PFX named {appname}.pfx under dest_dir.

    Windows: New-SelfSignedCertificate (EKU code signing) + Export-PfxCertificate.
    Linux/macOS: openssl req + pkcs12 (best-effort).
    Returns dict: path, filename, relpath is filled by the caller.
    """
    os.makedirs(dest_dir, exist_ok=True)
    stem = pfx_stem(appname)
    cn = _cn(appname)
    pfx_path = os.path.join(dest_dir, f"{stem}.pfx")
    cer_path = os.path.join(dest_dir, f"{stem}.cer")
    if not password:
        raise ValueError("PFX password is required")

    if os.name == "nt":
        thumb = _create_windows(pfx_path, cer_path, cn, password, years)
        trusted = _trust_windows(cer_path)
    else:
        thumb = _create_openssl(pfx_path, cer_path, cn, password, years)
        trusted = False

    if not os.path.isfile(pfx_path) or os.path.getsize(pfx_path) < 64:
        raise RuntimeError("PFX was not created")
    return {
        "ok": True,
        "path": pfx_path,
        "filename": f"{stem}.pfx",
        "thumbprint": thumb or "",
        "trusted_locally": trusted,
        "cn": cn,
    }


def _create_windows(pfx_path, cer_path, cn, password, years):
    script = r"""
$ErrorActionPreference = 'Stop'
$pfxPath = $env:DVF_PFX
$cerPath = $env:DVF_CER
$cn = $env:DVF_CN
$years = [int]$env:DVF_YEARS
$pass = ConvertTo-SecureString -String $env:DVF_PASS -Force -AsPlainText
$subject = "CN=$cn"
Get-ChildItem Cert:\CurrentUser\My -ErrorAction SilentlyContinue |
  Where-Object { $_.FriendlyName -like 'DVForge self-signed*' -and $_.Subject -eq $subject } |
  ForEach-Object { Remove-Item $_.PSPath -DeleteKey -ErrorAction SilentlyContinue }
$cert = New-SelfSignedCertificate `
  -Type CodeSigningCert `
  -Subject $subject `
  -FriendlyName "DVForge self-signed code signing ($cn)" `
  -CertStoreLocation 'Cert:\CurrentUser\My' `
  -KeyExportPolicy Exportable `
  -KeySpec Signature `
  -KeyLength 2048 `
  -HashAlgorithm SHA256 `
  -NotAfter (Get-Date).AddYears($years)
Export-PfxCertificate -Cert $cert -FilePath $pfxPath -Password $pass | Out-Null
Export-Certificate -Cert $cert -FilePath $cerPath | Out-Null
Write-Output $cert.Thumbprint
"""
    env = os.environ.copy()
    env["DVF_PFX"] = pfx_path
    env["DVF_CER"] = cer_path
    env["DVF_CN"] = cn
    env["DVF_PASS"] = password
    env["DVF_YEARS"] = str(int(years))
    fd, ps1 = tempfile.mkstemp(suffix=".ps1", prefix="dvforge-pfx-")
    os.close(fd)
    try:
        with open(ps1, "w", encoding="utf-8") as f:
            f.write(script)
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", ps1],
            capture_output=True, text=True, env=env, timeout=60,
        )
    finally:
        try:
            os.remove(ps1)
        except OSError:
            pass
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()[:400]
        raise RuntimeError(f"New-SelfSignedCertificate failed: {err}")
    return (r.stdout or "").strip().splitlines()[-1].strip() if r.stdout else ""


def _trust_windows(cer_path):
    """Import the public cert so this user account treats the signature as Valid."""
    if not os.path.isfile(cer_path):
        return False
    ok = True
    for store in ("Root", "TrustedPublisher"):
        r = subprocess.run(
            ["certutil", "-user", "-addstore", store, cer_path],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            ok = False
    return ok


def _create_openssl(pfx_path, cer_path, cn, password, years):
    openssl = shutil.which("openssl")
    if not openssl:
        raise RuntimeError(
            "openssl not found. On Windows use the in-app button "
            "(PowerShell New-SelfSignedCertificate); on Linux install openssl.")
    days = max(1, int(years) * 365)
    key_path = pfx_path + ".key.tmp"
    try:
        req = [
            openssl, "req", "-new", "-x509", "-newkey", "rsa:2048",
            "-keyout", key_path, "-out", cer_path, "-days", str(days),
            "-nodes", "-subj", f"/CN={cn}",
            "-addext", "extendedKeyUsage=codeSigning",
        ]
        r = subprocess.run(req, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            req = [
                openssl, "req", "-new", "-x509", "-newkey", "rsa:2048",
                "-keyout", key_path, "-out", cer_path, "-days", str(days),
                "-nodes", "-subj", f"/CN={cn}",
            ]
            r = subprocess.run(req, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "openssl req failed")[:400])
        r = subprocess.run(
            [openssl, "pkcs12", "-export", "-out", pfx_path,
             "-inkey", key_path, "-in", cer_path, "-passout", f"pass:{password}"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "openssl pkcs12 failed")[:400])
    finally:
        try:
            os.remove(key_path)
        except OSError:
            pass
    return ""
