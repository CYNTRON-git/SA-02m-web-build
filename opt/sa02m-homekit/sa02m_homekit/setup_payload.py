"""The HomeKit setup code, setup id, X-HM:// setup URI and its QR matrix.

The code is drawn from `random.SystemRandom` and re-drawn while it is one of
the codes the HAP specification forbids (constants.FORBIDDEN_SETUP_CODES).
The QR matrix is computed here with `segno` (venv-only, pure Python) and
handed to the browser as rows of 0/1 — the served page draws `<rect>`s and
carries no QR library (web-code-rigor "No new runtime dependencies").
"""

from __future__ import annotations

import random
from typing import List, Optional

from . import constants as C

_rng = random.SystemRandom()
_BASE36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def generate_setup_code(rng: Optional[random.Random] = None) -> str:
    source = rng or _rng
    while True:
        digits = [source.randint(0, 9) for _ in range(8)]
        code = "%d%d%d-%d%d-%d%d%d" % tuple(digits)
        if code not in C.FORBIDDEN_SETUP_CODES:
            return code


def generate_setup_id(rng: Optional[random.Random] = None) -> str:
    source = rng or _rng
    return "".join(source.choice(C.SETUP_ID_ALPHABET) for _ in range(4))


def _base36(value: int) -> str:
    if value == 0:
        return "0"
    out = []
    while value:
        value, rem = divmod(value, 36)
        out.append(_BASE36[rem])
    return "".join(reversed(out))


def xhm_uri(code: str, setup_id: str, category: int = C.CATEGORY_BRIDGE) -> str:
    """The HAP setup payload URI (IP transport).

    Layout, most significant first: version (3 bits, 0), reserved (4, 0),
    category (8), flags (4; 2 = IP), setup code (27). Base36, upper-case,
    left-padded to 9 characters, then the 4-character setup id. Same layout
    HAP-python's `Accessory.xhm_uri` builds (it needs the optional `base36`
    package, which we do not ship).
    """
    payload = 0
    payload = (payload << 4) | 0          # reserved (version 0 occupies the top bits)
    payload = (payload << 8) | (category & 0xFF)
    payload = (payload << 4) | 2          # flags: IP
    payload = (payload << 27) | (int(code.replace("-", ""), 10) & 0x7FFFFFF)
    return "X-HM://" + _base36(payload).rjust(9, "0") + setup_id


def qr_matrix(uri: str) -> Optional[List[str]]:
    """QR modules as strings of '0'/'1' (no quiet zone), or None without segno."""
    try:
        import segno  # type: ignore
    except ImportError:
        return None
    qr = segno.make(uri, error="m", micro=False)
    return ["".join("1" if cell else "0" for cell in row) for row in qr.matrix]
