"""docs/contracts/audio-recorder.md tables == the code's tables.

The contract carries a second copy of lists the code owns once: §2 controls
(`constants.CONTROLS`), §3 last_status values (`constants.LAST_STATUSES`),
§5 media (`constants.MEDIA`), §8 conf keys + defaults (`config.KEYS`,
`config.RecorderConfig`), §9 states and status.json keys (`constants.STATES`,
`service.STATUS_KEYS`), §10 event kinds (`constants.EVENT_KINDS`), §15 panel
actions and dispatch codes (`api.MUTATIONS`, `api.ERROR_CODES`), §16 helper
verbs (`constants.TRIGGER_VERBS`), and every "`NAME` = <number>" it quotes. Each is parsed out of the markdown and compared;
a table that is missing or parses to zero rows FAILS — an empty parse never
reads as "nothing differs".

AUDIO_CONTRACT_MD=<file> judges another copy of the contract (the RED run).
"""

from __future__ import annotations

import os
import re
import unittest

from sa02m_audio import api
from sa02m_audio import config
from sa02m_audio import constants as C
from sa02m_audio import service

from . import REPO_ROOT

CONTRACT = os.environ.get("AUDIO_CONTRACT_MD") or os.path.join(
    REPO_ROOT, "docs", "contracts", "audio-recorder.md")

_SECTION_RE = re.compile(r"^## (\d+)\.", re.M)
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")
_FIRST_CODE = re.compile(r"`([^`]+)`")
_QUOTED_CONSTANT = re.compile(r"`([A-Z][A-Z0-9_]+)` = ([0-9][0-9 ]*)")


def section(text, number):
    heads = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(heads):
        if int(m.group(1)) == number:
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            return text[m.end():end]
    return ""


def split_cells(line):
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [c.strip().replace("\\|", "|") for c in _UNESCAPED_PIPE.split(body)]


def table_by_header(block, first_header):
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


def first_code(cell):
    m = _FIRST_CODE.search(cell)
    if not m:
        raise AssertionError("no `code` in cell %r" % cell)
    return m.group(1)


def plain(cell):
    cell = cell.strip()
    if cell in ("—", "-", ""):
        return ""
    return cell.strip("`")


class ContractTablesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(CONTRACT, encoding="utf-8") as fh:
            cls.text = fh.read()

    def block(self, number):
        body = section(self.text, number)
        self.assertTrue(body.strip(), "section %d missing" % number)
        return body

    def test_controls(self):
        rows = table_by_header(self.block(2), "control")
        doc = {(first_code(r[0]), plain(r[1]), plain(r[2])) for r in rows}
        self.assertEqual(doc, {(c.name, c.wb_type, c.units) for c in C.CONTROLS})

    def test_last_status_values(self):
        rows = table_by_header(self.block(3), "last_status")
        self.assertEqual({first_code(r[0]) for r in rows}, set(C.LAST_STATUSES))

    def test_media(self):
        rows = table_by_header(self.block(5), "Носитель")
        doc = set()
        for r in rows:
            mount = None if r[1].startswith("—") else first_code(r[1])
            doc.add((first_code(r[0]), mount, first_code(r[2]), int(r[3]), int(r[4])))
        code = {(m.name, m.mount_point, m.store_dir, m.floor_bytes // C.MB, m.keep_mb_max)
                for m in C.MEDIA}
        # store_dir of eMMC is env-overridable for tests; the contract names the default.
        code = {(n, mp, "/var/lib/sa02m-audio" if n == C.MEDIUM_EMMC else sd, f, k)
                for n, mp, sd, f, k in code}
        self.assertEqual(doc, code)

    def test_conf_keys_and_defaults(self):
        rows = table_by_header(self.block(8), "Ключ")
        self.assertEqual([first_code(r[0]) for r in rows], list(config.KEYS))
        defaults = config.RecorderConfig()
        for r in rows:
            key = first_code(r[0])
            value = getattr(defaults, key)
            rendered = ("true" if value else "false") if isinstance(value, bool) else str(value)
            # An empty default (upload_url) is written «—»; any other cell
            # must carry the value as `code`.
            doc = "" if r[1].strip() == "—" else first_code(r[1])
            self.assertEqual(doc, rendered, key)

    def test_states_and_status_keys(self):
        block = self.block(9)
        self.assertEqual({first_code(r[0]) for r in table_by_header(block, "Состояние")},
                         set(C.STATES))
        self.assertEqual({first_code(r[0]) for r in table_by_header(block, "Ключ")},
                         set(service.STATUS_KEYS))

    def test_event_kinds_and_keys(self):
        block = self.block(10)
        self.assertEqual({first_code(r[0]) for r in table_by_header(block, "Событие")},
                         set(C.EVENT_KINDS))
        for key in service.EVENT_KEYS:
            self.assertIn("`%s`" % key, block, key)

    def test_panel_actions_and_dispatch_codes(self):
        block = self.block(15)
        actions = {first_code(r[0]) for r in table_by_header(block, "Действие")}
        self.assertEqual(actions, {"status", "download"} | set(api.MUTATIONS))
        codes = {first_code(r[0]) for r in table_by_header(block, "Код")}
        self.assertEqual(codes, set(api.ERROR_CODES))

    def test_trigger_verbs(self):
        verbs = {first_code(r[0]) for r in table_by_header(self.block(16), "Глагол")}
        self.assertEqual(verbs, set(C.TRIGGER_VERBS))

    def test_quoted_constants_match(self):
        quoted = _QUOTED_CONSTANT.findall(self.text)
        self.assertGreaterEqual(len(quoted), 5, quoted)
        for name, value in quoted:
            self.assertTrue(hasattr(C, name), name)
            self.assertEqual(int(getattr(C, name)), int(value.replace(" ", "")), name)


if __name__ == "__main__":
    unittest.main()
