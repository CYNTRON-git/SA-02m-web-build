#!/bin/bash
# Монтирование USB (/media/usb) и microSD на mmcblk3 (/media/sdcard).
# Опционально: автоформат в exFAT при пустой ФС или NTFS — см. /etc/sa02m_storage.conf

ACTION=$1
DEVICE=$2

if [[ $DEVICE == mmcblk3* ]]; then
  MOUNT_POINT="/media/sdcard"
  if [[ $DEVICE == mmcblk3 ]]; then
    DEV_PATH="/dev/${DEVICE}p1"
  else
    DEV_PATH="/dev/${DEVICE}"
  fi
  TYPE="sdcard"
else
  MOUNT_POINT="/media/usb"
  DEV_PATH="/dev/${DEVICE}"
  TYPE="usb"
fi

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

case "${ACTION}" in
  add) do_mount ;;
  remove) do_unmount ;;
  *) log "Неверное действие: ${ACTION}" ;;
esac
