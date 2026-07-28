"""Regressions for bugs that reached main.

Each test here exists because something shipped broken. The comment on each
says what broke and, more usefully, why the existing suite did not catch it.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import pytest

from lxtool import library as library_mod
from lxtool.formats import chamsys, gdtf, ma2, ma3, mvr, ofl
from lxtool.model import Channel, Fixture, Mode


# --------------------------------------------------------------------------
# heads.all: scanning a folder that contains a container
# --------------------------------------------------------------------------
#
# `ChamSysLibrary.scan` called `_cached_container`, which had been renamed to
# `container_index` - a NameError that broke `lx scan` and every web scan and
# match endpoint on any real MagicQ library. No test caught it because no test
# ever scanned a folder containing a heads.all: the container path was only
# exercised through `decode_container` on raw bytes, which skips `scan`
# entirely. So these tests go in through the folder, the way a user does.

def _fixture(model: str, attrs: list[str]) -> Fixture:
    mode = Mode(name="Basic", channels=[
        Channel(offset=i, name=a, attribute=a) for i, a in enumerate(attrs, start=1)
    ])
    return Fixture(manufacturer="Testco", model=model, modes=[mode])


def _cipher(text: str, start: int) -> bytes:
    """Cipher `text` with the counter starting at `start`. Newlines are literal."""
    out = bytearray()
    i = start
    for ch in text:
        if ch == "\n":
            out.append(0x0A)
            continue
        out.append((ord(ch) ^ chamsys._key(i)) | 0x80)
        i += 1
    return bytes(out)


def _container_bytes(fixtures: list[Fixture], *, header_phase: int = 40) -> bytes:
    """Build a synthetic heads.all, framed the way a real one is.

    A section is a `PP,"...hed",...` header line on some phase of the
    keystream, followed by a personality body that restarts the counter at
    zero - the same framing a standalone .hed uses. Both halves were read off
    a real heads.all: all 68,420 of its sections decode cleanly under it.
    """
    out = bytearray()
    for fx in fixtures:
        name = f"{fx.manufacturer}@{fx.model}.hed".lower()
        header = f'PP,"{name}","Sun Jul 26 17:14:58 2026",0000,0000;'
        out += _cipher(header, header_phase or chamsys._MODULUS)
        out += b"\n"
        out += _cipher(chamsys.build_personality(fx) + "\n", 0)
    return bytes(out)


@pytest.fixture
def container_folder(tmp_path, monkeypatch):
    """A MagicQ heads folder holding a heads.all, with the cache redirected."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("lxtool.catalog.cache_dir", lambda: cache)

    folder = tmp_path / "heads"
    folder.mkdir()
    (folder / "heads.all").write_bytes(_container_bytes([
        _fixture("Alpha", ["Dimmer", "Pan", "Tilt", "Red", "Green", "Blue"]),
        _fixture("Beta", ["Dimmer", "Shutter", "Cyan", "Magenta", "Yellow"]),
    ]))
    return folder


def test_scan_folder_with_container(container_folder):
    """The exact call that raised NameError on every real library."""
    lib = chamsys.ChamSysLibrary.scan(container_folder)

    models = sorted(f.model for f in lib.library)
    assert models == ["Alpha", "Beta"]
    assert all(f.modes and f.modes[0].channels for f in lib.library)


def test_scan_container_can_be_skipped(container_folder):
    assert chamsys.ChamSysLibrary.scan(container_folder, include_library=False).library == []


def test_container_index_is_cached(container_folder):
    """Second read comes from the cache and matches the first exactly."""
    container = container_folder / "heads.all"

    first = chamsys.container_index(container)
    assert first, "container produced no rows"
    assert list((container_folder / ".." / "cache").resolve().glob("heads-all-*.index"))

    second = chamsys.container_index(container)
    assert [r.label for r in second] == [r.label for r in first]
    assert [r.signature() for r in second] == [r.signature() for r in first]


def test_library_load_reads_container(container_folder):
    """`lx match`/`lx dupes` go through lxtool.library, not ChamSysLibrary."""
    lib = library_mod.load([container_folder])
    assert lib.modes == 2
    assert {r.model for r in lib.rows} == {"Alpha", "Beta"}


