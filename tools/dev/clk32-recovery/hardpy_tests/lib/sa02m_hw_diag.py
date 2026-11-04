"""Пассивная диагностика железа SA-02m: кварцы, RTC, watchdog, периферия.

Реализация ТЗ «диагностика кварцев и периферии платы SA-02m» (C1–C12).
Повод: плата 136 зависала после «Rebooting.» на каждой программной
перезагрузке — умер внешний кварц 32.768 кГц. На sunxi программная
перезагрузка И ЕСТЬ сброс watchdog'ом, а watchdog считает от 32 кГц: без
кварца он не досчитывает, и плату возвращает только снятие питания. Разбор:
SA-02m-web-build `.ai-dev/notes/reboot-hang-2026-10-21.md` (коммит 42f3518a).

Правила (ТЗ §2) — модуль их соблюдает конструктивно:
  * только чтение: никаких reboot/poweroff, записи в /dev/mem, i2cset/gpioset,
    открытия /dev/ttyS*, остановки служб, правок /etc;
  * каждая команда, способная зависнуть, — под `timeout N`;
  * PCA9536 читается под тем же flock, что у веб-панели (/etc/sa02m_hw.conf);
  * без потоковых команд (-f/-w): длинные потоки по SSH подвешивают плату.
Все выполненные команды пишутся в ``report["command_log"]`` — по нему
приёмка проверяет, что запрещённых команд не было (tests/test_sa02m_hw_diag).

Сбор отделён от анализа: ``collect_*`` выполняет команды через ``run(cmd,
timeout) -> (code, out, err)`` (PlatformClient.run, ssh или локальный
subprocess), ``analyze_*`` — чистые функции над сырыми данными.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

Runner = Callable[[str, float], "tuple[int, str, str]"]

PASS, WARN, FAIL, SKIP, INFO = "PASS", "WARN", "FAIL", "SKIP", "INFO"

# --- Регистры A40i (sun8i-r40) ------------------------------------------------
# RTC @0x01C20400: LOSC_CTRL +0x00, LOSC_AUTO_SWT_STA +0x04.
#   AUTO_SWT_STA bit0 = работаем от внешнего 32 кГц; bit2 = внешний генератор
#   потерян, железо само перешло на внутренний RC. Эталон: 135 → 0x1, 136 → 0x4.
# WDT0 @0x01C20C90: CTRL +0x00 (bit0 restart самосбрасывается при живых часах),
#   CFG +0x04, MODE +0x08. Только ЧТЕНИЕ — пинг watchdog'а это запись (ТЗ §2).
REG_LOSC_CTRL = 0x01C20400
REG_LOSC_AUTO_SWT_STA = 0x01C20404
REG_WDT_CTRL = 0x01C20C90
REG_WDT_CFG = 0x01C20C94
REG_WDT_MODE = 0x01C20C98

REBOOT_HANG_TEXT = ("Программная перезагрузка этой платы зависнет. "
                    "Только снятие питания.")

# Порты RS-485 по варианту (README web-build § «Аппаратные варианты»).
VARIANT_TTYS = {
    "sa02m-1eth": (0, 3, 4, 5, 7),
    "sa02m-2eth": (3, 4, 5, 7),
}
VARIANT_ETH_COUNT = {"sa02m-1eth": 1, "sa02m-2eth": 2}

# TWI-контроллеры A40i: адрес в dmesg → номер шины.
I2C_BUS_BY_ADDR = {"1c2ac00": "i2c-0", "1c2b000": "i2c-1", "1c2b400": "i2c-2"}

# Пороги, помеченные в ТЗ «(калибровать)»: уточняются по эталону 135.
CHRONY_PPM_WARN = 100.0
CHRONY_PPM_FAIL = 500.0
TEMP_WARN_C = 80.0
TEMP_FAIL_C = 90.0
DS3231_SKEW_WARN_S = 5
UART_ERR_RATIO_WARN = 0.01
UART_MIN_RX_FOR_RATIO = 100
REGULATOR_DEVIATION_WARN = 0.05


@dataclass
class Check:
    id: str
    title: str
    verdict: str
    summary: str
    raw: Any = field(default_factory=dict)
    method: str = ""


class _Recorder:
    """Обёртка над run: журнал команд + единый формат ответа."""

    def __init__(self, run: Runner):
        self._run = run
        self.log: list[str] = []

    def __call__(self, cmd: str, timeout: float = 15.0) -> tuple[int, str]:
        self.log.append(cmd)
        try:
            code, out, _err = self._run(cmd, timeout)
        except Exception as exc:  # noqa: BLE001 — сбой транспорта = нет данных
            return 255, f"__ERR__ {exc}"
        return int(code), out or ""


def _hx(v: int | None) -> str:
    return "—" if v is None else f"0x{v:08x}"


# =============================================================================
# Паспорт платы и тип несущей (§3)
# =============================================================================
_BOARD_CMD = r"""
echo "H=$(hostname)"
echo "IP=$(ip -4 -br addr 2>/dev/null | awk '$1!="lo"{print $1":"$3}' | tr '\n' ' ')"
. /etc/sa02m_hw_variant.conf 2>/dev/null; echo "VARIANT=${SA02M_HW_VARIANT:-}"
echo "COMPAT=$(tr '\0' ' ' < /proc/device-tree/compatible 2>/dev/null)"
echo "MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null)"
echo "KERNEL=$(uname -r)"
echo "WEBVER=$(grep -E '^[0-9]+(\.[0-9]+)+' /var/www/network_config/VERSION 2>/dev/null | head -1)"
for i in /sys/class/net/eth* /sys/class/net/end*; do [ -e "$i" ] && echo "MAC=$(basename $i):$(cat $i/address)"; done
echo "UPTIME=$(cut -d' ' -f1 /proc/uptime)"
echo "RTC1=$([ -e /dev/rtc1 ] && echo yes || echo no)"
"""


def collect_board(rec: _Recorder) -> dict:
    _, out = rec(_BOARD_CMD, 10)
    b: dict[str, Any] = {"mac": {}}
    for ln in out.splitlines():
        if "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        v = v.strip()
        if k == "MAC" and ":" in v:
            iface, mac = v.split(":", 1)
            b["mac"][iface] = mac
        else:
            b[k.lower()] = v
    try:
        b["uptime_s"] = float(b.pop("uptime", "0") or 0)
    except ValueError:
        b["uptime_s"] = 0.0
    if b.get("variant") not in VARIANT_TTYS:
        b["variant_assumed"] = True
        b["variant"] = b.get("variant") or "sa02m-1eth"
    return b


def detect_carrier(board: dict, dmesg: str) -> tuple[str, str]:
    """«debug» — отладочная несущая (нет устройств I2C, шторм bus locked)."""
    locked = len(re.findall(r"I2C bus locked", dmesg))
    if board.get("rtc1") != "yes" and locked >= 3:
        return "debug", f"нет /dev/rtc1, в dmesg «I2C bus locked» ×{locked}"
    return "standard", f"/dev/rtc1={board.get('rtc1')}, «I2C bus locked» ×{locked}"


# =============================================================================
# C1. Кварц 32.768 кГц и watchdog — главная проверка
# =============================================================================
_REGS_CMD = r"""timeout 10 python3 - <<'PY'
import mmap, os, struct, time
try:
    fd = os.open('/dev/mem', os.O_RDONLY | os.O_SYNC)
