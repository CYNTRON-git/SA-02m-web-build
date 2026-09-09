"""Generic wide-table read engine (METRICS-driven series, batch) and the
CE period summary."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from sa02m_devices.history_metrics import DEFAULT_KWH_RUB, HISTORY_GROUPS, METRICS
from sa02m_devices.history_ranges import _normalize_range, resolve_time_range
from sa02m_devices.history_store import _connect, _read_paths, storage_status
from sa02m_devices.history_write import _number_is_finite


def _query_series(
    conn: sqlite3.Connection,
    table: str,
    fields: list[str],
    labels: dict[str, str],
    t0: float,
    t1: float,
    bucket_s: float,
    device_id: str | None = None,
    *,
    agg: str = "avg",
) -> list[dict[str, Any]]:
    cols = ", ".join(fields)
    where = " WHERE ts >= ? AND ts <= ?"
    params: list[Any] = [t0, t1]
    if device_id:
        where += " AND device_id = ?"
        params.append(device_id)
    agg_fn = "max" if agg == "max" else "avg"
    if bucket_s <= 1.0:
        sql = f"SELECT ts, {cols} FROM {table}{where} ORDER BY ts"
        rows = conn.execute(sql, params).fetchall()
        series_map: dict[str, list[list[Any]]] = {f: [] for f in fields}
        for row in rows:
            ts_ms = int(float(row[0]) * 1000)
            for i, field in enumerate(fields):
                val = row[i + 1]
                if val is None:
                    continue
                try:
                    series_map[field].append([ts_ms, float(val)])
                except (TypeError, ValueError):
                    continue
    else:
        # По одному полю: бакеты с NULL в соседних колонках не теряют точки
        series_map = {f: [] for f in fields}
        for field in fields:
            sql = (
                f"SELECT cast(ts / ? as integer) * ? AS bucket, {agg_fn}({field})"
                f" FROM {table}{where} AND {field} IS NOT NULL"
                f" GROUP BY bucket ORDER BY bucket"
            )
            try:
                rows = conn.execute(sql, [bucket_s, bucket_s, *params]).fetchall()
            except sqlite3.Error:
                continue
            for row in rows:
                if row[1] is None:
                    continue
                try:
                    series_map[field].append(
                        [int(float(row[0]) * 1000), float(row[1])]
                    )
                except (TypeError, ValueError):
                    continue
    return [
        {
            "field": f,
            "label": labels.get(f, f),
            "points": series_map[f],
        }
        for f in fields
        if series_map[f]
    ]


def _round_series_values(
    series: list[dict[str, Any]], decimals: int | None
) -> list[dict[str, Any]]:
    """Round Y values to metric publish precision (suppress AVG float noise)."""
    if decimals is None or not isinstance(decimals, int) or decimals < 0:
        return series
    out: list[dict[str, Any]] = []
    for ser in series:
        pts_in = ser.get("points") or []
        pts: list[list[Any]] = []
        for pair in pts_in:
            if not pair or len(pair) < 2:
                continue
            try:
                y = round(float(pair[1]), decimals)
            except (TypeError, ValueError):
                continue
            pts.append([pair[0], y])
        if pts:
            out.append({**ser, "points": pts})
    return out


def _merge_series_lists(
    parts: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Объединить series из нескольких БД по field, точки по ts."""
    by_field: dict[str, dict[str, Any]] = {}
    for series_list in parts:
        for ser in series_list:
            field = ser["field"]
            if field not in by_field:
                by_field[field] = {
                    "field": field,
                    "label": ser.get("label", field),
                    "points": [],
                }
            by_field[field]["points"].extend(ser.get("points") or [])
    out: list[dict[str, Any]] = []
    for field, ser in by_field.items():
        pts = ser["points"]
        # dedupe by ts keep last
        seen: dict[int, float] = {}
        for ts_ms, val in pts:
            seen[int(ts_ms)] = val
        merged = [[ts, seen[ts]] for ts in sorted(seen)]
        if merged:
            out.append({
                "field": field,
                "label": ser["label"],
                "points": merged,
            })
    return out


