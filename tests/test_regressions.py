"""Regressions for bugs that reached main.

Each test here exists because something shipped broken. The comment on each
says what broke and, more usefully, why the existing suite did not catch it.
"""

from __future__ import annotations

import json
import zipfile

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


def _encode_section(text: str) -> bytes:
    """Encode one heads.all section.

    Same cipher as a .hed, but a container section starts on the keystream
    proper - a standalone .hed uses key 0 for its first character, where the
    container uses 127. Verified against a real heads.all, whose first byte
    decodes to 'P' only under this convention.
    """
    out = bytearray()
    i = 0
    for ch in text:
        if ch == "\n":
            out.append(0x0A)
            continue
        out.append((ord(ch) ^ chamsys._KEYBLOCK[i % len(chamsys._KEYBLOCK)]) | 0x80)
        i += 1
    return bytes(out)


def _container_bytes(fixtures: list[Fixture]) -> bytes:
    """Build a synthetic heads.all.

    Each member is its own section with the keystream restarted, which is what
    the real container does and what `decode_container` resynchronises against.
    """
    out = bytearray()
    for fx in fixtures:
        name = f"{fx.manufacturer}@{fx.model}.hed".lower()
        body = f'PP,"{name}","Sun Jul 26 17:14:58 2026",0000,0000;\n'
        body += chamsys.build_personality(fx) + "\n"
        out += _encode_section(body)
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


def test_container_sections_are_anchored_not_guessed(monkeypatch):
    """Section headers must survive without help from the printability probe.

    Building this container exposed the weakness: the second section's first
    two characters stayed printable under the previous section's phase, so the
    resync fired late and ate the `PP,"...hed"` line - and a section whose
    header is gone is not merely garbled, it is never found. Anchoring on the
    ciphered header fixes it. Clearing the marker table reproduces the old
    behaviour, which is what makes this a regression test rather than a
    restatement of the code.
    """
    data = _container_bytes([
        _fixture("Alpha", ["Dimmer", "Pan", "Tilt", "Red", "Green", "Blue"]),
        _fixture("Beta", ["Dimmer", "Shutter", "Cyan", "Magenta", "Yellow"]),
    ])

    names = [n for n, _ in chamsys.iter_container_heads(chamsys.decode_container(data))]
    assert names == ["testco@alpha.hed", "testco@beta.hed"]

    monkeypatch.setattr(chamsys, "_SECTION_MARKERS", {})
    unanchored = [n for n, _ in chamsys.iter_container_heads(chamsys.decode_container(data))]
    assert unanchored != names, "probe-only decoding no longer misses the boundary"


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
