# SA-02m — снять образ eMMC с устройства → .img (через WSL2 + make-image.sh)
#
# Учётки: tools/sa02m-device.env (SA02M_HOST / SA02M_USER / SA02M_PASS)
# Документация: tools/imaging/README.md , docs/SA02M_IMAGING_GUIDE.md
#
# Примеры:
#   .\tools\imaging\capture-image.ps1
#   .\tools\imaging\capture-image.ps1 -Ip 192.168.1.136 -Name SA-02m-lab
#   .\tools\imaging\capture-image.ps1 -NoCleanup

[CmdletBinding()]
param(
    [string]$Ip = "",
    [string]$Name = "",
    [string]$OutDir = "",
    [switch]$NoCleanup,
    [switch]$NoZerofill,
    [switch]$NoIdReset
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$EnvFile = Join-Path $RepoRoot "tools\sa02m-device.env"

function Import-Sa02mDeviceEnv {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return }
    Get-Content -LiteralPath $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        $eq = $line.IndexOf("=")
        if ($eq -lt 1) { return }
        $k = $line.Substring(0, $eq).Trim()
        $v = $line.Substring($eq + 1).Trim()
        if (-not [string]::IsNullOrEmpty($k) -and -not (Test-Path "Env:$k")) {
            Set-Item -Path "Env:$k" -Value $v
        }
    }
}

Import-Sa02mDeviceEnv $EnvFile

if (-not $Ip) { $Ip = if ($env:SA02M_HOST) { $env:SA02M_HOST } else { "192.168.1.136" } }
if (-not $Name) { $Name = "SA-02m-" + (Get-Date -Format "yyyyMMdd") }
if (-not $OutDir) { $OutDir = Join-Path $PSScriptRoot "out" }

$wslRepo = (wsl -e wslpath -a $RepoRoot).Trim()
$wslOut = (wsl -e wslpath -a $OutDir).Trim()

$extra = @()
if ($NoCleanup) { $extra += "--no-cleanup" }
if ($NoZerofill) { $extra += "--no-zerofill" }
if ($NoIdReset) { $extra += "--no-id-reset" }

Write-Host "SA-02m image capture"
Write-Host "  donor : root@$Ip (password from tools/sa02m-device.env)"
Write-Host "  web   : http://${Ip}:9999  admin / (SA02M_WEB_PASS)"
Write-Host "  name  : $Name"
Write-Host "  out   : $OutDir"
Write-Host ""
Write-Host "WARNING: during dd SSH on the donor drops until reboot; host keys are regenerated on first boot of clones."
Write-Host ""

$bashExtra = ($extra | ForEach-Object { $_ }) -join " "
$cmd = @"
set -euo pipefail
cd '$wslRepo/tools/imaging'
chmod +x capture-image.sh make-image.sh cleanup-donor.sh stream-after-cleanup.sh wait-donor.sh 2>/dev/null || true
export SA02M_HOST='$Ip'
export SA02M_PASS='$(if ($env:SA02M_PASS) { $env:SA02M_PASS } else { "cyntron" })'
export SA02M_USER='$(if ($env:SA02M_USER) { $env:SA02M_USER } else { "root" })'
./capture-image.sh --ip '$Ip' --name '$Name' --out-dir '$wslOut' $bashExtra
"@

wsl -e bash -lc $cmd
if ($LASTEXITCODE -ne 0) { throw "capture-image failed (exit $LASTEXITCODE)" }

Write-Host ""
Write-Host "Done. Look for:"
Write-Host "  $(Join-Path $OutDir "$Name.img")"
Write-Host "  $(Join-Path $OutDir "$Name.img.xz")"
