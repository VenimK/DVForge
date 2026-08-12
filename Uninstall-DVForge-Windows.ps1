<#
.SYNOPSIS
  Uninstalls the DVForge native Windows build environment created by
  Setup-DVForge-Windows.ps1.

.DESCRIPTION
  Default (recommended): removes DVForge-local toolchains and optional short
  junctions, but keeps Git, Python, Visual Studio, and user Rust installs:

    - <InstallRoot>\.toolchains  (Flutter, LLVM, vcpkg, JDK, env.json, ...)
    - <InstallRoot>\workspace\rustdesk-src  (source + cargo/flutter build junk)
    - <InstallRoot>\workspace\output        (built artifacts)
    - Drive-root junctions used for MAX_PATH: X:\rdlb, X:\rdlb-src, X:\r
      (only if they point into this InstallRoot)

  Optional switches (more destructive):
    -RemoveProject     Delete the entire InstallRoot folder
    -RemoveRust        Remove user rustup/cargo (~/.cargo, ~/.rustup)
    -RemoveSccache     Remove sccache cache (~/.cache/sccache)
    -RemoveChocoPkgs   choco uninstall nuget.commandline imagemagick (if choco)

  Does NOT uninstall:
    - Visual Studio / Build Tools (use Apps & Features or VS Installer)
    - .NET SDK, Git, Python (system-wide)
    - Chocolatey itself

  Idempotent - safe to re-run. Missing pieces are skipped.

.PARAMETER InstallRoot
  DVForge install directory. Default: C:\DVForge
  If omitted and this script lives inside a DVForge tree, that tree is used.

.PARAMETER Force
  Skip interactive confirmation.

.PARAMETER RemoveProject
  Delete the entire InstallRoot after cleaning toolchains.

.PARAMETER RemoveRust
  Delete %USERPROFILE%\.cargo and %USERPROFILE%\.rustup

.PARAMETER RemoveSccache
  Delete sccache disk cache.

.PARAMETER RemoveChocoPkgs
  Attempt choco uninstall of nuget.commandline and imagemagick.

.EXAMPLE
  # Clean toolchains only; keep project source and VS
  .\Uninstall-DVForge-Windows.ps1

.EXAMPLE
  # Wipe C:\DVForge entirely
  .\Uninstall-DVForge-Windows.ps1 -InstallRoot C:\DVForge -RemoveProject -Force
#>

#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$InstallRoot = '',
    [switch]$Force,
    [switch]$RemoveProject,
    [switch]$RemoveRust,
    [switch]$RemoveSccache,
    [switch]$RemoveChocoPkgs
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Auto-elevate (junctions at drive root + optional full delete)
# ---------------------------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host 'Requesting Administrator privileges (UAC) ...' -ForegroundColor Yellow
    $scriptPath = $MyInvocation.MyCommand.Path
    if (-not $scriptPath) { $scriptPath = $PSCommandPath }
    $argList = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$scriptPath`""
    )
    if ($InstallRoot)      { $argList += '-InstallRoot'; $argList += "`"$InstallRoot`"" }
    if ($Force)             { $argList += '-Force' }
    if ($RemoveProject)     { $argList += '-RemoveProject' }
    if ($RemoveRust)        { $argList += '-RemoveRust' }
    if ($RemoveSccache)     { $argList += '-RemoveSccache' }
    if ($RemoveChocoPkgs)   { $argList += '-RemoveChocoPkgs' }
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

function Confirm-Action {
    param([string]$Message)
    if ($Force) { return $true }
    $r = Read-Host "$Message [y/N]"
    return ($r -match '^[Yy](es)?$')
}

