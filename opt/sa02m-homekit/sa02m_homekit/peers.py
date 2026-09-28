"""The exact surface the bridge uses from its sibling packages, and the probe
that checks an installed tree provides it (docs/contracts/homekit-bridge.md §1).

`sa02m_alice` and `sa02m_rules` are sibling installs, not venv dependencies: a
www-only update or an older full install can leave them a release behind the
bridge (bench 1.135, 2026-09-28 — the daemon crash-looped on an absent
`DeviceRegistry.catalogue_items`). Two consumers read the ONE list below: the
daemon at start-up (`peer_package_outdated`, main.py) and scripts/06c-homekit.sh
before it touches the board (`cli`). tests/test_peers.py derives the list from
the package's own imports, so a new peer symbol that is not listed goes RED.

Spec grammar: `module:attr[.attr…][(keyword, …)]` — the keywords are
parameters the call site passes by name (a signature, not just a name).

Stdlib only: 06c runs this before the venv exists.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import re
import sys
from typing import Iterable, List, Optional, Sequence, Tuple

# Refusing to start is right for these: the bridge cannot run without them.
REQUIRED_PEER_SYMBOLS: Tuple[str, ...] = (
    "sa02m_alice.client.converters:BOOL_EVENT_VALUES",
    "sa02m_alice.client.device_registry:DeviceRegistry(profile)",
    "sa02m_alice.client.device_registry:DeviceRegistry.apply_actions",
    "sa02m_alice.client.device_registry:DeviceRegistry.catalogue_items",
    "sa02m_alice.client.device_registry:DeviceRegistry.note_mqtt",
    "sa02m_alice.client.device_registry:DeviceRegistry.query_devices",
    "sa02m_alice.client.device_registry:DeviceRegistry.reload",
    "sa02m_alice.client.device_registry:DeviceRegistry.subscribe_topics",
    "sa02m_alice.client.reload_watch:DevicesWatcher",
    "sa02m_alice.client.reload_watch:DevicesWatcher.changed",
    "sa02m_alice.client.reload_watch:RulesExposureWatcher(fingerprint)",
    "sa02m_alice.client.reload_watch:RulesExposureWatcher.changed",
    "sa02m_alice.common.config_store:load_devices",
    "sa02m_alice.common.constants:DEVICES_CONF",
    "sa02m_alice.common.constants:ERR_DEVICE_UNREACHABLE",
    # The registry's "homekit" catalogue profile (scene rows from
    # `homekit_scenes`) arrived with this constant; the bridge passes its own
    # equal string (constants.CATALOGUE_PROFILE), so the constant is the marker.
    "sa02m_alice.common.constants:PROFILE_HOMEKIT",
    "sa02m_alice.common.constants:STATUS_DONE",
    "sa02m_alice.config.scene_devices:homekit_exposure_fingerprint",
    "sa02m_alice.config.scene_devices:homekit_scene_state",
)

# Read lazily by sa02m_alice.config.scene_devices; an absent rules stack means
# «no scenes», never a refusal (§1). Checked only where the package imports.
OPTIONAL_PEER_SYMBOLS: Tuple[str, ...] = (
    "sa02m_rules.store:DEFAULT_PATH",
    "sa02m_rules.store:load",
)

_SPEC_RE = re.compile(
    r"^(?P<module>[A-Za-z_][\w.]*):(?P<attr>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)"
    r"(?:\((?P<kw>[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\))?$"
)

_ABSENT = object()

# cli exit codes (scripts/06c-homekit.sh branches on them).
RC_OK = 0
RC_BAD_LIST = 2
RC_OUTDATED = 10
RC_OPTIONAL_OUTDATED = 11
RC_BROKEN = 12


def parse_spec(spec: str) -> Tuple[str, List[str], List[str]]:
    """`module:a.b(k1, k2)` → ("module", ["a", "b"], ["k1", "k2"])."""
    m = _SPEC_RE.match(spec)
    if not m:
        raise ValueError("malformed peer symbol spec %r" % spec)
    kws = [k.strip() for k in (m.group("kw") or "").split(",") if k.strip()]
    return m.group("module"), m.group("attr").split("."), kws


def _root(spec: str) -> str:
    return parse_spec(spec)[0].split(".", 1)[0]


def _outdated_import(exc: ImportError, root: str) -> bool:
    """An import that failed on the PEER itself (a submodule or a name it does
    not have yet) — as opposed to the peer being absent, or a peer module
    failing on some other dependency (that is `missing_deps`, not «outdated»)."""
    name = getattr(exc, "name", None) or ""
    return name.startswith(root + ".")


def _accepts(obj: object, keyword: str) -> bool:
    try:
        params = inspect.signature(obj).parameters  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if keyword in params:
        return params[keyword].kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                        inspect.Parameter.KEYWORD_ONLY)
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def probe(specs: Sequence[str]) -> Tuple[List[str], List[str]]:
    """(outdated, broken) for `specs`.

    outdated — the peer package imports, but a listed submodule, attribute or
    keyword is absent: refreshing that package fixes it.
    broken — the peer package is absent, or a module raised on some other
    dependency: `main.missing_dependencies` names that one.

    An empty list raises ValueError: a probe that checks nothing proves
    nothing, and must never read as «all present».
    """
    if not specs:
        raise ValueError("empty peer symbol list")
    parsed = [(spec,) + parse_spec(spec) for spec in specs]
    outdated: List[str] = []
    broken: List[str] = []
    for spec, module, attrs, kws in parsed:
        root = module.split(".", 1)[0]
        try:
            obj: object = importlib.import_module(module)
        except (KeyboardInterrupt, SystemExit):
            raise
        except ImportError as exc:
            if _outdated_import(exc, root):
                outdated.append(spec)
            else:
                broken.append("%s (%s: %s)" % (spec, type(exc).__name__, exc))
            continue
        except BaseException as exc:
            broken.append("%s (%s: %s)" % (spec, type(exc).__name__, exc))
            continue
        for attr in attrs:
            obj = getattr(obj, attr, _ABSENT)
            if obj is _ABSENT:
                break
        if obj is _ABSENT or any(not _accepts(obj, kw) for kw in kws):
            outdated.append(spec)
    return outdated, broken


def missing_required() -> List[str]:
    """The daemon's start-up check: the outdated REQUIRED symbols. A broken
    peer is left to `missing_dependencies` (it names the real culprit)."""
    return probe(REQUIRED_PEER_SYMBOLS)[0]


def _package_importable(root: str) -> bool:
    try:
        importlib.import_module(root)
    except ImportError:
        return False
    return True


def cli(argv: Optional[Iterable[str]] = None,
        required: Sequence[str] = REQUIRED_PEER_SYMBOLS,
        optional: Sequence[str] = OPTIONAL_PEER_SYMBOLS) -> int:
    """scripts/06c-homekit.sh's probe. One line per finding on stdout:
    `outdated <spec>`, `outdated-optional <spec>`, `broken <spec> (…)`.
    Exit: 0 all present · 10 a required symbol is outdated · 12 a required
    peer is broken (nothing outdated) · 11 only an optional one is outdated ·
    2 the list itself is empty or malformed."""
    ap = argparse.ArgumentParser(prog="sa02m_homekit.peers")
    ap.add_argument("--rules-dir", default="",
                    help="where sa02m_rules lives (scene_devices.RULES_DIR on the board)")
    args = ap.parse_args(list(argv) if argv is not None else None)
    try:
        outdated, broken = probe(required)
        for spec in optional:
            parse_spec(spec)
    except ValueError as exc:
        print("FAIL %s" % exc)
        return RC_BAD_LIST
    if args.rules_dir and args.rules_dir not in sys.path:
        # The same resolution scene_devices.rules_store() makes at run time.
        sys.path.insert(0, args.rules_dir)
    present = [s for s in optional if _package_importable(_root(s))]
    opt_outdated = probe(present)[0] if present else []
    for spec in outdated:
        print("outdated %s" % spec)
    for spec in broken:
        print("broken %s" % spec)
    for spec in opt_outdated:
        print("outdated-optional %s" % spec)
    if outdated:
        return RC_OUTDATED
    if broken:
        return RC_BROKEN
    if opt_outdated:
        return RC_OPTIONAL_OUTDATED
    return RC_OK


if __name__ == "__main__":
    sys.exit(cli())
