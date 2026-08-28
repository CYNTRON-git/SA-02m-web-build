#!/bin/bash
# Монтирование USB (/media/usb) и microSD (mmcblk1/3 → /media/sdcard; eMMC на СА-02м обычно mmcblk2).
# Опционально: автоформат в exFAT при пустой ФС или NTFS — см. /etc/sa02m_storage.conf

ACTION=${1:-}
DEVICE=${2:-}
MOUNT_POINT=""
DEV_PATH=""
TYPE=""

# Fail SAFE: default OFF when the config is missing/unreadable — matches the
# shipped /etc/sa02m_storage.conf (=0) and never lets a lost config turn a
# recoverable mount failure (dirty NTFS, missing ntfs-3g) into a destructive
# reformat.
STORAGE_AUTO_FORMAT=0
if [ -f /etc/sa02m_storage.conf ]; then
  # shellcheck source=/dev/null
  . /etc/sa02m_storage.conf 2>/dev/null || true
fi
case "${STORAGE_AUTO_FORMAT:-0}" in
  1|yes|true|on|ON|Y) STORAGE_AUTO_FORMAT=1 ;;
  *) STORAGE_AUTO_FORMAT=0 ;;
esac

log() {
  logger -t storage-mount "$1"
  echo "$1"
}

set_device_context() {
  local device=${1:-}
  if [ -z "$device" ]; then
    log "Не передано имя устройства"
    return 1
  fi

  if [[ $device == mmcblk1* || $device == mmcblk3* ]]; then
    MOUNT_POINT="/media/sdcard"
    if [[ $device == mmcblk1 || $device == mmcblk3 ]]; then
      # Чаще p1; суперфлоппи без таблицы разделов — только /dev/mmcblkN
      if [ -b "/dev/${device}p1" ]; then
        DEV_PATH="/dev/${device}p1"
      else
        DEV_PATH="/dev/${device}"
      fi
    else
      DEV_PATH="/dev/${device}"
    fi
    TYPE="sdcard"
  else
    MOUNT_POINT="/media/usb"
    DEV_PATH="/dev/${device}"
    TYPE="usb"
  fi
}

format_exfat() {
  if [[ "${TYPE}" == "sdcard" ]]; then
    LABEL="SDCARD_EXFAT"
  else
    LABEL="USB_EXFAT"
  fi

  umount "${DEV_PATH}" 2>/dev/null || true
  log "Форматирование ${DEV_PATH} в exFAT с меткой: ${LABEL}"

  if ! mkfs.exfat -n "${LABEL}" "${DEV_PATH}"; then
    log "Ошибка форматирования ${DEV_PATH}"
    return 1
  fi
  sync
  sleep 2
  return 0
}

probe_fstype() {
  # Читаем FSTYPE с ретраями. При boot-time udev ещё не успевает прочесть
  # таблицу разделов и blkid возвращает пусто. Делаем до 5 попыток с
  # backoff: 0, 0.5, 1, 1.5, 2 c — суммарно ~5 c, для USB это безопасно.
  # Сначала пробуем udev (он берёт значение из стандартного property
  # ID_FS_TYPE), потом blkid как fallback.
  local fs=""
  for i in 1 2 3 4 5; do
    udevadm settle --timeout=2 -E "${DEV_PATH}" 2>/dev/null || true
    fs=$(udevadm info --query=property --name "${DEV_PATH}" 2>/dev/null \
            | awk -F= '/^ID_FS_TYPE=/{print $2; exit}')
    [ -n "$fs" ] && { printf '%s' "$fs"; return; }
    fs=$(blkid -o value -s TYPE "${DEV_PATH}" 2>/dev/null)
    [ -n "$fs" ] && { printf '%s' "$fs"; return; }
    sleep "0.$((i * 5))"
  done
  printf ''
}

is_disk_with_partitions() {
  # Если это «целый диск» (sda, не sda1) И у него есть хотя бы одна
  # партиция — НЕ пытаемся монтировать его как FS: партиция будет
  # обработана отдельным вызовом storage-mount@sdaN.service.
  local base="${DEV_PATH##*/}"
  case "$base" in
    sd[a-z]|mmcblk[0-9]) ;;
    *) return 1 ;;
  esac
  local parts
  parts=$(lsblk -nro NAME "/dev/${base}" 2>/dev/null | tail -n +2 | wc -l)
  [ "$parts" -gt 0 ]
}

