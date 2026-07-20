#!/usr/bin/env python3
"""
SA-02m Cloud Agent — frpc reverse-tunnel edition.

Pairs the device with the cloud (cloud.cyntron.ru), maintains the frpc
reverse tunnel, and POSTs send-only telemetry heartbeats.

Contract: cloud repo docs/contracts/cloud-enrollment.md (frozen).
SECURITY: there is deliberately NO command channel — the cloud can never
make this device execute anything. Heartbeat responses are ignored except
for the "ok" field. (The former handle_command() root channel — threat
model F1 — was removed in Phase B together with WireGuard.)

Activation modes (no SSH needed):
  1. Claim code (primary): web UI Cloud tab → "connect" → the agent requests
     a pairing code, shows it via /run/sa02m-cloud-status.json, and polls
     until the user attaches the code in the cloud UI.
  2. Enroll token (fallback for installers): write the token to
     /etc/sa02m-cloud/activation_token (web UI POST or sa02m-cloud-activate).
"""
import argparse
import configparser
import json
import logging
import os
import platform
import subprocess
import sys
import time
import urllib.request
import urllib.error

_handlers = [logging.StreamHandler(sys.stdout)]
try:
    _handlers.append(logging.FileHandler("/var/log/sa02m-cloud-agent.log", mode="a"))
except OSError:
    pass  # read-only rootfs / test host — stdout (journal) still has it
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("sa02m-cloud")

DEFAULT_CONFIG        = "/etc/sa02m-cloud/agent.conf"
ACTIVATION_TOKEN_FILE = "/etc/sa02m-cloud/activation_token"   # enroll-token fallback
PAIR_REQUEST_FILE     = "/etc/sa02m-cloud/pair_request"       # claim-code trigger
FRPC_CONFIG           = "/etc/sa02m-cloud/frpc.toml"
DEVICE_SECRET_FILE    = "/etc/sa02m-cloud/device_secret"      # per-device identity
FRPC_BINARY           = "/usr/local/bin/frpc"
FRPC_UNIT             = "sa02m-cloud-frpc"
STATUS_FILE           = "/run/sa02m-cloud-status.json"        # for web UI CGI
ROSTER_FILE           = "/run/sa02m-rs485-roster.json"        # bus-free module cache
HW_VARIANT            = "sa02m-1eth"
VERSION_FILE          = "/var/www/network_config/VERSION"

STANDBY_POLL_S  = 5
WATCHDOG_S      = 60

# Порты, которые устройство СОГЛАСНО туннелировать наружу (defense-in-depth,
# threat-model актор A5 — зловредное/скомпрометированное облако). Облако диктует
# local_port в claim/enroll-ответе, но устройство пиннит СВОЙ набор ролей —
# web (:80) и cfg (:9999) — и отбрасывает любой другой прокси (напр. :22 SSH,
# :1883 MQTT). Дополняет облачную frps NewProxy-authz, которая защищает ФЛОТ от
# зловредного устройства, но не УСТРОЙСТВО от зловредного облака. Один дом,
# greppable. Контракт: docs/contracts/cloud-enrollment.md.
ALLOWED_LOCAL_PORTS = frozenset({80, 9999})


def _write_status(state: str, **kw):
    """Write machine-readable status for CGI/web UI."""
    payload = {"state": state, "ts": int(time.time()), **kw}
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump(payload, f)
    except Exception:
        pass


# ── Config ────────────────────────────────────────────────────────────────────
def load_config(path: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "cloud": {
            "api_url":            "https://cloud.cyntron.ru/api/v1",
            "server_host":        "cloud.cyntron.ru",
            "enrolled":           "false",
            "device_id":          "",
            "heartbeat_interval": "30",
        },
        "device": {
            "serial":   "",
            "web_port": "9999",
        },
    })
    if os.path.exists(path):
        cfg.read(path)
    # WireGuard-era leftovers must not survive a migrated config file
    cfg.remove_section("wireguard")
    cfg.remove_option("cloud", "device_token")
    cfg.remove_option("cloud", "metrics_interval")
    return cfg


