#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Включает для WSL2 зеркальную сеть (доступ ко всем сетям Windows: LAN, VPN, несколько NIC).

.DESCRIPTION
  Создаёт/обновляет %USERPROFILE%\.wslconfig и перезапускает WSL.
  Требуется WSL >= 2.0 (проверка: wsl --version).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\imaging\setup-wsl-network.ps1
#>
$ErrorActionPreference = 'Stop'

$wslConfig = Join-Path $env:USERPROFILE '.wslconfig'
$block = @'
[wsl2]
networkingMode=mirrored
dnsTunneling=true
firewall=true
autoProxy=true

'@

$repoRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$netFixSrc = Join-Path $PSScriptRoot 'wsl-net-fix.sh'

$existing = if (Test-Path $wslConfig) { Get-Content -Raw $wslConfig } else { '' }
if ($existing -notmatch 'networkingMode\s*=\s*mirrored') {
    if ($existing -match '\[wsl2\]') {
        if ($existing -match 'networkingMode\s*=') {
            $existing = $existing -replace 'networkingMode\s*=\s*\w+', 'networkingMode=mirrored'
        } else {
            $existing = $existing -replace '(\[wsl2\][^\[]*)', "`$1`nnetworkingMode=mirrored`n"
        }
        Set-Content -Path $wslConfig -Value $existing.TrimEnd() -Encoding utf8NoBOM
    } else {
        Set-Content -Path $wslConfig -Value $block.TrimEnd() -Encoding utf8NoBOM
    }
    Write-Host "Updated $wslConfig (networkingMode=mirrored)"
} else {
    Write-Host "Already configured: $wslConfig"
}

if (-not (Test-Path $netFixSrc)) {
    Write-Error "Missing $netFixSrc"
}

$winPath = (Resolve-Path $netFixSrc).Path
Write-Host "Installing wsl-net-fix.sh into WSL..."
wsl -e sh -c "sudo install -m 755 '$(wslpath -a $winPath)' /usr/local/sbin/wsl-net-fix.sh"

$wslConfSrc = Join-Path $PSScriptRoot 'wsl.conf.snippet'
if (-not (Test-Path $wslConfSrc)) { Write-Error "Missing $wslConfSrc" }
$wslConfWin = (Resolve-Path $wslConfSrc).Path
wsl -e sh -c "sudo cp '$(wslpath -a $wslConfWin)' /etc/wsl.conf"
Write-Host 'Updated /etc/wsl.conf [boot] command'

Write-Host 'Shutting down WSL (apply on next start)...'
wsl --shutdown
Start-Sleep -Seconds 3
wsl -e true
Write-Host 'WSL restarted. Check: wsl -e ip route get 192.168.1.136'
