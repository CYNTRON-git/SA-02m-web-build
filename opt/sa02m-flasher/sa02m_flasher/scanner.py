# -*- coding: utf-8 -*-
"""
Сканирование RS-485: сначала быстрый скан WB extended (0xFD 0x46 0x01) по всем выбранным скоростям, затем опрос диапазона адресов по всем скоростям. Порядок скоростей: 115200 → 38400 → 19200 → 9600.
Через Modbus TCP (шлюз, MBAP) или RTU over TCP (сырой RTU по сокету) доступна только фаза 2 (стандартный Modbus); 0xFD по TCP обычно не поддерживается.
"""
import time
from enum import Enum
from pathlib import Path
from typing import Any, List, Optional, Callable, Tuple, Set, Dict
from dataclasses import dataclass, replace

from . import modbus_rtu
from . import flasher_log
from .modbus_io import (
    decode_bootloader_version_registers_8,
    decode_signature_from_holding_290_payload,
    serial_reconcile_modbus_regs_with_wb,
    u32_swap_halfwords,
    uint32_from_modbus_reg_pair_be,
)
from .modbus_tcp import modbus_rtu_over_tcp_transact, modbus_tcp_transact
from .serial_port import (
    open_port,
    send_receive,
    send_receive_all,
    send_receive_wb_ext_scan,
    _format_listen_chunk,
    _sleep_interruptible,
)
from .flash_protocol import FlasherProtocol
from . import module_profiles
from .serial_ranges import signature_from_serial

BROADCAST_ADDR = modbus_rtu.BROADCAST_ADDR
# Адреса для broadcast: 255 (MP-02m), 0xFD (быстрый Modbus / расширенный WB)
BROADCAST_ADDRS = (modbus_rtu.BROADCAST_ADDR, modbus_rtu.BROADCAST_ADDR_FD)


def tcp_endpoint_host_port_mode(
    tcp_ep: Optional[Tuple[Any, ...]],
) -> Tuple[Optional[str], Optional[int], str]:
    """
    tcp_ep: (host, port) — Modbus TCP (MBAP);
    (host, port, 'mbap') — то же;
    (host, port, 'rtu_tcp') — Modbus RTU поверх TCP без MBAP.
    """
    if tcp_ep is None:
        return None, None, "mbap"
    try:
        h = str(tcp_ep[0]).strip()
        p = int(tcp_ep[1])
    except (TypeError, ValueError, IndexError):
        return None, None, "mbap"
    mode = "mbap"
    if len(tcp_ep) >= 3:
        m = str(tcp_ep[2]).strip().lower()
        if m in ("rtu_tcp", "rtu"):
            mode = "rtu_tcp"
        elif m == "mbap":
            mode = "mbap"
    return h, p, mode


class ScanMode(str, Enum):
    """Режим сканирования для UI."""
    EXTENDED_ONLY = "быстрый модбас"
    STANDARD_ONLY = "обычный поиск"
    BOOTLOADER_ONLY = "устройства в bootloader"


REG_PROBE_0 = 0          # быстрая проверка «есть ответ» (бутлоадер отдаёт 2 рег: 0x0001, 0x0000)
REG_PROBE_0_COUNT = 2
REG_SIGNATURE = 290
REG_SIGNATURE_COUNT = 12
REG_SERIAL_LO = 270
REG_SERIAL_HI = 271
REG_VERSION_MAJOR = 320
REG_VERSION_MINOR = 321
REG_VERSION_PATCH = 322
REG_VERSION_SUFFIX = 323
REG_BOOTLOADER_VER = 330
REG_BOOTLOADER_VER_COUNT = 8

SCAN_TIMEOUT_MS = 250   # таймаут ответа при скане (один запрос на адрес: reg 0, count 2)
BOOTLOADER_BAUD = 115200


def _rtu_char_time_s(baudrate: int, parity: str, stopbits: int) -> float:
    """Длительность одного символа на линии (с), как в serial_port (старт + 8 данных + стоп/чётность)."""
    bits = 1 + 8 + (2 if int(stopbits) == 2 else 1)
    if str(parity).upper() in ("E", "O"):
        bits += 1
    return bits / float(max(int(baudrate), 300))


def _rtu_inter_frame_delay_s(baudrate: int, parity: str, stopbits: int) -> float:
    """Пауза между кадрами (≥ 3.5 символа Modbus RTU), с нижней границей для USB/RS‑485."""
    t = _rtu_char_time_s(baudrate, parity, stopbits) * 3.5 * 1.2
    br = int(baudrate)
    # На 115200 цепочка reg0→270→320→… в одном сеансе COM: 2 ms мало для части адаптеров/приложений МК.
    if br >= 115200:
        lo = 0.004
    elif br >= 38400:
        lo = 0.0025
    elif br >= 19200:
        lo = 0.0018
    else:
        lo = 0.0035
    return max(t, lo)


def _phase2_gap_between_addresses_s(baudrate: int, parity: str, stopbits: int) -> float:
    """Пауза между опросами разных адресов (фаза 2): быстрее на высоких бодах, безопасно на 9600."""
    d = _rtu_inter_frame_delay_s(baudrate, parity, stopbits)
    if int(baudrate) <= 19200:
        return max(d * 2.2, 0.0045)
    return max(d * 2.5, 0.002)
# Таймаут приёма ответов на один broadcast (устройства с арбитражем отвечают с задержкой)
BROADCAST_COLLECT_TIMEOUT_MS = 800
# Резерв: число попыток broadcast «один запрос — один ответ», если приём всех ответов пуст
BROADCAST_FALLBACK_ATTEMPTS = 5
BROADCAST_FALLBACK_DELAY_S = 0.08
# Порядок скоростей: по убыванию (сначала быстрые)
SCAN_BAUDRATES = [115200, 38400, 19200, 9600]
DEFAULT_PARITY = "N"
DEFAULT_STOPBITS = 1   # 8N1
# Варианты параметров связи для выбора в UI: (чётность, стоп-биты) → подпись
SCAN_LINK_OPTIONS = [
    ("N", 1, "8N1"),
    ("E", 1, "8E1"),
    ("O", 1, "8O1"),
    ("E", 2, "8E2"),
    ("O", 2, "8O2"),
    ("N", 2, "8N2"),
]
# Тип: (baudrate, parity, stopbits)
SpeedConfig = Tuple[int, str, int]


def _default_speed_configs() -> List[SpeedConfig]:
    """Конфиги по умолчанию: все скорости, 8N1."""
    return [(b, DEFAULT_PARITY, DEFAULT_STOPBITS) for b in SCAN_BAUDRATES]


@dataclass
class DeviceInfo:
    address: int
    baudrate: int
    parity: str
    stopbits: int
    signature: str
    app_version: str  # "X.Y.Z.W" or "—"
    bootloader_version: str
    serial: int  # uint32
    in_bootloader: bool
    supports_fast_modbus: bool = False  # True если найдено в фазе 1 (0xFD 0x46); иначе только стандартный Modbus
    # Серийный из WB extended-скана для этой строки; стабилен при опросе регистров (различает два устройства с одним адресом).
    wb_scan_serial: Optional[int] = None


def _is_scan_info_missing(s: Optional[str]) -> bool:
    """Нет данных для поля таблицы: пусто или заполнитель «—» (truthy-строка «—» ломала прежнюю проверку not dev.bootloader_version)."""
    if s is None:
        return True
    t = str(s).strip()
    return t == "" or t == "—"


