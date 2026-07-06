# WB Linux — компоненты «на будущее» для СА-02м

**Контекст:** мы планируем перейти на ядро [wirenboard/linux](https://github.com/wirenboard/linux), ветка [`release/wb-2606/wb7-bullseye`](https://github.com/wirenboard/linux/tree/release/wb-2606/wb7-bullseye) (Linux `5.10.35-wb182`, armhf, sun8i-r40 = A40i, полностью совпадает с процессором СА-02м).

Wiren Board 7 — плата на том же A40i, что и СА-02м, поэтому её defconfig и DTS используются как донор. В базовом порту мы **не тянем** часть WB-компонентов, потому что соответствующего железа/чипов на СА-02м нет. Этот документ фиксирует, **что именно урезано** и **как это включить позже**, если появится нужный вариант СА-02м, WBIO-модуль или сторонняя периферия.

Практическая реализация порта — в каталоге [`kernel-port/`](../kernel-port/README.md) с оверлеем defconfig и DTS. Инструменты сборки/деплоя — [`tools/kernel-wb/`](../tools/kernel-wb/README.md).

---

## 1. WBEC — микроконтроллер питания Wiren Board

Wiren Board 7/8 имеет отдельный STM32-подобный микроконтроллер (Wiren Board Embedded Controller, «WBEC»), который управляет питанием, кнопкой Power, watchdog, буферной батареей и часами RTC. На СА-02м этой микросхемы нет: питание неотключаемое (5 В приходит на плату напрямую), RTC — PCF8563 на I²C-3.

### Что урезаем сейчас

| Kconfig | Роль в WB |
|---------|-----------|
| `CONFIG_MFD_WBEC` | Multi-function device: агрегирует все WBEC-подсистемы |
| `CONFIG_GPIO_WBEC` | GPIO-экспандер внутри WBEC (кнопки, LED, реле) |
| `CONFIG_INPUT_WBEC_PWRBUTTON` | Драйвер длительного нажатия Power → shutdown/reboot |
| `CONFIG_RTC_DRV_WBEC` | RTC внутри WBEC с батарейным резервом |
| `CONFIG_WBEC_POWER` | Управление ON/OFF, sleep-состояния платы |
| `CONFIG_WBEC_WATCHDOG` | Hardware watchdog через WBEC (перезагрузка при зависании) |
| `CONFIG_WBEC_UART` | Отдельный UART внутри WBEC (сервисный порт WB) |
| `CONFIG_WBEC_ADC` | ADC-каналы для мониторинга питания (Vin, Vbat) |
| `CONFIG_INPUT_WBEC_*`, `CONFIG_WBEC_PWM` | Beeper/PWM/фронт-панель WB |

DTS-узлы: `wbec@50` (I²C-адрес 0x50), `pinctrl` для WBEC-GPIO, `crypto-chip bus`.

### Когда может понадобиться

- Появится ревизия СА-02м с WBEC-like чипом (маловероятно — конструктив другой).
- Захотим использовать WBEC-совместимый **сторонний модуль** (например, отдельная плата с ATmega/STM32 для мягкого выключения по кнопке или для аппаратного watchdog).
- Портирование на другую железку CYNTRON с WBEC.

### Как включить

1. Взять патчи WBEC из ветки [`release/wb-2606/wb7-bullseye`](https://github.com/wirenboard/linux/tree/release/wb-2606/wb7-bullseye) — они уже в дереве (`drivers/mfd/wbec.c`, `drivers/gpio/gpio-wbec.c` и т.д.).
2. В нашем форке (`sa02m-linux`) вернуть в `arch/arm/configs/sa02m_defconfig` строки `CONFIG_MFD_WBEC=y` и все зависимые.
3. Прописать в новый файл `sun8i-r40-sa02m-wbec.dtsi`:
   ```dts
   &i2c1 {
       wbec: wbec@50 {
           compatible = "wirenboard,wbec";
           reg = <0x50>;
           interrupt-parent = <&pio>;
           interrupts = <PIN_WBEC_IRQ IRQ_TYPE_EDGE_FALLING>;
       };
   };
   ```
4. Опционально: `sa02m-wbec` overlay-фрагмент, чтобы включать через `sa02m_hw_variant.conf` без пересборки ядра.

**Оценка сложности:** низкая, если чип реально подключён по I²C. Драйверы уже готовы в WB-дереве.

---

## 2. AXP20x charger/battery (fuel gauge) и AC100 audio-dock

> ⚠ **ВАЖНАЯ ПОПРАВКА** (2026-07): в исходной редакции документа мы ошибочно
> считали, что PMIC AXP20x на СА-02м отсутствует полностью и весь MFD надо
> урезать. Изучение эталонного DTS от Starterkit
> ([`kernel-port/reference/sun8i-a40i-nano2e-none-sk.dts`](../kernel-port/reference/sun8i-a40i-nano2e-none-sk.dts))
> показало, что **PMIC AXP221 стоит на I²C-0 @0x34** и управляет всеми
> регуляторами SoC (dcdc1..5 для CPU/DRAM/VCC-IO, aldo/eldo/dldo для
> eMMC/PLL/microSD). Без ядра PMIC-драйвера SoC **не запустится**.
>
> Поэтому в базовом `sa02m_defconfig` **включены**:
> - `CONFIG_MFD_AXP20X_I2C=y` — обязательно, иначе регуляторы не активируются
> - `CONFIG_REGULATOR_AXP20X=y` — вся семья dcdc/aldo/eldo/dldo
> - `CONFIG_AXP20X_ADC=y` — используется `iio-hwmon-axp*` для мониторинга Vin/Vbus/T
> - `CONFIG_INPUT_AXP20X_PEK=m` — модуль, если понадобится кнопка Power (на SA-02м не разведена, но не мешает)
>
> Эта секция теперь описывает **только те подсистемы AXP20x, которые
> действительно не нужны** на SA-02м: зарядное устройство, fuel gauge
> батареи и AC100 audio dock.

### Что урезаем сейчас (нет батареи и нет audio dock)

| Kconfig | Роль в WB | Почему не нужно на SA-02м |
|---------|-----------|---------------------------|
| `CONFIG_AXP20X_POWER` | Power supply class (Vin/Vbus status, коммутация зарядки) | Питание неотключаемое, поступает напрямую |
| `CONFIG_CHARGER_AXP20X` | Драйвер CC/CV зарядки Li-Ion | Батареи нет |
| `CONFIG_BATTERY_AXP20X` | Fuel gauge (учёт %) | Батареи нет |
| `CONFIG_CHARGER_GPIO` | Простой GPIO-charger detect | Нет CHRG_OK линии |
| `CONFIG_MFD_AC100` | RTC + audio-кодек AC100 (WB Combo PMIC) | На SA-02м AC100 не установлен |
| `CONFIG_RTC_DRV_AC100` | RTC из AC100 | Есть DS3231 на I²C-1 @0x68 |
| `CONFIG_MFD_AXP20X_RSB` | Транспорт RSB (используется на HW у которого нет I²C линии к AXP) | На SA-02м AXP на I²C-0 |
| `CONFIG_BATTERY_EDLC` | Драйвер супер-конденсатора Wiren Board | Нет буферного капа |
| `CONFIG_GENERIC_ADC_BATTERY` | Универсальный ADC-based fuel-gauge | Нет батареи |

### Когда это может понадобиться (SA-02м-2 с UPS-mode)

- **Гипотетическая ревизия SA-02м с батарейным резервом.** Тогда нужны `AXP20X_POWER`, `CHARGER_AXP20X`, `BATTERY_AXP20X` + DTS-узлы `battery { ... }` и `power_supply { ... }`.
- **Портирование Docker-контейнера с audio-заметкой на HDMI-out** — тогда AC100 (в паре с i2s из sun8i-r40) даст аудио-выход. Пока audio-контроллер SoC (`&codec { ... }` в DTS) обходится и без AC100.
- **Sleep-режим CPU через AXP223.** Пока не рассматриваем: SA-02м всегда online.

### Как включить (по мере появления железа)

1. **Батарея / зарядка.** Добавить в оверлей:
   ```
   CONFIG_AXP20X_POWER=y
   CONFIG_CHARGER_AXP20X=y
   CONFIG_BATTERY_AXP20X=y
   CONFIG_CHARGER_GPIO=y
   ```
   В DTS в `&axp22x { ... }` добавить `battery-power-supply`/`ac-power-supply`/`usb-power-supply` subnodes с параметрами Li-Ion (например, `charger-constant-current-microamp = <900000>;`).

2. **AC100.**
   ```
   CONFIG_MFD_AC100=y
   CONFIG_RTC_DRV_AC100=y
   ```
   DTS: подключить AC100 на RSB-шине (`rsb@01c25000`) с `reg = <0xe89>` и `ac100_rtc: rtc { ... }`. Для audio-in — добавить `simple-audio-card` вокруг AC100 в паре с `codec@1c22c00` (SoC audio codec).

3. **Fuel gauge через ADC** (fallback если AXP20x не поддерживает).
   ```
   CONFIG_GENERIC_ADC_BATTERY=m
   ```
   + iio-channel к любому ADC (AXP20X_ADC уже есть).

**Оценка сложности:**
- Батарея/зарядка — низкая по kernel-side (все драйверы уже в WB tree), средняя по HW (нужен корректный VBUS и Li-Ion pinout).
- AC100 — низкая (mainline-driver готов), но требует RSB pinctrl.

---

## 3. Wi-Fi (Realtek RTL8xxxU / Broadcom BRCMFMAC / MediaTek MT76)

WB7 имеет опциональный USB Wi-Fi модуль (RTL8723BU / RTL8733BU) и опциональный BRCM SDIO chip. На СА-02м Wi-Fi отсутствует — интернет только через Ethernet или USB-LTE-модем.

### Что урезаем сейчас

| Kconfig | Роль в WB |
|---------|-----------|
| `CONFIG_RTL8723BU` / `CONFIG_RTL8733BU` | Внешние подмодули WB для RealTek 802.11n USB |
| `CONFIG_RTL8XXXU`, `CONFIG_RTL8XXXU_UNTESTED` | Ин-tree Realtek драйвер |
| `CONFIG_BRCMFMAC`, `CONFIG_BRCMFMAC_USB` | Broadcom FullMAC (SoM Wi-Fi на WB) |
| `CONFIG_MT7601U`, `CONFIG_MT76x0U`, `CONFIG_MT76x2U`, `CONFIG_MT7663U`, `CONFIG_MT7663S` | MediaTek 802.11ac USB/SDIO |
| `CONFIG_CFG80211`, `CONFIG_MAC80211`, `CONFIG_MAC80211_MESH`, `CONFIG_RFKILL` | Стек Wi-Fi |
| `CONFIG_B43`, `CONFIG_B43_SDIO` | Broadcom legacy |

### Когда может понадобиться

- Пользователь просит USB Wi-Fi-адаптер (например, TP-Link Archer T4U на MT7612U) для беспроводного подключения СА-02м к цеховой сети.
- Добавление точки доступа (hostapd) для настройки прибора со смартфона.
- Портирование на СА-02м-Wi-Fi (гипотетическая ревизия с onboard-Wi-Fi).

### Как включить

1. В `sa02m_defconfig` включить как модули:
   ```
   CONFIG_CFG80211=m
   CONFIG_MAC80211=m
   CONFIG_RFKILL=m
   CONFIG_MT7601U=m       # или нужный чипсет
   CONFIG_RTL8XXXU=m      # или CONFIG_RTL8723BU=m через git-submodule
   ```
2. Firmware-файлы (`/lib/firmware/rtlwifi/*`, `mediatek/*`) — установить из пакета `firmware-realtek` / `firmware-misc-nonfree` (Debian) или из релизов производителей.
3. В веб-интерфейсе (`www/network_config/index.html`) добавить вкладку «Wi-Fi» с `nmcli`/`wpa_supplicant`-обёрткой в `scripts/02-network.sh`.
4. NetworkManager: `apt install network-manager` + hook в `install.sh`, если ещё не установлен.

**Оценка сложности:** низкая для kernel-стороны, средняя для UX (нужна вкладка в SPA + backend CGI).

---

## 4. Bluetooth (BT_HCIBTUSB, BT_RFCOMM, BT_HIDP)

WB7 имеет BT (в паре с Wi-Fi Realtek). На СА-02м BT-чипа нет.

### Что урезаем сейчас

| Kconfig | Роль |
|---------|------|
| `CONFIG_BT=m`, `CONFIG_BT_RFCOMM`, `CONFIG_BT_BNEP`, `CONFIG_BT_HIDP` | Основной BT-стек |
| `CONFIG_BT_HCIBTUSB` | USB-транспорт (для донглов CSR/Realtek) |

### Когда может понадобиться

- Пользователь подключает USB-BLE-донгл (nRF52840 dongle) для интеграции с BLE-датчиками (Xiaomi Mi Sensor, Ruuvi, iBeacon).
- BT-serial к внешним устройствам (весы, принтеры чеков).
- MQTT-BLE-мост (например, `theengs-gateway`) — активная тема в IoT.

### Как включить

1. `CONFIG_BT=m`, `CONFIG_BT_HCIBTUSB=m`, `CONFIG_BT_RFCOMM=m` в `sa02m_defconfig`.
2. Userspace: `apt install bluez bluez-tools`.
3. Firmware для донгла — обычно уже в `firmware-linux-nonfree`.
4. Веб-UI: опциональная вкладка «Bluetooth» — можно вообще без UI, только CLI-режим для BLE-датчиков (BlueZ + `theengs-decoder`).

**Оценка сложности:** низкая (kernel + apt), средняя (если делаем UI).

---

## 5. Медиа / камеры / HDMI

WB7 поддерживает USB Video Class камеры, HDMI-выход (в WB8/85+), Sunxi CSI/CEDRUS. На СА-02м HDMI отсутствует, а usb-камеры не в приоритете.

### Что урезаем сейчас

| Kconfig | Роль |
|---------|------|
| `CONFIG_MEDIA_SUPPORT`, `CONFIG_MEDIA_CAMERA_SUPPORT` | Каркас Video4Linux |
| `CONFIG_VIDEO_SUN4I_CSI`, `CONFIG_VIDEO_SUN6I_CSI` | Camera Serial Interface на SoC (для DVP-камер) |
| `CONFIG_VIDEO_SUN8I_DEINTERLACE`, `CONFIG_VIDEO_SUN8I_ROTATE` | Аппаратное преобразование |
| `CONFIG_VIDEO_SUNXI_CEDRUS` | VPU H.264/H.265 hardware decoder |
| `CONFIG_USB_VIDEO_CLASS` | UVC-камеры |
| `CONFIG_SND_*`, `CONFIG_SND_SOC_*`, `CONFIG_SOUND` | Звук |
| `CONFIG_BACKLIGHT_*`, `CONFIG_PWM_SUN8I` | Подсветка LCD (LVDS-канал на A40i) |

### Когда может понадобиться

- Появится ревизия СА-02м-HMI с LCD/LVDS-панелью (у A40i есть встроенный LVDS-контроллер, «зашит» в SoC).
- Хочется USB-web-камеры для CCTV / motion-detection в связке с MQTT.
- Хочется HDMI-вывод для локального терминала (у A40i есть HDMI IP-блок, WB не использует).

### Как включить

1. `CONFIG_VIDEO_DEV=m`, `CONFIG_USB_VIDEO_CLASS=m` — для UVC-камер.
2. `CONFIG_VIDEO_SUNXI_CEDRUS=m` + userspace-libcedrus/ffmpeg-sunxi — для декодирования на VPU.
3. LVDS-панель: описывается через `panel-simple` compatible в DTS + backlight-node с pwm. Требует физическую ревизию платы.
4. HDMI: `CONFIG_DRM_SUN4I=m` + `CONFIG_DRM_SUN4I_HDMI=m` (mainline поддержка H3/H5/R40 HDMI есть).

**Оценка сложности:** средняя (UVC), высокая (LVDS panel — нужна DTS-модель для конкретной матрицы + подобрать тайминги).

---

## 6. 1-Wire (w1-gpio) и W1-сенсоры

WB7 использует 1-Wire на 2 клеммниках терминала (W1, W2) для DS18B20 датчиков температуры и DS2408/DS2413 GPIO-экспандеров.

### Что урезаем сейчас

| Kconfig | Роль |
|---------|------|
| `CONFIG_W1=m` | Ядро 1-Wire |
| `CONFIG_W1_MASTER_DS2482=m` | I²C-1Wire мост |
| `CONFIG_W1_MASTER_GPIO=m` | GPIO-bit-banging мастер |
| `CONFIG_W1_SLAVE_THERM=m` | DS18B20 |
| `CONFIG_W1_SLAVE_SMEM`, `DS2408`, `DS2413`, `DS2423`, `DS2431`, `DS2433`, `DS2780`, `DS2781`, `DS28E04` | Разные slave-чипы |

DTS-узлы `onewire_w1`, `onewire_w2` с `w1-gpio` compatible и pull-up-GPIO.

### Когда может понадобиться

- Подключение DS18B20 температурных датчиков в цехе (популярный дешёвый датчик).
- 1-Wire GPIO-расширители (DS2408) — редкий кейс.

### Как включить

1. Вернуть в `sa02m_defconfig`: `CONFIG_W1=m`, `CONFIG_W1_MASTER_GPIO=m`, `CONFIG_W1_SLAVE_THERM=m` — остальные slave-чипы по потребности.
2. В `sun8i-r40-sa02m.dts` добавить узел:
   ```dts
   onewire_w1: onewire_w1 {
       compatible = "w1-gpio";
       gpios = <&pio N M (GPIO_ACTIVE_HIGH | GPIO_OPEN_DRAIN)>;
       pu-gpios = <&pio N M GPIO_ACTIVE_HIGH>;
       status = "okay";
   };
   ```
   Выбрать свободный GPIO из СА-02м (например, PH2 из WB7-разводки — но у нас другой конструктив, нужно уточнить какой GPIO выведен на терминал).
3. В веб-интерфейсе — вкладка «1-Wire» с чтением `/sys/bus/w1/devices/*/temperature` (простая CGI).

**Оценка сложности:** низкая, но требует ясности с физической разводкой GPIO на плате СА-02м.

---

## 7. Camera Serial Interface / GPADC / IIO-hwmon внутренние ADC

WB7 использует GPADC для аналоговых входов (A1/A2/A3/Vin) и iio-hwmon для мониторинга.

### Что урезаем сейчас

| Kconfig | Роль |
|---------|------|
| `CONFIG_SUN4I_GPADC=m` | Встроенный ADC в SoC A40i (4 канала) |
| `CONFIG_TI_ADS1015=m` | Внешний I²C ADC TI |
| `CONFIG_IIO_RESCALE=y`, `CONFIG_IIO_SYSFS_TRIGGER=y` | IIO helpers |
| `CONFIG_SENSORS_IIO_HWMON=y`, `CONFIG_SENSORS_LM75=y`, `CONFIG_SENSORS_NTC_THERMISTOR`, `CONFIG_SENSORS_INA2XX` | hwmon-обвязка |

На СА-02м **есть** thermal-зона SoC (уже используется). Внешние аналоговые входы (A1/A2/A3) в текущей ревизии отсутствуют, но есть в родственных вариантах (`sa02m-hw_variant.conf` может расширяться).

### Когда может понадобиться

- Добавление ревизии СА-02м с 4×AI (аналог WB7 analog inputs).
- Внешний ADC на I²C (TI ADS1015) для расширения — актуальный кейс для MR-02м-AI модулей.
- Датчик LM75 (температура платы) — уже применяется на некоторых MR-модулях.

### Как включить

1. `CONFIG_SUN4I_GPADC=m`, `CONFIG_TI_ADS1015=m`, `CONFIG_SENSORS_LM75=y`, `CONFIG_IIO_RESCALE=y` — вернуть в `sa02m_defconfig`.
2. DTS: узлы `voltage-divider` (см. `sun8i-r40-wirenboard72x.dtsi` строки 306-346) для A1/A2/A3/Vin с реальными делителями напряжения СА-02м.
3. `iio-hwmon` для интеграции в `sa02m-modbus-mqtt` и `sa02m-telemetry` (значения улетят в MQTT).
4. Веб UI: `sa02m-hw.conf` расширить блоком `[ANALOG_INPUTS]`, отрисовать в dashboard.

**Оценка сложности:** средняя, но целиком userspace если ADC внешний (ADS1015 через `i2cget`).

---

## 8. CAN-контроллер SoC + внешние MCP251X

WB7 использует встроенный CAN-контроллер A40i (`CONFIG_CAN_SUN4I`) плюс SPI-модули MCP2515. На СА-02м CAN не заявлен в базовой конфигурации.

### Что урезаем сейчас (частично — базовый CAN оставим)

| Kconfig | Роль |
|---------|------|
| `CONFIG_CAN=y` | Ядро CAN-стека — **оставляем** (полезно, лёгкое) |
| `CONFIG_CAN_J1939=y` | Автопротокол J1939 — оставляем |
| `CONFIG_CAN_SUN4I=y` | Встроенный CAN в A40i — оставляем (пины могут быть выведены) |
| `CONFIG_CAN_MCP251X=m` | SPI-CAN мост — **урезаем**, вернём при необходимости |
| `CONFIG_CAN_GS_USB=m` | USB-CAN Geschwister-Schneider — урезаем |

### Когда может понадобиться

- Появление СА-02м-CAN с MCP2515 (SPI-CAN мост, дёшево).
- Использование gs_usb-совместимых USB-CAN адаптеров (candleLight, canable) для интеграции с автомобильными шинами / OBD-II.
- SocketCAN-Modbus мосты, MQTT-CAN gateway (реализуемо на userspace `python-can`).

### Как включить

1. `CONFIG_CAN_MCP251X=m`, `CONFIG_CAN_GS_USB=m` в `sa02m_defconfig`.
2. Для MCP2515: DTS-узел на `&spi0` c `compatible = "microchip,mcp2515";` + xtal-clocks-property.
3. Userspace: `apt install can-utils python3-can`; веб-вкладка «CAN» с чтением `candump can0` — опционально.

**Оценка сложности:** низкая на kernel-стороне.

---

## 9. WBIO — модульная шина Wiren Board

WB7 имеет собственную высокоскоростную шину «WBIO» для стековых модулей расширения (WBIO-DI-14, WBIO-DO-R10, WBIO-AI-DV, WBEC-I-CAN, WBEC-DAM-1). Драйверы:

| Kconfig | Роль |
|---------|------|
| `CONFIG_PINCTRL_MCP23S08=m` | SPI-GPIO экспандер MCP23S08 (WBIO-DI/DO) |
| `CONFIG_MCP4728=y` | I²C-DAC 12-бит (WBIO-AO) |
| `CONFIG_AD5064=y` | I²C-DAC AD5064 (WBIO-AO-4) |

Плюс WB-специфические patches для «wbio-bus» и «wbio recovery».

### Когда может понадобиться

- Появится конструктив СА-02м с WBIO-стеком или совместимой шиной (маловероятно — механически несовместимо).
- Захотим использовать **отдельные** MCP23S08 или MCP4728 модули в качестве универсального SPI/I²C-GPIO/DAC расширителя. Тогда — только соответствующий kconfig-флаг, WBIO-magic не нужен.

### Как включить (без WBIO-специфики)

1. Только `CONFIG_PINCTRL_MCP23S08=m` и `CONFIG_MCP4728=y` — без WBIO patches. Универсальные драйверы, работают с любым чипом.
2. DTS: SPI/I²C-узел с соответствующим compatible.

**Оценка сложности:** низкая.

---

## 10. USB-Gadget / composite (device mode)

WB7 использует USB-gadget-режим SoC-порта для сервисной прошивки (FIT bootlet, mass storage, ACM console). На СА-02м USB работает только в host-режиме (2 USB-A).

### Что урезаем сейчас

| Kconfig | Роль |
|---------|------|
| `CONFIG_USB_GADGET=m` | Ядро gadget-стека |
| `CONFIG_USB_CONFIGFS`, `CONFIG_USB_CONFIGFS_*` | ConfigFS-based composite gadget |
| `CONFIG_USB_ETH`, `CONFIG_USB_G_NCM`, `CONFIG_USB_MASS_STORAGE`, `CONFIG_USB_G_SERIAL` | Функции gadget |
| `CONFIG_U_SERIAL_CONSOLE` | UART-over-USB-console |
| `CONFIG_TYPEC`, `CONFIG_TYPEC_TCPM`, `CONFIG_USB_ROLE_SWITCH` | USB-C role switch |

### Когда может понадобиться

- Появится СА-02м-Recovery с USB-C: тогда gadget-mode позволит устройство прошивать через USB-cable как «флешку» (аналог WB7 factory bootlet).
- Прямое SSH-over-USB (RNDIS/ECM) для сервис-инженера — иногда полезно, если сеть недоступна.
- Экспорт RTC/serial-consol на сервисный USB.

### Как включить

1. Проверить, что USB0-порт СА-02м поддерживает OTG (device-mode) на аппаратном уровне (обычно да — musb-sunxi поддерживает).
2. `CONFIG_USB_MUSB_GADGET=m`, `CONFIG_USB_CONFIGFS=m`, `CONFIG_USB_CONFIGFS_NCM=y`, `CONFIG_USB_CONFIGFS_MASS_STORAGE=y`.
3. Systemd unit `sa02m-usb-gadget.service` с сценарием configfs-настройки.

**Оценка сложности:** средняя. Нужен OTG-детект в DTS + правильный `dr_mode`.

---

## 11. Крипто SoC (Sun4i-SS / Sun8i-CE)

WB7 включает аппаратные крипто-акселераторы A40i для AES/SHA/RNG.

### Что урезаем сейчас

Ничего — оставляем как есть (компактно, ускоряет OpenSSL/dm-crypt):

| Kconfig | Роль |
|---------|------|
| `CONFIG_CRYPTO_DEV_SUN4I_SS=y` | Security System — AES/SHA |
| `CONFIG_CRYPTO_DEV_SUN4I_SS_PRNG=y` | Аппаратный RNG |
| `CONFIG_CRYPTO_DEV_SUN8I_CE=y` | Crypto Engine (следующее поколение) |
| `CONFIG_CRYPTO_DEV_SUN8I_SS=y` | SS в sun8i-варианте |

### Комментарий

Не урезаем даже сейчас — фича лёгкая, а `dm-crypt`-разделы (если будут) заметно ускоряются.

---

## 12. UBI/UBIFS для NAND-флеш

WB7 использует NAND (SLC) с UBIFS для WBEC-раздела. СА-02м использует только eMMC (ext4).

### Что можно урезать

| Kconfig | Роль |
|---------|------|
| `CONFIG_MTD_UBI=y`, `CONFIG_MTD_UBI_FASTMAP=y`, `CONFIG_MTD_UBI_BLOCK=y` | UBI-контейнер |
| `CONFIG_MTD_DATAFLASH=m`, `CONFIG_MTD_SST25L=y` | SPI flash |
| `CONFIG_MTD_CMDLINE_PARTS=y`, `CONFIG_MTD_BLOCK=y` | MTD helpers |

### Когда может понадобиться

- Появится вариант СА-02м с SPI-NOR (для U-Boot env или factory data).
- USB-флешки не MTD-based — не нужно.

### Как включить

Просто включить `CONFIG_MTD_UBI=m` — драйвер маленький, накладных расходов почти нет. Не критично.

---

## 13. Сети — HSR / PRP / IEEE802154

WB7 поддерживает HSR (High-availability Seamless Redundancy) и IEEE802154 (ZigBee/Thread через USB-донгл).

### Что урезаем сейчас

| Kconfig | Роль |
|---------|------|
| `CONFIG_HSR=m` | HSR — резервирование Ethernet (2 линка → один MAC) |
| `CONFIG_IEEE802154=m`, `CONFIG_MAC802154=m` | 802.15.4 для ZigBee/Thread |

### Когда может понадобиться

- **HSR** — реальный кейс для industrial-Ethernet-резервирования (два `end0/end1` в одном HSR-кольце). На СА-02м-2 (2 Ethernet) это может быть очень полезно — тема для отдельной итерации.
- **IEEE802154** — редко нужен, но `nRF52840`/`CC2531` USB-донглы делают ZigBee-мост очень дёшево.

### Как включить

1. `CONFIG_HSR=m`, `CONFIG_PRP=m` — вернуть в defconfig.
2. Userspace: `ip link add name hsr0 type hsr slave1 end0 slave2 end1`.
3. Для 802.15.4: `CONFIG_IEEE802154=m`, `CONFIG_MAC802154=m`, `CONFIG_IEEE802154_ATUSB=m` (для nRF-USB-донгла — вернее его использовать напрямую как ZigBee-coordinator через `zigbee2mqtt`).

**Оценка сложности:** низкая на kernel-стороне; средняя для UX (HSR-конфигурация не тривиальна).

---

## 14. WBEC-battery / WBMZ (батарейное резервирование)

WB7 имеет опциональный модуль WBMZ («батарея») — суперконденсаторы или Li-Ion для аварийного питания.

### Что урезаем сейчас

| Kconfig | Роль |
|---------|------|
| `CONFIG_WBEC_POWER=m` | Управление WBMZ через WBEC |
| `CONFIG_BATTERY_EDLC=m` | Модель суперконденсатора |
| `CONFIG_GENERIC_ADC_BATTERY=m` | Общий ADC-battery |
| `CONFIG_CHARGER_GPIO=y` | Индикация зарядки через GPIO |

### Когда может понадобиться

- Появление СА-02м-UPS с суперкапом (актуально для критичных объектов, чтобы успеть корректно сохранить FS и погасить питание).

### Как включить

1. `CONFIG_BATTERY_EDLC=m`, `CONFIG_GENERIC_ADC_BATTERY=m`, `CONFIG_CHARGER_GPIO=y` — вернуть.
2. DTS: `battery-charger` node с GPIO-инпутами и ADC-каналом (см. `sun8i-r40-wirenboard72x.dtsi` строки 293-347).
3. Userspace: демон-monitor `/sys/class/power_supply/*` + graceful shutdown через `systemctl poweroff --no-block`.

**Оценка сложности:** средняя. Требует физической ревизии платы.

---

## 15. Bootlet / initramfs / FIT-images

WB7 использует FIT (Flattened Image Tree) images и отдельный initramfs («bootlet») для factory-reset и recovery. СА-02м использует классический U-Boot `boot.scr` → FAT-загрузка `zImage` (см. [`docs/codesys-rt/README.md`](codesys-rt/README.md#3-архитектура-загрузки-устройства)).

### Что урезаем сейчас (не тянем)

- `CONFIG_INITRAMFS_SOURCE=...` — не делаем встроенный initramfs.
- `wb-bootlet-*.deb` — не собираем.
- `sun8i-r40-wirenboard72x-initram.dts` — не переносим на СА-02м.

### Когда может понадобиться

- Захотим сделать recovery-режим через USB-кабель (аналог WB `wb-image-update`) — тогда нужен gadget-mode + initramfs.
- Захотим FIT-image для криптосигнатуры boot-цепочки (secure boot).

### Как включить

1. Отдельный `sa02m-bootlet` в форке — новый `sun8i-r40-sa02m-bootlet.dts` + `sa02m-bootlet.config` + `INITRAMFS_SOURCE=...` в defconfig.
2. `scripts/package/wb/version.sh`: добавить flavour `sa02m-bootlet`.
3. Собирается через `KERNEL_FLAVOUR=sa02m-bootlet bash scripts/package/wb/do_build_deb.sh` (см. `make_bootlet_deb` в [`scripts/package/wb/do_build_deb.sh`](https://github.com/wirenboard/linux/blob/release/wb-2606/wb7-bullseye/scripts/package/wb/do_build_deb.sh)).
4. Тиражирование через `wb-image-update` — отдельный проект. Пока проще старая процедура `tools/imaging/`.

**Оценка сложности:** высокая. Рассматривается только если реально нужен recovery-mode.

---

## Сводная таблица

| # | Компонент | Приоритет включения | Оценка сложности |
|---|-----------|--------------------|-----------------|
| 1 | WBEC | Низкий (нет чипа) | Низкая |
| 2 | AXP20x charger/battery + AC100 (только батарея/audio-dock, **сам PMIC AXP221 включен**) | Низкий (нет батареи/дока) | Средняя |
| 3 | Wi-Fi | **Средний** (USB-донглы) | Низкая (kernel), средняя (UX) |
| 4 | Bluetooth | Средний (BLE IoT) | Низкая |
| 5 | Медиа/HDMI/LVDS | Низкий (нет дисплея) | Высокая |
| 6 | 1-Wire | **Средний** (DS18B20) | Низкая |
| 7 | Analog ADC | Средний (расширения) | Средняя |
| 8 | CAN расширители | Низкий | Низкая |
| 9 | WBIO | Не планируется | — |
| 10 | USB-gadget | Средний (recovery) | Средняя |
| 11 | SoC crypto | Оставлено | — |
| 12 | UBI/UBIFS | Низкий | Низкая |
| 13 | HSR / 802.15.4 | **Высокий для СА-02м-2** (HSR) | Низкая |
| 14 | Battery/WBMZ | Низкий | Средняя |
| 15 | FIT bootlet | Низкий | Высокая |

**Топ-3 фичи для ближайшего roadmap:**

1. **HSR** для СА-02м-2 (`end0`+`end1` в резервирующее кольцо) — реальная промышленная потребность.
2. **Wi-Fi USB-адаптеры** — для мобильной настройки на объекте.
3. **1-Wire DS18B20** — дешёвые температурные датчики через любой GPIO.

## Как жить с этим документом

- Каждая новая ревизия СА-02м или отдельная задача, требующая одного из компонентов выше — заводится issue в GitHub со ссылкой на соответствующую секцию.
- При включении фичи: обновить `sa02m_defconfig` в форке `sa02m-linux`, обновить DTS, повысить `sa02m-N` суффикс в `debian/changelog`, обновить эту таблицу (пометить «Включено с версии `5.10.35-wb182-sa02mN`»).
- Раз в квартал сверять список с новыми WB-релизами (`release/wb-YYYY/wb7-bullseye`) — иногда появляются новые полезные патчи для sun8i-r40.
