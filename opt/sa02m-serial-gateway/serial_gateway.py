#!/usr/bin/env python3
"""SA-02m RS-485 → Ethernet gateway daemon v1.0.

Modes per port:
  modbus_tcp   — Modbus TCP server (MBAP↔RTU). One request at a time.
  rtu_over_tcp — Raw RTU bytes over TCP (no MBAP stripping). Queue-based.
  transparent  — Raw serial bytes ↔ TCP. Multi-client fan-out from RS-485.

Config:  /etc/sa02m-gateway.yaml  (env SA02M_GATEWAY_CONFIG to override)
Status:  /run/sa02m-gateway/status.json  (updated every 5 s)
Lock:    /var/lock/sa02m-gateway-{port}.lock
Systemd: sd_notify READY=1 / WATCHDOG=1
"""

import argparse
import asyncio
import fcntl
import json
import logging
import os
import signal
import socket
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("pyyaml not installed: pip3 install pyyaml")

try:
    import serial
except ImportError:
    sys.exit("pyserial not installed: pip3 install pyserial")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gateway")

# ── Paths ─────────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(os.environ.get("SA02M_GATEWAY_CONFIG", "/etc/sa02m-gateway.yaml"))
STATUS_DIR  = Path("/run/sa02m-gateway")
STATUS_FILE = STATUS_DIR / "status.json"
LOCK_DIR    = Path("/var/lock")

# ── Modbus RTU helpers ────────────────────────────────────────────────────────

def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc

def _append_crc(data: bytes) -> bytes:
    return data + struct.pack('<H', _crc16(data))

def _check_crc(data: bytes) -> bool:
    return len(data) >= 4 and _crc16(data[:-2]) == struct.unpack('<H', data[-2:])[0]

# ── MBAP helpers ──────────────────────────────────────────────────────────────

MBAP_LEN = 6  # bytes in Modbus TCP header

# Wiren Board Fast Modbus probe identifiers
_FM_PROBE_PAYLOAD = b"WB-FAST-MODBUS?"
_FM_PROBE_RESP    = b"WB-FAST-MODBUS-OK"

def _parse_mbap_header(raw: bytes):
    """Return (transaction_id, protocol_id, length) or None if too short."""
    if len(raw) < MBAP_LEN:
        return None
    return struct.unpack('>HHH', raw[:MBAP_LEN])

def _build_mbap_response(tid: int, unit_id: int, pdu: bytes) -> bytes:
    length = 1 + len(pdu)
    return struct.pack('>HHH', tid, 0, length) + bytes([unit_id]) + pdu

# ── Inter-frame gap ───────────────────────────────────────────────────────────

def _inter_frame_sec(baudrate: int) -> float:
    """3.5 character times at given baud + 1 ms margin."""
    char_time = 11.0 / max(baudrate, 1)       # 8 data + start + stop + parity
    return max(3.5 * char_time, 0.00175) + 0.001

def _default_serial(port_name: str) -> tuple[int, int]:
    """Factory defaults: (baudrate, stopbits) per COM port."""
    if port_name in ('COM4', 'COM5'):
        return 115200, 1
    return 19200, 1

def _rtu_char_time_s(baudrate: int) -> float:
    return 10.0 / max(baudrate, 300)

def _modbus_read_frame_len(data: bytes) -> int:
    """Expected RTU response length once header bytes are present."""
    if len(data) < 3:
        return 0
    func = data[1]
    if func & 0x80:
        return 5
    if func in (0x01, 0x02, 0x03, 0x04):
        return 3 + int(data[2]) + 2
    if func in (0x06, 0x10):
        return 8
    return 0

def _strip_rtu_echo(request: bytes, data: bytes) -> bytes:
    """Remove TX echo from RS-485 adapters (use > not >=: FC06 reply == request)."""
    if (len(request) > 0 and len(data) > len(request)
            and data[:len(request)] == request):
        return data[len(request):]
    return data

# ── Serial worker (blocking, thread-safe) ─────────────────────────────────────

