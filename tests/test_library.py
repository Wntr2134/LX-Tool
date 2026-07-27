"""Multi-library loading, the two-stage ranker, and OFL value parsing."""

from __future__ import annotations

import json

import pytest

from lxtool import index as index_mod
from lxtool import library as libmod
from lxtool import matching
from lxtool.formats import chamsys, gdtf, ofl
from lxtool.model import Channel, Fixture, Mode


def _fixture(mfr, model, attrs, source="gdtf"):
    return Fixture(
        manufacturer=mfr, model=model, source=source,
        modes=[Mode(name="M", channels=[
            Channel(offset=i, name=a, attribute=a) for i, a in enumerate(attrs, 1)
        ])],
    )


# --------------------------------------------------------------------------
# source labelling
# --------------------------------------------------------------------------

@pytest.mark.parametrize("source,label", [
    ("chamsys", "ChamSys"), ("ma3", "grandMA3"), ("ofl", "Open Fixture Library"),
    ("gdtf", "GDTF"), ("", "unknown"), ("weird", "weird"),
])
def test_label_for(source, label):
    assert libmod.label_for(source) == label


def test_counts_group_by_source():
    lib = libmod.Library(rows=index_mod.build([
        _fixture("A", "1", ["Dimmer"], source="chamsys"),
        _fixture("B", "2", ["Dimmer"], source="chamsys"),
        _fixture("C", "3", ["Dimmer"], source="ma3"),
    ]))
    assert lib.counts() == {"ChamSys": 2, "grandMA3": 1}
    assert len(lib) == 3
    assert lib.modes == 3


# --------------------------------------------------------------------------
# loading folders
# --------------------------------------------------------------------------

def test_load_gdtf_folder(tmp_path):
    gdtf.write(_fixture("Robe", "Pointe", ["Dimmer", "Pan", "Tilt"]), tmp_path / "a.gdtf")
    gdtf.write(_fixture("Martin", "MAC", ["Dimmer", "Cyan"]), tmp_path / "b.gdtf")

    lib = libmod.load([tmp_path])
    assert len(lib) == 2
    assert lib.counts() == {"GDTF": 2}
    assert lib.errors == []


def test_load_chamsys_folder(tmp_path):
    chamsys.write(_fixture("Robe", "Pointe", ["Dimmer", "Pan", "Tilt"]),
                  tmp_path / "Robe_Pointe_3ch.hed")
    lib = libmod.load([tmp_path])
    assert len(lib) == 1
    assert lib.counts() == {"ChamSys": 1}


def test_one_bad_file_does_not_sink_the_folder(tmp_path):
    gdtf.write(_fixture("Robe", "Pointe", ["Dimmer"]), tmp_path / "good.gdtf")
    (tmp_path / "broken.gdtf").write_bytes(b"not a zip at all")

    lib = libmod.load([tmp_path])
    assert len(lib) == 1                      # the good one still loads
    assert any("broken.gdtf" in e for e in lib.errors)


def test_missing_folder_is_reported_not_raised(tmp_path):
    lib = libmod.load([tmp_path / "nope"])
    assert len(lib) == 0
    assert any("not a directory" in e for e in lib.errors)


def test_load_several_folders_keeps_sources_distinct(tmp_path):
    a = tmp_path / "gdtfs"; a.mkdir()
    b = tmp_path / "heads"; b.mkdir()
    gdtf.write(_fixture("Robe", "Pointe", ["Dimmer"]), a / "x.gdtf")
    chamsys.write(_fixture("Martin", "MAC", ["Dimmer", "Pan"]), b / "Martin_MAC_2ch.hed")

    lib = libmod.load([a, b])
    assert lib.counts() == {"ChamSys": 1, "GDTF": 1}


def test_ofl_not_cached_is_reported(tmp_path):
    lib = libmod.load([], include_ofl=True, cache=tmp_path / "empty")
    assert any("lx fetch" in e for e in lib.errors)


# --------------------------------------------------------------------------
# two-stage ranking
# --------------------------------------------------------------------------

def _mode(attrs):
    return Mode(name="m", channels=[
        Channel(offset=i, name=a, attribute=a) for i, a in enumerate(attrs, 1)
    ])


def test_small_library_is_unaffected_by_the_pool():
    """Below the pool size, results must be identical to scoring everything."""
    target = Fixture(manufacturer="ACME", model="Mover")
    t_mode = _mode(["Dimmer", "Pan", "Tilt"])
    lib = [
        _fixture("ACME", "Mover", ["Dimmer", "Pan", "Tilt"]),
        _fixture("Other", "Thing", ["Zoom", "Iris"]),
    ]
    big = matching.find_candidates(target, t_mode, lib, pool=10_000)
    small = matching.find_candidates(target, t_mode, lib, pool=1)
    assert big[0].label == small[0].label


