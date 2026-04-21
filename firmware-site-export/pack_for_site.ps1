# Сборка пакета для загрузки на сайт: копирует *.fw из build/AppBoot и пишет index.json в этот каталог.
# Каталог сборки по умолчанию: site-deploy.config.json (defaultPackScanDir) или FW_PACK_SCAN_DIR, иначе аргумент.
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$WebRoot = Split-Path -Parent $Root
$Sa02m = Join-Path $WebRoot "opt\sa02m-flasher"
$Script = Join-Path $Sa02m "scripts\prepare_firmware_for_site.py"

$CfgPath = Join-Path $Root "site-deploy.config.json"
$Cfg = $null
if (Test-Path -LiteralPath $CfgPath) {
  try {
    $Cfg = Get-Content -LiteralPath $CfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
  } catch {
    Write-Error "Некорректный JSON в ${CfgPath}: $_"
  }
}

$Scan = ""
if ($args.Count -ge 1 -and $args[0]) {
  $Scan = $args[0]
}
if (-not $Scan.Trim()) {
  $Scan = ($env:FW_PACK_SCAN_DIR).Trim()
}
if (-not $Scan.Trim() -and $Cfg) {
  $Scan = ([string]$Cfg.defaultPackScanDir).Trim()
}
if (-not $Scan.Trim()) {
  Write-Error "Не задан каталог MR-02m build/AppBoot: передайте аргументом, задайте FW_PACK_SCAN_DIR или defaultPackScanDir в site-deploy.config.json (шаблон: site-deploy.config.example.json)"
}

if (-not (Test-Path -LiteralPath $Scan -PathType Container)) {
  Write-Error "Каталог со сборкой не найден: $Scan`nУкажите путь: .\pack_for_site.ps1 'D:\MR-02m\build\AppBoot'"
}

python "$Script" --scan "$Scan" --bundle-dir $Root
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "Готово. Загрузите на сайт содержимое: $Root"
