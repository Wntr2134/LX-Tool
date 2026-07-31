"""Read a whole patch sheet: paste, triage, draft.

The input is whatever a patch sheet becomes when it leaves its author: a
spreadsheet export, a phone screenshot pushed through select-text-in-image,
a table pasted out of a PDF. Columns vary, separators vary, half the
numbers are spreadsheet-mangled ("8.11" for address 110). What survives
reliably is a fixture name, a channel count ("24 Ch"), and universe.address
pairs - and that is enough to plan a console file: group the rows into
fixture types, say how many of each and where they live, flag addressing
collisions, and hand each unknown type to the head builder with its channel
count already filled in.

Nothing here needs the fixtures documented anywhere. It is the step before
:mod:`lxtool.stock`: the sheet says what exists, stock drafts what nobody
wrote down.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# "24 Ch", "24ch", "24 CH (Std)", "1 Ch"
_COUNT = re.compile(r"\b(\d{1,3})\s*ch\b", re.IGNORECASE)
# "7.001", "7/001", "7-001" - universe separator address. Requires the
# address half zero-padded to 3 digits or more, because a spreadsheet that
# ever held "8.110" will render it "8.11" and there is no way back.
_UNI_ADDR = re.compile(r"\b(\d{1,3})[./-](\d{3})\b")
_SEP = re.compile(r"\s*\|\s*|\t+|\s{2,}")
# A header row, not data.
_HEADER_WORDS = ("fixture", "address", "universe", "handle", "location",
                 "position", "channel", "footprint", "mode", "type", "name",
                 "qty", "start", "end", "sheet", "patch")

# Fixture-name keywords -> stock layout kind, first hit wins. "bsw" before
# "spot" so a "BSW Moving Spot" drafts as the beam family it really is.
_KIND_HINTS = (
    ("spark", "spark"), ("strobe", "strobe"), ("bsw", "beam"),
    ("beam", "beam"), ("wash", "wash"), ("spot", "spot"),
    ("blinder", "par"), ("par", "par"), ("dimmer", "par"),
)


@dataclass
class Row:
    lineno: int
    name: str
    channels: int          # 0 when the line never said
    universe: int          # 0 when unknown
    address: int           # start address, 0 when unknown


@dataclass
class Group:
    """All the units of one fixture type at one channel count."""

    name: str
    channels: int
    rows: list[Row] = field(default_factory=list)

    @property
    def qty(self) -> int:
        return len(self.rows)

    @property
    def universes(self) -> list[int]:
        return sorted({r.universe for r in self.rows if r.universe})

    @property
    def kind_guess(self) -> str:
        low = self.name.lower()
        for word, kind in _KIND_HINTS:
            if word in low:
                return kind
        return ""


@dataclass
class Sheet:
    groups: list[Group]
    warnings: list[str]
    skipped: int           # lines that had no recognisable fixture in them


def parse(text: str) -> Sheet:
    """Group a pasted patch sheet into fixture types, and flag collisions."""
    rows: list[Row] = []
    skipped = 0

    for lineno, raw in enumerate(text.split("\n"), start=1):
        line = raw.strip()
        if not line:
            continue
        fields = [f for f in _SEP.split(line) if f.strip()]
        if _is_header(fields):
            continue

        count = 0
        m = _COUNT.search(line)
        if m:
            count = int(m.group(1))
        ua = _UNI_ADDR.search(line)
        name = _pick_name(fields)
        if not name:
            skipped += 1
            continue
        rows.append(Row(
            lineno=lineno, name=name, channels=count,
            universe=int(ua.group(1)) if ua else 0,
            address=int(ua.group(2)) if ua else 0,
        ))

    groups: dict[tuple[str, int], Group] = {}
    for r in rows:
        key = (r.name.lower(), r.channels)
        if key not in groups:
            groups[key] = Group(name=r.name, channels=r.channels)
        groups[key].rows.append(r)

    return Sheet(groups=list(groups.values()),
                 warnings=_collisions(rows), skipped=skipped)


def _is_header(fields: list[str]) -> bool:
    """A line made mostly of column-heading words, with no address in it."""
    if not fields:
        return True
    hits = sum(1 for f in fields
               if any(w in f.lower() for w in _HEADER_WORDS)
               and not _UNI_ADDR.search(f))
    return hits >= max(2, len(fields) // 2)


def _pick_name(fields: list[str]) -> str:
    """The field that reads most like a fixture name.

    The FIRST field with a real word in it wins - sheets put the fixture
    before its location, and a "longest text" rule loses "BSW Moving Spot"
    to "FOH Truss / Position" every time. Pure numbers, addresses and the
    "24 Ch" mode column never qualify.
    """
    fallback = ""
    for f in fields:
        f = f.strip()
        if _COUNT.fullmatch(f) or _UNI_ADDR.fullmatch(f):
            continue
        letters = sum(c.isalpha() for c in f)
        if letters >= 3:
            return f
        if letters and not fallback:
            fallback = f
    return fallback


def _collisions(rows: list[Row]) -> list[str]:
    """Overlapping DMX footprints, the error that eats a load-in morning."""
    out: list[str] = []
    by_uni: dict[int, list[Row]] = {}
    for r in rows:
        if r.universe and r.address and r.channels:
            by_uni.setdefault(r.universe, []).append(r)
    for uni, urows in sorted(by_uni.items()):
        urows.sort(key=lambda r: r.address)
        for a, b in zip(urows, urows[1:]):
            end = a.address + a.channels - 1
            if b.address <= end:
                out.append(
                    f"universe {uni}: {a.name} at {a.address} runs to {end} "
                    f"({a.channels} ch) and collides with {b.name} at "
                    f"{b.address} (sheet lines {a.lineno} and {b.lineno})"
                )
    return out
