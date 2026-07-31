"""The bridge: MCU events on one side, a pluggable console on the other.

Pure logic - the Bridge is fed decoded events and returns the bytes to send
each way, so the whole mapping (including motor-fader feedback and echo
suppression) tests without an X-Touch or a console in the room.

The Bridge owns everything that belongs to the *surface*: pages, fader
touch, encoder accumulation, MCU byte encoding. The Target (see
targets.py) owns everything that belongs to one console's dialect.

grandMA3 onPC setup (Menu > In & Out > OSC):
  - add a line, set the destination IP to this machine and the ports to
    match the bridge's, enable Send + Receive,
  - leave the prefix empty or set one and give the bridge the same prefix,
  - enable "Executors" in the send filter so fader moves come back to the
    motors.

X32/M32 setup: none - the console answers OSC on port 10023 out of the
box; the bridge subscribes itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import mcu, osc, targets


@dataclass
class Config:
    """What the surface drives. Everything is overridable from JSON."""

    target: str = "ma3"                # "ma3" | "x32"
    prefix: str = ""                   # MA3 OSC prefix, e.g. "gMA3"
    page: int = 1
    fader_execs: tuple = tuple(range(201, 209))   # strips 1-8 (MA3)
    master_exec: int = 0               # 0 = grand master via /cmd (MA3)
    select_execs: tuple = tuple(range(101, 109))  # SELECT row (MA3)
    mute_execs: tuple = tuple(range(291, 299))    # MUTE row (MA3)
    encoder_execs: tuple = tuple(range(301, 309))
    encoder_step: float = 0.02         # fraction of full scale per tick
    # Transport row -> MA3 command line. Empty string = unmapped. They act
    # on the selected sequence, which is the least surprising default.
    cmd_play: str = "Go+"
    cmd_stop: str = "Pause"
    cmd_rewind: str = "Go-"
    cmd_fastfwd: str = ""
    cmd_record: str = ""


_BUTTON_ROWS = (("select", mcu.SELECT), ("mute", mcu.MUTE))
_TRANSPORT = {mcu.PLAY: "play", mcu.STOP: "stop", mcu.REWIND: "rewind",
              mcu.FASTFWD: "fastfwd", mcu.RECORD: "record"}


@dataclass
class Bridge:
    config: Config = field(default_factory=Config)
    target: object = None
    _enc_levels: dict = field(default_factory=dict)
    _touched: set = field(default_factory=set)

    def __post_init__(self):
        if self.target is None:
            self.target = targets.make_target(self.config)

    @property
    def max_page(self) -> int:
        return getattr(self.target, "pages", 9999)

    # ---- surface -> console ---------------------------------------------

    def midi_in(self, data: bytes) -> list[bytes]:
        """One MIDI message from the X-Touch -> OSC datagrams out."""
        ev = mcu.decode(data)
        out: list[osc.Message] = []

        if isinstance(ev, mcu.FaderMoved):
            if ev.strip == 8:
                out += self.target.master(ev.unit)
            else:
                out += self.target.fader(ev.strip, ev.unit)

        elif isinstance(ev, mcu.FaderTouched):
            (self._touched.add if ev.touching else
             self._touched.discard)(ev.strip)

        elif isinstance(ev, mcu.ButtonPressed):
            out += self._button(ev)

        elif isinstance(ev, mcu.EncoderTurned):
            level = self._enc_levels.get(ev.encoder, 0.0)
            level = max(0.0, min(1.0,
                                 level + ev.ticks * self.config.encoder_step))
            self._enc_levels[ev.encoder] = level
            out += self.target.encoder(ev.encoder, level)

        return [osc.encode(m) for m in out]

    def _button(self, ev: mcu.ButtonPressed) -> list[osc.Message]:
        if ev.note in _TRANSPORT:
            return self.target.transport(_TRANSPORT[ev.note], ev.down)
        if ev.note in (mcu.FADER_BANK_RIGHT, mcu.CHANNEL_RIGHT):
            if ev.down and self.config.page < self.max_page:
                self.config.page += 1
                return self.target.set_page(self.config.page)
            return []
        if ev.note in (mcu.FADER_BANK_LEFT, mcu.CHANNEL_LEFT):
            if ev.down and self.config.page > 1:
                self.config.page -= 1
                return self.target.set_page(self.config.page)
            return []
        for row, notes in _BUTTON_ROWS:
            if ev.note in notes:
                return self.target.button(row, notes.index(ev.note), ev.down)
        return []

    # ---- console -> surface ---------------------------------------------

    def osc_in(self, datagram: bytes) -> list[bytes]:
        """One OSC datagram from the console -> MIDI for the X-Touch."""
        msg = osc.decode(datagram)
        if msg is None:
            return []
        out: list[bytes] = []
        for fb in self.target.feedback(msg):
            if isinstance(fb, targets.FaderFB):
                # Never fight the human hand on the fader.
                if fb.strip not in self._touched:
                    out.append(mcu.fader_out(fb.strip, fb.unit))
            elif isinstance(fb, targets.ButtonFB):
                row_notes = mcu.SELECT if fb.row == "select" else mcu.MUTE
                out.append(mcu.button_led(row_notes[fb.idx], fb.on))
            elif isinstance(fb, targets.RingFB):
                self._enc_levels[fb.idx] = fb.unit
                out.append(mcu.encoder_ring(fb.idx, fb.unit, mode=2))
            elif isinstance(fb, targets.LabelFB):
                out.append(mcu.lcd_text(fb.strip, fb.line, fb.text))
        return out

    # ---- lifecycle -------------------------------------------------------

    def hello(self) -> list[bytes]:
        """MIDI to bring the surface to a known state, with strip labels."""
        return mcu.blank_surface() + self.page_labels()

    def osc_hello(self) -> list[bytes]:
        """Datagrams to introduce ourselves to the console (subscribe/query)."""
        return [osc.encode(m) for m in self.target.hello()]

    def tick(self, now: float) -> list[bytes]:
        """Periodic datagrams (e.g. the X32's /xremote renewal)."""
        return [osc.encode(m) for m in self.target.tick(now)]

    def page_labels(self) -> list[bytes]:
        return [mcu.lcd_text(s, ln, text)
                for s, ln, text in self.target.strip_labels(self.config.page)]
