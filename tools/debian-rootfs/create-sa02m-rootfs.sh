#!/usr/bin/env bash
#
# create-sa02m-rootfs.sh — Debian bullseye armhf rootfs для SA-02m
# (аналог WB create_rootfs.sh, но с linux-image-sa02m + install.sh).
#
# Usage:
#   sudo ./tools/debian-rootfs/create-sa02m-rootfs.sh [options]
#
# Options:
#   --output DIR           каталог rootfs (default: ~/build/sa02m-bullseye-rootfs)
#   --kernel-deb-dir DIR   где лежат linux-image-*.deb (default: ~/build/sa02m-kernel)
#   --repo PATH            sa02m-web-build (default: auto)
#   --variant V            sa02m-1eth | sa02m-2eth (default: sa02m-1eth)
#   --ip ADDR              default IP eth0 (default: 192.168.1.136 / .0.136 for 2eth)
#   --gw ADDR              default gateway
#   --pass PASS            root + web admin (default: cyntron)
#   --skip-debootstrap     переиспользовать существующий --output
#   --skip-install         только debootstrap + kernel, без install.sh
#   --tarball              упаковать rootfs в .tar.gz рядом с output
#
# Host: Debian 11+ / Ubuntu 20.04+ x86_64, root, debootstrap, qemu-user-static.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUTPUT="${SA02M_ROOTFS_OUTPUT:-$HOME/build/sa02m-bullseye-rootfs}"
KERNEL_DEB_DIR="${SA02M_KERNEL_DEB_DIR:-$HOME/build/sa02m-kernel}"
VARIANT="sa02m-1eth"
IP=""
GW=""
ADMIN_PASS="cyntron"
SKIP_DEBOOT=0
SKIP_INSTALL=0
MAKE_TARBALL=0
DEBIAN_RELEASE="${DEBIAN_RELEASE:-bullseye}"
DEBIAN_MIRROR="${DEBIAN_MIRROR:-http://deb.debian.org/debian}"
ARCH=armhf

usage() {
	sed -n '2,22p' "$0" | sed 's/^# \?//'
	exit 2
}

while [ "$#" -gt 0 ]; do
	case "$1" in
		--output) OUTPUT="$2"; shift 2 ;;
		--kernel-deb-dir) KERNEL_DEB_DIR="$2"; shift 2 ;;
		--repo) REPO_ROOT="$2"; shift 2 ;;
		--variant) VARIANT="$2"; shift 2 ;;
		--ip) IP="$2"; shift 2 ;;
		--gw) GW="$2"; shift 2 ;;
		--pass) ADMIN_PASS="$2"; shift 2 ;;
		--skip-debootstrap) SKIP_DEBOOT=1; shift ;;
		--skip-install) SKIP_INSTALL=1; shift ;;
		--tarball) MAKE_TARBALL=1; shift ;;
		-h|--help) usage ;;
		*) echo "unknown arg: $1" >&2; usage ;;
	esac
done

case "$VARIANT" in
	sa02m-1eth)
		IP="${IP:-192.168.1.136}"
		GW="${GW:-192.168.1.1}"
		;;
	sa02m-2eth)
		IP="${IP:-192.168.0.136}"
		GW="${GW:-192.168.0.1}"
		;;
	*)
		echo "bad --variant: $VARIANT" >&2
		exit 2
		;;
esac

if [ "$EUID" -ne 0 ]; then
	echo "run as root: sudo $0 ..." >&2
	exit 2
fi

command -v debootstrap >/dev/null || { echo "install debootstrap" >&2; exit 2; }

log() { printf '[create-sa02m-rootfs] %s\n' "$*"; }

find_kernel_debs() {
	local d="$1"
	find "$d" -maxdepth 1 -type f \( \
		-name 'linux-image-*sa02m*.deb' -o \
		-name 'linux-headers-*sa02m*.deb' -o \
		-name 'linux-libc-dev_*sa02m*.deb' \
	\) | sort
}

KERNEL_DEBS=()
while IFS= read -r f; do
	[ -n "$f" ] && KERNEL_DEBS+=("$f")
