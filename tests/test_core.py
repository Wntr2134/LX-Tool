"""Tests for the parts that must not silently drift: attribute normalisation,
format round-trips, and the matching/change-plan logic."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lxtool import attributes, matching
from lxtool.formats import chamsys, gdtf, ma2, ofl
from lxtool.model import Channel, Fixture, Mode


# --------------------------------------------------------------------------
# attribute normalisation
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Pan", "Pan"),
    ("Pan Fine", "Pan"),
    ("PAN_FINE", "Pan"),
    ("Tilt (16 bit)", "Tilt"),
    ("Dimmer", "Dimmer"),
    ("Master Dimmer", "Dimmer"),
    ("Intensity", "Dimmer"),
    ("ColorAdd_R", "Red"),
    ("ColorRGB1", "Red"),
    ("Red", "Red"),
    ("ColorSub_C", "Cyan"),
    ("Cyan", "Cyan"),
    ("Colour Wheel", "ColorWheel"),
    ("Color2", "ColorWheel2"),
    ("Gobo1", "Gobo1"),
    ("Gobo 1 Rotation", "Gobo1Rot"),
    ("Gobo2 Spin", "Gobo2Rot"),
    ("Prism Rot", "PrismRot"),
    ("Shutter/Strobe", "Shutter"),
    ("Zoom", "Zoom"),
    ("Frost", "Frost"),
])
def test_normalise(raw, expected):
    assert attributes.normalise(raw) == expected


def test_normalise_unknown_stays_unknown():
    # Guessing here would be worse than admitting ignorance.
    assert attributes.normalise("Wibble Flange 7") == "Unknown"
    assert attributes.normalise("") == "Unknown"


@pytest.mark.parametrize("raw", ["Pan Fine", "Tilt LSB", "Dimmer 16 bit", "pan_f"])
def test_is_fine(raw):
    assert attributes.is_fine(raw)


def test_is_fine_negative():
    assert not attributes.is_fine("Pan")
    assert not attributes.is_fine("Definitely Coarse")


def test_criticality_ordering():
    assert attributes.criticality("Dimmer") > attributes.criticality("Pan")
    assert attributes.criticality("Pan") > attributes.criticality("Red")
    assert attributes.criticality("Red") > attributes.criticality("Gobo1")


# --------------------------------------------------------------------------
# ChamSys library
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name,mfr,model,mode,count", [
    ("Laserworld_EL-400RGB_9ch.hed", "Laserworld", "EL-400RGB", "9ch", 9),
    ("China_7x9WMiniParRGB_7ch.hed", "China", "7x9WMiniParRGB", "7ch", 7),
    ("Martin__Standard.hed", "Martin", "", "Standard", None),
    ("SPOT MOVERS.hed", "", "SPOT MOVERS", "", None),
    ("test_test_SPOT MOVERS.hed", "test", "test", "SPOT MOVERS", None),
])
def test_parse_head_filename(name, mfr, model, mode, count):
    h = chamsys.parse_head_filename(Path(name))
    assert (h.manufacturer, h.model, h.mode) == (mfr, model, mode)
    assert h.channel_count == count


def test_extra_underscores_stay_in_mode():
    h = chamsys.parse_head_filename(Path("Robe_Robin 600_Mode 1_16ch.hed"))
    assert h.manufacturer == "Robe"
    assert h.model == "Robin 600"
    assert h.mode == "Mode 1_16ch"
    assert h.channel_count == 16


def test_obfuscation_detection():
    # Encoded bodies are 0x80-0xFF plus literal newlines.
    assert chamsys.looks_obfuscated(bytes([0xA3, 0xDE, 0x0A, 0xB0]))
    assert not chamsys.looks_obfuscated(b"Version, 2\nHead, Foo\n")
    assert not chamsys.looks_obfuscated(b"")


def test_decode_hed_refuses_rather_than_guesses():
    with pytest.raises(chamsys.HedDecodeError):
        chamsys.decode_hed(bytes([0xA3, 0xDE, 0x0A, 0xB0]))
    assert chamsys.decode_hed(b"Head, Foo\n") == "Head, Foo\n"


def test_scan_library(tmp_path):
    for n in ("Robe_Pointe_16ch.hed", "Robe_Pointe_24ch.hed", "Martin_MAC700_20ch.hed"):
        (tmp_path / n).write_bytes(b"\xa3\xde\n")
    (tmp_path / "manufacturer_exceptions.csv").write_text("adj,americandj\nrush,martin\n")

    lib = chamsys.ChamSysLibrary.scan(tmp_path)
    assert len(lib.heads) == 3
    assert lib.aliases["adj"] == "americandj"

    fixtures = lib.as_fixtures()
    # The two Robe heads collapse into one fixture with two modes.
    assert len(fixtures) == 2
    robe = next(f for f in fixtures if f.model == "Pointe")
    assert len(robe.modes) == 2


def test_load_head_map(tmp_path):
    p = tmp_path / "headmapcapture.csv"
    p.write_text(
        "martin mac250m1,Martin,MAC 250,9,Mac250\\#M1\n"
        "abstract ce8,,,4\n"
    )
    rows = chamsys.load_head_map(p)
    assert rows[0]["manufacturer"] == "Martin"
    assert rows[0]["channel_count"] == 9
    assert rows[0]["visualiser"] == "Mac250#M1"
    assert rows[1]["channel_count"] == 4


# --------------------------------------------------------------------------
# GDTF
# --------------------------------------------------------------------------

GDTF_XML = """<?xml version="1.0" encoding="UTF-8"?>
<GDTF DataVersion="1.2">
  <FixtureType Name="Test" LongName="Test Mover" Manufacturer="ACME">
    <DMXModes>
      <DMXMode Name="16ch">
        <DMXChannels>
          <DMXChannel Offset="1,2" Default="0/1" Highlight="255/1" Geometry="Yoke">
            <LogicalChannel Attribute="Pan">
              <ChannelFunction Attribute="Pan" DMXFrom="0/1" Name="Pan"/>
            </LogicalChannel>
          </DMXChannel>
          <DMXChannel Offset="3" Default="255/1" Geometry="Body">
            <LogicalChannel Attribute="Dimmer">
              <ChannelFunction Attribute="Dimmer" DMXFrom="0/1" Name="Dim"/>
            </LogicalChannel>
          </DMXChannel>
          <DMXChannel Offset="" Geometry="Body">
            <LogicalChannel Attribute="Control"/>
          </DMXChannel>
        </DMXChannels>
      </DMXMode>
    </DMXModes>
  </FixtureType>
