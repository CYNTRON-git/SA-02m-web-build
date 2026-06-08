# -*- coding: utf-8 -*-
"""
Связка JobManager + модулей MR-02m-flasher (scanner.scan_all / flash_protocol.run_flash_sequence*).

Все опасные операции оборачиваются в port_lease (останавливаем mplc*), блокируются через flock
на /var/lock/sa02m-flasher-<port>.lock, и корректно обрабатывают отмену через threading.Event.
"""
from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import flash_protocol as fp
from . import modbus_rtu
from . import scanner as scn
from .config import FlasherConfig
from .firmware_repo import FirmwareEntry, FirmwareRepo
from .jobs import Job
from .module_profiles import device_allowed_for_mr_firmware_flash
from .modbus_io import uint32_from_modbus_reg_pair_be
from .mplc_lease import port_lease, PortBusyError, device_path_exists
from .scanner import REG_SERIAL_LO
from .serial_port import open_port, send_receive, send_receive_wb_ext_scan

log = logging.getLogger(__name__)


def _device_to_dict(dev: scn.DeviceInfo) -> Dict[str, Any]:
    d = asdict(dev)
    d["serial_hex"] = f"0x{int(dev.serial) & 0xFFFFFFFF:08X}"
    d["serial_dec"] = str(int(dev.serial) & 0xFFFFFFFF)
    if dev.wb_scan_serial is not None:
        d["wb_scan_serial_hex"] = f"0x{int(dev.wb_scan_serial) & 0xFFFFFFFF:08X}"
    return d


def _port_lock_path(cfg: FlasherConfig, port_key: str) -> Path:
    return cfg.lock_dir / f"sa02m-flasher-{port_key}.lock"


@contextlib.contextmanager
def _port_flock(cfg: FlasherConfig, port_key: str):
    """flock(LOCK_EX|LOCK_NB) — защита от двух одновременных job'ов на одном порту в разных процессах."""
    path = _port_lock_path(cfg, port_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o664)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"Порт {port_key} занят другим процессом (flock {path}).")
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def resolve_device_path(cfg: FlasherConfig, port_key: str) -> str:
    path = cfg.ports_map.get(port_key)
    if not path:
        raise ValueError(f"Неизвестный COM-порт: {port_key}")
    if not device_path_exists(path):
        raise FileNotFoundError(f"Устройство {path} недоступно")
    return path


# ─── Scan ──────────────────────────────────────────────────────────────────────


def _scan_mode_from_str(value: str) -> scn.ScanMode:
    v = (value or "").strip().lower()
    if v in ("fast", "extended", "быстрый"):
        return scn.ScanMode.EXTENDED_ONLY
    if v in ("bootloader", "bl", "bootloader_only"):
        return scn.ScanMode.BOOTLOADER_ONLY
    return scn.ScanMode.STANDARD_ONLY


def _build_speed_configs(
    baudrates: Optional[Iterable[int]],
    parity: str,
    stopbits: int,
) -> Optional[List[scn.SpeedConfig]]:
    if not baudrates:
        return None
    pr = (parity or "N").upper()
    if pr not in ("N", "E", "O"):
        pr = "N"
    sb = 2 if int(stopbits or 1) == 2 else 1
    return [(int(b), pr, sb) for b in baudrates if int(b) > 0]


def run_scan_job(job: Job, ctx: Dict[str, Any], cfg: FlasherConfig) -> None:
    """Обработчик задачи сканирования. Вызывается из JobManager._worker."""
    params = job.params
    port_key = str(params.get("port") or job.port)
    mode = _scan_mode_from_str(str(params.get("mode") or "standard"))
    baudrates = params.get("baudrates") or []
    parity = str(params.get("parity") or "N")
    stopbits = int(params.get("stopbits") or 1)
    addr_min = int(params.get("addr_min") or 1)
    addr_max = int(params.get("addr_max") or 247)
    timing_profile = str(params.get("timing_profile") or "standard").strip().lower()
    speed_configs = _build_speed_configs(baudrates, parity, stopbits)

    device_path = resolve_device_path(cfg, port_key)
    log_cb = ctx["log"]
    progress_cb = ctx["progress"]
    device_found_cb = ctx["device_found"]
    cancel_evt = ctx["cancel_evt"]

    log_cb(f"Скан {port_key} ({device_path}), {mode.value}", "info")
    progress_cb(0, "Подготовка порта")

    with port_lease(device_path, cfg.mplc_stop_services):
        with _port_flock(cfg, port_key):

            def sc_log(msg: str) -> None:
                log_cb(msg, "debug")

            def sc_log_ui(msg: str) -> None:
                log_cb(msg, "info")

            def sc_cancel() -> bool:
                return cancel_evt.is_set()

            addr_span = max(1, addr_max - addr_min + 1)

            def sc_progress(current_addr: int = 0, *_rest, **_kw) -> None:
                try:
                    val = int((max(0, int(current_addr) - addr_min) / addr_span) * 100)
                except Exception:
                    val = 0
                progress_cb(val, f"Опрос адреса {int(current_addr)}")

            def sc_found(dev: scn.DeviceInfo) -> None:
                device_found_cb(_device_to_dict(dev))

            devices = scn.scan_all(
                port=device_path,
                progress_cb=sc_progress,
                log_cb=sc_log_ui,
                log_verbose_cb=sc_log,
                log_ui_cb=sc_log_ui,
                cancel_cb=sc_cancel,
                on_device_found=sc_found,
                speed_configs=speed_configs,
                addr_min=addr_min,
                addr_max=addr_max,
                fast_scan=True,
                scan_mode=mode,
                timing_profile=("aggressive" if timing_profile == "aggressive" else "standard"),
            )

    # Финальный снэпшот: перезаписать список устройств из результата scan_all (упорядочено).
    job.devices = [_device_to_dict(d) for d in devices]
    progress_cb(100, f"Сканирование завершено. Найдено {len(devices)} устройств.")
    log_cb(f"Найдено устройств: {len(devices)}", "info")