def device_identity_complete_for_module_config(dev: DeviceInfo) -> bool:
    """Сканер прочитал сигнатуру, серийный и версию ПО (или версию загрузчика в режиме BL)."""
    if _is_scan_info_missing(dev.signature):
        return False
    sn = int(dev.serial) & 0xFFFFFFFF
    if sn in (0, 0xFFFFFFFF):
        return False
    if dev.in_bootloader:
        return not _is_scan_info_missing(dev.bootloader_version)
    return not _is_scan_info_missing(dev.app_version)


def device_is_mp02_product_line_for_config(dev: DeviceInfo) -> bool:
    """Модули линейки МР‑02м / MP‑02m и совместимые (DTV, CE‑02m‑3 и т.д. по сигнатуре)."""
    sig = dev.signature or ""
    if module_profiles.is_mp_module_signature_for_batch_flash(sig):
        return True
    if module_profiles.code_from_signature(sig) is not None:
        return True
    return False


def device_eligible_for_module_config_window(dev: DeviceInfo) -> bool:
    """Окно настройки модуля — только для «своих» устройств с полной идентификацией из скана."""
    return device_is_mp02_product_line_for_config(
        dev
    ) and device_identity_complete_for_module_config(dev)


def _apply_signature_from_serial_if_missing(dev: DeviceInfo) -> DeviceInfo:
    """
    Если рег. 290 дал пустую/бинарную сигнатуру (например EEPROM 240 занят блобом 4TO6DI),
    подставить тип по диапазону серийного (см. serial_ranges).
    """
    if not _is_scan_info_missing(dev.signature):
        return dev
    guess = signature_from_serial(dev.serial)
    if guess:
        return replace(dev, signature=guess)
    return dev


def _fill_bootloader_info_by_serial(
    port: str,
    baud: int,
    parity: str,
    stopbits: int,
    serial: int,
    dev: "DeviceInfo",
    log_cb: Optional[Callable[[str], None]] = None,
    response_timeout_ms: int = 1200,
) -> None:
    """Сигнатура 290, версия бутлоадера 330 и при необходимости версия приложения 320–323 по 0xFD 0x46 0x08 (один проход)."""
    try:
        ser = open_port(port, baudrate=baud, parity=parity, stopbits=stopbits)
        try:
            try:
                ser.reset_input_buffer()
            except Exception:
                pass
            proto = FlasherProtocol(
                lambda req: send_receive(ser, req, response_timeout_ms=response_timeout_ms),
                log_cb=None,
                verbose_exchange_log=False,
            )
            if _is_scan_info_missing(dev.signature) or _is_scan_info_missing(dev.bootloader_version):
                sig, ver, err = proto.read_bootloader_info_by_serial(serial)
                # Важный кейс: у части устройств версия бутлоадера недоступна (или невалидна),
                # но сигнатура по reg 290 читается корректно — её нужно сохранять даже при err.
                if sig:
                    dev.signature = sig.strip()
                if ver and not _is_scan_info_missing(ver):
                    dev.bootloader_version = ver.strip()
                elif log_cb and err:
                    log_cb(
                        f"[{baud} {parity}{stopbits}] WB serial 0x{serial & 0xFFFFFFFF:08X}: "
                        f"чтение bootloader info (290/330) неуспешно: {err}"
                    )
            if _is_scan_info_missing(dev.app_version):
                pl, err2 = proto.read_holding_registers_by_serial(
                    serial, REG_VERSION_MAJOR, 4
                )
                if not err2 and pl:
                    app_ver = _parse_version_4(pl)
                    if app_ver and not _is_scan_info_missing(app_ver):
                        dev.app_version = app_ver
                        dev.in_bootloader = _is_bootloader_mode(app_ver)
                elif log_cb and err2:
                    log_cb(
                        f"[{baud} {parity}{stopbits}] WB serial 0x{serial & 0xFFFFFFFF:08X}: "
                        f"чтение app version (320..323) неуспешно: {err2}"
                    )
        finally:
            ser.close()
    except Exception:
        if log_cb:
            log_cb(
                f"[{baud} {parity}{stopbits}] WB serial 0x{serial & 0xFFFFFFFF:08X}: "
                "ошибка открытия/обмена порта при чтении bootloader info"
            )


def _poll_identity_fast_modbus_for_dup_addr(
    port: str,
    baud: int,
    parity: str,
    stopbits: int,
    serial: int,
    dev: DeviceInfo,
    log_cb: Optional[Callable[[str], None]],
    response_timeout_ms: int = 2000,
) -> None:
    """
    Несколько устройств с одним Modbus-адресом (часто два 247 в загрузчике): идентификация
    только по 0xFD 0x46 0x08 на серийный из WB-скана (рег. 320–323, 290/330).
    Опрос по адресу slave даёт ответ «случайного» узла — не используем.
    Подробные строки в log_cb (при сканировании GUI → flasher_log.txt, ui=False).
    """
    cfg = f"{baud} {parity}{stopbits}"
    sn = serial & 0xFFFFFFFF
    if log_cb:
        log_cb(
            f"[DUP_ADDR {cfg}] Адрес {dev.address} совпадает у нескольких устройств на линии — "
            f"сигнатура и версии только по быстрому Modbus, целевой SN 0x{sn:08X} "
            f"(чтение 320–323, затем 290/330)."
        )
    try:
        ser = open_port(port, baudrate=baud, parity=parity, stopbits=stopbits)
        try:
            try:
                ser.reset_input_buffer()
            except Exception:
                pass
            proto = FlasherProtocol(
                lambda req: send_receive(ser, req, response_timeout_ms=response_timeout_ms),
                log_cb=None,
                verbose_exchange_log=False,
            )
            if log_cb:
                log_cb(
                    f"[DUP_ADDR {cfg}] SN 0x{sn:08X}: запрос FC по 0x46 — holding 320..323 (версия приложения, 4 рег.)."
                )
            pl_ver, err_ver = proto.read_holding_registers_by_serial(serial, REG_VERSION_MAJOR, 4)
            if err_ver or not pl_ver:
                if log_cb:
                    log_cb(
                        f"[DUP_ADDR {cfg}] SN 0x{sn:08X}: ответ 320–323: ошибка={err_ver!r}, длина payload={len(pl_ver or b'')} б."
                    )
            else:
                preview = pl_ver[: min(16, len(pl_ver))].hex(" ") if pl_ver else ""
                if log_cb:
                    log_cb(
                        f"[DUP_ADDR {cfg}] SN 0x{sn:08X}: данные 320–323 (до 16 б): {preview}"
                    )
                app_ver = _parse_version_4(pl_ver)
                if log_cb:
                    log_cb(
                        f"[DUP_ADDR {cfg}] SN 0x{sn:08X}: версия пр. «{app_ver}», режим загрузчика={_is_bootloader_mode(app_ver)}."
                    )
                dev.app_version = app_ver
                dev.in_bootloader = _is_bootloader_mode(app_ver)
            if log_cb:
                log_cb(
                    f"[DUP_ADDR {cfg}] SN 0x{sn:08X}: запрос FC по 0x46 — рег. 290 (сигнатура), 330 (версия загрузчика)."
                )
            sig, bl, err_bl = proto.read_bootloader_info_by_serial(serial)
            if log_cb:
                if err_bl:
                    log_cb(
                        f"[DUP_ADDR {cfg}] SN 0x{sn:08X}: 290/330: ошибка={err_bl!r}; sig={sig!r}, bl_ver={bl!r}."
                    )
                else:
                    log_cb(
                        f"[DUP_ADDR {cfg}] SN 0x{sn:08X}: сигнатура «{sig or '—'}», версия загрузчика «{bl or '—'}»."
                    )
            if sig:
                dev.signature = sig.strip()
            if bl and not _is_scan_info_missing(bl):
                dev.bootloader_version = bl.strip()
        finally:
            ser.close()
    except Exception as ex:
        if log_cb:
            log_cb(f"[DUP_ADDR {cfg}] SN 0x{sn:08X}: исключение при опросе: {ex!r}")


