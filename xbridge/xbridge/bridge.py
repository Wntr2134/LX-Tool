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

from . import mcu, osc, surfaces, targets


@dataclass
class Config:
    """What the surface drives. Everything is overridable from JSON."""

    target: str = "ma3"     # ma3 | x32 | magicq | resolume | companion
    # MA3 drops any message that does not start with the OSC line's
    # prefix, and does it silently - a mismatch here looks exactly like
    # "the bridge is not sending". The console's own default is an EMPTY
    # prefix (the manual's receive example notes "no prefix is defined");
    # "gma3" appears only in MA's Open Stage Control walkthrough, which
    # says it "assumes the OSCData line has a prefix of gma3 configured".
    # So empty matches a stock console, and the probe finds the rest.
    prefix: str = ""
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
    magicq_master_pb: int = 9          # MagicQ: playback for the master fader
    # Generic OSC target: address templates, {n} = strip number (+8/page).
    gen_fader: str = "/fader/{n}"
    gen_master: str = "/master"
    gen_select: str = "/button/{n}"
    gen_mute: str = ""
    gen_encoder: str = ""
    gen_scale: str = "float01"         # "float01" | "int100"
    # grandMA2 web remote (no OSC on MA2): console login + master command.
    ma2_user: str = "remote"
    ma2_password: str = "remote"
    ma2_master_cmd: str = "SpecialMaster 2.1 At {pct}"
    # MA3 fader argument type. The Advanced Examples table gives faders
    # type tags "i f, 0 ... 100", so int and float are equally valid and
    # the canonical example is "/Page1/Fader201,i,100". int255 is for an
    # OSC line whose FaderRange cell has been moved off 100.
    ma3_value: str = "int100"     # int100 | float100 | float01 | int255
    # Address shape. "page" = /Page<n>/Fader<x>, which names the page
    # explicitly. "selected" = /Fader<x>, which MA3 applies to whichever
    # page is currently selected - the fallback when the OSC line's
    # "Page" address cell has been renamed or cleared.
    ma3_addr: str = "page"        # page | selected
    # Encoders. "fader" drives an executor fader absolutely, which suits
    # an absolute control like the MPK's knobs and is the default. MA3
    # also documents /Encoder<x> taking a RELATIVE -100..100 step, which
    # is its native mini-encoder path - better for the X-Touch's endless
    # encoders, and what to pick if 301-308 have no fader function.
    ma3_encoder: str = "fader"    # fader | encoder
    # How a fader move reaches MA3. "osc" uses /Page<n>/Fader<x>, which
    # depends on the OSC line's Fader/Page address cells routing. "cmd"
    # sends command-line syntax to /cmd instead - a completely separate
    # path that needs only Receive Command, and works when the executor
    # addressing does not.
    ma3_fader: str = "osc"        # osc | cmd
    # MA's OSC page shows "FaderMaster Page 1.201 At 50"; a 2.4 console
    # answers that with IllegalProperty, because the FaderMaster keyword
    # page documents "FaderMaster [Object] [Number] At [Value]" and Page
    # is not an object type in that slot. The default below is the
    # keyword page's own example shape, confirmed working on 2.4.2.
    #
    # It addresses the executor on MA3's CURRENT page, so the surface's
    # bank buttons do not change which executors are hit. Set
    # ma3_page_cmd as well if the console should follow the bank.
    ma3_fader_cmd: str = "FaderMaster {exec} At {pct}"
    # Sent to /cmd when the surface changes bank, if set. Left empty
    # because the right spelling varies by version and shipping an
    # unverified one is what made the fader syntax wrong in the first
    # place - try "Select Page {page}" in the mapping editor and see.
    ma3_page_cmd: str = ""
    # Re-assert a fader to the surface right after it moves it.
    #
    # A motorised MCU surface holds the position the DAW last told it,
    # not the one your hand left it at. A DAW echoes every fader move
    # straight back, so the two agree; a bridge that stays silent leaves
    # the surface still believing the old value, and the motor drags the
    # fader back there the moment you let go. Echoing what the fader
    # itself just reported cannot fight the hand - it is already there.
    local_echo: bool = True
    # Ignore a surface fader move that only repeats the position the
    # bridge just drove the motor to.
    #
    # Console feedback moves the motor; a surface that reports its own
    # motor movement back sends that straight to the console, which
    # reports it again - a loop that saturates the link and makes a fader
    # crawl on its own. Only near-identical values inside this window are
    # dropped, so a real move is never swallowed.
    motor_echo_guard: float = 0.35     # seconds; 0 disables
    # MA3's playback feedback arrives addressed by pool index, not by
    # executor: /13.13.1.6.1 ,sif, "FaderMaster",3,63.5. Only the user
    # knows which object sits on which strip, so map them here:
    # {"13.13.1.6.1": 1} drives strip 1's motor from that object.
    ma3_feedback: dict = field(default_factory=dict)
    # Work the feedback map out instead of being told it. When the bridge
    # drives an executor and, moments later, an unclaimed pool address
    # reports back that same level, that address is that strip's object.
    # Moving each fader once teaches the lot. Ambiguous matches - two
    # strips sitting at the same level - are declined rather than guessed.
    ma3_autolearn: bool = True
    # MIDI-learn: press a control, say what it should do. Keyed by the
    # control's own identity, so ANY button can be assigned - not only
    # the rows the default mapping happens to know about.
    #
    #   "note:24"   -> {"do": "key",  "exec": 205}
    #   "fader:2"   -> {"do": "fader","exec": 207}
    #   "enc:4"     -> {"do": "enc",  "exec": 303}
    #   "note:94"   -> {"do": "cmd",  "cmd": "Off Executor 1.201"}
    #
    # An entry here wins over the default mapping for that control; every
    # other control carries on as before.
    learn_map: dict = field(default_factory=dict)
    # Which hardware is in your hands, and the MPK's factory MIDI numbers.
    surface: str = "xtouch"            # "xtouch" | "mpk"
    mpk_knob_ccs: tuple = tuple(range(70, 78))
    mpk_pad_notes: tuple = tuple(range(36, 44))


