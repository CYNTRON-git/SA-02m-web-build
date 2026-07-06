#!/usr/bin/env python3
"""
inspect_uboot.py — разбор u-boot-sunxi-with-spl.bin (Allwinner sunxi SPL v0.2 + U-Boot proper).

Usage:
  python3 tools/imaging/boot/inspect_uboot.py [PATH]
  (по умолчанию PATH = tools/imaging/boot/u-boot-sunxi-with-spl.bin)
"""
import struct, sys, os

DEFAULT = os.path.join(os.path.dirname(__file__), "u-boot-sunxi-with-spl.bin")
path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT

with open(path, "rb") as f:
    data = f.read()

# SPL header (Allwinner eGON.BT0, v0.2)
#   0x00: 4B  b/branch instruction
#   0x04: 8B  magic 'eGON.BT0'
#   0x0C: 4B  checksum (stamp seed 0x5F0A6C39)
#   0x10: 4B  length (SPL size)
#   0x14: 4B  spl signature ('SPL' + version byte)
#   0x18: 4B  fel_script_address
#   0x1C: 4B  fel_uenv_length
#   0x20: 4B  dt_name_offset (points into extension area)
#   0x24: 4B  reserved / dram_size
#   0x28: 4B  reserved
#   0x2C: 16B dt_name (board DT name, null-padded)
#   0x3C: 4B  reserved
#   0x40: 4B  boot_media_type (0=fel, 1=mmc0, 2=mmc2)
magic = data[4:12].decode(errors="replace")
csum  = struct.unpack("<I", data[12:16])[0]
splen = struct.unpack("<I", data[16:20])[0]
sig   = data[20:24].decode(errors="replace")
fel_addr = struct.unpack("<I", data[24:28])[0]
fel_len  = struct.unpack("<I", data[28:32])[0]
dt_name  = data[44:60].rstrip(b"\x00").decode(errors="replace")
uboot_off = splen  # U-Boot proper starts right after SPL
uboot_len = len(data) - splen

print(f"file          = {path}")
print(f"file size     = {len(data):,} bytes ({len(data)/1024:.1f} KiB)")
print()
print(f"[SPL header @0x00–0x40]")
print(f"  magic       = {magic!r}       (должно быть 'eGON.BT0')")
print(f"  checksum    = 0x{csum:08x}")
print(f"  spl_length  = {splen:,} bytes (0x{splen:x} = {splen//1024} KiB)")
print(f"  spl_sig     = {sig!r}         (v0.2 = SPL\\x02)")
print(f"  fel_script  = 0x{fel_addr:08x} (для FEL режима, 0 если не FEL boot)")
print(f"  fel_uenv    = {fel_len} bytes")
print(f"  dt_name     = {dt_name!r} (какой DTB U-Boot будет искать)")
print()
print(f"[U-Boot proper]")
print(f"  offset      = 0x{uboot_off:x} ({splen} bytes от начала файла)")
print(f"  size        = {uboot_len:,} bytes (~{uboot_len/1024:.1f} KiB)")
print()
print(f"[eMMC layout при dd bs=1024 seek=8]")
print(f"  0x000000 –  0x001FFF : зарезервировано (partition table в 0x1BE–0x1FE)")
print(f"  0x002000 –  0x00{2+splen//4096:>03x}FFF: SPL ({splen} B)")
print(f"  0x00{(2+splen//4096):>03x}000 –  0x0FFFFF : U-Boot proper ({uboot_len} B)")
print(f"  0x100000 –  0x40FFFFF : FAT16 boot (~64 MiB, /dev/mmcblk2p1)")
print(f"  0x4100000 – …         : ext4 rootfs (/dev/mmcblk2p2)")
