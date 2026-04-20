# -*- coding: utf-8 -*-
"""
Полный журнал прошивальщика в flasher_log.txt (рядом с exe или корнем проекта).
Отдельный журнал обновлений прошивок: firmware_update_log.txt (краткие события START/OK/ERROR и итог пакета).
Потокобезопасная запись; для разбора проблем смотреть эти файлы.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, TextIO

_lock = threading.Lock()
_fp: Optional[TextIO] = None
_log_path: Optional[Path] = None
_wb_fp: Optional[TextIO] = None
_wb_path: Optional[Path] = None
_com_listen_fp: Optional[TextIO] = None
_com_listen_path: Optional[Path] = None
_fw_update_fp: Optional[TextIO] = None
_fw_update_path: Optional[Path] = None


def init_log(app_dir: Path) -> Optional[Path]:
    """Открыть flasher_log.txt на дозапись. Возвращает путь или None при ошибке."""
    global _fp, _log_path
    path = app_dir / "flasher_log.txt"
    with _lock:
        if _fp is not None:
            return _log_path
        try:
            _fp = open(path, "a", encoding="utf-8", errors="replace")
            _log_path = path
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _fp.write(f"\n=== {ts} сеанс ===\n")
            _fp.flush()
        except OSError:
            _fp = None
            _log_path = None
            return None
    return path


def close_log() -> None:
    global _fp, _log_path
    with _lock:
        if _fp is not None:
            try:
                _fp.close()
            except OSError:
                pass
            _fp = None


def init_wb_trace(app_dir: Path) -> Optional[Path]:
    """
    Детальный журнал арбитража WB (все TX/RX кадры быстрого скана 0xFD 0x46).
    Сначала каталог приложения, затем %TEMP%, затем домашний каталог.
    """
    global _wb_fp, _wb_path
    temp = os.environ.get("TEMP") or os.environ.get("TMP") or "."
    candidates = [
        app_dir / "wb_arbitration_trace.txt",
        Path(temp) / "wb_arbitration_trace_mp02m.txt",
        Path.home() / "wb_arbitration_trace_mp02m.txt",
    ]
    with _lock:
        if _wb_fp is not None:
            return _wb_path
        for path in candidates:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                _wb_fp = open(path, "a", encoding="utf-8", errors="replace")
                _wb_path = path.resolve()
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                _wb_fp.write(f"\n### {ts} сеанс трассировки WB (арбитраж) ###\n")
                _wb_fp.write(f"### путь: {_wb_path} ###\n")
                _wb_fp.flush()
                return _wb_path
            except OSError:
                _wb_fp = None
                _wb_path = None
                continue
        return None


def init_com_listen_trace(app_dir: Path) -> Optional[Path]:
    """Файл com_listen_trace.txt — пассивная прослушка линии (тайминги, WB / быстрый Modbus)."""
    global _com_listen_fp, _com_listen_path
    path = app_dir / "com_listen_trace.txt"
    with _lock:
        if _com_listen_fp is not None:
            return _com_listen_path
        try:
            _com_listen_fp = open(path, "a", encoding="utf-8", errors="replace")
            _com_listen_path = path
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _com_listen_fp.write(f"\n### {ts} сеанс прослушки COM ###\n")
            _com_listen_fp.flush()
        except OSError:
            _com_listen_fp = None
            _com_listen_path = None
            return None
    return path


def close_com_listen_trace() -> None:
    global _com_listen_fp, _com_listen_path
    with _lock:
        if _com_listen_fp is not None:
            try:
                _com_listen_fp.close()
            except OSError:
                pass
            _com_listen_fp = None
            _com_listen_path = None


def append_com_listen(msg: str, *, also_main_log: bool = True) -> str:
    """Строка в com_listen_trace.txt (если файл открыт) и опционально в flasher_log [COM_listen]."""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}\n"
    with _lock:
        try:
            if _com_listen_fp is not None:
                _com_listen_fp.write(line)
                _com_listen_fp.flush()
        except OSError:
            pass
    if also_main_log:
        return append_line(f"[COM_listen] {msg}")
    return line


def close_wb_trace() -> None:
    global _wb_fp, _wb_path
    with _lock:
        if _wb_fp is not None:
            try:
                _wb_fp.close()
            except OSError:
                pass
            _wb_fp = None
            _wb_path = None


def wb_trace_path() -> Optional[Path]:
    return _wb_path


def append_wb_trace(msg: str) -> None:
    """Строка или многострочный блок. Если отдельный файл недоступен — дублирование в flasher_log.txt с [WB_ARB]."""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    lines = msg.splitlines() or [msg]
    with _lock:
        try:
            if _wb_fp is not None:
                for line in lines:
                    _wb_fp.write(f"[{ts}] {line}\n")
                _wb_fp.flush()
            elif _fp is not None:
                for line in lines:
                    _fp.write(f"[{ts}] [WB_ARB] {line}\n")
                _fp.flush()
        except OSError:
            pass


def log_path() -> Optional[Path]:
    return _log_path


def init_firmware_update_log(app_dir: Path) -> Optional[Path]:
    """Открыть firmware_update_log.txt на дозапись — только события прошивки (не полный обмен)."""
    global _fw_update_fp, _fw_update_path
    path = app_dir / "firmware_update_log.txt"
    with _lock:
        if _fw_update_fp is not None:
            return _fw_update_path
        try:
            _fw_update_fp = open(path, "a", encoding="utf-8", errors="replace")
            _fw_update_path = path
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _fw_update_fp.write(f"\n=== {ts} сеанс (обновления прошивок) ===\n")
            _fw_update_fp.flush()
        except OSError:
            _fw_update_fp = None
            _fw_update_path = None
            return None
    return path


def close_firmware_update_log() -> None:
    global _fw_update_fp, _fw_update_path
    with _lock:
        if _fw_update_fp is not None:
            try:
                _fw_update_fp.close()
            except OSError:
                pass
            _fw_update_fp = None
            _fw_update_path = None


def firmware_update_log_path() -> Optional[Path]:
    return _fw_update_path


def append_firmware_update(msg: str) -> str:
    """Строка в firmware_update_log.txt (если открыт). Безопасен из фонового потока."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}\n"
    with _lock:
        if _fw_update_fp is not None:
            try:
                _fw_update_fp.write(line)
                _fw_update_fp.flush()
            except OSError:
                pass
    return line


def append_line(msg: str) -> str:
    """
    Записать строку с меткой времени. Возвращает ту же строку с \\n (для дублирования в GUI при необходимости).
    Безопасен при вызове из фонового потока.
    """
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}\n"
    with _lock:
        if _fp is not None:
            try:
                _fp.write(line)
                _fp.flush()
            except OSError:
                pass
    return line