# ─── Flash ─────────────────────────────────────────────────────────────────────


def _load_firmware_for_flash(repo: FirmwareRepo, params: Dict[str, Any]) -> Tuple[bytes, str, str, FirmwareEntry]:
    """
    Вернуть (image_bytes, signature_from_file, version, entry) для параметров задачи.
    params:
        firmware_channel — канал ('stable'/'beta'/'local')
        firmware_file    — имя файла
        download_if_missing — True (по умолчанию): скачать через manifest, если нет локально
    """
    channel = str(params.get("firmware_channel") or "stable")
    file_name = str(params.get("firmware_file") or "").strip()
    if not file_name:
        raise ValueError("Не указан файл прошивки (firmware_file)")

    entry = repo.get(channel, file_name) or repo.get("local", file_name) or repo.get("stable", file_name)
    if entry is None:
        raise FileNotFoundError(f"Прошивка {channel}/{file_name} не найдена в репозитории")
    from .firmware_repo import is_flasher_supported_entry

    if not is_flasher_supported_entry(entry):
        raise ValueError(
            f"Прошивка {entry.file} не поддерживается прошивальщиком "
            "(полные дампы *_full_*, .elf и устаревшие образы исключены из репозитория)."
        )

    path = repo.path_for(entry)
    if path is None:
        if not params.get("download_if_missing", True):
            raise FileNotFoundError(f"Файл {entry.file} не скачан")
        path = repo.download(entry)

    from . import firmware as fw_parser
    image, _size, version, signature = fw_parser.load_firmware(path)
    return image, signature, version, entry


def _make_flasher(
    device_path: str,
    baudrate: int,
    parity: str,
    stopbits: int,
    cancel_evt,
    log_cb: Callable[[str, str], None],
    timeout_ms: int = fp.FLASH_ENTER_BOOTLOADER_APP_TIMEOUT_MS,
) -> Tuple[fp.FlasherProtocol, Any]:
    """
    Создать FlasherProtocol поверх серийного порта на заданной скорости.
    Возвращает (flasher, serial_obj) — serial_obj нужно закрыть вручную.
    """
    ser = open_port(device_path, baudrate=baudrate, parity=parity, stopbits=stopbits)

    def sr(request: bytes):
        if cancel_evt.is_set():
            return None
        return send_receive(ser, request, response_timeout_ms=timeout_ms, cancel_check=cancel_evt.is_set)

    flasher = fp.FlasherProtocol(sr, timeout_ms=timeout_ms, log_cb=lambda m: log_cb(m, "debug"))
    return flasher, ser

def _prime_wb_fast_modbus_line(
    ser: Any,
    cancel_check: Optional[Callable[[], bool]],
    response_timeout_ms: int = 120,
) -> None:
    """
    Перед первым 0xFD 0x46 0x08: кадр 0xFD 0x46 0x04 (конец WB-скана) — см. MR-02m-flasher gui_flasher_support.
    """
    try:
        if cancel_check and cancel_check():
            return
        send_receive(
            ser,
            modbus_rtu.build_wb_ext_scan_end(),
            response_timeout_ms=response_timeout_ms,
            cancel_check=cancel_check,
        )
    except Exception:
        pass
    try:
        ser.reset_input_buffer()
    except OSError:
        pass
    time.sleep(0.03)


def _bootloader_send_receive_profiled(
    ser: Any,
    cancel_evt,
    *,
    is_wb_firmware: bool = False,
) -> Callable[[bytes], Optional[bytes]]:
    """
    Таймаут приёма ответа строка в строку с
    MR-02m-flasher/gui_flasher_flash_mixin._send_recv_profiled.
    """

    def sr(request: bytes) -> Optional[bytes]:
        if cancel_evt.is_set():
            return None
        chk = cancel_evt.is_set
        to_ms = fp.bootloader_profiled_response_timeout_ms(
            request, is_wb_firmware=is_wb_firmware,
        )
        return send_receive(ser, request, response_timeout_ms=to_ms, cancel_check=chk)

    return sr


def _device_line_key(device: Dict[str, Any]) -> Tuple[int, int, str, int]:
    return (
        int(device.get("address") or 0),
        int(device.get("baudrate") or 0),
        str(device.get("parity") or "N").upper(),
        int(device.get("stopbits") or 1),
    )