try_mount_ntfs() {
  # NTFS: предпочитаем kernel-драйвер ntfs3 (быстрее, без FUSE-оверхеда).
  # Если он отказывается (например файловая система помечена грязной от
  # Windows quick-eject), пробуем ntfs-3g (userspace) — он умеет «replay
  # log» и монтирует более терпимо.
  if mount -t ntfs3 -o rw,noatime,uid=1000,gid=1000,umask=000 \
       "${DEV_PATH}" "${MOUNT_POINT}" 2>/dev/null; then
    log "Успешно смонтировано ${DEV_PATH} (ntfs3 kernel driver)"
    return 0
  fi
  if command -v mount.ntfs-3g >/dev/null 2>&1; then
    if mount -t ntfs-3g -o rw,noatime,uid=1000,gid=1000,umask=000,big_writes,recover \
         "${DEV_PATH}" "${MOUNT_POINT}" 2>/dev/null; then
      log "Успешно смонтировано ${DEV_PATH} (ntfs-3g FUSE fallback)"
      return 0
    fi
  fi
  return 1
}

do_mount() {
  if [ ! -e "${DEV_PATH}" ]; then
    log "Устройство ${DEV_PATH} не найдено"
    return 1
  fi

  if grep -q " ${MOUNT_POINT} " /proc/mounts 2>/dev/null; then
    log "${MOUNT_POINT} уже смонтирован — пропуск повторного mount"
    return 0
  fi

  # Целый диск с таблицей разделов — обработают отдельные сервисы для
  # каждой партиции. Не ругаемся, выходим тихо с return 0.
  if is_disk_with_partitions; then
    log "${DEV_PATH}: диск с таблицей разделов — пропуск, ждём партиции"
    return 0
  fi

  FSTYPE=$(probe_fstype)

  if [[ -z "${FSTYPE}" || "${FSTYPE}" == "ntfs" ]]; then
    mkdir -p "${MOUNT_POINT}"
    chmod 777 "${MOUNT_POINT}"
    if try_mount_ntfs; then
      return 0
    fi
    # The ONLY condition here is the operator's flag. An empty FSTYPE must
    # reach mkfs too — /etc/sa02m_storage.conf and the panel both promise
    # «пустой или NTFS раздел перезаписывается в exFAT», and an extra
    # `-z "${FSTYPE}"` term in this test silently killed half that promise
    # (an empty partition was never formatted). Guarded by the quality row
    # storage-automount-decision.
    if (( STORAGE_AUTO_FORMAT != 1 )); then
      local reason
      if [[ -z "${FSTYPE}" ]]; then
        reason="раздел без распознанной ФС"
      else
        reason="раздел ${FSTYPE} не смонтирован ни ntfs3, ни ntfs-3g"
      fi
      log "${DEV_PATH}: ${reason}, автоформатирование выключено (STORAGE_AUTO_FORMAT=${STORAGE_AUTO_FORMAT} в /etc/sa02m_storage.conf) — пропуск без mkfs."
      # Намеренный «нечего монтировать» — НЕ ошибка сервиса. Возвращаем 0,
      # чтобы systemd не показывал unit как failed (UI «1 loaded units listed»
      # после каждой загрузки сбивал с толку).
      return 0
    fi
    if ! format_exfat; then
      return 1
    fi
    FSTYPE="exfat"
  fi

  mkdir -p "${MOUNT_POINT}"
  chmod 777 "${MOUNT_POINT}"

  OPTS="rw,noatime,nodiratime,uid=1000,gid=1000"
  case "${FSTYPE}" in
    vfat|exfat) OPTS+=",umask=000,dmask=000,fmask=000" ;;
    ext4)       OPTS+=",user_xattr,noexec,errors=remount-ro" ;;
    *)          OPTS+=",umask=000" ;;
  esac

  log "Монтирование ${DEV_PATH} (${FSTYPE}) в ${MOUNT_POINT} с опциями: ${OPTS}"

  for attempt in {1..3}; do
    if mount -o "${OPTS}" -t "${FSTYPE}" "${DEV_PATH}" "${MOUNT_POINT}"; then
      log "Успешно смонтировано ${DEV_PATH}"
      # Автозапуск autorun.sh с USB выполняет код как root с недоверенного
      # носителя — по умолчанию ВЫКЛ. Включается только явно:
      # STORAGE_ALLOW_AUTORUN=1 в /etc/sa02m_storage.conf.
      if [ "${TYPE}" = "usb" ] && [ "${STORAGE_ALLOW_AUTORUN:-0}" = "1" ] && [ -f "${MOUNT_POINT}/autorun.sh" ]; then
        log "autorun.sh обнаружен на ${MOUNT_POINT} — запуск в фоне (STORAGE_ALLOW_AUTORUN=1)"
        (bash "${MOUNT_POINT}/autorun.sh" >> /var/log/flash-receiver.log 2>&1) &
      elif [ "${TYPE}" = "usb" ] && [ -f "${MOUNT_POINT}/autorun.sh" ]; then
        log "autorun.sh обнаружен, но STORAGE_ALLOW_AUTORUN не включён — пропуск"
      fi
      return 0
    fi

    log "Попытка восстановления: fsck -a ${DEV_PATH}"
    fsck -a "${DEV_PATH}" 2>/dev/null || true
    sleep 2
  done

  log "Критическая ошибка монтирования ${DEV_PATH}"
  return 1
}

