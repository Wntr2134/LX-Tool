"""Custom head plans and DMX-chart parsing: the venue-clone workflow."""

from __future__ import annotations

import pytest

from lxtool import chart, plan
from lxtool.formats import chamsys
from lxtool.model import Channel, Fixture, Mode, Range


def _aura_like() -> Fixture:
    return Fixture(manufacturer="Martin", model="MAC Aura", modes=[Mode(
        name="Standard",
        channels=[
            Channel(offset=1, name="Shutter", attribute="Shutter",
                    ranges=[Range(0, 19, "Closed"), Range(20, 24, "Open")]),
            Channel(offset=2, name="Dimmer", attribute="Dimmer", htp=True,
                    default=255),
            Channel(offset=3, name="Pan", attribute="Pan"),
            Channel(offset=4, name="Pan fine", attribute="Pan", fine=True),
            Channel(offset=5, name="Colour", attribute="ColorWheel"),
        ],
    )])


def test_plan_round_trips():
    fx = _aura_like()
    back = plan.parse(plan.dump(fx))
    a, b = fx.modes[0], back.modes[0]
    assert [c.name for c in b.channels] == [c.name for c in a.channels]
    assert [c.attribute for c in b.channels] == [c.attribute for c in a.channels]
    assert [c.fine for c in b.channels] == [c.fine for c in a.channels]
    assert b.channels[1].default == 255
    assert b.channels[0].ranges == a.channels[0].ranges
    assert back.manufacturer == "Martin"


def test_reordering_lines_reorders_the_dmx_layout():
    """The whole point: the clone with the channels in a different order."""
    text = plan.dump(_aura_like())
    lines = text.split("\n")
    pan = [l for l in lines if l.startswith("channel: Pan")]
    rest = [l for l in lines if l not in pan]
    edited = "\n".join(rest + pan)
    edited = edited.replace("manufacturer: Martin", "manufacturer: China")

    fx = plan.parse(edited)
    assert fx.manufacturer == "China"
    assert [c.attribute for c in fx.modes[0].channels][-2:] == ["Pan", "Pan"]
    assert fx.modes[0].channels[-1].fine

    # and it compiles straight to a .hed
    text = chamsys.build_personality(fx)
    assert '"Pan fine",' in text


def test_plan_errors_carry_line_numbers():
    with pytest.raises(ValueError, match="line 3"):
        plan.parse("model: X\nchannel: Dimmer\n  not a range\n")
    with pytest.raises(ValueError, match="model"):
        plan.parse("channel: Dimmer\n")
    with pytest.raises(ValueError, match="0-255"):
        plan.parse("model: X\nchannel: D | default=300\n")


def test_chart_parses_a_messy_manual():
    fx = chart.parse_chart("""
        DMX Channel Functions
        CH1 - Pan
        CH2 - Pan Fine
        3. Tilt
        4 | Dimmer
        5   Strobe
        0-9 Open
        10 - 250 Strobe slow to fast
        6-7  Zoom (16 bit)
        8  Red
    """)
    chans = fx.modes[0].channels
    assert [c.attribute for c in chans] == \
        ["Pan", "Pan", "Tilt", "Dimmer", "Strobe", "Zoom", "Zoom", "Red"]
    assert chans[1].fine and not chans[0].fine
    assert not chans[5].fine and chans[6].fine      # 6-7 Zoom = coarse+fine
    assert [r.name for r in chans[4].ranges] == ["Open", "Strobe slow to fast"]


def test_chart_rejects_unreadable_text():
    with pytest.raises(ValueError):
        chart.parse_chart("no channels here at all")


def test_chart_to_plan_to_hed():
    """The manual-photo workflow, end to end."""
    fx = chart.parse_chart("1 Pan\n2 Tilt\n3 Dimmer\n4 Red\n5 Green\n6 Blue")
    text = plan.dump(fx).replace("model: FromChart", "model: MysteryPar")
    built = plan.parse(text)
    head = chamsys.build_personality(built)
    assert '"MysteryPar",' in head.split("\n")[3]
    back = chamsys.parse_personality(head)
    assert [c.attribute for c in back.modes[0].channels] == \
        ["Pan", "Tilt", "Dimmer", "Red", "Green", "Blue"]


def test_warnings_catch_the_common_mistakes():
    fx = plan.parse(
        "model: X\n"
        "channel: Red\n"
        "channel: Pan fine\n"
        "channel: Mystery knob\n"
        "channel: Red\n"
        "channel: Strobe\n"
        "  0-100  A\n"
        "  50-200  B\n"
    )
    w = "\n".join(plan.warnings(fx))
    assert "no Dimmer" in w
    assert "fine half without a matching coarse" in w
    assert "attribute not recognised" in w
    assert "second Red channel" in w
    assert "ranges overlap" in w

    clean = plan.parse(
        "model: X\nchannel: Dimmer\nchannel: Pan\nchannel: Pan fine\n")
    assert plan.warnings(clean) == []


def test_blank_template():
    fx = plan.blank(8)
    assert len(fx.modes[0].channels) == 8
    assert fx.modes[0].channels[0].attribute == "Dimmer"
    back = plan.parse(plan.dump(fx))
    assert len(back.modes[0].channels) == 8
    with pytest.raises(ValueError):
        plan.blank(0)


def test_chart_column_wise_ocr():
    """Phone OCR reads tables one column at a time; zip them back."""
    fx = chart.parse_chart(
        "DMX functions\n1\n2\n3\n4\n5\nPan\nPan fine\nTilt\nDimmer\nRed\n")
    chans = fx.modes[0].channels
    assert [c.attribute for c in chans] == ["Pan", "Pan", "Tilt", "Dimmer", "Red"]
    assert chans[1].fine


def test_chart_dotted_range_separators():
    fx = chart.parse_chart("1 Strobe\n0..9 Open\n10…255 Strobe fast")
    assert [(r.dmx_from, r.dmx_to) for r in fx.modes[0].channels[0].ranges] == \
        [(0, 9), (10, 255)]


def test_chart_match_ranks_the_real_fixture(tmp_path, monkeypatch):
    """Paste a clone's chart, get told what it really is."""
    from lxtool import matching
    from lxtool.model import Channel, Fixture, Mode

    aura = Fixture(manufacturer="Martin", model="Aura", source="chamsys", modes=[Mode(
        name="Standard",
        channels=[Channel(offset=i + 1, name=a, attribute=a) for i, a in enumerate(
            ["Shutter", "Dimmer", "Zoom", "Pan", "Tilt", "Control",
             "ColorWheel", "Red", "Green", "Blue", "White", "CTO"])],
    )])
    par = Fixture(manufacturer="Generic", model="RGB Par", source="chamsys", modes=[Mode(
        name="3ch", channels=[Channel(offset=i + 1, name=a, attribute=a)
                              for i, a in enumerate(["Red", "Green", "Blue"])])])

    clone = chart.parse_chart(
        "1 Shutter\n2 Dimmer\n3 Zoom\n4 Pan\n5 Tilt\n6 Control\n"
        "7 Colour macro\n8 Red\n9 Green\n10 Blue\n11 White\n12 CTC")
    hits = matching.find_candidates(clone, clone.modes[0], [aura, par], limit=2)
    assert hits[0].fixture.model == "Aura"
