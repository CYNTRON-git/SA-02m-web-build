#!/usr/bin/env bash
# Выгрузка firmware-site-export на 84.201.134.96 (promprog): scp + при пути /home/bitrix/* — staging + sudo.
set -euo pipefail
SSH_HOST="84.201.134.96"
SSH_USER="promprog"
REMOTE="${1:?Укажите каталог, например: $0 /home/bitrix/ext_www/promprog.store/upload/medialibrary/cyntron/firmware}"
DIR="$(cd "$(dirname "$0")" && pwd)"
REMOTE="${REMOTE%/}"
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
