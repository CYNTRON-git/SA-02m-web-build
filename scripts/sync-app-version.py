#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Синхронизирует версию веб-интерфейса с git-веткой (или www/network_config/VERSION).

Использование (из корня репозитория):
  py -3 scripts/sync-app-version.py
  py -3 scripts/sync-app-version.py --check   # exit 1 если рассинхрон

Обновляет:
  - www/network_config/VERSION
  - www/network_config/static/js/app.js  (APP_VERSION)
  - www/network_config/index.html      (cache-bust ?v= для app.js)
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WWW = REPO_ROOT / "www" / "network_config"
VERSION_FILE = WWW / "VERSION"
APP_JS = WWW / "static" / "js" / "app.js"
INDEX_HTML = WWW / "index.html"

VERSION_RE = re.compile(r"^\d+(\.\d+){2,3}$")


def read_version_file() -> str | None:
    if not VERSION_FILE.is_file():
        return None
    for line in VERSION_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if VERSION_RE.match(line):
            return line
    return None


def git_branch() -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "branch", "--show-current"],
            cwd=REPO_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    branch = out.strip()
    return branch if VERSION_RE.match(branch) else None


def resolve_version() -> str:
    branch = git_branch()
    if branch:
        return branch
    from_file = read_version_file()
    if from_file:
        return from_file
    raise SystemExit("Не удалось определить версию: нет git-ветки M.M.P[.S] и нет www/network_config/VERSION")


def write_version_file(version: str) -> None:
    text = VERSION_FILE.read_text(encoding="utf-8") if VERSION_FILE.is_file() else ""
    lines = text.splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        if not replaced and line.strip() and not line.strip().startswith("#") and VERSION_RE.match(line.strip()):
            out.append(version)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(version)
    VERSION_FILE.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def patch_app_js(version: str) -> bool:
    text = APP_JS.read_text(encoding="utf-8")
    new, n = re.subn(
        r"(?m)^const APP_VERSION = '[^']*';",
        f"const APP_VERSION = '{version}';",
        text,
        count=1,
    )
    if n != 1:
        raise SystemExit(f"APP_VERSION не найден в {APP_JS}")
    if new == text:
        return False
    APP_JS.write_text(new, encoding="utf-8")
    return True


def patch_index_html(version: str) -> bool:
    text = INDEX_HTML.read_text(encoding="utf-8")
    new, n = re.subn(
        r'(<script src="/static/js/app\.js\?v=)[^"]+(">)',
        rf"\g<1>{version}\g<2>",
        text,
        count=1,
    )
    if n != 1:
        raise SystemExit(f"app.js cache-bust не найден в {INDEX_HTML}")
    if new == text:
        return False
    INDEX_HTML.write_text(new, encoding="utf-8")
    return True


def current_app_version() -> str | None:
    if not APP_JS.is_file():
        return None
    m = re.search(r"^const APP_VERSION = '([^']*)';", APP_JS.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync SA-02m web APP_VERSION with git branch")
    parser.add_argument("--check", action="store_true", help="Only verify sync; exit 1 on mismatch")
    args = parser.parse_args()

    version = resolve_version()
    cur = current_app_version()

    if args.check:
        mismatches: list[str] = []
        if cur != version:
            mismatches.append(f"app.js APP_VERSION={cur!r}, expected {version!r}")
        idx = INDEX_HTML.read_text(encoding="utf-8")
        if f'app.js?v={version}"' not in idx:
            mismatches.append(f"index.html app.js?v= does not match {version}")
        vf = read_version_file()
        if vf != version:
            mismatches.append(f"VERSION file={vf!r}, expected {version!r}")
        if mismatches:
            for msg in mismatches:
                print(msg, file=sys.stderr)
            return 1
        print(f"OK: web version {version}")
        return 0

    write_version_file(version)
    changed = patch_app_js(version) | patch_index_html(version)
    if changed:
        print(f"Synced web version to {version}")
    else:
        print(f"Already up to date: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
