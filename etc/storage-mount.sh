#!/bin/bash
# Монтирование USB (/media/usb) и microSD (mmcblk1/3 → /media/sdcard; eMMC на СА-02м обычно mmcblk2).
# Опционально: автоформат в exFAT при пустой ФС или NTFS — см. /etc/sa02m_storage.conf

ACTION=${1:-}
DEVICE=${2:-}
MOUNT_POINT=""
DEV_PATH=""
TYPE=""

STORAGE_AUTO_FORMAT=1
if [ -f /etc/sa02m_storage.conf ]; then
  # shellcheck source=/dev/null
  . /etc/sa02m_storage.conf 2>/dev/null || true
fi
case "${STORAGE_AUTO_FORMAT:-1}" in
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

do_mount() {
  if [ ! -e "${DEV_PATH}" ]; then
    log "Устройство ${DEV_PATH} не найдено"
    return 1
  fi

  if grep -q " ${MOUNT_POINT} " /proc/mounts 2>/dev/null; then
    log "${MOUNT_POINT} уже смонтирован — пропуск повторного mount"
    return 0
  fi

  FSTYPE=$(blkid -o value -s TYPE "${DEV_PATH}" 2>/dev/null)

  if [[ -z "${FSTYPE}" || "${FSTYPE}" == "ntfs" ]]; then
    if (( STORAGE_AUTO_FORMAT != 1 )); then
      log "Автоформатирование отключено (STORAGE_AUTO_FORMAT=0 в /etc/sa02m_storage.conf). Раздел ${DEV_PATH} без подходящей ФС для монтирования без mkfs — пропуск."
      return 1
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
