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


def create_self_signed_mac_p12(dest_dir, appname, password, years=5):
    """Create a code-signing PKCS12 named {appname}.p12 for local macOS tests.

    Gatekeeper on other Macs still blocks this. Empty Config → ad-hoc signing.
    """
    os.makedirs(dest_dir, exist_ok=True)
    stem = pfx_stem(appname)
    cn = _cn(appname)
    p12_path = os.path.join(dest_dir, f"{stem}.p12")
    cer_path = os.path.join(dest_dir, f"{stem}-macos.cer")
    if not password:
        raise ValueError("P12 password is required")
    # Prefer openssl with 3DES/SHA1 MAC so `security import` on macOS accepts
    # the file (modern PKCS12 AES-MAC → "MAC verification failed").
    if shutil.which("openssl"):
        _create_openssl(p12_path, cer_path, cn, password, years)
    elif os.name == "nt":
        _create_windows(p12_path, cer_path, cn, password, years)
    else:
        _create_openssl(p12_path, cer_path, cn, password, years)
    if not os.path.isfile(p12_path) or os.path.getsize(p12_path) < 64:
        raise RuntimeError("P12 was not created")
    return {
        "ok": True,
        "path": p12_path,
        "filename": f"{stem}.p12",
        "cn": cn,
        "identity": cn,
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
        passout = f"pass:{password}"
        exports = [
            [openssl, "pkcs12", "-export", "-out", pfx_path,
             "-inkey", key_path, "-in", cer_path, "-passout", passout,
             "-keypbe", "PBE-SHA1-3DES", "-certpbe", "PBE-SHA1-3DES",
             "-macalg", "sha1"],
            [openssl, "pkcs12", "-export", "-out", pfx_path,
             "-inkey", key_path, "-in", cer_path, "-passout", passout,
             "-legacy"],
            [openssl, "pkcs12", "-export", "-out", pfx_path,
             "-inkey", key_path, "-in", cer_path, "-passout", passout],
        ]
        r = None
        for cmd in exports:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                break
        if r is None or r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "openssl pkcs12 failed")[:400]
                               if r else "openssl pkcs12 failed")
    finally:
        try:
            os.remove(key_path)
        except OSError:
            pass
    return ""


def find_keytool():
    """keytool.exe / keytool from PATH, JAVA_HOME, or DVForge .toolchains."""
    for name in ("keytool", "keytool.exe"):
        p = shutil.which(name)
        if p:
            return p
    homes = []
    for envk in ("JAVA_HOME", "JDK_HOME"):
        v = os.environ.get(envk) or ""
        if v:
            homes.append(v)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    homes.extend([
        os.path.join(root, ".toolchains", "java", "Contents", "Home"),
        os.path.join(root, ".toolchains", "java"),
    ])
    extra_roots = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     "Eclipse Adoptium"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Java"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     "Microsoft"),
        "/usr/lib/jvm",
        "/Library/Java/JavaVirtualMachines",
        "/opt/homebrew/opt/openjdk@17",
        "/usr/local/opt/openjdk@17",
    ]
    for base in extra_roots:
        if os.path.isdir(base):
            try:
                names = os.listdir(base)
            except OSError:
                names = []
            if os.path.isfile(os.path.join(base, "bin", "keytool")) or \
               os.path.isfile(os.path.join(base, "bin", "keytool.exe")):
                homes.append(base)
            for n in names:
                homes.append(os.path.join(base, n))
                homes.append(os.path.join(base, n, "Contents", "Home"))
    seen = set()
    for home in homes:
        if not home or home in seen:
            continue
        seen.add(home)
        nested = os.path.join(home, "Contents", "Home")
        if os.path.isdir(os.path.join(nested, "bin")):
            home = nested
        for exe in ("keytool.exe", "keytool"):
            p = os.path.join(home, "bin", exe)
            if os.path.isfile(p):
                return p
    return ""


def create_android_keystore(dest_dir, appname, password, alias="upload",
                            key_password="", years=27):
    """Create a PKCS12/JKS upload keystore named {appname}.jks.

    Free, local, same idea as the Windows self-signed PFX. Keep a backup:
    losing this file means you cannot update already-installed APKs.
    """
    os.makedirs(dest_dir, exist_ok=True)
    stem = pfx_stem(appname)
    cn = _cn(appname)
    alias = (alias or "upload").strip() or "upload"
    alias = re.sub(r"[^A-Za-z0-9._+-]+", "-", alias).strip(".-") or "upload"
    key_password = (key_password or password or "").strip()
    if not password:
        raise ValueError("keystore password is required")
    if len(password) < 6:
        raise ValueError("keystore password must be at least 6 characters")
    ks_path = os.path.join(dest_dir, f"{stem}.jks")
    prefix, via_wsl = _keytool_prefix()
    if not prefix:
        raise RuntimeError(
            "keytool not found. Install JDK 17 (Toolchain panel) and re-scan, "
            "or install a JDK in WSL if you build Android there.")
    if os.path.isfile(ks_path):
        os.remove(ks_path)
    validity = str(max(365, int(years) * 365))
    dname = f"CN={cn}"
    store = _wsl_path(ks_path) if via_wsl else ks_path
    cmd = prefix + [
        "-genkeypair", "-noprompt",
        "-keystore", store,
        "-storetype", "PKCS12",
        "-keyalg", "RSA", "-keysize", "2048",
        "-validity", validity,
        "-alias", alias,
        "-storepass", password,
        "-keypass", key_password,
        "-dname", dname,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        if os.path.isfile(ks_path):
            try:
                os.remove(ks_path)
            except OSError:
                pass
        cmd = [c for c in cmd if c != "-storetype" and c != "PKCS12"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "keytool failed").strip()[:400]
        raise RuntimeError(err)
    if not os.path.isfile(ks_path) or os.path.getsize(ks_path) < 64:
        raise RuntimeError("keystore was not created")
    return {
        "ok": True,
        "path": ks_path,
        "filename": f"{stem}.jks",
        "alias": alias,
        "cn": cn,
        "keytool": " ".join(prefix),
    }


def _wsl_path(win_path):
    ap = os.path.abspath(win_path)
    drive, rest = os.path.splitdrive(ap)
    if drive:
        return "/mnt/" + drive[0].lower() + rest.replace("\\", "/")
    return ap.replace("\\", "/")


def _keytool_prefix():
    """Return (argv_prefix, via_wsl). prefix is e.g. ['C:\\...\\keytool.exe'] or ['wsl','-e','keytool']."""
    kt = find_keytool()
    if kt:
        return [kt], False
    if os.name == "nt" and shutil.which("wsl"):
        r = subprocess.run(
            ["wsl", "-e", "bash", "-lc", "command -v keytool"],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode == 0 and r.stdout.strip():
            return ["wsl", "-e", r.stdout.strip()], True
    return None, False
