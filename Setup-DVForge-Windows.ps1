<#
.SYNOPSIS
  Sets up a native Windows build environment for DVForge (Windows .exe / .msi).

.DESCRIPTION
  Mirrors Setup-DVForge-WSL2.ps1, but for host Windows desktop builds (not WSL).

  Installs / verifies (idempotent -- safe to re-run):
    - Git, Python 3 (winget if missing)
    - Chocolatey (only if NuGet / ImageMagick installers need it)
    - LongPathsEnabled (HKLM) so Flutter/MSBuild deep paths are less fragile
    - DVForge at a short fixed root (default C:\DVForge) -- copy from this tree
      or git-clone when running standalone
    - Via builder/toolchains.py (same pins as the GUI):
        Rust 1.75 (MSVC), Flutter 3.24.5, LLVM/libclang 15, vcpkg (pinned),
        VS Build Tools 2022 (C++ / link.exe + MSBuild), .NET 8 SDK, NuGet,
        sccache 0.11.0, ImageMagick, (optional) JDK 17
    - Pins rustup default to 1.75-x86_64-pc-windows-msvc
    - Writes portable .toolchains/env.json (project-relative paths)

  Companion uninstaller: Uninstall-DVForge-Windows.ps1

.PARAMETER InstallRoot
  Where DVForge should live. Default: C:\DVForge (short path = reliable builds).

.PARAMETER InPlace
  Use this script's directory as InstallRoot (do not copy to C:\DVForge).

.PARAMETER RepoUrl
  Git clone URL when InstallRoot is empty and this folder is not a DVForge tree.
  Default: https://github.com/VenimK/DVForge.git

.PARAMETER SkipVsBuildTools
  Skip the large (~4-6 GB) Visual Studio Build Tools install.

.PARAMETER SkipOptional
  Skip sccache, ImageMagick, potrace (still installs core Windows build tools).

.PARAMETER WithJava
  Also install Temurin JDK 17 into .toolchains (only needed for Android-on-Windows).

.PARAMETER SkipLongPaths
  Do not set HKLM LongPathsEnabled.

.PARAMETER Force
  Overwrite / re-copy source files into InstallRoot when it already exists.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\Setup-DVForge-Windows.ps1

.EXAMPLE
  .\Setup-DVForge-Windows.ps1 -InPlace

.EXAMPLE
  .\Setup-DVForge-Windows.ps1 -SkipVsBuildTools
#>

#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$InstallRoot = 'C:\DVForge',
    [switch]$InPlace,
    [string]$RepoUrl = 'https://github.com/VenimK/DVForge.git',
    [switch]$SkipVsBuildTools,
    [switch]$SkipOptional,
    [switch]$WithJava,
    [switch]$SkipLongPaths,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Auto-elevate to Administrator if not already elevated