function Test-DvForgeRoot($Path) {
    if (-not $Path) { return $false }
    return (Test-Path (Join-Path $Path 'app.py')) -and `
           (Test-Path (Join-Path $Path 'builder'))
}

function Remove-PathSafe {
    param([string]$Path, [string]$Label = '')
    if (-not $Path) { return }
    if (Test-Path -LiteralPath $Path) {
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            Write-Ok "removed $(if ($Label) { $Label } else { $Path })"
        } catch {
            Write-Warn "could not remove $Path : $_"
        }
    } else {
        Write-Skip "not found: $(if ($Label) { $Label } else { $Path })"
    }
}

function Test-JunctionTarget {
    param([string]$LinkPath, [string]$MustContain)
    if (-not (Test-Path -LiteralPath $LinkPath)) { return $false }
    try {
        $item = Get-Item -LiteralPath $LinkPath -Force
        # Junctions report as Directory with LinkType or ReparsePoint
        $target = $null
        if ($item.LinkType -eq 'Junction' -or $item.Attributes -match 'ReparsePoint') {
            # .NET: resolve final path
            $target = [System.IO.Path]::GetFullPath($LinkPath)
        }
        if (-not $target) {
            $target = (Resolve-Path -LiteralPath $LinkPath).Path
        }
        $must = [IO.Path]::GetFullPath($MustContain).TrimEnd('\')
        return ($target -like "$must*")
    } catch {
        return $false
    }
}

function Remove-ShortJunctions {
    param([string]$ProjectRoot)
    $drive = (Split-Path -Qualifier $ProjectRoot)
    if (-not $drive) { $drive = 'C:' }
    $candidates = @(
        (Join-Path $drive 'rdlb'),
        (Join-Path $drive 'rdlb-src'),
        (Join-Path $drive 'r'),
        'C:\rdlb',
        'C:\rdlb-src'
    ) | Select-Object -Unique

    $srcHint = Join-Path $ProjectRoot 'workspace\rustdesk-src'
    foreach ($cand in $candidates) {
        if (-not (Test-Path -LiteralPath $cand)) {
            continue
        }
        # Only remove if it resolves under this project's workspace
        $belongs = $false
        try {
            $resolved = (Resolve-Path -LiteralPath $cand -ErrorAction Stop).Path
            $rootFull = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
            if ($resolved -like "$rootFull*") { $belongs = $true }
        } catch {}
        if (-not $belongs) {
            Write-Skip "left junction alone (not our tree): $cand"
            continue
        }
        # rmdir removes junction without deleting target contents
        cmd /c "rmdir `"$cand`"" | Out-Null
        if (-not (Test-Path -LiteralPath $cand)) {
            Write-Ok "removed junction $cand"
        } else {
            Write-Warn "could not remove junction $cand"
        }
    }
}

# ---------------------------------------------------------------------------
# Resolve InstallRoot
# ---------------------------------------------------------------------------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ScriptDir) { $ScriptDir = $PSScriptRoot }

if (-not $InstallRoot) {
    if (Test-DvForgeRoot $ScriptDir) {
        $InstallRoot = $ScriptDir
    } else {
        $InstallRoot = 'C:\DVForge'
    }
}
$InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)

Write-Step 'DVForge Windows environment uninstaller'
Write-Host "  Install root:     $InstallRoot"
Write-Host "  Remove toolchains: yes"
Write-Host "  Remove workspace:  yes (src/output under project)"
Write-Host "  Remove project:    $(if ($RemoveProject) { 'YES' } else { 'no' })"
Write-Host "  Remove Rust user:  $(if ($RemoveRust) { 'YES' } else { 'no' })"
Write-Host "  Remove sccache:    $(if ($RemoveSccache) { 'YES' } else { 'no' })"
Write-Host "  choco pkgs:        $(if ($RemoveChocoPkgs) { 'YES' } else { 'no' })"
Write-Host ''
Write-Host '  Kept by default: Git, Python, VS Build Tools, .NET SDK, Chocolatey' -ForegroundColor DarkGray
Write-Host ''

if ($RemoveProject) {
    Write-Warn "This will DELETE the entire folder: $InstallRoot"
}
if ($RemoveRust) {
    Write-Warn 'This will DELETE your user Rust install (~/.cargo, ~/.rustup) for ALL projects.'
}

if (-not (Confirm-Action 'Proceed with uninstall?')) {
    Write-Host 'Cancelled.'
    exit 0
}

# ---------------------------------------------------------------------------
# Phase 1 - toolchains + workspace + junctions
# ---------------------------------------------------------------------------
Write-Step 'Phase 1: Remove local toolchains and build trees'

