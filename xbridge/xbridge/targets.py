"""Pluggable targets: what the X-Touch drives on the other side.

The bridge owns the surface (MCU bytes, touch, paging); a Target owns one
console's dialect - how a fader move becomes that console's OSC, and how
its feedback becomes surface intents. Adding a console means adding a
Target, not touching the bridge.

Two targets ship today:

- MA3Target: grandMA3 onPC. Pages of executors; /Page<n>/Fader<exec>.
- X32Target: Behringer X32/M32 audio consoles. Banks of 8 channels;
  /ch/NN/mix/fader, real mutes, channel select, pan on the encoders, and
  the channel names on the scribble strips. The X32 answers a query (an
  address with no arguments) with the current value, and streams changes
  for ~10s after each /xremote - so hello() subscribes and queries, and
  tick() renews the subscription.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import osc


# Surface intents a target's feedback can produce; the bridge turns these
# into MCU bytes (and applies touch suppression to FaderFB).
@dataclass(frozen=True)
class FaderFB:
    strip: int          # 0-7, 8 = master
    unit: float


@dataclass(frozen=True)
class ButtonFB:
    row: str            # "select" | "mute"
    idx: int            # 0-7
    on: bool


@dataclass(frozen=True)
class RingFB:
    idx: int
    unit: float


@dataclass(frozen=True)
class LabelFB:
    strip: int
    line: int
    text: str


class MA3Target:
    """grandMA3 onPC over OSC. The dialect the bridge always spoke."""

    name = "ma3"
    default_send_port = 8000

    def __init__(self, config):
        self.config = config
        self._enc_pos: dict = {}

    def _level(self, unit: float):
        """MA3's fader argument.

        The Advanced Examples table gives faders the type tags "i f" over
        0...100, so int and float are both accepted; the OSC line's
        FaderRange cell can move the top of the scale to 255. Which one a
        given console wants is not knowable from here, so all four are
        selectable and the probe ("Find MA3 format") tries them in turn.
        """
        form = getattr(self.config, "ma3_value", "float100")
        if form == "int100":
            return round(unit * 100)
        if form == "float01":
            return float(unit)
        if form == "int255":
            return round(unit * 255)
        return float(unit * 100.0)          # float100 - MA's own example

    # -- surface -> console ------------------------------------------------

    def fader(self, strip: int, unit: float) -> list[osc.Message]:
        cfg = self.config
        if strip >= len(cfg.fader_execs):
            return []
        exec_no = cfg.fader_execs[strip]
        if getattr(cfg, "ma3_fader", "osc") == "cmd":
            # The command line reaches the same fader without depending
            # on the OSC line's Fader/Page address cells at all. Needs
            # Receive Command = Yes rather than Receive.
            body = cfg.ma3_fader_cmd.format(page=cfg.page, exec=exec_no,
                                            pct=f"{unit * 100:.1f}")
            return [osc.Message(self._cmd_addr(), (body,))]
        return [osc.Message(self._addr(f"Fader{exec_no}"),
                            (self._level(unit),))]

    def master(self, unit: float) -> list[osc.Message]:
        cfg = self.config
        if cfg.master_exec:
            return [osc.Message(self._addr(f"Fader{cfg.master_exec}"),
                                (self._level(unit),))]
        return [osc.Message(self._cmd_addr(),
                            (f"Master 2.1 At {unit * 100:.1f}",))]

    def button(self, row: str, idx: int, down: bool) -> list[osc.Message]:
        execs = (self.config.select_execs if row == "select"
                 else self.config.mute_execs)
        if idx >= len(execs):
            return []
        return [osc.Message(self._addr(f"Key{execs[idx]}"),
                            (1 if down else 0,))]

    def encoder(self, idx: int, unit: float) -> list[osc.Message]:
        cfg = self.config
        if idx >= len(cfg.encoder_execs):
            return []
        if getattr(cfg, "ma3_encoder", "encoder") == "fader":
            return [osc.Message(self._addr(f"Fader{cfg.encoder_execs[idx]}"),
                                (self._level(unit),))]
        # The documented path: /Encoder<x> takes a relative step in
        # percent, so send the delta since the last position rather than
        # the absolute level.
        prev = self._enc_pos.get(idx, 0.0)
        self._enc_pos[idx] = unit
        step = round((unit - prev) * 100)
        if not step:
            return []
        return [osc.Message(self._addr(f"Encoder{cfg.encoder_execs[idx]}"),
                            (step,))]

    def transport(self, key: str, down: bool) -> list[osc.Message]:
        cmd = {"play": self.config.cmd_play, "stop": self.config.cmd_stop,
               "rewind": self.config.cmd_rewind,
               "fastfwd": self.config.cmd_fastfwd,
               "record": self.config.cmd_record}.get(key, "")
        if cmd and down:
            return [osc.Message(self._cmd_addr(), (cmd,))]
        return []

    # -- paging / lifecycle ------------------------------------------------

    def set_page(self, page: int) -> list[osc.Message]:
        return []      # MA3 re-broadcasts levels itself when sending is on

    def hello(self) -> list[osc.Message]:
        return []

    def tick(self, now: float) -> list[osc.Message]:
        return []

    def strip_labels(self, page: int) -> list[tuple[int, int, str]]:
        out = []
        for s in range(8):
            if s < len(self.config.fader_execs):
                out.append((s, 0, f"Ex {self.config.fader_execs[s]}"))
                out.append((s, 1, f"Pg {page}"))
        return out

    # -- console -> surface ------------------------------------------------

    def feedback(self, msg: osc.Message):
        if not msg.args:
            return []
        # Real executor names, pushed by the optional MA3 Lua plugin
        # (ma3-plugin/): /xbridge/label/<strip 1-8> "name".
        parts = msg.address.strip("/").split("/")
        if (len(parts) == 3 and parts[0] == "xbridge" and parts[1] == "label"
                and isinstance(msg.args[0], str)):
            try:
                strip = int(parts[2])
            except ValueError:
                return []
            if 1 <= strip <= 8:
                return [LabelFB(strip - 1, 0, msg.args[0])]
            return []
        enumerated = self._playback_feedback(msg)
        if enumerated is not None:
            return enumerated
        leaf = self._leaf_of(msg.address)
        if leaf is None:
            return []
        unit = _unit_percent(msg.args[0])
        if unit is None:
            return []
        cfg = self.config
        if leaf.startswith("Fader"):
            try:
                exec_no = int(leaf[5:])
            except ValueError:
                return []
            if exec_no in cfg.fader_execs:
                return [FaderFB(cfg.fader_execs.index(exec_no), unit)]
            if exec_no in cfg.encoder_execs:
                return [RingFB(cfg.encoder_execs.index(exec_no), unit)]
            if cfg.master_exec and exec_no == cfg.master_exec:
                return [FaderFB(8, unit)]
        elif leaf.startswith("Key"):
            try:
                exec_no = int(leaf[3:])
            except ValueError:
                return []
            if exec_no in cfg.select_execs:
                return [ButtonFB("select", cfg.select_execs.index(exec_no),
                                 unit > 0)]
            if exec_no in cfg.mute_execs:
                return [ButtonFB("mute", cfg.mute_execs.index(exec_no),
                                 unit > 0)]
        return []

    def _playback_feedback(self, msg: osc.Message):
        """MA3's *output* format, which is nothing like its input format.

        Moving a fader on the console does not echo /Page1/Fader201 back.
        Per "Object Playback Feedback", MA3 sends the object's enumerated
        address with an ``sif`` payload::

            /13.13.1.6.1 ,sif, "FaderMaster", 3, 63.5    (a sequence)
            /13.12.3.1   ,sif, "FaderMaster", 3, 63.5    (a master)

        The leading number is a pool index, not an executor number, so it
        can only be tied to a strip by a table the user supplies
        (``ma3_feedback``: {"13.13.1.6.1": 1} = that object drives strip
        1). Without one there is nothing to map it to - but the message
        is still recognised, so the sniffer can show what to put in the
        table. Returns None when this is not a playback feedback message.
        """
        addr = msg.address.lstrip("/")
        prefix = self.config.prefix
        if prefix and addr.startswith(prefix + "/"):
            addr = addr[len(prefix) + 1:]
        if not addr or not all(p.isdigit() for p in addr.split(".")):
            return None
        if len(msg.args) < 2 or not isinstance(msg.args[0], str):
            return None
        level = next((a for a in reversed(msg.args)
                      if isinstance(a, (int, float))
                      and not isinstance(a, bool)), None)
        if level is None:
            return None
        table = getattr(self.config, "ma3_feedback", {}) or {}
        strip = table.get(addr)
        if strip is None:
            return []
        func = msg.args[0].lower()
        idx = int(strip) - 1
        if "fader" in func or "master" in func:
            unit = _unit_percent(level)
            return [] if unit is None else [FaderFB(idx, unit)]
        return [ButtonFB("select", idx, float(level) > 0)]

    # -- helpers -----------------------------------------------------------

    def _addr(self, leaf: str) -> str:
        cfg = self.config
        p = f"/{cfg.prefix}" if cfg.prefix else ""
        if getattr(cfg, "ma3_addr", "page") == "selected":
            # /Fader201 - MA3 applies it to the selected page. The escape
            # hatch when the OSC line's "Page" address cell is renamed.
            return f"{p}/{leaf}"
        return f"{p}/Page{cfg.page}/{leaf}"

    def _cmd_addr(self) -> str:
        return f"/{self.config.prefix}/cmd" if self.config.prefix else "/cmd"

    def _leaf_of(self, address: str) -> str | None:
        want = self._addr("")
        if address.startswith(want):
            return address[len(want):]
        return None


_XREMOTE_SECS = 8.0


class X32Target:
    """Behringer X32/M32 over OSC (port 10023).

    A "page" is a bank of 8 input channels: page 1 = ch 1-8, page 2 =
    ch 9-16, up to page 4. Faders are channel levels, MUTE is a real
    mute (lit when muted), SELECT selects the channel on the desk,
    encoders are pan, the master fader is the main stereo bus, and the
    scribble strips carry the console's own channel names.
    """

    name = "x32"
    default_send_port = 10023
    pages = 4

    def __init__(self, config):
        self.config = config
        self._names: dict[int, str] = {}      # channel (1-32) -> name
        self._mutes: dict[int, bool] = {}     # channel -> muted?
        self._last_xremote = 0.0

    def _ch(self, strip: int) -> int:
        return (self.config.page - 1) * 8 + strip + 1

    # -- surface -> console ------------------------------------------------

    def fader(self, strip: int, unit: float) -> list[osc.Message]:
        return [osc.Message(f"/ch/{self._ch(strip):02d}/mix/fader",
                            (float(unit),))]

    def master(self, unit: float) -> list[osc.Message]:
        return [osc.Message("/main/st/mix/fader", (float(unit),))]

    def button(self, row: str, idx: int, down: bool) -> list[osc.Message]:
        ch = self._ch(idx)
        if row == "mute":
            if not down:
                return []
            muted = self._mutes.get(ch, False)
            self._mutes[ch] = not muted
            # mix/on is "channel on": 0 = muted. Toggle on press.
            return [osc.Message(f"/ch/{ch:02d}/mix/on",
                                (0 if not muted else 1,))]
        if row == "select" and down:
            return [osc.Message("/-stat/selidx", (ch - 1,))]
        return []

    def encoder(self, idx: int, unit: float) -> list[osc.Message]:
        return [osc.Message(f"/ch/{self._ch(idx):02d}/mix/pan",
                            (float(unit),))]

    def transport(self, key: str, down: bool) -> list[osc.Message]:
        return []      # tape transport is possible; unmapped until asked for

    # -- paging / lifecycle ------------------------------------------------

    def set_page(self, page: int) -> list[osc.Message]:
        return self._bank_queries()

    def hello(self) -> list[osc.Message]:
        return [osc.Message("/xremote")] + self._bank_queries()

    def tick(self, now: float) -> list[osc.Message]:
        if now - self._last_xremote >= _XREMOTE_SECS:
            self._last_xremote = now
            return [osc.Message("/xremote")]
        return []

    def _bank_queries(self) -> list[osc.Message]:
        """Queries for the visible bank: an X32 address with no arguments
        is a question, and the console replies with the current value."""
        out = []
        for s in range(8):
            ch = self._ch(s)
            out.append(osc.Message(f"/ch/{ch:02d}/mix/fader"))
            out.append(osc.Message(f"/ch/{ch:02d}/mix/on"))
            out.append(osc.Message(f"/ch/{ch:02d}/mix/pan"))
            out.append(osc.Message(f"/ch/{ch:02d}/config/name"))
        out.append(osc.Message("/main/st/mix/fader"))
        return out

    def strip_labels(self, page: int) -> list[tuple[int, int, str]]:
        out = []
        for s in range(8):
            ch = (page - 1) * 8 + s + 1
            out.append((s, 0, self._names.get(ch, f"Ch {ch}")))
            out.append((s, 1, f"MUTED" if self._mutes.get(ch) else ""))
        return out

    # -- console -> surface ------------------------------------------------

    def feedback(self, msg: osc.Message):
        parts = msg.address.strip("/").split("/")
        if msg.address == "/main/st/mix/fader" and msg.args:
            unit = _unit_float(msg.args[0])
            return [FaderFB(8, unit)] if unit is not None else []
        if len(parts) != 4 or parts[0] != "ch":
            return []
        try:
            ch = int(parts[1])
        except ValueError:
            return []
        lo = (self.config.page - 1) * 8 + 1
        strip = ch - lo
        leaf = "/".join(parts[2:])
        if leaf == "config/name" and msg.args:
            self._names[ch] = str(msg.args[0])
            if 0 <= strip < 8:
                return [LabelFB(strip, 0, self._names[ch])]
            return []
        if not 0 <= strip < 8 or not msg.args:
            return []
        if leaf == "mix/fader":
            unit = _unit_float(msg.args[0])
            return [FaderFB(strip, unit)] if unit is not None else []
        if leaf == "mix/on":
            on = bool(_int_of(msg.args[0]))
            self._mutes[ch] = not on
            return [ButtonFB("mute", strip, not on),
                    LabelFB(strip, 1, "MUTED" if not on else "")]
        if leaf == "mix/pan":
            unit = _unit_float(msg.args[0])
            return [RingFB(strip, unit)] if unit is not None else []
        return []


class MagicQTarget:
    """ChamSys MagicQ over OSC.

    MagicQ's built-in addresses reach the first 10 playbacks (per its
    manual): /pb/<n> float 0-1 sets a fader, /pb/<n>/go, /pb/<n>/flash
    (with a real 0 on release), /pb/<n>/pause, /pb/<n>/release. Sending
    /feedback/pb+exec once makes MagicQ dump current state and stream
    changes - which is what drives the motors. Enable OSC in MagicQ:
    Setup > View Settings > Network (receive 8000 / transmit 9000, the
    manual's own suggested defaults, which match this bridge's).
    """

    name = "magicq"
    default_send_port = 8000
    pages = 1                          # built-in OSC reaches pb 1-10 only

    def __init__(self, config):
        self.config = config

    def fader(self, strip: int, unit: float) -> list[osc.Message]:
        return [osc.Message(f"/pb/{strip + 1}", (float(unit),))]

    def master(self, unit: float) -> list[osc.Message]:
        pb = getattr(self.config, "magicq_master_pb", 9)
        if not pb:
            return []
        return [osc.Message(f"/pb/{pb}", (float(unit),))]

    def button(self, row: str, idx: int, down: bool) -> list[osc.Message]:
        pb = idx + 1
        if row == "select":
            return [osc.Message(f"/pb/{pb}/go", (1,))] if down else []
        if row == "mute":               # FLASH: real press and release
            return [osc.Message(f"/pb/{pb}/flash", (1 if down else 0,))]
        return []

    def encoder(self, idx: int, unit: float) -> list[osc.Message]:
        # Execute grid 1, items 1-8.
        return [osc.Message(f"/exec/1/{idx + 1}", (float(unit),))]

    def transport(self, key: str, down: bool) -> list[osc.Message]:
        if key == "stop" and down:      # the one universally safe transport
            return [osc.Message("/dbo", (1,))]
        if key == "play" and down:
            return [osc.Message("/dbo", (0,))]
        return []

    def set_page(self, page: int) -> list[osc.Message]:
        return []

    def hello(self) -> list[osc.Message]:
        return [osc.Message("/feedback/pb+exec")]

    def tick(self, now: float) -> list[osc.Message]:
        return []

    def strip_labels(self, page: int) -> list[tuple[int, int, str]]:
        return [(s, ln, txt) for s in range(8)
                for ln, txt in ((0, f"PB {s + 1}"), (1, "MagicQ"))]

    def feedback(self, msg: osc.Message):
        parts = msg.address.strip("/").split("/")
        if not parts:
            return []
        if parts[0] == "pb" and len(parts) >= 2:
            try:
                pb = int(parts[1])
            except ValueError:
                return []
            if len(parts) == 2 and msg.args:
                unit = _unit_float(msg.args[0])
                if unit is None:
                    return []
                if 1 <= pb <= 8:
                    return [FaderFB(pb - 1, unit)]
                if pb == getattr(self.config, "magicq_master_pb", 9):
                    return [FaderFB(8, unit)]
            elif len(parts) == 3 and parts[2] == "flash" and msg.args:
                if 1 <= pb <= 8:
                    return [ButtonFB("mute", pb - 1, bool(_int_of(msg.args[0])))]
        elif parts[0] == "exec" and len(parts) == 3 and msg.args:
            try:
                page, item = int(parts[1]), int(parts[2])
            except ValueError:
                return []
            unit = _unit_float(msg.args[0])
            if page == 1 and 1 <= item <= 8 and unit is not None:
                return [RingFB(item - 1, unit)]
        return []


class ResolumeTarget:
    """Resolume Arena/Avenue over OSC (input port 7000 by default).

    Faders are layer opacity (/composition/layers/<n>/video/opacity),
    MUTE bypasses the layer, SELECT connects the matching column, the
    encoders ride the layer masters, and the master fader is the
    composition master. Banks page layers 8 at a time. For motor
    feedback, enable OSC *output* in Resolume's preferences and aim it at
    this bridge's listen port.
    """

    name = "resolume"
    default_send_port = 7000
    pages = 4

    def __init__(self, config):
        self.config = config
        self._bypassed: dict[int, bool] = {}

    def _layer(self, strip: int) -> int:
        return (self.config.page - 1) * 8 + strip + 1

    def fader(self, strip: int, unit: float) -> list[osc.Message]:
        return [osc.Message(
            f"/composition/layers/{self._layer(strip)}/video/opacity",
            (float(unit),))]

    def master(self, unit: float) -> list[osc.Message]:
        return [osc.Message("/composition/master", (float(unit),))]

    def button(self, row: str, idx: int, down: bool) -> list[osc.Message]:
        if row == "select" and down:
            col = (self.config.page - 1) * 8 + idx + 1
            return [osc.Message(f"/composition/columns/{col}/connect", (1,))]
        if row == "mute" and down:
            layer = self._layer(idx)
            bypassed = self._bypassed.get(layer, False)
            self._bypassed[layer] = not bypassed
            return [osc.Message(f"/composition/layers/{layer}/bypassed",
                                (0 if bypassed else 1,))]
        return []

    def encoder(self, idx: int, unit: float) -> list[osc.Message]:
        return [osc.Message(f"/composition/layers/{self._layer(idx)}/master",
                            (float(unit),))]

    def transport(self, key: str, down: bool) -> list[osc.Message]:
        return []

    def set_page(self, page: int) -> list[osc.Message]:
        return []

    def hello(self) -> list[osc.Message]:
        return []

    def tick(self, now: float) -> list[osc.Message]:
        return []

    def strip_labels(self, page: int) -> list[tuple[int, int, str]]:
        return [(s, ln, txt) for s in range(8)
                for ln, txt in ((0, f"Lay {(page - 1) * 8 + s + 1}"),
                                (1, "Resolme"))]

    def feedback(self, msg: osc.Message):
        if not msg.args:
            return []
        if msg.address == "/composition/master":
            unit = _unit_float(msg.args[0])
            return [FaderFB(8, unit)] if unit is not None else []
        parts = msg.address.strip("/").split("/")
        if len(parts) < 4 or parts[0] != "composition" or parts[1] != "layers":
            return []
        try:
            layer = int(parts[2])
        except ValueError:
            return []
        strip = layer - 1 - (self.config.page - 1) * 8
        leaf = "/".join(parts[3:])
        if leaf == "bypassed":
            self._bypassed[layer] = bool(_int_of(msg.args[0]))
            if 0 <= strip < 8:
                return [ButtonFB("mute", strip, self._bypassed[layer])]
            return []
        if not 0 <= strip < 8:
            return []
        if leaf == "video/opacity":
            unit = _unit_float(msg.args[0])
            return [FaderFB(strip, unit)] if unit is not None else []
        if leaf == "master":
            unit = _unit_float(msg.args[0])
            return [RingFB(strip, unit)] if unit is not None else []
        return []


class CompanionTarget:
    """Bitfocus Companion over OSC (listen port 12321).

    The X-Touch becomes a Companion surface: SELECT row presses buttons on
    row 0 of the current Companion page, MUTE row presses row 1, the
    transport keys press row 2 columns 0-4, all with true down/up so
    latch and momentary actions both behave. Faders write custom
    variables fader1-fader8 (and master) as 0-100, ready to use in any
    Companion action; encoders send rotate-left/right on row 3.
    Companion doesn't stream OSC feedback, so this target is one-way.
    """

    name = "companion"
    default_send_port = 12321
    pages = 99

    def __init__(self, config):
        self.config = config

    def _loc(self, row: int, col: int, verb: str) -> str:
        return f"/location/{self.config.page}/{row}/{col}/{verb}"

    def fader(self, strip: int, unit: float) -> list[osc.Message]:
        return [osc.Message(f"/custom-variable/fader{strip + 1}/value",
                            (round(unit * 100),))]

    def master(self, unit: float) -> list[osc.Message]:
        return [osc.Message("/custom-variable/master/value",
                            (round(unit * 100),))]

    def button(self, row: str, idx: int, down: bool) -> list[osc.Message]:
        r = 0 if row == "select" else 1
        return [osc.Message(self._loc(r, idx, "down" if down else "up"))]

    def encoder(self, idx: int, unit: float) -> list[osc.Message]:
        return []      # handled as raw ticks via encoder_ticks below

    def encoder_ticks(self, idx: int, ticks: int) -> list[osc.Message]:
        verb = "rotate-right" if ticks > 0 else "rotate-left"
        return [osc.Message(self._loc(3, idx, verb))
                for _ in range(min(8, abs(ticks)))]

    def transport(self, key: str, down: bool) -> list[osc.Message]:
        col = {"rewind": 0, "fastfwd": 1, "stop": 2, "play": 3,
               "record": 4}.get(key)
        if col is None:
            return []
        return [osc.Message(self._loc(2, col, "down" if down else "up"))]

    def set_page(self, page: int) -> list[osc.Message]:
        return []

    def hello(self) -> list[osc.Message]:
        return []

    def tick(self, now: float) -> list[osc.Message]:
        return []

    def strip_labels(self, page: int) -> list[tuple[int, int, str]]:
        return [(s, ln, txt) for s in range(8)
                for ln, txt in ((0, f"Col {s}"), (1, f"Cmp p{page}"))]

    def feedback(self, msg: osc.Message):
        return []      # Companion does not push OSC state


class EosTarget:
    """ETC Eos family over OSC.

    Uses Eos's virtual OSC fader banks: hello() sends
    /eos/fader/1/config/10 to create bank 1, then the strips ride faders
    /eos/fader/1/<n> as floats 0.0-1.0. Eos streams positions back on
    /eos/out/fader/1/<n> (delayed ~3s for faders Eos itself saw us move -
    an Eos behaviour, not a bug here). SELECT is the fader's Fire button,
    MUTE is its Stop, PLAY/STOP are the master Go and Stop/Back keys.

    Eos setup: enable OSC RX/TX (Setup > System > Show Control > OSC),
    UDP RX port matching the bridge's send port (default here 8000), TX
    port + IP aimed back at the bridge's listen port.
    """

    name = "eos"
    default_send_port = 8000
    pages = 1

    def __init__(self, config):
        self.config = config

    def fader(self, strip: int, unit: float) -> list[osc.Message]:
        return [osc.Message(f"/eos/fader/1/{strip + 1}", (float(unit),))]

    def master(self, unit: float) -> list[osc.Message]:
        return []          # Eos has no OSC grand master; deliberately unmapped

    def button(self, row: str, idx: int, down: bool) -> list[osc.Message]:
        n = idx + 1
        if row == "select":
            return [osc.Message(f"/eos/fader/1/{n}/fire", (1.0 if down else 0.0,))]
        if row == "mute" and down:
            return [osc.Message(f"/eos/fader/1/{n}/stop", (1.0,))]
        return []

    def encoder(self, idx: int, unit: float) -> list[osc.Message]:
        return []          # Eos encoders are a later, careful project

    def transport(self, key: str, down: bool) -> list[osc.Message]:
        if key == "play":
            return [osc.Message("/eos/key/go_0", (1.0 if down else 0.0,))]
        if key == "stop":
            return [osc.Message("/eos/key/stop", (1.0 if down else 0.0,))]
        return []

    def set_page(self, page: int) -> list[osc.Message]:
        return []

    def hello(self) -> list[osc.Message]:
        return [osc.Message("/eos/fader/1/config/10")]

    def tick(self, now: float) -> list[osc.Message]:
        return []

    def strip_labels(self, page: int) -> list[tuple[int, int, str]]:
        return [(s, ln, txt) for s in range(8)
                for ln, txt in ((0, f"Fdr {s + 1}"), (1, "Eos"))]

    def feedback(self, msg: osc.Message):
        parts = msg.address.strip("/").split("/")
        if (len(parts) == 5 and parts[:3] == ["eos", "out", "fader"]
                and parts[3] == "1" and msg.args):
            try:
                n = int(parts[4])
            except ValueError:
                return []
            unit = _unit_float(msg.args[0])
            if 1 <= n <= 8 and unit is not None:
                return [FaderFB(n - 1, unit)]
        return []


class GenericOSCTarget:
    """Anything that listens to OSC, described by address templates.

    Config supplies templates with a {n} placeholder; {n} is the strip
    number (1-8) plus 8 per page above the first, so paging works if the
    receiver numbers things. An empty template leaves that control
    unmapped. gen_scale picks the fader argument: "float01" (0.0-1.0) or
    "int100" (0-100). Buttons send 1 on press and 0 on release.
    Feedback: incoming messages matching gen_fader on a visible strip
    drive the motors; everything else is ignored.
    """

    name = "generic"
    default_send_port = 9001
    pages = 99

    def __init__(self, config):
        self.config = config

    def _n(self, strip: int) -> int:
        return (self.config.page - 1) * 8 + strip + 1

    def _fill(self, template: str, n: int) -> str:
        return template.replace("{n}", str(n))

    def _level(self, unit: float):
        if getattr(self.config, "gen_scale", "float01") == "int100":
            return round(unit * 100)
        return float(unit)

    def fader(self, strip: int, unit: float) -> list[osc.Message]:
        t = getattr(self.config, "gen_fader", "")
        if not t:
            return []
        return [osc.Message(self._fill(t, self._n(strip)),
                            (self._level(unit),))]

    def master(self, unit: float) -> list[osc.Message]:
        t = getattr(self.config, "gen_master", "")
        if not t:
            return []
        return [osc.Message(t, (self._level(unit),))]

    def button(self, row: str, idx: int, down: bool) -> list[osc.Message]:
        t = getattr(self.config,
                    "gen_select" if row == "select" else "gen_mute", "")
        if not t:
            return []
        return [osc.Message(self._fill(t, self._n(idx)),
                            (1 if down else 0,))]

    def encoder(self, idx: int, unit: float) -> list[osc.Message]:
        t = getattr(self.config, "gen_encoder", "")
        if not t:
            return []
        return [osc.Message(self._fill(t, self._n(idx)),
                            (self._level(unit),))]

    def transport(self, key: str, down: bool) -> list[osc.Message]:
        return []

    def set_page(self, page: int) -> list[osc.Message]:
        return []

    def hello(self) -> list[osc.Message]:
        return []

    def tick(self, now: float) -> list[osc.Message]:
        return []

    def strip_labels(self, page: int) -> list[tuple[int, int, str]]:
        return [(s, ln, txt) for s in range(8)
                for ln, txt in ((0, f"#{(page - 1) * 8 + s + 1}"),
                                (1, "OSC"))]

    def feedback(self, msg: osc.Message):
        t = getattr(self.config, "gen_fader", "")
        if not t or "{n}" not in t or not msg.args:
            return []
        for strip in range(8):
            if msg.address == self._fill(t, self._n(strip)):
                unit = _unit_float(msg.args[0])
                if unit is not None and getattr(
                        self.config, "gen_scale", "float01") == "int100":
                    unit = max(0.0, min(1.0, float(msg.args[0]) / 100.0))
                return [FaderFB(strip, unit)] if unit is not None else []
        return []


class MA2Target:
    """grandMA2 via its Web Remote websocket - MA2 has no OSC.

    The same route ShowCockpit takes. The console's built-in web server
    (port 80) accepts a websocket session: {"session":0} to get a session
    number, a login with an MD5'd password, then JSON-wrapped command-line
    commands - so faders ride "Executor <page>.<n> At <pct>" and buttons
    are Go/Off, exactly as if typed. Keepalives hold the session open,
    and polling the "playbacks" request returns executor levels and
    titles, which this target parses (best-effort - the response shape is
    reverse-engineered, not documented) to drive the motors and strips.

    MA2 setup: Setup > Console > Global Settings > Remotes: "Login
    enabled"; make sure the web remote works from a browser first. The
    default remote user is "remote"/"remote" - change ma2_user /
    ma2_password in the mapping if yours differs.

    Unlike the OSC targets this speaks TCP websocket; the runner opens a
    websocket session for it instead of a UDP socket.
    """

    name = "ma2"
    transport = "ma2ws"
    default_send_port = 80
    pages = 9999

    def __init__(self, config):
        self.config = config
        self._labels: dict[int, str] = {}     # strip -> title

    def _exec(self, strip: int) -> str:
        return f"{self.config.page}.{strip + 1}"

    # ---- surface -> console (command strings, not OSC) -------------------

    def fader(self, strip: int, unit: float) -> list[str]:
        return [f"Executor {self._exec(strip)} At {unit * 100:.1f}"]

    def master(self, unit: float) -> list[str]:
        cmd = getattr(self.config, "ma2_master_cmd", "SpecialMaster 2.1 At {pct}")
        if not cmd:
            return []
        return [cmd.replace("{pct}", f"{unit * 100:.1f}")]

    def button(self, row: str, idx: int, down: bool) -> list[str]:
        if not down:
            return []
        if row == "select":
            return [f"Go Executor {self._exec(idx)}"]
        if row == "mute":
            return [f"Off Executor {self._exec(idx)}"]
        return []

    def encoder(self, idx: int, unit: float) -> list[str]:
        # Encoders ride executors 9-16 of the same page.
        return [f"Executor {self.config.page}.{idx + 9} At {unit * 100:.1f}"]

    def transport(self, key: str, down: bool) -> list[str]:
        if not down:
            return []
        if key == "play":
            return ["Go Executor " + self._exec(0)]
        if key == "stop":
            return ["Pause Executor " + self._exec(0)]
        if key == "rewind":
            return ["GoBack Executor " + self._exec(0)]
        return []

    def set_page(self, page: int) -> list[str]:
        return []

    def strip_labels(self, page: int) -> list[tuple[int, int, str]]:
        out = []
        for s in range(8):
            out.append((s, 0, self._labels.get(s, f"Ex {page}.{s + 1}")))
            out.append((s, 1, f"MA2 p{page}"))
        return out

    # ---- console -> surface (parsed web-remote JSON) ---------------------

    def playbacks_request(self, session: int) -> dict:
        """The poll that makes MA2 report executor levels and titles."""
        return {
            "requestType": "playbacks",
            "startIndex": [(self.config.page - 1) * 0],
            "itemsCount": [16],
            "pageIndex": self.config.page - 1,
            "itemsType": [2],
            "view": 2,
            "execButtonViewMode": 1,
            "buttonsViewMode": 0,
            "session": session,
            "maxRequests": 1,
        }

    def ws_feedback(self, msg: dict):
        """Surface intents out of a web-remote JSON message (best-effort)."""
        if msg.get("responseType") != "playbacks":
            return []
        out = []
        try:
            for group in msg.get("itemGroups", []):
                for row in group.get("items", []):
                    for item in row if isinstance(row, list) else [row]:
                        out += self._item_feedback(item)
        except (TypeError, AttributeError):
            return []
        return out

    def _item_feedback(self, item: dict):
        out = []
        idx = item.get("iExec")
        if not isinstance(idx, int) or not 0 <= idx < 8:
            return out
        title = (item.get("i") or {}).get("t")
        if isinstance(title, str) and title.strip():
            if self._labels.get(idx) != title.strip():
                self._labels[idx] = title.strip()
                out.append(LabelFB(idx, 0, title.strip()))
        for block in item.get("executorBlocks", []):
            fader = block.get("fader") if isinstance(block, dict) else None
            if isinstance(fader, dict):
                v = fader.get("v")
                if isinstance(v, (int, float)):
                    out.append(FaderFB(idx, max(0.0, min(1.0, float(v)))))
        return out


TARGETS = {
    "ma3": MA3Target,
    "ma2": MA2Target,
    "x32": X32Target,
    "magicq": MagicQTarget,
    "resolume": ResolumeTarget,
    "companion": CompanionTarget,
    "eos": EosTarget,
    "generic": GenericOSCTarget,
}


def make_target(config):
    """The Target for config.target, defaulting to MA3."""
    kind = getattr(config, "target", "ma3") or "ma3"
    cls = TARGETS.get(kind)
    if cls is None:
        raise ValueError(
            f"unknown target {kind!r} (have: {', '.join(sorted(TARGETS))})")
    return cls(config)


def _unit_percent(arg) -> float | None:
    """MA3 levels: int/float 0-100 -> 0.0-1.0."""
    if isinstance(arg, bool):
        return 1.0 if arg else 0.0
    if isinstance(arg, (int, float)):
        v = float(arg)
        return max(0.0, min(1.0, v / 100.0)) if v >= 0 else None
    return None


def _unit_float(arg) -> float | None:
    """X32 levels: float 0.0-1.0 already."""
    if isinstance(arg, bool):
        return 1.0 if arg else 0.0
    if isinstance(arg, (int, float)):
        return max(0.0, min(1.0, float(arg)))
    return None


def _int_of(arg) -> int:
    if isinstance(arg, (int, float)):
        return int(arg)
    return 0