def _duplicate_modbus_address_on_line(
    device: Dict[str, Any],
    peers: Optional[List[Dict[str, Any]]],
) -> bool:
    """То же, что MR _duplicate_modbus_address_on_link: явный флаг или ≥2 строк с одинаковой «линией» приложения."""
    if device.get("duplicate_modbus_address_on_line") is True:
        return True
    if not peers or len(peers) < 2:
        return False
    k = _device_line_key(device)
    return sum(1 for d in peers if _device_line_key(d) == k) > 1


def _serial_valid_fast_modbus_u32(serial: int) -> bool:
    """Диапазон SN для 0x46 (MR serial_ranges.is_valid_device_serial_u32)."""
    u = int(serial) & 0xFFFFFFFF
    return u not in (0, 0xFFFFFFFF) and (u & 0xFFFF0000) == 0x0E0A0000


def _open_bootloader_serial(device_path: str, *, is_wb_firmware: bool) -> Any:
    baud = fp.BOOTLOADER_BAUDRATE_WB if is_wb_firmware else fp.BOOTLOADER_BAUDRATE
    stop = int(fp.BOOTLOADER_STOPBITS_WB if is_wb_firmware else fp.BOOTLOADER_STOPBITS)
    return open_port(
        device_path,
        baudrate=baud,
        parity=fp.BOOTLOADER_PARITY,
        stopbits=stop,
    )


def _make_bootloader_flasher_protocol(
    ser: Any,
    cancel_evt,
    log_cb: Callable[[str, str], None],
    *,
    is_wb_firmware: bool,
) -> fp.FlasherProtocol:
    sr = _bootloader_send_receive_profiled(ser, cancel_evt, is_wb_firmware=is_wb_firmware)
    return fp.FlasherProtocol(
        sr,
        timeout_ms=fp.BOOTLOADER_DATA_BLOCK_TIMEOUT_MS,
        log_cb=lambda m: log_cb(m, "debug"),
    )


def _wait_bootloader_ready_on_serial(
    ser: Any,
    cancel_evt,
    log_cb: Callable[[str, str], None],
    *,
    prime_fast: bool,
    probe_serial: Optional[int],
    probe_addr: Optional[int],
    deadline_s: float = 1.4,
) -> Tuple[bool, Optional[str]]:
    """
    MR gui_flasher_flash_mixin._wait_bootloader_ready_after_reset на уже открытом COM линии загрузчика.
    """
    if prime_fast:
        log_cb(
            "WB 0xFD 0x46 0x04: перед опросом reg 290 при быстром Modbus",
            "debug",
        )
        _prime_wb_fast_modbus_line(ser, cancel_evt.is_set)

    probe_timeout_ms = 180

    def sr(request: bytes):
        if cancel_evt.is_set():
            return None
        return send_receive(
            ser,
            request,
            response_timeout_ms=probe_timeout_ms,
            cancel_check=cancel_evt.is_set,
        )

    fl_probe = fp.FlasherProtocol(
        sr,
        timeout_ms=probe_timeout_ms,
        log_cb=lambda m: log_cb(m, "debug"),
    )
    last_err: Optional[str] = None
    deadline = time.perf_counter() + max(0.1, float(deadline_s))
    while time.perf_counter() < deadline:
        if cancel_evt.is_set():
            return False, last_err
        if probe_serial is not None:
            pl, err = fl_probe.read_holding_registers_by_serial(
                probe_serial, fp.REG_SIGNATURE, 1
            )
            last_err = err or last_err
            if err is None and pl and len(pl) >= 2:
                return True, None
        if probe_addr is not None:
            pl, err = fl_probe.read_holding_registers(
                probe_addr, fp.REG_SIGNATURE, 1
            )
            last_err = err or last_err
            if err is None and pl and len(pl) >= 2:
                return True, None
        time.sleep(0.08)
    if last_err:
        log_cb(f"Загрузчик: за время ожидания — {last_err}", "debug")
    return False, last_err


def _enter_bootloader_from_application_line(
    device_path: str,
    device: Dict[str, Any],
    cancel_evt,
    log_cb: Callable[[str, str], None],
    *,
    is_wb_firmware: bool,
) -> Optional[str]:
    """MR: reg 129 с линии приложения (закрыть порт)."""
    if device.get("in_bootloader"):
        return None
    baud = int(device.get("baudrate") or 0) or 19200
    parity = str(device.get("parity") or "N").upper() or "N"
    stopbits = int(device.get("stopbits") or 2) or 2
    addr = int(device.get("address") or fp.BOOTLOADER_DEFAULT_ADDR)

    log_cb(f"Перевод адр.{addr} в bootloader (app baud {baud} {parity}{stopbits})", "info")
    flasher, ser = _make_flasher(
        device_path,
        baud,
        parity,
        stopbits,
        cancel_evt,
        log_cb,
    )
    try:
        err = flasher.enter_bootloader_wb(addr)
        if not is_wb_firmware and err:
            log_cb(f"enter_bootloader_wb 0x10(slave={addr}) → {err}", "warn")
            err2 = flasher.enter_bootloader(addr)
            if err2:
                log_cb(f"enter_bootloader 0x06(slave={addr}) → {err2}", "warn")
                err = err2
            else:
                err = None
        elif not err:
            log_cb("Запись reg 129 через 0x10 (Write Multiple): OK", "info")
            err = None
        if err and "Таймаут" not in err:
            return (
                f"Не удалось перевести устройство в загрузчик (reg 129): {err}. "
                "Оба способа записи отклонены, это не ожидаемый таймаут при сбросе."
            )
        if err:
            log_cb(
                "Таймаут записи reg 129 — возможен сброс в загрузчик; продолжаем",
                "info",
            )
        return None
    except Exception as exc:
        log_cb(f"enter_bootloader исключение: {exc}", "error")
        return str(exc)
    finally:
        try:
            ser.close()
        except Exception:
            pass


