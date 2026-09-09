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

# The shared binding-reset core. This file IS its authoritative home
# (opt/sa02m-cloud-agent/binding_core.py); the smart-home package carries a
# byte-identical copy, and the `binding-reset-parity` quality row keeps them
# equal. The explicit sys.path entry is what lets the tests load this agent by
# file path (importlib.spec_from_file_location) as well as systemd running it
# as a script — the script's own directory is only on sys.path in the latter.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import binding_core  # noqa: E402

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


# The counter, the cursor and the whole stand-down live in binding_core.py —
# ONE home for both doors (this agent and the smart-home client's Alice
# profile). What stays here is this door's FACTS: which files are the binding,
# which unit carries the tunnel, which strings the server may say, where the
# marker and the status file live. Nothing below re-implements a shared rule;
# the wrappers exist so the module surface (and every monkeypatch point the
# suites use) is unchanged by the extraction.
_MEM_CURSOR = binding_core._MEM_CURSOR
_SINCE_FROM = binding_core._SINCE_FROM
_WARNED = binding_core._WARNED
_warn_once = binding_core._warn_once
PENDING_STAND_DOWN = binding_core.PENDING_STAND_DOWN
# The core leaves the one-shot journal window empty at import; this door seeds
# it with its OWN start, which is the narrow window of the very first read.
_SINCE_FROM["at"] = AGENT_STARTED_AT


class RefusalTracker(binding_core.RefusalTracker):
    """The core's counter with THIS door's default threshold.

    Three, because this door reads evidence — a 403 reason, a journal marker —
    on a channel with no command semantics, and three consecutive of one class
    is what turns evidence into a verdict. The Alice door counts to one for the
    opposite reason (it receives a verdict); the number is the parameter, the
    counting is shared (`binding_core`).
    """

    def __init__(self, threshold: int = REFUSAL_STANDDOWN_COUNT):
        binding_core.RefusalTracker.__init__(self, threshold)


def _read_cursor(path: str = None) -> str:
    return binding_core.read_cursor(path or CURSOR_FILE)


def _save_cursor(cursor: str, path: str = None) -> bool:
    return binding_core.save_cursor(cursor, path or CURSOR_FILE)


def _drop_cursor(path: str = None) -> None:
    binding_core.drop_cursor(path or CURSOR_FILE)


def frpc_reject_reason(unit: str = FRPC_UNIT) -> str:
    """Причина серверного отказа из НОВЫХ строк журнала frpc, или "".

    Механика — курсор, окно `--since`, правило fail-closed «нет курсора ==
    чистый такт» — живёт в `binding_core.journal_reject_reason` (один дом,
    общий с дверью Алисы) и здесь не пересказывается. Дверные факты: юнит
    frpc, таблица маркеров `FRPC_REJECT_MARKERS`, файл курсора `CURSOR_FILE`.
    """
    return binding_core.journal_reject_reason(unit, FRPC_REJECT_MARKERS, CURSOR_FILE)


# The cloud binding, enumerated. The stand-down may erase THESE and nothing
# else — the identity files the cloud issued at enrollment. api_url /
# server_host stay (configuration, not identity — the same rule the manual
# «Облако — отвязать» step in docs/deployment.md §3 follows), and so do the
# serial, the heartbeat interval and everything outside /etc/sa02m-cloud.
def binding_files():
    return [DEVICE_SECRET_FILE, FRPC_CONFIG]


def wipe_cloud_binding() -> dict:
    """Erase the two identity files. Idempotence, the `absent` report and the
    raise-on-any-other-OSError rule are the core's (`binding_core.wipe_binding`);
    this door contributes only the clear-list above."""
    return binding_core.wipe_binding(binding_files)


def _status_state() -> str:
    try:
        with open(STATUS_FILE) as f:
            return str(json.load(f).get("state") or "")
    except Exception:
        return ""


def _stand_down_state(cls: str) -> str:
    return "revoked" if cls == REFUSAL_CLASS_REVOKED else "unlinked"


