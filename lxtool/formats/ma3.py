"""grandMA3 fixture-type XML reader.

MA3's library stores fixture types as XML under
``shared/lib_fixture_types/grandma3``.  The structure is GDTF-derived - MA3
is GDTF-native - but with its own conventions, so it needs its own reader
rather than going through :mod:`lxtool.formats.gdtf`::

    <GMA3 DataVersion="1.5.0.0">
      <FixtureType Name="Alienpix-RS" ShortName="APix" Manufacturer="Ayrton">
        <DMXModes>
          <DMXMode Name="Ex 16 Bit (52 ch)" Geometry="Base">
            <DMXChannels>
              <DMXChannel Coarse="1" Fine="2" Default="800000" Geometry="Base.Yoke1">
                <LogicalChannel Attribute="Pan">

Three differences from GDTF that matter:

* DMX slots are ``Coarse``/``Fine``/``Ultra`` attributes, not a single
  comma-separated ``Offset``.
* DMX values are 24-bit hex triplets (``800000``, ``7F7F7F``) rather than
  GDTF's ``value/bytecount``.
* Attributes use MA3 spellings such as ``ColorRGB_R`` and ``TiltMode``.

A ``DMXChannel`` with no ``Coarse`` is a virtual channel - typically a
virtual dimmer driven by a ``Relation`` - and occupies no DMX, so it is
skipped.

Not handled: ``GeometryReference`` expansion. A multi-element fixture repeats
a sub-geometry's channels at several DMX breaks, and the referenced channels
are counted once here rather than per instance. Where the mode name carries
its own channel count - MA3 names them like ``"Ex 16 Bit (52 ch)"`` - that
count is recorded as the declared footprint so the true size is not lost.
"""

from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree as ET

from .. import attributes
from ..model import Channel, Fixture, Mode, Range

# MA3 writes the channel count into the mode name for most library fixtures.
_MODE_COUNT_RE = re.compile(r"\((\d+)\s*ch\)", re.I)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_all(root: ET.Element, name: str) -> list[ET.Element]:
    return [el for el in root.iter() if _local(el.tag) == name]


def dmx_value(raw: str | None, default: int = 0) -> int:
    """Parse an MA3 DMX value.

    Values are hex triplets covering up to 24-bit resolution - ``FFFFFF`` is
    full, ``800000`` is half - and are normalised to 8 bits so they compare
    with every other format.
    """
    if not raw:
        return default
    raw = raw.strip()
    if not raw:
        return default
    try:
        v = int(raw, 16)
    except ValueError:
        return default
    # Take the most significant byte, whatever the width written.
    width = len(raw)
    if width > 2:
        v >>= 4 * (width - 2)
    return max(0, min(255, v))


def _int(raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def parse(xml: str | bytes) -> Fixture:
    """Parse a grandMA3 fixture-type XML document."""
    root = ET.fromstring(xml)

    ft_els = _find_all(root, "FixtureType")
    if not ft_els:
        raise ValueError("no FixtureType element - not a grandMA3 fixture file")
    ft = ft_els[0]

    fixture = Fixture(
        manufacturer=(ft.get("Manufacturer") or "").strip(),
        model=(ft.get("Name") or ft.get("ShortName") or "").strip(),
        source="ma3",
        fixture_type=(ft.get("Description") or "").strip(),
    )

    for mode_el in _find_all(ft, "DMXMode"):
        name = (mode_el.get("Name") or "Default").strip()
        mode = Mode(name=name)

        for chan in _find_all(mode_el, "DMXChannel"):
            coarse = _int(chan.get("Coarse"))
            if coarse is None:
                # Virtual channel: no DMX footprint, so it cannot affect a patch.
                continue

            logical = next(iter(_find_all(chan, "LogicalChannel")), None)
            raw_attr = (logical.get("Attribute") if logical is not None else "") or ""
            attr = attributes.normalise(raw_attr or chan.get("Geometry") or "")

            ranges: list[Range] = []
            if logical is not None:
                sets = _find_all(logical, "ChannelSet")
                for i, cs in enumerate(sets):
                    start = dmx_value(cs.get("DMXFrom"), 0)
                    if i + 1 < len(sets):
                        end = max(start, dmx_value(sets[i + 1].get("DMXFrom"), 255) - 1)
                    else:
                        end = 255
                    label = (cs.get("Name") or "").strip()
                    if label:
                        ranges.append(Range(start, end, label))

            default = dmx_value(chan.get("Default"))
            highlight = dmx_value(chan.get("Highlight")) if chan.get("Highlight") else None

            mode.channels.append(Channel(
                offset=coarse,
                name=raw_attr or f"ch{coarse}",
                attribute=attr,
                fine=False,
                default=default,
                highlight=highlight,
                htp=attr == "Dimmer",
                ranges=ranges,
            ))

            for extra in (_int(chan.get("Fine")), _int(chan.get("Ultra"))):
                if extra is None:
                    continue
                mode.channels.append(Channel(
                    offset=extra,
                    name=f"{raw_attr} fine".strip(),
                    attribute=attr,
                    fine=True,
                ))

        mode.channels.sort(key=lambda c: c.offset)

        # MA3 mode names carry the real footprint, which is authoritative when
        # GeometryReference repetition means we cannot derive it ourselves.
        m = _MODE_COUNT_RE.search(name)
        if m:
            mode.declared_count = int(m.group(1))

        if mode.channels:
            fixture.modes.append(mode)

    if not fixture.modes:
        raise ValueError("no DMX modes with channels found in this MA3 fixture")

    return fixture


def read(path: Path | str) -> Fixture:
    path = Path(path)
    fx = parse(path.read_bytes())
    fx.source_id = path.name
    return fx


def looks_like_ma3(data: bytes) -> bool:
    """True when a file is grandMA3 fixture XML rather than GDTF or MA2."""
    head = data[:2048]
    return b"<GMA3" in head or (b"FixtureType" in head and b"grandMA3" in head)
