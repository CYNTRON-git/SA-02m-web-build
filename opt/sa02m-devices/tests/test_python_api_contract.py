# -*- coding: utf-8 -*-
"""The public Python API of ``sa02m_devices`` as a cross-project contract.

Consumer: the HardPy stand (sibling repo ``hardpy_tests``), which imports this
package from ``/opt/sa02m-devices`` on the shared bench (``SA02M_DEVICES_ROOT``)
and falls back to its vendored ``lib/`` copy when the package is absent. It
runs no DDL of its own and calls the names below with the keywords below —
so a rename, a removal, or a keyword change here breaks a service in another
repo with no failing test on this side. This file is that failing test.

``CONTRACT`` is the ONE home of the machine-readable surface (names, keywords,
leading positional arity). The prose contract ``docs/contracts/
sa02m-devices-python-api.md`` names the modules and names and POINTS here for
the grammar; the name parity between the two is asserted below, so the doc
cannot drift from the table. The HardPy side mirrors the same table in its own
check (``scripts/check_devices_contract.py --expect upstream``).

What each entry pins, via ``inspect.signature``:
  * the name exists and is callable;
  * every keyword in ``kw`` is accepted by that name (a POSITIONAL_OR_KEYWORD or
    KEYWORD_ONLY parameter of that name, or a ``**kwargs``);
  * at least ``pos`` leading parameters can be passed positionally (or ``*args``)
    — pinned only where the consumer's call is on record; ``0`` means "existence
    and keywords only". Positional NAMES are free to change (the consumer never
    passes them by keyword).
Not pinned, on purpose: return shapes beyond the two below, defaults, types.

Stability rule and the CHANGELOG heading to watch: the contract doc. Run alone
with ``python -m unittest tests.test_python_api_contract`` from
``opt/sa02m-devices`` (row ``devices-python-api-contract``); pytest collects it
too (row ``py-unit-devices``).
"""

from __future__ import annotations

import importlib
import inspect
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

# module -> name -> {"pos": leading positional arity, "kw": keywords passed by keyword}
CONTRACT: dict[str, dict[str, dict[str, object]]] = {
    "sa02m_devices.device_history_db": {
        "history": {"pos": 2, "kw": ("device_id",)},
        "history_batch": {"pos": 1, "kw": ("group", "device_id")},
        "history_mr": {"pos": 2, "kw": ("ch",)},
        "history_mr_batch": {"pos": 2, "kw": ()},
        "history_carel": {"pos": 2, "kw": ("metric",)},
        "history_carel_batch": {"pos": 2, "kw": ()},
        "export_text": {"pos": 1, "kw": ("metric_id", "group", "device_id", "kind")},
        "export_xlsx": {"pos": 1, "kw": ("metric_id", "group", "device_id", "kind")},
        "period_summary_ce": {"pos": 1, "kw": ("device_id", "kwh_rub")},
        "storage_status": {"pos": 0, "kw": ()},
        "custom_range_key": {"pos": 1, "kw": ()},
        "db_path": {"pos": 0, "kw": ()},
        "ensure_schema": {"pos": 0, "kw": ()},
        "insert_sample": {"pos": 1, "kw": ("path",)},
        "purge_old": {"pos": 0, "kw": ()},
        "rotate_if_needed": {"pos": 0, "kw": ()},
    },
    "sa02m_devices.stand_devices": {
        "live_snapshot": {"pos": 0, "kw": ()},
        "parse_device_id": {"pos": 1, "kw": ()},
    },
    "sa02m_devices.devices_widgets": {
        "load": {"pos": 0, "kw": ()},
        "config_path": {"pos": 0, "kw": ()},
        "apply_widgets_view": {"pos": 1, "kw": ()},
        "add_widget": {"pos": 1, "kw": ()},
        "remove_widget": {"pos": 1, "kw": ("device",)},
        "filter_for_archive": {"pos": 1, "kw": ()},
    },
    "sa02m_devices.device_events": {
        "list_events": {"pos": 0, "kw": ("limit", "device_id")},
        "detect_ce_events": {"pos": 1, "kw": ("path",)},
        "purge_events": {"pos": 0, "kw": ()},
    },
    "sa02m_devices.websession": {
        "check_session_store": {"pos": 2, "kw": ("cookie_header", "session_dir")},
        "check_csrf": {"pos": 3, "kw": ("cookie_header", "csrf_header", "session_dir")},
        "csrf_error_body": {"pos": 1, "kw": ("reason",)},
    },
}

# module -> constants the consumer reads (existence + type)
CONSTANTS: dict[str, dict[str, type]] = {
    "sa02m_devices.websession": {"CSRF_HEADER": str, "DEFAULT_SESSION_DIR": str},
}

# Schema ownership: the tables this package creates and the consumer never DDLs.
OWNED_TABLES = ("dtv_samples", "ce_samples", "mr_samples", "carel_samples")

_DOC = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "sa02m-devices-python-api.md"

_KW_KINDS = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
_POS_KINDS = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)


