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
import time
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


def _timeout_cmd() -> Optional[str]:
    return shutil.which("timeout")


def _pkill() -> Optional[str]:
    return shutil.which("pkill")


def _run(args: List[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _service_unit_files(service: str) -> List[str]:
    bare = str(service or "").strip()
    if bare.endswith(".service"):
        bare = bare[:-8]
    if not bare:
        return []
    return [
        f"/etc/systemd/system/{bare}.service",
        f"/lib/systemd/system/{bare}.service",
        f"/usr/lib/systemd/system/{bare}.service",
    ]


def _service_file_exists(service: str) -> bool:
    return any(Path(p).exists() for p in _service_unit_files(service))


def _is_mplc_family(service: str) -> bool:
    bare = str(service or "").strip()
    if bare.endswith(".service"):
        bare = bare[:-8]
    return bare in {"mplc", "mplc4"}


def _mplc_process_names(service: str) -> List[str]:
    bare = str(service or "").strip()
    if bare.endswith(".service"):
        bare = bare[:-8]
    if bare == "mplc4":
        return ["mplc4", "mplc_monitor"]
    return ["mplc", "mplc4", "mplc_monitor"]


def _proc_is_running(name: str) -> bool:
    try:
        res = _run(["pgrep", "-x", name], timeout=2.0)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return res.returncode == 0


def _pkill_names(names: List[str]) -> bool:
    pkill = _pkill()
    if not pkill:
        return False
    any_match = False
    for name in names:
        try:
            res = _run([pkill, "-x", name], timeout=5.0)
        except subprocess.TimeoutExpired:
            continue
        if res.returncode == 0:
            any_match = True
    return any_match


def _pkill_pattern(pattern: str) -> bool:
    pkill = _pkill()
    if not pkill:
        return False
    try:
        res = _run([pkill, "-f", pattern], timeout=5.0)
    except subprocess.TimeoutExpired:
        return False
    return res.returncode == 0


def _is_mqtt_bridge_service(service: str) -> bool:
    s = str(service or "").lower()
    return "modbus-mqtt" in s or "modbus_mqtt" in s


def _mqtt_bridge_process_active() -> bool:
    try:
        res = _run(["pgrep", "-f", "[m]odbus_mqtt_bridge"], timeout=2.0)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return res.returncode == 0


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
    if _is_mplc_family(service):
        if _service_file_exists("mplc4"):
            return "loaded"
        if _service_file_exists("mplc"):
            return "loaded"
        return "not-found"
    systemctl = _systemctl()
    if not systemctl:
        return "unknown"
    try:
        res = _run([systemctl, "show", "-p", "LoadState", "--value", service], timeout=1.5)
    except subprocess.TimeoutExpired:
        log.warning("systemctl show LoadState завис для %s", service)
        return "unknown"
    if res.returncode != 0:
        return "unknown"
    return (res.stdout or "").strip() or "unknown"


def service_exists(service: str) -> bool:
    return service_load_state(service) != "not-found"


def resolve_service_name(service: str) -> Optional[str]:
    if _is_mplc_family(service):
        if _proc_is_running("mplc4") or _proc_is_running("mplc_monitor"):
            if _service_file_exists("mplc4"):
                return "mplc4.service"
            return "mplc4"
        if _proc_is_running("mplc"):
            if _service_file_exists("mplc"):
                return "mplc.service"
            return "mplc"
        if _service_file_exists("mplc4"):
            return "mplc4.service"
        if _service_file_exists("mplc"):
            return "mplc.service"
        return None
    for candidate in _service_candidates(service):
        if service_exists(candidate):
            return candidate
    return None


def active_service_name(service: str) -> Optional[str]:
    if _is_mplc_family(service):
        if _proc_is_running("mplc4") or _proc_is_running("mplc_monitor"):
            return "mplc4.service" if _service_file_exists("mplc4") else "mplc4"
        if _proc_is_running("mplc"):
            return "mplc.service" if _service_file_exists("mplc") else "mplc"
        return None
    systemctl = _systemctl()
    if not systemctl:
        return None
    for candidate in _service_candidates(service):
        try:
            res = _run([systemctl, "is-active", "--quiet", candidate], timeout=1.5)
        except subprocess.TimeoutExpired:
            log.warning("systemctl is-active завис для %s", candidate)
            continue
        if res.returncode == 0:
            return candidate
    return None


def is_service_active(service: str) -> bool:
    if _is_mqtt_bridge_service(service) and _mqtt_bridge_process_active():
        return True
    return active_service_name(service) is not None


def stop_service(service: str) -> bool:
    actual = resolve_service_name(service)
    if not actual:
        log.info("Служба %s не найдена, stop пропущен", service)
        return False
    if _is_mqtt_bridge_service(actual) or _is_mqtt_bridge_service(service):
        systemctl = _systemctl()
        sudo = _sudo()
        ok = False
        if systemctl:
            cmd = [sudo, systemctl, "stop", actual] if sudo else [systemctl, "stop", actual]
            try:
                res = _run(cmd, timeout=8.0)
                ok = res.returncode == 0
                log.info(
                    "systemctl stop %s (%s) → rc=%d stderr=%r",
                    service,
                    actual,
                    res.returncode,
                    (res.stderr or "").strip(),
                )
            except subprocess.TimeoutExpired:
                log.warning("systemctl stop %s (%s) завис; пробую pkill modbus_mqtt_bridge", service, actual)
        if _mqtt_bridge_process_active():
            killed = _pkill_pattern("modbus_mqtt_bridge")
            still = _mqtt_bridge_process_active()
            ok = killed or not still
            log.info(
                "pkill modbus_mqtt_bridge (%s) → killed=%s still_active=%s",
                actual,
                killed,
                still,
            )
        return ok
    if _is_mplc_family(actual):
        names = _mplc_process_names(actual)
        killed = _pkill_names(names)
        still_active = any(_proc_is_running(name) for name in names)
        ok = killed or not still_active
        log.info(
            "direct stop %s (%s) via pkill → killed=%s still_active=%s",
            service,
            actual,
            killed,
            still_active,
        )
        return ok
    systemctl = _systemctl()
    sudo = _sudo()
    if not systemctl:
        log.warning("systemctl не найден, пропускаю stop %s", service)
        return False
    cmd = [sudo, systemctl, "stop", actual] if sudo else [systemctl, "stop", actual]
    try:
        res = _run(cmd, timeout=3.0)
        ok = res.returncode == 0
        log.info("systemctl stop %s (%s) → rc=%d stderr=%r", service, actual, res.returncode, (res.stderr or "").strip())
        if ok:
            return True
    except subprocess.TimeoutExpired:
        log.warning("systemctl stop %s (%s) завис; пробую pkill fallback", service, actual)

    if _is_mplc_family(actual):
        names = _mplc_process_names(actual)
        killed = _pkill_names(names)
        still_active = any(_proc_is_running(name) for name in names)
        ok = killed or not still_active
        log.info("pkill fallback stop %s (%s) → killed=%s still_active=%s", service, actual, killed, still_active)
        return ok
    return False


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
    try:
        res = _run(cmd, timeout=3.0)
    except subprocess.TimeoutExpired:
        log.warning("systemctl start %s (%s) завис", service, actual)
        return False
    ok = res.returncode == 0
    log.info("systemctl start %s (%s) → rc=%d stderr=%r", service, actual, res.returncode, (res.stderr or "").strip())
    return ok


def _pid_cmdline(pid: str) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def _poll_label_from_cmdline(cmd: str) -> str:
    c = (cmd or "").lower()
    if not c or "agetty" in c or "getty" in c or "/bin/login" in c:
        return ""
    if "modbus_mqtt_bridge" in c or "sa02m-modbus-mqtt" in c:
        return "MQTT"
    if "mplc_daemon" in c or "mplc_monitor" in c or "mplc4" in c:
        return "MPLC4"
    if "mplc" in c:
        return "MPLC4"
    if "sa02m-flasher" in c or "flasher" in c:
        return "Flasher"
    return ""


def port_poll_service_labels(device_path: str) -> List[str]:
    """Какая служба реально держит /dev/COM* (по fuser + cmdline), не глобальный systemd MPLC."""
    labels: List[str] = []
    seen: Set[str] = set()
    for pid in port_occupants(device_path):
        label = _poll_label_from_cmdline(_pid_cmdline(pid))
        if not label:
            continue
        if label not in seen:
            seen.add(label)
            labels.append(label)
    return labels


def port_occupants(device_path: str) -> List[str]:
    """
    Вернуть список PID'ов, удерживающих /dev/<port>. Пустой список = свободен.
    Требует наличия fuser; если нет — возвращает пустой список (не блокируем операцию).
    """
    fuser = _fuser()
    if not fuser:
        return []
    sudo = _sudo()
    timeout_cmd = _timeout_cmd()
    cmd: List[str] = []
    if timeout_cmd:
        cmd.extend([timeout_cmd, "-s", "KILL", "1"])
    if sudo:
        cmd.append(sudo)
    cmd.extend([fuser, device_path])
    try:
        res = _run(cmd, timeout=2.0 if timeout_cmd else 1.0)
    except (subprocess.TimeoutExpired, PermissionError):
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


def is_port_poll_free(device_path: str) -> bool:
    """
    Порт свободен для flasher: fuser пуст (ни MPLC, ни MQTT не держат этот /dev/COM*).
    Не смотрит на глобальный systemctl active — только фактическое удержание порта.
    """
    if not device_path_exists(device_path):
        return False
    return not port_occupants(device_path)


def wait_port_poll_free(device_path: str, timeout_s: float = 4.0) -> bool:
    """Ждать освобождения /dev/COM* после systemctl stop / pkill (MQTT отпускает порт с задержкой)."""
    deadline = time.monotonic() + max(0.1, float(timeout_s))
    while time.monotonic() < deadline:
        if is_port_poll_free(device_path):
            return True
        time.sleep(0.1)
    return is_port_poll_free(device_path)


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
    preserve_released: bool = False,
) -> Iterator[List[str]]:
    """
    Контекст-менеджер: останавливает указанные службы, проверяет освобождение порта,
    возвращает список фактически остановленных служб (для логов), по выходу — запускает их обратно.

    Безопасен при вложении (если одна служба уже в _STOPPED_SERVICES — не трогает её повторно).

    Быстрый путь: если fuser пуст и опросчики (MPLC/MQTT) не держат этот порт — не вызываем
    systemctl stop/start глобальных служб (экономит ~3–4 с на каждом скане).
    """
    device = str(device_path)
    stopped_now: List[str] = []
    preserved: Set[str] = set()
    if preserve_released:
        with _LEASE_LOCK:
            preserved = set(_STOPPED_SERVICES)
    poll_free = False
    with _LEASE_LOCK:
        poll_free = is_port_poll_free(device)
        if poll_free:
            log.debug("port_lease: быстрый путь для %s (порт не удерживается опросчиками)", device)
        else:
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
        if require_free and not wait_port_poll_free(device):
            pids = port_occupants(device)
            if pids:
                raise PortBusyError(device, pids)
        yield list(stopped_now)
    finally:
        with _LEASE_LOCK:
            for svc in reversed(stopped_now):
                if preserve_released and svc in preserved:
                    continue
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