class SerialWorker:
    """Synchronous serial port wrapper; use with run_in_executor from asyncio."""

    def __init__(self, port_name: str, cfg: dict):
        self.port_name = port_name
        self.dev       = f"/dev/{port_name}"
        self._cfg      = cfg
        self._ser: serial.Serial | None = None
        self._lock     = threading.Lock()
        def_baud, def_stop = _default_serial(port_name)
        self._def_baud = def_baud
        self._def_stop = def_stop
        self._gap      = _inter_frame_sec(cfg.get('baudrate', def_baud))
        self._pending_rx: bytes = b''   # for transparent read_pending()

    # ── serial.Serial attribute maps ──────────────────────────────────────────
    _PARITY_MAP = {
        'none': serial.PARITY_NONE,
        'even': serial.PARITY_EVEN,
        'odd':  serial.PARITY_ODD,
    }
    _STOPBITS_MAP = {
        1:   serial.STOPBITS_ONE,
        1.5: serial.STOPBITS_ONE_POINT_FIVE,
        2:   serial.STOPBITS_TWO,
    }

    def open(self):
        baudrate = int(self._cfg.get('baudrate', self._def_baud))
        parity   = self._PARITY_MAP.get(
            str(self._cfg.get('parity', 'none')).lower(), serial.PARITY_NONE
        )
        stopbits = self._STOPBITS_MAP.get(
            self._cfg.get('stopbits', self._def_stop), serial.STOPBITS_ONE
        )
        databits = int(self._cfg.get('databits', 8))
        self._gap = _inter_frame_sec(baudrate)

        self._ser = serial.Serial(
            port=self.dev,
            baudrate=baudrate,
            bytesize=databits,
            parity=parity,
            stopbits=stopbits,
            timeout=0.05,
            write_timeout=1.0,
            exclusive=True,
        )
        log.info("Opened %s at %d baud %s%s%s",
                 self.dev, baudrate,
                 self._cfg.get('databits', 8),
                 {'none': 'N', 'even': 'E', 'odd': 'O'}.get(
                     str(self._cfg.get('parity', 'none')).lower(), 'N'),
                 self._cfg.get('stopbits', self._def_stop))

    def close(self):
        if self._ser and self._ser.is_open:
            try:
                self._ser.close()
            except Exception:
                pass

    def exchange(self, request: bytes, response_timeout: float = 1.0) -> bytes:
        """Send bytes on RS-485, receive Modbus RTU response. Blocking, thread-safe."""
        with self._lock:
            if not self._ser or not self._ser.is_open:
                raise RuntimeError("Port not open")
            try:
                time.sleep(self._gap)
                self._ser.reset_input_buffer()
                self._ser.write(request)
                self._ser.flush()
            except serial.SerialException as exc:
                raise RuntimeError(f"Write error: {exc}") from exc

            baudrate = int(self._cfg.get('baudrate', self._def_baud))
            char_time = _rtu_char_time_s(baudrate)
            post_send = max(0.001, min(0.02, char_time * 3.5 + 0.002))
            time.sleep(post_send)

            deadline = time.monotonic() + response_timeout
            buf = b""
            last_recv = time.monotonic()
            silence = max(0.02, char_time * 3.5)

            while time.monotonic() < deadline:
                waiting = self._ser.in_waiting
                if waiting:
                    buf += self._ser.read(max(waiting, 1))
                    last_recv = time.monotonic()
                    buf = _strip_rtu_echo(request, buf)
                    flen = _modbus_read_frame_len(buf)
                    if flen and len(buf) >= flen:
                        time.sleep(self._gap)
                        return buf[:flen]
                elif buf and (time.monotonic() - last_recv) >= silence:
                    buf = _strip_rtu_echo(request, buf)
                    flen = _modbus_read_frame_len(buf)
                    if flen and len(buf) >= flen:
                        time.sleep(self._gap)
                        return buf[:flen]
                    break
                else:
                    time.sleep(0.001)

            buf = _strip_rtu_echo(request, buf)
            time.sleep(self._gap)
            return buf

    def write_raw(self, data: bytes):
        """Write raw bytes without awaiting a response (transparent TX)."""
        with self._lock:
            if self._ser and self._ser.is_open:
                self._ser.reset_input_buffer()
                self._ser.write(data)
                self._ser.flush()

    def read_pending(self) -> bytes:
        """Return any bytes waiting in RX buffer (non-blocking). Used by transparent mode."""
        if not self._ser or not self._ser.is_open:
            return b''
        try:
            waiting = self._ser.in_waiting
            if waiting:
                return self._ser.read(waiting)
        except Exception:
            pass
        return b''

# ── Port statistics ────────────────────────────────────────────────────────────

