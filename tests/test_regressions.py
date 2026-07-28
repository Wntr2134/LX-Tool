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
