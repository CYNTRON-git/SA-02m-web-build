# Buildroot VM — RT + Docker kernel для SA-02m

[Испытательные SMP и PREEMPT_RT на Linux 6.1.193, QEMU и порядок HIL](lts-trial/README.md)

> ### Это действующий путь сборки ядра — единственный в репозитории
>
> Именно эта цепочка (Starterkit VM + Buildroot 2022.08.x-sk-a40i) собирает пару
> ядер, которую несёт флот: **`6.1.0-rc6`** (SMP) и **`6.1.0-rc6-rt4`**
> (PREEMPT_RT). Оба профиля пересобраны с docker-набором netfilter и модемными
> драйверами и подтверждены на железе в 1.0.5.58
> ([`../../docs/contracts/kernel-conditional-services.md`](../../docs/contracts/kernel-conditional-services.md) §4).
>
> Прежняя редакция этого файла называла каталог устаревающим в пользу порта на
> форк `wirenboard/linux` (5.10.35, `.deb` + CI). Тот порт до устройств не дошёл
> и удалён — см. [`../../.ai-dev/notes/kernel-line.md`](../../.ai-dev/notes/kernel-line.md).
>
> Штатная установка и обновление устройства идут документированным путём деплоя,
> а не сборкой ядра: сборка нужна только когда меняется сам kernel.

Подготовка виртуальной машины и пересборка ядра `6.1.0-rc6-rt4` с Docker и драйверами SA-02m.

## Результаты аудита устройства (192.168.1.136, 2026-06-23)

| Компонент | Состояние | Действие при сборке |
|-----------|-----------|---------------------|
| **Ядро (FAT zImage)** | SMP `6.1.0-rc6`, Jun 3 2026 | Заменить на RT rebuild |
| **device_boot** | Совпадает с FAT 1:1 (MD5) | Эталон текущего SMP для отката |
| **Linux RT/zImage** | Другой MD5 — **не задеплоен** | Не использовать без rebuild (нет Docker) |
| **overlay / br_netfilter** | Отсутствуют в SMP и RT modules.builtin | Включить в Kconfig, пересобрать |
| **iptables** | `Protocol not supported` | Netfilter в ядре |
| **RT modules на диске** | 120 `.ko`, USB modem (cdc_*, qmi_wwan) | Обновить после rebuild |
| **option.ko** | Нет в RT tree | `CONFIG_USB_SERIAL_OPTION=m` |
| **DS3231** | i2c-1 @0x68, нет `/dev/rtc1` | `CONFIG_RTC_DRV_DS3231=y/m` |
| **PCF8563** | i2c-3 не отвечает на скане | Оставить в Kconfig + DTS |
| **ICPLUS end1** | IP101G OK | `CONFIG_ICPLUS_PHY=y` |
| **LED** | `eth0_link`, `eth1_link` | Сохранить из текущего defconfig |
| **Modem userspace** | MM, ppp, udev `99-modem.rules` — OK | Без изменений |
| **Docker** | Не установлен | После ядра: `apt install docker.io` |
| **Интернет** | OK (ICS с ПК) | — |