class PortStats:
    __slots__ = ('bytes_tx', 'bytes_rx', 'requests', 'errors',
                 'tcp_clients', 'started_at', 'last_error')

    def __init__(self):
        self.bytes_tx    = 0
        self.bytes_rx    = 0
        self.requests    = 0
        self.errors      = 0
        self.tcp_clients = 0
        self.started_at  = time.time()
        self.last_error  = ""

    def to_dict(self) -> dict:
        return {
            "bytes_tx":    self.bytes_tx,
            "bytes_rx":    self.bytes_rx,
            "requests":    self.requests,
            "errors":      self.errors,
            "tcp_clients": self.tcp_clients,
            "uptime_s":    int(time.time() - self.started_at),
            "last_error":  self.last_error,
        }

# ── Lock file ─────────────────────────────────────────────────────────────────

class PortLock:
    def __init__(self, port_name: str):
        self._path = LOCK_DIR / f"sa02m-gateway-{port_name}.lock"
        self._fd: int | None = None

    def acquire(self) -> bool:
        try:
            fd = os.open(str(self._path), os.O_CREAT | os.O_WRONLY, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(fd, str(os.getpid()).encode())
            self._fd = fd
            return True
        except (OSError, BlockingIOError):
            return False

    def release(self):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError:
                pass
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass
            self._fd = None

# ── Modbus TCP gateway ────────────────────────────────────────────────────────

class ModbusTcpGateway:
    """Modbus TCP server: MBAP ↔ RTU, one in-flight request per port."""

    def __init__(self, port_name: str, cfg: dict,
                 worker: SerialWorker, stats: PortStats,
                 executor: ThreadPoolExecutor):
        self.port_name    = port_name
        self._worker      = worker
        self._stats       = stats
        self._executor    = executor
        self._tcp_port    = int(cfg.get('tcp_port', 502))
        self._fm_probe    = bool(cfg.get('fast_modbus_probe', True))
        self._server      = None
        self._req_lock    = asyncio.Lock()  # one RTU exchange at a time

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle_client, '0.0.0.0', self._tcp_port,
            reuse_address=True,
        )
        log.info("[%s] Modbus TCP server on :%d", self.port_name, self._tcp_port)

    async def stop(self):
        if self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass

    async def _handle_client(self,
                              reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter):
        peer = writer.get_extra_info('peername')
        log.info("[%s] Modbus TCP connect from %s", self.port_name, peer)
        self._stats.tcp_clients += 1
        try:
            await self._client_loop(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as exc:
            log.warning("[%s] client error: %s", self.port_name, exc)
            self._stats.errors += 1
            self._stats.last_error = str(exc)
        finally:
            self._stats.tcp_clients -= 1
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            log.info("[%s] Modbus TCP disconnect %s", self.port_name, peer)

    async def _client_loop(self, reader, writer):
        loop = asyncio.get_event_loop()
        while True:
            # Read MBAP header (6 bytes)
            try:
                header = await asyncio.wait_for(
                    reader.readexactly(MBAP_LEN), timeout=30.0
                )
            except asyncio.TimeoutError:
                break

            parsed = _parse_mbap_header(header)
            if parsed is None:
                break
            tid, pid, length = parsed

            if length < 1 or length > 253:
                log.warning("[%s] invalid MBAP length %d", self.port_name, length)
                break

            body = await asyncio.wait_for(
                reader.readexactly(length), timeout=5.0
            )
            unit_id = body[0]
            pdu     = body[1:]
            if not pdu:
                continue
            fc = pdu[0]

            # Fast Modbus probe: FC=0x47, unit_id=0x00, payload="WB-FAST-MODBUS?"
            if (self._fm_probe
                    and fc == 0x47
                    and unit_id == 0x00
                    and pdu[1:1+len(_FM_PROBE_PAYLOAD)] == _FM_PROBE_PAYLOAD):
                resp_pdu = bytes([0x47]) + _FM_PROBE_RESP
                writer.write(_build_mbap_response(tid, unit_id, resp_pdu))
                await writer.drain()
                continue

            # Build RTU frame and exchange with serial port
            async with self._req_lock:
                rtu_req = _append_crc(bytes([unit_id]) + pdu)
                self._stats.bytes_tx += len(rtu_req)

                try:
                    rtu_resp = await asyncio.wait_for(
                        loop.run_in_executor(
                            self._executor, self._worker.exchange, rtu_req
                        ),
                        timeout=3.0,
                    )
                except asyncio.TimeoutError:
                    rtu_resp = b''

                if not rtu_resp:
                    # Modbus exception: gateway target device failed to respond (11)
                    writer.write(_build_mbap_response(
                        tid, unit_id, bytes([fc | 0x80, 0x0B])
                    ))
                    await writer.drain()
                    self._stats.errors += 1
                    self._stats.last_error = f"RTU timeout ({self.port_name})"
                    continue

                # Strip Fast Modbus arbitration prefix byte 0xFF
                while rtu_resp and rtu_resp[0] == 0xFF:
                    rtu_resp = rtu_resp[1:]

                if not _check_crc(rtu_resp):
                    log.debug("[%s] bad CRC in RTU response", self.port_name)
                    writer.write(_build_mbap_response(
                        tid, unit_id, bytes([fc | 0x80, 0x0A])  # exception 10
                    ))
                    await writer.drain()
                    self._stats.errors += 1
                    continue

                self._stats.bytes_rx += len(rtu_resp)
                self._stats.requests += 1

                # RTU response → MBAP (strip 2-byte CRC)
                resp_unit = rtu_resp[0]
                resp_pdu  = rtu_resp[1:-2]
                writer.write(_build_mbap_response(tid, resp_unit, resp_pdu))
                await writer.drain()

# ── RTU over TCP gateway ──────────────────────────────────────────────────────

class RtuOverTcpGateway:
    """Raw RTU bytes over TCP (no MBAP header). Queue-based serialisation."""

    def __init__(self, port_name: str, cfg: dict,
                 worker: SerialWorker, stats: PortStats,
                 executor: ThreadPoolExecutor):
        self.port_name = port_name
        self._worker   = worker
        self._stats    = stats
        self._executor = executor
        self._tcp_port = int(cfg.get('tcp_port', 8502))
        self._server   = None
        self._lock     = asyncio.Lock()

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle_client, '0.0.0.0', self._tcp_port,
            reuse_address=True,
        )
        log.info("[%s] RTU-over-TCP server on :%d", self.port_name, self._tcp_port)

    async def stop(self):
        if self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass

    async def _handle_client(self, reader, writer):
        peer = writer.get_extra_info('peername')
        log.info("[%s] RTU-over-TCP connect from %s", self.port_name, peer)
        self._stats.tcp_clients += 1
        loop = asyncio.get_event_loop()
        try:
            while True:
                try:
                    data = await asyncio.wait_for(reader.read(512), timeout=30.0)
                except asyncio.TimeoutError:
                    continue
                if not data:
                    break

                async with self._lock:
                    self._stats.bytes_tx += len(data)
                    try:
                        resp = await asyncio.wait_for(
                            loop.run_in_executor(
                                self._executor, self._worker.exchange, data
                            ),
                            timeout=3.0,
                        )
                    except asyncio.TimeoutError:
                        resp = b''

                    if resp:
                        self._stats.bytes_rx += len(resp)
                        self._stats.requests += 1
                        writer.write(resp)
                        await writer.drain()
        except (ConnectionResetError, asyncio.IncompleteReadError, BrokenPipeError):
            pass
        except Exception as exc:
            self._stats.errors += 1
            self._stats.last_error = str(exc)
        finally:
            self._stats.tcp_clients -= 1
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

