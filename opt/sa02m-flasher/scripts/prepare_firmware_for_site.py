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

Для бутлоадера (имя исходника содержит ``bootloader`` / ``MR-02m_bootloader``):

  MR-02m_bootloader_<X.Y.Z.W>.fw

и в ``index.json`` добавляется ``"kind": "bootloader"`` (у приложения — ``app``).

Примеры:
  python scripts/prepare_firmware_for_site.py --scan "C:/Users/admin/Downloads/MR-02m/build/AppBoot" --dry-run
  python scripts/prepare_firmware_for_site.py --scan ./build/AppBoot --rename --out-json ./index.json

Пакет для загрузки на сайт (копии с каноническими именами + index.json в одном каталоге,
исходный ``--scan`` не переименовывается):

  python scripts/prepare_firmware_for_site.py --scan ./build/AppBoot --bundle-dir ./firmware-site-export

Схема сайта и выгрузки: в репозитории шаблон
``firmware-site-export/SITE_AND_FIRMWARE_UPLOAD.md.example``; рабочая памятка
``firmware-site-export/SITE_AND_FIRMWARE_UPLOAD.md`` — локально (в git не коммитится).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Корень пакета: .../opt/sa02m-flasher
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sa02m_flasher.firmware import load_fw  # noqa: E402
from sa02m_flasher.firmware_repo import _infer_kind_from_filename  # noqa: E402


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


def _target_name(version: str, signature: str, *, include_slug: bool, kind: str) -> str:
    v = _normalize_version(version)
    if kind == "bootloader":
        return f"MR-02m_bootloader_{v}.fw"
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
    kind = _infer_kind_from_filename(path.name)
    _data, _size, version, signature = load_fw(path)
    target = _target_name(version, signature, include_slug=include_slug, kind=kind)
    sha = _sha256_file(path)
    entry: Dict[str, Any] = {
        "file": target,
        "version": _normalize_version(version),
        "kind": kind,
        # Один образ на всю линейку MR-02м — в index.json не привязываем к сигнатуре варианта.
        "signatures": [],
        "device": f"MR-02m ({signature})" if signature != "NONE" else "MR-02m",
        "size": path.stat().st_size,
        "sha256": sha,
        "released": "",
        "notes": f"renamed from {path.name}",
    }
    return target, entry


def _write_index_json(path: Path, entries: List[Dict[str, Any]], *, updated: str) -> None:
    doc = {
        "schema": 1,
        "updated": updated,
        "channels": {"stable": entries},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Имена .fw и черновик index.json для cyntron.ru")
    ap.add_argument("--scan", type=Path, required=True, help="Каталог с .fw (рекурсивно не ищем — только этот каталог)")
    ap.add_argument("--include-signature", action="store_true", help="Имя MR-02m_<sig>_<ver>.fw (иначе только MR-02m_<ver>.fw)")
    ap.add_argument("--dry-run", action="store_true", help="Только вывести план, не переименовывать")
    ap.add_argument("--rename", action="store_true", help="Переименовать файлы в каталоге --scan")
    ap.add_argument("--out-json", type=Path, help="Записать index.json (schema v1, channel stable)")
    ap.add_argument(
        "--bundle-dir",
        type=Path,
        help="Каталог выгрузки: скопировать туда переименованные .fw и записать index.json (исходники в --scan не трогать)",
    )
    args = ap.parse_args()
    scan: Path = args.scan
    if not scan.is_dir():
        print(f"Не каталог: {scan}", file=sys.stderr)
        return 2

    # Канонические имена из Makefile MR-02m: MR-02m_<ver>.fw, MR-02m_bootloader_<ver>.fw —
    # не тащить в пакет boot_fw.fw / mp-02m.fw (дубликаты и другая схема имён).
    fw_files = sorted(scan.glob("MR-02m*.fw"))
    if not fw_files:
        fw_files = sorted(scan.glob("*.fw"))
    if not fw_files:
        print(f"В {scan} нет *.fw", file=sys.stderr)
        return 1

    manifest_entries: List[Dict[str, Any]] = []
    bundle_dir: Optional[Path] = args.bundle_dir
    updated = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for src in fw_files:
        try:
            target, entry = _process_one(src, include_slug=args.include_signature)
        except Exception as exc:
            print(f"SKIP {src.name}: {exc}", file=sys.stderr)
            continue
        print(f"{src.name}  ->  {entry['file']}  sha256={entry['sha256'][:16]}…")

        if bundle_dir is not None and not args.dry_run:
            bundle_dir.mkdir(parents=True, exist_ok=True)
            out_fw = bundle_dir / entry["file"]
            shutil.copy2(src, out_fw)
            entry["sha256"] = _sha256_file(out_fw)
            entry["size"] = out_fw.stat().st_size
            entry["notes"] = f"from {src.name}"
            print(f"  -> copied to {out_fw}")

        manifest_entries.append(entry)
        dest = scan / entry["file"]
        if bundle_dir is not None:
            continue
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

    if bundle_dir is not None and args.dry_run:
        print("(dry-run) --bundle-dir: копирование пропущено, index.json не записан", file=sys.stderr)
        return 0

    if bundle_dir is not None:
        idx_path = bundle_dir / "index.json"
        _write_index_json(idx_path, manifest_entries, updated=updated)
        print(f"Wrote {idx_path} ({len(manifest_entries)} entries, updated={updated})")

    if args.out_json:
        _write_index_json(args.out_json, manifest_entries, updated=updated)
        print(f"Wrote {args.out_json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
