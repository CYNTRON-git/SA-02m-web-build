#!/usr/bin/env python3
"""
SA-02m Cloud Agent — frpc reverse-tunnel edition.

Pairs the device with the cloud (cloud.cyntron.ru), maintains the frpc
reverse tunnel, and POSTs send-only telemetry heartbeats.

Contract: cloud repo docs/contracts/cloud-enrollment.md (frozen).
SECURITY: there is deliberately NO command channel — the cloud can never
make this device execute anything. The heartbeat is send-only: the ONLY
thing read back is the refusal reason (HTTP status + `error`) of a NON-200
response. A 200 body is JSON-parsed inside api_post (shared with claim/enroll)
but the parsed value never reaches any agent logic: the heartbeat call site
discards api_post's return and _note_heartbeat forces error=None on a 200;
an unparseable 200 is recorded as status 0, i.e. not a success
(tests/test_agent.py, tests/test_revoke_standdown.py pin all of it). A
refusal stated N times makes the
board erase its own cloud binding (stand_down) — a cloud-driven action that
is confined to the binding and needs TLS-verified HTTPS to be stated
(docs/threat-model.md §3). (The former handle_command() root channel —
threat model F1 — was removed in Phase B together with WireGuard.)

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


# Keys that only mean something while the binding is LIVE. Any other state
# must not carry them: the file is rewritten whole on every write, but a
# writer that passed a stale `tunnel` through would put «Туннель: Работает»
# on the card of a board that has no binding (bench 1.135, 2026-09-03).
LIVE_ONLY_KEYS = ("tunnel", "last_heartbeat", "identity")


def _write_status(state: str, **kw):
    """Write machine-readable status for CGI/web UI. Whole-file rewrite —
    nothing from a previous state survives — and the live-only keys are
    dropped by construction unless the state is `active`."""
    if state != "active":
        for key in LIVE_ONLY_KEYS:
            kw.pop(key, None)
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
# Heartbeat refusal side channel (threat-model F1, contract §2/§4). The
# heartbeat stays SEND-ONLY: its call site never captures api_post's result
# (tests/test_agent.py::test_heartbeat_response_not_interpreted pins that by
# regex). What the agent needs from a heartbeat is one bit the cloud is
# entitled to say — "I refuse you, and why" — and that arrives on a NON-200
# response. api_post records (status, error) of the last heartbeat here; on a
# 200 it records the status ONLY and the body is not read for it. The
# classifier below sees nothing but (status, error).
_HEARTBEAT_LAST = {"status": None, "error": None}


def _note_heartbeat(url: str, status: int, error):
    if not url.rstrip("/").endswith("/heartbeat"):
        return
    _HEARTBEAT_LAST["status"] = status
    _HEARTBEAT_LAST["error"] = error if status != 200 else None


def heartbeat_refusal() -> dict:
    """(status, error) of the LAST heartbeat — the only thing read from it."""
    return dict(_HEARTBEAT_LAST)


def _refusal_error(body) -> str:
    """The `error` string of a refusal body, or "" — nothing else is read."""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, str):
            return err.strip().lower()
    return ""


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
            _note_heartbeat(url, resp.status, None)
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read()[:300]
        log.warning("POST %s -> HTTP %d: %s", url, e.code, body)
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = None
        _note_heartbeat(url, e.code, _refusal_error(parsed))
        return e.code, parsed
    except Exception as e:
        # Transport failure — AND a 200 whose body is not JSON (json.loads
        # above raises into here): recorded as status 0, so it is neither a
        # refusal nor a success for the heartbeat tracker.
        log.debug("POST %s error: %s", url, e)
        _note_heartbeat(url, 0, None)
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
# `subdomain not enrolled` is what frps prints for an UNCLAIMED board: under
# FRP_IDENTITY_MODE=grace a heartbeat without a secret gets no 403 at all, so
# this marker is the ONLY signal of a detach — mandatory, not a bonus.
FRPC_REJECT_MARKERS = ("device revoked", "device identity required",
                       "credential mismatch", "device not enrolled",
                       "no credential issued", "invalid credential",
                       "subdomain not enrolled")

# ── Refusal classes (contract §4) ─────────────────────────────────────────────
# What the cloud / frps SAID → what it means for this board. Only these
# strings are refusals; a network error, a timeout, a 5xx, a DNS failure or an
# unrecognised 403 is a tunnel/transport failure and NEVER a stand-down.
REFUSAL_CLASS_REVOKED  = "revoked"    # owner pressed «Отозвать доступ»
REFUSAL_CLASS_UNLINKED = "unlinked"   # owner detached («Отвязать»/«Забыть») — free to re-pair
REFUSAL_CLASS_UNKNOWN  = "unknown"    # identity refused for a reason the board cannot tell apart
HEARTBEAT_REFUSALS = {
    "device revoked":           REFUSAL_CLASS_REVOKED,
    "unknown device":           REFUSAL_CLASS_UNLINKED,
    "invalid credential":       REFUSAL_CLASS_UNKNOWN,
    "device identity required": REFUSAL_CLASS_UNKNOWN,
}
FRPS_REFUSALS = {
    "device revoked":           REFUSAL_CLASS_REVOKED,
    "subdomain not enrolled":   REFUSAL_CLASS_UNLINKED,
    "device not enrolled":      REFUSAL_CLASS_UNLINKED,
    "no credential issued":     REFUSAL_CLASS_UNLINKED,
    "credential mismatch":      REFUSAL_CLASS_UNKNOWN,
    "device identity required": REFUSAL_CLASS_UNKNOWN,
    "invalid credential":       REFUSAL_CLASS_UNKNOWN,
}
# N consecutive refusals of ONE class ⇒ stand-down: N refused heartbeats
# (≈ 30 s at the 10 s interval), or N watchdog ticks each bringing a NEW
# refusal line in the frpc journal. Any success resets — for the frps path a
# success is a tick with no new refusal line (the journal is read through a
# cursor, so one line is counted exactly once; see frpc_reject_reason).
REFUSAL_STANDDOWN_COUNT = 3

# Durable stand-down marker keys in agent.conf — IDENTITY, not configuration:
# cleared by a fresh enrollment and by the image/identity reset
# (docs/contracts/image-identity-reset.md §6).
STAND_DOWN_MARKER_KEYS = ("unlinked_at", "unlinked_reason", "unlinked_reason_text")

# Every `state` this agent writes to STATUS_FILE — the one home of the enum
# that docs/contracts/cloud-agent-status.md documents and cloud.js renders
# (tests/test_status_contract.py::test_status_state_enum_matches_contract_and_card).
STATUS_STATES = (
    "standby", "pairing", "pair_expired", "already_claimed", "claim_failed",
    "enrolling", "enroll_failed", "active",
    "revoked", "unlinked", "unlink_failed",
)

# Journal cursor of the last frpc line read. /run is tmpfs: a reboot starts
# afresh from the agent's own start time, which is what we want.
CURSOR_FILE = "/run/sa02m-cloud-frpc.cursor"
# The agent's own start, in a form `journalctl --since` accepts: the window of
# the very first read (no cursor yet) — narrow, never a previous life.
AGENT_STARTED_AT = time.strftime("%Y-%m-%d %H:%M:%S")


def classify_refusal(status, error) -> str:
    """Heartbeat verdict → refusal class or "" (not a refusal).

    Reads exactly two things: the HTTP status and the `error` string of a
    NON-200 body. A 200 is a success whatever it carries; anything that is
    not a 403 with a known reason is not a refusal.
    """
    if status != 403:
        return ""
    return HEARTBEAT_REFUSALS.get((error or "").strip().lower(), "")


def classify_frps_marker(marker: str) -> str:
    return FRPS_REFUSALS.get((marker or "").strip().lower(), "")


class RefusalTracker:
    """Counts CONSECUTIVE refusal EVENTS of one class; any success resets.

    An event is one refused heartbeat, or one NEW refusal line in the frpc
    journal (the cursor guarantees a line is fed here once, never re-read on
    the next tick). A refusal of a different class restarts the count at one
    (three refusals are only meaningful when they all say the same thing).
    On the heartbeat path a network error or a 5xx is neither a refusal nor a
    success and leaves the count alone; on the frps path a tick with no new
    refusal line IS a success.
    """

    def __init__(self, threshold: int = REFUSAL_STANDDOWN_COUNT):
        self.threshold = int(threshold)
        self.cls = ""
        self.count = 0

    def note_success(self):
        self.cls = ""
        self.count = 0

    def note_refusal(self, cls: str) -> bool:
        """Record one refusal; True when the threshold is reached."""
        if not cls:
            return False
        if cls == self.cls:
            self.count += 1
        else:
            self.cls = cls
            self.count = 1
        return self.count >= self.threshold


# In-memory twin of CURSOR_FILE. Within one process it is always the NEWER
# position (it moves before the file write), so _read_cursor prefers it; the
# file matters only at process start. What this buys is "never re-COUNTED":
# with the file unwritable the process keeps reading from the twin, and any
# tick whose cursor could not be saved is clean, so a line can never add a
# second count. Lost on restart — then /run's copy (if any) or the --since
# window takes over.
_MEM_CURSOR = {"cursor": ""}
# The `--since` window used whenever no cursor is at hand. It is spent by a
# read that yields a cursor and RE-ARMED by every read that does not (a failed
# journalctl, an empty or footerless answer, a dropped stale cursor) — so
# journalctl is invoked on every tick for the life of the process, never
# once. Recount-safe: every re-arm follows a tick that returned "" and reset
# the counter (review 1.0.6.26, round 10).
_SINCE_FROM = {"at": AGENT_STARTED_AT}
_WARNED = set()


def _warn_once(key: str, msg: str, *args) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    log.warning(msg, *args)


def _read_cursor(path: str = None) -> str:
    if _MEM_CURSOR["cursor"]:
        return _MEM_CURSOR["cursor"]
    try:
        with open(path or CURSOR_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def _save_cursor(cursor: str, path: str = None) -> bool:
    """Persist the cursor; the in-memory twin moves first. False on a failed
    write — the caller then treats the tick as CLEAN (B1): inability to save
    the cursor must never turn into re-reading the same window."""
    _MEM_CURSOR["cursor"] = cursor
    p = path or CURSOR_FILE
    try:
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            f.write(cursor)
        os.replace(tmp, p)
        return True
    except OSError as e:
        _warn_once("save:" + p, "journal cursor not saved (%s): %s — the tick counts as "
                   "clean; reads continue from the in-memory position, so nothing "
                   "is counted twice", p, e)
        return False


def _drop_cursor(path: str = None) -> None:
    """A cursor journalctl refuses (its entry rotated out): drop it and re-arm
    the one-shot window from NOW — nothing already counted can be re-read."""
    _MEM_CURSOR["cursor"] = ""
    try:
        os.unlink(path or CURSOR_FILE)
    except OSError:
        pass
    _SINCE_FROM["at"] = time.strftime("%Y-%m-%d %H:%M:%S")


def frpc_reject_reason(unit: str = FRPC_UNIT) -> str:
    """Причина серверного отказа из НОВЫХ строк журнала frpc, или "".

    Журнал читается по КУРСОРУ: `--after-cursor <последняя прочитанная строка>`
    (`--show-cursor` даёт новый курсор — он сохраняется в CURSOR_FILE и в
    памяти), а в самый первый раз — `--since <старт агента>`. Каждая строка
    отказа видна ровно один раз; такт без НОВЫХ строк отказа — успех.

    FAIL-CLOSED (ревью 1.0.6.26, B1 второго круга): любая невозможность
    получить или сохранить курсор означает «новых отказов нет» — такт чистый.
    Ответ journalctl без строки `-- cursor:` — чистый такт. Не сохранившийся
    курсор — чистый такт, следующее чтение идёт от позиции в памяти.
    НО журнал читается на КАЖДОМ такте (ревью, круг 10): окно `--since`
    тратится только чтением, которое вернуло курсор, а каждый выход без
    курсора (ошибка journalctl, пустой или без-футерный ответ, сброшенный
    устаревший курсор) взводит окно заново — иначе одно неудачное первое
    чтение глушило сигнал отвязки до перезапуска процесса. Повторного счёта
    это не даёт: каждому повторному взведению предшествует такт, вернувший ""
    и сбросивший счётчик. Лимита `-n` нет: на пути курсора окно и так
    ограничено, а на пути `--since` каждое чтение всё равно чистое, пока не
    появится курсор.

    Честность важнее удобства: «отозвано» показываем ТОЛЬКО когда сервер прямо
    это сказал. Недоступное облако, севшая сеть, упавший frpc или нечитаемый
    журнал — это не отказ ("" — как «нет новых отказов»; в сторону стирания
    привязки это никогда не ошибается)."""
    cursor = _read_cursor()
    since = ""
    if cursor:
        args = ["journalctl", "-u", unit, "--after-cursor", cursor,
                "--show-cursor", "--no-pager"]
    else:
        since = _SINCE_FROM["at"] or time.strftime("%Y-%m-%d %H:%M:%S")
        args = ["journalctl", "-u", unit, "--since", since,
                "--show-cursor", "--no-pager"]
        # Spent by THIS read; every exit below that yields no cursor puts it
        # back, so the next tick reads again (same window — nothing in it was
        # counted, so nothing can be counted twice).
        _SINCE_FROM["at"] = ""

    def _rearm():
        if not cursor:
            _SINCE_FROM["at"] = since

    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except Exception as e:
        log.debug("journalctl unavailable: %s", e)
        _rearm()
        return ""
    if r.returncode != 0:
        if cursor:
            # A stale cursor fails on every tick and would silently kill the
            # only detach signal (L8): drop it, read from NOW next time.
            log.warning("journalctl refused cursor %s (rc=%d) — dropping it, "
                        "next read starts from now", cursor, r.returncode)
            _drop_cursor()
        else:
            log.warning("journalctl failed (rc=%d): %s", r.returncode,
                        (r.stderr or "").strip()[:200])
            _rearm()
        return ""
    body = []
    new_cursor = ""
    for line in (r.stdout or "").splitlines():
        if line.startswith("-- cursor:"):
            new_cursor = line.split(":", 1)[1].strip()
        else:
            body.append(line)
    if not new_cursor:
        # An empty window (a connected, quiet frpc — the ordinary case on an
        # agent restart) or a footerless answer: clean tick, read again next.
        if body:
            _warn_once("nofooter", "journalctl returned %d line(s) without a cursor "
                       "footer — counted as clean", len(body))
        _rearm()
        return ""
    if not _save_cursor(new_cursor):
        return ""
    text = "\n".join(body).lower()
    # The LATEST refusal line wins when several new lines carry markers.
    found, at = "", -1
    for marker in FRPC_REJECT_MARKERS:
        pos = text.rfind(marker)
        if pos > at:
            found, at = marker, pos
    return found


# The cloud binding, enumerated. The stand-down may erase THESE and nothing
# else — the identity files the cloud issued at enrollment. api_url /
# server_host stay (configuration, not identity — the same rule the manual
# «Облако — отвязать» step in docs/deployment.md §3 follows), and so do the
# serial, the heartbeat interval and everything outside /etc/sa02m-cloud.
def binding_files():
    return [DEVICE_SECRET_FILE, FRPC_CONFIG]


def wipe_cloud_binding() -> dict:
    """Delete the identity files. Idempotent; a missing file is reported as
    `absent`, never hidden; any other OSError is logged and RAISED — a binding
    that could not be erased must not read as erased."""
    removed, absent = [], []
    for path in binding_files():
        name = os.path.basename(path)
        try:
            os.unlink(path)
            removed.append(name)
        except FileNotFoundError:
            absent.append(name)
        except OSError as e:
            # A partial wipe: say what DID go before raising, or that record
            # is lost with the exception.
            log.error("stand-down: could not remove %s: %s (already removed: %s; absent: %s)",
                      path, e, ", ".join(removed) or "nothing", ", ".join(absent) or "nothing")
            raise
    return {"removed": removed, "absent": absent}


def _status_state() -> str:
    try:
        with open(STATUS_FILE) as f:
            return str(json.load(f).get("state") or "")
    except Exception:
        return ""


def stand_down(cfg, config_path: str, cls: str, reason: str, systemctl=None) -> str:
    """The cloud refused this board N times for one reason: de-enroll locally.

    The SAME routine the manual «Отвязать» performs (docs/deployment.md §3):
    stop the tunnel and the heartbeats, `enrolled = false`, clear `device_id`,
    delete the device secret and frpc.toml, keep api_url / server_host, and
    leave a durable `unlinked_at` + reason in agent.conf. The status file gets
    `revoked` (owner revoked) or `unlinked` (detached / unknown) with the
    reason and the time, so the «Облако» card can say so.

    Returns "repair": the caller drops to the standby loop, which polls the
    web UI's pairing trigger — «Привязать заново» is one button press away and
    needs no SSH. The process never exits (Restart=on-failure would not bring
    it back).
    """
    run = systemctl or _systemctl
    log.error("cloud refused this device (%s: %s) — standing down: tunnel stopped, "
              "binding erased, waiting for a new pairing", cls, reason)
    run("stop", FRPC_UNIT)
    run("disable", FRPC_UNIT)
    try:
        wiped = wipe_cloud_binding()
    except OSError as e:
        # The binding is STILL on disk: say so — never «Подключено», never
        # «отвязано». main() retries the wipe from this state (low 2).
        PENDING_STAND_DOWN.update({"cls": cls, "reason": reason})
        log.error("stand-down: binding NOT erased: %s", e)
        _write_status("unlink_failed", reason="wipe_failed", detail=str(e), reason_class=cls,
                      refusal=reason, serial=cfg["device"]["serial"])
        return "unlink_failed"
    return _finish_stand_down(cfg, config_path, cls, reason, wiped)


# (cls, reason) of a stand-down whose wipe failed — main() finishes it once the
# wipe succeeds. Module-level because the failure crosses the loop boundary.
PENDING_STAND_DOWN = {"cls": "", "reason": ""}


def _stand_down_state(cls: str) -> str:
    return "revoked" if cls == REFUSAL_CLASS_REVOKED else "unlinked"


def _finish_stand_down(cfg, config_path: str, cls: str, reason: str, wiped: dict) -> str:
    """Bookkeeping after a SUCCESSFUL wipe: config, durable marker, status."""
    state = _stand_down_state(cls)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cfg["cloud"]["enrolled"] = "false"
    cfg["cloud"]["device_id"] = ""
    cfg["cloud"]["unlinked_at"] = stamp
    cfg["cloud"]["unlinked_reason"] = cls
    cfg["cloud"]["unlinked_reason_text"] = reason
    save_config(config_path, cfg)
    _write_status(state, reason=reason, reason_class=cls, unlinked_at=stamp,
                  serial=cfg["device"]["serial"])
    log.warning("cloud binding erased: removed %s; already absent %s",
                ", ".join(wiped["removed"]) or "nothing",
                ", ".join(wiped["absent"]) or "nothing")
    return "repair"


def retry_wipe_loop(cfg, config_path: str, sleep=time.sleep) -> str:
    """After a failed wipe: keep the error state on the card and retry the
    wipe every WATCHDOG_S until it succeeds, then finish the stand-down."""
    cls, reason = PENDING_STAND_DOWN["cls"], PENDING_STAND_DOWN["reason"]
    while True:
        sleep(WATCHDOG_S)
        try:
            wiped = wipe_cloud_binding()
        except OSError as e:
            log.error("stand-down retry: binding still NOT erased: %s", e)
            _write_status("unlink_failed", reason="wipe_failed", detail=str(e), reason_class=cls,
                          refusal=reason, serial=cfg["device"]["serial"])
            continue
        PENDING_STAND_DOWN.update({"cls": "", "reason": ""})
        return _finish_stand_down(cfg, config_path, cls, reason, wiped)


def restore_stand_down_status(cfg) -> bool:
    """On start in the stand-down state rebuild the status file from the
    durable marker in agent.conf (/run is tmpfs — after a reboot the card
    would otherwise show a bare «Не подключено» for a revoked board)."""
    if cfg["cloud"].getboolean("enrolled", fallback=False):
        return False
    stamp = cfg["cloud"].get("unlinked_at", "").strip()
    if not stamp:
        return False
    cls = cfg["cloud"].get("unlinked_reason", "").strip() or REFUSAL_CLASS_UNKNOWN
    reason = cfg["cloud"].get("unlinked_reason_text", "").strip() or cls
    _write_status(_stand_down_state(cls), reason=reason, reason_class=cls,
                  unlinked_at=stamp, serial=cfg["device"]["serial"], restored=True)
    return True


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
    # A new identity supersedes any stand-down marker (docs/contracts/
    # image-identity-reset.md §6 lists these three keys as identity).
    for key in STAND_DOWN_MARKER_KEYS:
        cfg.remove_option("cloud", key)
    # The frpc journal cursor is a per-enrollment read position: a new
    # identity starts reading from now (docs/contracts/image-identity-reset.md §6).
    _drop_cursor()
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
        # A reason the card can explain without the journal: the cloud still
        # lists this board under an owner (revoked, or simply not detached).
        _write_status("already_claimed", device_id=device_id, serial=serial,
                      reason="already claimed", reason_class="already_claimed",
                      since=int(time.time()))
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
    # After a stand-down the status already says revoked/unlinked with its
    # reason — the card must keep showing that, not a bare «Не подключено».
    if _status_state() not in ("revoked", "unlinked"):
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
def active_loop(cfg: configparser.ConfigParser, config_path: str = DEFAULT_CONFIG) -> str:
    """Основной цикл. Возвращает "repair" после stand-down (облако отказало
    N раз подряд по одной причине — привязка стёрта, нужна новая); иначе не
    возвращается."""
    api_url    = cfg["cloud"]["api_url"].rstrip("/")
    device_id  = get_device_id(cfg)
    h_interval = int(cfg["cloud"]["heartbeat_interval"])

    log.info("SA-02m Cloud Agent active (device %s, heartbeat %ds). "
             "Send-only: no command channel.", device_id, h_interval)

    last_heartbeat = 0.0
    last_watchdog  = 0.0
    tunnel         = ensure_frpc_running()
    cpu_snap       = None
    hb_refusals    = RefusalTracker()
    frps_refusals  = RefusalTracker()
    device_secret  = load_device_secret()
    log.info("per-device identity: %s",
             "present" if device_secret else "absent (legacy grace path)")

    while True:
        now = time.time()

        if now - last_watchdog > WATCHDOG_S:
            tunnel = ensure_frpc_running()
            last_watchdog = now
            # The journal is read on EVERY tick, not only when the unit is
            # down: frps refuses the proxies but the login succeeds, so the
            # unit stays active while the board is already detached
            # (`subdomain not enrolled`). Cursor-based: only lines NEW since
            # the last tick, each counted once; no new refusal line == success,
            # and so is any tick on which the cursor could not be obtained or
            # saved (fail-closed — see frpc_reject_reason).
            marker = frpc_reject_reason()
            if marker:
                cls = classify_frps_marker(marker)
                if cls and frps_refusals.note_refusal(cls):
                    # Контракт §4: повторный отказ == снятие с учёта.
                    return stand_down(cfg, config_path, cls, marker)
            else:
                frps_refusals.note_success()

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
            # removed cloud-side too); nothing here interprets it. The ONE bit
            # the board may learn is a refusal — (status, error) of a non-200,
            # left on the side channel by api_post, never a 200 body.
            api_post(f"{api_url}/heartbeat", payload)
            verdict = heartbeat_refusal()
            if verdict["status"] == 200:
                hb_refusals.note_success()
            else:
                cls = classify_refusal(verdict["status"], verdict["error"])
                if cls and hb_refusals.note_refusal(cls):
                    return stand_down(cfg, config_path, cls, verdict["error"] or "")
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
    # A board that stood down before this (re)boot: /run is empty, but
    # agent.conf carries the durable marker — put the reason back on the card.
    restore_stand_down_status(cfg)

    while True:
        if not cfg["cloud"].getboolean("enrolled"):
            bootstrap_loop(cfg, args.config)
            cfg = load_config(args.config)
        rc = active_loop(cfg, args.config)
        if rc == "unlink_failed":
            rc = retry_wipe_loop(cfg, args.config)
        if rc == "repair":
            # stand_down already wrote enrolled=false + the wiped identity to
            # disk — reload so the standby loop runs on what is on disk.
            cfg = load_config(args.config)


if __name__ == "__main__":
    main()
