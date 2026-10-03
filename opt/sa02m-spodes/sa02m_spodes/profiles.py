"""Load profile and event journal (class 7), off the live poll.

Selective access is a range on the clock (0.0.1.0.0.255, class 8,
attribute 2). The exchange timeout is capped at PROFILE_BUDGET_S so a
profile read cannot push the next live poll by more than that budget.
An empty window is an empty list, not an error.
"""

from __future__ import annotations

from datetime import datetime

from sa02m_spodes.axdr import (
    TAG_ARRAY,
    TAG_DATETIME,
    TAG_OCTET_STRING,
    TAG_STRUCTURE,
    AxdrError,
    Data,
    array,
    as_int,
    encode,
    octet,
    structure,
    u16,
    u8,
)
from sa02m_spodes.obis_spodes import (
    CLASS_PROFILE,
    CLOCK_OBIS,
    EVENT_LOG_OBIS,
    LOAD_PROFILE_OBIS,
    obis_bytes,
)

PROFILE_BUDGET_S = 2.0


def pack_datetime(dt: datetime) -> bytes:
    """COSEM date-time, 12 bytes, deviation 'not specified'."""
    return (
        int(dt.year).to_bytes(2, "big")
        + bytes((dt.month, dt.day, 0xFF, dt.hour, dt.minute, dt.second, 0))
        + (0x8000).to_bytes(2, "big")
        + bytes((0,))
    )


def unpack_datetime(raw: bytes) -> datetime | None:
    if len(raw) != 12 or raw[0:2] == b"\xff\xff":
        return None
    year = int.from_bytes(raw[0:2], "big")
    month, day = raw[2], raw[3]
    hour, minute, second = raw[5], raw[6], raw[7]
    if month in (0, 0xFF) or day in (0, 0xFF) or hour == 0xFF:
        return None
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def range_access(start: datetime, end: datetime) -> bytes:
    """Selector 1 (range). encode_get adds the 'access selection present' byte."""
    clock = structure([
        u16(8),
        octet(bytes(CLOCK_OBIS)),
        u8(2),
        u16(0),
    ])
    window = structure([
        clock,
        Data(TAG_DATETIME, pack_datetime(start)),
        Data(TAG_DATETIME, pack_datetime(end)),
        array([]),
    ])
    return bytes((0x01,)) + encode(window)


def _field_value(field: Data):
    if field.tag == TAG_DATETIME:
        return unpack_datetime(bytes(field.value))
    if field.tag == TAG_OCTET_STRING and len(field.value) == 12:
        stamp = unpack_datetime(bytes(field.value))
        if stamp is not None:
            return stamp
    try:
        return as_int(field)
    except AxdrError:
        return field.value


def rows_from_buffer(data: Data) -> list[dict]:
    """Profile buffer (attribute 2) → ``[{timestamp, values}, …]``."""
    if data.tag != TAG_ARRAY or not data.value:
        return []
    rows = []
    for item in data.value:
        if item.tag != TAG_STRUCTURE or not isinstance(item.value, list):
            continue
        stamp = None
        values = []
        for field in item.value:
            got = _field_value(field)
            if stamp is None and isinstance(got, datetime):
                stamp = got
            else:
                values.append(got)
        rows.append({"timestamp": stamp, "values": values})
    return rows


def _budget(timeout_s: float | None) -> float:
    if timeout_s is None or timeout_s > PROFILE_BUDGET_S:
        return PROFILE_BUDGET_S
    return float(timeout_s)


def read_profile(session, obis, start: datetime, end: datetime,
                 timeout_s: float | None = None) -> list[dict]:
    limit = _budget(timeout_s)
    data = session.get(
        CLASS_PROFILE, obis_bytes(obis), 2,
        access=range_access(start, end), timeout_s=limit)
    return rows_from_buffer(data)


def read_load_profile(session, start: datetime, end: datetime,
                      timeout_s: float | None = None) -> list[dict]:
    return read_profile(session, LOAD_PROFILE_OBIS, start, end, timeout_s)


def read_event_log(session, start: datetime, end: datetime,
                   timeout_s: float | None = None) -> list[dict]:
    return read_profile(session, EVENT_LOG_OBIS, start, end, timeout_s)
