"""docs/contracts/home-connect.md tables == the code's tables.

The contract carries a second copy of lists the code owns once: §4 controls
(`mapping.MAPPING`), §6 states / reasons / status.json keys
(`constants.STATES`, `constants.REASONS`, `status.STATUS_KEYS`), §9 actions
and dispatcher error codes (`api.MUTATIONS` + `status`, the `"error"` literals
in api.py), §10 trigger verbs (`constants.TRIGGER_VERBS`). Each is parsed out
of the markdown and compared as a set; a table that is missing or parses to
zero rows FAILS — an empty parse never reads as "nothing differs".

HOMECONNECT_CONTRACT_MD=<file> judges another copy of the contract (the RED run).
"""

from __future__ import annotations

import os
import re
import unittest

from sa02m_homeconnect import api
from sa02m_homeconnect import constants as C
from sa02m_homeconnect import mapping
from sa02m_homeconnect import status as S

from . import HOMECONNECT_ROOT, REPO_ROOT

CONTRACT = os.environ.get("HOMECONNECT_CONTRACT_MD") or os.path.join(
    REPO_ROOT, "docs", "contracts", "home-connect.md")

_SECTION_RE = re.compile(r"^## (\d+)\.", re.M)
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")
_FIRST_CODE = re.compile(r"`([^`]+)`")


def section(text: str, number: int) -> str:
    heads = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(heads):
        if int(m.group(1)) == number:
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            return text[m.end():end]
    return ""


def split_cells(line: str) -> list:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [c.strip().replace("\\|", "|") for c in _UNESCAPED_PIPE.split(body)]


def table_by_header(block: str, first_header: str) -> list:
    lines = block.splitlines()
    for i in range(len(lines) - 1):
        if (lines[i].lstrip().startswith("|") and lines[i + 1].lstrip().startswith("|")
                and all(_SEPARATOR_CELL.match(c) for c in split_cells(lines[i + 1]))
                and split_cells(lines[i])[0] == first_header):
            rows = []
            j = i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                rows.append(split_cells(lines[j]))
                j += 1
            if not rows:
                raise AssertionError("table %r has no rows" % first_header)
            return rows
    raise AssertionError("no table with first header %r" % first_header)


def first_code(cell: str) -> str:
    m = _FIRST_CODE.search(cell)
    if not m:
        raise AssertionError("no `code` in cell %r" % cell)
    return m.group(1)


def plain(cell: str) -> str:
    cell = cell.strip()
    if cell in ("—", "-", ""):
        return ""
    return cell.strip("`")


class ContractTablesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with open(CONTRACT, encoding="utf-8") as fh:
            cls.text = fh.read()

    def block(self, number: int) -> str:
        body = section(self.text, number)
        self.assertTrue(body.strip(), "section %d missing" % number)
        return body

    def test_control_table(self) -> None:
        rows = table_by_header(self.block(4), "control")
        self.assertGreaterEqual(len(rows), 14)
        doc = {(first_code(r[0]), plain(r[1]), plain(r[2])) for r in rows}
        code = {(c.name, c.wb_type, c.units) for c in mapping.MAPPING}
        self.assertEqual(doc, code)

    def test_type_table_names_only_real_controls(self) -> None:
        rows = table_by_header(self.block(4), "Тип прибора")
        named = set()
        for r in rows:
            named.update(re.findall(r"`([a-z_]+)`", r[1]))
        self.assertTrue(named)
        self.assertLessEqual(named, set(mapping.CONTROLS))

    def test_states_reasons_status_keys(self) -> None:
        block = self.block(6)
        self.assertEqual({first_code(r[0]) for r in table_by_header(block, "Состояние")}, set(C.STATES))
        self.assertEqual({first_code(r[0]) for r in table_by_header(block, "Причина")}, set(C.REASONS))
        self.assertEqual({first_code(r[0]) for r in table_by_header(block, "Ключ")}, set(S.STATUS_KEYS))

    def test_actions_and_error_codes(self) -> None:
        block = self.block(9)
        actions = {first_code(r[0]) for r in table_by_header(block, "Действие")}
        self.assertEqual(actions, {"status"} | set(api.MUTATIONS))
        doc_codes = {first_code(r[0]) for r in table_by_header(block, "Код")}
        with open(os.path.join(HOMECONNECT_ROOT, "sa02m_homeconnect", "api.py"), encoding="utf-8") as fh:
            src = fh.read()
        code_codes = set(re.findall(r'"error": "([a-z_]+)"', src))
        self.assertGreaterEqual(len(code_codes), 8)
        self.assertEqual(doc_codes, code_codes)

    def test_trigger_verbs(self) -> None:
        verbs = {first_code(r[0]) for r in table_by_header(self.block(10), "Глагол")}
        self.assertEqual(verbs, set(C.TRIGGER_VERBS))


if __name__ == "__main__":
    unittest.main()