</GDTF>
"""


def test_gdtf_parse():
    fx = gdtf.parse_description(GDTF_XML)
    assert fx.manufacturer == "ACME"
    assert fx.model == "Test Mover"
    mode = fx.modes[0]

    # 16-bit Pan occupies two slots; the virtual channel has no footprint.
    assert mode.channel_count == 3
    assert len(mode.channels) == 3

    pan, pan_fine = mode.channels[0], mode.channels[1]
    assert (pan.attribute, pan.fine) == ("Pan", False)
    assert (pan_fine.attribute, pan_fine.fine) == ("Pan", True)
    assert pan.highlight == 255

    dim = mode.channels[2]
    assert dim.attribute == "Dimmer" and dim.default == 255 and dim.htp


def test_gdtf_roundtrip(tmp_path):
    original = gdtf.parse_description(GDTF_XML)
    out = gdtf.write(original, tmp_path / "out.gdtf")
    reparsed = gdtf.read(out)

    assert reparsed.manufacturer == original.manufacturer
    assert reparsed.model == original.model
    assert len(reparsed.modes) == len(original.modes)

    a, b = original.modes[0], reparsed.modes[0]
    assert b.channel_count == a.channel_count
    assert [c.attribute for c in b.channels] == [c.attribute for c in a.channels]
    assert [c.fine for c in b.channels] == [c.fine for c in a.channels]


def test_gdtf_dmx_value_normalises_to_8bit():
    assert gdtf._dmx_value("255/1") == 255
    assert gdtf._dmx_value("65535/2") == 255
    assert gdtf._dmx_value("32768/2") == 128
    assert gdtf._dmx_value(None, 7) == 7
    assert gdtf._dmx_value("None", 3) == 3


# --------------------------------------------------------------------------
# Open Fixture Library
# --------------------------------------------------------------------------

OFL_DOC = {
    "name": "MH-X25",
    "categories": ["Moving Head"],
    "availableChannels": {
        "Pan": {"fineChannelAliases": ["Pan fine"], "defaultValue": 128,
                "capabilities": [{"dmxRange": [0, 255], "type": "Pan", "comment": "Pan"}]},
        "Dimmer": {"defaultValue": 0},
        "Colour Wheel": {"capabilities": [
            {"dmxRange": [0, 9], "type": "ColorPreset", "comment": "Open"},
            {"dmxRange": [10, 19], "type": "ColorPreset", "comment": "Red"},
        ]},
    },
    "modes": [{"name": "5-channel", "channels": ["Pan", "Pan fine", "Dimmer", None, "Colour Wheel"]}],
}


def test_ofl_parse():
    fx = ofl.parse(json.dumps(OFL_DOC), manufacturer="eurolite")
    assert fx.manufacturer == "eurolite"
    assert fx.model == "MH-X25"

    mode = fx.modes[0]
    assert mode.channel_count == 5

    assert mode.channels[0].attribute == "Pan"
    assert mode.channels[0].default == 128
    assert mode.channels[1].fine is True and mode.channels[1].attribute == "Pan"
    assert mode.channels[2].attribute == "Dimmer"
    # A null entry still consumes a slot, so later channels keep their offsets.
    assert mode.channels[3].attribute == "Unknown"
    assert mode.channels[4].attribute == "ColorWheel"
    assert mode.channels[4].offset == 5

    ranges = mode.channels[4].ranges
    assert (ranges[0].dmx_from, ranges[0].dmx_to, ranges[0].name) == (0, 9, "Open")


def test_ofl_to_gdtf(tmp_path):
    fx = ofl.parse(json.dumps(OFL_DOC), manufacturer="eurolite")
    out = gdtf.write(fx, tmp_path / "mhx25.gdtf")
    back = gdtf.read(out)
    assert back.modes[0].channel_count == 5
    assert "Pan" in back.modes[0].attribute_set()
    assert "ColorWheel" in back.modes[0].attribute_set()


# --------------------------------------------------------------------------
# grandMA2
# --------------------------------------------------------------------------

MA2_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MA xmlns="http://schemas.malighting.de/grandma2/xml/MA">
  <FixtureType name="Mover" manufacturer="ACME">
    <Modules>
      <Module name="Mode 1">
        <ChannelType attribute="PAN" default="128">
          <coarse dmx_offset="1"/>
          <fine dmx_offset="2"/>
        </ChannelType>
        <ChannelType attribute="DIM">
          <coarse dmx_offset="3"/>
        </ChannelType>
      </Module>
    </Modules>
  </FixtureType>
</MA>
"""