do_unmount() {
  if grep -q "${MOUNT_POINT}" /proc/mounts; then
    log "Размонтирование ${MOUNT_POINT}"
    umount -l "${MOUNT_POINT}" || umount -f "${MOUNT_POINT}"
  fi

  if [ "${TYPE}" = "sdcard" ] && [ -d "${MOUNT_POINT}" ]; then
    find "${MOUNT_POINT}" -mindepth 1 -delete 2>/dev/null || true
  elif [ "${TYPE}" = "usb" ] && [ -d "${MOUNT_POINT}" ]; then
    rmdir "${MOUNT_POINT}" 2>/dev/null || true
  fi

  log "Устройство ${DEV_PATH} полностью отключено"
}

is_usb_candidate() {
  local device=$1
  local base=${device%%[0-9]*}

  if [ -r "/sys/block/${base}/removable" ] && [ "$(cat "/sys/block/${base}/removable" 2>/dev/null)" = "1" ]; then
    return 0
  fi

  udevadm info --query=property --name "/dev/${device}" 2>/dev/null | grep -Eq '^(ID_BUS=usb|ID_PATH=.*usb)'
}

scan_and_mount() {
  local name type pk target
  local scanned=0
  local succeeded=0
  local seen=" "

  while read -r name type pk; do
    [ -n "${name:-}" ] || continue
    target=""

    case "${name}:${type}" in
      mmcblk1:disk|mmcblk3:disk)
        target="$name"
        ;;
      mmcblk1p*:part|mmcblk3p*:part)
        target="${pk:-${name%%p*}}"
        ;;
      sd[a-z]:disk)
        if is_usb_candidate "$name"; then
          target="$name"
        fi
        ;;
      sd[a-z][0-9]*:part)
        if is_usb_candidate "$name" || { [ -n "${pk:-}" ] && is_usb_candidate "$pk"; }; then
          target="$name"
        fi
        ;;
    esac

    [ -n "$target" ] || continue
    case "$seen" in
      *" ${target} "*) continue ;;
    esac
    seen="${seen}${target} "
    scanned=$((scanned + 1))

    if set_device_context "$target" && do_mount; then
      succeeded=$((succeeded + 1))
    fi
  done < <(lsblk -nr -o NAME,TYPE,PKNAME 2>/dev/null)

  if (( scanned > 0 && succeeded == 0 )); then
    log "Ни один найденный накопитель не удалось смонтировать"
    return 1
  fi
  return 0
}

case "${ACTION}" in
  add) set_device_context "$DEVICE" && do_mount ;;
  remove) set_device_context "$DEVICE" && do_unmount ;;
  scan) scan_and_mount ;;
  *) log "Неверное действие: ${ACTION}" ;;
esac