def _cloud_spec(cfg, config_path: str, classify=None, systemctl=None):
    """This door's descriptor for the shared core — built per call, so a test
    (or an operator override) that repoints a path or swaps `_systemctl`,
    `_write_status`, `save_config` or `wipe_cloud_binding` repoints the
    stand-down with it.

    `classify` is per CHANNEL, not per door: this agent reads TWO evidence
    channels — the heartbeat's 403 reason and the frpc journal's marker — each
    with its own table and its own counter. Every other field is identical, so
    one factory serves both.
    """
    run = systemctl or _systemctl

    def stop_tunnel():
        run("stop", FRPC_UNIT)
        run("disable", FRPC_UNIT)

    def read_marker():
        # Nothing to restore while the board is enrolled: a marker beside a
        # live binding is stale, not a verdict.
        if cfg["cloud"].getboolean("enrolled", fallback=False):
            return ("", "", "")
        stamp = cfg["cloud"].get("unlinked_at", "").strip()
        if not stamp:
            return ("", "", "")
        cls = cfg["cloud"].get("unlinked_reason", "").strip() or REFUSAL_CLASS_UNKNOWN
        return (stamp, cls, cfg["cloud"].get("unlinked_reason_text", "").strip())

    def write_marker(stamp: str, cls: str, reason: str):
        # De-enrolled AND explained, in one save: `enrolled=false` plus an empty
        # device_id are what put the standby loop back on the pairing trigger,
        # and the three marker keys are what survive the tmpfs status file.
        cfg["cloud"]["enrolled"] = "false"
        cfg["cloud"]["device_id"] = ""
        cfg["cloud"]["unlinked_at"] = stamp
        cfg["cloud"]["unlinked_reason"] = cls
        cfg["cloud"]["unlinked_reason_text"] = reason
        save_config(config_path, cfg)

    def clear_marker():
        for key in STAND_DOWN_MARKER_KEYS:
            cfg["cloud"].pop(key, None)
        save_config(config_path, cfg)

    def write_pending(cls: str, reason: str):
        # An honest no-op, and a door fact rather than a branch in the core:
        # on THIS door the retry runs in the process that failed. main() takes
        # the "unlink_failed" return straight into retry_wipe_loop, so the
        # in-memory record is never handed across a process boundary and a
        # durable copy would buy nothing. Should this process die mid-retry the
        # board is not stranded either: it comes back enrolled, dials, and the
        # cloud restates the same refusal — this door reads EVIDENCE and can
        # re-derive its verdict, which the Alice door (one delivered event)
        # cannot. That asymmetry is why only the other door persists.
        return None

    def read_pending():
        return ("", "")

    def write_status(state: str, **kw):
        _write_status(state, serial=cfg["device"]["serial"], **kw)

    return binding_core.SourceSpec(
        name="cloud",
        threshold=REFUSAL_STANDDOWN_COUNT,
        wipe=lambda: wipe_cloud_binding(),
        stop_tunnel=stop_tunnel,
        read_marker=read_marker,
        write_marker=write_marker,
        clear_marker=clear_marker,
        write_pending=write_pending,
        read_pending=read_pending,
        write_status=write_status,
        state_for_class=_stand_down_state,
        states=("revoked", "unlinked", "unlink_failed"),
        classify=classify or classify_refusal_evidence,
    )


def classify_refusal_evidence(evidence) -> str:
    """The heartbeat channel's classifier in the core's one-argument shape:
    the (status, error) pair `heartbeat_refusal()` leaves on the side channel."""
    status, error = evidence
    return classify_refusal(status, error)


def stand_down(cfg, config_path: str, cls: str, reason: str, systemctl=None) -> str:
    """The cloud refused this board N times for one reason: de-enroll locally.

    The SAME routine the manual «Отвязать» performs (docs/deployment.md §3):
    stop the tunnel and the heartbeats, `enrolled = false`, clear `device_id`,
    delete the device secret and frpc.toml, keep api_url / server_host, and
    leave a durable `unlinked_at` + reason in agent.conf. The status file gets
    `revoked` (owner revoked) or `unlinked` (detached / unknown) with the
    reason and the time, so the «Облако» card can say so. The sequence itself
    is `binding_core.stand_down` — shared with the Alice door, not restated.

    Returns "repair": the caller drops to the standby loop, which polls the
    web UI's pairing trigger — «Привязать заново» is one button press away and
    needs no SSH. The process never exits (Restart=on-failure would not bring
    it back). "unlink_failed" when the wipe itself failed; main() retries it.
    """
    return binding_core.stand_down(
        _cloud_spec(cfg, config_path, systemctl=systemctl), cls, reason)


def _finish_stand_down(cfg, config_path: str, cls: str, reason: str, wiped: dict) -> str:
    """Bookkeeping after a SUCCESSFUL wipe: config, durable marker, status."""
    return binding_core.finish_stand_down(
        _cloud_spec(cfg, config_path), cls, reason, wiped)


def retry_wipe_loop(cfg, config_path: str, sleep=time.sleep) -> str:
    """After a failed wipe: keep the error state on the card and retry the
    wipe every WATCHDOG_S until it succeeds, then finish the stand-down."""
    return binding_core.retry_wipe_loop(
        _cloud_spec(cfg, config_path), WATCHDOG_S, sleep=sleep)


def restore_stand_down_status(cfg) -> bool:
    """On start in the stand-down state rebuild the status file from the
    durable marker in agent.conf (/run is tmpfs — after a reboot the card
    would otherwise show a bare «Не подключено» for a revoked board)."""
    return binding_core.restore_stand_down_status(_cloud_spec(cfg, DEFAULT_CONFIG))


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
    # Two evidence channels, two counters, two classifiers — one descriptor
    # factory. The counting itself is `binding_core.refusal_verdict`, shared
    # with the Alice door, which passes the same way with a threshold of one.
    hb_spec        = _cloud_spec(cfg, config_path, classify=classify_refusal_evidence)
    frps_spec      = _cloud_spec(cfg, config_path, classify=classify_frps_marker)
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
                cls = binding_core.refusal_verdict(frps_spec, frps_refusals, marker)
                if cls:
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
                cls = binding_core.refusal_verdict(
                    hb_spec, hb_refusals, (verdict["status"], verdict["error"]))
                if cls:
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
