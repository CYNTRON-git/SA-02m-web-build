"""MR-02m AI long-table path (`mr_samples`): per-channel series, overview,
export table. Permanent — docs/contracts/devices-mr-history.md."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from sa02m_devices.history_export import _fmt_export_ts
from sa02m_devices.history_query import _first_device_id
from sa02m_devices.history_ranges import (
    _normalize_range,
    export_bucket_s,
    resolve_time_range,
)
from sa02m_devices.history_store import _connect, _read_paths, storage_status
from sa02m_devices.history_write import _ai_ch_num


def _coerce_ch(ch: Any) -> int | None:
    """«ai_5» / «5» / 5 → 5; None / мусор → None."""
    if ch is None:
        return None
    s = str(ch).strip()
    if s.startswith("ai_"):
        s = s[3:]
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _query_series_mr(
    conn: sqlite3.Connection,
    t0: float,
    t1: float,
    bucket_s: float,
    device_id: str,
    ch_filter: int | None = None,
) -> list[dict[str, Any]]:
    """Серии по каналам: [{field:'ai_N', label:'AI N', unit, unit_ts, points}].

    Единица канала — из строки с максимальным ts в окне (last-wins), поэтому смена
    типа датчика видна честно (без синтетического «моста»)."""
    where = " WHERE ts >= ? AND ts <= ? AND device_id = ? AND value IS NOT NULL"
    params: list[Any] = [t0, t1, device_id]
    if ch_filter is not None:
        where += " AND ch = ?"
        params.append(int(ch_filter))

    by_ch: dict[int, list[list[Any]]] = {}
    if bucket_s <= 1.0:
        sql = f"SELECT ch, ts, value FROM mr_samples{where} ORDER BY ch, ts"
        rows = conn.execute(sql, params).fetchall()
    else:
        sql = (
            "SELECT ch, cast(ts / ? as integer) * ? AS bucket, avg(value)"
            f" FROM mr_samples{where}"
            " GROUP BY ch, bucket ORDER BY ch, bucket"
        )
        rows = conn.execute(sql, [bucket_s, bucket_s, *params]).fetchall()
    for row in rows:
        if row[2] is None:
            continue
        try:
            ch = int(row[0])
            ts_ms = int(float(row[1]) * 1000)
            val = float(row[2])
        except (TypeError, ValueError):
            continue
        by_ch.setdefault(ch, []).append([ts_ms, val])

    # Единица на канал: строка с MAX(ts) в окне (SQLite bare-column-с-агрегатом).
    unit_where = " WHERE ts >= ? AND ts <= ? AND device_id = ?"
    unit_params: list[Any] = [t0, t1, device_id]
    if ch_filter is not None:
        unit_where += " AND ch = ?"
        unit_params.append(int(ch_filter))
    unit_by_ch: dict[int, tuple[str, float]] = {}
    for row in conn.execute(
        f"SELECT ch, unit, MAX(ts) FROM mr_samples{unit_where} GROUP BY ch",
        unit_params,
    ).fetchall():
        try:
            ch = int(row[0])
        except (TypeError, ValueError):
            continue
        unit_by_ch[ch] = (str(row[1] or ""), float(row[2] or 0.0))

    out: list[dict[str, Any]] = []
    for ch in sorted(by_ch):
        unit, unit_ts = unit_by_ch.get(ch, ("", 0.0))
        out.append({
            "field": f"ai_{ch}",
            "label": f"AI {ch}",
            "unit": unit,
            "unit_ts": unit_ts,
            "points": by_ch[ch],
        })
    return out


def _merge_mr_series(
    parts: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Объединить MR-серии из нескольких БД по каналу; единица — самая свежая."""
    by_field: dict[str, dict[str, Any]] = {}
    for series_list in parts:
        for ser in series_list:
            field = ser["field"]
            entry = by_field.get(field)
            if entry is None:
                entry = by_field[field] = {
                    "field": field,
                    "label": ser.get("label", field),
                    "unit": "",
                    "unit_ts": -1.0,
                    "points": [],
                }
            unit = ser.get("unit") or ""
            unit_ts = float(ser.get("unit_ts") or 0.0)
            if unit and unit_ts >= entry["unit_ts"]:
                entry["unit"] = unit
                entry["unit_ts"] = unit_ts
            entry["points"].extend(ser.get("points") or [])
    out: list[dict[str, Any]] = []
    for field in sorted(by_field, key=_ai_ch_num):
        entry = by_field[field]
        seen: dict[int, float] = {}
        for ts_ms, val in entry["points"]:
            seen[int(ts_ms)] = val
        merged = [[ts, seen[ts]] for ts in sorted(seen)]
        if merged:
            out.append({
                "field": field,
                "label": entry["label"],
                "unit": entry["unit"],
                "points": merged,
            })
    return out