if (Test-Path -LiteralPath $InstallRoot) {
    Remove-PathSafe (Join-Path $InstallRoot '.toolchains') '.toolchains'
    Remove-PathSafe (Join-Path $InstallRoot 'workspace\rustdesk-src') 'workspace\rustdesk-src'
    Remove-PathSafe (Join-Path $InstallRoot 'workspace\output') 'workspace\output'
    # pycache under builder
    Get-ChildItem -Path (Join-Path $InstallRoot 'builder') -Filter '__pycache__' -Recurse `
        -Directory -ErrorAction SilentlyContinue | ForEach-Object {
        Remove-PathSafe $_.FullName
    }
} else {
    Write-Skip "install root not found: $InstallRoot"
}

Write-Step 'Phase 2: Short-path junctions (if ours)'
if (Test-Path -LiteralPath $InstallRoot) {
    Remove-ShortJunctions -ProjectRoot $InstallRoot
} else {
    Write-Skip 'no project root - skip junctions'
}

# ---------------------------------------------------------------------------
# Phase 3 - optional user Rust / sccache
# ---------------------------------------------------------------------------
if ($RemoveRust) {
    Write-Step 'Phase 3: User Rust (rustup)'
    Remove-PathSafe (Join-Path $env:USERPROFILE '.cargo')  '%USERPROFILE%\.cargo'
    Remove-PathSafe (Join-Path $env:USERPROFILE '.rustup') '%USERPROFILE%\.rustup'
} else {
    Write-Step 'Phase 3: User Rust'
    Write-Skip 'kept (pass -RemoveRust to delete ~/.cargo and ~/.rustup)'
}

if ($RemoveSccache) {
    Write-Step 'Phase 3b: sccache cache'
    Remove-PathSafe (Join-Path $env:USERPROFILE '.cache\sccache')
    Remove-PathSafe (Join-Path $env:LOCALAPPDATA 'Mozilla\sccache')
} else {
    Write-Skip 'sccache cache kept'
}

# ---------------------------------------------------------------------------
# Phase 4 - optional choco packages
# ---------------------------------------------------------------------------
if ($RemoveChocoPkgs) {
    Write-Step 'Phase 4: Chocolatey packages (nuget / imagemagick)'
    if (Get-Command choco -ErrorAction SilentlyContinue) {
        foreach ($pkg in @('nuget.commandline', 'imagemagick')) {
            Write-Host "  choco uninstall $pkg -y ..."
            & choco uninstall $pkg -y --limit-output 2>$null
            if ($LASTEXITCODE -eq 0) {
                Write-Ok "uninstalled $pkg"
            } else {
                Write-Skip "$pkg not removed (exit $LASTEXITCODE / not installed)"
            }
        }
    } else {
        Write-Skip 'choco not on PATH'
    }
} else {
    Write-Step 'Phase 4: Chocolatey packages'
    Write-Skip 'kept (pass -RemoveChocoPkgs to uninstall nuget/imagemagick)'
}

# ---------------------------------------------------------------------------
# Phase 5 - optional full project delete
# ---------------------------------------------------------------------------
if ($RemoveProject) {
    Write-Step 'Phase 5: Remove entire project folder'
    if (Test-Path -LiteralPath $InstallRoot) {
        # If this script lives inside InstallRoot, we cannot delete ourselves mid-run
        # reliably - copy a delayed cmd to TEMP.
        $selfInTree = $false
        try {
            $here = [IO.Path]::GetFullPath($ScriptDir).TrimEnd('\')
            $root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
            if ($here -ieq $root -or $here.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
                $selfInTree = $true
            }
        } catch {}

        if ($selfInTree) {
            $bat = Join-Path $env:TEMP 'dvforge-remove-project.cmd'
            $lines = @(
                '@echo off',
                'timeout /t 2 /nobreak >nul',
                "rmdir /s /q `"$InstallRoot`"",
                "if exist `"$InstallRoot`" (echo FAILED to remove $InstallRoot) else (echo Removed $InstallRoot)",
                "del `"%~f0`""
            )
            Set-Content -Path $bat -Value $lines -Encoding ASCII
            Start-Process -FilePath 'cmd.exe' -ArgumentList "/c `"$bat`"" -WindowStyle Hidden
            Write-Ok "scheduled delete of $InstallRoot (script was inside the tree)"
        } else {
            Remove-PathSafe $InstallRoot "project $InstallRoot"
        }
    } else {
        Write-Skip "already gone: $InstallRoot"
    }
} else {
    Write-Step 'Phase 5: Project folder'
    Write-Skip "kept $InstallRoot (pass -RemoveProject to delete)"
}

Write-Step 'Done'
Write-Host ''
Write-Host '  Visual Studio / Build Tools, .NET SDK, Git, and Python were left installed.' -ForegroundColor DarkGray
Write-Host '  Remove VS via:  "Visual Studio Installer" or Apps & Features.' -ForegroundColor DarkGray
Write-Host '  Reinstall:  Setup-DVForge-Windows.ps1' -ForegroundColor White
Write-Host ''
