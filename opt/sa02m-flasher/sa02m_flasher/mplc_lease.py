# -*- coding: utf-8 -*-
"""
Остановка служб, занимающих RS-485, на время сканирования/прошивки и гарантированное
восстановление по окончании (в том числе при аварийном завершении — см. signals в service.py,
ExecStopPost в systemd unit и глобальный _restore_all_on_exit()).

Используются команды:
  systemctl is-active <svc>
  systemctl stop      <svc>
  systemctl start     <svc>
  fuser /dev/<port>

Запуск — через sudo (правила в /etc/sudoers.d/sa02m-flasher, минимальный whitelist).
"""
from __future__ import annotations

import atexit
import logging
import shutil
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Set

log = logging.getLogger(__name__)

_LEASE_LOCK = threading.Lock()
_STOPPED_SERVICES: Set[str] = set()   # учёт глобально остановленных служб (для восстановления)


def _sudo() -> Optional[str]:
    """Путь к sudo; None если недоступен (в юнит-тестах/разработке)."""
    return shutil.which("sudo")


def _systemctl() -> Optional[str]:
    return shutil.which("systemctl")


def _fuser() -> Optional[str]:
    return shutil.which("fuser")


def _run(args: List[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def is_service_active(service: str) -> bool:
    systemctl = _systemctl()
    if not systemctl:
        return False
    res = _run([systemctl, "is-active", "--quiet", service], timeout=5.0)
    return res.returncode == 0


def stop_service(service: str) -> bool:
    systemctl = _systemctl()
    sudo = _sudo()
    if not systemctl:
        log.warning("systemctl не найден, пропускаю stop %s", service)
        return False
    cmd = [sudo, systemctl, "stop", service] if sudo else [systemctl, "stop", service]
    res = _run(cmd, timeout=15.0)
    ok = res.returncode == 0
    log.info("systemctl stop %s → rc=%d stderr=%r", service, res.returncode, (res.stderr or "").strip())
    return ok


def start_service(service: str) -> bool:
    systemctl = _systemctl()
    sudo = _sudo()
    if not systemctl:
        log.warning("systemctl не найден, пропускаю start %s", service)
        return False
    cmd = [sudo, systemctl, "start", service] if sudo else [systemctl, "start", service]
    res = _run(cmd, timeout=15.0)
    ok = res.returncode == 0
    log.info("systemctl start %s → rc=%d stderr=%r", service, res.returncode, (res.stderr or "").strip())
    return ok


def port_occupants(device_path: str) -> List[str]:
    """
    Вернуть список PID'ов, удерживающих /dev/<port>. Пустой список = свободен.
    Требует наличия fuser; если нет — возвращает пустой список (не блокируем операцию).
    """
    fuser = _fuser()
    if not fuser:
        return []
    sudo = _sudo()
    cmd = [sudo, fuser, device_path] if sudo else [fuser, device_path]
    try:
        res = _run(cmd, timeout=3.0)
    except subprocess.TimeoutExpired:
        return []
    if res.returncode != 0:
        # fuser без совпадений возвращает код 1 и пустой stdout.
        return []
    raw = (res.stdout or "").strip() + " " + (res.stderr or "").strip()
    pids = [tok for tok in raw.split() if tok.isdigit()]
    return pids


class PortBusyError(RuntimeError):
    def __init__(self, device_path: str, pids: Iterable[str]):
        self.device_path = device_path
        self.pids = list(pids)
        super().__init__(
            f"Порт {device_path} занят внешним процессом (PID {', '.join(self.pids)})."
            " Остановите процесс или освободите порт и попробуйте снова."
        )


@contextmanager
def port_lease(
    device_path: str,
    services_to_stop: Iterable[str],
    *,
    require_free: bool = True,
) -> Iterator[List[str]]:
    """
    Контекст-менеджер: останавливает указанные службы, проверяет освобождение порта,
    возвращает список фактически остановленных служб (для логов), по выходу — запускает их обратно.

    Безопасен при вложении (если одна служба уже в _STOPPED_SERVICES — не трогает её повторно).
    """
    device = str(device_path)
    stopped_now: List[str] = []
    with _LEASE_LOCK:
        for svc in list(services_to_stop):
            if not svc:
                continue
            if svc in _STOPPED_SERVICES:
                continue
            if is_service_active(svc):
                if stop_service(svc):
                    _STOPPED_SERVICES.add(svc)
                    stopped_now.append(svc)
    try:
        if require_free:
            pids = port_occupants(device)
            if pids:
                raise PortBusyError(device, pids)
        yield list(stopped_now)
    finally:
        with _LEASE_LOCK:
            for svc in reversed(stopped_now):
                try:
                    start_service(svc)
                finally:
                    _STOPPED_SERVICES.discard(svc)


def _restore_all_on_exit() -> None:
    """Страховочное восстановление для atexit и сигнал-хэндлеров service.py."""
    with _LEASE_LOCK:
        remaining = list(_STOPPED_SERVICES)
        _STOPPED_SERVICES.clear()
    for svc in reversed(remaining):
        try:
            start_service(svc)
        except Exception:
            log.exception("Не удалось восстановить службу %s при завершении", svc)


atexit.register(_restore_all_on_exit)


def device_path_exists(device_path: str) -> bool:
    try:
        return Path(device_path).exists()
    except OSError:
        return False
