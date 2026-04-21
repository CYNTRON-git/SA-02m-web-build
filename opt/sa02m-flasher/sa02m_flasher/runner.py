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
import struct
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
from .mplc_lease import port_lease, PortBusyError, device_path_exists
from .serial_port import open_port, send_receive

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
    speed_configs = _build_speed_configs(baudrates, parity, stopbits)

    device_path = resolve_device_path(cfg, port_key)
    log_cb = ctx["log"]
    progress_cb = ctx["progress"]
    device_found_cb = ctx["device_found"]
    cancel_evt = ctx["cancel_evt"]

    log_cb(f"Скан COM={port_key} ({device_path}), режим={mode.value}", "info")
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
            )

    # Финальный снэпшот: перезаписать список устройств из результата scan_all (упорядочено).
    job.devices = [_device_to_dict(d) for d in devices]
    progress_cb(100, "Сканирование завершено")
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
    timeout_ms: int = 2000,
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


def _transition_to_bootloader(
    device_path: str,
    device: Dict[str, Any],
    cancel_evt,
    log_cb: Callable[[str, str], None],
) -> None:
    """
    Если устройство не в bootloader'е — открыть порт на его app-скорости и записать reg 129 = 1.
    После этого устройство перезагружается и ожидает на 115200 8N1.
    """
    if device.get("in_bootloader"):
        return
    baud = int(device.get("baudrate") or 0) or 19200
    parity = str(device.get("parity") or "N").upper() or "N"
    stopbits = int(device.get("stopbits") or 2) or 2
    addr = int(device.get("address") or fp.BOOTLOADER_DEFAULT_ADDR)

    log_cb(f"Перевод адр.{addr} в bootloader (app baud {baud} {parity}{stopbits})", "info")
    flasher, ser = _make_flasher(device_path, baud, parity, stopbits, cancel_evt, log_cb, timeout_ms=1500)
    try:
        err = flasher.enter_bootloader(addr)
        if err:
            log_cb(f"enter_bootloader(slave={addr}) → {err} (продолжаем — устройство могло уже перезагрузиться)", "warn")
    except Exception as exc:
        log_cb(f"enter_bootloader исключение: {exc}", "warn")
    finally:
        try:
            ser.close()
        except Exception:
            pass
    time.sleep(1.5)


def _flash_one_device(
    flasher: fp.FlasherProtocol,
    device: Dict[str, Any],
    image: bytes,
    file_signature: str,
    *,
    use_fast_modbus: bool,
    force_unlisted_signature: bool,
    cancel_evt,
    log_cb: Callable[[str, str], None],
    progress_cb: Callable[[int, str], None],
) -> Optional[str]:
    """Прошить одно устройство (порт уже открыт на BOOTLOADER_BAUDRATE).
    Возвращает None при успехе, строку ошибки иначе."""
    addr = int(device.get("address") or fp.BOOTLOADER_DEFAULT_ADDR)
    serial = int(device.get("serial") or 0) & 0xFFFFFFFF
    dev_sig = str(device.get("signature") or "").strip()

    # Один образ на всю линейку MR-02м: не сравниваем сигнатуру файла с модулем.
    # Разрешаем прошивку только для «наших» сигнатур (MR/MP-02м…), либо с флагом обхода (отладка).
    if not device_allowed_for_mr_firmware_flash(dev_sig, allow_unlisted=force_unlisted_signature):
        return (
            f"Сигнатура «{dev_sig}» не распознана как модуль расширения MR/MP-02м. "
            "Прошивка отменена. Для лабораторных случаев включите опцию «Разрешить устройство вне списка сигнатур»."
        )

    # Для .bin info-блок собирается из сигнатуры; для .fw первые 32 B берутся из файла — там параметр не используется.
    info_sig = (dev_sig if dev_sig and dev_sig.upper() != "NONE" else file_signature) or fp.DEFAULT_SIGNATURE

    def prog(sent: int, total: int) -> None:
        total = max(1, int(total))
        sent = max(0, int(sent))
        pct = min(100, int(sent * 100 / total))
        progress_cb(pct, f"Блок {sent}/{total}")

    if use_fast_modbus:
        if not serial:
            return "Для быстрого Modbus нужен серийный номер устройства"
        log_cb(f"Прошивка по серийному 0x{serial:08X} (быстрый Modbus)", "info")
        err = fp.run_flash_sequence(
            flasher,
            serial,
            image,
            info_sig,
            progress_cb=prog,
            cancel_cb=cancel_evt.is_set,
        )
    else:
        log_cb(f"Прошивка по адресу {addr}", "info")
        err = fp.run_flash_sequence_by_address(
            flasher,
            addr,
            image,
            info_sig,
            progress_cb=prog,
            cancel_cb=cancel_evt.is_set,
        )

    if err:
        return err

    log_cb("Запуск приложения (reg 1004)", "info")
    try:
        if use_fast_modbus:
            err = flasher.jump_to_app_by_serial(serial)
        else:
            err = flasher.jump_to_app(addr)
    except Exception as exc:
        log_cb(f"jump_to_app исключение: {exc}", "warn")
        err = str(exc)
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
    log_cb(f"Файл: {entry.file} sig={file_sig} ver={file_ver} size={len(image)}", "info")
    progress_cb(1, "Открытие порта")

    with port_lease(device_path, cfg.mplc_stop_services):
        with _port_flock(cfg, port_key):
            _transition_to_bootloader(device_path, target, cancel_evt, log_cb)
            flasher, ser = _make_flasher(
                device_path,
                fp.BOOTLOADER_BAUDRATE,
                fp.BOOTLOADER_PARITY,
                fp.BOOTLOADER_STOPBITS,
                cancel_evt,
                log_cb,
            )
            try:
                err = _flash_one_device(
                    flasher,
                    target,
                    image,
                    file_sig,
                    use_fast_modbus=use_fast,
                    force_unlisted_signature=force_unlisted,
                    cancel_evt=cancel_evt,
                    log_cb=log_cb,
                    progress_cb=progress_cb,
                )
            finally:
                try:
                    ser.close()
                except Exception:
                    pass
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
    log_cb(f"Пакет: {len(targets)} устройств, файл {entry.file} sig={file_sig} ver={file_ver}", "info")

    with port_lease(device_path, cfg.mplc_stop_services):
        with _port_flock(cfg, port_key):
            errors: List[Tuple[Dict[str, Any], str]] = []
            total = len(targets)
            for i, dev in enumerate(targets):
                if cancel_evt.is_set():
                    log_cb("Отмена пакетной прошивки", "warn")
                    break
                log_cb(f"[{i+1}/{total}] Прошивка устройства {dev.get('address')} sn=0x{int(dev.get('serial') or 0):08X}", "info")

                def sub_progress(pct: int, message: str) -> None:
                    overall = int((i + pct / 100.0) * 100 / total)
                    progress_cb(overall, f"[{i+1}/{total}] {message}")

                _transition_to_bootloader(device_path, dev, cancel_evt, log_cb)
                flasher, ser = _make_flasher(
                    device_path,
                    fp.BOOTLOADER_BAUDRATE,
                    fp.BOOTLOADER_PARITY,
                    fp.BOOTLOADER_STOPBITS,
                    cancel_evt,
                    log_cb,
                )
                try:
                    err = _flash_one_device(
                        flasher,
                        dev,
                        image,
                        file_sig,
                        use_fast_modbus=use_fast,
                        force_unlisted_signature=force_unlisted,
                        cancel_evt=cancel_evt,
                        log_cb=log_cb,
                        progress_cb=sub_progress,
                    )
                finally:
                    try:
                        ser.close()
                    except Exception:
                        pass
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
