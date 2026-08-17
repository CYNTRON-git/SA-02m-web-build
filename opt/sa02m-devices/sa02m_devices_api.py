#!/usr/bin/env python3
"""Entry point: python3 /opt/sa02m-devices/sa02m_devices_api.py"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sa02m_devices.api import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
