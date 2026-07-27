"""Tests for the parts that must not silently drift: attribute normalisation,
format round-trips, and the matching/change-plan logic."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lxtool import attributes, matching
from lxtool.formats import chamsys, gdtf, ma2, ma3, ofl
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
    ("Beamshaper", "Beamshaper"),
    ("Beam Shaper", "Beamshaper"),
    ("Framing", "Framing"),
    ("Blade 1A", "Framing"),
    ("Barndoor", "Framing"),
    ("Shaper Rot", "FramingRot"),
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


DATA = Path(__file__).parent / "data"
REAL_HEAD = DATA / "test_test_AAAAAAAA.hed"


def test_plain_text_head_passes_through():
    assert chamsys.decode_hed(b"# MagicQ personality file.\n") == "# MagicQ personality file.\n"


def test_decode_real_head_file():
    """Decoding a genuine MagicQ personality, produced by the Head Editor."""
    text = chamsys.decode_hed(REAL_HEAD.read_bytes())
    assert text.startswith("# MagicQ personality file.")
    assert "www.chamsys.co.uk" in text
    assert 'V,0099,"MagicQ 1";' in text
    # 20 channels, all named AAAAAAAA, all "Reserved (63)" = 0x3f
    assert text.count('"AAAAAAAA",00000012,0000003f,') == 20
    # The decode must be exact: any residual key error shows up as non-ASCII.
    assert all(32 <= ord(c) < 127 or c == "\n" for c in text)


def test_encode_is_the_inverse_of_decode():
    original = REAL_HEAD.read_bytes()
    assert chamsys.encode_hed(chamsys.decode_hed(original)) == original


@pytest.mark.parametrize("text", [
    "A",
    "AAAAAAAA",
    "# header\nline two\n",
    "x" * 300,                      # crosses the 127-byte keystream wrap
    "\n".join(["abc"] * 100),       # newlines must not advance the counter
    "".join(chr(c) for c in range(32, 127)),
])
def test_encode_decode_roundtrip(text):
    assert chamsys.decode_hed(chamsys.encode_hed(text)) == text


def test_encoded_output_looks_like_a_real_head():
    blob = chamsys.encode_hed("# MagicQ personality file.\n")
    assert chamsys.looks_obfuscated(blob)
    assert all(b >= 0x80 for b in blob if b != 0x0A)


def test_encode_rejects_non_ascii():
    with pytest.raises(chamsys.HedDecodeError):
        chamsys.encode_hed("Café")


def test_parse_real_personality():
    fx = chamsys.read(REAL_HEAD)
    assert fx.manufacturer == "test"
    assert fx.model == "test"
    mode = fx.modes[0]
    assert mode.name == "AAAAAAAA"
    assert mode.channel_count == 20
    # "Reserved (63)" carries no meaning, so it must stay Unknown, not be guessed.
    assert set(mode.attribute_set()) == {"Unknown"}
    assert all(not c.fine for c in mode.channels)


def test_parse_personality_channel_semantics():
    """Names, 16-bit pairing and HTP, on a personality shaped like a real one."""
    text = (
        "# MagicQ personality file.\n"
        '\\ Personality file for Test\n'
        'V,008c,"MagicQ 1";\n'
        'P,000c,"Robe_Spot_12ch","Robe","12ch","Spot",\n'
        "000c,0000,0000,0000,0000,0000,0001,0001,01f5,00000000,\n"
        '"Pan",00000032,00000004,\n'
        '"Pan",00000032,00000004,\n'
        '"Tilt",00000032,00000005,\n'
        '"Tilt",00000032,00000005,\n'
        '"Int",00000001,00000000,\n'
        '"Shutter",00000012,00000002,\n'
        '"Col1",00000022,00000006,\n'
        '"Gobo",00000012,00000008,\n'
    )
    fx = chamsys.parse_personality(text)
    assert (fx.manufacturer, fx.model) == ("Robe", "Spot")
    mode = fx.modes[0]
    assert mode.name == "12ch"

    pan, pan_fine, tilt, tilt_fine = mode.channels[:4]
    assert (pan.attribute, pan.fine) == ("Pan", False)
    assert (pan_fine.attribute, pan_fine.fine) == ("Pan", True)
    assert (tilt.attribute, tilt.fine) == ("Tilt", False)
    assert tilt_fine.fine is True

    intensity = mode.channels[4]
    assert intensity.attribute == "Dimmer" and intensity.htp is True
    assert mode.channels[5].htp is False
    assert mode.channels[6].attribute == "ColorWheel"
    assert mode.channels[7].attribute == "Gobo1"

    # Declared footprint (0x000c = 12) is kept even though only 8 are named.
    assert mode.channel_count == 12


def test_channel_name_beats_attribute_number():
    """Real personalities misuse attribute numbers; the name is the better signal.

    A cheap fixture's head really does ship "Speed" on attribute 0x02, which
    the table calls Shutter.
    """
    text = (
        'P,0001,"x","ACME","1ch","Thing",\n'
        "0001,0000,0000,0000,0000,0000,0001,0001,01f5,00000000,\n"
        '"Speed",00000012,00000002,\n'
    )
    assert chamsys.parse_personality(text).modes[0].channels[0].attribute == "Speed"


def test_nameless_head_falls_back_to_filename(tmp_path):
    """A head with no manufacturer and no model must not show as a blank row."""
    text = (
        'P,0007,"","","HILED","",\n'
        "0007,0000,0000,0000,0000,0000,0001,0001,01f5,00000000,\n"
        '"Dimmer",00000001,00000000,\n'
    )
    path = tmp_path / "__HILED.hed"
    path.write_bytes(chamsys.encode_hed(text))

    fx = chamsys.read(path)
    assert fx.key == "HILED"          # not "" - the filename identifies it
    assert fx.modes[0].name == "HILED"


def test_write_then_read_roundtrip(tmp_path):
    fx = Fixture(manufacturer="Robe", model="Pointe", modes=[Mode(name="16ch", channels=[
        Channel(offset=1, name="Dimmer", attribute="Dimmer", htp=True),
        Channel(offset=2, name="Pan", attribute="Pan"),
        Channel(offset=3, name="Tilt", attribute="Tilt"),
        Channel(offset=4, name="Col1", attribute="ColorWheel"),
    ])])
    out = chamsys.write(fx, tmp_path / "Robe_Pointe_16ch.hed")

    assert chamsys.looks_obfuscated(out.read_bytes())
    back = chamsys.read(out)
    assert back.manufacturer == "Robe"
    assert back.model == "Pointe"
    assert back.modes[0].name == "16ch"
    assert back.modes[0].attributes() == ["Dimmer", "Pan", "Tilt", "ColorWheel"]
    assert back.modes[0].channels[0].htp is True


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


@pytest.mark.parametrize("canonical,gdtf_name", [
    ("Shutter", "Shutter1"), ("Strobe", "Shutter1Strobe"),
    ("Red", "ColorAdd_R"), ("Cyan", "ColorSub_C"),
    ("ColorWheel", "Color1"), ("ColorWheel2", "Color2"),
    ("Gobo1Rot", "Gobo1Pos"), ("Gobo2Rot", "Gobo2Pos"),
    ("Prism", "Prism1"), ("PrismRot", "Prism1Pos"),
    ("Focus", "Focus1"), ("Frost", "Frost1"), ("Control", "Control1"),
    ("Pan", "Pan"), ("Dimmer", "Dimmer"), ("Zoom", "Zoom"),
])
def test_gdtf_uses_standard_attribute_names(canonical, gdtf_name):
    """Importers map channels onto encoders by recognising the standard name."""
    assert gdtf.gdtf_attribute(canonical) == gdtf_name
    # and it must survive the trip home
    assert attributes.normalise(gdtf_name) == canonical


def test_gdtf_standard_names_survive_export(tmp_path):
    fx = ofl.parse(json.dumps(OFL_DOC), manufacturer="eurolite")
    out = gdtf.write(fx, tmp_path / "x.gdtf")
    import zipfile
    xml = zipfile.ZipFile(out).read("description.xml").decode()

    assert 'Attribute Name="Color1"' in xml
    assert 'Feature="Position.PanTilt"' in xml
    assert 'Feature="Dimmer.Dimmer"' in xml
    assert 'Name="Body_Pan"' in xml
    # Wheels must snap, continuous parameters must not.
    assert 'Attribute="Color1" Snap="Yes"' in xml
    assert 'Attribute="Pan" Snap="No"' in xml

    # Round-trip still lands back on our canonical vocabulary.
    assert gdtf.read(out).modes[0].attribute_set() == fx.modes[0].attribute_set()


def test_gdtf_feature_defaults_to_colour():
    assert gdtf.gdtf_feature("ColorAdd_R") == "Color.Color"
    assert gdtf.gdtf_feature("Pan") == "Position.PanTilt"


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


def test_single_insertion_does_not_cascade():
    """The reason the matcher uses alignment rather than slot-by-slot compare.

    Inserting one channel near the top shifts every later offset. A positional
    comparison calls all of them wrong; alignment must report one insertion.
    """
    cand = _mode("lib", ["Dimmer", "Pan", "Tilt", "Zoom", "Focus", "Iris", "Frost"])
    target = _mode("t", ["Dimmer", "Shutter", "Pan", "Tilt", "Zoom", "Focus", "Iris", "Frost"])
    score, edits = matching.compare_modes(target, cand)

    assert len(edits) == 1
    assert edits[0].action == "add"
    assert edits[0].attribute == "Shutter"
    assert edits[0].offset == 2
    assert score > 0.85


def test_deletion_does_not_cascade():
    cand = _mode("lib", ["Dimmer", "Shutter", "Pan", "Tilt", "Zoom"])
    target = _mode("t", ["Dimmer", "Pan", "Tilt", "Zoom"])
    _, edits = matching.compare_modes(target, cand)
    assert len(edits) == 1
    assert edits[0].action == "remove" and edits[0].attribute == "Shutter"


def test_relocated_channel_reads_as_a_move():
    cand = _mode("lib", ["Dimmer", "Zoom", "Pan", "Tilt"])
    target = _mode("t", ["Dimmer", "Pan", "Tilt", "Zoom"])
    _, edits = matching.compare_modes(target, cand)
    moves = [e for e in edits if e.action == "move"]
    assert len(moves) == 1 and moves[0].attribute == "Zoom"
    # A move must not also be reported as a separate add/remove pair.
    assert not [e for e in edits if e.action in ("add", "remove") and e.attribute == "Zoom"]


@pytest.mark.parametrize("attrs,system,detail", [
    ({"Cyan", "Magenta", "Yellow"}, "cmy", "CMY"),
    ({"Red", "Green", "Blue"}, "rgb", "RGB"),
    ({"Red", "Green", "Blue", "White"}, "rgb", "RGBW"),
    ({"Red", "Green", "Blue", "White", "Amber", "UV"}, "rgb", "RGBWAUV"),
    ({"Cyan", "Magenta", "Yellow", "Red"}, "hybrid", "CMY+R"),
    ({"ColorWheel"}, "wheel", "colour wheel"),
    ({"Dimmer", "Pan"}, "none", "no colour"),
])
def test_colour_system(attrs, system, detail):
    assert attributes.colour_system(attrs) == system
    assert attributes.colour_detail(attrs) == detail


def test_colour_system_mismatch_is_penalised():
    """A CMY head is not a substitute for an RGB one, whatever the names say."""
    cmy = _mode("cmy", ["Dimmer", "Cyan", "Magenta", "Yellow"])
    rgb = _mode("rgb", ["Dimmer", "Red", "Green", "Blue"])
    mismatch, _ = matching.compare_modes(cmy, rgb)
    same, _ = matching.compare_modes(cmy, _mode("c2", ["Dimmer", "Cyan", "Magenta", "Yellow"]))
    assert same == 1.0
    assert mismatch < 0.4


def test_content_score_ignores_order():
    a = _mode("a", ["Dimmer", "Pan", "Tilt"])
    b = _mode("b", ["Tilt", "Dimmer", "Pan"])
    assert matching.content_score(a, b) == 1.0
    c = _mode("c", ["Dimmer", "Pan", "Zoom"])
    assert 0 < matching.content_score(a, c) < 1.0


def test_right_channels_wrong_order_beats_missing_channels():
    target = _mode("t", ["Dimmer", "Pan", "Tilt", "Zoom"])
    shuffled = _mode("s", ["Zoom", "Tilt", "Pan", "Dimmer"])
    missing = _mode("m", ["Dimmer", "Pan", "Gobo1", "Frost"])
    assert matching.compare_modes(target, shuffled)[0] > matching.compare_modes(target, missing)[0]


def test_footprint_only_comparison_is_capped():
    """A ChamSys head with no channel detail must never claim an exact match."""
    target = _mode("t", ["Dimmer", "Pan", "Tilt"])
    blind = Mode(name="9ch", declared_count=3)
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


# --------------------------------------------------------------------------
# grandMA3
# --------------------------------------------------------------------------

MA3_FILE = DATA / "ayrton@alienpix-rs.xml"


def test_ma3_parse_real_library_file():
    """A genuine fixture from a grandMA3 install's lib_fixture_types."""
    fx = ma3.read(MA3_FILE)
    assert fx.manufacturer == "Ayrton"
    assert fx.model == "Alienpix-RS"
    assert fx.source == "ma3"

    mode = fx.modes[0]
    assert mode.name == "Ex 16 Bit (52 ch)"
    # GeometryReference repeats are not expanded, so the footprint comes from
    # the mode name rather than from the channels we could resolve.
    assert mode.channel_count == 52

    by_offset = mode.by_offset()
    assert by_offset[1].attribute == "Pan" and not by_offset[1].fine
    assert by_offset[2].attribute == "Pan" and by_offset[2].fine
    assert by_offset[3].attribute == "Tilt"
    assert by_offset[11].attribute == "Shutter"
    assert by_offset[12].attribute == "Dimmer" and by_offset[12].htp
    assert by_offset[13].attribute == "Red"
    assert by_offset[16].attribute == "White"
    assert by_offset[52].attribute == "Control"