def test_prefilter_keeps_the_right_answer_in_a_large_library():
    """The cheap pass must not discard the obvious match."""
    target = Fixture(manufacturer="Martin", model="MAC 700 Wash")
    t_mode = _mode(["Dimmer", "Pan", "Tilt", "Cyan", "Magenta", "Yellow"])

    # One correct answer buried in a pile of unrelated fixtures.
    lib = [_fixture(f"Brand{i}", f"Model{i}", ["Zoom", "Iris", "Frost"])
           for i in range(2000)]
    lib.append(_fixture("Martin", "MAC 700 Wash",
                        ["Dimmer", "Pan", "Tilt", "Cyan", "Magenta", "Yellow"]))

    best = matching.find_candidates(target, t_mode, lib, limit=1, pool=200)
    assert best[0].fixture.model == "MAC 700 Wash"
    assert best[0].score > 0.9


def test_cheap_score_prefers_size_and_colour_agreement():
    target = Fixture(manufacturer="X", model="Y")
    t = _mode(["Dimmer", "Red", "Green", "Blue"])
    same = _fixture("X", "Y", ["Dimmer", "Red", "Green", "Blue"])
    wrong = _fixture("Q", "Z", ["Cyan", "Magenta", "Yellow", "Zoom", "Iris", "Frost", "Gobo1"])
    assert (matching._cheap_score(target, t, same, same.modes[0])
            > matching._cheap_score(target, t, wrong, wrong.modes[0]))


# --------------------------------------------------------------------------
# OFL value parsing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("50%", 128), ("0%", 0), ("100%", 255),
    (128, 128), ("200", 200), (300, 255), (-5, 0),
    ("", 0), (None, 0), ("nonsense", 0), (True, 0),
])
def test_ofl_dmx_default(raw, expected):
    assert ofl.dmx_default(raw) == expected


def test_ofl_percentage_default_does_not_break_parsing():
    """A percentage default is legal OFL and used to abort the whole load."""
    doc = {
        "name": "Thing",
        "manufacturerKey": "acme",
        "availableChannels": {"Dimmer": {"defaultValue": "50%",
                                         "highlightValue": "100%"}},
        "modes": [{"name": "1ch", "channels": ["Dimmer"]}],
    }
    fx = ofl.parse(json.dumps(doc))
    assert fx.modes[0].channels[0].default == 128
    assert fx.modes[0].channels[0].highlight == 255


# --------------------------------------------------------------------------
# duplicate detection
# --------------------------------------------------------------------------

def _multi(mfr, model, modes):
    """A fixture with several named modes."""
    return Fixture(manufacturer=mfr, model=model, source="chamsys", modes=[
        Mode(name=name, channels=[
            Channel(offset=i, name=a, attribute=a) for i, a in enumerate(attrs, 1)
        ]) for name, attrs in modes
    ])


RGBW = ["Dimmer", "Red", "Green", "Blue", "White"]


def test_signature_ignores_names_but_not_order():
    a = Mode(name="a", channels=[Channel(offset=i, name=x, attribute=x)
                                 for i, x in enumerate(RGBW, 1)])
    b = Mode(name="totally different", channels=[Channel(offset=i, name="zz", attribute=x)
                                                 for i, x in enumerate(RGBW, 1)])
    c = Mode(name="c", channels=[Channel(offset=i, name=x, attribute=x)
                                 for i, x in enumerate(reversed(RGBW), 1)])
    assert libmod.signature(a) == libmod.signature(b)
    assert libmod.signature(a) != libmod.signature(c)


def test_effect_modes_are_not_called_duplicates():
    """One fixture's effect modes share a layout but are genuinely distinct.

    Reporting these as redundant would invite deleting working modes.
    """
    fx = _multi("Aputure", "MCPro", [
        ("M21 Fire", RGBW), ("M21 Strobe", RGBW), ("M21 TV", RGBW),
    ])
    groups = libmod.find_duplicates(index_mod.build([fx]))
    assert len(groups) == 1
    assert groups[0].size == 3
    assert groups[0].redundant() is False
    assert groups[0].interchangeable() is False


def test_same_fixture_and_mode_twice_is_a_duplicate():
    """The same head stored under two filenames is real clutter."""
    a = _multi("Robe", "Pointe", [("16ch", RGBW)])
    b = _multi("Robe", "Pointe", [("16ch", RGBW)])
    groups = libmod.find_duplicates(index_mod.build([a, b]))
    assert groups[0].redundant() is True