_BUTTON_ROWS = (("select", mcu.SELECT), ("mute", mcu.MUTE))
_TRANSPORT = {mcu.PLAY: "play", mcu.STOP: "stop", mcu.REWIND: "rewind",
              mcu.FASTFWD: "fastfwd", mcu.RECORD: "record"}


@dataclass
class Bridge:
    config: Config = field(default_factory=Config)
    target: object = None
    _enc_levels: dict = field(default_factory=dict)
    _touched: set = field(default_factory=set)
    _motor: dict = field(default_factory=dict)   # strip -> (when, unit)

    surface: object = None
    surfaces: list = None

    def __post_init__(self):
        if self.target is None:
            self.target = targets.make_target(self.config)
        if self.surfaces is None:
            self.surfaces = surfaces.make_surfaces(self.config)
        if self.surface is None:
            self.surface = self.surfaces[0]

    @property
    def max_page(self) -> int:
        return getattr(self.target, "pages", 9999)

    # ---- surface -> console ---------------------------------------------

    def midi_in(self, data: bytes, surface=None) -> list[bytes]:
        """One MIDI message from a surface -> console output.

        `surface` says which surface the bytes came from (multi-surface
        rigs); default is the primary one.
        """
        out: list[osc.Message] = []
        for ev in (surface or self.surface).decode(data):
            out += self._event(ev)
        return _wire(out)

    @staticmethod
    def control_key(ev) -> str:
        """One surface control's identity, stable across sessions.

        Buttons are keyed by their raw note rather than by the row the
        default mapping puts them in, so a key nothing currently uses can
        still be assigned.
        """
        if isinstance(ev, surfaces.KnobSet):
            return f"knob:{ev.knob}"
        if isinstance(ev, mcu.FaderMoved):
            return f"fader:{ev.strip}"
        if isinstance(ev, mcu.EncoderTurned):
            return f"enc:{ev.encoder}"
        if isinstance(ev, mcu.ButtonPressed):
            return f"note:{ev.note}"
        return ""

    def _learned(self, ev) -> list | None:
        """Whatever the user assigned to this control, or None."""
        table = getattr(self.config, "learn_map", None) or {}
        spec = table.get(self.control_key(ev))
        if not isinstance(spec, dict):
            return None
        target, do = self.target, spec.get("do")
        exec_no = spec.get("exec")

        if do == "cmd":
            cmd = str(spec.get("cmd", "")).strip()
            send = getattr(target, "command", None)
            if not cmd or send is None:
                return []
            # Commands fire once, on the way down.
            if isinstance(ev, mcu.ButtonPressed) and not ev.down:
                return []
            return send(cmd)

        fn = {"key": "key_exec", "fader": "fader_exec",
              "enc": "encoder_exec"}.get(do)
        call = getattr(target, fn, None) if fn else None
        if call is None or exec_no is None:
            return []
        if do == "key":
            down = getattr(ev, "down", True)
            return call(int(exec_no), bool(down))
        unit = getattr(ev, "unit", None)
        if unit is None and isinstance(ev, mcu.EncoderTurned):
            level = self._enc_levels.get(ev.encoder, 0.0)
            unit = max(0.0, min(1.0, level + ev.ticks * self.config.encoder_step))
            self._enc_levels[ev.encoder] = unit
        if unit is None:
            return []
        return call(int(exec_no), float(unit))

    def _event(self, ev) -> list:
        out: list = []

        learned = self._learned(ev)
        if learned is not None:
            if isinstance(ev, mcu.FaderMoved):
                if self._is_motor_echo(ev.strip, ev.unit):
                    return []
            return learned

        if isinstance(ev, surfaces.KnobSet):
            self._enc_levels[ev.knob] = ev.unit
            out += self.target.encoder(ev.knob, ev.unit)

        elif isinstance(ev, mcu.FaderMoved):
            if self._is_motor_echo(ev.strip, ev.unit):
                return []
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
            # A target that wants raw ticks (Companion's rotate-left/right)
            # declares encoder_ticks; otherwise the bridge accumulates an
            # absolute level for it.
            ticks_fn = getattr(self.target, "encoder_ticks", None)
            if ticks_fn is not None:
                out += ticks_fn(ev.encoder, ev.ticks)
            else:
                level = self._enc_levels.get(ev.encoder, 0.0)
                level = max(0.0, min(1.0, level
                                     + ev.ticks * self.config.encoder_step))
                self._enc_levels[ev.encoder] = level
                out += self.target.encoder(ev.encoder, level)

        return out

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

    def _is_motor_echo(self, strip: int, unit: float) -> bool:
        """Is this the surface reporting a motor move we just made?"""
        import time as _time

        window = getattr(self.config, "motor_echo_guard", 0.0) or 0.0
        if window <= 0:
            return False
        seen = self._motor.get(strip)
        if not seen:
            return False
        when, was = seen
        return (_time.monotonic() - when) <= window and abs(was - unit) <= 0.02

    def _note_motor(self, strip: int, unit: float) -> None:
        import time as _time

        self._motor[strip] = (_time.monotonic(), unit)

    def echo_for(self, data: bytes, surface=None) -> list[bytes]:
        """Bytes to send back to the surface that just moved a fader.

        Deliberately bypasses touch suppression: this is not the console
        arguing with a hand, it is the surface being told the value it
        just sent, which is where the fader already is.
        """
        if not getattr(self.config, "local_echo", True):
            return []
        surface = surface or self.surface
        out: list[bytes] = []
        for ev in surface.decode(data):
            if isinstance(ev, mcu.FaderMoved):
                out += surface.render(targets.FaderFB(ev.strip, ev.unit),
                                      targets)
        return out

    def osc_in(self, datagram: bytes) -> list[bytes]:
        """One OSC datagram from the console -> MIDI for the X-Touch."""
        msg = osc.decode(datagram)
        if msg is None:
            return []
        return self.apply_feedback(self.target.feedback(msg))

    def apply_feedback(self, intents) -> list[bytes]:
        """Surface intents -> MIDI for the primary surface, touch-aware."""
        return self.render_for(self.surface, intents)

    def render_for(self, surface, intents) -> list[bytes]:
        """The same intents, rendered for one specific surface.

        Multi-surface rigs call this once per connected surface, so a
        fader move from the console lands as a motor move on the X-Touch
        AND (nothing) on the MPK, while a key state lights both the
        X-Touch button and the MPK pad.
        """
        out: list[bytes] = []
        for fb in intents:
            if isinstance(fb, targets.FaderFB):
                # Never fight the human hand on the fader.
                if fb.strip in self._touched:
                    continue
                self._note_motor(fb.strip, fb.unit)
            elif isinstance(fb, targets.RingFB):
                self._enc_levels[fb.idx] = fb.unit
            out += surface.render(fb, targets)
        return out

    # ---- lifecycle -------------------------------------------------------

    def hello(self) -> list[bytes]:
        """MIDI to bring the surface to a known state, with strip labels."""
        return self.surface.hello(self.target.strip_labels(self.config.page))

    def osc_hello(self) -> list[bytes]:
        """Datagrams to introduce ourselves to the console (subscribe/query)."""
        return _wire(self.target.hello())

    def tick(self, now: float) -> list[bytes]:
        """Periodic datagrams (e.g. the X32's /xremote renewal)."""
        return _wire(self.target.tick(now))

    def page_labels(self) -> list[bytes]:
        out = []
        for s, ln, text in self.target.strip_labels(self.config.page):
            out += self.surface.render(targets.LabelFB(s, ln, text), targets)
        return out

    # ---- the OSC control port: Stream Decks (via Companion), TouchOSC,
    # anything that can send OSC becomes extra buttons and faders. -------

    def control_in(self, msg: osc.Message) -> list:
        """/xbridge/... datagrams -> console output, same paths as hardware.

        Addresses (n = 1-8):
          /xbridge/fader/<n>   float 0-1 or int 0-100
          /xbridge/master      float 0-1 or int 0-100
          /xbridge/enc/<n>     float 0-1 or int 0-100
          /xbridge/key/select/<n>  1 press / 0 release
          /xbridge/key/mute/<n>    1 press / 0 release
          /xbridge/page        int (absolute page/bank)
        """
        parts = msg.address.strip("/").split("/")
        if not parts or parts[0] != "xbridge":
            return []
        arg = msg.args[0] if msg.args else 1
        unit = _ctl_unit(arg)

        if len(parts) == 3 and parts[1] in ("fader", "enc"):
            n = _ctl_int(parts[2])
            if n is None or not 1 <= n <= 8 or unit is None:
                return []
            if parts[1] == "fader":
                return _wire(self.target.fader(n - 1, unit))
            self._enc_levels[n - 1] = unit
            return _wire(self.target.encoder(n - 1, unit))
        if len(parts) == 2 and parts[1] == "master" and unit is not None:
            return _wire(self.target.master(unit))
        if len(parts) == 4 and parts[1] == "key" and parts[2] in ("select", "mute"):
            n = _ctl_int(parts[3])
            if n is None or not 1 <= n <= 8:
                return []
            return _wire(self.target.button(parts[2], n - 1, bool(unit)))
        if len(parts) == 2 and parts[1] == "page":
            n = _ctl_int(str(int(arg)) if isinstance(arg, (int, float)) else "")
            if n and 1 <= n <= self.max_page:
                self.config.page = n
                return _wire(self.target.set_page(n))
        return []


def _wire(out):
    """Encode a target's mixed output: OSC messages to datagrams, command
    strings (the MA2 web-remote transport) passed through untouched."""
    return [osc.encode(m) if isinstance(m, osc.Message) else m for m in out]


def _ctl_unit(arg) -> float | None:
    if isinstance(arg, bool):
        return 1.0 if arg else 0.0
    if isinstance(arg, (int, float)):
        v = float(arg)
        if v > 1.0:
            v = v / 100.0
        return max(0.0, min(1.0, v))
    return None


def _ctl_int(s: str) -> int | None:
    try:
        return int(s)
    except (TypeError, ValueError):
        return None
