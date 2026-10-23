#!/bin/bash
echo "== uname"; uname -a; cat /proc/cmdline
echo "== mounts"; findmnt -rn -o TARGET,SOURCE,FSTYPE | grep -E 'mmcblk|vfat' 
F=$(findmnt -rn -t vfat -o TARGET | head -1); [ -z "$F" ] && { mkdir -p /mnt/fatchk; mount -o ro /dev/mmcblk2p1 /mnt/fatchk 2>/dev/null && F=/mnt/fatchk; }
echo "== FAT $F"; ls -la --time-style=+%F "$F" 2>/dev/null | head -30; (cd "$F" && md5sum $(ls | grep -vE '^System' ) 2>/dev/null)
ls -la "$F"/dtb* "$F"/overlay* 2>/dev/null | head
echo "== /boot"; ls -la --time-style=+%F /boot 2>/dev/null | head -30
echo "== share"; ls -la --time-style=+%F /usr/local/share/sa02m/ 2>/dev/null; md5sum /usr/local/share/sa02m/* 2>/dev/null
echo "== uboot on emmc"; dd if=/dev/mmcblk2 bs=1k skip=8 count=1000 2>/dev/null | strings | grep -m3 -E 'U-Boot (SPL )?20'
echo "== dmesg wdt/rtc/psci/clk"; dmesg | grep -iE 'wdt|watchdog|rtc|psci|osc32|losc|restart' | head -30
echo "== wdt sysfs"; for f in identity timeout status bootstatus state nowayout; do printf '%s=' $f; cat /sys/class/watchdog/watchdog0/$f 2>/dev/null; done
echo "== who holds watchdog"; ls -l /proc/*/fd 2>/dev/null | grep -B0 watchdog | head; for p in /proc/[0-9]*; do ls -l $p/fd 2>/dev/null | grep -q watchdog && echo "$(basename $p) $(tr '\0' ' ' < $p/cmdline)"; done
echo "== lsmod"; lsmod | awk 'NR>1{print $1}' | sort | tr '\n' ' '; echo
echo "== clk_summary osc32/rtc"; cat /sys/kernel/debug/clk/clk_summary 2>/dev/null | grep -iE 'osc32|losc|rtc|ext-osc|iosc|osc24' | head
echo "== dpkg linux/u-boot/armbian"; dpkg -l 2>/dev/null | awk '/^ii/ && $2 ~ /linux|u-boot|armbian|firmware/ {print $2, $3}'
echo "== modprobe/udev sa02m"; ls /etc/modprobe.d /etc/modules-load.d /etc/udev/rules.d 2>/dev/null | tr '\n' ' '; echo