@pytest.mark.parametrize("header_phase", [0, 1, 40, 126])
def test_container_decodes_exactly_on_any_header_phase(header_phase):
    """No character may be lost, whatever phase a section header lands on.

    Decoding used to be a guess: the phase was recovered by watching for the
    output to stop being printable, which only fires once the wrong phase
    happens to yield an unprintable byte. Everything it consumed getting
    there came out corrupt - and on a real heads.all that was 50,022 of
    68,420 sections with a mangled opening line.
    """
    fixtures = [
        _fixture("Alpha", ["Dimmer", "Pan", "Tilt", "Red", "Green", "Blue"]),
        _fixture("Beta", ["Dimmer", "Shutter", "Cyan", "Magenta", "Yellow"]),
    ]
    text = chamsys.decode_container(_container_bytes(fixtures, header_phase=header_phase))

    assert "\x7f" not in text, "undecodable bytes"
    heads = list(chamsys.iter_container_heads(text))
    assert [n for n, _ in heads] == ["testco@alpha.hed", "testco@beta.hed"]

    for (_, body), fx in zip(heads, fixtures):
        # The opening comment is the part a late resync eats first.
        assert body.startswith("# MagicQ personality file.")
        assert body == chamsys.build_personality(fx) + "\n"
        assert chamsys.parse_personality(body).model == fx.model


def test_container_body_restarts_the_keystream():
    """The body after a header restarts at index 0, not at the header's phase.

    This is the framing rule the decoder now relies on; if it were wrong, the
    body would decode as noise rather than as a personality.
    """
    fx = _fixture("Alpha", ["Dimmer", "Pan", "Tilt"])
    data = _container_bytes([fx], header_phase=40)

    body_start = data.index(b"\n") + 1
    first = chr((data[body_start] & 0x7F) ^ chamsys._key(0))
    assert first == "#"
    # 40 characters of header consumed, so a continuing keystream would be here.
    assert chr((data[body_start] & 0x7F) ^ chamsys._key(40 + data.index(b"\n"))) != "#"


def test_section_markers_cover_every_phase():
    assert len(chamsys._SECTION_MARKERS) == 127
    assert set(chamsys._SECTION_MARKERS.values()) == set(range(127))
    assert all(b & 0x80 for mark in chamsys._SECTION_MARKERS for b in mark)


# --------------------------------------------------------------------------
# malformed input must raise ValueError, not a traceback
# --------------------------------------------------------------------------
#
# `cli.main` catches ValueError and OSError. `zipfile.BadZipFile` is neither
# (it subclasses Exception directly), and `ET.ParseError` is a SyntaxError, so
# a truncated .gdtf or a stray .xml dumped a stack trace at the user instead
# of a one-line error. The readers now translate both at the boundary.

@pytest.mark.parametrize("suffix,read", [
    (".gdtf", gdtf.read),
    (".mvr", mvr.read),
])
def test_not_a_zip_raises_value_error(tmp_path, suffix, read):
    path = tmp_path / f"broken{suffix}"
    path.write_bytes(b"this is not a zip file")
    with pytest.raises(ValueError):
        read(path)


@pytest.mark.parametrize("read", [gdtf.read, ma2.read, ma3.read])
def test_not_valid_xml_raises_value_error(tmp_path, read):
    path = tmp_path / "broken.xml"
    path.write_text("<FixtureType><unclosed>")
    with pytest.raises(ValueError):
        read(path)


def test_zip_without_scene_raises_value_error(tmp_path):
    path = tmp_path / "empty.mvr"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("readme.txt", "nothing to see")
    with pytest.raises(ValueError):
        mvr.read(path)


def test_mvr_with_unparseable_scene_raises_value_error(tmp_path):
    path = tmp_path / "bad-scene.mvr"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(mvr.SCENE_FILE, "<GeneralSceneDescription>")
    with pytest.raises(ValueError):
        mvr.read(path)


# --------------------------------------------------------------------------
# OFL documents with a malformed `modes` field
# --------------------------------------------------------------------------
#
# `ofl.parse` assumed every entry in `modes` was a dict and called .get() on
# it, so a hand-edited or truncated document raised AttributeError - again not
# caught by `main`. A non-list `modes` is a broken document and is rejected; a
# single bad entry inside a good list is skipped, because losing one mode
# beats losing the fixture.

def _ofl_doc(modes):
    return {
        "name": "Testlight",
        "categories": ["Moving Head"],
        "availableChannels": {"Dimmer": {}, "Pan": {}},
        "modes": modes,
    }


def test_ofl_modes_not_a_list_is_rejected():
    with pytest.raises(ValueError):
        ofl.parse(_ofl_doc({"name": "Basic"}))


def test_ofl_skips_a_malformed_mode_and_keeps_the_rest():
    fx = ofl.parse(_ofl_doc([
        "Basic",                                        # not a dict
        None,
        {"name": "Full", "channels": ["Dimmer", "Pan"]},
    ]))
    assert [m.name for m in fx.modes] == ["Full"]
    assert len(fx.modes[0].channels) == 2


def test_ofl_missing_modes_is_not_an_error():
    doc = _ofl_doc(None)
    del doc["modes"]
    assert ofl.parse(doc).modes == []


