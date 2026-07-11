# -*- coding: utf-8 -*-
"""
Карта Modbus для модуля DTV (CYNTRON DTV-RS-485 / RTU_SENSOR).

Синхронизировано с прошивкой репозитория:
  https://github.com/CYNTRON-git/DTV-RS-485
и с `Core/Src/svc_modbus_registers.c` (Input/Holding 0–49, датчики 1–30).

Опрос живых значений в конфигураторе: Input регистры 1..30 (см. DTV_INPUT_LIVE_*).
"""

from __future__ import annotations

# Единый источник правды по адресам — README и svc_modbus_registers.c в DTV-RS-485.
DTV_FIRMWARE_REF = "https://github.com/CYNTRON-git/DTV-RS-485"

# Фоновый снимок датчиков (как в UI): FC04, стартовый адрес 1, количество 30.
DTV_INPUT_LIVE_START = 1
DTV_INPUT_LIVE_COUNT = 30

# Holding 6: выбор внешней температуры NTC10K vs PT1000 (0 = NTC10K, 1 = PT1000); на Input 6 — та же адресация, RO ×0.1 °C.
DTV_HOLDING_EXT_TEMP_SELECT = 6

# Holding: задержка выключения присутствия LD2412 (сек), то же адрес 27 что и Input «логика присутствия».
DTV_HOLDING_PRESENCE_OFF_DELAY = 27

# Калибровка: темп. 31–36 (смещение в десятых °C, диапазон как в прошивке ±100),
# влажность 37–39 (±200 в прошивке).
DTV_CALIB_TEMP_REGS = range(31, 37)
DTV_CALIB_HUM_REGS = range(37, 40)
DTV_CALIB_TEMP_MIN = -100
DTV_CALIB_TEMP_MAX = 100
DTV_CALIB_HUM_MIN = -200
DTV_CALIB_HUM_MAX = 200

# Калман: Holding 40–49.
DTV_KALMAN_START = 40
DTV_KALMAN_COUNT = 10

# Скользящее среднее по датчикам: непрерывный блок Holding **533–559** (27 регистров = 9×3 слота),
# как у MP/MR по шагу 3: для слота `s` субиндекс `k = 0..2` — адрес `533 + 3*s + k`.
# sub==0 — запись запрещена (чтение даёт 1); sub==1 — глубина `sensor_avg_depth[s]`, 1…50
# (запись 0 трактуется как 1); sub==2 — только запись 0. См. `svc_modbus_registers.c` и
# ветку `default` в `svc_mb_holding.c` (проект DTV-RS-485 / cyntron-dtv).
DTV_MOV_AVG_HOLDING_ANCHOR = 533
DTV_MOV_AVG_HOLDING_LAST = 559
DTV_MOV_AVG_HOLDING_SPAN = DTV_MOV_AVG_HOLDING_LAST - DTV_MOV_AVG_HOLDING_ANCHOR + 1  # 27
DTV_MOV_AVG_SLOT_COUNT = 9
DTV_MOV_AVG_DEPTH_MIN = 1
DTV_MOV_AVG_DEPTH_MAX = 50

# Явный список holding глубины по слотам 0..8 (= ANCHOR + 3*s + 1): 534, 536, …, 550.
DTV_MOV_AVG_DEPTH_REGS: tuple[int, ...] = tuple(
    DTV_MOV_AVG_HOLDING_ANCHOR + 3 * s + 1 for s in range(DTV_MOV_AVG_SLOT_COUNT)
)


def dtv_mov_avg_depth_holding_reg(slot: int) -> int:
    """Holding-адрес глубины MA для слота 0..8 (sub==1 в тройке 533+3×slot+{0,1,2})."""
    if slot < 0 or slot >= DTV_MOV_AVG_SLOT_COUNT:
        raise IndexError("DTV MA slot must be 0..8")
    return DTV_MOV_AVG_HOLDING_ANCHOR + 3 * int(slot) + 1


def dtv_mov_avg_depth_index_in_anchor_block(slot: int) -> int:
    """Смещение слова глубины внутри FC03, start=DTV_MOV_AVG_HOLDING_ANCHOR, count=DTV_MOV_AVG_HOLDING_SPAN."""
    return dtv_mov_avg_depth_holding_reg(slot) - DTV_MOV_AVG_HOLDING_ANCHOR


# Управление выходами (RTU_SENSOR / svc_mb_coils.c, gpio run_update_output_data):
# катушка 1 — пищалка; катушка 2 — все светодиоды вкл./выкл. (all_set_light / all_clear_light).
DTV_COIL_BUZZER = 1
DTV_COIL_LEDS_ALL = 2


def dtv_u16_to_s16(v: int) -> int:
    return v - 0x10000 if v >= 0x8000 else v
