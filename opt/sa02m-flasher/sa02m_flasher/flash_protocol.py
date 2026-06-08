# -*- coding: utf-8 -*-
"""
Протокол прошивки MP-02m (bootloader). Совместимость с алгоритмом WB (wb-mcu-fw-flasher).

Обнаружение: Modbus RTU (read reg 290/330), WB extended (0xFD 0x46 0x01).
Бутлоадер: 0xFD 0x46 0x08/0x09 по серийному или обычный Modbus по адресу 247, 115200 8N1.
Прошивка: 0x1000 (info 32 B, 16 рег BE) → пауза ~2.8 с (стирание) → 0x2000 (блоки).
Приложение: блоки 246 B (123 reg). WB: блоки 136 B (68 рег). Порядок байт для .fw — LE в Flash. Наши: 3 попытки на блок, при неудаче — прерывание; задержка 5 мс между блоками; таймаут 5 с.
Транспорт — Modbus RTU (0x10 Write Multiple Registers).
"""
from __future__ import annotations

import struct
import sys
import time
import zlib
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from . import modbus_rtu
from .modbus_io import (
    decode_bootloader_version_registers_8,
    decode_signature_from_holding_290_payload,
    uint32_from_modbus_reg_pair_be,
)
from .module_profiles import is_mp_module_signature_for_batch_flash

# --- Параметры линии бутлоадера (прошивка всегда на этой скорости) ---
# Должно совпадать с BL_UART_BAUDRATE / BL_BOOTLOADER_FIXED_BAUD в shared/bootloader/bootloader_config.h
BOOTLOADER_BAUDRATE = 115200
BOOTLOADER_PARITY = "N"
BOOTLOADER_STOPBITS = 1
# Бутлоадер по умолчанию слушает адрес 247; прошивка наших устройств — по этому адресу (обычный Modbus).
# Быстрый Modbus (0xFD 0x46 0x08) — только при выборе по серийному номеру (несколько устройств на линии).
BOOTLOADER_DEFAULT_ADDR = 247

# --- Регистры протокола прошивки (0x1000 / 0x2000) ---
INFO_BLOCK_REG = 0x1000
INFO_BLOCK_REGS = 16
INFO_BLOCK_BYTES = 32

DATA_BLOCK_REG = 0x2000
DATA_BLOCK_REGS = 123   # Modbus 0x10 max
DATA_BLOCK_BYTES = 246

# --- Wiren Board (wb-mcu-fw-flasher): 0x2000 — 68 рег (136 B) ---
DATA_BLOCK_BYTES_WB = 136
DATA_BLOCK_REGS_WB = 68
BOOTLOADER_BAUDRATE_WB = 9600
BOOTLOADER_STOPBITS_WB = 2
# Как в wb-mcu-fw-flasher: после info только interFrameDelay (sleep(0)), паузы нет — сразу первый блок.
ERASE_WAIT_AFTER_INFO_S_WB = 0.0
# Таймаут ответа загрузчика: BL_MINIMAL_RESPONSE_TIMEOUT 5.0 с (flasher.c)
BOOTLOADER_RESPONSE_TIMEOUT_S_WB = 5.0
BOOTLOADER_INFOBLOCK_TIMEOUT_MS_WB = max(3000, int(BOOTLOADER_RESPONSE_TIMEOUT_S_WB * 1000))
# Повторы info при ошибке: до MAX_ERROR_COUNT (3), между попытками sleep(3)
WB_INFO_RETRY_SLEEP_S = 3.0
WB_MAX_ERROR_COUNT = 3  # после 3 ошибок подряд на блоке — пропуск блока; после 6 подряд — выход

# --- Регистры загрузчика Wiren Board (опционально): 1000 UART reset, 1001 EEPROM erase, 1003 free space FlashFS ---
REG_WB_UART_RESET = 1000
REG_WB_EEPROM_ERASE = 1001
REG_WB_FREE_SPACE = 1003

# --- Обновление бутлоадера (0x1001 = 0x424C "BL", блоки 244 B / 122 reg, последний 168 B / 84 reg, commit 0x1006) ---
REG_FIRMWARE_TYPE = 0x1001
FIRMWARE_TYPE_BOOTLOADER = 0x424C  # "BL"
REG_COMMIT_BOOTLOADER = 0x1006
DATA_BLOCK_BYTES_BOOTLOADER = 244
DATA_BLOCK_REGS_BOOTLOADER = 122
DATA_BLOCK_LAST_BYTES_BOOTLOADER = 168
DATA_BLOCK_LAST_REGS_BOOTLOADER = 84
BL_STAGING_VECTORS_SIZE = 2048
BL_STAGING_CODE_SIZE = 32768
BL_IMAGE_TOTAL_BYTES = BL_STAGING_VECTORS_SIZE + BL_STAGING_CODE_SIZE  # 34816
BL_BLOCKS_FULL_COUNT = 142  # 142 * 244 = 34648; остаток 34816 - 34648 = 168 B (1 блок 84 reg)

# --- Регистры Modbus приложения/бутлоадера (для перехода и опроса) ---
REG_ENTER_BOOTLOADER = 129
REG_JUMP_APP = 1004
REG_PROGRAM_SERIAL = 1005  # Write 2 regs (lo, hi): запись серийного номера в Flash 0x080000C4
REG_SELECT_SERIAL = 0xF0   # Write 2 regs (serial_lo, serial_hi): выбор устройства по серийному при нескольких на линии
REG_LAST_INFO_REJECT = 0xF1  # Read 2 regs: код отказа info-блока (0=нет, 1=size, 2=sig_mismatch, 3=eeprom_write), при 2 — индекс байта во 2-м рег.
REG_LAST_DATA_REJECT = 0xF2  # Read 1 reg: код отказа блока данных (0=нет, 1=info, 2=staging, 3=offset, 4=erase_wait, 5=pending).
REG_SIGNATURE = 290
REG_SIGNATURE_COUNT = 12
# Рег. 330: в бутлоадере — строка из кода; в приложении — чтение из Flash 0x080000D0 (секция .bl_version бутлоадера). Формат: 8 регистров, младший байт = ASCII.
REG_BOOTLOADER_VERSION = 330
REG_BOOTLOADER_VERSION_COUNT = 8
# Версия приложения (в загрузчике бутлоадер отдаёт 0.0.0.0 — см. scanner._is_bootloader_mode).
REG_APP_VERSION_MAJOR = 320
REG_APP_VERSION_COUNT = 4

# Сигнатура = тип нижней платы (6DO8DI, 12AI, 14DI …), заглавными. Если не определена — NONE.
DEFAULT_SIGNATURE = "NONE"

# Пауза после успешного чтения регистров по быстрому Modbus (0x46 0x08 + внутренний 0x03):
# подряд два запроса без t3.5/времени обработки на МК дают таймаут или пустой разбор (сигнатура/версии «—»).
FAST_MODBUS_INTER_READ_GAP_S = 0.012

# --- Тайминги (с), совместимость с WB (wb-mcu-fw-flasher) и бутлоадером ---
# После info бутлоадер полностью стирает регион (см. MR-02m-flasher flash_protocol).
ERASE_WAIT_AFTER_INFO_S = 2.8
# Между блоками: по адресу — 5 мс (RS-485 turnaround + Flash); по 0x46 (быстрый Modbus) — 0 с, как у WB.
BLOCK_DELAY_AFTER_RESPONSE_S = 0.005
BLOCK_DELAY_FIRST_BLOCK_S = 0.005
BLOCK_DELAY_AFTER_RESPONSE_S_FAST = 0.0  # прошивка по серийному 0x46 (как WB)
BLOCK_DELAY_FIRST_BLOCK_S_FAST = 0.0
RETRY_DELAY_BETWEEN_BLOCKS_S = 0.3    # пауза перед повтором (дать МК время дописать Flash)
# Повтор при таймауте: увеличенная пауза (дренаж UART / переполнение буфера на МК) — как MR-02m-flasher.
RETRY_DELAY_AFTER_TIMEOUT_S = 2.0
RETRY_AFTER_SLAVE_FAILURE_FLASH_S = 1.0  # Modbus код 4 + 0xF2=4 (ожидание стирания)
# Для приложения наших устройств: 3 попытки на блок; при неудаче — прерывание (пропуск блока портит образ).
# WB: 3 попытки, пропуск блока, выход при 6 подряд — только в run_flash_sequence_wb.
APP_MAX_ERROR_COUNT = 3
# Таймаут ответа загрузчика при прошивке приложения (как WB: BL_MINIMAL_RESPONSE_TIMEOUT 5 с).
BOOTLOADER_RESPONSE_TIMEOUT_S = 5.0
BOOTLOADER_DATA_BLOCK_TIMEOUT_MS = max(600, int(BOOTLOADER_RESPONSE_TIMEOUT_S * 1000))
# Ответ на запись info 0x1000 / чтения в сессии прошивки (RS-485, обработка в бутлоадере) — как gui_flasher_support MR-02m-flasher.
INFO_RESPONSE_TIMEOUT_MS = 6000
# Рег. 1005: запись серийника / тяжёлые операции во Flash.
SERIAL_PROGRAM_RESPONSE_TIMEOUT_MS = max(8000, BOOTLOADER_DATA_BLOCK_TIMEOUT_MS)
# Повтор info-блока при ошибке/таймауте: как у WB — пауза 3 с между попытками.
INFO_RETRY_SLEEP_S = 3.0

# --- Как MR-02m-flasher/flasher_windows/gui_flasher_support.py + gui_flasher_flash_mixin ---
FAST_RESPONSE_TIMEOUT_MS = 500
DATA_BLOCK_RESPONSE_TIMEOUT_MS = 600
LINK_AND_FACTORY_WRITE_TIMEOUT_MS = 7500
# Перевод в загрузчик (reg 129) с линии приложения: MR flash mixin держит 2000 мс на PDU.
FLASH_ENTER_BOOTLOADER_APP_TIMEOUT_MS = 2000

# --- Лимиты: область записи приложения в бутлоадере 0x08000800..0x0802F800 (до staging). ---
FLASH_APP_START = 0x08000800
FLASH_APP_END = 0x0802F800
BOOTLOADER_MAX_APP_BYTES = FLASH_APP_END - FLASH_APP_START  # 190464 байт (~186 КБ)
MAX_FIRMWARE_SIZE_BYTES = BOOTLOADER_MAX_APP_BYTES
DEFAULT_RETRIES_PER_BLOCK = 5  # для образа бутлоадера; для приложения используется APP_MAX_ERROR_COUNT (3, как WB)

REG_SERIAL_LO = 270
REG_SERIAL_HI = 271
REG_DIAG_STATE = 0xF3
REG_DIAG_APP_VALID = 0xF4
REG_DIAG_JUMP_FAIL_COUNT = 0xF5
REG_DIAG_APP_MARKER_LO = 0xF6
REG_DIAG_FAULT_MARKER_LO = 0xF8
REG_DIAG_FLASH_STICKY_LO = 0xFA

BOOT_DIAG_STATE_INFO_RECEIVED = 1 << 0
BOOT_DIAG_STATE_ERASE_PENDING = 1 << 1
BOOT_DIAG_STATE_ERASE_WAITING = 1 << 2
BOOT_DIAG_STATE_ERASE_FAILED = 1 << 3
BOOT_DIAG_STATE_FLASH_COMPLETE = 1 << 4
BOOT_DIAG_STATE_REQUEST_JUMP = 1 << 5
BOOT_DIAG_STATE_IMAGE_CRC_FAILED = 1 << 6
BOOT_DIAG_STATE_PENDING_EEPROM_SIG = 1 << 7
BOOT_DIAG_STATE_STAGING_MODE = 1 << 8
BOOT_DIAG_STATE_TARGET_BOOTLOADER = 1 << 9
BOOT_DIAG_STATE_REGION_PRE_ERASED = 1 << 10
BOOT_DIAG_STATE_DATA_PENDING = 1 << 11

INFO_MAX_ATTEMPTS = 3
POST_ERASE_BEFORE_FIRST_DATA_S = 0.0
ERASE_READY_POLL_S = 0.18
ERASE_READY_MAX_WAIT_S = 15.0


@dataclass(frozen=True)
class BootloaderDiagnostics:
    signature: Optional[str] = None
    bootloader_version: Optional[str] = None
    serial_u32: Optional[int] = None
    info_reject_code: Optional[int] = None
    info_reject_detail: Optional[int] = None
    data_reject_code: Optional[int] = None
    state_flags: Optional[int] = None
    app_valid: Optional[bool] = None
    jump_fail_count: Optional[int] = None
    app_marker: Optional[int] = None
    fault_marker: Optional[int] = None
    flash_sticky: Optional[int] = None


def _read_u16_from_payload(payload: Optional[bytes]) -> Optional[int]:
    if not payload or len(payload) < 2:
        return None
    return (payload[0] << 8) | payload[1]


def _read_u32_regs_from_payload(payload: Optional[bytes]) -> Optional[int]:
    if not payload or len(payload) < 4:
        return None
    return uint32_from_modbus_reg_pair_be(payload, 0)


def _last_info_reject_reason_text(code: Optional[int], detail: Optional[int] = None) -> Optional[str]:
    if code is None:
        return None
    reasons = {
        0: "нет",
        1: "неверный размер образа",
        2: "несовпадение сигнатуры с EEPROM",
        3: "ошибка записи EEPROM сигнатуры",
        4: "в info нет CRC2/CRC32",
        5: "размер меньше минимального AppBoot",
    }
    text = reasons.get(int(code), f"код {int(code)}")
    if int(code) == 2 and detail not in (None, 0):
        text += f", байт {int(detail)}"
    return text


