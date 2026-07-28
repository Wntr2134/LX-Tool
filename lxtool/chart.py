"""Turn a pasted DMX chart into a draft fixture.

Manuals and screenshots are not machine-readable, but the text inside them
is one gesture away: every recent iPhone, Android and Mac OCRs text out of
a photo natively (select the text in the image and copy). Paste that text
here and the messy table becomes a draft head plan to review and build.

The parser is deliberately tolerant, because manual layouts are chaos:

    1  Pan
    2. Pan fine
    CH 3 - Dimmer
    4 | Shutter
    0-9    Open
    10-63  Strobe slow to fast
    5-6    Zoom (16 bit)

Rules of thumb it applies:

* a leading number that matches the next expected channel starts a channel;
  ``5-6 Zoom`` at channel 5 becomes a coarse+fine 16-bit pair
* any other ``lo-hi name`` line is a named range on the current channel
* lines with no leading number (column headers, page furniture) are skipped

The output is a *draft*: it goes to a plan for a human to check, never
straight to the desk.
"""

from __future__ import annotations

import re

from . import attributes
from .model import Channel, Fixture, Mode, Range

# "CH 3 - Dimmer", "3. Dimmer", "3-4 Pan (16bit)", "3 | Dimmer", "3\tDimmer"
_LINE = re.compile(
    r"^(?:ch(?:annel)?\s*)?(\d{1,3})(?:\s*[-–—]\s*(\d{1,3}))?"
    r"\s*[:.|\-–—\t ]\s*(.+?)\s*$",
    re.IGNORECASE,
)
_NOISE = re.compile(r"^[\s>*•●\-–—|]+|[\s|]+$")


def parse_chart(text: str, *, manufacturer: str = "", model: str = "",
                mode: str = "") -> Fixture:
    """Best-effort parse of a pasted DMX chart into a draft fixture."""
    channels: list[Channel] = []
    current: Channel | None = None
    skipped: list[str] = []

    for raw in text.split("\n"):
        line = _NOISE.sub("", raw.strip())
        if not line:
            continue
        m = _LINE.match(line)
        if not m:
            if any(c.isdigit() for c in line):
                skipped.append(line)
            continue

        n1 = int(m.group(1))
        n2 = int(m.group(2)) if m.group(2) else None
        name = _clean_name(m.group(3))
        expected = len(channels) + 1

        if n1 == expected and (n2 is None or n2 >= expected):
            if not name:
                continue
            current = _channel(expected, name)
            channels.append(current)
            if n2 is not None and n2 > n1:
                # "5-6 Zoom" - a 16-bit parameter spanning two addresses.
                base = re.sub(r"\s*\(?\b16\s*bit\b\)?", "", name).strip() or name
                for extra in range(n1 + 1, min(n2, n1 + 3) + 1):
                    fine = _channel(extra, f"{base} fine", fine=True,
                                    attribute=current.attribute)
                    channels.append(fine)
        elif n2 is not None and current is not None and 0 <= n1 <= n2 <= 255:
            current.ranges.append(Range(n1, n2, name))
        elif n1 == expected - 1 and current is not None and not current.ranges \
                and n2 is None and name and not _clean_name(current.name):
            current.name = name
        else:
            skipped.append(line)

    if not channels:
        raise ValueError(
            "no channel rows recognised - expected lines like '1  Pan' or "
            "'CH 3 - Dimmer'"
        )

    fixture = Fixture(
        manufacturer=manufacturer,
        model=model or "FromChart",
        modes=[Mode(name=mode or f"{len(channels)}ch", channels=channels)],
        source="chart",
    )
    fixture.source_id = "; ".join(skipped[:5])
    return fixture


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip(" .:|-")
    # Drop a trailing "channel"/"control" qualifier: "Dimmer channel" -> Dimmer
    name = re.sub(r"\s+channel$", "", name, flags=re.IGNORECASE)
    return name


def _channel(offset: int, name: str, *, fine: bool = False,
             attribute: str = "") -> Channel:
    attr = attribute or attributes.normalise(name, default="Unknown")
    # "(16 bit)" on a row names a 16-bit *parameter*, usually the coarse
    # half - only "fine"/"LSB" mark the fine half.
    is_fine = fine or bool(re.search(r"\bfine\b|\blsb\b", name.lower()))
    return Channel(
        offset=offset,
        name=name,
        attribute=attr,
        fine=is_fine,
        htp=attr == "Dimmer" and not is_fine,
    )