def _accepts_keyword(sig: inspect.Signature, name: str) -> bool:
    params = sig.parameters
    if name in params and params[name].kind in _KW_KINDS:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _positional_capacity(sig: inspect.Signature) -> int | None:
    """Leading positional slots; None = unbounded (``*args``)."""
    params = list(sig.parameters.values())
    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params):
        return None
    return sum(1 for p in params if p.kind in _POS_KINDS)


class PythonApiContract(unittest.TestCase):
    def test_table_is_not_empty(self):
        # Non-vacuity: a gutted table must not pass as "nothing to check".
        self.assertGreaterEqual(len(CONTRACT), 5, "CONTRACT lost a module")
        self.assertGreaterEqual(sum(len(v) for v in CONTRACT.values()), 30, "CONTRACT lost names")
        self.assertTrue(CONSTANTS)

    def test_every_module_imports(self):
        for mod in list(CONTRACT) + list(CONSTANTS):
            with self.subTest(module=mod):
                importlib.import_module(mod)

    def test_names_exist_and_accept_the_contracted_call(self):
        for mod_name, names in CONTRACT.items():
            mod = importlib.import_module(mod_name)
            for name, spec in names.items():
                with self.subTest(name=f"{mod_name}.{name}"):
                    fn = getattr(mod, name, None)
                    self.assertIsNotNone(fn, f"{mod_name}.{name} is missing (renamed or removed?)")
                    self.assertTrue(callable(fn), f"{mod_name}.{name} is not callable")
                    sig = inspect.signature(fn)
                    for kw in spec["kw"]:  # type: ignore[union-attr]
                        self.assertTrue(
                            _accepts_keyword(sig, kw),
                            f"{mod_name}.{name} no longer accepts keyword '{kw}' - signature is {sig}",
                        )
                    cap = _positional_capacity(sig)
                    pos = int(spec["pos"])  # type: ignore[arg-type]
                    self.assertTrue(
                        cap is None or cap >= pos,
                        f"{mod_name}.{name} takes {cap} positional argument(s), the consumer passes {pos} - {sig}",
                    )

    def test_constants_exist(self):
        for mod_name, consts in CONSTANTS.items():
            mod = importlib.import_module(mod_name)
            for name, typ in consts.items():
                with self.subTest(name=f"{mod_name}.{name}"):
                    self.assertTrue(hasattr(mod, name), f"{mod_name}.{name} is missing")
                    self.assertIsInstance(getattr(mod, name), typ)
                    self.assertTrue(getattr(mod, name), f"{mod_name}.{name} is empty")

    def test_check_csrf_returns_ok_reason_tuple(self):
        # The consumer unpacks ``ok, reason = check_csrf(...)``; no cookie is the
        # pure branch (no file read), so the shape is pinned without a session dir.
        from sa02m_devices.websession import check_csrf

        res = check_csrf(None, None, "/nonexistent")
        self.assertIsInstance(res, tuple)
        self.assertEqual(len(res), 2)
        self.assertIs(res[0], False)
        self.assertIsInstance(res[1], str)

    def test_schema_is_owned_here_and_carel_is_wide(self):
        # The consumer runs no DDL: ensure_schema() alone must yield every owned
        # table, and carel_samples in its WIDE layout (one column per metric, no
        # long-table `metric`/`value` pair) — the layout HardPy's reads assume.
        from sa02m_devices.device_history_db import CAREL_COLUMNS, ensure_schema

        self.assertTrue(CAREL_COLUMNS)
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "history.sqlite"
            ensure_schema(db)
            conn = sqlite3.connect(str(db))
            try:
                tables = {
                    r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                for t in OWNED_TABLES:
                    self.assertIn(t, tables, f"ensure_schema() did not create {t}")
                carel_cols = {r[1] for r in conn.execute("PRAGMA table_info(carel_samples)")}
            finally:
                conn.close()
        for col in CAREL_COLUMNS:
            self.assertIn(col, carel_cols, f"carel_samples lost wide column {col}")
        self.assertNotIn("metric", carel_cols, "carel_samples is long again")
        self.assertNotIn("value", carel_cols, "carel_samples is long again")

    def test_contract_doc_names_exactly_this_table(self):
        # The doc lists modules and names for humans and points here for the
        # grammar; the two name sets must be equal so neither can drift.
        self.assertTrue(_DOC.is_file(), f"contract doc missing: {_DOC}")
        text = _DOC.read_text(encoding="utf-8")
        table = text.split("<!-- names:begin -->", 1)[1].split("<!-- names:end -->", 1)[0]
        doc_names: set[str] = set()
        for line in table.splitlines():
            m = re.match(r"^\|\s*`([a-z_.0-9]+)`\s*\|\s*`([A-Za-z_][A-Za-z_0-9]*)`", line)
            if m:
                doc_names.add(f"{m.group(1)}.{m.group(2)}")
        expected = {f"{m}.{n}" for m, names in CONTRACT.items() for n in names}
        expected |= {f"{m}.{n}" for m, consts in CONSTANTS.items() for n in consts}
        self.assertTrue(doc_names, "the doc's names table is empty or its markers moved")
        self.assertEqual(
            doc_names, expected,
            f"doc vs CONTRACT drift - only in doc: {sorted(doc_names - expected)}; "
            f"only in CONTRACT: {sorted(expected - doc_names)}",
        )


if __name__ == "__main__":
    unittest.main()
