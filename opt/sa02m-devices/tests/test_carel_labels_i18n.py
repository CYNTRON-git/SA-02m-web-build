"""Every Carel chart label the devices API emits has an EN entry in i18n.js.

`CAREL_METRIC_META` (device_history_db) is the ONE home of the Russian labels
the `/api/devices/history?kind=carel` response carries as `label`; devices.js
renders them as text nodes (metric chips, tooltip rows), where the i18n
runtime's MutationObserver translates a text node only when the WHOLE string
is a DICT key. A label with no DICT entry therefore renders Russian in the EN
UI (ship review 1.0.6.39, advisory; widened by the four state metrics). The
label set lives in Python and the dictionary in JS, so neither side's own
gate sees the seam — this case reads both.

Non-vacuous: the meta must carry the known metric set, the DICT parse must see
a known entry and a realistic size, and every label is asserted individually.
Parsing: DICT keys are single-quoted string literals immediately followed by
`:` inside the `const DICT = { ... };` object; the runtime is not executed here
(i18n-dict-contract does that for the uiT()/markup halves).
"""

from __future__ import annotations

import re
from pathlib import Path

from sa02m_devices.device_history_db import CAREL_METRIC_META

_REPO = Path(__file__).resolve().parents[3]
_I18N = _REPO / "www" / "network_config" / "static" / "js" / "i18n.js"


def _dict_keys(src: str) -> set[str]:
    start = src.index("const DICT = {")
    end = src.index("\n  };", start)
    body = src[start:end]
    keys = set()
    for m in re.finditer(r"^\s*'((?:[^'\\]|\\.)*)'\s*:", body, flags=re.M):
        keys.add(m.group(1).replace("\\'", "'"))
    return keys


def test_every_carel_metric_label_has_a_dict_entry() -> None:
    labels = [meta[0] for meta in CAREL_METRIC_META.values()]
    # Non-vacuity on the producer side: the 9 continuous + 4 state metrics.
    assert len(labels) >= 13, labels
    assert {"Приток", "Авария", "Установка вкл."} <= set(labels)

    keys = _dict_keys(_I18N.read_text(encoding="utf-8"))
    # Non-vacuity on the consumer side: the parse saw the real dictionary.
    assert len(keys) > 500, len(keys)
    assert "Сеть" in keys

    missing = [lab for lab in labels if lab not in keys]
    assert not missing, f"Carel labels with no EN entry in i18n.js DICT: {missing}"