def test_ma2_parse():
    fx = ma2.parse(MA2_XML)
    assert fx.manufacturer == "ACME"
    assert fx.model == "Mover"
    mode = fx.modes[0]
    assert mode.name == "Mode 1"
    assert mode.channel_count == 3
    assert mode.channels[0].attribute == "Pan" and not mode.channels[0].fine
    assert mode.channels[1].attribute == "Pan" and mode.channels[1].fine
    assert mode.channels[2].attribute == "Dimmer"


def test_ma2_roundtrip():
    fx = ma2.parse(MA2_XML)
    back = ma2.parse(ma2.build(fx))
    assert back.model == fx.model
    assert back.modes[0].channel_count == fx.modes[0].channel_count
    assert [c.attribute for c in back.modes[0].channels] == \
           [c.attribute for c in fx.modes[0].channels]


def test_ma2_to_gdtf_bridges_the_desks(tmp_path):
    fx = ma2.parse(MA2_XML)
    out = gdtf.write(fx, tmp_path / "mover.gdtf")
    back = gdtf.read(out)
    assert back.modes[0].attribute_set() == {"Pan", "Dimmer"}


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

def _mode(name: str, attrs: list[str]) -> Mode:
    return Mode(name=name, channels=[
        Channel(offset=i, name=a, attribute=a) for i, a in enumerate(attrs, 1)
    ])