def test_ofl_parses_from_bytes(tmp_path):
    """The catalogue feeds parse() raw JSON bytes out of the archive."""
    raw = json.dumps(_ofl_doc([{"name": "Basic", "channels": ["Dimmer"]}])).encode()
    assert ofl.parse(raw).modes[0].name == "Basic"


# --------------------------------------------------------------------------
# the flags word, checked against a real ChamSys personality
# --------------------------------------------------------------------------
#
# build_personality emitted only `(bank << 4) | HTP/LTP`, so every generated
# head was missing the 16-bit and colour-mix bits. The 16-bit one has teeth:
# without it MagicQ shows Pan and Pan fine as two unrelated faders instead of
# one 16-bit parameter. The bits were read off 1,459,430 channel rows in the
# stock library; this pins them against one personality ChamSys wrote.

AURA = Path(__file__).parent / "data" / "martin_macauraxb_standard.txt"

_CHANNEL_ROW = re.compile(
    r'^"((?:[^"]|"")*)",([0-9a-f]{8}),([0-9a-f]{8}),\s*$', re.M
)


def _rows(text: str) -> list[tuple[str, int, int]]:
    return [(m.group(1), int(m.group(2), 16), int(m.group(3), 16))
            for m in _CHANNEL_ROW.finditer(text)]


@pytest.fixture
def aura_rows():
    return _rows(AURA.read_text())


def test_real_personality_shows_the_16bit_bits(aura_rows):
    """Ground truth: Pan/Tilt coarse carry 0x4 and their fine halves 0x8."""
    flags = {name: fl for name, fl, _ in aura_rows}
    for coarse, fine in (("Pan", "Pan F"), ("Tilt", "Tilt F")):
        assert flags[coarse] & chamsys.CH_COARSE
        assert not flags[coarse] & chamsys.CH_FINE
        assert flags[fine] & chamsys.CH_FINE
        assert not flags[fine] & chamsys.CH_COARSE


def test_real_personality_shows_the_additive_bit(aura_rows):
    flags = {name: fl for name, fl, _ in aura_rows}
    for primary in ("Red", "Green", "Blue"):
        assert flags[primary] & chamsys.CH_ADDITIVE
    # Never set on white: 0 of 94,318 rows in the stock library.
    assert not flags["White"] & chamsys.CH_ADDITIVE


def test_generated_aura_matches_chamsys_attributes_and_flags():
    """Rebuild the MAC Aura's layout and compare with ChamSys's own file.

    Names differ - ours come from the source library - so this compares the
    two things MagicQ acts on: the attribute number and the flags word.
    """
    original = _rows(AURA.read_text())

    aura = Fixture(manufacturer="Martin", model="MacAuraXB", modes=[Mode(
        name="Standard",
        channels=[
            Channel(offset=1, name="Shutter", attribute="Shutter"),
            Channel(offset=2, name="Dimmer", attribute="Dimmer", htp=True),
            Channel(offset=3, name="Zoom", attribute="Zoom"),
            Channel(offset=4, name="Pan", attribute="Pan"),
            Channel(offset=5, name="Pan fine", attribute="Pan", fine=True),
            Channel(offset=6, name="Tilt", attribute="Tilt"),
            Channel(offset=7, name="Tilt fine", attribute="Tilt", fine=True),
            Channel(offset=8, name="Control", attribute="Control"),
            Channel(offset=9, name="Col Macro", attribute="ColorWheel2"),
            Channel(offset=10, name="Red", attribute="Red"),
            Channel(offset=11, name="Green", attribute="Green"),
            Channel(offset=12, name="Blue", attribute="Blue"),
            Channel(offset=13, name="White", attribute="White"),
            Channel(offset=14, name="CTC", attribute="CTO"),
        ],
    )])
    built = _rows(chamsys.build_personality(aura))

    assert len(built) == len(original) == 14
    for (_, want_fl, want_at), (name, got_fl, got_at) in zip(original, built):
        assert got_at == want_at, f"{name}: attribute {got_at:08x} != {want_at:08x}"
        # 0x8000 appears on two of ChamSys's rows and is not modelled; 0x40000000
        # likewise. Compare the bits whose meaning is established.
        mask = 0xFFF
        assert got_fl & mask == want_fl & mask, \
            f"{name}: flags {got_fl:08x} != {want_fl:08x}"


def test_shutter_sits_on_the_beam_bank():
    """MagicQ banks the shutter with beam, not intensity - 51,210 rows agree."""
    assert (chamsys._flags_for("Shutter", False) >> 4) & 0xF == 1
    assert (chamsys._flags_for("Dimmer", True) >> 4) & 0xF == 0
    assert (chamsys._flags_for("Pan", False) >> 4) & 0xF == 3
    assert (chamsys._flags_for("Red", False) >> 4) & 0xF == 2


