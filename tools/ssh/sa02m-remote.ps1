# SA-02m — неинтерактивный SSH через PuTTY plink (Windows, для агентов).
# Использование:
#   .\tools\ssh\sa02m-remote.ps1 "systemctl is-active sa02m-flasher"
#   .\tools\ssh\sa02m-remote.ps1 -HostKey "SHA256:..." "hostname"
#
# Переменные окружения: SA02M_HOST, SA02M_PASS, SA02M_HOSTKEY

param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemoteCommand,
    [string]$HostAddr = $(if ($env:SA02M_HOST) { $env:SA02M_HOST } else { "192.168.1.136" }),
    [string]$User = "root",
    [string]$Password = $(if ($env:SA02M_PASS) { $env:SA02M_PASS } else { "cyntron" }),
    [string]$HostKey = $(if ($env:SA02M_HOSTKEY) { $env:SA02M_HOSTKEY } else { "SHA256:TMkrSFsuRUe0F1caCEcTNUli9gb7KaQYsPC7FELohKc" })
)

$ErrorActionPreference = "Stop"
$Plink = "C:\Program Files\PuTTY\plink.exe"
if (-not (Test-Path $Plink)) {
    Write-Error "PuTTY plink не найден: $Plink"
}

if (-not $RemoteCommand -or $RemoteCommand.Count -eq 0) {
    Write-Error "Укажите удалённую команду, например: .\sa02m-remote.ps1 'hostname'"
}

$cmd = ($RemoteCommand -join " ")
& $Plink -batch -ssh "${User}@${HostAddr}" -pw $Password -hostkey $HostKey $cmd
exit $LASTEXITCODE
