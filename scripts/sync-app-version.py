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
  - www/network_config/index.html, login.html  (all ?v= cache-bust query strings)
  - www/network_config/static/js/**/*.js  (?v= inside ES-module import/export
    specifiers — the browser fetches those itself, so index.html's ?v= cannot
    bust them; docs/decisions/es-modules.md П2)
  - README.md  (shields.io version badge)
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
JS_DIR = WWW / "static" / "js"
INDEX_HTML = WWW / "index.html"
LOGIN_HTML = WWW / "login.html"
README_MD = REPO_ROOT / "README.md"

VERSION_RE = re.compile(r"^\d+(\.\d+){2,3}$")
CACHE_BUST_RE = re.compile(r"(\?v=)(\d+(?:\.\d+){2,3}(?:[-.]\d+)?)")
# shields.io badge: .../badge/version-<X.Y.Z[.W]>-cyan?...
README_BADGE_RE = re.compile(r"(badge/version-)(\d+(?:\.\d+){2,3})(-cyan)")
# ES-module import specifier carrying a cache-bust: the quoted string right after
# `from` (static import/export ... from), a bare `import '...'`, or a dynamic
# `import('...')`. Scoped to import syntax on purpose — the same `?v=` token
# elsewhere in a bundle (flasher.js builds one at runtime from a variable) is
# not a specifier and stays untouched. Groups: lead / q / pre / ver / post.
IMPORT_SPEC_RE = re.compile(
    r"""(?P<lead>\b(?:from\s*|import\s*\(?\s*))(?P<q>['"])"""
    r"""(?P<pre>[^'"\n]*?\?v=)(?P<ver>\d+(?:\.\d+){2,3}(?:[-.]\d+)?)(?P<post>[^'"\n]*)(?P=q)"""
)


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


def patch_html_cache_bust(path: Path, version: str) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    new = CACHE_BUST_RE.sub(rf"\g<1>{version}", text)
    if new == text:
        return False
    path.write_text(new, encoding="utf-8")
    return True


def html_cache_bust_mismatches(path: Path, version: str) -> list[str]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    bad: list[str] = []
    for m in CACHE_BUST_RE.finditer(text):
        if m.group(2) != version:
            bad.append(f"{path.name}: ?v={m.group(2)!r} (expected {version!r})")
    return bad


def js_module_files(js_dir: Path | None = None) -> list[Path]:
    """Every served *.js under static/js (recursive, sorted) — the files whose
    import specifiers may carry a ?v= cache-bust."""
    root = JS_DIR if js_dir is None else js_dir
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.js") if p.is_file())


def patch_js_import_specs(version: str, js_dir: Path | None = None) -> bool:
    """Rewrite ?v=<old> → ?v=<version> inside import/export specifiers of every
    served JS file. Returns True when at least one file changed. A file with no
    versioned specifier is never rewritten (byte-identical)."""
    changed = False
    for path in js_module_files(js_dir):
        text = path.read_text(encoding="utf-8")
        new = IMPORT_SPEC_RE.sub(rf"\g<lead>\g<q>\g<pre>{version}\g<post>\g<q>", text)
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed = True
    return changed


def js_import_spec_mismatches(version: str, js_dir: Path | None = None) -> list[str]:
    root = JS_DIR if js_dir is None else js_dir
    bad: list[str] = []
    for path in js_module_files(js_dir):
        text = path.read_text(encoding="utf-8")
        for m in IMPORT_SPEC_RE.finditer(text):
            if m.group("ver") != version:
                rel = path.relative_to(root).as_posix()
                bad.append(f"static/js/{rel}: import ?v={m.group('ver')!r} (expected {version!r})")
    return bad


def patch_readme_badge(version: str) -> bool:
    if not README_MD.is_file():
        return False
    text = README_MD.read_text(encoding="utf-8")
    new = README_BADGE_RE.sub(rf"\g<1>{version}\g<3>", text)
    if new == text:
        return False
    README_MD.write_text(new, encoding="utf-8")
    return True


def readme_badge_mismatches(version: str) -> list[str]:
    if not README_MD.is_file():
        return []
    text = README_MD.read_text(encoding="utf-8")
    bad: list[str] = []
    for m in README_BADGE_RE.finditer(text):
        if m.group(2) != version:
            bad.append(f"README.md: version badge {m.group(2)!r} (expected {version!r})")
    return bad


def current_app_version() -> str | None:
    if not APP_JS.is_file():
        return None
    m = re.search(r"^const APP_VERSION = '([^']*)';", APP_JS.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync SA-02m web APP_VERSION with git branch")
    parser.add_argument("--check", action="store_true", help="Only verify sync; exit 1 on mismatch")
    args = parser.parse_args(argv)

    version = resolve_version()
    cur = current_app_version()

    if args.check:
        mismatches: list[str] = []
        if cur != version:
            mismatches.append(f"app.js APP_VERSION={cur!r}, expected {version!r}")
        for html in (INDEX_HTML, LOGIN_HTML):
            mismatches.extend(html_cache_bust_mismatches(html, version))
        mismatches.extend(js_import_spec_mismatches(version))
        mismatches.extend(readme_badge_mismatches(version))
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
    changed = patch_app_js(version)
    for html in (INDEX_HTML, LOGIN_HTML):
        changed |= patch_html_cache_bust(html, version)
    changed |= patch_js_import_specs(version)
    changed |= patch_readme_badge(version)
    if changed:
        print(f"Synced web version to {version}")
    else:
        print(f"Already up to date: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