def test_coarse_bit_only_when_a_fine_channel_exists():
    """An 8-bit Pan must not claim to be half of a 16-bit pair."""
    eight_bit = Fixture(manufacturer="T", model="X", modes=[Mode(
        name="M", channels=[Channel(offset=1, name="Pan", attribute="Pan")])])
    (_, flags, _), = _rows(chamsys.build_personality(eight_bit))
    assert not flags & chamsys.CH_COARSE
    assert not flags & chamsys.CH_FINE
    assert flags & 0xFF == 0x32      # what the library uses for 8-bit pan


def test_colour_macro_survives_a_geometry_prefix():
    """"Beam color wheel effect" is a colour attribute, not a generic macro.

    The last-word fallback used to take "effect" and return Macro. It now
    drops the geometry prefix a word at a time, longest remainder first.
    Measured over a stock library this moved 242 channel rows onto ChamSys's
    own attribute number and left 323 fewer Unknown.
    """
    from lxtool import attributes as A

    assert A.normalise("Beam color wheel effect") == "ColorMacro"
    assert A.normalise("Color wheel effect") == "ColorMacro"
    assert A.normalise("Colour Macro") == "ColorMacro"
    assert A.normalise("Col Macro") == "ColorMacro"


def test_speed_names_still_normalise_to_speed():
    """Guards the reorder that looked right and measured worse.

    Running the patterns ahead of the last-word fallback fixes the colour
    cases but reads "Tilt Speed" as Tilt and "Pan Speed" as Pan - 12,093 rows
    moved and agreement with ChamSys got worse, so the fallback stays first.
    """
    from lxtool import attributes as A

    for name in ("Tilt Speed", "Pan Speed", "Dimmer Speed", "Gobo Speed"):
        assert A.normalise(name) == "Speed", name


def test_colour_macro_has_an_attribute_number():
    """ColorMacro used to write as Reserved (0x3f)."""
    assert chamsys._ATTR_NUMBERS["ColorMacro"] == 0x07


def test_generated_hed_round_trips():
    """What we write, we must be able to read back."""
    fx = Fixture(manufacturer="Martin", model="MacAura", modes=[Mode(
        name="Standard",
        channels=[
            Channel(offset=1, name="Dimmer", attribute="Dimmer", htp=True),
            Channel(offset=2, name="Pan", attribute="Pan"),
            Channel(offset=3, name="Pan fine", attribute="Pan", fine=True),
            Channel(offset=4, name="Red", attribute="Red"),
            Channel(offset=5, name="Green", attribute="Green"),
            Channel(offset=6, name="Blue", attribute="Blue"),
        ],
    )])
    text = chamsys.build_personality(fx)
    back = chamsys.parse_personality(chamsys.decode_hed(chamsys.encode_hed(text)))

    assert back.manufacturer == "Martin"
    assert back.model == "MacAura"
    channels = back.modes[0].channels
    assert [c.attribute for c in channels] == \
        ["Dimmer", "Pan", "Pan", "Red", "Green", "Blue"]
    assert [c.fine for c in channels] == [False, False, True, False, False, False]


def test_convert_reports_only_the_mode_it_wrote(tmp_path, capsys, monkeypatch):
    """A .hed holds one mode; convert used to claim it had written them all."""
    from lxtool import cli

    fx = Fixture(manufacturer="Martin", model="MacAura", modes=[
        Mode(name="Standard", channels=[Channel(offset=1, name="Dimmer",
                                                attribute="Dimmer", htp=True)]),
        Mode(name="Extended", channels=[Channel(offset=1, name="Dimmer",
                                                attribute="Dimmer", htp=True),
                                        Channel(offset=2, name="Pan", attribute="Pan")]),
    ])
    monkeypatch.setattr(cli, "load_fixture", lambda *a, **k: fx)

    out = tmp_path / "Martin_MacAura_Standard.hed"
    assert cli.main(["convert", str(tmp_path / "in.gdtf"), str(out)]) == 0
    printed = capsys.readouterr().out
    assert "wrote Standard (1 ch)" in printed
    assert "not written: Extended" in printed

    # And the file really does contain that mode, not the other.
    assert len(chamsys.read(out).modes[0].channels) == 1


def test_convert_mode_selection(tmp_path, capsys, monkeypatch):
    from lxtool import cli

    fx = Fixture(manufacturer="Martin", model="MacAura", modes=[
        Mode(name="Standard", channels=[Channel(offset=1, name="Dimmer",
                                                attribute="Dimmer", htp=True)]),
        Mode(name="Extended", channels=[Channel(offset=1, name="Dimmer",
                                                attribute="Dimmer", htp=True),
                                        Channel(offset=2, name="Pan", attribute="Pan")]),
    ])
    monkeypatch.setattr(cli, "load_fixture", lambda *a, **k: fx)

    out = tmp_path / "ext.hed"
    assert cli.main(["convert", str(tmp_path / "in.gdtf"), str(out), "--mode", "extended"]) == 0
    assert "wrote Extended (2 ch)" in capsys.readouterr().out
    assert len(chamsys.read(out).modes[0].channels) == 2

    with pytest.raises(SystemExit):
        cli.main(["convert", str(tmp_path / "in.gdtf"), str(out), "--mode", "Nope"])


