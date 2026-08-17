#Requires -Version 5.1
<#
.SYNOPSIS
  SA-02m zero-touch flash stand — one entry point for a shift (Windows + WSL2).

.DESCRIPTION
  1) Ensures stand.env exists
  2) Optionally attaches Allwinner FEL USB (1f3a:efe8) into WSL via usbipd
  3) Starts provisioner + FEL agent + postflash monitor + status UI in WSL
  4) Opens http://localhost:8765

.EXAMPLE
  .\tools\imaging\stand\start-stand.ps1
  .\tools\imaging\stand\start-stand.ps1 -SkipUsbAttach -NoBrowser
#>
[CmdletBinding()]
param(
    [switch]$SkipUsbAttach,
    [switch]$NoBrowser,
    [string]$Distro = ""
)

$ErrorActionPreference = 'Stop'
$StandDir = $PSScriptRoot
$ImagingDir = Split-Path $StandDir -Parent

$EnvFile = Join-Path $StandDir 'stand.env'
$EnvExample = Join-Path $StandDir 'stand.env.example'
if (-not (Test-Path $EnvFile)) {
    Copy-Item $EnvExample $EnvFile
    Write-Host "Created $EnvFile — set STAND_IP to this PC's LAN address before flashing."
}

function Get-StandIpFromEnv {
    $ip = '192.168.1.10'
    Get-Content $EnvFile | ForEach-Object {
        if ($_ -match '^\s*STAND_IP\s*=\s*(.+)\s*$') { $ip = $Matches[1].Trim() }
    }
    return $ip
}

$standIp = Get-StandIpFromEnv
Write-Host "SA-02m flash stand  STAND_IP=$standIp"

# ── usbipd: attach FEL device to WSL ─────────────────────────────────────
if (-not $SkipUsbAttach) {
    $usbipd = Get-Command usbipd -ErrorAction SilentlyContinue
    if ($usbipd) {
        Write-Host "Checking FEL USB 1f3a:efe8 …"
        $list = & usbipd list 2>&1 | Out-String
        if ($list -match '1f3a:efe8') {
            # Parse busid from usbipd list (column BUSID)
            $busLine = ($list -split "`n" | Where-Object { $_ -match '1f3a:efe8' } | Select-Object -First 1)
            if ($busLine -match '(\d+-\d+)') {
                $busid = $Matches[1]
                Write-Host "Attaching FEL busid=$busid to WSL"
                try {
                    & usbipd attach --wsl --busid $busid 2>&1 | Write-Host
                } catch {
                    Write-Warning "usbipd attach failed (may already be attached): $_"
                }
            }
        } else {
            Write-Host "FEL device not plugged yet — agent will wait. When board enters FEL, re-run attach or use:"
            Write-Host "  usbipd attach --wsl --busid <BUSID>"
        }
    } else {
        Write-Warning "usbipd not found — install usbipd-win for FEL passthrough to WSL"
    }
}

# ── WSL paths ────────────────────────────────────────────────────────────
function ConvertTo-WslPath([string]$WinPath) {
    $full = (Resolve-Path $WinPath).Path
    if ($full -match '^([A-Za-z]):\\(.*)$') {
        $drive = $Matches[1].ToLowerInvariant()
        $rest = ($Matches[2] -replace '\\', '/')
        return "/mnt/$drive/$rest"
    }
    throw "Cannot convert path: $WinPath"
}

$wslStand = ConvertTo-WslPath $StandDir
$wslScript = "$wslStand/start-stand.sh"
$wslArgs = @()
if ($Distro) { $wslArgs += @('-d', $Distro) }

Write-Host "Starting stand inside WSL …"
$bashCmd = "chmod +x '$wslStand'/*.sh '$wslStand/../net-provisioner'/*.sh '$wslStand/../netinstall'/*.sh '$wslStand/../publish-image.sh' '$wslStand/../prepare-fel-usb.sh' 2>/dev/null; bash '$wslScript'"
& wsl.exe @wslArgs -e bash -lc $bashCmd
if ($LASTEXITCODE -ne 0) {
    throw "WSL start-stand.sh failed with exit $LASTEXITCODE"
}

$ui = 'http://localhost:8765/'
Write-Host ""
Write-Host "Status UI: $ui"
Write-Host "Per board: ETH + USB-OTG + power → FEL → wait DONE"
Write-Host "Stop:      wsl -e bash '$(ConvertTo-WslPath (Join-Path $StandDir 'stop-stand.sh'))'"

if (-not $NoBrowser) {
    try { Start-Process $ui } catch { Write-Host "Open $ui manually" }
}