done < <(find_kernel_debs "$KERNEL_DEB_DIR")

if [ "${#KERNEL_DEBS[@]}" -eq 0 ]; then
	echo "error: no linux-image-sa02m *.deb in $KERNEL_DEB_DIR" >&2
	echo "  run: tools/kernel-wb/build-sa02m-kernel.sh sa02m" >&2
	exit 2
fi

BASE_PKGS=(
	ca-certificates
	systemd
	systemd-sysv
	openssh-server
	sudo
	locales
	ifupdown
	isc-dhcp-client
	iproute2
	iptables
	nftables
	net-tools
	psmisc
	curl
	wget
	python3
	python3-minimal
	openssl
	udev
	kmod
	rsync
	htop
	vim-tiny
	less
	# Утилиты для sa02m-rootfs-expand.service (первая загрузка после PiShrink)
	parted
	fdisk
	cloud-guest-utils
	e2fsprogs
	# fake-hwclock — сохраняет время между перезагрузками без RTC питания
	fake-hwclock
	# DS3231 RTC utilities
	util-linux
	# nftables — kernel 5.10.35-sa02m+ БЕЗ CONFIG_NF_TABLES, сервис маскируется
	# в scripts/02-network.sh, но пакет нужен как depend для iptables-nft.
	iptables-nft
	# Сетевая диагностика (ping, dig, host)
	iputils-ping
	dnsutils
	# I2C / GPIO — обязательны для PCA9536 (бипер + синий boot LED),
	# USB VBUS (gpio 268), sa02m-pre-start, веб CGI управления GPIO.
	# Без i2c-tools и gpiod не работают опрос микросхемы расширения,
	# индикация загрузки и питание USB.
	i2c-tools
	gpiod
	libgpiod2
	python3-libgpiod
	# HDD/USB SMART, диагностика (в комплекте с i2c-tools часто)
	# ── Python3 для CGI/скриптов
	python3-pip
	python3-yaml
	python3-paho-mqtt
	python3-serial
	# ── USB-модемы (3G/4G/LTE): PPP + ModemManager + QMI/MBIM utils.
	# Стек ставится сразу в rootfs, чтобы модем поднимался автоматически
	# без интернета (после первой загрузки apt может быть недоступен).
	modemmanager
	ppp
	usb-modeswitch
	usb-modeswitch-data
	libqmi-utils
	libmbim-utils
	usbutils
	# ── Runtime-зависимости для vendor-стека (CODESYS SL, MPLC 4D).
	# Ставим сразу в rootfs, чтобы vendor install-скрипты (scripts/08-codesys.sh,
	# scripts/09-mplc.sh) не требовали apt после первой прошивки. Все пакеты —
	# из штатного Debian bullseye main; codemeter-lite (WIBU) в main отсутствует,
	# CODESYS ставим с --force-depends и запускаем в demo-режиме до активации.
	libssl1.1
	zlib1g
	libstdc++6
	libgcc-s1
	libudev1
	libpcre3
	libatomic1
)

log "output       = $OUTPUT"
log "repo         = $REPO_ROOT"
log "variant      = $VARIANT"
log "ip/gw        = $IP / $GW"
log "kernel debs  = ${#KERNEL_DEBS[@]} from $KERNEL_DEB_DIR"
log "debian       = $DEBIAN_RELEASE ($ARCH)"

if [ "$SKIP_DEBOOT" = 0 ]; then
	[ -d "$OUTPUT" ] && rm -rf "$OUTPUT"
	mkdir -p "$OUTPUT"

	log "debootstrap stage 1 (--foreign)"
	debootstrap --foreign --arch="$ARCH" --variant=minbase \
		--include="$(IFS=,; echo "${BASE_PKGS[*]}")" \
		"$DEBIAN_RELEASE" "$OUTPUT" "$DEBIAN_MIRROR"

	QEMU=""
	for q in /usr/bin/qemu-arm-static /usr/bin/qemu-arm; do
		[ -x "$q" ] && QEMU="$q" && break
	done
	if [ -z "$QEMU" ]; then
		echo "error: qemu-arm-static not found (apt install qemu-user-static)" >&2
		exit 2
	fi
	cp "$QEMU" "$OUTPUT/usr/bin/"
	modprobe binfmt_misc 2>/dev/null || true

	log "debootstrap stage 2"
	chroot "$OUTPUT" /debootstrap/debootstrap --second-stage

	log "base config (hostname, fstab, os-release)"
	echo "SA-02" > "$OUTPUT/etc/hostname"
	cat > "$OUTPUT/etc/hosts" <<'EOF'