except OSError as e:
    print('ERR open /dev/mem: %s' % e); raise SystemExit
def rd(a):
    base = a & ~0xfff
    m = mmap.mmap(fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ, offset=base)
    try:
        return struct.unpack_from('<I', m, a - base)[0]
    finally:
        m.close()
try:
    for name, a in (('LOSC_CTRL', 0x01C20400), ('LOSC_AUTO_SWT_STA', 0x01C20404),
                    ('WDT_CFG', 0x01C20C94), ('WDT_MODE', 0x01C20C98)):
        print('%s=0x%08x' % (name, rd(a)))
    for i in range(3):
        print('WDT_CTRL_%d=0x%08x' % (i, rd(0x01C20C90)))
        time.sleep(0.5)
except OSError as e:
    print('ERR read: %s' % e)
PY"""


def collect_c1(rec: _Recorder) -> dict:
    code, out = rec(_REGS_CMD, 15)
    regs: dict[str, Any] = {"exit": code, "error": None}
    for ln in out.splitlines():
        ln = ln.strip()
        if ln.startswith("ERR") or ln.startswith("__ERR__"):
            regs["error"] = ln
        elif "=0x" in ln:
            k, v = ln.split("=", 1)
            try:
                regs[k] = int(v, 16)
            except ValueError:
                pass
    return regs


def analyze_c1(regs: dict) -> Check:
    method = ("Чтение /dev/mem (только PROT_READ): LOSC_AUTO_SWT_STA @0x01C20404, "
              "LOSC_CTRL @0x01C20400; WDT_CTRL @0x01C20C90 ×3 через 0.5 с")
    sta = regs.get("LOSC_AUTO_SWT_STA")
    if sta is None:
        return Check("C1", "Кварц 32 кГц / watchdog", SKIP,
                     f"регистры не прочитаны: {regs.get('error') or 'нет данных'}",
                     regs, method)
    wdt = [regs.get(f"WDT_CTRL_{i}") for i in range(3)]
    wdt_stuck = all(v is not None and (v & 1) for v in wdt)
    raw = {k: (_hx(v) if isinstance(v, int) else v) for k, v in regs.items()
           if k != "exit"}
    raw["wdt_restart_bit_stuck"] = wdt_stuck
    ext_ok, ext_lost = bool(sta & 0x1), bool(sta & 0x4)
    if ext_lost and not ext_ok:
        summary = (f"внешний кварц 32.768 кГц ОСТАНОВЛЕН (LOSC_AUTO_SWT_STA={_hx(sta)}: "
                   "бит2 — аппаратный переход на внутренний RC). " + REBOOT_HANG_TEXT)
        if wdt_stuck:
            summary += " Подтверждение: restart-бит WDT_CTRL не сбрасывается — watchdog без часов."
        return Check("C1", "Кварц 32 кГц / watchdog", FAIL, summary, raw, method)
    if ext_ok and not ext_lost:
        if wdt_stuck:
            return Check("C1", "Кварц 32 кГц / watchdog", WARN,
                         f"кварц в работе ({_hx(sta)}), но restart-бит WDT_CTRL "
                         "залип в 1 — проверить watchdog", raw, method)
        return Check("C1", "Кварц 32 кГц / watchdog", PASS,
                     f"внешний 32.768 кГц в работе (LOSC_AUTO_SWT_STA={_hx(sta)})",
                     raw, method)
    return Check("C1", "Кварц 32 кГц / watchdog", SKIP,
                 f"неоднозначное состояние LOSC_AUTO_SWT_STA={_hx(sta)} "
                 f"(LOSC_CTRL={_hx(regs.get('LOSC_CTRL'))}) — нужен разбор вручную",
                 raw, method)


# =============================================================================
# C2. Ход внутреннего RTC (rtc0) против монотонного uptime
# =============================================================================
# В ядре СА-02м у rtc0 (sun6i) нет атрибута since_epoch (калибровка на 135 и
# 136, 2026-10-22) — тогда читаем ioctl'ом через hwclock (только чтение).
# hwclock ждёт фронта секунды, поэтому uptime снимается сразу за ним.
_RTC_SAMPLE_CMD = r"""if [ -r /sys/class/rtc/rtc0/since_epoch ]; then echo "RTC=$(cat /sys/class/rtc/rtc0/since_epoch)"
else R=$(timeout 5 hwclock --show --utc -f /dev/rtc0 2>/dev/null); [ -n "$R" ] && echo "RTC=$(date -d "$R" +%s 2>/dev/null)"; fi
echo "UP=$(cut -d' ' -f1 /proc/uptime)"
"""


def rtc_sample(rec: _Recorder) -> tuple[int | None, float | None]:
    _, out = rec(_RTC_SAMPLE_CMD, 10)
    tags = dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)
    try:
        return int(tags["RTC"]), float(tags["UP"])
    except (KeyError, ValueError):
        return None, None


def analyze_c2(samples: list[tuple[int | None, float | None]]) -> Check:
    """k = Δrtc/Δuptime по сегментам; сегмент со скачком (ядро пишет rtc0
    раз в 11 мин при NTP-синхронизации) исключается."""
    method = ("rtc0/since_epoch против /proc/uptime, сегменты ~10 с; сегменты "
              "со ступенькой >max(5 с, 30% сегмента) — запись ядром rtc0 при "
              "NTP-синхронизации (раз в 11 мин) — исключаются")
    pts = [(r, u) for r, u in samples if r is not None and u is not None]
    if len(pts) < 2:
        return Check("C2", "Ход RTC rtc0", SKIP,
                     "rtc0 не читается (нет /sys/class/rtc/rtc0/since_epoch)",
                     {"samples": samples}, method)
    d_rtc = d_up = 0.0
    jumps = []
    for (r0, u0), (r1, u1) in zip(pts, pts[1:]):
        dr, du = r1 - r0, u1 - u0
        if du <= 0:
            continue
        # Скачок — резкая ступенька, а не плавный дрейф: даже k≈0.75 даёт на
        # сегменте не больше 0.25·Δu, поэтому порог относительный.
        if abs(dr - du) > max(5.0, 0.3 * du):
            jumps.append({"d_rtc": dr, "d_uptime": round(du, 1)})
            continue
        d_rtc += dr
        d_up += du
    if d_up < 30:
        return Check("C2", "Ход RTC rtc0", SKIP,
                     f"мало чистого интервала ({d_up:.0f} с) — повторить замер",
                     {"samples": pts, "jumps": jumps}, method)
    k = d_rtc / d_up
    tol = max(0.01, 2.0 / d_up)  # 1 с разрешения на каждом конце
    raw = {"d_rtc_s": d_rtc, "d_uptime_s": round(d_up, 1), "k": round(k, 4),
           "tolerance": round(tol, 4), "jumps_excluded": jumps}
    if abs(k - 1.0) <= tol:
        return Check("C2", "Ход RTC rtc0", PASS,
                     f"k={k:.4f} (допуск ±{tol:.3f} за {d_up:.0f} с)", raw, method)
    return Check("C2", "Ход RTC rtc0", FAIL,
                 f"rtc0 идёт с коэффициентом k={k:.3f} — не от кварца 32 кГц "
                 "(на внутреннем RC ≈0.8×, картина неисправного кварца)", raw, method)


# =============================================================================
# C3. Основной кварц 24 МГц (точность по chrony)
# =============================================================================
def collect_c3(rec: _Recorder) -> dict:
    code, out = rec("timeout 5 chronyc tracking", 8)
    raw: dict[str, Any] = {"exit": code}
    for ln in out.splitlines():
        if ":" in ln:
            k, v = ln.split(":", 1)
            raw[k.strip()] = v.strip()
    _, clk = rec("[ -r /sys/kernel/debug/clk/clk_summary ] && timeout 5 grep -iE "
                 "'osc24M|osc32k|losc' /sys/kernel/debug/clk/clk_summary || true", 8)
    if clk.strip():
        raw["clk_summary"] = clk.strip()
    return raw


def analyze_c3(raw: dict) -> Check:
    method = "chronyc tracking: Frequency (ppm), Leap status"
    leap = raw.get("Leap status")
    if raw.get("exit") not in (0,) or leap is None:
        return Check("C3", "Кварц 24 МГц (точность)", SKIP,
                     "chronyc недоступен", raw, method)
    if leap != "Normal":
        return Check("C3", "Кварц 24 МГц (точность)", SKIP,
                     f"chrony не синхронизирован (Leap status={leap})", raw, method)
    m = re.search(r"([-\d.]+)\s*ppm\s*(slow|fast)?", raw.get("Frequency", ""))
    if not m:
        return Check("C3", "Кварц 24 МГц (точность)", SKIP,
                     f"не разобрано Frequency={raw.get('Frequency')!r}", raw, method)
    ppm = abs(float(m.group(1)))
    raw["ppm_abs"] = ppm
    if ppm <= CHRONY_PPM_WARN:
        v = PASS
    elif ppm <= CHRONY_PPM_FAIL:
        v = WARN
    else:
        v = FAIL
    return Check("C3", "Кварц 24 МГц (точность)", v,
                 f"уход частоты {ppm:.3f} ppm (PASS ≤{CHRONY_PPM_WARN:.0f}, "
                 f"FAIL >{CHRONY_PPM_FAIL:.0f})", raw, method)


# =============================================================================
# C4. Внешний RTC DS3231 (i2c-1 @0x68)
# =============================================================================
# В ядре СА-02м нет CONFIG_RTC_DRV_DS1307: /dev/rtc1 обычно НЕ появляется
# (калибровка на штатной 135), DS3231 читается напрямую с i2c-1 @0x68.
# i2cget без -f допустим, пока чип не занят драйвером (ТЗ запрещает только -f).
DS3231_REGS = (0, 1, 2, 4, 5, 6, 15)


def collect_c4(rec: _Recorder, carrier: str = "standard") -> dict:
    i2c = ("echo SRC=i2c; for r in " + " ".join(map(str, DS3231_REGS))
           + "; do echo \"R$r=$(timeout 2 i2cget -y 1 0x68 $r 2>&1)\"; done")
    if carrier == "debug":
        i2c = "echo SRC=none"
    code, out = rec("if [ -e /dev/rtc1 ]; then echo SRC=rtc1; "
                    "R=$(timeout 5 hwclock --show --utc -f /dev/rtc1 2>&1); "
                    "echo \"HW=$R\"; echo \"RTC=$(date -d \"$R\" +%s 2>/dev/null)\"; "
                    f"else {i2c}; fi; echo \"SYS=$(date +%s)\"", 25)
    raw: dict[str, Any] = {"exit": code, "regs": {}}
    for ln in out.splitlines():
        if "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        v = v.strip()
        if k == "SRC":
            raw["source"] = v
        elif k == "HW":
            raw["hwclock"] = v[:120]
        elif k in ("RTC", "SYS"):
            raw["rtc_epoch" if k == "RTC" else "sys_epoch"] = int(v) if v.isdigit() else None
        elif k.startswith("R") and k[1:].isdigit():
            raw["regs"][int(k[1:])] = v
    return raw


def _bcd(v: int) -> int:
    return (v >> 4) * 10 + (v & 0x0F)


def _ds3231_epoch(regs: dict) -> tuple[int | None, int | None, str]:
    """(epoch UTC, OSF, ошибка) из регистров 0x00..0x06 и 0x0F."""
    try:
        r = {k: int(v, 16) for k, v in regs.items()}
    except (TypeError, ValueError):
        return None, None, "чтение не удалось: " + "; ".join(
            f"0x{k:02x}={v}" for k, v in sorted(regs.items()) if not str(v).startswith("0x"))[:160]
    if not all(k in r for k in DS3231_REGS):
        return None, None, "прочитаны не все регистры"
    try:
        dt = datetime(2000 + _bcd(r[6]), _bcd(r[5] & 0x1F), _bcd(r[4] & 0x3F),
                      _bcd(r[2] & 0x3F), _bcd(r[1] & 0x7F), _bcd(r[0] & 0x7F))
    except ValueError as exc:
        return None, (r[15] >> 7) & 1, f"недопустимое время в регистрах: {exc}"
    # Чип держит UTC (см. test_62).
    return int(dt.replace(tzinfo=timezone.utc).timestamp()), (r[15] >> 7) & 1, ""


def analyze_c4(raw: dict, dmesg: str, carrier: str) -> Check:
    method = ("/dev/rtc1 (hwclock) или, без драйвера ядра, DS3231 напрямую: i2cget "
              "-y 1 0x68 рег. 0x00–0x06, 0x0F (OSF); сверка с date +%s в эпохах")
    bad = [ln for ln in dmesg.splitlines()
           if re.search(r"ds323|rtc-ds1307", ln, re.I)
           and re.search(r"oscillator|discontinu|stop", ln, re.I)]
    raw = dict(raw, dmesg_osc=bad[-5:])
    src = raw.get("source")
    if src in (None, "none"):
        if carrier == "debug":
            return Check("C4", "RTC DS3231", SKIP, "отладочная несущая (нет I2C-устройств)",
                         raw, method)
        return Check("C4", "RTC DS3231", FAIL, "DS3231 не найден", raw, method)
    if src == "i2c":
        ep, osf, err = _ds3231_epoch(raw.get("regs", {}))
        raw["osf"], raw["rtc_epoch"] = osf, ep
        if err:
            if carrier == "debug":
                return Check("C4", "RTC DS3231", SKIP,
                             "отладочная несущая (нет I2C-устройств)", raw, method)
            return Check("C4", "RTC DS3231", FAIL,
                         f"DS3231 на i2c-1 @0x68: {err}", raw, method)
    if bad or raw.get("rtc_epoch") is None or raw.get("sys_epoch") is None:
        return Check("C4", "RTC DS3231", FAIL,
                     "ошибка чтения DS3231 или в dmesg «oscillator stop/discontinuity» "
                     "(батарея или генератор DS3231)", raw, method)
    skew = raw["rtc_epoch"] - raw["sys_epoch"]
    raw["skew_s"] = skew
    if raw.get("osf"):
        # OSF липкий: lib_rtc.sh пишет только 0x00–0x06 и флаг не сбрасывает,
        # поэтому он остаётся от первого включения. Сам по себе — история;
        # авария — только если время при этом неверное.
        if abs(skew) > DS3231_SKEW_WARN_S:
            return Check("C4", "RTC DS3231", FAIL,
                         f"DS3231: взведён OSF и время ушло на {skew:+d} с — генератор "
                         "останавливался (батарея села/отсутствует или неисправен кварц "
                         "DS3231)", raw, method)
        return Check("C4", "RTC DS3231", WARN,
                     f"время верное ({skew:+d} с), но взведён OSF — генератор когда-то "
                     "останавливался (первое включение/замена батареи); прошивка OSF не "
                     "сбрасывает", raw, method)
    if abs(skew) <= DS3231_SKEW_WARN_S:
        return Check("C4", "RTC DS3231", PASS, f"расхождение с системой {skew:+d} с", raw, method)
    return Check("C4", "RTC DS3231", WARN,
                 f"расхождение с системой {skew:+d} с (>±{DS3231_SKEW_WARN_S}); при "
                 "sa02m-rtc-sync.timer должно выравниваться каждые 30 мин", raw, method)


# =============================================================================
# C5. Расширитель PCA9536 (i2c-2 @0x41) — чтение под flock веб-панели
# =============================================================================
_PCA_CMD = r"""
. /etc/sa02m_hw.conf 2>/dev/null
echo "BACKEND=${SA02M_HW_BACKEND:-}"
L="${SA02M_I2C_LOCK_FILE:-/run/lock/sa02m-pca9536.lock}"
B="${SA02M_I2C_BUS:-2}"; A="${SA02M_I2C_EXP_ADDR:-0x41}"
echo "BUS=$B ADDR=$A LOCK=$L"
[ "${SA02M_HW_BACKEND:-}" = disabled ] && exit 0
timeout 6 flock -w 3 "$L" sh -c "echo IN=\$(timeout 2 i2cget -y $B $A 0x00 2>&1); echo CFG=\$(timeout 2 i2cget -y $B $A 0x03 2>&1)"
"""
PCA_BITS = {0: "Alarm LED", 1: "DO", 2: "Buzzer", 3: "Blue LED"}


def collect_c5(rec: _Recorder) -> dict:
    _, out = rec(_PCA_CMD, 10)
    raw: dict[str, Any] = {}
    for ln in out.splitlines():
        if ln.startswith(("BACKEND=", "IN=", "CFG=")):
            k, v = ln.split("=", 1)
            raw[k.lower()] = v.strip()
        elif ln.startswith("BUS="):
            raw["where"] = ln.strip()
    return raw


def _decode_pca(val: int, cfg: bool) -> dict:
    return {PCA_BITS[b]: ("вход" if cfg else "1") if val & (1 << b)
            else ("выход" if cfg else "0") for b in PCA_BITS}


def analyze_c5(raw: dict, carrier: str) -> Check:
    method = "flock(SA02M_I2C_LOCK_FILE) + i2cget -y 2 0x41 0x00/0x03 (только чтение)"
    if raw.get("backend") == "disabled":
        return Check("C5", "PCA9536", SKIP, "SA02M_HW_BACKEND=disabled", raw, method)
    try:
        vin, vcfg = int(raw.get("in", ""), 16), int(raw.get("cfg", ""), 16)
    except ValueError:
        if carrier == "debug":
            return Check("C5", "PCA9536", SKIP, "отладочная несущая (нет I2C-устройств)",
                         raw, method)
        return Check("C5", "PCA9536", FAIL,
                     f"PCA9536 не отвечает: in={raw.get('in')!r} cfg={raw.get('cfg')!r}",
                     raw, method)
    raw = dict(raw, input_bits=_decode_pca(vin, False), config_bits=_decode_pca(vcfg, True))
    return Check("C5", "PCA9536", PASS, f"вход=0x{vin:02x}, конфигурация=0x{vcfg:02x}",
                 raw, method)


# =============================================================================
# C6. Здоровье шин I2C (по dmesg)
# =============================================================================
def analyze_c6(dmesg: str, carrier: str, c4: Check, c5: Check) -> Check:
    method = "dmesg: «I2C bus locked», timeout, arbitration lost — по шинам"
    counts: dict[str, int] = {}
    for ln in dmesg.splitlines():
        if not re.search(r"I2C bus locked|arbitration lost|i2c.*timeout|timeout.*i2c", ln, re.I):
            continue
        m = re.search(r"(1c2[0-9a-f]{4})\.i2c", ln)
        bus = I2C_BUS_BY_ADDR.get(m.group(1), m.group(1)) if m else "?"
        counts[bus] = counts.get(bus, 0) + 1
    raw = {"errors_by_bus": counts}
    total = sum(counts.values())
    if total == 0:
        return Check("C6", "Шины I2C", PASS, "ошибок I2C в dmesg нет", raw, method)
    if carrier == "debug":
        return Check("C6", "Шины I2C", SKIP,
                     f"отладочная несущая — шторм ожидаем ({total} строк)", raw, method)
    v = FAIL if FAIL in (c4.verdict, c5.verdict) else WARN
    return Check("C6", "Шины I2C", v, f"ошибок I2C: {counts}", raw, method)


# =============================================================================
# C7. UART / RS-485 — только счётчики, порты не открываются
# =============================================================================
def collect_c7(rec: _Recorder) -> dict:
    _, out = rec("timeout 5 cat /proc/tty/driver/serial", 8)
    ports: dict[int, dict] = {}
    for ln in out.splitlines():
        m = re.match(r"\s*(\d+):\s*uart:(\S+)", ln)
        if not m:
            continue
        idx = int(m.group(1))
        p: dict[str, Any] = {"uart": m.group(2)}
        for key in ("tx", "rx", "fe", "pe", "brk", "oe"):
            mm = re.search(rf"\b{key}:(\d+)", ln)
            p[key] = int(mm.group(1)) if mm else 0
        ports[idx] = p
    _, links = rec("ls /dev/COM* /dev/RS-485-* 2>/dev/null | tr '\\n' ' '", 5)
    return {"ports": ports, "links": links.split()}


def analyze_c7(raw: dict, variant: str) -> Check:
    method = "/proc/tty/driver/serial (tx/rx/fe/pe/brk/oe); наличие /dev/COM*, /dev/RS-485-*"
    expected = VARIANT_TTYS.get(variant, VARIANT_TTYS["sa02m-1eth"])
    ports = raw.get("ports", {})
    missing = [f"ttyS{i}" for i in expected
               if ports.get(i, {}).get("uart", "unknown") == "unknown"]
    if missing:
        return Check("C7", "UART / RS-485", FAIL,
                     f"нет портов {', '.join(missing)} (вариант {variant})", raw, method)
    noisy = []
    for i in expected:
        p = ports[i]
        rx = p.get("rx", 0)
        if rx >= UART_MIN_RX_FOR_RATIO and (p["fe"] + p["pe"]) / rx > UART_ERR_RATIO_WARN:
            noisy.append(f"ttyS{i} fe+pe={p['fe'] + p['pe']}/rx={rx}")
    if noisy:
        return Check("C7", "UART / RS-485", WARN,
                     "ошибки кадра/чётности >1% (скорость, тактирование, наводка, "
                     "терминатор): " + "; ".join(noisy), raw, method)
    return Check("C7", "UART / RS-485", PASS,
                 f"порты {', '.join(f'ttyS{i}' for i in expected)} на месте", raw, method)


# =============================================================================
# C8. Ethernet (PHY и его кварц 25 МГц) — два снимка счётчиков
# =============================================================================
_NET_CMD = r"""for d in /sys/class/net/eth* /sys/class/net/end*; do
  [ -e "$d" ] || continue; i=$(basename "$d")
  printf '%s carrier=%s speed=%s duplex=%s' "$i" "$(cat $d/carrier 2>/dev/null)" "$(cat $d/speed 2>/dev/null)" "$(cat $d/duplex 2>/dev/null)"
  for c in rx_packets tx_packets rx_errors tx_errors rx_crc_errors rx_dropped; do printf ' %s=%s' $c "$(cat $d/statistics/$c 2>/dev/null)"; done
  echo
