"""Catalogue tests.

These build a synthetic archive rather than hitting the network, so the suite
stays fast and works offline. The live endpoint is exercised by ``lx fetch``.
"""

from __future__ import annotations

import json
import zipfile

import pytest

from lxtool.catalog import Catalog


def _doc(name, mfr, key, modes, categories=("Moving Head",)):
    return {
        "name": name,
        "manufacturerKey": mfr,
        "fixtureKey": key,
        "categories": list(categories),
        "availableChannels": {
            "Pan": {"fineChannelAliases": ["Pan fine"]},
            "Dimmer": {},
            "Red": {}, "Green": {}, "Blue": {},
        },
        "modes": [{"name": n, "channels": ch} for n, ch in modes],
    }


@pytest.fixture
def catalog(tmp_path):
    docs = {
        "martin/mac-700-wash.json": _doc(
            "MAC 700 Wash", "martin", "mac-700-wash",
            [("Basic", ["Pan", "Pan fine", "Dimmer"]),
             ("Extended", ["Pan", "Pan fine", "Dimmer", "Red", "Green"])],
        ),
        "martin/mac-250-beam.json": _doc("MAC 250 Beam", "martin", "mac-250-beam",
                                         [("16-Bit", ["Pan", "Dimmer"])]),
        "chauvet-dj/rogue-r2.json": _doc("Rogue R2", "chauvet-dj", "rogue-r2",
                                         [("Standard", ["Pan", "Dimmer", "Red"])]),
        "eurolite/led-par-56.json": _doc("LED PAR-56", "eurolite", "led-par-56",
                                         [("RGB", ["Red", "Green", "Blue"])],
                                         categories=("Color Changer",)),
        "junk/broken.json": {"not": "a fixture"},
    }
    path = Catalog.archive_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, doc in docs.items():
            zf.writestr(name, json.dumps(doc))
    return Catalog.load(tmp_path)


def test_load_skips_non_fixtures(catalog):
    # The malformed document must be dropped, not crash the load.
    assert len(catalog) == 4
    assert all(e.name for e in catalog.entries)


def test_entry_metadata(catalog):
    e = catalog.get("martin/mac-700-wash")
    assert e is not None
    assert e.name == "MAC 700 Wash"
    assert e.manufacturer == "Martin"
    assert e.label == "Martin MAC 700 Wash"
    assert e.modes == (("Basic", 3), ("Extended", 5))


def test_get_unknown_key(catalog):
    assert catalog.get("nope/nothing") is None


def test_search_ranks_exact_name_first(catalog):
    hits = catalog.search("mac 700 wash")
    assert hits[0].key == "martin/mac-700-wash"


def test_search_whole_word_beats_substring(catalog):
    hits = catalog.search("rogue")
    assert hits[0].key == "chauvet-dj/rogue-r2"


def test_search_matches_categories(catalog):
    hits = catalog.search("color changer")
    assert [h.key for h in hits] == ["eurolite/led-par-56"]


def test_search_empty_query(catalog):
    assert catalog.search("") == []
    assert catalog.search("   ") == []


def test_search_no_hits(catalog):
    assert catalog.search("definitely-not-a-fixture") == []


def test_channel_hint_breaks_ties(catalog):
    """A channel-count hint should promote a fixture that has such a mode."""
    plain = catalog.search_scored("mac")
    hinted = catalog.search_scored("mac", channels=5)
    score_of = lambda pairs, key: next(s for s, e in pairs if e.key == key)
    assert score_of(hinted, "martin/mac-700-wash") > score_of(plain, "martin/mac-700-wash")


def test_search_scored_is_ordered(catalog):
    scores = [s for s, _ in catalog.search_scored("mac")]
    assert scores == sorted(scores, reverse=True)


def test_to_fixture_uses_display_manufacturer(catalog):
    fx = catalog.get("martin/mac-700-wash").to_fixture()
    assert fx.manufacturer == "Martin"          # not the lower-case "martin" key
    assert fx.model == "MAC 700 Wash"
    assert fx.source_id == "martin/mac-700-wash"
    assert len(fx.modes) == 2
    assert fx.modes[0].channels[1].fine is True


def test_manufacturers_counts(catalog):
    assert catalog.manufacturers() == {"Chauvet Dj": 1, "Eurolite": 1, "Martin": 2}


def test_load_without_cache_is_explicit(tmp_path):
    with pytest.raises(FileNotFoundError, match="lx fetch"):
        Catalog.load(tmp_path / "empty")