def test_identical_modes_score_perfectly():
    m = _mode("A", ["Dimmer", "Pan", "Tilt"])
    score, edits = matching.compare_modes(m, _mode("B", ["Dimmer", "Pan", "Tilt"]))
    assert score == 1.0
    assert edits == []


def test_swapped_channels_report_a_move():
    target = _mode("t", ["Pan", "Tilt", "Dimmer"])
    cand = _mode("c", ["Dimmer", "Pan", "Tilt"])
    _, edits = matching.compare_modes(target, cand)
    assert any(e.action == "move" and e.attribute == "Dimmer" for e in edits)


def test_missing_channel_reports_add():
    target = _mode("t", ["Dimmer", "Pan", "Tilt", "Zoom"])
    cand = _mode("c", ["Dimmer", "Pan", "Tilt"])
    _, edits = matching.compare_modes(target, cand)
    add = [e for e in edits if e.action == "add"]
    assert len(add) == 1 and add[0].attribute == "Zoom" and add[0].offset == 4


def test_extra_channel_reports_remove():
    target = _mode("t", ["Dimmer", "Pan"])
    cand = _mode("c", ["Dimmer", "Pan", "Gobo1"])
    _, edits = matching.compare_modes(target, cand)
    assert [e.action for e in edits if e.action == "remove"] == ["remove"]


def test_edits_are_ordered_most_critical_first():
    target = _mode("t", ["Gobo1", "Dimmer", "Pan"])
    cand = _mode("c", ["Zoom", "Frost", "Iris"])
    _, edits = matching.compare_modes(target, cand)
    severities = [e.severity for e in edits]
    assert severities == sorted(severities, reverse=True)
    # Dimmer is the most critical thing to fix, so it must lead.
    assert edits[0].attribute == "Dimmer"


def test_footprint_only_comparison_is_capped():
    """A ChamSys head with no channel detail must never claim an exact match."""
    target = _mode("t", ["Dimmer", "Pan", "Tilt"])
    blind = Mode(name="9ch")
    blind.__dict__["_declared_count"] = 3
    score, edits = matching.compare_modes(target, blind)
    assert 0 < score < 1.0
    assert edits == []


def test_find_candidates_ranks_layout_over_name():
    target = Fixture(manufacturer="ACME", model="Mover")
    t_mode = _mode("t", ["Dimmer", "Pan", "Tilt"])

    same_name_wrong_layout = Fixture(
        manufacturer="ACME", model="Mover",
        modes=[_mode("x", ["Zoom", "Iris", "Frost", "Gobo1"])],
    )
    other_name_right_layout = Fixture(
        manufacturer="Generic", model="Thing",
        modes=[_mode("y", ["Dimmer", "Pan", "Tilt"])],
    )

    ranked = matching.find_candidates(
        target, t_mode, [same_name_wrong_layout, other_name_right_layout]
    )
    assert ranked[0].fixture.model == "Thing"


def test_exact_match_flagged():
    target = Fixture(manufacturer="ACME", model="Mover")
    t_mode = _mode("t", ["Dimmer", "Pan"])
    lib = [Fixture(manufacturer="ACME", model="Mover", modes=[_mode("t", ["Dimmer", "Pan"])])]
    best = matching.find_candidates(target, t_mode, lib)[0]
    assert best.exact


def test_name_similarity():
    assert matching.name_similarity("Robe", "robe") == 1.0
    assert matching.name_similarity("Robe", "Robe Lighting") == 0.9
    assert matching.name_similarity("Robe", "Martin") < 0.5
    assert matching.name_similarity("", "Robe") == 0.0
