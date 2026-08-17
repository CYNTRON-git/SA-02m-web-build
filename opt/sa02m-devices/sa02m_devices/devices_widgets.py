"""Конфиг виджетов ДТВ/СЭ на вкладке «Устройства».

Удалённый виджет скрывается из UI и не пишется в архив SQLite;
исторические строки в БД не трогаем. Повторное добавление — из списка
ранее удалённых (и/или снова появившихся в MQTT).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(
    os.environ.get(
        "STAND_DEVICES_WIDGETS_PATH",
        "/var/lib/sa02m-stand/devices_widgets.json",
    )
)

_CACHE: dict[str, Any] = {"mtime": None, "path": None, "data": None}


def config_path(path: Path | None = None) -> Path:
    return Path(path) if path is not None else DEFAULT_PATH


def _empty() -> dict[str, Any]:
    return {"version": 1, "removed_ids": [], "catalog": {}}


def load(path: Path | None = None) -> dict[str, Any]:
    p = config_path(path)
    try:
        st = p.stat()
        mtime = st.st_mtime_ns
    except OSError:
        return _empty()
    if (
        _CACHE.get("path") == str(p)
        and _CACHE.get("mtime") == mtime
        and isinstance(_CACHE.get("data"), dict)
    ):
        return dict(_CACHE["data"])
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return _empty()
    if not isinstance(raw, dict):
        return _empty()
    removed = raw.get("removed_ids") or []
    if not isinstance(removed, list):
        removed = []
    catalog = raw.get("catalog") or {}
    if not isinstance(catalog, dict):
        catalog = {}
    data = {
        "version": int(raw.get("version") or 1),
        "removed_ids": [str(x) for x in removed if str(x).strip()],
        "catalog": {
            str(k): v for k, v in catalog.items() if isinstance(v, dict)
        },
    }
    _CACHE.update({"path": str(p), "mtime": mtime, "data": data})
    return dict(data)


def save(cfg: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    p = config_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": int(cfg.get("version") or 1),
        "removed_ids": sorted(
            {str(x).strip() for x in (cfg.get("removed_ids") or []) if str(x).strip()}
        ),
        "catalog": cfg.get("catalog") if isinstance(cfg.get("catalog"), dict) else {},
        "updated_at": time.time(),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix="devices_widgets_", suffix=".json", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _CACHE.update({"path": str(p), "mtime": None, "data": None})
    return load(path=p)


def removed_set(path: Path | None = None) -> set[str]:
    return set(load(path=path).get("removed_ids") or [])


def _catalog_entry(device: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(device.get("id") or ""),
        "kind": "ce" if device.get("kind") == "ce" else "dtv",
        "label": str(device.get("label") or device.get("title") or device.get("id") or ""),
        "sku": str(device.get("sku") or ""),
        "port_num": device.get("port_num"),
        "addr": device.get("addr"),
        "com": device.get("com") or "",
        "removed_at": time.time(),
    }


def remove_widget(
    device_id: str,
    *,
    device: dict[str, Any] | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    did = str(device_id or "").strip()
    if not did:
        return {"ok": False, "error": "device_id пуст"}
    cfg = load(path=path)
    ids = list(cfg.get("removed_ids") or [])
    if did not in ids:
        ids.append(did)
    catalog = dict(cfg.get("catalog") or {})
    if device and isinstance(device, dict):
        catalog[did] = _catalog_entry({**device, "id": did})
    elif did not in catalog:
        catalog[did] = {
            "id": did,
            "kind": "ce" if did.startswith("ce") else "dtv",
            "label": did,
            "removed_at": time.time(),
        }
    cfg["removed_ids"] = ids
    cfg["catalog"] = catalog
    saved = save(cfg, path=path)
    return {"ok": True, "removed_ids": saved["removed_ids"], "id": did}


def add_widget(device_id: str, *, path: Path | None = None) -> dict[str, Any]:
    did = str(device_id or "").strip()
    if not did:
        return {"ok": False, "error": "device_id пуст"}
    cfg = load(path=path)
    ids = [x for x in (cfg.get("removed_ids") or []) if x != did]
    cfg["removed_ids"] = ids
    saved = save(cfg, path=path)
    return {"ok": True, "removed_ids": saved["removed_ids"], "id": did}


def _device_brief(d: dict[str, Any], *, online: bool) -> dict[str, Any]:
    return {
        "id": str(d.get("id") or ""),
        "kind": "ce" if d.get("kind") == "ce" else "dtv",
        "label": str(d.get("label") or d.get("title") or d.get("id") or ""),
        "sku": str(d.get("sku") or ""),
        "port_num": d.get("port_num"),
        "addr": d.get("addr"),
        "com": d.get("com") or "",
        "online": bool(online),
        "ok": bool(d.get("ok")),
        "age_s": d.get("age_s"),
    }


def apply_widgets_view(
    snap: dict[str, Any],
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    """Фильтр для UI: скрыть removed; добавить available для повторного добавления."""
    out = dict(snap or {})
    cfg = load(path=path)
    removed = set(cfg.get("removed_ids") or [])
    catalog = cfg.get("catalog") or {}

    dtv_all = [d for d in (out.get("dtv") or []) if isinstance(d, dict)]
    ce_all = [d for d in (out.get("ce") or []) if isinstance(d, dict)]
    live_by_id = {
        str(d.get("id") or ""): d
        for d in dtv_all + ce_all
        if str(d.get("id") or "")
    }

    out["dtv"] = [d for d in dtv_all if str(d.get("id") or "") not in removed]
    out["ce"] = [d for d in ce_all if str(d.get("id") or "") not in removed]
    out["devices"] = list(out["dtv"]) + list(out["ce"])

    available: list[dict[str, Any]] = []
    seen: set[str] = set()
    for did in sorted(removed):
        if did in live_by_id:
            available.append(_device_brief(live_by_id[did], online=True))
            seen.add(did)
        elif did in catalog:
            available.append(_device_brief(catalog[did], online=False))
            seen.add(did)
        else:
            available.append(
                _device_brief(
                    {"id": did, "kind": "ce" if did.startswith("ce") else "dtv"},
                    online=False,
                )
            )
            seen.add(did)
    out["available"] = available
    out["widgets"] = {
        "removed_ids": list(cfg.get("removed_ids") or []),
        "removed_count": len(removed),
        "available_count": len(available),
        "config_path": str(config_path(path)),
    }
    return out


def filter_for_archive(
    snap: dict[str, Any],
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    """Убрать removed из снимка перед записью в SQLite (старые строки не трогаем)."""
    out = dict(snap or {})
    removed = removed_set(path=path)
    if not removed:
        return out
    out["dtv"] = [
        d
        for d in (out.get("dtv") or [])
        if isinstance(d, dict) and str(d.get("id") or "") not in removed
    ]
    out["ce"] = [
        d
        for d in (out.get("ce") or [])
        if isinstance(d, dict) and str(d.get("id") or "") not in removed
    ]
    out["devices"] = list(out["dtv"]) + list(out["ce"])
    return out
