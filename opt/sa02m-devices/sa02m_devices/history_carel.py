"""Carel AHU long-table path (`carel_samples`): per-metric series, overview,
export table. Temporary — retired by the wide-table migration (plan B4)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from sa02m_devices.history_export import _fmt_export_ts
from sa02m_devices.history_metrics import CAREL_METRIC_AGG, CAREL_METRIC_META
from sa02m_devices.history_mr import _merge_mr_series
from sa02m_devices.history_query import _first_device_id
from sa02m_devices.history_ranges import (
    _normalize_range,
    export_bucket_s,
    resolve_time_range,
)
from sa02m_devices.history_store import _connect, _read_paths, storage_status


def _query_series_carel(
    conn: sqlite3.Connection,
    t0: float,
    t1: float,
    bucket_s: float,
    device_id: str,
    metric_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Серии Carel: [{field, label, unit, points}] — единица last-wins как у MR."""
    where = " WHERE ts >= ? AND ts <= ? AND device_id = ? AND value IS NOT NULL"
    params: list[Any] = [t0, t1, device_id]
    if metric_filter:
        where += " AND metric = ?"
        params.append(metric_filter)

    by_m: dict[str, list[list[Any]]] = {}
    if bucket_s <= 1.0:
        sql = f"SELECT metric, ts, value FROM carel_samples{where} ORDER BY metric, ts"
        rows = conn.execute(sql, params).fetchall()
    else:
        # Per-metric aggregate (CAREL_METRIC_AGG): state flags take max() so a
        # bucket that contained an alarm stays painted; the rest average.
        max_metrics = [m for m, agg in CAREL_METRIC_AGG.items() if agg == "max"]
        agg_sql = (
            "CASE WHEN metric IN (%s) THEN max(value) ELSE avg(value) END"
            % ",".join("?" for _ in max_metrics)
        )
        sql = (
            f"SELECT metric, cast(ts / ? as integer) * ? AS bucket, {agg_sql}"
            f" FROM carel_samples{where}"
            " GROUP BY metric, bucket ORDER BY metric, bucket"
        )
        rows = conn.execute(
            sql, [bucket_s, bucket_s, *max_metrics, *params]
        ).fetchall()
    for row in rows:
        if row[2] is None:
            continue
        try:
            metric = str(row[0] or "")
            ts_ms = int(float(row[1]) * 1000)
            val = float(row[2])
        except (TypeError, ValueError):
            continue
        if not metric:
            continue
        by_m.setdefault(metric, []).append([ts_ms, val])

    unit_where = " WHERE ts >= ? AND ts <= ? AND device_id = ?"
    unit_params: list[Any] = [t0, t1, device_id]
    if metric_filter:
        unit_where += " AND metric = ?"
        unit_params.append(metric_filter)
    unit_by_m: dict[str, tuple[str, float]] = {}
    for row in conn.execute(
        f"SELECT metric, unit, MAX(ts) FROM carel_samples{unit_where} GROUP BY metric",
        unit_params,
    ).fetchall():
        unit_by_m[str(row[0] or "")] = (str(row[1] or ""), float(row[2] or 0.0))

    out: list[dict[str, Any]] = []
    for metric in sorted(by_m, key=lambda m: list(CAREL_METRIC_META).index(m) if m in CAREL_METRIC_META else 99):
        meta = CAREL_METRIC_META.get(metric, (metric, "", 1))
        unit, unit_ts = unit_by_m.get(metric, (meta[1], 0.0))
        out.append({
            "field": metric,
            "label": meta[0],
            "unit": unit or meta[1],
            "unit_ts": unit_ts,
            "points": by_m[metric],
        })
    return out


def _carel_series_over_dbs(
    device_id: str | None,
    range_key: str,
    path: Path | None,
    metric_filter: str | None,
    bucket_s: float | None,
) -> tuple[list[dict[str, Any]], str, float, float]:
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
                did = _first_device_id(conn, "carel_samples", t0, t1)
            if not did:
                continue
            parts.append(
                _query_series_carel(
                    conn, t0, t1, bucket_s, did, metric_filter=metric_filter
                )
            )
        finally:
            conn.close()
    return _merge_mr_series(parts), (did or ""), t0, t1


def history_carel(
    device_id: str | None,
    range_key: str = "1h",
    metric: str | None = None,
    path: Path | None = None,
    *,
    bucket_s: float | None = None,
) -> dict[str, Any]:
    """История одной метрики Carel — форма ответа как у history()."""
    mid = str(metric or "").strip()
    series, did, t0, t1 = _carel_series_over_dbs(
        device_id, range_key, path, mid or None, bucket_s
    )
    range_key = _normalize_range(range_key)
    meta = CAREL_METRIC_META.get(mid, (mid or "Carel", "", 1))
    unit = series[0]["unit"] if series else meta[1]
    return {
        "ok": True,
        "metric": mid,
        "label": meta[0],
        "unit": unit,
        "decimals": meta[2],
        "device": "carel",
        "device_id": did,
        "range": range_key,
        "t0": t0,
        "t1": t1,
        "t0_ms": int(t0 * 1000),
        "t1_ms": int(t1 * 1000),
        "series": series,
        **(storage_status() if path is None else {}),
    }


def history_carel_batch(
    device_id: str | None,
    range_key: str = "1h",
    path: Path | None = None,
    *,
    bucket_s: float | None = None,
) -> dict[str, Any]:
    """Все метрики Carel как metrics[] — форма как history_batch()."""
    series, did, t0, t1 = _carel_series_over_dbs(
        device_id, range_key, path, None, bucket_s
    )
    range_key = _normalize_range(range_key)
    metrics = [
        {
            "metric": s["field"],
            "label": s["label"],
            "unit": s["unit"],
            "decimals": CAREL_METRIC_META.get(s["field"], ("", "", 1))[2],
            "device": "carel",
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
        "device": "carel",
        "device_id": did,
        "t0": t0,
        "t1": t1,
        "t0_ms": int(t0 * 1000),
        "t1_ms": int(t1 * 1000),
        "metrics": metrics,
        **(storage_status() if path is None else {}),
    }


def collect_export_table_carel(
    range_key: str = "1h",
    *,
    device_id: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Таблица экспорта Carel (время × метрики) — форма как collect_export_table_mr()."""
    range_key = _normalize_range(range_key)
    bucket = export_bucket_s(range_key)
    batch = history_carel_batch(device_id, range_key, path=path, bucket_s=bucket)
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
        "kind": "carel",
        "title": "Carel AHU",
    }
