"""Time windows and bucket math for the device archive: preset ranges,
the `w:<seconds>` custom window, chart/export bucket derivation, RU labels."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any


try:
    from zoneinfo import ZoneInfo

    _TZ = ZoneInfo(os.environ.get("STAND_TZ", "Europe/Moscow"))
except Exception:  # noqa: BLE001
    _TZ = timezone(timedelta(hours=3))


# range_key → (window_s OR None, chart_bucket_s). None window = календарный режим.
RANGES: dict[str, tuple[float | None, float]] = {
    "1h": (3600.0, 5.0),
    "6h": (6 * 3600.0, 30.0),
    "24h": (24 * 3600.0, 120.0),
    "7d": (7 * 86400.0, 600.0),
    "30d": (30 * 86400.0, 3600.0),
    "mtd": (None, 3600.0),  # с начала месяца
    "month": (None, 86400.0),  # предыдущий календарный месяц
}

# Агрегация для текстового экспорта
EXPORT_BUCKET_S: dict[str, float] = {
    "1h": 60.0,
    "6h": 1800.0,
    "24h": 3600.0,
    "7d": 86400.0,
    "30d": 86400.0,
    "mtd": 86400.0,
    "month": 86400.0,
}

RANGE_LABELS_RU: dict[str, str] = {
    "1h": "1 ч",
    "6h": "6 ч",
    "24h": "24 ч",
    "7d": "7 д",
    "30d": "30 д",
    "mtd": "с начала месяца",
    "month": "за месяц",
}


def _now_local() -> datetime:
    return datetime.now(_TZ)


# ── Continuous (custom) window support ────────────────────────────
# The chart wheel-zoom produces an ARBITRARY span (seconds), not a preset key.
# It is carried through the same `range_key` argument as a "w:<seconds>" token,
# so every history/export path validates and resolves it via the same helpers
# below — no new parameter is threaded through the call graph.
WINDOW_MIN_S = 60  # 1 minute — finest zoom-in
WINDOW_MAX_S = 30 * 86400  # 30 days — widest zoom-out
_CUSTOM_PREFIX = "w:"
# Target point counts: chart wants a dense-but-readable curve, export a coarser
# table. The bucket is snapped UP to a "sane" step so labels/ticks stay clean.
CHART_TARGET_POINTS = 400
EXPORT_TARGET_POINTS = 200
_BUCKET_STEPS: tuple[float, ...] = (
    1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800,
    3600, 7200, 10800, 21600, 43200, 86400,
)


def clamp_window_s(window_s: Any) -> int:
    """Coerce an arbitrary window to an int in [WINDOW_MIN_S, WINDOW_MAX_S]."""
    try:
        w = int(round(float(window_s)))
    except (TypeError, ValueError):
        return WINDOW_MIN_S
    return max(WINDOW_MIN_S, min(WINDOW_MAX_S, w))


def custom_range_key(window_s: Any) -> str:
    """Build the `w:<seconds>` range token from a clamped window."""
    return f"{_CUSTOM_PREFIX}{clamp_window_s(window_s)}"


def _parse_custom_window(range_key: Any) -> int | None:
    """Return the clamped window seconds for a `w:<seconds>` token, else None."""
    if isinstance(range_key, str) and range_key.startswith(_CUSTOM_PREFIX):
        return clamp_window_s(range_key[len(_CUSTOM_PREFIX):])
    return None


def _normalize_range(range_key: Any) -> str:
    """A valid preset OR a valid custom window passes through; else default 1h."""
    if range_key in RANGES or _parse_custom_window(range_key) is not None:
        return str(range_key)
    return "1h"


def _snap_bucket_up(raw: float) -> float:
    """Smallest sane bucket step ≥ raw (never below 1 s)."""
    for step in _BUCKET_STEPS:
        if step >= raw:
            return float(step)
    return float(_BUCKET_STEPS[-1])


def _derive_bucket_s(window_s: int, target_points: int = CHART_TARGET_POINTS) -> float:
    """Chart bucket for a custom window: ~target_points, snapped to a sane step."""
    w = max(1, int(window_s))
    return _snap_bucket_up(w / float(max(1, target_points)))


def _derive_export_bucket_s(window_s: int) -> float:
    """Export bucket for a custom window — coarser than chart, never sub-minute."""
    return max(60.0, _derive_bucket_s(window_s, target_points=EXPORT_TARGET_POINTS))


def _range_slug(range_key: str) -> str:
    """Filename-safe token — a `w:3600` custom range becomes `w3600` (no colon)."""
    win = _parse_custom_window(range_key)
    return f"w{win}" if win is not None else str(range_key)


def _fmt_window_ru(window_s: int) -> str:
    """Human window label, e.g. «90 мин» / «6 ч» / «3 д»."""
    s = int(window_s)
    if s < 3600:
        return f"{max(1, round(s / 60))} мин"
    if s < 86400:
        h = s / 3600.0
        return f"{int(h) if h.is_integer() else round(h, 1)} ч"
    d = s / 86400.0
    return f"{int(d) if d.is_integer() else round(d, 1)} д"


def range_label_ru(range_key: str) -> str:
    """RU period label for a preset OR a custom window."""
    win = _parse_custom_window(range_key)
    if win is not None:
        return _fmt_window_ru(win)
    return RANGE_LABELS_RU.get(range_key, range_key)


def resolve_time_range(range_key: str) -> tuple[float, float, float]:
    """Вернуть (t0, t1, chart_bucket_s) для range_key (пресет или w:<сек>)."""
    win = _parse_custom_window(range_key)
    if win is not None:
        now = time.time()
        return now - float(win), now, _derive_bucket_s(win)
    key = range_key if range_key in RANGES else "1h"
    window_s, bucket_s = RANGES[key]
    now = time.time()
    if window_s is not None:
        return now - float(window_s), now, float(bucket_s)
    local = _now_local()
    if key == "mtd":
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp(), now, float(bucket_s)
    # previous calendar month
    first_this = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_prev = first_this - timedelta(seconds=1)
    start_prev = last_prev.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end_prev = first_this
    return start_prev.timestamp(), end_prev.timestamp(), float(bucket_s)


def export_bucket_s(range_key: str) -> float:
    win = _parse_custom_window(range_key)
    if win is not None:
        return _derive_export_bucket_s(win)
    return float(EXPORT_BUCKET_S.get(range_key) or EXPORT_BUCKET_S["1h"])
