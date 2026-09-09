"""Device telemetry archive (DTV / CE-02m-3 / MR-02m AI / Carel, SQLite, 30 d window)
— the package's public surface.

Every importer (api, the logger, device_events, device_history_migrate, the
tests, the contracts) reads this module; the implementation lives in the
history_* modules re-exported below, one responsibility each:

  history_ranges   time windows + bucket math
  history_metrics  the declarative metric catalog
  history_store    file, DDL, migrations, connection, rotation
  history_write    live_snapshot → rows, purge
  history_query    generic wide-table engine + CE summary
  history_mr       MR-02m long-table path (docs/contracts/devices-mr-history.md)
  history_export   tables → TSV / xlsx

DTV/CE and Carel are wide tables, PK (ts, device_id): DTV/CE written every tick
(1 Hz USB/SD, 5 s eMMC), Carel on its own 10 s cadence through the METRICS
engine and the `kind=carel` adapter in history_query (docs/contracts/
carel-ahu.md §7). MR-02m AI stays long — its channel count and per-sample unit
are dynamic (docs/contracts/devices-mr-history.md). Path: USB → SD → eMMC
(stand_storage_path).

Re-exports are explicit and permanent (plan 1.0.6.41, fork F3): the
underscored names are the ones tests and device_events reach.
"""

from __future__ import annotations

from sa02m_devices.history_ranges import (  # noqa: F401
    _TZ,
    _now_local,
    RANGES,
    EXPORT_BUCKET_S,
    RANGE_LABELS_RU,
    WINDOW_MIN_S,
    WINDOW_MAX_S,
    _CUSTOM_PREFIX,
    CHART_TARGET_POINTS,
    EXPORT_TARGET_POINTS,
    _BUCKET_STEPS,
    clamp_window_s,
    custom_range_key,
    _parse_custom_window,
    _normalize_range,
    _snap_bucket_up,
    _derive_bucket_s,
    _derive_export_bucket_s,
    _range_slug,
    _fmt_window_ru,
    range_label_ru,
    resolve_time_range,
    export_bucket_s,
)
from sa02m_devices.history_metrics import (  # noqa: F401
    METRICS,
    HISTORY_GROUPS,
    AHU_PREFIX,
    AHU_GROUP,
    CAREL_COLUMNS,
    CAREL_METRIC_META,
    CAREL_PLANT_STATE_CODE,
    CAREL_METRIC_AGG,
    carel_plant_code,
    DEFAULT_KWH_RUB,
    _DTV_SENSOR_COLUMNS,
    _DTV_SENSOR_LIST_COLS,
)
from sa02m_devices.history_store import (  # noqa: F401
    RETENTION_S,
    EVENT_RETENTION_S,
    ROTATE_BYTES,
    ROTATE_HEADROOM,
    log,
    _CREATE_DTV,
    _CREATE_CE,
    _CREATE_MR,
    _CREATE_CAREL,
    _ensure_ce_power_phase_cols,
    _ensure_dtv_sensor_cols,
    db_path,
    storage_status,
    _needs_pk_migration,
    _migrate_table,
    _carel_is_long,
    _migrate_carel_to_wide,
    ensure_schema,
    _connect,
    _read_paths,
    rotate_if_needed,
    DEFAULT_DB_PATH,
)
from sa02m_devices.history_write import (  # noqa: F401
    _as_device_list,
    _dtv_sensor_col_values,
    _insert_dtv,
    _insert_ce,
    insert_sample,
    _ai_ch_num,
    _insert_mr,
    insert_mr_sample,
    _insert_carel,
    insert_carel_sample,
    _number_is_finite,
    purge_old,
)
from sa02m_devices.history_query import (  # noqa: F401
    _query_series,
    _round_series_values,
    _merge_series_lists,
    _first_device_id,
    history,
    history_batch,
    history_carel,
    history_carel_batch,
    period_summary_ce,
)
from sa02m_devices.history_mr import (  # noqa: F401
    _coerce_ch,
    _query_series_mr,
    _merge_mr_series,
    _mr_series_over_dbs,
    history_mr,
    history_mr_batch,
    collect_export_table_mr,
)
from sa02m_devices.history_export import (  # noqa: F401
    _fmt_export_ts,
    _export_bucket_label,
    _export_col_title,
    collect_export_table,
    collect_export_table_carel,
    _xml_escape,
    _export_xlsx_minimal,
    export_xlsx,
    export_text,
)
