#!/usr/bin/env bash
# Выгрузка firmware-site-export: scp + при пути /home/bitrix/* — staging + sudo.
# Хост/пользователь: site-deploy.config.json (копия site-deploy.config.example.json) или
# FW_UPLOAD_SSH_HOST, FW_UPLOAD_SSH_USER. Ключ: SSH_IDENTITY_FILE (OpenSSH) для scp/ssh.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
CFG="$DIR/site-deploy.config.json"

cfg_get() {
  local key="$1"
  python3 -c "import json,sys; p,k=sys.argv[1],sys.argv[2]; d=json.load(open(p,encoding='utf-8')); v=d.get(k); sys.stdout.write('' if v is None else str(v))" "$CFG" "$key" 2>/dev/null || true
}

SSH_HOST="${FW_UPLOAD_SSH_HOST:-}"
SSH_USER="${FW_UPLOAD_SSH_USER:-}"
REMOTE_DEFAULT=""

if [[ -f "$CFG" ]]; then
  [[ -n "$SSH_HOST" ]] || SSH_HOST="$(cfg_get sshHost)"
  [[ -n "$SSH_USER" ]] || SSH_USER="$(cfg_get sshUser)"
  REMOTE_DEFAULT="$(cfg_get defaultRemoteFirmwareDir)"
fi

REMOTE="${1:-$REMOTE_DEFAULT}"
REMOTE="${REMOTE%/}"

if [[ -z "$SSH_HOST" || -z "$SSH_USER" ]]; then
  echo "Задайте FW_UPLOAD_SSH_HOST и FW_UPLOAD_SSH_USER или создайте site-deploy.config.json из site-deploy.config.example.json" >&2
  exit 1
fi
if [[ -z "$REMOTE" ]]; then
  echo "Укажите каталог на сервере первым аргументом или defaultRemoteFirmwareDir в site-deploy.config.json" >&2
  exit 1
fi

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new)
if [[ -n "${SSH_IDENTITY_FILE:-}" ]]; then
  SSH_OPTS=(-i "$SSH_IDENTITY_FILE" "${SSH_OPTS[@]}")
fi
if [[ ! -f "$DIR/index.json" ]]; then
  echo "Нет $DIR/index.json — сначала ./pack_for_site.sh" >&2
  exit 1
fi
ssh "${SSH_OPTS[@]}" "${SSH_USER}@${SSH_HOST}" "echo ok" >/dev/null

shopt -s nullglob
fw=( "$DIR"/*.fw )

if [[ "$REMOTE" == *"/home/bitrix"* ]] || [[ "${FW_UPLOAD_USE_STAGING:-}" == "1" ]]; then
  TMP=/tmp/sa02m_fw_staging
  echo "Режим sudo: $TMP → $REMOTE/"
  ssh "${SSH_OPTS[@]}" "${SSH_USER}@${SSH_HOST}" "mkdir -p $TMP && rm -f $TMP/*"
  scp "${SSH_OPTS[@]}" "$DIR/index.json" "${SSH_USER}@${SSH_HOST}:${TMP}/"
  for f in "${fw[@]}"; do
    [[ -f "$f" ]] || continue
    scp "${SSH_OPTS[@]}" "$f" "${SSH_USER}@${SSH_HOST}:${TMP}/"
  done
  if ((${#fw[@]} == 0)); then
    echo "Предупреждение: нет *.fw — только index.json." >&2
  fi
  for bn in index.json; do
    ssh "${SSH_OPTS[@]}" "${SSH_USER}@${SSH_HOST}" \
      "sudo cp $TMP/$bn $REMOTE/$bn && sudo chown bitrix:bitrix $REMOTE/$bn && sudo chmod 644 $REMOTE/$bn && rm -f $TMP/$bn"
  done
  for f in "${fw[@]}"; do
    [[ -f "$f" ]] || continue
    bn=$(basename "$f")
    ssh "${SSH_OPTS[@]}" "${SSH_USER}@${SSH_HOST}" \
      "sudo cp $TMP/$bn $REMOTE/$bn && sudo chown bitrix:bitrix $REMOTE/$bn && sudo chmod 644 $REMOTE/$bn && rm -f $TMP/$bn"
  done
  ssh "${SSH_OPTS[@]}" "${SSH_USER}@${SSH_HOST}" "rmdir $TMP 2>/dev/null || true"
  ssh "${SSH_OPTS[@]}" "${SSH_USER}@${SSH_HOST}" "sudo ls -la $REMOTE"
  echo "Готово (sudo): $REMOTE/"
  exit 0
fi

scp "${SSH_OPTS[@]}" "$DIR/index.json" "${SSH_USER}@${SSH_HOST}:${REMOTE}/"
for f in "${fw[@]}"; do
  [[ -f "$f" ]] || continue
  scp "${SSH_OPTS[@]}" "$f" "${SSH_USER}@${SSH_HOST}:${REMOTE}/"
done
if ((${#fw[@]} == 0)); then
  echo "Предупреждение: нет *.fw — загружен только index.json." >&2
fi
echo "Готово: ${SSH_USER}@${SSH_HOST}:${REMOTE}/"
