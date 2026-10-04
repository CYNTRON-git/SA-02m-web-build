# -*- coding: utf-8 -*-
"""Optional model-name normalization from the shared device library.

Source: https://github.com/CYNTRON-git/devices.git, generated/devices_tables.py
(ALIASES: alias or model name -> canonical count-first model, e.g.
AO6AI6 -> 6AI6AO). scripts/14-cyntron-devices.sh copies that file beside this
module (and beside the MQTT bridge); the packages never import each other.

The roster sources already report canonical names for known types (the bridge
from MR02M_TYPE_NAMES, the flasher from MP02_TYPE_NAMES); the alias matters for
the flasher's signature fallback (an unresolved letter-first EEPROM signature
such as AO6AI6). Without the table — or for a name the table does not know —
the input is returned unchanged, so /run/sa02m-rs485-roster.json keeps the
exact model strings the sources wrote (docs/contracts/rs485-roster.md).

python3.6-safe like the rest of this package (no f-strings, no annotations).
"""


def _aliases():
    try:
        import devices_tables  # sibling copy; not a cross-package import
    except ImportError:
        return {}
    aliases = getattr(devices_tables, "ALIASES", None)
    return aliases if isinstance(aliases, dict) else {}


def canonical_model(name):
    """Canonical model for `name`; `name` itself when unknown; "" for None."""
    if name is None:
        return ""
    text = str(name)
    if not text:
        return text
    aliases = _aliases()
    if not aliases:
        return text
    hit = aliases.get(text)
    if isinstance(hit, str) and hit:
        return hit
    upper = text.upper()
    for key, val in aliases.items():
        if isinstance(key, str) and isinstance(val, str) and val and key.upper() == upper:
            return val
    return text
