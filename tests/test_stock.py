"""Stock draft layouts: the no-information-at-all path must stand alone.

A tech in a venue with nothing but a patch sheet ("22C Moving Head, 22 Ch")
has to get a patchable, fixable head out of these - every layout, at any
plausible channel count, must parse, build and carry its own verification
instructions.
"""

from __future__ import annotations

import pytest

from lxtool import plan, stock
from lxtool.formats import chamsys


def test_every_kind_is_listed_with_a_label():
    ks = dict(stock.kinds())
    for expected in ("beam", "spot", "wash", "par", "strobe", "spark"):
        assert expected in ks
        assert ks[expected]


@pytest.mark.parametrize("kind", [k for k, _ in stock.kinds()])
@pytest.mark.parametrize("count", [1, 2, 8, 12, 16, 22, 24, 40])
def test_any_kind_any_count_parses_to_exactly_that_many_channels(kind, count):
    fixture = plan.parse(stock.plan_text(kind, count))
    assert len(fixture.modes[0].channels) == count


@pytest.mark.parametrize("kind", [k for k, _ in stock.kinds()])
def test_a_fine_channel_always_follows_its_coarse_partner(kind):
    """Trimming a layout to a small count must never orphan a fine channel."""
    for count in range(1, 30):
        channels = plan.parse(stock.plan_text(kind, count)).modes[0].channels
        for i, ch in enumerate(channels):
            if ch.fine:
                assert i > 0, f"{kind}/{count}ch: fine channel first"
                prev = channels[i - 1]
                assert prev.attribute == ch.attribute and not prev.fine, (
                    f"{kind}/{count}ch: {ch.name} lost its coarse partner")


def test_drafts_lead_with_the_fader_test_instructions():
    text = stock.plan_text("beam", 16)
    assert "DRAFT" in text
    assert "fader test" in text
    assert "DMX menu" in text


def test_extra_channels_become_renameable_placeholders():
    fixture = plan.parse(stock.plan_text("spark", 6))
    names = [c.name for c in fixture.modes[0].channels]
    assert names[0] == "Preheat Arm"
    assert names[2:] == ["Channel 3", "Channel 4", "Channel 5", "Channel 6"]


def test_spark_layout_never_puts_output_on_a_dimmer():
    """A spark machine on an HTP intensity channel can fire on a full-on."""
    fixture = plan.parse(stock.plan_text("spark", 2))
    assert all(c.attribute != "Dimmer" for c in fixture.modes[0].channels)
    assert all(not c.htp for c in fixture.modes[0].channels)


def test_drafts_build_into_valid_hed_files(tmp_path):
    for kind, _ in stock.kinds():
        fixture = plan.parse(stock.plan_text(kind, 16))
        out = tmp_path / f"{kind}.hed"
        chamsys.write(fixture, out, fixture.modes[0])
        back = chamsys.read(out)
        assert len(back.modes[0].channels) == 16


def test_unknown_kind_and_silly_counts_are_rejected():
    with pytest.raises(ValueError):
        stock.plan_text("laser", 16)
    with pytest.raises(ValueError):
        stock.plan_text("beam", 0)
    with pytest.raises(ValueError):
        stock.plan_text("beam", 513)


def test_web_endpoints_serve_the_picker_and_the_draft():
    # The endpoint functions are called directly: fastapi's TestClient needs
    # an httpx flavour that CI does not install, and these endpoints have no
    # request-object behaviour worth exercising through a client.
    from fastapi import HTTPException

    from lxtool.web import app as web

    kinds = web.api_head_stock_kinds()["kinds"]
    assert {"key": "beam", "label": dict(stock.kinds())["beam"]} in kinds

    d = web.api_head_stock(kind="beam", channels=16)
    assert d["channels"] == 16
    assert "DRAFT" in d["plan"]

    with pytest.raises(HTTPException) as exc:
        web.api_head_stock(kind="nope", channels=8)
    assert exc.value.status_code == 400