done"""


def collect_net(rec: _Recorder) -> dict:
    _, out = rec(_NET_CMD, 8)
    nets: dict[str, dict] = {}
    for ln in out.splitlines():
        parts = ln.split()
        if not parts:
            continue
        d: dict[str, Any] = {}
        for kv in parts[1:]:
            if "=" in kv:
                k, v = kv.split("=", 1)
                d[k] = int(v) if v.lstrip("-").isdigit() else v
        nets[parts[0]] = d
    return nets


def analyze_c8(n0: dict, n1: dict, variant: str, window_s: float) -> Check:
    method = (f"/sys/class/net/*/carrier,speed + statistics/*; два снимка с "
              f"интервалом {window_s:.0f} с")
    want = VARIANT_ETH_COUNT.get(variant, 1)
    # SoM A40i-2eth несёт два PHY всегда; вариант 1eth использует только
    # первый — второй без линка не неисправность (калибровка на 135/136).
    used = sorted(n1)[:want]
    raw = {"t0": n0, "t1": n1, "used": used,
           "unused_by_variant": [i for i in sorted(n1) if i not in used]}
    if len(n1) < want:
        return Check("C8", "Ethernet", FAIL,
                     f"интерфейсов {len(n1)} из {want} (вариант {variant})", raw, method)
    notes, grow = [], []
    for i in used:
        d = n1[i]
        if d.get("carrier") != 1:
            notes.append(f"{i}: нет линка (кабель/коммутатор/PHY)")
        elif isinstance(d.get("speed"), int) and 0 < d["speed"] < 100:
            notes.append(f"{i}: скорость {d['speed']} Мбит/с")
        a = n0.get(i, {})
        for c in ("rx_errors", "tx_errors", "rx_crc_errors"):
            if isinstance(d.get(c), int) and isinstance(a.get(c), int) and d[c] > a[c]:
                grow.append(f"{i}.{c} {a[c]}→{d[c]}")
    if grow:
        notes.append("ошибки растут: " + ", ".join(grow))
    if notes:
        return Check("C8", "Ethernet", WARN, "; ".join(notes), raw, method)
    links = ", ".join(f"{i} {n1[i].get('speed')}/{n1[i].get('duplex')}" for i in used)
    return Check("C8", "Ethernet", PASS, f"линк есть, ошибки не растут: {links}", raw, method)


# =============================================================================
# C9. USB (хост и OTG)
# =============================================================================
def collect_c9(rec: _Recorder) -> dict:
    _, out = rec("ls /sys/bus/usb/devices/ 2>/dev/null | tr '\\n' ' '; echo; "
                 "for f in /sys/class/udc/*/state; do [ -e \"$f\" ] && echo \"UDC=$f:$(cat $f)\"; done; "
                 "timeout 5 gpioinfo 0 2>/dev/null | grep -w 268 || true", 10)
    lines = out.splitlines()
    devs = lines[0].split() if lines else []
    return {"devices": devs,
            "root_hubs": [d for d in devs if re.fullmatch(r"usb\d+", d)],
            "udc": [ln[4:] for ln in lines if ln.startswith("UDC=")],
            "vbus_line_268": next((ln.strip() for ln in lines if "268" in ln), "")}


def analyze_c9(raw: dict, dmesg: str) -> Check:
    method = "/sys/bus/usb/devices, /sys/class/udc/*/state, gpioinfo 0 (линия 268), dmesg"
    errs = [ln for ln in dmesg.splitlines()
            if re.search(r"over-?current|device descriptor read.*error", ln, re.I)]
    raw = dict(raw, dmesg_errors=errs[-5:])
    if not raw.get("root_hubs"):
        return Check("C9", "USB", FAIL, "нет корневых хабов USB", raw, method)
    if errs:
        return Check("C9", "USB", WARN, f"в dmesg {len(errs)} строк over-current/descriptor",
                     raw, method)
    return Check("C9", "USB", PASS, f"корневых хабов {len(raw['root_hubs'])}, ошибок нет",
                 raw, method)


# =============================================================================
# C10. eMMC (износ и ошибки)
# =============================================================================
def collect_c10(rec: _Recorder) -> dict:
    _, out = rec("d=/sys/block/mmcblk2/device; echo LT=$(cat $d/life_time 2>/dev/null); "
                 "echo EOL=$(cat $d/pre_eol_info 2>/dev/null)", 6)
    raw: dict[str, Any] = {"source": "sysfs"}
    for ln in out.splitlines():
        if ln.startswith("LT="):
            raw["life_time"] = ln[3:].strip()
        elif ln.startswith("EOL="):
            raw["pre_eol_info"] = ln[4:].strip()
    if not raw.get("life_time") and not raw.get("pre_eol_info"):
        _, ext = rec("command -v mmc >/dev/null && timeout 10 mmc extcsd read /dev/mmcblk2 "
                     "| grep -E 'LIFE_TIME_EST|PRE_EOL' || echo NO_MMC_UTILS", 15)
        raw["source"] = "mmc extcsd"
        if "NO_MMC_UTILS" in ext:
            raw["source"] = None
        for ln in ext.splitlines():
            m = re.search(r"\[(EXT_CSD_[A-Z_]+)\]:\s*(0x[0-9a-fA-F]+)", ln)
            if m and "PRE_EOL" in m.group(1):
                raw["pre_eol_info"] = m.group(2)
            elif m:
                raw.setdefault("life_time", "")
                raw["life_time"] = (raw["life_time"] + " " + m.group(2)).strip()
    return raw


def _hexes(s: str | None) -> list[int]:
    return [int(x, 16) for x in re.findall(r"0x[0-9a-fA-F]+", s or "")]


def analyze_c10(raw: dict, dmesg: str) -> Check:
    method = "/sys/block/mmcblk2/device/{life_time,pre_eol_info} (или mmc extcsd), dmesg mmc"
    errs = [ln for ln in dmesg.splitlines()
            if re.search(r"mmc", ln, re.I) and re.search(r"error|timeout|crc", ln, re.I)]
    raw = dict(raw, dmesg_errors=errs[-5:])
    lt, eol = _hexes(raw.get("life_time")), _hexes(raw.get("pre_eol_info"))
    if not lt and not eol:
        return Check("C10", "eMMC", SKIP,
                     "атрибуты износа недоступны (нет sysfs life_time/pre_eol_info и mmc-utils)",
                     raw, method)
    lt_max, e = (max(lt) if lt else 0), (eol[0] if eol else 1)
    if e >= 3 or lt_max >= 0x0A or len(errs) >= 3:
        v = FAIL
    elif e == 2 or 6 <= lt_max <= 9 or errs:
        v = WARN
    else:
        v = PASS
    return Check("C10", "eMMC", v,
                 f"life_time={raw.get('life_time')}, pre_eol={raw.get('pre_eol_info')}, "
                 f"ошибок mmc в dmesg {len(errs)}", raw, method)


# =============================================================================
# C11. Питание (AXP221) и температура
# =============================================================================
_PWR_CMD = r"""for d in /sys/class/power_supply/*; do [ -e "$d" ] && echo "PS $(basename $d) type=$(cat $d/type 2>/dev/null) online=$(cat $d/online 2>/dev/null) uv=$(cat $d/voltage_now 2>/dev/null)"; done
for d in /sys/class/regulator/regulator.*; do [ -e "$d" ] && echo "REG $(cat $d/name 2>/dev/null | tr ' ' '_') state=$(cat $d/state 2>/dev/null) uv=$(cat $d/microvolts 2>/dev/null)"; done
for d in /sys/class/thermal/thermal_zone*; do [ -e "$d" ] && echo "TZ $(cat $d/type 2>/dev/null) temp=$(cat $d/temp 2>/dev/null)"; done"""


def collect_c11(rec: _Recorder) -> dict:
    _, out = rec(_PWR_CMD, 10)
    raw: dict[str, Any] = {"supplies": {}, "regulators": {}, "thermal": {}}
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) < 2:
            continue
        kv = dict(p.split("=", 1) for p in parts[2:] if "=" in p)
        if parts[0] == "PS":
            raw["supplies"][parts[1]] = kv
        elif parts[0] == "REG":
            raw["regulators"][parts[1]] = kv
        elif parts[0] == "TZ":
            try:
                raw["thermal"][parts[1]] = int(kv.get("temp", "")) / 1000.0
            except ValueError:
                raw["thermal"][parts[1]] = None
    return raw


# Слова-признаки ошибки PMIC — целыми словами: «governor» не должен ловиться
# как «over» (калибровка 2026-10-22).
PMIC_ERR_RE = (r"\b(errors?|fail(ed|ure)?|critical|undervolt\w*|brown-?out|"
               r"over-?(voltage|current|temp\w*))\b")
# Штатные строки загрузки эталонной 135 — не неисправность.
PMIC_BENIGN = (r"axp20x-gpio: Failed to locate of_node",)


def analyze_c11(raw: dict, dmesg: str, reference: dict | None = None) -> Check:
    method = ("/sys/class/power_supply, /sys/class/regulator, /sys/class/thermal; "
              "dmesg axp/undervolt/brownout/thermal")
    pmic = [ln for ln in dmesg.splitlines()
            if re.search(r"axp|undervolt|brownout|thermal", ln, re.I)
            and re.search(PMIC_ERR_RE, ln, re.I)
            and not any(re.search(b, ln) for b in PMIC_BENIGN)]
    temps = {k: v for k, v in raw.get("thermal", {}).items() if isinstance(v, float)}
    hottest = max(temps.values()) if temps else None
    dev = []
    for name, ref_uv in (reference or {}).items():
        cur = raw.get("regulators", {}).get(name, {}).get("uv", "")
        if str(cur).isdigit() and ref_uv and abs(int(cur) - ref_uv) / ref_uv > REGULATOR_DEVIATION_WARN:
            dev.append(f"{name}: {int(cur)/1e6:.3f} В (эталон {ref_uv/1e6:.3f})")
    raw = dict(raw, pmic_errors=pmic[-5:], regulator_deviation=dev)
    t = f"{hottest:.1f} °C" if hottest is not None else "—"
    if (hottest is not None and hottest > TEMP_FAIL_C) or pmic:
        return Check("C11", "Питание / температура", FAIL,
                     f"температура {t}, ошибок PMIC {len(pmic)}", raw, method)
    if (hottest is not None and hottest >= TEMP_WARN_C) or dev:
        return Check("C11", "Питание / температура", WARN,
                     f"температура {t}" + (f"; регуляторы: {'; '.join(dev)}" if dev else ""),
                     raw, method)
    return Check("C11", "Питание / температура", PASS,
                 f"температура {t}, ошибок PMIC нет", raw, method)


# =============================================================================
# C12. Сводка ошибок ядра (без вердикта)
# =============================================================================
_C12_MAP = (
    ("C1", r"rtc|losc|32k|watchdog|wdt"), ("C4", r"ds323|ds1307|rtc1"),
    ("C6", r"i2c|mv64xxx"), ("C7", r"tty|serial|uart"),
    ("C8", r"eth|end\d|dwmac|stmmac|phy|link"), ("C9", r"usb|musb|ehci|ohci|udc"),
    ("C10", r"mmc|ext4|blk_update"), ("C11", r"axp|thermal|regulator|voltage"),
)


def collect_c12(rec: _Recorder) -> dict:
    _, kern = rec("timeout 10 dmesg -l emerg,alert,crit,err", 15)
    _, jour = rec("timeout 10 journalctl -b -p err --no-pager -n 200 -q", 15)
    return {"dmesg_err": kern.splitlines()[-200:], "journal_err": jour.splitlines()[-200:]}


def analyze_c12(raw: dict) -> Check:
    buckets: dict[str, list[str]] = {}
    for ln in raw.get("dmesg_err", []) + raw.get("journal_err", []):
        node = next((cid for cid, pat in _C12_MAP if re.search(pat, ln, re.I)), "прочее")
        buckets.setdefault(node, []).append(ln.strip())
    summary = ", ".join(f"{k}: {len(v)}" for k, v in sorted(buckets.items())) or "ошибок нет"
    return Check("C12", "Сводка ядра", INFO, summary,
                 {k: v[-20:] for k, v in buckets.items()},
                 "dmesg -l emerg,alert,crit,err; journalctl -b -p err -n 200")


# =============================================================================
# Полный прогон
# =============================================================================
def reboot_safe(run: Runner) -> tuple[bool, str]:
    """Быстрая проверка перед программной перезагрузкой (только C1)."""
    chk = analyze_c1(collect_c1(_Recorder(run)))
    return chk.verdict == PASS, f"{chk.verdict}: {chk.summary}"


def default_window_s() -> float:
    try:
        return float(os.environ.get("SA02M_HWDIAG_WINDOW_S", "120"))
    except ValueError:
        return 120.0


def run_all(run: Runner, *, window_s: float | None = None, reference: dict | None = None,
            sleep_fn: Callable[[float], None] = time.sleep,
            clock: Callable[[], float] = time.monotonic,
            log: Callable[[str], None] = lambda s: None) -> dict:
    """Все проверки C1–C12. C2 (RTC) и C8 (рост ошибок Ethernet) замеряются в
    общем окне ``window_s``, пока идут остальные проверки.
    ``sleep_fn``/``clock`` — точки подмены для юнит-тестов."""
    window = default_window_s() if window_s is None else window_s
    rec = _Recorder(run)
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    board = collect_board(rec)
    log(f"плата: {board.get('h')} {board.get('variant')} ядро {board.get('kernel')}")

    t0 = clock()
    samples = [rtc_sample(rec)]
    net0 = collect_net(rec)

    _, dmesg = rec("timeout 10 dmesg", 15)
    carrier, carrier_why = detect_carrier(board, dmesg)
    board["carrier"], board["carrier_evidence"] = carrier, carrier_why
    log(f"несущая: {carrier} ({carrier_why})")

    c1 = analyze_c1(collect_c1(rec))
    log(f"C1 {c1.verdict}: {c1.summary}")
    c3 = analyze_c3(collect_c3(rec))
    c4 = analyze_c4(collect_c4(rec, carrier), dmesg, carrier)
    c5 = analyze_c5(collect_c5(rec), carrier)
    c6 = analyze_c6(dmesg, carrier, c4, c5)
    c7 = analyze_c7(collect_c7(rec), board["variant"])
    c9 = analyze_c9(collect_c9(rec), dmesg)
    c10 = analyze_c10(collect_c10(rec), dmesg)
    c11 = analyze_c11(collect_c11(rec), dmesg, reference)
    c12 = analyze_c12(collect_c12(rec))

    # Добрать окно C2/C8: сэмплы rtc0 раз в ~10 с (для исключения скачков).
    while clock() - t0 < window:
        sleep_fn(min(10.0, max(0.0, window - (clock() - t0))))
        samples.append(rtc_sample(rec))
    if len(samples) < 2:
        samples.append(rtc_sample(rec))
    net1 = collect_net(rec)
    c2 = analyze_c2(samples)
    c8 = analyze_c8(net0, net1, board["variant"], clock() - t0)

    checks = [c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11, c12]
    return {
        "board": board,
        "started_at": started,
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "window_s": window,
        "checks": [asdict(c) for c in checks],
        "reboot_safe": c1.verdict == PASS,
        "command_log": rec.log,
    }


def check_of(report: dict, cid: str) -> dict:
    return next(c for c in report["checks"] if c["id"] == cid)


# =============================================================================
# Отчёт (§5)
# =============================================================================
def render_markdown(report: dict) -> str:
    b = report["board"]
    c1 = check_of(report, "C1")
    if report["reboot_safe"]:
        head = "**Перезагружать программно можно** — кварц 32.768 кГц в работе (C1 PASS)."
    elif c1["verdict"] == FAIL:
        head = f"**ПЕРЕЗАГРУЖАТЬ ПРОГРАММНО НЕЛЬЗЯ. {REBOOT_HANG_TEXT}**"
    else:
        head = f"**Перезагружать программно нельзя** — C1 {c1['verdict']}: {c1['summary']}"
    lines = [head, "",
             f"Плата: `{b.get('h')}` ({b.get('ip', '').strip()}), вариант `{b.get('variant')}`"
             f"{' (предположен)' if b.get('variant_assumed') else ''}, "
             f"несущая **{'отладочная' if b.get('carrier') == 'debug' else 'штатная'}**, "
             f"ядро `{b.get('kernel')}`, веб `{b.get('webver')}`, аптайм "
             f"{b.get('uptime_s', 0) / 60:.0f} мин. Замер: {report['started_at']}.", "",
             "| Проверка | Вердикт | Пояснение |", "|---|---|---|"]
    for c in report["checks"]:
        lines.append(f"| {c['id']} {c['title']} | **{c['verdict']}** | {c['summary']} |")
    bad = [c for c in report["checks"] if c["verdict"] in (FAIL, WARN)]
    if bad:
        lines += ["", "## Подробности FAIL / WARN"]
        for c in bad:
            lines += ["", f"### {c['id']} {c['title']} — {c['verdict']}", "",
                      c["summary"], "", f"Метод: {c['method']}", "", "```json",
                      json.dumps(c["raw"], ensure_ascii=False, indent=1, default=str)[:4000],
                      "```"]
    return "\n".join(lines) + "\n"


def save_report(report: dict, out_dir: str | Path) -> tuple[Path, Path]:
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    j, m = d / "report.json", d / "report.md"
    j.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                 encoding="utf-8")
    m.write_text(render_markdown(report), encoding="utf-8")
    return j, m


# Запрещённое ТЗ §2 — для самоконтроля журнала команд.
FORBIDDEN_PATTERNS = (
    r"\breboot\b(?!_)", r"\bpoweroff\b", r"systemctl\s+(reboot|kexec|poweroff|stop|restart)",
    r"sysrq-trigger", r"\bdevmem\s+\S+\s+\S+\s+\S+", r"\bi2cset\b", r"\bi2ctransfer\b",
    r"\bgpioset\b", r"journalctl\s+[^|]*-f\b", r"dmesg\s+[^|]*-w\b", r"tail\s+-f",
    r"ping\s+-f", r"\biperf", r"/dev/ttyS\d", r"\bstty\b", r"\bminicom\b",
    r"os\.O_RDWR|PROT_WRITE",
)


def forbidden_commands(command_log: list[str]) -> list[str]:
    """Команды журнала, совпавшие с запретами ТЗ §2 (должно быть пусто)."""
    bad = []
    for cmd in command_log:
        for pat in FORBIDDEN_PATTERNS:
            if re.search(pat, cmd):
                bad.append(f"{pat} :: {cmd[:120]}")
    return bad
