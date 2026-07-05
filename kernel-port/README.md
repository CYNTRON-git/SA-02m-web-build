# kernel-port — Порт `wirenboard/linux` на SA-02м

Этот каталог **не содержит форк ядра**. Здесь лежит **оверлей** — минимальный набор файлов и патчей, которые накладываются на чистый checkout репозитория `wirenboard/linux`. Такой подход позволяет:

- получать все bug-fixes upstream'а WB одним `git pull`;
- явно видеть, что именно мы добавили (короткий diff вместо параллельной ветки);
- воспроизводить сборку в любом CI без клонов огромных монорепо.

## Структура

```
kernel-port/
├── README.md                          — этот файл
├── apply.sh                           — накладывает оверлей и патчи на checkout wirenboard/linux
├── reference/                         — эталонные исходники (readonly, для аудита)
│   ├── README.md
│   └── sun8i-a40i-nano2e-none-sk.dts  — оригинальный DTS от Starterkit
├── overlay/                           — файлы, копируемые как есть в WB tree
│   └── arch/arm/
│       ├── boot/dts/sun8i-r40-sa02m.dts
│       └── configs/
│           ├── sa02m_defconfig        — SMP kernel base config
│           └── sa02m_rt.config        — merge-fragment для PREEMPT_RT
└── patches/                           — точечные патчи в существующие файлы WB tree
    ├── 0001-wb-version-add-sa02m-flavours.patch  — flavours sa02m / sa02m-rt
    └── 0002-arm-dts-Makefile-add-sa02m.patch     — регистрация DTB в arm/boot/dts/Makefile
```

## Базовые предпосылки

- **Ветка WB:** `release/wb-2606/wb7-bullseye` (kernel 5.10.35, armhf).
  - Здесь есть in-tree поддержка Allwinner A40i (sun8i-r40): CPU OPP-table, GMAC, EMAC, USB PHY, DMA, crypto engine, watchdog.
  - Пакет собирается как Debian `linux-image-sa02m` / `linux-image-sa02m-rt`.
- **Cross-toolchain:** `arm-linux-gnueabihf-` (armhf, для eabihf soft-fp совместимость с WB7).
- **PREEMPT_RT patchset:** `patch-5.10.35-rt36.patch.gz` (или ближайший к тегу kernel'а — см. `tools/kernel-wb/build-sa02m-kernel.sh`).

## Как накладывать

```bash
git clone --depth 1 -b release/wb-2606/wb7-bullseye \
    https://github.com/wirenboard/linux ~/build/wb-linux

cd sa02m
./kernel-port/apply.sh ~/build/wb-linux

cd ~/build/wb-linux
make ARCH=arm CROSS_COMPILE=arm-linux-gnueabihf- sa02m_defconfig
make ARCH=arm CROSS_COMPILE=arm-linux-gnueabihf- -j"$(nproc)" \
     zImage modules dtbs
```

Собранные артефакты:
- `arch/arm/boot/zImage`
- `arch/arm/boot/dts/sun8i-r40-sa02m.dtb`
- `.tmp_versions/` + `modules/` — `make INSTALL_MOD_PATH=... modules_install`

Для сборки Debian-пакета:

```bash
make -f wb.mk deb KERNEL_FLAVOUR=sa02m
make -f wb.mk deb KERNEL_FLAVOUR=sa02m-rt   # после наложения PREEMPT_RT-патча
```

Развёртывание на устройство описано в [`../tools/kernel-wb/README.md`](../tools/kernel-wb/README.md).

## Что даёт SA-02м по сравнению с текущим Buildroot-ядром 6.1.0-rc6

| Свойство | Текущий (Buildroot 6.1.0-rc6-rt4) | SA-02м на WB-linux 5.10.35 |
|----------|------------------------------------|----------------------------|
| Источник ядра | Case-by-case cherry-pick от Starterkit / mainline | Полный fork WB с активной upstream-поддержкой |
| Кол-во исправлений A40i | ~5 патчей | 80+ патчей (см. wirenboard/linux git log) |
| Debian-пакетизация | Buildroot + ручной deploy zImage | Штатный `linux-image-sa02m_*.deb` |
| RT-патчи | Ручное скачивание rt-preempt из /Linux RT/ | Автоматическая интеграция в build-скрипт |
| CI/CD | Нет | GitHub Actions (см. `.github/workflows/build-sa02m-kernel.yml`) |
| Поддержка `apt full-upgrade` | Нет | Да, через собственное репо |
| Ready-to-boot образ | Нет | Да, готов к `dd if=... of=/dev/mmcblk2` |

## Что мы **сознательно** оставили за бортом

Полный список — в [`../docs/WB_LINUX_FUTURE_FEATURES.md`](../docs/WB_LINUX_FUTURE_FEATURES.md). Основное:

| Компонент | Причина исключения |
|-----------|---------------------|
| WBEC (Wiren Board Embedded Controller) | Нет физического МК на плате |
| Wi-Fi (RTL/BRCM/MT76 и cfg80211/mac80211) | SA-02м — только Ethernet |
| Bluetooth стек | Нет BT/BLE-модуля |
| Media / cameras / HDMI / LVDS | Нет графического вывода |
| USB-Gadget composite | SA-02м работает только в USB-host режиме |
| MTD dataflash / SST NOR / UBI/UBIFS | Загрузка с eMMC, не с NAND |
| 1-Wire (W1) | На плате не разведён |
| AXP20x charger / battery | Нет батареи; PMIC AXP221 **остаётся** для управления регуляторами |
| HSR / IEEE 802.15.4 | Отложено до SA-02м-2 |
| ADS1015 / MCP4728 / AD5064 / TCS3472 / BMP280 / MAG3110 | Нет соответствующих датчиков |
| SUN4I_GPADC / LRADC | Нет аналоговых кнопок |
| VIDEO_SUNXI_CEDRUS + rotate/deinterlace | Не декодируем видео |

Всё, что нужно на SA-02м, **включено**: AXP221 PMIC + регуляторы + ADC (`AXP20X_ADC` для iio-hwmon), два Ethernet-контроллера (`SUN4I_EMAC` + `STMMAC_ETH`), ICPlus IP101G PHY, MMC/eMMC, встроенный CAN, sunxi watchdog, thermal, hardware crypto acceleration, wireguard, Docker-стек (cgroups + overlayfs + veth/macvlan/vxlan), nftables/iptables.

## <a name="i2c-3-pcf8563"></a>Включение PCF8563 на i2c-3

На SA-02м-2 (ревизия с внешним RTC PCF8563 на i2c-3 @0x51) DTS-декларация закомментирована. Чтобы её активировать:

1. В `overlay/arch/arm/boot/dts/sun8i-r40-sa02m.dts` снять комментарий с блока `pcf8563: rtc@51 { ... }`.
2. `RTC_DRV_PCF8563=m` уже включён в `sa02m_defconfig` (модуль `rtc-pcf8563`).
3. Пересобрать DTB и залить в FAT-раздел.

Порядок `/dev/rtc0` / `/dev/rtc1`: сейчас `rtc0 = &rtc` (in-SoC), `rtc1 = DS3231`. После включения PCF8563 он появится как `/dev/rtc2` — если нужно поменять приоритет, используйте `chosen { rtc = <&pcf8563>; };` или udev rule.