# ── Transparent gateway ───────────────────────────────────────────────────────

class TransparentGateway:
    """Raw bytes bidirectional bridge; RS-485 RX fan-out to all TCP clients."""

    def __init__(self, port_name: str, cfg: dict,
                 worker: SerialWorker, stats: PortStats,
                 executor: ThreadPoolExecutor):
        self.port_name  = port_name
        self._worker    = worker
        self._stats     = stats
        self._executor  = executor
        self._tcp_port  = int(cfg.get('tcp_port', 9502))
        self._server    = None
        self._clients: set[asyncio.StreamWriter] = set()
        self._cli_lock  = asyncio.Lock()
        self._stop_evt  = asyncio.Event()

    async def start(self):
        self._stop_evt.clear()
        self._server = await asyncio.start_server(
            self._handle_client, '0.0.0.0', self._tcp_port,
            reuse_address=True,
        )
        asyncio.create_task(self._serial_read_loop(), name=f"ser-rx-{self.port_name}")
        log.info("[%s] Transparent server on :%d", self.port_name, self._tcp_port)

    async def stop(self):
        self._stop_evt.set()
        if self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
        async with self._cli_lock:
            for w in list(self._clients):
                try:
                    w.close()
                except Exception:
                    pass
            self._clients.clear()

    async def _handle_client(self, reader, writer):
        peer = writer.get_extra_info('peername')
        log.info("[%s] Transparent connect from %s", self.port_name, peer)
        async with self._cli_lock:
            self._clients.add(writer)
        self._stats.tcp_clients += 1
        loop = asyncio.get_event_loop()
        try:
            while not self._stop_evt.is_set():
                try:
                    data = await asyncio.wait_for(reader.read(1024), timeout=30.0)
                except asyncio.TimeoutError:
                    continue
                if not data:
                    break
                self._stats.bytes_tx += len(data)
                await loop.run_in_executor(
                    self._executor, self._worker.write_raw, data
                )
        except (ConnectionResetError, asyncio.IncompleteReadError, BrokenPipeError):
            pass
        except Exception as exc:
            self._stats.errors += 1
            self._stats.last_error = str(exc)
        finally:
            async with self._cli_lock:
                self._clients.discard(writer)
            self._stats.tcp_clients -= 1
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _serial_read_loop(self):
        """Poll serial RX and fan-out to all connected TCP clients."""
        loop = asyncio.get_event_loop()
        while not self._stop_evt.is_set():
            try:
                data = await loop.run_in_executor(
                    self._executor, self._worker.read_pending
                )
                if data:
                    self._stats.bytes_rx += len(data)
                    async with self._cli_lock:
                        dead: set[asyncio.StreamWriter] = set()
                        for w in self._clients:
                            try:
                                w.write(data)
                                await w.drain()
                            except Exception:
                                dead.add(w)
                        self._clients -= dead
                else:
                    await asyncio.sleep(0.01)
            except Exception as exc:
                log.debug("[%s] serial read loop: %s", self.port_name, exc)
                await asyncio.sleep(0.1)

