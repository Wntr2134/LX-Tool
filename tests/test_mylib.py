"""The personal head library: save a cracked clone, find it next time."""

from __future__ import annotations

import pytest

from lxtool import library as libmod, matching, mylib
from lxtool.model import Channel, Fixture, Mode


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("LXTOOL_MYHEADS", str(tmp_path / "heads"))
    return tmp_path / "heads"


def _clone() -> Fixture:
    return Fixture(manufacturer="China", model="AuraClone", modes=[Mode(
        name="14ch",
        channels=[Channel(offset=i + 1, name=a, attribute=a) for i, a in enumerate(
            ["Shutter", "Dimmer", "Zoom", "Pan", "Tilt", "Control",
             "ColorWheel", "Red", "Green", "Blue", "White", "CTO"])],
    )])


def test_save_then_list_and_reopen(store):
    saved = mylib.save(_clone())
    assert saved.hed.is_file() and saved.plan.is_file()

    rows = mylib.entries()
    assert [r.model for r in rows] == ["AuraClone"]
    assert rows[0].channels == 12

    plan = mylib.get_plan(saved.stem)
    assert "channel: Pan" in plan
    assert "China" in plan


def test_saved_heads_appear_in_matching(store):
    mylib.save(_clone())
    lib = libmod.load(None, include_ofl=False, include_mine=True)
    mine = [r for r in lib.rows if r.source == "myheads"]
    assert len(mine) == 1

    target = _clone()
    hits = matching.find_in_index(target, target.modes[0], lib.rows, limit=1)
    assert hits and hits[0].fixture.source == "myheads"


def test_remove(store):
    saved = mylib.save(_clone())
    assert mylib.remove(saved.stem)
    assert mylib.entries() == []
    assert not mylib.remove("nothing-here")


def test_saved_head_is_a_valid_hed(store):
    from lxtool.formats import chamsys

    saved = mylib.save(_clone())
    back = chamsys.read(saved.hed)
    assert back.manufacturer == "China"
    assert [c.attribute for c in back.modes[0].channels][:3] == ["Shutter", "Dimmer", "Zoom"]


def test_load_is_isolated_when_disabled(store):
    mylib.save(_clone())
    lib = libmod.load(None, include_ofl=False, include_mine=False)
    assert not any(r.source == "myheads" for r in lib.rows)