def history(
    metric_id: str,
    range_key: str = "1h",
    path: Path | None = None,
    *,
    device_id: str | None = None,
    bucket_s: float | None = None,
    agg: str | None = None,
) -> dict[str, Any]:
    meta = METRICS.get(metric_id)
    if not meta:
        return {"ok": False, "error": "unknown metric", "metric": metric_id}
    range_key = _normalize_range(range_key)
    t0, t1, chart_bucket = resolve_time_range(range_key)
    if bucket_s is None:
        bucket_s = chart_bucket
    # The entry's own bucket aggregate (`agg` in METRICS — the Carel state group
    # takes max so a bucket that contained an alarm stays painted) unless the
    # caller names one (the export's energy max). Absent both → avg, as before.
    agg = agg or str(meta.get("agg") or "avg")
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
                row = conn.execute(
                    f"SELECT device_id FROM {meta['table']}"
                    f" WHERE ts >= ? AND ts <= ? AND device_id != ''"
                    f" ORDER BY device_id LIMIT 1",
                    (t0, t1),
                ).fetchone()
                if row:
                    did = str(row[0])
            parts.append(
                _query_series(
                    conn,
                    meta["table"],
                    list(meta["fields"]),
                    dict(meta["labels"]),
                    t0,
                    t1,
                    bucket_s,
                    device_id=did,
                    agg=agg,
                )
            )
        finally:
            conn.close()
    series = _merge_series_lists(parts)
    dec = meta.get("decimals")
    if isinstance(dec, int):
        series = _round_series_values(series, dec)
    status = storage_status() if path is None else {}
    return {
        "ok": True,
        "metric": metric_id,
        "label": meta["label"],
        "unit": meta["unit"],
        "decimals": dec if isinstance(dec, int) else None,
        "device": meta["device"],
        "device_id": did or "",
        "range": range_key,
        "t0": t0,
        "t1": t1,
        "t0_ms": int(t0 * 1000),
        "t1_ms": int(t1 * 1000),
        "series": series,
        **status,
    }


def history_batch(
    range_key: str = "1h",
    group: str | None = None,
    metric_ids: list[str] | None = None,
    path: Path | None = None,
    *,
    device_id: str | None = None,
    bucket_s: float | None = None,
) -> dict[str, Any]:
    range_key = _normalize_range(range_key)
    if metric_ids:
        ids = [m for m in metric_ids if m in METRICS]
    elif group and group in HISTORY_GROUPS:
        ids = list(HISTORY_GROUPS[group])
    else:
        return {
            "ok": False,
            "error": "specify group=climate|energy or metrics=…",
        }
    out_metrics: list[dict[str, Any]] = []
    errors: list[str] = []
    for mid in ids:
        one = history(
            mid, range_key, path=path, device_id=device_id, bucket_s=bucket_s
        )
        if not one.get("ok"):
            errors.append(f"{mid}: {one.get('error', 'fail')}")
            out_metrics.append({
                "metric": mid,
                "label": METRICS[mid]["label"],
                "unit": METRICS[mid]["unit"],
                "decimals": METRICS[mid].get("decimals"),
                "device": METRICS[mid]["device"],
                "device_id": device_id or "",
                "series": [],
                "error": one.get("error"),
            })
            continue
        out_metrics.append({
            "metric": mid,
            "label": one["label"],
            "unit": one["unit"],
            "decimals": one.get("decimals"),
            "device": one["device"],
            "device_id": one.get("device_id") or "",
            "series": one.get("series") or [],
        })
    status = storage_status() if path is None else {}
    t0, t1, _b = resolve_time_range(range_key)
    return {
        "ok": True,
        "range": range_key,
        "group": group or "custom",
        "device_id": (device_id or "").strip(),
        "t0": t0,
        "t1": t1,
        "t0_ms": int(t0 * 1000),
        "t1_ms": int(t1 * 1000),
        "metrics": out_metrics,
        "errors": errors,
        **status,
    }


def _first_device_id(
    conn: sqlite3.Connection, table: str, t0: float, t1: float
) -> str | None:
    row = conn.execute(
        f"SELECT device_id FROM {table}"
        " WHERE ts >= ? AND ts <= ? AND device_id != ''"
        " ORDER BY device_id LIMIT 1",
        (t0, t1),
    ).fetchone()
    return str(row[0]) if row else None