def test_written_hed_takes_its_identity_from_the_filename(tmp_path):
    """The head must appear in Choose Head under the name on the file.

    Found on the desk: a file called Martin_MACAuraLX_Standard.hed whose
    internal fields said "MAC Aura" (straight from OFL) was indexed by MagicQ
    under MAC Aura - filed among the stock Auras, invisible under the name
    the user gave it. In 67,861 of 68,418 stock heads the internal name
    matches the filename; the writer now follows the same convention.
    """
    fx = Fixture(manufacturer="Martin", model="MAC Aura", modes=[Mode(
        name="Standard",
        channels=[Channel(offset=1, name="Dimmer", attribute="Dimmer", htp=True)],
    )])
    out = chamsys.write(fx, tmp_path / "Martin_MACAuraLX_Standard.hed")

    text = chamsys.decode_hed(out.read_bytes())
    assert '"Martin_MACAuraLX_Standard","Martin","Standard","MACAuraLX",' in text

    back = chamsys.read(out)
    assert back.manufacturer == "Martin"
    assert back.model == "MACAuraLX"
    assert back.modes[0].name == "Standard"


def test_written_hed_keeps_fixture_identity_without_a_conventional_name(tmp_path):
    """A dest like test.hed has no Manufacturer_Model_Mode to honour."""
    fx = Fixture(manufacturer="Martin", model="MAC Aura", modes=[Mode(
        name="Standard",
        channels=[Channel(offset=1, name="Dimmer", attribute="Dimmer", htp=True)],
    )])
    text = chamsys.decode_hed(chamsys.write(fx, tmp_path / "test.hed").read_bytes())
    assert '"Martin_MAC Aura_Standard","Martin","Standard","MAC Aura",' in text


def test_p_line_second_field_is_not_the_channel_count(tmp_path):
    """P,<here> is a small enum, not the footprint.

    Real heads carry 0x0000 or 0x0002 there almost universally; it equals
    the channel count in only 72 of 68,418. We wrote the count, which for
    anything bigger than a par produced a value real files never carry.
    """
    fx = Fixture(manufacturer="T", model="X", modes=[Mode(
        name="M",
        channels=[Channel(offset=i, name=f"c{i}", attribute="Dimmer")
                  for i in range(1, 15)],
    )])
    text = chamsys.build_personality(fx)
    pline = next(l for l in text.split("\n") if l.startswith("P,"))
    assert pline.startswith('P,0000,')
    # The count still appears where it belongs, on the following line.
    body = text.split("\n")
    assert body[body.index(pline) + 1].startswith("000e,")


# --------------------------------------------------------------------------
# default values - found by patching a generated head next to a stock one
# --------------------------------------------------------------------------
#
# Second on-desk result: the generated head patched and its 16-bit pairs
# worked, but the desk's Output column read 0 on every channel while the
# stock Aura idled at Pan/Tilt 128 and RGBW 255. The .hed carries a defaults
# line - count, then a (channel, value) pair per channel - which we neither
# wrote nor read. Format verified on 65,187 stock heads.

def test_real_personality_defaults_are_read():
    fx = chamsys.parse_personality(AURA.read_text())
    defaults = {c.name: c.default for c in fx.modes[0].channels}
    # These are the values the desk showed in the stock Aura's Output column.
    assert defaults["Pan"] == 128
    assert defaults["Tilt"] == 128
    assert defaults["Pan F"] == 0
    assert defaults["Dimmer"] == 255
    assert defaults["Red"] == 255
    assert defaults["White"] == 255
    assert defaults["CTC"] == 0


def test_built_personality_writes_a_defaults_line():
    fx = Fixture(manufacturer="T", model="X", modes=[Mode(name="M", channels=[
        Channel(offset=1, name="Dimmer", attribute="Dimmer", htp=True),
        Channel(offset=2, name="Pan", attribute="Pan"),
        Channel(offset=3, name="Pan fine", attribute="Pan", fine=True),
        Channel(offset=4, name="Shutter", attribute="Shutter", default=22),
    ])])
    lines = chamsys.build_personality(fx).split("\n")

    after_channels = lines[lines.index('"Shutter",00000012,00000002,') + 1]
    # Dimmer falls back to 255, Pan to centre, the fine half to 0; the
    # explicit shutter default is kept.
    assert after_channels == "0004,0000,00ff,0001,0080,0002,0000,0003,0016,"