def _last_data_reject_reason_text(code: Optional[int]) -> Optional[str]:
    if code is None:
        return None
    reasons = {
        0: "нет",
        1: "info ещё не принят",
        2: "устройство в staging-режиме bootloader",
        3: "смещение/размер блока вне диапазона",
        4: "идёт стирание Flash",
        5: "предыдущий блок ещё не обработан",
        6: "таймаут стирания региона",
        7: "CRC образа не совпала",
        8: "read-back таблицы векторов не совпал",
    }
    return reasons.get(int(code), f"код {int(code)}")


def _bootloader_ready_for_data(state_flags: Optional[int], data_reject: Optional[int]) -> bool:
    flags = int(state_flags or 0)
    if flags & (BOOT_DIAG_STATE_ERASE_PENDING | BOOT_DIAG_STATE_ERASE_WAITING):
        return False
    if data_reject in (4, 5):
        return False
    return True


def _collect_bootloader_diagnostics_impl(
    read_regs: Callable[[int, int], Tuple[Optional[bytes], Optional[str]]],
    read_boot_info: Callable[[], Tuple[Optional[str], Optional[str], Optional[str]]],
) -> BootloaderDiagnostics:
    sig, bl_ver, _ = read_boot_info()
    payload_sn, _ = read_regs(REG_SERIAL_LO, 2)
    payload_f1, _ = read_regs(REG_LAST_INFO_REJECT, 2)
    payload_f2, _ = read_regs(REG_LAST_DATA_REJECT, 1)
    payload_state, _ = read_regs(REG_DIAG_STATE, 1)
    payload_app_valid, _ = read_regs(REG_DIAG_APP_VALID, 1)
    payload_jump_fail, _ = read_regs(REG_DIAG_JUMP_FAIL_COUNT, 1)
    payload_app_marker, _ = read_regs(REG_DIAG_APP_MARKER_LO, 2)
    payload_fault_marker, _ = read_regs(REG_DIAG_FAULT_MARKER_LO, 2)
    payload_flash_sticky, _ = read_regs(REG_DIAG_FLASH_STICKY_LO, 2)
    return BootloaderDiagnostics(
        signature=sig,
        bootloader_version=bl_ver,
        serial_u32=_read_u32_regs_from_payload(payload_sn),
        info_reject_code=_read_u16_from_payload(payload_f1),
        info_reject_detail=((payload_f1[2] << 8) | payload_f1[3]) if payload_f1 and len(payload_f1) >= 4 else None,
        data_reject_code=_read_u16_from_payload(payload_f2),
        state_flags=_read_u16_from_payload(payload_state),
        app_valid=(bool(_read_u16_from_payload(payload_app_valid)) if _read_u16_from_payload(payload_app_valid) is not None else None),
        jump_fail_count=_read_u16_from_payload(payload_jump_fail),
        app_marker=_read_u32_regs_from_payload(payload_app_marker),
        fault_marker=_read_u32_regs_from_payload(payload_fault_marker),
        flash_sticky=_read_u32_regs_from_payload(payload_flash_sticky),
    )


def collect_bootloader_diagnostics_by_serial(
    flasher: "FlasherProtocol",
    serial: int,
) -> BootloaderDiagnostics:
    return _collect_bootloader_diagnostics_impl(
        lambda start, count: flasher.read_holding_registers_by_serial(serial, start, count),
        lambda: flasher.read_bootloader_info_by_serial(serial),
    )


def collect_bootloader_diagnostics_by_address(
    flasher: "FlasherProtocol",
    slave: int,
) -> BootloaderDiagnostics:
    return _collect_bootloader_diagnostics_impl(
        lambda start, count: flasher.read_holding_registers(slave, start, count),
        lambda: flasher.read_bootloader_info(slave),
    )


def _wait_bootloader_ready_for_data_impl(
    flasher: "FlasherProtocol",
    read_regs: Callable[[int, int], Tuple[Optional[bytes], Optional[str]]],
    target_label: str,
    *,
    max_wait_s: float = ERASE_READY_MAX_WAIT_S,
) -> Optional[str]:
    started_at = time.perf_counter()
    deadline = started_at + max_wait_s
    last_state: Optional[int] = None
    last_reject: Optional[int] = None
    last_err: Optional[str] = None
    legacy_wait_due_to_missing_diag = False
    if flasher.log_cb:
        flasher.log_cb(
            "Ожидание готовности bootloader к первому блоку данных (%s): опрос diag/state вместо фиксированной паузы."
            % target_label
        )
    while time.perf_counter() < deadline:
        payload_state, err_state = read_regs(REG_DIAG_STATE, 1)
        payload_f2, err_f2 = read_regs(REG_LAST_DATA_REJECT, 1)
        # Exc 02 = регистр не поддерживается (старый bootloader).
        # Таймаут = bootloader занят блокирующим стиранием Flash (bl_nmbs_poll не вызывается).
        # В обоих случаях переключаемся на legacy-паузу ERASE_WAIT_AFTER_INFO_S.
        state_unsupported = (
            _modbus_inner_exception_code(err_state) == 2
            or _fp_is_timeout_err(err_state)
        )
        reject_unsupported = (
            _modbus_inner_exception_code(err_f2) == 2
            or _fp_is_timeout_err(err_f2)
        )
        if state_unsupported and not legacy_wait_due_to_missing_diag:
            legacy_wait_due_to_missing_diag = True
            if flasher.log_cb:
                missing = ["0xF3"]
                if reject_unsupported:
                    missing.append("0xF2")
                flasher.log_cb(
                    "Bootloader не поддерживает %s для %s; используем совместимый fallback "
                    "с legacy-паузой %.1f с перед первым блоком данных."
                    % ("/".join(missing), target_label, ERASE_WAIT_AFTER_INFO_S)
                )
        if state_unsupported:
            err_state = None
            payload_state = None
        if reject_unsupported:
            err_f2 = None
            payload_f2 = None
        state = _read_u16_from_payload(payload_state)
        reject = _read_u16_from_payload(payload_f2)
        if err_state:
            last_err = err_state
        elif err_f2:
            last_err = err_f2
        else:
            last_state = state
            last_reject = reject
            if state is not None and (state & BOOT_DIAG_STATE_ERASE_FAILED):
                return (
                    "Bootloader сообщил ошибку стирания региона перед первым блоком данных "
                    f"({target_label}, state=0x{state:04X}, 0xF2={reject})."
                )
            if legacy_wait_due_to_missing_diag and state is None:
                if reject in (4, 5):
                    time.sleep(ERASE_READY_POLL_S)
                    continue
                if (time.perf_counter() - started_at) < ERASE_WAIT_AFTER_INFO_S:
                    time.sleep(ERASE_READY_POLL_S)
                    continue
                if flasher.log_cb:
                    flasher.log_cb(
                        "Bootloader готов к приёму первого блока данных (%s) по fallback без 0xF3. 0xF2=%s."
                        % (target_label, "—" if reject is None else str(int(reject)))
                    )
                return None
            if _bootloader_ready_for_data(state, reject):
                if flasher.log_cb:
                    flasher.log_cb(
                        "Bootloader готов к приёму первого блока данных (%s). state=0x%04X, 0xF2=%s."
                        % (target_label, int(state or 0), "—" if reject is None else str(int(reject)))
                    )
                return None
        time.sleep(ERASE_READY_POLL_S)
    detail = []
    if last_state is not None:
        detail.append(f"state=0x{int(last_state):04X}")
    if last_reject is not None:
        detail.append(f"0xF2={int(last_reject)}")
    if last_err:
        detail.append(last_err)
    suffix = ("; ".join(detail)) if detail else "нет ответа от диагностических регистров"
    return (
        "Не дождались готовности bootloader к первому блоку данных "
        f"({target_label}) за {max_wait_s:.1f} с: {suffix}."
    )


def wait_bootloader_ready_for_data_by_serial(
    flasher: "FlasherProtocol",
    serial: int,
) -> Optional[str]:
    return _wait_bootloader_ready_for_data_impl(
        flasher,
        lambda start, count: flasher.read_holding_registers_by_serial(serial, start, count),
        "0x46",
    )


def wait_bootloader_ready_for_data_by_address(
    flasher: "FlasherProtocol",
    slave: int,
) -> Optional[str]:
    return _wait_bootloader_ready_for_data_impl(
        flasher,
        lambda start, count: flasher.read_holding_registers(slave, start, count),
        "addr %d" % int(slave),
    )


def _validate_bootloader_board_signature_for_bin_flash(
    board_sig: Optional[str],
) -> Optional[str]:
    s = (board_sig or "").strip()
    if not s:
        return (
            "Не удалось прочитать сигнатуру нижней платы (рег. 290) в загрузчике. "
            "Проверьте адрес загрузчика, скорость 115200 8N1 и режим bootloader."
        )
    if not is_mp_module_signature_for_batch_flash(s):
        return (
            f"Сигнатура «{s}» не поддерживается для прошивки приложения из MR-02m Flasher по Modbus. "
            "Используйте образ .fw из каталога для вашей платы или проверьте подключение."
        )
    return None


def _read_board_signature_bootloader_warmup(
    flasher: "FlasherProtocol",
    *,
    slave: Optional[int] = None,
    serial: Optional[int] = None,
) -> Tuple[Optional[str], Optional[str]]:
    if serial is not None and (serial & 0xFFFFFFFF) not in (0, 0xFFFFFFFF):
        sn = int(serial) & 0xFFFFFFFF
        flasher.read_holding_registers_by_serial(sn, REG_SIGNATURE, REG_SIGNATURE_COUNT)
        time.sleep(max(FAST_MODBUS_INTER_READ_GAP_S, 0.03))
        sig, _ver, err = flasher.read_bootloader_info_by_serial(sn)
        if err:
            return None, err
        return (sig.strip() if sig else None), None
    if slave is not None and 1 <= int(slave) <= 247:
        a = int(slave)
        flasher.read_holding_registers(a, REG_SIGNATURE, REG_SIGNATURE_COUNT)
        time.sleep(max(FAST_MODBUS_INTER_READ_GAP_S, 0.03))
        sig, _ver, err = flasher.read_bootloader_info(a)
        if err:
            return None, err
        return (sig.strip() if sig else None), None
    return None, "Не задан адрес загрузчика или серийный номер для чтения рег. 290."


class FlashCancelGate:
    """
    Отмена до mark_irreversible() — прерывание прошивки.
    После mark_irreversible() запрос отмены только блокируется.
    """

    __slots__ = ("_cancel_cb", "_on_blocked", "irreversible")

    def __init__(
        self,
        cancel_cb: Optional[Callable[[], bool]],
        *,
        on_cancel_blocked: Optional[Callable[[], None]] = None,
    ) -> None:
        self._cancel_cb = cancel_cb
        self._on_blocked = on_cancel_blocked
        self.irreversible = False

    @classmethod
    def legacy_cancel(cls, cancel_cb: Optional[Callable[[], bool]]) -> "FlashCancelGate":
        return cls(cancel_cb, on_cancel_blocked=None)

    def mark_irreversible(self) -> None:
        self.irreversible = True

    def should_abort(self) -> bool:
        if not self._cancel_cb or not self._cancel_cb():
            return False
        if self.irreversible:
            if self._on_blocked:
                self._on_blocked()
            return False
        return True

    def sleep(self, total_s: float, step_s: float = 0.12) -> bool:
        end = time.perf_counter() + float(total_s)
        while time.perf_counter() < end:
            if self.should_abort():
                return True
            time.sleep(min(step_s, max(0.0, end - time.perf_counter())))
        return False


def _resolve_flash_cancel_gate(
    cancel_cb: Optional[Callable[[], bool]],
    cancel_gate: Optional[FlashCancelGate],
) -> FlashCancelGate:
    if cancel_gate is not None:
        return cancel_gate
    return FlashCancelGate.legacy_cancel(cancel_cb)


def bootloader_profiled_response_timeout_ms(
    request: bytes,
    *,
    is_wb_firmware: bool = False,
) -> int:
    """
    Таймаут приёма ответа транспорта в сеансе прошивки на линии загрузчика.
    Совпадает с MR-02m-flasher/gui_flasher_flash_mixin.py::_send_recv_profiled
    (is_wb_firmware == (suffix образа == .wbfw)).
    """
    fast = FAST_RESPONSE_TIMEOUT_MS
    if len(request) < 2:
        return fast
    # Быстрый Modbus 0xFD 0x46 0x08: длинные кадры (блоки 0x2000) — BOOTLOADER_DATA_BLOCK_TIMEOUT_MS и т.д.
    if request[0] == 0xFD and request[1] == 0x46:
        to_ms = BOOTLOADER_DATA_BLOCK_TIMEOUT_MS if len(request) > 100 else 3500
        if len(request) >= 10 and request[2] == 0x08 and request[7] == 0x10:
            inner_start = (request[8] << 8) | request[9]
            if inner_start == REG_PROGRAM_SERIAL:
                to_ms = max(to_ms, SERIAL_PROGRAM_RESPONSE_TIMEOUT_MS)
            elif inner_start == INFO_BLOCK_REG:
                if is_wb_firmware:
                    to_ms = max(
                        to_ms,
                        max(INFO_RESPONSE_TIMEOUT_MS, BOOTLOADER_INFOBLOCK_TIMEOUT_MS_WB),
                    )
                else:
                    to_ms = max(to_ms, INFO_RESPONSE_TIMEOUT_MS)
            elif inner_start == DATA_BLOCK_REG:
                to_ms = max(
                    to_ms,
                    BOOTLOADER_INFOBLOCK_TIMEOUT_MS_WB
                    if is_wb_firmware
                    else BOOTLOADER_DATA_BLOCK_TIMEOUT_MS,
                )
        return to_ms
    if request[1] == 0x03:
        return INFO_RESPONSE_TIMEOUT_MS
    if request[1] == 0x10 and len(request) >= 6:
        start_addr = (request[2] << 8) | request[3]
        if start_addr == REG_PROGRAM_SERIAL:
            return SERIAL_PROGRAM_RESPONSE_TIMEOUT_MS
        if request[2] == 0x10 and request[3] == 0x00:
            if is_wb_firmware:
                return max(INFO_RESPONSE_TIMEOUT_MS, BOOTLOADER_INFOBLOCK_TIMEOUT_MS_WB)
            return INFO_RESPONSE_TIMEOUT_MS
        if request[2] == 0x20 and request[3] == 0x00:
            return (
                BOOTLOADER_INFOBLOCK_TIMEOUT_MS_WB
                if is_wb_firmware
                else BOOTLOADER_DATA_BLOCK_TIMEOUT_MS
            )
    return fast


