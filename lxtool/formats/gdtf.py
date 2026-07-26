"""GDTF (General Device Type Format) reader and writer.

A ``.gdtf`` file is a zip archive whose ``description.xml`` describes the
fixture.  The structure that matters for patching is::

    FixtureType
      DMXModes
        DMXMode name=...
          DMXChannels
            DMXChannel Offset="1,2" Default="..." Highlight="...">
              LogicalChannel Attribute="Pan">
                ChannelFunction Attribute="Pan" DMXFrom="0/1" .../>

``Offset`` is a comma-separated list of 1-based DMX slots, coarse first, so
``Offset="1,2"`` is a 16-bit parameter occupying slots 1 and 2.

Writing GDTF matters as much as reading it: MagicQ imports GDTF natively, so
emitting GDTF is the supported route to get a fixture into ChamSys without
touching the obfuscated ``.hed`` format at all.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .. import attributes
from ..model import Channel, Fixture, Mode, Range

_DESCRIPTION = "description.xml"


def _dmx_value(raw: str | None, default: int = 0) -> int:
    """Parse a GDTF DMXValue like ``"128/1"`` (value/byte-count).

    Values are normalised to 8-bit so that defaults from a 16-bit channel are
    comparable with an 8-bit one.
    """
    if not raw:
        return default
    raw = raw.strip()
    if not raw or raw.lower() == "none":
        return default
    value, _, size = raw.partition("/")
    try:
        v = int(value)
        n = int(size) if size else 1
    except ValueError:
        return default
    if n > 1:
        v >>= 8 * (n - 1)
    return max(0, min(255, v))


def _offsets(raw: str | None) -> list[int]:
    if not raw:
        return []
    out = []
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.append(int(part))
    return out


def _channel_name(chan: ET.Element, logical: ET.Element | None) -> str:
    """Best available human name for a DMX channel."""
    for candidate in (
        chan.get("Name"),
        logical.get("Attribute") if logical is not None else None,
        chan.get("Geometry"),
    ):
        if candidate:
            return candidate
    return "Unknown"


def parse_description(xml: str | bytes) -> Fixture:
    """Parse a GDTF ``description.xml`` into a :class:`Fixture`."""
    root = ET.fromstring(xml)
    ft = root.find(".//FixtureType")
    if ft is None:
        raise ValueError("no FixtureType element - not a GDTF description")

    fixture = Fixture(
        manufacturer=(ft.get("Manufacturer") or "").strip(),
        model=(ft.get("LongName") or ft.get("Name") or "").strip(),
        source="gdtf",
        fixture_type=(ft.get("FixtureTypeID") or "").strip(),
    )

    for mode_el in ft.findall(".//DMXModes/DMXMode"):
        mode = Mode(name=(mode_el.get("Name") or "Default").strip())

        for chan in mode_el.findall(".//DMXChannels/DMXChannel"):
            offsets = _offsets(chan.get("Offset"))
            if not offsets:
                # A channel with no offset is a virtual channel - it has no DMX
                # footprint, so it cannot affect patching.
                continue

            logical = chan.find("LogicalChannel")
            raw_name = _channel_name(chan, logical)
            attr = attributes.normalise(
                (logical.get("Attribute") if logical is not None else None) or raw_name
            )

            ranges: list[Range] = []
            if logical is not None:
                funcs = logical.findall("ChannelFunction")
                for i, fn in enumerate(funcs):
                    start = _dmx_value(fn.get("DMXFrom"), 0)
                    # A function runs until the next one begins.
                    if i + 1 < len(funcs):
                        end = max(start, _dmx_value(funcs[i + 1].get("DMXFrom"), 255) - 1)
                    else:
                        end = 255
                    ranges.append(Range(start, end, (fn.get("Name") or fn.get("Attribute") or "").strip()))

            default = _dmx_value(chan.get("Default"))
            highlight_raw = chan.get("Highlight")
            highlight = _dmx_value(highlight_raw, 0) if highlight_raw else None

            for i, off in enumerate(offsets):
                mode.channels.append(
                    Channel(
                        offset=off,
                        name=raw_name if i == 0 else f"{raw_name} fine",
                        attribute=attr,
                        fine=i > 0,
                        default=default if i == 0 else 0,
                        highlight=highlight if i == 0 else None,
                        htp=attr == "Dimmer",
                        ranges=ranges if i == 0 else [],
                    )
                )

        mode.channels.sort(key=lambda c: c.offset)
        fixture.modes.append(mode)

    return fixture


def read(path: Path | str) -> Fixture:
    """Read a ``.gdtf`` archive (or a bare ``description.xml``)."""
    path = Path(path)
    if path.suffix.lower() == ".xml":
        fx = parse_description(path.read_bytes())
    else:
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(_DESCRIPTION)]
            if not names:
                raise ValueError(f"{path.name} has no {_DESCRIPTION}")
            fx = parse_description(zf.read(names[0]))
    fx.source_id = path.name
    return fx


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def build_description(fixture: Fixture) -> str:
    """Render a :class:`Fixture` as a minimal but valid GDTF description.

    This emits the DMX-relevant subset: attribute definitions, one geometry,
    and the DMX modes.  It is deliberately not a full GDTF (no physical
    descriptions, wheels or models) because the target - importing a
    personality into a console - only consumes the DMX layout.
    """
    used = sorted({c.attribute for m in fixture.modes for c in m.channels})

    root = ET.Element("GDTF", {"DataVersion": "1.2"})
    ft = ET.SubElement(root, "FixtureType", {
        "Name": fixture.model or "Fixture",
        "LongName": fixture.model or "Fixture",
        "ShortName": (fixture.model or "Fixture")[:16],
        "Manufacturer": fixture.manufacturer or "Unknown",
        "Description": f"Generated by LX-Tool from {fixture.source or 'unknown source'}",
        "FixtureTypeID": "00000000-0000-0000-0000-000000000000",
    })

    attr_defs = ET.SubElement(ft, "AttributeDefinitions")
    ET.SubElement(attr_defs, "ActivationGroups")
    feature_groups = ET.SubElement(attr_defs, "FeatureGroups")
    for group in sorted({attributes.group_of(a) for a in used}):
        fg = ET.SubElement(feature_groups, "FeatureGroup", {"Name": group, "Pretty": group.title()})
        ET.SubElement(fg, "Feature", {"Name": group})
    attrs_el = ET.SubElement(attr_defs, "Attributes")
    for a in used:
        ET.SubElement(attrs_el, "Attribute", {
            "Name": a,
            "Pretty": a,
            "Feature": f"{attributes.group_of(a)}.{attributes.group_of(a)}",
        })

    geometries = ET.SubElement(ft, "Geometries")
    ET.SubElement(geometries, "Geometry", {"Name": "Body", "Model": "", "Position": _IDENTITY})

    modes_el = ET.SubElement(ft, "DMXModes")
    for mode in fixture.modes:
        mode_el = ET.SubElement(modes_el, "DMXMode", {"Name": mode.name, "Geometry": "Body"})
        chans_el = ET.SubElement(mode_el, "DMXChannels")

        # Re-pair fine channels with their coarse partner for the Offset list.
        by_attr: dict[str, list[Channel]] = {}
        for c in sorted(mode.channels, key=lambda c: c.offset):
            by_attr.setdefault(f"{c.attribute}@{_coarse_offset(mode, c)}", []).append(c)

        for chans in by_attr.values():
            coarse = next((c for c in chans if not c.fine), chans[0])
            offsets = ",".join(str(c.offset) for c in chans)
            chan_el = ET.SubElement(chans_el, "DMXChannel", {
                "DMXBreak": "1",
                "Offset": offsets,
                "Default": f"{coarse.default}/1",
                "Highlight": f"{coarse.highlight}/1" if coarse.highlight is not None else "None",
                "Geometry": "Body",
            })
            log_el = ET.SubElement(chan_el, "LogicalChannel", {
                "Attribute": coarse.attribute,
                "Snap": "No",
                "Master": "None",
            })
            if coarse.ranges:
                for r in coarse.ranges:
                    ET.SubElement(log_el, "ChannelFunction", {
                        "Name": r.name or coarse.attribute,
                        "Attribute": coarse.attribute,
                        "DMXFrom": f"{r.dmx_from}/1",
                        "Default": f"{coarse.default}/1",
                    })
            else:
                ET.SubElement(log_el, "ChannelFunction", {
                    "Name": coarse.attribute,
                    "Attribute": coarse.attribute,
                    "DMXFrom": "0/1",
                    "Default": f"{coarse.default}/1",
                })

    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


_IDENTITY = "{1.000000,0.000000,0.000000,0.000000}{0.000000,1.000000,0.000000,0.000000}{0.000000,0.000000,1.000000,0.000000}{0.000000,0.000000,0.000000,1.000000}"


def _coarse_offset(mode: Mode, chan: Channel) -> int:
    """Offset of the coarse channel a fine channel belongs to.

    Fine channels sit immediately after their coarse partner in almost every
    real personality, so walk backwards to the nearest non-fine channel with
    the same attribute.
    """
    if not chan.fine:
        return chan.offset
    for other in sorted(mode.channels, key=lambda c: -c.offset):
        if other.offset < chan.offset and not other.fine and other.attribute == chan.attribute:
            return other.offset
    return chan.offset


def write(fixture: Fixture, path: Path | str) -> Path:
    """Write ``fixture`` as a ``.gdtf`` archive ready for MagicQ import."""
    path = Path(path)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(_DESCRIPTION, build_description(fixture))
    return path