def test_defaults_round_trip():
    fx = Fixture(manufacturer="T", model="X", modes=[Mode(name="M", channels=[
        Channel(offset=1, name="Dimmer", attribute="Dimmer", htp=True),
        Channel(offset=2, name="Pan", attribute="Pan"),
        Channel(offset=3, name="Shutter", attribute="Shutter", default=22),
    ])])
    back = chamsys.parse_personality(chamsys.build_personality(fx))
    assert [c.default for c in back.modes[0].channels] == [255, 128, 22]


def test_header_second_line_is_not_mistaken_for_defaults():
    """The header's second line also starts with the channel count.

    It is excluded because its last field is 8 hex digits - if that rule
    broke, a head's defaults would be read from the wrong line entirely.
    """
    fx = chamsys.parse_personality(AURA.read_text())
    # Header line 2 field order would give Dimmer 0, not 255.
    assert fx.modes[0].channels[1].default == 255


def test_built_personality_has_the_complete_v008f_tail():
    """The tail must be complete, not merely plausible.

    Third on-desk result: a head with an invented, truncated tail patched but
    was mis-read - phantom elements in the Hd no column ("4.245"), Pan's
    16-bit flag dropped while Tilt kept its, defaults ignored. The desk hit
    end-of-file in the middle of the section grammar and made something up.
    The writer now emits the full V,008f skeleton; the size rules here were
    checked across 2,245 stock heads of that version.
    """
    fx = Fixture(manufacturer="T", model="X", modes=[Mode(name="M", channels=[
        Channel(offset=i, name=f"c{i}", attribute="Dimmer") for i in range(1, 6)
    ])])
    lines = chamsys.build_personality(fx).rstrip("\n").split("\n")

    assert 'V,008f,"MagicQ 1";' in lines
    assert lines[-1] == ";", "file must terminate the grammar"
    assert '"{00000000-0000-0000-0000-000000000000}",' in lines
    # zeros row: one field per channel
    assert ",".join(["00000000"] * 5) + "," in lines
    # near-final row: count+1 fields after the two 8-hex ones
    assert "00000000,00000000," + ",".join(["0000"] * 6) + "," in lines
    # no truncated palette rows keyed to attribute zero
    assert "00000000,0000,0100,01ff," not in lines


def test_header_declares_the_range_row_count():
    """Field 2 of the line after P is the number of range rows.

    True in 68,417 of 68,418 stock heads, and MagicQ trusts it over the file
    contents: fourth on-desk result was that declaring zero and then writing
    95 rows made the desk parse those rows as later grammar - phantom
    elements in the Hd no column, a dropped 16-bit flag. The control heads
    proved it: ChamSys's own personality re-encoded by us was clean, and
    removing the mismatch (either way) cured ours.
    """
    from lxtool.model import Range

    fx = Fixture(manufacturer="T", model="X", modes=[Mode(name="M", channels=[
        Channel(offset=1, name="Dimmer", attribute="Dimmer", htp=True,
                ranges=[Range(0, 127, "Dim"), Range(128, 255, "Bright")]),
        Channel(offset=2, name="Gobo", attribute="Gobo1",
                ranges=[Range(0, 9, "Open"), Range(10, 19, "Stars"),
                        Range(20, 29, "")]),      # unnamed: not written
    ])])
    lines = chamsys.build_personality(fx).split("\n")
    header = lines[4].split(",")

    n_rows = sum(1 for l in lines
                 if re.fullmatch(r'[0-9a-f]{4},"(?:[^"]|"")*",[0-9a-f]{4},'
                                 r'[0-9a-f]{4},[0-9a-f]{4},[0-9a-f]{8},', l))
    assert n_rows == 4, "unnamed ranges must not be written"
    assert int(header[1], 16) == n_rows
    # macro count: none are emitted, so it must say zero
    assert int(header[3], 16) == 0


def test_palette_rows_carry_the_defaults():
    """Idle output and Locate come from the palette block, not the pairs line.

    Fifth on-desk result: with the header count fixed the head patched clean,
    but still idled at zero while ChamSys's own Aura sat at Pan/Tilt 128 and
    RGBW 255. The remaining difference was this block - and no stock head
    (of 8,000 checked) has live defaults without it. One row per channel:
    the attribute's palette id, then three 0x100|value fields.
    """
    fx = Fixture(manufacturer="T", model="X", modes=[Mode(name="M", channels=[
        Channel(offset=1, name="Dimmer", attribute="Dimmer", htp=True),
        Channel(offset=2, name="Pan", attribute="Pan"),
        Channel(offset=3, name="Pan fine", attribute="Pan", fine=True),
        Channel(offset=4, name="Red", attribute="Red"),
    ])])
    lines = chamsys.build_personality(fx).split("\n")
    rows = [l for l in lines
            if re.fullmatch(r'000000[0-9a-f]{2},[0-9a-f]{4},[0-9a-f]{4},[0-9a-f]{4},', l)]

    assert rows == [
        "00000007,01ff,01ff,01ff,",   # Dimmer id 0x07, default 255
        "00000047,0180,0180,0180,",   # Pan id 0x47, centred 128
        "00000047,0100,0100,0100,",   # Pan fine, same id, 0
        "00000080,01ff,01ff,01ff,",   # Red id 0x80, full
    ]


