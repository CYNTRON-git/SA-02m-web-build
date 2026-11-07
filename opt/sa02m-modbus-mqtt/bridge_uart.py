"""Receive limits for RTU register reads on SA-02m serial ports."""
from __future__ import annotations

import os
import re


def uart_line_index(port_path: str) -> int | None:
    """/dev/COM3 → /dev/ttyS4 → 4; None for a port that is not an 8250 ttyS
    (a USB adapter has no line in /proc/tty/driver/serial)."""
    m = re.fullmatch(r"ttyS(\d+)", os.path.basename(os.path.realpath(port_path)))
    return int(m.group(1)) if m else None


# ── Host RX FIFO margin (8250 UART overrun guard) ─────────────────────────────
# The A40i UARTs (dw-apb-uart, 16550-class) buffer 64 received bytes. On the
# PREEMPT_RT kernel the RX IRQ thread is sometimes served later than 64 char
# times (5.5 ms at 115200); the FIFO then overruns and a longer reply loses
# bytes. The source of that latency is not isolated: moving eth/mmc off the
# UART's CPU did not change it, moving the UART IRQs to CPU3 cut it ~25x but
# not to zero (docs/decisions/rt-core-affinity.md).
# The kernel marks the loss with a NUL after the byte it read first, and n_tty
# in raw mode hands that NUL to us as data (evidence from bench 1.135:
# docs/bugs/BUGLOG.md, 2026-09-30).
# A reply that fits the FIFO survives any IRQ latency, so fast 8250 lines cap
# the registers per FC03/FC04 read. The margin leaves room for an 8-byte
# request echo on a transceiver that does not mute RX while it transmits.
UART_RX_FIFO_BYTES = 64
UART_RX_SAFE_BYTES = UART_RX_FIFO_BYTES - 8
# Below this the FIFO holds >= 11 ms of line time (33 ms at 19200).
UART_FIFO_GUARD_MIN_BAUD = 57600
MODBUS_MAX_READ_REGS = 125
# Names that are a built-in 8250 line on this board: /dev/COMx is a udev
# symlink to one (scripts/01-system.sh 99-sa02m-serial.rules).
_BUILTIN_UART_NAME_RE = re.compile(r"COM[1-5]|ttyS\d+")


def _unresolved_builtin_uart(port_path) -> bool:
    """A built-in UART name whose device node is not there yet (udev has not
    linked /dev/COMx): fail closed, it is capped until the open re-resolves it."""
    return (bool(_BUILTIN_UART_NAME_RE.fullmatch(os.path.basename(str(port_path))))
            and not os.path.exists(port_path))


def fifo_safe_read_regs(port_path, baudrate: int) -> int:
    """Registers per FC03/FC04 read that keep the reply inside the host FIFO.

    Capped only on an 8250 ttyS line (uart_line_index — a USB adapter buffers
    on its own side, TCP never gets here) at >= UART_FIFO_GUARD_MIN_BAUD, and
    on a built-in UART name not resolvable yet (fail closed); the cap is even
    so a read split at it keeps even-aligned 32-bit pairs whole.
    """
    if not port_path or int(baudrate) < UART_FIFO_GUARD_MIN_BAUD:
        return MODBUS_MAX_READ_REGS
    if uart_line_index(port_path) is None and not _unresolved_builtin_uart(port_path):
        return MODBUS_MAX_READ_REGS
    n = (UART_RX_SAFE_BYTES - 5) // 2
    return n - n % 2