def _post_reset_delays_before_bootloader_open(
    log_cb: Callable[[str, str], None],
    *,
    is_wb_firmware: bool,
) -> None:
    log_cb("Ожидание 1 с после перезагрузки.", "debug")
    time.sleep(1.0)
    if is_wb_firmware:
        log_cb(
            "Wiren Board: ещё 1.5 с до 9600 8N2 (загрузчик готов).",
            "info",
        )
        time.sleep(1.5)


def _preflight_read_bootloader_info_by_serial_mr(
    flasher: fp.FlasherProtocol,
    ser: Any,
    *,
    bootloader_serial: int,
    duplicate_on_line: bool,
    cancel_evt,
    log_cb: Callable[[str, str], None],
) -> Tuple[Optional[str], Optional[str]]:
    """
    MR после «Обмен по серийному»: при дубликате — 3 с + 0x04; затем read 290/330 по serial + до 2 повторов.
    Возвращает (сигнатура для info или None, текст ошибки).
    """
    if duplicate_on_line:
        log_cb(
            "Несколько модулей на одном Modbus-адресе: пауза 3 с перед опросом загрузчика (арбитраж шины).",
            "info",
        )
        time.sleep(3.0)
        _prime_wb_fast_modbus_line(ser, cancel_evt.is_set)

    time.sleep(max(fp.FAST_MODBUS_INTER_READ_GAP_S, 0.05))
    log_cb("Запрос информации загрузчика (рег. 290, 330) по серийному.", "info")

    sig, bl_ver, err = flasher.read_bootloader_info_by_serial(bootloader_serial)
    if err:
        for _retry in range(2):
            log_cb(
                "Повтор запроса информации загрузчика (после перезагрузки или таймаута).",
                "warn",
            )
            time.sleep(2.0)
            _prime_wb_fast_modbus_line(ser, cancel_evt.is_set)
            sig, bl_ver, err = flasher.read_bootloader_info_by_serial(bootloader_serial)
            if not err:
                break
    if err:
        return None, (
            "Загрузчик не отвечает по серийному 0x%08X (0xFD 0x46). Проверьте режим bootloader (%d 8N1)."
            % (bootloader_serial, fp.BOOTLOADER_BAUDRATE)
        )
    log_cb(
        "Данные загрузчика с устройства: сигнатура %s, версия загрузчика %s."
        % (sig or "—", bl_ver or "—"),
        "info",
    )
    cleaned = (sig or "").strip()[:12] if sig and str(sig).strip() else None
    return cleaned, None


