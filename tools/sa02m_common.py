# -*- coding: utf-8 -*-
"""Общие константы и хелперы для dev/test-скриптов SA-02m."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

HOST = os.environ.get("SA02M_HOST", "192.168.1.136")
USER = os.environ.get("SA02M_USER", "root")
PASSWORD = os.environ.get("SA02M_PASS", "cyntron")
WEB_COOKIE = "session_token=cyntron_session"
WEB_BASE = "http://127.0.0.1:9999"
FLASHER_SOCK = "/run/sa02m-flasher/flasher.sock"


def ssh_exec(command: str, *, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools/ssh/sa02m_remote.py"), "exec", command],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=timeout,
    )


def ssh_run(command: str, *, timeout: float = 120.0) -> str:
    """Remote command via sa02m_remote.py; returns combined stdout+stderr."""
    result = ssh_exec(command, timeout=timeout)
    return ((result.stdout or "") + (result.stderr or "")).strip()


def connect_ssh():
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        HOST,
        username=USER,
        password=PASSWORD,
        timeout=20,
        allow_agent=False,
        look_for_keys=False,
    )
    return client
