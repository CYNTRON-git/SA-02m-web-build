"""test_58: пассивная диагностика железа SA-02m — кварцы, RTC, watchdog, периферия.

Реализация ТЗ «диагностика кварцев и периферии платы SA-02m» (lib/sa02m_hw_diag,
проверки C1–C12). Главный вопрос — можно ли плату перезагружать программно:
с мёртвым кварцем 32.768 кГц (стенд 136, 2026-10-21) она после «Rebooting.»
зависает до снятия питания. Тест только читает — ничего не пишет в железо,
не перезагружает, не открывает COM-порты (ТЗ §2).

Все проверки снимаются одним прогоном (фикстура модуля): замеры с окном —
ход RTC (C2) и рост ошибок Ethernet (C8) — идут параллельно с остальными.
Отчёт report.json + report.md пишется в STAND_HWDIAG_DIR (по умолчанию
/var/log/sa02m-test/hw-diag/<плата>-<время>/).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from lib import sa02m_hw_diag as hw

pytestmark = [pytest.mark.sa02m_platform]


def _report_dir(host: str) -> Path:
    base = Path(os.environ.get("STAND_HWDIAG_DIR", "/var/log/sa02m-test/hw-diag"))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"hw-diag-{host.replace('.', '_')}-{stamp}"
    try:
        base.mkdir(parents=True, exist_ok=True)
        return base / name
    except OSError:
        return Path(tempfile.gettempdir()) / name


@pytest.fixture(scope="module")
def hw_report(sa02m_platform_client):
    client = sa02m_platform_client
    report = hw.run_all(lambda cmd, t: client.run(cmd, timeout=t),
                        log=lambda s: print(f"[hw-diag] {s}"))
    out = _report_dir(client.host)
    j, m = hw.save_report(report, out)
    report["_paths"] = {"json": str(j), "md": str(m)}
    print(f"[hw-diag] отчёт: {m}")
    return report


def _apply(check: dict) -> None:
    line = f"{check['id']} {check['title']}: {check['verdict']} — {check['summary']}"
    print(line)
    if check["verdict"] == hw.FAIL:
        raw = json.dumps(check["raw"], ensure_ascii=False, default=str)[:1500]
        pytest.fail(f"{line}\nметод: {check['method']}\nсырые: {raw}")
    if check["verdict"] == hw.SKIP:
        pytest.skip(line)
    if check["verdict"] == hw.WARN:
        print(f"WARN: {line}")


@pytest.mark.fast
@pytest.mark.critical
def test_hwdiag_c1_clk32_reboot_safe(require_sa02m_platform, hw_report):
    """Кварц 32.768 кГц жив — плату можно перезагружать программно.

    На sunxi reboot — это сброс watchdog'ом, а watchdog считает от 32 кГц.
    FAIL здесь означает: программная перезагрузка (веб «Перезагрузка», OTA,
    смена ядра) зависнет, вернёт только снятие питания.
    """
    c1 = hw.check_of(hw_report, "C1")
    print(f"reboot_safe={hw_report['reboot_safe']}")
    _apply(c1)


@pytest.mark.fast
@pytest.mark.parametrize("cid", ["C2", "C3", "C4", "C5", "C6", "C7",
                                 "C8", "C9", "C10", "C11"])
def test_hwdiag_check(require_sa02m_platform, hw_report, cid):
    """C2 ход RTC, C3 24 МГц, C4 DS3231, C5 PCA9536, C6 шины I2C, C7 UART,
    C8 Ethernet, C9 USB, C10 eMMC, C11 питание/температура."""
    _apply(hw.check_of(hw_report, cid))


@pytest.mark.fast
def test_hwdiag_c12_kernel_errors(require_sa02m_platform, hw_report):
    """Сводка ошибок ядра по узлам — приложение для проверяющего, без вердикта."""
    c12 = hw.check_of(hw_report, "C12")
    print(f"C12: {c12['summary']}")
    for node, lines in c12["raw"].items():
        for ln in lines[-3:]:
            print(f"  [{node}] {ln}")


@pytest.mark.fast
def test_hwdiag_passive_only(require_sa02m_platform, hw_report):
    """Самоконтроль ТЗ §2: ни одной запрещённой команды (reboot, запись в
    железо, открытие COM-портов, потоковые -f/-w) в журнале диагностики."""
    bad = hw.forbidden_commands(hw_report["command_log"])
    print(f"команд выполнено: {len(hw_report['command_log'])}; "
          f"отчёт: {hw_report['_paths']['md']}")
    assert not bad, f"диагностика выполнила запрещённые команды: {bad}"
