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


def make_target(config):
    """The Target for config.target, defaulting to MA3."""
    kind = getattr(config, "target", "ma3") or "ma3"
    if kind == "x32":
        return X32Target(config)
    if kind == "ma3":
        return MA3Target(config)
    raise ValueError(f"unknown target {kind!r} (have: ma3, x32)")


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
