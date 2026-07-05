# kernel-port/reference — Референсные исходники сторонних платформ

Здесь лежат **эталонные** исходные файлы платформ, на которых основан наш порт ядра для СА-02м. Файлы не изменяются: они нужны как источник правды при аудите и правках в [`../overlay/`](../overlay/).

## Файлы

### `sun8i-a40i-nano2e-none-sk.dts`

- **Автор:** [Starterkit](http://starterkit.ru), SODIMM SK-A40i-NANO-2E.
- **Model:** `"Cyntron A40i-2Eth"`.
- **Compatible:** `"sk,a40i-nano-2e", "allwinner,sun8i-r40"`.
- **Источник:** VM Starterkit для сборки Buildroot (архив `buildroot-2022.08.4-sk-a40i` / `buildroot-2022.08.8-sk-a40i`) — публично не выкладывается.
- **Пришёл из:** передан по Яндекс.Диску 2026-07-05 (в рамках подготовки порта на wirenboard/linux).

Файл идентичен тому, что скомпилирован в `/mnt/boot_fat/sun8i-a40i-sk.dtb` на боевых устройствах SA-02м-2. Служит **единственным** референсом для DDR3-timings, регуляторов AXP221, pinmux ethernet/UART/mmc.

Полученные из него ключевые факты:

| Узел | Что описано |
|------|-------------|
| `axp22x@34` на i2c-0 | **AXP221 PMIC** установлен — управляет всеми регуляторами SoC (dcdc1..5, aldo1..3, eldo3, dldo1, dc1sw). Без AXP20x-driver ядро **не поднимется**. |
| `rtc@68` на i2c-1 | **DS3231** через `"maxim,ds3231,d1307"` compatible → драйвер `rtc-ds1307`. |
| `&i2c3 { status = "okay"; }` (без узлов) | **PCA9536 (i2c-2 @0x41) и PCF8563 (i2c-3 @0x51) — не в DTS**, работают через `/dev/i2c-*` из userspace (MPLC4, sa02m-hw-backend). |
| `emac_ph_pins` + `phy2@0` c reset PH12 | end0: MII, PHY IP101G, `icplus,select-rx-error`. |
| `gmac_mii_pins_reduced` + `phy1@0` c reset PH13 | end1: MII на PA0-15 (без ECOL/ETXERR), PHY IP101G. |
| `leds eth0_link` PB2 GPIO_ACTIVE_LOW, trigger `1c0b080.mdio-mii:00:link` | Аппаратный link-LED end0. |
| `leds eth1_link` PI13 GPIO_ACTIVE_LOW, trigger `stmmac-1:00:link` | Аппаратный link-LED end1. |
| `mmc2` bus-width 8, hs200-1.8V, hw-reset, vqmmc=aldo2, vmmc=dcdc1 | eMMC (`/dev/mmcblk2`). |
| `mmc3` 4-bit, no-1-8-v, cd-gpios закомментирован | microSD (`/dev/mmcblk0`/`/dev/mmcblk3`). |
| `uart0` (PB), `uart3` (PG), `uart4` (PH, rts=PI12 закомментирован), `uart5` (PI10/11), `uart7` (PI) | 5×RS-485. |
| `can0` `can_pa_pins` | Встроенный CAN A40i выведен. |
| `usbphy` c usb0/1/2 vbus = reg_usb0_vbus, но `gpio` закомментирован | USB VBUS всегда включен (без GPIO-переключения). |
| `&codec { ... }` | Встроенный SoC audio codec активен. |
| `&pio vcc-p*-supply` | Соответствие регуляторов AXP221 к банкам GPIO. |

## Почему референс лежит в репо

- **Отсутствие в public git** — Starterkit не публикует DTS в открытом доступе, только через VM. Держим у себя.
- **Аудит** — при правках в `overlay/` можно всегда сверить diff.
- **Восстановление** — если рабочий DTB на устройстве повредится, `dtc -O dtb sun8i-a40i-nano2e-none-sk.dts -o sun8i-a40i-sk.dtb` вернёт boot.

## Лицензия

Файл распространяется под GPL-2.0 (стандартная лицензия Linux kernel DTS). Copyright Starterkit / ЦИНТРОН.
