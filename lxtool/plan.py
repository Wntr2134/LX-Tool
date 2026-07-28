"""Editable head plans: build or tweak a personality by hand.

The problem this solves is the venue clone. A mover arrives badged as a
known fixture but is really a copy with the channels in a different order -
the genuine profile patches fine and then pan sits on the colour wheel. The
fix is to start from the genuine profile as a reference, rearrange it to
match what the fixture actually does, and save it under the name you want
("China", the hire company, whoever).

A plan is deliberately plain text so it can be edited anywhere - TextEdit,
Notepad, nano on a console. One line per DMX channel, in order; reordering
the lines reorders the DMX layout. Ranges (gobo names, strobe modes) sit
indented under their channel and move with it.

    manufacturer: China
    model: AuraClone
    mode: 14ch

    channel: Shutter
      0-19    Closed
      20-24   Open
    channel: Dimmer | default=255
    channel: Pan
    channel: Pan fine
    channel: Colour Wheel | attr=ColorWheel

``dump()`` writes a plan from any fixture we can read (OFL, GDTF, .hed);
``parse()`` turns an edited plan back into a fixture, ready for
:func:`lxtool.formats.chamsys.write`.
"""

from __future__ import annotations

import re

from . import attributes
from .model import Channel, Fixture, Mode, Range

_HEADER = """\
# LX-Tool head plan.
#
# One "channel:" line per DMX channel, in order - reorder the lines to
# reorder the DMX layout. Indented lines under a channel are its named
# ranges and move with it. Blank lines and # comments are ignored.
#
# A channel line is:   channel: <Name> [| attr=<Attribute>] [| default=<0-255>] [| fine]
# The attribute is worked out from the name; use attr= when the name is
# unusual. Common attributes: Dimmer, Shutter, Strobe, Pan, Tilt, Red,
# Green, Blue, White, Amber, UV, Cyan, Magenta, Yellow, ColorWheel,
# ColorMacro, Gobo1, Gobo1Rot, Gobo2, Prism, Iris, Zoom, Focus, Frost,
# Control, Speed, Macro, CTO, CTB.
#
# Build the head with:   lx head build <this file>
# The output patches in MagicQ under the manufacturer/model/mode below.
"""

_RANGE_LINE = re.compile(r"^(\d{1,3})\s*-\s*(\d{1,3})\s+(.+?)\s*$")


class PlanError(ValueError):
    """A plan that cannot be parsed, with the line number attached."""

    def __init__(self, lineno: int, message: str):
        super().__init__(f"line {lineno}: {message}")
        self.lineno = lineno


def dump(fixture: Fixture, mode: Mode | None = None) -> str:
    """Write a fixture's mode as an editable plan."""
    mode = mode or (fixture.modes[0] if fixture.modes else Mode(name="Default"))
    out = [_HEADER]
    out.append(f"manufacturer: {fixture.manufacturer}")
    out.append(f"model: {fixture.model}")
    out.append(f"mode: {mode.name}")
    out.append("")

    for ch in sorted(mode.channels, key=lambda c: c.offset):
        parts = [ch.name]
        derived = attributes.normalise(ch.name, default="Unknown")
        if ch.attribute and ch.attribute != derived:
            parts.append(f"attr={ch.attribute}")
        if ch.default:
            parts.append(f"default={ch.default}")
        if ch.fine and not _looks_fine(ch.name):
            parts.append("fine")
        out.append("channel: " + " | ".join(parts))
        for r in ch.ranges:
            if r.name:
                out.append(f"  {r.dmx_from}-{r.dmx_to}  {r.name}")
    out.append("")
    return "\n".join(out)


def _looks_fine(name: str) -> bool:
    return attributes.normalise(name, default="") != "" and bool(
        re.search(r"\bfine\b|\blsb\b", name.lower())
    )


def parse(text: str) -> Fixture:
    """Turn an edited plan back into a fixture."""
    manufacturer = model = ""
    mode_name = "Custom"
    channels: list[Channel] = []
    current: Channel | None = None

    for lineno, raw in enumerate(text.split("\n"), start=1):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indented = line[:1] in (" ", "\t")
        if indented and current is not None:
            m = _RANGE_LINE.match(stripped)
            if not m:
                raise PlanError(
                    lineno,
                    f"expected a range like '0-19  Closed', got {stripped!r}",
                )
            lo, hi = int(m.group(1)), int(m.group(2))
            if not (0 <= lo <= hi <= 255):
                raise PlanError(lineno, f"range {lo}-{hi} is not within 0-255")
            current.ranges.append(Range(lo, hi, m.group(3)))
            continue

        key, sep, value = stripped.partition(":")
        if not sep:
            raise PlanError(
                lineno,
                f"expected 'channel: ...' or 'manufacturer/model/mode: ...', got {stripped!r}",
            )
        key = key.strip().lower()
        value = value.strip()

        if key == "manufacturer":
            manufacturer = value
        elif key == "model":
            model = value
        elif key == "mode":
            mode_name = value or mode_name
        elif key == "channel":
            current = _parse_channel(lineno, value, offset=len(channels) + 1)
            channels.append(current)
        else:
            raise PlanError(lineno, f"unknown key {key!r}")

    if not channels:
        raise PlanError(1, "the plan has no channels")
    if not model:
        raise PlanError(1, "the plan needs a 'model:' line")

    return Fixture(
        manufacturer=manufacturer,
        model=model,
        modes=[Mode(name=mode_name, channels=channels)],
        source="plan",
    )


def _parse_channel(lineno: int, value: str, *, offset: int) -> Channel:
    parts = [p.strip() for p in value.split("|")]
    name = parts[0]
    if not name:
        raise PlanError(lineno, "channel has no name")

    attr_override = ""
    default = 0
    fine = _looks_fine(name)
    for part in parts[1:]:
        if part.lower() == "fine":
            fine = True
        elif part.lower().startswith("attr="):
            attr_override = part[5:].strip()
        elif part.lower().startswith("default="):
            try:
                default = int(part[8:].strip())
            except ValueError as exc:
                raise PlanError(lineno, f"default must be a number: {part!r}") from exc
            if not 0 <= default <= 255:
                raise PlanError(lineno, f"default {default} is not within 0-255")
        elif part:
            raise PlanError(
                lineno,
                f"unknown option {part!r} (expected attr=, default= or fine)",
            )

    attribute = attr_override or attributes.normalise(name, default="Unknown")
    return Channel(
        offset=offset,
        name=name,
        attribute=attribute,
        fine=fine,
        default=default,
        htp=attribute == "Dimmer" and not fine,
    )