127.0.0.1 localhost SA-02
::1       localhost ip6-localhost ip6-loopback
EOF
	cat > "$OUTPUT/etc/fstab" <<'EOF'
# SA-02m eMMC layout (Starterkit / Cyntron)
#
# ВАЖНО: используем LABEL= (устойчиво к смене нумерации eMMC/SD при новом kernel/DTS).
# У boot FAT — nofail + x-systemd.automount, чтобы сбой FAT не ронял local-fs.target
# и не отправлял устройство в emergency (без сети).
LABEL=sa02m_root  /              ext4  defaults,noatime,errors=remount-ro                              0 1
LABEL=BOOT        /mnt/boot_fat  vfat  defaults,nofail,x-systemd.device-timeout=5s,x-systemd.automount 0 0
EOF
	mkdir -p "$OUTPUT/mnt/boot_fat"
	cat > "$OUTPUT/etc/os-release" <<EOF
PRETTY_NAME="ЦИНТРОН SA-02m (Debian ${DEBIAN_RELEASE})"
NAME="Debian GNU/Linux"
VERSION_ID="11"
VERSION="11 (bullseye)"
ID=debian
ID_LIKE=debian
VENDOR="ЦИНТРОН"
VENDOR_URL="https://cyntron.ru/"
HOME_URL="https://cyntron.ru/"
SUPPORT_URL="https://cyntron.ru/"
EOF
	ln -sf os-release "$OUTPUT/etc/sa02m-os-release"

	echo "en_US.UTF-8 UTF-8" > "$OUTPUT/etc/locale.gen"
	echo "ru_RU.UTF-8 UTF-8" >> "$OUTPUT/etc/locale.gen"
	chroot "$OUTPUT" locale-gen
	chroot "$OUTPUT" update-locale LANG=ru_RU.UTF-8

	echo "root:${ADMIN_PASS}" | chroot "$OUTPUT" chpasswd
	echo "SA02M_HW_VARIANT=${VARIANT}" > "$OUTPUT/etc/sa02m_hw_variant.conf"

	cat > "$OUTPUT/etc/apt/sources.list" <<EOF
deb ${DEBIAN_MIRROR} ${DEBIAN_RELEASE} main
deb ${DEBIAN_MIRROR} ${DEBIAN_RELEASE}-updates main
deb http://security.debian.org/debian-security ${DEBIAN_RELEASE}-security main
EOF

	log "apt update in chroot"
	cp /etc/resolv.conf "$OUTPUT/etc/resolv.conf"
	chroot "$OUTPUT" apt-get update -qq
	chroot "$OUTPUT" apt-get install -y -qq "${BASE_PKGS[@]}" 2>/dev/null || \
		chroot "$OUTPUT" apt-get install -y -qq ca-certificates systemd openssh-server sudo locales ifupdown
fi

if [ ! -d "$OUTPUT/etc" ]; then
	echo "error: $OUTPUT is not a rootfs" >&2
	exit 2
fi

log "install kernel .deb (${#KERNEL_DEBS[@]})"
mkdir -p "$OUTPUT/tmp/sa02m-kernel-deb"
for deb in "${KERNEL_DEBS[@]}"; do
	cp -v "$deb" "$OUTPUT/tmp/sa02m-kernel-deb/"
done
# postinst hook до dpkg — install.sh тоже ставит, но нужен до первого linux-image
if [ -f "$REPO_ROOT/etc/kernel-postinst.d/50-sa02m-fat-sync" ]; then
	install -d -m 755 "$OUTPUT/etc/kernel/postinst.d"
	install -m 755 "$REPO_ROOT/etc/kernel-postinst.d/50-sa02m-fat-sync" \
		"$OUTPUT/etc/kernel/postinst.d/50-sa02m-fat-sync"
	sed -i 's/\r$//' "$OUTPUT/etc/kernel/postinst.d/50-sa02m-fat-sync"