def _mr_series_over_dbs(
    device_id: str | None,
    range_key: str,
    path: Path | None,
    ch_filter: int | None,
    bucket_s: float | None,
) -> tuple[list[dict[str, Any]], str, float, float]:
    """Собрать MR-серии по всем файлам БД; вернуть (series, device_id, t0, t1)."""
    range_key = _normalize_range(range_key)
    t0, t1, chart_bucket = resolve_time_range(range_key)
    if bucket_s is None:
        bucket_s = chart_bucket
    did = (device_id or "").strip() or None
    parts: list[list[dict[str, Any]]] = []
    for dbfile in _read_paths(path):
        if not dbfile.is_file():
            continue
        try:
            conn = _connect(dbfile)
        except sqlite3.Error:
            continue
        try:
            if not did:
                did = _first_device_id(conn, "mr_samples", t0, t1)
            if not did:
                continue
            parts.append(
                _query_series_mr(conn, t0, t1, bucket_s, did, ch_filter=ch_filter)
            )
        finally:
            conn.close()
    return _merge_mr_series(parts), (did or ""), t0, t1


def history_mr(
    device_id: str | None,
    range_key: str = "1h",
    ch: Any = None,
    path: Path | None = None,
    *,
    bucket_s: float | None = None,
) -> dict[str, Any]:
    """История одного AI-канала MR-02m — форма ответа как у history()."""
    ch_num = _coerce_ch(ch)
    series, did, t0, t1 = _mr_series_over_dbs(
        device_id, range_key, path, ch_num, bucket_s
    )
    range_key = _normalize_range(range_key)
    unit = series[0]["unit"] if series else ""
    label = f"AI {ch_num}" if ch_num is not None else "MR-02m AI"
    return {
        "ok": True,
        "metric": f"ai_{ch_num}" if ch_num is not None else "",
        "label": label,
        "unit": unit,
        "decimals": None,
        "device": "mr",
        "device_id": did,
        "range": range_key,
        "t0": t0,
        "t1": t1,
        "t0_ms": int(t0 * 1000),
        "t1_ms": int(t1 * 1000),
        "series": series,
        **(storage_status() if path is None else {}),
    }


def history_mr_batch(
    device_id: str | None,
    range_key: str = "1h",
    path: Path | None = None,
    *,
    bucket_s: float | None = None,
) -> dict[str, Any]:
    """Все включённые AI-каналы MR-02m как metrics[] — форма как history_batch()."""
    series, did, t0, t1 = _mr_series_over_dbs(
        device_id, range_key, path, None, bucket_s
    )
    range_key = _normalize_range(range_key)
    metrics = [
        {
            "metric": s["field"],
            "label": s["label"],
            "unit": s["unit"],
            "decimals": None,
            "device": "mr",
            "device_id": did,
            "series": [{
                "field": s["field"],
                "label": s["label"],
                "unit": s["unit"],
                "points": s["points"],
            }],
        }
        for s in series
    ]
    return {
        "ok": True,
        "range": range_key,
        "group": "all",
        "device": "mr",
        "device_id": did,
        "t0": t0,
        "t1": t1,
        "t0_ms": int(t0 * 1000),
        "t1_ms": int(t1 * 1000),
        "metrics": metrics,
        "errors": [],
        **(storage_status() if path is None else {}),
    }


def collect_export_table_mr(
    range_key: str = "1h",
    *,
    device_id: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Таблица экспорта MR-02m AI (время × включённые каналы) — форма как
    collect_export_table(), но колонки динамические (по каналам в окне)."""
    range_key = _normalize_range(range_key)
    bucket = export_bucket_s(range_key)
    batch = history_mr_batch(device_id, range_key, path=path, bucket_s=bucket)
    did = str(batch.get("device_id") or (device_id or "").strip())
    t0 = float(batch.get("t0") or 0.0)
    t1 = float(batch.get("t1") or 0.0)

    col_fields: list[str] = []
    col_titles: list[str] = []
    by_ts: dict[int, dict[str, float]] = {}
    for metric in batch.get("metrics") or []:
        unit = str(metric.get("unit") or "").strip()
        for ser in metric.get("series") or []:
            field = str(ser.get("field") or "")
            if not field:
                continue
            label = str(ser.get("label") or field)
            col_fields.append(field)
            col_titles.append(f"{label}, {unit}" if unit else label)
            for ts_ms, val in ser.get("points") or []:
                try:
                    by_ts.setdefault(int(ts_ms), {})[field] = float(val)
                except (TypeError, ValueError):
                    continue

    headers = ["Время"] + col_titles
    rows: list[list[Any]] = []
    for ts_ms in sorted(by_ts):
        cells: list[Any] = [_fmt_export_ts(ts_ms / 1000.0, bucket)]
        vals = by_ts[ts_ms]
        for field in col_fields:
            v = vals.get(field)
            cells.append(None if v is None else float(v))
        rows.append(cells)

    return {
        "ok": True,
        "error": "",
        "headers": headers,
        "rows": rows,
        "device_id": did,
        "range": range_key,
        "bucket_s": bucket,
        "t0": t0,
        "t1": t1,
        "metric_ids": col_fields,
        "kind": "mr",
        "title": "MR-02m AI",
    }