def save_config(path: str, cfg: configparser.ConfigParser):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        cfg.write(f)
    os.chmod(path, 0o640)


# ── Per-device identity (Phase C) ─────────────────────────────────────────────
# Облако выдаёт секрет ТОЛЬКО в момент зачисления (claim/enroll), где устройство
# доказало право на него; лениво по одному device_id он не выдаётся никогда.
# Отдельный файл, а не agent.conf: agent.conf лежит 0640 (его читает веб-UI), а
# секрет должен быть 0600 — как frpc.toml, который тоже несёт учётные данные.
def save_device_secret(secret: str, path: str = DEVICE_SECRET_FILE):
    # os.open с режимом сразу: write-then-chmod оставлял окно, в котором
    # долгоживущий секрет лежал с правами по umask (обычно 0644).
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    # os.fdopen забирает владение fd: закрывать его самим в except нельзя —
    # `with` уже закрыл его при раскрутке, и повторный close бросал EBADF ПОВЕРХ
    # настоящей ошибки. Вызывающий логирует то, что реально случилось, а эта
    # строка лога — единственный сигнал оператору, что устройство вот-вот уйдёт
    # в Offline при живом туннеле (режим strict).
    with os.fdopen(fd, "w") as f:
        f.write(secret)
    os.chmod(path, 0o600)   # существующий файл мог быть создан ранее с другими правами


def load_device_secret(path: str = DEVICE_SECRET_FILE) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""


# ── Identity ──────────────────────────────────────────────────────────────────
def get_serial() -> str:
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if "Serial" in line:
                    return line.split(":")[1].strip()
    except Exception:
        pass
    try:
        with open("/etc/machine-id") as f:
            return f.read().strip()[:16]
    except Exception:
        pass
    return platform.node()


def get_device_id(cfg: configparser.ConfigParser) -> str:
    """Stable device id, bench convention: sa02m-<serial> (contract charset
    ^[A-Za-z0-9._-]{1,64}$; the serial sources satisfy it)."""
    did = cfg["cloud"].get("device_id", "")
    if did:
        return did
    return "sa02m-" + get_serial().lower()


