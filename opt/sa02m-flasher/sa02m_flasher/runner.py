# -*- coding: utf-8 -*-
"""
Связка JobManager + модулей MR-02m-flasher (scanner.scan_all / flash_protocol.run_flash_sequence*).

Все опасные операции оборачиваются в port_lease (останавливаем mplc*), блокируются через flock
на /var/lock/sa02m-flasher-<port>.lock, и корректно обрабатывают отмену через threading.Event.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import bacnet_mstp
from . import bus_mode
from . import device_config
from . import flash_protocol as fp
from . import modbus_rtu
from . import module_profiles as mp
from . import scanner as scn
from .config import FlasherConfig
from .firmware_repo import FirmwareEntry, FirmwareRepo, load_for_flash
from .jobs import Job, failed_targets_text
from .module_profiles import (
    PROFILE_MP_MR,
    application_line_profile,
    device_flash_route,
    line_profile_family,
    validate_firmware_device_route,
)
from .modbus_io import (
    decode_signature_from_holding_290_payload,
    serial_reconcile_modbus_regs_with_wb,
    uint32_from_modbus_reg_pair_be,
)
from .mplc_lease import port_lease, PortBusyError, device_path_exists, is_port_poll_free
from .scanner import REG_SERIAL_LO
from .serial_port import open_port, send_receive, send_receive_wb_ext_scan

log = logging.getLogger(__name__)

# Inter-device settle in the batch-flash path: a just-flashed MR module reboots
# into its new app (~2 s per the MR-02m bus-protocol contract) and is transiently
# at the bootloader's fixed addr 247. Wait for it to vacate the shared line before
# the NEXT device's enter_bootloader + info-block frames, or the two collide
# (bench .135: mid-batch cascade). Overridable per-job via inter_device_settle_s.
BATCH_INTER_DEVICE_SETTLE_S = 2.5
BATCH_INTER_DEVICE_SETTLE_MAX_S = 30.0


def _flash_cancel_gate(cancel_evt, on_irreversible: Optional[Callable[[], None]] = None) -> fp.FlashCancelGate:
    return fp.FlashCancelGate(
        lambda: cancel_evt.is_set(),
        on_irreversible=on_irreversible,
    )


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


def _model_name_from_signature(signature: str) -> str:
    """Display model name for a scanned signature, reusing module_profiles only
    (no new register map — the MR-02m map is owned in the firmware repo).
    Falls back to the cleaned signature string when the type is not resolvable."""
    sig = signature or ""
    code = mp.code_from_signature(sig)
    if code is not None:
        name = mp.MP02_TYPE_NAMES.get(code)
        if name:
            return name
    stripped = mp.strip_bootloader_signature_suffix(sig).strip()
    return stripped


def _classify_scanned_device(dev: scn.DeviceInfo) -> Dict[str, Any]:
    """One scan roster row: addr, signature, model, ours(bool). Classification
    reuses module_profiles (is_mp_module_signature_for_batch_flash / code_from_signature)."""
    sig = dev.signature or ""
    ours = mp.is_mp_module_signature_for_batch_flash(sig) or mp.is_meter_driver_alias(sig)
    return {
        "addr": int(dev.address),
        "signature": mp.strip_bootloader_signature_suffix(sig).strip(),
        "model": _model_name_from_signature(sig) if ours else "",
        "ours": bool(ours),
    }


def persist_scan_result(cfg: FlasherConfig, port_key: str, devices: Iterable[scn.DeviceInfo]) -> None:
    """Write the classified scan roster to <scan_cache_dir>/<port_key>.json (atomic).

    Provider B source for the bus-free RS-485 roster aggregator. Last scan wins
    per port (overwrite). No request value reaches the path (port_key is a fixed
    COM key from config); the file is written with the stdlib JSON writer."""
    # port_key is already validated against ports_map (resolve_device_path); sanitise
    # anyway so no separator can ever reach the path (allow-list, defence in depth).
    safe_key = "".join(ch for ch in str(port_key) if ch.isalnum() or ch in ("-", "_"))
    if not safe_key:
        return
    scan_dir = Path(cfg.scan_cache_dir)
    scan_dir.mkdir(parents=True, exist_ok=True)
    rows = [_classify_scanned_device(d) for d in devices]
    payload = {
        "port": str(port_key),
        "ts": int(time.time()),
        "devices": rows,
    }
    path = scan_dir / f"{safe_key}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


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
    if mode == scn.ScanMode.EXTENDED_ONLY:
        if is_port_poll_free(device_path):
            first_baud = int(speed_configs[0][0]) if speed_configs else 0
            progress_cb(0, f"Поиск на {first_baud}" if first_baud else "Поиск")
        else:
            progress_cb(0, "Подготовка порта")
    elif is_port_poll_free(device_path):
        first_baud = int(speed_configs[0][0]) if speed_configs else 0
        progress_cb(
            0,
            f"Адрес {addr_min}, {first_baud}" if first_baud else f"Адрес {addr_min}",
            address=addr_min,
            baudrate=first_baud or None,
        )
    else:
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

            def _scan_step_pct(done: int, total: int) -> int:
                try:
                    return int((int(done) / max(1, int(total))) * 100)
                except Exception:
                    return 0

            def _scan_progress_message(addr: int, baud: int) -> str:
                if baud:
                    return f"Адрес {addr}, {baud}"
                return f"Адрес {addr}"

            def sc_progress(*args, **_kw) -> None:
                if len(args) >= 2 and isinstance(args[1], str):
                    progress_cb(int(args[0]), args[1])
                    return
                if len(args) >= 4:
                    done, total, addr = int(args[0]), int(args[1]), int(args[2])
                    cfg = args[3]
                    baud = int(cfg[0]) if cfg else 0
                    progress_cb(
                        _scan_step_pct(done, total),
                        _scan_progress_message(addr, baud),
                        address=addr,
                        baudrate=baud or None,
                        step=done,
                        step_total=total,
                    )
                    return
                if len(args) >= 3:
                    done, total, addr = int(args[0]), int(args[1]), int(args[2])
                    first_baud = int(speed_configs[0][0]) if speed_configs else 0
                    progress_cb(
                        _scan_step_pct(done, total),
                        _scan_progress_message(addr, first_baud),
                        address=addr,
                        baudrate=first_baud or None,
                        step=done,
                        step_total=total,
                    )
                    return
                if len(args) >= 1:
                    try:
                        val = int((max(0, int(args[0]) - addr_min) / addr_span) * 100)
                    except Exception:
                        val = 0
                    first_baud = int(speed_configs[0][0]) if speed_configs else 0
                    addr = int(args[0])
                    progress_cb(
                        val,
                        _scan_progress_message(addr, first_baud),
                        address=addr,
                        baudrate=first_baud or None,
                    )

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
    # Persist the classified roster for the bus-free RS-485 aggregator (Provider B).
    # Pure file write — no port access — so it is safe after the lease/flock released.
    try:
        persist_scan_result(cfg, port_key, devices)
    except Exception as exc:  # noqa: BLE001 — persistence must never fail a scan
        log.debug("persist_scan_result %s: %s", port_key, exc)
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
            raise FileNotFoundError(
                f"Файл {entry.file} не скачан в кеш шлюза. "
                "Нажмите «Скачать прошивки», выберите другой образ или загрузите .fw вручную."
            )
        path = repo.ensure_local_path(entry, allow_download=True)

    image, signature, version = load_for_flash(entry, path)
    return image, signature, version, entry


def _application_line_params(
    device: Dict[str, Any],
    *,
    is_wb_firmware: bool,
) -> Tuple[int, str, int]:
    """
    Скорость/формат для обмена с приложением (reg 129 → bootloader).
    Профиль по сигнатуре устройства (module_profiles.application_line_profile), не blind scan baud.
    """
    sig = str(device.get("signature") or "")
    profile = application_line_profile(sig, device=device, is_wb_firmware=is_wb_firmware)
    return profile.as_tuple()


def _scan_line_tuple(device: Dict[str, Any]) -> Optional[Tuple[int, str, int]]:
    """Параметры линии, на которых СКАН реально получил ответ устройства (None — скан их не дал)."""
    try:
        baud = int(device.get("baudrate") or 0)
    except (TypeError, ValueError):
        return None
    if baud <= 0:
        return None
    parity = str(device.get("parity") or "N").upper()
    if parity not in ("N", "E", "O"):
        parity = "N"
    try:
        stop = int(device.get("stopbits") or 1)
    except (TypeError, ValueError):
        stop = 1
    return baud, parity, (2 if stop == 2 else 1)


def _application_line_candidates(
    device: Dict[str, Any],
    *,
    is_wb_firmware: bool,
) -> List[Tuple[int, str, int]]:
    """
    Линии, на которых пробуют заговорить с приложением (порядок = порядок попыток).

    Первая — как раньше (явные app_line_* цели, иначе профиль по сигнатуре). Для MR/MP/CE/DTV
    (не .wbfw) затем — линия, на которой скан нашёл устройство (FL-01: MR на 19200 недостижим на
    константе 115200), и профиль 115200 N1 — на случай ложного baud в скане. WB: как раньше,
    одна линия (скан или 19200 N2).
    """
    first = _application_line_params(device, is_wb_firmware=is_wb_firmware)
    out: List[Tuple[int, str, int]] = [first]
    if not is_wb_firmware:
        scan = _scan_line_tuple(device)
        for cand in (scan, PROFILE_MP_MR.as_tuple()):
            if cand and cand not in out:
                out.append(cand)
    return out


# Префикс ошибки «устройство не ответило» — по нему вход в загрузчик пробует следующую линию (FL-01/FL-03).
IDENTITY_NO_ANSWER = "Нет ответа устройства"


def _is_no_answer(err: Optional[str]) -> bool:
    return bool(err) and str(err).startswith(IDENTITY_NO_ANSWER)


def _firmware_signature_for_log(
    file_sig: str,
    entry: FirmwareEntry,
    device: Optional[Dict[str, Any]] = None,
) -> str:
    """Сигнатура для строки лога: из .fw, иначе manifest, иначе из скана устройства."""
    fs = str(file_sig or "").strip()
    if fs and fs.upper() != "NONE":
        return fs
    for sig in entry.signatures or []:
        s = str(sig).strip()
        if s and s.upper() != "NONE":
            return s
    if device:
        ds = str(device.get("signature") or "").strip()
        if ds and ds.upper() != "NONE":
            return ds
    return fs or "NONE"


def _firmware_signature_label(
    file_sig: str,
    entry: FirmwareEntry,
    device: Optional[Dict[str, Any]] = None,
) -> str:
    """Подпись для строки лога «файл … sig=…»: сигнатура ФАЙЛА (из .fw или манифеста).

    Если у файла её нет, сигнатура цели не выдаётся за файловую (FL-04: «файл ce-02m-3… sig=14DI»):
    пишем, что в файле её нет, и отдельно — что прочитано у устройства.
    """
    own = _firmware_signature_for_log(file_sig, entry, None)
    if own and own.upper() != "NONE":
        return own
    ds = str((device or {}).get("signature") or "").strip()
    return f"нет в файле (устройство: {ds})" if ds and ds.upper() != "NONE" else "нет в файле"


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


def _resolve_use_fast_modbus(
    requested: bool,
    *,
    duplicate_on_line: bool,
    is_wb_firmware: bool,
) -> bool:
    """По умолчанию обычный Modbus; при дубликате адреса на линии — быстрый 0xFD 0x46 по серийному."""
    if is_wb_firmware:
        return False
    if duplicate_on_line:
        return True
    return bool(requested)


def _open_bootloader_serial(
    device_path: str,
    *,
    is_wb_firmware: bool,
    line: Optional[Tuple[int, str, int]] = None,
) -> Any:
    """Порт на линии загрузчика. `line` (baud, parity, stopbits) — линия, найденная поиском (D36b-01);
    без неё — как раньше: константа 115200 8N1 (WB: 9600 8N2, `line` для WB не задаётся)."""
    if line is not None and not is_wb_firmware:
        return open_port(device_path, baudrate=line[0], parity=line[1], stopbits=line[2])
    baud = fp.BOOTLOADER_BAUDRATE_WB if is_wb_firmware else fp.BOOTLOADER_BAUDRATE
    stop = int(fp.BOOTLOADER_STOPBITS_WB if is_wb_firmware else fp.BOOTLOADER_STOPBITS)
    return open_port(
        device_path,
        baudrate=baud,
        parity=fp.BOOTLOADER_PARITY,
        stopbits=stop,
    )


# D36b-01 (bench 2026-11-12): after reg 129 the bootloader is NOT always on the constant 115200. The rule lives
# in the MR-02m repo, one home per fact: doc/05_BOOTLOADER.md §5 «Параметры линии (один дом)», the contract
# docs/contracts/bootloader-modbus.md «Listen parameters», shared/bootloader/Core/Inc/bl_link_params.h. In short:
# a final BL >= 0.0.0.35 listens on the APPLICATION's baud, parity and stop bits; older loaders, the «pre-final»
# 0.0.0.35 and the family image (CE-02m-3, RTU-Sensor/DTV, RGBW) listen on 115200 8N1 — the version number alone
# does not tell the variant, so the loader is looked for on the application's line first and then on 115200 8N1,
# for every family (MR team's recommendation). The probe is read-only and ends before any block is written.
BOOTLOADER_FIXED_LINE = (fp.BOOTLOADER_BAUDRATE, fp.BOOTLOADER_PARITY, int(fp.BOOTLOADER_STOPBITS))
# Rounds of the bounded search: the module resets after reg 129, so the first look can be too early.
BOOTLOADER_SEARCH_ROUNDS = 2


def _line_text(line: Tuple[int, str, int]) -> str:
    return f"{line[0]} {line[1]}{line[2]}"


def _bootloader_line_candidates(
    device: Dict[str, Any],
    entered_line: Optional[Tuple[int, str, int]],
) -> List[Tuple[int, str, int]]:
    """Линии для поиска загрузчика (порядок = порядок попыток): линия приложения, затем 115200 8N1.

    Модуль, найденный сканом уже в загрузчике (`in_bootloader`), — линия скана (там он ответил), затем 115200 8N1.
    """
    first = _scan_line_tuple(device) if device.get("in_bootloader") else entered_line
    out: List[Tuple[int, str, int]] = []
    if first:
        out.append(first)
    if BOOTLOADER_FIXED_LINE not in out:
        out.append(BOOTLOADER_FIXED_LINE)
    return out


def _open_bootloader_on_candidates(
    device_path: str,
    candidates: List[Tuple[int, str, int]],
    cancel_evt,
    log_cb: Callable[[str, str], None],
    *,
    prime_fast: bool,
    probe_serial: Optional[int],
    probe_addr: Optional[int],
    confirm: Optional[Callable[[Any], Tuple[Any, Optional[str]]]] = None,
) -> Tuple[Any, Tuple[int, str, int], bool, Optional[str], Optional[Tuple[Any, Optional[str]]]]:
    """Открыть порт на линии, где отвечает ЦЕЛЕВОЙ загрузчик: (ser, линия, готов, ошибка, итог проверки личности).

    Только чтение рег. 290 по 247 (или по серийному) и — через `confirm` — чтение серийного номера и сигнатуры:
    ни одного записывающего кадра. Ответ «код 1» — это ещё приложение: порт остаётся на этой линии, решает
    вызывающий. Если на линии отвечает НЕ целевое устройство (другой модуль в загрузчике, приложение по адресу
    247), `confirm` возвращает ошибку личности — поиск идёт дальше по следующей линии (G-2): целевой модуль
    может сидеть в загрузчике на 115200, пока на линии приложения кто-то другой отвечает на 247. Не нашли нигде —
    порт на первой линии; итог проверки личности — (None, текст последнего отказа) или None, если никто не отвечал.
    Порт закрывается на любом выходе, в том числе при исключении (G-4).
    """
    ser: Any = None
    last_err: Optional[str] = None
    foreign: Optional[str] = None
    rounds = BOOTLOADER_SEARCH_ROUNDS if len(candidates) > 1 else 1
    for rnd in range(rounds):
        for idx, line in enumerate(candidates):
            if cancel_evt.is_set():
                break
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
            ser = _open_bootloader_serial(device_path, is_wb_firmware=False, line=line)
            try:
                ready, err = _wait_bootloader_ready_on_serial(
                    ser, cancel_evt, log_cb,
                    prime_fast=prime_fast, probe_serial=probe_serial, probe_addr=probe_addr,
                )
                ident: Optional[Tuple[Any, Optional[str]]] = None
                if ready and confirm is not None:
                    ident = confirm(ser)
                    if ident[1] is not None:
                        foreign = ident[1]
                        log_cb(
                            f"На линии {_line_text(line)} отвечает не целевой загрузчик: {ident[1]}",
                            "warn",
                        )
                        ready = False
                        err = None
            except BaseException:
                try:
                    ser.close()
                except Exception:
                    pass
                raise
            if ready:
                log_cb(f"Загрузчик найден на линии {_line_text(line)}.", "info")
                return ser, line, True, None, ident
            last_err = err or last_err
            if err and ("код 1" in err or "код 01" in err.lower()):
                return ser, line, False, err, None
            nxt = candidates[idx + 1] if idx + 1 < len(candidates) else (candidates[0] if rnd + 1 < rounds else None)
            if nxt is not None and not cancel_evt.is_set():
                log_cb(f"Загрузчик не ответил на {_line_text(line)}; пробую линию {_line_text(nxt)}", "warn")
    first = candidates[0]
    # Park on the first candidate; with nobody answering, the identity read that follows reports the final «no answer».
    if ser is not None:
        try:
            ser.close()
        except Exception:
            pass
    ser = _open_bootloader_serial(device_path, is_wb_firmware=False, line=first)
    return ser, first, False, last_err, ((None, foreign) if foreign else None)


def _bootloader_not_found_text(lines: List[Tuple[int, str, int]]) -> str:
    """D36b-02: reg 129 was accepted, the bootloader answers nowhere — the module may still be in it.

    The way back is the bootloader's own: a magic entry (reg 129) without a following flash ends by idle timeout,
    BL_IDLE_TIMEOUT_MS = 120 s, and jumps to a valid application (MR-02m shared/bootloader:
    Core/Inc/bootloader_config.h, Core/Inc/bl_boot_decision.h bl_idle_jump_due; the MR team REPORTED that bootloader
    0.0.0.40 of the bench module returned by itself after ~120 s — the bench report itself did not observe it).
    Nothing is written or retried here.
    """
    return (
        f"Загрузчик не найден на линиях {', '.join(_line_text(l) for l in lines)} после успешной записи "
        "рег. 129 (вход в загрузчик): модуль мог остаться в загрузчике. Образ не записывался. "
        "По исходникам загрузчика MR-02м он сам возвращается в приложение примерно через 120 с без обращений к нему; "
        "можно также выключить и включить питание модуля. Проверьте скорость и чётность линии."
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


FlashIdentity = Tuple[int, str]


def _confirm_flash_identity(
    flasher: fp.FlasherProtocol, device: Dict[str, Any], address: int, use_fast: bool,
    *, image_family: Optional[str], prior: Optional[FlashIdentity] = None,
) -> Tuple[Optional[FlashIdentity], Optional[str]]:
    """Read identity on the same transport as the write. Prior is local to this reset."""
    selected_sn = int(device.get("serial") or 0) & 0xFFFFFFFF
    family = mp.manifest_device_for_signature(str(device.get("signature") or ""))
    if not family or family != image_family:
        return None, "Требуется опознание семейства устройства по регистру 290."
    if use_fast:
        if selected_sn in (0, 0xFFFFFFFF):
            return None, "Не определён серийный номер устройства."
        read = flasher.read_holding_registers_by_serial
        target = selected_sn
    else:
        read = flasher.read_holding_registers
        target = address
    sn_data, sn_err = read(target, REG_SERIAL_LO, 2)
    if sn_err:
        # FL-03: no answer / a damaged answer is NOT a serial mismatch. The caller
        # (_enter_bootloader_from_application_line) retries another line on «no answer».
        if fp._fp_is_timeout_err(sn_err):
            return None, f"{IDENTITY_NO_ANSWER} (таймаут чтения серийного номера)."
        return None, f"Не удалось прочитать серийный номер устройства: {sn_err}."
    actual_sn = (uint32_from_modbus_reg_pair_be(sn_data)
                 if not sn_err and sn_data and len(sn_data) == 4 else 0)
    if use_fast and not sn_err:
        # The 0x46 reader verified the response envelope's serial and CRC.
        actual_sn = serial_reconcile_modbus_regs_with_wb(actual_sn, selected_sn)
    if selected_sn not in (0, 0xFFFFFFFF) and actual_sn != selected_sn:
        return None, "Серийный номер устройства не совпал с выбранной целью."
    if prior and prior[0] not in (0, 0xFFFFFFFF) and actual_sn != prior[0]:
        return None, "Серийный номер загрузчика не совпал с номером приложения."
    sig_data, sig_err = read(target, fp.REG_SIGNATURE, fp.REG_SIGNATURE_COUNT)
    if sig_err or not sig_data or len(sig_data) != 2 * fp.REG_SIGNATURE_COUNT:
        return None, "Не удалось прочитать сигнатуру устройства по регистру 290."
    signature = decode_signature_from_holding_290_payload(sig_data)
    actual_family = mp.manifest_device_for_signature(signature)
    identity = (actual_sn, family)
    if actual_family == image_family:
        return identity, None
    if (mp.firmware_identity_missing(signature) and actual_sn not in (0, 0xFFFFFFFF)
            and prior == identity):
        return identity, None
    return None, f"Регистр 290 («{signature or '—'}») не подтвердил семейство {family}."


def _enter_bootloader_from_application_line(
    device_path: str,
    device: Dict[str, Any],
    cancel_evt,
    log_cb: Callable[[str, str], None],
    *,
    is_wb_firmware: bool,
    use_fast_modbus: bool,
    image_family: Optional[str],
    entered: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[FlashIdentity], Optional[str]]:
    """MR: reg 129 с линии приложения (закрыть порт).

    `entered` (необязательный словарь): при успехе получает ключ "line" — линию приложения
    (baud, parity, stopbits), на которой принят reg 129; загрузчик ищут с неё (D36b-01).

    Линия берётся не из константы: первой идёт линия цели/профиля, затем та, на которой скан
    нашёл устройство, затем профиль 115200 N1. Следующая пробуется только если на предыдущей
    устройство НЕ ОТВЕТИЛО на чтение серийного номера (записей до подтверждения личности нет).
    """
    if device.get("in_bootloader"):
        return None, None
    addr = int(device.get("address") or fp.BOOTLOADER_DEFAULT_ADDR)
    candidates = _application_line_candidates(device, is_wb_firmware=is_wb_firmware)
    tried: List[str] = []
    for idx, (baud, parity, stopbits) in enumerate(candidates):
        tried.append(f"{baud} {parity}{stopbits}")
        verified, err = _enter_bootloader_on_line(
            device_path, device, cancel_evt, log_cb, baud, parity, stopbits,
            is_wb_firmware=is_wb_firmware, use_fast_modbus=use_fast_modbus, image_family=image_family,
        )
        if not _is_no_answer(err):
            if entered is not None and not err:
                entered["line"] = (baud, parity, stopbits)
            return verified, err
        if idx + 1 < len(candidates) and not cancel_evt.is_set():
            nb, npar, nst = candidates[idx + 1]
            log_cb(
                f"Адр.{addr} не ответил на {baud} {parity}{stopbits}; пробую линию {nb} {npar}{nst}",
                "warn",
            )
    return None, (
        f"{IDENTITY_NO_ANSWER} адр.{addr}: таймаут чтения серийного номера на линиях "
        f"{', '.join(tried)}. Прошивка не начата, запись не отправлялась. "
        "Проверьте скорость линии модуля и адрес."
    )


def _enter_bootloader_on_line(
    device_path: str,
    device: Dict[str, Any],
    cancel_evt,
    log_cb: Callable[[str, str], None],
    baud: int,
    parity: str,
    stopbits: int,
    *,
    is_wb_firmware: bool,
    use_fast_modbus: bool,
    image_family: Optional[str],
) -> Tuple[Optional[FlashIdentity], Optional[str]]:
    """Одна попытка входа в загрузчик на заданной линии приложения."""
    addr = int(device.get("address") or fp.BOOTLOADER_DEFAULT_ADDR)
    family = line_profile_family(str(device.get("signature") or ""), is_wb_firmware=is_wb_firmware)
    scan = _scan_line_tuple(device)
    if scan and scan != (baud, parity, stopbits):
        log_cb(
            f"Профиль {family}: скан {scan[0]} {scan[1]}{scan[2]} "
            f"→ для reg 129 пробуем {baud} {parity}{stopbits}",
            "debug",
        )

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
        verified = None
        if not is_wb_firmware:
            verified, identity_err = _confirm_flash_identity(
                flasher, device, addr, use_fast_modbus, image_family=image_family)
            if identity_err:
                return None, identity_err
        if use_fast_modbus and not is_wb_firmware:
            err = flasher.write_multiple_registers_by_serial(int(device["serial"]), fp.REG_ENTER_BOOTLOADER, [1])
        else:
            err = flasher.enter_bootloader_wb(addr)
        if not is_wb_firmware and not use_fast_modbus and err:
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
            return None, (
                f"Не удалось перевести устройство в загрузчик (reg 129): {err}. "
                "Запись отклонена."
            )
        if err:
            log_cb(
                "Таймаут записи reg 129 — возможен сброс в загрузчик; продолжаем",
                "info",
            )
        return verified, None
    except Exception as exc:
        log_cb(f"enter_bootloader исключение: {exc}", "error")
        return None, str(exc)
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
            ".wbfw: ещё 1.5 с до 9600 8N2 (загрузчик готов).",
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
            "Загрузчик не отвечает по серийному 0x%08X (0xFD 0x46). Проверьте режим bootloader и параметры линии."
            % bootloader_serial
        )
    log_cb(
        "Данные загрузчика с устройства: сигнатура %s, версия загрузчика %s."
        % (sig or "—", bl_ver or "—"),
        "info",
    )
    cleaned = (sig or "").strip()[:12] if sig and str(sig).strip() else None
    return cleaned, None


def _preflight_read_bootloader_info_by_address_mr(
    flasher: fp.FlasherProtocol,
    *,
    bootloader_addr: int,
    log_cb: Callable[[str, str], None],
) -> Optional[str]:
    """
    Bootloader-.bin по Modbus-адресу: читаем сигнатуру платы (рег. 290) на адресе
    загрузчика (247), симметрично ветке по серийному
    (_preflight_read_bootloader_info_by_serial_mr). Тот же helper, что и app-.bin путь
    по адресу (flash_protocol._read_board_signature_bootloader_warmup).

    Fail-safe: возвращаем сигнатуру ТОЛЬКО если чтение прошло и сигнатура валидна и
    поддерживается (та же проверка, что у app/.bin-пути,
    _validate_bootloader_board_signature_for_bin_flash); иначе None — вызывающий
    сохраняет прежний info_sig, прошивка не ломается и NONE в info-блок не попадает.
    """
    log_cb(
        "Запрос информации загрузчика (рег. 290) по адресу %d." % bootloader_addr,
        "info",
    )
    board_sig, read_err = fp._read_board_signature_bootloader_warmup(
        flasher, slave=bootloader_addr, serial=None
    )
    if read_err:
        log_cb(
            "Не удалось прочитать сигнатуру платы (рег. 290) по адресу %d: %s "
            "Сохраняю прежнюю сигнатуру для info-блока." % (bootloader_addr, read_err),
            "warn",
        )
        return None
    reject = fp._validate_bootloader_board_signature_for_bin_flash(board_sig)
    if reject:
        log_cb(
            "Сигнатура платы (рег. 290) по адресу %d непригодна («%s»); "
            "сохраняю прежнюю сигнатуру для info-блока."
            % (bootloader_addr, board_sig or "—"),
            "warn",
        )
        return None
    cleaned = (board_sig or "").strip()[:12]
    log_cb(
        "Данные загрузчика с устройства (адрес %d): сигнатура %s."
        % (bootloader_addr, cleaned),
        "info",
    )
    return cleaned


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
        # Identity was verified by this serial. Address 247 can have several responders.
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
    image_family: Optional[str],
    on_irreversible: Optional[Callable[[], None]] = None,
) -> Optional[str]:
    """
    Одна сессия: перевод из приложения (при необходимости) → один COM на скорости загрузчика
    → wait на том же ser (MR) → второй prime при duplicate+fast → FlasherProtocol → прошивка.
    """
    application_identity = None
    entered: Dict[str, Any] = {}
    entered_by_us = False
    if not device.get("in_bootloader"):
        application_identity, fatal = _enter_bootloader_from_application_line(
            device_path, device, cancel_evt, log_cb, is_wb_firmware=is_wb_firmware,
            use_fast_modbus=use_fast_modbus, image_family=image_family, entered=entered,
        )
        if fatal:
            return fatal
        entered_by_us = True
        _post_reset_delays_before_bootloader_open(log_cb, is_wb_firmware=is_wb_firmware)
    else:
        if recovery:
            log_cb("Режим восстановления: устройство уже в загрузчике.", "info")
        else:
            log_cb(
                "Устройство уже в режиме загрузчика, ищем линию загрузчика.",
                "info",
            )

    ser: Optional[Any] = None
    bl_line: Tuple[int, str, int] = BOOTLOADER_FIXED_LINE
    bl_tried: List[Tuple[int, str, int]] = []
    try:
        sn_pf = int(device.get("serial") or 0) & 0xFFFFFFFF
        probe_serial = (
            sn_pf
            if (use_fast_modbus and sn_pf not in (0, 0xFFFFFFFF) and not is_wb_firmware)
            else None
        )
        # После входа в bootloader (reg 129) устройство всегда отвечает на адресе 247; адрес приложения нужен
        # только для самого входа.
        probe_addr = None if probe_serial is not None else fp.BOOTLOADER_DEFAULT_ADDR
        prime_for_wait = bool(probe_serial and duplicate_on_line)
        found_ready: Optional[bool] = None
        found_err: Optional[str] = None
        ident_state: Optional[Tuple[Any, Optional[str]]] = None

        def _confirm_on_line(port: Any) -> Tuple[Any, Optional[str]]:
            # Identity on the bootloader line itself (serial + signature of the TARGET) - read only, before any block.
            if use_fast_modbus and duplicate_on_line:
                log_cb(
                    "Прогрев шины (0xFD 0x46 0x04) перед быстрым Modbus — устраняет таймаут первого 0x08 после долгого открытия COM.",
                    "debug",
                )
                _prime_wb_fast_modbus_line(port, cancel_evt.is_set)
            fl = _make_bootloader_flasher_protocol(port, cancel_evt, log_cb, is_wb_firmware=False)
            return _confirm_flash_identity(
                fl, device, fp.BOOTLOADER_DEFAULT_ADDR, use_fast_modbus,
                image_family=image_family, prior=application_identity,
            )

        if is_wb_firmware:
            ser = _open_bootloader_serial(device_path, is_wb_firmware=is_wb_firmware)
        else:
            bl_tried = _bootloader_line_candidates(device, entered.get("line"))
            ser, bl_line, found_ready, found_err, ident_state = _open_bootloader_on_candidates(
                device_path, bl_tried, cancel_evt, log_cb,
                prime_fast=prime_for_wait, probe_serial=probe_serial, probe_addr=probe_addr,
                confirm=_confirm_on_line,
            )
        if is_wb_firmware:
            baud = fp.BOOTLOADER_BAUDRATE_WB
            stop = int(fp.BOOTLOADER_STOPBITS_WB)
            log_cb(
                "Режим .wbfw: загрузчик %d 8%c%d."
                % (baud, fp.BOOTLOADER_PARITY, stop),
                "info",
            )
            log_cb(".wbfw: пауза 1.5 с после открытия порта загрузчика.", "debug")
            time.sleep(1.5)

        if not is_wb_firmware:
            ready, wait_err = found_ready, found_err
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
                application_identity, err_re = _enter_bootloader_from_application_line(
                    device_path,
                    device,
                    cancel_evt,
                    log_cb,
                    is_wb_firmware=is_wb_firmware,
                    use_fast_modbus=use_fast_modbus, image_family=image_family, entered=entered,
                )
                if err_re:
                    if entered_by_us and _is_no_answer(err_re):
                        # G-6: reg 129 was already written in this job, so «запись не отправлялась» would be untrue.
                        return _bootloader_not_found_text(bl_tried)
                    return err_re
                _post_reset_delays_before_bootloader_open(
                    log_cb, is_wb_firmware=is_wb_firmware
                )
                bl_tried = _bootloader_line_candidates(device, entered.get("line"))
                ser, bl_line, ready, _, ident_state = _open_bootloader_on_candidates(
                    device_path, bl_tried, cancel_evt, log_cb,
                    prime_fast=prime_for_wait, probe_serial=probe_serial, probe_addr=probe_addr,
                    confirm=_confirm_on_line,
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
                fp.BOOTLOADER_BAUDRATE_WB if is_wb_firmware else bl_line[0],
                fp.BOOTLOADER_STOPBITS_WB if is_wb_firmware else bl_line[2],
            ),
            "debug",
        )

        flasher = _make_bootloader_flasher_protocol(
            ser, cancel_evt, log_cb, is_wb_firmware=is_wb_firmware
        )

        if not is_wb_firmware:
            if ident_state is None:
                # Nobody answered the probe on any line (or a slow bootloader): one identity read on the parked line.
                ident_state = _confirm_on_line(ser)
            identity_err = ident_state[1]
            if identity_err:
                if entered_by_us:
                    # D36b-02 (+G-2): reg 129 was accepted and the target's identity was confirmed on no line,
                    # so the module may be sitting in its bootloader; the reason goes to the job log.
                    log_cb(f"Загрузчик цели не подтверждён: {identity_err}", "warn")
                    return _bootloader_not_found_text(bl_tried)
                return identity_err
        return _flash_one_device(
            flasher,
            device,
            image,
            file_signature,
            bootloader_line=None if is_wb_firmware else bl_line,
            ser=ser,
            duplicate_on_line=duplicate_on_line,
            use_fast_modbus=use_fast_modbus,
            is_wb_firmware=is_wb_firmware,
            is_bootloader_firmware=is_bootloader_firmware,
            recovery=recovery,
            cancel_evt=cancel_evt,
            log_cb=log_cb,
            progress_cb=progress_cb,
            on_irreversible=on_irreversible,
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
    is_wb_firmware: bool = False,
    is_bootloader_firmware: bool = False,
    recovery: bool = False,
    cancel_evt,
    log_cb: Callable[[str, str], None],
    progress_cb: Callable[[int, str], None],
    on_irreversible: Optional[Callable[[], None]] = None,
    bootloader_line: Optional[Tuple[int, str, int]] = None,
) -> Optional[str]:
    addr = int(device.get("address") or fp.BOOTLOADER_DEFAULT_ADDR)
    serial = int(device.get("serial") or 0) & 0xFFFFFFFF
    dev_sig = str(device.get("signature") or "").strip()

    # Bootloader всегда отвечает на адресе 247 (BOOTLOADER_DEFAULT_ADDR),
    # независимо от Modbus-адреса приложения.
    boot_addr_for_address_path = fp.BOOTLOADER_DEFAULT_ADDR

    info_sig = (dev_sig if dev_sig and dev_sig.upper() != "NONE" else file_signature) or fp.DEFAULT_SIGNATURE
    flash_cancel_gate = _flash_cancel_gate(cancel_evt, on_irreversible)

    def prog(sent: int, total: int) -> None:
        total = max(1, int(total))
        sent = max(0, int(sent))
        pct = min(100, int(sent * 100 / total))
        progress_cb(pct, f"Блок {sent}/{total}")

    if is_wb_firmware:
        log_cb(f"Прошивка .wbfw по адресу {addr}", "info")
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
            % (boot_addr_for_address_path, (bootloader_line or BOOTLOADER_FIXED_LINE)[0]),
            "info",
        )
        if is_bootloader_firmware:
            # Симметрия с веткой по серийному (1093–1104): перед прошивкой .bin читаем
            # реальную сигнатуру платы из рег. 290 на адресе загрузчика и переопределяем
            # info_sig ею. Fail-safe: только валидную сигнатуру; иначе info_sig не меняем
            # (не регрессируем к NONE, не ломаем прошивку). .fw шлёт свой info-блок raw.
            sig_from_bl = _preflight_read_bootloader_info_by_address_mr(
                flasher,
                bootloader_addr=boot_addr_for_address_path,
                log_cb=log_cb,
            )
            if sig_from_bl:
                info_sig = sig_from_bl
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


def _firmware_file_is_wb(entry: FirmwareEntry) -> bool:
    return str(entry.file).lower().endswith(".wbfw")


def _resolve_flash_route_for_device(
    device: Dict[str, Any],
    entry: FirmwareEntry,
) -> Tuple[bool, Optional[str]]:
    """
    Определить is_wb_firmware и проверить соответствие сигнатуры устройства и типа образа.
    Маршрут задаётся сигнатурой (mp_mr vs wb), не только расширением файла.
    """
    dev_sig = str(device.get("signature") or "").strip()
    firmware_is_wb = _firmware_file_is_wb(entry)
    if device.get("signature_inferred"):
        return False, "Требуется опознание семейства устройства по регистру 290."
    is_bl_fw = (entry.kind or "").lower() == "bootloader"
    err = validate_firmware_device_route(
        dev_sig,
        firmware_is_wb=firmware_is_wb,
        is_bootloader_firmware=is_bl_fw,
    )
    if err:
        return False, err
    target_family = mp.manifest_device_for_signature(dev_sig)
    image_family = mp.manifest_device_family(entry.device)
    if not firmware_is_wb and target_family is None:
        return False, "Сигнатура выбранного устройства не подтверждает нативное семейство."
    if target_family and not image_family:
        return False, "Семейство образа не определено в каталоге."
    if target_family and image_family and target_family != image_family:
        return False, f"Семейство образа {image_family} не соответствует устройству {target_family}."
    route = device_flash_route(dev_sig)
    return route == "wb", None


def _fail_flash_targets(job: Job, failed: List[Tuple[Dict[str, Any], str]]) -> "RuntimeError":
    """Записать неудавшиеся цели в params["errors"] и вернуть исключение с текстом, который
    показывает вкладка: «Не прошито: адр.N SN 0x…: причина» — на ЛЮБОМ пути (одна цель, пакет
    с остановкой на первой ошибке, пакет с продолжением), без имени класса исключения."""
    job.params["errors"] = [
        {"address": d.get("address"), "serial": d.get("serial"), "error": e} for d, e in failed
    ]
    return RuntimeError(failed_targets_text(job.params["errors"]))


def run_flash_job(job: Job, ctx: Dict[str, Any], cfg: FlasherConfig, repo: FirmwareRepo) -> None:
    """
    Задача прошивки одного устройства.

    params:
        port               — ключ COM (COM1..COM5)
        target             — {'address': int} или {'serial': int} (из таблицы)
        use_fast_modbus    — bool
        firmware_channel   — канал
        firmware_file      — имя файла
    """
    params = job.params
    port_key = str(params.get("port") or job.port)
    target = params.get("target") or {}
    use_fast = bool(params.get("use_fast_modbus"))

    log_cb = ctx["log"]
    progress_cb = ctx["progress"]
    cancel_evt = ctx["cancel_evt"]
    on_irreversible = ctx.get("mark_irreversible")

    device_path = resolve_device_path(cfg, port_key)
    image, file_sig, file_ver, entry = _load_firmware_for_flash(repo, params)
    image_family = mp.manifest_device_family(entry.device)
    is_wb, route_err = _resolve_flash_route_for_device(target, entry)
    if route_err:
        raise _fail_flash_targets(job, [(target, route_err)])
    log_sig = _firmware_signature_label(file_sig, entry, target)
    route_name = device_flash_route(str(target.get("signature") or ""))
    log_cb(
        f"Файл: {entry.file} sig={log_sig} ver={file_ver} size={len(image)} route={route_name}",
        "info",
    )
    progress_cb(1, "Открытие порта")

    with port_lease(device_path, cfg.mplc_stop_services):
        with _port_flock(cfg, port_key):
            peers_raw = params.get("devices_on_port")
            peers: Optional[List[Dict[str, Any]]] = peers_raw if isinstance(peers_raw, list) else None
            dup = _duplicate_modbus_address_on_line(target, peers)
            use_fast = _resolve_use_fast_modbus(
                use_fast, duplicate_on_line=dup, is_wb_firmware=is_wb
            )
            if dup and not bool(params.get("use_fast_modbus")):
                log_cb(
                    "Быстрый Modbus (0xFD 0x46): включён автоматически — на линии несколько устройств с одним Modbus-адресом.",
                    "info",
                )
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
                image_family=image_family,
                on_irreversible=on_irreversible,
            )
            if err:
                raise _fail_flash_targets(job, [(target, err)])
    progress_cb(100, "Готово")


def _resolve_batch_settle_s(params: Dict[str, Any]) -> float:
    """Inter-device settle seconds for the batch loop.

    Default BATCH_INTER_DEVICE_SETTLE_S unless the job carries an
    ``inter_device_settle_s`` override, which is clamped to [0, MAX] (invalid →
    default). Same clamp idiom as ``_bounded_int``, kept float so a fractional
    default survives.
    """
    raw = params.get("inter_device_settle_s")
    if raw is None:
        return BATCH_INTER_DEVICE_SETTLE_S
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return BATCH_INTER_DEVICE_SETTLE_S
    return max(0.0, min(BATCH_INTER_DEVICE_SETTLE_MAX_S, val))


def _settle_between_batch_devices(
    settle_s: float,
    cancel_evt: Any,
    log_cb: Callable[[str, str], None],
) -> None:
    """Bounded, cancellable pause between two batch devices on the shared line.

    Waits up to ``settle_s`` so the just-flashed module finishes rebooting and
    vacates the bootloader's fixed addr 247 before the next enter_bootloader.
    A cancel cuts the wait short — it never sleeps the full settle once cancelled.
    """
    if settle_s <= 0 or cancel_evt.is_set():
        return
    log_cb(
        f"Пауза {settle_s:g} с перед следующим устройством "
        f"(осадка после перезагрузки модуля)…",
        "info",
    )
    deadline = time.monotonic() + settle_s
    while not cancel_evt.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.1, remaining))


def run_flash_batch_job(job: Job, ctx: Dict[str, Any], cfg: FlasherConfig, repo: FirmwareRepo) -> None:
    """
    Пакетная прошивка нескольких устройств на одном COM.

    params.targets — список dict: {address, serial, signature, in_bootloader, ...}
    params.firmware_* — одна прошивка на всю партию; маршрут по сигнатуре каждого устройства.
    """
    params = job.params
    port_key = str(params.get("port") or job.port)
    targets: List[Dict[str, Any]] = list(params.get("targets") or [])
    use_fast_requested = bool(params.get("use_fast_modbus"))
    skip_on_error = bool(params.get("skip_on_error", True))
    peers_raw = params.get("devices_on_port")
    batch_peers: Optional[List[Dict[str, Any]]] = (
        list(peers_raw) if isinstance(peers_raw, list) and peers_raw else targets
    )

    if not targets:
        raise ValueError("Список устройств для пакетной прошивки пуст")

    log_cb = ctx["log"]
    progress_cb = ctx["progress"]
    cancel_evt = ctx["cancel_evt"]
    on_irreversible = ctx.get("mark_irreversible")

    device_path = resolve_device_path(cfg, port_key)
    image, file_sig, file_ver, entry = _load_firmware_for_flash(repo, params)
    log_sig = _firmware_signature_label(file_sig, entry, targets[0] if targets else None)
    log_cb(f"Пакет: {len(targets)} устройств, файл {entry.file} sig={log_sig} ver={file_ver}", "info")
    settle_s = _resolve_batch_settle_s(params)
    image_family = mp.manifest_device_family(entry.device)
    routes = [_resolve_flash_route_for_device(dev, entry) for dev in targets]
    invalid = [reason for _, reason in routes if reason]
    if invalid and (not skip_on_error or len(invalid) == len(routes)):
        bad_targets = [(dev, reason) for dev, (_, reason) in zip(targets, routes) if reason]
        raise _fail_flash_targets(job, bad_targets if skip_on_error else bad_targets[:1])

    with port_lease(device_path, cfg.mplc_stop_services):
        with _port_flock(cfg, port_key):
            errors: List[Tuple[Dict[str, Any], str]] = []
            total = len(targets)
            is_bl_fw = (entry.kind or "").lower() == "bootloader"
            for i, dev in enumerate(targets):
                if cancel_evt.is_set():
                    log_cb("Отмена пакетной прошивки", "warn")
                    break
                # Settle after the previous device (flashed OR skipped-on-error)
                # before touching the shared line again — never after the last.
                if i > 0:
                    _settle_between_batch_devices(settle_s, cancel_evt, log_cb)
                    if cancel_evt.is_set():
                        log_cb("Отмена пакетной прошивки", "warn")
                        break
                is_wb, route_err = routes[i]
                if route_err:
                    errors.append((dev, route_err))
                    log_cb(f"Ошибка: {route_err}", "error")
                    if not skip_on_error:
                        raise _fail_flash_targets(job, errors)
                    continue
                route_name = device_flash_route(str(dev.get("signature") or ""))
                log_cb(
                    f"[{i+1}/{total}] Прошивка устройства {dev.get('address')} "
                    f"sn=0x{int(dev.get('serial') or 0):08X} route={route_name}",
                    "info",
                )

                def sub_progress(pct: int, message: str) -> None:
                    overall = int((i + pct / 100.0) * 100 / total)
                    progress_cb(overall, f"[{i+1}/{total}] {message}")

                dup = _duplicate_modbus_address_on_line(dev, batch_peers)
                use_fast = _resolve_use_fast_modbus(
                    use_fast_requested,
                    duplicate_on_line=dup,
                    is_wb_firmware=is_wb,
                )
                if dup and not use_fast_requested:
                    log_cb(
                        "Быстрый Modbus (0xFD 0x46): включён автоматически — на линии несколько устройств с одним Modbus-адресом.",
                        "info",
                    )
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
                    image_family=image_family,
                    on_irreversible=on_irreversible,
                )
                if err:
                    errors.append((dev, err))
                    log_cb(f"Ошибка: {err}", "error")
                    if not skip_on_error:
                        raise _fail_flash_targets(job, errors)
            if errors:
                log_cb(f"Завершено с ошибками: {len(errors)} из {total}", "warn")
                # Партия с продолжением (skip_on_error=true, только по явной просьбе): ошибки — в
                # params; JobManager завершает такую задачу статусом error (jobs.failed_flash_targets).
                job.params["errors"] = [
                    {"address": d.get("address"), "serial": d.get("serial"), "error": e}
                    for d, e in errors
                ]
    progress_cb(100, "Пакетная прошивка завершена")


# ─── BACnet MS/TP verify / recover (§5.2 / §5.4) ────────────────────────────
#
# The daemon-route<->serial-wire glue below is the ONE non-host-testable layer
# (plan §11): the pure MS/TP codec (bacnet_mstp) and bus-mode semantics
# (bus_mode) are unit-tested; opening the leased COM at the MS/TP baud and the
# on-the-wire behaviour are covered by the bench HIL scenario (plan §7).


def _bounded_int(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, iv))


def _verify_family(params: Dict[str, Any]) -> str:
    fam = str(params.get("family") or bus_mode.FAMILY_MR).strip().lower()
    return fam if fam in (bus_mode.FAMILY_MR, bus_mode.FAMILY_DTV) else bus_mode.FAMILY_MR


def run_bacnet_verify_job(job: Job, ctx: Dict[str, Any], cfg: FlasherConfig) -> None:
    """Passive MS/TP sniff on the leased port (§5.2) — read-only, RX-only.

    MR sniffs at fixed 38400 8N1; DTV at its configured MS/TP baud. Silence and
    port-open errors are reported, never swallowed as a false 'alive'."""
    params = job.params
    port_key = str(params.get("port") or job.port)
    family = _verify_family(params)
    duration = _bounded_int(params.get("duration_s"), 2, 30, 5)
    baud = bacnet_mstp.resolve_mstp_baud(family, params.get("baud"))
    device_path = resolve_device_path(cfg, port_key)

    log_cb = ctx["log"]
    progress_cb = ctx["progress"]
    log_cb("Пассивный сниф MS/TP на %s @ %d 8N1, %d с (только чтение)" % (port_key, baud, duration), "info")
    progress_cb(5, "Подготовка порта для снифа")

    res = bacnet_mstp.SniffResult()
    with port_lease(device_path, cfg.mplc_stop_services):
        with _port_flock(cfg, port_key):
            ser = None
            try:
                ser = open_port(device_path, baudrate=baud, parity="N", stopbits=1)
                progress_cb(20, "Слушаю MS/TP %d с" % duration)
                res = bacnet_mstp.sniff_serial(ser, baud, duration)
            except Exception as exc:  # noqa: BLE001 — surface the open/read error, never a false pass
                res = bacnet_mstp.SniffResult(open_error="%s: %s" % (type(exc).__name__, exc))
            finally:
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass

    summary = res.to_dict()
    summary.update({"family": family, "baud": baud, "duration_s": duration, "kind": "bacnet_verify"})
    job.result = summary
    if res.open_error:
        log_cb("Ошибка порта: %s" % res.open_error, "error")
        progress_cb(100, "Сниф не выполнен: ошибка порта")
    elif res.alive:
        types = ", ".join("%s×%d" % (n, c) for n, c in res.type_counts_named())
        log_cb("MS/TP жив: кадров %d (%s)" % (res.frames_seen, types), "info")
        progress_cb(100, "MS/TP активен — кадров: %d" % res.frames_seen)
    else:
        log_cb(
            "Нет кадров MS/TP (прочитано %d байт, битых заголовков %d)" % (res.bytes_read, res.bad_header_crc),
            "warn",
        )
        progress_cb(100, "Тишина: кадры MS/TP не обнаружены")


def run_bacnet_recover_job(job: Job, ctx: Dict[str, Any], cfg: FlasherConfig) -> None:
    """In-band return to Modbus (§5.4). Two paths, chosen by whether the module
    still answers Modbus:

      pending window (still answers) -> direct Modbus write reg122 = target
      latched in BACnet (silent)     -> ring-join WriteProperty over MS/TP
        MR:  MSV:1 present-value = target_mode + 1
        DTV: AV:122 present-value = target_mode (0 = classic)
      then wait the deferred-reset window and confirm Modbus is back.

    Runs under the same COM lease + flock as scan/flash (the SA-02m owns the
    segment). The fake-station ring-join only transmits on a leased port."""
    params = job.params
    port_key = str(params.get("port") or job.port)
    family = _verify_family(params)
    device = params.get("device") if isinstance(params.get("device"), dict) else {}
    prior_mode = params.get("prior_mode")
    modbus_addr = _bounded_int(params.get("address") or device.get("address"), 1, 247, 1)
    dut_mac = max(1, min(127, modbus_addr))
    src_mac = _bounded_int(params.get("src_mac"), 1, 127, (dut_mac % 127) + 1)
    if src_mac == dut_mac:
        src_mac = (dut_mac % 127) + 1
    ring_secs = _bounded_int(params.get("duration_s"), 5, 60, 20)
    reset_wait = _bounded_int(params.get("reset_wait_s"), 4, 15, 10)
    mstp_baud = bacnet_mstp.resolve_mstp_baud(family, params.get("baud"))
    device_path = resolve_device_path(cfg, port_key)

    target_mode = bus_mode.force_modbus_return_value(family, prior_mode)
    log_cb = ctx["log"]
    progress_cb = ctx["progress"]

    result: Dict[str, Any] = {
        "kind": "bacnet_recover",
        "family": family,
        "target_mode": target_mode,
        "path": None,
        "recovered": False,
    }
    job.result = result

    with port_lease(device_path, cfg.mplc_stop_services):
        with _port_flock(cfg, port_key):
            # 1) Pending window? — a Modbus read of reg122 that answers means the
            #    module has not yet rebooted/latched: a direct write returns it.
            progress_cb(10, "Проверяю, отвечает ли модуль по Modbus")
            probe = device_config.probe_bus_mode(device_path, device)
            plan = bus_mode.force_modbus_recovery_plan(family, probe["answered"], prior_mode)

            if plan.action == bus_mode.RECOVERY_WRITE:
                log_cb("Модуль ещё отвечает по Modbus — прямая запись возврата рег.122", "info")
                progress_cb(40, "Прямая запись возврата в Modbus")
                err = _direct_modbus_bus_write(device_path, device, target_mode)
                if err:
                    result["path"] = "modbus_write"
                    log_cb("Ошибка записи возврата: %s" % err, "error")
                    progress_cb(100, "Не удалось записать возврат в Modbus")
                    return
                result["path"] = "modbus_write"
                result["recovered"] = True
                log_cb("Возврат записан напрямую (рег.122 = %d)" % target_mode, "info")
                progress_cb(100, "Модуль возвращён в Modbus")
                return

            # 2) Latched — ring-join WriteProperty over MS/TP.
            pv = bus_mode.mr_msv_pv_for_mode(target_mode)
            if family == bus_mode.FAMILY_DTV:
                payload = bacnet_mstp.dtv_recovery_wp_payload(7, target_mode)
                obj_desc = "AV:122 PV=%d" % target_mode
            else:
                payload = bacnet_mstp.mr_recovery_wp_payload(7, pv)
                obj_desc = "MSV:1 PV=%d" % pv
            log_cb(
                "Модуль не отвечает по Modbus (залатчен). Ring-join WP %s @ %d 8N1, MAC %d->%d"
                % (obj_desc, mstp_baud, src_mac, dut_mac),
                "info",
            )
            progress_cb(30, "Вхожу в кольцо MS/TP и шлю WriteProperty")
            fsm = bacnet_mstp.RingJoinFSM(my_mac=src_mac, dut_mac=dut_mac, der_payload=payload)
            rj = bacnet_mstp.RingJoinResult(open_error="serial not open")
            ser = None
            try:
                ser = open_port(device_path, baudrate=mstp_baud, parity="N", stopbits=1)
                rj = bacnet_mstp.ring_join_serial(ser, mstp_baud, fsm, ring_secs)
            except Exception as exc:  # noqa: BLE001
                rj = bacnet_mstp.RingJoinResult(open_error="%s: %s" % (type(exc).__name__, exc))
            finally:
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass

            result["path"] = "mstp_wp"
            result["ring_join"] = {
                "state": rj.state,
                "frames_rx": rj.frames_rx,
                "frames_tx": rj.frames_tx,
                "answered": rj.answered,
                "acked": rj.acked,
                "open_error": rj.open_error,
            }
            if rj.open_error:
                log_cb("Ошибка порта: %s" % rj.open_error, "error")
                progress_cb(100, "Восстановление не выполнено: ошибка порта")
                return
            if not rj.answered:
                log_cb(
                    "Не удалось войти в кольцо / модуль не ответил на WriteProperty. "
                    "Верните модуль физической кнопкой сброса на устройстве.",
                    "warn",
                )
                progress_cb(100, "Модуль не вернулся — нужна физическая кнопка сброса")
                return

            log_cb("WriteProperty подтверждён, жду отложенный сброс модуля", "info")
            progress_cb(75, "Жду перезагрузку модуля (%d с)" % reset_wait)
            time.sleep(reset_wait)

            progress_cb(90, "Проверяю возврат на Modbus")
            post = device_config.probe_bus_mode(device_path, device)
            result["post_probe"] = post
            if post["answered"]:
                result["recovered"] = True
                log_cb("Модуль вернулся на Modbus (рег.122 = %s)" % post.get("bus_mode"), "info")
                progress_cb(100, "Модуль возвращён в Modbus")
            else:
                log_cb(
                    "Модуль не ответил по Modbus после восстановления — "
                    "нужна физическая кнопка сброса на устройстве.",
                    "warn",
                )
                progress_cb(100, "Модуль не вернулся — нужна физическая кнопка сброса")


def _direct_modbus_bus_write(device_path: str, device: Dict[str, Any], target_mode: int) -> Optional[str]:
    """Direct Modbus FC06 write of reg122 = target_mode (pending-window recovery).

    Returns None on success or an error string. Kept thin here (HIL glue) —
    reuses the device_config transport."""
    from .modbus_io import write_single as _write_single

    send, close_transport = device_config._open_transport(device_path, device)
    try:
        slave = device_config._device_slave(device)
        return _write_single(send, slave, device_config.REG_FAST_MODBUS, int(target_mode) & 0xFFFF, 800)
    finally:
        close_transport()
