#!/bin/bash
set -euo pipefail
profile=$1
case "$profile" in
    smp) src=/trial/linux; obj=/trial/obj; suffix=-sa02m-trial-smp; number=5 ;;
    rt) src=/trial/rt-linux; obj=/trial/rt-obj; suffix=-sa02m-trial; number=1 ;;
    *) exit 2 ;;
esac
export LC_ALL=C ARCH=arm HOSTCC=gcc
export KBUILD_BUILD_USER=cyntron KBUILD_BUILD_HOST=offline-lts KBUILD_BUILD_VERSION=$number
export KBUILD_BUILD_TIMESTAMP='Fri Nov 27 00:00:00 UTC 2026'
host=/work/profiles/$profile/output/host
export CROSS_COMPILE=$host/bin/arm-none-linux-gnueabihf-
seed=/work/profiles/$profile/output/build/linux-custom/.config
mkdir -p /trial/patches
cd /trial/patches
for patch in /inputs/*.patch.gz; do
    name=$(basename "$patch" .gz)
    gzip -dc "$patch" > "$name"
done
sha256sum --strict -c /inputs/source.sha256
(cd "$host" && sha256sum --strict -c /inputs/toolchain.sha256)
mkdir -p "$src" "$obj" /trial/install /out
tar -xJf /linux.tar.xz --strip-components=1 -C "$src"
cd "$src"
git apply /trial/patches/01-starterkit.patch
git apply /trial/patches/02-sunxi-audio.patch
if [[ $profile == rt ]]; then git apply /trial/patches/03-rt72.patch; fi
cp "$seed" "$obj/.config"
scripts/config --file "$obj/.config" -m IPV6 -m TUN -m WIREGUARD
opts=(O="$obj" LOCALVERSION="$suffix")
make "${opts[@]}" olddefconfig
cp "$obj/.config" /out/kernel.config
(cd /out && sha256sum --strict -c "/inputs/$profile.config.sha256")
make -j2 "${opts[@]}" zImage modules
make -j2 "${opts[@]}" INSTALL_MOD_PATH=/trial/install INSTALL_MOD_STRIP=1 modules_install
release=$(cat "$obj/include/config/kernel.release")
moddir=lib/modules/$release
cp "$obj/arch/arm/boot/zImage" "$obj/Module.symvers" "$obj/System.map" /out/
tar --sort=name --mtime=@0 --numeric-owner --owner=0 --group=0 \
    --exclude="$moddir/build" --exclude="$moddir/source" \
    -C /trial/install -cf - "$moddir" | gzip -n > "/out/modules-$release.tar.gz"
readelf -n "$obj/vmlinux" | grep 'Build ID' > /out/build-id.txt
"${CROSS_COMPILE}gcc" -Wall -Wextra -Werror -Os -static -s "-DEXPECT_RELEASE=\"$release\"" \
    -o /trial/passive-init /inputs/passive.c
touch -d @0 /trial/passive-init
{
    for name in proc dev sys; do echo "dir /$name 0555 0 0"; done
    echo 'nod /dev/console 0600 0 0 c 5 1'
    echo 'file /init /trial/passive-init 0755 0 0'
} > /trial/passive.list
"$obj/usr/gen_init_cpio" -t 0 /trial/passive.list | gzip -n > /out/passive.cpio.gz
(cd /out && sha256sum --strict -c "/inputs/$profile.output.sha256")
