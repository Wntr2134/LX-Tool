"""Find the OSC dialect a grandMA3 actually wants.

MA3's OSC page has three cells that silently decide whether a message is
heard at all, and no two sources agree on what to put in them:

* **Prefix.** The manual says "if a prefix is specified, only OSC messages
  beginning with the specified prefix are processed" - so a wrong prefix
  is not an error, it is silence. MA's shipped templates use ``gma3``;
  plenty of rigs clear it.
* **Value type.** The manual documents an integer 0-100. MA's own Open
  Stage Control worked example sends a float 0-100. Some builds want the
  0-1 unit float.
* **FaderRange.** Set to 255 instead of 100, the same fader wants 0-255.

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

# Every plausible reading of the documentation, worst-to-best ordered so
# the most likely dialect (MA's own example) is tried first.
DIALECTS: tuple[tuple[str, str], ...] = (
    ("gma3", "float100"),   # MA's Open Stage Control example
    ("gma3", "int100"),     # the manual's stated type
    ("", "float100"),       # prefix cleared on the OSC page
    ("", "int100"),
    ("gma3", "int255"),     # FaderRange 255
    ("", "int255"),
    ("gma3", "float01"),    # 0-1 unit float
    ("", "float01"),
)


@dataclass
class Step:
    """One dialect, and the exact message it puts on the wire."""

    index: int
    prefix: str
    value: str
    address: str = ""
    args: tuple = ()

    @property
    def label(self) -> str:
        pfx = f"/{self.prefix}" if self.prefix else "(no prefix)"
        return f"{self.index + 1}. {pfx} + {self.value}"

    @property
    def line(self) -> str:
        return f"{self.address} {' '.join(str(a) for a in self.args)}".rstrip()

    def as_dict(self) -> dict:
        return {"index": self.index, "prefix": self.prefix,
                "value": self.value, "label": self.label, "sent": self.line}


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
            self.steps = [self._build(i, p, v)
                          for i, (p, v) in enumerate(DIALECTS)]

    def _build(self, index: int, prefix: str, value: str) -> Step:
        """Ask the real target to format it - the probe must not carry a
        second, drifting copy of the addressing rules."""
        cfg = Config(target="ma3", prefix=prefix, ma3_value=value,
                     page=self.page, fader_execs=(self.exec_,))
        out = make_target(cfg).fader(0, self.level)
        step = Step(index=index, prefix=prefix, value=value)
        for m in out:
            if isinstance(m, osc.Message):
                step.address, step.args = m.address, tuple(m.args)
                break
        return step

    def datagram(self, index: int) -> bytes:
        s = self.steps[index]
        return osc.encode(osc.Message(s.address, s.args))

    def run(self, *, dwell: float = 2.0, on_step=None,
            sock=None, sleep=time.sleep) -> list[Step]:
        """Send each dialect in turn, pausing so a human can watch.

        ``on_step`` is called with each Step as it goes out, so a UI can
        show "watch executor 201 now" in step with the wire.
        """
        own = sock is None
        sock = sock or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for step in self.steps:
                if not step.address:
                    continue
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
        config.ma3_value = step.value
        return config
