"""Input surfaces: the hardware in your hands.

The bridge core is surface-agnostic - a Surface turns raw MIDI into the
bridge's events and surface feedback back into raw MIDI. Two ship:

- XTouchSurface: the full-size X-Touch in MC mode. Motorised faders,
  scribble strips, LED rings - the whole two-way conversation.
- MPKSurface: an Akai MPK Mini (or anything shaped like it). The 8 knobs
  send absolute CCs and ride the target's encoder slots; the 8 pads press
  the SELECT row with LED feedback. No motors, no screens - a knobs-and-
  pads remote, which is exactly what it is on the desk.

Stream Decks are not MIDI - they reach the bridge through the OSC control
port (see bridge.control_in): point Bitfocus Companion (which owns Stream
Decks natively) at /xbridge/... addresses and every key becomes whatever
you map it to.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import mcu


@dataclass(frozen=True)
class KnobSet:
    """An absolute knob position (MPK-style CC), 0.0-1.0."""

    knob: int
    unit: float


class XTouchSurface:
    """The X-Touch: full MCU, both directions."""

    name = "xtouch"

    def __init__(self, config):
        self.config = config

    def decode(self, data: bytes) -> list:
        ev = mcu.decode(data)
        return [ev] if ev is not None else []

    def render(self, fb, targets_mod) -> list[bytes]:
        if isinstance(fb, targets_mod.FaderFB):
            return [mcu.fader_out(fb.strip, fb.unit)]
        if isinstance(fb, targets_mod.ButtonFB):
            notes = mcu.SELECT if fb.row == "select" else mcu.MUTE
            return [mcu.button_led(notes[fb.idx], fb.on)]
        if isinstance(fb, targets_mod.RingFB):
            return [mcu.encoder_ring(fb.idx, fb.unit, mode=2)]
        if isinstance(fb, targets_mod.LabelFB):
            return [mcu.lcd_text(fb.strip, fb.line, fb.text)]
        return []

    def hello(self, labels: list[tuple[int, int, str]]) -> list[bytes]:
        return mcu.blank_surface() + [
            mcu.lcd_text(s, ln, txt) for s, ln, txt in labels]


class MPKSurface:
    """Akai MPK Mini: 8 absolute knobs + 8 pads.

    Defaults match the MPK Mini mk3 factory profile (knobs CC 70-77,
    pads notes 36-43); both lists are config so any knobby MIDI box works.
    Knobs ride the target's encoder slots as absolute levels; pads press
    the SELECT row. Pad LEDs follow the target where it reports state.
    """

    name = "mpk"

    def __init__(self, config):
        self.knobs = list(getattr(config, "mpk_knob_ccs", range(70, 78)))
        self.pads = list(getattr(config, "mpk_pad_notes", range(36, 44)))

    def decode(self, data: bytes) -> list:
        if not data:
            return []
        kind = data[0] & 0xF0
        if kind == 0xB0 and len(data) >= 3 and data[1] in self.knobs:
            idx = self.knobs.index(data[1])
            return [KnobSet(idx, data[2] / 127.0)]
        if kind in (0x90, 0x80) and len(data) >= 3 and data[1] in self.pads:
            idx = self.pads.index(data[1])
            down = kind == 0x90 and data[2] > 0
            return [mcu.ButtonPressed(mcu.SELECT[idx], down)]
        return []

    def render(self, fb, targets_mod) -> list[bytes]:
        if isinstance(fb, targets_mod.ButtonFB) and fb.row == "select" \
                and fb.idx < len(self.pads):
            return [bytes((0x90, self.pads[fb.idx], 127 if fb.on else 0))]
        return []      # no motors, no rings, no screens

    def hello(self, labels) -> list[bytes]:
        return [bytes((0x90, n, 0)) for n in self.pads]


class X32MCSurface(XTouchSurface):
    """A Behringer X32 / Midas M32 in DAW-remote Mackie Control mode.

    Setup -> Remote: encoder 1 enables remote control, encoder 2 picks
    the protocol (choose **Mackie Control**, not HUI or raw CC), and the
    interface is MIDI In/Out (DIN), Card MIDI (the X-USB card) or
    RTPMIDI. Card MIDI is the easy one - the console then appears as an
    ordinary USB MIDI port on the PC.

    Mackie Control is Mackie Control, so this inherits the X-Touch's
    decoding wholesale. Two things differ in practice:

    * **No scribble strips.** The X32 draws its own channel names on its
      own screen and ignores MCU's LCD SysEx. Sending it is harmless but
      pointless, so it is skipped - which also keeps the hello burst
      small enough not to choke a DIN-MIDI link at 31250 baud.
    * **Fader touch is not guaranteed.** If the console never sends the
      touch notes, nothing is ever "held", and motor feedback simply
      applies - which is the right behaviour rather than a fault.

    The console has 8 channel strips plus a main fader in this mode, the
    same shape the bridge already speaks.
    """

    name = "x32mc"

    def render(self, fb, targets_mod) -> list[bytes]:
        if isinstance(fb, targets_mod.LabelFB):
            return []                  # the X32 labels its own strips
        return super().render(fb, targets_mod)

    def hello(self, labels) -> list[bytes]:
        # blank_surface() clears the LCDs too; drop those SysEx blocks.
        return [m for m in mcu.blank_surface() if not m.startswith(b"\xf0")]


_SURFACES = {"xtouch": XTouchSurface, "mpk": MPKSurface,
             "x32mc": X32MCSurface}


def make_surface(config):
    return make_surfaces(config)[0]


def make_surfaces(config) -> list:
    """Every surface named in config.surface ("xtouch", "xtouch,mpk", ...).

    Running several at once is the point: faders on the X-Touch, knobs
    and pads on an MPK, deck keys over the OSC control port - one bridge,
    one target, many hands.
    """
    spec = getattr(config, "surface", "xtouch") or "xtouch"
    out = []
    for kind in [s.strip() for s in spec.replace("+", ",").split(",") if s.strip()]:
        cls = _SURFACES.get(kind)
        if cls is None:
            raise ValueError(
                f"unknown surface {kind!r} (have: {', '.join(sorted(_SURFACES))})")
        out.append(cls(config))
    return out or [XTouchSurface(config)]
