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

    # -- surface -> console ------------------------------------------------

    def fader(self, strip: int, unit: float) -> list[osc.Message]:
        cfg = self.config
        if strip >= len(cfg.fader_execs):
            return []
        return [osc.Message(self._addr(f"Fader{cfg.fader_execs[strip]}"),
                            (round(unit * 100),))]

    def master(self, unit: float) -> list[osc.Message]:
        cfg = self.config
        if cfg.master_exec:
            return [osc.Message(self._addr(f"Fader{cfg.master_exec}"),
                                (round(unit * 100),))]
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
        return [osc.Message(self._addr(f"Fader{cfg.encoder_execs[idx]}"),
                            (round(unit * 100),))]

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

    # -- helpers -----------------------------------------------------------

    def _addr(self, leaf: str) -> str:
        cfg = self.config
        p = f"/{cfg.prefix}" if cfg.prefix else ""
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


TARGETS = {
    "ma3": MA3Target,
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