def test_shutter_ranges_are_typed():
    """Untyped shutter ranges are a Head Editor error.

    Sixth on-desk round: the head patches and homes perfectly and every slot
    name displays, but the editor title reads "ERRORS Shutter Types" because
    every range carried type 0 ("None"). Types are inferred from the slot
    name using the majority vocabulary of 3.69M stock rows.
    """
    from lxtool.model import Range

    fx = Fixture(manufacturer="T", model="X", modes=[Mode(name="M", channels=[
        Channel(offset=1, name="Shutter", attribute="Shutter", ranges=[
            Range(0, 19, "Closed"),
            Range(20, 24, "Open"),
            Range(25, 64, "ShutterStrobe"),
            Range(65, 69, "Rnd Strobe"),
            Range(70, 84, "Pulse Open"),
            Range(85, 255, "Sine wave"),
        ]),
        Channel(offset=2, name="Gobo", attribute="Gobo1", ranges=[
            Range(0, 9, "Open gobo"), Range(10, 19, "Stars"),
        ]),
    ])])
    text = chamsys.build_personality(fx)
    flags = {m.group(1): m.group(2) for m in re.finditer(
        r'^[0-9a-f]{4},"((?:[^"]|"")*)",[0-9a-f]{4},[0-9a-f]{4},([0-9a-f]{4}),', text, re.M)}

    assert flags["Closed"] == "2000"
    assert flags["Open"] == "1000"
    assert flags["ShutterStrobe"] == "3000"
    assert flags["Rnd Strobe"] == "5000"
    assert flags["Pulse Open"] == "9000"
    assert flags["Sine wave"] == "0000"     # not a wheel: unrecognised stays None
    assert flags["Open gobo"] == "1000"
    # Wheel slots are typed 0x1000 too - untyped wheel ranges are the same
    # Head Editor error the shutter raised, just spelled "Col Types".
    assert flags["Stars"] == "1000"


def test_ofl_shutter_capabilities_get_real_names():
    """shutterEffect must survive into the slot name.

    Seventh on-desk round: shutter ranges were typed but the Head Editor
    still flagged "ERRORS Shutter Types", because every OFL shutter
    capability had been flattened to the name "ShutterStrobe" - so the
    channel had strobes and nothing else. The effect field is where
    Open/Closed live, and OFL's RampUp is byte-identical to ChamSys's own
    "Pulse Open" on the MAC Aura.
    """
    fx = ofl.parse({
        "name": "T",
        "availableChannels": {"Shutter": {"capabilities": [
            {"dmxRange": [0, 19], "type": "ShutterStrobe", "shutterEffect": "Closed"},
            {"dmxRange": [20, 24], "type": "ShutterStrobe", "shutterEffect": "Open"},
            {"dmxRange": [25, 64], "type": "ShutterStrobe", "shutterEffect": "Strobe",
             "speedStart": "fast", "speedEnd": "slow"},
            {"dmxRange": [65, 84], "type": "ShutterStrobe", "shutterEffect": "RampUp",
             "speedStart": "fast", "speedEnd": "slow"},
            {"dmxRange": [85, 99], "type": "ShutterStrobe", "shutterEffect": "Strobe",
             "randomTiming": True},
            {"dmxRange": [100, 255], "type": "NoFunction"},
        ]}},
        "modes": [{"name": "M", "channels": ["Shutter"]}],
    })
    names = [r.name for r in fx.modes[0].channels[0].ranges]
    assert names == ["Closed", "Open", "Strobe F>S", "Pulse Open F>S",
                     "Rnd Strobe", "No Function"]

    # And the writer types them into what the Head Editor needs.
    assert chamsys._range_flags("Closed") == 0x2000
    assert chamsys._range_flags("Open") == 0x1000
    assert chamsys._range_flags("Pulse Open F>S") == 0x9000
    assert chamsys._range_flags("Rnd Strobe") == 0x5000


