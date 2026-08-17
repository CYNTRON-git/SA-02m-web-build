# Boot artifacts for FEL / netinstall stand

| File | Source | In git? |
|---|---|---|
| `u-boot-sunxi-with-spl.bin` | Allwinner SPL+U-Boot (FEL load) | yes (~1 MiB) |
| `sun8i-a40i-sk.dtb` | Cyntron A40i-2Eth DTB (`sk,a40i-nano-2e`) | yes (~31 KiB) |
| `zImage` | Live FAT `/dev/mmcblk2p1` on donor | **no** — fetch locally |

## Fetch / refresh

```powershell
py -3 tools/imaging/boot/fetch-boot-artifacts.py
# optional: also replace u-boot from Armbian package on donor
py -3 tools/imaging/boot/fetch-boot-artifacts.py --refresh-uboot
```

Inspect SPL header:

```powershell
py -3 tools/imaging/boot/inspect_uboot.py
```
