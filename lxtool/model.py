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
    # Footprint stated by the source, used when it exceeds the channels we
    # could name: a personality may declare 16 slots but label only 12, and
    # losing the other four would shift everything patched after it.
    declared_count: int | None = None

    @property
    def channel_count(self) -> int:
        """Footprint in DMX slots, which is not the same as len(channels)
        when a personality leaves gaps or trailing slots unnamed."""
        highest = max((c.offset for c in self.channels), default=0)
        return max(highest, self.declared_count or 0)

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


# ---------------------------------------------------------------------------
# Patch level
#
# Everything above describes a fixture *type*. A show also needs the patch:
# which types are in the rig, how many, and at what addresses. That is what
# MVR carries, and it is the unit you actually move between desks.
# ---------------------------------------------------------------------------

@dataclass
class PatchedFixture:
    """One physical fixture in a rig, at an address."""

    name: str
    fixture: Fixture
    mode: str = ""
    fixture_id: str = ""
    universe: int = 1          # 1-based, as every desk displays it
    address: int = 1           # 1-512 within the universe
    layer: str = ""
    uuid: str = ""

    @property
    def footprint(self) -> int:
        m = self.fixture.mode(self.mode) if self.mode else None
        if m is None and self.fixture.modes:
            m = self.fixture.modes[0]
        return m.channel_count if m else 0

    @property
    def last_address(self) -> int:
        return self.address + max(self.footprint, 1) - 1

    @property
    def absolute_address(self) -> int:
        """Address counted continuously across universes, as MVR stores it."""
        return (self.universe - 1) * 512 + self.address

    def overlaps(self, other: "PatchedFixture") -> bool:
        if self.universe != other.universe:
            return False
        return self.address <= other.last_address and other.address <= self.last_address


@dataclass
class Rig:
    """A whole patch: the fixtures in a show and where they live."""

    name: str = ""
    fixtures: list[PatchedFixture] = field(default_factory=list)
    source: str = ""

    def types(self) -> list[Fixture]:
        """Distinct fixture types in the rig, in first-seen order."""
        seen: dict[str, Fixture] = {}
        for pf in self.fixtures:
            seen.setdefault(pf.fixture.key.lower(), pf.fixture)
        return list(seen.values())

    def by_universe(self) -> dict[int, list[PatchedFixture]]:
        out: dict[int, list[PatchedFixture]] = {}
        for pf in self.fixtures:
            out.setdefault(pf.universe, []).append(pf)
        for group in out.values():
            group.sort(key=lambda p: p.address)
        return dict(sorted(out.items()))

    def conflicts(self) -> list[tuple[PatchedFixture, PatchedFixture]]:
        """Pairs of fixtures whose DMX footprints overlap.

        Worth surfacing on import: a patch that was legal on one desk can
        collide on another if a mode's channel count differs between
        libraries.
        """
        clashes = []
        for group in self.by_universe().values():
            for i, a in enumerate(group):
                for b in group[i + 1:]:
                    if a.address > b.last_address:
                        break
                    if a.overlaps(b):
                        clashes.append((a, b))
        return clashes
