# tools/kernel-wb — сборка ядра SA-02м на базе `wirenboard/linux`

Этот каталог заменяет старый `tools/buildroot/` для сборки ядра. Основное отличие — вместо Buildroot-Vиртуалки от Starterkit мы используем чистый checkout `wirenboard/linux` + [`../../kernel-port/`](../../kernel-port/README.md) overlay. На выходе — штатные `.deb` пакеты, устанавливаемые `apt` через локальный APT-репо или `dpkg -i`.

## Файлы

| Файл | Назначение |
|------|------------|
| [`build-sa02m-kernel.sh`](build-sa02m-kernel.sh) | сборка `.deb` для одной из flavours: `sa02m` (SMP) или `sa02m-rt` (PREEMPT_RT) |
| [`deploy-sa02m-kernel.sh`](deploy-sa02m-kernel.sh) | отправка собранных `.deb` на устройство по SSH и установка через `dpkg -i` |

## Требования к host-машине

- Debian 12 (bookworm) или Ubuntu 22.04+ x86_64
- Установлено: `git`, `build-essential`, `bc`, `kmod`, `cpio`, `fakeroot`, `dpkg-dev`, `flex`, `bison`, `libssl-dev`, `libelf-dev`, `quilt`, `rsync`, `gcc-arm-linux-gnueabihf`, `crossbuild-essential-armhf`

```bash
sudo apt install git build-essential bc kmod cpio fakeroot dpkg-dev \
                 flex bison libssl-dev libelf-dev quilt rsync \
                 gcc-arm-linux-gnueabihf crossbuild-essential-armhf
```

## Быстрый старт

```bash
# 1) Собрать SMP-ядро (~15-40 минут на host'е)
./tools/kernel-wb/build-sa02m-kernel.sh sa02m

# 2) Собрать RT-ядро (те же исходники + rt-patchset)
./tools/kernel-wb/build-sa02m-kernel.sh sa02m-rt

# 3) Развернуть на устройство (SSH)
./tools/kernel-wb/deploy-sa02m-kernel.sh root@sa02m-01.local
```

Выход: `~/build/sa02m-kernel/linux-image-sa02m*_5.10.35-*_armhf.deb` (+ headers, +libc-dev).

## Что происходит внутри `build-sa02m-kernel.sh`

1. `git clone --depth 1 -b release/wb-2606/wb7-bullseye https://github.com/wirenboard/linux ~/build/sa02m-kernel/wb-linux` (при отсутствии).
2. Копирует [`../../kernel-port/overlay/`](../../kernel-port/) в `wb-linux/`.
3. Накладывает патчи из [`../../kernel-port/patches/`](../../kernel-port/patches/).
4. Для `sa02m-rt` — скачивает `patch-5.10.35-rtNN.patch.gz` с kernel.org и применяет.
5. `make sa02m_defconfig`.
6. Для RT дополнительно: `./scripts/kconfig/merge_config.sh .config arch/arm/configs/sa02m_rt.config`.
7. `make -j$(nproc) bindeb-pkg` (штатный target из kernel/Makefile).

## Что делает `deploy-sa02m-kernel.sh`

1. По SSH подключается к устройству, проверяет флаги (`compatible = cyntron,sa-02m`).
2. Останавливает `mplc4.service`, `codesyscontrol.service` (если активны).
3. `scp` всех `.deb` и `dpkg -i` — post-inst-хук [`/etc/kernel/postinst.d/50-sa02m-fat-sync`](../../etc/kernel-postinst.d/50-sa02m-fat-sync) автоматически:
   - копирует `zImage` в `/usr/local/share/sa02m/kernel/zImage.<flavour>`;
   - копирует `sun8i-r40-sa02m.dtb` → `/mnt/boot_fat/sun8i-a40i-sk.dtb` (совместимо с `boot.scr`);
   - если новая flavour совпадает с текущей — обновляет `/mnt/boot_fat/zImage`.
4. Пересчитывает manifest в `/usr/local/share/sa02m/kernel/manifest.json`.
5. Опционально просит через `sa02m-kernel-select.sh` о reboot.

## Как проверить локально без устройства (smoke)

```bash
./tools/kernel-wb/build-sa02m-kernel.sh sa02m --smoke
```

`--smoke` собирает только `zImage + dtbs + modules` (без `bindeb-pkg`) и проверяет что символы `mv64xxx_i2c`, `sun8i_thermal`, `stmmac`, `sun4i_emac`, `icplus_phy`, `sunxi_rtc`, `ds1307`, `sun4i_ss`, `sunxi_wdt` присутствуют в `.config`. Это быстрая (~10 мин) sanity-проверка что overlay корректный.

## Совместимость с legacy `tools/buildroot/`

Каталог [`tools/buildroot/`](../buildroot/README.md) сохраняется для сборки старых 6.1.0-rc6-rt4 kernel'ей (пока миграция на SA-02м-2 не завершена). Со временем он будет удалён. См. [`docs/WB_LINUX_ADOPT.md`](../../docs/WB_LINUX_ADOPT.md) о переходном плане.
