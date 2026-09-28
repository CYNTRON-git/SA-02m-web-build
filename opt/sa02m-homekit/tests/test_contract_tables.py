"""docs/contracts/homekit-bridge.md tables == the code's tables.

The contract carries a second copy of five lists the code owns once:
§3 mapping rows (`projection.MAPPING`), §4 skip reasons
(`projection.SKIP_REASONS`), §10 states / reasons (`constants.STATES`,
`constants.REASONS`) and the `status.json` keys (`status.STATUS_KEYS`). Each is
parsed out of the markdown and compared as a set, so a row added, dropped or
renamed on one side only fails here. A table that is missing or parses to zero
rows FAILS — an empty parse must never read as "nothing differs".

GFM detail: a literal `|` inside a cell is written `\\|` (the M08 condition);
cells are split on UNESCAPED pipes only and then unescaped.

HOMEKIT_CONTRACT_MD=<file> judges another copy of the contract (the RED run).
"""

from __future__ import annotations

import os
import re
import unittest

from sa02m_homekit import constants as C
from sa02m_homekit import projection as P
from sa02m_homekit import status as S

from . import REPO_ROOT

CONTRACT = os.environ.get("HOMEKIT_CONTRACT_MD") or os.path.join(
    REPO_ROOT, "docs", "contracts", "homekit-bridge.md")

_SECTION_RE = re.compile(r"^## (\d+)\.", re.M)
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")
_CODE_CELL = re.compile(r"^`([^`]+)`$")


def section(text: str, number: int) -> str:
    """The body of `## <number>.` up to the next `## ` heading ('' if absent)."""
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


def tables(block: str) -> list:
    """Every GFM table in `block` as (header cells, [row cells]), in order."""
    found = []
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        if (lines[i].lstrip().startswith("|") and i + 1 < len(lines)
                and lines[i + 1].lstrip().startswith("|")
                and all(_SEPARATOR_CELL.match(c) for c in split_cells(lines[i + 1]))):
            header = split_cells(lines[i])
            rows = []
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(split_cells(lines[i]))
                i += 1
            found.append((header, rows))
            continue
        i += 1
    return found


def table_by_header(block: str, first_header: str) -> list:
    """The rows of the first table whose first header cell is `first_header`.
    Raises AssertionError when there is no such table or it has no rows."""
    for header, rows in tables(block):
        if header and header[0] == first_header:
            if not rows:
                raise AssertionError("table «%s» has no rows — an empty parse proves nothing"
                                     % first_header)
            return rows
    raise AssertionError("no table with first column «%s» — the contract lost it or the "
                         "parser stopped matching" % first_header)


def code(cell: str) -> str:
    m = _CODE_CELL.match(cell)
    if not m:
        raise AssertionError("cell is not a single `code` span: %r" % cell)
    return m.group(1)


def first_column_codes(rows: list) -> set:
    return {code(r[0].split(" ", 1)[0]) for r in rows}


class ParserSelfTest(unittest.TestCase):
    """The parser itself must not be the vacuous part."""

    DOC = ("## 1. A\n\n| K | V |\n|---|---|\n| `a\\|b` | x |\n| `c` | y \\| z |\n\n"
           "## 2. B\n\n| K | V |\n|---|---|\n\n## 3. C\n\ntext only\n")

    def test_escaped_pipe_stays_inside_the_cell(self):
        rows = table_by_header(section(self.DOC, 1), "K")
        self.assertEqual(rows, [["`a|b`", "x"], ["`c`", "y | z"]])

    def test_header_only_table_fails(self):
        with self.assertRaises(AssertionError):
            table_by_header(section(self.DOC, 2), "K")

    def test_missing_table_fails(self):
        with self.assertRaises(AssertionError):
            table_by_header(section(self.DOC, 3), "K")

    def test_missing_section_is_empty(self):
        self.assertEqual(section(self.DOC, 9), "")


class ContractTablesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(CONTRACT, encoding="utf-8") as fh:
            cls.text = fh.read()
        cls.s3 = section(cls.text, 3)
        cls.s4 = section(cls.text, 4)
        cls.s10 = section(cls.text, 10)
        for num, body in ((3, cls.s3), (4, cls.s4), (10, cls.s10)):
            if not body.strip():
                raise AssertionError("contract section §%d not found in %s" % (num, CONTRACT))

    def test_mapping_rows_equal_projection_mapping(self):
        rows = table_by_header(self.s3, "Строка")
        doc = set()
        for r in rows:
            self.assertGreaterEqual(len(r), 5, r)
            chars = tuple(re.findall(r"`([^`]+)`", r[4]))
            self.assertTrue(chars, "no characteristics in row %s" % r[0])
            doc.add((code(r[0]), code(r[1]), code(r[2]), code(r[3]), chars))
        want = {(m.row_id, m.source, m.condition, m.service, tuple(m.characteristics))
                for m in P.MAPPING}
        self.assertGreaterEqual(len(doc), 14, "§3 carries fewer than 14 mapping rows")
        self.assertEqual(doc, want,
                         "\nonly in the contract: %s\nonly in projection.MAPPING: %s"
                         % (sorted(doc - want), sorted(want - doc)))

    def test_m08_condition_is_written_with_the_gfm_escape(self):
        raw = [ln for ln in self.s3.splitlines() if ln.startswith("| `M08`")]
        self.assertEqual(len(raw), 1, "exactly one M08 row expected")
        self.assertIn("\\|", raw[0], "the M08 condition must escape its `|` as `\\|`")

    def test_skip_reasons_equal_projection_skip_reasons(self):
        doc = first_column_codes(table_by_header(self.s4, "Причина"))
        self.assertEqual(doc, set(P.SKIP_REASONS))
        self.assertEqual(len(P.SKIP_REASONS), len(set(P.SKIP_REASONS)))

    def test_states_equal_constants_states(self):
        doc = first_column_codes(table_by_header(self.s10, "Состояние"))
        self.assertEqual(doc, set(C.STATES))

    def test_reasons_equal_constants_reasons(self):
        doc = first_column_codes(table_by_header(self.s10, "Причина"))
        self.assertEqual(doc, set(C.REASONS))

    def test_status_keys_equal_status_status_keys(self):
        doc = first_column_codes(table_by_header(self.s10, "Ключ"))
        self.assertEqual(doc, set(S.STATUS_KEYS))


if __name__ == "__main__":
    unittest.main()
