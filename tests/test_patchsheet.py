"""Patch sheet triage and bulk conversion.

The parser is fed the shapes a real sheet arrives in - spreadsheet pipes,
OCR spacing, mangled numbers - and must group fixtures, find the address
collisions a tired eye misses, and hand unknowns to the stock drafts. The
bulk path must turn a folder of ChamSys heads into MA3-ready GDTF without
one bad file sinking the batch.
"""

from __future__ import annotations

import pytest

from lxtool import patchsheet, plan, stock
from lxtool.formats import chamsys, gdtf

# A slice of the real Sri Lanka sheet, exactly as the spreadsheet dumped it -
# including the 24ch fixtures spaced 20 apart (a genuine collision) and the
# sparkular sitting on the key dimmer's address (another).
SHEET = """\
Handle # | Fixture Name / Type | Location / Position | DMX Mode | Universe | Start Address (Physical DMX) | End Address
78 | BSW Moving Spot | FOH Truss / Position | 24 Ch | 7 | 7.001 | 7.024
79 | BSW Moving Spot | FOH Truss / Position | 24 Ch | 7 | 7.021 | 7.044
1 | IP380B / BSW 380 Spot | Row 1 (Back Truss) | 16 Ch | 1 | 1.001 | 1.016
2 | IP380B / BSW 380 Spot | Row 1 (Back Truss) | 16 Ch | 1 | 1.017 | 1.032
23 | Martin MAC Aura XB | Row 1 (Back Truss) | 25 Ch (Std) | 3 | 3.001 | 3.025
56 | Cold Sparkular | Row 1 (Back Truss - Hanging) | 2 Ch | 4 | 4.400 | 4.401
58 | Conventional Key Dimmer | Front Wash / Key Dimmers | 1 Ch | 4 | 4.400 | 4.400
"""


def test_rows_group_by_fixture_and_channel_count():
    sheet = patchsheet.parse(SHEET)
    by_name = {g.name: g for g in sheet.groups}
    assert by_name["BSW Moving Spot"].qty == 2
    assert by_name["BSW Moving Spot"].channels == 24
    assert by_name["IP380B / BSW 380 Spot"].channels == 16
    assert by_name["Martin MAC Aura XB"].channels == 25
    assert by_name["BSW Moving Spot"].universes == [7]


def test_the_header_row_is_not_a_fixture():
    sheet = patchsheet.parse(SHEET)
    assert all("Handle" not in g.name for g in sheet.groups)


def test_both_real_collisions_are_caught():
    """The two errors that were actually on the sheet must both be flagged."""
    warnings = "\n".join(patchsheet.parse(SHEET).warnings)
    # 24ch fixtures addressed every 20: 1..24 overlaps the unit at 21.
    assert "universe 7" in warnings
    assert "BSW Moving Spot" in warnings
    # The sparkular and the key dimmer share 4.400.
    assert "universe 4" in warnings
    assert "Cold Sparkular" in warnings


def test_correctly_spaced_rows_raise_no_alarm():
    sheet = patchsheet.parse(
        "1 | Thing | 16 Ch | 1 | 1.001 | 1.016\n"
        "2 | Thing | 16 Ch | 1 | 1.017 | 1.032\n")
    assert sheet.warnings == []


def test_kind_guesses_feed_the_stock_drafts():
    sheet = patchsheet.parse(SHEET)
    guesses = {g.name: g.kind_guess for g in sheet.groups}
    # "BSW Moving Spot" is the beam family, despite the word "spot".
    assert guesses["BSW Moving Spot"] == "beam"
    assert guesses["Cold Sparkular"] == "spark"
    valid = {k for k, _ in stock.kinds()}
    for g in sheet.groups:
        if g.kind_guess:
            assert g.kind_guess in valid
            # and the pipeline continues: guess + count -> buildable plan
            if g.channels:
                fx = plan.parse(stock.plan_text(g.kind_guess, g.channels))
                assert len(fx.modes[0].channels) == g.channels


def test_ocr_spacing_works_without_pipes():
    sheet = patchsheet.parse("1   IP380B Spot   Back Truss   16 Ch   1.001\n")
    (g,) = sheet.groups
    assert g.name == "IP380B Spot"
    assert g.channels == 16
    assert g.rows[0].universe == 1 and g.rows[0].address == 1


def test_garbage_is_counted_not_crashed():
    sheet = patchsheet.parse("what even is this\n42\n\n1 | Real Light | 8 Ch | 1 | 1.001 | 1.008\n")
    assert len(sheet.groups) >= 1
    assert any(g.name == "Real Light" for g in sheet.groups)


def test_web_endpoint_triages_and_suggests():
    from lxtool.web import app as web

    d = web.api_patch_sheet(sheet_text=SHEET)
    bsw = next(g for g in d["groups"] if g["name"] == "BSW Moving Spot")
    assert bsw["kind"] == "beam" and bsw["channels"] == 24
    assert len(d["warnings"]) >= 2

    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        web.api_patch_sheet(sheet_text="")


def test_bulk_folder_conversion_chamsys_to_gdtf(tmp_path, capsys):
    """A folder of .hed becomes a folder of MA3-importable GDTF."""
    from lxtool.cli import build_parser

    src, dest = tmp_path / "heads", tmp_path / "ma3"
    src.mkdir()
    for kind in ("beam", "wash"):
        fx = plan.parse(stock.plan_text(kind, 16, model=f"Test {kind}"))
        chamsys.write(fx, src / f"{kind}.hed", fx.modes[0])
    (src / "broken.hed").write_bytes(b"not a head file")

    args = build_parser().parse_args(
        ["convert", str(src), str(dest), "--to", "gdtf"])
    assert args.func(args) == 0

    outs = sorted(p.name for p in dest.glob("*.gdtf"))
    assert len(outs) == 2
    for p in dest.glob("*.gdtf"):
        back = gdtf.read(p)
        assert len(back.modes[0].channels) == 16
    assert "1 skipped" in capsys.readouterr().out


def test_bulk_conversion_requires_a_target(tmp_path):
    from lxtool.cli import build_parser

    (tmp_path / "x.hed").write_bytes(b"")
    args = build_parser().parse_args(
        ["convert", str(tmp_path), str(tmp_path / "out")])
    with pytest.raises(SystemExit, match="--to"):
        args.func(args)


def test_web_uploads_accept_chamsys_heads():
    """ChamSys .hed is a readable source now - the upload gate must agree."""
    from lxtool.web.app import _SUPPORTED

    assert ".hed" in _SUPPORTED


def test_cli_sheet_command_prints_the_triage(tmp_path, capsys):
    from lxtool.cli import build_parser

    f = tmp_path / "sheet.txt"
    f.write_text(SHEET, encoding="utf-8")
    args = build_parser().parse_args(["sheet", str(f)])
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert "COLLISION" in out
    assert "--stock beam --channels 24" in out