# ---------------------------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host 'Requesting Administrator privileges (UAC) ...' -ForegroundColor Yellow
    $scriptPath = $MyInvocation.MyCommand.Path
    if (-not $scriptPath) { $scriptPath = $PSCommandPath }
    $argList = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$scriptPath`"",
        '-InstallRoot', "`"$InstallRoot`"",
        '-RepoUrl', "`"$RepoUrl`""
    )
    if ($InPlace)           { $argList += '-InPlace' }
    if ($SkipVsBuildTools)  { $argList += '-SkipVsBuildTools' }
    if ($SkipOptional)      { $argList += '-SkipOptional' }
    if ($WithJava)          { $argList += '-WithJava' }
    if ($SkipLongPaths)     { $argList += '-SkipLongPaths' }
    if ($Force)             { $argList += '-Force' }
    try {
        Start-Process -FilePath 'powershell.exe' -ArgumentList ($argList -join ' ') `
            -Verb RunAs -Wait
    } catch {
        Write-Host '  [ERROR] UAC elevation was declined. Please re-run as Administrator.' -ForegroundColor Red
        exit 1
    }
    exit 0
}

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Skip($msg) { Write-Host "  [SKIP] $msg" -ForegroundColor Yellow }
function Write-Warn($msg) { Write-Host "  [WARN] $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "  [ERROR] $msg" -ForegroundColor Red }

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user    = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

function Test-Command($Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Test-DvForgeRoot($Path) {
    if (-not $Path) { return $false }
    return (Test-Path (Join-Path $Path 'app.py')) -and `
           (Test-Path (Join-Path $Path 'builder\toolchains.py'))
}

function Install-WingetPackage {
    param(
        [string]$Id,
        [string]$Label
    )
    if (-not (Test-Command 'winget')) {
        Write-Warn "winget not found - install $Label manually"
        return $false
    }
    Write-Host "  winget install $Id ..."
    $wingetArgs = @(
        'install', '--id', $Id, '-e',
        '--accept-source-agreements',
        '--accept-package-agreements',
        '--disable-interactivity'
    )
    & winget @wingetArgs
    $code = $LASTEXITCODE
    # 0 = ok; -1978335189 = already installed; -1978335212 = no applicable upgrade
    if ($code -in 0, -1978335189, -1978335212) {
        Write-Ok "$Label ready (winget exit $code)"
        Refresh-Path
        return $true
    }
    Write-Warn "winget install $Id exited $code"
    return $false
}

function Ensure-Chocolatey {
    if (Test-Command 'choco') {
        Write-Skip 'Chocolatey already installed'
        return $true
    }
    Write-Host '  Installing Chocolatey (needed for NuGet CLI / ImageMagick) ...'
    try {
        Set-ExecutionPolicy Bypass -Scope Process -Force
        [System.Net.ServicePointManager]::SecurityProtocol =
            [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
        Invoke-Expression ((New-Object System.Net.WebClient).DownloadString(
            'https://community.chocolatey.org/install.ps1'))
        Refresh-Path
        if (Test-Command 'choco') {
            Write-Ok 'Chocolatey installed'
            return $true
        }
    } catch {
        Write-Warn "Chocolatey install failed: $_"
    }
    return $false
}

function Copy-DvForgeTree {
    param(
        [string]$Source,
        [string]$Dest
    )
    # Exclude heavy / machine-local build debris; keep source and configs.
    $excludeDirs = @(
        'workspace\rustdesk-src\target',
        'workspace\rustdesk-src\flutter\build',
        'workspace\output',
        'builder\__pycache__',
        '.git'
    )
    New-Item -ItemType Directory -Path $Dest -Force | Out-Null
    Write-Host "  Copying $Source -> $Dest ..."
    $robolog = Join-Path $env:TEMP 'dvforge-robocopy.log'
    $roboArgs = @(
        $Source, $Dest, '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/nc', '/ns', '/np',
        '/R:2', '/W:2', "/LOG:$robolog",
        '/XD', '__pycache__', '.git'
    )
    foreach ($d in $excludeDirs) {
        $full = Join-Path $Source $d
        if (Test-Path $full) {
            $roboArgs += '/XD'
            $roboArgs += $full
        }
    }

    & robocopy @roboArgs | Out-Null
    $rc = $LASTEXITCODE
    # robocopy: 0-7 = success-ish
    if ($rc -ge 8) {
        Write-Err "robocopy failed (exit $rc). See $robolog"
        exit 1
    }
    Write-Ok "DVForge files at $Dest"
}

# ---------------------------------------------------------------------------
# Resolve install root + source
# ---------------------------------------------------------------------------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ScriptDir) { $ScriptDir = $PSScriptRoot }
$SourceRoot = $null
if (Test-DvForgeRoot $ScriptDir) {
    $SourceRoot = $ScriptDir
}

if ($InPlace) {
    if (-not $SourceRoot) {
        Write-Err '-InPlace requires this script to live inside a DVForge tree (app.py + builder/).'
        exit 1
    }
    $InstallRoot = $SourceRoot
}

$InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)

Write-Step 'DVForge Windows build environment setup'
Write-Host "  Install root:       $InstallRoot"
Write-Host "  Source tree:        $(if ($SourceRoot) { $SourceRoot } else { '(git clone)' })"
Write-Host "  VS Build Tools:     $(if ($SkipVsBuildTools) { 'skip' } else { 'install if missing' })"
Write-Host "  Optional tools:     $(if ($SkipOptional) { 'skip' } else { 'sccache + ImageMagick' })"
Write-Host "  JDK 17:             $(if ($WithJava) { 'yes' } else { 'no (Android-on-Windows only)' })"
Write-Host "  LongPathsEnabled:   $(if ($SkipLongPaths) { 'skip' } else { 'set if needed' })"
Write-Host ''

# ---------------------------------------------------------------------------
# Phase 1 - host prerequisites
# ---------------------------------------------------------------------------
Write-Step 'Phase 1: Host prerequisites (Git, Python, Chocolatey)'

# Git
if (Test-Command 'git') {
    Write-Skip "Git present: $((Get-Command git).Source)"
} else {
    if (-not (Install-WingetPackage -Id 'Git.Git' -Label 'Git')) {
        Write-Err 'Git is required. Install from https://git-scm.com/download/win and re-run.'
        exit 1
    }
    Refresh-Path
    if (-not (Test-Command 'git')) {
        Write-Err 'Git installed but not on PATH yet. Open a new admin PowerShell and re-run.'
        exit 1
    }
}

# Python 3
$py = $null
foreach ($cand in @('python', 'py')) {
    if (Test-Command $cand) {
        try {
            $ver = & $cand -c "import sys; print(sys.version)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver -match '^3\.') {
                $py = $cand
                break
            }
        } catch {}
    }
}
if ($py) {
    Write-Skip "Python present: $((Get-Command $py).Source)"
} else {
    if (-not (Install-WingetPackage -Id 'Python.Python.3.12' -Label 'Python 3.12')) {
        Write-Err 'Python 3 is required. Install from https://www.python.org/downloads/ (Add to PATH) and re-run.'
        exit 1
    }
    Refresh-Path
    $py = $null
    foreach ($cand in @('python', 'py')) {
        if (Test-Command $cand) { $py = $cand; break }
    }
    if (-not $py) {
        Write-Err 'Python installed but not on PATH yet. Open a new admin PowerShell and re-run.'
        exit 1
    }
}

# Prefer real python.exe over WindowsApps stub
try {
    $probe = & $py -c "import sys; print(sys.executable)" 2>$null
    if ($probe -match 'WindowsApps') {
        Write-Warn 'python resolves to WindowsApps stub; trying py -3'
        if (Test-Command 'py') {
            $py = 'py'
            $env:PY_PYTHON = '3'
        }
    }
} catch {}

Write-Ok "Using Python launcher: $py"

# Chocolatey (for nuget + imagemagick via toolchains)
Ensure-Chocolatey | Out-Null

# ---------------------------------------------------------------------------
# Phase 2 - Long paths
# ---------------------------------------------------------------------------
if (-not $SkipLongPaths) {
    Write-Step 'Phase 2: Win32 long paths'
    $regPath = 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem'
    $cur = 0
    try {
        $cur = (Get-ItemProperty -Path $regPath -Name LongPathsEnabled -ErrorAction SilentlyContinue).LongPathsEnabled
    } catch {}
    if ([int]$cur -eq 1) {
        Write-Skip 'LongPathsEnabled already 1'
    } else {
        New-ItemProperty -Path $regPath -Name LongPathsEnabled -Value 1 `
            -PropertyType DWORD -Force | Out-Null
        Write-Ok 'LongPathsEnabled set to 1 (reboot if some tools still fail on long paths)'
    }
} else {
    Write-Step 'Phase 2: Win32 long paths'
    Write-Skip 'skipped (-SkipLongPaths)'
}

# ---------------------------------------------------------------------------
# Phase 3 - Place DVForge at InstallRoot
# ---------------------------------------------------------------------------
Write-Step 'Phase 3: DVForge project files'

if (Test-DvForgeRoot $InstallRoot) {
    if ($SourceRoot -and (
            [IO.Path]::GetFullPath($SourceRoot).TrimEnd('\') -ieq
            [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\'))) {
        Write-Skip "Already running inside install root: $InstallRoot"
    } elseif ($Force -and $SourceRoot) {
        Copy-DvForgeTree -Source $SourceRoot -Dest $InstallRoot
    } else {
        Write-Skip "DVForge already present at $InstallRoot (pass -Force to re-copy from source)"
    }
} elseif ($SourceRoot) {
    if (-not (Test-Path $InstallRoot) -or $Force) {
        Copy-DvForgeTree -Source $SourceRoot -Dest $InstallRoot
    } else {
        Write-Err "$InstallRoot exists but is not a DVForge tree. Pass -Force or choose another -InstallRoot."
        exit 1
    }
} else {
    # Standalone: clone
    if (Test-Path $InstallRoot) {
        if (-not $Force) {
            Write-Err "$InstallRoot exists. Pass -Force to remove and re-clone, or -InstallRoot elsewhere."
            exit 1
        }
        Write-Warn "Removing existing $InstallRoot for fresh clone ..."
        Remove-Item -LiteralPath $InstallRoot -Recurse -Force
    }
    Write-Host "  git clone $RepoUrl $InstallRoot ..."
    & git clone $RepoUrl $InstallRoot
    if ($LASTEXITCODE -ne 0) {
        Write-Err 'git clone failed'
        exit 1
    }
    Write-Ok "Cloned to $InstallRoot"
}

if (-not (Test-DvForgeRoot $InstallRoot)) {
    Write-Err "Install root is not a valid DVForge tree: $InstallRoot"
    exit 1
}

# ---------------------------------------------------------------------------
# Phase 4 - DVForge toolchains (Python installer)
# ---------------------------------------------------------------------------
Write-Step 'Phase 4: Install pinned toolchains via builder/toolchains.py'

$toolIds = New-Object System.Collections.Generic.List[string]
[void]$toolIds.Add('rust')
[void]$toolIds.Add('flutter')
[void]$toolIds.Add('llvm')
[void]$toolIds.Add('vcpkg')
if (-not $SkipVsBuildTools) { [void]$toolIds.Add('vs_buildtools') }
[void]$toolIds.Add('dotnet')
[void]$toolIds.Add('nuget')
if (-not $SkipOptional) {
    [void]$toolIds.Add('sccache')
    [void]$toolIds.Add('imagemagick')
}
if ($WithJava) { [void]$toolIds.Add('java') }

$idsCsv = ($toolIds -join ',')
Write-Host "  tools: $idsCsv"
Write-Host '  (VS Build Tools alone can take several minutes and ~4-6 GB disk.)'
Write-Host ''

# Single-quoted here-string so PowerShell does not parse the Python body.
# Placeholders are substituted after.
$pyTemplate = @'
import os
import sys

root = r"__INSTALL_ROOT__"
ids_csv = r"__IDS_CSV__"

sys.path.insert(0, root)
os.chdir(root)

from builder import toolchains

ids = [x.strip() for x in ids_csv.split(",") if x.strip()]
print("Installing:", ids)
r = toolchains.install_many(ids, root, print)
# Always re-apply so env.json is portable + PATH is wired for this process
toolchains.apply_persisted_env(root)
errs = r.get("errors") or []
if errs:
    print("TOOLCHAIN_ERRORS", errs)
    # Non-fatal for optional pieces; fatal if core failed
    core = {"rust", "flutter", "llvm", "vcpkg", "vs_buildtools", "dotnet", "nuget"}
    bad_core = [e for e in errs if e[0] in core]
    if bad_core:
        raise SystemExit("core toolchain install failed: %s" % bad_core)
print("TOOLCHAINS_OK", r.get("installed"))
'@

$pyCode = $pyTemplate.
    Replace('__INSTALL_ROOT__', $InstallRoot.Replace('\', '\\')).
    Replace('__IDS_CSV__', $idsCsv)

$pyFile = Join-Path $env:TEMP 'dvforge-win-toolchains.py'
# UTF-8 without BOM is fine for Python 3
[System.IO.File]::WriteAllText($pyFile, $pyCode)

# Ensure cargo/git on PATH for this session after any prior installs
Refresh-Path
$cargoBin = Join-Path $env:USERPROFILE '.cargo\bin'
if (Test-Path $cargoBin) {
    $env:Path = "$cargoBin;$env:Path"
}

$pyExit = 1
Push-Location $InstallRoot
try {
    if ($py -eq 'py') {
        & py -3 $pyFile
    } else {
        & $py $pyFile
    }
    $pyExit = $LASTEXITCODE
} finally {
    Pop-Location
    Remove-Item -Path $pyFile -Force -ErrorAction SilentlyContinue
}

if ($pyExit -ne 0) {
    Write-Err "Toolchain install reported errors (exit $pyExit). See log above."
    Write-Warn 'You can re-run this script, or open the GUI and click install on missing tools.'
} else {
    Write-Ok 'Toolchains install finished'
}

# ---------------------------------------------------------------------------
# Phase 5 - Pin Rust MSVC host (matches official CI / orchestrator)
# ---------------------------------------------------------------------------
Write-Step 'Phase 5: Pin Rust 1.75 MSVC toolchain'
Refresh-Path
if (Test-Path $cargoBin) { $env:Path = "$cargoBin;$env:Path" }
$env:RUSTUP_INIT_SKIP_PATH_CHECK = 'yes'

# rustup writes progress to stderr. PowerShell wraps that as ErrorRecords
# (red NativeCommandError) even when exit code is 0. Run via cmd so stderr
# is plain text and $ErrorActionPreference=Stop cannot trip on it.
function Invoke-RustupQuiet {
    param([Parameter(Mandatory)][string[]]$RustupArgs)
    $joined = ($RustupArgs | ForEach-Object {
        if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
    }) -join ' '
    # cmd merges streams; exit code is preserved in $LASTEXITCODE
    cmd.exe /c "rustup $joined 2>&1"
    return $LASTEXITCODE
}

if (Test-Command 'rustup') {
    [void](Invoke-RustupQuiet @('set', 'default-host', 'x86_64-pc-windows-msvc'))
    [void](Invoke-RustupQuiet @('toolchain', 'install', '1.75-x86_64-pc-windows-msvc'))
    [void](Invoke-RustupQuiet @('target', 'add', 'x86_64-pc-windows-msvc',
        '--toolchain', '1.75-x86_64-pc-windows-msvc'))
    [void](Invoke-RustupQuiet @('default', '1.75-x86_64-pc-windows-msvc'))
    [void](Invoke-RustupQuiet @('component', 'add', 'rustfmt',
        '--toolchain', '1.75-x86_64-pc-windows-msvc'))
    $active = (cmd.exe /c "rustup show active-toolchain 2>&1").Trim()
    if ($active -match 'windows-msvc') {
        Write-Ok "rustup default = $active"
    } else {
        Write-Warn "rustup active toolchain is '$active' (wanted *-windows-msvc)"
    }
} else {
    Write-Warn 'rustup not on PATH - open a new terminal or install Rust via the GUI'
}

# ---------------------------------------------------------------------------
# Phase 6 - Sanity summary
# ---------------------------------------------------------------------------
Write-Step 'Phase 6: Sanity check'
$checks = @(
    @{ Name = 'git';     Cmd = 'git' },
    @{ Name = 'python';  Cmd = $py },
    @{ Name = 'rustc';   Cmd = 'rustc' },
    @{ Name = 'cargo';   Cmd = 'cargo' },
    @{ Name = 'flutter'; Cmd = 'flutter' },
    @{ Name = 'dotnet';  Cmd = 'dotnet' },
    @{ Name = 'nuget';   Cmd = 'nuget' }
)
# Prefer toolchains flutter on PATH for check
$flutterBin = Join-Path $InstallRoot '.toolchains\flutter\flutter\bin'
if (Test-Path $flutterBin) {
    $env:Path = "$flutterBin;$env:Path"
}
$vcpkgDir = Join-Path $InstallRoot '.toolchains\vcpkg'
if (Test-Path $vcpkgDir) {
    $env:Path = "$vcpkgDir;$env:Path"
}

foreach ($c in $checks) {
    if (Test-Command $c.Cmd) {
        Write-Ok "$($c.Name): $((Get-Command $c.Cmd).Source)"
    } else {
        Write-Warn "$($c.Name): not on PATH (may still work inside the app via env.json)"
    }
}

# MSBuild / vswhere
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (Test-Path $vswhere) {
    $msbuild = & $vswhere -latest -products * -requires Microsoft.Component.MSBuild `
        -find 'MSBuild\**\Bin\MSBuild.exe' 2>$null | Select-Object -First 1
    if ($msbuild) {
        Write-Ok "MSBuild: $msbuild"
    } else {
        Write-Warn 'vswhere found but MSBuild missing - install VS Build Tools (C++ workload)'
    }
} else {
    Write-Warn 'vswhere not found - VS Build Tools may be missing (-SkipVsBuildTools?)'
}

$envJson = Join-Path $InstallRoot '.toolchains\env.json'
if (Test-Path $envJson) {
    Write-Ok "env.json: $envJson (portable / relative paths)"
} else {
    Write-Warn 'env.json missing - launch app.py once or re-run setup'
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Step 'All done!'
Write-Host ''
Write-Host '  DVForge (Windows builds) is ready.' -ForegroundColor White
Write-Host ''
Write-Host '  Next steps:' -ForegroundColor White
Write-Host "    1. cd `"$InstallRoot`"" -ForegroundColor White
Write-Host '    2. run.bat' -ForegroundColor White
Write-Host '       or:  python app.py' -ForegroundColor White
Write-Host '    3. Open http://127.0.0.1:8765' -ForegroundColor White
Write-Host ''
Write-Host '  Capability board should light Windows .exe / .msi when VS C++ is present.' -ForegroundColor White
Write-Host '  Linux/Android packaging: use Setup-DVForge-WSL2.ps1 instead (or as well).' -ForegroundColor White
Write-Host ''
Write-Host "  Uninstall:  powershell -NoProfile -ExecutionPolicy Bypass -File `"$InstallRoot\Uninstall-DVForge-Windows.ps1`"" -ForegroundColor DarkGray
Write-Host ''
