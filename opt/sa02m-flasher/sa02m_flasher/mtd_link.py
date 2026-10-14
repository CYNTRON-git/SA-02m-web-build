# -*- coding: utf-8 -*-
"""MTDx62-MB recognition predicate, flasher side.

The same rule the MQTT bus scan applies (opt/sa02m-modbus-mqtt/mqtt_bus_scan.py
`mtdx62_match` plus the input 0 / input 3 check in `detect_type`): holding 7..9
read back as the polled slave id, a baud from the sensor's list and parity
0..2; input 0 is presence 0/1 and input 3 the self-test code 0..6.

The two trees deploy separately (module_profiles: the flasher package and the
bridge share no import path), so this is a second implementation, not a copy.
They are held equal by BEHAVIOUR: opt/sa02m-modbus-mqtt/tests/
test_mtd_link_parity.py loads this file by path and feeds both sides the same
register banks. ZERO imports, on purpose — that test runs in the bridge suite
and must be able to load this file without the sa02m_flasher package.
"""

# The sensor's baud register holds the rate literally (9600 = 0x2580); it
# cannot talk faster than 57600.
MTD_BAUDS = (1200, 2400, 4800, 9600, 19200, 38400, 57600)
MTD_MAX_BAUD = 57600
# Holding 9: 0 = none, 1 = odd, 2 = even.
MTD_PARITY_CODES = (0, 1, 2)
PRESENCE_VALUES = (0, 1)
DEVICE_STATUS_MAX = 6


def link_triple_match(ident, addr):
    """Holding 7..9 of an MTDx62-MB: own slave id, a listed baud, parity 0..2.

    `ident` is the word list read from holding 7 (three words), or None for a
    failed read.
    """
    if not ident or len(ident) < 3:
        return False
    slave, baud, parity = int(ident[0]), int(ident[1]), int(ident[2])
    return slave == int(addr) and baud in MTD_BAUDS and parity in MTD_PARITY_CODES


def inputs_match(presence, status):
    """Input 0 (presence) is 0/1 and input 3 (self-test code) is 0..6.

    Each argument is the word list of a one-register read, or None.
    """
    if not presence or presence[0] not in PRESENCE_VALUES:
        return False
    if not status:
        return False
    return 0 <= status[0] <= DEVICE_STATUS_MAX
