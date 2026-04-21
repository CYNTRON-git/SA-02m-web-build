# Выгрузка firmware-site-export на сервер (PuTTY pscp + ключ .ppk).
# Хост, пользователь и пути по умолчанию — в site-deploy.config.json (копия с site-deploy.config.example.json)
# или переменные окружения FW_UPLOAD_SSH_HOST, FW_UPLOAD_SSH_USER, FW_UPLOAD_PPK (и устар. STORE_SITE_PPK).
# OpenSSH (ssh/scp) не использует Pageant — нужен pscp из PuTTY и -i key.ppk
param(
  [Parameter(Mandatory = $false, Position = 0)]
  [string] $RemotePath = "",
  [Parameter(Position = 1)]
  [string] $Ppk = ""
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$CfgPath = Join-Path $Root "site-deploy.config.json"
$Cfg = $null
if (Test-Path -LiteralPath $CfgPath) {
  try {
    $Cfg = Get-Content -LiteralPath $CfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
  } catch {
    Write-Error "Некорректный JSON в ${CfgPath}: $_"
  }
}

function _trim([string] $s) { if ($null -eq $s) { "" } else { $s.Trim() } }

$SshHost = _trim $env:FW_UPLOAD_SSH_HOST
if (-not $SshHost -and $Cfg) { $SshHost = _trim [string]$Cfg.sshHost }

$SshUser = _trim $env:FW_UPLOAD_SSH_USER
if (-not $SshUser -and $Cfg) { $SshUser = _trim [string]$Cfg.sshUser }

$RemoteNorm = _trim $RemotePath
if (-not $RemoteNorm -and $Cfg) { $RemoteNorm = _trim [string]$Cfg.defaultRemoteFirmwareDir }

if (-not $SshHost) {
  Write-Error "Не задан SSH-хост: скопируйте site-deploy.config.example.json → site-deploy.config.json и заполните sshHost, либо задайте `$env:FW_UPLOAD_SSH_HOST"
}
if (-not $SshUser) {
  Write-Error "Не задан SSH-пользователь: site-deploy.config.json (sshUser) или `$env:FW_UPLOAD_SSH_USER"
}
if (-not $RemoteNorm) {
  Write-Error "Не задан каталог прошивок на сервере: передайте первым аргументом или задайте defaultRemoteFirmwareDir в site-deploy.config.json"
}

if (-not $Ppk.Trim()) {
  $Ppk = _trim $env:FW_UPLOAD_PPK
}
if (-not $Ppk.Trim()) {
  $Ppk = _trim $env:STORE_SITE_PPK
}
if (-not $Ppk.Trim() -and $Cfg) {
  $Ppk = _trim [string]$Cfg.defaultPpkPath
}
if (-not (Test-Path -LiteralPath $Ppk)) {
  Write-Error "Не найден ключ .ppk: $Ppk`nПередайте вторым аргументом, задайте `$env:FW_UPLOAD_PPK / STORE_SITE_PPK или defaultPpkPath в site-deploy.config.json"
}

if (-not (Test-Path -LiteralPath (Join-Path $Root "index.json") -PathType Leaf)) {
  Write-Error "Нет index.json в $Root — сначала выполните pack_for_site.ps1"
}

# PuTTY кладёт pscp в Program Files; PATH в текущей сессии может быть старым
$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
  [System.Environment]::GetEnvironmentVariable("Path", "User")
$Pf86 = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFilesX86)
if ([string]::IsNullOrEmpty($Pf86)) { $Pf86 = "C:\Program Files (x86)" }
$PscpCandidates = @(
  (Get-Command pscp -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
  (Join-Path $env:ProgramFiles "PuTTY\pscp.exe"),
  (Join-Path $Pf86 "PuTTY\pscp.exe")
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
if (-not $PscpCandidates) {
  Write-Error "Не найден pscp.exe (PuTTY). Установите: winget install PuTTY.PuTTY"
}
$Pscp = $PscpCandidates[0]
Write-Host "Используется pscp: $Pscp"

# Удалённые команды (mkdir, sudo cp) — только PuTTY plink: у TortoisePlink при ошибке SSH-команды
# часто не обновляется `$LASTEXITCODE`, остаётся 0 от предыдущего шага → sudo cp падает, скрипт всё равно exit 0.
$Plink = Join-Path (Split-Path -Parent $Pscp) "plink.exe"
if (-not (Test-Path -LiteralPath $Plink)) {
  $PlinkCandidates = @(
    (Get-Command plink -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
    (Join-Path $env:ProgramFiles "PuTTY\plink.exe"),
    (Join-Path $Pf86 "PuTTY\plink.exe")
  ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
  if (-not $PlinkCandidates) {
    Write-Error "Не найден plink.exe (рядом с pscp или в PuTTY). Нужен для sudo-установки в каталог bitrix."
  }
  $Plink = $PlinkCandidates[0]
}
Write-Host "Используется plink: $Plink"

$RemoteNorm = $RemoteNorm.Trim().Replace("\", "/").TrimEnd("/")
$PscpArgs = @("-batch", "-i", $Ppk)
$DestPrefix = "${SshUser}@${SshHost}:${RemoteNorm}/"

# Каталог сайта bitrix: пользователь не пишет напрямую — pscp в /tmp, затем sudo cp + chown.
$useSudoStaging = $RemoteNorm.StartsWith("/home/bitrix") -or ($env:FW_UPLOAD_USE_STAGING -eq "1")

if ($useSudoStaging) {
  $tmp = "/tmp/sa02m_fw_staging"
  Write-Host "Режим sudo: загрузка в $tmp, затем установка в $RemoteNorm/"
  & $Plink @("-batch", "-i", $Ppk, "${SshUser}@${SshHost}", "mkdir -p $tmp && rm -f $tmp/*")
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  $DestTmp = "${SshUser}@${SshHost}:${tmp}/"

  Write-Host "Копирование index.json → $tmp"
  & $Pscp @PscpArgs (Join-Path $Root "index.json") $DestTmp
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

  $fw = Get-ChildItem -Path $Root -Filter *.fw -File -ErrorAction SilentlyContinue
  foreach ($f in $fw) {
    Write-Host "Копирование $($f.Name) → $tmp"
    & $Pscp @PscpArgs $f.FullName $DestTmp
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  }
  if (-not $fw) {
    Write-Host "Предупреждение: нет *.fw — на сервер уходит только index.json." -ForegroundColor Yellow
  }

  $names = @("index.json") + @($fw | ForEach-Object { $_.Name })
  foreach ($n in $names) {
    $cmd = "sudo cp $tmp/$n $RemoteNorm/$n && sudo chown bitrix:bitrix $RemoteNorm/$n && sudo chmod 644 $RemoteNorm/$n && rm -f $tmp/$n"
    & $Plink @("-batch", "-i", $Ppk, "${SshUser}@${SshHost}", $cmd)
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  }
  & $Plink @("-batch", "-i", $Ppk, "${SshUser}@${SshHost}", "rmdir $tmp 2>/dev/null || true")
  Write-Host "Готово (sudo): $RemoteNorm/"
  & $Plink @("-batch", "-i", $Ppk, "${SshUser}@${SshHost}", "sudo ls -la $RemoteNorm")
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  exit 0
}

Write-Host "Прямая загрузка pscp → $DestPrefix"
& $Pscp @PscpArgs (Join-Path $Root "index.json") $DestPrefix
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$fw = Get-ChildItem -Path $Root -Filter *.fw -File -ErrorAction SilentlyContinue
if (-not $fw) {
  Write-Host "Предупреждение: нет *.fw — загружен только index.json." -ForegroundColor Yellow
  exit 0
}
foreach ($f in $fw) {
  Write-Host "Копирование $($f.Name)…"
  & $Pscp @PscpArgs $f.FullName $DestPrefix
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
Write-Host "Готово: pscp → $DestPrefix"
