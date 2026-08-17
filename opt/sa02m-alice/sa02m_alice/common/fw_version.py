"""Device firmware version + hardware variant reported to the Alice gateway.

Single home for the two identity facts the alice-client puts on the mTLS
connect handshake (X-FW-Version / X-HW-Variant). Both MIRROR the cloud-agent's
implementation so the cloud dashboard sees the same values from either agent:

  - get_fw_version(): sa02m-cloud-agent.py:166 (same parse rules, same
    "unknown" fallback).
  - HW_VARIANT:       sa02m-cloud-agent.py:54 ("sa02m-1eth").

Keep these two in sync with the cloud-agent constants — a divergence makes the
dashboard footer disagree between the two reporting paths.
"""

from __future__ import annotations

# Web version file — the deployed overlay's single version home
# (www/network_config/VERSION → /var/www/network_config/VERSION on the device).
VERSION_FILE = "/var/www/network_config/VERSION"

# Source of truth for the hardware variant is the cloud-agent constant
# (sa02m-cloud-agent.py:54). Mirrored here; both currently report "sa02m-1eth".
HW_VARIANT = "sa02m-1eth"


def get_fw_version(path: str = VERSION_FILE) -> str:
    """First non-comment, non-blank line of the web VERSION file.

    Guarded → "unknown" on any failure (missing/unreadable file, no usable
    line), matching the cloud-agent's fallback so both agents report the same
    string. The `path` argument exists for tests; production uses the default.
    """
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except Exception:
        pass
    return "unknown"
