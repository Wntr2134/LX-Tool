"""Console patch export -> another desk's import format.

Built against a real MagicQ "Fixture Patch" export, including the parts
that make real exports awkward: blank manufacturer/model cells, mode
cells that are names rather than channel counts, and a header row where
"mode" is a substring of "model".
"""

from __future__ import annotations

import pytest

from lxtool import patchlist
from lxtool.formats import mvr, patchout

# A slice of the real export, verbatim in shape.
MAGICQ = """\
Head No,DMX,Position,Hang,Manufacturer,Model,Mode
1,01-001,,,,,HILED
2,01-008,,,,,HILED
11,01-071,,,Beamz,Panther40MK2,9ch
12,01-082,,,Beamz,Panther40MK2,9ch
15,01-115,,,Martin,,Standard
24,01-421,,,BeamZ,H2000Fazer,2ch
25,01-430,,,Chauvet DJ,HurriHaze1DX,1ch
"""


def _rows(text=MAGICQ):
    return {r.head_no: r for r in patchlist.parse(text, cache=None).rows}


def test_every_row_is_read_with_its_address():
    rows = _rows()
    assert len(rows) == 7
    assert rows[1].universe == 1 and rows[1].address == 1
    assert rows[11].address == 71
    assert rows[25].address == 430


def test_mode_column_is_not_confused_with_model():
    """"mode" is a substring of "model" - exact header match must win."""
    rows = _rows()
    assert rows[11].model == "Panther40MK2"
    assert rows[11].mode == "9ch"
    assert rows[11].channels == 9
    assert rows[11].channels_from == "mode"
    assert rows[24].channels == 2
    assert rows[25].channels == 1


def test_footprints_fall_back_to_the_gap_the_patcher_left():
    rows = _rows()
    # HILEDs are 7 apart and say nothing about their size.
    assert rows[1].channels == 7
    assert rows[1].channels_from == "gap"
    # The last row has no next address: it must still get something usable.
    assert rows[25].channels >= 1


def test_a_bare_manufacturer_never_triggers_a_catalogue_guess():
    """"Martin" alone matches *something* in 17k fixtures - and a
    confident wrong footprint is worse than an honest inferred one."""
    class FakeCatalogue:
        def search_scored(self, query, limit=1, channels=None):
            raise AssertionError(f"should not have searched for {query!r}")

    row = patchlist.PatchRow(manufacturer="Martin", model="", mode="Standard")
    assert patchlist._from_catalogue(FakeCatalogue(), row) == 0


def test_a_weak_catalogue_match_is_refused():
    class Entry:
        modes = (("Mode", 42),)

    class FakeCatalogue:
        def search_scored(self, query, limit=1, channels=None):
            return [(0.2, Entry())]

    row = patchlist.PatchRow(manufacturer="Beamz", model="Panther40MK2")
    assert patchlist._from_catalogue(FakeCatalogue(), row) == 0


def test_catalogue_picks_the_mode_by_name_then_falls_back():
    class Entry:
        modes = (("Basic", 8), ("Extended", 16))

    class FakeCatalogue:
        def search_scored(self, query, limit=1, channels=None):
            return [(0.9, Entry())]

    named = patchlist.PatchRow(model="Some Light", mode="Extended")
    assert patchlist._from_catalogue(FakeCatalogue(), named) == 16
    unnamed = patchlist.PatchRow(model="Some Light", mode="Whatever")
    assert patchlist._from_catalogue(FakeCatalogue(), unnamed) == 8


@pytest.mark.parametrize("cell,expected", [
    ("01-001", (1, 1)), ("1.071", (1, 71)), ("2/15", (2, 15)),
    ("03:100", (3, 100)), ("250", (1, 250)),
    ("513", (2, 1)),                 # a continuous address splits
])
def test_address_forms(cell, expected):
    assert patchlist._address(cell) == expected


def test_pdf_style_ragged_text_still_parses():
    """PDF text arrives as runs of spaces, not commas, and often with no
    usable header line."""
    text = ("11    01-071          Beamz     Panther40MK2   9ch\n"
            "12    01-082          Beamz     Panther40MK2   9ch\n")
    rows = patchlist.parse(text, cache=None).rows
    assert len(rows) == 2
    assert rows[0].head_no == 11
    assert rows[0].address == 71
    assert rows[0].channels == 9


def test_junk_lines_are_skipped_not_fatal():
    text = ("POCKETS NEW 2026 : Fixture Patch\n"
            "Weights include only fixtures for which the weight is specified.\n"
            + MAGICQ)
    assert len(patchlist.parse(text, cache=None).rows) == 7


def test_overlapping_footprints_are_flagged():
    text = ("Head No,DMX,Manufacturer,Model,Mode\n"
            "1,01-001,X,Thing,16ch\n"       # 1-16 overlaps the next at 8
            "2,01-008,X,Thing,16ch\n")
    warnings = patchlist.parse(text, cache=None).warnings
    assert warnings and "overlaps" in warnings[0]


