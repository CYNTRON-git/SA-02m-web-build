"""Тесты конфига виджетов ДТВ/СЭ."""

from __future__ import annotations

from pathlib import Path

from sa02m_devices.devices_widgets import (
    add_widget,
    apply_widgets_view,
    filter_for_archive,
    load,
    remove_widget,
    save,
)


def test_remove_hides_and_stops_archive(tmp_path: Path):
    cfg = tmp_path / "widgets.json"
    snap = {
        "ok": True,
        "dtv": [{"id": "dtv-A", "kind": "dtv", "label": "ДТВ A", "ok": True}],
        "ce": [{"id": "ce-B", "kind": "ce", "label": "СЭ B", "ok": True}],
    }
    r = remove_widget("dtv-A", device=snap["dtv"][0], path=cfg)
    assert r["ok"] and "dtv-A" in r["removed_ids"]
    view = apply_widgets_view(snap, path=cfg)
    assert [d["id"] for d in view["dtv"]] == []
    assert [d["id"] for d in view["ce"]] == ["ce-B"]
    assert len(view["available"]) == 1
    assert view["available"][0]["id"] == "dtv-A"
    arch = filter_for_archive(snap, path=cfg)
    assert arch["dtv"] == []
    assert len(arch["ce"]) == 1


def test_mr_passes_through_view_and_devices(tmp_path: Path):
    """MR-02m analog cards are display-only: apply_widgets_view keeps them in
    mr[] and in the rebuilt flat devices[], and removing a ДТВ never drops MR."""
    cfg = tmp_path / "widgets.json"
    snap = {
        "ok": True,
        "dtv": [{"id": "dtv-A", "kind": "dtv", "label": "ДТВ A", "ok": True}],
        "ce": [],
        "mr": [{"id": "mr02m-COM4-12", "kind": "mr", "module_type": "12AI", "ok": True}],
    }
    remove_widget("dtv-A", device=snap["dtv"][0], path=cfg)
    view = apply_widgets_view(snap, path=cfg)
    assert [d["id"] for d in view["dtv"]] == []  # removed
    assert [d["id"] for d in view["mr"]] == ["mr02m-COM4-12"]  # untouched
    # flat devices[] = carel + dtv(filtered) + ce + mr → only the MR survives here
    assert [d["id"] for d in view["devices"]] == ["mr02m-COM4-12"]


def test_carel_is_display_only_like_mr(tmp_path: Path):
    """Operator decision F2 (2026-09-03), restored by audit E10: Carel cards are
    NOT removable in v1 — removal would also stop the archive, the opposite of
    what was asked for. remove_widget refuses a Carel id; the view and the
    archive filter pass Carel through untouched, like MR. Docstring changed by
    decision (1.0.6.35–38 shipped them removable)."""
    cfg = tmp_path / "widgets.json"
    snap = {
        "ok": True,
        "dtv": [],
        "ce": [],
        "mr": [{"id": "mr02m-COM4-12", "kind": "mr", "ok": True}],
        "carel": [{
            "id": "carel-COM3-1",
            "kind": "carel",
            "label": "Carel c.pCOmini № 1 порт 3",
            "ok": True,
        }],
    }
    r = remove_widget("carel-COM3-1", device=snap["carel"][0], path=cfg)
    assert r["ok"] is False and "Carel" in r["error"]
    assert load(path=cfg)["removed_ids"] == []
    # An id-only call (no device dict) is refused on the prefix too.
    assert remove_widget("carel-COM3-2", path=cfg)["ok"] is False
    view = apply_widgets_view(snap, path=cfg)
    assert [d["id"] for d in view["carel"]] == ["carel-COM3-1"]
    # AHU cards first in the flat list (F5 / E12), then MR.
    assert [d["id"] for d in view["devices"]] == ["carel-COM3-1", "mr02m-COM4-12"]
    assert view["available"] == []
    arch = filter_for_archive(snap, path=cfg)
    assert [d["id"] for d in arch["carel"]] == ["carel-COM3-1"]


def test_a_stale_removed_carel_id_from_an_older_config_is_ignored(tmp_path: Path):
    """A widgets.json written by 1.0.6.35–38 may still list a Carel id as
    removed: the card and its archive come back without a config edit, and
    the id is not offered under «available» (there is nothing to re-add)."""
    cfg = tmp_path / "widgets.json"
    save({"version": 1, "removed_ids": ["carel-COM3-1", "dtv-A"], "catalog": {}}, path=cfg)
    snap = {
        "ok": True,
        "dtv": [{"id": "dtv-A", "kind": "dtv", "ok": True}],
        "ce": [],
        "mr": [],
        "carel": [{"id": "carel-COM3-1", "kind": "carel", "ok": True}],
    }
    view = apply_widgets_view(snap, path=cfg)
    assert [d["id"] for d in view["carel"]] == ["carel-COM3-1"]
    assert view["dtv"] == []
    assert [a["id"] for a in view["available"]] == ["dtv-A"]
    arch = filter_for_archive(snap, path=cfg)
    assert [d["id"] for d in arch["carel"]] == ["carel-COM3-1"]
    assert arch["dtv"] == []


def test_add_restores_widget(tmp_path: Path):
    cfg = tmp_path / "widgets.json"
    snap = {
        "ok": True,
        "dtv": [{"id": "dtv-A", "kind": "dtv", "label": "ДТВ A", "ok": True}],
        "ce": [],
    }
    remove_widget("dtv-A", device=snap["dtv"][0], path=cfg)
    add_widget("dtv-A", path=cfg)
    view = apply_widgets_view(snap, path=cfg)
    assert [d["id"] for d in view["dtv"]] == ["dtv-A"]
    assert view["available"] == []
    assert load(path=cfg)["removed_ids"] == []