def _read_regs(
    port: str,
    slave: int,
    start: int,
    count: int,
    baudrate: int,
    parity: str,
    stopbits: int,
    timeout_ms: int = SCAN_TIMEOUT_MS,
    tcp_ep: Optional[Tuple[str, int]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    ser: Optional[Any] = None,
) -> Tuple[Optional[int], Optional[bytes]]:
    """Возвращает (адрес ответившего, payload) или (None, None). При broadcast (slave=255) в ответе приходит реальный адрес устройства (1–247).
    Если передан открытый ser — порт не закрывается (для серии запросов в scan_address)."""
    try:
        req = modbus_rtu.build_read_holding_registers(slave, start, count)
        if tcp_ep is not None:
            th, tp, tmode = tcp_endpoint_host_port_mode(tcp_ep)
            if th is None or tp is None:
                return (None, None)
            if tmode == "rtu_tcp":
                rsp = modbus_rtu_over_tcp_transact(
                    th, tp, req, timeout_ms, cancel_check=cancel_cb
                )
            else:
                rsp = modbus_tcp_transact(
                    th, tp, req, timeout_ms, cancel_check=cancel_cb
                )
            if rsp is None:
                return (None, None)
            expected = None if slave in BROADCAST_ADDRS else slave
            addr, payload, err = modbus_rtu.parse_response(rsp, expected_slave=expected)
            if addr is None:
                return (None, None)
            if slave in BROADCAST_ADDRS:
                return (addr, payload if payload is not None else None)
            if err or payload is None:
                return (None, None)
            return (addr, payload)
        own_ser = ser is None
        s = ser if ser is not None else open_port(port, baudrate=baudrate, parity=parity, stopbits=stopbits)
        try:
            ifd = _rtu_inter_frame_delay_s(baudrate, parity, stopbits)
            if slave == BROADCAST_ADDR:
                pre = max(0.055, ifd * 5.0)
                if not _sleep_interruptible(pre, cancel_cb, step=0.01):
                    return (None, None)
            else:
                if not _sleep_interruptible(ifd, cancel_cb, step=0.005):
                    return (None, None)
            rsp = send_receive(
                s, req, response_timeout_ms=timeout_ms, cancel_check=cancel_cb
            )
            if rsp is None:
                return (None, None)
            expected = None if slave in BROADCAST_ADDRS else slave
            addr, payload, err = modbus_rtu.parse_response(rsp, expected_slave=expected)
            if addr is None:
                return (None, None)
            if slave in BROADCAST_ADDRS:
                return (addr, payload if payload is not None else None)
            if err or payload is None:
                return (None, None)
            return (addr, payload)
        finally:
            if own_ser:
                s.close()
    except Exception:
        return (None, None)


def _read_regs_broadcast(
    port: str,
    start: int,
    count: int,
    baudrate: int,
    parity: str,
    stopbits: int,
    timeout_ms: int = BROADCAST_COLLECT_TIMEOUT_MS,
) -> List[Tuple[int, Optional[bytes]]]:
    """
    Широковещательный запрос по адресам из BROADCAST_ADDRS (255, 0xFD) — принять ответы от всех устройств.
    Возвращает список (адрес, payload) без дубликатов по адресу.
    """
    try:
        ser = open_port(port, baudrate=baudrate, parity=parity, stopbits=stopbits)
        try:
            time.sleep(0.12)
            seen: Set[int] = set()
            result: List[Tuple[int, Optional[bytes]]] = []
            for broadcast_addr in BROADCAST_ADDRS:
                req = modbus_rtu.build_read_holding_registers(broadcast_addr, start, count)
                responses = send_receive_all(ser, req, response_timeout_ms=timeout_ms)
                for (addr, payload) in responses:
                    if 1 <= addr <= 247 and addr not in seen:
                        seen.add(addr)
                        result.append((addr, payload))
                if result and broadcast_addr == BROADCAST_ADDRS[0]:
                    time.sleep(0.05)
            return result
        finally:
            ser.close()
    except Exception:
        return []


# Длительность тишины для завершения приёма (мс); больше — даёт время на ответ нескольких устройств
WB_EXT_SCAN_SILENCE_MS = 100


def _wb_ext_scan_host_preamble(
    ser,
    log_cb: Optional[Callable[[str], None]] = None,
    *,
    silent: bool = False,
) -> None:
    """
    Перед первым 0xFD 0x46 0x01 в цикле сканирования: отправить 0xFD 0x46 0x04 (конец WB-скана).

    Почему: после длинной сессии быстрого Modbus (0x08) / смены скорости COM / повторного открытия порта
    бутлоадер MP-02m может оставаться в промежуточном состоянии WB-скана; повторный 0x01 иногда даёт
    полное молчание на линии (0 байт RX за окно), хотя устройство в загрузчике физически на шине
    (см. dist/flasher_log.txt: 21:55:33 на 115200 — тишина; на 19200 скан сразу видит ответы).
    Кадр 0x04 на МК вызывает bl_wb_reset_scan_cycle() и выравнивает ожидание следующего 0x01.
    """
    try:
        send_receive(ser, modbus_rtu.build_wb_ext_scan_end(), response_timeout_ms=120)
    except Exception:
        pass
    try:
        ser.reset_input_buffer()
    except OSError:
        pass
    time.sleep(0.03)
    if log_cb and not silent:
        log_cb("  Быстрый скан: преамбула 0xFD 0x46 0x04 (сброс цикла WB на линии перед 0x01).")


def _wb_ext_scan(
    port: str,
    baudrate: int,
    parity: str,
    stopbits: int,
    timeout_ms: int = BROADCAST_COLLECT_TIMEOUT_MS,
    log_cb: Optional[Callable[[str], None]] = None,
    log_listen: bool = False,
    tcp_ep: Optional[Tuple[str, int]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    wb_trace_cb: Optional[Callable[[str], None]] = None,
) -> List[Tuple[int, int]]:
    """WB extended scan (0xFD 0x46 0x01): открыть порт, отправить запрос, собрать ответы 0xFD 0x46 0x03 (один проход).
    log_listen=True — в лог пишется прослушивание линии (каждый RX-фрагмент с временной меткой и пометкой арбитраж/ответ).
    wb_trace_cb — полный дамп TX/RX в отдельный журнал (см. flasher_log.append_wb_trace)."""
    if tcp_ep is not None:
        return []
    # Уникальные пары (адрес Modbus, серийный из WB-скана). Раньше слияние шло только по адресу —
    # при двух устройствах с одним адресом и разными SN оставалось одно (случайный SN по голосованию).
    merged_pairs: List[Tuple[int, int]] = []
    seen_pair: Set[Tuple[int, int]] = set()
    listen_cb: Optional[Callable[[float, bytes], None]] = None
    if log_listen and log_cb:

        def _listen(t_ms: float, chunk: bytes) -> None:
            if chunk:
                log_cb("    " + _format_listen_chunk(t_ms, chunk))

        listen_cb = _listen
    try:
        try:
            ser = open_port(port, baudrate=baudrate, parity=parity, stopbits=stopbits)
        except Exception:
            return []
        try:
            if cancel_cb and cancel_cb():
                return []
            _wb_ext_scan_host_preamble(ser, log_cb)
            if log_listen and log_cb:
                log_cb("  Прослушивание линии (арбитраж):")
            tag = "%d %s%d" % (baudrate, parity, stopbits)
            if wb_trace_cb:
                wb_trace_cb(
                    "### WB arbitration | port=%s | line=%s ###"
                    % (port, tag)
                )
            partial = send_receive_wb_ext_scan(
                ser,
                response_timeout_ms=timeout_ms,
                silence_ms=WB_EXT_SCAN_SILENCE_MS,
                log_cb=log_cb,
                listen_cb=listen_cb,
                cancel_check=cancel_cb,
                wb_trace_cb=wb_trace_cb,
                wb_trace_tag=tag,
            )
            if (
                not partial
                and baudrate >= 57600
                and not (cancel_cb and cancel_cb())
            ):
                if log_cb:
                    log_cb(
                        "  Быстрый скан: ответов нет — повтор цикла после паузы (рассинхрон WB / USB‑UART после предыдущей сессии)."
                    )
                time.sleep(0.12)
                _wb_ext_scan_host_preamble(ser, log_cb, silent=True)
                partial = send_receive_wb_ext_scan(
                    ser,
                    response_timeout_ms=timeout_ms,
                    silence_ms=WB_EXT_SCAN_SILENCE_MS,
                    log_cb=log_cb,
                    listen_cb=listen_cb,
                    cancel_check=cancel_cb,
                    wb_trace_cb=wb_trace_cb,
                    wb_trace_tag=tag,
                )
            for addr, serial in partial:
                k = (addr, serial & 0xFFFFFFFF)
                if k not in seen_pair:
                    seen_pair.add(k)
                    merged_pairs.append((addr, serial))
            if log_cb and partial:
                addrs = sorted(set(a for a, _ in partial))
                log_cb("  Быстрый скан: найдено адреса %s" % addrs)
        finally:
            ser.close()
        merged_pairs.sort(key=lambda p: (0x6 << 28) | (p[1] & 0x0FFFFFFF))
        return merged_pairs
    except Exception:
        return []


def _parse_serial(payload: Optional[bytes]) -> int:
    """Серийный из рег. 270–271; payload — только байты данных ответа 0x03 (без byte_count). См. uint32_from_modbus_reg_pair_be."""
    return uint32_from_modbus_reg_pair_be(payload or b"", 0)


def format_serial_decimal(serial: int) -> str:
    """Только десятичное представление uint32 (для колонки таблицы)."""
    u = serial & 0xFFFFFFFF
    if u == 0 or u == 0xFFFFFFFF:
        return "—"
    return str(u)


def format_serial_hex_only(serial: int) -> str:
    """Только hex uint32 (8 hex цифр)."""
    u = serial & 0xFFFFFFFF
    if u == 0 or u == 0xFFFFFFFF:
        return "—"
    return f"0x{u:08X}"


def format_serial_for_display(serial: int) -> str:
    """Строка для логов: десятичное и hex в одной строке."""
    u = serial & 0xFFFFFFFF
    if u == 0 or u == 0xFFFFFFFF:
        return "—"
    return f"{u} (0x{u:08X})"


def device_table_key(d: DeviceInfo) -> Tuple[int, int, str, int, int]:
    """Ключ строки таблицы прошивальщика: линия связи + серийный из WB-скана (если есть), иначе текущий serial."""
    if d.wb_scan_serial is not None:
        sid = d.wb_scan_serial & 0xFFFFFFFF
    else:
        sid = d.serial & 0xFFFFFFFF
    return (d.address, d.baudrate, d.parity, d.stopbits, sid)


def wb_arb_sort_key(d: DeviceInfo) -> int:
    """Ключ как arbitrage_word в fast_mb make_arbitrage_data для фазы «ещё не отсканированы» (0x01): (0x6<<28)|serial28."""
    if d.wb_scan_serial is not None:
        sid = d.wb_scan_serial & 0xFFFFFFFF
    else:
        sid = d.serial & 0xFFFFFFFF
    return (0x6 << 28) | (sid & 0x0FFFFFFF)


def _parse_version_4(payload: Optional[bytes]) -> str:
    if payload is None or len(payload) < 8:
        return "—"
    # 320=MAJOR, 321=MINOR, 322=PATCH, 323=SUFFIX; each reg big-endian in response
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


def _safe_str_from_bytes(raw: bytes, max_len: int = 0) -> str:
    """
    Безопасный вывод байтов в таблицу: latin-1 (без замены на U+FFFD),
    непечатаемые символы (в т.ч. 0x80–0xFF) заменяются точкой; обрезка по max_len.
    """
    if not raw:
        return ""
    s = raw.decode("latin-1")
    out = "".join(c if 32 <= ord(c) <= 126 else "." for c in s)
    out = out.rstrip(". ")
    if max_len and len(out) > max_len:
        out = out[:max_len]
    return out


def _parse_signature(payload: Optional[bytes]) -> str:
    """Рег. 290..301: сначала считанная сигнатура (ASCII или блоб 4TO6DI A8+v1/v2)."""
    return decode_signature_from_holding_290_payload(payload)


def _is_bootloader_mode(app_version: str) -> bool:
    """Режим загрузчика: бутлоадер отдаёт версию приложения 0.0.0.0; приложение — ненулевую (например 2.0.0.0)."""
    return app_version in ("—", "", "0.0.0.0")


def _parse_bootloader_ver(payload: Optional[bytes]) -> str:
    """
    Версия загрузчика: 8 регистров (строка по младшему или старшему байту регистра), null-terminated.
    Рег. 330 в приложении — байты из Flash 0x080000D0 (.bl_version бутлоадера); если там не ASCII-строка,
    uint32 из первых двух регистров — мусор (код/векторы), не показываем как версию.
    """
    if payload is None or len(payload) < 16:
        return "—"
    return decode_bootloader_version_registers_8(payload) or "—"


def scan_address(
    port: str,
    address: int,
    speed_configs: List[SpeedConfig],
    log_cb: Optional[Callable[[str], None]] = None,
    current_cb: Optional[Callable[[int, int, str, int], None]] = None,
    tcp_ep: Optional[Tuple[str, int]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    on_partial: Optional[Callable[[DeviceInfo], None]] = None,
    wb_serial_hint: Optional[int] = None,
    wb_scan_row_serial: Optional[int] = None,
) -> Optional[DeviceInfo]:
    """Опрос одного адреса: reg 0, затем 270–271, версия, сигнатура, версия загрузчика.
    on_partial вызывается после каждого шага с накопленным DeviceInfo (для постепенного обновления таблицы в GUI).
    wb_serial_hint — из WB extended: при каждом on_partial сверять с рег. 270–271 (подстановка до опроса и reconcile после).
    wb_scan_row_serial — стабильный SN строки WB-скана (два устройства с одним адресом)."""
    row_key: Optional[int] = (
        wb_scan_row_serial & 0xFFFFFFFF
        if wb_scan_row_serial is not None
        else None
    )

    def _tag(d: DeviceInfo) -> DeviceInfo:
        return replace(d, wb_scan_serial=row_key) if row_key is not None else d

    def _emit(d: DeviceInfo) -> None:
        if on_partial is None:
            return
        out = replace(d, wb_scan_serial=row_key) if row_key is not None else d
        if wb_serial_hint is not None:
            w = wb_serial_hint & 0xFFFFFFFF
            if w not in (0, 0xFFFFFFFF):
                rs = out.serial & 0xFFFFFFFF
                if rs in (0, 0xFFFFFFFF):
                    out = replace(out, serial=w)
                else:
                    out = replace(out, serial=serial_reconcile_modbus_regs_with_wb(rs, w))
        on_partial(out)

    for (baud, parity, stopbits) in speed_configs:
        if cancel_cb and cancel_cb():
            return None
        if current_cb:
            current_cb(address, baud, parity, stopbits)
        ser_local: Optional[Any] = None
        if tcp_ep is None:
            try:
                ser_local = open_port(port, baudrate=baud, parity=parity, stopbits=stopbits)
            except Exception:
                continue
        try:
            _, pl0 = _read_regs(
                port, address, REG_PROBE_0, REG_PROBE_0_COUNT,
                baud, parity, stopbits, SCAN_TIMEOUT_MS, tcp_ep=tcp_ep,
                cancel_cb=cancel_cb, ser=ser_local,
            )
            if pl0 is not None:
                dev = DeviceInfo(
                    address=address,
                    baudrate=baud,
                    parity=parity,
                    stopbits=stopbits,
                    signature="",
                    app_version="—",
                    bootloader_version="—",
                    serial=0,
                    in_bootloader=True,
                )
                _, pl_ser = _read_regs(
                    port, address, REG_SERIAL_LO, 2,
                    baud, parity, stopbits, SCAN_TIMEOUT_MS, tcp_ep=tcp_ep,
                    cancel_cb=cancel_cb, ser=ser_local,
                )
                if pl_ser is None:
                    _emit(_tag(dev))
                    return _tag(dev)
                serial_val = _parse_serial(pl_ser)
                dev = replace(dev, serial=serial_val)
                _emit(_tag(dev))
                _, pl_ver = _read_regs(
                    port, address, REG_VERSION_MAJOR, 4,
                    baud, parity, stopbits, SCAN_TIMEOUT_MS, tcp_ep=tcp_ep,
                    cancel_cb=cancel_cb, ser=ser_local,
                )
                app_ver = _parse_version_4(pl_ver) if pl_ver else "—"
                dev = replace(dev, app_version=app_ver, in_bootloader=_is_bootloader_mode(app_ver))
                _emit(dev)
                _, sig_pl = _read_regs(
                    port, address, REG_SIGNATURE, REG_SIGNATURE_COUNT,
                    baud, parity, stopbits, SCAN_TIMEOUT_MS, tcp_ep=tcp_ep,
                    cancel_cb=cancel_cb, ser=ser_local,
                )
                sig = _parse_signature(sig_pl) if sig_pl else ""
                dev = replace(dev, signature=sig)
                _emit(dev)
                _, bl_ver_pl = _read_regs(
                    port, address, REG_BOOTLOADER_VER, REG_BOOTLOADER_VER_COUNT,
                    baud, parity, stopbits, SCAN_TIMEOUT_MS, tcp_ep=tcp_ep,
                    cancel_cb=cancel_cb, ser=ser_local,
                )
                bl_ver = _parse_bootloader_ver(bl_ver_pl) if bl_ver_pl else "—"
                dev = replace(dev, bootloader_version=bl_ver)
                _emit(dev)
                return _tag(_apply_signature_from_serial_if_missing(dev))
            # Приложение: сначала серийный 270–271
            _, pl_ser = _read_regs(
                port, address, REG_SERIAL_LO, 2,
                baud, parity, stopbits, SCAN_TIMEOUT_MS, tcp_ep=tcp_ep,
                cancel_cb=cancel_cb, ser=ser_local,
            )
            if pl_ser is not None:
                serial_val = _parse_serial(pl_ser)
                dev = DeviceInfo(
                    address=address,
                    baudrate=baud,
                    parity=parity,
                    stopbits=stopbits,
                    signature="",
                    app_version="—",
                    bootloader_version="—",
                    serial=serial_val,
                    in_bootloader=False,
                )
                _emit(dev)
                _, pl_ver = _read_regs(
                    port, address, REG_VERSION_MAJOR, 4,
                    baud, parity, stopbits, SCAN_TIMEOUT_MS, tcp_ep=tcp_ep,
                    cancel_cb=cancel_cb, ser=ser_local,
                )
                app_ver = _parse_version_4(pl_ver) if pl_ver else "—"
                dev = replace(dev, app_version=app_ver, in_bootloader=_is_bootloader_mode(app_ver))
                _emit(dev)
                _, sig_pl = _read_regs(
                    port, address, REG_SIGNATURE, REG_SIGNATURE_COUNT,
                    baud, parity, stopbits, SCAN_TIMEOUT_MS, tcp_ep=tcp_ep,
                    cancel_cb=cancel_cb, ser=ser_local,
                )
                sig = _parse_signature(sig_pl) if sig_pl else ""
                dev = replace(dev, signature=sig)
                _emit(dev)
                _, bl_ver_pl = _read_regs(
                    port, address, REG_BOOTLOADER_VER, REG_BOOTLOADER_VER_COUNT,
                    baud, parity, stopbits, SCAN_TIMEOUT_MS, tcp_ep=tcp_ep,
                    cancel_cb=cancel_cb, ser=ser_local,
                )
                bl_ver = _parse_bootloader_ver(bl_ver_pl) if bl_ver_pl else "—"
                dev = replace(dev, bootloader_version=bl_ver)
                _emit(dev)
                return _tag(_apply_signature_from_serial_if_missing(dev))
        finally:
            if ser_local is not None:
                try:
                    ser_local.close()
                except Exception:
                    pass
    return None


def scan_broadcast(port: str, log_cb: Optional[Callable[[str], None]] = None) -> Optional[DeviceInfo]:
    """DEPRECATED: legacy broadcast-scan (addr 255) отключён для консистентности с firmware.

    Основной путь поиска: WB extended scan (0xFD 0x46 0x01) + адресный опрос.
    """
    if log_cb:
        log_cb("scan_broadcast(): deprecated, используйте scan_all() (WB extended + стандартный опрос)")
    return None


def _broadcast_probe_bauds(
    port: str,
    speed_configs: List[SpeedConfig],
    log_verbose_cb: Optional[Callable[[str], None]] = None,
    log_ui_cb: Optional[Callable[[str], None]] = None,
    log_cb: Optional[Callable[[str], None]] = None,
    on_device_found: Optional[Callable[["DeviceInfo"], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    log_listen: bool = False,
    tcp_ep: Optional[Tuple[str, int]] = None,
    wb_trace_cb: Optional[Callable[[str], None]] = None,
    bootloader_only: bool = False,
) -> Tuple[List[SpeedConfig], Set[Tuple[int, int, str, int]], List["DeviceInfo"]]:
    """
    Быстрый скан: WB extended (0xFD 0x46 0x01) на каждый конфиг. По каждому найденному адресу — scan_address и on_device_found.
    log_verbose_cb — полный журнал (flasher_log.txt); log_ui_cb — краткие строки в окно.
    Если задан только log_cb — все сообщения идут в него (старое поведение).
    log_listen=True — детальное прослушивание только в verbose.
    wb_trace_cb — каждый TX/RX быстрого скана в журнал арбитража (файл wb_arbitration_trace.txt).
    Возвращает (responsive_configs, broadcast_seen, list_of_devices).
    """

    def v(msg: str) -> None:
        if log_verbose_cb:
            log_verbose_cb(msg)
        elif log_cb:
            log_cb(msg)

    def u(msg: str) -> None:
        if log_ui_cb:
            log_ui_cb(msg)
        elif log_cb:
            log_cb(msg)

    responsive: List[SpeedConfig] = []
    broadcast_seen: Set[Tuple[int, int, str, int]] = set()
    devices_found: List[DeviceInfo] = []
    timeout_ms = BROADCAST_COLLECT_TIMEOUT_MS
    for (baud, parity, stopbits) in speed_configs:
        if cancel_cb and cancel_cb():
            break
        cfg = f"{baud} {parity}{stopbits}"
        responses: List[Tuple[int, Optional[int], Optional[bytes]]] = []  # (addr, serial_from_wb_scan, payload)
        # Только WB extended scan (0xFD 0x46 0x01) — единый алгоритм как у Wiren Board; старый broadcast (255/0xFD read reg) убран.
        v(f"[{cfg}] WB extended: запрос 0xFD 0x46 0x01 (сканирование).")
        wb_scan = _wb_ext_scan(
            port, baud, parity, stopbits, timeout_ms,
            log_cb=v,
            log_listen=log_listen,
            tcp_ep=tcp_ep,
            cancel_cb=cancel_cb,
            wb_trace_cb=wb_trace_cb,
        )
        seen_wb_pair: Set[Tuple[int, int]] = set()
        if wb_scan:
            for addr, serial in wb_scan:
                pk = (addr, serial & 0xFFFFFFFF)
                if pk not in seen_wb_pair:
                    seen_wb_pair.add(pk)
                    responses.append((addr, serial, None))
            # Сортировка только для сообщения в лог; порядок опроса на шине — по арбитражу WB (serial28), не по адресу.
            addrs_wb = sorted({a for (a, _) in wb_scan})
            n_addr = len(addrs_wb)
            n_ans = len(wb_scan)
            v(f"[{cfg}] WB extended: найдены адреса {addrs_wb} ({n_ans} ответ(ов) скана).")
            if n_ans > n_addr:
                v(
                    f"[{cfg}] На линии несколько устройств с одним Modbus-адресом (разные серийные по WB-скану); "
                    f"в таблице — отдельная строка на каждую пару (адрес, SN)."
                )
            u(f"[{cfg}] быстрый поиск → адреса: {addrs_wb}")
        else:
            if tcp_ep is not None:
                u(f"[{cfg}] TCP: быстрый поиск 0xFD не выполняется (шлюз Modbus TCP)")
            else:
                u(f"[{cfg}] быстрый поиск — ответов нет")
        if responses:
            addrs = sorted({item[0] for item in responses})
            v(f"[{cfg}] WB extended: ответы от адресов {addrs}.")
        dup_modbus_addrs: Set[int] = set()
        if responses:
            ac: Dict[int, int] = {}
            for a, _, _ in responses:
                ac[a] = ac.get(a, 0) + 1
            dup_modbus_addrs = {a for a, n in ac.items() if n > 1}
            responses.sort(
                key=lambda it: (0x6 << 28) | ((it[1] or 0) & 0x0FFFFFFF)
            )
        for item in responses:
            if cancel_cb and cancel_cb():
                break
            addr = item[0]
            serial_from_wb = item[1] if len(item) > 1 else None
            broadcast_seen.add((addr, baud, parity, stopbits))
            # Сразу показываем в таблице: адрес и серийный (если есть), остальное дополним после опроса.
            _sn_row = (
                (serial_from_wb & 0xFFFFFFFF)
                if serial_from_wb is not None
                else None
            )
            placeholder = DeviceInfo(
                address=addr,
                baudrate=baud,
                parity=parity,
                stopbits=stopbits,
                signature="",
                app_version="—",
                bootloader_version="—",
                serial=serial_from_wb if serial_from_wb is not None else 0,
                in_bootloader=True,
                supports_fast_modbus=True,
                wb_scan_serial=_sn_row,
            )
            if on_device_found:
                on_device_found(placeholder)
            if bootloader_only:
                # В режиме bootloader — только WB fast scan + serial; без адресного опроса.
                # Дубликат одного Modbus-адреса (два 247): только 0xFD 0x46 по SN, с подробным журналом [DUP_ADDR].
                sn_ok = (
                    tcp_ep is None
                    and serial_from_wb is not None
                    and (serial_from_wb & 0xFFFFFFFF) not in (0, 0xFFFFFFFF)
                )
                if sn_ok and addr in dup_modbus_addrs:
                    _poll_identity_fast_modbus_for_dup_addr(
                        port, baud, parity, stopbits, serial_from_wb, placeholder, v
                    )
                elif sn_ok and (
                    _is_scan_info_missing(placeholder.signature)
                    or _is_scan_info_missing(placeholder.bootloader_version)
                    or _is_scan_info_missing(placeholder.app_version)
                ):
                    _fill_bootloader_info_by_serial(
                        port, baud, parity, stopbits, serial_from_wb, placeholder, v
                    )
                devices_found.append(placeholder)
                if on_device_found:
                    on_device_found(placeholder)
                v(f"[{cfg}] WB extended (bootloader-only): адрес {addr}, идентификация без адресного опроса.")
                u(f"[{cfg}] адр.{addr} — bootloader-only (WB/serial)")
                continue
            # Один Modbus-адрес у нескольких узлов: не вызывать опрос по slave — только Fast Modbus по SN.
            if (
                tcp_ep is None
                and addr in dup_modbus_addrs
                and serial_from_wb is not None
                and (serial_from_wb & 0xFFFFFFFF) not in (0, 0xFFFFFFFF)
            ):
                dev_dup = DeviceInfo(
                    address=addr,
                    baudrate=baud,
                    parity=parity,
                    stopbits=stopbits,
                    signature="",
                    app_version="—",
                    bootloader_version="—",
                    serial=serial_from_wb & 0xFFFFFFFF,
                    in_bootloader=True,
                    supports_fast_modbus=True,
                    wb_scan_serial=_sn_row,
                )
                _poll_identity_fast_modbus_for_dup_addr(
                    port, baud, parity, stopbits, serial_from_wb, dev_dup, v
                )
                devices_found.append(dev_dup)
                if on_device_found:
                    on_device_found(replace(dev_dup, supports_fast_modbus=True))
                ser_str = format_serial_for_display(dev_dup.serial)
                v(
                    f"[{cfg}] WB extended (DUP_ADDR, только Fast Modbus): адрес {addr} — серийный № {ser_str}, "
                    f"версия пр. {dev_dup.app_version}, версия загрузчика {dev_dup.bootloader_version}, "
                    f"сигнатура «{dev_dup.signature or '—'}»"
                )
                u(
                    f"[{cfg}] адр.{addr} — дубликат адреса, SN {ser_str}; подробности [DUP_ADDR] в flasher_log.txt"
                )
                continue
            # Сброс буфера перед опросом (остатки после WB scan могут мешать приёму ответа по обычному Modbus).
            if tcp_ep is None:
                try:
                    ser = open_port(port, baudrate=baud, parity=parity, stopbits=stopbits)
                    ser.reset_input_buffer()
                    ser.close()
                except Exception:
                    pass
            wb_hint: Optional[int] = None
            if serial_from_wb is not None and (serial_from_wb & 0xFFFFFFFF) not in (0, 0xFFFFFFFF):
                wb_hint = serial_from_wb

            def partial_cb(d: DeviceInfo) -> None:
                if on_device_found:
                    on_device_found(replace(d, supports_fast_modbus=True))

            dev = scan_address(
                port,
                addr,
                [(baud, parity, stopbits)],
                v,
                tcp_ep=tcp_ep,
                cancel_cb=cancel_cb,
                on_partial=partial_cb if on_device_found else None,
                wb_serial_hint=wb_hint,
                wb_scan_row_serial=serial_from_wb,
            )
            if dev is not None:
                # Серийный из регистров — основной источник; быстрый скан подставляем только если регистры пустые.
                # Иначе при битых CRC/арбитраже WB не перетираем корректное значение из 270–271.
                if serial_from_wb is not None and (serial_from_wb & 0xFFFFFFFF) not in (0, 0xFFFFFFFF):
                    wb_sn = serial_from_wb & 0xFFFFFFFF
                    rs_before = dev.serial & 0xFFFFFFFF
                    if rs_before in (0, 0xFFFFFFFF):
                        dev.serial = wb_sn
                    else:
                        reconciled = serial_reconcile_modbus_regs_with_wb(rs_before, wb_sn)
                        dev.serial = reconciled
                        if reconciled == wb_sn and rs_before != wb_sn:
                            if u32_swap_halfwords(rs_before) == wb_sn:
                                v(
                                    "[%s] адрес %d: рег. 270–271 дали 0x%08X; принят канонический 0x%08X "
                                    "(как WB-скан; типично — неверная склейка двух uint16 или порядок половин на шине)."
                                    % (cfg, addr, rs_before, wb_sn)
                                )
                        elif reconciled != wb_sn and rs_before != wb_sn:
                            v(
                                "[%s] адрес %d: WB 0x%08X не совпал с регистрами 0x%08X (и не как перестановка половин) — оставлено из регистров."
                                % (cfg, addr, wb_sn, rs_before)
                            )
                            u("[%s] адр.%d: расхождение SN скана и регистров — см. flasher_log.txt" % (cfg, addr))
                if tcp_ep is None and serial_from_wb is not None and (
                    _is_scan_info_missing(dev.signature)
                    or _is_scan_info_missing(dev.bootloader_version)
                    or _is_scan_info_missing(dev.app_version)
                ):
                    # Обычный Modbus не дал полей — 0xFD 0x46 0x08 по серийному из WB-скана (290/330, при необходимости 320–323).
                    _fill_bootloader_info_by_serial(
                        port, baud, parity, stopbits, serial_from_wb, dev, v
                    )
                dev = _apply_signature_from_serial_if_missing(dev)
                devices_found.append(dev)
                if on_device_found:
                    on_device_found(replace(dev, supports_fast_modbus=True))
                ser_str = format_serial_for_display(dev.serial)
                v(
                    f"[{cfg}] WB extended: адрес {addr} — серийный № {ser_str}, "
                    f"версия пр. {dev.app_version}, версия загрузчика {dev.bootloader_version}, сигнатура «{dev.signature or '—'}»"
                )
                u(f"[{cfg}] адр.{addr} опрошен, SN {ser_str}")
            else:
                if tcp_ep is None and serial_from_wb is not None and (
                    _is_scan_info_missing(placeholder.signature)
                    or _is_scan_info_missing(placeholder.bootloader_version)
                    or _is_scan_info_missing(placeholder.app_version)
                ):
                    _fill_bootloader_info_by_serial(
                        port, baud, parity, stopbits, serial_from_wb, placeholder, v
                    )
                devices_found.append(placeholder)
                if on_device_found:
                    on_device_found(placeholder)
                v(f"[{cfg}] WB extended: адрес {addr} (данные уточняются при опросе).")
                u(f"[{cfg}] адр.{addr} — только быстрый поиск, регистры не ответили")
        if responses:
            responsive.append((baud, parity, stopbits))
    return responsive, broadcast_seen, devices_found


# Допустимый диапазон адресов Modbus RTU (1–247)
SCAN_ADDR_MIN_DEFAULT = 1
SCAN_ADDR_MAX_DEFAULT = 10


def scan_all(
    port: str,
    progress_cb: Optional[Callable[..., None]] = None,
    log_cb: Optional[Callable[[str], None]] = None,
    log_verbose_cb: Optional[Callable[[str], None]] = None,
    log_ui_cb: Optional[Callable[[str], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    on_device_found: Optional[Callable[["DeviceInfo"], None]] = None,
    speed_configs: Optional[List[SpeedConfig]] = None,
    addr_min: int = SCAN_ADDR_MIN_DEFAULT,
    addr_max: int = SCAN_ADDR_MAX_DEFAULT,
    fast_scan: bool = True,
    log_listen: bool = False,
    scan_mode: ScanMode = ScanMode.STANDARD_ONLY,
    tcp_endpoint: Optional[Tuple[Any, ...]] = None,
    wb_trace_cb: Optional[Callable[[str], None]] = None,
    app_dir: Optional[Path] = None,
) -> List[DeviceInfo]:
    """Сканирование по режиму scan_mode; фаза 2 — последовательный опрос addr_min..addr_max.
    tcp_endpoint=(host, port) или (host, port, 'mbap') — Modbus TCP (MBAP); (host, port, 'rtu_tcp') — RTU по TCP без MBAP. Фаза 1 по 0xFD при любом TCP отключается.
    При фазе 1 по COM и заданном app_dir открывается wb_arbitration_trace.txt (или fallback TEMP/home);
    wb_trace_cb если задан — подменяет запись (иначе используется flasher_log.append_wb_trace)."""
    if speed_configs is None or len(speed_configs) == 0:
        speed_configs = _default_speed_configs()
    addr_min = max(1, min(addr_min, 247))
    addr_max = max(1, min(addr_max, 247))
    if addr_min > addr_max:
        addr_min, addr_max = addr_max, addr_min
    # Порядок по убыванию скорости
    baud_order = {b: i for i, b in enumerate(SCAN_BAUDRATES)}
    config_order = sorted(
        speed_configs,
        key=lambda c: (baud_order.get(c[0], 999), c[1], c[2]),
    )
    if scan_mode == ScanMode.BOOTLOADER_ONLY:
        # Бутлоадер: 115200 N1 (см. BOOTLOADER_BAUD / flash_protocol.BOOTLOADER_BAUDRATE).
        config_order = [c for c in config_order if int(c[0]) == BOOTLOADER_BAUD and str(c[1]).upper() == "N" and int(c[2]) == 1]
        if not config_order:
            config_order = [(BOOTLOADER_BAUD, "N", 1)]
    tcp_ep = tcp_endpoint
    addrs_order = list(range(addr_min, addr_max + 1))

    def v(msg: str) -> None:
        if log_verbose_cb:
            log_verbose_cb(msg)
        elif log_cb:
            log_cb(msg)

    def u(msg: str) -> None:
        if log_ui_cb:
            log_ui_cb(msg)
        elif log_cb:
            log_cb(msg)

    devices: List[DeviceInfo] = []
    seen_keys: Set[Tuple[int, int, str, int, int]] = set()
    num_addrs = len(addrs_order)
    total = len(config_order) * num_addrs
    cfg_summary = ", ".join(f"{b} {p}{s}" for (b, p, s) in config_order)
    v(f"Сканирование: порядок по скорости {[f'{b} {p}{s}' for (b, p, s) in config_order]}.")
    if tcp_ep:
        _h, _p, _m = tcp_endpoint_host_port_mode(tcp_ep)
        link_desc = (
            f"RTU/TCP {_h}:{_p}" if _m == "rtu_tcp" else f"Modbus TCP {_h}:{_p}"
        )
    else:
        link_desc = port
    u(f"Поиск устройств, {link_desc}; линии: {cfg_summary}; режим: {scan_mode.value}")

    run_phase1 = (
        fast_scan
        and tcp_ep is None
        and scan_mode in (ScanMode.EXTENDED_ONLY, ScanMode.BOOTLOADER_ONLY)
    )
    if scan_mode == ScanMode.STANDARD_ONLY:
        run_phase1 = False
    # В режиме bootloader используем только WB extended (0xFD 0x46):
    # не опрашиваем диапазон адресов 1..N, чтобы не цеплять устройства в приложении.
    run_phase2 = scan_mode == ScanMode.STANDARD_ONLY
    if not run_phase1:
        flasher_log.close_wb_trace()

    if run_phase1:
        v("Фаза 1. Быстрое сканирование (0xFD 0x46 0x01 по выбранным скоростям и параметрам связи).")
        u("Фаза 1: быстрый поиск (WB 0xFD 0x46)…")
        wb_file_cb: Optional[Callable[[str], None]] = wb_trace_cb
        if tcp_ep is None and app_dir is not None:
            p = flasher_log.init_wb_trace(app_dir)
            if p is not None:
                v("Детальный журнал арбитража WB (все TX/RX): %s" % p)
                u("Полный лог арбитража WB → см. файл выше или строки [WB_ARB] в flasher_log.txt.")
            else:
                v(
                    "Не удалось создать wb_arbitration_trace.txt (нет прав?) — трассировка WB только в flasher_log.txt с префиксом [WB_ARB]."
                )
            if wb_file_cb is None:
                wb_file_cb = flasher_log.append_wb_trace
        for cfg in config_order:
            if cancel_cb and cancel_cb():
                break
            _, _, devices_from_wb = _broadcast_probe_bauds(
                port,
                [cfg],
                log_verbose_cb=log_verbose_cb,
                log_ui_cb=log_ui_cb,
                log_cb=log_cb,
                on_device_found=on_device_found,
                cancel_cb=cancel_cb,
                log_listen=log_listen,
                tcp_ep=tcp_ep,
                wb_trace_cb=wb_file_cb,
                bootloader_only=(scan_mode == ScanMode.BOOTLOADER_ONLY),
            )
            for d in devices_from_wb:
                k = device_table_key(d)
                if k not in seen_keys:
                    d1 = replace(d, supports_fast_modbus=True)
                    devices.append(d1)
                    seen_keys.add(k)
                    if on_device_found:
                        on_device_found(d1)
            if scan_mode == ScanMode.BOOTLOADER_ONLY and any(d.in_bootloader for d in devices_from_wb):
                v("Режим bootloader: найдено устройство(а) в загрузчике, дальнейшие скорости пропускаем.")
                break
        v("Фаза 1: только устройства с поддержкой 0xFD 0x46. Устройства WB (только стандартный Modbus) будут в фазе 2.")
        u("Фаза 1 завершена. Далее — опрос Modbus по адресам.")
    elif tcp_ep is not None and scan_mode != ScanMode.STANDARD_ONLY:
        u("TCP: фаза 1 (0xFD) пропущена — используйте опрос по адресам.")

    if not run_phase2:
        if scan_mode == ScanMode.EXTENDED_ONLY:
            u("Режим «только extended» — фаза 2 не выполняется.")
        elif scan_mode == ScanMode.BOOTLOADER_ONLY:
            u("Режим «только bootloader» — фаза 2 (опрос адресов) отключена.")
        out = list(devices)
        if scan_mode == ScanMode.BOOTLOADER_ONLY:
            out = [d for d in out if d.in_bootloader]
        out.sort(
            key=lambda d: (
                d.baudrate,
                d.parity,
                d.stopbits,
                wb_arb_sort_key(d),
                d.address,
            )
        )
        return out

    v(f"Фаза 2: опрос адресов {addrs_order[0]}..{addrs_order[-1]} (по порядку).")
    u(f"Фаза 2: опрос {num_addrs} адрес(ов)…")
    for config_idx, (baud, parity, stopbits) in enumerate(config_order):
        if cancel_cb and cancel_cb():
            break
        cfg = (baud, parity, stopbits)
        for idx, addr in enumerate(addrs_order):
            if cancel_cb and cancel_cb():
                break
            done = config_idx * num_addrs + idx + 1
            if progress_cb:
                progress_cb(done, total, addr, cfg)

            def _current_cb(a: int, b: int, p: str, s: int, _step=idx) -> None:
                if progress_cb:
                    progress_cb(config_idx * num_addrs + _step + 1, total, a, (b, p, s))

            n_same_addr = sum(1 for d in devices if d.address == addr)
            if n_same_addr > 1:
                v(
                    f"Фаза 2: адрес {addr} пропущен — на линии несколько устройств с этим Modbus-адресом "
                    f"(опрос выполнен в фазе 1 по WB-скану)."
                )
                _sleep_interruptible(
                    _phase2_gap_between_addresses_s(baud, parity, stopbits),
                    cancel_cb,
                    step=0.02,
                )
                continue

            dev = scan_address(
                port,
                addr,
                [cfg],
                v,
                current_cb=_current_cb,
                tcp_ep=tcp_ep,
                cancel_cb=cancel_cb,
                on_partial=on_device_found,
            )
            if dev is not None:
                k2 = device_table_key(dev)
                if k2 not in seen_keys:
                    devices.append(dev)
                    seen_keys.add(k2)
                    if on_device_found:
                        on_device_found(dev)
                    ser_str = format_serial_for_display(dev.serial)
                    v(
                        f"Modbus (сканирование): адрес {addr}, {dev.baudrate} бод; "
                        f"серийный № {ser_str}, версия пр. {dev.app_version}, версия загрузчика {dev.bootloader_version}"
                    )
                    u(
                        f"  адр.{addr} @ {dev.baudrate} {parity}{stopbits} — SN {ser_str}"
                    )
            _sleep_interruptible(
                _phase2_gap_between_addresses_s(baud, parity, stopbits),
                cancel_cb,
                step=0.02,
            )

    devices.sort(
        key=lambda d: (
            d.baudrate,
            d.parity,
            d.stopbits,
            wb_arb_sort_key(d),
            d.address,
        )
    )
    if scan_mode == ScanMode.BOOTLOADER_ONLY:
        devices = [d for d in devices if d.in_bootloader]
    return devices