def test_a_clean_patch_raises_no_alarm():
    text = ("Head No,DMX,Manufacturer,Model,Mode\n"
            "1,01-001,X,Thing,8ch\n"
            "2,01-009,X,Thing,8ch\n")
    assert patchlist.parse(text, cache=None).warnings == []


# ---- exports ----------------------------------------------------------


def _rig():
    return patchlist.parse(MAGICQ, cache=None).rig


def test_eos_csv_has_the_columns_eos_maps():
    text = patchout.eos_csv(_rig())
    header = text.splitlines()[0].split(",")
    for want in ("Channel", "Address", "Manufacturer", "Fixture Type"):
        assert want in header
    # Eos reads universe/address directly.
    assert ",1/71," in text


def test_ma2_csv_is_exactly_what_the_plugin_wants():
    """Semicolons, no header, no quoting - the plugin is strict."""
    lines = patchout.ma2_csv(_rig()).strip().splitlines()
    assert lines[0] == "1;1.1"
    assert all(";" in ln and "," not in ln for ln in lines)
    assert not lines[0].startswith('"')


def test_magicq_round_trip_keeps_addresses_and_footprints():
    once = patchout.magicq_csv(_rig())
    rows = patchlist.parse(once, cache=None).rows
    original = patchlist.parse(MAGICQ, cache=None).rows
    assert [r.address for r in rows] == [r.address for r in original]
    assert [r.channels for r in rows] == [r.channels for r in original]


def test_generic_csv_reports_the_computed_last_address():
    text = patchout.generic_csv(_rig())
    row = [ln for ln in text.splitlines() if ln.startswith("11,")][0].split(",")
    assert row[2] == "71"          # address
    assert row[3] == "79"          # last address: 71 + 9 - 1
    assert row[4] == "9"           # footprint


def test_mvr_writes_and_reads_back_with_the_same_patch(tmp_path):
    out = patchout.write(_rig(), "mvr", tmp_path / "rig.mvr")
    back = mvr.read(out)
    assert len(back.fixtures) == 7
    by_id = {pf.fixture_id: pf for pf in back.fixtures}
    assert by_id["11"].universe == 1 and by_id["11"].address == 71
    assert by_id["11"].footprint == 9
    assert by_id["11"].fixture.model == "Panther40MK2"


def test_unknown_target_is_refused(tmp_path):
    with pytest.raises(ValueError, match="unknown target"):
        patchout.write(_rig(), "hog4", tmp_path / "x")


def test_every_advertised_target_actually_writes(tmp_path):
    rig = _rig()
    for target in patchout.TARGETS:
        out = patchout.write(rig, target, tmp_path / f"out-{target}")
        assert out.is_file() and out.stat().st_size > 0
        assert target in patchout.TARGET_HELP


def test_cli_lists_and_converts(tmp_path, capsys):
    from lxtool.cli import build_parser

    src = tmp_path / "patch.csv"
    src.write_text(MAGICQ, encoding="utf-8")

    args = build_parser().parse_args(["patch", str(src), "--list"])
    assert args.func(args) == 0
    listed = capsys.readouterr().out
    assert "7 fixture(s)" in listed
    assert "[mode     ]" in listed

    out = tmp_path / "eos.csv"
    args = build_parser().parse_args(
        ["patch", str(src), "--to", "eos", "-o", str(out)])
    assert args.func(args) == 0
    assert "Channel,Address" in out.read_text()


def test_pdf_reader_is_honest_when_unavailable(monkeypatch):
    from lxtool import pdftext

    monkeypatch.setattr(pdftext, "available", lambda: (False, "no pypdf here"))
    with pytest.raises(RuntimeError, match="no pypdf here"):
        pdftext.read_text(b"%PDF-1.4")


def test_pdf_reader_turns_any_failure_into_a_readable_message():
    """The invariant: a RuntimeError a human can act on - never a crash.

    That includes a broken `cryptography` install, whose Rust bindings
    raise a PanicException at *import* time. It is a BaseException, so
    `except ImportError` (or even `except Exception`) would sail past it
    and take the whole app down over one optional feature.
    """
    from lxtool import pdftext

    with pytest.raises(RuntimeError) as exc:
        pdftext.read_text(b"not a pdf at all")
    message = str(exc.value)
    assert message and not message.startswith("<")
    # Whichever way it failed, it says what to do instead.
    assert "PDF" in message or "pypdf" in message


def test_pdf_availability_never_raises():
    """available() is called to decide whether to show the feature at
    all, so it must answer even on a machine where importing fails."""
    from lxtool import pdftext

    ok, reason = pdftext.available()
    assert isinstance(ok, bool)
    assert reason
