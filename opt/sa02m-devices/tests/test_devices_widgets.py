"""Тесты конфига виджетов ДТВ/СЭ."""

from __future__ import annotations

from pathlib import Path

from sa02m_devices.devices_widgets import (
    add_widget,
    apply_widgets_view,
    filter_for_archive,
    load,
    remove_widget,
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