def _resolve_bootloader_serial_mr_style(
    flasher: fp.FlasherProtocol,
    ser: Any,
    device: Dict[str, Any],
    cancel_evt,
    log_cb: Callable[[str, str], None],
    *,
    use_fast_modbus: bool,
    is_wb_firmware: bool,
    recovery: bool,
) -> Tuple[int, Optional[str]]:
    if is_wb_firmware or not use_fast_modbus:
        return int(device.get("serial") or 0) & 0xFFFFFFFF, None

    bootloader_serial = int(device.get("serial") or 0) & 0xFFFFFFFF
    in_bl = bool(device.get("in_bootloader"))

    if (in_bl or recovery) and use_fast_modbus:
        _slave = (
            int(device["address"])
            if (device.get("address") and 1 <= int(device["address"]) <= 247)
            else int(fp.BOOTLOADER_DEFAULT_ADDR)
        )
        pl_sn, err_sn = flasher.read_holding_registers(_slave, REG_SERIAL_LO, 2)
        if not err_sn and pl_sn and len(pl_sn) >= 4:
            sn247 = uint32_from_modbus_reg_pair_be(pl_sn, 0)
            if _serial_valid_fast_modbus_u32(sn247):
                if sn247 != bootloader_serial:
                    log_cb(
                        "Загрузчик: фактический SN (рег.%d @ адр.%d)=0x%08X; в таблице 0x%08X — используем фактический."
                        % (REG_SERIAL_LO, _slave, sn247, bootloader_serial),
                        "info",
                    )
                bootloader_serial = sn247
        return bootloader_serial, None

    if not (in_bl or recovery):
        wtag = "%s%s%s" % (
            int(device.get("baudrate") or 0),
            str(device.get("parity") or "N"),
            int(device.get("stopbits") or 1),
        )
        scan_result = send_receive_wb_ext_scan(
            ser,
            response_timeout_ms=1200,
            silence_ms=50,
            log_cb=lambda m: log_cb(m, "debug"),
            cancel_check=cancel_evt.is_set,
            wb_trace_tag=wtag,
        )
        if scan_result:
            bl_candidates = [
                (a, s) for a, s in scan_result if a == fp.BOOTLOADER_DEFAULT_ADDR
            ]
            match_serial = next(
                (s for _a, s in bl_candidates if s == bootloader_serial), None
            )
            if match_serial is not None:
                bootloader_serial = match_serial
                if len(bl_candidates) > 1:
                    log_cb(
                        "На адресе %d в загрузчике ответило несколько модулей; выбран целевой SN 0x%08X "
                        "(быстрый Modbus по серийному)."
                        % (fp.BOOTLOADER_DEFAULT_ADDR, bootloader_serial),
                        "info",
                    )
            elif len(bl_candidates) == 1:
                other_sn = bl_candidates[0][1]
                return 0, (
                    "В режиме загрузчика (адрес %d) другое устройство 0x%08X, целевой 0x%08X не перешёл в bootloader."
                    % (fp.BOOTLOADER_DEFAULT_ADDR, other_sn, bootloader_serial)
                )
            elif len(bl_candidates) >= 2:
                found_sns = ", ".join("0x%08X" % s for _a, s in bl_candidates)
                return 0, (
                    "Целевой серийный 0x%08X не найден среди модулей в загрузчике на адресе %d. Ответили на скан: %s"
                    % (bootloader_serial, fp.BOOTLOADER_DEFAULT_ADDR, found_sns)
                )
            else:
                log_cb(
                    "WB-скан: ни одно устройство не отвечает на адресе %d (bootloader). "
                    "Пробуем опрос по серийному 0x%08X."
                    % (fp.BOOTLOADER_DEFAULT_ADDR, bootloader_serial),
                    "debug",
                )

    if not bootloader_serial or bootloader_serial == 0xFFFFFFFF:
        return 0, (
            "Для обмена с загрузчиком по 0xFD 0x46 нужен серийный номер. Выполните сканирование и выберите устройство "
            "по серийному №."
        )
    return bootloader_serial, None