def _modbus_inner_exception_code(err: Optional[str]) -> Optional[int]:
    """Из текста «Исключение Modbus: код N» достать N (внутренний PDU в 0x46)."""
    if not err:
        return None
    marker = "код "
    i = err.find(marker)
    if i < 0:
        return None
    tail = err[i + len(marker) :].strip().split()[0]
    try:
        return int(tail, 10)
    except ValueError:
        try:
            return int(tail, 0)
        except ValueError:
            return None


def _fp_is_timeout_err(err: object) -> bool:
    if not err:
        return False
    e = str(err).lower()
    return "таймаут" in e or "timeout" in e


def _sleep_before_retry_flash_data_block_impl(
    flasher: "FlasherProtocol",
    err: Optional[str],
    block_index: int,
    read_f2: Callable[[], Tuple[Optional[bytes], Optional[str]]],
) -> None:
    """Пауза перед повтором блока 0x2000; при таймауте — увеличенная пауза; при коде 4 и 0xF2=4 — ожидание стирания."""
    delay = RETRY_DELAY_BETWEEN_BLOCKS_S
    if "Таймаут" in (err or ""):
        delay = max(delay, RETRY_DELAY_AFTER_TIMEOUT_S)
        if flasher.log_cb:
            flasher.log_cb(
                "Таймаут блока %d: пауза %.1f с перед повтором."
                % (block_index + 1, delay)
            )
    elif _modbus_inner_exception_code(err) == 4:
        pl, re = read_f2()
        if not re and pl and len(pl) >= 2:
            rej = (pl[0] << 8) | pl[1]
            if rej == 4:
                delay = max(delay, RETRY_AFTER_SLAVE_FAILURE_FLASH_S)
                if flasher.log_cb:
                    flasher.log_cb(
                        "Бутлоадер: 0xF2=4 (ожидание стирания), пауза %.1f с перед повтором блока %d."
                        % (delay, block_index + 1)
                    )
            elif rej == 5:
                delay = max(delay, 0.55)
                if flasher.log_cb:
                    flasher.log_cb(
                        "Бутлоадер: 0xF2=5 (pending), пауза %.2f с." % delay
                    )
        else:
            delay = max(delay, RETRY_AFTER_SLAVE_FAILURE_FLASH_S)
            if flasher.log_cb:
                flasher.log_cb(
                    "Исключение Modbus код 4 при блоке %d: пауза %.1f с перед повтором."
                    % (block_index + 1, delay)
                )
    time.sleep(delay)


def _sleep_before_retry_flash_data_block(
    flasher: "FlasherProtocol",
    serial: int,
    err: Optional[str],
    block_index: int,
) -> None:
    serial = serial & 0xFFFFFFFF

    _sleep_before_retry_flash_data_block_impl(
        flasher,
        err,
        block_index,
        lambda: flasher.read_holding_registers_by_serial(
            serial, REG_LAST_DATA_REJECT, 1
        ),
    )


def _sleep_before_retry_flash_data_block_slave(
    flasher: "FlasherProtocol",
    slave: int,
    err: Optional[str],
    block_index: int,
) -> None:
    _sleep_before_retry_flash_data_block_impl(
        flasher,
        err,
        block_index,
        lambda: flasher.read_holding_registers(slave, REG_LAST_DATA_REJECT, 1),
    )


def _fw_payload_first_8_bytes_to_le(fw_data: bytes) -> bytes:
    """
    В .fw payload хранится как 16-битные слова Big-Endian (make_fw.py).
    Первые 8 байт payload = 4 слова BE → преобразуем в 2 слова LE (SP, Reset_Handler) для check_app_vector_table.
    """
    if len(fw_data) < 8:
        return b""
    # 4 слова BE: (b0<<8|b1), (b2<<8|b3), (b4<<8|b5), (b6<<8|b7) → SP_lo, SP_hi, Res_lo, Res_hi
    sp_lo = (fw_data[0] << 8) | fw_data[1]
    sp_hi = (fw_data[2] << 8) | fw_data[3]
    res_lo = (fw_data[4] << 8) | fw_data[5]
    res_hi = (fw_data[6] << 8) | fw_data[7]
    sp = sp_lo | (sp_hi << 16)
    res = res_lo | (res_hi << 16)
    return struct.pack("<II", sp, res)


def _image_starts_like_appboot_vector_table(image: bytes) -> bool:
    """Первые байты похожи на таблицу векторов приложения (RAM SP), а не на ASCII-сигнатуру .fw."""
    if len(image) < 8:
        return False
    sp = struct.unpack_from("<I", image, 0)[0]
    if sp == 0 or sp == 0xFFFFFFFF:
        return False
    return (sp & 0x2FFE0000) == 0x20000000


def _image_has_embedded_fw_header(image: bytes) -> bool:
    """
    Полный файл .fw: 12 B sig + 4 B size LE + b'CRC2' + crc32 + pad.

    У сырого AppBoot .bin по offset 12 может случайно оказаться число из допустимого диапазона,
    поэтому для ветки .fw проверяем наличие CRC2-маркера. Для старого .fw без CRC2 оставляем
    совместимость: печатная сигнатура в начале и отсутствие таблицы векторов приложения.
    """
    if len(image) < INFO_BLOCK_BYTES:
        return False
    size_le = struct.unpack_from("<I", image, 12)[0]
    if not (1 <= size_le <= MAX_FIRMWARE_SIZE_BYTES):
        return False
    if image[16:20] == b"CRC2":
        return True
    if _image_starts_like_appboot_vector_table(image):
        return False
    sig_raw = image[:12].split(b"\x00", 1)[0]
    if len(sig_raw) < 2:
        return False
    if not all(0x20 <= b <= 0x7E for b in sig_raw):
        return False
    return True


def check_app_vector_table(image: bytes) -> Optional[str]:
    """
    Проверка: первые 8 байт образа — таблица векторов для 0x08000800.
    SP (word0) должен быть в RAM 0x2000xxxx; Reset_Handler (word1) — в 0x08000800..0x0802F800, LSB=1 (Thumb).
    image: либо сырые 8 байт LE (как в .bin), либо уже преобразованные из .fw через _fw_payload_first_8_bytes_to_le.
    Returns None если OK, иначе текст ошибки (образ собран не под бутлоадер / не тот файл).
    """
    if len(image) < 8:
        return "Образ короче 8 байт (нет таблицы векторов)."
    sp, res = struct.unpack_from("<II", image, 0)
    if sp == 0 or sp == 0xFFFFFFFF:
        return f"Таблица векторов невалидна: SP=0x{sp:08X} (ожидается 0x2000xxxx). Соберите приложение с линкером под бутлоадер (STM32F030CCXX_FLASH_boot.ld, ORIGIN 0x08000800)."
    if (sp & 0x2FFE0000) != 0x20000000:
        hint = " Выбран образ Debug/Release или не тот файл — соберите и откройте образ конфигурации AppBoot." if (sp >> 16) == 0x0020 else ""
        return f"Таблица векторов: SP=0x{sp:08X} не в RAM (0x2000xxxx). Нужен образ под бутлоадер: конфигурация AppBoot, линкер STM32F030CCXX_FLASH_boot.ld (ORIGIN 0x08000800).{hint}"
    if res == 0 or res == 0xFFFFFFFF:
        return f"Таблица векторов невалидна: Reset_Handler=0x{res:08X}. Используйте образ приложения под бутлоадер (0x08000800)."
    if (res & 0x08000000) == 0:
        return f"Таблица векторов: Reset_Handler=0x{res:08X} не во Flash (0x0800xxxx). Соберите с STM32F030CCXX_FLASH_boot.ld."
    if res < FLASH_APP_START or res >= FLASH_APP_END:
        return f"Таблица векторов: Reset_Handler=0x{res:08X} вне области приложения 0x{FLASH_APP_START:08X}..0x{FLASH_APP_END:08X}. Не тот образ или линкер (нужен FLASH_boot.ld)."
    if (res & 1) == 0:
        return f"Таблица векторов: Reset_Handler=0x{res:08X} (LSB должен быть 1, Thumb). Повреждённый образ или не тот файл."
    return None


def _safe_display_bytes(raw: bytes, max_len: int = 0) -> str:
    """Безопасный вывод для лога/UI: latin-1, непечатаемые → точка."""
    if not raw:
        return ""
    s = raw.decode("latin-1")
    out = "".join(c if 32 <= ord(c) <= 126 else "." for c in s).rstrip(". ")
    return out[:max_len] if max_len else out


def _hex_packet_log(data: bytes, bytes_per_line: int = 16) -> str:
    """Форматирование байт для детального лога: строки по bytes_per_line в hex."""
    if not data:
        return ""
    lines = []
    for i in range(0, len(data), bytes_per_line):
        chunk = data[i : i + bytes_per_line]
        lines.append(" ".join(f"{b:02X}" for b in chunk))
    return "\n".join(lines)


def build_info_block(signature: str, firmware_size: int, payload: bytes) -> bytes:
    """
    32 байта как в make_fw.py:
    сигнатура 12 (ASCII, null-pad), размер 4 B LE, b'CRC2', CRC32 полезной нагрузки LE, pad до 32.
    """
    sig = signature.encode("ascii", errors="replace")[:12].ljust(12, b"\x00")
    n = len(payload)
    if int(firmware_size) != n:
        raise ValueError("build_info_block: firmware_size must equal len(payload)")
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    return sig + struct.pack("<I", n) + b"CRC2" + struct.pack("<I", crc) + (b"\x00" * 8)


def info_block_to_registers(info: bytes) -> List[int]:
    """32 байта → 16 регистров, big-endian на регистр."""
    if len(info) != INFO_BLOCK_BYTES:
        raise ValueError(f"info must be {INFO_BLOCK_BYTES} bytes")
    return [(info[i] << 8) | info[i + 1] for i in range(0, INFO_BLOCK_BYTES, 2)]


def payload_block_to_registers(block: bytes) -> List[int]:
    """246 байт → 123 регистра, big-endian (старший байт первым в слове)."""
    if len(block) != DATA_BLOCK_BYTES:
        raise ValueError(f"block must be {DATA_BLOCK_BYTES} bytes")
    return [(block[i] << 8) | block[i + 1] for i in range(0, DATA_BLOCK_BYTES, 2)]


def payload_block_to_registers_app_le(block: bytes) -> List[int]:
    """246 B блока приложения (.fw): в файле слова BE; бутлоадер пишет в буфер (reg>>8, reg&0xFF).
    Чтобы в Flash получился little-endian, шлём регистр как (мл.байт<<8)|ст.байт."""
    if len(block) != DATA_BLOCK_BYTES:
        raise ValueError(f"block must be {DATA_BLOCK_BYTES} bytes")
    return [(block[i + 1] << 8) | block[i] for i in range(0, DATA_BLOCK_BYTES, 2)]


def payload_bytes_to_registers_le(block: bytes) -> List[int]:
    """Произвольный чётный буфер (.fw payload байт-своп) → регистры app_le:
    swap каждой пары → в staging окажется raw binary."""
    n = len(block)
    if n % 2 != 0:
        raise ValueError("block length must be even")
    return [(block[i + 1] << 8) | block[i] for i in range(0, n, 2)]


def info_block_to_registers_le(info: bytes) -> List[int]:
    """32 байта как 16 слов LE (формат .wbfw) → 16 регистров для Modbus (BE на линии)."""
    if len(info) < INFO_BLOCK_BYTES:
        info = info + b"\x00" * (INFO_BLOCK_BYTES - len(info))
    return [(info[2 * i + 1] << 8) | info[2 * i] for i in range(INFO_BLOCK_REGS)]


def payload_block_to_registers_wb(block: bytes) -> List[int]:
    """136 байт → 68 регистров Big-Endian (как в wb-mcu-fw-updater: байт 0 = старший байт слова)."""
    if len(block) < DATA_BLOCK_BYTES_WB:
        block = block + b"\xff" * (DATA_BLOCK_BYTES_WB - len(block))
    return [(block[i] << 8) | block[i + 1] for i in range(0, DATA_BLOCK_BYTES_WB, 2)]


