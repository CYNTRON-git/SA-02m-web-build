#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Подготовка файлов .fw для публикации на сайте и заполнения index.json.

Читает каждый .fw (сигнатура и размер из заголовка, версия — из имени файла или из образа),
предлагает каноническое имя для репозитория sa02m-flasher / firmware_repo.py:

  MR-02m_<X.Y.Z.W>.fw

или с суффиксом модуля (чтобы различать варианты на одной версии):

  MR-02m_<slug>_<X.Y.Z.W>.fw

где slug — упрощённая сигнатура (например MR-02m-DI16 → MR-02m_DI16).

Примеры:
  python scripts/prepare_firmware_for_site.py --scan "C:/Users/admin/Downloads/MR-02m/build/AppBoot" --dry-run
  python scripts/prepare_firmware_for_site.py --scan ./build/AppBoot --rename --out-json ./index.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Корень пакета: .../opt/sa02m-flasher
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sa02m_flasher.firmware import load_fw  # noqa: E402


def _normalize_version(ver: str) -> str:
    """Привести к X.Y.Z.W (как в firmware.parse_version_from_filename для 1–4 компонентов)."""
    ver = (ver or "").strip()
    if not ver or ver == "?":
        return "0.0.0.0"
    parts = [p for p in ver.split(".") if p.isdigit()]
    if not parts:
        return "0.0.0.0"
    while len(parts) < 4:
        parts.append("0")
    return ".".join(parts[:4])


def _slug_from_signature(sig: str) -> str:
    s = (sig or "NONE").strip()
    s = re.sub(r"[^A-Za-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "UNKNOWN"


def _target_name(version: str, signature: str, *, include_slug: bool) -> str:
    v = _normalize_version(version)
    if include_slug:
        return f"MR-02m_{_slug_from_signature(signature)}_{v}.fw"
    return f"MR-02m_{v}.fw"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _process_one(path: Path, *, include_slug: bool) -> Tuple[str, Dict[str, Any]]:
    _data, _size, version, signature = load_fw(path)
    target = _target_name(version, signature, include_slug=include_slug)
    sha = _sha256_file(path)
    entry = {
        "file": target,
        "version": _normalize_version(version),
        # Один образ на всю линейку MR-02м — в index.json не привязываем к сигнатуре варианта.
        "signatures": [],
        "device": f"MR-02m ({signature})" if signature != "NONE" else "MR-02m",
        "size": path.stat().st_size,
        "sha256": sha,
        "released": "",
        "notes": f"renamed from {path.name}",
    }
    return target, entry


def main() -> int:
    ap = argparse.ArgumentParser(description="Имена .fw и черновик index.json для cyntron.ru")
    ap.add_argument("--scan", type=Path, required=True, help="Каталог с .fw (рекурсивно не ищем — только этот каталог)")
    ap.add_argument("--include-signature", action="store_true", help="Имя MR-02m_<sig>_<ver>.fw (иначе только MR-02m_<ver>.fw)")
    ap.add_argument("--dry-run", action="store_true", help="Только вывести план, не переименовывать")
    ap.add_argument("--rename", action="store_true", help="Переименовать файлы в каталоге --scan")
    ap.add_argument("--out-json", type=Path, help="Записать index.json (schema v1, channel stable)")
    args = ap.parse_args()
    scan: Path = args.scan
    if not scan.is_dir():
        print(f"Не каталог: {scan}", file=sys.stderr)
        return 2

    fw_files = sorted(scan.glob("*.fw"))
    if not fw_files:
        print(f"В {scan} нет *.fw", file=sys.stderr)
        return 1

    manifest_entries: List[Dict[str, Any]] = []
    for src in fw_files:
        try:
            target, entry = _process_one(src, include_slug=args.include_signature)
        except Exception as exc:
            print(f"SKIP {src.name}: {exc}", file=sys.stderr)
            continue
        print(f"{src.name}  ->  {entry['file']}  sha256={entry['sha256'][:16]}…")
        manifest_entries.append(entry)
        dest = scan / entry["file"]
        if src.name == entry["file"]:
            continue
        if args.rename:
            if dest.exists() and dest.resolve() != src.resolve():
                print(f"  ERR: цель уже существует: {dest.name}", file=sys.stderr)
                continue
            if not args.dry_run:
                src.rename(dest)
        elif args.dry_run:
            print(f"  (dry-run) rename -> {dest.name}")

    if args.out_json:
        doc = {
            "schema": 1,
            "updated": "",
            "channels": {"stable": manifest_entries},
        }
        text = json.dumps(doc, ensure_ascii=False, indent=2)
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(text, encoding="utf-8")
        print(f"Wrote {args.out_json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
