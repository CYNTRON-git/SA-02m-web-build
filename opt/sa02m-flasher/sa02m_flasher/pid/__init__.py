# -*- coding: utf-8 -*-
"""PID auto-tune core for Carel AHU controllers, hosted by the flasher daemon.

Ported from the desktop tuner (MR-02m-flasher, branch `carel`, `pid_tuner/`).
Stdlib only — the Nelder-Mead fit is hand-written so the board needs no numpy.
The desktop's `gui/`, `cli.py`, `report.py` and `robustness.py` stayed behind:
the web page is the UI here.

Two seams differ from the desktop and nothing else does:

* `transport` drives the bus through `sa02m_flasher.modbus_io` instead of the
  desktop's `flasher_windows.modbus_io`, and never opens a port of its own —
  the daemon owns the COM lease and hands in a `send_rtu`.
* `profile` reads every address `sa02m_carel` already owns through
  `module_profiles.carel_ahu()`; only the PID registers the shared map does not
  name (Xp, Ti, regulation type, frost setpoint, valve feedback) live here.

Register-map semantics: `docs/contracts/carel-ahu.md`.
"""

__all__ = [
    "analysis",
    "experiments",
    "ident",
    "logger",
    "profile",
    "sim",
    "supervisor",
    "transport",
    "tuning",
]
