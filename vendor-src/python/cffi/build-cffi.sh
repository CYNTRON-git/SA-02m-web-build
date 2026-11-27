#!/bin/bash
set -euo pipefail
cd /work
mkdir -p /tmp/target/usr/lib
ln -s usr/lib /tmp/target/lib
for input in apt-evidence/arm/*.deb; do dpkg-deb -x "$input" /tmp/target; done
target=/tmp/target
tools=/tmp/build-backend
mkdir -p "$tools" /tmp/source
python3 -m zipfile -e build-inputs/setuptools-80.9.0-py3-none-any.whl "$tools"
tar -xf build-inputs/cffi-2.1.1.tar.gz -C /tmp/source
cd /tmp/source/cffi-2.1.1
export SOURCE_DATE_EPOCH=1760000000 PYTHONHASHSEED=0
export PYTHONPATH="$tools:$target/usr/lib/python3.12"
export _PYTHON_SYSCONFIGDATA_NAME=_sysconfigdata__arm-linux-gnueabihf
export _PYTHON_HOST_PLATFORM=linux-armv7l
export PKG_CONFIG_LIBDIR="$target/usr/lib/arm-linux-gnueabihf/pkgconfig"
export PKG_CONFIG_SYSROOT_DIR="$target"
export CC="arm-linux-gnueabihf-gcc --sysroot=$target -B$target/usr/lib/arm-linux-gnueabihf/ -L$target/usr/lib/arm-linux-gnueabihf"
export LDSHARED="$CC -shared"
export CFLAGS="--sysroot=$target -I$target/usr/include/python3.12 -I$target/usr/include/arm-linux-gnueabihf/python3.12 -I$target/usr/include/arm-linux-gnueabihf -g0 -O2 -ffile-prefix-map=/tmp/source=."
export LDFLAGS="--sysroot=$target -L$target/usr/lib/arm-linux-gnueabihf -Wl,-rpath-link,$target/usr/lib/arm-linux-gnueabihf"
python3 -c 'from setuptools.build_meta import build_wheel; print(build_wheel("/output"))'
sha256sum /output/*.whl
elf=$(find build -name '_cffi_backend*.so' | head -1)
arm-linux-gnueabihf-readelf -Ah -d "$elf"
arm-linux-gnueabihf-readelf -V "$elf"
