# tools/debian-rootfs — Debian bullseye rootfs для SA-02m

Сборка **userspace Debian 11 armhf**, с ядром из `.deb`-пакета `linux-image-sa02m` и нашим `install.sh`.

> ⚠ **Производителя этих `.deb` в репозитории больше нет.** Пакеты собирал порт
> ядра на форк `wirenboard/linux` (`kernel-port/` + `tools/kernel-wb/`) — он до
> устройств не дошёл и удалён; флот несёт `6.1.0-rc6` / `6.1.0-rc6-rt4` из
> [`../buildroot/`](../buildroot/README.md), а тот отдаёт `zImage` + tarball
> модулей, а не `.deb` ([`../../.ai-dev/notes/kernel-line.md`](../../.ai-dev/notes/kernel-line.md)).
> Чтобы воспользоваться этим rootfs-путём, `.deb` придётся принести своими
> руками. Судьба самого каталога — отдельное решение, не это.

## Отличие от Armbian/Noble

| | Armbian (сейчас на 192.168.1.136) | Этот rootfs |
|---|-----------------------------------|-------------|
| Base | Ubuntu 24.04 + Armbian | **Debian bullseye** |
| Ядро | 6.1.0-rc6 Buildroot | **5.10.35-sa02m** (.deb) |
| APT | ports.ubuntu + armbian | deb.debian.org |

## Требования (build host)

```bash
sudo apt install debootstrap qemu-user-static binfmt-support \
                 rsync tar gzip
```

На Ubuntu 18.04 VM (наш VMware) — пакеты доступны.

Перед сборкой rootfs нужны **собранные kernel .deb** — положите их в
`KERNEL_DEB_DIR` (см. `create-sa02m-rootfs.sh`). Скрипта-производителя в
репозитории нет: см. предупреждение в начале файла.

## Быстрый старт

```bash
sudo bash tools/debian-rootfs/create-sa02m-rootfs.sh \
  --variant sa02m-1eth \
  --ip 192.168.1.136 \
  --pass cyntron \
  --tarball
```

Выход: `~/build/sa02m-bullseye-rootfs/` (+ опционально `.tar.gz`).

## Что делает скрипт

1. `debootstrap bullseye armhf`
2. Базовые пакеты (systemd, ssh, ifupdown, python3, …)
3. `dpkg -i linux-image-sa02m*.deb` + headers + libc-dev
4. Копирует репозиторий в `/opt/sa02m-web-build`
5. `install.sh` в chroot (`SA02M_ROOTFS_BUILD=1` — enable unit'ов без start)

## После сборки

### Упаковка в образ eMMC + USB

```bash
# raw .img → PiShrink → .img.xz (+ sha256 + manifest)
sudo bash tools/debian-rootfs/pack-sa02m-image.sh \
  --rootfs ~/build/sa02m-bullseye-rootfs \
  --out-dir ./tools/imaging/out \
  --name sa02m-1eth-bullseye-v1.0.3.37 \
  --profile sa02m-1eth --version 1.0.3.37

# USB-носитель для flash-receiver (устройство само пишет eMMC)
./tools/debian-rootfs/prepare-sa02m-flash-usb.sh \
  --image ./tools/imaging/out/sa02m-1eth-bullseye-v1.0.3.37-shrunk.img.xz \
  --dest /mnt/usb/SA02m
```

На Windows: распаковать образ для ImageUSB — `tools/imaging/prepare-imageusb.sh`.

FAT boot (`zImage`, `sun8i-r40-sa02m.dtb` + fallback имена, `boot.scr`) заполняется при упаковке; на первой загрузке postinst может синхронизировать снова.

### U-Boot в образе (важно!)

`pack-sa02m-image.sh` **встраивает** `u-boot-sunxi-with-spl.bin` в первые ~1 MiB raw образа (offset 8 KiB), потому что `flash-receiver.sh` пишет `dd of=/dev/mmcblk2` целиком и **затирает** существующий загрузчик Armbian/Starterkit. Без встроенного U-Boot устройство не грузится и не поднимает сеть после прошивки.

Файл лежит в `tools/imaging/boot/u-boot-sunxi-with-spl.bin` (извлечён из работающего Armbian `SA-02m-v1.0.3.35.bin`: `dd if=… bs=512 skip=1 count=2048 | dd bs=1024 skip=8`).

Опции pack:

```
--uboot PATH       путь к u-boot-sunxi-with-spl.bin (default: tools/imaging/boot/…)
--no-uboot         НЕ встраивать U-Boot (только если dd будет пропускать offset 8 KiB!)
```

Rootfs **без** bootable `.img` до шага `pack-sa02m-image.sh`.

## Опции

```
--output DIR           каталог rootfs
--kernel-deb-dir DIR   ~/build/sa02m-kernel
--repo PATH            путь к sa02m-web-build
--variant sa02m-1eth|sa02m-2eth
--skip-debootstrap     только kernel + install (пересборка поверх)
--skip-install         только debootstrap + kernel
--tarball              sa02m-bullseye-rootfs.tar.gz
```