class FlasherProtocol:
    """
    Протокол прошивки бутлоадера MP-02m.
    Транспорт: Modbus RTU (send_receive). Используется для перехода в бутлоадер (reg 129),
    чтения информации (reg 290/330), отправки info (0x1000) и блоков данных (0x2000), перехода в приложение (1004).
    """

    def __init__(
        self,
        send_receive: Callable[[bytes], Optional[bytes]],
        timeout_ms: int = FLASH_ENTER_BOOTLOADER_APP_TIMEOUT_MS,
        log_cb: Optional[Callable[[str], None]] = None,
        verbose_exchange_log: bool = False,
    ):
        self.send_receive = send_receive
        self.timeout_ms = timeout_ms
        self.log_cb = log_cb
        self.verbose_exchange_log = verbose_exchange_log

    def _exchange(
        self, request: bytes, log_timeout: bool = True
    ) -> Tuple[Optional[int], Optional[bytes], Optional[str]]:
        # Не логировать каждый блок 0x2000 в горячем пути — 775×3 вызовов лога дают ~100+ с из-за GUI (insert/see).
        is_data_block = (
            len(request) >= 8
            and request[1] == 0x10
            and (request[2] << 8) | request[3] == DATA_BLOCK_REG
        )
        if self.log_cb and len(request) >= 2 and not is_data_block:
            addr, func = request[0], request[1]
            line = f"  [Modbus] TX addr={addr} func=0x{func:02X}"
            if len(request) >= 8 and func in (0x03, 0x04):
                start = (request[2] << 8) | request[3]
                qty = (request[4] << 8) | request[5]
                line += f" reg={start} qty={qty}"
            elif len(request) >= 8 and func == 0x10:
                start = (request[2] << 8) | request[3]
                qty = (request[4] << 8) | request[5]
                line += f" write reg=0x{start:X} qty={qty}"
            self.log_cb(line)
        if self.log_cb and self.verbose_exchange_log and request:
            self.log_cb(f"  TX hex ({len(request)} bytes):")
            for ln in _hex_packet_log(request).split("\n"):
                self.log_cb("    " + ln)
        response = self.send_receive(request)
        if response is None:
            if self.log_cb and log_timeout:
                self.log_cb("  [Modbus] RX: таймаут")
            return None, None, "Таймаут ответа"
        if self.log_cb and not is_data_block:
            self.log_cb(f"  [Modbus] RX: {len(response)} bytes")
        if self.log_cb and self.verbose_exchange_log and response:
            self.log_cb(f"  RX hex ({len(response)} bytes):")
            for ln in _hex_packet_log(response).split("\n"):
                self.log_cb("    " + ln)
        slave, payload, err = modbus_rtu.parse_response(
            response,
            expected_slave=None,
            log_cb=(self.log_cb if self.verbose_exchange_log else None),
        )
        if err is not None:
            err = f"{err} (ответ: {response[:64].hex()})"
        if self.log_cb and not is_data_block:
            self.log_cb(
                f"  [Modbus] parse: slave={slave} err={'OK' if err is None else err}"
            )
        if self.log_cb and self.verbose_exchange_log:
            self.log_cb(f"parse: slave={slave} err={err if err else 'OK'}")
        return slave, payload, err

    def _exchange_by_serial(
        self,
        serial: int,
        inner_pdu: bytes,
        log_timeout: bool = True,
        is_data_block: bool = False,
    ) -> Tuple[Optional[bytes], Optional[str]]:
        """Обмен с бутлоадером по серийному номеру (0xFD 0x46 0x08). Возвращает (inner_payload, err)."""
        req = modbus_rtu.build_fast_modbus_request(serial & 0xFFFFFFFF, inner_pdu)
        if self.log_cb and not is_data_block:
            self.log_cb(f"  [Modbus] TX 0xFD 0x46 0x08 serial=0x{serial:08X} len={len(inner_pdu)}")
        if self.log_cb and self.verbose_exchange_log and req:
            self.log_cb("  TX hex:")
            for ln in _hex_packet_log(req).split("\n"):
                self.log_cb("    " + ln)
        response = self.send_receive(req)
        if response is None:
            if self.log_cb and log_timeout:
                self.log_cb("  [Modbus] RX: таймаут")
            return None, "Таймаут ответа"
        if self.log_cb and not is_data_block:
            self.log_cb(f"  [Modbus] RX: {len(response)} bytes")
        if self.log_cb and self.verbose_exchange_log and response:
            self.log_cb("  RX hex:")
            for ln in _hex_packet_log(response).split("\n"):
                self.log_cb("    " + ln)
        _, payload, err = modbus_rtu.parse_fast_modbus_response(
            response,
            expected_serial=serial,
            log_cb=(self.log_cb if self.verbose_exchange_log else None),
        )
        if err and self.log_cb and not is_data_block:
            self.log_cb(f"  [Modbus] parse 0x46: {err}")
        if (
            not err
            and payload is not None
            and len(inner_pdu) >= 1
            and inner_pdu[0] == 0x03
        ):
            time.sleep(FAST_MODBUS_INTER_READ_GAP_S)
        return payload, err

    def write_single_register(
        self, slave: int, reg_addr: int, value: int
    ) -> Optional[str]:
        req = modbus_rtu.build_write_single_register(slave, reg_addr, value)
        _, _, err = self._exchange(req)
        return err

    def read_holding_registers(
        self, slave: int, start_addr: int, count: int
    ) -> Tuple[Optional[bytes], Optional[str]]:
        req = modbus_rtu.build_read_holding_registers(slave, start_addr, count)
        _, payload, err = self._exchange(req)
        if err:
            return None, err
        return payload, None

    def write_multiple_registers(
        self,
        slave: int,
        start_addr: int,
        values: List[int],
        log_timeout: bool = True,
    ) -> Optional[str]:
        req = modbus_rtu.build_write_multiple_registers(
            slave, start_addr, values
        )
        _, _, err = self._exchange(req, log_timeout=log_timeout)
        return err

    def enter_bootloader(self, slave: int) -> Optional[str]:
        """Перевод в бутлоадер (запись 1 в reg 129 → сброс устройства)."""
        return self.write_single_register(slave, REG_ENTER_BOOTLOADER, 1)

    def enter_bootloader_wb(self, slave: int) -> Optional[str]:
        """Перевод в загрузчик WB: запись 1 в reg 129 через 0x10 (Write Multiple); часть устройств WB не принимают 0x06."""
        return self.write_multiple_registers(slave, REG_ENTER_BOOTLOADER, [1])

    def jump_to_app(self, slave: int) -> Optional[str]:
        """Переход в приложение (запись в reg 1004). Бутлоадер принимает только 0x10, не 0x06.
        После ответа устройство выполняет сброс и запускает приложение (таймаут обмена обычный)."""
        return self.write_multiple_registers(slave, REG_JUMP_APP, [1])

    def write_serial_number(self, slave: int, serial: int) -> Optional[str]:
        """Записать серийный номер (32 бит) в Flash по адресу 0x080000C4 (reg 1005, 2 reg: lo, hi)."""
        lo = serial & 0xFFFF
        hi = (serial >> 16) & 0xFFFF
        return self.write_multiple_registers(slave, REG_PROGRAM_SERIAL, [lo, hi])

    # --- Обмен с бутлоадером по серийному (0xFD 0x46 0x08/0x09, алгоритм Wiren Board) ---

    def read_holding_registers_by_serial(
        self, serial: int, start_addr: int, count: int
    ) -> Tuple[Optional[bytes], Optional[str]]:
        """Чтение регистров бутлоадера по серийному номеру (0x46 0x08)."""
        body = modbus_rtu.build_read_holding_registers_body(start_addr, count)
        payload, err = self._exchange_by_serial(serial, body)
        return payload, err

    def write_multiple_registers_by_serial(
        self,
        serial: int,
        start_addr: int,
        values: List[int],
        log_timeout: bool = True,
    ) -> Optional[str]:
        """Запись регистров бутлоадера по серийному номеру (0x46 0x08)."""
        body = modbus_rtu.build_write_multiple_registers_body(start_addr, values)
        _, err = self._exchange_by_serial(
            serial, body, log_timeout=log_timeout
        )
        return err

    def read_bootloader_info_by_serial(
        self, serial: int
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Чтение reg 290 (сигнатура) и 330 (версия бутлоадера) по серийному."""
        payload, err = self.read_holding_registers_by_serial(
            serial, REG_SIGNATURE, REG_SIGNATURE_COUNT
        )
        if err:
            return None, None, err
        sig = decode_signature_from_holding_290_payload(payload)
        payload2, err2 = self.read_holding_registers_by_serial(
            serial, REG_BOOTLOADER_VERSION, REG_BOOTLOADER_VERSION_COUNT
        )
        if err2:
            return sig or None, None, err2
        ver = decode_bootloader_version_registers_8(payload2 or b"") or "—"
        return sig or None, ver, None

    def send_info_block_by_serial(
        self, serial: int, signature: str, firmware_size: int, image_payload: bytes
    ) -> Optional[str]:
        """Отправка info-блока (0x1000) по серийному: signature + size + CRC2 + crc32(payload)."""
        info = build_info_block(signature, firmware_size, image_payload)
        regs = info_block_to_registers(info)
        return self.write_multiple_registers_by_serial(
            serial, INFO_BLOCK_REG, regs
        )

    def send_info_block_bytes_by_serial(
        self, serial: int, info_32_bytes: bytes
    ) -> Optional[str]:
        """Отправка info-блока (первые 32 B файла) по серийному — 16 рег BE, как для .fw."""
        if len(info_32_bytes) < INFO_BLOCK_BYTES:
            info_32_bytes = info_32_bytes + b"\x00" * (INFO_BLOCK_BYTES - len(info_32_bytes))
        regs = info_block_to_registers(info_32_bytes)
        return self.write_multiple_registers_by_serial(
            serial, INFO_BLOCK_REG, regs
        )

    def send_data_block_by_serial(
        self,
        serial: int,
        block_index: int,
        block_data: bytes,
        log_timeout: bool = True,
        app_from_fw: bool = True,
    ) -> Optional[str]:
        """Отправка одного блока данных (0x2000) по серийному номеру. app_from_fw: True для .fw, False для .bin."""
        if len(block_data) < DATA_BLOCK_BYTES:
            block_data = block_data + b"\xff" * (
                DATA_BLOCK_BYTES - len(block_data)
            )
        regs = (
            payload_block_to_registers_app_le(block_data)
            if app_from_fw
            else payload_block_to_registers(block_data)
        )
        return self.write_multiple_registers_by_serial(
            serial, DATA_BLOCK_REG, regs, log_timeout=log_timeout
        )

    def write_firmware_type_bootloader_by_serial(self, serial: int) -> Optional[str]:
        """Режим «обновление бутлоадера»: запись 0x424C в 0x1001 по серийному."""
        return self.write_multiple_registers_by_serial(
            serial, REG_FIRMWARE_TYPE, [FIRMWARE_TYPE_BOOTLOADER]
        )

    def send_info_block_bootloader_by_serial(
        self,
        serial: int,
        signature: str,
        image: bytes,
        size: int = BL_IMAGE_TOTAL_BYTES,
    ) -> Optional[str]:
        """Info-блок для бутлоадера по серийному."""
        info = build_info_block(signature, size, image)
        regs = info_block_to_registers(info)
        return self.write_multiple_registers_by_serial(
            serial, INFO_BLOCK_REG, regs
        )

    def send_data_block_bootloader_by_serial(
        self,
        serial: int,
        block_data: bytes,
        log_timeout: bool = True,
        app_from_fw: bool = False,
    ) -> Optional[str]:
        """Отправка одного блока образа бутлоадера по серийному.
        app_from_fw=True: payload из .fw (байт-своп) → app_le-кодирование → staging = raw binary."""
        size = len(block_data)
        if size not in (DATA_BLOCK_BYTES_BOOTLOADER, DATA_BLOCK_LAST_BYTES_BOOTLOADER):
            if size < DATA_BLOCK_LAST_BYTES_BOOTLOADER:
                block_data = block_data + b"\xff" * (
                    DATA_BLOCK_LAST_BYTES_BOOTLOADER - size
                )
                size = DATA_BLOCK_LAST_BYTES_BOOTLOADER
            else:
                block_data = block_data + b"\xff" * (
                    DATA_BLOCK_BYTES_BOOTLOADER - size
                )
                size = DATA_BLOCK_BYTES_BOOTLOADER
        regs = (
            payload_bytes_to_registers_le(block_data)
            if app_from_fw
            else [(block_data[i] << 8) | block_data[i + 1] for i in range(0, size, 2)]
        )
        return self.write_multiple_registers_by_serial(
            serial, DATA_BLOCK_REG, regs, log_timeout=log_timeout
        )

    def send_commit_bootloader_by_serial(self, serial: int) -> Optional[str]:
        """Запись 1 в регистр 0x1006 по серийному (commit бутлоадера)."""
        return self.write_multiple_registers_by_serial(
            serial, REG_COMMIT_BOOTLOADER, [1]
        )

    def write_serial_number_by_serial(
        self, target_serial: int, new_serial_value: int
    ) -> Optional[str]:
        """Запись серийного номера (reg 1005) по серийному целевого устройства."""
        lo = new_serial_value & 0xFFFF
        hi = (new_serial_value >> 16) & 0xFFFF
        return self.write_multiple_registers_by_serial(
            target_serial, REG_PROGRAM_SERIAL, [lo, hi]
        )

    def jump_to_app_by_serial(self, serial: int) -> Optional[str]:
        """Переход в приложение (reg 1004) по серийному номеру."""
        return self.write_multiple_registers_by_serial(
            serial, REG_JUMP_APP, [1]
        )

    def jump_to_app_with_console_diagnostics(
        self, slave: int, image: bytes
    ) -> Optional[str]:
        """
        Переход в приложение (reg 1004) с выводом диагностики в stderr (без hex посылок).
        """
        from flasher_windows.firmware import check_app_vector_table

        def _log(s: str) -> None:
            try:
                sys.stderr.write(s + "\n")
                sys.stderr.flush()
            except (AttributeError, OSError):
                pass

        _log("")
        _log("=== Диагностика перехода в приложение (конец прошивки) ===")
        _log(f"  Размер образа: {len(image)} байт")
        _log(f"  Адрес устройства: {slave}")

        if len(image) >= 8:
            sp, reset_handler = struct.unpack_from("<II", image, 0)
            _log(f"  Векторная таблица (0x08000800): SP=0x{sp:08X}, Reset_Handler=0x{reset_handler:08X}")
            ok, err = check_app_vector_table(image)
            if ok:
                _log("  Проверка: OK (образ для 0x08000800)")
            else:
                _log(f"  ВНИМАНИЕ: {err}")
                _log(
                    "  Если образ собран под Debug/Release (0x08000000), после прошивки "
                    "приложение не запустится! Пересоберите AppBoot (STM32F030CCXX_FLASH_boot.ld)."
                )

        _log(f"  Команда: запись 1 в регистр {REG_JUMP_APP} (jump to app, Modbus 0x10)")

        req = modbus_rtu.build_write_multiple_registers(
            slave, REG_JUMP_APP, [1]
        )
        response = self.send_receive(req)

        if response is None:
            _log("  RX: таймаут (устройство не ответило)")
            _log(
                "  Возможные причины: устройство уже перешло в приложение и "
                "перезагрузилось; разрыв линии; устройство зависло."
            )
            _log("=" * 56)
            return "Таймаут ответа на jump"

        _, _, err = modbus_rtu.parse_response(
            response, expected_slave=slave, log_cb=None
        )
        if err:
            _log(f"  Разбор ответа: {err}")
        else:
            _log("  Разбор ответа: OK")

        _log(
            "  Примечание: после jump устройство может не ответить (оно уже "
            "перешло в приложение). Таймаут не всегда означает ошибку."
        )
        _log(
            "  Если приложение не запустилось: подключите USART1 TX (PB6) к USB-адаптеру "
            "115200 8N1 — бутлоадер выводит диагностику jump (app_valid, SP, reset_handler)."
        )
        _log("=" * 56)
        return err

    def jump_to_app_with_console_diagnostics_by_serial(
        self, serial: int, image: bytes
    ) -> Optional[str]:
        """Переход в приложение (reg 1004) по серийному с диагностикой в stderr."""
        from flasher_windows.firmware import check_app_vector_table

        def _log(s: str) -> None:
            try:
                sys.stderr.write(s + "\n")
                sys.stderr.flush()
            except (AttributeError, OSError):
                pass

        _log("")
        _log("=== Диагностика перехода в приложение (0x46 по серийному) ===")
        _log(f"  Размер образа: {len(image)} байт")
        _log(f"  Серийный: 0x{serial:08X}")

        if len(image) >= 8:
            sp, reset_handler = struct.unpack_from("<II", image, 0)
            _log(f"  Векторная таблица: SP=0x{sp:08X}, Reset_Handler=0x{reset_handler:08X}")
            ok, err = check_app_vector_table(image)
            if ok:
                _log("  Проверка: OK")
            else:
                _log(f"  ВНИМАНИЕ: {err}")

        _log(f"  Команда: 0xFD 0x46 0x08 serial=0x{serial:08X} reg {REG_JUMP_APP}=1")
        body = modbus_rtu.build_write_multiple_registers_body(REG_JUMP_APP, [1])
        req = modbus_rtu.build_fast_modbus_request(serial & 0xFFFFFFFF, body)
        response = self.send_receive(req)

        if response is None:
            _log("  RX: таймаут")
            _log("=" * 56)
            return "Таймаут ответа на jump"

        _, _, err = modbus_rtu.parse_fast_modbus_response(
            response, expected_serial=serial, log_cb=None
        )
        if err:
            _log(f"  Разбор ответа: {err}")
        else:
            _log("  Разбор ответа: OK")
        _log("=" * 56)
        return err

    @staticmethod
    def parse_app_version_from_holding_payload(payload: Optional[bytes]) -> str:
        """Рег. 320–323: MAJOR, MINOR, PATCH, SUFFIX (BE), как в scanner._parse_version_4."""
        if payload is None or len(payload) < 8:
            return "—"
        try:
            maj = (payload[0] << 8) | payload[1]
            mi = (payload[2] << 8) | payload[3]
            patch = (payload[4] << 8) | payload[5]
            suf = (payload[6] << 8) | payload[7]
            if suf & 0x8000:
                suf -= 0x10000
            return f"{maj}.{mi}.{patch}.{suf}"
        except Exception:
            return "—"

    @staticmethod
    def app_version_indicates_bootloader(app_ver: str) -> bool:
        """Режим загрузчика: версия приложения 0.0.0.0 (или нет данных)."""
        return app_ver in ("—", "", "0.0.0.0")

    def discover_bootloader_address(self) -> Tuple[Optional[int], Optional[str]]:
        """DEPRECATED: legacy broadcast-discovery (addr 255) отключён в прошивке.

        Для определения адреса используйте discover_bootloader_address_scan()
        (последовательный опрос адресов) или быстрый WB extended scan (0xFD 0x46).
        """
        return None, "Broadcast-discovery deprecated: используйте адресный scan или WB 0xFD 0x46"

    def discover_bootloader_address_scan(
        self, first: int = 1, last: int = 20
    ) -> Tuple[Optional[int], Optional[str]]:
        """Если broadcast не сработал — опрос адресов first..last по одному (read reg 290). Возвращает (адрес, ошибка)."""
        for addr in range(first, min(last, 248)):
            payload, err = self.read_holding_registers(addr, REG_SIGNATURE, 1)
            if err is None and payload:
                return addr, None
        return None, "По адресам 1..20 ответа нет"

    def read_bootloader_info(
        self, slave: int
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Чтение reg 290 (сигнатура) и 330 (версия бутлоадера). (sig, ver, error)."""
        payload, err = self.read_holding_registers(
            slave, REG_SIGNATURE, REG_SIGNATURE_COUNT
        )
        if err:
            return None, None, err
        sig = decode_signature_from_holding_290_payload(payload)
        payload2, err2 = self.read_holding_registers(
            slave, REG_BOOTLOADER_VERSION, REG_BOOTLOADER_VERSION_COUNT
        )
        if err2:
            return sig or None, None, err2
        ver = decode_bootloader_version_registers_8(payload2 or b"") or "—"
        return sig or None, ver, None

    def send_select_by_serial(self, slave: int, serial: int) -> Optional[str]:
        """
        Выбор устройства по серийному номеру (рег 0xF0, 2 reg: lo, hi).
        Используется при нескольких устройствах в бутлоадере с одним адресом — отвечает только устройство с этим serial.
        При успехе возвращает None.
        """
        lo = serial & 0xFFFF
        hi = (serial >> 16) & 0xFFFF
        return self.write_multiple_registers(
            slave, REG_SELECT_SERIAL, [lo, hi], log_timeout=True
        )

    def send_info_block(
        self, slave: int, signature: str, firmware_size: int, image_payload: bytes
    ) -> Optional[str]:
        """Отправка info-блока (0x1000): сигнатура + размер + CRC2 + crc32(payload)."""
        info = build_info_block(signature, firmware_size, image_payload)
        regs = info_block_to_registers(info)
        return self.write_multiple_registers(slave, INFO_BLOCK_REG, regs)

    def send_data_block(
        self,
        slave: int,
        block_index: int,
        block_data: bytes,
        log_timeout: bool = True,
        app_from_fw: bool = True,
    ) -> Optional[str]:
        """Отправка одного блока данных (0x2000). app_from_fw=True для .fw (слова BE → LE в Flash), False для .bin (сырой LE)."""
        if len(block_data) < DATA_BLOCK_BYTES:
            block_data = block_data + b"\xff" * (
                DATA_BLOCK_BYTES - len(block_data)
            )
        regs = (
            payload_block_to_registers_app_le(block_data)
            if app_from_fw
            else payload_block_to_registers(block_data)
        )
        return self.write_multiple_registers(
            slave, DATA_BLOCK_REG, regs, log_timeout=log_timeout
        )

    def send_info_block_wb(self, slave: int, info_32_bytes: bytes) -> Optional[str]:
        """Wiren Board: отправка info-блока в 0x1000. Как в wb-mcu-fw-updater — первые 32 байта как 16 регистров Big-Endian (байт 0 = старший байт слова)."""
        if len(info_32_bytes) < INFO_BLOCK_BYTES:
            info_32_bytes = info_32_bytes + b"\x00" * (INFO_BLOCK_BYTES - len(info_32_bytes))
        regs = info_block_to_registers(info_32_bytes)
        return self.write_multiple_registers(slave, INFO_BLOCK_REG, regs)

    def read_wb_free_space(self, slave: int) -> Tuple[Optional[int], Optional[str]]:
        """Wiren Board: чтение рег. 1003 (свободное место FlashFS в байтах). Возвращает (bytes_free, err)."""
        payload, err = self.read_holding_registers(slave, REG_WB_FREE_SPACE, 1)
        if err:
            return None, err
        # Ответ 0x03: payload = данные регистров (2 байта на регистр, BE)
        if not payload or len(payload) < 2:
            return None, "Нет данных"
        free = (payload[0] << 8) | payload[1]
        return free, None

    def send_data_block_wb(
        self,
        slave: int,
        block_data: bytes,
        log_timeout: bool = True,
    ) -> Optional[str]:
        """Wiren Board: один блок 136 B (68 рег) в 0x2000, Big-Endian (как в wb-mcu-fw-updater)."""
        regs = payload_block_to_registers_wb(block_data)
        return self.write_multiple_registers(
            slave, DATA_BLOCK_REG, regs, log_timeout=log_timeout
        )

    def write_firmware_type_bootloader(self, slave: int) -> Optional[str]:
        """Режим «обновление бутлоадера»: запись 0x424C в регистр 0x1001."""
        return self.write_multiple_registers(
            slave, REG_FIRMWARE_TYPE, [FIRMWARE_TYPE_BOOTLOADER]
        )

    def send_info_block_bootloader(
        self,
        slave: int,
        signature: str,
        image: bytes,
        size: int = BL_IMAGE_TOTAL_BYTES,
    ) -> Optional[str]:
        """Info-блок для бутлоадера: сигнатура 12 B + размер 34 КБ (4 B LE)."""
        info = build_info_block(signature, size, image)
        regs = info_block_to_registers(info)
        return self.write_multiple_registers(slave, INFO_BLOCK_REG, regs)

    def send_data_block_bootloader(
        self,
        slave: int,
        block_data: bytes,
        log_timeout: bool = True,
        app_from_fw: bool = False,
    ) -> Optional[str]:
        """Отправка одного блока для образа бутлоадера (0x2000): 244 B (122 reg) или 168 B (84 reg) последний.
        app_from_fw=True: payload из .fw (байт-своп) → app_le-кодирование → в staging попадает raw binary."""
        size = len(block_data)
        if size not in (DATA_BLOCK_BYTES_BOOTLOADER, DATA_BLOCK_LAST_BYTES_BOOTLOADER):
            if size < DATA_BLOCK_LAST_BYTES_BOOTLOADER:
                block_data = block_data + b"\xff" * (DATA_BLOCK_LAST_BYTES_BOOTLOADER - size)
                size = DATA_BLOCK_LAST_BYTES_BOOTLOADER
            else:
                block_data = block_data + b"\xff" * (DATA_BLOCK_BYTES_BOOTLOADER - size)
                size = DATA_BLOCK_BYTES_BOOTLOADER
        regs = (
            payload_bytes_to_registers_le(block_data)
            if app_from_fw
            else [(block_data[i] << 8) | block_data[i + 1] for i in range(0, size, 2)]
        )
        return self.write_multiple_registers(
            slave, DATA_BLOCK_REG, regs, log_timeout=log_timeout
        )

    def send_commit_bootloader(self, slave: int) -> Optional[str]:
        """Запись 1 в регистр 0x1006: commit staging → 0x08000000/0x08038000, сброс устройства."""
        return self.write_multiple_registers(slave, REG_COMMIT_BOOTLOADER, [1])


def verify_app_running_after_jump_on_app_link(
    flasher: FlasherProtocol,
    slave: int,
    attempts: int = 3,
    delay_s: float = 0.55,
    initial_delay_s: float = 2.5,
    log_cb: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """
    Проверка запуска приложения после jump по обычному Modbus (рег. 320–323).

    Было: до 22 попыток read_holding_registers_by_serial (0xFD 0x46 0x08) на 115200 — после jump
    приложение поднимает UART по EEPROM (часто 19200), быстрый Modbus на шине не использует → ложные таймауты.

    Стало: переключить порт на скорость/адрес из таблицы сканирования и читать 320–323 как обычный Modbus;
    по умолчанию до 3 попыток.
    """
    if slave < 1 or slave > 247:
        return "Некорректный Modbus-адрес %d для проверки версии приложения." % slave
    time.sleep(initial_delay_s)
    last_err: Optional[str] = None
    last_ver = "—"
    for attempt in range(attempts):
        if attempt:
            time.sleep(delay_s)
        pl, err = flasher.read_holding_registers(
            slave, REG_APP_VERSION_MAJOR, REG_APP_VERSION_COUNT
        )
        if err:
            last_err = err
            if log_cb:
                log_cb(
                    "Проверка запуска приложения (320–323, обычный Modbus), попытка %d/%d: %s"
                    % (attempt + 1, attempts, err)
                )
            continue
        app_ver = FlasherProtocol.parse_app_version_from_holding_payload(pl)
        last_ver = app_ver
        if not FlasherProtocol.app_version_indicates_bootloader(app_ver):
            if log_cb:
                log_cb("Запуск приложения подтверждён: версия %s." % app_ver)
            return None
        if log_cb:
            log_cb(
                "Проверка запуска приложения, попытка %d/%d: пока загрузчик (версия пр. %s)."
                % (attempt + 1, attempts, app_ver)
            )
    parts = [
        "После jump устройство не подтвердило запуск приложения по обычному Modbus (версия пр. %s)."
        % last_ver
    ]
    if last_err:
        parts.append("Последняя ошибка обмена: %s." % last_err)
    parts.append("Проверьте адрес и скорость линии из сканирования, линию RS-485, питание.")
    return " ".join(parts)


def verify_app_running_after_jump_by_serial(
    flasher: FlasherProtocol,
    serial: int,
    attempts: int = 3,
    delay_s: float = 0.55,
    initial_delay_s: float = 2.5,
    log_cb: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """
    Устаревший путь: опрос 320–323 по 0xFD 0x46 0x08 после jump.
    Работает только пока устройство в загрузчике; в приложении обычно нужен
    verify_app_running_after_jump_on_app_link().
    """
    serial = serial & 0xFFFFFFFF
    time.sleep(initial_delay_s)
    last_err: Optional[str] = None
    last_ver = "—"
    for attempt in range(attempts):
        if attempt:
            time.sleep(delay_s)
        time.sleep(FAST_MODBUS_INTER_READ_GAP_S)
        pl, err = flasher.read_holding_registers_by_serial(
            serial, REG_APP_VERSION_MAJOR, REG_APP_VERSION_COUNT
        )
        if err:
            last_err = err
            if log_cb:
                log_cb(
                    "Проверка (быстрый Modbus 0x46), попытка %d/%d: %s"
                    % (attempt + 1, attempts, err)
                )
            continue
        app_ver = FlasherProtocol.parse_app_version_from_holding_payload(pl)
        last_ver = app_ver
        if not FlasherProtocol.app_version_indicates_bootloader(app_ver):
            if log_cb:
                log_cb("Запуск приложения подтверждён: версия %s." % app_ver)
            return None
        if log_cb:
            log_cb(
                "Проверка (0x46), попытка %d/%d: пока загрузчик (версия пр. %s)."
                % (attempt + 1, attempts, app_ver)
            )
    parts = [
        "По быстрому Modbus (0x46) не подтверждён запуск приложения (версия пр. %s)."
        % last_ver
    ]
    if last_err:
        parts.append("Последняя ошибка обмена: %s." % last_err)
    parts.append(
        "После jump используйте проверку по обычному Modbus (скорость/адрес из сканирования)."
    )
    return " ".join(parts)


def run_flash_sequence_by_address(
    flasher: FlasherProtocol,
    slave: int,
    image: bytes,
    signature: str,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    retries_per_block: int = DEFAULT_RETRIES_PER_BLOCK,
    cancel_gate: Optional[FlashCancelGate] = None,
) -> Optional[str]:
    """
    Прошивка приложения по Modbus-адресу. Как у WB: если образ с info-блоком (первые 32 B файла .fw) — отправляем их как 16 рег BE; иначе — сборка из signature + size (для .bin).
    """
    gate = _resolve_flash_cancel_gate(cancel_cb, cancel_gate)
    # Полный файл .fw: первые 32 байта = info, payload в файле — слова BE (как в make_fw.py)
    if _image_has_embedded_fw_header(image):
        size_from_file = struct.unpack_from("<I", image, 12)[0]
        # В .fw данные с offset 32 хранятся как 16-bit BE; первые 8 байт payload = таблица векторов в BE-словах
        vt_be = image[INFO_BLOCK_BYTES : INFO_BLOCK_BYTES + 8]
        vt_le = _fw_payload_first_8_bytes_to_le(vt_be)
        if vt_le:
            err_vt = check_app_vector_table(vt_le)
            if err_vt:
                return err_vt
        size = size_from_file
        if flasher.log_cb:
            flasher.log_cb(f"Отправка info-блока (первые 32 B файла .fw, 16 рег BE) на адрес {slave}.")
        err = None
        for info_try in range(INFO_MAX_ATTEMPTS):
            if gate.should_abort():
                return "Отменено пользователем"
            err = flasher.send_info_block_wb(slave, image[:INFO_BLOCK_BYTES])
            if err is None:
                break
            if info_try < INFO_MAX_ATTEMPTS - 1:
                if flasher.log_cb:
                    flasher.log_cb(
                        "Повтор info-блока (%d/%d): %s"
                        % (info_try + 2, INFO_MAX_ATTEMPTS, err)
                    )
                if gate.should_abort():
                    return "Отменено пользователем"
                time.sleep(INFO_RETRY_SLEEP_S)
        if err:
            return f"Ошибка отправки info-блока: {err}"
        gate.mark_irreversible()
        ready_err = wait_bootloader_ready_for_data_by_address(flasher, slave)
        if ready_err:
            return ready_err
        gate.sleep(POST_ERASE_BEFORE_FIRST_DATA_S)
        if flasher.log_cb and POST_ERASE_BEFORE_FIRST_DATA_S > 0:
            flasher.log_cb(
                "Пауза %.2f с перед первым блоком данных (0x2000)."
                % POST_ERASE_BEFORE_FIRST_DATA_S
            )
        total_blocks = (size + DATA_BLOCK_BYTES - 1) // DATA_BLOCK_BYTES
        t_start = time.perf_counter()
        if progress_cb:
            progress_cb(0, total_blocks)
        blocks_sent = 0
        for idx in range(total_blocks):
            if gate.should_abort():
                return "Отменено пользователем"
            start = INFO_BLOCK_BYTES + idx * DATA_BLOCK_BYTES
            block = image[start : start + DATA_BLOCK_BYTES]
            if len(block) < DATA_BLOCK_BYTES:
                block = block + b"\xff" * (DATA_BLOCK_BYTES - len(block))
            block_ok = False
            last_err: Optional[str] = None
            for attempt in range(APP_MAX_ERROR_COUNT):
                log_timeout = attempt == APP_MAX_ERROR_COUNT - 1
                err = flasher.send_data_block(
                    slave, idx, block, log_timeout=log_timeout, app_from_fw=True
                )
                if err is None:
                    block_ok = True
                    break
                last_err = err
                if attempt < APP_MAX_ERROR_COUNT - 1:
                    if flasher.log_cb:
                        flasher.log_cb(
                            f"Повтор блока {idx + 1}/{total_blocks} (попытка {attempt + 2}/{APP_MAX_ERROR_COUNT})..."
                        )
                    _sleep_before_retry_flash_data_block_slave(flasher, slave, err, idx)
            if not block_ok:
                return f"Ошибка блока {idx + 1}/{total_blocks}: {last_err} (исчерпано {APP_MAX_ERROR_COUNT} попыток)."
            blocks_sent += 1
            if progress_cb:
                progress_cb(blocks_sent, total_blocks)
            if flasher.log_cb and (
                blocks_sent % 50 == 0 or blocks_sent == total_blocks
            ):
                elapsed = max(0.001, time.perf_counter() - t_start)
                bps = blocks_sent / elapsed
                eta = (total_blocks - blocks_sent) / max(0.001, bps)
                flasher.log_cb(
                    f"Скорость: {bps:.1f} бл/с, ETA: {eta:.1f} с ({blocks_sent}/{total_blocks})"
                )
            time.sleep(BLOCK_DELAY_FIRST_BLOCK_S if idx == 0 else BLOCK_DELAY_AFTER_RESPONSE_S)
        return None

    # Образ без info-блока (.bin): сигнатура для info берётся с платы (рег. 290), как в эталоне.
    err_vt = check_app_vector_table(image)
    if err_vt:
        return err_vt
    size = len(image)
    if size <= 0 or size > MAX_FIRMWARE_SIZE_BYTES:
        return (
            f"Недопустимый размер образа: {size} байт "
            f"(допустимо 1..{MAX_FIRMWARE_SIZE_BYTES})"
        )
    board_sig, read_err = _read_board_signature_bootloader_warmup(
        flasher, slave=slave, serial=None
    )
    if read_err:
        return read_err
    reject = _validate_bootloader_board_signature_for_bin_flash(board_sig)
    if reject:
        return reject
    info_sig = ((board_sig or "").strip()[:12] or DEFAULT_SIGNATURE)
    if flasher.log_cb:
        flasher.log_cb(
            "Плата (рег. 290): «%s». В info 0x1000 — та же сигнатура (до 12 симв.), размер %d B, CRC2+crc32(payload)."
            % (board_sig or "—", size)
        )
    if gate.should_abort():
        return "Отменено пользователем"
    if gate.sleep(max(FAST_MODBUS_INTER_READ_GAP_S, 0.25)):
        return "Отменено пользователем"
    err = None
    for info_try in range(INFO_MAX_ATTEMPTS):
        if gate.should_abort():
            return "Отменено пользователем"
        err = flasher.send_info_block(slave, info_sig, size, image)
        if err is None:
            break
        if info_try < INFO_MAX_ATTEMPTS - 1:
            if flasher.log_cb:
                flasher.log_cb(
                    "Повтор info-блока (%d/%d): %s"
                    % (info_try + 2, INFO_MAX_ATTEMPTS, err)
                )
            if gate.should_abort():
                return "Отменено пользователем"
            time.sleep(INFO_RETRY_SLEEP_S)
    if err:
        msg = f"Ошибка отправки info-блока: {err}"
        if "код 4" in err or "код 04" in err.lower() or "Исключение Modbus" in (err or ""):
            msg += (
                " Устройство отклонило info-блок (исключение 04). "
                "Проверьте размер образа AppBoot, CRC в info и совпадение первых 12 байт с EEPROM (рег. 290)."
            )
            payload_rej, err_f1 = flasher.read_holding_registers(
                slave, REG_LAST_INFO_REJECT, 2
            )
            if (
                not err_f1
                and payload_rej
                and len(payload_rej) >= 4
            ):
                code = (payload_rej[0] << 8) | payload_rej[1]
                idx = (payload_rej[2] << 8) | payload_rej[3]
                reason_str = _last_info_reject_reason_text(code, idx) or f"код {code}"
                msg += f" Причина по устройству (рег. 0xF1): {reason_str}"
                if code == 1:
                    msg += (
                        f". Отправлено {size} байт, макс. допустимо устройством: "
                        f"{BOOTLOADER_MAX_APP_BYTES} байт. Используйте образ AppBoot."
                    )
        return msg

    gate.mark_irreversible()
    ready_err = wait_bootloader_ready_for_data_by_address(flasher, slave)
    if ready_err:
        return ready_err
    gate.sleep(POST_ERASE_BEFORE_FIRST_DATA_S)
    if flasher.log_cb and POST_ERASE_BEFORE_FIRST_DATA_S > 0:
        flasher.log_cb(
            "Пауза %.2f с перед первым блоком данных (0x2000)." % POST_ERASE_BEFORE_FIRST_DATA_S
        )

    total_blocks = (size + DATA_BLOCK_BYTES - 1) // DATA_BLOCK_BYTES
    t_start = time.perf_counter()
    if progress_cb:
        progress_cb(0, total_blocks)
    blocks_sent = 0
    for idx in range(total_blocks):
        if gate.should_abort():
            return "Отменено пользователем"
        start = idx * DATA_BLOCK_BYTES
        block = image[start : start + DATA_BLOCK_BYTES]
        if len(block) < DATA_BLOCK_BYTES:
            block = block + b"\xff" * (DATA_BLOCK_BYTES - len(block))
        block_ok = False
        last_err_app: Optional[str] = None
        for attempt in range(APP_MAX_ERROR_COUNT):
            log_timeout = attempt == APP_MAX_ERROR_COUNT - 1
            err = flasher.send_data_block(
                slave, idx, block, log_timeout=log_timeout, app_from_fw=False
            )
            if err is None:
                block_ok = True
                break
            last_err_app = err
            if attempt < APP_MAX_ERROR_COUNT - 1:
                if flasher.log_cb:
                    flasher.log_cb(
                        f"Повтор блока {idx + 1}/{total_blocks} (попытка {attempt + 2}/{APP_MAX_ERROR_COUNT})..."
                    )
                _sleep_before_retry_flash_data_block_slave(flasher, slave, err, idx)
        if not block_ok:
            return f"Ошибка блока {idx + 1}/{total_blocks}: {last_err_app} (исчерпано {APP_MAX_ERROR_COUNT} попыток)."
        blocks_sent += 1
        if progress_cb:
            progress_cb(blocks_sent, total_blocks)
        if flasher.log_cb and (
            blocks_sent % 50 == 0 or blocks_sent == total_blocks
        ):
            elapsed = max(0.001, time.perf_counter() - t_start)
            bps = blocks_sent / elapsed
            eta = (total_blocks - blocks_sent) / max(0.001, bps)
            flasher.log_cb(
                f"Скорость: {bps:.1f} бл/с, ETA: {eta:.1f} с ({blocks_sent}/{total_blocks})"
            )
        time.sleep(BLOCK_DELAY_FIRST_BLOCK_S if idx == 0 else BLOCK_DELAY_AFTER_RESPONSE_S)

    return None


def run_flash_sequence(
    flasher: FlasherProtocol,
    serial: int,
    image: bytes,
    signature: str,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    retries_per_block: int = DEFAULT_RETRIES_PER_BLOCK,
    target_serial: Optional[int] = None,
    cancel_gate: Optional[FlashCancelGate] = None,
) -> Optional[str]:
    """
    Полная последовательность прошивки по серийному номеру (0xFD 0x46 0x08). Как у WB: при образе с info-блоком (.fw) — первые 32 B как 16 рег BE; иначе — сборка из signature + size.
    """
    if not serial or serial == 0xFFFFFFFF:
        return "Для прошивки по 0x46 нужен серийный номер устройства (выполните сканирование и выберите по серийному №)."
    gate = _resolve_flash_cancel_gate(cancel_cb, cancel_gate)

    # Полный файл .fw: первые 32 байта = info, payload в файле — слова BE
    if _image_has_embedded_fw_header(image):
        size_from_file = struct.unpack_from("<I", image, 12)[0]
        vt_be = image[INFO_BLOCK_BYTES : INFO_BLOCK_BYTES + 8]
        vt_le = _fw_payload_first_8_bytes_to_le(vt_be)
        if vt_le:
            err_vt = check_app_vector_table(vt_le)
            if err_vt:
                return err_vt
        size = size_from_file
        payload_start = INFO_BLOCK_BYTES
        if flasher.log_cb:
            flasher.log_cb(f"Отправка info-блока (первые 32 B файла .fw, 16 рег BE) по серийному 0x{serial:08X}.")
        err = None
        for info_try in range(INFO_MAX_ATTEMPTS):
            if gate.should_abort():
                return "Отменено пользователем"
            err = flasher.send_info_block_bytes_by_serial(serial, image[:INFO_BLOCK_BYTES])
            if err is None:
                break
            if info_try < INFO_MAX_ATTEMPTS - 1:
                if flasher.log_cb:
                    flasher.log_cb(
                        "Повтор info-блока (%d/%d): %s"
                        % (info_try + 2, INFO_MAX_ATTEMPTS, err)
                    )
                if gate.should_abort():
                    return "Отменено пользователем"
                time.sleep(INFO_RETRY_SLEEP_S)
        if not err:
            gate.mark_irreversible()
            ready_err = wait_bootloader_ready_for_data_by_serial(flasher, serial)
            if ready_err:
                return ready_err
            gate.sleep(POST_ERASE_BEFORE_FIRST_DATA_S)
            if flasher.log_cb and POST_ERASE_BEFORE_FIRST_DATA_S > 0:
                flasher.log_cb(
                    "Пауза %.2f с перед первым блоком данных (0x2000)." % POST_ERASE_BEFORE_FIRST_DATA_S
                )
            total_blocks = (size + DATA_BLOCK_BYTES - 1) // DATA_BLOCK_BYTES
            t_start = time.perf_counter()
            if progress_cb:
                progress_cb(0, total_blocks)
            blocks_sent = 0
            for idx in range(total_blocks):
                if gate.should_abort():
                    return "Отменено пользователем"
                start = payload_start + idx * DATA_BLOCK_BYTES
                block = image[start : start + DATA_BLOCK_BYTES]
                if len(block) < DATA_BLOCK_BYTES:
                    block = block + b"\xff" * (DATA_BLOCK_BYTES - len(block))
                block_ok = False
                last_err_s = None
                for attempt in range(APP_MAX_ERROR_COUNT):
                    log_t = attempt == APP_MAX_ERROR_COUNT - 1
                    err = flasher.send_data_block_by_serial(
                        serial, idx, block, log_timeout=log_t, app_from_fw=True
                    )
                    if err is None:
                        block_ok = True
                        break
                    last_err_s = err
                    if attempt < APP_MAX_ERROR_COUNT - 1:
                        if flasher.log_cb:
                            flasher.log_cb(
                                f"Повтор блока {idx + 1}/{total_blocks} (попытка {attempt + 2}/{APP_MAX_ERROR_COUNT})..."
                            )
                        _sleep_before_retry_flash_data_block(flasher, serial, err, idx)
                if not block_ok:
                    return f"Ошибка блока {idx + 1}/{total_blocks}: {last_err_s} (исчерпано {APP_MAX_ERROR_COUNT} попыток)."
                blocks_sent += 1
                if progress_cb:
                    progress_cb(blocks_sent, total_blocks)
                time.sleep(BLOCK_DELAY_AFTER_RESPONSE_S_FAST if idx else BLOCK_DELAY_FIRST_BLOCK_S_FAST)
            return None
        return err or "Ошибка отправки info-блока"

    size = len(image)
    if size <= 0 or size > MAX_FIRMWARE_SIZE_BYTES:
        return (
            f"Недопустимый размер образа: {size} байт "
            f"(допустимо 1..{MAX_FIRMWARE_SIZE_BYTES})"
        )
    board_sig, read_err = _read_board_signature_bootloader_warmup(
        flasher, slave=None, serial=serial
    )
    if read_err:
        return read_err
    reject = _validate_bootloader_board_signature_for_bin_flash(board_sig)
    if reject:
        return reject
    info_sig = ((board_sig or "").strip()[:12] or DEFAULT_SIGNATURE)
    if flasher.log_cb:
        flasher.log_cb(
            "Плата (рег. 290): «%s». В info по серийному 0x%08X — та же сигнатура в info, размер %d B, CRC2+crc32."
            % (board_sig or "—", serial & 0xFFFFFFFF, size)
        )
    if gate.should_abort():
        return "Отменено пользователем"
    if gate.sleep(max(FAST_MODBUS_INTER_READ_GAP_S, 0.25)):
        return "Отменено пользователем"
    err = None
    for info_try in range(INFO_MAX_ATTEMPTS):
        if gate.should_abort():
            return "Отменено пользователем"
        err = flasher.send_info_block_by_serial(serial, info_sig, size, image)
        if err is None:
            break
        if info_try < INFO_MAX_ATTEMPTS - 1:
            if flasher.log_cb:
                flasher.log_cb(
                    "Повтор info-блока (%d/%d): %s"
                    % (info_try + 2, INFO_MAX_ATTEMPTS, err)
                )
            if gate.should_abort():
                return "Отменено пользователем"
            time.sleep(INFO_RETRY_SLEEP_S)
    if err:
        msg = f"Ошибка отправки info-блока: {err}"
        if "код 4" in err or "код 04" in err.lower():
            msg += (
                " Устройство отклонило блок (исключение 04). "
                "Проверьте размер AppBoot, CRC в info и совпадение сигнатуры в info с EEPROM (рег. 290)."
            )
            payload_rej, _ = flasher.read_holding_registers_by_serial(
                serial, REG_LAST_INFO_REJECT, 2
            )
            if payload_rej and len(payload_rej) >= 4:
                code = (payload_rej[0] << 8) | payload_rej[1]
                idx = (payload_rej[2] << 8) | payload_rej[3]
                reason_str = _last_info_reject_reason_text(code, idx) or f"код {code}"
                msg += f" Причина по устройству (рег. 0xF1): {reason_str}"
                if code == 1:
                    msg += (
                        f". Отправлено {size} байт, макс. допустимо устройством: {BOOTLOADER_MAX_APP_BYTES} байт. "
                        "Используйте образ AppBoot."
                    )
        return msg

    gate.mark_irreversible()
    ready_err = wait_bootloader_ready_for_data_by_serial(flasher, serial)
    if ready_err:
        return ready_err
    gate.sleep(POST_ERASE_BEFORE_FIRST_DATA_S)
    if flasher.log_cb and POST_ERASE_BEFORE_FIRST_DATA_S > 0:
        flasher.log_cb(
            "Пауза %.2f с перед первым блоком данных (0x2000)." % POST_ERASE_BEFORE_FIRST_DATA_S
        )

    total_blocks = (size + DATA_BLOCK_BYTES - 1) // DATA_BLOCK_BYTES
    t_start = time.perf_counter()
    if progress_cb:
        progress_cb(0, total_blocks)
    blocks_sent = 0
    for idx in range(total_blocks):
        if gate.should_abort():
            return "Отменено пользователем"
        start = idx * DATA_BLOCK_BYTES
        block = image[start : start + DATA_BLOCK_BYTES]
        if len(block) < DATA_BLOCK_BYTES:
            block = block + b"\xff" * (DATA_BLOCK_BYTES - len(block))
        block_ok = False
        last_err_ser: Optional[str] = None
        for attempt in range(APP_MAX_ERROR_COUNT):
            log_timeout = attempt == APP_MAX_ERROR_COUNT - 1
            err = flasher.send_data_block_by_serial(
                serial, idx, block, log_timeout=log_timeout, app_from_fw=False
            )
            if err is None:
                block_ok = True
                break
            last_err_ser = err
            if attempt < APP_MAX_ERROR_COUNT - 1:
                if flasher.log_cb:
                    flasher.log_cb(
                        f"Повтор блока {idx + 1}/{total_blocks} (попытка {attempt + 2}/{APP_MAX_ERROR_COUNT})..."
                    )
                _sleep_before_retry_flash_data_block(flasher, serial, err, idx)
        if not block_ok:
            return f"Ошибка блока {idx + 1}/{total_blocks}: {last_err_ser} (исчерпано {APP_MAX_ERROR_COUNT} попыток)."
        blocks_sent += 1
        if progress_cb:
            progress_cb(blocks_sent, total_blocks)
        if flasher.log_cb and (
            blocks_sent % 50 == 0 or blocks_sent == total_blocks
        ):
            elapsed = max(0.001, time.perf_counter() - t_start)
            bps = blocks_sent / elapsed
            eta = (total_blocks - blocks_sent) / max(0.001, bps)
            flasher.log_cb(
                f"Скорость: {bps:.1f} бл/с, ETA: {eta:.1f} с ({blocks_sent}/{total_blocks})"
            )
        time.sleep(BLOCK_DELAY_FIRST_BLOCK_S_FAST if idx == 0 else BLOCK_DELAY_AFTER_RESPONSE_S_FAST)

    return None


def run_flash_sequence_wb(
    flasher: FlasherProtocol,
    slave: int,
    image: bytes,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    retries_per_block: int = DEFAULT_RETRIES_PER_BLOCK,
    cancel_gate: Optional[FlashCancelGate] = None,
) -> Optional[str]:
    """
    Прошивка WB (.wbfw) по алгоритму wb-mcu-fw-flasher (flasher.c):
    - Info: первые 32 B файла как 16 рег BE в 0x1000; до 3 повторов при ошибке с sleep(3); после успеха — без паузы.
    - Data: блоки по 136 B в 0x2000 (BE). Между блоками — без задержки (interFrameDelay).
    - При ошибке блока: до 3 попыток; после 3 неудач — пропуск блока; при 6 подряд ошибках — выход.
    """
    gate = _resolve_flash_cancel_gate(cancel_cb, cancel_gate)
    if len(image) < INFO_BLOCK_BYTES:
        return f"Образ слишком короткий: {len(image)} байт (минимум {INFO_BLOCK_BYTES} для info-блока)."
    size = len(image)
    if flasher.log_cb:
        free_space, err_fs = flasher.read_wb_free_space(slave)
        if err_fs is None and free_space is not None:
            flasher.log_cb(f"Wiren Board: свободное место FlashFS (рег. 1003): {free_space} байт")
            if size > free_space and free_space >= 0:
                flasher.log_cb(f"Внимание: размер прошивки ({size} байт) больше заявленного свободного места ({free_space} байт). Прошивка продолжится.")
        elif err_fs:
            flasher.log_cb(f"Wiren Board: рег. 1003 недоступен: {err_fs} — пропускаем.")
    info_block = image[:INFO_BLOCK_BYTES]
    if flasher.log_cb:
        flasher.log_cb(f"Wiren Board: отправка info-блока (32 B, 16 рег BE) на адрес {slave}...")
    if gate.should_abort():
        return "Отменено пользователем"
    err = flasher.send_info_block_wb(slave, info_block)
    for attempt in range(WB_MAX_ERROR_COUNT):
        if err is None:
            break
        if flasher.log_cb:
            flasher.log_cb(f"Ошибка info-блока: {err}. Повтор через {WB_INFO_RETRY_SLEEP_S} с (попытка {attempt + 2}/{WB_MAX_ERROR_COUNT})...")
        if gate.should_abort():
            return "Отменено пользователем"
        time.sleep(WB_INFO_RETRY_SLEEP_S)
        err = flasher.send_info_block_wb(slave, info_block)
    if err:
        hint = " Устройство WB при несовпадении сигнатуры возвращает исключение 4." if ("код 4" in (err or "") or "Исключение Modbus" in (err or "")) else ""
        return f"Ошибка отправки info-блока (WB): {err}{hint}"
    gate.mark_irreversible()
    gate.sleep(ERASE_WAIT_AFTER_INFO_S_WB)
    if ERASE_WAIT_AFTER_INFO_S_WB > 0 and flasher.log_cb:
        flasher.log_cb(f"Пауза {ERASE_WAIT_AFTER_INFO_S_WB} с после info.")
    total_blocks = (size - INFO_BLOCK_BYTES + DATA_BLOCK_BYTES_WB - 1) // DATA_BLOCK_BYTES_WB
    t_start = time.perf_counter()
    if progress_cb:
        progress_cb(0, total_blocks)
    consecutive_errors = 0
    blocks_sent = 0
    for idx in range(total_blocks):
        if gate.should_abort():
            return "Отменено пользователем"
        start = INFO_BLOCK_BYTES + idx * DATA_BLOCK_BYTES_WB
        block = image[start : start + DATA_BLOCK_BYTES_WB]
        block_ok = False
        for attempt in range(WB_MAX_ERROR_COUNT):
            err = flasher.send_data_block_wb(slave, block, log_timeout=(attempt == WB_MAX_ERROR_COUNT - 1))
            if err is None:
                consecutive_errors = 0
                block_ok = True
                break
            consecutive_errors += 1
            if flasher.log_cb and attempt < WB_MAX_ERROR_COUNT - 1:
                flasher.log_cb(f"Ошибка блока {idx + 1}/{total_blocks}: {err}, повтор...")
        if not block_ok:
            if consecutive_errors >= WB_MAX_ERROR_COUNT * 2:
                return f"Ошибка блока {idx + 1}/{total_blocks}: {err} (подряд {consecutive_errors} ошибок, выход по алгоритму WB)."
            if flasher.log_cb:
                flasher.log_cb(f"Блок {idx + 1}/{total_blocks} пропущен после {WB_MAX_ERROR_COUNT} попыток (алгоритм WB).")
            time.sleep(0)
            continue
        blocks_sent += 1
        if progress_cb:
            progress_cb(blocks_sent, total_blocks)
        if flasher.log_cb and (blocks_sent % 20 == 0 or blocks_sent == total_blocks):
            elapsed = max(0.001, time.perf_counter() - t_start)
            bps = blocks_sent / elapsed
            flasher.log_cb(f"WB: {blocks_sent}/{total_blocks} бл., {bps:.1f} бл/с")
        time.sleep(0)
    return None


def run_flash_bootloader_sequence_by_address(
    flasher: FlasherProtocol,
    slave: int,
    image: bytes,
    signature: str,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    retries_per_block: int = DEFAULT_RETRIES_PER_BLOCK,
    cancel_gate: Optional[FlashCancelGate] = None,
) -> Optional[str]:
    """
    Прошивка образа бутлоадера (34 КБ) по Modbus-адресу (обычный Modbus): 0x1001=0x424C → info 0x1000 → блоки → commit 0x1006.
    slave: адрес устройства в загрузчике (обычно 247).
    """
    gate = _resolve_flash_cancel_gate(cancel_cb, cancel_gate)
    # .fw формат: 32 байта info-заголовка (CRC над raw binary) + байт-свопированный payload.
    # При app_from_fw кодировании payload_block_to_registers_app_le: в staging попадает raw binary,
    # running_crc = CRC32(raw binary) = CRC из заголовка .fw → коммит проходит.
    fw_info_bytes: Optional[bytes] = None
    if len(image) == BL_IMAGE_TOTAL_BYTES + INFO_BLOCK_BYTES:
        fw_info_bytes = image[:INFO_BLOCK_BYTES]
        image = image[INFO_BLOCK_BYTES:]
    if len(image) != BL_IMAGE_TOTAL_BYTES:
        return (
            f"Образ бутлоадера должен быть {BL_IMAGE_TOTAL_BYTES} байт, получено {len(image)}. "
            "Используйте .fw или .bin 34 КБ бутлоадера."
        )
    from_fw = fw_info_bytes is not None

    if flasher.log_cb:
        flasher.log_cb("Режим обновления бутлоадера: запись 0x424C в 0x1001 на адрес %d..." % slave)
    if gate.should_abort():
        return "Отменено пользователем"
    err = flasher.write_firmware_type_bootloader(slave)
    if err:
        return f"Ошибка установки режима бутлоадера: {err}"
    if gate.sleep(0.2):
        return "Отменено пользователем"

    if flasher.log_cb:
        flasher.log_cb(
            f"Отправка info-блока (сигнатура «{signature}», размер {BL_IMAGE_TOTAL_BYTES} байт)..."
        )
    # .fw: CRC в заголовке — над raw binary; app_le-кодирование блоков → staging = raw binary → running_crc совпадёт.
    # .bin: CRC пересчитываем через build_info_block(image), BE-кодирование.
    def _send_info() -> Optional[str]:
        if fw_info_bytes is not None:
            return flasher.send_info_block_wb(slave, fw_info_bytes)
        return flasher.send_info_block_bootloader(slave, signature, image)

    err = _send_info()
    for _ in range(3):
        if err and "Таймаут" in str(err):
            if gate.should_abort():
                return "Отменено пользователем"
            time.sleep(INFO_RETRY_SLEEP_S)
            err = _send_info()
        else:
            break
    if err:
        return f"Ошибка отправки info-блока бутлоадера: {err}"

    gate.mark_irreversible()
    if flasher.log_cb:
        flasher.log_cb(
            f"Ожидание {ERASE_WAIT_AFTER_INFO_S} с перед блоками данных..."
        )
    gate.sleep(ERASE_WAIT_AFTER_INFO_S)

    total_blocks = BL_BLOCKS_FULL_COUNT + 1
    t_start = time.perf_counter()
    if progress_cb:
        progress_cb(0, total_blocks)

    for idx in range(total_blocks):
        if gate.should_abort():
            return "Отменено пользователем"
        if idx < BL_BLOCKS_FULL_COUNT:
            start = idx * DATA_BLOCK_BYTES_BOOTLOADER
            block = image[start : start + DATA_BLOCK_BYTES_BOOTLOADER]
        else:
            start = BL_BLOCKS_FULL_COUNT * DATA_BLOCK_BYTES_BOOTLOADER
            block = image[start : BL_IMAGE_TOTAL_BYTES]
        last_err: Optional[str] = None
        for attempt in range(retries_per_block):
            log_timeout = attempt == retries_per_block - 1
            err = flasher.send_data_block_bootloader(slave, block, log_timeout=log_timeout, app_from_fw=from_fw)
            if err is None:
                last_err = None
                break
            last_err = err
            if attempt < retries_per_block - 1:
                if flasher.log_cb:
                    flasher.log_cb(
                        f"Повтор блока бутлоадера {idx + 1}/{total_blocks} (попытка {attempt + 2}/{retries_per_block})"
                    )
                time.sleep(RETRY_DELAY_BETWEEN_BLOCKS_S)

        if last_err is not None:
            return f"Ошибка блока бутлоадера {idx + 1}/{total_blocks}: {last_err}"

        if progress_cb:
            progress_cb(idx + 1, total_blocks)
        if flasher.log_cb and (
            (idx + 1) % 50 == 0 or (idx + 1) == total_blocks
        ):
            elapsed = max(0.001, time.perf_counter() - t_start)
            bps = (idx + 1) / elapsed
            eta = (total_blocks - (idx + 1)) / max(0.001, bps)
            flasher.log_cb(
                f"Бутлоадер: {idx + 1}/{total_blocks}, ETA: {eta:.1f} с"
            )
        if idx + 1 < total_blocks:
            delay = (
                BLOCK_DELAY_FIRST_BLOCK_S
                if idx == 0
                else BLOCK_DELAY_AFTER_RESPONSE_S
            )
            if delay > 0:
                time.sleep(delay)

    if flasher.log_cb:
        flasher.log_cb(
            "Все блоки бутлоадера записаны в staging. Готовность к commit."
        )
        flasher.log_cb(
            "Отправка commit: запись регистр 0x1006 = 1 на адрес %d..." % slave
        )
    err = flasher.send_commit_bootloader(slave)
    if err:
        if flasher.log_cb:
            flasher.log_cb(f"Ошибка commit: {err}")
        return f"Ошибка commit бутлоадера: {err}"
    if flasher.log_cb:
        flasher.log_cb("Команда 0x1006 отправлена успешно; устройство выполняет commit и перезагрузку.")
    return None


def run_flash_sequence_bootloader(
    flasher: FlasherProtocol,
    serial: int,
    image: bytes,
    signature: str,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    retries_per_block: int = DEFAULT_RETRIES_PER_BLOCK,
    cancel_gate: Optional[FlashCancelGate] = None,
) -> Optional[str]:
    """
    Прошивка образа бутлоадера (34 КБ) по серийному номеру (0x46): 0x1001=0x424C → info 0x1000 → блоки → commit 0x1006.
    image: ровно BL_IMAGE_TOTAL_BYTES.
    """
    gate = _resolve_flash_cancel_gate(cancel_cb, cancel_gate)
    fw_info_bytes_s: Optional[bytes] = None
    if len(image) == BL_IMAGE_TOTAL_BYTES + INFO_BLOCK_BYTES:
        fw_info_bytes_s = image[:INFO_BLOCK_BYTES]
        image = image[INFO_BLOCK_BYTES:]
    if len(image) != BL_IMAGE_TOTAL_BYTES:
        return (
            f"Образ бутлоадера должен быть {BL_IMAGE_TOTAL_BYTES} байт, получено {len(image)}. "
            "Используйте .fw или .bin 34 КБ бутлоадера."
        )
    from_fw_s = fw_info_bytes_s is not None
    if not serial or serial == 0xFFFFFFFF:
        return "Для прошивки по 0x46 нужен серийный номер устройства."

    if flasher.log_cb:
        flasher.log_cb("Режим обновления бутлоадера: запись 0x424C в 0x1001 по серийному...")
    if gate.should_abort():
        return "Отменено пользователем"
    err = flasher.write_firmware_type_bootloader_by_serial(serial)
    if err:
        return f"Ошибка установки режима бутлоадера: {err}"
    if gate.sleep(0.2):
        return "Отменено пользователем"

    if flasher.log_cb:
        flasher.log_cb(
            f"Отправка info-блока (сигнатура «{signature}», размер {BL_IMAGE_TOTAL_BYTES} байт)..."
        )
    def _send_info_s() -> Optional[str]:
        if fw_info_bytes_s is not None:
            return flasher.send_info_block_bytes_by_serial(serial, fw_info_bytes_s)
        return flasher.send_info_block_bootloader_by_serial(serial, signature, image)

    err = _send_info_s()
    for _ in range(3):
        if err and "Таймаут" in str(err):
            if gate.should_abort():
                return "Отменено пользователем"
            time.sleep(INFO_RETRY_SLEEP_S)
            err = _send_info_s()
        else:
            break
    if err:
        return f"Ошибка отправки info-блока бутлоадера: {err}"

    gate.mark_irreversible()
    if flasher.log_cb:
        flasher.log_cb(
            f"Ожидание {ERASE_WAIT_AFTER_INFO_S} с перед блоками данных..."
        )
    gate.sleep(ERASE_WAIT_AFTER_INFO_S)

    total_blocks = BL_BLOCKS_FULL_COUNT + 1  # 143: 142 по 244 B + 1 по 168 B
    t_start = time.perf_counter()
    if progress_cb:
        progress_cb(0, total_blocks)

    for idx in range(total_blocks):
        if gate.should_abort():
            return "Отменено пользователем"
        if idx < BL_BLOCKS_FULL_COUNT:
            start = idx * DATA_BLOCK_BYTES_BOOTLOADER
            block = image[start : start + DATA_BLOCK_BYTES_BOOTLOADER]
        else:
            start = BL_BLOCKS_FULL_COUNT * DATA_BLOCK_BYTES_BOOTLOADER
            block = image[start : BL_IMAGE_TOTAL_BYTES]
        last_err: Optional[str] = None
        for attempt in range(retries_per_block):
            log_timeout = attempt == retries_per_block - 1
            err = flasher.send_data_block_bootloader_by_serial(
                serial, block, log_timeout=log_timeout, app_from_fw=from_fw_s
            )
            if err is None:
                last_err = None
                break
            last_err = err
            if attempt < retries_per_block - 1:
                if flasher.log_cb:
                    flasher.log_cb(
                        f"Повтор блока бутлоадера {idx + 1}/{total_blocks} (попытка {attempt + 2}/{retries_per_block})"
                    )
                time.sleep(RETRY_DELAY_BETWEEN_BLOCKS_S)

        if last_err is not None:
            return f"Ошибка блока бутлоадера {idx + 1}/{total_blocks}: {last_err}"

        if progress_cb:
            progress_cb(idx + 1, total_blocks)
        if flasher.log_cb and (
            (idx + 1) % 50 == 0 or (idx + 1) == total_blocks
        ):
            elapsed = max(0.001, time.perf_counter() - t_start)
            bps = (idx + 1) / elapsed
            eta = (total_blocks - (idx + 1)) / max(0.001, bps)
            flasher.log_cb(
                f"Бутлоадер: {idx + 1}/{total_blocks}, ETA: {eta:.1f} с"
            )
        if idx + 1 < total_blocks:
            delay = (
                BLOCK_DELAY_FIRST_BLOCK_S
                if idx == 0
                else BLOCK_DELAY_AFTER_RESPONSE_S
            )
            if delay > 0:
                time.sleep(delay)

    if flasher.log_cb:
        flasher.log_cb(
            "Все блоки бутлоадера записаны в staging (0x0802F800 векторы 2 КБ, 0x08030000 код 32 КБ). "
            "Готовность к commit."
        )
        flasher.log_cb(
            "Отправка commit: запись регистр 0x1006 = 1 (устройство: копирование staging→0x08000000 и 0x08038000, "
            "верификация read-back, перезагрузка)."
        )
    err = flasher.send_commit_bootloader_by_serial(serial)
    if err:
        if flasher.log_cb:
            flasher.log_cb(f"Ошибка commit: {err}")
        return f"Ошибка commit бутлоадера: {err}"
    if flasher.log_cb:
        flasher.log_cb("Команда 0x1006 отправлена успешно; устройство выполняет commit и перезагрузку.")
    return None
