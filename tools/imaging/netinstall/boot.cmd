# SA-02m netinstall — U-Boot script (zero-touch, no serial interaction)
# Compile: mkimage -C none -A arm -T script -d boot.cmd boot.scr
#
# Placeholders replaced by build-netinstall.sh / start-stand:
#   __SERVERIP__   stand HTTP/TFTP host (e.g. 192.168.1.10)
#   __HTTP_PORT__  image HTTP port (default 8080)
#
# Loaded by sunxi-fel after U-Boot, or as boot.scr from TFTP/USB.

setenv autoload no
setenv serverip __SERVERIP__
setenv autostart no

# Prefer DHCP for client IP; keep serverip from stand even if DHCP omits siaddr
dhcp || echo "DHCP failed — using static fallback if set"
if test -n "${serverip}"; then true; else setenv serverip __SERVERIP__; fi

setenv bootargs "console=ttyS0,115200 earlycon=uart,mmio32,0x1c28000 sa02m.image=http://${serverip}:__HTTP_PORT__/current.img.xz sa02m.status=http://${serverip}:8765 sa02m.server=${serverip} sa02m.iface=eth0"

echo "TFTP zImage from ${serverip}"
tftpboot ${kernel_addr_r} zImage || tftp ${kernel_addr_r} zImage
echo "TFTP DTB"
tftpboot ${fdt_addr_r} sun8i-a40i-sk.dtb || tftp ${fdt_addr_r} sun8i-a40i-sk.dtb
echo "TFTP uInitrd-netinstall"
tftpboot ${ramdisk_addr_r} uInitrd-netinstall || tftp ${ramdisk_addr_r} uInitrd-netinstall

echo "bootz netinstall"
bootz ${kernel_addr_r} ${ramdisk_addr_r} ${fdt_addr_r}