def _run_bootloader_flash_session(
    device_path: str,
    device: Dict[str, Any],
    cancel_evt,
    log_cb: Callable[[str, str], None],
    progress_cb: Callable[[int, str], None],
    *,
    use_fast_modbus: bool,
    is_wb_firmware: bool,
    is_bootloader_firmware: bool,
    duplicate_on_line: bool,
    recovery: bool,
    image: bytes,
    file_signature: str,
    force_unlisted_signature: bool,
) -> Optional[str]:
    """
    Одна сессия: перевод из приложения (при необходимости) → один COM на скорости загрузчика
    → wait на том же ser (MR) → второй prime при duplicate+fast → FlasherProtocol → прошивка.
    """
    if not device.get("in_bootloader"):
        fatal = _enter_bootloader_from_application_line(
            device_path, device, cancel_evt, log_cb, is_wb_firmware=is_wb_firmware
        )
        if fatal:
            return fatal
        _post_reset_delays_before_bootloader_open(log_cb, is_wb_firmware=is_wb_firmware)
    else:
        if recovery:
            log_cb("Режим восстановления: устройство уже в загрузчике.", "info")
        else:
            log_cb(
                "Устройство уже в режиме загрузчика, открываем линию загрузчика %d бод."
                % (fp.BOOTLOADER_BAUDRATE_WB if is_wb_firmware else fp.BOOTLOADER_BAUDRATE),
                "info",
            )

    ser: Optional[Any] = None
    try:
        ser = _open_bootloader_serial(device_path, is_wb_firmware=is_wb_firmware)
        if is_wb_firmware:
            baud = fp.BOOTLOADER_BAUDRATE_WB
            stop = int(fp.BOOTLOADER_STOPBITS_WB)
            log_cb(
                "Режим Wiren Board (.wbfw): загрузчик %d 8%c%d."
                % (baud, fp.BOOTLOADER_PARITY, stop),
                "info",
            )
            log_cb("Wiren Board: пауза 1.5 с после открытия порта загрузчика.", "debug")
            time.sleep(1.5)

        sn_pf = int(device.get("serial") or 0) & 0xFFFFFFFF
        probe_serial = (
            sn_pf
            if (
                use_fast_modbus
                and sn_pf not in (0, 0xFFFFFFFF)
                and not is_wb_firmware
            )
            else None
        )
        # После входа в bootloader (reg 129) устройство всегда отвечает на адресе 247.
        # addr приложения используется только для самого входа (enter_bootloader_wb/enter_bootloader).
        addr_probe = fp.BOOTLOADER_DEFAULT_ADDR
        probe_addr = None if probe_serial is not None else addr_probe
        prime_for_wait = bool(probe_serial and duplicate_on_line)

        if not is_wb_firmware:
            ready, wait_err = _wait_bootloader_ready_on_serial(
                ser,
                cancel_evt,
                log_cb,
                prime_fast=prime_for_wait,
                probe_serial=probe_serial,
                probe_addr=probe_addr,
            )
            if ready:
                log_cb("Загрузчик ответил без дополнительной длинной паузы.", "debug")
            else:
                log_cb(
                    "Быстрый опрос после reset ответа не дал; продолжаем штатную инициализацию.",
                    "debug",
                )

            wr = wait_err or ""
            if (
                not ready
                and wr
                and ("код 1" in wr or "код 01" in wr.lower())
            ):
                log_cb(
                    "Повтор входа в загрузчик — при проверке готовности ответ «код 1» (ещё приложение).",
                    "warn",
                )
                try:
                    ser.close()
                except OSError:
                    pass
                err_re = _enter_bootloader_from_application_line(
                    device_path,
                    device,
                    cancel_evt,
                    log_cb,
                    is_wb_firmware=is_wb_firmware,
                )
                if err_re:
                    return err_re
                _post_reset_delays_before_bootloader_open(
                    log_cb, is_wb_firmware=is_wb_firmware
                )
                ser = _open_bootloader_serial(device_path, is_wb_firmware=is_wb_firmware)
                if is_wb_firmware:
                    time.sleep(1.5)

                ready, _ = _wait_bootloader_ready_on_serial(
                    ser,
                    cancel_evt,
                    log_cb,
                    prime_fast=prime_for_wait,
                    probe_serial=probe_serial,
                    probe_addr=probe_addr,
                )
                if ready:
                    log_cb("Загрузчик ответил без дополнительной длинной паузы.", "debug")
                else:
                    log_cb(
                        "Быстрый опрос после reset ответа не дал; продолжаем штатную инициализацию.",
                        "debug",
                    )
        else:
            time.sleep(1.5)

        log_cb(
            "Скорость загрузчика: %d бод, стоп-биты: %s"
            % (
                fp.BOOTLOADER_BAUDRATE_WB if is_wb_firmware else fp.BOOTLOADER_BAUDRATE,
                fp.BOOTLOADER_STOPBITS_WB if is_wb_firmware else fp.BOOTLOADER_STOPBITS,
            ),
            "debug",
        )

        if use_fast_modbus and not is_wb_firmware and duplicate_on_line:
            log_cb(
                "Прогрев шины (0xFD 0x46 0x04) перед быстрым Modbus — устраняет таймаут первого 0x08 после долгого открытия COM.",
                "debug",
            )
            _prime_wb_fast_modbus_line(ser, cancel_evt.is_set)

        flasher = _make_bootloader_flasher_protocol(
            ser, cancel_evt, log_cb, is_wb_firmware=is_wb_firmware
        )

        return _flash_one_device(
            flasher,
            device,
            image,
            file_signature,
            ser=ser,
            duplicate_on_line=duplicate_on_line,
            use_fast_modbus=use_fast_modbus,
            force_unlisted_signature=force_unlisted_signature,
            is_wb_firmware=is_wb_firmware,
            is_bootloader_firmware=is_bootloader_firmware,
            recovery=recovery,
            cancel_evt=cancel_evt,
            log_cb=log_cb,
            progress_cb=progress_cb,
        )
    finally:
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass


def _flash_one_device(
    flasher: fp.FlasherProtocol,
    device: Dict[str, Any],
    image: bytes,
    file_signature: str,
    *,
    ser: Any,
    duplicate_on_line: bool,
    use_fast_modbus: bool,
    force_unlisted_signature: bool,
    is_wb_firmware: bool = False,
    is_bootloader_firmware: bool = False,
    recovery: bool = False,
    cancel_evt,
    log_cb: Callable[[str, str], None],
    progress_cb: Callable[[int, str], None],
) -> Optional[str]:
    addr = int(device.get("address") or fp.BOOTLOADER_DEFAULT_ADDR)
    serial = int(device.get("serial") or 0) & 0xFFFFFFFF
    dev_sig = str(device.get("signature") or "").strip()

    # Bootloader всегда отвечает на адресе 247 (BOOTLOADER_DEFAULT_ADDR),
    # независимо от Modbus-адреса приложения.
    boot_addr_for_address_path = fp.BOOTLOADER_DEFAULT_ADDR

    if not is_wb_firmware and not is_bootloader_firmware:
        if not device_allowed_for_mr_firmware_flash(
            dev_sig, allow_unlisted=force_unlisted_signature
        ):
            return (
                f"Сигнатура «{dev_sig}» не распознана как модуль расширения MR/MP-02м. "
                "Прошивка отменена. Для лабораторных случаев включите опцию «Разрешить устройство вне списка сигнатур»."
            )

    info_sig = (dev_sig if dev_sig and dev_sig.upper() != "NONE" else file_signature) or fp.DEFAULT_SIGNATURE
    flash_cancel_gate = fp.FlashCancelGate(lambda: cancel_evt.is_set())

    def prog(sent: int, total: int) -> None:
        total = max(1, int(total))
        sent = max(0, int(sent))
        pct = min(100, int(sent * 100 / total))
        progress_cb(pct, f"Блок {sent}/{total}")

    if is_wb_firmware:
        log_cb(f"Прошивка Wiren Board (.wbfw) по адресу {addr}", "info")
        err = fp.run_flash_sequence_wb(
            flasher,
            addr,
            image,
            progress_cb=prog,
            cancel_cb=cancel_evt.is_set,
            cancel_gate=flash_cancel_gate,
        )
        if err:
            return err
        log_cb("Команда на устройство: переход в приложение (запись рег. 1004).", "info")
        try:
            werr = flasher.write_multiple_registers(addr, fp.REG_JUMP_APP, [1])
        except Exception as exc:
            return str(exc)
        return werr

    use_by_address = (not use_fast_modbus) and addr and 1 <= addr <= 247

    if use_by_address:
        log_cb(
            "Прошивка по Modbus-адресу %d (%d бод, обычный Modbus)."
            % (boot_addr_for_address_path, fp.BOOTLOADER_BAUDRATE),
            "info",
        )
        if is_bootloader_firmware:
            err = fp.run_flash_bootloader_sequence_by_address(
                flasher,
                boot_addr_for_address_path,
                image,
                info_sig,
                progress_cb=prog,
                cancel_cb=cancel_evt.is_set,
                cancel_gate=flash_cancel_gate,
            )
        else:
            err = fp.run_flash_sequence_by_address(
                flasher,
                boot_addr_for_address_path,
                image,
                info_sig,
                progress_cb=prog,
                cancel_cb=cancel_evt.is_set,
                cancel_gate=flash_cancel_gate,
            )
        if err:
            return err
        if is_bootloader_firmware:
            log_cb("Bootloader записан; устройство выполняет commit и перезагрузку.", "info")
            return None
        log_cb("Команда на устройство: переход в приложение (рег. 1004).", "info")
        try:
            return flasher.jump_to_app(boot_addr_for_address_path)
        except Exception as exc:
            log_cb(f"jump_to_app исключение: {exc}", "warn")
            return str(exc)

    # Быстрый Modbus: MR — WB scan / SN из 270 → лог → duplicate 3 с → read bootloader info → прошивка
    bs, ferr = _resolve_bootloader_serial_mr_style(
        flasher,
        ser,
        device,
        cancel_evt,
        log_cb,
        use_fast_modbus=use_fast_modbus,
        is_wb_firmware=is_wb_firmware,
        recovery=recovery,
    )
    if ferr:
        return ferr
    serial = bs & 0xFFFFFFFF

    log_cb(
        "Обмен с загрузчиком по серийному номеру (0xFD 0x46 0x08), серийный 0x%08X." % serial,
        "info",
    )
    bs_from_bl, ierr = _preflight_read_bootloader_info_by_serial_mr(
        flasher,
        ser,
        bootloader_serial=serial,
        duplicate_on_line=duplicate_on_line,
        cancel_evt=cancel_evt,
        log_cb=log_cb,
    )
    if ierr:
        return ierr
    if bs_from_bl:
        info_sig = bs_from_bl
    time.sleep(max(fp.FAST_MODBUS_INTER_READ_GAP_S, 0.05))

    if is_bootloader_firmware:
        log_cb("Режим прошивки bootloader (34 КБ, 0x46 по серийному).", "info")
        err = fp.run_flash_sequence_bootloader(
            flasher,
            serial,
            image,
            info_sig,
            progress_cb=prog,
            cancel_cb=cancel_evt.is_set,
            cancel_gate=flash_cancel_gate,
        )
        if err:
            return err
        log_cb("Образ bootloader записан; устройство выполняет commit и перезагрузку.", "info")
        return None

    log_cb(f"Прошивка приложения по серийному 0x{serial:08X} (быстрый Modbus)", "info")
    err = fp.run_flash_sequence(
        flasher,
        serial,
        image,
        info_sig,
        progress_cb=prog,
        cancel_cb=cancel_evt.is_set,
        cancel_gate=flash_cancel_gate,
    )
    if err:
        return err

    log_cb("Команда на устройство: переход в приложение (рег. 1004).", "info")
    try:
        err_j = flasher.jump_to_app_by_serial(serial)
    except Exception as exc:
        log_cb(f"jump_to_app исключение: {exc}", "warn")
        err_j = str(exc)
    if err_j:
        log_cb(
            "Ответ на jump (таймаут часто нормален — устройство уже перезагружается): %s" % err_j,
            "warn",
        )
    return None