def get_fw_version() -> str:
    try:
        with open(VERSION_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except Exception:
        pass
    return "unknown"


# ── HTTP (stdlib only) ────────────────────────────────────────────────────────
def api_post(url: str, payload: dict, timeout: int = 15):
    """POST JSON; returns (http_status, parsed_body|None). Network failure
    returns (0, None)."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json",
                 "User-Agent": "sa02m-cloud-agent/2.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read()[:300]
        log.warning("POST %s -> HTTP %d: %s", url, e.code, body)
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, None
    except Exception as e:
        log.debug("POST %s error: %s", url, e)
        return 0, None


# ── frpc profile → config ─────────────────────────────────────────────────────
def render_frpc_toml(frpc: dict) -> str:
    """Render frpc.toml from the contract's frpc profile. Emits the proxies the
    cloud handed out (normally two: <sub> → :80 web SCADA, <sub>-cfg → :9999
    settings), type http — the only shape the frps NewProxy authz accepts.

    Устройство-сторонний allow-list (ALLOWED_LOCAL_PORTS): любой прокси с
    local_port вне набора {80, 9999} ОТБРАСЫВАЕТСЯ с log.warning — защита от
    зловредного облака (A5), которое иначе продиктовало бы напр. :22 (SSH) или
    :1883 (MQTT). Легаси одиночный fallback проходит ту же проверку. Если
    отброшены ВСЕ прокси — рендерим конфиг без [[proxies]] и логируем error
    (fail closed: лучше без туннеля, чем зловредный туннель).

    transport.tls.enable пиннится явно (O3): frpc 0.61 включает TLS
    control-соединения по умолчанию, пин fail-safe если будущий frpc сменит
    дефолт.

    metadatas.device_id + metadatas.device_secret (Phase C) эмитятся ТОЛЬКО когда
    облако выдало секрет; без него рендер байт-в-байт прежний (legacy-путь окна
    grace)."""
    server_addr = frpc["server_addr"]
    server_port = int(frpc["server_port"])
    token       = frpc["token"]
    device_id     = frpc.get("device_id") or ""
    device_secret = frpc.get("device_secret") or ""
    proxies     = frpc.get("proxies") or []
    if not proxies:
        # Legacy single-proxy fallback fields (pre-Phase-B contract)
        proxies = [{
            "name":       frpc["proxy_name"],
            "subdomain":  frpc["subdomain"],
            "local_port": frpc["local_port"],
        }]
    lines = [
        'serverAddr = "%s"' % server_addr,
        "serverPort = %d" % server_port,
        'auth.token = "%s"' % token,
        # Пиним TLS транспортного (control) соединения frpc→frps явно.
        "transport.tls.enable = true",
    ]
    # transport.poolCount — сколько work-соединений frpc держит открытыми в пуле,
    # чтобы проксируемый запрос не платил свежий handshake через ~555ms облачный
    # линк (замер стенда: медиана 1.38s → 0.431s при poolCount=4). Значение
    # приходит ИЗ ОБЛАЧНОГО профиля, поэтому валидируется как враждебный вход —
    # тем же приёмом, что ALLOWED_LOCAL_PORTS для local_port (A5): принимаем
    # ТОЛЬКО целое в 0..16 (облако клампит у себя — перепроверяем здесь), на
    # чём угодно ином (не-int, bool, вне диапазона) log.warning и НЕ эмитим
    # строку (fallback на собственный дефолт frpc, не на аварию и не на
    # диктуемое облаком число удерживаемых сокетов). Ключ отсутствует → строки
    # нет → legacy-рендер байт-в-байт (test_legacy_render_is_byte_identical).
    pool_count = frpc.get("pool_count")
    if pool_count is not None:
        if (isinstance(pool_count, int) and not isinstance(pool_count, bool)
                and 0 <= pool_count <= 16):
            lines.append("transport.poolCount = %d" % pool_count)
        else:
            log.warning("ignoring cloud pool_count %r: not an integer in 0..16 "
                        "(A5 defense-in-depth) — falling back to frpc default",
                        pool_count)
    # Per-device identity (Phase C). frps передаёт metadatas в Login-хук облака,
    # тот проверяет пару id+secret и ОТКЛОНЯЕТ соединение целиком при несовпадении;
    # дальше NewProxy-хук берёт личность из СЕРВЕРНОЙ сессии логина, а не из
    # клиентского сообщения — поэтому привязка «личность → субдомён» надёжна.
    # Легаси-профиль (облако не выдало секрет) рендерится БЕЗ metadatas, байт-в-байт
    # как раньше — это и есть окно grace на стороне облака.
    if device_secret:
        lines += [
            'metadatas.device_id = "%s"' % device_id,
            'metadatas.device_secret = "%s"' % device_secret,
        ]
    lines.append("")
    emitted = 0
    for p in proxies:
        local_port = int(p["local_port"])
        if local_port not in ALLOWED_LOCAL_PORTS:
            log.warning("dropping cloud proxy %s: local_port %d not in device "
                        "allow-list %s (A5 defense-in-depth)",
                        p.get("name", "?"), local_port,
                        sorted(ALLOWED_LOCAL_PORTS))
            continue
        lines += [
            "[[proxies]]",
            'name = "%s"' % p["name"],
            'type = "http"',
            'subdomain = "%s"' % p["subdomain"],
            "localIP = \"127.0.0.1\"",
            "localPort = %d" % local_port,
            "",
        ]
        emitted += 1
    if emitted == 0:
        log.error("all cloud proxies dropped by the local-port allow-list %s — "
                  "rendering a tunnel config with zero proxies (fail closed: no "
                  "tunnel rather than a malicious one)",
                  sorted(ALLOWED_LOCAL_PORTS))
    return "\n".join(lines)


def write_frpc_config(frpc: dict, path: str = FRPC_CONFIG):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = render_frpc_toml(frpc)
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, 0o600)  # carries the frp token
    log.info("frpc config written: %s (%d proxies)", path, content.count("[[proxies]]"))


def _systemctl(*args, timeout: int = 15) -> int:
    try:
        r = subprocess.run(["systemctl", *args],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode
    except Exception as e:
        log.warning("systemctl %s failed: %s", " ".join(args), e)
        return 1


def ensure_frpc_running() -> str:
    """frpc watchdog (replaces the WireGuard one): make sure the tunnel unit
    runs whenever a config exists. Returns a state string for the status file."""
    if not os.path.exists(FRPC_CONFIG):
        return "no_config"
    if not os.path.exists(FRPC_BINARY):
        return "frpc_missing"
    if _systemctl("is-active", "--quiet", FRPC_UNIT) == 0:
        return "running"
    log.info("frpc unit not active — (re)starting %s", FRPC_UNIT)
    _systemctl("enable", FRPC_UNIT)
    _systemctl("restart", FRPC_UNIT)
    if _systemctl("is-active", "--quiet", FRPC_UNIT) == 0:
        return "running"
    return "failed"


# Маркеры серверного отказа в логине в журнале frpc. Облако отклоняет Login с
# конкретной причиной (см. cloud docs/contracts/cloud-enrollment.md §4), frpc
# печатает её в свой журнал. Это ЕДИНСТВЕННЫЙ доступный агенту сигнал: ответ
# heartbeat агент принципиально не читает (send-only, threat-model F1).
FRPC_REJECT_MARKERS = ("device revoked", "device identity required",
                       "credential mismatch", "device not enrolled",
                       "no credential issued", "invalid credential")
FRPC_FAIL_CYCLES = 3          # подряд неудачных циклов watchdog до диагностики
REVOKED_RETRY_S  = 600        # как часто пере-проверять отказ, стоя в standby
RECOVERY_SETTLE_S = 30        # дать frpc реально попытаться залогиниться перед вердиктом
JOURNAL_FALLBACK_SINCE = "-10 min"   # если точное время старта юнита недоступно


def _unit_active_since(unit: str) -> str:
    """Момент последнего запуска юнита в формате, понятном journalctl --since.

    Возвращает "" если значение недоступно или неправдоподобно — вызывающий
    обязан подставить своё ограничение окна, но НИКОГДА не читать журнал целиком."""
    try:
        r = subprocess.run(["systemctl", "show", "-p", "ActiveEnterTimestamp",
                            "--value", unit],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return ""
    if r.returncode != 0:
        return ""
    v = (r.stdout or "").strip()
    # systemctl отдаёт "n/a" для незапущенного юнита. Грубая проверка на дату:
    # нужны цифра, дефис и двоеточие — иначе это не timestamp.
    if (not v or v == "n/a" or not any(ch.isdigit() for ch in v)
            or "-" not in v or ":" not in v):
        return ""
    return v


def frpc_reject_reason(unit: str = FRPC_UNIT) -> str:
    """Причина серверного отказа из журнала frpc, или "" если её там нет.

    Окно чтения ОГРАНИЧЕНО текущим запуском frpc (--since). Без этого маркер от
    прошлого отказа переживал перепривязку: после честного revoke → re-pair три
    неудачных цикла watchdog по любой причине (сеть, рестарт frps) вычитывали
    старое "device revoked" и глушили исправное устройство.

    Честность важнее удобства: «отозвано» показываем ТОЛЬКО когда сервер прямо
    это сказал в ЭТОМ запуске. Недоступное облако, севшая сеть или упавший
    frpc — это не отзыв, и выдавать их за отзыв нельзя."""
    since = _unit_active_since(unit) or JOURNAL_FALLBACK_SINCE
    try:
        r = subprocess.run(["journalctl", "-u", unit, "--since", since,
                            "-n", "80", "--no-pager"],
                           capture_output=True, text=True, timeout=10)
    except Exception as e:
        log.debug("journalctl unavailable: %s", e)
        return ""
    if r.returncode != 0:
        return ""
    text = (r.stdout or "").lower()
    for marker in FRPC_REJECT_MARKERS:
        if marker in text:
            return marker
    return ""


def revoked_standby(cfg, device_id: str, reason: str) -> str:
    """Отказ сервера подтверждён: гасим туннель и встаём в standby.

    НЕ выходим из процесса. `Restart=on-failure` в юните не перезапускает чистый
    exit(0), поэтому прежний `return` из main() означал: устройство заглушено
    навсегда, пока человек не приедет и не зайдёт по SSH. Вместо этого:
      - раз в REVOKED_RETRY_S пере-проверяем, не сняли ли отзыв в облаке;
      - в любой момент реагируем на запрос привязки из веб-UI устройства.
    Оба пути восстанавливают связь без выезда на объект."""
    log.error("cloud refused this device (%s) — tunnel stopped, standing by", reason)
    _systemctl("stop", FRPC_UNIT)
    _systemctl("disable", FRPC_UNIT)
    while True:
        _write_status("revoked", device_id=device_id, reason=reason,
                      serial=cfg["device"]["serial"])
        waited = 0
        while waited < REVOKED_RETRY_S:
            # Пользователь нажал «подключить» в веб-UI — сразу в привязку.
            if os.path.exists(PAIR_REQUEST_FILE):
                log.info("pairing requested from the web UI — leaving stand-down")
                return "repair"
            time.sleep(STANDBY_POLL_S)
            waited += STANDBY_POLL_S
        log.info("re-testing the tunnel after stand-down (%s)", reason)
        if ensure_frpc_running() != "running":
            _systemctl("stop", FRPC_UNIT)
            continue
        # frpc — Type=simple: is-active отвечает 0 сразу после спавна, ДО первой
        # попытки логина, и журнал в этот момент пуст. Объявлять восстановление
        # здесь значило бы мигать active/revoked каждые REVOKED_RETRY_S на
        # устройстве, которое облако по-прежнему отвергает. Даём frpc время
        # реально сходить на сервер и перечитываем — юнит должен ВЫЖИТЬ и журнал
        # остаться чистым.
        time.sleep(RECOVERY_SETTLE_S)
        if _systemctl("is-active", "--quiet", FRPC_UNIT) == 0 and not frpc_reject_reason():
            log.info("cloud accepts this device again — resuming normal operation")
            return "recovered"
        log.info("still refused after settle — staying in stand-down")
        _systemctl("stop", FRPC_UNIT)


# ── Telemetry (heartbeat filler) ──────────────────────────────────────────────
def _read_first(path: str, default: str = "0") -> str:
    try:
        with open(path) as f:
            return f.read().strip().split()[0]
    except Exception:
        return default


def _fs_stats(path: str):
    v = os.statvfs(path)
    tot = v.f_blocks * v.f_frsize
    free = v.f_bavail * v.f_frsize
    if not tot:
        return None
    return {"total_gb": round(tot / 1e9, 1),
            "used_pct": round(100.0 * (1 - free / tot), 1)}


def collect_storage() -> dict:
    """eMMC (root fs) + removable drives, per the contract: emmc = the mmcblk
    with boot0/boot1 siblings (reported as the root fs), other mmcblk = sd,
    a /sys device path containing /usb = usb. Unmounted → dev only."""
    st = {}
    try:
        s = _fs_stats("/")
        if s:
            st["emmc"] = s
    except Exception:
        pass
    mounts = {}
    try:
        with open("/proc/mounts") as f:
            for line in f:
                p = line.split()
                if p[0].startswith("/dev/"):
                    mounts[p[0][5:]] = p[1]
    except Exception:
        pass
    usb, sd = [], []
    try:
        blocks = os.listdir("/sys/block")
    except Exception:
        blocks = []
    for b in blocks:
        if b.startswith(("loop", "ram", "zram")) or "boot" in b:
            continue
        try:
            link = os.readlink("/sys/block/" + b)
        except OSError:
            link = ""
        is_usb = "/usb" in link
        is_mmc = b.startswith("mmcblk")
        if not is_usb and not is_mmc:
            continue
        if is_mmc and os.path.exists("/sys/block/%sboot0" % b):
            continue  # eMMC — reported as the root fs above
        entry = {"dev": b}
        for d, mp in mounts.items():
            if d == b or (d.startswith(b) and d[len(b):].lstrip("p").isdigit()):
                entry["dev"] = d
                try:
                    s = _fs_stats(mp)
                    if s:
                        entry.update(s)
                except Exception:
                    pass
                break
        (usb if is_usb else sd).append(entry)
    if usb:
        st["usb"] = usb
    if sd:
        st["sd"] = sd
    return st


def read_roster_modules(path: str = ROSTER_FILE):
    """The modules block, passed VERBATIM from the bus-free roster cache —
    the agent NEVER opens the RS-485 port. Absent/invalid cache → None."""
    try:
        with open(path) as f:
            roster = json.load(f)
        ports = roster.get("ports") or {}
        if ports:
            return {"ports": ports}
    except Exception:
        pass
    return None


def collect_telemetry(prev_cpu=None) -> tuple:
    """Contract telemetry object. CPU% is computed between successive calls
    (heartbeat cadence) via /proc/stat deltas — no sleep on the loop.
    Returns (telemetry, cpu_snapshot)."""
    t = {}
    t["uptime_s"] = int(float(_read_first("/proc/uptime")))
    try:
        with open("/proc/stat") as f:
            fields = list(map(int, f.readline().split()[1:]))
        idle, total = fields[3] + fields[4], sum(fields)
        if prev_cpu:
            didle, dtotal = idle - prev_cpu[0], total - prev_cpu[1]
            t["cpu"] = round(100.0 * (1 - didle / max(dtotal, 1)), 1)
        cpu_snap = (idle, total)
    except Exception:
        cpu_snap = prev_cpu
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                k, v = line.split(":", 1)
                mem[k.strip()] = int(v.strip().split()[0])
        t["ram_pct"] = round(
            100.0 * (mem["MemTotal"] - mem["MemAvailable"]) / mem["MemTotal"], 1)
    except Exception:
        pass
    try:
        t["temp_c"] = round(
            int(_read_first("/sys/class/thermal/thermal_zone0/temp")) / 1000.0, 1)
    except Exception:
        pass
    try:
        t["storage"] = collect_storage()
    except Exception:
        pass
    t["services_ok"] = _systemctl("is-active", "--quiet", "nginx") == 0
    modules = read_roster_modules()
    if modules:
        t["modules"] = modules
    return t, cpu_snap


# ── Enrollment finalisation (shared by claim + token flows) ───────────────────
def finalize_enrollment(resp: dict, cfg: configparser.ConfigParser,
                        config_path: str, device_id: str) -> bool:
    """Write frpc config + agent config from a claim/enroll response."""
    frpc = resp.get("frpc") or {}
    if not frpc.get("server_addr") or not frpc.get("token"):
        log.error("enrollment response missing frpc profile: %s",
                  {k: v for k, v in resp.items() if k != "frpc"})
        return False
    try:
        write_frpc_config(frpc)
    except Exception as e:
        log.error("cannot write frpc config: %s", e)
        return False
    # Секрет получен вместе с профилем — сохраняем ДО записи конфига агента,
    # чтобы heartbeat мог им аутентифицироваться сразу после зачисления.
    secret = frpc.get("device_secret") or ""
    if secret:
        try:
            save_device_secret(secret)
            log.info("per-device identity stored for %s", device_id)
        except Exception as e:
            # Не фатально, но последствие зависит от режима облака: в `grace`
            # heartbeat пойдёт по legacy-пути и устройство останется на связи; в
            # `strict` heartbeat получит 403, и устройство будет числиться
            # Offline во флоте ПРИ ЖИВОМ туннеле. Ошибку видно в журнале.
            log.error("cannot store device secret: %s", e)
    cfg["cloud"]["enrolled"]  = "true"
    cfg["cloud"]["device_id"] = device_id
    hb = resp.get("heartbeat_interval_s")
    if hb:
        cfg["cloud"]["heartbeat_interval"] = str(int(hb))
    cfg["device"]["serial"] = get_serial()
    save_config(config_path, cfg)
    for p in (ACTIVATION_TOKEN_FILE, PAIR_REQUEST_FILE):
        try:
            os.remove(p)
        except OSError:
            pass
    tunnel = ensure_frpc_running()
    log.info("Enrolled as %s; tunnel: %s", device_id, tunnel)
    _write_status("active", device_id=device_id, tunnel=tunnel,
                  serial=cfg["device"]["serial"])
    return True


# ── Claim-code flow (primary) ─────────────────────────────────────────────────
def run_claim_flow(cfg: configparser.ConfigParser, config_path: str) -> bool:
    """POST /claim → show the code via the status file → poll /claim/status
    until claimed/expired. Returns True once enrolled."""
    api_url   = cfg["cloud"]["api_url"].rstrip("/")
    device_id = get_device_id(cfg)
    serial    = get_serial()

    status, resp = api_post(f"{api_url}/claim", {
        "device_id":  device_id,
        "hw_variant": HW_VARIANT,
        "fw_version": get_fw_version(),
    })
    if status == 409:
        log.warning("device already claimed in the cloud — detach it first")
        _write_status("already_claimed", device_id=device_id, serial=serial)
        return False
    if status != 200 or not resp or not resp.get("claim_code"):
        log.warning("claim request failed (HTTP %s)", status)
        _write_status("claim_failed", device_id=device_id, serial=serial)
        return False

    code     = resp["claim_code"]
    ttl      = int(resp.get("expires_in_s", 900))
    poll_s   = max(int(resp.get("poll_interval_s", 5)), 2)
    deadline = time.time() + ttl
    log.info("Pairing code %s (valid %ds) — enter it at %s",
             code, ttl, cfg["cloud"]["server_host"])

    while time.time() < deadline:
        _write_status("pairing", claim_code=code, device_id=device_id,
                      serial=serial, expires_at=int(deadline))
        status, st = api_post(f"{api_url}/claim/status",
                              {"device_id": device_id, "claim_code": code})
        if status == 200 and st:
            state = st.get("state", "")
            if state == "claimed":
                return finalize_enrollment(st, cfg, config_path, device_id)
            if state == "expired":
                break
        # user may cancel pairing from the web UI (trigger file removed)
        if not os.path.exists(PAIR_REQUEST_FILE):
            log.info("pairing cancelled from the web UI")
            _write_status("standby", serial=serial)
            return False
        time.sleep(poll_s)

    log.info("pairing code expired")
    _write_status("pair_expired", device_id=device_id, serial=serial)
    try:
        os.remove(PAIR_REQUEST_FILE)
    except OSError:
        pass
    return False


# ── Enroll-token flow (installer fallback) ────────────────────────────────────
def run_token_flow(token: str, cfg: configparser.ConfigParser,
                   config_path: str) -> bool:
    api_url   = cfg["cloud"]["api_url"].rstrip("/")
    device_id = get_device_id(cfg)
    _write_status("enrolling", device_id=device_id, serial=get_serial())
    status, resp = api_post(f"{api_url}/enroll", {
        "enroll_token": token,
        "device_id":    device_id,
        "hw_variant":   HW_VARIANT,
        "fw_version":   get_fw_version(),
    })
    if status != 200 or not resp or not resp.get("ok"):
        log.warning("enroll failed (HTTP %s): %s", status, resp)
        _write_status("enroll_failed", device_id=device_id)
        return False
    return finalize_enrollment(resp, cfg, config_path, device_id)


# ── Standby loop (wait for an activation trigger) ─────────────────────────────
def bootstrap_loop(cfg: configparser.ConfigParser, config_path: str) -> bool:
    log.info("Standby: waiting for pairing (web UI Cloud tab) or an enroll "
             "token at %s", ACTIVATION_TOKEN_FILE)
    _write_status("standby", serial=get_serial())
    while True:
        if os.path.exists(PAIR_REQUEST_FILE):
            if run_claim_flow(cfg, config_path):
                return True
            # Pairing failed with the trigger still present (already_claimed /
            # cloud unreachable): keep retrying so a detach in the cloud
            # auto-resumes pairing without another button press — but slowly,
            # the claim endpoint is rate-limited per IP (10 / 5 min).
            if os.path.exists(PAIR_REQUEST_FILE):
                time.sleep(60)
            continue
        if os.path.exists(ACTIVATION_TOKEN_FILE):
            try:
                with open(ACTIVATION_TOKEN_FILE) as f:
                    token = f.read().strip()
            except Exception as e:
                log.error("cannot read activation token: %s", e)
                token = ""
            if token:
                if run_token_flow(token, cfg, config_path):
                    return True
                log.warning("enroll failed, retrying in 60s")
                time.sleep(60)
                continue
        time.sleep(STANDBY_POLL_S)


# ── Active loop — frpc watchdog + send-only heartbeat ─────────────────────────
def active_loop(cfg: configparser.ConfigParser) -> str:
    """Основной цикл. Возвращает "repair" (нужна повторная привязка) или
    "recovered" (отказ снят) — оба через revoked_standby; иначе не возвращается."""
    api_url    = cfg["cloud"]["api_url"].rstrip("/")
    device_id  = get_device_id(cfg)
    h_interval = int(cfg["cloud"]["heartbeat_interval"])

    log.info("SA-02m Cloud Agent active (device %s, heartbeat %ds). "
             "Send-only: no command channel.", device_id, h_interval)

    last_heartbeat = 0.0
    last_watchdog  = 0.0
    tunnel         = ensure_frpc_running()
    cpu_snap       = None
    fail_cycles    = 0
    device_secret  = load_device_secret()
    log.info("per-device identity: %s",
             "present" if device_secret else "absent (legacy grace path)")

    while True:
        now = time.time()

        if now - last_watchdog > WATCHDOG_S:
            tunnel = ensure_frpc_running()
            last_watchdog = now
            if tunnel == "running":
                fail_cycles = 0
            else:
                fail_cycles += 1
                # Туннель не поднимается подряд — выясняем, отказ ли это сервера.
                if fail_cycles >= FRPC_FAIL_CYCLES:
                    reason = frpc_reject_reason()
                    if reason:
                        # Контракт §4: повторный отказ в логине == снятие с учёта.
                        # Перестаём звонить (иначе бесконечный цикл рестартов), но
                        # процесс НЕ завершаем — standby умеет восстановиться сам.
                        return revoked_standby(cfg, device_id, reason)

        if now - last_heartbeat > h_interval:
            telemetry, cpu_snap = collect_telemetry(cpu_snap)
            payload = {
                "device_id":  device_id,
                "uptime_s":   telemetry.get("uptime_s", 0),
                "telemetry":  telemetry,
                "fw_version": get_fw_version(),
                "hw_variant": HW_VARIANT,
            }
            # Аутентификация heartbeat (Phase C): секрет уходит ВВЕРХ, если он есть.
            # Без него облако в режиме grace всё ещё принимает биение, в strict —
            # отклоняет. Направление одностороннее: ответ по-прежнему не читается.
            if device_secret:
                payload["device_secret"] = device_secret
            # Send-only by design: the response carries no commands (F1
            # removed cloud-side too); nothing here interprets it.
            api_post(f"{api_url}/heartbeat", payload)
            _write_status("active", device_id=device_id, tunnel=tunnel,
                          serial=cfg["device"]["serial"],
                          identity="present" if device_secret else "absent",
                          last_heartbeat=int(now))
            last_heartbeat = now

        time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description="SA-02m Cloud Agent (frpc)")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    args = parser.parse_args()

    os.makedirs("/etc/sa02m-cloud", exist_ok=True)

    cfg = load_config(args.config)

    # Один цикл на весь жизненный путь: привязка -> работа -> (при отказе облака)
    # standby -> восстановление или повторная привязка. Процесс не завершается
    # сам: юнит имеет Restart=on-failure, который чистый выход НЕ перезапускает,
    # поэтому любой выход отсюда означал бы «устройство молчит до приезда людей».
    while True:
        if not cfg["cloud"].getboolean("enrolled"):
            bootstrap_loop(cfg, args.config)
            cfg = load_config(args.config)
        if active_loop(cfg) == "repair":
            cfg["cloud"]["enrolled"] = "false"   # обратно в привязку


if __name__ == "__main__":
    main()
