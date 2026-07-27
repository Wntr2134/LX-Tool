"""GDTF Share client.

The authenticated calls cannot run in CI - they need a real account - so
these cover the parts that can be checked without one: how responses are
interpreted, how failures are reported, and that credentials never reach
disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lxtool import gdtfshare
from lxtool.gdtfshare import GdtfShareError, ShareEntry


# --------------------------------------------------------------------------
# naming
# --------------------------------------------------------------------------

@pytest.mark.parametrize("entry,expected", [
    (ShareEntry(1, "Robe", "Robin 600"), "Robe@Robin 600.gdtf"),
    (ShareEntry(2, "Martin", "MAC 700", "1.2"), "Martin@MAC 700@1.2.gdtf"),
    (ShareEntry(3, "A/B", "C:D"), "A_B@C_D.gdtf"),
    (ShareEntry(4, "", "Nameless"), "@Nameless.gdtf"),
])
def test_filename_is_safe_and_descriptive(entry, expected):
    assert entry.filename == expected


def test_label():
    assert ShareEntry(1, "Robe", "Robin 600").label == "Robe Robin 600"
    assert ShareEntry(1, "", "Thing").label == "Thing"


# --------------------------------------------------------------------------
# searching a listing
# --------------------------------------------------------------------------

LISTING = [
    ShareEntry(1, "Martin", "MAC 700 Wash"),
    ShareEntry(2, "Martin", "MAC 250 Krypton"),
    ShareEntry(3, "Robe", "Robin 600 LED Wash"),
]


@pytest.mark.parametrize("query,expected", [
    ("mac 700", [1]),
    ("martin", [1, 2]),
    ("wash", [1, 3]),
    ("robe robin", [3]),
    ("nothing here", []),
])
def test_search(query, expected):
    assert [e.rid for e in gdtfshare.search(LISTING, query)] == expected


def test_empty_query_returns_everything():
    assert gdtfshare.search(LISTING, "  ") == LISTING


# --------------------------------------------------------------------------
# response parsing
# --------------------------------------------------------------------------

def _fake_request(monkeypatch, payload: bytes):
    monkeypatch.setattr(gdtfshare, "_request", lambda *a, **k: payload)


def test_fetch_list_accepts_a_wrapped_list(monkeypatch, tmp_path):
    _fake_request(monkeypatch, json.dumps({
        "result": True,
        "list": [
            {"rid": 7, "manufacturer": "Robe", "fixture": "Pointe", "revision": "2"},
            {"rid": 8, "manufacturer": "Martin", "name": "MAC", "creator": "someone"},
        ],
    }).encode())

    entries = gdtfshare.fetch_list(cache=tmp_path)
    assert [e.rid for e in entries] == [7, 8]
    assert entries[0].fixture == "Pointe"
    assert entries[1].fixture == "MAC"          # falls back to "name"


def test_fetch_list_accepts_a_bare_list(monkeypatch, tmp_path):
    _fake_request(monkeypatch, json.dumps(
        [{"id": 3, "manufacturer": "Ayrton", "fixture": "Alienpix"}]
    ).encode())
    entries = gdtfshare.fetch_list(cache=tmp_path)
    assert entries[0].rid == 3


def test_fetch_list_skips_rows_with_no_id(monkeypatch, tmp_path):
    _fake_request(monkeypatch, json.dumps({
        "list": [{"manufacturer": "X", "fixture": "Y"}, {"rid": 5, "fixture": "Z"}],
    }).encode())
    assert [e.rid for e in gdtfshare.fetch_list(cache=tmp_path)] == [5]


def test_fetch_list_reports_a_refusal(monkeypatch, tmp_path):
    _fake_request(monkeypatch, json.dumps(
        {"result": False, "error": "Unauthorized."}
    ).encode())
    with pytest.raises(GdtfShareError, match="Unauthorized"):
        gdtfshare.fetch_list(cache=tmp_path)


def test_non_json_response_is_reported(monkeypatch, tmp_path):
    _fake_request(monkeypatch, b"<html>maintenance</html>")
    with pytest.raises(GdtfShareError, match="not JSON"):
        gdtfshare.fetch_list(cache=tmp_path)


# --------------------------------------------------------------------------
# downloading
# --------------------------------------------------------------------------

def test_download_rejects_a_non_archive(monkeypatch, tmp_path):
    """An error page must not be saved as though it were a fixture."""
    _fake_request(monkeypatch, b'{"result":false,"error":"Unauthorized."}')
    with pytest.raises(GdtfShareError, match="not a GDTF archive"):
        gdtfshare.download(ShareEntry(1, "Robe", "Pointe"), tmp_path, cache=tmp_path)
    assert list(tmp_path.glob("*.gdtf")) == []


def test_download_writes_the_archive(monkeypatch, tmp_path):
    _fake_request(monkeypatch, b"PK\x03\x04rest-of-a-zip")
    out = gdtfshare.download(ShareEntry(1, "Robe", "Pointe"), tmp_path, cache=tmp_path)
    assert out.name == "Robe@Pointe.gdtf"
    assert out.read_bytes().startswith(b"PK")
    assert not list(tmp_path.glob("*.part"))     # no litter on success


def test_download_skips_existing_unless_overwriting(monkeypatch, tmp_path):
    entry = ShareEntry(1, "Robe", "Pointe")
    (tmp_path / entry.filename).write_bytes(b"PK-original")

    _fake_request(monkeypatch, b"PK-new-content")
    gdtfshare.download(entry, tmp_path, cache=tmp_path)
    assert (tmp_path / entry.filename).read_bytes() == b"PK-original"

    gdtfshare.download(entry, tmp_path, cache=tmp_path, overwrite=True)
    assert (tmp_path / entry.filename).read_bytes() == b"PK-new-content"


# --------------------------------------------------------------------------
# credential handling
# --------------------------------------------------------------------------

def test_login_stores_no_password(monkeypatch, tmp_path):
    """Only the session cookie may reach disk."""
    captured = {}

    def fake_request(opener, url, data=None, timeout=60):
        captured["data"] = data
        return b'{"result": true}'

    monkeypatch.setattr(gdtfshare, "_request", fake_request)
    gdtfshare.login("me@example.com", "hunter2", cache=tmp_path)

    assert b"hunter2" in captured["data"]          # sent, as it must be
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"hunter2" not in path.read_bytes(), f"password leaked into {path}"


def test_login_failure_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(gdtfshare, "_request",
                        lambda *a, **k: b'{"result": false, "error": "No valid user"}')
    with pytest.raises(GdtfShareError, match="No valid user"):
        gdtfshare.login("x", "y", cache=tmp_path)


def test_logged_in_and_logout(tmp_path):
    assert gdtfshare.logged_in(tmp_path) is False
    assert gdtfshare.logout(tmp_path) is False

    (tmp_path / "gdtf-share-session.txt").write_text("# Netscape HTTP Cookie File\n")
    assert gdtfshare.logged_in(tmp_path) is True
    assert gdtfshare.logout(tmp_path) is True
    assert gdtfshare.logged_in(tmp_path) is False
