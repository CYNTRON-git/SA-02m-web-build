# open-build-vm.ps1 — открыть VM сборки A40i в VMware Workstation
param(
    [string]$VmxPath = "D:\SK-A40i-SODIMM\Linux\SK-A40i_Linux_build_machine\lubuntu64.vmx"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $VmxPath)) {
    Write-Error "VMX not found: $VmxPath"
}

$candidates = @(
    "${env:ProgramFiles(x86)}\VMware\VMware Workstation\vmware.exe",
    "$env:ProgramFiles\VMware\VMware Workstation\vmware.exe",
    "${env:ProgramFiles(x86)}\VMware\VMware Player\vmplayer.exe",
    "$env:ProgramFiles\VMware\VMware Player\vmplayer.exe"
)

$exe = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $exe) {
    Write-Host "VMware not found. Open manually:" -ForegroundColor Yellow
    Write-Host "  $VmxPath"
    Write-Host "Login: user / 123456"
    Write-Host "SSH: user@192.168.150.128 (VMnet1, see vmrun getGuestIPAddress)"
    exit 1
}

Write-Host "Opening VM with $exe"
Start-Process -FilePath $exe -ArgumentList @($VmxPath)
Write-Host @"

Next steps ON THE VM (as user, sudo for build):
  ssh user@192.168.150.128   # password 123456
  cd /home/user/src/buildroot-2022.08.8-sk-a40i
  bash /path/to/prepare-rt-docker-kernel.sh check
  bash /path/to/prepare-rt-docker-kernel.sh build

Device boot reference (host): D:\SK-A40i-SODIMM\device_boot
See tools/buildroot/README.md
"@