fi
chroot "$OUTPUT" /bin/bash -c 'dpkg -i /tmp/sa02m-kernel-deb/*.deb' || {
	log "WARN: dpkg kernel had errors — trying dpkg --configure -a"
	chroot "$OUTPUT" /bin/bash -c 'dpkg --configure -a' || true
}
rm -rf "$OUTPUT/tmp/sa02m-kernel-deb"

log "copy sa02m-web-build → /opt/sa02m-web-build"
rm -rf "$OUTPUT/opt/sa02m-web-build"
mkdir -p "$OUTPUT/opt/sa02m-web-build"
tar -C "$REPO_ROOT" \
	--exclude='.git' \
	--exclude='tools/buildroot' \
	-cf - . | tar -C "$OUTPUT/opt/sa02m-web-build" -xf -

chmod +x "$OUTPUT/opt/sa02m-web-build/install.sh" \
	"$OUTPUT/opt/sa02m-web-build/scripts/"*.sh \
	"$OUTPUT/opt/sa02m-web-build/tools/debian-rootfs/"*.sh 2>/dev/null || true

# ── vendor-payload (CODESYS + MPLC + Node-RED) → /opt/vendor-installers/ ───
# Копируем большие проприетарные/собранные пакеты ТОЛЬКО если каталоги
# существуют на build-host (в репо их нет — см. .gitignore /vendor/). Позволяет
# получать готовый rootfs, с которого устройство ставит стек без сети и pscp.
#
# Ожидаемая структура на build-host:
#   $REPO_ROOT/vendor/codesys/codesyscontrol_linuxarm_*.deb
#   $REPO_ROOT/vendor/mplc4/{install.sh,mplc4.tar.gz,nginx.tar.gz,mplc_cyntron.so}
#   $REPO_ROOT/vendor/nodered/{node-red-*.tar.gz,node-v*-linux-armv7l.tar.xz,
#                              nodered.service,BUILD-INFO.txt}
#
# Список ЯВНЫЙ, а не vendor/* : glob молча унёс бы в образ всё, что кто-то
# оставил в vendor/. Но и молча терять каталог нельзя — раньше vendor/nodered
# исчезал без единой строки в логе, и свежепрошитая плата отвечала
# staging_missing. Незнакомый каталог теперь виден как WARN (ниже).
VENDOR_SUBS="codesys mplc4 nodered"
vendor_any=0
for sub in $VENDOR_SUBS; do
	[ -d "$REPO_ROOT/vendor/$sub" ] && vendor_any=1
