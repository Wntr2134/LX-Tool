"""Canonical fixture model.

Every supported format (GDTF, Open Fixture Library, grandMA2 XML, ChamSys) is
parsed into these types, and written back out from them.  Keeping one neutral
representation in the middle means adding format N+1 costs one reader and one
writer rather than N converters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class Range:
    """A named DMX range within a channel, e.g. gobo 3 or 'shutter open'."""

    dmx_from: int
    dmx_to: int
    name: str

    def contains(self, value: int) -> bool:
        return self.dmx_from <= value <= self.dmx_to


@dataclass
class Channel:
    """One DMX slot within a mode.

    ``offset`` is 1-based, matching how every desk in the world numbers
    patch offsets.  A 16-bit parameter is represented as two channels sharing
    an ``attribute`` where the fine one has ``fine=True``.
    """

    offset: int
    name: str
    attribute: str
    fine: bool = False
    default: int = 0
    highlight: int | None = None
    htp: bool = False
    invert: bool = False
    ranges: list[Range] = field(default_factory=list)

    @property
    def resolution(self) -> str:
        return "16bit" if self.fine else "8bit"


@dataclass
class Mode:
    """A DMX personality: an ordered set of channels."""

    name: str
    channels: list[Channel] = field(default_factory=list)

    @property
    def channel_count(self) -> int:
        """Footprint in DMX slots, which is not the same as len(channels)
        when a personality leaves gaps."""
        if not self.channels:
            return 0
        return max(c.offset for c in self.channels)

    def by_offset(self) -> dict[int, Channel]:
        return {c.offset: c for c in self.channels}

    def attributes(self) -> list[str]:
        """Coarse attributes in patch order, fine channels folded away."""
        return [c.attribute for c in sorted(self.channels, key=lambda c: c.offset) if not c.fine]

    def attribute_set(self) -> set[str]:
        return {c.attribute for c in self.channels if not c.fine}


@dataclass
class Fixture:
    manufacturer: str
    model: str
    modes: list[Mode] = field(default_factory=list)
    source: str = ""          # which format/file this came from
    source_id: str = ""       # native identifier, e.g. the .hed filename
    fixture_type: str = ""    # 'Moving Head', 'LED Par', ... when the source says

    @property
    def key(self) -> str:
        return f"{self.manufacturer} {self.model}".strip()

    def mode(self, name: str) -> Mode | None:
        for m in self.modes:
            if m.name.lower() == name.lower():
                return m
        return None

    def mode_with_count(self, count: int) -> Mode | None:
        for m in self.modes:
            if m.channel_count == count:
                return m
        return None


def flatten(fixtures: Iterable[Fixture]) -> list[tuple[Fixture, Mode]]:
    """Every (fixture, mode) pair - the unit that actually gets patched."""
    return [(f, m) for f in fixtures for m in f.modes]
