# -*- coding: utf-8 -*-
"""CSV-журнал точек опроса и событий эксперимента."""
from __future__ import annotations

import csv
import io
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

COLUMNS = ["t", "pv", "sp_active", "cv", "oat", "rwt", "fan", "xp", "ti", "event"]


class CsvLogger:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path
        self._fh = open(path, "w", newline="", encoding="utf-8") if path else io.StringIO()
        self._w = csv.writer(self._fh, delimiter=";")
        self._w.writerow(COLUMNS)
        self.rows: List[Dict[str, object]] = []

    def log_sample(self, t: float, values: Dict[str, float], event: str = "") -> None:
        row = {
            "t": round(t, 3),
            "pv": values.get("pv"), "sp_active": values.get("sp_active"),
            "cv": values.get("cv"), "oat": values.get("oat"),
            "rwt": values.get("rwt"), "fan": values.get("fan"),
            "xp": values.get("xp"), "ti": values.get("ti"),
            "event": event,
        }
        self.rows.append(row)
        self._w.writerow([row[c] for c in COLUMNS])
        self._fh.flush()

    def log_event(self, t: float, event: str) -> None:
        self.log_sample(t, {}, event=event)

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            logger.debug("close CSV log file handle failed", exc_info=True)

    # выборки для идентификации
    def series(self, key: str) -> List[float]:
        return [float(r[key]) for r in self.rows if r.get(key) is not None and r["event"] != "#"]


def load_csv(path: str) -> Dict[str, List[float]]:
    """Загрузка журнала для offline-идентификации (cli fit/tune)."""
    out: Dict[str, List[float]] = {c: [] for c in ("t", "pv", "sp_active", "cv")}
    with open(path, "r", encoding="utf-8") as f:
        rd = csv.DictReader(f, delimiter=";")
        for row in rd:
            try:
                t = float(row["t"])
                pv = float(row["pv"])
                cv = float(row["cv"])
            except (TypeError, ValueError, KeyError):
                continue  # строки-события без данных
            out["t"].append(t)
            out["pv"].append(pv)
            out["cv"].append(cv)
            try:
                out["sp_active"].append(float(row.get("sp_active") or "nan"))
            except ValueError:
                out["sp_active"].append(float("nan"))
    return out
