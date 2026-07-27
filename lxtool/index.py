"""A compact, fast-loading index of a fixture library.

Holding a big library as :class:`~lxtool.model.Fixture` objects is fine for a
handful of files and ruinous for a real one. A stock ChamSys library is
68,227 modes and 1.47 million channels; rebuilding those objects on every
command costs about 11 seconds before any work starts, which is most of what
made ``lx match`` feel slow.

Almost nothing needs the objects. Ranking, duplicate detection and library
listings need a mode's manufacturer, model, name, footprint, colour system
and attribute sequence - and the first three of those decide which handful
of candidates are worth looking at properly. So the index stores one flat
row per mode, with the per-channel detail packed into strings, and expands
to real objects only for the few candidates that survive ranking.

Measured on a stock library: 0.09s to load instead of 11.62s, and 31 MB on
disk instead of 82 MB.
"""

from __future__ import annotations

import pickle
from dataclasses import dataclass
from pathlib import Path

from . import attributes
from .model import Channel, Fixture, Mode

# Bumped when the row layout changes, so a stale cache is rebuilt rather than
# misread.
FORMAT_VERSION = 1

_SEP = "|"


@dataclass(frozen=True, order=False)
class ModeRow:
    """One mode, flattened. Per-channel data is packed until it is needed."""

    manufacturer: str
    model: str
    mode_name: str
    footprint: int
    colour: str
    source: str
    source_id: str
    _attrs: str
    _fines: str
    _names: str
    _offsets: str

    # -- cheap accessors, no unpacking -------------------------------------

    @property
    def key(self) -> str:
        return f"{self.manufacturer} {self.model}".strip()

    @property
    def label(self) -> str:
        return f"{self.key} [{self.mode_name}]"

    # -- unpacking, for the few rows that need it --------------------------

    def attributes(self) -> list[str]:
        return self._attrs.split(_SEP) if self._attrs else []

    def attribute_set(self) -> set[str]:
        return set(self.attributes())

    def signature(self) -> tuple:
        """The fingerprint used for duplicate detection."""
        attrs = self.attributes()
        fines = self._fines.split(_SEP) if self._fines else []
        return tuple(zip(attrs, (f == "1" for f in fines)))

    def to_mode(self) -> Mode:
        """Rebuild a real :class:`Mode`, for the candidates worth inspecting."""
        attrs = self.attributes()
        fines = self._fines.split(_SEP) if self._fines else []
        names = self._names.split(_SEP) if self._names else []
        offsets = self._offsets.split(_SEP) if self._offsets else []

        channels = [
            Channel(
                offset=int(offsets[i]) if i < len(offsets) else i + 1,
                name=names[i] if i < len(names) else attrs[i],
                attribute=attrs[i],
                fine=i < len(fines) and fines[i] == "1",
            )
            for i in range(len(attrs))
        ]
        mode = Mode(name=self.mode_name, channels=channels)
        if self.footprint > (max((c.offset for c in channels), default=0)):
            mode.declared_count = self.footprint
        return mode

    def to_fixture(self) -> Fixture:
        return Fixture(
            manufacturer=self.manufacturer,
            model=self.model,
            modes=[self.to_mode()],
            source=self.source,
            source_id=self.source_id,
        )


def row_for(fixture: Fixture, mode: Mode) -> ModeRow:
    channels = sorted(mode.channels, key=lambda c: c.offset)
    return ModeRow(
        manufacturer=fixture.manufacturer,
        model=fixture.model,
        mode_name=mode.name,
        footprint=mode.channel_count,
        colour=attributes.colour_system(mode.attribute_set()),
        source=fixture.source,
        source_id=fixture.source_id,
        _attrs=_SEP.join(c.attribute for c in channels),
        _fines=_SEP.join("1" if c.fine else "0" for c in channels),
        # A channel name containing the separator would corrupt the split, and
        # real libraries do contain odd names, so neutralise it.
        _names=_SEP.join(c.name.replace(_SEP, "/") for c in channels),
        _offsets=_SEP.join(str(c.offset) for c in channels),
    )


def build(fixtures: list[Fixture]) -> list[ModeRow]:
    return [row_for(f, m) for f in fixtures for m in f.modes]


def save(rows: list[ModeRow], path: Path | str) -> Path:
    """Write the index, atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": FORMAT_VERSION,
        # Plain tuples pickle and unpickle far faster than dataclasses.
        "rows": [_astuple(r) for r in rows],
    }
    tmp = path.with_suffix(".part")
    with tmp.open("wb") as fh:
        pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)
    return path


def _astuple(row: ModeRow) -> tuple:
    return (row.manufacturer, row.model, row.mode_name, row.footprint,
            row.colour, row.source, row.source_id,
            row._attrs, row._fines, row._names, row._offsets)


def load(path: Path | str) -> list[ModeRow] | None:
    """Read an index, or None when it is absent, stale or unreadable."""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        with path.open("rb") as fh:
            payload = pickle.load(fh)
    except (pickle.UnpicklingError, EOFError, AttributeError, ImportError, OSError):
        return None

    if not isinstance(payload, dict) or payload.get("version") != FORMAT_VERSION:
        return None
    return [ModeRow(*row) for row in payload.get("rows", [])]