def test_different_fixtures_same_layout_are_interchangeable():
    a = _multi("Robe", "Pointe", [("16ch", RGBW)])
    b = _multi("Martin", "MAC", [("Mode 1", RGBW)])
    groups = libmod.find_duplicates(index_mod.build([a, b]))
    assert groups[0].interchangeable() is True
    assert groups[0].redundant() is False
    assert sorted(groups[0].names) == ["Martin MAC [Mode 1]", "Robe Pointe [16ch]"]


def test_tiny_modes_are_skipped():
    """A 3-channel RGB par is identical across hundreds of fixtures."""
    a = _multi("A", "1", [("rgb", ["Red", "Green", "Blue"])])
    b = _multi("B", "2", [("rgb", ["Red", "Green", "Blue"])])
    assert libmod.find_duplicates(index_mod.build([a, b])) == []
    assert len(libmod.find_duplicates(index_mod.build([a, b]), min_channels=3)) == 1


def test_unique_layouts_are_not_reported():
    a = _multi("A", "1", [("m", RGBW)])
    b = _multi("B", "2", [("m", RGBW + ["Pan", "Tilt"])])
    assert libmod.find_duplicates(index_mod.build([a, b])) == []


def test_groups_are_ordered_by_size():
    fixtures = [_multi(f"M{i}", f"X{i}", [("m", RGBW)]) for i in range(5)]
    fixtures += [_multi("Q", "Y", [("m", RGBW + ["Zoom"])]),
                 _multi("R", "Z", [("m", RGBW + ["Zoom"])])]
    groups = libmod.find_duplicates(index_mod.build(fixtures))
    assert [g.size for g in groups] == [5, 2]


# --------------------------------------------------------------------------
# the compact index
# --------------------------------------------------------------------------

def test_index_round_trips_a_mode(tmp_path):
    fx = _fixture("Robe", "Robin 600", ["Dimmer", "Pan", "Tilt", "Zoom"])
    fx.modes[0].channels[1].fine = True
    rows = index_mod.build([fx])

    index_mod.save(rows, tmp_path / "i.index")
    back = index_mod.load(tmp_path / "i.index")

    assert len(back) == 1
    row = back[0]
    assert row.key == "Robe Robin 600"
    assert row.footprint == 4
    assert row.attributes() == ["Dimmer", "Pan", "Tilt", "Zoom"]
    assert row.signature() == rows[0].signature()

    mode = row.to_mode()
    assert [c.attribute for c in mode.channels] == ["Dimmer", "Pan", "Tilt", "Zoom"]
    assert mode.channels[1].fine is True
    assert [c.offset for c in mode.channels] == [1, 2, 3, 4]


def test_index_ranking_matches_object_ranking():
    """The whole optimisation is void if it changes an answer."""
    target = Fixture(manufacturer="Martin", model="MAC 700")
    t_mode = _mode(["Dimmer", "Pan", "Tilt", "Cyan", "Magenta", "Yellow"])

    lib = [
        _fixture("Martin", "MAC 700", ["Dimmer", "Pan", "Tilt", "Cyan", "Magenta", "Yellow"]),
        _fixture("Martin", "MAC 600", ["Dimmer", "Pan", "Tilt", "Cyan", "Magenta"]),
        _fixture("Robe", "Pointe", ["Dimmer", "Red", "Green", "Blue"]),
        _fixture("Other", "Thing", ["Zoom", "Iris", "Frost", "Gobo1", "Prism"]),
    ]

    by_object = matching.find_candidates(target, t_mode, lib, limit=4)
    by_index = matching.find_candidates(target, t_mode, index_mod.build(lib), limit=4)

    assert [(m.score, m.label) for m in by_object] == [(m.score, m.label) for m in by_index]
    assert [str(e) for e in by_object[0].edits] == [str(e) for e in by_index[0].edits]


def test_index_rejects_a_stale_format(tmp_path):
    import pickle

    path = tmp_path / "old.index"
    with path.open("wb") as fh:
        pickle.dump({"version": index_mod.FORMAT_VERSION + 99, "rows": []}, fh)
    assert index_mod.load(path) is None       # rebuilt rather than misread


def test_index_handles_a_missing_or_corrupt_file(tmp_path):
    assert index_mod.load(tmp_path / "absent.index") is None
    bad = tmp_path / "bad.index"
    bad.write_bytes(b"not a pickle")
    assert index_mod.load(bad) is None


def test_index_survives_a_separator_in_a_channel_name(tmp_path):
    """Packing is separator-delimited, so a name containing one must not corrupt it."""
    fx = _fixture("ACME", "Thing", ["Dimmer", "Pan"])
    fx.modes[0].channels[0].name = "Dim|mer"
    row = index_mod.build([fx])[0]
    mode = row.to_mode()
    assert len(mode.channels) == 2
    assert mode.channels[0].attribute == "Dimmer"
