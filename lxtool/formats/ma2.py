"""grandMA2 fixture-type XML reader/writer.

STATUS: written against the published shape of grandMA2 fixture exports but
**not yet validated against a real export from the desk**.  MA2's schema is
not publicly specified the way GDTF's is, so this parser is deliberately
tolerant: it strips namespaces, searches by local element name, and accepts
missing elements rather than assuming a rigid layout.  Anything it cannot
interpret becomes an ``Unknown`` attribute rather than a silent wrong guess.

Validate with ``lx doctor --ma2 <file>`` before trusting output.

For MA3, prefer GDTF: MA3 uses GDTF natively, so :mod:`lxtool.formats.gdtf`
is the correct path in both directions.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

from .. import attributes
from ..model import Channel, Fixture, Mode, Range

# MA2 attribute short names -> our canonical vocabulary.  MA2 uses terse,
# upper-case attribute keys on ChannelType elements.
MA2_ATTRIBUTES: dict[str, str] = {
    "DIM": "Dimmer", "SHUTTER": "Shutter", "STROBE": "Strobe",
    "PAN": "Pan", "TILT": "Tilt",
    "COLOR1": "ColorWheel", "COLOR2": "ColorWheel2", "COLORMACRO": "ColorMacro",
    "COLORRGB1": "Red", "COLORRGB2": "Green", "COLORRGB3": "Blue",
    "COLORRGB4": "White", "COLORRGB5": "Amber", "COLORRGB6": "UV",
    "CYAN": "Cyan", "MAGENTA": "Magenta", "YELLOW": "Yellow",
    "CTO": "CTO", "CTB": "CTB",
    "GOBO1": "Gobo1", "GOBO1_POS": "Gobo1Rot", "GOBO2": "Gobo2",
    "GOBO2_POS": "Gobo2Rot", "PRISMA1": "Prism", "PRISMA1_POS": "PrismRot",
    "FOCUS": "Focus", "ZOOM": "Zoom", "IRIS": "Iris", "FROST": "Frost",
    "CONTROL": "Control", "SPEED": "Speed", "MACRO": "Macro",
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_all(root: ET.Element, name: str) -> list[ET.Element]:
    """Find elements by local name, ignoring namespaces."""
    return [el for el in root.iter() if _local(el.tag) == name]


def _text(root: ET.Element, name: str, default: str = "") -> str:
    for el in _find_all(root, name):
        if el.text and el.text.strip():
            return el.text.strip()
    return default


def _attr_of(el: ET.Element) -> str:
    """Canonical attribute for an MA2 ChannelType element."""
    raw = (el.get("attribute") or el.get("Attribute") or el.get("name") or "").strip()
    if raw.upper() in MA2_ATTRIBUTES:
        return MA2_ATTRIBUTES[raw.upper()]
    return attributes.normalise(raw)


def parse(xml: str | bytes) -> Fixture:
    """Parse a grandMA2 fixture-type XML document."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError(f"not valid XML: {exc}") from exc

    ft_els = _find_all(root, "FixtureType")
    ft = ft_els[0] if ft_els else root

    model = (ft.get("name") or _text(ft, "Name") or "").strip()
    manufacturer = (
        ft.get("manufacturer")
        or _text(ft, "Manufacturer")
        or _text(root, "Manufacturer")
    ).strip()

    fixture = Fixture(
        manufacturer=manufacturer,
        model=model,
        source="ma2",
    )

    modules = _find_all(ft, "Module") or [ft]
    for mod in modules:
        mode = Mode(name=(mod.get("name") or "Default").strip() or "Default")

        for ct in _find_all(mod, "ChannelType"):
            attr = _attr_of(ct)
            ranges: list[Range] = []
            for fn in _find_all(ct, "ChannelFunction"):
                lo = _int(fn.get("from_dmx") or fn.get("From"), 0)
                hi = _int(fn.get("to_dmx") or fn.get("To"), 255)
                ranges.append(Range(lo, hi, (fn.get("name") or "").strip()))

            # MA2 expresses resolution via coarse/fine child elements carrying
            # a DMX offset each.
            slots: list[tuple[int, bool]] = []
            for kind, fine in (("coarse", False), ("fine", True), ("ultra", True)):
                for el in _find_all(ct, kind):
                    off = _int(el.get("dmx_offset") or el.text, 0)
                    if off:
                        slots.append((off, fine))
            if not slots:
                off = _int(ct.get("coarse") or ct.get("dmx_offset"), 0)
                fine_off = _int(ct.get("fine"), 0)
                if off:
                    slots.append((off, False))
                if fine_off:
                    slots.append((fine_off, True))

            for off, fine in slots:
                mode.channels.append(Channel(
                    offset=off,
                    name=(ct.get("attribute") or attr) + (" fine" if fine else ""),
                    attribute=attr,
                    fine=fine,
                    default=_int(ct.get("default"), 0),
                    htp=attr == "Dimmer",
                    ranges=ranges if not fine else [],
                ))

        mode.channels.sort(key=lambda c: c.offset)
        if mode.channels:
            fixture.modes.append(mode)

    if not fixture.modes:
        # Returning an empty fixture would report success on a file we did not
        # understand at all, which is worse than failing.
        raise ValueError(
            "no DMX channels found - this does not look like a grandMA2 "
            "fixture export. grandMA3 fixture XML is a different format and "
            "is not supported; export the fixture as GDTF instead."
        )

    return fixture


def _int(raw: str | None, default: int = 0) -> int:
    if raw is None:
        return default
    raw = str(raw).strip()
    try:
        return int(float(raw))
    except ValueError:
        return default


def read(path: Path | str) -> Fixture:
    path = Path(path)
    fx = parse(path.read_bytes())
    fx.source_id = path.name
    return fx


def build(fixture: Fixture) -> str:
    """Render a :class:`Fixture` as grandMA2 fixture-type XML."""
    reverse = {v: k for k, v in MA2_ATTRIBUTES.items()}

    root = ET.Element("MA", {"xmlns": "http://schemas.malighting.de/grandma2/xml/MA"})
    ET.SubElement(root, "Info", {"datetime": "", "showfile": "LX-Tool"})
    ft = ET.SubElement(root, "FixtureType", {
        "name": fixture.model or "Fixture",
        "short_name": (fixture.model or "FIX")[:8].upper(),
        "manufacturer": fixture.manufacturer or "Unknown",
    })
    modules = ET.SubElement(ft, "Modules")

    for mode in fixture.modes:
        mod = ET.SubElement(modules, "Module", {"name": mode.name})
        coarse_seen: dict[str, ET.Element] = {}
        for ch in sorted(mode.channels, key=lambda c: c.offset):
            ma_attr = reverse.get(ch.attribute, ch.attribute.upper())
            if ch.fine and ma_attr in coarse_seen:
                ET.SubElement(coarse_seen[ma_attr], "fine", {"dmx_offset": str(ch.offset)})
                continue
            ct = ET.SubElement(mod, "ChannelType", {
                "attribute": ma_attr,
                "default": str(ch.default),
            })
            ET.SubElement(ct, "coarse", {"dmx_offset": str(ch.offset)})
            for r in ch.ranges:
                ET.SubElement(ct, "ChannelFunction", {
                    "name": r.name or ch.attribute,
                    "from_dmx": str(r.dmx_from),
                    "to_dmx": str(r.dmx_to),
                })
            coarse_seen[ma_attr] = ct

    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def write(fixture: Fixture, path: Path | str) -> Path:
    path = Path(path)
    path.write_text(build(fixture), encoding="utf-8")
    return path
