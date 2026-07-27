"""Multiple fixture libraries as one searchable set.

A tech rarely owns one library. The useful question is not "is this fixture
in my ChamSys library" but "which of my desks already has it, and what
differs between them" - so this loads every source that is present and hands
back one list, with each fixture tagged by where it came from.

Sources understood:

* a ChamSys ``heads`` folder - both the ``.hed`` files and ``heads.all``
* a grandMA3 ``lib_fixture_types/grandma3`` folder
* a folder of ``.gdtf`` files
* the cached Open Fixture Library
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .formats import chamsys, gdtf, ma3
from .model import Fixture

# Fixture.source values, used to label results.
SOURCE_LABELS = {
    "chamsys": "ChamSys",
    "ma3": "grandMA3",
    "ma2": "grandMA2",
    "gdtf": "GDTF",
    "ofl": "Open Fixture Library",
    "mvr": "MVR",
}


def label_for(source: str) -> str:
    return SOURCE_LABELS.get(source, source or "unknown")


@dataclass
class Library:
    """Every fixture we could load, and where each came from."""

    fixtures: list[Fixture] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.fixtures)

    @property
    def modes(self) -> int:
        return sum(len(f.modes) for f in self.fixtures)

    def counts(self) -> dict[str, int]:
        """Fixtures per source, for reporting what actually got loaded."""
        out: dict[str, int] = {}
        for f in self.fixtures:
            out[label_for(f.source)] = out.get(label_for(f.source), 0) + 1
        return dict(sorted(out.items()))


def signature(mode) -> tuple:
    """A mode's DMX fingerprint: what sits on each slot, in order.

    Two modes with the same signature are interchangeable at the desk
    whatever they are called, which is what makes this useful for finding
    redundancy in a library that has grown for twenty years.
    """
    return tuple(
        (c.attribute, c.fine)
        for c in sorted(mode.channels, key=lambda c: c.offset)
    )


@dataclass
class DuplicateGroup:
    """Modes that share a DMX fingerprint."""

    signature: tuple
    members: list[tuple[Fixture, object]] = field(default_factory=list)

    @property
    def channel_count(self) -> int:
        return self.members[0][1].channel_count if self.members else 0

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def names(self) -> list[str]:
        return [f"{fx.key} [{m.name}]" for fx, m in self.members]

    def redundant(self) -> bool:
        """True when this really is the same thing stored more than once.

        Sharing a layout is not enough. One fixture's effect modes - "M20
        Fire", "M20 Strobe" - are all 11 channels with identical attributes
        but are genuinely different modes, and calling those redundant would
        invite someone to delete working parts of their library. Real
        duplication means the same fixture *and* the same mode name.
        """
        seen = {(fx.key.lower(), m.name.strip().lower()) for fx, m in self.members}
        return len(seen) < len(self.members)

    def interchangeable(self) -> bool:
        """True when distinct fixtures share a layout - useful, not a problem."""
        return len({fx.key.lower() for fx, _ in self.members}) > 1


def find_duplicates(fixtures: list[Fixture], *, min_channels: int = 4) -> list[DuplicateGroup]:
    """Group every mode in the library by DMX fingerprint.

    Modes below ``min_channels`` are skipped: a 1-channel dimmer or a 3-channel
    RGB par is identical across hundreds of fixtures by definition, and
    reporting those would bury the findings that matter.
    """
    groups: dict[tuple, DuplicateGroup] = {}
    for fx in fixtures:
        for mode in fx.modes:
            if mode.channel_count < min_channels or not mode.channels:
                continue
            sig = signature(mode)
            if not sig:
                continue
            group = groups.get(sig)
            if group is None:
                group = groups[sig] = DuplicateGroup(signature=sig)
            group.members.append((fx, mode))

    dupes = [g for g in groups.values() if g.size > 1]
    dupes.sort(key=lambda g: (-g.size, -g.channel_count))
    return dupes


def detect_sources() -> list[Path]:
    """Fixture libraries present on this machine."""
    found = list(chamsys.find_heads_folders())
    found.extend(find_ma3_folders())
    return found


def find_ma3_folders() -> list[Path]:
    """grandMA3 fixture-type folders, across installed versions."""
    home = Path.home()
    roots = [
        home / "MALightingTechnology",
        home / "Documents/MALightingTechnology",
        Path("C:/ProgramData/MALightingTechnology"),
    ]

    found: list[Path] = []
    for root in roots:
        try:
            if not root.is_dir():
                continue
            # gma3_2.4.2/shared/lib_fixture_types/grandma3
            found.extend(
                p for p in root.glob("*/shared/lib_fixture_types/grandma3") if p.is_dir()
            )
        except OSError:
            continue
    return sorted(found)


def _load_folder(path: Path) -> tuple[list[Fixture], list[str]]:
    """Load whichever kind of library folder this is."""
    fixtures: list[Fixture] = []
    errors: list[str] = []

    heads = list(path.glob("*.hed"))
    container = path / "heads.all"
    if heads or container.is_file():
        try:
            fixtures.extend(chamsys.ChamSysLibrary.scan(path).as_fixtures())
        except Exception as exc:      # noqa: BLE001
            errors.append(f"{path}: {exc}")
        return fixtures, errors

    # Per file, and deliberately broad: a library is other people's data, and
    # one malformed fixture among twenty thousand must cost that fixture, not
    # the scan. zipfile.BadZipFile and ElementTree.ParseError are neither
    # OSError nor ValueError, so an explicit tuple would keep missing cases.
    for xml in sorted(path.glob("*.xml")):
        try:
            data = xml.read_bytes()
            if not ma3.looks_like_ma3(data):
                continue
            fx = ma3.parse(data)
            fx.source_id = xml.name
            fixtures.append(fx)
        except Exception as exc:      # noqa: BLE001
            errors.append(f"{xml.name}: {exc}")

    for g in sorted(path.glob("*.gdtf")):
        try:
            fixtures.append(gdtf.read(g))
        except Exception as exc:      # noqa: BLE001
            errors.append(f"{g.name}: {exc}")

    return fixtures, errors


def load(
    paths: list[Path | str] | None = None,
    *,
    include_ofl: bool = False,
    cache: Path | str | None = None,
) -> Library:
    """Load every requested library, plus auto-detected ones when none given.

    A source that fails to load is recorded in ``errors`` rather than raising,
    so one bad folder cannot take out a search across the others.
    """
    lib = Library()

    targets = [Path(p) for p in paths] if paths else detect_sources()
    for path in targets:
        if not path.is_dir():
            lib.errors.append(f"{path}: not a directory")
            continue
        fixtures, errors = _load_folder(path)
        lib.fixtures.extend(fixtures)
        lib.errors.extend(errors)

    if include_ofl:
        try:
            from .catalog import Catalog

            cat = Catalog.load(cache)
            # Per entry, so one malformed document costs one fixture rather
            # than silently truncating the whole catalogue.
            bad = 0
            for entry in cat.entries:
                try:
                    lib.fixtures.append(entry.to_fixture())
                except Exception:     # noqa: BLE001 - see note above
                    bad += 1
            if bad:
                lib.errors.append(f"Open Fixture Library: skipped {bad} unreadable fixture(s)")
        except FileNotFoundError:
            lib.errors.append("Open Fixture Library not cached - run 'lx fetch'")
        except (OSError, ValueError) as exc:
            lib.errors.append(f"Open Fixture Library: {exc}")

    return lib