done
if [ -d "$REPO_ROOT/vendor" ]; then
	for d in "$REPO_ROOT"/vendor/*/; do
		[ -d "$d" ] || continue
		name=$(basename "$d")
		case " $VENDOR_SUBS " in
			*" $name "*) ;;
			*) log "WARN: vendor/$name не в списке известных ($VENDOR_SUBS) — в образ НЕ копируется" ;;
		esac
	done
fi
if [ "$vendor_any" = 1 ]; then
	log "copy vendor-payload ($VENDOR_SUBS) → /opt/vendor-installers/"
	install -d -m 0755 "$OUTPUT/opt/vendor-installers"
	for sub in $VENDOR_SUBS; do
		if [ -d "$REPO_ROOT/vendor/$sub" ]; then
			install -d -m 0755 "$OUTPUT/opt/vendor-installers/$sub"
			cp -a "$REPO_ROOT/vendor/$sub/." "$OUTPUT/opt/vendor-installers/$sub/"
			log "  vendor/$sub: $(du -sh "$OUTPUT/opt/vendor-installers/$sub" 2>/dev/null | awk '{print $1}')"
		fi
	done
	# Repo-owned runtime assets the on-device install/uninstall entry-point
	# (etc/sa02m-web-service-ctl.sh) reads from the vendor dirs: the CODESYS
	# systemd drop-in and the ЦИНТРОН MPLC plugin. Staged here so a freshly
	# imaged device can (re)install CODESYS/MPLC from the panel without a pscp.
	if [ -d "$OUTPUT/opt/vendor-installers/codesys" ] \
	   && [ -f "$REPO_ROOT/etc/systemd/system/codesyscontrol.service.d/sa02m.conf" ]; then
		install -m 0644 "$REPO_ROOT/etc/systemd/system/codesyscontrol.service.d/sa02m.conf" \
			"$OUTPUT/opt/vendor-installers/codesys/sa02m.conf"
		log "  vendor/codesys: staged systemd drop-in sa02m.conf"
	fi
	if [ -d "$OUTPUT/opt/vendor-installers/nodered" ] \
	   && [ -f "$REPO_ROOT/etc/systemd/system/nodered.service" ]; then
		# Unit — репозиторный артефакт: кладём копию из репо поверх той, что
		# положил сборщик payload'а. Дом один, и он здесь, а не в payload'е.
		install -m 0644 "$REPO_ROOT/etc/systemd/system/nodered.service" \
			"$OUTPUT/opt/vendor-installers/nodered/nodered.service"
		log "  vendor/nodered: staged nodered.service"
	fi
	if [ -d "$OUTPUT/opt/vendor-installers/mplc4" ] \
	   && [ -f "$REPO_ROOT/firmware/mplc4/mplc_cyntron.so" ]; then
		install -m 0755 "$REPO_ROOT/firmware/mplc4/mplc_cyntron.so" \
			"$OUTPUT/opt/vendor-installers/mplc4/mplc_cyntron.so"
		log "  vendor/mplc4: staged mplc_cyntron.so"
	fi
fi

if [ "$SKIP_INSTALL" = 0 ]; then
	log "run install.sh in chroot (SA02M_ROOTFS_BUILD=1)"
	mount --bind /dev "$OUTPUT/dev"
	mount --bind /dev/pts "$OUTPUT/dev/pts"
	mount -t proc proc "$OUTPUT/proc"
	mount -t sysfs sysfs "$OUTPUT/sys"
	trap 'umount -lf "$OUTPUT/sys" "$OUTPUT/proc" "$OUTPUT/dev/pts" "$OUTPUT/dev" 2>/dev/null || true' EXIT

	set +e
	chroot "$OUTPUT" env SA02M_ROOTFS_BUILD=1 SA02M_ROOTFS_ROOT=/ \
		/bin/bash -c "cd /opt/sa02m-web-build && ./install.sh \
			--variant ${VARIANT} --ip ${IP} --gw ${GW} --pass ${ADMIN_PASS}"
	install_rc=$?
	set -e
	umount -lf "$OUTPUT/sys" "$OUTPUT/proc" "$OUTPUT/dev/pts" "$OUTPUT/dev" 2>/dev/null || true
	trap - EXIT

	if [ "$install_rc" -ne 0 ]; then
		log "WARN: install.sh exited $install_rc (systemctl start in chroot is expected to fail partially)"
	else
		log "install.sh OK"
	fi
fi

log "cleanup apt caches"
chroot "$OUTPUT" apt-get clean 2>/dev/null || true
rm -rf "$OUTPUT/var/lib/apt/lists/"*
mkdir -p "$OUTPUT/var/lib/apt/lists/partial"

# machine-id for first boot
echo "uninitialized" > "$OUTPUT/etc/machine-id"
rm -f "$OUTPUT/var/lib/dbus/machine-id"

if [ "$MAKE_TARBALL" = 1 ]; then
	TARBALL="${OUTPUT%/}.tar.gz"
	log "tarball → $TARBALL"
	tar -C "$(dirname "$OUTPUT")" -czpf "$TARBALL" "$(basename "$OUTPUT")"
fi

log "done: $OUTPUT"
log "kernel: $(chroot "$OUTPUT" uname -r 2>/dev/null || ls "$OUTPUT/lib/modules" 2>/dev/null | head -1)"
log "next: tools/imaging/ (pack to eMMC image) or rsync rootfs to donor partition"
