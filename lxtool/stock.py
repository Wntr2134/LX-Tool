"""Stock draft layouts for undocumented fixtures.

The tour-in-a-third-world-venue problem: the patch sheet says "22C Moving
Head, 22 Ch" and that is ALL the information that exists. No brand, no
manual, nothing online. The fixtures themselves are OEM clones, and the
good news is that the cheap-fixture world is deeply conventional: nearly
every Chinese beam uses the same colour-first layout, nearly every LED
spot and wash the same pan-first one. A draft head built from those
conventions is roughly right, and roughly right plus ten minutes of
fader-testing at load-in beats an empty console the night before a show.

Each layout is an ordered list of channels tagged with a priority. For a
requested channel count the highest-priority channels are kept (a fine
channel is never kept without its coarse partner, because the coarse line
always sits earlier at the same-or-lower priority), and any channels
beyond the layout become unnamed placeholders to identify on the day.

The generated plan opens with the fader test instructions, so the person
holding it at load-in does not need anyone's help to finish the job.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class _Chan:
    prio: int
    line: str                      # everything after "channel: "
    ranges: tuple[str, ...] = ()   # pre-indented range lines


_KINDS: dict[str, tuple[str, list[_Chan]]] = {
    "beam": ("Beam (lamp, colour-first — 380W/230W family)", [
        _Chan(1, "Colour Wheel | attr=ColorWheel"),
        _Chan(1, "Shutter Strobe | attr=Shutter"),
        _Chan(1, "Dimmer"),
        _Chan(1, "Gobo Wheel | attr=Gobo1"),
        _Chan(2, "Prism | attr=Prism"),
        _Chan(2, "Prism Rotation | attr=Prism"),
        _Chan(2, "Frost"),
        _Chan(2, "Focus"),
        _Chan(1, "Pan"),
        _Chan(1, "Pan fine"),
        _Chan(1, "Tilt"),
        _Chan(1, "Tilt fine"),
        _Chan(2, "Pan Tilt Speed | attr=Speed"),
        _Chan(2, "Reset Lamp Control | attr=Control"),
        _Chan(3, "Prism 2 | attr=Prism"),
        _Chan(3, "Effect Macro | attr=Macro"),
        _Chan(4, "Halo Red | attr=Red"),
        _Chan(4, "Halo Green | attr=Green"),
        _Chan(4, "Halo Blue | attr=Blue"),
        _Chan(5, "Halo Macro | attr=ColorMacro"),
    ]),
    "spot": ("Spot (LED, pan-first)", [
        _Chan(1, "Pan"),
        _Chan(1, "Pan fine"),
        _Chan(1, "Tilt"),
        _Chan(1, "Tilt fine"),
        _Chan(2, "Pan Tilt Speed | attr=Speed"),
        _Chan(1, "Dimmer"),
        _Chan(1, "Shutter Strobe | attr=Shutter"),
        _Chan(1, "Colour Wheel | attr=ColorWheel"),
        _Chan(1, "Gobo Wheel | attr=Gobo1"),
        _Chan(2, "Gobo Rotation | attr=Gobo1Rot"),
        _Chan(2, "Focus"),
        _Chan(3, "Gobo Wheel 2 | attr=Gobo2"),
        _Chan(3, "Zoom"),
        _Chan(3, "Prism | attr=Prism"),
        _Chan(4, "Prism Rotation | attr=Prism"),
        _Chan(4, "Frost"),
        _Chan(4, "Iris"),
        _Chan(4, "Effect Macro | attr=Macro"),
        _Chan(5, "Reset Lamp Control | attr=Control"),
    ]),
    "wash": ("Wash (LED RGBW mover)", [
        _Chan(1, "Pan"),
        _Chan(1, "Pan fine"),
        _Chan(1, "Tilt"),
        _Chan(1, "Tilt fine"),
        _Chan(2, "Pan Tilt Speed | attr=Speed"),
        _Chan(1, "Dimmer"),
        _Chan(1, "Shutter Strobe | attr=Shutter"),
        _Chan(1, "Red"),
        _Chan(1, "Green"),
        _Chan(1, "Blue"),
        _Chan(2, "White"),
        _Chan(3, "Zoom"),
        _Chan(3, "Colour Macro | attr=ColorMacro"),
        _Chan(4, "Reset | attr=Control"),
        _Chan(4, "Amber"),
        _Chan(5, "UV"),
    ]),
    "par": ("Par / static wash (LED)", [
        _Chan(1, "Dimmer"),
        _Chan(1, "Red"),
        _Chan(1, "Green"),
        _Chan(1, "Blue"),
        _Chan(2, "White"),
        _Chan(2, "Strobe | attr=Shutter"),
        _Chan(3, "Colour Macro | attr=ColorMacro"),
        _Chan(3, "Amber"),
        _Chan(4, "UV"),
        _Chan(4, "Reset | attr=Control"),
    ]),
    "strobe": ("Strobe (LED, zoned)", [
        _Chan(1, "Dimmer"),
        _Chan(1, "Strobe Rate | attr=Shutter"),
        _Chan(2, "Strobe Duration | attr=Speed"),
        _Chan(2, "Effect Macro | attr=Macro"),
        _Chan(3, "Red"),
        _Chan(3, "Green"),
        _Chan(3, "Blue"),
        _Chan(4, "White Segments | attr=White"),
        _Chan(4, "Zone Effect | attr=Macro"),
    ]),
    "spark": ("Cold spark machine", [
        _Chan(1, "Preheat Arm | attr=Control",
              ("0-9  Off / safe", "250-255  Preheat on")),
        _Chan(1, "Spark Launch | attr=Control",
              ("0-9  Off", "10-255  Spark height")),
    ]),
}

_HEADER = """\
# DRAFT head - built from the typical {label} layout, NOT from this
# fixture's real manual. Undocumented clones usually follow this map, but
# verify it at load-in with the ten-minute fader test:
#
#   1. Address one unit to 001 and patch this head at 001.
#   2. Bring Dimmer to full (and open Shutter). No light? Step through the
#      raw channels until it lights, and note the real intensity channel.
#   3. Sweep every other channel 0-255 one at a time and note what the
#      fixture actually does.
#   4. Where this draft is wrong, rename/reorder the lines below to match
#      what you saw, rebuild, and re-copy the .hed to the console.
#   5. Check pan/tilt move smoothly (16-bit pairs: coarse then fine).
#
# Faster still: photograph the fixture's own DMX menu (usually under
# DMX / Function on its display) and read that chart in instead.
"""


def kinds() -> list[tuple[str, str]]:
    """The available stock layouts as (key, human label) pairs."""
    return [(k, label) for k, (label, _) in _KINDS.items()]


def plan_text(kind: str, count: int, *, manufacturer: str = "",
              model: str = "") -> str:
    """A draft plan for `kind` squeezed or padded to exactly `count` channels."""
    if kind not in _KINDS:
        raise ValueError(
            f"unknown layout {kind!r} (have: {', '.join(sorted(_KINDS))})")
    if not 1 <= count <= 512:
        raise ValueError("channel count must be 1-512")

    label, chans = _KINDS[kind]
    keep_idx = sorted(range(len(chans)),
                      key=lambda i: (chans[i].prio, i))[:count]
    picked = [chans[i] for i in sorted(keep_idx)]

    out = [_HEADER.format(label=label)]
    out.append(f"manufacturer: {manufacturer or 'Unknown'}")
    out.append(f"model: {model or 'My ' + kind.capitalize()}")
    out.append(f"mode: {count}ch")
    out.append("")
    for ch in picked:
        out.append(f"channel: {ch.line}")
        for r in ch.ranges:
            out.append(f"  {r}")
    if len(picked) < count:
        out.append("# The remaining channels are unknown - identify them at")
        out.append("# load-in and rename these lines:")
        for n in range(len(picked) + 1, count + 1):
            out.append(f"channel: Channel {n}")
    out.append("")
    return "\n".join(out)