def test_ma3_colour_system_detected():
    fx = ma3.read(MA3_FILE)
    assert attributes.colour_detail(fx.modes[0].attribute_set()) == "RGBW"


def test_ma3_skips_virtual_channels():
    """Channels with no Coarse have no DMX footprint and must not be counted."""
    fx = ma3.read(MA3_FILE)
    offsets = sorted(fx.modes[0].by_offset())
    assert offsets[0] == 1
    # The file has virtual dimmers driven by Relations; none may appear twice.
    assert len(offsets) == len(set(offsets))


def test_ma3_ranges_are_named():
    fx = ma3.read(MA3_FILE)
    shutter = fx.modes[0].by_offset()[11]
    names = [r.name for r in shutter.ranges]
    assert "Closed" in names and "Open" in names


@pytest.mark.parametrize("raw,expected", [
    ("FFFFFF", 255), ("800000", 128), ("000000", 0), ("7F7F7F", 127),
    ("FF", 255), ("", 0), ("zz", 0),
])
def test_ma3_dmx_value(raw, expected):
    assert ma3.dmx_value(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("ColorRGB_R", "Red"), ("ColorRGB_G", "Green"), ("ColorRGB_B", "Blue"),
    ("ColorRGB_W", "White"), ("ColorRGB_UV", "UV"),
    ("TiltMode", "Control"), ("PanMode", "Control"), ("PositionModes", "Control"),
])
def test_ma3_attribute_names(raw, expected):
    assert attributes.normalise(raw) == expected


def test_ma3_detection_and_rejection():
    assert ma3.looks_like_ma3(MA3_FILE.read_bytes())
    assert not ma3.looks_like_ma3(GDTF_XML.encode())
    with pytest.raises(ValueError, match="not a grandMA3"):
        ma3.parse(b"<?xml version='1.0'?><Nope/>")


def test_ma3_to_gdtf_bridges_to_chamsys(tmp_path):
    """The point of reading MA3: get the fixture into another desk."""
    fx = ma3.read(MA3_FILE)
    out = gdtf.write(fx, tmp_path / "alienpix.gdtf")
    back = gdtf.read(out)
    assert back.manufacturer == "Ayrton"
    assert {"Pan", "Tilt", "Dimmer", "Red", "Green", "Blue", "White"} <= back.modes[0].attribute_set()
