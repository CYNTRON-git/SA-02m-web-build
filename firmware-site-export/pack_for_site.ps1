# Сборка пакета для загрузки на сайт: копирует *.fw из build/AppBoot и пишет index.json в этот каталог.
$ErrorActionPreference = "Stop"
$WebRoot = Split-Path -Parent $PSScriptRoot
$Sa02m = Join-Path $WebRoot "opt\sa02m-flasher"
$Script = Join-Path $Sa02m "scripts\prepare_firmware_for_site.py"
$Scan = if ($args.Count -ge 1 -and $args[0]) { $args[0] } else { Join-Path $env:USERPROFILE "Downloads\MR-02m\build\AppBoot" }
if (-not (Test-Path -LiteralPath $Scan -PathType Container)) {
    Write-Error "Каталог со сборкой не найден: $Scan`nУкажите путь: .\pack_for_site.ps1 'D:\MR-02m\build\AppBoot'"
}
python "$Script" --scan "$Scan" --bundle-dir "$PSScriptRoot"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "Готово. Загрузите на сайт содержимое: $PSScriptRoot"
