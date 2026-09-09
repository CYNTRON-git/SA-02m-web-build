"""Scenes marked «в Алису» projected as virtual Alice switches (1.0.6.41).

One home for every reader of the `alice_expose` flag: the catalogue build
(`client/device_registry.py`), the Yandex unit's reload watcher
(`client/reload_watch.py`) and the web card's `full_config()`
(`config/api.py`). Nothing here writes: the projection is built IN MEMORY on
each catalogue build, exactly like the Carel AHU rows (`ahu_status`), so the
stored device document keeps only what the operator saved and the flag keeps
its single writer — the cloud scenario channel.

The device shape (contract docs/contracts/alice-mqtt-mapping.md §Scene
devices): one `on_off` capability bound to the scenario engine's own virtual
device `/devices/sa02m-rules-<sid>/controls/run`, `retrievable`/`reportable`
false with `split: true` — the board cannot know whether «вечер в гостиной»
is currently «on», so the switch never claims a state, and Alice renders two
independent buttons instead of a toggle.

The Yandex id is board-keyed (`scene-<board>-<sid>`): two SA-02m under ONE
Yandex account would otherwise both mint `scene-s1` and the account would
see one device where there are two. The key is the controller serial, a
cloned machine-id being the imaging contract's problem, not ours
(docs/contracts/image-identity-reset.md).

Fail closed everywhere: a row whose id cannot carry a valid Alice id is NOT
exposed rather than truncated (a truncated id could collide with another
scene's), a missing/unreadable rules stack projects nothing, and the flag is
read as `is True` so a hand-edited `"true"` string never exposes a scene.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..common import constants as C
from ..common.config_store import controller_sn
from . import models

log = logging.getLogger("sa02m_alice.config.scene_devices")

#: The on-board scenario engine (opt/sa02m-rules) is a sibling install, not a
#: python dependency of this package — resolved lazily, its absence read as
#: «scenarios unsupported», never as an import-time crash.
RULES_DIR = os.environ.get("SA02M_RULES_DIR", "/opt/sa02m-rules")

#: F1 (Operator, 2026-09-09): a scene answers «Алиса, включи вечер», which is
#: what a switch tile means; `devices.types.other` was the alternative.
SCENE_DEVICE_TYPE = "devices.types.switch"
#: The scenario engine's own virtual device (contract cloud-scenarios.md
#: §MQTT mirror). `run` is its ONE command control; every other control under
#: this prefix is retained state the engine publishes.
RULES_DEVICE_PREFIX = "sa02m-rules-"
VIRTUAL_TOPIC_PREFIX = "/devices/" + RULES_DEVICE_PREFIX
RUN_CONTROL = "run"
SCENE_ID_PREFIX = "scene-"
BOARD_KEY_MAX = 24
BOARD_KEY_FALLBACK = "sa02m"

_BAD_KEY_CHARS = re.compile(r"[^A-Za-z0-9_.:-]")


def rules_store():
    """`sa02m_rules.store`, or None when the rules stack is not installed."""
    try:
        from sa02m_rules import store as _store  # type: ignore
        return _store
    except ImportError:
        pass
    rules_dir = os.environ.get("SA02M_RULES_DIR", RULES_DIR)
    if rules_dir and rules_dir not in sys.path:
        sys.path.insert(0, rules_dir)
    try:
        from sa02m_rules import store as _store  # type: ignore
        return _store
    except ImportError:
        return None


def rules_store_path() -> str:
    """The scenario store this board reads; `""` when there is no rules stack.

    The env override is read at CALL time, not at import: the CGI, the daemon
    and the tests all resolve the same file without a module reload.
    """
    env = (os.environ.get("SA02M_RULES_PATH") or "").strip()
    if env:
        return env
    store = rules_store()
    if store is None:
        return ""
    return str(getattr(store, "DEFAULT_PATH", "") or "")


def load_rules_doc(path: Optional[str] = None) -> Dict[str, Any]:
    """The merged scenario view, or `{}` when there is nothing to read.

    `store.load` answers an empty document for an absent file, so «no
    scenarios» and «no rules stack» look the same to every caller — which is
    the point: an unreadable store must cost the account its PHYSICAL devices
    nothing.
    """
    store = rules_store()
    if store is None:
        return {}
    target = path or rules_store_path()
    if not target:
        return {}
    try:
        return store.load(target)
    except Exception as exc:  # a corrupt store must not take the catalogue down
        log.error("scenario store unreadable, no scenes exposed: %s", exc)
        return {}


def sanitise_board_key(raw: Any) -> str:
    """Controller serial → an id fragment. Empty/garbage ⇒ `BOARD_KEY_FALLBACK`."""
    key = _BAD_KEY_CHARS.sub("", str(raw or ""))[:BOARD_KEY_MAX]
    return key or BOARD_KEY_FALLBACK


def board_key() -> str:
    """This board's id fragment for a scene device — the controller serial."""
    return sanitise_board_key(controller_sn())


def scene_device_id(board: Any, sid: Any) -> Optional[str]:
    """`scene-<board>-<sid>`, or None when it would not be a valid Alice id."""
    if not isinstance(sid, str) or not models.id_ok(sid):
        return None
    did = "%s%s-%s" % (SCENE_ID_PREFIX, sanitise_board_key(board), sid)
    return did if models.id_ok(did) else None


def scene_run_topic(sid: str) -> str:
    """The engine's command topic for one scenario (no `/on` — the registry
    appends that when it publishes an action)."""
    return "%s%s/controls/%s" % (VIRTUAL_TOPIC_PREFIX, sid, RUN_CONTROL)


