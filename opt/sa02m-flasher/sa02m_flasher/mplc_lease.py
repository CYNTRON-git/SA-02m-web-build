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


def _service_candidates(service: str) -> List[str]:
    raw = str(service or "").strip()
    if not raw:
        return []
    candidates: List[str] = []

    def add(name: str) -> None:
        name = str(name or "").strip()
        if name and name not in candidates:
            candidates.append(name)

    add(raw)
    bare = raw[:-8] if raw.endswith(".service") else raw
    add(bare)
    add(f"{bare}.service")
    if bare == "mplc":
        add("mplc4")
        add("mplc4.service")
    elif bare == "mplc4":
        add("mplc")
        add("mplc.service")
    return candidates


def service_load_state(service: str) -> str:
    systemctl = _systemctl()
    if not systemctl:
        return "unknown"
    res = _run([systemctl, "show", "-p", "LoadState", "--value", service], timeout=5.0)
    if res.returncode != 0:
        return "unknown"
    return (res.stdout or "").strip() or "unknown"


def service_exists(service: str) -> bool:
    return service_load_state(service) != "not-found"


def resolve_service_name(service: str) -> Optional[str]:
    for candidate in _service_candidates(service):
        if service_exists(candidate):
            return candidate
    return None


def active_service_name(service: str) -> Optional[str]:
    systemctl = _systemctl()
    if not systemctl:
        return None
    for candidate in _service_candidates(service):
        res = _run([systemctl, "is-active", "--quiet", candidate], timeout=5.0)
        if res.returncode == 0:
            return candidate
    return None


def is_service_active(service: str) -> bool:
    return active_service_name(service) is not None


def stop_service(service: str) -> bool:
    systemctl = _systemctl()
    sudo = _sudo()
    if not systemctl:
        log.warning("systemctl не найден, пропускаю stop %s", service)
        return False
    actual = resolve_service_name(service)
    if not actual:
        log.info("Служба %s не найдена, stop пропущен", service)
        return False
    cmd = [sudo, systemctl, "stop", actual] if sudo else [systemctl, "stop", actual]
    res = _run(cmd, timeout=15.0)
    ok = res.returncode == 0
    log.info("systemctl stop %s (%s) → rc=%d stderr=%r", service, actual, res.returncode, (res.stderr or "").strip())
    return ok


def start_service(service: str) -> bool:
    systemctl = _systemctl()
    sudo = _sudo()
    if not systemctl:
        log.warning("systemctl не найден, пропускаю start %s", service)
        return False
    actual = resolve_service_name(service)
    if not actual:
        log.info("Служба %s не найдена, start пропущен", service)
        return False
    cmd = [sudo, systemctl, "start", actual] if sudo else [systemctl, "start", actual]
    res = _run(cmd, timeout=15.0)
    ok = res.returncode == 0
    log.info("systemctl start %s (%s) → rc=%d stderr=%r", service, actual, res.returncode, (res.stderr or "").strip())
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


def released_services() -> List[str]:
    with _LEASE_LOCK:
        return sorted(_STOPPED_SERVICES)


def release_pollers(services_to_stop: Iterable[str]) -> dict:
    stopped_now: List[str] = []
    already_released: List[str] = []
    inactive: List[str] = []
    missing: List[str] = []
    failed: List[str] = []

    with _LEASE_LOCK:
        for svc in list(services_to_stop):
            actual = resolve_service_name(svc)
            if not actual:
                missing.append(svc)
                continue
            if actual in _STOPPED_SERVICES:
                already_released.append(actual)
                continue
            if not is_service_active(actual):
                inactive.append(actual)
                continue
            if stop_service(actual):
                _STOPPED_SERVICES.add(actual)
                stopped_now.append(actual)
            else:
                failed.append(actual)

    return {
        "stopped_now": stopped_now,
        "already_released": already_released,
        "inactive": inactive,
        "missing": missing,
        "failed": failed,
    }


def restore_pollers(services_to_start: Iterable[str]) -> dict:
    restarted: List[str] = []
    already_running: List[str] = []
    not_released: List[str] = []
    missing: List[str] = []
    failed: List[str] = []

    with _LEASE_LOCK:
        for svc in list(services_to_start):
            actual = resolve_service_name(svc)
            if not actual:
                missing.append(svc)
                continue
            if actual not in _STOPPED_SERVICES:
                if is_service_active(actual):
                    already_running.append(actual)
                else:
                    not_released.append(actual)
                continue
            if start_service(actual):
                _STOPPED_SERVICES.discard(actual)
                restarted.append(actual)
            else:
                failed.append(actual)

    return {
        "restarted": restarted,
        "already_running": already_running,
        "not_released": not_released,
        "missing": missing,
        "failed": failed,
    }


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
            actual = resolve_service_name(svc)
            if not actual:
                continue
            if actual in _STOPPED_SERVICES:
                continue
            if is_service_active(actual):
                if stop_service(actual):
                    _STOPPED_SERVICES.add(actual)
                    stopped_now.append(actual)
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
