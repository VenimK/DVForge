<#
.SYNOPSIS
    Create the virtual printer that feeds the open printer_driver_adapter.

.DESCRIPTION
    Builds a printer on an *inbox* (already Microsoft-signed) print driver whose
    port is a plain file path inside our spool directory. The spooler writes the
    rendered job to that file; the adapter picks it up, hands it to RustDesk and
    deletes it. Nothing here needs WHQL signing or a custom driver.

    Do NOT also run the app's own "Install Printer" button / --install-remote-printer.
    That installs RustDesk's driver on a port their signed adapter owns, and the
    two setups would fight over the same printer name.

.PARAMETER Tag
    The app name RustDesk passes to the adapter's init(). Must match
    get_app_name() in the build, because the adapter derives its spool path
    from it: %ProgramData%\<Tag>\printer-spool

.PARAMETER DriverName
    Inbox driver to render with. See -ListDrivers.

.EXAMPLE
    .\setup-printer.ps1 -Tag "Darnellcloud-Connect"

.EXAMPLE
    .\setup-printer.ps1 -ListDrivers

.EXAMPLE
    .\setup-printer.ps1 -Tag "Darnellcloud-Connect" -Remove
#>
[CmdletBinding()]
param(
    [string]$Tag = "Darnellcloud-Connect",
    [string]$DriverName = "Microsoft XPS Document Writer v4",
    [switch]$ListDrivers,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this from an elevated PowerShell. Adding a printer port requires administrator rights."
    }
}

if ($ListDrivers) {
    Write-Host "Print drivers currently installed:`n"
    Get-PrinterDriver | Select-Object Name, MajorVersion | Sort-Object Name | Format-Table -AutoSize
    Write-Host "Pick one that renders to a file. 'Microsoft XPS Document Writer v4' emits OpenXPS;"
    Write-Host "'Microsoft Print To PDF' emits PDF. RustDesk's own driver declares XpsFormat=XPS."
    return
}

$spool    = Join-Path $env:ProgramData (Join-Path $Tag "printer-spool")
$portPath = Join-Path $spool "job.prn"
$printer  = "$Tag Printer"

if ($Remove) {
    Assert-Admin
    if (Get-Printer -Name $printer -ErrorAction SilentlyContinue) {
        Remove-Printer -Name $printer
        Write-Host "removed printer: $printer"
    }
    if (Get-PrinterPort -Name $portPath -ErrorAction SilentlyContinue) {
        Remove-PrinterPort -Name $portPath
        Write-Host "removed port:    $portPath"
    }
    Write-Host "spool directory left in place: $spool"
    return
}

Assert-Admin

# --- driver must exist ------------------------------------------------------
if (-not (Get-PrinterDriver -Name $DriverName -ErrorAction SilentlyContinue)) {
    Write-Host "Driver not installed: $DriverName" -ForegroundColor Red
    Write-Host "Run with -ListDrivers to see what is available."
    throw "missing driver"
}

# --- spool directory --------------------------------------------------------
# The spooler runs as a service account, so it needs write access here.
New-Item -ItemType Directory -Force -Path $spool | Out-Null
$acl = Get-Acl $spool
foreach ($who in @("NT AUTHORITY\SYSTEM", "NT AUTHORITY\LOCAL SERVICE", "BUILTIN\Administrators")) {
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $who, "Modify",
        "ContainerInherit,ObjectInherit", "None", "Allow")
    $acl.AddAccessRule($rule)
}
Set-Acl -Path $spool -AclObject $acl
Write-Host "spool directory: $spool"

# --- port -------------------------------------------------------------------
# A Local Port whose *name is a file path* makes the spooler write output
# straight to that file, with no "save as" prompt.
if (Get-PrinterPort -Name $portPath -ErrorAction SilentlyContinue) {
    Write-Host "port already exists: $portPath"
} else {
    Add-PrinterPort -Name $portPath
    Write-Host "created port:    $portPath"
}

# --- printer ----------------------------------------------------------------
if (Get-Printer -Name $printer -ErrorAction SilentlyContinue) {
    Set-Printer -Name $printer -PortName $portPath -DriverName $DriverName
    Write-Host "updated printer: $printer"
} else {
    Add-Printer -Name $printer -DriverName $DriverName -PortName $portPath
    Write-Host "created printer: $printer"
}

Write-Host ""
Write-Host "Done. Next:" -ForegroundColor Green
Write-Host "  1. Copy printer_driver_adapter.dll next to the app exe:"
Write-Host "     Copy-Item .\target\release\printer_driver_adapter.dll 'C:\Program Files\$Tag\' -Force"
Write-Host "  2. Restart the $Tag service so the server reloads the adapter."
Write-Host "  3. Confirm it initialised:"
Write-Host "     Get-ChildItem 'C:\Windows\ServiceProfiles\LocalService\AppData\Roaming\$Tag\log' -Recurse -Filter *.log |"
Write-Host "       Select-String 'printer service'"
Write-Host "     Expect: 'printer service initialized' (not 'init failed')."
Write-Host "  4. Connect from another machine, print to '$printer' on this one,"
Write-Host "     then check $spool\adapter.log for a 'captured' line."