def is_virtual_scene_topic(topic: str) -> bool:
    """True for the scenario engine's own virtual device topics.

    Used by the registry wherever «the poller never published this» means
    «dead»: a command topic HAS no poller and nothing ever republishes it, so
    a freshness rule written for a Modbus coil would announce every scene
    switch unreachable and then refuse its second command.
    """
    return bool(topic) and topic.startswith(VIRTUAL_TOPIC_PREFIX)


def is_exposed_scene(row: Any) -> bool:
    """The store row earns an Alice device (F3: disabled ⇒ removed — listing a
    scene `run_now` silently ignores would be a lie)."""
    return (
        isinstance(row, dict)
        and row.get("type") == "scene"
        and row.get("alice_expose") is True
        and row.get("enabled") is not False
    )


def _scenario_rows(rules_doc: Any) -> List[Any]:
    if not isinstance(rules_doc, dict):
        return []
    rows = rules_doc.get("scenarios")
    return rows if isinstance(rows, list) else []


def _captured_room(row: Dict[str, Any]) -> Optional[str]:
    """`captured_from.room_id` — the cloud editor's room for a captured scene.

    `group_id` rides the same object and is deliberately NOT read here: it is
    cloud-side provenance of a group capture (contract cloud-scenarios.md).
    """
    captured = row.get("captured_from")
    if not isinstance(captured, dict):
        return None
    rid = captured.get("room_id")
    return rid if isinstance(rid, str) and rid else None


def exposed_scene_devices(
    rules_doc: Any, rooms: Optional[Iterable[Any]], board: Any
) -> List[Dict[str, Any]]:
    """Catalogue rows for every scene marked «в Алису», in document order.

    `rooms` is the DEVICE document's room list — a `captured_from.room_id`
    naming a room that has since been deleted places the switch in no room
    rather than in a phantom one.
    """
    known_rooms = {
        r["id"] for r in (rooms or [])
        if isinstance(r, dict) and isinstance(r.get("id"), str)
    }
    out: List[Dict[str, Any]] = []
    for row in _scenario_rows(rules_doc):
        if not is_exposed_scene(row):
            continue
        sid = row.get("id")
        did = scene_device_id(board, sid)
        if did is None:
            log.warning("scenario %r cannot carry an Alice device id — "
                        "not exposed", sid)
            continue
        dev: Dict[str, Any] = {
            "id": did,
            "scene_id": sid,
            "name": str(row.get("name") or sid),
            "type": SCENE_DEVICE_TYPE,
            "capabilities": [{
                "type": "devices.capabilities.on_off",
                "mqtt": scene_run_topic(sid),
                # The board has no idea whether a scene is «on»: it fired
                # once and the room may have been changed by hand since.
                # `retrievable:false` + `split:true` is the honest pair —
                # Yandex renders «включить» / «выключить», not a toggle
                # whose position we would have to invent.
                "retrievable": False,
                "reportable": False,
                "parameters": {"split": True},
            }],
            "properties": [],
        }
        rid = _captured_room(row)
        if rid in known_rooms:
            dev["room_id"] = rid
        out.append(dev)
    return out


def attach_exposed_scenes(doc: Dict[str, Any], profile: str) -> Dict[str, Any]:
    """`doc` plus the exposed scenes — a COPY, or `doc` itself when there is
    nothing to add. The `ahu_status.prepare_catalogue_doc` shape, same reason:
    the caller's document (a test fixture, or what `load_devices` just
    returned) is never mutated and `save_devices` is never called.

    Yandex profile only (F4): the cloud page already has scenarios
    first-class, so a second tile there would duplicate its own list.
    """
    if not isinstance(doc, dict) or profile != C.PROFILE_YANDEX:
        return doc
    rows = exposed_scene_devices(load_rules_doc(), doc.get("rooms"), board_key())
    if not rows:
        return doc
    out = copy.deepcopy(doc)
    devices = out.get("devices")
    if not isinstance(devices, list):
        devices = []
        out["devices"] = devices
    # A saved device that already owns one of these ids wins: the stored
    # document is the operator's, and an id clash must not silently replace
    # what they bound.
    taken = {d.get("id") for d in devices if isinstance(d, dict)}
    devices.extend(row for row in rows if row["id"] not in taken)
    return out


def web_scene_rows(devices_doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The «Умный дом» card's read-only rows — the SAME projection the Alice
    catalogue is built from, so the page and the account cannot disagree.

    `enabled` is always True here by construction (a disabled scene is not
    exposed); it rides along so the card can render the state without a
    second read if that ever changes.
    """
    rooms = devices_doc.get("rooms") if isinstance(devices_doc, dict) else None
    return [
        {"id": row["id"], "scene_id": row["scene_id"], "name": row["name"],
         "room_id": row.get("room_id", ""), "enabled": True}
        for row in exposed_scene_devices(load_rules_doc(), rooms, board_key())
    ]


def exposure_fingerprint(rules_doc: Any) -> Tuple[Tuple[Any, ...], ...]:
    """What the projection above depends on, in projection order.

    The watcher rebuilds the catalogue only when THIS changes, so the
    engine's own writes cost nothing: run records live in the sibling journal
    (`runs.json`, 1.0.6.41) and a `mode` action rewrites `vars`, neither of
    which appears here. Order is the document's, not sorted: the projection
    IS ordered, so a reorder is a change — fail-safe to one extra reload.
    """
    return tuple(
        (row.get("id"), row.get("name"), row.get("enabled") is not False,
         _captured_room(row))
        for row in _scenario_rows(rules_doc)
        if is_exposed_scene(row)
    )
