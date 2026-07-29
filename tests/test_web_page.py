"""The web UI page must contain valid JavaScript.

A single broken string literal in the embedded <script> disables every
button on the page with no visible error - which is exactly what shipped
when a JS split call was written with one backslash in the Python source and
became a real newline, splitting the string across two lines. These checks
make that class of bug fail the suite instead of the app.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from lxtool.web.app import PAGE

SQ = "'"
DQ = '"'
BT = "`"


def _script() -> str:
    return re.search(r"<script>(.*?)</script>", PAGE, re.S).group(1)


def test_no_quoted_js_string_spans_a_newline():
    """A JS '...' or "..." string must never contain a raw newline.

    Walks the script with a small state machine that understands strings,
    template literals, regex literals and comments, so a regex like /"/g does
    not raise a false alarm. A newline reached while inside a single- or
    double-quoted string is the bug that silently disabled every button.
    """
    js = _script()
    state = None          # None | SQ | DQ | BT | "//" | "/*" | "/re"
    prev_sig = ""         # last significant char: regex-vs-divide disambiguation
    i = 0
    while i < len(js):
        c = js[i]
        nxt = js[i + 1] if i + 1 < len(js) else ""
        if state in (SQ, DQ):
            if c == "\\":
                i += 2
                continue
            if c == "\n":
                pytest.fail(f"a {state} string contains a raw newline near "
                            f"offset {i} - the string is split across lines")
            if c == state:
                state = None
        elif state == BT:
            if c == "\\":
                i += 2
                continue
            if c == BT:
                state = None
        elif state == "//":
            if c == "\n":
                state = None
        elif state == "/*":
            if c == "*" and nxt == "/":
                state = None
                i += 2
                continue
        elif state == "/re":
            if c == "\\":
                i += 2
                continue
            if c == "/":
                state = None
        else:
            if c in (SQ, DQ, BT):
                state = c
            elif c == "/" and nxt == "/":
                state = "//"
                i += 2
                continue
            elif c == "/" and nxt == "*":
                state = "/*"
                i += 2
                continue
            elif c == "/" and prev_sig in "(,=:[!&|?{};+":
                state = "/re"
        if not c.isspace():
            prev_sig = c
        i += 1
    assert state is None, f"script ended inside a {state!r} literal"


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_embedded_js_parses():
    """If node is present, the whole script must parse.

    The script is piped to node as explicit UTF-8 bytes: it contains a
    non-Latin-1 glyph (the drag grip), and letting subprocess encode stdin
    with the platform default fails on Windows, whose default is cp1252.
    """
    proc = subprocess.run(
        ["node", "--check", "-"], input=_script().encode("utf-8"),
        capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")


def test_page_has_the_expected_sections():
    for marker in ("Make your own head", "My saved heads", "chanlist",
                   "planToRows", "headBuild"):
        assert marker in PAGE, f"missing {marker!r}"