# ── Per-port coordinator ──────────────────────────────────────────────────────

class PortGateway:
    """Owns the serial worker, lock file and selected gateway type for one port."""

    _GATEWAY_CLASSES = {
        'modbus_tcp':   ModbusTcpGateway,
        'rtu_over_tcp': RtuOverTcpGateway,
        'transparent':  TransparentGateway,
    }

    def __init__(self, port_name: str, cfg: dict, executor: ThreadPoolExecutor):
        self.port_name = port_name
        self.mode      = str(cfg.get('mode', 'disabled'))
        self.enabled   = bool(cfg.get('enabled', False))
        self._cfg      = cfg
        self._executor = executor
        self._lock     = PortLock(port_name)
        self._worker: SerialWorker | None = None
        self._gw = None
        self.stats     = PortStats()

    async def start(self) -> bool:
        if not self.enabled or self.mode not in self._GATEWAY_CLASSES:
            return False

        if not self._lock.acquire():
            log.warning("[%s] locked by another process — skipped", self.port_name)
            self.stats.last_error = "Порт занят другим процессом"
            return False

        try:
            self._worker = SerialWorker(self.port_name, self._cfg)
            self._worker.open()
        except Exception as exc:
            log.error("[%s] serial open failed: %s", self.port_name, exc)
            self.stats.last_error = str(exc)
            self.stats.errors    += 1
            self._lock.release()
            return False

        try:
            cls     = self._GATEWAY_CLASSES[self.mode]
            self._gw = cls(self.port_name, self._cfg,
                           self._worker, self.stats, self._executor)
            await self._gw.start()
            return True
        except Exception as exc:
            log.error("[%s] gateway start failed: %s", self.port_name, exc)
            self.stats.last_error = str(exc)
            self.stats.errors    += 1
            self._teardown()
            return False

    async def stop(self):
        if self._gw:
            try:
                await self._gw.stop()
            except Exception:
                pass
            self._gw = None
        self._teardown()

    def _teardown(self):
        if self._worker:
            self._worker.close()
            self._worker = None
        self._lock.release()

    def is_running(self) -> bool:
        return self._gw is not None

# ── Status helpers ────────────────────────────────────────────────────────────

