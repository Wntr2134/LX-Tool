"""Find the OSC dialect a grandMA3 actually wants.

MA3's OSC page has three cells that silently decide whether a message is
heard at all, and no two sources agree on what to put in them:

* **Prefix.** The manual says "if a prefix is specified, only OSC messages
  beginning with the specified prefix are processed" - so a wrong prefix
  is not an error, it is silence. A stock console has none; MA's Open
  Stage Control walkthrough assumes one of ``gma3``.
* **Value type.** Faders take "i f, 0 ... 100", so int and float are both
  documented; the 0-1 unit float is what some templates send anyway.
* **FaderRange.** Set to 255 instead of 100, the same fader wants 0-255.
* **Address cells.** The OSC line has editable "Page", "Fader", "Key" and
  "Encoder" address names, so /Page1/Fader201 only routes if they are
  still at their defaults; /Fader201 (the selected page) is the fallback.

Guessing costs a show. This walks every combination, one at a time, with
a pause between, so the console can be watched: whichever step moves the
executor names the dialect, and the answer is written straight back into
the mapping.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field

from . import osc
from .bridge import Config
from .targets import make_target

# Ordered most-likely first, so a stock console is usually answered in
# the first step or two. (prefix, value form, address form)
#
# The last two are a different route entirely: command-line syntax to
# /cmd, which does not use the OSC line's Fader or Page address cells at
# all and needs Receive Command rather than Receive. If only those move
# the fader, executor addressing is the problem - and "cmd" is also a
# perfectly good way to run the show.
DIALECTS: tuple[tuple[str, str, str], ...] = (
    ("", "int100", "page"),        # the manual's own example, stock console
    ("", "float100", "page"),
    ("gma3", "int100", "page"),    # MA's Open Stage Control walkthrough
    ("gma3", "float100", "page"),
    ("", "int100", "selected"),    # "Page" address cell renamed or cleared
    ("gma3", "int100", "selected"),
    ("", "int255", "page"),        # FaderRange moved off 100
    ("gma3", "int255", "page"),
    ("", "float01", "page"),       # 0-1 unit float
    ("gma3", "float01", "page"),
    ("", "int255", "selected"),
    ("gma3", "int255", "selected"),
    ("", "cmd", "page"),           # command line, Executor page.exec
    ("", "cmd2", "page"),          # command line, bare executor number
    ("", "cmd3", "page"),          # command line, MA's OSC-page wording
    ("gma3", "cmd", "page"),       # the same, prefixed
)

# MA documents two different command-line spellings for the same thing:
# the FaderMaster keyword page says "FaderMaster [Object] [Number] At
# [Value]" (example: "FaderMaster 205 At 50"), while the OSC page writes
# "FaderMaster Page 1.201 At 50". "Page" is not an object type, and 2.4
# answers that form with IllegalProperty - so all three are swept rather
# than trusting either page.
CMD_TEMPLATES = {
    "cmd": "FaderMaster Executor {page}.{exec} At {pct}",
    "cmd2": "FaderMaster {exec} At {pct}",
    "cmd3": "FaderMaster Page {page}.{exec} At {pct}",
}

CMD_NAMES = {
    "cmd": "command line, Executor page.exec",
    "cmd2": "command line, executor only (current page)",
    "cmd3": "command line, Page page.exec",
}


@dataclass
class Step:
    """One dialect, and the exact message it puts on the wire."""

    index: int
    prefix: str
    value: str
    addr_form: str = "page"
    address: str = ""
    args: tuple = ()
    args_low: tuple = ()

    @property
    def label(self) -> str:
        pfx = f"/{self.prefix}" if self.prefix else "(no prefix)"
        if self.value in CMD_TEMPLATES:
            return f"{self.index + 1}. {pfx} + {CMD_NAMES[self.value]}"
        page = "" if self.addr_form == "page" else " + no /Page"
        return f"{self.index + 1}. {pfx} + {self.value}{page}"

    @property
    def line(self) -> str:
        return f"{self.address} {' '.join(str(a) for a in self.args)}".rstrip()

    def as_dict(self) -> dict:
        return {"index": self.index, "prefix": self.prefix,
                "value": self.value, "addr_form": self.addr_form,
                "label": self.label, "sent": self.line}


@dataclass
class Ma3Probe:
    """Walks the dialects against one executor and reports what it sent."""

    host: str = "127.0.0.1"
    port: int = 8000
    page: int = 1
    exec_: int = 201
    level: float = 0.75
    steps: list[Step] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.steps:
            self.steps = [self._build(i, *d) for i, d in enumerate(DIALECTS)]

    def _build(self, index: int, prefix: str, value: str,
               addr_form: str = "page") -> Step:
        """Ask the real target to format it - the probe must not carry a
        second, drifting copy of the addressing rules."""
        is_cmd = value in CMD_TEMPLATES
        cfg = Config(target="ma3", prefix=prefix, ma3_addr=addr_form,
                     page=self.page, fader_execs=(self.exec_,),
                     ma3_fader="cmd" if is_cmd else "osc",
                     ma3_value="int100" if is_cmd else value)
        if is_cmd:
            cfg.ma3_fader_cmd = CMD_TEMPLATES[value]
        target = make_target(cfg)
        step = Step(index=index, prefix=prefix, value=value,
                    addr_form=addr_form)
        for m in target.fader(0, self.level):
            if isinstance(m, osc.Message):
                step.address, step.args = m.address, tuple(m.args)
                break
        for m in target.fader(0, 0.0):
            if isinstance(m, osc.Message):
                step.args_low = tuple(m.args)
                break
        return step

    def datagram(self, index: int) -> bytes:
        s = self.steps[index]
        return osc.encode(osc.Message(s.address, s.args))

    def datagram_low(self, index: int) -> bytes:
        """The same dialect at zero.

        Every step sends zero first: an executor already sitting near the
        test level would not visibly move otherwise, and "whichever step
        moves the fader" is the whole method.
        """
        s = self.steps[index]
        return osc.encode(osc.Message(s.address, s.args_low))

    def run(self, *, dwell: float = 1.5, on_step=None,
            sock=None, sleep=time.sleep) -> list[Step]:
        """Send each dialect in turn, pausing so a human can watch.

        Each step drives the fader to zero and then up, so it moves
        whatever it was sitting at before. ``on_step`` is called as each
        one goes out, so a UI can keep pace with the wire.
        """
        own = sock is None
        sock = sock or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for step in self.steps:
                if not step.address:
                    continue
                sock.sendto(self.datagram_low(step.index),
                            (self.host, self.port))
                if dwell:
                    sleep(min(dwell / 3.0, 0.5))
                sock.sendto(self.datagram(step.index), (self.host, self.port))
                if on_step is not None:
                    on_step(step)
                if dwell:
                    sleep(dwell)
        finally:
            if own:
                sock.close()
        return self.steps

    def apply(self, config: Config, index: int) -> Config:
        """Write the winning dialect into a mapping, so the answer is
        kept rather than remembered."""
        step = self.steps[index]
        config.prefix = step.prefix
        config.ma3_addr = step.addr_form
        if step.value in CMD_TEMPLATES:
            config.ma3_fader = "cmd"
            config.ma3_fader_cmd = CMD_TEMPLATES[step.value]
        else:
            config.ma3_fader = "osc"
            config.ma3_value = step.value
        return config