Эталон boot с устройства: `D:\SK-A40i-SODIMM\device_boot\` (`zImage`, `boot.scr`, `sun8i-a40i-sk.dtb`).

## VM и Buildroot

| Параметр | Путь |
|----------|------|
| VM VMware | `D:\SK-A40i-SODIMM\Linux\SK-A40i_Linux_build_machine\lubuntu64.vmx` |
| Логин VM | `user` / `123456` (SSH: `192.168.150.128`, VMnet1) |
| VirtualBox-вариант (ПК Оператора, 2026-10) | VM «A40-i», диск `E:\SK-A40i_Linux_build_machine\lubuntu64.vmdk`, NAT: `ssh -p 2222 user@127.0.0.1` |
| Buildroot | `/home/user/src/buildroot-2022.08.8-sk-a40i` |
| RT патч A40i | `patch-6.1-rc6-rt4.patch.gz` ([Сборка ядра на ВМ.txt](file:///C:/Users/admin/YandexDisk/ЦИНТРОН/Сборка%20линукс/Linux%20RT/Сборка%20ядра%20на%20ВМ.txt)) |

> USB на VM отключён в `.vmx` (не ломать sunxi-fel). Перед записью на плату — **пауза VM** ([Boot/readme.txt](file:///D:/SK-A40i-SODIMM/Boot/readme.txt)).

## Сборка RT-ядра (patch-first, Starterkit)

PREEMPT_RT **не включается только Kconfig** — сначала патч на этапе `make linux-patch` ([Сборка ядра на ВМ.txt](file:///C:/Users/admin/YandexDisk/ЦИНТРОН/Сборка%20линукс/Linux%20RT/Сборка%20ядра%20на%20ВМ.txt)):

```
make linux-extract
make linux-patch          # patch-6.1-rc6-rt4 из buildroot/linux/
sudo bash /root/prepare-rt-docker-kernel.sh security-fix
make linux-configure
# Expert → Preemption Model → Fully Preemptible Kernel (RT) + Docker kconfig
make linux
```

Патч скачивается в `linux/patch-6.1-rc6-rt4.patch`. **Не** дублировать через `BR2_LINUX_KERNEL_PATCH`.

На VM (рекомендуется **`build-kernel`**, не полный `build` — Qt/rootfs ~1–2 ч):

```bash
cd /home/user/src/buildroot-2022.08.8-sk-a40i
sudo bash prepare-rt-docker-kernel.sh check
sudo bash prepare-rt-docker-kernel.sh build-kernel
# внутри: linux-extract → linux-patch → verify → linux-configure → PREEMPT_RT + Docker kconfig → make linux
```

Ручной `menuconfig` / Expert → PREEMPT_RT имеет смысл только **после** `make linux-patch` — иначе опции RT в дереве нет.

Артефакты: `output/images/zImage`, modules `output/target/lib/modules/6.1.0-rc6-rt4/`.

Для SMP и RT скрипт применяет исправления Netfilter (CVE-2024-1086) и
OverlayFS (CVE-2023-0386). При ручной или инкрементальной сборке выполните
`security-fix` после распаковки исходников (для RT — после наложения
RT-патча) и до компиляции. Команда проверяет SHA-256 каждого исходного и
исправленного файла для обоих профилей и сбрасывает отметки сборки и
установки ядра. Следующий `make linux` пересоберёт оба объекта и запишет
обновлённый `output/images/zImage`, в том числе в ранее собранном дереве.
[Исправление Netfilter](https://github.com/gregkh/linux/commit/8e34430e33b8a80bc014f3efe29cac76bc30a4b4) ·
[Исправление OverlayFS](https://github.com/torvalds/linux/commit/4f11ada10d0ad3fd53e2bd67806351de63a4f9c3).

## Быстрый старт (Windows)

```powershell
# 1. Проверить boot-файлы и аудит устройства
py -3 .tmp_compare_boot.py
py -3 .tmp_device_audit.py
# вывод: .tmp_device_audit_out.txt

# 2. Открыть VM (если установлен VMware Workstation)
powershell -File tools/buildroot/open-build-vm.ps1

# 3. На VM — скопировать скрипт (shared folder или scp) и собрать
bash tools/buildroot/prepare-rt-docker-kernel.sh check
bash tools/buildroot/prepare-rt-docker-kernel.sh build-kernel
```

## Рекомендуемый shared folder (опционально)

В VMware для `lubuntu64.vmx` включить общую папку:

- Host: `D:\SK-A40i-SODIMM\device_boot` → Guest: `/mnt/device_boot`
- Host: репозиторий `SA-02m-web-build\tools\buildroot` → Guest: `/mnt/sa02m-buildroot`

Тогда на VM:

```bash
cp /mnt/sa02m-buildroot/prepare-rt-docker-kernel.sh /root/
bash /root/prepare-rt-docker-kernel.sh build-kernel
```

## После сборки на VM

```bash
# RT (CODESYS / PREEMPT_RT)
md5sum output/images/zImage
cp output/images/zImage /tmp/zImage.rt   # SMP-сборка ниже перезапишет output/images/zImage
tar -czf /tmp/modules-rt4-docker.tgz -C output/target/lib/modules 6.1.0-rc6-rt4

# SMP (без RT, Docker netfilter) — когда нужен переключатель в веб:
sudo bash prepare-rt-docker-kernel.sh build-kernel-smp
md5sum output/images/zImage.smp
tar -czf /tmp/modules-smp-docker.tgz -C output/target/lib/modules 6.1.0-rc6
```

На устройстве (после scp):

```bash
sa02m-kernel-deploy.sh install-rt /path/zImage.rt [/path/modules-rt4-docker.tgz]
sa02m-kernel-deploy.sh install-smp /path/zImage.smp [/path/modules-smp-docker.tgz]
sa02m-kernel-select.sh init
```

`install-rt`/`install-smp` сами запускают `depmod -a <версия>` после
распаковки модулей — без актуального индекса udev не подгрузит драйверы горячего
подключения (USB-модем, USB-микрофон: `docs/usb-audio-microphone.md`).

## Опциональный TUN для испытаний VPN

Для двух проверенных ядер rc6 сборщик формирует отдельные архивы `tun.ko` и сверяет точную идентичность работающего ядра перед распаковкой. Порядок сборки, SHA-256 и передача на стенд приведены в [инструкции TUN для SMP и RT](rc6-tun.md).

## Откат

**После неудачного RT-деплоя (2026-06-23):** устройство может не подняться по сети — с консоли/UART:

```bash
mount /dev/mmcblk2p1 /mnt/boot_fat
cp /mnt/boot_fat/zImage.smp /mnt/boot_fat/zImage
sync && reboot
```

Или восстановить `D:\SK-A40i-SODIMM\device_boot\zImage` → FAT `zImage` (SMP Jun 3 2026).