def period_summary_ce(
    range_key: str = "1h",
    path: Path | None = None,
    *,
    device_id: str | None = None,
    kwh_rub: float | None = None,
) -> dict[str, Any]:
    """Сводка СЭ за период: ср. мощность по фазам, ΔE, стоимость."""
    range_key = _normalize_range(range_key)
    t0, t1, _bucket = resolve_time_range(range_key)
    did = (device_id or "").strip() or None
    tariff = float(kwh_rub if kwh_rub is not None else DEFAULT_KWH_RUB)

    sum_pa = sum_pb = sum_pc = sum_pt = 0.0
    n_pa = n_pb = n_pc = n_pt = 0
    e_first: float | None = None
    e_last: float | None = None
    e_first_ts: float | None = None
    e_last_ts: float | None = None
    samples = 0

    for dbfile in _read_paths(path):
        if not dbfile.is_file():
            continue
        try:
            conn = _connect(dbfile)
        except sqlite3.Error:
            continue
        try:
            if not did:
                row = conn.execute(
                    "SELECT device_id FROM ce_samples"
                    " WHERE ts >= ? AND ts <= ? AND device_id != ''"
                    " ORDER BY device_id LIMIT 1",
                    (t0, t1),
                ).fetchone()
                if row:
                    did = str(row[0])
            if not did:
                continue
            where = " WHERE ts >= ? AND ts <= ? AND device_id = ?"
            params: list[Any] = [t0, t1, did]
            # averages
            row = conn.execute(
                "SELECT"
                " avg(power_w_a), count(power_w_a),"
                " avg(power_w_b), count(power_w_b),"
                " avg(power_w_c), count(power_w_c),"
                " avg(power_w_total), count(power_w_total),"
                " count(*)"
                f" FROM ce_samples{where}",
                params,
            ).fetchone()
            if row and int(row[8] or 0) > 0:
                samples += int(row[8])
                if row[0] is not None and int(row[1] or 0) > 0:
                    sum_pa += float(row[0]) * int(row[1])
                    n_pa += int(row[1])
                if row[2] is not None and int(row[3] or 0) > 0:
                    sum_pb += float(row[2]) * int(row[3])
                    n_pb += int(row[3])
                if row[4] is not None and int(row[5] or 0) > 0:
                    sum_pc += float(row[4]) * int(row[5])
                    n_pc += int(row[5])
                if row[6] is not None and int(row[7] or 0) > 0:
                    sum_pt += float(row[6]) * int(row[7])
                    n_pt += int(row[7])
            # energy endpoints
            row0 = conn.execute(
                "SELECT ts, energy_kwh_import FROM ce_samples"
                f"{where} AND energy_kwh_import IS NOT NULL"
                " ORDER BY ts ASC LIMIT 1",
                params,
            ).fetchone()
            row1 = conn.execute(
                "SELECT ts, energy_kwh_import FROM ce_samples"
                f"{where} AND energy_kwh_import IS NOT NULL"
                " ORDER BY ts DESC LIMIT 1",
                params,
            ).fetchone()
            if row0 and _number_is_finite(row0[1]):
                ts0, e0 = float(row0[0]), float(row0[1])
                if e_first is None or ts0 < (e_first_ts or ts0):
                    e_first, e_first_ts = e0, ts0
            if row1 and _number_is_finite(row1[1]):
                ts1, e1 = float(row1[0]), float(row1[1])
                if e_last is None or ts1 > (e_last_ts or ts1):
                    e_last, e_last_ts = e1, ts1
        finally:
            conn.close()

    def _avg(s: float, n: int) -> float | None:
        return None if n <= 0 else round(s / n, 3)

    # Стоимость только по энергии (кВт·ч), не по средней мощности (кВт/Вт):
    # cost_rub = ΔE_kWh × ₽/кВт·ч. Мощность в ответе — справочно.
    energy_kwh = None
    if e_first is not None and e_last is not None:
        energy_kwh = round(max(0.0, e_last - e_first), 4)
    cost = None if energy_kwh is None else round(float(energy_kwh) * tariff, 2)

    return {
        "ok": True,
        "device": "ce",
        "device_id": did or "",
        "range": range_key,
        "t0": t0,
        "t1": t1,
        "samples": samples,
        "power_w": {
            "a": _avg(sum_pa, n_pa),
            "b": _avg(sum_pb, n_pb),
            "c": _avg(sum_pc, n_pc),
            "total": _avg(sum_pt, n_pt),
        },
        "energy_kwh_import": {
            "first": e_first,
            "last": e_last,
            "delta": energy_kwh,
            "unit": "kWh",
        },
        "kwh_rub": tariff,
        "kwh_rub_default": DEFAULT_KWH_RUB,
        "kwh_rub_unit": "RUB/kWh",
        "cost_rub": cost,
        "cost_basis": "energy_kwh",
        "cost_note": "Стоимость = ΔE (кВт·ч) × тариф (₽/кВт·ч)",
        **(storage_status() if path is None else {}),
    }
