#!/usr/bin/python3
"""Isolated administrative entry point; imports only its installed package."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sa02m_devices.event_journal_startup import main

if __name__ == "__main__":
    raise SystemExit(main())