def _write_status(ports: dict[str, 'PortGateway']):
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "daemon":    "sa02m-serial-gateway",
        "timestamp": time.time(),
        "ports":     {},
    }
    for name, pg in ports.items():
        payload["ports"][name] = {
            "enabled": pg.enabled,
            "mode":    pg.mode,
            "running": pg.is_running(),
            **pg.stats.to_dict(),
        }
    try:
        tmp = STATUS_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(payload, ensure_ascii=False))
        tmp.rename(STATUS_FILE)
    except Exception as exc:
        log.debug("Status write: %s", exc)

# ── sd_notify ─────────────────────────────────────────────────────────────────

def _sd_notify(msg: str):
    sock_path = os.environ.get("NOTIFY_SOCKET", "")
    if not sock_path:
        return
    abstract = sock_path.startswith('@')
    addr = ('\0' + sock_path[1:]) if abstract else sock_path
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(msg.encode(), addr)
    except Exception:
        pass

# ── Main daemon ────────────────────────────────────────────────────────────────

class GatewayDaemon:
    def __init__(self, config_path: Path):
        self._config_path = config_path
        self._ports: dict[str, PortGateway] = {}
        self._executor    = ThreadPoolExecutor(
            max_workers=12, thread_name_prefix="gw-serial"
        )
        self._reload_flag = False
        self._stop_flag   = False

    def _load_config(self) -> dict:
        with open(self._config_path, encoding='utf-8') as fh:
            return yaml.safe_load(fh) or {}

    async def _start_ports(self, cfg: dict):
        ports_cfg = cfg.get('ports', {}) or {}
        for port_name, port_cfg in ports_cfg.items():
            if not isinstance(port_cfg, dict):
                continue
            pg = PortGateway(port_name, port_cfg, self._executor)
            ok = await pg.start()
            if ok:
                log.info("[%s] mode=%s  tcp_port=%s",
                         port_name,
                         port_cfg.get('mode'),
                         port_cfg.get('tcp_port'))
            self._ports[port_name] = pg

    async def _stop_ports(self):
        for pg in list(self._ports.values()):
            await pg.stop()
        self._ports.clear()

    async def run(self):
        try:
            cfg = self._load_config()
        except Exception as exc:
            log.error("Cannot load config %s: %s", self._config_path, exc)
            sys.exit(1)

        await self._start_ports(cfg)

        _sd_notify("READY=1")
        running = sum(1 for pg in self._ports.values() if pg.is_running())
        log.info("Gateway daemon ready — %d/%d port(s) active",
                 running, len(self._ports))

        loop = asyncio.get_event_loop()
        loop.add_signal_handler(
            signal.SIGHUP,  lambda: setattr(self, '_reload_flag', True))
        loop.add_signal_handler(
            signal.SIGTERM, lambda: setattr(self, '_stop_flag',   True))
        loop.add_signal_handler(
            signal.SIGINT,  lambda: setattr(self, '_stop_flag',   True))

        wd_interval  = 10.0
        last_wd      = time.monotonic()
        last_status  = 0.0

        while not self._stop_flag:
            now = time.monotonic()

            if self._reload_flag:
                self._reload_flag = False
                log.info("SIGHUP — reloading config…")
                await self._stop_ports()
                try:
                    cfg = self._load_config()
                    await self._start_ports(cfg)
                    running = sum(1 for pg in self._ports.values() if pg.is_running())
                    log.info("Config reloaded — %d/%d port(s) active",
                             running, len(self._ports))
                except Exception as exc:
                    log.error("Reload failed: %s", exc)

            if now - last_status >= 5.0:
                _write_status(self._ports)
                last_status = now

            if now - last_wd >= wd_interval:
                _sd_notify("WATCHDOG=1")
                last_wd = now

            await asyncio.sleep(0.5)

        log.info("Shutting down…")
        await self._stop_ports()
        _write_status(self._ports)
        self._executor.shutdown(wait=False)

# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="SA-02m RS-485→Ethernet gateway"
    )
    parser.add_argument('--config', default=str(CONFIG_PATH),
                        help='Path to YAML config (default: %(default)s)')
    parser.add_argument('--debug', action='store_true',
                        help='Enable DEBUG logging')
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    daemon = GatewayDaemon(Path(args.config))
    asyncio.run(daemon.run())


if __name__ == '__main__':
    main()
