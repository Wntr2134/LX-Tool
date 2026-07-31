"""The bridge: MCU events on one side, grandMA3 OSC on the other.

Pure logic - the Bridge is fed decoded events and returns the bytes to send
each way, so the whole mapping (including motor-fader feedback and echo
suppression) tests without an X-Touch or an MA3 in the room.

grandMA3 onPC setup (Menu > In & Out > OSC):
  - add a line, set the destination IP to this machine and the ports to
    match the bridge's, enable Send + Receive,
  - leave the prefix empty or set one and give the bridge the same prefix,
  - enable "Executors" in the send filter so fader moves come back to the
    motors.

MA3's OSC dialect, which the addresses below follow:
  /Page<n>/Fader<executor>  int 0-100     executor fader level
  /Page<n>/Key<executor>    int 1 / 0     executor button press / release
  /cmd                      string        a command-line command
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import mcu, osc


@dataclass
class Config:
    """What the surface drives. Everything is overridable from JSON."""

    prefix: str = ""                   # MA3 OSC prefix, e.g. "gMA3"
    page: int = 1
    fader_execs: tuple = tuple(range(201, 209))   # strips 1-8
    master_exec: int = 0               # 0 = grand master via /cmd
    select_execs: tuple = tuple(range(101, 109))  # SELECT row -> key execs
    mute_execs: tuple = tuple(range(291, 299))    # MUTE row (flash/blackout style)
    encoder_execs: tuple = tuple(range(301, 309))
    encoder_step: float = 0.02         # fraction of full scale per tick
    # Transport row -> MA3 command line. Empty string = unmapped. They act
    # on the selected sequence, which is the least surprising default.
    cmd_play: str = "Go+"
    cmd_stop: str = "Pause"
    cmd_rewind: str = "Go-"
    cmd_fastfwd: str = ""
    cmd_record: str = ""

    def addr(self, leaf: str) -> str:
        p = f"/{self.prefix}" if self.prefix else ""
        return f"{p}/Page{self.page}/{leaf}"


@dataclass
class Bridge:
    config: Config = field(default_factory=Config)
    # last levels as units 0.0-1.0, indexed by strip; used for encoder
    # relative moves and for suppressing feedback echo while touched.
    _levels: dict = field(default_factory=dict)
    _enc_levels: dict = field(default_factory=dict)
    _touched: set = field(default_factory=set)

    # ---- surface -> MA3 -------------------------------------------------

    def midi_in(self, data: bytes) -> list[bytes]:
        """One MIDI message from the X-Touch -> OSC datagrams for MA3."""
        ev = mcu.decode(data)
        cfg = self.config
        out: list[osc.Message] = []

        if isinstance(ev, mcu.FaderMoved):
            if ev.strip == 8:
                if cfg.master_exec:
                    out.append(osc.Message(cfg.addr(f"Fader{cfg.master_exec}"),
                                           (round(ev.unit * 100),)))
                else:
                    out.append(osc.Message(self._cmd_addr(),
                                           (f"Master 2.1 At {ev.unit * 100:.1f}",)))
            elif ev.strip < len(cfg.fader_execs):
                self._levels[ev.strip] = ev.unit
                out.append(osc.Message(
                    cfg.addr(f"Fader{cfg.fader_execs[ev.strip]}"),
                    (round(ev.unit * 100),)))

        elif isinstance(ev, mcu.FaderTouched):
            (self._touched.add if ev.touching else
             self._touched.discard)(ev.strip)

        elif isinstance(ev, mcu.ButtonPressed):
            command = self._transport_cmd(ev.note)
            if command is not None:
                if command and ev.down:
                    out.append(osc.Message(self._cmd_addr(), (command,)))
                return [osc.encode(m) for m in out]
            leaf = self._button_leaf(ev.note)
            if leaf == "page+" and ev.down:
                self.config.page += 1
                return self._page_flip_bytes()
            if leaf == "page-" and ev.down:
                self.config.page = max(1, self.config.page - 1)
                return self._page_flip_bytes()
            if leaf:
                out.append(osc.Message(cfg.addr(leaf), (1 if ev.down else 0,)))

        elif isinstance(ev, mcu.EncoderTurned):
            if ev.encoder < len(cfg.encoder_execs):
                level = self._enc_levels.get(ev.encoder, 0.0)
                level = max(0.0, min(1.0, level + ev.ticks * cfg.encoder_step))
                self._enc_levels[ev.encoder] = level
                out.append(osc.Message(
                    cfg.addr(f"Fader{cfg.encoder_execs[ev.encoder]}"),
                    (round(level * 100),)))

        return [osc.encode(m) for m in out]

    # ---- MA3 -> surface -------------------------------------------------

    def osc_in(self, datagram: bytes) -> list[bytes]:
        """One OSC datagram from MA3 -> MIDI messages for the X-Touch."""
        msg = osc.decode(datagram)
        if msg is None or not msg.args:
            return []
        leaf = self._leaf_of(msg.address)
        if leaf is None:
            return []
        unit = _unit_of(msg.args[0])
        if unit is None:
            return []
        cfg = self.config
        out: list[bytes] = []

        if leaf.startswith("Fader"):
            try:
                exec_no = int(leaf[5:])
            except ValueError:
                return []
            if exec_no in cfg.fader_execs:
                strip = cfg.fader_execs.index(exec_no)
                self._levels[strip] = unit
                # Never fight the human hand on the fader.
                if strip not in self._touched:
                    out.append(mcu.fader_out(strip, unit))
            elif exec_no in cfg.encoder_execs:
                enc = cfg.encoder_execs.index(exec_no)
                self._enc_levels[enc] = unit
                out.append(mcu.encoder_ring(enc, unit, mode=2))
            elif cfg.master_exec and exec_no == cfg.master_exec:
                out.append(mcu.fader_out(8, unit))

        elif leaf.startswith("Key"):
            try:
                exec_no = int(leaf[3:])
            except ValueError:
                return []
            if exec_no in cfg.select_execs:
                note = mcu.SELECT[cfg.select_execs.index(exec_no)]
                out.append(mcu.button_led(note, unit > 0))
            elif exec_no in cfg.mute_execs:
                note = mcu.MUTE[cfg.mute_execs.index(exec_no)]
                out.append(mcu.button_led(note, unit > 0))

        return out

    # ---- helpers --------------------------------------------------------

    def hello(self) -> list[bytes]:
        """MIDI to bring the surface to a known state, with strip labels."""
        out = mcu.blank_surface()
        out += self._labels()
        return out

    def _labels(self) -> list[bytes]:
        cfg = self.config
        out = []
        for s in range(8):
            if s < len(cfg.fader_execs):
                out.append(mcu.lcd_text(s, 0, f"Ex {cfg.fader_execs[s]}"))
                out.append(mcu.lcd_text(s, 1, f"Pg {cfg.page}"))
        return out

    def _page_flip_bytes(self) -> list[bytes]:
        # On a page flip MA3 will (if sending) re-broadcast levels; refresh
        # the labels now and let the feedback move the motors.
        return []  # OSC side sends nothing for the flip itself

    def page_labels(self) -> list[bytes]:
        return self._labels()

    def _cmd_addr(self) -> str:
        return f"/{self.config.prefix}/cmd" if self.config.prefix else "/cmd"

    def _transport_cmd(self, note: int) -> str | None:
        """The command for a transport button, "" if unmapped, None if not
        a transport button at all."""
        cfg = self.config
        return {mcu.PLAY: cfg.cmd_play, mcu.STOP: cfg.cmd_stop,
                mcu.REWIND: cfg.cmd_rewind, mcu.FASTFWD: cfg.cmd_fastfwd,
                mcu.RECORD: cfg.cmd_record}.get(note)

    def _button_leaf(self, note: int) -> str | None:
        cfg = self.config
        if note in mcu.SELECT:
            i = mcu.SELECT.index(note)
            if i < len(cfg.select_execs):
                return f"Key{cfg.select_execs[i]}"
        if note in mcu.MUTE:
            i = mcu.MUTE.index(note)
            if i < len(cfg.mute_execs):
                return f"Key{cfg.mute_execs[i]}"
        if note == mcu.FADER_BANK_RIGHT or note == mcu.CHANNEL_RIGHT:
            return "page+"
        if note == mcu.FADER_BANK_LEFT or note == mcu.CHANNEL_LEFT:
            return "page-"
        return None

    def _leaf_of(self, address: str) -> str | None:
        """ ".../Page<current>/<leaf>" -> leaf, else None."""
        want = self.config.addr("")
        if address.startswith(want):
            return address[len(want):]
        return None


def _unit_of(arg) -> float | None:
    """An MA3 level argument (int 0-100 or float 0.0-100.0) -> 0.0-1.0."""
    if isinstance(arg, bool):
        return 1.0 if arg else 0.0
    if isinstance(arg, (int, float)):
        v = float(arg)
        if v < 0:
            return None
        return max(0.0, min(1.0, v / 100.0))
    return None