def test_colour_slots_get_types_and_swatches():
    """Eighth on-desk round: "ERRORS Col Types".

    Same complaint as the shutter, next channel over: colour-wheel slots
    were untyped. Fixed slots are 0x1000 and ramps 0x2000, and the extra
    field carries MagicQ's colour chart (0x06000000 | id), decoded from
    319,403 stock rows - the generated Aura's swatches now match ChamSys's
    own file id for id (Congo Blue 0x0b, Steel Blue 0x02, Fern 0x46).
    """
    from lxtool.model import Range

    fx = Fixture(manufacturer="T", model="X", modes=[Mode(name="M", channels=[
        Channel(offset=1, name="Colour", attribute="ColorWheel", ranges=[
            Range(0, 9, "No Function"),
            Range(10, 19, "LEE 181 - Congo Blue"),
            Range(20, 29, "White"),
            Range(30, 39, "Rainbow CW>"),
        ]),
    ])])
    rows = re.findall(
        r'^0000,"((?:[^"]|"")*)",[0-9a-f]{4},[0-9a-f]{4},([0-9a-f]{4}),([0-9a-f]{8}),',
        chamsys.build_personality(fx), re.M)

    assert rows == [
        ("No Function", "0000", "00000000"),
        ("LEE 181 - Congo Blue", "1000", "0600000b"),
        ("White", "1000", "06000026"),
        ("Rainbow CW>", "2000", "0600001f"),
    ]


def test_trivial_full_range_rows_are_omitted():
    """A lone 0-255 row on a continuous channel is noise ChamSys omits.

    Ninth on-desk round: colour swatches lit up, but "ERRORS Col Types"
    survived on the untyped single "ColorIntensity" 0-255 rows the RGB mix
    channels carried - rows the stock Aura simply does not have. They are
    dropped; a wheel's lone full-span slot (a one-gobo fixture) stays.
    """
    from lxtool.model import Range

    fx = Fixture(manufacturer="T", model="X", modes=[Mode(name="M", channels=[
        Channel(offset=1, name="Red", attribute="Red",
                ranges=[Range(0, 255, "ColorIntensity")]),
        Channel(offset=2, name="Gobo", attribute="Gobo1",
                ranges=[Range(0, 255, '6" Dish')]),
    ])])
    text = chamsys.build_personality(fx)
    assert "ColorIntensity" not in text
    assert '6"" Dish' in text
    header = text.split("\n")[4].split(",")
    assert int(header[1], 16) == 1


def test_every_typed_colour_slot_declares_a_colour():
    """Tenth on-desk round: Fixed colour slots with an empty Icon column.

    "ERRORS Col Types" survived every other fix; the remaining oddity was
    colour-wheel rows typed Fixed but carrying no swatch - a typed colour
    slot that declares no colour. Stock practice covers the gap: effects
    take the rainbow id (0x1f) and unrecognisable slots the generic col-N
    id (0x47).
    """
    from lxtool.model import Range

    fx = Fixture(manufacturer="T", model="X", modes=[Mode(name="M", channels=[
        Channel(offset=1, name="Colour", attribute="ColorWheel", ranges=[
            Range(0, 9, "No Function"),
            Range(10, 19, "Virtual color wheel rotation"),
            Range(20, 29, "Effect"),
            Range(30, 39, "Split col 3+4"),
        ]),
    ])])
    rows = re.findall(
        r'^0000,"(?:[^"]|"")*",[0-9a-f]{4},[0-9a-f]{4},([0-9a-f]{4}),([0-9a-f]{8}),',
        chamsys.build_personality(fx), re.M)

    assert rows == [
        ("0000", "00000000"),          # No Function: untyped, no swatch
        ("1000", "0600001f"),          # effect-ish -> rainbow id
        ("1000", "0600001f"),
        ("1000", "06000047"),          # unrecognisable -> generic col id
    ]


def test_shutter_types_match_the_stock_aura_exactly():
    """The stock Aura, opened in the Head Editor, is the answer key.

    Eleventh round: the control check came back - ChamSys's own Aura shows
    NO errors, and its editor screenshots display every flag value, which
    exposed two mismatches: the strobe nibble carries direction (3=S>F,
    4=F>S, 5/6 random), pulses split open/closed (9000/a000), and random
    pulses, bursts and sine waves stay untyped. This test walks our
    generated shutter against the stock head's types row for row.
    """
    cases = [
        ("Closed", 0x2000),
        ("Open", 0x1000),
        ("Strobe F>S", 0x4000),
        ("Strobe S>F", 0x3000),
        ("Pulse Open F>S", 0x9000),
        ("Pulse Closed F>S", 0xA000),
        ("Rnd Strobe F>S", 0x6000),
        ("Rnd Strobe S>F", 0x5000),
        ("Rnd Pulse Open F>S", 0x0000),
        ("Rnd Pulse Closed F>S", 0x0000),
        ("Burst F>S", 0x0000),
        ("Sine wave", 0x0000),
    ]
    for name, want in cases:
        assert chamsys._range_flags(name) == want, name