def run_flash_job(job: Job, ctx: Dict[str, Any], cfg: FlasherConfig, repo: FirmwareRepo) -> None:
    """
    Задача прошивки одного устройства.

    params:
        port               — ключ COM (COM1..COM5)
        target             — {'address': int} или {'serial': int} (из таблицы)
        use_fast_modbus    — bool
        firmware_channel   — канал
        firmware_file      — имя файла
        force_signature_mismatch / force_unlisted_signature — обход whitelist сигнатур MR/MP-02м (только отладка)
    """
    params = job.params
    port_key = str(params.get("port") or job.port)
    target = params.get("target") or {}
    use_fast = bool(params.get("use_fast_modbus"))
    force_unlisted = bool(
        params.get("force_unlisted_signature", params.get("force_signature_mismatch"))
    )

    log_cb = ctx["log"]
    progress_cb = ctx["progress"]
    cancel_evt = ctx["cancel_evt"]

    device_path = resolve_device_path(cfg, port_key)
    image, file_sig, file_ver, entry = _load_firmware_for_flash(repo, params)
    is_wb = str(entry.file).lower().endswith(".wbfw")
    log_cb(f"Файл: {entry.file} sig={file_sig} ver={file_ver} size={len(image)}", "info")
    progress_cb(1, "Открытие порта")

    with port_lease(device_path, cfg.mplc_stop_services):
        with _port_flock(cfg, port_key):
            peers_raw = params.get("devices_on_port")
            peers: Optional[List[Dict[str, Any]]] = peers_raw if isinstance(peers_raw, list) else None
            dup = _duplicate_modbus_address_on_line(target, peers)
            is_bl_fw = (entry.kind or "").lower() == "bootloader"
            recovery = bool(params.get("recovery"))
            err = _run_bootloader_flash_session(
                device_path,
                target,
                cancel_evt,
                log_cb,
                progress_cb,
                use_fast_modbus=use_fast,
                is_wb_firmware=is_wb,
                is_bootloader_firmware=is_bl_fw,
                duplicate_on_line=dup,
                recovery=recovery,
                image=image,
                file_signature=file_sig,
                force_unlisted_signature=force_unlisted,
            )
            if err:
                raise RuntimeError(err)
    progress_cb(100, "Готово")


def run_flash_batch_job(job: Job, ctx: Dict[str, Any], cfg: FlasherConfig, repo: FirmwareRepo) -> None:
    """
    Пакетная прошивка нескольких устройств на одном COM.

    params.targets — список dict: {address, serial, signature, in_bootloader, ...}
    params.firmware_* — одна прошивка на всю партию; допуск только для сигнатур MR/MP-02м (или force_*).
    """
    params = job.params
    port_key = str(params.get("port") or job.port)
    targets: List[Dict[str, Any]] = list(params.get("targets") or [])
    use_fast = bool(params.get("use_fast_modbus", True))
    force_unlisted = bool(
        params.get("force_unlisted_signature", params.get("force_signature_mismatch"))
    )
    skip_on_error = bool(params.get("skip_on_error", True))

    if not targets:
        raise ValueError("Список устройств для пакетной прошивки пуст")

    log_cb = ctx["log"]
    progress_cb = ctx["progress"]
    cancel_evt = ctx["cancel_evt"]

    device_path = resolve_device_path(cfg, port_key)
    image, file_sig, file_ver, entry = _load_firmware_for_flash(repo, params)
    is_wb = str(entry.file).lower().endswith(".wbfw")
    log_cb(f"Пакет: {len(targets)} устройств, файл {entry.file} sig={file_sig} ver={file_ver}", "info")

    with port_lease(device_path, cfg.mplc_stop_services):
        with _port_flock(cfg, port_key):
            errors: List[Tuple[Dict[str, Any], str]] = []
            total = len(targets)
            is_bl_fw = (entry.kind or "").lower() == "bootloader"
            for i, dev in enumerate(targets):
                if cancel_evt.is_set():
                    log_cb("Отмена пакетной прошивки", "warn")
                    break
                log_cb(f"[{i+1}/{total}] Прошивка устройства {dev.get('address')} sn=0x{int(dev.get('serial') or 0):08X}", "info")

                def sub_progress(pct: int, message: str) -> None:
                    overall = int((i + pct / 100.0) * 100 / total)
                    progress_cb(overall, f"[{i+1}/{total}] {message}")

                dup = _duplicate_modbus_address_on_line(dev, targets)
                recovery = bool(dev.get("recovery") or params.get("recovery"))
                err = _run_bootloader_flash_session(
                    device_path,
                    dev,
                    cancel_evt,
                    log_cb,
                    sub_progress,
                    use_fast_modbus=use_fast,
                    is_wb_firmware=is_wb,
                    is_bootloader_firmware=is_bl_fw,
                    duplicate_on_line=dup,
                    recovery=recovery,
                    image=image,
                    file_signature=file_sig,
                    force_unlisted_signature=force_unlisted,
                )
                if err:
                    errors.append((dev, err))
                    log_cb(f"Ошибка: {err}", "error")
                    if not skip_on_error:
                        raise RuntimeError(err)
            if errors:
                log_cb(f"Завершено с ошибками: {len(errors)} из {total}", "warn")
                # Запишем ошибки в параметры, чтобы UI показал; статус job — DONE, но с сообщением.
                job.params["errors"] = [
                    {"address": d.get("address"), "serial": d.get("serial"), "error": e}
                    for d, e in errors
                ]
    progress_cb(100, "Пакетная прошивка завершена")
