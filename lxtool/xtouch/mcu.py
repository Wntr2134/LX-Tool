"""Mackie Control (MCU) protocol for the X-Touch, as pure byte-in/byte-out.

Everything here is a function from MIDI bytes to typed events, or from
intents to MIDI bytes - no ports, no threads - so the whole protocol layer
is testable without hardware.

The MCU wire format, as the X-Touch full size implements it:

- Faders: pitch-bend per channel. Channels 0-7 are strips 1-8, channel 8 is
  the master. 14-bit value, 0..16383.
- Fader touch: notes 104-112 (velocity 127 touch, 0 release).
- Buttons: note on/off, velocity 127/0. The same note numbers, sent back as
  note-on, drive the button LEDs (0 off, 127 on, 1 flashing on X-Touch).
- Encoders: CC 16-23, relative: value 1..7 = clockwise by that many ticks,
  65..71 = counter-clockwise. LED rings around them are CC 48-55.
- Scribble strips: SysEx F0 00 00 66 14 12 <offset> <ascii...> F7. Each of
  the 8 strips has 7 characters on each of 2 lines; offset 0-55 is the top
  line, 56-111 the bottom.
- 7-segment "assignment" display: CC 96-107 (rarely needed; the two
  assignment digits are CC 96/97).
"""

from __future__ import annotations

from dataclasses import dataclass

# Note numbers for the buttons this bridge cares about. The full MCU map
# has ~100 buttons; these are the ones with a natural MA3 meaning.
REC = tuple(range(0, 8))          # top row per strip ("rec/rdy")
SOLO = tuple(range(8, 16))
MUTE = tuple(range(16, 24))
SELECT = tuple(range(24, 32))
ENCODER_PRESS = tuple(range(32, 40))
FADER_TOUCH = tuple(range(104, 113))   # 8 strips + master

FADER_BANK_LEFT = 46
FADER_BANK_RIGHT = 47
CHANNEL_LEFT = 48
CHANNEL_RIGHT = 49

# Transport row.
REWIND = 91
FASTFWD = 92
STOP = 93
PLAY = 94
RECORD = 95

_SYSEX_HEAD = bytes((0xF0, 0x00, 0x00, 0x66, 0x14, 0x12))
LCD_CHARS = 7          # per strip, per line
LCD_STRIPS = 8


@dataclass(frozen=True)
class FaderMoved:
    strip: int         # 0-7, 8 = master
    value: int         # 14-bit, 0..16383

    @property
    def unit(self) -> float:
        return self.value / 16383.0


@dataclass(frozen=True)
class ButtonPressed:
    note: int
    down: bool


@dataclass(frozen=True)
class EncoderTurned:
    encoder: int       # 0-7
    ticks: int         # signed; positive clockwise


@dataclass(frozen=True)
class FaderTouched:
    strip: int
    touching: bool


def decode(msg: bytes) -> object | None:
    """One incoming MIDI message -> one typed event, or None if unmapped."""
    if not msg:
        return None
    status, kind = msg[0], msg[0] & 0xF0

    if kind == 0xE0 and len(msg) >= 3:                    # pitch bend: fader
        strip = status & 0x0F
        if strip <= 8:
            return FaderMoved(strip, msg[1] | (msg[2] << 7))
        return None

    if kind in (0x90, 0x80) and len(msg) >= 3:            # note: button/touch
        note, vel = msg[1], msg[2]
        down = kind == 0x90 and vel > 0
        if note in FADER_TOUCH:
            return FaderTouched(note - FADER_TOUCH[0], down)
        return ButtonPressed(note, down)

    if kind == 0xB0 and len(msg) >= 3:                    # CC: encoder
        cc, val = msg[1], msg[2]
        if 16 <= cc <= 23:
            ticks = val if val < 64 else -(val - 64)
            return EncoderTurned(cc - 16, ticks)
        return None

    return None


def fader_out(strip: int, unit: float) -> bytes:
    """Move a motor fader to `unit` (0.0-1.0). Strip 8 is the master."""
    if not 0 <= strip <= 8:
        raise ValueError("strip must be 0-8")
    v = max(0, min(16383, round(unit * 16383)))
    return bytes((0xE0 | strip, v & 0x7F, v >> 7))


def button_led(note: int, on: bool, *, flash: bool = False) -> bytes:
    """Light (or flash, or clear) a button LED."""
    vel = 127 if on and not flash else (1 if on else 0)
    return bytes((0x90, note & 0x7F, vel))


def encoder_ring(encoder: int, unit: float, *, mode: int = 0) -> bytes:
    """Set an encoder's LED ring to show `unit` full-scale.

    mode 0 = single dot, 1 = boost/cut, 2 = wrap (fill), 3 = spread.
    """
    if not 0 <= encoder <= 7:
        raise ValueError("encoder must be 0-7")
    pos = 1 + max(0, min(10, round(unit * 10)))           # 11 LEDs, 1..11
    return bytes((0xB0, 48 + encoder, (mode << 4) | pos))


def lcd_text(strip: int, line: int, text: str) -> bytes:
    """Write one strip's scribble line (7 chars, ASCII, padded/truncated)."""
    if not 0 <= strip < LCD_STRIPS:
        raise ValueError("strip must be 0-7")
    if line not in (0, 1):
        raise ValueError("line must be 0 or 1")
    payload = text[:LCD_CHARS].ljust(LCD_CHARS)
    ascii_safe = bytes(c if 0x20 <= c < 0x7F else 0x3F     # '?' for non-ASCII
                       for c in payload.encode("ascii", "replace"))
    offset = line * LCD_STRIPS * LCD_CHARS + strip * LCD_CHARS
    return _SYSEX_HEAD + bytes((offset,)) + ascii_safe + b"\xF7"


def blank_surface() -> list[bytes]:
    """Everything off: faders down, rings dark, strips cleared."""
    out: list[bytes] = [fader_out(s, 0.0) for s in range(9)]
    out += [encoder_ring(e, 0.0) for e in range(8)]
    out += [lcd_text(s, ln, "") for s in range(LCD_STRIPS) for ln in (0, 1)]
    for note in (*REC, *SOLO, *MUTE, *SELECT):
        out.append(button_led(note, False))
    return out
