"""Mercury / SPODES association, keys, and the live register read.

A reader or configurator without a complete key set stays on the public
client. No empty ciphering is sent. A missing OBIS (data-access error)
is skipped; a transport or session error still fails the cycle.
"""

from __future__ import annotations

from sa02m_spodes.axdr import as_int
from sa02m_spodes.obis_spodes import (
    CLASS_REGISTER,
    ENERGY,
    POWER,
    SAP,
    SINGLE_PHASE_VOLTAGE,
    obis_bytes,
    parse_scaler_unit,
    physical,
)
from sa02m_spodes.security import Secret, SecurityContext, SecurityError
from sa02m_spodes.xdlms import CosemAccessError

_SECRET_KEYS = ("password", "encryption_key", "authentication_key")


def _text(value) -> str:
    if isinstance(value, Secret):
        value = value.reveal()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("latin1")
    return "" if value is None else str(value).strip()


def parse_key(value) -> bytes:
    """16-byte key: 32 hex digits, or 16 latin-1 characters. Empty → b''."""
    if isinstance(value, Secret):
        value = value.reveal()
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        return raw if len(raw) == 16 else b""
    text = _text(value)
    if not text:
        return b""
    if len(text) == 32 and all(c in "0123456789abcdefABCDEF" for c in text):
        return bytes.fromhex(text)
    raw = text.encode("latin1")
    return raw if len(raw) == 16 else b""


def parse_system_title(value) -> bytes:
    """8-byte system title: 16 hex digits, or 8 latin-1 characters."""
    if isinstance(value, Secret):
        value = value.reveal()
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        return raw if len(raw) == 8 else b""
    text = _text(value)
    if not text:
        return b""
    if len(text) == 16 and all(c in "0123456789abcdefABCDEF" for c in text):
        return bytes.fromhex(text)
    raw = text.encode("latin1")
    return raw if len(raw) == 8 else b""


def named_role(cfg: dict) -> str:
    role = str(cfg.get("association") or "public").strip().lower()
    return role if role in SAP else "public"


def keys_complete(cfg: dict) -> bool:
    return bool(parse_key(cfg.get("encryption_key"))
                and parse_key(cfg.get("authentication_key"))
                and parse_system_title(cfg.get("system_title")))


def effective_role(cfg: dict) -> str:
    """Reader/configurator without keys does not leave the public client."""
    role = named_role(cfg)
    if role == "public" or not keys_complete(cfg):
        return "public"
    return role


def client_sap_for(cfg: dict) -> int:
    return SAP[effective_role(cfg)]


def security_for(cfg: dict):
    """Suite-0 context, or None when the poller must stay in the clear."""
    if effective_role(cfg) == "public":
        return None
    try:
        return SecurityContext(
            parse_key(cfg.get("encryption_key")),
            parse_key(cfg.get("authentication_key")),
            parse_system_title(cfg.get("system_title")),
        )
    except (SecurityError, ImportError):
        return None


def password_for(cfg: dict) -> bytes | None:
    if effective_role(cfg) == "public":
        return None
    raw = _text(cfg.get("password"))
    if not raw:
        return None
    return raw.encode("latin1")


def seal_cfg(cfg: dict) -> dict:
    """Shallow copy whose password and keys do not appear in repr."""
    out = dict(cfg)
    for key in _SECRET_KEYS:
        if out.get(key) not in (None, ""):
            out[key] = Secret(out[key])
    return out


def format_magnitude(mag: float) -> str:
    if mag == int(mag) and abs(mag) < 1e15:
        return str(int(mag))
    text = "%.3f" % mag
    return text.rstrip("0").rstrip(".")


def _one(session, code, scaler_cache) -> tuple[float, str] | None:
    raw = session.get(CLASS_REGISTER, obis_bytes(code), 2)
    if code not in scaler_cache:
        scaler_cache[code] = parse_scaler_unit(
            session.get(CLASS_REGISTER, obis_bytes(code), 3))
    scaler, unit = scaler_cache[code]
    return physical(as_int(raw), scaler, unit)


def read_named(session, catalog: dict, scaler_cache: dict) -> dict:
    """``{mqtt name: (magnitude, unit)}``. A missing object is omitted."""
    out = {}
    for name, code in catalog.items():
        try:
            got = _one(session, code, scaler_cache)
        except CosemAccessError:
            continue
        if got is not None:
            out[name] = got
    return out


def read_power(session, scaler_cache: dict) -> dict:
    out = read_named(session, POWER, scaler_cache)
    if "voltage_a" not in out:
        try:
            got = _one(session, SINGLE_PHASE_VOLTAGE, scaler_cache)
        except CosemAccessError:
            got = None
        if got is not None:
            out["voltage_a"] = got
    return out


def read_energy(session, scaler_cache: dict) -> dict:
    return read_named(session, ENERGY, scaler_cache)
