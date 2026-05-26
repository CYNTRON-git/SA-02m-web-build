# MPLC4 Modbus + MQTT — промышленные сценарии

Путь A интеграции: используем штатные драйверы MPLC4 v1.3.10 (`mplc_modbus.so`, `mplc_mqtt.so`).

## Когда использовать MPLC4 вместо Python-моста

| Критерий | Python-мост | MPLC4 |
|----------|------------|-------|
| Простая передача данных Modbus→MQTT | ✓ | ✓ |
| Логика управления (PID, таймеры, алармы) | — | ✓ |
| Архивирование тегов | — | ✓ |
| OPC UA сервер | — | ✓ |
| Веб-конфигурация без командной строки | ✓ | Через браузер MasterSCADA |
| RAM-overhead | ~30 MB | ~100 MB |

## Установка и запуск

MPLC4 уже установлен в `/opt/mplc4/`. Запустить:

```bash
systemctl enable mplc4
systemctl start mplc4
# Проверить статус
systemctl status mplc4
```

Веб-интерфейс MasterSCADA 4D — порт **4096** (HTTP):
```
http://192.168.1.136:4096
```

## Настройка Modbus RTU канала

В MasterSCADA 4D:
1. **Конфигурация** → **Каналы ввода/вывода** → **Добавить**
2. Тип драйвера: `Modbus RTU` (mplc_modbus.so)
3. Параметры порта:
   - Порт: `/dev/COM1` (или COM2..COM5)
   - Скорость: 115200 (MR-02m, CE-02m-3) или 19200 (DTV)
   - Формат: 8N1
4. Добавить устройство: адрес 1–247
5. Добавить теги по адресной карте:

| Тег | Тип | Адрес Modbus |
|-----|-----|--------------|
| MR02m_DO1 | Coil RW | 00001 |
| MR02m_DI1 | DiscreteInput R | 10018 |
| MR02m_AO1 | HoldingReg RW | 40033 |
| MR02m_AI1_val | HoldingReg R | 40403 |
| DTV_temp_bme680 | InputReg R | 30005 |
| DTV_humidity | InputReg R | 30009 |
| DTV_presence | InputReg R | 30027 |
| CE02M3_voltage_a | InputReg R | 30500 |
| CE02M3_power_total_hi | InputReg R | 30524 |
| CE02M3_power_total_lo | InputReg R | 30525 |
| CE02M3_energy_import_r0 | InputReg R | 30580 |

## Настройка MQTT-публикатора

1. **Каналы** → **Добавить** → `MQTT` (mplc_mqtt.so)
2. Параметры брокера:
   ```
   Broker: 127.0.0.1
   Port: 1883
   Client ID: mplc4-mqtt
   QoS: 1
   ```
3. Топики (формат Wiren Board):
   ```
   /devices/mr02m-COM1-5/controls/do_1       ← DO1
   /devices/mr02m-COM1-5/controls/di_1       ← DI1
   /devices/dtv-COM3-1/controls/temp_bme680  ← T°C
   /devices/ce02m3-COM2-14/controls/power_total ← P Total
   ```
4. **Масштаб** задаётся в формуле тега MPLC4:
   - AI raw×0.1 → °C
   - CE-02m-3 power int32 ×0.1 → Вт
5. Для writable-тегов (DO, AO): подписаться на топик `/on` и передавать в Coil/HoldingReg.

## Координация с Python-мостом

MPLC4 и Python-мост **не должны опрашивать один порт одновременно** — RS-485 half-duplex.

Схемы разделения:
- **По портам**: MPLC4 — COM1+COM2, Python-мост — COM3+COM4
- **По устройствам**: MPLC4 — производственные MR-02m, Python-мост — DTV+CE-02m-3
- **Исключительный режим**: остановить `sa02m-modbus-mqtt` при запуске MPLC4 на том же порту

В `/etc/sa02m_flasher.conf` параметр `MPLC_STOP_SERVICES` управляет этим автоматически
при прошивке.

## Топики, публикуемые MPLC4

MPLC4 публикует значения по топикам, настроенным в проекте.  
Рекомендуется использовать **ту же структуру**, что и у Python-моста (`/devices/<id>/controls/<name>`),
чтобы все клиенты (SCADA, Node-RED, Home Assistant) видели единую иерархию.

Пример конфигурации тега в MPLC4:
```
Tag name: mr02m_COM1_5_do_1
MQTT topic: /devices/mr02m-COM1-5/controls/do_1
MQTT retained: yes
Scale: 1.0
```
